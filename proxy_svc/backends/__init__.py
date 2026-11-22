"""可插拔后端：browser（真实浏览器）与 mock（本地模拟）。"""
from __future__ import annotations


def make_backend(name: str, cfg):
    kind = (name or "browser").strip().lower()
    if kind == "mock":
        from .mock import MockBackend
        return MockBackend(cfg)
    if kind == "browser":
        from .browser import BrowserBackend
        return BrowserBackend(cfg)
    raise ValueError(f"未知后端: {name!r}，只能是 browser / mock")
