"""HTTP 服务入口：FastAPI + 多租户公平调度池 + 实时监控与冒烟压测。

启动:
  export YES_KEY=xxx
  export USERS="alice:k1:2:20, bob:k2:4:40"   # 可选，不配置则单租户
  export EGRESS_PROXY=http://127.0.0.1:7897    # 可选
  uvicorn proxy_svc.app:app --host 0.0.0.0 --port 8080

接口:
  GET    /health                   健康 + 池状态 + 进程内存
  GET    /stats                    本租户配额与计数；?all=1 需管理员密钥
  POST   /ask                      {"question": "...", "fresh": false, "wait": true}
  POST   /ask/batch                {"questions": ["..."], "fresh": false}
  GET    /tasks/{task_id}          查询异步任务状态与结果
  DELETE /tasks/{task_id}          取消排队或执行中的任务
  POST   /warm                     预热 worker
  GET    /v1/models                OpenAI 模型列表
  POST   /v1/chat/completions      OpenAI Chat Completions
  GET    /admin/metrics            TPS/Token 曲线、分位延迟、来源 IP、租户用量、最近请求
  POST   /admin/metrics/reset      清空统计计数
  GET    /admin/tasks              查看在跑与排队中的任务
  POST   /admin/pool/pause         暂停接单
  POST   /admin/pool/resume        恢复接单
  POST   /admin/pool/clear         清空排队队列
  GET    /admin/soak               冒烟压测状态
  POST   /admin/soak/start         启动冒烟压测（Mock 调度）
  POST   /admin/soak/stop          停止冒烟压测
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .backends import make_backend
from .config import CFG
from .core.pool import Pool, PoolLimits
from .core.soak import SoakRunner
from .core.sysinfo import process_info
from .core.tasks import Task
from .core.tokens import count_tokens, usage_of
from .metrics import Metrics

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app")

POOL: Pool | None = None
METRICS: Metrics | None = None
SOAK: SoakRunner | None = None


def make_pool(cfg, name: str = "main") -> Pool:
    backend = make_backend(cfg.backend, cfg)
    limits = PoolLimits(
        min_workers=lambda: cfg.min_workers,
        max_workers=lambda: cfg.max_workers,
        queue_limit=lambda: cfg.queue_limit,
        idle_ttl=lambda: cfg.idle_ttl,
        default_timeout=lambda: max(60.0, float(cfg.answer_timeout) + 30.0),
    )
    return Pool(
        name=name,
        backend=backend,
        limits=limits,
        quota=cfg.quota,
        task_ttl=cfg.task_ttl,
    )


def _record_task(task: Task) -> None:
    """Task 完成回调：把结果写入 Metrics。"""
    if METRICS is None:
        return
    res = task.result or {}
    usage = res.get("usage") or usage_of(
        task.question, res.get("answer") if res.get("kind") == "OK" else None)
    METRICS.record(
        user_id=task.user_id,
        ip=task.meta.get("ip") or "-",
        endpoint=task.meta.get("endpoint") or "/ask",
        kind=res.get("kind") or "ERROR",
        tokens_in=int(usage.get("prompt_tokens") or 0),
        tokens_out=int(usage.get("completion_tokens") or 0),
        total_s=res.get("total_s_all") or res.get("total_s"),
        queue_s=res.get("queue_s"),
        serve_s=res.get("serve_s"),
        question=task.question,
        ua=task.meta.get("ua") or "",
        task_id=task.id,
        error=res.get("error"),
        ts=task.finished,
    )


def _record_rejection(res: dict, *, question: str, user_id: str,
                      meta: dict) -> None:
    if METRICS is None:
        return
    METRICS.record(
        user_id=user_id,
        ip=meta.get("ip") or "-",
        endpoint=meta.get("endpoint") or "/ask",
        kind=res.get("kind") or "BUSY",
        tokens_in=count_tokens(question),
        tokens_out=0,
        total_s=0.0,
        queue_s=0.0,
        serve_s=0.0,
        question=question,
        ua=meta.get("ua") or "",
        task_id=None,
        error=res.get("error"),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global POOL, METRICS, SOAK
    os.makedirs(CFG.state_dir, exist_ok=True)
    os.makedirs(CFG.data_dir, exist_ok=True)
    CFG.load_store()
    errs = CFG.validate()
    if errs:
        log.error("配置错误: %s", "; ".join(errs))
    if not CFG.auth_enabled:
        if CFG.allow_no_auth:
            log.warning("ALLOW_NO_AUTH=1：当前无任何鉴权，任何能访问端口的人都是管理员，"
                        "切勿暴露到公网")
        else:
            log.error("未配置 API_KEY/USERS：所有接口将返回 503。"
                      "请设置 API_KEY（或仅在本机调试时设 ALLOW_NO_AUTH=1）")
    METRICS = Metrics(CFG.data_dir, CFG.metrics_preview_chars)
    METRICS.start()
    POOL = make_pool(CFG, name="main")
    POOL.start()
    SOAK = SoakRunner(CFG, lambda: POOL, on_task_done=_record_task)
    log.info("服务就绪 %s:%d backend=%s headless=%s proxy=%s 租户=%s 鉴权=%s",
             CFG.host, CFG.port, CFG.backend, CFG.headless,
             CFG.egress_proxy or "无", ",".join(CFG.tenants),
             "开" if CFG.auth_enabled else "关")
    try:
        yield
    finally:
        if SOAK:
            SOAK.stop()
        if POOL:
            POOL.shutdown()
        if METRICS:
            METRICS.stop()


app = FastAPI(title="Gemini5.0 Proxy", lifespan=lifespan)


class BodyLimitMiddleware:
    """按 Content-Length 拒绝过大的请求体，避免内存被大包撑爆。"""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            for name, value in scope["headers"]:
                if name == b"content-length":
                    try:
                        too_big = int(value) > CFG.max_body_bytes
                    except ValueError:
                        too_big = True
                    if too_big:
                        resp = JSONResponse(
                            {"detail": "request body too large"}, status_code=413)
                        await resp(scope, receive, send)
                        return
        await self.inner(scope, receive, send)


app.add_middleware(BodyLimitMiddleware)
_STATIC = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_STATIC):
    app.mount("/panel", StaticFiles(directory=_STATIC, html=True),
              name="panel")


@app.get("/assets/{asset_path:path}")
def panel_asset(asset_path: str):
    assets_root = os.path.normpath(os.path.join(_STATIC, "assets"))
    target = os.path.normpath(os.path.join(assets_root, asset_path))
    try:
        inside = os.path.commonpath([assets_root, target]) == assets_root
    except ValueError:
        inside = False
    if not inside or not os.path.isfile(target):
        raise HTTPException(404, "asset not found")
    return FileResponse(target)


@app.get("/")
def panel_index():
    page = os.path.join(_STATIC, "index.html")
    if os.path.exists(page):
        return FileResponse(page)
    return {"ok": True, "panel": False}


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=60000)
    fresh: bool = True
    timeout: float | None = Field(default=None, gt=0, le=600)
    wait: bool = True
    user_id: str | None = None      # 仅管理员可用：代某租户发起


class BatchIn(BaseModel):
    questions: list[str] = Field(min_length=1, max_length=64)
    fresh: bool = True
    timeout: float | None = Field(default=None, gt=0, le=600)
    user_id: str | None = None      # 仅管理员可用


class KeyIn(BaseModel):
    user_id: str | None = None
    max_concurrent: int | None = None
    queue_limit: int | None = None
    note: str | None = None


class SoakIn(BaseModel):
    concurrency: int = 4
    duration_s: float = 60.0
    max_requests: int = 0
    think_ms: int = 50
    users: list[str] | None = None


def _is_trusted_proxy(host: str) -> bool:
    trusted = CFG.trusted_proxies or []
    if "*" in trusted:
        return True
    if host in trusted:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    for pat in trusted:
        if "/" in pat:
            try:
                if addr in ipaddress.ip_network(pat, strict=False):
                    return True
            except ValueError:
                continue
    return False


def _client_ip(request: Request | None) -> str:
    if request is None:
        return "-"
    peer = (request.client.host if request.client else "") or "-"
    if peer != "-" and _is_trusted_proxy(peer):
        xff = request.headers.get("x-forwarded-for")
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first
        xri = (request.headers.get("x-real-ip") or "").strip()
        if xri:
            return xri
    return peer


def _bearer(authorization: str | None) -> str | None:
    """OpenAI 客户端发 Authorization: Bearer <key>。"""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return None


def _extract_key(request: Request | None, x_api_key: str | None = None,
                 authorization: str | None = None) -> str | None:
    k = _bearer(authorization) or x_api_key
    if not k and request is not None:
        k = (_bearer(request.headers.get("authorization"))
             or request.headers.get("x-api-key"))
    return k


def _ident(key: str | None,
           request: Request | None = None) -> tuple[str, bool]:
    """校验密钥，返回 (user_id, 是否管理员)。鉴权未开启时放开。"""
    if not CFG.auth_enabled:
        if not CFG.allow_no_auth:
            raise HTTPException(
                503, "auth is not configured: set API_KEY "
                     "(ALLOW_NO_AUTH=1 only for local debugging)")
        return (CFG.default_user, True)
    who = CFG.whois(key)
    if who is None:
        if METRICS is not None and request is not None:
            METRICS.auth_fail(
                _client_ip(request),
                request.url.path,
                request.headers.get("user-agent") or "",
            )
        raise HTTPException(401, "invalid api key")
    return who


def _admin(x_api_key: str | None, authorization: str | None = None,
           request: Request | None = None) -> str:
    uid, admin = _ident(_extract_key(request, x_api_key, authorization), request)
    if not admin:
        raise HTTPException(403, "only admin key may access admin endpoints")
    return uid


def _openai_error(status: int, message: str, code: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "error": {"message": message, "type": "invalid_request_error",
                  "param": None, "code": code}})


def _pick_user(who: tuple[str, bool], asked: str | None) -> str:
    """确定本次请求归属的租户。管理员可代租户发起，普通租户只能用自己的。"""
    uid, admin = who
    if asked:
        if not admin:
            raise HTTPException(403, "only admin key may set user_id")
        if asked not in CFG.tenants:
            raise HTTPException(404, f"unknown user_id: {asked}")
        return asked
    return uid


def _openai_user(who: tuple[str, bool], asked) -> str:
    """OpenAI 的 user 字段只是终端用户标识，客户端常会带上；只有管理员指向已存在租户时才生效。"""
    uid, admin = who
    if admin and isinstance(asked, str) and asked in CFG.tenants:
        return asked
    return uid


def _safe_timeout(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return min(float(value), 600.0)


async def _submit_one(question: str, *, fresh: bool, user_id: str,
                      wait: bool, timeout: float | None,
                      request: Request | None, endpoint: str) -> dict:
    assert POOL
    meta = {
        "ip": _client_ip(request),
        "ua": (request.headers.get("user-agent") or "") if request else "",
        "endpoint": endpoint,
    }
    t = POOL.enqueue(question, fresh=fresh, user_id=user_id, meta=meta)
    if isinstance(t, dict):
        _record_rejection(t, question=question, user_id=user_id, meta=meta)
        return t
    t.add_done_callback(_record_task)
    if not wait:
        return {
            "kind": "QUEUED",
            "task_id": t.id,
            "user_id": user_id,
            "queue_depth": POOL.q.qsize(),
        }
    return await POOL.wait_async(t, timeout)


@app.get("/health")
def health(request: Request,
           x_api_key: str | None = Header(default=None),
           authorization: str | None = Header(default=None)):
    assert POOL
    s = POOL.stats(tenants=CFG.tenants)
    basic = {
        "ok": True,
        "backend": CFG.backend,
        "auth": CFG.auth_enabled,
        "workers": s["workers"],
        "busy": s["busy"],
        "queued": s["queued"],
        "paused": s["paused"],
    }
    if CFG.auth_enabled:
        who = CFG.whois(_extract_key(request, x_api_key, authorization))
        if who is None or not who[1]:
            return basic
    elif not CFG.allow_no_auth:
        return basic
    return {
        "ok": True,
        "headless": CFG.headless,
        "backend": CFG.backend,
        "auth": CFG.auth_enabled,
        "tenants": len(CFG.tenants),
        "process": process_info(),
        **s,
    }


@app.get("/stats")
def stats(request: Request, all: int = 0,
          x_api_key: str | None = Header(default=None),
          authorization: str | None = Header(default=None)):
    assert POOL
    uid, admin = _ident(_extract_key(request, x_api_key, authorization), request)
    if all:
        if not admin:
            raise HTTPException(403, "only admin key may read all stats")
        out = POOL.stats(tenants=CFG.tenants)
        out["process"] = process_info()
        if METRICS is not None:
            snap = METRICS.snapshot(window=900, top_ips=20, recent=20)
            out["rates"] = snap["rates"]
            out["latency"] = snap["latency"]
            out["totals"] = snap["totals"]
        return out
    out = POOL.stats(user_id=uid, tenants=CFG.tenants)
    if METRICS is not None:
        out["usage"] = METRICS.user_usage(uid)
    return out


@app.post("/ask")
async def ask(body: AskIn, request: Request,
              x_api_key: str | None = Header(default=None),
              authorization: str | None = Header(default=None)):
    who = _ident(_extract_key(request, x_api_key, authorization), request)
    uid = _pick_user(who, body.user_id)
    t0 = time.time()
    r = await _submit_one(
        body.question,
        fresh=body.fresh,
        user_id=uid,
        wait=body.wait,
        timeout=body.timeout,
        request=request,
        endpoint="/ask",
    )
    out = dict(r)
    out["api_s"] = round(time.time() - t0, 2)
    return out


@app.post("/ask/batch")
async def ask_batch(body: BatchIn, request: Request,
                    x_api_key: str | None = Header(default=None),
                    authorization: str | None = Header(default=None)):
    who = _ident(_extract_key(request, x_api_key, authorization), request)
    uid = _pick_user(who, body.user_id)
    for q in body.questions:
        if not q.strip() or len(q) > 60000:
            raise HTTPException(400, "each question must be 1-60000 chars")
    coros = [
        _submit_one(
            q,
            fresh=body.fresh,
            user_id=uid,
            wait=True,
            timeout=body.timeout,
            request=request,
            endpoint="/ask/batch",
        )
        for q in body.questions
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)
    out = []
    for r in results:
        if isinstance(r, Exception):
            out.append({"kind": "ERROR", "error": str(r)[:200]})
        else:
            out.append(r)
    return {"count": len(out), "user_id": uid, "results": out}


@app.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request,
             x_api_key: str | None = Header(default=None),
             authorization: str | None = Header(default=None)):
    assert POOL
    uid, admin = _ident(_extract_key(request, x_api_key, authorization), request)
    t = POOL.get_task(task_id)
    if t is None:
        raise HTTPException(404, "task not found")
    if not admin and t.user_id != uid:
        raise HTTPException(403, "forbidden")
    return t.view(with_result=True)


@app.delete("/tasks/{task_id}")
def cancel_task(task_id: str, request: Request,
                x_api_key: str | None = Header(default=None),
                authorization: str | None = Header(default=None)):
    assert POOL
    uid, admin = _ident(_extract_key(request, x_api_key, authorization), request)
    t = POOL.get_task(task_id)
    if t is None:
        raise HTTPException(404, "task not found")
    if not admin and t.user_id != uid:
        raise HTTPException(403, "forbidden")
    action = POOL.cancel(t, "cancelled_by_user")
    return {"task_id": task_id, "action": action, **t.view(with_result=False)}


MODEL_ID = "Gemini5.0"


def _text_of(content) -> str:
    """OpenAI content 可以是字符串，也可以是 [{type,text}, ...]。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
        return "\n".join(p for p in parts if p)
    return "" if content is None else str(content)


