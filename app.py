"""muse2api — 把 Muse(muse.ai) 网页免费账号的对话/生图/生视频额度反代成 API。

OpenAI 兼容:
  POST /v1/chat/completions          （文本 / 代码对话，支持 stream，可接 Codex）
  POST /v1/images/generations
  POST /v1/videos  +  GET /v1/videos/{task_id}
  GET  /v1/models
  GET  /v1/media/{name}

管理（前端账号池管理页面在 GET /）:
  GET    /admin/status
  GET    /admin/accounts
  POST   /admin/accounts            （单条 / 批量文本 / 批量数组）
  PATCH  /admin/accounts/{id}       （改标签、启用/禁用）
  DELETE /admin/accounts/{id}
  POST   /admin/accounts/{id}/test  （真实打开 muse.ai 验证会话是否有效）
  POST   /admin/accounts/{id}/relogin
  GET    /admin/tasks               （任务记录）
  DELETE /admin/tasks/{id}  |  POST /admin/tasks/clear
  GET    /admin/media  |  POST /admin/media/delete  |  DELETE /admin/media/{name}
  GET    /admin/extension           （浏览器扩展 zip，用来取 cookie）
  GET    /admin/cookie-helper       （命令行取 cookie 脚本，进阶）
"""
from __future__ import annotations

from typing import Any

import asyncio
import copy
from contextlib import contextmanager
from contextvars import ContextVar
import base64
import collections
import binascii
import io
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
import zipfile

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response, StreamingResponse)
from pydantic import model_validator, BaseModel, Field, SecretStr, field_validator

from config import CFG
from engine import (ESSENTIAL_COOKIES, MAX_REFERENCE_IMAGE_BYTES,
                    MAX_REFERENCE_IMAGES, MAX_REFERENCE_TOTAL_BYTES,
                    REFERENCE_MIME_EXT, MuseAuthError, MuseEngine, MuseGenerationError,
                    MuseModelRefusalError, MuseRegionRestrictedError,
                    RefImageSafetyError, reference_kind)
from engine import MuseProxyError
from store import Store, account_expiry, min_expiry
from proxy import normalize_server, request_proxy_context, split_proxy_url
from maintenance import MaintenanceSettings, MaintenanceSettingsStore, operation_due
from diagnostics import Journal, redact, safe_message
from account_identity import AccountIdentityConflict, identity_from_session, identity_keys
from autologin import (AutoLoginService, LoginCapacityError, LoginConflictError,
                       LoginNotFoundError)
from releases import ReleaseChecker
from cleanup import CleanupManager, CleanupSettings
from generation_specs import validate_geometry, geometry_instruction
from quota_dates import reset_metadata
from task_views import project_task
from media_history import index_legacy_media

import sys
log = logging.getLogger("muse2api")
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(_h)

CFG.ensure_dirs()
app = FastAPI(title="muse2api", version="1.6.13")

# Cookie 助手脚本从 muse.ai 页面发起导入请求，需要放行该来源；
# 浏览器扩展从 chrome-extension:// 发起，也一并放行。
#
# 这里直接放行所有来源：本服务用 Bearer Key 鉴权、不依赖 Cookie，
# 放行来源不会带来越权风险；反之如果把来源限死，浏览器端的智能体
# （Open WebUI / LobeChat / 各种 Web 客户端）会被 CORS 拦住用不了。
_origins = [o.strip() for o in (CFG.cors_origins or "").split(",") if o.strip()]
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(CORSMiddleware,
                   allow_origins=["*"],
                   allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                   allow_headers=["*"],
                   expose_headers=["*"],
                   max_age=600)

journal = Journal(os.path.join(CFG.data_dir, "diagnostics.jsonl"))
cleanup_manager = CleanupManager(
    CFG.media_dir, os.path.join(CFG.data_dir, "cleanup.json"),
    os.path.join(CFG.data_dir, "cleanup-state.json"), journal)
_CLEANUP_TASK = None
_CLEANUP_WAKEUP = asyncio.Event()
maintenance_settings = MaintenanceSettingsStore(
    os.path.join(CFG.data_dir, "maintenance.json"),
    MaintenanceSettings(keepalive_interval_minutes=CFG.keepalive_interval_min,
                        quota_interval_minutes=CFG.quota_refresh_min,
                        include_disabled=CFG.keepalive_disabled_accounts))
store = Store(CFG)
engine = MuseEngine(CFG, journal=journal)
engine.proxy_resolver = lambda aid: (store.get_account(aid) or {}).get("proxy")


class ConcurrencyLock:
    """多账号并发调度锁。
    由 CFG.max_concurrency 限制最大并发槽位；
    同时提供与标准 Lock 兼容的 acquire / release / locked 接口，
    确保向下兼容历史单锁断言与管理端操作。"""
    def __init__(self, limit: int = 2):
        self.limit = max(1, limit)
        self._sem = threading.Semaphore(self.limit)
        self._count_lock = threading.Lock()
        self._active_count = 0

    def acquire(self, blocking: bool = True, timeout: float | None = -1) -> bool:
        if timeout is None:
            timeout = -1
        if timeout < 0:
            ok = self._sem.acquire(blocking=blocking)
        else:
            ok = self._sem.acquire(blocking=blocking, timeout=timeout)
        if ok:
            with self._count_lock:
                self._active_count += 1
        return ok

    def release(self):
        with self._count_lock:
            if self._active_count > 0:
                self._active_count -= 1
        self._sem.release()

    def locked(self) -> bool:
        with self._count_lock:
            return self._active_count > 0

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class AccountPoolManager:
    """账号并发冲突管理器：确保同一账号不会同时被两个并发任务操作，多账号则并行执行。"""
    def __init__(self, store_instance):
        self.store = store_instance
        self.busy_accounts: set[str] = set()
        self.cond = threading.Condition()

    def acquire(self, account_id: str | None = None, preferred_id: str | None = None,
                rotate: bool = True, force_rotate: bool = False,
                timeout: float = 60.0, record_use: bool = True) -> dict:
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MuseGenerationError("等待空闲账号超时，当前所有账号均处于繁忙中")

                if account_id:
                    acc = self.store.get_account(account_id)
                    if not acc or not acc.get("enabled", True):
                        raise MuseAuthError(f"指定账号 {account_id} 不存在或已禁用")
                    canonical_id = acc["id"]
                    if canonical_id not in self.busy_accounts:
                        self.busy_accounts.add(canonical_id)
                        return acc
                else:
                    pref = preferred_id if preferred_id not in self.busy_accounts else None
                    acc = self.store.pick_account(
                        preferred_id=pref,
                        rotate=rotate,
                        force_rotate=force_rotate,
                        exclude_ids=self.busy_accounts,
                        record_use=record_use,
                    )
                    if acc:
                        self.busy_accounts.add(acc["id"])
                        return acc

                live = [a for a in self.store.accounts if a.get("enabled", True) and a.get("cookies")]
                if not live:
                    raise MuseAuthError("没有可用账号，请先在管理页面导入 cookie")

                self.cond.wait(timeout=min(remaining, 0.5))

    def release(self, account_id: str | None):
        if not account_id:
            return
        with self.cond:
            self.busy_accounts.discard(account_id)
            account = self.store.get_account(account_id)
            if account:
                self.busy_accounts.discard(account["id"])
            self.cond.notify_all()


GEN_LOCK = ConcurrencyLock(limit=CFG.max_concurrency)
ACCOUNT_POOL = AccountPoolManager(store)
IMAGE_TASK_LOCK = threading.Lock()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _bind_account_proxy(account: dict):
    """Pin the selected account's route for the whole operation, including retries."""
    if hasattr(engine, "_ensure_local"):
        engine._ensure_local().proxy_snapshot = copy.deepcopy(account.get("proxy")) or None


def _clear_account_proxy():
    if hasattr(engine, "_ensure_local"):
        local = engine._ensure_local()
        if hasattr(local, "proxy_snapshot"):
            del local.proxy_snapshot


def _account_proxy(account: dict):
    if hasattr(engine, "_ensure_local"):
        local = engine._ensure_local()
        if hasattr(local, "proxy_snapshot"):
            return local.proxy_snapshot
    return account.get("proxy")


@contextmanager
def _account_operation(aid: str):
    """Maintenance and settings share the same account lease as generation."""
    with ACCOUNT_POOL.cond:
        account = store.get_account(aid)
        if account:
            aid = account["id"]
        if not account:
            raise HTTPException(404, "账号不存在")
        if aid in ACCOUNT_POOL.busy_accounts:
            raise HTTPException(409, "账号正在执行任务或检测，请稍后再试")
        ACCOUNT_POOL.busy_accounts.add(aid)
        account = copy.deepcopy(account)
    try:
        _bind_account_proxy(account)
        yield account
    finally:
        try:
            engine.close_current_page()
        finally:
            _clear_account_proxy()
            ACCOUNT_POOL.release(aid)


# ------------------------- OpenAI 风格的错误响应 -------------------------
# 各类智能体基本都按 OpenAI 的 {"error": {"message": ...}} 取错误信息；
# FastAPI 默认返回的是 {"detail": ...}，客户端会读不到原因、只显示"未知错误"。
# 所以 /v1/* 统一转成 OpenAI 格式，管理接口保持原样（前端依赖 detail）。
def _err_type(status: int) -> str:
    if status == 404:
        return "not_found_error"
    if status == 429:
        return "rate_limit_error"
    if status >= 500:
        return "server_error"
    return "invalid_request_error"


@app.exception_handler(HTTPException)
async def _http_exc(request: Request, exc: HTTPException):
    if request.url.path.startswith("/v1/"):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": str(exc.detail),
                               "type": _err_type(exc.status_code),
                               "param": None, "code": exc.status_code}},
            headers=getattr(exc, "headers", None))
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": exc.detail},
                        headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def _validation_exc(request: Request, exc: RequestValidationError):
    # Never echo user-supplied proxy credentials in validation errors.
    errors = [{k: v for k, v in error.items() if k not in ("input", "ctx")}
              for error in exc.errors()]
    if request.url.path.startswith("/v1/"):
        return JSONResponse(status_code=422, content={"error": {
            "message": "请求参数校验失败：" + str(errors)[:400],
            "type": "invalid_request_error", "param": None, "code": 422}})
    return JSONResponse(status_code=422, content={"detail": errors})

MODELS = [
    {"id": "muse-spark", "object": "model", "owned_by": "muse",
     "description": "Muse Spark —— 文本 / 代码对话（网页免费额度，支持流式）"},
    {"id": "muse-image", "object": "model", "owned_by": "muse",
     "description": "Muse Image —— 文生图 / 图像编辑（网页免费额度）"},
    {"id": "muse-video", "object": "model", "owned_by": "muse",
     "description": "Muse Video —— 文生视频 / 图生视频（网页免费额度）"},
]

# 下游（Codex / Cline / 各种客户端）习惯按 OpenAI、Anthropic 的名字传模型，
# 这里统一映射到 muse 的真实能力上。
#
# 说明：muse.ai 网页是自动路由的 agent，对外**没有可枚举的模型清单**，
# 能稳定调用的就是三条真实能力 —— Muse Spark（语言/代码）、
# Muse Image（生图）、Muse Video（生视频）。别名只是让下游不用改配置。
MODEL_ALIASES = {
    # ---- 文本 / 代码 → muse-spark ----
    "muse-text": "muse-spark", "muse-chat": "muse-spark", "muse-llm": "muse-spark",
    "koda": "muse-spark",
    "gpt-3.5-turbo": "muse-spark", "gpt-4": "muse-spark", "gpt-4-turbo": "muse-spark",
    "gpt-4o": "muse-spark", "gpt-4o-mini": "muse-spark", "gpt-4.1": "muse-spark",
    "gpt-4.1-mini": "muse-spark", "gpt-5": "muse-spark", "gpt-5-codex": "muse-spark",
    "o1": "muse-spark", "o1-mini": "muse-spark", "o3": "muse-spark",
    "o3-mini": "muse-spark", "o4-mini": "muse-spark",
    "codex": "muse-spark", "codex-mini-latest": "muse-spark",
    "claude-3-5-sonnet": "muse-spark", "claude-3-5-sonnet-latest": "muse-spark",
    "claude-3-7-sonnet": "muse-spark", "claude-sonnet-4": "muse-spark",
    "claude-opus-4": "muse-spark", "claude-3-opus": "muse-spark",
    "claude-3-haiku": "muse-spark",
    "deepseek-chat": "muse-spark", "deepseek-coder": "muse-spark",
    "deepseek-reasoner": "muse-spark", "qwen-coder": "muse-spark",
    "gemini-2.5-pro": "muse-spark", "gemini-2.5-flash": "muse-spark",
    # ---- 生图 → muse-image ----
    "muse-img": "muse-image", "dall-e": "muse-image", "dall-e-3": "muse-image",
    "gpt-image-1": "muse-image", "flux": "muse-image", "midjourney": "muse-image",
    # ---- 生视频 → muse-video ----
    "muse-vid": "muse-video", "muse-videos": "muse-video",
    "sora": "muse-video", "sora-2": "muse-video", "veo": "muse-video",
    "veo-3": "muse-video", "kling": "muse-video", "runway": "muse-video",
}


def resolve_model(name: str | None, default: str = "muse-image") -> str:
    n = (name or "").strip().lower()
    return MODEL_ALIASES.get(n, n or default)


# ------------------------- 鉴权 -------------------------
def auth(authorization: str | None = Header(default=None)):
    if not CFG.api_key:
        return True
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "缺少 Authorization: Bearer <key>")
    parts = authorization.split(None, 1)
    token = parts[1].strip() if len(parts) > 1 else ""
    if not token or token != CFG.api_key:
        raise HTTPException(401, "API key 无效")
    return True


# ------------------------- 请求模型 -------------------------

def _renew_and_persist(acc_id: str, wake_vm: bool = True, force: bool = False) -> dict | None:
    """调用 /api/session 续签账号 cookie 并写回 store，返回最新账号 dict。
    近期（10分钟内）已续签且状态正常的账号直接复用，避免每次请求阻塞 2~3 秒 HTTP 往返。"""
    acc = store.get_account(acc_id)
    if not acc or not acc.get("cookies"):
        return acc
    now = time.time()
    last_sync = max(
        int(acc.get("synced_at") or 0),
        int(acc.get("last_keepalive") or 0),
        int(engine._last_http_renew.get(acc_id, 0)),
    )
    if not force and acc.get("ok") is True and (now - last_sync) < 600:
        engine._last_http_renew[acc_id] = last_sync
        return acc
    try:
        res = engine.renew_session_http(acc["cookies"], acc.get("cookies_exp"), wake_vm=wake_vm,
                                        **({"proxy": _account_proxy(acc)} if _account_proxy(acc) else {}))
        engine._last_http_renew[acc_id] = now
        if res.get("cookies"):
            store.update_account(acc_id, cookies=res["cookies"],
                                 cookies_exp=res.get("cookies_exp"), verified_session=True,
                                 ok=True if res.get("ok") else acc.get("ok"),
                                 synced_at=int(now))
            store.touch_keepalive(acc_id, True, f"会话正常 (VM: {res.get('vm_state') or 'RUNNING'})")
    except MuseAuthError as exc:
        store.mark(acc_id, False, str(exc))
        raise
    except MuseProxyError:
        raise
    except Exception as exc:
        log.warning("HTTP 预续签账号 %s 异常: %s", acc_id, exc)
    return store.get_account(acc_id)


