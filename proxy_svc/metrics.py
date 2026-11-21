"""请求统计：TPS、token、来源 IP、租户用量、延迟分位、最近请求。

全部在内存里，按秒 / 按分钟两级环形桶聚合：
- 秒级桶保留 1 小时，用于实时 TPS 和短窗口曲线；
- 分钟级桶保留 24 小时，用于长窗口曲线，并定时落盘，重启不丢。
累计总量、租户用量、IP 用量也会一起落盘（DATA_DIR/metrics.json）。

token 数：装了 tiktoken 用 cl100k_base 精确计数；没装则估算
（中日韩字符 1 字 1 token，其余按 4 字符 1 token）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import Counter, OrderedDict, deque

from .core.tokens import TOKENIZER, count_tokens

log = logging.getLogger("metrics")

# 每个桶里的计数列
COLS = ("req", "ok", "err", "rej", "tin", "tout", "lat_ms", "lat_n")
_NC = len(COLS)

# 结果分类
REJECT_KINDS = {"BUSY", "FORBIDDEN", "PAUSED"}


def classify(kind: str | None) -> str:
    if kind == "OK":
        return "ok"
    if kind in REJECT_KINDS:
        return "rej"
    return "err"


class _Ring:
    """固定长度的时间环形桶。"""

    def __init__(self, size: int, step: int):
        self.size = size
        self.step = step
        self.keys = [-1] * size
        self.vals = [[0] * _NC for _ in range(size)]

    def add(self, t: float, vec: list) -> None:
        k = int(t // self.step)
        i = k % self.size
        if self.keys[i] != k:
            self.keys[i] = k
            self.vals[i] = [0] * _NC
        row = self.vals[i]
        for j, v in enumerate(vec):
            row[j] += v

    def row(self, k: int) -> list:
        i = k % self.size
        return self.vals[i] if self.keys[i] == k else [0] * _NC

    def series(self, now: float, n: int, group: int = 1) -> list:
        """最近 n 个桶，按 group 个合并；每项 [起始时间戳, *COLS]。"""
        n = min(n, self.size)
        k_now = int(now // self.step)
        start = k_now - n + 1
        start -= (start % group)            # 对齐，曲线不抖
        out = []
        k = start
        while k <= k_now:
            acc = [0] * _NC
            for kk in range(k, min(k + group, k_now + 1)):
                r = self.row(kk)
                for j in range(_NC):
                    acc[j] += r[j]
            out.append([k * self.step] + acc)
            k += group
        return out

    def total(self, now: float, n: int) -> list:
        k_now = int(now // self.step)
        acc = [0] * _NC
        for k in range(k_now - min(n, self.size) + 1, k_now + 1):
            r = self.row(k)
            for j in range(_NC):
                acc[j] += r[j]
        return acc

    def peak(self, now: float, n: int, col: int = 0) -> int:
        k_now = int(now // self.step)
        return max((self.row(k)[col]
                    for k in range(k_now - min(n, self.size) + 1, k_now + 1)),
                   default=0)

    def dump(self) -> dict:
        return {"step": self.step, "keys": self.keys, "vals": self.vals}

    def load(self, d: dict) -> None:
        if not isinstance(d, dict) or d.get("step") != self.step:
            return
        keys, vals = d.get("keys"), d.get("vals")
        if (isinstance(keys, list) and isinstance(vals, list)
                and len(keys) == self.size and len(vals) == self.size):
            self.keys = [int(k) for k in keys]
            self.vals = [[int(x) for x in (v + [0] * _NC)[:_NC]] for v in vals]


def _blank_usage() -> dict:
    return {"req": 0, "ok": 0, "err": 0, "rej": 0, "tin": 0, "tout": 0,
            "auth_fail": 0, "first": 0.0, "last": 0.0}


class Metrics:
    MAX_IPS = 5000
    MAX_RECENT = 500
    MAX_LAT = 5000

    def __init__(self, data_dir: str | None = None, preview_chars: int = 80):
        self.data_dir = data_dir
        self.preview_chars = max(0, preview_chars)
        self._lock = threading.Lock()
        self.started = time.time()
        self.since = self.started             # 累计统计起点（跨重启）
        self.sec = _Ring(3600, 1)             # 1 小时秒级
        self.min = _Ring(1440, 60)            # 24 小时分钟级
        self.totals = _blank_usage()
        self.kinds: Counter = Counter()
        self.endpoints: Counter = Counter()
        self.users: dict[str, dict] = {}
        self.ips: "OrderedDict[str, dict]" = OrderedDict()
        self.recent: deque = deque(maxlen=self.MAX_RECENT)
        self.lat: deque = deque(maxlen=self.MAX_LAT)     # (ts, ms) 仅成功
        self.ip_seen: deque = deque(maxlen=20000)        # (ts, ip) 算窗口内独立 IP
        self._dirty = False
        self._stop = threading.Event()
        self._saver: threading.Thread | None = None

    # ---------------- 记录 ----------------
    def _ip(self, ip: str, now: float) -> dict:
        d = self.ips.get(ip)
        if d is None:
            d = _blank_usage()
            d["first"] = now
            d["users"] = []
            d["ua"] = ""
            self.ips[ip] = d
            while len(self.ips) > self.MAX_IPS:
                self.ips.popitem(last=False)
        else:
            self.ips.move_to_end(ip)
        return d

    def _user(self, uid: str, now: float) -> dict:
        d = self.users.get(uid)
        if d is None:
            d = _blank_usage()
            d["first"] = now
            d["ips"] = []
            self.users[uid] = d
        return d

    def record(self, *, user_id: str, ip: str, endpoint: str, kind: str,
               tokens_in: int = 0, tokens_out: int = 0,
               total_s: float | None = None, queue_s: float | None = None,
               serve_s: float | None = None, question: str | None = None,
               ua: str = "", task_id: str | None = None,
               error: str | None = None, ts: float | None = None) -> None:
        now = ts or time.time()
        cls = classify(kind)
        lat_ms = int((total_s or 0) * 1000) if cls == "ok" else 0
        vec = [1, cls == "ok", cls == "err", cls == "rej",
               tokens_in, tokens_out, lat_ms, 1 if lat_ms else 0]
        vec = [int(v) for v in vec]
        preview = ""
        if question and self.preview_chars:
            q = " ".join(question.split())
            preview = q[:self.preview_chars] + ("…" if len(q) > self.preview_chars else "")
        with self._lock:
            self.sec.add(now, vec)
            self.min.add(now, vec)
            self.kinds[kind or "?"] += 1
            self.endpoints[endpoint] += 1
            for d in (self.totals, self._user(user_id, now), self._ip(ip, now)):
                d["req"] += 1
                d[cls] += 1
                d["tin"] += tokens_in
                d["tout"] += tokens_out
                d["last"] = now
            ipd = self.ips[ip]
            if user_id not in ipd["users"]:
                ipd["users"] = (ipd["users"] + [user_id])[-10:]
            if ua:
                ipd["ua"] = ua[:200]
            ud = self.users[user_id]
            if ip not in ud["ips"]:
                ud["ips"] = (ud["ips"] + [ip])[-20:]
            if lat_ms:
                self.lat.append((now, lat_ms))
            self.ip_seen.append((now, ip))
            self.recent.append({
                "ts": round(now, 3), "id": task_id, "ip": ip, "user": user_id,
                "endpoint": endpoint, "kind": kind, "cls": cls,
                "tin": tokens_in, "tout": tokens_out,
                "total_s": total_s, "queue_s": queue_s, "serve_s": serve_s,
                "preview": preview, "error": (error or "")[:200]})
            self._dirty = True

    def auth_fail(self, ip: str, path: str, ua: str = "") -> None:
        now = time.time()
        with self._lock:
            d = self._ip(ip, now)
            d["auth_fail"] += 1
            d["last"] = now
            if ua:
                d["ua"] = ua[:200]
            self.totals["auth_fail"] += 1
            self.recent.append({
                "ts": round(now, 3), "id": None, "ip": ip, "user": "-",
                "endpoint": path, "kind": "AUTH", "cls": "rej",
                "tin": 0, "tout": 0, "total_s": None, "queue_s": None,
                "serve_s": None, "preview": "", "error": "invalid api key"})
            self._dirty = True

    # ---------------- 查询 ----------------
    def user_usage(self, uid: str) -> dict:
        with self._lock:
            d = self.users.get(uid)
            return dict(d) if d else _blank_usage()

    @staticmethod
    def _pct(sorted_vals: list, p: float) -> float | None:
        if not sorted_vals:
            return None
        i = min(len(sorted_vals) - 1, max(0, int(round(p * (len(sorted_vals) - 1)))))
        return round(sorted_vals[i] / 1000, 2)

    def snapshot(self, window: int = 900, top_ips: int = 200,
                 recent: int = 200) -> dict:
        window = max(60, min(int(window), 86400))
        now = time.time()
        # 选数据源和合并粒度，让曲线保持 150~360 个点
        if window <= 3600:
            ring, n = self.sec, window
            group = max(1, window // 300)
        else:
            ring, n = self.min, window // 60
            group = max(1, n // 300)
        with self._lock:
            series = ring.series(now, n, group)
            win = ring.total(now, n)
            s10 = self.sec.total(now, 10)
            s60 = self.sec.total(now, 60)
            peak = self.sec.peak(now, 3600)
            lats = sorted(ms for t, ms in self.lat if t >= now - window)
            uniq = {ip for t, ip in self.ip_seen if t >= now - window}
            ips = sorted(self.ips.items(), key=lambda kv: kv[1]["last"],
                         reverse=True)
            ip_rows = [{"ip": ip, **{k: v for k, v in d.items()}}
                       for ip, d in ips[:top_ips]]
            users = {u: dict(d) for u, d in self.users.items()}
            rec = list(self.recent)[-recent:][::-1]
            totals = dict(self.totals)
            kinds = dict(self.kinds)
            endpoints = dict(self.endpoints)
        step = ring.step * group
        win_req = win[0]
        return {
            "now": now,
            "since": self.since,
            "uptime_s": round(now - self.started, 1),
            "tokenizer": TOKENIZER,
            "window": window,
            "totals": totals,
            "kinds": kinds,
            "endpoints": endpoints,
            "rates": {
                "tps_10s": round(s10[0] / 10, 2),
                "tps_60s": round(s60[0] / 60, 2),
                "tps_window": round(win_req / window, 3),
                "tps_peak_1h": peak,
                "tokens_per_min": s60[4] + s60[5],
            },
            "window_totals": dict(zip(COLS, win)),
            "success_rate": (round(win[1] / win_req * 100, 1)
                             if win_req else None),
            "latency": {
                "n": len(lats),
                "avg": round(sum(lats) / len(lats) / 1000, 2) if lats else None,
                "p50": self._pct(lats, 0.5),
                "p95": self._pct(lats, 0.95),
                "p99": self._pct(lats, 0.99),
            },
            "unique_ips": len(uniq),
            "series": {"step": step, "cols": ["t", *COLS], "points": series},
            "ips": ip_rows,
            "ip_count": len(self.ips),
            "users": users,
            "recent": rec,
        }

    # ---------------- 落盘 ----------------
    def _path(self) -> str | None:
        return os.path.join(self.data_dir, "metrics.json") if self.data_dir else None

    def load(self) -> None:
        path = self._path()
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            log.warning("读取统计失败: %s", e)
            return
        with self._lock:
            try:
                self.since = float(d.get("since") or self.since)
                self.totals.update({k: v for k, v in (d.get("totals") or {}).items()
                                    if k in self.totals})
                self.kinds.update(d.get("kinds") or {})
                self.endpoints.update(d.get("endpoints") or {})
                for uid, u in (d.get("users") or {}).items():
                    self.users[uid] = {**_blank_usage(), "ips": [], **u}
                for ip, v in (d.get("ips") or []):
                    self.ips[ip] = {**_blank_usage(), "users": [], "ua": "", **v}
                self.min.load(d.get("min") or {})
            except Exception as e:
                log.warning("统计文件格式不对，忽略: %s", e)

    def save(self) -> None:
        path = self._path()
        if not path:
            return
        with self._lock:
            if not self._dirty:
                return
            data = {
                "since": self.since,
                "totals": self.totals,
                "kinds": dict(self.kinds),
                "endpoints": dict(self.endpoints),
                "users": self.users,
                "ips": list(self.ips.items())[-2000:],
                "min": self.min.dump(),
            }
            blob = json.dumps(data, ensure_ascii=False)
            self._dirty = False
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(blob)
            os.replace(tmp, path)
        except Exception as e:
            log.warning("保存统计失败: %s", e)

    def reset(self) -> None:
        with self._lock:
            self.since = time.time()
            self.sec = _Ring(3600, 1)
            self.min = _Ring(1440, 60)
            self.totals = _blank_usage()
            self.kinds.clear()
            self.endpoints.clear()
            self.users.clear()
            self.ips.clear()
            self.recent.clear()
            self.lat.clear()
            self.ip_seen.clear()
            self._dirty = True
        self.save()

    def start(self, every: float = 30.0) -> None:
        self.load()

        def loop():
            while not self._stop.wait(every):
                self.save()
        self._saver = threading.Thread(target=loop, name="metrics-save",
                                       daemon=True)
        self._saver.start()

    def stop(self) -> None:
        self._stop.set()
        self.save()