def _messages_to_question(messages: list, tools: list | None = None) -> str:
    """把 chat messages 与可选 tools 定义收成一次无状态提问。"""
    blocks = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "user").strip().lower()
        text = _text_of(m.get("content")).strip()
        if not text and m.get("tool_calls"):
            try:
                text = json.dumps(m["tool_calls"], ensure_ascii=False)
            except Exception:
                text = str(m["tool_calls"])
        if not text:
            continue
        if role == "system":
            blocks.append(f"[system]\n{text}")
        elif role == "assistant":
            blocks.append(f"[assistant]\n{text}")
        elif role == "tool":
            tname = m.get("name") or m.get("tool_call_id") or "tool"
            blocks.append(f"[tool:{tname}]\n{text}")
        else:
            blocks.append(text if role == "user" and len(messages) == 1 and not tools else
                          f"[{role}]\n{text}")
    if isinstance(tools, list) and tools:
        try:
            t_desc = json.dumps(tools, ensure_ascii=False)
            if len(t_desc) <= 8000:
                blocks.insert(0, f"[tools]\n{t_desc}")
        except Exception:
            pass
    if len(blocks) == 1 and not blocks[0].startswith("["):
        return blocks[0]
    return "\n\n".join(blocks)


_KIND_HTTP = {
    "BUSY": (429, "rate_limit_exceeded"),
    "PAUSED": (503, "service_paused"),
    "TIMEOUT": (504, "timeout"),
    "CANCELLED": (499, "cancelled"),
    "FORBIDDEN": (403, "insufficient_quota"),
    "BLOCKED": (503, "server_error"),
    "UNAVAILABLE": (503, "server_error"),
    "FAILED": (502, "server_error"),
    "ERROR": (502, "server_error"),
    "TOOLONG": (400, "context_length_exceeded"),
}