def safe_chat_stream(cookies: dict, prompt: str, expires: dict | None,
                     timeout: int, account_id: str | None):
    """在独立线程中执行 chat_stream 并持有 GEN_LOCK，通过 Queue 往外吐。
    无论下游客户端何时断连、异常或超时，stop_event + finally 块保证 100% 立即释放 GEN_LOCK，绝不死锁。
    若首字前遇到单账号 VM 卡死或会话异常，自动切换下一个健康账号重试一次。"""
    import queue
    q = queue.Queue(maxsize=100)
    stop_event = threading.Event()

    def worker():
        cur_id = account_id
        cur_cookies = cookies
        cur_exp = expires
        last_exc = None
        leased_id = None
        try:
            for attempt in range(2):
                if stop_event.is_set():
                    return
                if attempt > 0:
                    if leased_id:
                        ACCOUNT_POOL.release(leased_id)
                        leased_id = None
                    try:
                        alt = ACCOUNT_POOL.acquire(rotate=True, force_rotate=True, timeout=timeout)
                    except Exception:
                        break
                    if not alt or alt["id"] == cur_id:
                        if alt:
                            leased_id = alt["id"]
                        break
                    cur_id = alt["id"]
                    leased_id = cur_id
                    cur_cookies = alt["cookies"]
                    cur_exp = alt.get("cookies_exp")
                    log.info("【对话自动切号】切换到备用账号 %s (%s) 重试...", alt.get("label"), cur_id)
                elif cur_id:
                    ACCOUNT_POOL.acquire(account_id=cur_id, timeout=timeout)
                    leased_id = cur_id

                account = store.get_account(cur_id) if cur_id else None
                _assign_generation_account(account or {"id": cur_id or "unassigned"}, attempt + 1)
                with journal.context(account_id=cur_id, account_label=safe_message((account or {}).get("label") or cur_id or ""), attempt=attempt + 1):
                    yielded = False
                    try:
                        if cur_id:
                            _bind_account_proxy(store.get_account(cur_id) or {})
                            refreshed = _renew_and_persist(cur_id, wake_vm=True, force=(attempt > 0))
                            if refreshed:
                                cur_cookies = refreshed["cookies"]
                                cur_exp = refreshed.get("cookies_exp")
                        # 必须带超时：图像/视频路径是「先拿浏览器槽、再等账号」，
                        # 对话路径是「先拿账号、再等槽」。视频单任务最长 10 分钟，
                        # 对话在这里无超时地等槽会把请求无限期挂住（超过 chat_timeout）。
                        if not GEN_LOCK.acquire(timeout=max(1, timeout)):
                            raise MuseGenerationError("等待浏览器并发槽超时，请稍后重试")
                        try:
                            if stop_event.is_set():
                                return
                            engine.start()
                            for chunk in engine.chat_stream(
                                cur_cookies, prompt, cur_exp, timeout,
                                account_id=cur_id, stop_event=stop_event
                            ):
                                yielded = True
                                q.put(("data", chunk))
                                if stop_event.is_set():
                                    return
                        finally:
                            GEN_LOCK.release()
                        if cur_id:
                            store.mark(cur_id, True, "")
                            _sync_cookies(cur_id)
                        return
                    except MuseAuthError as exc:
                        last_exc = exc
                        if cur_id:
                            store.mark(cur_id, False, str(exc))
                        engine.close_current_page()
                        if yielded:
                            break
                    except Exception as exc:
                        last_exc = exc
                        try:
                            engine.reset_thread()
                        except Exception:
                            pass
                        if yielded:
                            break
            if last_exc is not None:
                q.put(("error", last_exc))
        finally:
            if leased_id:
                ACCOUNT_POOL.release(leased_id)
            engine.close_current_page()
            _clear_account_proxy()
            q.put(("done", None))

    def traced_worker():
        with _generation_trace():
            worker()
    threading.Thread(target=traced_worker, daemon=True).start()

    try:
        while True:
            kind, val = q.get()
            if kind == "data":
                yield val
            elif kind == "error":
                raise val
            else:
                break
    finally:
        stop_event.set()


class ImageRequest(BaseModel):
    prompt: str
    model: str = "muse-image"
    n: int = 1
    size: str | None = None
    aspect_ratio: str | None = None
    resolution: str | None = Field(default=None, max_length=64)
    response_format: str = "url"      # url | b64_json
    timeout: int | None = Field(default=None, ge=1, le=600)
    extra: str | None = None
    image: Any = None
    images: list | None = None
    videos: list | None = None
    audios: list | None = None
    files: list | None = None
    reference_image: str | None = None
    async_: bool = Field(False, alias="async")


    @model_validator(mode="after")
    def validate_target_geometry(self):
        validate_geometry(self.size, self.aspect_ratio, self.resolution)
        return self


class VideoRequest(BaseModel):
    prompt: str
    model: str = "muse-video"
    # 视频单次要消耗账号额度，脏参数（0/负数/超大值）会直接被写进提示词。
    # 与 ImageRequest 对齐：时长 1~30 秒，单次任务 ~10 分钟。
    duration: int | None = Field(default=None, ge=1, le=30)
    size: str | None = None
    aspect_ratio: str | None = None
    resolution: str | None = None
    timeout: int | None = Field(default=None, ge=1, le=600)
    extra: str | None = None
    image: Any = None
    images: list | None = None
    videos: list | None = None
    audios: list | None = None
    files: list | None = None
    image_url: Any = None
    reference_image: str | None = None


    @model_validator(mode="after")
    def validate_target_geometry(self):
        validate_geometry(self.size, self.aspect_ratio, self.resolution)
        return self


class ChatMessage(BaseModel):
    role: str
    content: str | list | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list | None = None


class ChatRequest(BaseModel):
    """OpenAI Chat Completions 请求。

    对第三方客户端要尽量宽松：不认识字段一律忽略（Pydantic 默认行为），
    只挑我们真正用得上的读 —— 否则各种智能体各传各的参数就会 422。
    """
    model: str = "muse-spark"
    messages: list[ChatMessage] = []
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: int | None = None
    prompt: str | None = None       # 兼容把 prompt 直接放顶层的客户端
    # 下面这些声明出来只是为了「能读到」，muse.ai 端不做对应处理
    tools: list | None = None
    tool_choice: object | None = None
    response_format: object | None = None
    stream_options: object | None = None


class ResponsesRequest(BaseModel):
    """OpenAI Responses API（新版 Codex 默认走这个）。"""
    model: str = "muse-spark"
    input: str | list | None = None
    instructions: str | None = None
    stream: bool = False
    max_output_tokens: int | None = None
    timeout: int | None = None
    tools: list | None = None
    store: bool | None = None


class AccountProxy(BaseModel):
    server: str
    username: str = ""
    password: SecretStr | None = None

    @model_validator(mode="before")
    @classmethod
    def parse_proxy_url(cls, value):
        return split_proxy_url(value) if isinstance(value, dict) else value

    @field_validator("server")
    @classmethod
    def validate_server(cls, value):
        return normalize_server(value)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value):
        if ":" in value or any(ord(c) < 32 for c in value):
            raise ValueError("Proxy username cannot contain a colon or control characters")
        try:
            value.encode("latin-1")
        except UnicodeError:
            raise ValueError("Proxy credentials must use Latin-1 characters") from None
        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value):
        if value is not None:
            try:
                value.get_secret_value().encode("latin-1")
            except UnicodeError:
                raise ValueError("Proxy credentials must use Latin-1 characters") from None
        return value

    def configuration(self, previous=None):
        previous = previous or {}
        password = self.password.get_secret_value() if self.password is not None else None
        if password is None:
            if previous.get("server") == self.server and previous.get("username", "") == self.username:
                password = previous.get("password", "")
            elif self.username:
                raise HTTPException(400, "Enter the proxy password when changing its server or username")
            else:
                password = ""
        return {"server": self.server, "username": self.username, "password": password}


class AccountRequest(BaseModel):
    label: str = ""
    cookies: dict[str, str] = Field(default_factory=dict)
    cookie_header: str | None = None
    batch: str | None = None          # 多行文本，每行一个账号
    expires: dict[str, int] = Field(default_factory=dict)   # cookie 名 -> 过期时间戳
    proxy: AccountProxy | None = None


class AccountPatch(BaseModel):
    label: str | None = None
    enabled: bool | None = None
    proxy: AccountProxy | None = None


class ProxyTestRequest(BaseModel):
    proxy: AccountProxy


# ------------------------- 提示词构造 -------------------------
def _image_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        candidate = (value.get("image_url") or value.get("video_url") or value.get("audio_url")
                     or value.get("file_url") or value.get("url") or value.get("b64_json")
                     or value.get("data"))
        if isinstance(candidate, dict):
            candidate = candidate.get("url")
        return candidate.strip() if isinstance(candidate, str) and candidate.strip() else None
    return None


def _reference_list(images: list | None, legacy: list[Any], field: str,
                    extra: dict[str, list | None] | None = None) -> list[str]:
    """Validate ordered references and make legacy precedence explicit.

    顺序：images（或旧版单图字段）在前，其后依次是 videos / audios / files。
    images 本身也可以混放视频、音频与文档，类型在预处理时按 MIME/文件头识别。
    """
    primary = next((ref for value in legacy if (ref := _image_value(value))), None)
    if primary and images:
        raise HTTPException(400, f"{field} 不能与旧版单图字段同时使用")
    groups: list[tuple[str, list]] = []
    if primary:
        groups.append((field, [primary]))
    elif images is not None:
        groups.append((field, images))
    for name, values in (extra or {}).items():
        if values is not None:
            if not isinstance(values, list):
                raise HTTPException(400, f"{name} 必须是数组")
            groups.append((name, values))
    total = sum(len(values) for _, values in groups)
    if total > MAX_REFERENCE_IMAGES:
        raise HTTPException(400, f"参考素材最多 {MAX_REFERENCE_IMAGES} 个（当前 {total} 个）")
    refs = []
    for name, values in groups:
        for index, value in enumerate(values):
            ref = _image_value(value)
            if not ref:
                raise HTTPException(400, f"{name}[{index}] 不是有效素材引用")
            refs.append(ref)
    return refs


def _extra_media(req) -> dict[str, list | None]:
    return {"videos": req.videos, "audios": req.audios, "files": req.files}


def _image_references(req: ImageRequest) -> list[str]:
    return _reference_list(req.images, [req.reference_image, req.image], "images",
                           _extra_media(req))


def _video_references(req: VideoRequest) -> list[str]:
    return _reference_list(
        req.images, [req.reference_image, req.image_url, req.image], "images",
        _extra_media(req))


def _reference_kinds(prepared: list[str]) -> list[str]:
    """预处理后的 data URL -> image/video/audio/document 列表（保持顺序）。"""
    return [reference_kind(ref[5:].split(";", 1)[0]) for ref in prepared]


_KIND_LABELS = {"image": ("张", "图片"), "video": ("段", "视频"),
                "audio": ("段", "音频"), "document": ("份", "文档")}


def _describe_kinds(kinds: list[str]) -> str:
    counts = collections.Counter(kinds)
    return "、".join(f"{counts[k]} {_KIND_LABELS[k][0]}{_KIND_LABELS[k][1]}"
                    for k in ("image", "video", "audio", "document") if counts[k])


# 工作台提示词用 [图N] / [视频N] / [音频N] 指代素材，按同类顺序编号（与上传顺序一致）。
# muse 不认识这些标记，所以把「标记 -> 第几个附件」写进提示词。
_KIND_TOKENS = {"image": "图", "video": "视频", "audio": "音频", "document": "文档"}
_REFERENCE_TOKEN = re.compile(r"\[(?:图|视频|音频|文档)\d+\]")


def _reference_legend(kinds: list[str]) -> str:
    seen: collections.Counter = collections.Counter()
    parts = []
    for index, kind in enumerate(kinds, 1):
        seen[kind] += 1
        parts.append(f"[{_KIND_TOKENS[kind]}{seen[kind]}] 是第 {index} 个附件（{_KIND_LABELS[kind][1]}）")
    return "附件对照：" + "，".join(parts)

def build_image_prompt(r: ImageRequest, kinds: list[str] | None = None) -> str:
    refs = _image_references(r)
    has_ref = bool(refs)
    kinds = kinds or ["image"] * len(refs)
    if has_ref and any(k != "image" for k in kinds):
        parts = [f"基于我本次上传附带的参考素材（{_describe_kinds(kinds)}；{_reference_legend(kinds)}；"
                 f"只使用本次上传的这些附件，各附件的用途以下面的描述为准）"
                 f"进行生图/编辑，输出一张图片：{r.prompt.strip()}"]
    elif len(refs) > 1 and _REFERENCE_TOKEN.search(r.prompt):
        parts = [f"基于我本次上传附带的参考图片（{_reference_legend(kinds)}）进行生图/编辑：{r.prompt.strip()}"]
    elif has_ref:
        parts = [f"基于我本次上传附带的参考图片进行生图/编辑：{r.prompt.strip()}"]
    else:
        parts = [f"全新文生图创作（当前未提供任何参考图，请勿查找历史相册或向用户索要原图，直接根据文字描述从零绘制生成一张全新图片）：{r.prompt.strip()}"]
    ar = (r.aspect_ratio or "").strip().lower()
    sz = (r.size or "").strip().lower()

    if any(k in ar or k in sz for k in ("9:16", "9/16", "portrait", "竖屏", "720x1280", "1080x1920")):
        parts.append("【画面构图与比例要求】：严格 9:16 竖屏满屏画幅（9:16 vertical portrait aspect ratio，高大于宽的手机全屏竖版画面），绝对不要生成横屏，保持垂直构图")
    elif any(k in ar or k in sz for k in ("16:9", "16/9", "landscape", "横屏", "1280x720", "1920x1080")):
        parts.append("【画面构图与比例要求】：16:9 宽屏横屏画幅（16:9 widescreen landscape aspect ratio）")
    elif any(k in ar or k in sz for k in ("1:1", "square", "正方形", "1024x1024")):
        parts.append("【画面构图与比例要求】：1:1 正方形画幅（1:1 square aspect ratio）")
    elif any(k in ar or k in sz for k in ("4:3", "4/3")):
        parts.append("【画面构图与比例要求】：4:3 比例画幅")
    elif any(k in ar or k in sz for k in ("3:4", "3/4")):
        parts.append("【画面构图与比例要求】：3:4 竖向画幅")
    elif r.aspect_ratio:
        parts.append(f"【画面构图与比例要求】：{r.aspect_ratio} 画面比例")
    elif r.size:
        parts.append(f"尺寸/比例：{r.size}")

    target = geometry_instruction(r.size, r.aspect_ratio, r.resolution)
    if target:
        parts.append(target)

    if has_ref and "image" in kinds:
        parts.append("【纯净画面要求】：彻底清除并去除参考图中的所有文字、水印、签名、角标及Logo标记（clean image without any watermark, text, or logo），输出绝对纯净无字画面")

    if r.extra:
        parts.append(r.extra)
    return "，".join(parts)


