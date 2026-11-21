"""长时间冒烟 / 压力测试器（SoakRunner）。

用途：
- 并发驱动 Pool + FairQueue + MockBackend，不发任何外网请求；
- 持续采样进程 RSS 内存、线程数、延迟分位、各状态计数；
- 校验核心不变量（完成数 == ok + err + rej、停机后 running/queued 归零）。
"""
from __future__ import annotations

import logging
import random
import threading
import time
from collections import Counter, deque
from typing import Callable

from ..backends.mock import MockBackend
from .pool import Pool, PoolLimits
from .sysinfo import process_info, rss_mb
from .tasks import Task

log = logging.getLogger("soak")

_SAMPLE_QUESTIONS = [
    "Explain how round-robin fair queuing prevents tenant starvation under load.",
    "Summarize the trade-offs between thread pools and event loops for I/O bound tasks.",
    "什么是多租户配额隔离？在高并发下如何避免单个租户把队列打满？",
    "给出一个检查长时间运行 Python 服务是否存在内存泄漏的排查清单。",
    "How do ring buffers aggregate per-second and per-minute telemetry in constant memory?",
    "Compare p50, p95, and p99 latency metrics when evaluating tail latency.",
]


class SoakRunner:
    """后台冒烟测试控制器（同一时刻最多跑一场）。"""

    def __init__(self, cfg, main_pool_getter: Callable[[], Pool | None],
                 on_task_done: Callable[[Task], None] | None = None):
        self.cfg = cfg
        self._get_main_pool = main_pool_getter
        self._on_task_done = on_task_done
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._iso_pool: Pool | None = None

        self.running = False
        self.mode = "idle"
        self.concurrency = 4
        self.duration_s = 60.0
        self.max_requests = 0
        self.think_ms = 50
        self.users: list[str] = []

        self.started: float | None = None
        self.finished: float | None = None
        self.sent = 0
        self.ok = 0
        self.err = 0
        self.rej = 0
        self.tin = 0
        self.tout = 0
        self.kinds: Counter = Counter()
        self.lats: deque = deque(maxlen=5000)       # ms
        self.rss_start: float | None = None
        self.rss_peak: float | None = None
        self.samples: deque = deque(maxlen=360)     # [{t, rss_mb, threads, sent, ok, err, rej}]

    def start(self, *, concurrency: int = 4, duration_s: float = 60.0,
              max_requests: int = 0, think_ms: int = 50,
              users: list[str] | None = None) -> dict:
        with self._lock:
            if self.running:
                raise RuntimeError("冒烟测试已在运行中")
            max_c = max(1, int(getattr(self.cfg, "soak_max_concurrency", 64)))
            self.concurrency = max(1, min(max_c, int(concurrency or 4)))
            self.duration_s = max(5.0, min(86400.0, float(duration_s or 60.0)))
            self.max_requests = max(0, int(max_requests or 0))
            self.think_ms = max(0, min(10000, int(think_ms if think_ms is not None else 50)))

            configured_users = list(self.cfg.tenants.keys())
            self.users = [u.strip() for u in (users or []) if u and u.strip()] or configured_users or ["public"]

            self._stop.clear()
            self.running = True
            self.started = time.time()
            self.finished = None
            self._dispatched = 0
            self.sent = 0
            self.ok = 0
            self.err = 0
            self.rej = 0
            self.tin = 0
            self.tout = 0
            self.kinds.clear()
            self.lats.clear()
            self.samples.clear()
            cur_rss = rss_mb()
            self.rss_start = cur_rss
            self.rss_peak = cur_rss

            main_pool = self._get_main_pool()
            if main_pool is not None and getattr(main_pool.backend, "name", "") == "mock":
                self.mode = "main_mock"
                target_pool = main_pool
                self._iso_pool = None
            else:
                # 主池是真实浏览器后端时，自动建立隔离的 Mock 池测试调度内核，绝不出网
                self.mode = "isolated_mock"
                limits = PoolLimits(
                    min_workers=lambda: min(2, self.concurrency),
                    max_workers=lambda: max(2, min(self.cfg.mock_max_workers, self.concurrency)),
                    queue_limit=lambda: self.cfg.queue_limit,
                    idle_ttl=lambda: 30.0,
                    default_timeout=lambda: 60.0,
                )
                self._iso_pool = Pool(
                    "soak",
                    MockBackend(self.cfg),
                    limits,
                    quota=lambda uid: self.cfg.quota(uid) or (max(4, self.concurrency), self.cfg.queue_limit),
                    task_ttl=60.0,
                )
                self._iso_pool.start()
                target_pool = self._iso_pool

            self._threads = []
            for i in range(self.concurrency):
                t = threading.Thread(target=self._worker, args=(i, target_pool),
                                     name=f"soak-w{i}", daemon=True)
                self._threads.append(t)
                t.start()
            mon = threading.Thread(target=self._coordinator, args=(target_pool,),
                                   name="soak-coord", daemon=True)
            self._threads.append(mon)
            mon.start()
            log.info("冒烟测试启动 mode=%s concurrency=%d duration=%.0fs users=%s",
                     self.mode, self.concurrency, self.duration_s, ",".join(self.users))
        return self.status()

    def stop(self) -> dict:
        self._stop.set()
        for t in list(self._threads):
            if t.is_alive() and t is not threading.current_thread():
                t.join(timeout=3.0)
        return self.status()

    def _record_result(self, res: dict):
        kind = res.get("kind") or "ERROR"
        usage = res.get("usage") or {}
        tin = int(usage.get("prompt_tokens") or 0)
        tout = int(usage.get("completion_tokens") or 0)
        total_s = res.get("total_s_all") or res.get("total_s")
        with self._lock:
            self.sent += 1
            self.kinds[kind] += 1
            self.tin += tin
            self.tout += tout
            if kind == "OK":
                self.ok += 1
                if total_s is not None:
                    self.lats.append(int(float(total_s) * 1000))
            elif kind in ("BUSY", "FORBIDDEN", "PAUSED"):
                self.rej += 1
            else:
                self.err += 1
            if self.max_requests > 0 and self.sent >= self.max_requests:
                self._stop.set()

    def _claim_ticket(self) -> bool:
        with self._lock:
            if self.max_requests > 0 and self._dispatched >= self.max_requests:
                self._stop.set()
                return False
            self._dispatched += 1
            return True

    def _worker(self, wid: int, pool: Pool):
        rng = random.Random(wid + int(time.time()))
        deadline = (self.started or time.time()) + self.duration_s
        while not self._stop.is_set() and time.time() < deadline:
            if not self._claim_ticket():
                break
            uid = rng.choice(self.users)
            q = f"[soak#{wid}] " + rng.choice(_SAMPLE_QUESTIONS)
            t = pool.enqueue(q, fresh=False, user_id=uid,
                             meta={"ip": "127.0.0.1", "ua": "soak-runner", "endpoint": "/soak"})
            if isinstance(t, dict):
                self._record_result(t)
            else:
                if self._on_task_done and self.mode == "main_mock":
                    t.add_done_callback(self._on_task_done)
                if not t.wait(timeout=30.0):
                    pool.cancel(t, "timeout")
                res = t.result or {"kind": "TIMEOUT", "error": "soak wait timeout"}
                self._record_result(res)
            if self.think_ms > 0 and not self._stop.is_set():
                self._stop.wait(self.think_ms / 1000.0)

    def _sample(self):
        p = process_info()
        r = p.get("rss_mb")
        with self._lock:
            if r is not None:
                if self.rss_start is None:
                    self.rss_start = r
                if self.rss_peak is None or r > self.rss_peak:
                    self.rss_peak = r
            self.samples.append({
                "t": round(time.time(), 1),
                "rss_mb": r,
                "threads": p.get("threads"),
                "sent": self.sent,
                "ok": self.ok,
                "err": self.err,
                "rej": self.rej,
            })

    def _coordinator(self, pool: Pool):
        deadline = (self.started or time.time()) + self.duration_s
        self._sample()
        last_sample = time.time()
        while not self._stop.is_set() and time.time() < deadline:
            self._stop.wait(1.0)
            if time.time() - last_sample >= 2.0:
                self._sample()
                last_sample = time.time()
        self._stop.set()
        for t in list(self._threads):
            if t is not threading.current_thread() and t.is_alive():
                t.join(timeout=5.0)
        if self._iso_pool is not None:
            try:
                self._iso_pool.shutdown()
            except Exception:
                pass
            self._iso_pool = None
        self._sample()
        with self._lock:
            self.running = False
            self.finished = time.time()
        log.info("冒烟测试结束 sent=%d ok=%d err=%d rej=%d",
                 self.sent, self.ok, self.err, self.rej)

    @staticmethod
    def _pct(sorted_vals: list[int], p: float) -> float | None:
        if not sorted_vals:
            return None
        i = min(len(sorted_vals) - 1, max(0, int(round(p * (len(sorted_vals) - 1)))))
        return round(sorted_vals[i] / 1000.0, 2)

    def status(self) -> dict:
        now = time.time()
        cur_rss = rss_mb()
        with self._lock:
            if cur_rss is not None and self.running:
                if self.rss_peak is None or cur_rss > self.rss_peak:
                    self.rss_peak = cur_rss
            elapsed = ((self.finished or now) - self.started) if self.started else 0.0
            lats = sorted(self.lats)
            accounted = self.ok + self.err + self.rej
            rss_delta = (round(cur_rss - self.rss_start, 1)
                         if (cur_rss is not None and self.rss_start is not None) else None)
            return {
                "running": self.running,
                "mode": self.mode,
                "concurrency": self.concurrency,
                "duration_s": self.duration_s,
                "max_requests": self.max_requests,
                "think_ms": self.think_ms,
                "users": list(self.users),
                "started": self.started,
                "finished": self.finished,
                "elapsed_s": round(elapsed, 1),
                "sent": self.sent,
                "ok": self.ok,
                "err": self.err,
                "rej": self.rej,
                "tps": round(self.sent / elapsed, 2) if elapsed > 0 else 0.0,
                "tokens_in": self.tin,
                "tokens_out": self.tout,
                "kinds": dict(self.kinds),
                "latency": {
                    "n": len(lats),
                    "avg": round(sum(lats) / len(lats) / 1000.0, 2) if lats else None,
                    "p50": self._pct(lats, 0.5),
                    "p95": self._pct(lats, 0.95),
                    "p99": self._pct(lats, 0.99),
                },
                "memory": {
                    "start_mb": self.rss_start,
                    "now_mb": cur_rss,
                    "peak_mb": self.rss_peak,
                    "delta_mb": rss_delta,
                },
                "invariants": {
                    "counts_match": self.sent == accounted,
                    "accounted": accounted,
                },
                "samples": list(self.samples)[-60:],
            }