@app.get("/v1/models")
def openai_models(request: Request,
                  authorization: str | None = Header(default=None),
                  x_api_key: str | None = Header(default=None)):
    _ident(_extract_key(request, x_api_key, authorization), request)
    return {"object": "list", "data": [
        {
            "id": MODEL_ID,
            "object": "model",
            "created": 1700000000,
            "owned_by": "google",
        },
        {
            "id": "google-ai-mode",
            "object": "model",
            "created": 1700000000,
            "owned_by": "google",
        },
    ]}


@app.post("/v1/chat/completions")
async def openai_chat(request: Request,
                      authorization: str | None = Header(default=None),
                      x_api_key: str | None = Header(default=None)):
    """OpenAI Chat Completions（默认无状态单次直连 goto，同时兼容 stream=true SSE 流式返回）。"""
    assert POOL
    who = _ident(_extract_key(request, x_api_key, authorization), request)
    try:
        body = await request.json()
    except Exception:
        return _openai_error(400, "invalid json body", "invalid_json")
    if not isinstance(body, dict):
        return _openai_error(400, "body must be a json object", "invalid_json")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return _openai_error(400, "messages is required", "invalid_messages")
    question = _messages_to_question(messages, body.get("tools")).strip()
    if not question:
        return _openai_error(400, "messages have no text", "invalid_messages")
    if len(question) > 60000:
        return _openai_error(400, "messages too long", "context_length_exceeded")

    uid = _openai_user(who, body.get("user"))

    # OpenAI 协议调用默认无状态 fresh=True（每次直接 goto，不与前序请求串上下文）
    fresh = bool(body.get("fresh", True))
    timeout = _safe_timeout(body.get("timeout"))
    t0 = time.time()
    cmpl_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    model_name = body.get("model") or MODEL_ID

    if body.get("stream"):
        meta = {
            "ip": _client_ip(request),
            "ua": request.headers.get("user-agent") or "",
            "endpoint": "/v1/chat/completions",
        }
        t = POOL.enqueue(question, fresh=fresh, user_id=uid, meta=meta)
        if isinstance(t, dict):
            _record_rejection(t, question=question, user_id=uid, meta=meta)
            kind = t.get("kind")
            status, code = _KIND_HTTP.get(kind, (502, "server_error"))
            msg = t.get("error") or f"upstream returned {kind}"
            return _openai_error(status, msg, code)
        t.add_done_callback(_record_task)

        async def _sse():
            now = int(time.time())
            role_chunk = {
                "id": cmpl_id,
                "object": "chat.completion.chunk",
                "created": now,
                "model": model_name,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(role_chunk, ensure_ascii=False)}\n\n"
            wait_task = asyncio.create_task(POOL.wait_async(t, timeout))
            try:
                while not wait_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(wait_task), timeout=5.0)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                r = wait_task.result()
            except asyncio.CancelledError:
                POOL.cancel(t, "client_gone")
                wait_task.cancel()
                raise
            now = int(time.time())
            kind = r.get("kind")
            answer = (r.get("answer") or "") if kind == "OK" else f"[Error: {r.get('error') or kind}]"
            usage = r.get("usage") or usage_of(question, answer if kind == "OK" else None)
            content_chunk = {
                "id": cmpl_id,
                "object": "chat.completion.chunk",
                "created": now,
                "model": model_name,
                "choices": [{"index": 0, "delta": {"content": answer}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(content_chunk, ensure_ascii=False)}\n\n"
            end_chunk = {
                "id": cmpl_id,
                "object": "chat.completion.chunk",
                "created": now,
                "model": model_name,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": usage,
            }
            yield f"data: {json.dumps(end_chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(_sse(), media_type="text/event-stream")

    r = await _submit_one(
        question,
        fresh=fresh,
        user_id=uid,
        wait=True,
        timeout=timeout,
        request=request,
        endpoint="/v1/chat/completions",
    )
    kind = r.get("kind")
    if kind != "OK":
        status, code = _KIND_HTTP.get(kind, (502, "server_error"))
        msg = r.get("error") or f"upstream returned {kind}"
        return _openai_error(status, msg, code)

    answer = r.get("answer") or ""
    usage = r.get("usage") or usage_of(question, answer)
    now = int(time.time())

    return {
        "id": cmpl_id,
        "object": "chat.completion",
        "created": now,
        "model": model_name,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": answer},
            "finish_reason": "stop",
        }],
        "usage": usage,
        "gemini_proxy": {
            "task_id": r.get("task_id"),
            "user_id": r.get("user_id"),
            "kind": kind,
            "slot": r.get("slot"),
            "solve": r.get("solve", 0),
            "mode": r.get("mode"),
            "queue_s": r.get("queue_s"),
            "serve_s": r.get("serve_s"),
            "api_s": round(time.time() - t0, 2),
        },
    }


