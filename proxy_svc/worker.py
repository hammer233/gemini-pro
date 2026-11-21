"""单个 worker:在**共享浏览器**里独占一个 BrowserContext + Page,负责真正取答案。

相对上一版的四处结构性改动(抓取策略、判定阈值、返回字段全部保持不变):

1) 共享浏览器进程
   上一版每个 worker 自己 ``sync_playwright().start()`` + ``chromium.launch()``:
   N 个并发槽位 = N 个 node 驱动进程 + N 个 Chrome 主进程(2~4 个就吃满 4GB)。
   现在 Playwright 驱动和 Browser 由 backends.browser.PlaywrightHost 全进程唯一持有,
   worker 只申请属于自己的 BrowserContext + Page;换身份也只重建上下文,
   不再重启浏览器,省掉「node 进程 + Chrome 主进程 + 2s 冷启动」。

2) 全局时间预算(deadline)
   ``ask(question, fresh, timeout, abort)`` 一进函数就敲定
   ``deadline = monotonic() + timeout``;goto / settle / 打码 / 等答案
   全部按「剩余 = deadline - monotonic()」收紧,并检查调用方是否已放弃
   (``abort()`` 为真)。到点或放弃立刻返回,不再把 120s~180s 叠加起来
   撑爆上游 OpenAI 客户端的 ReadTimeout,也把槽位尽快还回池子。

3) cookie 只存干净状态 + 原子落盘
   ``_save_state()`` 只在 ``kind == "OK" 且 not self.dirty`` 时被调用;
   写入先落临时文件再 ``os.replace``,失败绝不截断旧文件;
   载入失败或连续被拦时把可疑文件隔离成 ``*.bad<ts>``,下次从干净身份开始。

4) 一次采样
   ``_probe()`` 与旧的 ``SNAP_JS`` 合并成一次 ``page.evaluate``:
   同一次往返里拿到 URL / 拦截判据 / 输入框 / 锚点数 / 答案正文;
   轮询间隔也从 ``page.wait_for_timeout``(一次 CDP 往返)换成 ``asyncio.sleep``。
   等答案时每次轮询的 IPC 次数从 2 次降到 1 次,且空转不再产生往返。

5) 取答案层:网络响应旁路(原始 Markdown)
   除 DOM 轮询外,再被动监听 ``page.on("response")``,从承载答案的接口
   响应里直接取原始数据(见 ``NetworkTap``):
     * DOM 的 ``innerText`` 会把 Markdown 压平(代码块缩进/换行丢失),
       接口原文是 Markdown,``\u0060\u0060\u0060`` 代码块可以原样返回;
     * 用网络活动驱动 DOM 采样节奏:AI 思考期间(无响应、无锚点)按
       0.22s → 1.2s 指数退避,几乎不产生 ``evaluate`` 往返;
     * 只旁听,不 ``route``/``fulfill``(那样会把整个响应体读进内存,
       并把页面的流式输出截断);单次读 body 带超时与大小上限;
     * 归属靠「问题原文是否出现在请求 URL / POST body 里」+ 与 DOM 答案
       做词集比对双重校验;校验不过就只留诊断不参与返回,
       DOM 路径永远是最终兜底。

实测时间线(headed / headless 均适用):
  浏览器启动  0.25s(现已全进程一次)
  首次导航    2.5–2.7s
  首个答案就绪 ~7s(含渲染)
  同页追问答案 ~3s
被拦时:judge=BLOCKED → 打码 ~38s → 提交 token → 页面自行跳转到真实结果
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.parse

from .fingerprint import Fingerprint
from .solver import solve_async

log = logging.getLogger("worker")

SEARCH = "https://www.google.com/search?q={q}&udm=50&hl=en&gl=us"

# 从正文里截出答案:锚点之后,到尾部 UI 噪声之前
_TAIL_NOISE = (
    "Skip to previous prompt",
    "AI Mode response is ready",
    "AI Mode is thinking about your query",
    "AI can make mistakes",
    "Something went wrong and an AI response wasn't generated.",
    "Seems there was a brief connection drop. Try again.",
)

ANCHOR = "AI Mode reply for"

# ---------------------------------------------------------------- 返回标记
# 内部标记 -> (对外 kind, 说明)。对外 kind 必须在 app._KIND_HTTP 里能映射到状态码。
M_OK = "OK"
M_BLOCKED = "__BLOCKED__"
M_TOOLONG = "__TOOLONG__"
M_FAILED = "__FAILED__"
M_STALE = "__STALE__"
M_SHELL = "__SHELL__"
M_TIMEOUT = "__TIMEOUT__"
M_ABORTED = "__ABORTED__"

_MARKERS: dict[str, tuple[str, str]] = {
    M_BLOCKED: ("BLOCKED", "被 Google 拦截"),
    M_TOOLONG: ("TOOLONG", "输入超长,页面已冻结"),
    M_FAILED: ("FAILED", "页面生成失败"),
    M_STALE: ("STALE", "答案归属不明(疑上一轮残留)"),
    M_SHELL: ("EMPTY", "页面空壳(疑似身份受限)"),
    M_TIMEOUT: ("TIMEOUT", "超出本次请求的总时间预算"),
    M_ABORTED: ("CANCELLED", "调用方已放弃请求"),
}

# 三次采样内仍能判定为拦截的文案(只在正文很薄时使用,避免正文里恰好
# 提到 "not a robot" 的正常答案被误判)
_BLOCK_WORDS = ("unusual traffic", "not a robot", "our systems have detected")

# 合并采样:一次往返同时给出「页面状态」和「本轮答案」。
# 参数 want = 至少出现多少个锚点才抽取答案正文(默认 0 = 不抽取)。
# 这样轮询早期(锚点还没出现)不会把整段正文搬回 Python,
# 而抽取条件与调用方的 min_anchors 判断保持完全一致。
PROBE_JS = """(want) => {
  const b = document.body ? document.body.innerText : '';
  const A = 'AI Mode reply for';
  let i = b.lastIndexOf(A), n = 0;
  for (let k = 0; k >= 0; k = b.indexOf(A, k + 1)) n++;
  let ans = '', echo = '', ready = false;
  const w = Number(want) || 0;
  if (w > 0 && i >= 0 && n >= w) {
    let s = b.slice(i + A.length);
    const nl = s.indexOf('\\n');
    if (nl >= 0) { echo = s.slice(0, nl); s = s.slice(nl + 1); }
    else { echo = s; s = ''; }
    ans = s;
    ready = b.slice(i).includes('AI Mode response is ready');
  }
  const tas = [...document.querySelectorAll('textarea')];
  const ta = tas.find(t => {
    const r = t.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  // 只有「没有锚点且正文很薄」才需要正文快照(空壳判定 / 兜底抠答案),
  // 其余情况不回传 1.6KB 字符串。
  const shell = n === 0 && b.length < 4000;
  return {
    url: location.href,
    body_len: b.length,
    head: shell ? b.replace(/\\s+/g, ' ').slice(0, 1600) : '',
    sitekey: !!document.querySelector('[data-sitekey]'),
    word: /unusual traffic|our systems have detected/i.test(b.slice(0, 2000)),
    ta: !!ta,
    ta_len: ta ? ta.value.length : -1,
    anchors: n,
    avail: /not currently available/i.test(b),
    failed: /wasn't generated|brief connection drop/i.test(b.slice(-1200)),
    answer: ans,
    echo: echo.trim(),
    ready: ready
  };
}"""

# 兼容旧名(有外部脚本 import 过它);新代码统一用 PROBE_JS
SNAP_JS = PROBE_JS

SET_JS = """(txt) => {
  const tas = [...document.querySelectorAll('textarea')];
  const ta = tas.find(t => {
    const r = t.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  if (!ta) return 'no-textarea';
  const proto = Object.getPrototypeOf(ta);
  const desc = Object.getOwnPropertyDescriptor(proto, 'value')
            || Object.getOwnPropertyDescriptor(
                 HTMLTextAreaElement.prototype, 'value');
  ta.focus();
  desc.set.call(ta, txt);
  ta.dispatchEvent(new Event('input', {bubbles: true}));
  ta.dispatchEvent(new Event('change', {bubbles: true}));
  return ta.value.length;
}"""

SEND_JS = """() => {
  const cs = [...document.querySelectorAll('button')].filter(b => {
    const a = (b.getAttribute('aria-label') || '').toLowerCase();
    const r = b.getBoundingClientRect();
    return r.width > 0 && r.height > 0
        && (a.includes('send') || a.includes('submit'));
  });
  if (!cs.length) return 'no-send';
  cs[0].click();
  return 'clicked';
}"""

CLEAR_JS = "() => { if (document.body) document.body.innerHTML = ''; }"

FILL_JS = """(tok) => {
  let ta = document.getElementById('g-recaptcha-response');
  let created = false;
  if (!ta) {
    // headless + 资源拦截下 reCAPTCHA widget 常常根本没渲染,
    // 页面上就不存在这个字段;但表单 POST 只认这个 name,
    // 所以缺了要自己补一个,否则永远打不开打码页。
    const f = document.getElementById('captcha-form')
           || document.querySelector('form');
    if (!f) return 'no-form';
    ta = document.createElement('textarea');
    ta.id = 'g-recaptcha-response';
    ta.name = 'g-recaptcha-response';
    ta.style.display = 'none';
    f.appendChild(ta);
    created = true;
  }
  ta.style.display = 'block';
  ta.value = tok;
  if (ta.value !== tok) return 'set-failed';
  return created ? 'created' : 'filled';
}"""

# 回填后必须等 reCAPTCHA 内部状态同步再提交,合并成一次 evaluate 会失败。
SUBMIT_JS = """(tok) => {
  if (typeof submitCallback === 'function') { submitCallback(tok); return 'callback'; }
  const f = document.getElementById('captcha-form');
  if (f) { f.submit(); return 'form'; }
  return 'none';
}"""


def _norm(s: str) -> str:
    """归一化问题文本:比对锚点回显和原始问题用。"""
    return " ".join((s or "").lower().split())[:120]


def _is_blocked(r: dict) -> bool:
    """一次采样结果 -> 是否被拦。

    sitekey / 明确的拦截文案 / 被重定向到 /sorry/ 三者取或;
    正文很薄时再补一次完整文案匹配(那里最容易漏掉 "not a robot")。
    """
    if r.get("sitekey") or r.get("word"):
        return True
    if "/sorry/" in (r.get("url") or ""):
        return True
    if (r.get("body_len") or 0) < 4000:
        head = (r.get("head") or "").lower()
        if head and any(w in head for w in _BLOCK_WORDS):
            return True
    return False


def answer_from_shell(head: str, question: str) -> str:
    """锚点缺失时,从搜索结果正文里把问题后面的答案抠出来。

    新版 AI Mode 有时不写 "AI Mode reply for" 这一行,正文是
    "... Search Results <问题原文> <答案> ..."。锚点判空会把
    已经生成好的答案丢掉。
    """
    text = " ".join((head or "").split())
    q = " ".join((question or "").split())
    if not text or not q:
        return ""
    i = text.lower().find(q.lower())
    if i < 0:
        return ""
    tail = text[i + len(q):].strip(" \t-:—")
    for stop in ("Skip to previous", "AI Mode response is ready",
                 "AI can make mistakes", "Sign in"):
        k = tail.find(stop)
        if k > 0:
            tail = tail[:k]
    tail = tail.strip()
    if len(tail) < 40:
        return ""
    return tail


def clean_answer(raw: str) -> str:
    if not raw:
        return ""
    cut = len(raw)
    for n in _TAIL_NOISE:
        k = raw.find(n)
        if 0 <= k < cut:
            cut = k
    return raw[:cut].strip()


def url_for(q: str) -> str:
    return SEARCH.format(q=urllib.parse.quote_plus(q))


# ======================================================================
# 取答案层:网络响应旁路
# ======================================================================
#
# 思路:AI Mode 的答案本来就是一段 Markdown,页面前端把它渲染成 DOM;
# 我们再从 DOM 的 innerText 里抠回来,等于做了一次有损往返(代码块缩进、
# 空行、列表符号全被压平)。旁听承载答案的接口响应可以直接拿到原文。
#
# 但这条路径只能当「增值」不能当「权威」:
#   * RPC 的 URL / rpcid / 报文结构都未公开,而且会变;这里不对 schema
#     做任何假设,只从任意 JSON 里挑「最像答案的字符串」;
#   * 因此归属只能靠旁证:问题原文要出现在该响应的 URL 或 POST body 里,
#     并且拿到的原文必须能在 DOM 抽出的答案里找到(或 DOM 为空)。
#     对不上就整条丢弃,DOM 结果原样返回(见 Worker._answer_text)。
#   * 只 ``page.on("response")`` 被动旁听。绝不 ``route`` + ``fulfill``:
#     那要把整个响应体读进内存才放行,会把页面的流式增量截断。

# 哪些 URL 可能承载答案(命中任一即可;命中越窄,读 body 的次数越少)
_NET_URL_HINTS = ("batchexecute", "rpcids", "/async/", "udm=50")
# 允许读取的 content-type 前缀;text/html(页面本身)明确排除在外
_NET_TYPE_HINTS = ("application/json", "text/plain", "application/x-",
                   "text/x-", "application/x-protobuf", "text/event-stream")
# 单个响应体超过这个字节数就不读了(正常答案几百 KB 以内)
_NET_BODY_MAX = 2 * 1024 * 1024
# 读 body 的硬超时:Response 的 channel 调用是 NO_TIMEOUT,必须自己兜住
_NET_READ_TIMEOUT = 3.0
# 一次作答窗口内最多旁听多少条响应(防御性上限,正常个位数)
_NET_MAX_HITS = 60
# 「像答案」的最低长度:再短的串基本都是 id / 语言码 / 状态字段
_NET_MIN_CHARS = 28
# 引号内字符串扫描(非 JSON / 流式未闭合时的兜底)
_QUOTED_RE = re.compile(r'"((?:[^"\\]|\\.){%d,})"' % _NET_MIN_CHARS)
_JSONP_HEAD = ")]}'"


def _strip_jsonp(body: str) -> str:
    """去掉 Google RPC 的 XSSI 前缀 ``)]}'``。"""
    s = (body or "").lstrip()
    if s.startswith(_JSONP_HEAD):
        s = s[len(_JSONP_HEAD):]
    return s.strip()


def _flat(s: str) -> str:
    """小写 + 折叠空白,但**不截断**。

    归属校验要在整段 URL 和 POST body 里找问题原文;``_norm`` 会截到
    120 字,URL 前缀(``/batchexecute?rpcids=...``)就可能把 120 字吃完,
    导致 body 里明明有问题却匹配不到。这里只做归一,长度留给调用方。
    """
    return " ".join((s or "").lower().split())


def _walk_strings(node, out: list, depth: int = 0) -> None:
    """递归收集 JSON 里的所有字符串;内层若是「被转义的 JSON 串」再展开。

    batchexecute 的答案通常就是这种套娃:外层数组里有一项本身是
    JSON 文本,真正的 Markdown 在它里面。不做递归就永远捞不到。

    能解析成 list/dict 的字符串只展开、本身不作为候选:它是容器,
    长度往往比里面真正的答案还长,留着它会以「最长者胜」把答案挤掉。
    """
    if depth > 24:
        return
    if isinstance(node, str):
        t = node.strip()
        if len(t) > 2 and t[0] in "[{" and t[-1] in "]}":
            try:
                _walk_strings(json.loads(t), out, depth + 1)
                return             # 容器只展开,不参与评选
            except Exception:
                pass
        out.append(node)
    elif isinstance(node, (list, tuple)):
        for it in node:
            _walk_strings(it, out, depth + 1)
    elif isinstance(node, dict):
        for it in node.values():
            _walk_strings(it, out, depth + 1)


def _looks_like_answer(s: str) -> bool:
    """粗判一段字符串是不是「人话答案」。

    只做排除法,不做语义判断:短于底线、没有字母、像 id/语言码/协议
    标签的一律否掉;英文看空格数,中文看汉字占比。
    """
    t = s.strip()
    if len(t) < _NET_MIN_CHARS:
        return False
    low = t.lower()
    if low in ("en", "zh", "generic", "wrb.fr", "ok", "null", "true", "false"):
        return False
    if not any(c.isalpha() or "\u4e00" <= c <= "\u9fff" for c in t):
        return False
    if t.count(" ") >= 3:
        return True
    # 中文答案词间没有空格,只能按汉字占比判:过半是汉字才算散文
    cjk = sum(1 for c in t if "\u4e00" <= c <= "\u9fff")
    return cjk * 2 >= len(t)


def extract_markdown(body: str) -> tuple[str, str]:
    """从响应体里抠出最像答案的一段原文。返回 ``(文本, 模式)``。

    模式:``json`` 表示整段能解析成 JSON(走结构化遍历);
    ``scan`` 表示不是合法 JSON(流式未闭合 / 纯文本),退回到
    「引号内长字符串」扫描。两种都失败就返回空串,让调用方退回 DOM。
    """
    s = _strip_jsonp(body)
    if not s:
        return "", ""
    cands: list[str] = []
    mode = "json"
    try:
        _walk_strings(json.loads(s), cands)
    except Exception:
        cands = []
        mode = "scan"
    if not cands:
        mode = "scan"
        for m in _QUOTED_RE.finditer(s):
            raw = m.group(1)
            try:
                # 用 json 解码而不是手工 replace:转义/代理对都能正确还原
                cands.append(json.loads('"' + raw + '"'))
            except Exception:
                cands.append(raw)
    best = ""
    for c in cands:
        if not isinstance(c, str) or not _looks_like_answer(c):
            continue
        c = c.strip()
        if len(c) > len(best):
            best = c
    return best, (mode if best else "")


def _owner_hit(question: str, url: str, post: str) -> bool:
    """问题原文是否出现在这次请求里(URL 或 POST body,均已解码)。

    ``question`` 需先经 ``_flat`` 归一(``NetworkTap.reset`` 就是这么存的)。
    只比对前 40 字:长 prompt 的后半段常被前端截断或改写,拿整段做
    匹配反而会更早失配。
    """
    head = (question or "")[:40]
    if not head:
        return False
    hay = _flat(urllib.parse.unquote_plus(url or "")) + " " \
        + _flat(urllib.parse.unquote_plus(post or ""))
    return head in hay


class NetworkTap:
    """被动旁听一组响应,把承载答案的**原始 Markdown**攒下来。

    生命周期与 ``Worker`` 的一个 page 绑死:``_open()`` 里 ``bind()``,
    ``_close_ctx()`` 里 ``close()``。每次提问前 ``reset(question)``,
    这样上一轮迟到的响应会因为 epoch 不匹配而被丢弃——这正是 DOM 路径
    里 ``M_STALE`` 想防的那类串答案。

    线程模型:所有方法都在宿主的那个事件循环里跑。监听回调由 Playwright
    自己 ensure_future 成 Task,因此必须是 ``async def`` 并且自己吞掉
    全部异常——监听器抛出的异常会被记到 Connection 上,下一次任意 API
    调用时才会炸出来,极难定位。
    """

    def __init__(self, wid: int, cfg):
        self.wid = wid
        self.cfg = cfg
        self.epoch = 0          # 第几次提问;迟到的响应靠它丢弃
        self.q_norm = ""        # 当前问题的归一串(归属校验用,_flat 后不截断)
        self.best = ""          # 目前拿到的最长「像答案」的原文
        self.best_at = 0.0
        self.chunks = 0         # 命中的响应条数
        self.hits = 0           # 归属校验通过的条数
        self.misses = 0         # 归属校验失败 / 超限 / 读失败
        self.last_event = 0.0   # 最近一次命中的 monotonic 时间(驱动采样退避)
        self.closed = False
        self.dumps = 0

    # ---------- 生命周期 ----------

    def reset(self, question: str) -> None:
        """新问题开始:作废旧 epoch,清空已攒的原文。"""
        self.epoch += 1
        self.q_norm = _flat(question)   # 当前问题的归一串(不截断,归属校验用)
        self.best = ""
        self.best_at = 0.0
        self.chunks = 0
        self.hits = 0
        self.misses = 0
        self.last_event = 0.0

    def bind(self, page) -> None:
        if self.closed:
            return
        page.on("response", self._on_response)

    def close(self) -> None:
        """解绑靠销毁 page;这里只把状态作废,迟到的回调自行丢弃。"""
        self.closed = True
        self.best = ""
        self.epoch += 1

    def summary(self) -> dict:
        return {"hits": self.hits, "miss": self.misses,
                "chars": len(self.best),
                "seed": round(self.best_at, 2)}

    # ---------- 监听 ----------

    async def _on_response(self, resp) -> None:
        """Playwright 的 response 事件回调(async,自己吞异常)。"""
        try:
            await self._handle(resp)
        except Exception:
            # 绝不能让异常冒到 Connection 上(会在下一次任意 IPC 时炸)
            self.misses += 1

    async def _handle(self, resp) -> None:
        if self.closed:
            return
        req = getattr(resp, "request", None)
        if req is not None and req.resource_type in ("image", "media", "font"):
            return
        url = resp.url or ""
        if not url or not any(h in url for h in _NET_URL_HINTS):
            return
        ctype = (resp.headers or {}).get("content-type", "").lower()
        if ctype and not any(ctype.startswith(h) for h in _NET_TYPE_HINTS):
            return                     # text/html(页面本身)在这里被排除
        if resp.status and resp.status >= 400:
            return
        try:
            declared = int((resp.headers or {}).get("content-length") or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared > _NET_BODY_MAX:
            self.misses += 1
            return
        if self.chunks >= _NET_MAX_HITS:
            return
        # 归属:问题必须在请求 URL 或 POST body 里出现过。
        # epoch 在 await 之前就记下来:读 body 期间如果换了问题,整条丢弃。
        epoch = self.epoch
        qn = self.q_norm
        if not qn:
            return
        post = ""
        if req is not None:
            try:
                post = req.post_data or ""
            except Exception:
                post = ""
        if not _owner_hit(qn, url, post):
            self.misses += 1
            return
        try:
            text = await asyncio.wait_for(resp.text(),
                                          timeout=_NET_READ_TIMEOUT)
        except Exception:
            self.misses += 1           # 超时 / 响应被中止 / 流未结束
            return
        if self.closed or epoch != self.epoch:
            return                     # 上一轮问题的迟到响应
        self.chunks += 1
        self.last_event = time.monotonic()
        if len(text or "") > _NET_BODY_MAX:
            self.misses += 1
            return
        cand, mode = extract_markdown(text)
        if not cand:
            return
        self.hits += 1
        # 只换更长的:流式多响应里,后到的那条通常是「更完整」的快照
        if len(cand) > len(self.best):
            self.best, self.best_at = cand, self.last_event
            log.debug("[W%d] 网络旁路命中 %d 字(%s, %s)",
                      self.wid, len(cand), mode, url[:80])
            if bool(getattr(self.cfg, "net_debug", False)):
                self._dump(url, cand)

    def _dump(self, url: str, text: str) -> None:
        """net_debug:把抓到的原文落到 state 目录下,便于核对 RPC 报文。"""
        try:
            d = os.path.join(str(self.cfg.state_dir or "."), "net_dump")
            os.makedirs(d, exist_ok=True)
            self.dumps += 1
            p = os.path.join(d, f"w{self.wid}.log")
            if os.path.exists(p) and os.path.getsize(p) > 4 * 1024 * 1024:
                os.replace(p, p + ".1")       # 只留一代,别撑爆磁盘
            with open(p, "a", encoding="utf-8") as f:
                f.write(f"\n===== {time.strftime('%H:%M:%S')} {url[:200]}\n")
                f.write(text[:6000])
                f.write("\n")
        except Exception:
            pass


class Worker:
    """一个身份 = 共享 Browser 上的一个 context + 一个 page。

    所有会碰 page / context 的方法都是协程,并且只在 PlaywrightHost 的
    事件循环线程里执行;``info()``/``wants_rotation()``/``_state_path()``
    这类不碰浏览器的仍然保持同步,方便面板线程直接读。
    """

    # 连续多少次「载入/保存 cookie 失败」就判定文件已损坏
    STATE_FAIL_LIMIT = 2
    # 最多保留几个被隔离的坏文件,避免 state 目录无限膨胀
    STATE_BAD_KEEP = 3
    # DOM 采样间隔:有网络活动时最快 _TICK_MIN,静默期按 _TICK_STEP 退避到 _TICK_MAX
    _TICK_MIN = 0.22
    _TICK_MAX = 1.6
    _TICK_STEP = 1.5
    _ACTIVE_WINDOW = 1.2

    def __init__(self, wid: int, cfg, seq: int = 1,
                 user_id: str = "public", host=None):
        self.wid = wid
        self.cfg = cfg
        self.user_id = user_id     # 该身份当前归属的租户
        self.fp = Fingerprint(seq)
        self.seq = seq
        self.requests = 0          # 该身份已服务请求数
        self.block_streak = 0
        self.solves = 0
        self.dirty = False         # 页面状态不可信(超时/串答案/冻结)
        self.last_q = ""
        self.state_fails = 0
        self.state_ok = False      # 本次上下文是否成功载入了 cookie
        self._host = host
        self._ctx = None
        self._page = None
        self._abort = None
        self._last_error = ""
        self._net: NetworkTap | None = None   # 网络旁路(每个 page 一个)
        self._tick_cur = 0.0                  # DOM 采样间隔的退避游标
        self.state_path = self._state_path(seq, user_id)

    # ---------- 身份 / cookie 路径(同步,不碰浏览器) ----------

    def _state_path(self, seq: int, user_id: str | None = None) -> str:
        """cookie 按租户分目录存:u_<user>/w<wid>_s<seq>.json。

        不同租户的 storage_state 绝不互相覆盖;否则 A 的登录态会漏给 B。
        """
        uid = user_id or self.user_id or "public"
        safe = "".join(c if c.isalnum() or c in "-_." else "_"
                       for c in uid)[:40] or "public"
        return f"{self.cfg.state_dir}/u_{safe}/w{self.wid}_s{seq}.json"

    def _ensure_state_dir(self) -> None:
        """建好 u_<user>/ 目录;否则 storage_state 落盘会失败。"""
        d = os.path.dirname(self.state_path or "")
        if d:
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as e:
                log.warning("[W%d] 建 cookie 目录失败 %s: %s", self.wid, d, e)

    def _load_state(self) -> str | None:
        """该租户自己的 cookie 文件;没有就返回 None(全新身份)。"""
        p = self.state_path
        try:
            if p and os.path.exists(p) and os.path.getsize(p) > 2:
                return p
        except OSError:
            pass
        return None

    # ---------- cookie 落盘:只在干净时,且原子替换 ----------

    async def _save_state(self) -> bool:
        """把当前 cookie 存回**本租户**目录,供下次复用。

        只在 ``kind == OK 且 not dirty`` 时由 ``_finish`` 调用。
        先写 ``*.tmp`` 再 ``os.replace``,所以任何一步失败都不会把
        已知可用的旧文件截断成半截 JSON(这正是上一版污染的来源)。
        """
        ctx = self._ctx
        if ctx is None or not self.state_path:
            return False
        self._ensure_state_dir()
        tmp = f"{self.state_path}.{os.getpid()}.{int(time.time() * 1000)}.tmp"
        try:
            await ctx.storage_state(path=tmp)
            size = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            if size <= 2:
                raise RuntimeError(f"storage_state 内容为空({size}B)")
            os.replace(tmp, self.state_path)      # 同目录 rename,原子
            self.state_fails = 0
            return True
        except Exception as e:
            self.state_fails += 1
            log.warning("[W%d] 保存 cookie 失败(租户 %s,第 %d 次): %s",
                        self.wid, self.user_id, self.state_fails, e)
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass
            return False

    def _quarantine_state(self, why: str, force: bool = False) -> None:
        """把可疑的 cookie 文件改名隔离,下次从干净身份开始。

        连续 ``STATE_FAIL_LIMIT`` 次失败时不再等 force 也会清理,
        避免一个坏文件反复拖垮每次新建上下文。
        """
        p = self.state_path
        if force:
            self.state_fails = max(self.state_fails, self.STATE_FAIL_LIMIT)
        if self.state_fails < self.STATE_FAIL_LIMIT:
            return
        if not p or not os.path.exists(p):
            self.state_fails = 0
            return
        bad = f"{p}.bad{int(time.time())}"
        try:
            os.replace(p, bad)
            self.state_fails = 0
            log.warning("[W%d] cookie 文件已隔离 -> %s(%s)",
                        self.wid, os.path.basename(bad), (why or "")[:120])
            self._prune_bad(os.path.dirname(bad), os.path.basename(p))
        except OSError as e:
            # 隔离失败就删掉:留着它只会继续污染新上下文
            log.warning("[W%d] 隔离 cookie 失败(%s),直接删除", self.wid, e)
            try:
                os.unlink(p)
                self.state_fails = 0
            except OSError:
                pass

    def _prune_bad(self, d: str, prefix: str) -> None:
        """同一个 state 文件最多留 STATE_BAD_KEEP 个 .bad 副本。"""
        try:
            names = sorted(n for n in os.listdir(d) if n.startswith(prefix + ".bad"))
        except OSError:
            return
        for n in names[:-self.STATE_BAD_KEEP] if len(names) > self.STATE_BAD_KEEP else ():
            try:
                os.unlink(os.path.join(d, n))
            except OSError:
                pass

    # ---------- 上下文生命周期(协程,跑在宿主循环里) ----------

    async def _route(self, route):
        """拦掉图片/媒体/字体:省带宽,也省渲染进程内存。"""
        try:
            if route.request.resource_type in ("image", "media", "font"):
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            # 页面导航/关闭时 route 会失效,静默丢弃即可
            pass

    async def _open(self) -> None:
        """在共享 Browser 上开一个属于本身份的 context + page。"""
        host = self._host
        if host is None:
            raise RuntimeError("Worker 未绑定共享浏览器宿主(PlaywrightHost)")
        cfg = self.cfg
        state = self._load_state()
        ctx = None
        if state:
            try:
                ctx = await host.new_context(self.fp, state)
                self.state_ok = True
            except Exception as e:
                # cookie 文件坏了不能连累整个身份,隔离后改用干净上下文
                log.warning("[W%d] 复用 %s 失败,改用干净上下文: %s",
                            self.wid, self.state_path, e)
                self._quarantine_state(f"storage_state 载入失败: {e}",
                                       force=True)
                ctx = None
        if ctx is None:
            ctx = await host.new_context(self.fp, None)
            self.state_ok = False
        self._ctx = ctx
        await ctx.add_init_script(self.fp.stealth_js)
        if cfg.block_assets:
            await ctx.route("**/*", self._route)
        self._page = await ctx.new_page()
        self._page.set_default_timeout(30000)
        # 网络旁路必须在任何导航发生之前挂上:第一条响应可能就是答案。
        if bool(getattr(cfg, "net_capture", True)):
            self._net = NetworkTap(self.wid, cfg)
            try:
                self._net.bind(self._page)
            except Exception as e:
                log.warning("[W%d] 网络旁路挂载失败,只用 DOM: %s", self.wid, e)
                self._net = None
        self._tick_cur = 0.0
        host.page_opened()
        log.info("[W%d] %s 就绪(共享浏览器,已开 %d 个页面)",
                 self.wid, self.fp, host.pages)

    async def _close_ctx(self, save: bool = False) -> None:
        page, ctx = self._page, self._ctx
        self._page = self._ctx = None
        # 先作废旁路:之后迟到的 response 回调一律丢弃,不再读 body。
        if self._net is not None:
            self._net.close()
            self._net = None
        self._tick_cur = 0.0
        if save and ctx is not None and not self.dirty:
            await self._save_state()
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass
            if self._host is not None:
                self._host.page_closed()
        elif page is not None:
            try:
                await page.close()
            except Exception:
                pass
            if self._host is not None:
                self._host.page_closed()

    async def alive(self) -> bool:
        """上下文是否还能用(带一次轻量 IPC 探测,能抓到渲染进程崩溃)。"""
        page = self._page
        if page is None or self._ctx is None:
            return False
        try:
            if page.is_closed():
                return False
            await page.evaluate("() => 1")
            return True
        except Exception:
            return False

    async def start(self) -> None:
        """确保上下文可用。已可用时零 IPC 直接返回。"""
        page = self._page
        if page is not None and self._ctx is not None:
            try:
                if not page.is_closed():
                    return
            except Exception:
                pass
            log.warning("[W%d] 检测到上下文已关闭,自动重建", self.wid)
            await self._close_ctx(save=False)
        await self._open()

    async def rotate(self, seq: int, user_id: str | None = None) -> None:
        """换指纹 + 换 cookie:只重建 context,不重启浏览器。

        user_id 传了就把身份归属切到该租户,cookie 目录随之切换。
        """
        log.info("[W%d] 轮换身份(用了 %d 次,连续被拦 %d)租户 %s",
                 self.wid, self.requests, self.block_streak,
                 user_id or self.user_id)
        # 被轮换的身份通常已经脏了,不落盘;干净状态下最后一次成功
        # 回答时已经存过,这里再存一次没有收益。
        await self._close_ctx(save=False)
        self.fp = Fingerprint(seq)
        self.seq = seq
        self.requests = 0
        self.block_streak = 0
        self.dirty = False
        self.state_fails = 0
        self.state_ok = False
        if user_id:
            self.user_id = user_id
        self.state_path = self._state_path(seq)
        await self._open()

    async def close(self) -> None:
        """释放本身份的 context;共享浏览器由宿主统一关。"""
        await self._close_ctx(save=False)
        self.dirty = False

    # ---------- 时间预算 ----------

    def _left(self, deadline: float) -> float:
        return max(0.0, deadline - time.monotonic())

    def _is_aborted(self) -> bool:
        fn = self._abort
        if fn is None:
            return False
        try:
            return bool(fn())
        except Exception:
            return False

    def _stop_reason(self, deadline: float) -> str | None:
        """还要不要继续干下去:返回内部标记,None 表示可以继续。"""
        if self._is_aborted():
            return M_ABORTED
        if time.monotonic() >= deadline:
            return M_TIMEOUT
        return None

    # ---------- 判定 ----------

    async def _probe(self, want: int = 0) -> dict:
        """一次采样拿到 URL、是否被拦、是否有输入框、锚点数,以及(可选)答案。

        与上一版相比,原先紧接着的 ``evaluate(SNAP_JS)`` 已合并进来:
        等答案的每一轮只有这一次 IPC。
        """
        page = self._page
        if page is None:
            return {"url": "", "err": True, "closed": True}
        try:
            if page.is_closed():
                return {"url": "", "err": True, "closed": True}
            r = await page.evaluate(PROBE_JS, int(want))
        except Exception as e:
            msg = str(e).lower()
            closed = ("closed" in msg or "crash" in msg or "target" in msg
                      or self._page is None or self._page.is_closed())
            return {"url": "", "err": True, "closed": closed,
                    "error": str(e)[:200]}
        r["blocked"] = _is_blocked(r)
        return r

    def _tick(self) -> float:
        """两轮采样之间睡多久。

        有网络活动(答案正在流式到达)就贴近 _TICK_MIN,好尽快看到尾部;
        静默期指数退避到 _TICK_MAX。AI 思考期间页面上没有任何响应,
        0.22s 一次的 evaluate 纯属空转,退避能把这一段的 IPC 砍掉八成以上。

        旁路没抓到任何响应时(接口变了/被 CDN 改写)恒为 _TICK_MIN,
        也就是退回到改造前完全相同的节奏,不会因为旁路失效而变慢。
        """
        tap = self._net
        if tap is None or tap.chunks <= 0:
            return self._TICK_MIN
        if tap.last_event and time.monotonic() - tap.last_event < self._ACTIVE_WINDOW:
            self._tick_cur = self._TICK_MIN
            return self._tick_cur
        cur = self._tick_cur or self._TICK_MIN
        self._tick_cur = min(self._TICK_MAX, cur * self._TICK_STEP)
        return self._tick_cur

    def _answer_text(self, raw: str, s: dict) -> str:
        """DOM 抽出的答案 + 旁路抓到的原文 -> 最终返回的答案。

        旁路原文是 Markdown,``\u0060\u0060\u0060`` 代码块的缩进/空行都在;
        DOM 的 innerText 已经压平。所以只要两者是同一段内容,就用原文。

        「同一段内容」的判据:DOM 结果(去空白后)能在原文(去空白后)
        里找到,且原文不短于 DOM 结果——反过来(DOM 更长)说明原文只是
        一个片段(流式还没写完),这时宁可返回 DOM。

        判据不成立就整条丢弃并记 ``net_reject``(只做诊断,不外泄)。
        结果写进 ``s["answer_src"]``,一路带到 ``rec`` 里,便于面板核对
        某次回答到底来自网络还是 DOM。
        """
        dom = clean_answer(raw)
        tap = self._net
        if tap is None or tap.closed or not tap.best:
            s["answer_src"] = "dom"
            return dom
        net = clean_answer(tap.best)
        if not dom:
            # DOM 还没渲染出来(或渲染失败),但归属/epoch 已经校验过,
            # 旁路原文就是这个问题的答案。
            if net:
                s["answer_src"] = "net-only"
                return net
            s["answer_src"] = "dom"
            return dom
        if len(net) >= len(dom) \
                and " ".join(dom.split())[:120] in " ".join(net.split()):
            s["answer_src"] = "net"
            return net
        s["net_reject"] = len(net)
        s["answer_src"] = "dom"
        return dom

    async def _goto(self, url: str, deadline: float) -> bool:
        """按剩余预算收紧的超时导航。返回是否成功。"""
        left = self._left(deadline)
        if left <= 0.6:
            self._last_error = f"goto: 剩余预算 {left:.1f}s 不足"
            return False
        # 至少要给 1.2s,否则 goto 一定立刻抛 Timeout 而不是真的失败
        ms = int(min(float(self.cfg.goto_timeout) * 1000,
                     max(1200.0, left * 1000)))
        try:
            await self._page.goto(url, wait_until="domcontentloaded",
                                  timeout=ms)
            return True
        except Exception as e:
            self._last_error = f"goto: {str(e)[:200]}"
            return False

    async def _settle(self, deadline: float, timeout: float = 12.0,
                      want: int = 0) -> dict:
        """等页面稳定到「可判定」状态。

        goto(domcontentloaded) 返回时往往还在 search 页,
        重定向到 /sorry/ 要晚几百毫秒;此处必须等,不能立刻判定。
        """
        end = min(deadline, time.monotonic() + float(timeout))
        last: dict = {}
        while time.monotonic() < end:
            r = await self._probe(want)
            last = r
            if r.get("closed"):
                return r
            if r.get("blocked"):
                return r                      # 拦截已确定
            if r.get("anchors") or r.get("avail"):
                return r                      # 已进入 AI Mode 或明确不可用
            # 有输入框但正文只有顶栏(几百字)时 AI Mode 前端还没起来,
            # 不能当成就绪,否则后面会把一个空壳当最终页面干等。
            if r.get("ta") and (r.get("body_len") or 0) > 2500:
                return r
            if self._is_aborted():
                return r
            await asyncio.sleep(0.18)
        return last or {}

    async def _clear_block(self, deadline: float) -> bool:
        """打码并提交;成功后页面会自行跳到真实结果。"""
        left = self._left(deadline)
        if left < 8.0:
            log.warning("[W%d] 剩余 %.1fs,放弃打码", self.wid, left)
            return False
        budget = min(float(self.cfg.solve_timeout or 180.0),
                     max(15.0, left - 6.0))
        # 把「调用方是否走了」也传给打码轮询:否则客户端断开后,
        # 这里还会替一个没人要的回答烧满打码时限。
        tok = await solve_async(self._page, self.cfg, timeout=budget,
                                abort=self._is_aborted)
        if not tok:
            log.warning("[W%d] 打码未拿到 token", self.wid)
            return False
        self.solves += 1
        try:
            filled = await self._page.evaluate(FILL_JS, tok)
            log.info("[W%d] token 回填: %s (len=%d)", self.wid, filled, len(tok))
            if filled not in ("filled", "created"):
                return False
            await asyncio.sleep(0.8)
            how = await self._page.evaluate(SUBMIT_JS, tok)
            log.info("[W%d] 提交方式: %s", self.wid, how)
            if how == "none":
                return False
        except Exception as e:
            log.warning("[W%d] 提交 token 失败: %s", self.wid, e)
            return False
        # 等离开 /sorry/ 并稳定
        end = min(deadline, time.monotonic() + 40.0)
        while time.monotonic() < end:
            await asyncio.sleep(0.5)
            if "/sorry/" not in (self._page.url or ""):
                break
            if self._is_aborted():
                return False
        await asyncio.sleep(1.5)
        r = await self._probe()
        ok = not r.get("blocked")
        if not ok:
            log.warning("[W%d] 打码后仍被拦 url=%s",
                        self.wid, (self._page.url or "")[:120])
        return ok

    async def _wait_answer(self, question: str, deadline: float,
                           min_anchors: int = 1,
                           solve_tries: int = 1) -> tuple[str, dict]:
        """轮询到答案出现且稳定。

        min_anchors:追问时页面已有旧答案,锚点数量没增加就不算新答案。
        中途被拦则补一次打码再继续。
        页面明确报错(生成失败/连接中断)时立刻返回 __FAILED__,
        不要傻等到超时——那会白烧两分钟并让迟到答案污染下一次请求。
        整个循环严格受 ``deadline`` 约束;调用方放弃时立刻返回。
        """
        qn = _norm(question)
        last, stable_since = "", None
        self._net_last = ""
        info: dict = {}
        unmatched = ""
        unmatched_since = None             # 锚点回显对不上问题的起始时间
        low_since = None                   # 锚点数一直上不去
        reloads = 1                        # 空壳页允许的原地重导航次数
        shell_since = None                 # 连续空壳计时
        err_streak = 0
        while True:
            why = self._stop_reason(deadline)
            if why:
                if why == M_ABORTED:
                    log.info("[W%d] 调用方已放弃,中止轮询(剩余预算 %.1fs)",
                             self.wid, self._left(deadline))
                else:
                    log.info("[W%d] 本轮时间预算用尽,中止轮询", self.wid)
                self.dirty = True
                return why, info
            # asyncio.sleep 而不是 page.wait_for_timeout:
            # 后者每次都是一次 CDP 往返,空转成本远高于一次本地定时器。
            # 间隔由网络活动驱动(见 _tick):答案在流式到达时跟得很紧,
            # AI 静默思考时自动退避,不必再固定 0.22s 空采样。
            await asyncio.sleep(self._tick())
            s = await self._probe(min_anchors)
            info = s
            if s.get("closed"):
                self.dirty = True
                return M_FAILED, s
            if s.get("err"):
                err_streak += 1
                if err_streak >= 5:
                    self.dirty = True
                    return M_FAILED, s
                continue
            err_streak = 0
            if s.get("blocked"):
                if solve_tries <= 0:
                    return M_BLOCKED, s
                log.info("[W%d] 取答案中途被拦,补打码", self.wid)
                if not await self._clear_block(deadline):
                    return M_BLOCKED, s
                solve_tries -= 1
                # 打码后的页面是静态过渡页,答案不会自己长出来,重导航一次
                if not await self._goto(url_for(question), deadline):
                    self.dirty = True
                    return M_FAILED, s
                min_anchors, low_since = 1, None
                stable_since = None
                continue
            if s.get("failed"):
                return M_FAILED, s
            if (s.get("anchors") or 0) < min_anchors:
                # 打码会把页面整个重置,锚点数归零;追问要求的 base+1 就永远
                # 等不到。给它一段宽限,之后按 1 个锚点也认,靠 echo 校验归属。
                n = s.get("anchors") or 0
                if min_anchors > 1 and n > 0:
                    if low_since is None:
                        low_since = time.monotonic()
                    elif time.monotonic() - low_since >= 8.0:
                        log.info("[W%d] 锚点数停在 %d(要 %d),放宽",
                                 self.wid, n, min_anchors)
                        min_anchors, low_since = 1, None
                # 薄壳持续一段时间才重导航。刚进来时正文本来就只有顶栏,
                # 立刻重导航会把还在加载的 AI Mode 前端掐掉。
                if n == 0 and (s.get("body_len") or 0) < 3000 and reloads > 0 \
                        and shell_since is not None \
                        and time.monotonic() - shell_since >= 12.0:
                    reloads -= 1
                    log.info("[W%d] 页面是空壳(body=%s anchors=0),重导航",
                             self.wid, s.get("body_len"))
                    if not await self._goto(url_for(question), deadline):
                        self.dirty = True
                        return M_FAILED, s
                    stable_since, low_since = None, None
                    await asyncio.sleep(0.8)
                # 连续一段时间都是「有输入框、没有锚点、正文极薄」的壳,
                # 说明这个身份被 Google 软限流了,再等也不会长出答案。
                # 早退让上层换身份重试,别把 answer_timeout 全烧掉。
                if n == 0 and (s.get("body_len") or 0) < 3000:
                    if shell_since is None:
                        shell_since = time.monotonic()
                    elif time.monotonic() - shell_since >= 25.0:
                        dug = answer_from_shell(s.get("head") or "", question)
                        if dug:
                            log.info("[W%d] 无锚点但正文含答案 %d 字",
                                     self.wid, len(dug))
                            return self._answer_text(dug, s), s
                        log.info("[W%d] 连续 %ds 空壳(body=%s),判定身份受限",
                                 self.wid, int(time.monotonic() - shell_since),
                                 s.get("body_len"))
                        log.info("[W%d] 空壳正文: %s", self.wid,
                                 (s.get("head") or "")[:300])
                        self.dirty = True
                        return M_SHELL, s
                else:
                    shell_since = None
                continue                   # 新答案还没出现
            # 锚点已就位:答案正文在同一次采样里已经带回来了
            raw = s.get("answer") or ""
            if "too long" in raw.lower():
                return M_TOOLONG, s
            # 锚点行回显了问题原文:对不上就说明这是上一轮残留的答案
            echo = _norm(s.get("echo") or "")
            if qn and echo and qn[:40] not in echo and echo[:40] not in qn:
                # 页面正在把旧答案替换成新答案时也会短暂对不上,先等;
                # 但一直对不上就不能把别人的答案当成本次的返回。
                unmatched = clean_answer(raw)
                s["stale_answer"] = unmatched[:200]
                if unmatched_since is None:
                    unmatched_since = time.monotonic()
                elif time.monotonic() - unmatched_since >= 10.0:
                    log.info("[W%d] 锚点回显持续对不上问题(echo=%r),判定陈旧",
                             self.wid, echo[:60])
                    self.dirty = True
                    return M_STALE, s
                continue
            unmatched_since = None
            # 稳定判据要同时看两条来源:DOM 正文变了,或旁路原文又长长了。
            # 只看 DOM 会在「页面还在流式、innerText 暂时不动」时早退,
            # 返回一段没写完的内容。
            net_now = self._net.best if self._net is not None else ""
            if raw != last or net_now != self._net_last:
                self._net_last = net_now
                last, stable_since = raw, time.monotonic()
                if s.get("ready"):
                    a = self._answer_text(raw, s)
                    if len(a) >= 2:
                        return a, s
                continue
            quiet = 0.25 if s.get("ready") else float(self.cfg.ready_quiet_s)
            if (raw or net_now) and stable_since \
                    and time.monotonic() - stable_since >= quiet:
                a = self._answer_text(raw, s)
                if len(a) >= 2:
                    return a, s
        # 不会走到这里:循环只靠 return 退出

    # ---------- 主流程 ----------

    async def _maybe_quarantine(self) -> None:
        """连续被拦/连续异常:把已污染的 cookie 隔离掉。"""
        if self.block_streak < int(self.cfg.block_streak_limit or 2):
            return
        if not self.state_path or not os.path.exists(self.state_path):
            return
        self._quarantine_state(
            f"连续 {self.block_streak} 次异常,清理已污染的 cookie", force=True)

    async def ask(self, question: str, fresh: bool = False,
                  timeout: float | None = None, abort=None) -> dict:
        """提问并取答案。fresh=True 走新导航,否则尝试同页追问。

        流程:导航 → settle 等页面可判定 → 被拦则打码 → 等答案。
        打码成功后不能立刻取答案:/sorry/ 之后页面要重新渲染 AI Mode,
        必须等输入框或答案锚点出现。

        deadline 在这里一次敲定,后面每一步都只看剩余时间:
        无论内部走到哪个分支,总耗时都不会超过 ``timeout``(再加收尾时间)。
        """
        t0 = time.time()
        budget = float(timeout) if timeout else float(self.cfg.answer_timeout)
        budget = max(5.0, budget)
        deadline = time.monotonic() + budget
        self._abort = abort
        self.last_q = question
        rec = {"wid": self.wid, "fp": self.fp.seq, "solve": 0,
               "budget_s": round(budget, 2)}
        try:
            return await self._ask_body(rec, question, fresh, deadline, t0)
        finally:
            self._abort = None

    async def _ask_body(self, rec: dict, question: str, fresh: bool,
                        deadline: float, t0: float) -> dict:
        await self.start()
        why = self._stop_reason(deadline)
        if why:
            return await self._finish(rec, why, {}, t0)

        # 网络旁路的 epoch 在这里推进:上一轮问题迟到的响应会因为
        # epoch/归属不匹配被丢弃,不会混进本轮答案。必须早于任何导航。
        if self._net is not None:
            self._net.reset(question)
            self._tick_cur = 0.0
            self._net_last = ""

        # 超长 prompt(带大量背景/上下文/工具定义)放到 GET ?q= 会触发
        # 414 URI Too Long,自动切到页面 textarea 提交(走 POST 无长度限制)。
        long_prompt = len(urllib.parse.quote_plus(question)) > 1800
        if long_prompt and (self.dirty or self._page.url in ("about:blank", "")):
            if await self._goto(url_for("hi"), deadline):
                await self._settle(deadline, 12.0)
                self.dirty = False

        # 上一轮如果超时/串答案,页面状态不可信,强制新导航重来
        reuse = (long_prompt or not fresh) and not self.dirty \
            and self._page.url not in ("about:blank", "")
        base_anchors = 0

        if reuse:
            st = await self._probe()
            base_anchors = st.get("anchors") or 0
            if st.get("blocked"):
                reuse = False
                rec["reuse_fail"] = "blocked"
            elif not st.get("ta"):
                reuse = False
                rec["reuse_fail"] = "no-textarea"

        if reuse:
            try:
                n = await self._page.evaluate(SET_JS, question)
            except Exception as e:
                n = "no-textarea"
                rec["reuse_fail"] = f"set-error:{str(e)[:80]}"
            if n == "no-textarea":
                reuse = False
                rec.setdefault("reuse_fail", "set-failed")
        if reuse:
            await asyncio.sleep(0.2)
            try:
                clicked = await self._page.evaluate(SEND_JS)
            except Exception as e:
                clicked = f"error:{str(e)[:60]}"
            if clicked != "clicked":
                reuse = False
                rec["reuse_fail"] = f"send:{clicked}"
            else:
                rec["mode"] = "post" if long_prompt else "followup"
                answer, info = await self._wait_answer(
                    question, deadline, min_anchors=base_anchors + 1)
                return await self._finish(rec, answer, info, t0)

        # ---- 新导航 ----
        rec["mode"] = "goto"
        self.dirty = False                 # 全新导航,页面重新干净
        if self._page.url not in ("about:blank", ""):
            try:
                await self._page.evaluate(CLEAR_JS)
            except Exception:
                pass
        if not await self._goto(url_for(question), deadline):
            self.dirty = True
            self.requests += 1
            rec.update(kind="FAILED", error=self._last_error or "goto 失败",
                       total_s=round(time.time() - t0, 2))
            return rec
        rec["goto_s"] = round(time.time() - t0, 2)

        st = await self._settle(deadline, 15.0)
        rec["page"] = {k: st.get(k) for k in ("url", "body_len", "ta", "avail")}

        if st.get("blocked"):
            t1 = time.time()
            if not await self._clear_block(deadline):
                self.block_streak += 1
                self.requests += 1
                self.dirty = True
                rec.update(kind="BLOCKED", solve=1,
                           total_s=round(time.time() - t0, 2))
                rec["page"] = {k: st.get(k)
                               for k in ("url", "body_len", "ta")}
                await self._maybe_quarantine()
                return rec
            rec["solve_s"] = round(time.time() - t1, 2)
            rec["solve"] = 1
            self.block_streak = 0
            # 验证通过后的这一页是 /sorry/ POST 出来的静态过渡页:锚点占位
            # 有、textarea 有,但 AI Mode 应用没初始化,答案永远是空串,
            # 干等只会白耗掉整个 answer_timeout 再返回 EMPTY。此时 cookie
            # 已带验证,重新导航就能进正常页面,所以无条件重来一次。
            st = await self._settle(deadline, 3.0)
            rec["post_solve"] = {k: st.get(k) for k in
                                 ("ta", "anchors", "body_len")}
            log.info("[W%d] 打码后重新导航(ta=%s anchors=%s)",
                     self.wid, st.get("ta"), st.get("anchors"))
            rec["regoto"] = 1
            if not await self._goto(url_for(question), deadline):
                self.requests += 1
                self.dirty = True
                rec.update(kind="FAILED", error=self._last_error or "goto 失败",
                           total_s=round(time.time() - t0, 2))
                return rec
            st = await self._settle(deadline, 15.0)
            rec["post_regoto"] = {k: st.get(k) for k in
                                  ("ta", "anchors", "body_len")}
            if st.get("blocked"):
                if not await self._clear_block(deadline):
                    self.block_streak += 1
                    self.requests += 1
                    self.dirty = True
                    rec.update(kind="BLOCKED", solve=2,
                               total_s=round(time.time() - t0, 2))
                    await self._maybe_quarantine()
                    return rec
                rec["solve"] = 2
                self.block_streak = 0
                st = await self._settle(deadline, 3.0)
                rec["post_solve2"] = {k: st.get(k) for k in
                                      ("ta", "anchors", "body_len")}
                if not await self._goto(url_for(question), deadline):
                    self.requests += 1
                    self.dirty = True
                    rec.update(kind="FAILED",
                               error=self._last_error or "goto 失败",
                               total_s=round(time.time() - t0, 2))
                    return rec
                st = await self._settle(deadline, 15.0)
                rec["post_regoto2"] = {k: st.get(k) for k in
                                       ("ta", "anchors", "body_len")}

        if st.get("avail"):
            self.dirty = True
            rec.update(kind="UNAVAILABLE",
                       error="AI Mode 在当前设备/账号不可用")
            self.requests += 1
            return rec

        why = self._stop_reason(deadline)
        if why:
            return await self._finish(rec, why, st, t0)

        answer, info = await self._wait_answer(question, deadline)
        return await self._finish(rec, answer, info, t0)

    async def _finish(self, rec: dict, answer: str, info: dict,
                      t0: float) -> dict:
        self.requests += 1
        rec["total_s"] = round(time.time() - t0, 2)
        if answer == M_BLOCKED:
            self.block_streak += 1
            self.dirty = True
            rec["kind"] = "BLOCKED"
            rec.setdefault("error", _MARKERS[M_BLOCKED][1])
            await self._maybe_quarantine()
            return rec
        self.block_streak = 0
        if answer in _MARKERS:
            self.dirty = True
            kind, err = _MARKERS[answer]
            rec["kind"] = kind
            rec["error"] = err
            rec["page"] = {k: info.get(k)
                           for k in ("url", "body_len", "ta", "avail")}
            if answer in (M_FAILED, M_STALE, M_SHELL):
                await self._maybe_quarantine()
            return rec
        rec["kind"] = "OK" if answer else "EMPTY"
        rec["answer"] = answer
        rec["answer_chars"] = len(answer)
        # 出处:net / net-only / dom。面板和排障脚本据此核对「这次到底是
        # 网络原文还是 DOM 抠的」;net_reject 只在旁路候选被否决时出现。
        if info.get("answer_src"):
            rec["answer_src"] = info["answer_src"]
        if info.get("net_reject"):
            rec["net_reject"] = info["net_reject"]
        if self._net is not None and self._net.chunks:
            rec["net"] = self._net.summary()
        if answer:
            # 只有「成功且页面干净」才把 cookie 落盘。
            # 错误页 / 拦截页 / 半截渲染页的 storage_state 会把好 cookie 覆盖掉,
            # 下一次复用直接带着坏状态进站,这就是上一版越跑越差的根因。
            if not self.dirty:
                await self._save_state()
        else:
            self.dirty = True
            rec["page"] = {k: info.get(k)
                           for k in ("url", "body_len", "ta", "avail")}
        return rec

    def wants_rotation(self) -> bool:
        if self.block_streak >= int(self.cfg.block_streak_limit or 2):
            return True
        return self.requests >= int(self.cfg.max_requests_per_identity or 50)