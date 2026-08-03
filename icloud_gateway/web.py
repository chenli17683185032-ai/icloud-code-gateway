from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .browser_capture import CaptureBusyError
from .config import Settings
from .database import ConflictError, DatabaseError, NotFoundError
from .hme import HmeError, HmeSessionError
from .imap_otp import ImapCredentialsError, ImapError
from .jobs import BatchJobManager
from .security import (
    AdminSession,
    AdminSessionCodec,
    InvalidSessionError,
    SecurityError,
    validate_access_key,
    verify_admin_password,
)
from .service import (
    GatewayBusyError,
    GatewayEdgeSyncError,
    GatewayError,
    GatewayNotAllowedError,
    GatewayNotConfiguredError,
    GatewayRateLimitedError,
    GatewayService,
)

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"
ADMIN_COOKIE = "icg_admin"
MAX_REQUEST_BYTES = 2 * 1024 * 1024
NOTICE_MESSAGES = {
    "imap_saved": "IMAP 配置已验证并保存。",
    "imap_error": "IMAP 配置未保存，请检查连接信息。",
    "hme_saved": "HME Session 已验证并保存，历史 Alias 已导入。",
    "hme_error": "HME Session 未更新，原有会话保持不变。",
    "capture_started": "已连接持久浏览器，请打开 iCloud 浏览器完成 Apple 登录。",
    "capture_busy": "HME Session 捕获正在进行。",
    "capture_cancelled": "已请求取消 HME Session 捕获。",
    "sync_done": "Alias 已与 Apple HME 列表完成对账。",
    "sync_error": "Alias 对账失败，本地数据未被清空。",
    "alias_saved": "Alias 配置已保存。",
    "alias_error": "Alias 配置未保存。",
}
CAPTURE_STATE_LABELS = {
    "idle": "待机",
    "starting": "连接中",
    "waiting_login": "等待登录",
    "verifying": "验证会话",
    "cancelling": "正在取消",
    "captured": "已捕获",
    "cancelled": "已取消",
    "failed": "失败",
}
CAPTURE_STATE_MESSAGES = {
    "idle": "未启动捕获。",
    "starting": "正在连接持久浏览器。",
    "waiting_login": "请打开 iCloud 浏览器完成 Apple 登录。",
    "verifying": "已发现会话，正在只读验证。",
    "cancelling": "正在停止捕获。",
    "captured": "HME Session 已更新。",
    "cancelled": "捕获已取消。",
    "failed": "捕获失败，请检查浏览器连接或会话状态。",
}


class CodeRequest(BaseModel):
    access_key: Annotated[str, Field(min_length=1, max_length=128)]


class CreateAliasesRequest(BaseModel):
    count: Annotated[int, Field(ge=1, le=100)] = 1
    label_prefix: Annotated[str, Field(min_length=1, max_length=140)]
    note: Annotated[str, Field(max_length=500)] = ""
    sender_filter: Annotated[str, Field(max_length=254)] = ""


class BulkAliasesRequest(BaseModel):
    action: Literal["issue_keys", "reveal_keys", "deactivate", "delete"]
    alias_ids: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=64)]],
        Field(min_length=1, max_length=100),
    ]
    confirmed: bool = False


class ConfirmedAliasAction(BaseModel):
    confirmed: Literal[True]


class DeleteAliasRequest(BaseModel):
    confirmation: Annotated[str, Field(min_length=3, max_length=254)]


def _client_ip(request: Request) -> str:
    return "unknown" if request.client is None else str(request.client.host or "unknown")


def _admin_session(request: Request, codec: AdminSessionCodec) -> AdminSession | None:
    try:
        return codec.decode(request.cookies.get(ADMIN_COOKIE, ""))
    except InvalidSessionError:
        return None


def _csrf_matches(expected: str, supplied: Any) -> bool:
    # compare_digest rejects str operands that are not pure ASCII, so a token
    # carrying any non-ASCII byte has to be compared as bytes or it raises.
    token = str(supplied or "")
    if not token:
        return False
    return hmac.compare_digest(str(expected).encode("utf-8"), token.encode("utf-8"))