@app.get("/admin/settings")
def admin_settings(request: Request,
                   x_api_key: str | None = Header(default=None),
                   authorization: str | None = Header(default=None)):
    _admin(x_api_key, authorization, request)
    return {"settings": CFG.public_settings(), "keys": CFG.list_keys(),
            "admin_key_set": bool(CFG.api_key)}


@app.post("/admin/settings")
async def admin_settings_update(request: Request,
                                x_api_key: str | None = Header(default=None),
                                authorization: str | None = Header(default=None)):
    _admin(x_api_key, authorization, request)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "invalid json")
    if not isinstance(data, dict):
        raise HTTPException(400, "body must be an object")
    old_backend = CFG.backend
    updated = CFG.update_settings(data)
    if METRICS is not None:
        METRICS.preview_chars = max(0, CFG.metrics_preview_chars)
    if POOL is not None and CFG.backend != old_backend:
        POOL.set_backend(make_backend(CFG.backend, CFG))
    return updated


@app.post("/admin/bootstrap")
def admin_bootstrap():
    """只在还没有密钥且显式开启 ALLOW_BOOTSTRAP=1 时可用，用来生成第一个管理员密钥。"""
    if not CFG.allow_bootstrap:
        raise HTTPException(
            403, "bootstrap disabled: set API_KEY in the environment "
                 "(or ALLOW_BOOTSTRAP=1 temporarily)")
    if CFG.auth_enabled:
        raise HTTPException(409, "管理员密钥已存在，请用它登录")
    return {"api_key": CFG.bootstrap_admin(), "role": "admin"}


