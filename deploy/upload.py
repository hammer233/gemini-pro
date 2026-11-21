"""把前后端上传到 /opt/gemini-pro，不覆盖服务器上的 data 和 states。"""
import os
import sys

import paramiko

host = os.environ["DEPLOY_HOST"]
user = os.environ["DEPLOY_USER"]
password = os.environ["DEPLOY_PASS"]
root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

files = [
    "requirements.txt",
    "README.md",
    "proxy_svc/__init__.py",
    "proxy_svc/app.py",
    "proxy_svc/config.py",
    "proxy_svc/fingerprint.py",
    "proxy_svc/metrics.py",
    "proxy_svc/run.sh",
    "proxy_svc/solver.py",
    "proxy_svc/worker.py",
    "proxy_svc/backends/__init__.py",
    "proxy_svc/backends/base.py",
    "proxy_svc/backends/browser.py",
    "proxy_svc/backends/mock.py",
    "proxy_svc/core/__init__.py",
    "proxy_svc/core/fair_queue.py",
    "proxy_svc/core/pool.py",
    "proxy_svc/core/soak.py",
    "proxy_svc/core/sysinfo.py",
    "proxy_svc/core/tasks.py",
    "proxy_svc/core/tokens.py",
    "proxy_svc/.env.example",
    "deploy/gemini-pro.service",
    "deploy/proxy.env",
    "deploy/Caddyfile.snippet",
    "deploy/install_chrome.sh",
    "deploy/check_deps.py",
]

static_root = os.path.join(root, "proxy_svc", "static")
for dirpath, _, filenames in os.walk(static_root):
    for fn in filenames:
        rel_static = os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/")
        if rel_static not in files:
            files.append(rel_static)

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(host, username=user, password=password, timeout=20,
            allow_agent=False, look_for_keys=False)
sftp = cli.open_sftp()


def mkdir(path):
    parts = path.strip("/").split("/")
    cur = ""
    for part in parts:
        cur += "/" + part
        try:
            sftp.stat(cur)
        except FileNotFoundError:
            sftp.mkdir(cur)


for rel in files:
    local = os.path.join(root, rel.replace("/", os.sep))
    remote = "/opt/gemini-pro/" + rel
    mkdir(os.path.dirname(remote))
    sftp.put(local, remote)
    print("put", rel, os.path.getsize(local))

sftp.close()
cli.close()
print("uploaded", len(files))