def _require_admin_json(request: Request, codec: AdminSessionCodec) -> AdminSession:
    session = _admin_session(request, codec)
    if session is None:
        raise HTTPException(status_code=401, detail="admin authentication required")
    if not _csrf_matches(session.csrf_token, request.headers.get("X-CSRF-Token")):
        raise HTTPException(status_code=403, detail="CSRF validation failed")
    return session


def _validate_form_csrf(session: AdminSession, supplied: Any) -> None:
    if not _csrf_matches(session.csrf_token, supplied):
        raise HTTPException(status_code=403, detail="CSRF validation failed")


def _redirect_notice(code: str) -> RedirectResponse:
    return RedirectResponse(url=f"/admin?notice={code}", status_code=303)


def _require_control_token(request: Request, settings: Settings) -> None:
    expected = str(settings.control_plane_token or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="control plane is not configured")
    header = str(request.headers.get("Authorization") or "").strip()
    token = ""
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
    if not token:
        token = str(request.headers.get("X-Control-Token") or "").strip()
    if not token or not hmac.compare_digest(expected.encode("utf-8"), token.encode("utf-8")):
        raise HTTPException(status_code=401, detail="control authentication required")


class ControlAliasRequest(BaseModel):
    id: Annotated[str, Field(default="", max_length=64)] = ""
    email: Annotated[str, Field(min_length=3, max_length=254)]
    label: Annotated[str, Field(default="", max_length=160)] = ""
    note: Annotated[str, Field(default="", max_length=500)] = ""
    sender_filter: Annotated[str, Field(default="", max_length=254)] = ""
    state: Literal["active", "inactive"] = "active"
    access_key: Annotated[str, Field(default="", max_length=128)] = ""


class ControlKeyRequest(BaseModel):
    access_key: Annotated[str, Field(min_length=1, max_length=128)]
    id: Annotated[str, Field(default="", max_length=64)] = ""


class ControlStateRequest(BaseModel):
    state: Literal["active", "inactive"]



def _capture_view(status: dict[str, Any]) -> dict[str, Any]:
    state = str(status.get("state") or "idle")
    return {
        **status,
        "state_label": CAPTURE_STATE_LABELS.get(state, state),
        "message_label": CAPTURE_STATE_MESSAGES.get(state, "状态已更新。"),
    }


def _apply_security_headers(
    response: Response,
    *,
    request: Request,
    settings: Settings,
) -> None:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'; object-src 'none'"
    )
    if request.url.path.startswith(("/api/", "/admin")):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    elif request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    if settings.cookie_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"