@app.post("/admin/keys")
def admin_create_key(body: KeyIn, request: Request,
                     x_api_key: str | None = Header(default=None),
                     authorization: str | None = Header(default=None)):
    _admin(x_api_key, authorization, request)
    try:
        made = CFG.create_key(body.user_id, body.max_concurrent,
                              body.queue_limit, note=body.note)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return made


@app.patch("/admin/keys/{user_id}")
@app.put("/admin/keys/{user_id}")
async def admin_update_key(user_id: str, request: Request,
                           x_api_key: str | None = Header(default=None),
                           authorization: str | None = Header(default=None)):
    _admin(x_api_key, authorization, request)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "invalid json")
    if not isinstance(data, dict):
        raise HTTPException(400, "body must be an object")
    try:
        return CFG.update_key(user_id, data)
    except KeyError:
        raise HTTPException(404, "unknown user")


@app.post("/admin/keys/{user_id}/rotate")
def admin_rotate_key(user_id: str, request: Request,
                     x_api_key: str | None = Header(default=None),
                     authorization: str | None = Header(default=None)):
    _admin(x_api_key, authorization, request)
    try:
        return CFG.rotate_key(user_id)
    except KeyError:
        raise HTTPException(404, "unknown user")


@app.delete("/admin/keys/{user_id}")
def admin_delete_key(user_id: str, request: Request,
                     x_api_key: str | None = Header(default=None),
                     authorization: str | None = Header(default=None)):
    _admin(x_api_key, authorization, request)
    try:
        CFG.delete_key(user_id)
    except KeyError:
        raise HTTPException(404, "unknown user")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"deleted": user_id}


