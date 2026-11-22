"""服务配置：全部走环境变量，无硬编码路径，Linux/Windows 通用。

多租户
- USERS="alice:key1:2:20, bob:key2:4:40"
  格式 用户:密钥[:同时在跑的上限[:该用户排队上限]]
- USERS 为空时退化为单租户：无 API_KEY -> public；有 API_KEY -> default。
- API_KEY 仍然有效，作为管理员密钥：可以用请求体的 user_id 指定租户，
  也可以调用 /stats?all=1 看全部租户。
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("config")


def machine_capacity(reserve_mb: int = 500, per_browser_mb: int = 280,
                     cpu_multiplier: int = 5) -> dict:
    """按这台独占机器的 CPU 和内存，算最多能安全并发几个瘦身后的浏览器。

    开启 --renderer-process-limit=1 与 --disable-site-isolation-trials 后，
    单个 headless Chrome 常驻内存约 250~280MB，且 90% 时间处于网络 I/O 等待，
    CPU 侧按核数的 5 倍封顶（2 核最高可开 10 个实例），内存留 500MB 给系统与 Python。
    """
    cpus = os.cpu_count() or 2
    free_mb = None
    total_mb = None
    try:
        if os.name == "nt":
            import ctypes
            class M(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = M()
            m.dwLength = ctypes.sizeof(M)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                free_mb = int(m.ullAvailPhys // (1024 * 1024))
                total_mb = int(m.ullTotalPhys // (1024 * 1024))
        else:
            avail = 0
            tot = 0
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        avail = int(line.split()[1])  # kB
                    elif line.startswith("MemTotal:"):
                        tot = int(line.split()[1])
            if avail:
                free_mb = avail // 1024
            if tot:
                total_mb = tot // 1024
    except Exception:
        free_mb = None

    by_cpu = max(4, cpus * cpu_multiplier)
    budget_mb = max(free_mb or 0, (total_mb - 350) if total_mb else 0)
    if not budget_mb:
        cap = min(by_cpu, 4)
    else:
        by_mem = max(1, (budget_mb - reserve_mb) // per_browser_mb)
        cap = max(1, min(by_cpu, by_mem))
    return {"cpus": cpus, "free_mb": free_mb, "total_mb": total_mb,
            "cap": int(cap), "per_browser_mb": per_browser_mb,
            "reserve_mb": reserve_mb}


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _range(name: str, default: tuple[float, float]) -> tuple[float, float]:
    """解析 "1.5-4" 这类区间。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        a, _, b = raw.partition("-")
        lo = float(a)
        hi = float(b) if b else lo
        return (min(lo, hi), max(lo, hi))
    except ValueError:
        return default


def _list(name: str) -> list[str]:
    return [p.strip() for p in (os.environ.get(name) or "").split(",")
            if p.strip()]


@dataclass
class TenantCfg:
    """一个调用方的隔离配置。

    max_concurrent 同时覆盖“在跑”和“在排队”，超出即拒绝：
    配额按租户划分，超限的请求直接拒绝，而不是无界排队把池子拖垮。
    """
    user_id: str
    key: str = ""
    max_concurrent: int = 2
    queue_limit: int = 20
    disabled: bool = False
    note: str = ""
    created: float = 0.0

    def as_dict(self) -> dict:
        return {"user_id": self.user_id,
                "key": ("***" if self.key else ""),
                "max_concurrent": self.max_concurrent,
                "queue_limit": self.queue_limit,
                "disabled": self.disabled,
                "note": self.note}

    def full(self) -> dict:
        return {"user_id": self.user_id, "key": self.key,
                "max_concurrent": self.max_concurrent,
                "queue_limit": self.queue_limit,
                "disabled": self.disabled, "note": self.note,
                "created": self.created}