def _content_text(content) -> str:
    """把 OpenAI 的 content 归一成纯文本（兼容多模态 list 形式）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("type")
                if t in (None, "text", "input_text", "output_text"):
                    parts.append(str(item.get("text") or ""))
                elif t == "image_url":
                    parts.append("[图片]")
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return str(content)


def build_chat_prompt(messages: list[ChatMessage]) -> str:
    """把 messages 拼成发给 muse.ai 的一段提示词。

    muse.ai 网页本身是个带上下文的会话，但本 API 是无状态的（每次可能落到
    不同账号/页面），所以把历史拼进 prompt 最可控 —— 这正好匹配 Codex 这类
    「每轮都带全量历史」的客户端。
    """
    system, turns = [], []
    for m in messages:
        role = (m.role or "").strip().lower()
        text = _content_text(m.content).strip()
        if role == "tool":
            # 工具执行结果 → 当成"用户提供的信息"发过去
            if text:
                turns.append(("user", "【工具执行结果】\n" + text))
            continue
        if not text:
            # 部分客户端的 assistant 消息只带 tool_calls、没有正文
            if role == "assistant" and m.tool_calls:
                turns.append(("assistant", "【请求调用工具】" + json.dumps(
                    m.tool_calls, ensure_ascii=False)[:600]))
            continue
        if role in ("system", "developer"):
            system.append(text)
        else:
            turns.append((role, text))

    # 单轮且无系统指令 → 直接发原文，最贴近自然对话
    if len(turns) == 1 and not system and turns[0][0] == "user":
        return turns[0][1]

    parts = []
    if system:
        sys_text = "\n\n".join(system)
        sys_text = sys_text.replace("danger-full-access", "standard-workspace-access")
        parts.append(f"背景与任务设定：\n{sys_text}")
    for role, text in turns:
        label = "助手" if role == "assistant" else "用户"
        parts.append(f"{label}：\n{text}")
    return "\n\n".join(parts)


# ------------------------- 工具调用（function calling）适配 -------------------------
# muse.ai 的网页模型**不会**返回结构化的 tool_calls，所以这里做一层协议适配：
#   1) 请求带 tools 时，把工具定义翻译成提示词里的【工具调用协议】；
#   2) 模型按协议输出 ```json {"tool": "...", "arguments": {...}} ```；
#   3) 我们把这段解析回 OpenAI 的 tool_calls 交给下游 agent。
#
# 注意：这是"尽力适配"而非保证 —— 目标模型是通用对话模型，没有针对
# function calling 做微调，遵守协议的程度需要实测观察。
_TOOL_PROTOCOL_HEAD = """你可以根据需要调用以下工具来协助用户完成任务。
若需调用工具，请直接输出如下格式的 JSON 代码块（不要包含其他多余解释）：
```json
{"name": "<工具名>", "arguments": {<参数>}}
```
如果需要调用多个工具，请输出包含多个对象的 JSON 数组。
如果无需调用工具，请直接用自然语言回答。

