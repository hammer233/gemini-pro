"""冒烟测试：用 mock 后端拉起真实服务，不访问外网、不需要浏览器。

运行：python -m unittest discover -s tests -v
依赖：fastapi、uvicorn（requirements.txt 已包含）。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
ADMIN_KEY = "k-admin-" + uuid.uuid4().hex
TENANT_KEY = "k-alice-" + uuid.uuid4().hex


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, store: dict | None = None, **env):
        self.tmp_dir = os.path.join(tempfile.gettempdir(), "gp-test-" + uuid.uuid4().hex)
        os.makedirs(self.tmp_dir)
        if store is not None:
            os.makedirs(os.path.join(self.tmp_dir, "data"))
            with open(os.path.join(self.tmp_dir, "data", "settings.json"),
                      "w", encoding="utf-8") as f:
                json.dump(store, f)
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        full = dict(os.environ)
        full.update({
            "BACKEND": "mock",
            "MOCK_LATENCY": "0.05-0.1",
            "MOCK_FAIL_RATE": "0",
            "MIN_WORKERS": "1",
            "DATA_DIR": os.path.join(self.tmp_dir, "data"),
            "STATE_DIR": os.path.join(self.tmp_dir, "states"),
            "API_KEY": "",
            "USERS": "",
        })
        full.update(env)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "proxy_svc.app:app",
             "--host", "127.0.0.1", "--port", str(self.port),
             "--log-level", "warning"],
            cwd=ROOT, env=full,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self._wait_ready()

    def _wait_ready(self) -> None:
        end = time.time() + 30
        while time.time() < end:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read().decode("utf-8", "replace")
                raise RuntimeError("服务启动失败:\n" + out)
            try:
                urllib.request.urlopen(self.base + "/health", timeout=1).read()
                return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("服务启动超时")

    def call(self, method: str, path: str, body=None, key: str | None = None,
             headers: dict | None = None, raw: bytes | None = None):
        data = raw if raw is not None else (
            json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if key:
            req.add_header("Authorization", "Bearer " + key)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read().decode("utf-8", "replace")
            finally:
                e.close()

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        if self.proc.stdout:
            self.proc.stdout.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)


CHAT = {"model": "Gemini5.0", "messages": [{"role": "user", "content": "hi"}]}


class AuthEnabled(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Server(API_KEY=ADMIN_KEY,
                         USERS=f"alice:{TENANT_KEY}:2:10",
                         MAX_BODY_BYTES="100000")

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_missing_and_wrong_key_rejected(self):
        self.assertEqual(self.srv.call("GET", "/v1/models")[0], 401)
        self.assertEqual(self.srv.call("GET", "/v1/models", key="nope")[0], 401)

    def test_non_ascii_key_is_401_not_500(self):
        status, _ = self.srv.call(
            "GET", "/v1/models", headers={"Authorization": "Bearer caf\xe9"})
        self.assertEqual(status, 401)

    def test_chat_ok_and_openai_user_field_ignored_for_tenant(self):
        for key in (ADMIN_KEY, TENANT_KEY):
            body = dict(CHAT, user="end-user-42")
            status, text = self.srv.call("POST", "/v1/chat/completions",
                                         body, key=key)
            self.assertEqual(status, 200, text)
            self.assertIn("[mock]", json.loads(text)["choices"][0]["message"]["content"])

    def test_stream_ends_with_done(self):
        status, text = self.srv.call(
            "POST", "/v1/chat/completions", dict(CHAT, stream=True), key=TENANT_KEY)
        self.assertEqual(status, 200)
        self.assertTrue(text.rstrip().endswith("data: [DONE]"))

    def test_bootstrap_disabled(self):
        self.assertEqual(self.srv.call("POST", "/admin/bootstrap")[0], 403)

    def test_disabled_tenant_key_is_rejected(self):
        status, text = self.srv.call(
            "POST", "/admin/keys", {"user_id": "carol"}, key=ADMIN_KEY)
        self.assertEqual(status, 200, text)
        carol_key = json.loads(text)["key"]
        self.assertEqual(self.srv.call("GET", "/v1/models", key=carol_key)[0], 200)
        status, _ = self.srv.call(
            "PATCH", "/admin/keys/carol", {"disabled": True}, key=ADMIN_KEY)
        self.assertEqual(status, 200)
        self.assertEqual(self.srv.call("GET", "/v1/models", key=carol_key)[0], 401)

    def test_tenant_cannot_use_admin_endpoints(self):
        self.assertEqual(
            self.srv.call("GET", "/admin/settings", key=TENANT_KEY)[0], 403)
        self.assertEqual(
            self.srv.call("GET", "/admin/settings", key=ADMIN_KEY)[0], 200)

    def test_health_is_minimal_for_anonymous(self):
        _, anon = self.srv.call("GET", "/health")
        _, admin = self.srv.call("GET", "/health", key=ADMIN_KEY)
        anon, admin = json.loads(anon), json.loads(admin)
        self.assertNotIn("process", anon)
        self.assertNotIn("users", anon)
        self.assertIn("process", admin)

    def test_timeout_is_bounded(self):
        status, _ = self.srv.call(
            "POST", "/ask", {"question": "x", "timeout": 1e9}, key=ADMIN_KEY)
        self.assertEqual(status, 422)
        status, _ = self.srv.call(
            "POST", "/ask", {"question": "x", "timeout": 5}, key=ADMIN_KEY)
        self.assertEqual(status, 200)

    def test_batch_rejects_oversized_question(self):
        status, _ = self.srv.call(
            "POST", "/ask/batch", {"questions": ["a" * 60001]}, key=ADMIN_KEY)
        self.assertEqual(status, 400)

    def test_body_limit(self):
        status, _ = self.srv.call(
            "POST", "/ask", {"question": "a" * 150000}, key=ADMIN_KEY)
        self.assertEqual(status, 413)

    def test_asset_path_traversal_blocked(self):
        for path in ("/assets/..%2f..%2fapp.py", "/assets/../../app.py",
                     "/assets/%2e%2e/%2e%2e/config.py"):
            self.assertEqual(self.srv.call("GET", path)[0], 404, path)

    @unittest.skipIf(os.name == "nt", "POSIX 文件权限")
    def test_settings_file_is_private(self):
        self.srv.call("POST", "/admin/keys", {"user_id": "bob"}, key=ADMIN_KEY)
        path = os.path.join(self.srv.tmp_dir, "data", "settings.json")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_env_admin_key_is_not_duplicated_into_settings_json(self):
        self.srv.call("POST", "/admin/keys", {"user_id": "dave"}, key=ADMIN_KEY)
        path = os.path.join(self.srv.tmp_dir, "data", "settings.json")
        with open(path, encoding="utf-8") as f:
            self.assertNotIn(ADMIN_KEY, f.read())


class EnvKeyBeatsStoredKey(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Server(store={"api_key": "stale-key-from-settings-json"},
                         API_KEY=ADMIN_KEY)

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_env_api_key_wins(self):
        self.assertEqual(
            self.srv.call("GET", "/admin/settings", key=ADMIN_KEY)[0], 200)
        self.assertEqual(
            self.srv.call("GET", "/admin/settings", key="stale-key-from-settings-json")[0], 401)

    def test_corrupt_store_does_not_block_startup(self):
        srv = Server(store={"users": [{"user_id": "x", "key": "k",
                                       "max_concurrent": "not-a-number"}]},
                     API_KEY=ADMIN_KEY)
        try:
            self.assertEqual(srv.call("GET", "/v1/models", key=ADMIN_KEY)[0], 200)
        finally:
            srv.stop()


class ConfigUnit(unittest.TestCase):
    def test_single_key_is_admin_and_config_is_valid(self):
        env = {"API_KEY": ADMIN_KEY, "USERS": "", "BACKEND": "mock"}
        with mock.patch.dict(os.environ, env):
            from proxy_svc.config import Config
            cfg = Config()
        self.assertEqual(cfg.whois(ADMIN_KEY), ("default", True))
        self.assertEqual(cfg.validate(), [])

    def test_separate_admin_and_tenant_keys(self):
        env = {"API_KEY": ADMIN_KEY, "BACKEND": "mock",
               "USERS": f"alice:{TENANT_KEY}:2:10"}
        with mock.patch.dict(os.environ, env):
            from proxy_svc.config import Config
            cfg = Config()
        self.assertEqual(cfg.whois(TENANT_KEY), ("alice", False))
        self.assertTrue(cfg.whois(ADMIN_KEY)[1])
        self.assertIsNone(cfg.whois("caf\xe9"))


class AuthMissingServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_fails_closed_without_key(self):
        self.assertEqual(self.srv.call("GET", "/v1/models")[0], 503)
        self.assertEqual(
            self.srv.call("POST", "/v1/chat/completions", CHAT)[0], 503)
        self.assertEqual(self.srv.call("GET", "/admin/settings")[0], 503)

    def test_bootstrap_cannot_mint_admin_key(self):
        self.assertEqual(self.srv.call("POST", "/admin/bootstrap")[0], 403)

    def test_health_stays_minimal(self):
        _, text = self.srv.call("GET", "/health")
        self.assertNotIn("process", json.loads(text))


class NoAuthOptIn(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Server(ALLOW_NO_AUTH="1")

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_opt_in_allows_requests(self):
        status, _ = self.srv.call("POST", "/v1/chat/completions", CHAT)
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