@app.get("/admin/metrics")
def admin_metrics(request: Request, window: int = 900, top_ips: int = 200,
                  recent: int = 200,
                  x_api_key: str | None = Header(default=None),
                  authorization: str | None = Header(default=None)):
    _admin(x_api_key, authorization, request)
    assert METRICS and POOL
    snap = METRICS.snapshot(window=window, top_ips=top_ips, recent=recent)
    snap["process"] = process_info()
    snap["pool"] = POOL.stats(tenants=CFG.tenants)
    snap["soak"] = SOAK.status() if SOAK else None
    return snap


@app.post("/admin/metrics/reset")
def admin_metrics_reset(request: Request,
                        x_api_key: str | None = Header(default=None),
                        authorization: str | None = Header(default=None)):
    _admin(x_api_key, authorization, request)
    assert METRICS
    METRICS.reset()
    return {"ok": True}


@app.get("/admin/tasks")
def admin_live_tasks(request: Request,
                     x_api_key: str | None = Header(default=None),
                     authorization: str | None = Header(default=None)):
    _admin(x_api_key, authorization, request)
    assert POOL
    tasks = [t.view(with_result=False) for t in POOL.live_tasks()]
    return {"count": len(tasks), "paused": POOL.paused, "tasks": tasks}


@app.post("/admin/pool/pause")
def admin_pool_pause(request: Request,
                     x_api_key: str | None = Header(default=None),
                     authorization: str | None = Header(default=None)):
    _admin(x_api_key, authorization, request)
    assert POOL
    POOL.paused = True
    return {"paused": POOL.paused}


