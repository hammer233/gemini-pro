"""浏览器后端:全进程共享一个 Playwright 驱动 + 一个 Chromium 进程。

为什么改
--------
上一版每个 Slot 自己 ``sync_playwright().start()`` + ``chromium.launch()``:
每个槽位都带一份 node 驱动(约 120MB)+ 一个独立 Chrome(约 300MB+),
2~4 个槽位就把 4GB 吃满;而浏览器 90% 的时间在等网络,进程数是纯浪费。

``playwright.sync_api`` 是 greenlet 协程 + 线程亲和的,一个 ``Browser``
不能跨线程驱动,所以「共享浏览器」只能走 ``async_api``。这里的做法是:

  PlaywrightHost  独立线程 + 独立事件循环,唯一持有 async_playwright()
                  驱动与唯一一个 Browser。所有页面操作都以协程形式
                  提交到这个循环执行。
  Slot 线程       仍然一个线程一个会话(池子靠它做看门狗和面板统计),
                  但只持有自己的 BrowserContext + Page,通过
                  ``run_coroutine_threadsafe(...).result(timeout)`` 桥接。

内存账(单机 4GB / 2 核):
  基线  node 驱动 ~120MB + Chrome 主进程/GPU/网络 ~160MB
  每槽位 一个 context 一个 renderer;配上 --renderer-process-limit 与
         --process-per-site 让同站点复用 renderer,增量约 40~70MB。
所以 4 个槽位的常驻从「4×(120+300)=1.7GB」降到「280MB + 4×60MB≈520MB」。

时间预算
--------
``ask`` 里所有重试共享同一个 deadline(``timeout`` 一次敲定),每次尝试
只把**剩余**预算往下传,天然不会出现「goto 45s + settle 12s + 打码 180s
+ 等答案 120s」的叠加;桥接再加一点收尾余量,到点直接放弃并隔离上下文
(被取消的 context 绝不复用)。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading
import time
from urllib.parse import unquote, urlsplit

log = logging.getLogger("backend.browser")


# ---------------------------------------------------------------- 异常
class HostDown(RuntimeError):
    """宿主没在跑 / 已关闭 / 浏览器连不上。"""


class BridgeTimeout(RuntimeError):
    """把协程提交到宿主循环后,超过预算还没返回。"""


def _env_bool(name: str, default: bool = True) -> bool:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "y", "t")


# ---------------------------------------------------------------- 宿主
class PlaywrightHost:
    """全进程唯一的 Playwright 驱动 + Chromium 实例,跑在自己的事件循环线程里。

    对外只暴露同步方法(``run`` / ``new_context`` / ``stop``),内部是协程。
    这样池子里的 Slot 线程与 HTTP 线程都能安全调用,而不需要知道 asyncio。
    """

    BOOT_TIMEOUT = 90.0
    CLOSE_TIMEOUT = 15.0
    REBOOT_COOLDOWN = 10.0

    def __init__(self, cfg, name: str = "pw"):
        self.cfg = cfg
        self.name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pw = None
        self._browser = None
        self._lock = threading.Lock()
        self._booted = threading.Event()
        self._err: BaseException | None = None
        self._stopping = False
        self._pages = 0
        self._launches = 0
        self._started_at = 0.0
        self._last_launch_err = ""
        self._last_reboot = -1e9

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        """幂等启动。返回时浏览器已经可用(或抛 HostDown)。"""
        with self._lock:
            if self._stopping:
                raise HostDown("共享浏览器宿主已关闭")
            t = self._thread
            if t is None or not t.is_alive():
                self._booted.clear()
                self._err = None
                t = threading.Thread(target=self._main,
                                     name=f"{self.name}-loop", daemon=True)
                self._thread = t
                t.start()
        if not self._booted.wait(self.BOOT_TIMEOUT):
            raise HostDown(f"浏览器启动超过 {self.BOOT_TIMEOUT:.0f}s 未就绪")
        if self._err is None and self._browser is not None:
            return
        # 上次启动失败 / 浏览器掉线:在既有循环上补一次启动,而不是永久瘫掉。
        # 冷启动可能因临时原因失败(内存紧张、僵尸进程),不重试会让整个服务
        # 直到重启都不可用。
        if self._reboot():
            return
        raise HostDown(f"浏览器启动失败: {self._err}")

    def _reboot(self) -> bool:
        """在已存在的事件循环里再跑一次 _boot,限流避免启动风暴。"""
        now = time.monotonic()
        if now - self._last_reboot < self.REBOOT_COOLDOWN:
            return False
        self._last_reboot = now
        loop = self._loop
        if loop is None or loop.is_closed():
            return False
        self._err = None
        try:
            fut = asyncio.run_coroutine_threadsafe(self._boot(), loop)
            fut.result(self.BOOT_TIMEOUT)
        except BaseException as e:                     # noqa: BLE001
            log.warning("[%s] 重新启动浏览器失败: %s", self.name, e)
            return False
        return self._err is None and self._browser is not None

    def _on_loop_error(self, loop, ctx) -> None:
        """宿主循环的兜底异常处理。

        - ``Future exception was never retrieved``:playwright 驱动启动失败时,
          它内部会留下一个没人取结果的 future,解释器退出时会在 stderr 上
          打一大段噪音。这是驱动内部行为,不影响我们,降到 debug。
        - 其它异常正常记录,别把真问题吞掉。
        """
        msg = str(ctx.get("message") or "")
        exc = ctx.get("exception")
        if "never retrieved" in msg:
            log.debug("[%s] 驱动内部遗留异常: %r", self.name, exc)
            return
        log.error("[%s] 事件循环异常: %s %r", self.name, msg, exc)

    def _main(self) -> None:
        """事件循环线程的入口:启动 → 跑到底 → 收尾。"""
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        loop.set_exception_handler(self._on_loop_error)
        try:
            loop.run_until_complete(self._boot())
            loop.run_forever()
        except BaseException as e:                    # noqa: BLE001
            self._err = self._err or e
            log.error("[%s] 事件循环异常退出: %s", self.name, e)
        finally:
            # 先取消所有挂着的页面协程:它们对应的 concurrent.futures.Future
            # 会以 CancelledError 结束,正阻塞在 future.result() 的 Slot 线程
            # 因此能立刻醒来,而不是傻等到自己的超时。
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
            except BaseException:                      # noqa: BLE001
                pass
            try:
                loop.run_until_complete(self._teardown())
            except BaseException as e:                 # noqa: BLE001
                log.warning("[%s] 关闭浏览器时出错: %s", self.name, e)
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except BaseException:                      # noqa: BLE001
                pass
            finally:
                try:
                    loop.close()
                except BaseException:                  # noqa: BLE001
                    pass
                self._booted.set()                     # 放走任何还在等的人
                log.info("[%s] 事件循环已退出", self.name)

    async def _boot(self) -> None:
        """启动驱动 + 浏览器。可重复调用(第二次起只补缺的部分)。"""
        self._started_at = self._started_at or time.time()
        try:
            if self._pw is None:
                from playwright.async_api import async_playwright
                self._pw = await async_playwright().start()
            b = self._browser
            if b is None or not b.is_connected():
                self._browser = await self._launch()
            log.info("[%s] 共享浏览器就绪(headless=%s, 累计启动 %d 次)",
                     self.name, getattr(self.cfg, "headless", True),
                     self._launches)
        except BaseException as e:                     # noqa: BLE001
            self._err = e
            log.error("[%s] 启动共享浏览器失败: %s", self.name, e)
        finally:
            self._booted.set()

    # ---------------- Chromium 启动参数 ----------------

    def _proxy_kwarg(self) -> dict | None:
        """出口代理。带账号密码时走 ``proxy=`` 参数,Chrome 的
        ``--proxy-server`` 不认 URL 里的 user:pass。"""
        raw = (getattr(self.cfg, "egress_proxy", "") or "").strip()
        if not raw:
            return None
        if "://" not in raw:
            raw = "http://" + raw
        try:
            u = urlsplit(raw)
            host = u.hostname or ""
            if not host:
                return None
            server = f"{u.scheme or 'http'}://{host}"
            if u.port:
                server += f":{u.port}"
            out = {"server": server}
            if u.username:
                out["username"] = unquote(u.username)
                out["password"] = unquote(u.password or "")
            return out
        except Exception as e:
            log.warning("[%s] 解析 EGRESS_PROXY 失败(%s),忽略代理", self.name, e)
            return None

    def _args(self) -> list[str]:
        """Chromium 命令行:稳定性 + 省内存。

        省内存的三件套很关键(单机 4GB):
          --renderer-process-limit   限制同时存在的渲染进程数
          --process-per-site         同站点复用渲染进程(context 之间共享)
          --disable-features=IsolateOrigins,site-per-process
                                     关掉按站点强制隔离,避免每个 context
                                     都拉起独立渲染进程
        这三条会削弱浏览器侧的站点隔离,只适合「内部抓取」这种场景;
        需要严格隔离时设 PW_SHARE_RENDERER=0。
        """
        args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",             # 容器里 /dev/shm 只有 64MB
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",     # 省掉一堆后台请求
            "--disable-breakpad",
            "--disable-crash-reporter",
            "--disable-extensions",
            "--disable-default-apps",
            "--disable-component-update",
            "--disable-sync",
            "--mute-audio",
            "--disable-gpu",                       # headless 下 GPU 进程纯占内存
            "--disable-software-rasterizer",
            "--js-flags=--max-old-space-size=384",  # 单渲染进程堆上限
        ]
        if _env_bool("PW_SHARE_RENDERER", True):
            args += [
                "--renderer-process-limit=2",
                "--process-per-site",
                "--disable-features=IsolateOrigins,site-per-process",
            ]
        if getattr(self.cfg, "block_assets", True):
            # 与 context.route 的 abort 互补:图片在 blink 层就不解码,省渲染内存
            args.append("--blink-settings=imagesEnabled=false")
        if not getattr(self.cfg, "headless", True):
            args.append("--window-size=1440,900")
        return args

    def _launch_kwargs(self) -> dict:
        cfg = self.cfg
        kw: dict = {
            "headless": bool(getattr(cfg, "headless", True)),
            "args": self._args(),
            "timeout": 60_000,
        }
        exe = (getattr(cfg, "chrome_path", "") or "").strip()
        if exe:
            kw["executable_path"] = exe
        proxy = self._proxy_kwarg()
        if proxy:
            kw["proxy"] = proxy
        return kw

    async def _launch(self):
        """启动 Chromium。失败重试一次(上一次的僵尸进程常导致首启失败)。"""
        last: BaseException | None = None
        for attempt in (1, 2):
            try:
                b = await self._pw.chromium.launch(**self._launch_kwargs())
                self._launches += 1
                self._browser = b
                self._last_launch_err = ""
                return b
            except BaseException as e:                 # noqa: BLE001
                last = e
                self._last_launch_err = str(e)[:300]
                log.warning("[%s] 第 %d 次启动 Chromium 失败: %s",
                            self.name, attempt, e)
                await asyncio.sleep(1.0)
        raise HostDown(f"Chromium 启动失败: {last}")

    async def _ensure_browser(self):
        """拿到一个「连着」的 Browser;断了就重启一个。"""
        if self._stopping:
            raise HostDown("共享浏览器宿主正在关闭")
        b = self._browser
        try:
            if b is not None and b.is_connected():
                return b
        except Exception:
            pass
        log.warning("[%s] 浏览器已断开,重新启动", self.name)
        old, self._browser = self._browser, None
        if old is not None:
            try:
                await asyncio.wait_for(old.close(), 10)
            except BaseException:                      # noqa: BLE001
                pass
        return await self._launch()

    # ---------------- 桥接 ----------------

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    @property
    def pages(self) -> int:
        return self._pages

    def alive(self) -> bool:
        t = self._thread
        if self._stopping or t is None or not t.is_alive():
            return False
        b = self._browser
        try:
            return bool(b is not None and b.is_connected())
        except Exception:
            return False

    def run(self, coro, timeout: float | None = None):
        """把协程提交到宿主循环并等结果(Slot 线程在这里阻塞)。

        ``timeout`` 到点后取消该协程并抛 BridgeTimeout —— 调用方必须把
        对应上下文视为不可复用。
        """
        try:
            self.start()
        except BaseException:
            # 协程还没提交,必须显式关闭,否则会出现 "never awaited" 警告
            coro.close()
            raise
        loop = self._loop
        if self._stopping or loop is None or loop.is_closed():
            # stop() 已经叫停了循环:此时提交的协程永远不会被执行,
            # 必须立刻失败,否则调用方要白等到自己的超时。
            coro.close()
            raise HostDown("宿主事件循环不可用")
        if threading.current_thread() is self._thread:
            coro.close()
            raise HostDown("不能在宿主循环线程里同步等待自己(会自锁)")
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise BridgeTimeout(
                f"页面操作超过 {timeout:.1f}s 未返回" if timeout
                else "页面操作未返回") from None
        except concurrent.futures.CancelledError:
            raise HostDown("宿主正在关闭,任务被取消") from None

    # ---------------- 协程接口(只在宿主循环里执行) ----------------

    async def new_context(self, fp, state_path: str | None = None):
        """按指纹开一个新的 BrowserContext;可选载入 storage_state。

        载入失败会原样抛出,由 Worker 决定隔离坏 cookie 再退到干净上下文
        —— cookie 的隔离策略只应该有一处实现。
        """
        browser = await self._ensure_browser()
        kw = dict(fp.context_kwargs())
        if state_path:
            kw["storage_state"] = state_path
        try:
            ctx = await browser.new_context(**kw)
        except BaseException as e:                     # noqa: BLE001
            if state_path:
                raise
            # 没有 cookie 参与还失败,基本都是浏览器进程没了:重启再试一次
            log.warning("[%s] 新建上下文失败(%s),重启浏览器后重试", self.name, e)
            browser = await self._ensure_browser()
            ctx = await browser.new_context(**kw)
        return ctx

    def page_opened(self) -> None:
        self._pages += 1

    def page_closed(self) -> None:
        self._pages = max(0, self._pages - 1)

    def info(self) -> dict:
        return {
            "host": self.name,
            "host_ok": self.alive(),
            "pages": self._pages,
            "launches": self._launches,
            "uptime_s": round(time.time() - self._started_at, 1)
            if self._started_at else 0.0,
            "last_err": self._last_launch_err,
        }

    # ---------------- 关闭 ----------------

    def _teardown_coro(self):
        async def _do():
            b, self._browser = self._browser, None
            if b is not None:
                try:
                    await asyncio.wait_for(b.close(), 10)
                except BaseException as e:             # noqa: BLE001
                    log.warning("[%s] 关闭浏览器出错: %s", self.name, e)
            pw, self._pw = self._pw, None
            if pw is not None:
                try:
                    await asyncio.wait_for(pw.stop(), 10)
                except BaseException as e:             # noqa: BLE001
                    log.warning("[%s] 停止 Playwright 出错: %s", self.name, e)
            self._pages = 0
        return _do()

    async def _teardown(self) -> None:
        await self._teardown_coro()

    def stop(self, timeout: float | None = None) -> None:
        """停循环线程并关掉浏览器/驱动。可重复调用。"""
        with self._lock:
            if self._stopping:
                t = self._thread
            else:
                self._stopping = True
                t = self._thread
                loop = self._loop
                if loop is not None and not loop.is_closed():
                    try:
                        loop.call_soon_threadsafe(loop.stop)
                    except RuntimeError:
                        pass
                self._booted.set()          # 放走还在等启动的线程
        if t is not None and t.is_alive():
            t.join(timeout if timeout is not None else self.CLOSE_TIMEOUT)
            if t.is_alive():
                log.warning("[%s] 事件循环线程 %.0fs 内未退出,放弃等待",
                            self.name, timeout if timeout is not None
                            else self.CLOSE_TIMEOUT)
        self._thread = None
        self._loop = None
        log.info("[%s] 共享浏览器宿主已关闭(累计启动 %d 次,残留页面 %d)",
                 self.name, self._launches, self._pages)


# ---------------------------------------------------------------- 会话
class BrowserSession:
    """一个 Slot 的会话:只持有一个 context + 一个 page,共享宿主浏览器。

    对外行为与上一版一致:换租户强制新导航、身份到上限轮换、空壳换身份
    重试一次、goto 异常重建重试一次;新增的只是「一次请求一个 deadline」
    与「调用方走了就立刻收手」。
    """

    # 剩余预算低于这个值就不再重试:重试只会把超时往后拖,没有成功可能
    RETRY_RESERVE = 12.0
    # 桥接等待比内部 deadline 多给的收尾余量(存 cookie / 关上下文)
    BRIDGE_GRACE = 8.0

    def __init__(self, sid: int, cfg, host: PlaywrightHost | None = None):
        self.sid = sid
        self.cfg = cfg
        self.seq = 1
        self.worker = None
        self.cur_user: str | None = None
        self._host = host
        self._poisoned = False        # 上下文不可复用(桥接超时/被取消)
        self._bridge_fails = 0

    # ---------------- 宿主 ----------------

    def _host_obj(self) -> PlaywrightHost:
        if self._host is None:
            self._host = PlaywrightHost(self.cfg)
        return self._host

    def _bounded(self, deadline: float, cap: float) -> float:
        """还剩多少秒可用,封顶 cap,至少 2s(给关闭/重建留操作空间)。"""
        return max(2.0, min(cap, deadline - time.monotonic()))

    # ---------------- worker 生命周期 ----------------

    def _new_worker(self, user_id: str):
        from ..worker import Worker
        return Worker(self.sid, self.cfg, self.seq,
                      user_id=user_id, host=self._host_obj())

    def _drop_worker(self, why: str = "") -> None:
        """尽力释放当前 worker 的上下文;关不掉就放弃(浏览器整体还活着)。"""
        w, self.worker = self.worker, None
        if w is None:
            return
        host = self._host_obj()
        try:
            host.run(w.close(), 6.0)
        except Exception as e:                         # noqa: BLE001
            log.warning("[S%d] 释放上下文失败(%s): %s", self.sid, why or "-", e)

    def _recover(self, host: PlaywrightHost, deadline: float, what: str,
                 user_id: str | None = None) -> bool:
        """空壳/异常后的重建。返回是否成功(失败就不再重试)。"""
        left = self._bounded(deadline, 30.0)
        w = self.worker
        try:
            if w is None:
                self.worker = self._new_worker(user_id or self.cur_user or "public")
                host.run(self.worker.start(), left)
                return True
            # 只重建 context(换指纹/换 cookie),不重启浏览器
            self.seq += 1
            host.run(w.rotate(self.seq, user_id=user_id), left)
            self._poisoned = False
            return True
        except Exception as e:                         # noqa: BLE001
            log.warning("[S%d] %s 后重建失败: %s", self.sid, what, e)
            self._drop_worker("重建失败")
            self._poisoned = True
            try:
                self.seq += 1
                self.worker = self._new_worker(user_id or self.cur_user or "public")
                host.run(self.worker.start(), self._bounded(deadline, 30.0))
                self._poisoned = False
                return True
            except Exception as e2:                     # noqa: BLE001
                log.error("[S%d] 彻底重建也失败: %s", self.sid, e2)
                self.worker = None
                return False

    def _prepare(self, host: PlaywrightHost, deadline: float,
                 user_id: str) -> None:
        """确保上下文可用:必要时新建 / 换身份 / 复活。失败抛异常。"""
        if self.worker is None:
            self.worker = self._new_worker(user_id)
            host.run(self.worker.start(), self._bounded(deadline, 45.0))
            self._poisoned = False
            return
        if self._poisoned:
            log.warning("[S%d] 上一次页面操作未正常结束,换新上下文", self.sid)
            self._drop_worker("上下文已污染")
            self.seq += 1
            self.worker = self._new_worker(user_id)
            host.run(self.worker.start(), self._bounded(deadline, 45.0))
            self._poisoned = False
            return
        if self.worker.wants_rotation():
            self.seq += 1
            log.info("[S%d] 身份到达上限(被拦 %d 次 / 已服务 %d 次),轮换 #%d",
                     self.sid, self.worker.block_streak,
                     self.worker.requests, self.seq)
            host.run(self.worker.rotate(self.seq), self._bounded(deadline, 30.0))
            return
        # 页面还活着吗:一次轻量 IPC,能抓到渲染进程崩溃(崩溃时
        # page.is_closed() 未必为真,但 evaluate 一定失败)。
        host.run(self.worker.start(), self._bounded(deadline, 30.0))
        alive = host.run(self.worker.alive(), self._bounded(deadline, 10.0))
        if not alive:
            log.warning("[S%d] 页面已失效,换新上下文", self.sid)
            self.seq += 1
            host.run(self.worker.rotate(self.seq), self._bounded(deadline, 30.0))

    # ---------------- 主流程 ----------------

    def ask(self, question: str, fresh: bool, user_id: str,
            timeout: float | None = None, abort=None) -> dict:
        """一次提问。所有重试共享同一个 deadline,到点就带着 TIMEOUT 返回。"""
        budget = float(timeout) if timeout else float(
            getattr(self.cfg, "answer_timeout", 120.0) or 120.0)
        budget = max(5.0, budget)
        deadline = time.monotonic() + budget
        host = self._host_obj()

        try:
            host.start()
        except Exception as e:                         # noqa: BLE001
            return {"kind": "ERROR", "error": f"浏览器不可用: {e}"[:300]}

        # --- 会话隔离:换租户必须断开上一家的上下文 ---
        switched = self.cur_user is not None and self.cur_user != user_id
        if switched:
            fresh = True              # 绝不在 A 的对话里替 B 追问
            if self.worker is not None and self.cfg.tenant_rotate_identity:
                if self._recover(host, deadline, "租户切换", user_id=user_id):
                    log.info("[S%d] 租户切换 %s -> %s,已换身份 #%d",
                             self.sid, self.cur_user, user_id, self.seq)
                else:
                    log.warning("[S%d] 租户切换换身份失败,沿用当前身份", self.sid)
        self.cur_user = user_id

        res = self._attempt(host, question, fresh, user_id, deadline, abort)
        # 空壳(身份受限)与 goto 异常各给一次机会,但只在预算还够时
        shell_done = failed_done = False
        while True:
            kind = res.get("kind")
            err = res.get("error") or ""
            if kind in ("TIMEOUT", "CANCELLED", "OK", "BLOCKED", "TOOLONG",
                        "STALE", "UNAVAILABLE"):
                break
            if not shell_done and kind == "EMPTY" and "空壳" in err:
                shell_done, what = True, "shell"
            elif not failed_done and (kind == "FAILED" or "goto" in err):
                failed_done, what = True, "failed"
            else:
                break
            left = deadline - time.monotonic()
            if left < self.RETRY_RESERVE:
                log.warning("[S%d] %s 后重试需要预算,剩余仅 %.1fs,直接返回",
                            self.sid, what, left)
                res["retry_skipped"] = what
                break
            log.warning("[S%d] %s(%s),重建后重试(剩余 %.1fs)",
                        self.sid, "身份受限" if what == "shell" else "页面异常",
                        err or kind, left)
            if not self._recover(host, deadline, what,
                                 user_id=user_id if what == "shell" else None):
                break
            res = self._attempt(host, question, True, user_id, deadline, abort)

        w = self.worker
        res["identity_used"] = getattr(w, "requests", 0)
        res["identity_cap"] = self.cfg.max_requests_per_identity
        res["budget_s"] = round(budget, 2)
        res["used_s"] = round(budget - max(0.0, deadline - time.monotonic()), 2)
        return res

    def _attempt(self, host: PlaywrightHost, question: str, fresh: bool,
                 user_id: str, deadline: float, abort) -> dict:
        """单次尝试:准备上下文 → 桥接执行 → 结果归一化。"""
        left = deadline - time.monotonic()
        if left <= 2.0:
            return {"kind": "TIMEOUT", "error": "剩余时间不足,未发起请求"}
        try:
            self._prepare(host, deadline, user_id)
        except BridgeTimeout as e:
            self._poisoned = True
            self._bridge_fails += 1
            log.error("[S%d] 准备上下文超时(%s),隔离当前上下文", self.sid, e)
            return {"kind": "TIMEOUT", "error": f"页面准备超时: {e}"[:300]}
        except Exception as e:                        # noqa: BLE001
            self._poisoned = True
            self._drop_worker("准备失败")
            log.exception("[S%d] 准备上下文失败", self.sid)
            return {"kind": "ERROR", "error": f"页面准备失败: {e}"[:300]}

        if self.worker is None:
            return {"kind": "ERROR", "error": "会话没有可用上下文"}

        left = deadline - time.monotonic()
        if left <= 1.0:
            return {"kind": "TIMEOUT", "error": "剩余时间不足,未发起请求"}
        bridge = left + self.BRIDGE_GRACE
        try:
            res = host.run(self.worker.ask(question, fresh=fresh,
                                           timeout=left, abort=abort), bridge)
            self._bridge_fails = 0
        except BridgeTimeout as e:
            # 内部本该在 left 秒内自己收手;超过 left+GRACE 说明某次 CDP
            # 调用卡住了。这个页面已经不可信,隔离掉,不要复用。
            self._poisoned = True
            self._bridge_fails += 1
            log.error("[S%d] 页面操作卡死(%s,已 %.0f 次),隔离上下文",
                      self.sid, e, self._bridge_fails)
            return {"kind": "TIMEOUT", "error": f"浏览器调用超时: {e}"[:300]}
        except HostDown as e:
            self._poisoned = True
            log.error("[S%d] 浏览器宿主不可用: %s", self.sid, e)
            return {"kind": "ERROR", "error": f"浏览器宿主不可用: {e}"[:300]}
        except Exception as e:                        # noqa: BLE001
            self._poisoned = True
            log.exception("[S%d] ask 异常,上下文隔离", self.sid)
            return {"kind": "ERROR", "error": str(e)[:300]}
        if not isinstance(res, dict):
            return {"kind": "ERROR", "error": "no result"}
        return res

    # ---------------- 其它 ----------------

    def info(self) -> dict:
        """面板槽位卡片的附加字段(与上一版同名,只多两个观测字段)。"""
        out = {"seq": self.seq, "session_user": self.cur_user}
        host = self._host
        if host is not None:
            out["pages"] = host.pages
            out["host_ok"] = host.alive()
        return out

    def close(self) -> None:
        try:
            self._drop_worker("会话关闭")
        finally:
            self.worker = None
            self._poisoned = False


class BrowserBackend:
    name = "browser"

    def __init__(self, cfg):
        self.cfg = cfg
        # 一个 Backend 一个宿主。热切换后端时 Pool 会 close() 旧的,
        # 旧宿主的浏览器随之关闭,不会留下孤儿 Chrome。
        self.host = PlaywrightHost(cfg)
        self._closed = False
        if int(getattr(cfg, "min_workers", 1) or 0) > 0:
            self._prewarm()

    def _prewarm(self) -> None:
        """后台把浏览器拉起来:首个请求不必再等 2.5s 冷启动。

        min_workers=0 时不预热,避免「不起浏览器」的部署也拉起 Chrome。
        """
        def _go():
            try:
                self.host.start()
                log.info("共享浏览器已预热(首个请求无需冷启动)")
            except Exception as e:                    # noqa: BLE001
                log.warning("共享浏览器预热失败(将按需重试): %s", e)
        threading.Thread(target=_go, name="pw-prewarm", daemon=True).start()

    def new_session(self, sid: int) -> BrowserSession:
        return BrowserSession(sid, self.cfg, host=self.host)

    def info(self) -> dict:
        return self.host.info()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.host.stop()