可用工具列表：
"""


def _describe_params(params) -> str:
    """把 JSON Schema 的参数描述成易读的多行文本。"""
    if not isinstance(params, dict):
        return "      （无参数）"
    props = params.get("properties") or {}
    required = set(params.get("required") or [])
    if not props:
        return "      （无参数）"
    lines = []
    for name, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        lines.append("      - %s (%s, %s)%s" % (
            name, spec.get("type") or "any",
            "必填" if name in required else "可选",
            (" " + spec["description"]) if spec.get("description") else ""))
    return "\n".join(lines)


def build_tools_prompt(tools: list | None) -> str:
    """把 tools 定义翻译成提示词片段（没有工具时返回空串）。

    同时兼容 Chat Completions 的 `{"type":"function","function":{...}}`
    和 Responses API 的 `{"type":"function","name":...,"parameters":...}`。
    """
    if not tools:
        return ""
    items = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        if not fn.get("name"):
            continue
        desc = fn.get("description") or ""
        items.append("%d. %s%s\n   参数：\n%s" % (
            len(items) + 1, fn["name"], (" — " + desc) if desc else "",
            _describe_params(fn.get("parameters"))))
    if not items:
        return ""
    return _TOOL_PROTOCOL_HEAD + "\n".join(items) + "\n"


_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*([\s\S]*?)```")


def _as_tool_calls(obj) -> list[dict] | None:
    """把解析出的 JSON 转成 OpenAI tool_calls；不像工具调用就返回 None。"""
    raw = obj if isinstance(obj, list) else [obj]
    if not raw:
        return None
    out = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        name = item.get("tool") or item.get("name") or item.get("function")
        if not isinstance(name, str) or not name:
            return None
        args = item.get("arguments")
        if args is None:
            args = item.get("parameters") or item.get("args") or {}
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        out.append({"id": "call_" + uuid.uuid4().hex[:20], "type": "function",
                    "function": {"name": name, "arguments": args}})
    return out or None


def parse_tool_calls(text: str) -> tuple[list[dict] | None, str]:
    """从模型输出里抽出工具调用。

    返回 (tool_calls, 剩余文本)；抽不到就返回 (None, 原文)。
    支持多种形态：
    1) ```json ... ``` 代码块；
    2) 裸 JSON（或带有前导 json/JSON 关键字）；
    3) 文本中内嵌的完整 JSON 对象或数组。
    """
    if not text:
        return None, text
    for m in reversed(list(_FENCE_RE.finditer(text))):
        try:
            calls = _as_tool_calls(json.loads(m.group(1).strip()))
        except Exception:
            continue
        if calls:
            return calls, (text[:m.start()] + text[m.end():]).strip()

    stripped = text.strip()
    clean_stripped = re.sub(r"^(?:```)?(?:json|JSON)?\s*", "", stripped).rstrip("`").strip()
    if clean_stripped.startswith(("{", "[")):
        try:
            calls = _as_tool_calls(json.loads(clean_stripped))
            if calls:
                return calls, ""
        except Exception:
            pass

    m_json = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
    if m_json:
        try:
            calls = _as_tool_calls(json.loads(m_json.group(1).strip()))
            if calls:
                rest = (text[:m_json.start()] + text[m_json.end():]).strip()
                if re.sub(r"^(?:```)?(?:json|JSON)?\s*", "", rest).strip("`").strip() == "":
                    rest = ""
                return calls, rest
        except Exception:
            pass

    return None, text


def build_video_prompt(r: VideoRequest, kinds: list[str] | None = None) -> str:
    user_prompt = r.prompt.strip()
    ar = (r.aspect_ratio or "").strip().lower()
    sz = (r.size or "").strip().lower()
    # 兜底 clamp：请求模型已限制 1~30，这里再挡一次，避免绕过校验的调用方
    # 把「时长严格为 -3 秒」这类自相矛盾的提示词发给模型（模型会因此拒绝）。
    dur = min(30, max(1, r.duration or 6))
    refs = _video_references(r)
    has_ref = bool(refs)
    kinds = kinds or ["image"] * len(refs)
    if any(k != "image" for k in kinds):
        # 不再默认「第一张图作起始画面」：实测会把模型拉回图片场景、忽略参考视频
        reference_instruction = (
            f"基于我本次上传的参考素材（{_describe_kinds(kinds)}）生成视频"
            f"（{_reference_legend(kinds)}；只使用本次上传的这些附件，严禁使用历史素材；"
            "各附件的用途以下面的描述为准）")
    elif len(refs) > 1 and _REFERENCE_TOKEN.search(user_prompt):
        reference_instruction = (
            f"基于我本次上传的 {len(refs)} 张参考图片生成视频（{_reference_legend(kinds)}；"
            "各图片的用途以下面的描述为准，未说明时以第一张作为主要起始画面；严禁使用历史图片或其他图像）")
    elif len(refs) > 1:
        reference_instruction = (
            f"基于我本次上传的 {len(refs)} 张参考图片生成视频（以第一张作为主要起始画面，"
            "其余图片作为主体、场景与风格参考，严禁使用历史图片或其他图像）")
    else:
        reference_instruction = (
            "基于我本次上传的参考图片附件作为第一帧参考图（严禁使用历史图片或任何其他图像，"
            "必须严格以我当前刚刚上传并附带在此处的这张图片为起始帧）")

    gen_word = "图生视频" if all(k == "image" for k in kinds) else "视频"
    is_vertical = any(k in ar or k in sz for k in ("9:16", "9/16", "portrait", "竖屏", "720x1280", "1080x1920"))

    parts = []
    if is_vertical:
        if has_ref:
            parts.append(f"{reference_instruction}：生成一个严格9:16竖屏手机满屏的动态{gen_word}（9:16 vertical portrait video，高大于宽的手机全屏竖版画面，严禁生成横屏或黑边，时长严格为 {dur} 秒）：{user_prompt}")
        else:
            parts.append(f"全新文生视频创作（纯文本全新生成，严禁参考任何历史图片或上下文）：生成一个严格9:16竖屏手机满屏视频（9:16 vertical portrait video，高大于宽的手机全屏竖版画面，严禁生成横屏或带有左右黑边，保持垂直构图，时长严格为 {dur} 秒）：{user_prompt}")
    elif any(k in ar or k in sz for k in ("16:9", "16/9", "landscape", "横屏", "1280x720", "1920x1080")):
        if has_ref:
            parts.append(f"{reference_instruction}：生成一个16:9宽屏横屏{gen_word}（16:9 widescreen landscape video，时长严格为 {dur} 秒）：{user_prompt}")
        else:
            parts.append(f"全新文生视频创作（纯文本全新生成，严禁参考任何历史图片或上下文）：生成一个16:9横屏宽屏视频（16:9 widescreen landscape video，时长严格为 {dur} 秒）：{user_prompt}")
    else:
        if has_ref:
            parts.append(f"{reference_instruction}：生成动态{gen_word}（时长严格为 {dur} 秒）：{user_prompt}")
        else:
            parts.append(f"全新文生视频创作（纯文本全新生成，严禁参考任何历史图片或上下文）：生成一个视频（时长严格为 {dur} 秒）：{user_prompt}")

    target = geometry_instruction(r.size, r.aspect_ratio, r.resolution)
    if target:
        parts.append(target)
    if r.extra:
        parts.append(r.extra)
    return "，".join(parts)


def media_url(name: str) -> str:
    """媒体地址。

    配了 public_base 就返回**绝对 URL** —— OpenAI 兼容客户端（以及各类智能体
    平台）拿到 data[].url 后一般会直接渲染或下载，相对路径会被解析到客户端
    自己的域名上，导致 404。public_base 为空时退回相对路径。
    """
    base = _public_base()
    return f"{base}/v1/media/{name}" if base else f"/v1/media/{name}"


# ------------------------- cookie 解析 -------------------------
def parse_cookie_text(text: str) -> dict[str, str]:
    """把 `a=1; b=2` / JSON / Set-Cookie 行解析成 dict。"""
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        import json
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return {str(k): str(v) for k, v in obj.items()}
        except Exception:  # noqa: BLE001
            pass
    out: dict[str, str] = {}
    for part in re.split(r"[;\n]+", text):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


def parse_batch(text: str) -> list[tuple[str, dict]]:
    """批量导入：每行 `标签 | cookie串`，标签可省略。"""
    out: list[tuple[str, dict]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        label = ""
        body = line
        if "|" in line:
            head, tail = line.split("|", 1)
            if "=" not in head:          # `|` 出现在 cookie 里时不算分隔符
                label, body = head.strip(), tail.strip()
        cookies = parse_cookie_text(body)
        if cookies:
            out.append((label, cookies))
    return out


# ------------------------- 核心生成 -------------------------
def _sync_cookies(acc_id: str) -> dict:
    """生成完从浏览器读回 cookie 并写回账号池。

    实测结论：**核心 cookie 的 expires 不会被使用行为续期**（hatch_vml 固定
    约 2 天就到期），这里同步回来的主要是那些每次访问都会重新下发的非核心
    cookie（_fbp / wd / dpr 等）以及可能新增的条目，避免账号信息比实际更旧。

    注意：这个函数**绝不能把生成结果搞失败** —— 生成已经成功了，
    同步只是锦上添花，出任何问题都只记一条日志。
    """
    try:
        live = engine.read_cookies()
        if not live:
            return {"synced": 0}
        acc = store.get_account(acc_id)
        if not acc:
            return {"synced": 0}
        cur = dict(acc.get("cookies") or {})
        new_vals = {k: v["value"] for k, v in live.items() if v.get("value")}
        # 有效期要「合并」而不是「覆盖」：会话 cookie 读回的 expires 是 -1，
        # 若整体覆盖会把原有有效期记录清空（实测踩过这个坑）。
        exps = dict(acc.get("cookies_exp") or {})
        exps.update({k: v["expires"] for k, v in live.items()
                     if _pos(v.get("expires"))})
        changed = sum(1 for k, v in new_vals.items() if cur.get(k) != v)
        store.update_account(acc_id, cookies={**cur, **new_vals},
                             cookies_exp=exps, synced_at=int(time.time()), verified_session=True)
        updated = store.get_account(acc_id) or {}
        return {"synced": len(new_vals), "changed": changed,
                "expires_at": updated.get("expires_at")}
    except Exception as exc:  # noqa: BLE001
        log.warning("cookie 同步失败（不影响本次生成）: %s", exc)
        return {"synced": 0, "error": str(exc)[:200]}


def _pos(v) -> bool:
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return False


_REQUEST_TRACE = ContextVar("muse_generation_request", default=None)


@contextmanager
def _generation_trace(task_id: str | None = None):
    current = _REQUEST_TRACE.get()
    if current is not None:
        yield current
        return
    trace = {"task_id": task_id, "request_id": task_id or "gen_" + uuid.uuid4().hex,
             "account_id": None, "account_label": None, "attempt": None}
    token = _REQUEST_TRACE.set(trace)
    try:
        with journal.context(task_id=task_id, request_id=trace["request_id"]):
            yield trace
    finally:
        _REQUEST_TRACE.reset(token)


def _assign_generation_account(account: dict, attempt: int):
    trace = _REQUEST_TRACE.get()
    aid, label = account["id"], safe_message(account.get("label") or account["id"])
    if trace is not None:
        trace.update(account_id=aid, account_label=label, attempt=attempt)
        if trace.get("task_id"):
            task = store.get_task(trace["task_id"]) or {}
            attempts = list(task.get("attempts") or [])
            attempts.append({"attempt": attempt, "account_id": aid, "account_label": label,
                             "assigned_at": int(time.time())})
            store.update_task(trace["task_id"], account=aid, account_id=aid,
                              account_label=label, attempts=attempts)
    journal.emit("generation.assigned", stage="account", account_id=aid,
                 account_label=label, attempt=attempt)


def _run_generation(prompt: str, kind: str, timeout: int,
                    account_id: str | None = None, on_progress=None,
                    reference_image: str | None = None,
                    reference_images: list[str] | None = None,
                    on_acquired=None) -> tuple[dict, str | None]:
    with _generation_trace():
        # Browser ownership covers account selection, retry and cleanup, not just generate().
        deadline = time.monotonic() + max(1, timeout)
        if not GEN_LOCK.acquire(timeout=max(1, timeout)):
            raise MuseGenerationError("等待浏览器队列超时，请稍后重试")
        try:
            if on_acquired:
                try:
                    on_acquired()
                except Exception:
                    pass
            return _run_generation_locked(prompt, kind, timeout, account_id,
                                          on_progress, reference_image, reference_images,
                                          deadline=deadline)
        finally:
            GEN_LOCK.release()



def _run_generation_locked(prompt: str, kind: str, timeout: int,
                    account_id: str | None = None, on_progress=None,
                    reference_image: str | None = None,
                    reference_images: list[str] | None = None,
                    deadline=None) -> tuple[dict, str | None]:
    remaining_wait = int(deadline - time.monotonic()) if deadline is not None else timeout
    acc = ACCOUNT_POOL.acquire(account_id=account_id,
                               preferred_id=None,
                               rotate=True,
                               timeout=max(1, remaining_wait))
    cur_acc = acc
    last_exc = None
    try:
        for attempt in range(2):
            if deadline is not None and time.monotonic() >= deadline:
                raise MuseGenerationError("任务总等待时限已到，停止重试")
            if attempt > 0:
                if isinstance(last_exc, MuseModelRefusalError):
                    break
                old_id = cur_acc["id"] if cur_acc else None
                if old_id:
                    ACCOUNT_POOL.release(old_id)
                cur_acc = None
                rem = int(deadline - time.monotonic()) if deadline is not None else timeout
                if rem <= 0:
                    break
                alt = ACCOUNT_POOL.acquire(rotate=True, force_rotate=True, timeout=rem)
                if not alt or (old_id and alt["id"] == old_id):
                    if alt:
                        cur_acc = alt
                    break
                cur_acc = alt
                log.info("【生图/视频自动切号】切换到备用账号 %s (%s) 重试...", cur_acc.get("label"), cur_acc["id"])
            _assign_generation_account(cur_acc, attempt + 1)
            with journal.context(account_id=cur_acc["id"], account_label=safe_message(cur_acc.get("label") or cur_acc["id"]), attempt=attempt + 1):
                try:
                    _bind_account_proxy(cur_acc)
                    refreshed = _renew_and_persist(cur_acc["id"], wake_vm=True, force=(attempt > 0))
                    if refreshed:
                        cur_acc = refreshed
                    engine.start()
                    remaining = int(deadline - time.monotonic()) if deadline is not None else timeout
                    if remaining <= 0:
                        raise MuseGenerationError("任务总等待时限已到，停止重试")
                    refs = list(reference_images or [])
                    engine_kwargs = {}
                    if len(refs) == 1 and not reference_image:
                        reference_image = refs[0]
                    elif len(refs) > 1:
                        engine_kwargs["reference_images"] = refs
                    with journal.context(account_id=cur_acc["id"], attempt=attempt + 1):
                        journal.emit("generation.attempt", stage="prepare", kind=kind,
                                     remaining_seconds=remaining, reference_count=len(refs))
                        res = engine.generate(cur_acc["cookies"], prompt, expect=kind,
                                              timeout=remaining, expires=cur_acc.get("cookies_exp"),
                                              account_id=cur_acc["id"], on_progress=on_progress,
                                              reference_image=reference_image, **engine_kwargs)
                    store.mark(cur_acc["id"], True, "")
                    _sync_cookies(cur_acc["id"])
                    return res, cur_acc["id"]
                except MuseAuthError as exc:
                    journal.error("generation.attempt_failed", exc, stage="generation", reason=safe_message(str(exc)))
                    last_exc = exc
                    store.mark(cur_acc["id"], False, str(exc))
                    engine.close_current_page()
                except MuseProxyError as exc:
                    journal.error("generation.attempt_failed", exc, stage="generation", reason=safe_message(str(exc)))
                    last_exc = exc
                    store.touch_keepalive(cur_acc["id"], None, f"代理异常: {str(exc)[:160]}")
                    engine.close_current_page()
                except MuseRegionRestrictedError as exc:
                    journal.error("generation.attempt_failed", exc, stage="generation", reason=safe_message(str(exc)))
                    # 地区限制是账号级限制（线上日志：同一账号反复出现「这个账号
                    # 所在的国家或地区不支持视频生成」）。标成异常并换号，而不是
                    # 像内容拒绝那样直接终止。
                    last_exc = exc
                    store.mark(cur_acc["id"], False,
                               f"该账号地区不支持视频生成: {str(exc)[:120]}")
                    try:
                        engine.reset_thread()
                    except Exception:
                        pass
                except MuseGenerationError as exc:
                    journal.error("generation.attempt_failed", exc, stage="generation", reason=safe_message(str(exc)))
                    last_exc = exc
                    store.mark(cur_acc["id"], True, f"任务异常: {str(exc)[:60]}")
                    try:
                        engine.reset_thread()
                    except Exception:
                        pass
                except Exception as exc:  # noqa: BLE001
                    journal.error("generation.attempt_failed", exc, stage="generation", reason=safe_message(str(exc)))
                    engine.close_current_page()
                    last_exc = MuseGenerationError(f"生成失败: {safe_message(str(exc))}")
        raise last_exc
    finally:
        if cur_acc:
            ACCOUNT_POOL.release(cur_acc.get("id"))
        engine.close_current_page()
        _clear_account_proxy()


# ------------------------- 基础接口 -------------------------
@app.get("/healthz")
def healthz():
    return {"status": "ok", "time": int(time.time())}


@app.get("/readyz")
def readyz():
    accs = [a for a in store.list_accounts() if a.get("enabled", True)]
    return {"status": "ready" if accs else "no_account",
            "accounts": len(accs),
            "browser_running": bool(engine.proc and engine.proc.poll() is None)}


@app.get("/v1/models")
def models(_=Depends(auth)):
    return {"object": "list", "data": MODELS}


# ------------------------- 生图 -------------------------
def _image_response(req: ImageRequest, res: dict) -> dict:
    item = {"revised_prompt": req.prompt, "url": media_url(res["filename"]),
            "size": req.size or "auto", "kind": res["kind"], "bytes": res["size"]}
    if req.response_format == "b64_json":
        fpath = res.get("path") or os.path.join(CFG.media_dir, res["filename"])
        with open(fpath, "rb") as f:
            item["b64_json"] = base64.b64encode(f.read()).decode()
        item.pop("url", None)
    return {"created": int(time.time()), "data": [item]}


def _generic_upload_mime(mime: str) -> bool:
    """multipart 上传常见的笼统类型（交给文件头识别）以及别名写法。"""
    return mime in ("application/octet-stream", "binary/octet-stream") or mime in (
        "image/jpg", "audio/mp3", "audio/x-wav", "audio/wave", "audio/x-m4a",
        "audio/m4a", "audio/x-flac", "audio/x-aac", "video/x-m4v")


def _prepare_reference_images(images: list[str]) -> list[str]:
    """Validate and snapshot all sources before a task is accepted."""
    prepared = []
    total_bytes = 0
    for index, image in enumerate(images):
        try:
            b64, mime = engine._normalize_image(image)
            data = base64.b64decode(b64, validate=True)
        except (RefImageSafetyError, MuseGenerationError, binascii.Error, ValueError) as exc:
            raise HTTPException(400, f"images[{index}]: {exc}") from exc
        total_bytes += len(data)
        if total_bytes > MAX_REFERENCE_TOTAL_BYTES:
            raise HTTPException(
                400, f"参考素材总大小超过 {MAX_REFERENCE_TOTAL_BYTES // (1024 * 1024)} MiB 限制")
        prepared.append(f"data:{mime};base64,{b64}")
    return prepared


def _task_runner(tid: str, worker):
    """Attach the durable task id to every engine phase in this worker thread."""
    with _generation_trace(tid) as trace:
        journal.emit("task.started", stage="queue")
        try:
            worker()
        except Exception as exc:
            _record_task_failure(tid, exc)
        finally:
            task = store.get_task(tid) or {}
            journal.emit("task.finished", stage="terminal", status=task.get("status"),
                         account_id=trace.get("account_id"), account_label=trace.get("account_label"),
                         attempt=trace.get("attempt"), duration_seconds=task.get("elapsed"))


def _record_task_failure(tid: str, exc: Exception, elapsed: float | None = None):
    reason = safe_message(str(exc))
    trace = _REQUEST_TRACE.get() or {}
    journal.error("task.failed", exc, task_id=tid, stage="generation", reason=reason,
                  account_id=trace.get("account_id"), account_label=trace.get("account_label"),
                  attempt=trace.get("attempt"))
    details = {"error_type": type(exc).__name__, "reason": reason,
               "account_id": trace.get("account_id"), "account_label": trace.get("account_label"),
               "attempt": trace.get("attempt"), "logs_url": f"/admin/tasks/{tid}/logs"}
    changes = {"status": "failed", "error": reason, "error_detail": details}
    if elapsed is not None:
        changes["elapsed"] = round(elapsed, 1)
    store.update_task(tid, **changes)


def _generation_metadata(req, references: list[str]) -> dict:
    """Small task metadata only: never persist reference payloads or secret URLs."""
    return {"size": req.size, "aspect_ratio": req.aspect_ratio,
            "resolution": getattr(req, "resolution", None),
            "duration": getattr(req, "duration", None),
            "model": req.model,
            "reference_counts": dict(collections.Counter(_reference_kinds(references)))}


def _queue_image(req: ImageRequest, prompt: str, reference_images: list[str],
                 idempotency_key: str | None = None):
    """Opt-in polling avoids reverse-proxy timeouts; no generation is repeated by polling."""
    if idempotency_key and len(idempotency_key) > 256:
        raise HTTPException(400, "Idempotency-Key too long")
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest() if idempotency_key else None
    request_hash = hashlib.sha256(json.dumps(
        {"request": req.model_dump(by_alias=True), "references": reference_images},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with IMAGE_TASK_LOCK:
        tasks = list(store.tasks.values())
        if key_hash:
            for existing in tasks:
                if existing.get("image_request_key") == key_hash:
                    if existing.get("image_request_hash") != request_hash:
                        raise HTTPException(409, "Idempotency-Key reused with different request")
                    return JSONResponse(status_code=202, content={
                        "id": existing["id"], "task_id": existing["id"],
                        "object": "image.task", "status": existing["status"],
                        "progress": existing.get("progress", 0),
                        "created_at": existing["created_at"]})
        # ponytail: one Chromium worker; cap admission instead of adding a broker.
        if sum(t.get("kind") == "image" and t.get("status") in ("queued", "processing")
               for t in tasks) >= 8:
            raise HTTPException(429, "Image queue is full; retry later")
        task = store.create_task("image", req.prompt)
        tid = task["id"]
        store.update_task(tid, api_prompt=prompt, size=req.size,
                          generation=_generation_metadata(req, reference_images),
                          response_format=req.response_format, progress=0,
                          image_request_key=key_hash, image_request_hash=request_hash)

    def worker():
        timing = {"t0": time.time()}
        def _on_acquired():
            timing["t0"] = time.time()
            store.update_task(tid, status="processing", progress=5)
        try:
            res, acc_id = _run_generation(
                prompt, "image", req.timeout or CFG.image_timeout,
                reference_images=reference_images,
                on_progress=lambda p: store.update_task(tid, progress=p),
                on_acquired=_on_acquired)
            # Store only media metadata, not large base64 payloads or reference credentials.
            store.update_task(tid, status="completed", progress=100, account=acc_id,
                              elapsed=round(time.time() - timing["t0"], 1),
                              url=media_url(res["filename"]),
                              result={**{k: res[k] for k in ("filename", "size", "kind")},
                                      **{k: res[k] for k in ("w", "h", "mime", "ext") if k in res},
                                      "url": media_url(res["filename"])})
        except Exception as exc:  # noqa: BLE001
            _record_task_failure(tid, exc, time.time() - timing["t0"])

    journal.emit("task.queued", task_id=tid, stage="queue", kind="image")
    threading.Thread(target=_task_runner, args=(tid, worker), daemon=True).start()
    return JSONResponse(status_code=202, content={
        "id": tid, "task_id": tid, "object": "image.task", "status": "queued",
        "progress": 0, "created_at": task["created_at"]})


@app.post("/v1/images/tasks")
def create_image_task(req: ImageRequest,
                      idempotency_key: str | None = Header(default=None), _=Depends(auth)):
    refs = _prepare_reference_images(_image_references(req))
    return _queue_image(req, build_image_prompt(req, _reference_kinds(refs)), refs, idempotency_key)


@app.get("/v1/images/tasks/{task_id}")
def get_image_task(task_id: str, _=Depends(auth)):
    task = store.get_task(task_id)
    if not task or task.get("kind") != "image":
        raise HTTPException(404, "image task 不存在")
    out = dict(task)
    if out.get("status") == "completed":
        req = ImageRequest(prompt=out["prompt"], size=out.get("size"),
                           response_format=out.get("response_format") or "url")
        out.update(_image_response(req, out["result"]))
    return out


def _run_sync_image_task(req: ImageRequest, prompt: str, refs: list[str], timeout: int):
    """Keep the synchronous API response, but retain its result in task history too."""
    task = store.create_task("image", req.prompt)
    tid = task["id"]
    store.update_task(tid, api_prompt=prompt, generation=_generation_metadata(req, refs),
                      size=req.size, status="queued", progress=0, response_format=req.response_format)
    started = time.time()
    with _generation_trace(tid) as trace:
        journal.emit("task.started", stage="queue", kind="image")
        try:
            result, aid = _run_generation(prompt, "image", timeout, reference_images=refs,
                on_acquired=lambda: store.update_task(tid, status="processing", progress=5))
            store.update_task(tid, status="completed", progress=100, account=aid,
                elapsed=round(time.time() - started, 1), url=media_url(result["filename"]),
                result={**{k: result[k] for k in ("filename", "size", "kind")},
                        **{k: result[k] for k in ("w", "h", "mime", "ext") if k in result},
                        "url": media_url(result["filename"])})
            return result, aid
        except Exception as exc:
            _record_task_failure(tid, exc, time.time() - started)
            raise
        finally:
            journal.emit("task.finished", stage="terminal", account_id=trace.get("account_id"),
                         account_label=trace.get("account_label"), status=(store.get_task(tid) or {}).get("status"))


@app.post("/v1/images/generations")
async def images_generations(req: ImageRequest, _=Depends(auth)):
    ref_images = await asyncio.to_thread(
        _prepare_reference_images, _image_references(req))

    prompt = build_image_prompt(req, _reference_kinds(ref_images))
    timeout = req.timeout or CFG.image_timeout
    if req.async_:
        return _queue_image(req, prompt, ref_images)
    try:
        res, _acc = await asyncio.to_thread(
            _run_sync_image_task, req, prompt, ref_images, timeout)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc
    return _image_response(req, res)


@app.post("/v1/images/edits")
async def images_edits(request: Request, _=Depends(auth)):
    """OpenAI 兼容的图生图/图像编辑接口，兼容 multipart/form-data 与 application/json。"""
    content_type = request.headers.get("content-type", "").lower()
    prompt = ""
    model = "muse-image"
    size = None
    resolution = None
    aspect_ratio = None
    response_format = "url"
    timeout = None
    ref_image_data: list[str] = []
    async_mode = False

    if "multipart/form-data" in content_type:
        form = await request.form(
            max_files=MAX_REFERENCE_IMAGES, max_fields=20)
        async_mode = form.get("async", False)
        prompt = form.get("prompt") or ""
        model = form.get("model") or "muse-image"
        size = form.get("size")
        resolution = form.get("resolution")
        aspect_ratio = form.get("aspect_ratio")
        response_format = form.get("response_format") or "url"
        timeout_val = form.get("timeout")
        if timeout_val:
            try:
                timeout = int(timeout_val)
            except ValueError:
                pass
        # image 可重复提交；video / audio / file 同样可重复，按字段顺序排在 image 之后
        img_fields = [(name, value) for name in ("image", "image[]", "video", "audio", "file")
                      for value in form.getlist(name)]
        if len(img_fields) > MAX_REFERENCE_IMAGES:
            raise HTTPException(400, f"参考素材最多 {MAX_REFERENCE_IMAGES} 个（当前 {len(img_fields)} 个）")
        total_upload_bytes = 0
        mib = 1024 * 1024
        for index, (name, img_field) in enumerate(img_fields):
            if img_field and hasattr(img_field, "read"):
                ref_mime = (getattr(img_field, "content_type", "") or "").split(";", 1)[0].strip().lower()
                if ref_mime and ref_mime not in REFERENCE_MIME_EXT and not _generic_upload_mime(ref_mime):
                    raise HTTPException(400, f"{name}[{index}] 文件类型不受支持（{ref_mime}）")
                content = await img_field.read(MAX_REFERENCE_IMAGE_BYTES + 1)
                if len(content) > MAX_REFERENCE_IMAGE_BYTES:
                    raise HTTPException(400, f"{name}[{index}] 超过 {MAX_REFERENCE_IMAGE_BYTES // mib} MiB 限制")
                total_upload_bytes += len(content)
                if total_upload_bytes > MAX_REFERENCE_TOTAL_BYTES:
                    raise HTTPException(400, f"参考素材总大小超过 {MAX_REFERENCE_TOTAL_BYTES // mib} MiB 限制")
                ref_mime = ref_mime or "application/octet-stream"
                ref_image_data.append(
                    f"data:{ref_mime};base64,{base64.b64encode(content).decode('ascii')}")
            elif isinstance(img_field, str) and img_field.strip():
                ref_image_data.append(img_field.strip())
            else:
                raise HTTPException(400, f"{name}[{index}] 不是有效素材引用")
    else:
        body = await request.json()
        async_mode = body.get("async", False)
        prompt = body.get("prompt") or ""
        model = body.get("model") or "muse-image"
        size = body.get("size")
        resolution = body.get("resolution")
        aspect_ratio = body.get("aspect_ratio")
        response_format = body.get("response_format") or "url"
        timeout = body.get("timeout")
        raw_images = body.get("images")
        if raw_images is not None and not isinstance(raw_images, list):
            raise HTTPException(400, "images 必须是数组")
        legacy = [body.get("reference_image"), body.get("image_url"), body.get("image")]
        ref_image_data = _reference_list(raw_images, legacy, "images", {
            name: body.get(name) for name in ("videos", "audios", "files")})

    if not prompt:
        prompt = "参考此图片并进行生图创作"

    req_obj = ImageRequest(
        prompt=prompt,
        model=model,
        size=size,
        resolution=resolution,
        aspect_ratio=aspect_ratio,
        response_format=response_format,
        timeout=timeout,
        images=ref_image_data,
        **{"async": async_mode}
    )
    ref_image_data = await asyncio.to_thread(
        _prepare_reference_images, ref_image_data)
    full_prompt = build_image_prompt(req_obj, _reference_kinds(ref_image_data))
    gen_timeout = timeout or CFG.image_timeout
    if req_obj.async_:
        return _queue_image(req_obj, full_prompt, ref_image_data)
    try:
        res, _acc = await asyncio.to_thread(
            _run_sync_image_task, req_obj, full_prompt, ref_image_data, gen_timeout)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc

    return _image_response(req_obj, res)


# ------------------------- 生视频（异步任务） -------------------------
@app.post("/v1/videos")
@app.post("/v1/videos/generations")
async def create_video(req: VideoRequest,
                       idempotency_key: str | None = Header(default=None),
                       _=Depends(auth)):
    ref_images = await asyncio.to_thread(
        _prepare_reference_images, _video_references(req))

    prompt = build_video_prompt(req, _reference_kinds(ref_images))
    timeout = req.timeout or CFG.video_timeout
    if idempotency_key and len(idempotency_key) > 256:
        raise HTTPException(400, "Idempotency-Key too long")
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest() if idempotency_key else None
    request_hash = hashlib.sha256(json.dumps(
        {"request": req.model_dump(by_alias=True), "references": ref_images},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    # 视频是最贵的任务（单个最长 10 分钟，还共用 chat/image 的浏览器并发槽），
    # 所以和图片一样做「幂等去重 + 准入上限」，否则客户端超时重试会直接翻倍
    # 烧额度，并发打满后连对话/生图都排不进去。
    with IMAGE_TASK_LOCK:
        tasks = list(store.tasks.values())
        if key_hash:
            for existing in tasks:
                if existing.get("video_request_key") == key_hash:
                    if existing.get("video_request_hash") != request_hash:
                        raise HTTPException(409, "Idempotency-Key reused with different request")
                    return {"id": existing["id"], "task_id": existing["id"],
                            "object": "video.task", "status": existing.get("status"),
                            "progress": existing.get("progress", 0),
                            "created_at": existing["created_at"]}
        active = sum(1 for t in tasks
                     if t.get("kind") == "video"
                     and t.get("status") in ("queued", "processing"))
        if active >= CFG.video_queue:
            raise HTTPException(429, "Video queue is full; retry later")
        task = store.create_task("video", req.prompt)
        store.update_task(task["id"], api_prompt=prompt, duration=req.duration,
                          generation=_generation_metadata(req, ref_images),
                          size=req.size, progress=10,
                          video_request_key=key_hash, video_request_hash=request_hash)

    def worker():
        timing = {"t0": time.time()}
        def _on_acquired():
            timing["t0"] = time.time()
            store.update_task(task["id"], status="processing", progress=15)
        try:
            def prog_cb(p):
                store.update_task(task["id"], progress=p)
            res, acc_id = _run_generation(prompt, "video", timeout, on_progress=prog_cb,
                                          reference_images=ref_images, on_acquired=_on_acquired)
            vurl = media_url(res["filename"])
            store.update_task(task["id"], status="completed", progress=100, account=acc_id,
                              elapsed=round(time.time() - timing["t0"], 1),
                              url=vurl,
                              video={"url": vurl},
                              result={"url": vurl,
                                      "filename": res["filename"],
                                      "bytes": res["size"], "kind": res["kind"],
                                      **{k: res[k] for k in ("w", "h", "mime", "ext") if k in res}})
        except Exception as exc:  # noqa: BLE001
            _record_task_failure(task["id"], exc, time.time() - timing["t0"])

    journal.emit("task.queued", task_id=task["id"], stage="queue", kind="video",
                 reference_count=len(ref_images))
    threading.Thread(target=_task_runner, args=(task["id"], worker), daemon=True).start()
    return {"id": task["id"], "task_id": task["id"], "object": "video.task", "status": "queued",
            "progress": 10, "created_at": task["created_at"]}


@app.get("/v1/videos/{task_id}")
@app.get("/v1/videos/generations/{task_id}")
def get_video(task_id: str, _=Depends(auth)):
    t = store.get_task(task_id)
    # 和 /v1/images/tasks/{id} 对齐：不允许拿图片/对话任务 ID 来查视频任务，
    # 否则会返回一个 completed 但没有 video 字段的“成功”任务。
    if not t or t.get("kind") != "video":
        raise HTTPException(404, "video task 不存在")
    out = dict(t)
    status = out.get("status")
    if status in ("succeeded", "success", "done"):
        out["status"] = "completed"
    if out.get("status") == "completed":
        out["progress"] = 100
    elif out.get("status") == "processing":
        # Progress reports observed milestones only; Muse exposes no percentage.
        out["progress"] = max(out.get("progress", 0) or 0, 15)
    vurl = out.get("url")
    if not vurl and isinstance(out.get("result"), dict):
        vurl = out["result"].get("url")
    if vurl:
        out["url"] = vurl
        if "video" not in out:
            out["video"] = {"url": vurl}
    return out


# ------------------------- 对话（OpenAI 兼容） -------------------------
def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _chat_chunk(cid: str, created: int, model: str, delta: dict,
                finish: str | None = None) -> dict:
    return {"id": cid, "object": "chat.completion.chunk", "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _want_usage(stream_options) -> bool:
    """下游（LangChain、部分 SDK / 智能体框架）会带
    `stream_options.include_usage=true`，要求在 [DONE] 之前补一个
    `choices: []` + `usage` 的分片。不发的话少数框架会一直等 usage 而卡住。"""
    if isinstance(stream_options, dict):
        return bool(stream_options.get("include_usage"))
    return False


def _pace_text(text: str, chunk_size: int = 2, delay: float = 0.012):
    """把大块文本平滑切分为仿原生 LLM 打字机的微流式 Token。
    若传入文本本身微小（<= 3 字），直接秒级放行，零延迟；
    若传入文本是大块（如 DOM 批量刷新），按每 chunk_size 字间隔 delay 平滑输出。
    """
    if not text:
        return
    if len(text) <= 3 or delay <= 0:
        yield text
        return
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]
        if i + chunk_size < len(text):
            time.sleep(delay)


_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, _=Depends(auth)):
    """OpenAI 兼容的对话接口 —— 各类智能体客户端都能接。

    说明：muse.ai 网页是**自动路由**的 agent，界面上没有模型选择器，所有请求
    都会落到同一个网页助手（自称 Koda，底层是 Muse 系列语言模型）。所以
    `model` 字段只用于兼容下游，不影响路由结果。

    请求带 `tools` 时，会注入【工具调用协议】并把模型输出的 JSON 解析回
    `tool_calls`（muse.ai 没有原生 function calling，这层属于协议适配）。

    对话与生图/生视频共用同一个浏览器实例，靠 `GEN_LOCK` 串行。
    """
    prompt = build_chat_prompt(req.messages) or (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "messages 为空")

    # 工具调用协议默认不注入 —— 实测 muse.ai 的助手会明确拒绝输出"伪工具调用"，
    # 注入反而污染正常回答；详见 config.py 的 tool_protocol 注释。
    tool_note = build_tools_prompt(req.tools) if CFG.tool_protocol else ""
    if tool_note:
        # 放在末尾：开头是 agent 自己的 system 提示，夹在中间容易被忽略；
        # 紧贴用户消息之前，模型对末尾指令的遵守度明显更高。
        prompt = prompt + "\n\n" + tool_note

    model = resolve_model(req.model, default="muse-spark")
    timeout = int(req.timeout or CFG.chat_timeout)
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "没有可用账号，请先在管理页面导入 cookie")

    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    acc_id = acc["id"]
    cookies = acc["cookies"]
    expires = acc.get("cookies_exp")

    def tool_call_deltas(calls: list[dict]) -> list[list[dict]]:
        """按 OpenAI 习惯分两片发：先 id/name，再 arguments 全文。"""
        head = [{"index": i, "id": c["id"], "type": "function",
                 "function": {"name": c["function"]["name"], "arguments": ""}}
                for i, c in enumerate(calls)]
        body = [{"index": i, "function": {"arguments": c["function"]["arguments"]}}
                for i, c in enumerate(calls)]
        return [head, body]

    if req.stream:
        def sync_stream():
            try:
                # 握手建立瞬间立即发送 role: assistant 首包，让下游客户端秒级捕获光标
                yield _sse(_chat_chunk(cid, created, model, {"role": "assistant"}))

                stream_gen = safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id)
                if not tool_note:
                    for chunk in stream_gen:
                        for piece in _pace_text(chunk):
                            yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                    yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                else:
                    buf, mode = "", None
                    for chunk in stream_gen:
                        if mode == "text":
                            for piece in _pace_text(chunk):
                                yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            continue
                        buf += chunk
                        if mode is None:
                            head = buf.lstrip()
                            if len(head) >= 2:
                                if head[0] in "{[" or head.startswith("```"):
                                    mode = "maybe_tool"
                                else:
                                    mode = "text"
                                    for piece in _pace_text(buf):
                                        yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                                    buf = ""
                    if mode == "text":
                        yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                    else:
                        calls, rest = parse_tool_calls(buf)
                        if calls:
                            if rest:
                                for piece in _pace_text(rest):
                                    yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            for piece in tool_call_deltas(calls):
                                yield _sse(_chat_chunk(cid, created, model, {"tool_calls": piece}))
                            yield _sse(_chat_chunk(cid, created, model, {}, finish="tool_calls"))
                        else:
                            if buf:
                                for piece in _pace_text(buf):
                                    yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                if _want_usage(req.stream_options):
                    yield _sse({"id": cid, "object": "chat.completion.chunk",
                                "created": created, "model": model,
                                "choices": [],
                                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
                yield "data: [DONE]\n\n"
                store.mark(acc_id, True, "")
            except MuseAuthError as exc:
                store.mark(acc_id, False, str(exc))
                yield _sse({"error": {"message": str(exc), "type": "auth_error", "code": 401}})
            except MuseGenerationError as exc:
                store.mark(acc_id, True, f"助手超时: {str(exc)[:60]}")
                try:
                    engine.reset_thread()
                except Exception:
                    pass
                yield _sse({"error": {"message": str(exc), "type": "server_error", "code": 502}})
            except Exception as exc:
                yield _sse({"error": {"message": f"内部错误: {exc}", "type": "server_error", "code": 500}})
        return StreamingResponse(sync_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    def run() -> str:
        return "".join(safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id))

    try:
        text = await asyncio.to_thread(run)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc))
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc))

    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if tool_note:
        calls, rest = parse_tool_calls(text)
        if calls:
            return {"id": cid, "object": "chat.completion", "created": created,
                    "model": model,
                    "choices": [{"index": 0, "finish_reason": "tool_calls",
                                 "message": {"role": "assistant",
                                             "content": rest or None,
                                             "tool_calls": calls}}],
                    "usage": usage}
    return {"id": cid, "object": "chat.completion", "created": created,
            "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": usage}


def _responses_messages(req: ResponsesRequest) -> list[ChatMessage]:
    """把 Responses API 的 instructions / input 归一成 messages。"""
    msgs: list[ChatMessage] = []
    if req.instructions:
        msgs.append(ChatMessage(role="system", content=req.instructions))
    inp = req.input
    if isinstance(inp, str):
        msgs.append(ChatMessage(role="user", content=inp))
        return msgs
    if isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                msgs.append(ChatMessage(role="user", content=item))
                continue
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if item.get("role"):                      # message 形态
                msgs.append(ChatMessage(role=item["role"],
                                        content=item.get("content")))
            elif itype == "input_text":
                msgs.append(ChatMessage(role="user", content=item.get("text")))
            elif itype == "function_call_output":
                msgs.append(ChatMessage(role="user", content="【工具执行结果】\n"
                                        + str(item.get("output") or "")))
            elif itype == "function_call":
                msgs.append(ChatMessage(role="assistant", content="【请求调用工具】"
                                        + str(item.get("name") or "")))
    return msgs


def _sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/v1/responses")
async def responses_api(req: ResponsesRequest, _=Depends(auth)):
    """OpenAI Responses API —— 新版 Codex 默认走这个端点。

    只实现 Codex 实际用到的子集：文本进 → 文本出（含流式事件）。
    muse.ai 不会返回结构化的 tool_calls，关于工具调用的边界见 README。
    """
    prompt = build_chat_prompt(_responses_messages(req))
    if not prompt:
        raise HTTPException(400, "input 为空")

    model = resolve_model(req.model, default="muse-spark")
    timeout = int(req.timeout or CFG.chat_timeout)
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "没有可用账号，请先在管理页面导入 cookie")

    rid = "resp_" + uuid.uuid4().hex[:24]
    mid = "msg_" + uuid.uuid4().hex[:24]
    created = int(time.time())
    acc_id, cookies, expires = acc["id"], acc["cookies"], acc.get("cookies_exp")

    def envelope(status: str, text: str = "") -> dict:
        done = status == "completed"
        return {"id": rid, "object": "response", "created_at": created,
                "status": status, "model": model,
                "output": [{"type": "message", "id": mid, "role": "assistant",
                            "status": "completed" if done else "in_progress",
                            "content": ([{"type": "output_text", "text": text,
                                          "annotations": []}] if text else [])}],
                "usage": {"input_tokens": 0, "output_tokens": 0,
                          "total_tokens": 0}}

    if req.stream:
        def sync_stream():
            try:
                yield _sse_event("response.created", {
                    "type": "response.created",
                    "response": envelope("in_progress")})
                yield _sse_event("response.output_item.added", {
                    "type": "response.output_item.added", "output_index": 0,
                    "item": {"id": mid, "type": "message", "role": "assistant",
                             "status": "in_progress", "content": []}})
                yield _sse_event("response.content_part.added", {
                    "type": "response.content_part.added", "item_id": mid,
                    "output_index": 0, "content_index": 0,
                    "part": {"type": "output_text", "text": "",
                             "annotations": []}})
                full = ""
                for chunk in safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id):
                    full += chunk
                    yield _sse_event("response.output_text.delta", {
                        "type": "response.output_text.delta", "item_id": mid,
                        "output_index": 0, "content_index": 0, "delta": chunk})
                yield _sse_event("response.output_text.done", {
                    "type": "response.output_text.done", "item_id": mid,
                    "output_index": 0, "content_index": 0, "text": full})
                yield _sse_event("response.output_item.done", {
                    "type": "response.output_item.done", "output_index": 0,
                    "item": {"id": mid, "type": "message", "role": "assistant",
                             "status": "completed",
                             "content": [{"type": "output_text", "text": full,
                                          "annotations": []}]}})
                yield _sse_event("response.completed", {
                    "type": "response.completed",
                    "response": envelope("completed", full)})
            except Exception as exc:  # noqa: BLE001
                log.warning("responses 流式失败: %s", exc)
                yield _sse_event("response.failed", {
                    "type": "response.failed",
                    "response": envelope("failed")})
        return StreamingResponse(sync_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    def run() -> str:
        return "".join(safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id))

    try:
        text = await asyncio.to_thread(run)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc))
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc))

    return envelope("completed", text)


@app.get("/v1/media/{name}")
def get_media(name: str):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    p = os.path.join(CFG.media_dir, name)
    if not os.path.isfile(p):
        raise HTTPException(404, "文件不存在")
    return FileResponse(p)


# ------------------------- 管理：总览 -------------------------
@app.get("/admin/status")
def admin_status(_=Depends(auth)):
    base = _public_base()
    return {
        "accounts": _public_accounts(),
        "stats": store.stats(),
        "tasks": store.list_tasks(20),
        "media_count": len(os.listdir(CFG.media_dir))
        if os.path.isdir(CFG.media_dir) else 0,
        "browser_running": bool(engine.proc and engine.proc.poll() is None),
        "essential_cookies": list(ESSENTIAL_COOKIES),
        "base_url": f"{base}/v1" if base else "",
        "config": {"site": CFG.site_url, "cdp_port": CFG.cdp_port,
                   "image_timeout": CFG.image_timeout,
                   "video_timeout": CFG.video_timeout,
                   "video_queue": CFG.video_queue,
                   "host": CFG.host, "port": CFG.port,
                   "public_base": base},
    }


# ------------------------- 管理：账号池 -------------------------
def _public_accounts():
    accounts = store.list_accounts()
    for account in accounts:
        quota = account.get("quota")
        if isinstance(quota, dict) and quota.get("checked_at"):
            quota.update(reset_metadata(quota.get("weekly_reset"), quota["checked_at"]))
    return accounts


@app.get("/admin/accounts")
def list_accounts(_=Depends(auth)):
    return {"accounts": _public_accounts(), "stats": store.stats()}


@app.post("/admin/accounts")
def add_account(req: AccountRequest, _=Depends(auth)):
    supplied_proxy = "proxy" in req.model_fields_set
    route = req.proxy.configuration() if req.proxy else None
    sources = list(parse_batch(req.batch or ""))
    cookies = dict(req.cookies)
    if req.cookie_header:
        cookies.update(parse_cookie_text(req.cookie_header))
    if cookies:
        sources.append((req.label, cookies))
    if not sources:
        raise HTTPException(400, "未解析到任何 cookie，请检查格式")
    added, unique_ids = [], set()
    for label, values in sources:
        try:
            # Exact same session dedupes locally. Verified OTP identity dedupes across sessions.
            # Caller-supplied labels are never trusted as account identity.
            with ACCOUNT_POOL.cond:
                if any(a.get("cookies", {}).get("hatch_sess") == values.get("hatch_sess")
                       and a["id"] in ACCOUNT_POOL.busy_accounts for a in store.accounts
                       if values.get("hatch_sess")):
                    raise HTTPException(409, "该账号正在执行任务，请空闲后重新导入")
                kwargs = {"proxy": route} if supplied_proxy else {}
                account = store.add_account(values, label, cookies_exp=req.expires or None, **kwargs)
            if account["id"] not in unique_ids:
                unique_ids.add(account["id"])
                added.append({"id": account["id"], "label": account["label"],
                              "cookie_count": len(account["cookies"]),
                              "expires_at": account.get("expires_at")})
                journal.emit("account.imported", account_id=account["id"], stage="import")
        except AccountIdentityConflict as exc:
            raise HTTPException(409, safe_message(str(exc))) from None
    missing = [name for name in ESSENTIAL_COOKIES if not any(values.get(name) for _, values in sources)]
    _wake_maintenance()
    return {"added": added, "count": len(added), "input_count": len(sources),
            "essential_missing": missing,
            "warning": f"缺少核心 cookie：{', '.join(missing)}，该账号可能无法生成" if missing else ""}


# One short-lived receipt per account; credentials are represented only by a hash.
_PROXY_TEST_RECEIPTS: dict[str, dict] = {}
_PROXY_TEST_RECEIPT_TTL = 300


def _proxy_test_identity(proxy: dict, cookies: dict | None) -> str:
    value = json.dumps({"proxy": proxy, "cookies": cookies or {}},
                       sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


@app.patch("/admin/accounts/{aid}")
def patch_account(aid: str, req: AccountPatch, _=Depends(auth)):
    with ACCOUNT_POOL.cond:
        existing = store.get_account(aid)
        if not existing:
            raise HTTPException(404, "账号不存在")
        changes = {"label": req.label, "enabled": req.enabled}
        if "proxy" in req.model_fields_set:
            if aid in ACCOUNT_POOL.busy_accounts:
                raise HTTPException(409, "账号正在执行任务或检测，请稍后再修改代理")
            proxy = req.proxy.configuration(existing.get("proxy")) if req.proxy else {}
            if proxy != (existing.get("proxy") or {}):
                changes.update(proxy=proxy, synced_at=0, last_keepalive=0,
                               checked_at=0, proxy_status={}, ok=None,
                               note="代理已修改，请重新测试")
                receipt = _PROXY_TEST_RECEIPTS.get(aid)
                if (proxy and receipt and 0 <= time.monotonic() - receipt["at"] <= _PROXY_TEST_RECEIPT_TTL
                        and receipt["identity"] == _proxy_test_identity(proxy, existing.get("cookies"))):
                    changes["proxy_status"] = dict(receipt["result"])
                    changes["note"] = "代理配置已保存，账号浏览器状态待测试"
                engine._last_http_renew.pop(aid, None)
            _PROXY_TEST_RECEIPTS.pop(aid, None)
        acc = store.update_account(aid, **changes)
        return store.public_account(acc)


@app.post("/admin/accounts/{aid}/proxy/test")
def test_account_proxy(aid: str, req: ProxyTestRequest | None = None, _=Depends(auth)):
    import ipaddress
    import requests
    with _account_operation(aid) as acc:
        proxy = req.proxy.configuration(acc.get("proxy")) if req is not None else _account_proxy(acc)
        if not proxy:
            raise HTTPException(400, "请输入代理链接或先保存该账号的代理")
        identity = _proxy_test_identity(proxy, acc.get("cookies"))
        result = {"ok": False, "proxy_configured": True, "proxy_reachable": False,
                  "exit_ip": None, "session_ok": None, "workspace_status": None}
        try:
            # Fixed HTTPS endpoint receives no Muse cookies or proxy credentials.
            with request_proxy_context(proxy) as proxy_options, requests.Session() as session:
                session.trust_env = False
                response = session.get("https://api.ipify.org?format=json",
                                       timeout=(5, 10), allow_redirects=False,
                                       **proxy_options)
            if response.status_code != 200:
                raise MuseProxyError("代理出口检测未成功，请检查代理连接或认证")
            result["exit_ip"] = str(ipaddress.ip_address(response.json()["ip"]))
            result["proxy_reachable"] = True
            checked = engine.renew_session_http(acc.get("cookies") or {}, acc.get("cookies_exp"),
                                                wake_vm=False, proxy=proxy)
            result.update(ok=True, session_ok=True, workspace_status=checked.get("status"),
                          message="代理出口与 Muse 会话检查通过；生成能力请使用账号测试和实际任务验证")
        except MuseAuthError:
            result.update(session_ok=False, message="代理可连接，但 Muse 会话认证失败，请重新导入 Cookie")
        except MuseProxyError as exc:
            result["message"] = str(exc)
        except MuseGenerationError as exc:
            result.update(session_ok=False, message=str(exc))
        except (requests.RequestException, ValueError, KeyError, TypeError):
            result["message"] = "代理出口检测失败，请检查代理地址、认证和网络"
        recorded = {**result, "checked_at": int(time.time())}
        tested_saved = False
        with ACCOUNT_POOL.cond:
            current = store.get_account(aid)
            if current and identity == _proxy_test_identity(proxy, current.get("cookies")):
                # Unsaved routes stay only in memory; saving the exact configuration may adopt this proof.
                _PROXY_TEST_RECEIPTS[aid] = {"identity": identity, "at": time.monotonic(),
                                           "result": dict(recorded)}
                tested_saved = proxy == _account_proxy(current)
                if tested_saved:
                    store.update_account(aid, proxy_status=dict(recorded))
        result["tested_saved_proxy"] = tested_saved
        return result


@app.delete("/admin/accounts/{aid}")
def del_account(aid: str, _=Depends(auth)):
    ok = store.delete_account(aid)
    if not ok:
        raise HTTPException(404, "账号不存在")
    with ACCOUNT_POOL.cond:
        _PROXY_TEST_RECEIPTS.pop(aid, None)
    return {"deleted": True, "id": aid}


@app.post("/admin/accounts/{aid}/test")
async def test_account(aid: str, _=Depends(auth)):
    """真实打开 muse.ai 验证该账号 cookie 是否仍可登录。"""
    acc = store.get_account(aid)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if not acc.get("cookies"):
        raise HTTPException(400, "该账号没有 cookie")

    def _probe():
        with GEN_LOCK, _account_operation(aid) as acc:
            try:
                engine.start()
                engine.refresh(acc["cookies"], acc.get("cookies_exp"), account_id=aid)
                synced = _sync_cookies(aid)
                quota = None
                quota_error = None
                try:  # Session validity and quota readability are independent results.
                    quota = engine.quota(acc["cookies"],
                                         acc.get("cookies_exp"), account_id=aid)
                    if quota.get("found"):
                        quota["checked_at"] = int(time.time())
                        store.update_account(aid, quota=quota)
                    else:
                        quota_error = "额度面板未读到有效数据；旧缓存未覆盖"
                        quota = None
                except Exception as exc:  # noqa: BLE001
                    quota_error = f"{type(exc).__name__}；额度缓存未更新"
                store.mark(aid, True, "会话有效" + ("；额度读取失败" if quota_error else ""))
                return {"ok": True, "message": "会话有效，可正常生成",
                        "synced": synced, "quota": quota,
                        "quota_error": quota_error}
            except MuseAuthError as exc:
                store.mark(aid, False, str(exc)[:200])
                return {"ok": False, "message": str(exc)[:200]}
            except Exception as exc:  # noqa: BLE001
                store.touch_keepalive(aid, None, f"测试未确认（保留账号状态）: {str(exc)[:200]}")
                return {"ok": False, "message": str(exc)[:200]}

    res = await asyncio.to_thread(_probe)
    if not res["ok"]:
        return JSONResponse(res, status_code=200)
    return res


@app.post("/admin/accounts/{aid}/relogin")
async def relogin_account(aid: str, _=Depends(auth)):
    return await test_account(aid, _)


@app.post("/admin/accounts/{aid}/cookies")
def update_cookies(aid: str, payload: dict = Body(...), _=Depends(auth)):
    """更新某个账号的 cookie（用于会话过期后补新 cookie）。"""
    cookies = dict(payload.get("cookies") or {})
    if payload.get("cookie_header"):
        cookies.update(parse_cookie_text(payload["cookie_header"]))
    if not cookies:
        raise HTTPException(400, "未解析到 cookie")
    exp = {k: int(v) for k, v in (payload.get("expires") or {}).items()
           if _pos(v)}
    # 补入的是「全新会话」的 cookie，有效期估算锚点必须重置到当下，
    # 否则会沿用旧会话的锚点，把剩余天数算少。
    acc = store.update_account(aid, cookies=cookies, ok=None,
                               note="已更新 cookie",
                               expiry_anchor=int(time.time()),
                               cookies_exp=exp or None)
    if not acc:
        raise HTTPException(404, "账号不存在")
    return {"ok": True, "id": aid, "cookie_count": len(cookies),
            "expires_at": acc.get("expires_at")}


@app.post("/admin/relogin")
def relogin(_=Depends(auth)):
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "没有可用账号")
    try:
        with GEN_LOCK, _account_operation(acc["id"]) as selected:
            engine.start()
            engine.refresh(selected["cookies"], selected.get("cookies_exp"), account_id=selected["id"])
            return {"ok": True, "account": selected["id"]}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


# ------------------------- 管理：额度 -------------------------
@app.post("/admin/accounts/{aid}/quota")
async def query_quota(aid: str, _=Depends(auth)):
    """打开 muse.ai 的 Settings 面板读该账号的额度（实时），并缓存到账号记录。

    返回例：{"plan":"Free plan","weekly_reset":"Sep 30",
             "weekly_used_pct":1,"extra_left":"2B tokens left",
             "extra_used_pct":0,"extra_expires":"never"}
    """
    acc = store.get_account(aid)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if not acc.get("cookies"):
        raise HTTPException(400, "该账号没有 cookie")

    try:
        return await asyncio.to_thread(_query_quota_sync, aid)
    except HTTPException:
        raise
    except MuseAuthError as exc:
        store.mark(aid, False, str(exc)[:200])
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"额度查询失败: {exc}") from exc


@app.post("/admin/quota")
async def query_any_quota(_=Depends(auth)):
    """用当前最久未用的可用账号查一次额度（同池账号共享同一 muse.ai 计划的
    通常只有一人使用时够用；多账号时建议按账号查）。"""
    acc = store.pick_account(rotate=True, record_use=False)
    if not acc:
        raise HTTPException(400, "没有可用账号")
    return await query_quota(acc["id"], _)


# ------------------------- 管理：接入信息 / API Key -------------------------
def _public_base() -> str:
    return (CFG.public_base or "").rstrip("/")


@app.get("/admin/apikey")
def get_apikey(_=Depends(auth)):
    base = _public_base()
    base_url = f"{base}/v1" if base else ""
    return {"api_key": CFG.api_key, "base_url": base_url,
            "models_url": f"{base}/v1/models" if base else "",
            "media_url": f"{base}/v1/media/{{name}}" if base else "/v1/media/{name}"}


@app.post("/admin/apikey/rotate")
def rotate_apikey(_=Depends(auth)):
    """生成新的 API Key，写入 .env 并立即生效（不用重启）。"""
    import secrets
    new_key = "m2a_" + secrets.token_hex(24)
    old = CFG.api_key
    CFG.api_key = new_key
    _persist_env("MUSE2API_KEY", new_key)
    return {"ok": True, "api_key": new_key, "previous": old,
            "message": "已生成新 Key 并立即生效；旧 Key 已失效，请更新下游项目"}


def _persist_env(key: str, value: str):
    """把配置写回 .env（保留其它行，原子替换）。"""
    path = os.path.join(CFG.base_dir, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    found = False
    for i, ln in enumerate(lines):
        if ln.strip().startswith(key + "="):
            lines[i] = f"{key}={value}"
            found = True
    if not found:
        lines.append(f"{key}={value}")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).strip() + "\n")
    os.replace(tmp, path)


# ------------------------- 管理：Cookie 获取 -------------------------
@app.get("/admin/cookie-helper")
def cookie_helper(download: int = 0):
    """返回本机取 cookie 的助手脚本（进阶方式，需要装 Python）。"""
    p = os.path.join(BASE_DIR, "tools", "get_muse_cookie.py")
    if not os.path.isfile(p):
        raise HTTPException(404, "助手脚本缺失")
    headers = {}
    if download:
        headers["Content-Disposition"] = 'attachment; filename="get_muse_cookie.py"'
    return FileResponse(p, media_type="text/x-python", headers=headers)


@app.get("/admin/extension")
def extension_zip():
    """把浏览器扩展打包成 zip 返回（推荐方式，零命令行）。

    用户下载后解压 → chrome://extensions 开发者模式加载 → 点一下就导入 cookie。
    """
    src = os.path.join(BASE_DIR, "extension")
    if not os.path.isdir(src):
        raise HTTPException(404, "扩展目录缺失")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, names in os.walk(src):
            dirs.sort()
            for name in sorted(names):
                p = os.path.join(root, name)
                relative_path = os.path.relpath(p, src).replace(os.sep, "/")
                z.write(p, f"muse2api-extension/{relative_path}")
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition":
                 'attachment; filename="muse2api-extension.zip"',
                 "Cache-Control": "no-store"})


@app.get("/admin/extension/files")
def extension_files():
    """列出扩展目录内容（前端展示用）。"""
    src = os.path.join(BASE_DIR, "extension")
    if not os.path.isdir(src):
        raise HTTPException(404, "扩展目录缺失")
    files = []
    for root, dirs, names in os.walk(src):
        dirs.sort()
        for name in sorted(names):
            p = os.path.join(root, name)
            files.append(os.path.relpath(p, src).replace(os.sep, "/"))
    return {"files": sorted(files)}


# ------------------------- 管理：任务 / 媒体 -------------------------
@app.get("/admin/tasks")
def admin_tasks(limit: int = 50, _=Depends(auth)):
    if not 1 <= limit <= 200:
        raise HTTPException(400, "任务分页上限必须为1至200")
    accounts = store.list_accounts()
    return {"tasks": [project_task(task, accounts, CFG.media_dir, media_url) for task in store.list_tasks(limit)]}


@app.get("/admin/tasks/{tid}")
def admin_task_detail(tid: str, _=Depends(auth)):
    task = store.get_task(tid)
    if not task:
        raise HTTPException(404, "任务不存在")
    return {"task": project_task(task, store.list_accounts(), CFG.media_dir, media_url)}


@app.delete("/admin/tasks/{tid}")
def del_task(tid: str, _=Depends(auth)):
    if not store.delete_task(tid):
        raise HTTPException(404, "任务不存在")
    return {"deleted": True}


@app.post("/admin/tasks/clear")
def clear_tasks(payload: dict = Body(default={}), _=Depends(auth)):
    return {"removed": store.clear_tasks(int(payload.get("keep") or 0))}


@app.get("/admin/media")
def admin_media(_=Depends(auth)):
    d = CFG.media_dir
    items = []
    if os.path.isdir(d):
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            ext = os.path.splitext(name)[1].lower()
            items.append({"name": name, "url": media_url(name), "bytes": os.path.getsize(p),
                          "mtime": int(os.path.getmtime(p)),
                          "kind": "video" if ext in (".mp4", ".webm", ".mov") else "image"})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"media": items, "count": len(items)}


@app.post("/admin/media/delete")
def delete_media(payload: dict = Body(default={}), _=Depends(auth)):
    names = payload.get("names", [])
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list):
        raise HTTPException(400, "names 必须是文件名数组")
    d = CFG.media_dir
    removed = 0
    errors = []
    for name in names:
        if not isinstance(name, str) or "/" in name or "\\" in name or ".." in name:
            errors.append(f"非法文件名: {name}")
            continue
        p = os.path.join(d, name)
        if os.path.isfile(p):
            try:
                os.remove(p)
                removed += 1
            except OSError as e:
                errors.append(f"{name}: {str(e)}")
    return {"removed": removed, "errors": errors}


@app.delete("/admin/media/{name}")
def delete_single_media(name: str, _=Depends(auth)):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    p = os.path.join(CFG.media_dir, name)
    if not os.path.isfile(p):
        raise HTTPException(404, "文件不存在")
    try:
        os.remove(p)
        return {"status": "ok", "deleted": name}
    except OSError as e:
        raise HTTPException(500, f"删除失败: {e}")


# ------------------------- 前端页面 -------------------------
def _admin_html() -> str:
    p = os.path.join(BASE_DIR, "admin.html")
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ("<!doctype html><meta charset=utf-8><body style='background:#0b0f19;"
                "color:#e6e8ee;font-family:system-ui;padding:40px'>"
                "<h2>muse2api</h2><p>管理页面文件 admin.html 缺失。</p>"
                "<p>接口可用：<code>/v1/images/generations</code>、"
                "<code>/v1/videos</code></p></body>")


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(_admin_html(), headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return HTMLResponse(_admin_html(), headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})



# ------------------------- Account maintenance (manual + scheduled) -------------------------
KEEPALIVE_LOCK = asyncio.Lock()
KEEPALIVE_STATE = {"running": False, "last_run": None, "last_result": None,
                   "next_run": None, "last_keepalive_at": None, "last_quota_at": None}
_MAINTENANCE_WAKEUP = asyncio.Event()
_MAINTENANCE_TASK = None
_MAINTENANCE_MANUAL_TASK = None


def _wake_maintenance():
    # Called from sync routes/login worker threads as well as the event loop.
    loop = getattr(_MAINTENANCE_TASK, "get_loop", lambda: None)()
    if loop and loop.is_running():
        loop.call_soon_threadsafe(_MAINTENANCE_WAKEUP.set)


def _persist_session(aid: str, result: dict, note: str, *, keepalive: bool = True) -> dict:
    """Only confirmed assigned sessions refresh health and real cookie expiry."""
    if not result.get("ok"):
        raise MuseGenerationError("会话尚未确认，保留原状态与有效期")
    now = int(time.time())
    fields = {"cookies": result["cookies"], "cookies_exp": result["cookies_exp"],
              "verified_session": True, "synced_at": now, "session_checked_at": now,
              "vm_state": result.get("vm_state"), "vm_checked_at": now}
    if keepalive:
        fields.update(wake_ok=bool(result.get("wake_ok")), keepalive_checked_at=now, keepalive_error="")
    acc = store.update_account(aid, **fields)
    if keepalive:
        store.touch_keepalive(aid, True, note)
    else:
        store.mark(aid, True, note)
    if result.get("identity"):
        with ACCOUNT_POOL.cond:
            if not ACCOUNT_POOL.busy_accounts - {aid, (acc or {}).get("id")}:
                acc = store.bind_identity(aid, result["identity"])
                if aid in ACCOUNT_POOL.busy_accounts:
                    ACCOUNT_POOL.busy_accounts.add(acc["id"])
    return acc or store.get_account(aid) or {}


def _quota_in_lease(aid: str, acc: dict) -> dict:
    """The manual button and scheduler share the existing Settings-menu JS."""
    store.update_account(aid, quota_checked_at=int(time.time()))
    journal.emit("quota.started", account_id=aid, stage="quota")
    try:
        engine.start()
        try:
            quota = engine.quota(acc["cookies"], acc.get("cookies_exp"), account_id=aid)
        except MuseAuthError:
            raise
        except MuseGenerationError:
            # UI hydration/menu races are safe to retry: this only reads quota, never generates media.
            journal.emit("quota.retry", level="warning", account_id=aid, stage="quota",
                         reason="retry_once_in_fresh_context")
            engine.close_current_page()
            quota = engine.quota(acc["cookies"], acc.get("cookies_exp"), account_id=aid)
        if not quota.get("found"):
            raise MuseGenerationError("额度面板未读到有效数据；旧缓存未覆盖")
        quota["checked_at"] = int(time.time())
        quota.update(reset_metadata(quota.get("weekly_reset"), quota["checked_at"]))
        store.update_account(aid, quota=quota, quota_error="")
        live = getattr(engine, "last_quota_cookies", None) or {}
        if live:
            current = store.get_account(aid) or acc
            values = {k: v["value"] for k, v in live.items() if v.get("value")}
            exps = dict(current.get("cookies_exp") or {})
            exps.update({k: int(v["expires"]) for k, v in live.items() if _pos(v.get("expires"))})
            store.update_account(aid, cookies={**current["cookies"], **values},
                                 cookies_exp=exps, synced_at=int(time.time()), verified_session=True)
        journal.emit("quota.completed", account_id=aid, stage="quota",
                     weekly_used_pct=quota.get("weekly_used_pct"))
        return quota
    except Exception as exc:
        reason = safe_message(str(exc))
        store.update_account(aid, quota_error=reason, quota_checked_at=int(time.time()))
        journal.error("quota.failed", exc, account_id=aid, stage="quota", reason=reason)
        raise


def _query_quota_sync(aid: str, background: bool = False) -> dict:
    # Browser slot before account lease, matching generation. Background work never queues ahead of it.
    acquired = GEN_LOCK.acquire(blocking=False) if background else GEN_LOCK.acquire(timeout=30)
    if not acquired:
        raise HTTPException(409, "浏览器繁忙，额度查询将在下轮重试")
    try:
        with _account_operation(aid) as acc, journal.context(account_id=aid):
            if not acc.get("cookies"):
                raise HTTPException(400, "该账号没有 cookie")
            result = engine.renew_session_http(
                acc["cookies"], acc.get("cookies_exp"), wake_vm=False,
                **({"proxy": _account_proxy(acc)} if _account_proxy(acc) else {}))
            acc = _persist_session(aid, result, "会话有效 · 额度刷新", keepalive=False)
            return _quota_in_lease(aid, acc)
    except MuseAuthError as exc:
        store.touch_keepalive(aid, False, "额度查询认证失败: " + safe_message(str(exc)))
        journal.error("quota.auth_failed", exc, account_id=aid, stage="session",
                      reason=safe_message(str(exc)))
        raise
    finally:
        GEN_LOCK.release()


def _probe_account_sync(aid: str, check_quota: bool = False) -> dict:
    """Refresh health/real expiry, without counting maintenance as business use."""
    try:
        with _account_operation(aid) as acc, journal.context(account_id=aid):
            if not acc.get("cookies"):
                return {"ok": False, "id": aid, "error": "账号无有效 cookie"}
            store.update_account(aid, keepalive_checked_at=int(time.time()))
            journal.emit("keepalive.started", stage="session")
            res = engine.renew_session_http(
                acc["cookies"], acc.get("cookies_exp"), wake_vm=True,
                **({"proxy": _account_proxy(acc)} if _account_proxy(acc) else {}))
            vm_state = res.get("vm_state")
            updated = _persist_session(aid, res, f"会话有效 · 自动保活 (VM: {vm_state or 'unknown'})")
            quota, quota_error = updated.get("quota"), None
            if check_quota and GEN_LOCK.acquire(blocking=False):
                try:
                    quota = _quota_in_lease(aid, updated)
                except Exception as exc:
                    quota_error = safe_message(str(exc))
                finally:
                    GEN_LOCK.release()
            journal.emit("keepalive.completed", stage="session", vm_state=vm_state,
                         wake_ok=res.get("wake_ok"), expires_at=updated.get("expires_at"))
            return {"ok": True, "id": updated.get("id", aid), "label": updated.get("label", aid),
                    "vm_id": res.get("vm_id"), "vm_state": vm_state, "wake_ok": res.get("wake_ok"),
                    "expires_at": updated.get("expires_at"), "quota": quota, "quota_error": quota_error}
    except HTTPException as exc:
        return {"ok": False, "id": aid, "busy": exc.status_code == 409,
                "error": safe_message(str(exc.detail))}
    except MuseAuthError as exc:
        reason = safe_message(str(exc))
        store.touch_keepalive(aid, False, "保活认证失败: " + reason)
        store.update_account(aid, keepalive_checked_at=int(time.time()), keepalive_error=reason)
        journal.error("keepalive.auth_failed", exc, account_id=aid, stage="session", reason=reason)
        return {"ok": False, "id": aid, "error": reason}
    except Exception as exc:
        reason = safe_message(str(exc))
        store.touch_keepalive(aid, None, "保活未确认（保留账号状态）: " + reason)
        store.update_account(aid, keepalive_checked_at=int(time.time()), keepalive_error=reason)
        journal.error("keepalive.failed", exc, account_id=aid, stage="session", reason=reason)
        return {"ok": False, "id": aid, "error": reason}


async def _run_maintenance(force: bool, with_quota: bool, refresh_all: bool = False) -> dict:
    async with KEEPALIVE_LOCK:
        settings = maintenance_settings.get()
        now = int(time.time())
        KEEPALIVE_STATE.update(running=True, last_run=now)
        results, skipped = [], []
        try:
            for account in store.list_accounts():
                include_disabled = settings.include_disabled
                if not include_disabled and not account.get("enabled", True):
                    continue
                aid = account["id"]
                keep_due = (settings.keepalive_enabled or not with_quota or refresh_all) and operation_due(
                    account, "keepalive", settings.keepalive_interval_minutes, now, force)
                quota_due = with_quota and (settings.quota_enabled or refresh_all) and operation_due(
                    account, "quota", settings.quota_interval_minutes, now, force)
                if not keep_due and not quota_due:
                    skipped.append({"id": aid, "reason": "未到维护周期"})
                    continue
                if keep_due:
                    result = await asyncio.to_thread(_probe_account_sync, aid, False)
                    results.append({"operation": "keepalive", **result})
                    if result.get("ok"):
                        KEEPALIVE_STATE["last_keepalive_at"] = int(time.time())
                    elif not result.get("busy"):
                        continue
                if quota_due:
                    try:
                        quota = await asyncio.to_thread(_query_quota_sync, aid, True)
                        results.append({"operation": "quota", "id": aid, "ok": True,
                                        "checked_at": quota["checked_at"]})
                        KEEPALIVE_STATE["last_quota_at"] = quota["checked_at"]
                    except HTTPException as exc:
                        skipped.append({"id": aid, "operation": "quota", "busy": exc.status_code == 409,
                                        "error": safe_message(str(exc.detail))})
                    except Exception as exc:
                        reason = safe_message(str(exc))
                        store.update_account(aid, quota_checked_at=int(time.time()), quota_error=reason)
                        journal.error("maintenance.quota_failed", exc, account_id=aid,
                                      stage="quota", reason=reason)
                        results.append({"operation": "quota", "id": aid, "ok": False, "error": reason})
            summary = {"checked_at": now, "refreshed_count": len(results), "refreshed": results,
                       "skipped_count": len(skipped), "skipped": skipped}
            fields = {"last_run": now, "last_check": summary, "next_run": int(time.time()) + 30}
            if force or results or any(item.get("busy") for item in skipped):
                fields["last_result"] = summary
            KEEPALIVE_STATE.update(**fields)
            return summary
        finally:
            KEEPALIVE_STATE["running"] = False


async def run_keepalive_all(force: bool = False) -> dict:
    return await _run_maintenance(force, with_quota=False)


async def run_account_maintenance(force: bool = False, refresh_all: bool = False) -> dict:
    return await _run_maintenance(force, with_quota=True, refresh_all=refresh_all)


def _warmup_browser_sync():
    """后台静默预热浏览器与首个可用账号的 WebSocket 隧道，使重启后首条请求也秒回。"""
    if getattr(engine, "current_acc_id", None) and engine.page is not None:
        return
    if not GEN_LOCK.acquire(blocking=False):
        return
    acc = None
    try:
        acc = ACCOUNT_POOL.acquire(rotate=False, timeout=0.1, record_use=False)
        log.info("【浏览器预热】正在后台预热账号 %s (%s) 的热备标签页...", acc.get("label"), acc["id"])
        _bind_account_proxy(acc)
        refreshed = _renew_and_persist(acc["id"], wake_vm=True, force=False) or acc
        engine.start()
        engine.ensure_page(refreshed["cookies"], refreshed.get("cookies_exp"), account_id=acc["id"])
        log.info("【浏览器预热】账号 %s (%s) 热备标签页与 WebSocket 已就绪", acc.get("label"), acc["id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("【浏览器预热】预热异常: %s", exc)
    finally:
        # Warmup runs in a worker thread; do not retain its account reservation or page.
        engine.close_current_page()
        _clear_account_proxy()
        if acc:
            ACCOUNT_POOL.release(acc["id"])
        GEN_LOCK.release()


async def _keepalive_loop():
    """Short scheduler tick, persisted per-operation cadence, serial account work."""
    log.info("账号维护后台已启动：按设置自动刷新会话/额度，忙碌账号跳过")
    await asyncio.sleep(1)
    while True:
        try:
            await run_account_maintenance(force=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            journal.error("maintenance.loop_failed", exc, stage="maintenance",
                          reason=safe_message(str(exc)))
        KEEPALIVE_STATE["next_run"] = int(time.time()) + 30
        try:
            await asyncio.wait_for(_MAINTENANCE_WAKEUP.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
        finally:
            _MAINTENANCE_WAKEUP.clear()


@app.post("/admin/accounts/keepalive")
async def trigger_keepalive_all(force: bool = True, _=Depends(auth)):
    """管理员手动触发一次全账号保活续期。"""
    if KEEPALIVE_STATE["running"]:
        return {"status": "busy", "message": "保活任务正在执行中，请稍候"}
    return await run_keepalive_all(force=force)


@app.post("/admin/accounts/{aid}/keepalive")
async def trigger_keepalive_single(aid: str, _=Depends(auth)):
    """手动针对单个账号执行保活续期。"""
    return await asyncio.to_thread(_probe_account_sync, aid, False)


@app.get("/admin/keepalive/status")
def get_keepalive_status(_=Depends(auth)):
    """获取保活守护协程状态。"""
    return {**copy.deepcopy(KEEPALIVE_STATE), "settings": maintenance_settings.get().model_dump()}


class MaintenancePatch(BaseModel):
    model_config = {"extra": "forbid", "strict": True}
    keepalive_enabled: bool | None = None
    keepalive_interval_minutes: int | None = Field(default=None, ge=1, le=10080)
    quota_enabled: bool | None = None
    quota_interval_minutes: int | None = Field(default=None, ge=1, le=10080)
    include_disabled: bool | None = None


@app.get("/admin/maintenance/settings")
def get_maintenance_settings(_=Depends(auth)):
    return {"settings": maintenance_settings.get().model_dump(),
            "state": copy.deepcopy(KEEPALIVE_STATE),
            "settings_load_error": maintenance_settings.load_error}


@app.put("/admin/maintenance/settings")
def update_maintenance_settings(req: MaintenancePatch, _=Depends(auth)):
    changes = req.model_dump(exclude_none=True, exclude_unset=True)
    value = maintenance_settings.update(changes)
    _wake_maintenance()
    journal.emit("maintenance.settings_updated", stage="maintenance", settings=value.model_dump())
    return {"settings": value.model_dump(), "state": copy.deepcopy(KEEPALIVE_STATE)}


@app.post("/admin/maintenance/run", status_code=202)
async def trigger_account_maintenance(_=Depends(auth)):
    global _MAINTENANCE_MANUAL_TASK
    if KEEPALIVE_STATE["running"] or (_MAINTENANCE_MANUAL_TASK and not _MAINTENANCE_MANUAL_TASK.done()):
        return {"status": "busy", "message": "账号维护正在执行"}
    _MAINTENANCE_MANUAL_TASK = asyncio.create_task(run_account_maintenance(force=True, refresh_all=True))
    return {"status": "queued"}


@app.get("/admin/logs")
def get_diagnostic_logs(task_id: str | None = None, account_id: str | None = None,
                        level: str | None = None, limit: int = 100,
                        offset: int = 0, _=Depends(auth)):
    if limit < 1 or limit > 200 or offset < 0 or offset > 100000:
        raise HTTPException(400, "日志分页参数无效")
    if level:
        level = level.lower()
    if level and level not in ("debug", "info", "warning", "error", "critical"):
        raise HTTPException(400, "日志级别无效")
    page = journal.read(offset=offset, limit=limit, task_id=task_id,
                        account_id=account_id, level=level)
    entries = []
    for raw in page["items"]:
        entry = dict(raw)
        aid = entry.get("account_id")
        source = "event" if aid else None
        if not aid and entry.get("task_id"):
            task = store.get_task(entry["task_id"]) or {}
            attempts = task.get("attempts") or []
            mapped = next((a for a in attempts if a.get("attempt") == entry.get("attempt")), None)
            ids = {a.get("account_id") for a in attempts if a.get("account_id")}
            if mapped:
                aid = mapped.get("account_id")
            elif len(ids) <= 1:
                aid = task.get("account_id") or task.get("account")
            if aid:
                source = "task_history"
        account = store.get_account(aid) if aid else None
        entry["account_id"] = aid
        entry["account_label"] = safe_message(entry.get("account_label") or
                                              (account or {}).get("label") or aid or "")
        entry["account_mapping_source"] = source or "unassigned"
        entries.append(entry)
    return {**{k: v for k, v in page.items() if k != "items"}, "entries": entries,
            "health": journal.health()}


@app.get("/admin/tasks/{task_id}/logs")
def get_task_logs(task_id: str, limit: int = 100, offset: int = 0,
                  account_id: str | None = None, level: str | None = None, _=Depends(auth)):
    # Deleted tasks can still have retained diagnostic records.
    return get_diagnostic_logs(task_id=task_id, account_id=account_id, level=level,
                               limit=limit, offset=offset, _=_)


# ------------------------- Web-managed native OTP login -------------------------
@contextmanager
def _login_browser_slot():
    if not GEN_LOCK.acquire(timeout=30):
        raise TimeoutError("浏览器繁忙，请稍后重试批量登录")
    try:
        yield
    finally:
        GEN_LOCK.release()


def _save_logged_in_account(cookies, expires, identity, *, label="", proxy=None, proxy_id=None):
    # Login worker already verified this new isolated session upstream.
    deadline = time.monotonic() + 30
    with ACCOUNT_POOL.cond:
        while True:
            keys = identity_keys(identity)
            conflicting = {a["id"] for a in store.accounts
                           if (cookies.get("hatch_sess") and a.get("cookies", {}).get("hatch_sess") == cookies["hatch_sess"])
                           or keys & identity_keys(a.get("identity"))}
            if not conflicting & ACCOUNT_POOL.busy_accounts:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MuseGenerationError("该账号正在执行任务，暂不替换登录会话；请空闲时重试")
            ACCOUNT_POOL.cond.wait(timeout=min(remaining, 0.5))
        kwargs = {"identity": identity}
        if proxy is not None:
            kwargs["proxy"] = proxy
        account = store.add_account(cookies, label, cookies_exp=expires, **kwargs)
        store.update_account(account["id"], ok=True, note="网页登录认证成功", synced_at=int(time.time()))
    journal.emit("login.account_saved", account_id=account["id"], stage="login")
    _wake_maintenance()
    return account["id"]


def _login_event(event: dict):
    fields = {k: v for k, v in event.items() if k not in ("event", "time")}
    journal.emit(event.get("event", "login.event"), **fields)


login_service = AutoLoginService(engine, _save_logged_in_account,
                                 slot=_login_browser_slot, on_event=_login_event)


class LoginAccountInput(BaseModel):
    model_config = {"extra": "forbid"}
    account: str = Field(min_length=3, max_length=254)
    otp_url: str | None = Field(default=None, max_length=2048)
    label: str = Field(default="", max_length=80)


class LoginBatchInput(BaseModel):
    model_config = {"extra": "forbid"}
    accounts: list[LoginAccountInput] = Field(min_length=1, max_length=20)


class LoginOtpInput(BaseModel):
    model_config = {"extra": "forbid"}
    index: int = Field(ge=0, le=19)
    code: SecretStr


def _login_call(method, *args):
    try:
        return method(*args)
    except LoginNotFoundError:
        raise HTTPException(404, "登录批次或条目不存在（服务重启后需重新提交）") from None
    except LoginCapacityError as exc:
        raise HTTPException(429, safe_message(str(exc))) from None
    except LoginConflictError as exc:
        raise HTTPException(409, safe_message(str(exc))) from None
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, safe_message(str(exc))) from None


@app.post("/admin/login/batches", status_code=202)
async def create_login_batch(req: LoginBatchInput, _=Depends(auth)):
    return await asyncio.to_thread(_login_call, login_service.submit,
                                   [a.model_dump(exclude_none=True) for a in req.accounts])


@app.get("/admin/login/batches")
def list_login_batches(_=Depends(auth)):
    return {"batches": login_service.list_batches()}


@app.get("/admin/login/batches/{batch_id}")
def get_login_batch(batch_id: str, _=Depends(auth)):
    return _login_call(login_service.status, batch_id)


@app.post("/admin/login/batches/{batch_id}/otp")
def submit_login_otp(batch_id: str, req: LoginOtpInput, _=Depends(auth)):
    code = req.code.get_secret_value()
    if not re.fullmatch(r"[0-9]{6}", code):
        raise HTTPException(400, "验证码须为6位数字")
    return _login_call(login_service.submit_otp, batch_id, req.index, code)


@app.post("/admin/login/batches/{batch_id}/cancel")
def cancel_login_batch(batch_id: str, _=Depends(auth)):
    return _login_call(login_service.cancel, batch_id)


# ------------------------- Daily threshold-based storage cleanup -------------------------
@app.get("/admin/cleanup/settings")
def get_cleanup_settings(_=Depends(auth)):
    return {"settings": cleanup_manager.get_settings(), "state": cleanup_manager.status()}


@app.put("/admin/cleanup/settings")
def update_cleanup_settings(req: CleanupSettings, _=Depends(auth)):
    value = cleanup_manager.update_settings(req.model_dump())
    loop = getattr(_CLEANUP_TASK, "get_loop", lambda: None)()
    if loop and loop.is_running():
        loop.call_soon_threadsafe(_CLEANUP_WAKEUP.set)
    journal.emit("cleanup.settings_updated", stage="cleanup", settings=value)
    return {"settings": value, "state": cleanup_manager.status()}


async def _cleanup_loop():
    """Independent from slow browser maintenance; runs at most one daily claim."""
    while True:
        try:
            result = await asyncio.to_thread(cleanup_manager.run_daily)
            if result.get("status") not in ("disabled", "not_due", "already_checked"):
                journal.emit("cleanup.checked", stage="cleanup",
                             level="warning" if result.get("status") in ("partial", "error", "claim_failed") else "info",
                             result=result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            journal.error("cleanup.loop_failed", exc, stage="cleanup",
                          reason=safe_message(str(exc)))
        try:
            await asyncio.wait_for(_CLEANUP_WAKEUP.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
        finally:
            _CLEANUP_WAKEUP.clear()


# ------------------------- Optional read-only release check -------------------------
release_checker = ReleaseChecker()


def _read_local_version() -> dict:
    try:
        with open(os.path.join(BASE_DIR, "version.json"), encoding="utf-8") as stream:
            result = json.load(stream)
            if isinstance(result, dict):
                return result
    except (OSError, ValueError):
        pass
    return {"version": app.version}


def _check_update_sync(force: bool = False) -> dict:
    return release_checker.check(str(_read_local_version().get("version") or app.version),
                                 force=force, token=os.environ.get("GITHUB_TOKEN", ""))


@app.get("/admin/update/check")
@app.get("/admin/repo/status")
async def admin_check_update(force: bool = False, _=Depends(auth)):
    """Check only this fork's latest published release; no file or process changes."""
    return await asyncio.to_thread(_check_update_sync, force)


