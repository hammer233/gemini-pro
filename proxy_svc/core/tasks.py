"""Task：一次提问在池子里的完整生命周期。

queued -> running -> done
queued -> cancelled            （排队时被取消/等待超时，直接从队列移除）
running + abandoned            （调用方已放弃，但后端仍在跑；跑完照常收尾）
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

DoneCallback = Callable[["Task"], None]


@dataclass(eq=False)                    # 按身份比较，队列 remove 不会删错
class Task:
    question: str
    fresh: bool = False
    user_id: str = "public"
    meta: dict = field(default_factory=dict)      # ip / ua / endpoint 等
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    enqueued: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    state: str = "queued"
    abandoned: bool = False
    slot: int | None = None
    result: dict | None = None
    _event: threading.Event = field(default_factory=threading.Event, repr=False)
    _cbs: list = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ---- 完成通知 ----
    def add_done_callback(self, fn: DoneCallback) -> None:
        with self._lock:
            if not self._event.is_set():
                self._cbs.append(fn)
                return
        fn(self)                        # 已经完成：立即回调

    def finish(self, result: dict, state: str = "done") -> bool:
        """只生效一次。返回是否由本次调用完成。"""
        with self._lock:
            if self._event.is_set():
                return False
            self.result = result
            self.state = state
            self.finished = time.time()
            self._event.set()
            cbs, self._cbs = self._cbs, []
        for fn in cbs:
            try:
                fn(self)
            except Exception:           # 回调失败不影响其它回调
                import logging
                logging.getLogger("task").exception("done callback 失败")
        return True

    @property
    def done(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None) -> bool:
        return self._event.wait(timeout)

    # ---- 展示 ----
    def view(self, with_result: bool = False) -> dict:
        now = time.time()
        d = {
            "id": self.id,
            "user_id": self.user_id,
            "state": self.state,
            "abandoned": self.abandoned,
            "slot": self.slot,
            "ip": self.meta.get("ip"),
            "endpoint": self.meta.get("endpoint"),
            "enqueued": self.enqueued,
            "age_s": round((self.finished or now) - self.enqueued, 2),
            "chars": len(self.question),
        }
        if self.started:
            d["queue_s"] = round(self.started - self.enqueued, 2)
        if self.result is not None:
            d["kind"] = self.result.get("kind")
            if with_result:
                d["result"] = self.result
        return d