def _parse_tenants(raw: str, max_workers: int,
                   queue_limit: int) -> dict[str, TenantCfg]:
    out: dict[str, TenantCfg] = {}
    for item in (raw or "").replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        parts = [p.strip() for p in item.split(":")]
        uid = parts[0]
        if not uid:
            continue
        key = parts[1] if len(parts) > 1 else ""
        try:
            mc = int(parts[2]) if len(parts) > 2 and parts[2] else max_workers
        except ValueError:
            mc = max_workers
        try:
            ql = int(parts[3]) if len(parts) > 3 and parts[3] else queue_limit
        except ValueError:
            ql = queue_limit
        out[uid] = TenantCfg(uid, key, max(1, mc), max(1, ql))
    return out


@dataclass
class Config:
    # ---- 服务监听 ----
    host: str = field(default_factory=lambda: os.environ.get("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _i("PORT", 8888))
    api_key: str = field(default_factory=lambda: os.environ.get("API_KEY", ""))

    # ---- 多租户（用户:密钥:并发:排队）----
    users_raw: str = field(default_factory=lambda: os.environ.get("USERS", ""))

    # ---- 出口代理（服务自身走它出网）----
    egress_proxy: str = field(
        default_factory=lambda: os.environ.get("EGRESS_PROXY", ""))

    # ---- 打码 ----
    yes_key: str = field(default_factory=lambda: os.environ.get("YES_KEY", ""))
    yes_api: str = field(default_factory=lambda: os.environ.get(
        "YES_API", "https://api.yescaptcha.com"))
    solve_timeout: float = field(default_factory=lambda: _f("SOLVE_TIMEOUT", 180))

    # ---- 伸缩 ----
    # MAX_WORKERS 不设置时按本机 CPU 和空闲内存自动封顶。
    # 显式设成正数则用那个数，但仍然不会超过机器容量；设成 0 表示不起浏览器。
    min_workers: int = field(default_factory=lambda: _i("MIN_WORKERS", 1))
    max_workers: int = field(default_factory=lambda: _i(
        "MAX_WORKERS", -1))
    max_workers_set: bool = field(default_factory=lambda: bool(
        os.environ.get("MAX_WORKERS")))
    capacity: dict = field(default_factory=dict)
    queue_limit: int = field(default_factory=lambda: _i("QUEUE_LIMIT", 200))
    idle_ttl: float = field(default_factory=lambda: _f("IDLE_TTL", 300))

    # ---- 身份轮换 ----
    # 一个身份（指纹+cookie）服务多少次请求后重建
    max_requests_per_identity: int = field(
        default_factory=lambda: _i("MAX_REQUESTS_PER_IDENTITY", 50))
    # 连续多少次被拦就强制换身份
    block_streak_limit: int = field(
        default_factory=lambda: _i("BLOCK_STREAK_LIMIT", 2))
    # 换租户时是否整体换身份（新指纹+新 cookie）；默认关掉只换页面（直接 goto 更快，适合无状态 OpenAI 调用）
    tenant_rotate_identity: bool = field(
        default_factory=lambda: _b("TENANT_ROTATE_IDENTITY", False))

    # ---- 取答案 ----
    ready_quiet_s: float = field(default_factory=lambda: _f("READY_QUIET", 1.5))
    answer_timeout: float = field(default_factory=lambda: _f("ANSWER_TIMEOUT", 120))
    goto_timeout: float = field(default_factory=lambda: _f("GOTO_TIMEOUT", 45))

    # ---- 浏览器 ----
    headless: bool = field(default_factory=lambda: _b("HEADLESS", True))
    chrome_path: str = field(default_factory=lambda: os.environ.get(
        "CHROME_PATH", ""))          # 空 = 用 playwright 自带 chromium
    state_dir: str = field(default_factory=lambda: os.environ.get(
        "STATE_DIR", os.path.join(os.getcwd(), "states")))
    block_assets: bool = field(default_factory=lambda: _b("BLOCK_ASSETS", True))
    # ---- 取答案层:网络响应流旁路(原始 Markdown) ----
    # NET_CAPTURE: 被动监听 page response,抓取承载答案文本的接口原始数据。
    #   只旁路不拦截(不做 route+fulfill),因此不影响页面本身的渲染与流式输出。
    net_capture: bool = field(default_factory=lambda: _b("NET_CAPTURE", True))
    # NET_MARKDOWN: 命中网络原文时优先返回原文(保留 ``` 代码块缩进),
    #   关闭则只用网络活动降低 DOM 轮询频率,答案仍从 DOM 取。
    net_markdown: bool = field(default_factory=lambda: _b("NET_MARKDOWN", True))
    # NET_DEBUG: 把命中的原始载荷 dump 到 {state_dir}/netdebug/,用于换站点/换版本时定标。
    net_debug: bool = field(default_factory=lambda: _b("NET_DEBUG", False))

    # ---- 面板可改、落盘的运行时配置 ----
    data_dir: str = field(default_factory=lambda: os.environ.get(
        "DATA_DIR", os.path.join(os.getcwd(), "data")))

    # ---- 后端：browser（真实浏览器）/ mock（模拟，不出网）----
    backend: str = field(default_factory=lambda: (
        os.environ.get("BACKEND") or "browser").strip().lower())
    mock_latency: tuple = field(default_factory=lambda: _range(
        "MOCK_LATENCY", (1.5, 4.0)))
    mock_fail_rate: float = field(default_factory=lambda: _f(
        "MOCK_FAIL_RATE", 0.02))
    mock_answer_chars: tuple = field(default_factory=lambda: tuple(
        int(x) for x in _range("MOCK_ANSWER_CHARS", (200, 1200))))
    mock_max_workers: int = field(default_factory=lambda: _i(
        "MOCK_MAX_WORKERS", 32))

    # ---- 网络 / 统计 ----
    # 反向代理（Caddy/Nginx）地址，只有来自这些地址的 X-Forwarded-For 才可信。
    # 逗号分隔的 IP 或网段；"*" 表示全部信任（只在确定前面一定有代理时用）。
    trusted_proxies: list = field(default_factory=lambda: _list(
        "TRUSTED_PROXIES") or ["127.0.0.1", "::1"])
    # 最近请求列表里保留问题前多少个字，0 表示不保留内容
    metrics_preview_chars: int = field(default_factory=lambda: _i(
        "METRICS_PREVIEW_CHARS", 80))
    # wait=false 提交的任务，结果保留多少秒可供查询
    task_ttl: float = field(default_factory=lambda: _f("TASK_TTL", 600))
    # 冒烟测试单个任务最大并发
    soak_max_concurrency: int = field(default_factory=lambda: _i(
        "SOAK_MAX_CONCURRENCY", 64))

    # ---- 安全开关 ----
    # 未配置任何密钥时默认拒绝服务；ALLOW_NO_AUTH=1 才允许无鉴权运行（仅限本机/内网调试）
    allow_no_auth: bool = field(default_factory=lambda: _b("ALLOW_NO_AUTH", False))
    # /admin/bootstrap 会向任何访问者签发管理员密钥，默认关闭
    allow_bootstrap: bool = field(
        default_factory=lambda: _b("ALLOW_BOOTSTRAP", False))
    max_body_bytes: int = field(
        default_factory=lambda: _i("MAX_BODY_BYTES", 8 * 1024 * 1024))

    # ---- 派生（__post_init__ 填充）----
    tenants: dict[str, TenantCfg] = field(default_factory=dict)
    default_user: str = "public"
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _auto_default: bool = False

    def __post_init__(self):
        self.capacity = machine_capacity()
        if self.backend == "mock":
            # 模拟后端不起浏览器，容量按 MOCK_MAX_WORKERS 算
            self.capacity = {**self.capacity, "cap": max(1, self.mock_max_workers),
                             "mode": "mock"}
        cap = self.capacity["cap"]
        if not self.max_workers_set:
            self.max_workers = cap
        elif self.max_workers > 0:
            self.max_workers = min(self.max_workers, cap)
        self.min_workers = max(0, min(self.min_workers, max(self.max_workers, 0)))
        self.tenants = _parse_tenants(self.users_raw, self.max_workers,
                                      self.queue_limit)
        if not self.tenants:
            uid = "default" if self.api_key else "public"
            self.tenants = {uid: TenantCfg(uid, self.api_key,
                                           max(1, self.max_workers),
                                           max(1, self.queue_limit))}
            self._auto_default = True
        self.default_user = next(iter(self.tenants))

    # ---- 身份解析 ----
    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key) or any(t.key for t in self.tenants.values())

    def whois(self, key: str | None) -> tuple[str, bool] | None:
        """密钥 -> (user_id, 是否管理员)。None 表示密钥无效/未提供。"""
        if not key:
            return None
        probe = key.encode("utf-8", "replace")
        if self.api_key and hmac.compare_digest(
                self.api_key.encode("utf-8"), probe):
            return (self.default_user, True)
        for t in self.tenants.values():
            if (t.key and not t.disabled
                    and hmac.compare_digest(t.key.encode("utf-8"), probe)):
                return (t.user_id, False)
        return None

    def tenant(self, user_id: str) -> TenantCfg:
        t = self.tenants.get(user_id)
        if t is None:
            raise KeyError(user_id)
        return t

    def validate(self) -> list[str]:
        errs = []
        if self.max_workers < 0:
            errs.append("MAX_WORKERS must be >= 0")
        if self.min_workers > self.max_workers:
            errs.append("MIN_WORKERS must be <= MAX_WORKERS")
        if self.backend not in ("browser", "mock"):
            errs.append(f"BACKEND 只能是 browser / mock，当前 {self.backend}")
        if self.backend == "browser" and not self.yes_key:
            errs.append("YES_KEY is required (YesCaptcha client key)")
        keys: dict[str, str] = {}
        for t in self.tenants.values():
            if t.max_concurrent < 1:
                errs.append(f"USERS[{t.user_id}] max_concurrent must be >= 1")
            if t.queue_limit < 1:
                errs.append(f"USERS[{t.user_id}] queue_limit must be >= 1")
            if self.users_raw and not t.key:
                errs.append(f"USERS[{t.user_id}] missing api key")
            if t.key:
                if t.key in keys:
                    errs.append(f"USERS 密钥重复: {t.user_id} 与 {keys[t.key]}")
                keys[t.key] = t.user_id
        if self.api_key and not self._auto_default and self.api_key in keys:
            errs.append("API_KEY 与某个 USERS 密钥重复")
        return errs

    # ---- 面板运行时配置：改完立刻生效并落盘 ----
    def _store_path(self) -> str:
        return os.path.join(self.data_dir, "settings.json")

    def load_store(self) -> None:
        """启动时读面板保存的密钥和设置，覆盖环境变量里的同名项。"""
        path = self._store_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        with self._lock:
            try:
                self._apply_store(data)
            except Exception as e:
                log.error("settings.json 内容无法解析，已忽略: %s", e)

    def _apply_store(self, data: dict) -> None:
        users = data.get("users")
        if isinstance(users, list):
            parsed: dict[str, TenantCfg] = {}
            for item in users:
                if not isinstance(item, dict):
                    continue
                uid = str(item.get("user_id") or "").strip()
                key = str(item.get("key") or "").strip()
                if not uid or not key:
                    continue
                mc = int(item.get("max_concurrent") or self.max_workers)
                ql = int(item.get("queue_limit") or self.queue_limit)
                parsed[uid] = TenantCfg(
                    uid, key, max(1, mc), max(1, ql),
                    disabled=bool(item.get("disabled", False)),
                    note=str(item.get("note") or "")[:200],
                    created=float(item.get("created") or 0))
            if parsed:
                self.tenants = parsed
                self.default_user = next(iter(parsed))
                self._auto_default = False
        if (isinstance(data.get("api_key"), str) and data["api_key"]
                and not self.api_key):
            self.api_key = data["api_key"]
        self._apply_limits(data)

    @staticmethod
    def _pair(v, lo: float, hi: float, cast=float):
        if isinstance(v, str):
            a, _, b = v.partition("-")
            v = [a, b or a]
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise ValueError
        a, b = (max(lo, min(hi, cast(x))) for x in v)
        return (min(a, b), max(a, b))

    def _apply_limits(self, data: dict) -> None:
        if "backend" in data:
            b = str(data["backend"] or "").strip().lower()
            if b in ("browser", "mock") and b != self.backend:
                self.backend = b
                base_cap = machine_capacity()
                if self.backend == "mock":
                    self.capacity = {**base_cap, "cap": max(1, self.mock_max_workers),
                                     "mode": "mock"}
                else:
                    self.capacity = base_cap
                    self.max_workers = min(self.max_workers, base_cap["cap"])
        cap = self.capacity["cap"]
        ints = {
            "min_workers": (0, cap),
            "max_workers": (1, cap),
            "queue_limit": (1, 100000),
            "max_requests_per_identity": (1, 100000),
            "block_streak_limit": (1, 100),
            "mock_max_workers": (1, 256),
            "metrics_preview_chars": (0, 2000),
        }
        floats = {
            "idle_ttl": (10, 86400),
            "answer_timeout": (10, 600),
            "goto_timeout": (5, 180),
            "ready_quiet_s": (0.2, 30),
            "mock_fail_rate": (0, 1),
        }
        for name, (lo, hi) in ints.items():
            if name in data:
                try:
                    setattr(self, name, max(lo, min(hi, int(data[name]))))
                except (TypeError, ValueError):
                    pass
        for name, (lo, hi) in floats.items():
            if name in data:
                try:
                    setattr(self, name, max(lo, min(hi, float(data[name]))))
                except (TypeError, ValueError):
                    pass
        try:
            if "mock_latency" in data:
                self.mock_latency = self._pair(data["mock_latency"], 0, 600)
            if "mock_answer_chars" in data:
                self.mock_answer_chars = self._pair(
                    data["mock_answer_chars"], 1, 20000, int)
        except (TypeError, ValueError):
            pass
        if "tenant_rotate_identity" in data:
            self.tenant_rotate_identity = bool(data["tenant_rotate_identity"])
        if "block_assets" in data:
            self.block_assets = bool(data["block_assets"])
        for name in ("net_capture", "net_markdown", "net_debug"):
            if name in data:
                setattr(self, name, bool(data[name]))
        self.min_workers = min(self.min_workers, self.max_workers)
        for t in self.tenants.values():
            if not t.key:
                t.max_concurrent = max(1, self.max_workers)
                t.queue_limit = max(1, self.queue_limit)

    _PERSIST = ("min_workers", "max_workers", "queue_limit", "idle_ttl",
                "max_requests_per_identity", "block_streak_limit",
                "tenant_rotate_identity", "answer_timeout", "goto_timeout",
                "ready_quiet_s", "block_assets", "mock_latency",
                "mock_fail_rate", "mock_answer_chars", "mock_max_workers",
                "metrics_preview_chars", "net_capture", "net_markdown",
                "net_debug")

    def save_store(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        data = {"api_key": "" if os.environ.get("API_KEY") else self.api_key,
                "users": [t.full() for t in self.tenants.values() if t.key]}
        for k in self._PERSIST:
            v = getattr(self, k)
            data[k] = list(v) if isinstance(v, tuple) else v
        tmp = self._store_path() + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._store_path())

    def public_settings(self) -> dict:
        """给面板看的配置。密钥只给管理员接口，这里打码。"""
        with self._lock:
            out = {k: (list(getattr(self, k)) if isinstance(getattr(self, k), tuple)
                       else getattr(self, k)) for k in self._PERSIST}
            out.update({
                "backend": self.backend,
                "capacity": self.capacity,
                "headless": self.headless,
                "egress_proxy": bool(self.egress_proxy),
                "trusted_proxies": self.trusted_proxies,
                "task_ttl": self.task_ttl,
                "soak_max_concurrency": self.soak_max_concurrency,
                "users": [t.as_dict() for t in self.tenants.values()],
            })
            return out

    def update_settings(self, data: dict) -> dict:
        with self._lock:
            self._apply_limits(data)
            self.save_store()
            return self.public_settings()

    # ---- 租户密钥 ----
    def list_keys(self) -> list[dict]:
        with self._lock:
            return [t.full() for t in self.tenants.values() if t.key]

    def bootstrap_admin(self) -> str:
        """还没有任何密钥时，生成第一个管理员密钥。之后必须带它才能进管理。"""
        with self._lock:
            if self.auth_enabled:
                raise ValueError("管理员密钥已存在")
            self.api_key = "sk-admin-" + secrets.token_urlsafe(24)
            self.save_store()
            return self.api_key

    def create_key(self, user_id: str | None, max_concurrent: int | None,
                   queue_limit: int | None, note: str | None = None) -> dict:
        with self._lock:
            uid = (user_id or "").strip() or ("u" + secrets.token_hex(3))
            if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,48}", uid):
                raise ValueError("用户名只能包含字母、数字、_ . -，最长 48")
            if uid in self.tenants and self.tenants[uid].key:
                raise ValueError(f"用户 {uid} 已存在")
            key = "sk-" + secrets.token_urlsafe(24)
            mc = max(1, int(max_concurrent or 2))
            ql = max(1, int(queue_limit or self.queue_limit))
            self.tenants[uid] = TenantCfg(uid, key, mc, ql, note=(note or "")[:200],
                                          created=time.time())
            self.save_store()
            return self.tenants[uid].full()

    def update_key(self, user_id: str, data: dict) -> dict:
        with self._lock:
            t = self.tenant(user_id)
            if "max_concurrent" in data and data["max_concurrent"] is not None:
                t.max_concurrent = max(1, min(10000, int(data["max_concurrent"])))
            if "queue_limit" in data and data["queue_limit"] is not None:
                t.queue_limit = max(1, min(100000, int(data["queue_limit"])))
            if "disabled" in data and data["disabled"] is not None:
                t.disabled = bool(data["disabled"])
            if "note" in data and data["note"] is not None:
                t.note = str(data["note"])[:200]
            self.save_store()
            return t.full()

    def rotate_key(self, user_id: str) -> dict:
        with self._lock:
            t = self.tenant(user_id)
            t.key = "sk-" + secrets.token_urlsafe(24)
            self.save_store()
            return t.full()

    def delete_key(self, user_id: str) -> None:
        with self._lock:
            if user_id not in self.tenants:
                raise KeyError(user_id)
            # 没有管理员密钥时，最后一把租户密钥删掉就再也进不了管理，
            # 所以必须留一把；有管理员密钥兜底则可以删干净。
            if not self.api_key and len([t for t in self.tenants.values()
                                         if t.key]) <= 1:
                raise ValueError("至少保留一个密钥，否则将无法再登录管理")
            del self.tenants[user_id]
            if not self.tenants:
                uid = "default" if self.api_key else "public"
                self.tenants[uid] = TenantCfg(uid, "", 1, self.queue_limit)
            if self.default_user not in self.tenants:
                self.default_user = next(iter(self.tenants))
            self.save_store()

    def quota(self, user_id: str) -> tuple[int, int] | None:
        t = self.tenants.get(user_id)
        if t is None or t.disabled:
            return None
        return (t.max_concurrent, t.queue_limit)


CFG = Config()
