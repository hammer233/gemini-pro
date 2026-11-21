"""按租户轮转的公平队列。

原来是一个全局 FIFO：某个租户一次压进几十个请求，后来的租户只能排在后面。
这里每个租户一条子队列，出队时在有任务的租户之间轮转，
所以无论谁压了多少，每个租户都能按轮次拿到执行位。
"""
from __future__ import annotations

import queue
import threading
import time
from collections import deque
from typing import Callable


class FairQueue:
    def __init__(self, maxsize: Callable[[], int] | int = 0):
        self._maxsize = maxsize if callable(maxsize) else (lambda: maxsize)
        self._qs: dict[str, deque] = {}
        self._order: deque[str] = deque()        # 有任务的租户，轮转顺序
        self._n = 0
        self._cv = threading.Condition()

    @property
    def maxsize(self) -> int:
        return int(self._maxsize() or 0)

    def put_nowait(self, task) -> None:
        with self._cv:
            m = self.maxsize
            if m and self._n >= m:
                raise queue.Full
            uid = task.user_id
            dq = self._qs.get(uid)
            if dq is None:
                dq = self._qs[uid] = deque()
            if not dq:
                self._order.append(uid)
            dq.append(task)
            self._n += 1
            self._cv.notify()

    def get(self, timeout: float | None = None):
        with self._cv:
            end = None if timeout is None else time.monotonic() + timeout
            while self._n == 0:
                if end is None:
                    self._cv.wait()
                    continue
                rem = end - time.monotonic()
                if rem <= 0:
                    raise queue.Empty
                self._cv.wait(rem)
            uid = self._order.popleft()
            dq = self._qs[uid]
            task = dq.popleft()
            self._n -= 1
            if dq:
                self._order.append(uid)          # 还有任务，排到轮转末尾
            else:
                del self._qs[uid]
            return task

    def remove(self, task) -> bool:
        """把还没出队的任务拿掉（取消/超时）。已出队返回 False。"""
        with self._cv:
            dq = self._qs.get(task.user_id)
            if not dq:
                return False
            try:
                dq.remove(task)
            except ValueError:
                return False
            self._n -= 1
            if not dq:
                del self._qs[task.user_id]
                try:
                    self._order.remove(task.user_id)
                except ValueError:
                    pass
            return True

    def drain(self) -> list:
        with self._cv:
            out = [t for uid in list(self._order) for t in self._qs.get(uid, ())]
            self._qs.clear()
            self._order.clear()
            self._n = 0
            return out

    def snapshot(self) -> list:
        with self._cv:
            return [t for uid in self._order for t in self._qs.get(uid, ())]

    def depth_by_user(self) -> dict[str, int]:
        with self._cv:
            return {uid: len(dq) for uid, dq in self._qs.items()}

    def qsize(self) -> int:
        return self._n

    def empty(self) -> bool:
        return self._n == 0

    def wakeup(self) -> None:
        """唤醒所有阻塞在 get() 上的工作线程（用于快速关停或收缩）。"""
        with self._cv:
            self._cv.notify_all()
