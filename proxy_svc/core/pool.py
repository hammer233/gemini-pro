"""执行池：只依赖 Backend 接口，不认识浏览器、不认识 HTTP、不认识统计。

设计要点
- 每个 Slot 独占一个线程和一个后端 Session（浏览器 Session 绑定线程）。
- 公平队列（FairQueue）：租户间轮转出队，一个租户压满也不会饿死别人。
- 配额：每租户 max_concurrent（在跑 + 在排队）和 queue_limit，超限立刻拒绝。
- 伸缩：积压超过空闲产能时扩容到 max_workers；空闲超 idle_ttl 回收到 min_workers。
- 取消：等待超时 / 主动取消时，排队中的任务直接移出队列，不再白占后端；
  已在跑的任务标记 abandoned，跑完照常收尾。
- 登记：最近的任务按 id 保留 task_ttl 秒，支持 wait=false 后轮询结果。
- 暂停：pause() 后新请求直接拒绝（PAUSED），已排队的继续跑完。
- 完成钩子：Task.add_done_callback，统计等横切逻辑挂在外面，池子不感知。
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

from .fair_queue import FairQueue
from .sysinfo import avail_mem_mb
from .tasks import Task
from .tokens import usage_of

log = logging.getLogger("pool")

QuotaFn = Callable[[str], "tuple[int, int] | None"]   # uid -> (并发, 排队) / None=未知租户


@dataclass
class PoolLimits:
    """池子的伸缩参数。全部是取值函数，面板改了设置立刻生效。"""
    min_workers: Callable[[], int] = lambda: 1
    max_workers: Callable[[], int] = lambda: 2
    queue_limit: Callable[[], int] = lambda: 200
    idle_ttl: Callable[[], float] = lambda: 300.0
    default_timeout: Callable[[], float] = lambda: 360.0
    # ask_timeout: 单次 ask 内部允许花掉的时间预算(由 Session 换算成 deadline)。
    # 必须小于 default_timeout,给收尾/序列化/看门狗留余量;为 None 时
    # 自动取 max(20, default_timeout - 30)。
    ask_timeout: Callable[[], float] | None = None


@dataclass
class _UserStat:
    running: int = 0
    queued: int = 0
    served: int = 0
    rejected: int = 0
    last: float = 0.0


class Slot:
    """一个执行线程 + 一个后端 Session。"""

    def __init__(self, sid: int, pool: "Pool"):
        self.sid = sid
        self.pool = pool
        self.stop_flag = threading.Event()
        self.busy = False
        self.idle_since = time.time()
        self.served = 0
        self.dead = False
        self.session = None
        self.session_gen = 0
        self.current: Task | None = None
        self.thread = threading.Thread(target=self._run,
                                       name=f"{pool.name}-s{sid}", daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        pool = self.pool
        while not self.stop_flag.is_set():
            if self.session is not None and self.session_gen != pool._backend_gen:
                self._drop_session()
            try:
                task = pool.q.get(timeout=0.5)
            except queue.Empty:
                if pool._may_retire(self):
                    break
                continue

            if not pool._running:
                pool._finish(task, {"kind": "ERROR", "error": "shutting down"},
                             was_running=False, state="cancelled")
                break

            pool._claim(task, self)
            res = None
            try:
                if self.session is not None and self.session_gen != pool._backend_gen:
                    self._drop_session()
                if self.session is None:
                    gen = pool._backend_gen
                    self.session = pool.backend.new_session(self.sid)
                    self.session_gen = gen
                # 把「这一次请求最多能花多久」和「调用方是否已经走了」一起交给
                # Session:它据此算出唯一 deadline,内部所有等待都只看剩余时间,
                # 不会把 goto/settle/打码/等答案的各自超时叠加成 2~3 分钟。
                res = self.session.ask(task.question, fresh=task.fresh,
                                       user_id=task.user_id,
                                       timeout=pool._ask_budget(),
                                       abort=pool._abort_hook(task))
            except Exception as e:
                log.exception("[%s S%d] 处理异常", pool.name, self.sid)
                res = {"kind": "ERROR", "error": str(e)[:300]}
                self._drop_session()          # 出异常的 session 不再复用
            finally:
                res = res if isinstance(res, dict) else {
                    "kind": "ERROR", "error": "no result"}
                res.setdefault("slot", self.sid)
                if task.abandoned:
                    res["abandoned"] = True
                    # 看门狗是「到点强制收尾」,语义是超时而不是调用方主动取消。
                    if task.meta.get("watchdog") and res.get("kind") != "OK":
                        res["kind"] = "TIMEOUT"
                        res.setdefault("error", "worker slot watchdog timeout")
                pool._finish(task, res, was_running=True, slot=self)
                if self.session_gen != pool._backend_gen:
                    self._drop_session()
        self._drop_session()
        self.dead = True

    def _drop_session(self):
        s, self.session = self.session, None
        if s is not None:
            try:
                s.close()
            except Exception:
                pass

    def view(self) -> dict:
        cur = self.current
        return {"sid": self.sid, "busy": self.busy, "served": self.served,
                "idle_s": None if self.busy else round(time.time() - self.idle_since, 1),
                "task": cur.id if cur else None,
                "user": cur.user_id if cur else None,
                **(self.session.info() if self.session is not None
                   and hasattr(self.session, "info") else {})}


class Pool:
    def __init__(self, name: str, backend, limits: PoolLimits,
                 quota: QuotaFn, task_ttl: float = 600, keep_tasks: int = 5000):
        self.name = name
        self.backend = backend
        self.limits = limits
        self.quota = quota
        self.task_ttl = task_ttl
        self.keep_tasks = keep_tasks
        self.q = FairQueue(limits.queue_limit)
        self.slots: list[Slot] = []
        self.paused = False
        self.started = time.time()
        self.total_tasks = 0
        self.rejected = 0
        self._next = 0
        self._backend_gen = 1
        self._last_spawn_ts = 0.0
        self._lock = threading.Lock()          # slots / 计数
        self._scale_lock = threading.Lock()
        self._users: dict[str, _UserStat] = {}
        self._tasks: "OrderedDict[str, Task]" = OrderedDict()
        self._running = False
        self._monitor = threading.Thread(target=self._monitor_loop,
                                         name=f"{name}-monitor", daemon=True)

    # ================= 生命周期 =================
    def start(self):
        self._running = True
        for _ in range(self.limits.min_workers()):
            self._spawn()
        self._monitor.start()
        log.info("[%s] 池已启动 backend=%s min=%d max=%d queue=%d", self.name,
                 getattr(self.backend, "name", "?"), self.limits.min_workers(),
                 self.limits.max_workers(), self.limits.queue_limit())

    def set_backend(self, backend) -> None:
        """热切换后端：空闲 Slot 下轮循环立即重建 session，忙 Slot 做完当前单后重建。"""
        with self._lock:
            old = self.backend
            self.backend = backend
            self._backend_gen += 1
        self.q.wakeup()
        if old is not None and old is not backend and hasattr(old, "close"):
            try:
                old.close()
            except Exception:
                pass
        log.info("[%s] 已切换后端 -> %s (gen=%d)", self.name,
                 getattr(backend, "name", "?"), self._backend_gen)

    def shutdown(self):
        self._running = False
        for t in self.q.drain():
            self._finish(t, {"kind": "ERROR", "error": "shutting down"},
                         was_running=False, state="cancelled")
        for s in list(self.slots):
            s.stop_flag.set()
        self.q.wakeup()
        for s in list(self.slots):
            s.thread.join(timeout=8)
        if hasattr(self.backend, "close"):
            try:
                self.backend.close()
            except Exception:
                pass
        log.info("[%s] 池已关闭", self.name)

    def _spawn(self) -> Slot:
        with self._lock:
            self._next += 1
            s = Slot(self._next, self)
            self.slots.append(s)
        s.start()
        log.info("[%s] 启动 S%d（当前 %d）", self.name, s.sid, self.size())
        return s

    def _ask_budget(self) -> float:
        """单次 ask 的时间预算(秒)。

        Session 用它算唯一 deadline。必须比 default_timeout 小一截:
        上层要留出把结果序列化/写回响应的余量,看门狗也要有判断窗口。
        """
        fn = getattr(self.limits, "ask_timeout", None)
        if fn is not None:
            try:
                v = float(fn() or 0.0)
                if v > 0:
                    return v
            except Exception:
                pass
        try:
            base = float(self.limits.default_timeout() or 120.0)
        except Exception:
            base = 120.0
        return max(20.0, base - 30.0)

    def _abort_hook(self, task: Task):
        """交给 Session 的「该不该收手」探针。

        调用方断开(wait_async 的 CancelledError)、等待超时、看门狗到点、
        或者进程正在关闭,都会让这里返回 True。Session 在每个轮询点检查它,
        从而立刻放弃轮询并把槽位还回来,而不是傻等 answer_timeout。
        """
        def _aborted() -> bool:
            try:
                return bool(task.abandoned) or not self._running
            except Exception:
                return True
        return _aborted

    def size(self) -> int:
        return len([s for s in self.slots if not s.dead
                    and not s.stop_flag.is_set()])

    def warm(self, n: int = 1) -> int:
        made = 0
        with self._scale_lock:
            while made < n and self.size() < self.limits.max_workers():
                self._spawn()
                made += 1
        return made

    def _may_retire(self, s: Slot) -> bool:
        """空闲线程是否退出。加锁判断，防止多个线程同时退出跌破下限。"""
        with self._lock:
            alive = self.size()
            if alive > self.limits.max_workers():        # 面板调小了上限
                s.stop_flag.set()
                return True
            if alive <= self.limits.min_workers():
                return False
            idle = time.time() - s.idle_since
            if idle >= self.limits.idle_ttl() and self.q.empty():
                s.stop_flag.set()
                log.info("[%s] S%d 空闲 %.0fs，回收", self.name, s.sid, idle)
                return True
        return False

    def _monitor_loop(self):
        while self._running:
            time.sleep(1.0)
            try:
                self._reap()
                self._scale()
                self._prune_tasks()
            except Exception:
                log.exception("[%s] monitor 异常", self.name)

    def _reap(self):
        """看门狗:先协作中止,真卡死才摘除槽位。

        改用共享浏览器后,槽位线程卡住几乎都是「某一次 CDP 调用没回来」,
        而 ask 内部每个轮询点都会检查 abort。所以第一次超时只置
        abandoned,由 Session 自己收手并把槽位还回池子,线程继续服务;
        只有超过硬上限(1.6 倍)仍不收手,才判定线程真的卡死并摘除。
        """
        now = time.time()
        max_hold = max(90.0, float(self.limits.default_timeout() or 120.0))
        hard = max(max_hold * 1.6, max_hold + 30.0)
        timed_out: list[tuple[Slot, Task]] = []
        with self._lock:
            alive = []
            for s in self.slots:
                cur = s.current
                if s.dead or not s.thread.is_alive():
                    s.dead = True
                    if cur is not None and not cur.done:
                        timed_out.append((s, cur))
                    log.info("[%s] S%d 已退出", self.name, s.sid)
                elif cur is not None and cur.started and (now - cur.started) > hard:
                    log.warning("[%s] S%d 执行任务 %s 超过 %.0fs，看门狗强制摘除并释放槽位",
                                self.name, s.sid, cur.id, max_hold)
                    s.stop_flag.set()
                    s.dead = True
                    timed_out.append((s, cur))
                else:
                    # 到点但还没到硬上限:只标记放弃,让 Session 在下一个
                    # 轮询点自己退出,槽位立刻可以复用,线程不杀。
                    if (cur is not None and cur.started and not cur.abandoned
                            and (now - cur.started) > max_hold):
                        log.warning("[%s] S%d 任务 %s 已跑 %.0fs(> %.0fs),"
                                    "标记放弃并协作中止", self.name, s.sid,
                                    cur.id, now - cur.started, max_hold)
                        cur.meta["watchdog"] = True
                        cur.abandoned = True
                    alive.append(s)
            self.slots = alive
        for s, cur in timed_out:
            self._finish(cur, {"kind": "TIMEOUT", "error": "worker slot watchdog timeout"},
                         was_running=True, state="cancelled", slot=s)

    def _scale(self):
        if not self._running:
            return
        with self._scale_lock:
            alive = self.size()
            mx = self.limits.max_workers()
            mn = self.limits.min_workers()
            want = max(0, mn - alive)
            depth = self.q.qsize()
            idle = len([s for s in self.slots if not s.busy and not s.dead
                        and not s.stop_flag.is_set()])
            is_browser = getattr(self.backend, "name", "") == "browser"
            if depth > idle and want == 0:
                if is_browser:
                    # 渐进式错峰扩容：单 IP 下如果同一毫秒并发拉起多个冷浏览器，
                    # 容易触发上游突发风控与 CPU 尖峰；已有热实例 4s 即可消化一单，
                    # 因此超出保活数后按 2.5s 冷却步进扩容，每次稳步 +1。
                    now = time.time()
                    if now - self._last_spawn_ts >= 2.5:
                        want = 1
                else:
                    want = depth - idle
            want = min(want, max(0, mx - alive))
            for _ in range(want):
                if is_browser and self.size() >= max(1, mn):
                    free_m = avail_mem_mb()
                    if free_m is not None and free_m < 350:
                        log.warning("[%s] 可用物理内存 %dMB < 350MB，触发动态背压暂停扩容（当前 %d）",
                                    self.name, free_m, self.size())
                        break
                self._last_spawn_ts = time.time()
                self._spawn()

    # ================= 任务登记 =================
    def _register(self, task: Task):
        with self._lock:
            self._tasks[task.id] = task
            while len(self._tasks) > self.keep_tasks:
                old_id, old = next(iter(self._tasks.items()))
                if not old.done:
                    break
                self._tasks.pop(old_id)

    def _prune_tasks(self):
        cut = time.time() - self.task_ttl
        with self._lock:
            for tid in [tid for tid, t in self._tasks.items()
                        if t.done and (t.finished or 0) < cut]:
                self._tasks.pop(tid, None)

    def get_task(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def live_tasks(self) -> list[Task]:
        with self._lock:
            return [t for t in self._tasks.values() if not t.done]

    # ================= 计数 =================
    def _ustat(self, uid: str) -> _UserStat:
        u = self._users.get(uid)
        if u is None:
            u = self._users[uid] = _UserStat()
        return u

    def _claim(self, task: Task, slot: Slot):
        with self._lock:
            u = self._ustat(task.user_id)
            u.queued = max(0, u.queued - 1)
            u.running += 1
            task.state = "running"
            task.started = time.time()
            task.slot = slot.sid
            slot.busy = True
            slot.current = task

    def _finish(self, task: Task, res: dict, *, was_running: bool,
                state: str = "done", slot: Slot | None = None):
        res.setdefault("user_id", task.user_id)
        res.setdefault("task_id", task.id)
        now = time.time()
        if task.started:
            res.setdefault("queue_s", round(task.started - task.enqueued, 2))
            res.setdefault("serve_s", round(now - task.started, 2))
        else:
            res.setdefault("queue_s", round(now - task.enqueued, 2))
        res.setdefault("total_s_all", round(now - task.enqueued, 2))
        res["usage"] = usage_of(
            task.question, res.get("answer") if res.get("kind") == "OK" else None)
        with self._lock:
            if slot is not None:
                slot.current = None
                slot.busy = False
                slot.idle_since = now
                slot.served += 1
            if task.done:
                return
            u = self._ustat(task.user_id)
            if was_running:
                u.running = max(0, u.running - 1)
            else:
                u.queued = max(0, u.queued - 1)
            u.served += 1
            u.last = now
        task.finish(res, state=state)

    # ================= 提交 =================
    def enqueue(self, question: str, *, fresh: bool = False, user_id: str,
                meta: dict | None = None) -> "Task | dict":
        """入队。成功返回 Task；被拒绝返回结果 dict（kind=BUSY/FORBIDDEN/PAUSED）。"""
        q = self.quota(user_id)
        if q is None:
            with self._lock:
                self.rejected += 1
            return {"kind": "FORBIDDEN", "user_id": user_id,
                    "error": f"未知用户 {user_id}"}
        if self.paused:
            with self._lock:
                self.rejected += 1
                self._ustat(user_id).rejected += 1
            return {"kind": "PAUSED", "user_id": user_id,
                    "error": "服务暂停接单，请稍后重试"}
        max_conc, q_limit = q
        task = Task(question=question, fresh=fresh, user_id=user_id,
                    meta=dict(meta or {}))
        with self._lock:
            u = self._ustat(user_id)
            err = None
            if u.running + u.queued >= max_conc:
                err = f"用户 {user_id} 并发配额已满（上限 {max_conc}），请稍后重试"
            elif u.queued >= q_limit:
                err = f"用户 {user_id} 排队已满（上限 {q_limit}），请稍后重试"
            else:
                try:
                    self.q.put_nowait(task)
                except queue.Full:
                    err = "队列已满，请稍后重试"
            if err:
                self.rejected += 1
                u.rejected += 1
                return {"kind": "BUSY", "user_id": user_id, "error": err}
            u.queued += 1
            self.total_tasks += 1
        self._register(task)
        self._scale()                  # 立即评估，不等 monitor
        return task

    def cancel(self, task: Task, reason: str = "cancelled") -> str:
        """排队中：移出队列并以 TIMEOUT/CANCELLED 收尾；在跑：标记 abandoned。"""
        if task.done:
            return "done"
        if self.q.remove(task):
            kind = "TIMEOUT" if reason == "timeout" else "CANCELLED"
            self._finish(task, {"kind": kind, "error": reason},
                         was_running=False, state="cancelled")
            return "removed"
        task.abandoned = True
        return "abandoned"

    def clear_queue(self) -> int:
        n = 0
        for t in self.q.drain():
            self._finish(t, {"kind": "CANCELLED", "error": "队列被管理员清空"},
                         was_running=False, state="cancelled")
            n += 1
        return n

    def submit(self, question: str, fresh: bool = False,
               user_id: str = "public", wait: bool = True,
               timeout: float | None = None, meta: dict | None = None) -> dict:
        """同步提交（阻塞调用线程）。HTTP 层请用 submit_async。"""
        t = self.enqueue(question, fresh=fresh, user_id=user_id, meta=meta)
        if isinstance(t, dict):
            return t
        if not wait:
            return {"kind": "QUEUED", "task_id": t.id, "user_id": user_id,
                    "queue_depth": self.q.qsize()}
        limit = timeout or self.limits.default_timeout()
        if not t.wait(limit):
            self.cancel(t, "timeout")
            if not t.done:
                return {"kind": "TIMEOUT", "task_id": t.id, "user_id": user_id,
                        "error": f"等待超过 {limit:.0f}s"}
        return t.result or {"kind": "ERROR", "error": "no result"}

    async def wait_async(self, task: Task, timeout: float | None = None) -> dict:
        """在事件循环里等待，不占线程池线程。"""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        def _done(t: Task):
            def _set():
                if not fut.done():
                    fut.set_result(t.result)
            loop.call_soon_threadsafe(_set)

        task.add_done_callback(_done)
        limit = timeout or self.limits.default_timeout()
        try:
            return await asyncio.wait_for(asyncio.shield(fut), limit)
        except asyncio.TimeoutError:
            self.cancel(task, "timeout")
            if task.done and task.result is not None:
                return task.result
            return {"kind": "TIMEOUT", "task_id": task.id,
                    "user_id": task.user_id, "error": f"等待超过 {limit:.0f}s"}
        except asyncio.CancelledError:          # 调用方断开
            self.cancel(task, "client_gone")
            raise

    async def submit_async(self, question: str, *, fresh: bool = False,
                           user_id: str, timeout: float | None = None,
                           meta: dict | None = None,
                           on_done: Callable[[Task], None] | None = None) -> dict:
        t = self.enqueue(question, fresh=fresh, user_id=user_id, meta=meta)
        if isinstance(t, dict):
            return t
        if on_done:
            t.add_done_callback(on_done)
        return await self.wait_async(t, timeout)

    # ================= 状态 =================
    def stats(self, user_id: str | None = None,
              tenants: dict | None = None) -> dict:
        with self._lock:
            users = {uid: dict(vars(u)) for uid, u in self._users.items()}
        for uid in (tenants or {}):
            users.setdefault(uid, dict(vars(_UserStat())))
        for uid, u in users.items():
            q = self.quota(uid)
            u["active"] = u["running"] + u["queued"]
            u["max_concurrent"], u["queue_limit"] = q if q else (0, 0)
            u["last_s"] = round(time.time() - u["last"], 1) if u["last"] else None
        slots = [s.view() for s in self.slots if not s.dead]
        if user_id:
            users = {k: v for k, v in users.items() if k == user_id}
            slots = [s for s in slots if s.get("user") == user_id]
        alive = self.size()
        busy = len([s for s in self.slots if s.busy and not s.dead])
        out = {
            "pool": self.name,
            "backend": getattr(self.backend, "name", "?"),
            "paused": self.paused,
            "workers": alive,
            "busy": busy,
            "idle": max(0, alive - busy),
            "queued": self.q.qsize(),
            "queue_by_user": self.q.depth_by_user() if not user_id else None,
            "queue_limit": self.limits.queue_limit(),
            "max_workers": self.limits.max_workers(),
            "min_workers": self.limits.min_workers(),
            "total_tasks": self.total_tasks,
            "rejected": self.rejected,
            "uptime_s": round(time.time() - self.started, 1),
            "users": users,
            "slots": slots,
        }
        if user_id:
            out["scope"] = user_id
        return out