@app.post("/admin/pool/resume")
def admin_pool_resume(request: Request,
                      x_api_key: str | None = Header(default=None),
                      authorization: str | None = Header(default=None)):
    _admin(x_api_key, authorization, request)
    assert POOL
    POOL.paused = False
    return {"paused": POOL.paused}


@app.post("/admin/pool/clear")
def admin_pool_clear(request: Request,
                     x_api_key: str | None = Header(default=None),
                     authorization: str | None = Header(default=None)):
    _admin(x_api_key, authorization, request)
    assert POOL
    cleared = POOL.clear_queue()
    return {"cleared": cleared, "queued": POOL.q.qsize()}


@app.get("/admin/soak")
def admin_soak_status(request: Request,
                      x_api_key: str | None = Header(default=None),
                      authorization: str | None = Header(default=None)):
    _admin(x_api_key, authorization, request)
    assert SOAK
    return SOAK.status()


@app.post("/admin/soak/start")
def admin_soak_start(body: SoakIn, request: Request,
                     x_api_key: str | None = Header(default=None),
                     authorization: str | None = Header(default=None)):
    _admin(x_api_key, authorization, request)
    assert SOAK
    try:
        return SOAK.start(
            concurrency=body.concurrency,
            duration_s=body.duration_s,
            max_requests=body.max_requests,
            think_ms=body.think_ms,
            users=body.users,
        )
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.post("/admin/soak/stop")
def admin_soak_stop(request: Request,
                    x_api_key: str | None = Header(default=None),
                    authorization: str | None = Header(default=None)):
    _admin(x_api_key, authorization, request)
    assert SOAK
    return SOAK.stop()


@app.post("/warm")
def warm(request: Request, n: int = 1,
         x_api_key: str | None = Header(default=None),
         authorization: str | None = Header(default=None)):
    assert POOL
    _admin(x_api_key, authorization, request)
    made = POOL.warm(max(1, n))
    return {"spawned": made, **POOL.stats(tenants=CFG.tenants)}
