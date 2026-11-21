"""指纹生成：每次重建身份时换一套 UA / 视口 / 时区 / locale。

重点来自实测：单纯 headless 会被判定为
"AI Mode is not currently available on your device or account"，
抹掉 navigator.webdriver 并换成真实桌面 UA 后即可正常返回内容。
"""
from __future__ import annotations

import random

# UA 必须和本机实际启动的 Chrome 主版本一致。
# 本机是 Chrome 157 / Win64；混入 139/140、Mac、Linux 会和
# Sec-CH-UA、TLS、平台指纹对不上，Google 直接给一个只有顶栏的壳。
_UAS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36", "Win32"),
]

_VIEWPORTS = [
    (1440, 900), (1536, 864), (1920, 1080), (1600, 900), (1366, 768),
]

_LOCALES = [
    ("en-US", "America/Los_Angeles"),
    ("en-US", "America/New_York"),
    ("en-US", "America/Chicago"),
    ("en-GB", "Europe/London"),
]

# 必须在任何页面脚本之前执行
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages',
    {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins',
    {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'platform',
    {get: () => '%PLATFORM%'});
window.chrome = window.chrome || {runtime: {}};
const _q = navigator.permissions && navigator.permissions.query;
if (_q) {
  navigator.permissions.query = (p) =>
    p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : _q.call(navigator.permissions, p);
}
"""


class Fingerprint:
    __slots__ = ("seq", "ua", "platform", "viewport", "locale", "timezone")

    def __init__(self, seq: int):
        self.seq = seq
        self.ua, self.platform = random.choice(_UAS)
        self.viewport = random.choice(_VIEWPORTS)
        self.locale, self.timezone = random.choice(_LOCALES)

    @property
    def stealth_js(self) -> str:
        return STEALTH_JS.replace("%PLATFORM%", self.platform)

    def context_kwargs(self) -> dict:
        return {
            "user_agent": self.ua,
            "locale": self.locale,
            "timezone_id": self.timezone,
            "viewport": {"width": self.viewport[0],
                         "height": self.viewport[1]},
        }

    def __repr__(self) -> str:
        return (f"<FP#{self.seq} {self.viewport[0]}x{self.viewport[1]} "
                f"{self.timezone}>")
