"""后端接口约定。

一个 Backend 负责创建 Session;一个 Session 由池子里的一个线程独占。
注意:在共享浏览器架构下,Session **不再**独占浏览器进程,它只独占
一个 BrowserContext + 一个 Page;真正的 Playwright 驱动与浏览器实例由
backends.browser.PlaywrightHost 全进程唯一持有。

Session.ask 必须返回 dict,至少包含:
  kind    OK / EMPTY / FAILED / STALE / TOOLONG / BLOCKED / UNAVAILABLE /
          TIMEOUT / CANCELLED / ERROR
  answer  kind == OK 时的回答文本
  error   失败原因(可选)
其余字段原样透传给调用方。

ask 的额外两个可选参数是本次重构的契约要点:
  timeout  这一次请求允许花掉的总时间(秒)。实现必须在入口把它换算成
           唯一 deadline,内部所有等待都只看剩余时间,不得各自累加;
           到点就带着 TIMEOUT 尽快返回,把槽位还回池子。
  abort    无参可调用对象,返回 True 表示调用方已经走了(客户端断开、
           等待超时、看门狗到点或进程关闭)。实现应在每个轮询点检查它,
           命中后立刻返回 CANCELLED,不要继续把页面操作跑完。
"""
from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

AbortFn = Callable[[], bool]


@runtime_checkable
class Session(Protocol):
    def ask(self, question: str, fresh: bool, user_id: str,
            timeout: float | None = None,
            abort: "AbortFn | None" = None) -> dict: ...

    def close(self) -> None: ...


@runtime_checkable
class Backend(Protocol):
    name: str

    def new_session(self, sid: int) -> Session: ...

    def close(self) -> None: ...