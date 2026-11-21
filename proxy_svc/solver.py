"""YesCaptcha 打码：异步 createTask + 轮询 getTaskResult。

与原 gsearch.solve_captcha 等价，但去掉全局日志依赖，改为可注入 logger，
并支持并发安全（无共享可变状态）。
"""
from __future__ import annotations

import asyncio
import logging
import time
import urllib.error
import urllib.request
import json

log = logging.getLogger("solve")

NO_PROXY_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}))


def _post(base: str, path: str, payload: dict, timeout: float = 40) -> dict:
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    # 打码站本身不走出口代理
    with NO_PROXY_OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def solve(page, cfg, browser_ua: str | None = None,
          timeout: float | None = None) -> str | None:
    """在 /sorry/index 页面上解 recaptcha，返回 token。"""
    timeout = timeout or cfg.solve_timeout
    try:
        info = page.evaluate("""() => {
          const d = document.querySelector('[data-sitekey]');
          const s = document.querySelector('[data-s]');
          return {
            sitekey: d ? d.getAttribute('data-sitekey') : null,
            s: s ? s.getAttribute('data-s') : null,
            url: location.href
          };
        }""")
    except Exception as e:
        log.warning("抓 sitekey 失败: %s", e)
        return None

    sitekey = info.get("sitekey")
    if not sitekey:
        log.warning("页面无 data-sitekey，可能不是验证码页")
        return None

    payload = {
        "clientKey": cfg.yes_key,
        "task": {
            "type": "RecaptchaV2EnterpriseTaskProxyless",
            "websiteURL": info["url"],
            "websiteKey": sitekey,
            "userAgent": browser_ua or page.evaluate("navigator.userAgent"),
        }}
    if info.get("s"):
        payload["task"]["enterprisePayload"] = {"s": info["s"]}

    t0 = time.time()
    try:
        r = _post(cfg.yes_api, "/createTask", payload)
    except Exception as e:
        log.error("createTask 网络错误: %s", e)
        return None

    if r.get("errorId"):
        log.error("createTask 拒绝: %s %s", r.get("errorCode"),
                  r.get("errorDescription"))
        return None

    task_id = r.get("taskId")
    if not task_id:
        log.error("createTask 无 taskId: %s", r)
        return None

    log.info("打码任务 %s 已提交", task_id)
    while time.time() - t0 < timeout:
        time.sleep(5)
        try:
            r = _post(cfg.yes_api, "/getTaskResult",
                      {"clientKey": cfg.yes_key, "taskId": task_id})
        except Exception as e:
            log.warning("getTaskResult 网络错误: %s", e)
            continue
        if r.get("errorId"):
            log.error("getTaskResult 拒绝: %s", r.get("errorCode"))
            return None
        st = r.get("status")
        if st == "ready":
            tok = (r.get("solution") or {}).get("gRecaptchaResponse")
            log.info("打码成功 %.1fs (task %s)", time.time() - t0, task_id)
            return tok
        if st == "failed":
            log.error("打码失败: %s", r)
            return None
    log.error("打码超时 %.0fs", timeout)
    return None


# ---------------------------------------------------------------- 协程版
async def solve_async(page, cfg, browser_ua: str | None = None,
                      timeout: float | None = None, abort=None) -> str | None:
    """``solve`` 的协程版:同样的流程,但跑在浏览器宿主的事件循环里。

    - ``page.evaluate`` 直接 await(不能像 sync 版那样阻塞循环);
    - 打码站的 HTTP 调用(阻塞 socket)丢到线程池,避免堵住唯一的事件循环;
    - 轮询间隔 5s、总时长受 ``timeout`` 约束,并在每轮检查 ``abort``。
    """
    timeout = float(timeout or cfg.solve_timeout or 180.0)
    t0 = time.monotonic()

    def _left() -> float:
        return max(0.0, timeout - (time.monotonic() - t0))

    def _aborted() -> bool:
        try:
            return bool(abort()) if abort is not None else False
        except Exception:
            return False

    try:
        info = await page.evaluate("""() => {
          const d = document.querySelector('[data-sitekey]');
          const s = document.querySelector('[data-s]');
          return {
            sitekey: d ? d.getAttribute('data-sitekey') : null,
            s: s ? s.getAttribute('data-s') : null,
            url: location.href
          };
        }""")
    except Exception as e:
        log.warning("抓 sitekey 失败: %s", e)
        return None

    sitekey = info.get("sitekey")
    if not sitekey:
        log.warning("页面无 data-sitekey,可能不是验证码页")
        return None

    payload = {
        "clientKey": cfg.yes_key,
        "task": {
            "type": "RecaptchaV2EnterpriseTaskProxyless",
            "websiteURL": info["url"],
            "websiteKey": sitekey,
            "userAgent": browser_ua or await page.evaluate("navigator.userAgent"),
        }}
    if info.get("s"):
        payload["task"]["enterprisePayload"] = {"s": info["s"]}

    try:
        r = await asyncio.to_thread(_post, cfg.yes_api, "/createTask", payload)
    except Exception as e:
        log.error("createTask 网络错误: %s", e)
        return None

    if r.get("errorId"):
        log.error("createTask 拒绝: %s %s", r.get("errorCode"),
                  r.get("errorDescription"))
        return None

    task_id = r.get("taskId")
    if not task_id:
        log.error("createTask 无 taskId: %s", r)
        return None

    log.info("打码任务 %s 已提交", task_id)
    while _left() > 0:
        if _aborted():
            log.info("调用方已放弃,停止轮询打码结果(%.1fs)", time.monotonic() - t0)
            return None
        step = min(5.0, _left())
        if step <= 0:
            break
        await asyncio.sleep(step)
        try:
            r = await asyncio.to_thread(
                _post, cfg.yes_api, "/getTaskResult",
                {"clientKey": cfg.yes_key, "taskId": task_id})
        except Exception as e:
            log.warning("getTaskResult 网络错误: %s", e)
            continue
        if r.get("errorId"):
            log.error("getTaskResult 拒绝: %s", r.get("errorCode"))
            return None
        st = r.get("status")
        if st == "ready":
            tok = (r.get("solution") or {}).get("gRecaptchaResponse")
            log.info("打码成功 %.1fs (task %s)", time.monotonic() - t0, task_id)
            return tok
        if st == "failed":
            log.error("打码失败: %s", r)
            return None
    log.error("打码超时 %.0fs", timeout)
    return None