def create_app(
    settings: Settings,
    *,
    service: GatewayService | None = None,
) -> FastAPI:
    gateway = service or GatewayService(settings)
    jobs = BatchJobManager(gateway)
    session_codec = AdminSessionCodec(
        settings.master_key, lifetime_seconds=settings.admin_session_seconds
    )
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    static_versions = {
        name: hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:16]
        for name in ("admin.js", "app.css", "favicon.svg", "public.js")
    }
    templates.env.globals["static_versions"] = static_versions

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        gateway.database.purge_old_audit_events(days=7)
        jobs.start()
        try:
            yield
        finally:
            deadline = time.monotonic() + 10.0
            jobs.request_stop()
            gateway.request_stop()
            jobs_stopped = jobs.shutdown(timeout=max(0.0, deadline - time.monotonic()))
            gateway.shutdown(
                timeout=max(0.0, deadline - time.monotonic()),
                close_database=jobs_stopped,
            )

    app = FastAPI(
        title="iCloud Code Gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.gateway = gateway
    app.state.jobs = jobs
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.trusted_hosts))

    def _too_large(response_request: Request) -> JSONResponse:
        response = JSONResponse({"status": "request_too_large"}, status_code=413)
        _apply_security_headers(response, request=response_request, settings=settings)
        return response

    @app.middleware("http")
    async def security_boundary(request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
                if declared_length < 0 or declared_length > MAX_REQUEST_BYTES:
                    return _too_large(request)
            except ValueError:
                return _too_large(request)
        else:
            # Chunked HTTP/1 and HTTP/2 streams may have no declared length.
            # Buffer only up to the hard limit, then replay the bounded body to
            # Starlette through its cached request-body path.
            received = 0
            chunks: list[bytes] = []
            async for chunk in request.stream():
                received += len(chunk)
                if received > MAX_REQUEST_BYTES:
                    return _too_large(request)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        _apply_security_headers(response, request=request, settings=settings)
        return response

    @app.get("/", response_class=HTMLResponse)
    async def public_page(request: Request):
        if not settings.serves_public_otp:
            return RedirectResponse("/admin/login", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="public.html",
            context={"page_title": "验证码领取"},
        )

    @app.post("/api/code")
    async def code_lookup(request: Request, payload: CodeRequest):
        if not settings.serves_public_otp:
            return JSONResponse({"status": "not_allowed"}, status_code=404)
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    gateway.lookup_code,
                    payload.access_key,
                    client_ip=_client_ip(request),
                ),
                timeout=max(1, settings.otp_request_timeout_seconds) + 1,
            )
        except TimeoutError:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        except GatewayRateLimitedError as exc:
            return JSONResponse(
                {"status": "rate_limited", "retry_after": exc.retry_after},
                status_code=429,
                headers={"Retry-After": str(exc.retry_after)},
            )
        except GatewayBusyError:
            return JSONResponse(
                {"status": "busy", "retry_after": 3},
                status_code=503,
                headers={"Retry-After": "3"},
            )
        except (GatewayNotConfiguredError, GatewayError):
            return JSONResponse({"status": "unavailable"}, status_code=503)
        if result.status == "invalid_key":
            return JSONResponse({"status": "invalid_key"}, status_code=404)
        return {
            "status": result.status,
            "code": result.code or None,
            "received_at": result.received_at,
            "expires_at": result.expires_at,
            "retry_after": result.retry_after,
        }

    @app.get("/healthz")
    async def healthz():
        if gateway.database.quick_check() != "ok":
            return JSONResponse({"status": "error"}, status_code=503)
        return {"status": "ok"}

    @app.get("/robots.txt", response_class=PlainTextResponse)
    async def robots():
        return "User-agent: *\nDisallow: /\n"

    @app.get("/admin/login", response_class=HTMLResponse)
    async def admin_login_page(request: Request):
        if _admin_session(request, session_codec) is not None:
            return RedirectResponse("/admin", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="admin_login.html",
            context={"page_title": "管理员登录", "error": False},
        )

    @app.post("/admin/login")
    async def admin_login(request: Request):
        ip = _client_ip(request)
        decision = gateway.rate_limiter.check("admin-login", ip, limit=10, window_seconds=600)
        if not decision.allowed:
            return templates.TemplateResponse(
                request=request,
                name="admin_login.html",
                context={"page_title": "管理员登录", "error": True},
                status_code=429,
                headers={"Retry-After": str(decision.retry_after)},
            )
        form = await request.form()
        if not verify_admin_password(settings.admin_password, str(form.get("password") or "")):
            return templates.TemplateResponse(
                request=request,
                name="admin_login.html",
                context={"page_title": "管理员登录", "error": True},
                status_code=401,
            )
        token, _session = session_codec.issue()
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie(
            ADMIN_COOKIE,
            token,
            max_age=settings.admin_session_seconds,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            path="/admin",
        )
        gateway.database.record_audit_event("admin_login", "succeeded")
        return response

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_dashboard(request: Request, notice: str = ""):
        session = _admin_session(request, session_codec)
        if session is None:
            return RedirectResponse("/admin/login", status_code=303)
        context = gateway.dashboard()
        context["capture"] = _capture_view(context["capture"])
        context.update(
            {
                "page_title": "iCloud 验证码网关",
                "csrf_token": session.csrf_token,
                "notice": NOTICE_MESSAGES.get(notice, ""),
                "notice_kind": "error" if notice.endswith("error") else "success",
                "cdp_configured": bool(settings.cdp_url),
                "alias_batch_limit": settings.alias_batch_limit,
                "public_base_url": settings.public_base_url,
                "deployment_mode": settings.deployment_mode,
            }
        )
        return templates.TemplateResponse(
            request=request,
            name="admin.html",
            context=context,
        )

    @app.get("/admin/api/browser/auth")
    async def admin_browser_auth(request: Request):
        if _admin_session(request, session_codec) is None:
            return RedirectResponse("/admin/login", status_code=303)
        return Response(status_code=204)

    @app.post("/admin/logout")
    async def admin_logout(request: Request):
        session = _admin_session(request, session_codec)
        if session is None:
            return RedirectResponse("/admin/login", status_code=303)
        form = await request.form()
        _validate_form_csrf(session, form.get("csrf_token"))
        response = RedirectResponse("/admin/login", status_code=303)
        response.delete_cookie(ADMIN_COOKIE, path="/admin")
        return response

    @app.post("/admin/imap")
    async def save_imap(request: Request):
        session = _admin_session(request, session_codec)
        if session is None:
            return RedirectResponse("/admin/login", status_code=303)
        form = await request.form()
        _validate_form_csrf(session, form.get("csrf_token"))
        try:
            await asyncio.to_thread(
                gateway.configure_imap,
                {
                    "forwarding_email": form.get("forwarding_email"),
                    "host": form.get("host"),
                    "port": form.get("port"),
                    "username": form.get("username"),
                    "password": form.get("password"),
                    "folder": form.get("folder"),
                    "junk_folder": form.get("junk_folder"),
                    "proxy": form.get("proxy"),
                    "clear_proxy": form.get("clear_proxy") == "on",
                },
            )
        except (ValueError, ImapCredentialsError, ImapError, GatewayError):
            return _redirect_notice("imap_error")
        return _redirect_notice("imap_saved")

    @app.post("/admin/hme/import")
    async def import_hme(request: Request):
        if not settings.manages_hme:
            return _redirect_notice("hme_error")
        session = _admin_session(request, session_codec)
        if session is None:
            return RedirectResponse("/admin/login", status_code=303)
        form = await request.form()
        _validate_form_csrf(session, form.get("csrf_token"))
        try:
            await asyncio.to_thread(
                gateway.import_hme_session, str(form.get("session_import") or "")
            )
        except (GatewayError, HmeError, HmeSessionError, DatabaseError, ValueError):
            return _redirect_notice("hme_error")
        return _redirect_notice("hme_saved")

    @app.post("/admin/hme/capture/start")
    async def start_capture(request: Request):
        if not settings.manages_hme:
            return _redirect_notice("hme_error")
        session = _admin_session(request, session_codec)
        if session is None:
            return RedirectResponse("/admin/login", status_code=303)
        form = await request.form()
        _validate_form_csrf(session, form.get("csrf_token"))
        if not settings.cdp_url:
            return _redirect_notice("hme_error")
        try:
            gateway.capture_manager.start()
        except CaptureBusyError:
            return _redirect_notice("capture_busy")
        return _redirect_notice("capture_started")

    @app.post("/admin/hme/capture/cancel")
    async def cancel_capture(request: Request):
        session = _admin_session(request, session_codec)
        if session is None:
            return RedirectResponse("/admin/login", status_code=303)
        form = await request.form()
        _validate_form_csrf(session, form.get("csrf_token"))
        gateway.capture_manager.cancel()
        return _redirect_notice("capture_cancelled")

    @app.get("/admin/api/capture/status")
    async def capture_status(request: Request):
        if _admin_session(request, session_codec) is None:
            raise HTTPException(status_code=401, detail="admin authentication required")
        return _capture_view(gateway.capture_manager.status())

    @app.post("/admin/hme/sync")
    async def sync_hme(request: Request):
        if not settings.manages_hme:
            return _redirect_notice("sync_error")
        session = _admin_session(request, session_codec)
        if session is None:
            return RedirectResponse("/admin/login", status_code=303)
        form = await request.form()
        _validate_form_csrf(session, form.get("csrf_token"))
        try:
            await asyncio.to_thread(gateway.sync_aliases)
        except (GatewayError, HmeError, HmeSessionError, DatabaseError):
            return _redirect_notice("sync_error")
        return _redirect_notice("sync_done")

    def _idempotency_key(request: Request) -> str | None:
        value = request.headers.get("Idempotency-Key")
        if value is not None and (not value.strip() or len(value) > 200):
            raise ValueError("idempotency key is invalid")
        return value

    @app.post("/admin/api/aliases")
    async def create_aliases(request: Request, payload: CreateAliasesRequest):
        _require_admin_json(request, session_codec)
        if not settings.manages_hme:
            return JSONResponse({"status": "not_allowed"}, status_code=403)
        try:
            job, created = jobs.create_alias_job(
                count=payload.count,
                label_prefix=payload.label_prefix,
                note=payload.note,
                sender_filter=payload.sender_filter,
                idempotency_key=_idempotency_key(request),
            )
        except ConflictError:
            return JSONResponse({"status": "idempotency_conflict"}, status_code=409)
        except ValueError:
            return JSONResponse({"status": "invalid_request"}, status_code=422)
        return JSONResponse(
            {
                "status": "queued" if created else job["status"],
                "job_id": job["id"],
                "requested": job["requested"],
            },
            status_code=202,
            headers={"Location": f"/admin/api/jobs/{job['id']}"},
        )

    @app.post("/admin/api/aliases/bulk")
    async def bulk_aliases(request: Request, payload: BulkAliasesRequest):
        _require_admin_json(request, session_codec)
        if len(payload.alias_ids) > settings.alias_batch_limit:
            return JSONResponse({"status": "invalid_request"}, status_code=422)
        try:
            job, created = jobs.create_bulk_job(
                action=payload.action,
                alias_ids=payload.alias_ids,
                confirmed=payload.confirmed,
                idempotency_key=_idempotency_key(request),
            )
        except ConflictError:
            return JSONResponse({"status": "idempotency_conflict"}, status_code=409)
        except ValueError:
            return JSONResponse({"status": "invalid_request"}, status_code=422)
        return JSONResponse(
            {
                "status": "queued" if created else job["status"],
                "job_id": job["id"],
                "requested": job["requested"],
            },
            status_code=202,
            headers={"Location": f"/admin/api/jobs/{job['id']}"},
        )

    @app.get("/admin/api/jobs")
    async def active_batch_jobs(request: Request):
        if _admin_session(request, session_codec) is None:
            raise HTTPException(status_code=401, detail="admin authentication required")
        return {"jobs": jobs.active_jobs()}

    @app.get("/admin/api/jobs/{job_id}")
    async def batch_job_status(job_id: str, request: Request):
        if _admin_session(request, session_codec) is None:
            raise HTTPException(status_code=401, detail="admin authentication required")
        try:
            return jobs.public_job(job_id, reveal_keys=False)
        except NotFoundError:
            return JSONResponse({"status": "not_found"}, status_code=404)

    @app.post("/admin/api/jobs/{job_id}/results")
    async def batch_job_results(job_id: str, request: Request):
        _require_admin_json(request, session_codec)
        try:
            job = jobs.public_job(job_id, reveal_keys=True)
        except NotFoundError:
            return JSONResponse({"status": "not_found"}, status_code=404)
        if job["status"] not in {"completed", "partial", "failed", "needs_reconcile", "cancelled"}:
            return JSONResponse({"status": "conflict"}, status_code=409)
        return job

    @app.post("/admin/api/aliases/{alias_id}/key")
    async def issue_alias_key(alias_id: str, request: Request):
        _require_admin_json(request, session_codec)
        try:
            issued = await asyncio.to_thread(gateway.issue_access_key, alias_id)
            alias = gateway.database.get_alias(alias_id)
        except ConflictError:
            return JSONResponse({"status": "conflict"}, status_code=409)
        except GatewayNotAllowedError:
            return JSONResponse({"status": "not_allowed"}, status_code=403)
        except GatewayEdgeSyncError:
            return JSONResponse({"status": "edge_sync_error"}, status_code=502)
        except (NotFoundError, DatabaseError):
            return JSONResponse({"status": "error"}, status_code=404)
        return {
            "status": "issued",
            "id": alias_id,
            "email": alias["email"],
            "label": alias["label"],
            "access_key": issued.access_key,
            "public_url": settings.public_base_url,
        }

    @app.post("/admin/api/aliases/{alias_id}/key/reveal")
    async def reveal_alias_key(alias_id: str, request: Request):
        _require_admin_json(request, session_codec)
        try:
            access_key = await asyncio.to_thread(gateway.reveal_access_key, alias_id)
            alias = gateway.database.get_alias(alias_id)
        except ConflictError:
            return JSONResponse({"status": "conflict"}, status_code=409)
        except NotFoundError:
            return JSONResponse({"status": "error"}, status_code=404)
        except DatabaseError:
            return JSONResponse({"status": "error"}, status_code=500)
        return {
            "status": "revealed",
            "id": alias_id,
            "email": alias["email"],
            "label": alias["label"],
            "access_key": access_key,
            "public_url": settings.public_base_url,
        }

    @app.post("/admin/api/codes/recent")
    async def recent_admin_codes(request: Request):
        _require_admin_json(request, session_codec)
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(gateway.admin_recent_codes),
                timeout=max(1, settings.otp_request_timeout_seconds) + 1,
            )
        except TimeoutError:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        except GatewayBusyError:
            return JSONResponse(
                {"status": "busy", "retry_after": 3},
                status_code=503,
                headers={"Retry-After": "3"},
            )
        except GatewayNotConfiguredError:
            return JSONResponse({"status": "not_configured"}, status_code=503)
        except (GatewayError, DatabaseError):
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ok", **result}

    @app.delete("/admin/api/aliases/{alias_id}/key")
    async def revoke_alias_key(alias_id: str, request: Request):
        _require_admin_json(request, session_codec)
        try:
            await asyncio.to_thread(gateway.revoke_access_key, alias_id)
        except GatewayNotAllowedError:
            return JSONResponse({"status": "not_allowed"}, status_code=403)
        except GatewayEdgeSyncError:
            return JSONResponse({"status": "edge_sync_error"}, status_code=502)
        except (NotFoundError, DatabaseError):
            return JSONResponse({"status": "error"}, status_code=404)
        return {"status": "revoked"}

    @app.post("/admin/api/aliases/{alias_id}/deactivate")
    async def deactivate_alias(
        alias_id: str,
        request: Request,
        _payload: ConfirmedAliasAction,
    ):
        _require_admin_json(request, session_codec)
        try:
            alias = await asyncio.to_thread(gateway.deactivate_alias, alias_id)
        except ConflictError:
            return JSONResponse({"status": "conflict"}, status_code=409)
        except NotFoundError:
            return JSONResponse({"status": "error"}, status_code=404)
        except (GatewayError, HmeError, HmeSessionError, DatabaseError, ValueError):
            return JSONResponse({"status": "error"}, status_code=502)
        return {"status": "deactivated", "state": alias["state"]}

    @app.post("/admin/api/aliases/{alias_id}/reactivate")
    async def reactivate_alias(
        alias_id: str,
        request: Request,
        _payload: ConfirmedAliasAction,
    ):
        _require_admin_json(request, session_codec)
        try:
            alias = await asyncio.to_thread(gateway.reactivate_alias, alias_id)
        except ConflictError:
            return JSONResponse({"status": "conflict"}, status_code=409)
        except NotFoundError:
            return JSONResponse({"status": "error"}, status_code=404)
        except (GatewayError, HmeError, HmeSessionError, DatabaseError, ValueError):
            return JSONResponse({"status": "error"}, status_code=502)
        return {"status": "reactivated", "state": alias["state"]}

    @app.delete("/admin/api/aliases/{alias_id}")
    async def delete_alias(
        alias_id: str,
        request: Request,
        payload: DeleteAliasRequest,
    ):
        _require_admin_json(request, session_codec)
        try:
            await asyncio.to_thread(
                gateway.delete_alias,
                alias_id,
                confirmation=payload.confirmation,
            )
        except ConflictError:
            return JSONResponse({"status": "conflict"}, status_code=409)
        except NotFoundError:
            return JSONResponse({"status": "error"}, status_code=404)
        except (GatewayError, HmeError, HmeSessionError, DatabaseError, ValueError):
            return JSONResponse({"status": "error"}, status_code=502)
        return {"status": "deleted"}

    @app.post("/admin/aliases/{alias_id}")
    async def update_alias(alias_id: str, request: Request):
        session = _admin_session(request, session_codec)
        if session is None:
            return RedirectResponse("/admin/login", status_code=303)
        form = await request.form()
        _validate_form_csrf(session, form.get("csrf_token"))
        try:
            gateway.update_alias(
                alias_id,
                label=str(form.get("label") or ""),
                note=str(form.get("note") or ""),
                sender_filter=str(form.get("sender_filter") or ""),
            )
        except (ValueError, NotFoundError, DatabaseError):
            return _redirect_notice("alias_error")
        return _redirect_notice("alias_saved")


    @app.post("/control/v1/aliases")
    async def control_upsert_alias(request: Request, payload: ControlAliasRequest):
        if settings.deployment_mode == "control":
            # local control usually pushes out, but accept no-op mirror
            pass
        _require_control_token(request, settings)
        access_key = str(payload.access_key or "").strip() or None
        if access_key is not None:
            try:
                access_key = validate_access_key(access_key)
            except SecurityError:
                return JSONResponse({"status": "invalid_request"}, status_code=422)
        try:
            alias = await asyncio.to_thread(
                gateway.register_control_alias,
                alias_id=payload.id,
                email=payload.email,
                label=payload.label,
                note=payload.note,
                sender_filter=payload.sender_filter,
                state=payload.state,
                access_key=access_key,
            )
        except (ValueError, ConflictError, DatabaseError, SecurityError):
            return JSONResponse({"status": "invalid_request"}, status_code=422)
        return {
            "status": "ok",
            "id": alias["id"],
            "email": alias["email"],
            "state": alias["state"],
            "has_access_key": bool(alias.get("has_access_key")),
        }

    @app.post("/control/v1/aliases/by-email/{email}/key")
    async def control_issue_key(email: str, request: Request, payload: ControlKeyRequest):
        _require_control_token(request, settings)
        email = unquote(str(email or "")).strip()
        try:
            access_key = validate_access_key(payload.access_key)
            issued = await asyncio.to_thread(
                gateway.register_control_access_key_by_email,
                email,
                access_key,
            )
        except (ConflictError, NotFoundError, DatabaseError, SecurityError, ValueError):
            return JSONResponse({"status": "invalid_request"}, status_code=422)
        return {
            "status": "ok",
            "alias_id": issued.alias_id,
            "hint": issued.hint,
        }

    @app.delete("/control/v1/aliases/by-email/{email}/key")
    async def control_revoke_key(email: str, request: Request):
        _require_control_token(request, settings)
        email = unquote(str(email or "")).strip()
        try:
            alias = await asyncio.to_thread(
                gateway.register_control_state_by_email,
                email,
                "active",
            )
            await asyncio.to_thread(gateway.database.revoke_access_key, alias["id"])
        except (NotFoundError, DatabaseError, ValueError):
            return JSONResponse({"status": "not_found"}, status_code=404)
        return {"status": "ok"}

    @app.post("/control/v1/aliases/by-email/{email}/state")
    async def control_set_state(email: str, request: Request, payload: ControlStateRequest):
        _require_control_token(request, settings)
        email = unquote(str(email or "")).strip()
        try:
            alias = await asyncio.to_thread(
                gateway.register_control_state_by_email,
                email,
                payload.state,
            )
        except (NotFoundError, DatabaseError, ValueError, ConflictError):
            return JSONResponse({"status": "invalid_request"}, status_code=422)
        return {"status": "ok", "email": alias["email"], "state": alias["state"]}

    @app.delete("/control/v1/aliases/by-email/{email}")
    async def control_delete_alias(email: str, request: Request):
        _require_control_token(request, settings)
        email = unquote(str(email or "")).strip()
        try:
            await asyncio.to_thread(gateway.register_control_delete_by_email, email)
        except NotFoundError:
            return JSONResponse({"status": "not_found"}, status_code=404)
        except DatabaseError:
            return JSONResponse({"status": "error"}, status_code=500)
        return {"status": "ok"}

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException):
        return JSONResponse(
            {"status": "error"},
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_request: Request, _exc: RequestValidationError):
        return JSONResponse({"status": "invalid_request"}, status_code=422)

    return app


__all__ = ["create_app"]