@app.post("/admin/update/upgrade")
@app.post("/admin/repo/pull")
@app.post("/admin/repo/push")
def removed_online_repository_mutation(_=Depends(auth)):
    raise HTTPException(410, "在线热升级与仓库写操作已移除，请由维护者使用 Git 部署")


@app.on_event("startup")
async def _startup():
    for task in list(store.tasks.values()):
        if task.get("kind") in ("image", "video") and task.get("status") in ("queued", "processing"):
            _record_task_failure(task["id"], RuntimeError("服务重启中断了任务，请重新提交"))
    media_index = index_legacy_media(store, CFG.media_dir,
                                    os.path.join(CFG.data_dir, ".media-history-index-v1.json"), media_url)
    if media_index.get("indexed"):
        journal.emit("media.history_indexed", stage="startup", count=media_index["indexed"])
    accounts = store.list_accounts()
    KEEPALIVE_STATE["last_keepalive_at"] = max((a.get("last_keepalive") or 0 for a in accounts), default=0) or None
    KEEPALIVE_STATE["last_quota_at"] = max(((a.get("quota") or {}).get("checked_at") or 0 for a in accounts), default=0) or None
    report = getattr(store, "migration_report", {})
    if report.get("merged") or report.get("conflicts"):
        journal.emit("accounts.deduplicated", stage="startup", merged=report.get("merged", 0),
                     conflict_count=len(report.get("conflicts", [])))
    if not CFG.api_key:
        import secrets
        new_key = "m2a_" + secrets.token_hex(24)
        CFG.api_key = new_key
        _persist_env("MUSE2API_KEY", new_key)
        log.info("未检测到 MUSE2API_KEY，已生成初始密钥并保存至服务配置文件；日志不回显密钥")
    global _MAINTENANCE_TASK, _CLEANUP_TASK
    _MAINTENANCE_TASK = asyncio.create_task(_keepalive_loop())
    _CLEANUP_TASK = asyncio.create_task(_cleanup_loop())
    journal.emit("service.started", stage="startup")


@app.on_event("shutdown")
async def _shutdown():
    tasks = [t for t in (_MAINTENANCE_TASK, _MAINTENANCE_MANUAL_TASK, _CLEANUP_TASK) if t and not t.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    login_service.close()
    engine.stop()
    journal.emit("service.stopped", stage="shutdown")
