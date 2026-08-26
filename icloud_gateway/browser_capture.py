from __future__ import annotations

import re
import socket
import threading
import time
import urllib.parse
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .hme import (
    HmeError,
    HmeNetworkError,
    HmeSessionError,
    ICloudHmeSession,
    parse_hme_request,
)

DEFAULT_CAPTURE_URL = "https://www.icloud.com.cn/icloudplus/"
DEFAULT_CAPTURE_TIMEOUT_SECONDS = 15 * 60
_ACTIVE_STATES = frozenset({"starting", "waiting_login", "verifying", "cancelling"})


class CaptureError(RuntimeError):
    code = "capture_error"


class CaptureBusyError(CaptureError):
    code = "capture_busy"


class CaptureUnavailableError(CaptureError):
    code = "capture_unavailable"


class CaptureNoListRequestError(CaptureError):
    code = "capture_no_list_request"


class CaptureSessionRejectedError(CaptureError):
    code = "capture_session_rejected"


@dataclass(frozen=True)
class CaptureStatus:
    state: str
    message: str
    started_at: str | None = None
    finished_at: str | None = None
    error_code: str | None = None

    @property
    def active(self) -> bool:
        return self.state in _ACTIVE_STATES

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "active": self.active,
            "message": self.message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class _BrowserHandle:
    context: Any
    browser: Any | None
    owned_context: bool


@dataclass
class _CaptureJob:
    status: CaptureStatus
    cancel_event: threading.Event
    thread: threading.Thread | None = None


CaptureRunner = Callable[..., ICloudHmeSession]
SessionConsumer = Callable[[ICloudHmeSession], Any]
StatusConsumer = Callable[[dict[str, Any]], Any]
SessionTemplateProvider = Callable[[], ICloudHmeSession | None]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_capture_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlparse(str(value or "").strip())
        port = parsed.port
    except ValueError as exc:
        raise CaptureUnavailableError("iCloud capture URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or str(parsed.hostname or "").casefold() not in {"www.icloud.com", "www.icloud.com.cn"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path not in {"/", "/icloudplus/"}
    ):
        raise CaptureUnavailableError("iCloud capture URL is not allowed")
    return parsed.geturl()


def _ensure_private_profile(path: str | Path) -> Path:
    profile = Path(path).expanduser().resolve()
    try:
        profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        profile.chmod(0o700)
    except OSError as exc:
        raise CaptureUnavailableError("cannot create private browser profile") from exc
    return profile


def _resolve_cdp_endpoint(value: str) -> str:
    endpoint = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return endpoint
    if parsed.scheme not in {"http", "https"} or not hostname:
        return endpoint
    if parsed.username is not None or parsed.password is not None:
        return endpoint
    try:
        address = socket.gethostbyname(hostname)
    except OSError:
        return endpoint
    netloc = address if port is None else f"{address}:{port}"
    return urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


def _open_browser(
    playwright: Any,
    *,
    cdp_url: str,
    profile_dir: str | Path | None,
) -> _BrowserHandle:
    endpoint = str(cdp_url or "").strip()
    if endpoint:
        try:
            browser = playwright.chromium.connect_over_cdp(
                _resolve_cdp_endpoint(endpoint),
                timeout=20_000,
            )
        except Exception as exc:
            raise CaptureUnavailableError("cannot connect to persistent Chromium") from exc
        if not browser.contexts:
            raise CaptureUnavailableError("persistent Chromium has no browser context")
        return _BrowserHandle(context=browser.contexts[0], browser=browser, owned_context=False)
    if profile_dir is None:
        raise CaptureUnavailableError("persistent Chromium CDP is not configured")
    profile = _ensure_private_profile(profile_dir)
    try:
        context = playwright.chromium.launch_persistent_context(
            str(profile),
            headless=False,
            locale="zh-CN",
            viewport={"width": 1360, "height": 860},
            accept_downloads=False,
            args=["--no-first-run", "--no-default-browser-check"],
        )
    except Exception as exc:
        raise CaptureUnavailableError("cannot launch persistent Chromium") from exc
    return _BrowserHandle(context=context, browser=None, owned_context=True)


def _select_page(context: Any) -> Any:
    pages = list(context.pages)
    page = pages[-1] if pages else context.new_page()
    with suppress(Exception):
        page.bring_to_front()
    return page


def _request_headers_and_cookies(
    request: Any,
    context: Any,
    url: str,
) -> tuple[dict[str, str], Any]:
    try:
        headers = dict(request.all_headers())
    except Exception:
        headers = dict(getattr(request, "headers", {}) or {})
    cookies = ()
    if not str(headers.get("cookie") or "").strip():
        with suppress(Exception):
            cookies = context.cookies(url)
    return headers, cookies


def _click_icloud_sign_in(page: Any) -> bool:
    for label in ("登录", "Sign In"):
        try:
            button = page.get_by_role("button", name=label, exact=True)
            if button.count() != 1:
                continue
            button.click(timeout=5_000)
            return True
        except Exception:
            continue
    # Apple currently renders this entry point as a clickable DIV rather than
    # an accessible button, so role-only lookup leaves capture waiting forever.
    for label in ("使用 Apple 账户登录", "Sign in with Apple Account"):
        try:
            control = page.get_by_text(label, exact=True)
            if control.count() != 1:
                continue
            control.click(timeout=5_000)
            return True
        except Exception:
            continue
    return False


def _click_hide_my_email(page: Any) -> bool:
    try:
        button = page.get_by_role(
            "button",
            name=re.compile(r"(?:隐藏邮件地址|Hide My Email)", re.IGNORECASE),
        )
        if button.count() != 1:
            return False
        button.click(timeout=5_000)
        return True
    except Exception:
        return False


def _has_authenticated_cookies(context: Any, template: ICloudHmeSession | None) -> bool:
    if template is None:
        return False
    origin = (
        "https://www.icloud.com.cn"
        if template.host.endswith(".icloud.com.cn")
        else "https://www.icloud.com"
    )
    try:
        cookies = context.cookies([f"https://{template.host}/", f"{origin}/"])
    except Exception:
        return False
    names = {str(item.get("name") or "").strip() for item in cookies if isinstance(item, dict)}
    return {
        "X-APPLE-DS-WEB-SESSION-TOKEN",
        "X-APPLE-WEBAUTH-USER",
        "X-APPLE-WEBAUTH-TOKEN",
    }.issubset(names)


def capture_hme_session(
    *,
    cdp_url: str,
    cancel_event: threading.Event,
    on_waiting: Callable[[], Any],
    on_authenticated: Callable[[], Any] | None = None,
    timeout_seconds: float = DEFAULT_CAPTURE_TIMEOUT_SECONDS,
    start_url: str = DEFAULT_CAPTURE_URL,
    session_template: ICloudHmeSession | None = None,
    profile_dir: str | Path | None = None,
) -> ICloudHmeSession:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise CaptureUnavailableError("Playwright is unavailable") from exc

    target_url = _validate_capture_url(start_url)
    handle: _BrowserHandle | None = None
    captured: list[ICloudHmeSession] = []
    captured_event = threading.Event()
    authenticated_event = threading.Event()
    list_responses = 0
    rejected_sessions = 0

    def mark_authenticated() -> None:
        if authenticated_event.is_set():
            return
        authenticated_event.set()
        if on_authenticated is not None:
            with suppress(Exception):
                on_authenticated()

    def handle_response(response: Any) -> None:
        nonlocal list_responses, rejected_sessions
        if captured_event.is_set() or cancel_event.is_set():
            return
        try:
            response_url = str(response.url)
            parsed = urllib.parse.urlparse(response_url)
            if (
                parsed.scheme == "https"
                and str(parsed.hostname or "").casefold()
                in {"setup.icloud.com", "setup.icloud.com.cn"}
                and parsed.path == "/setup/ws/1/validate"
                and int(getattr(response, "status", 0) or 0) == 200
            ):
                mark_authenticated()
                return
            if parsed.path != "/v2/hme/list":
                return
            list_responses += 1
            if int(getattr(response, "status", 0) or 0) != 200:
                rejected_sessions += 1
                return
            request = response.request
            if str(getattr(request, "method", "GET") or "GET").upper() != "GET":
                rejected_sessions += 1
                return
            headers, cookies = _request_headers_and_cookies(request, handle.context, response_url)
            session = parse_hme_request(response_url, headers, cookies=cookies)
        except HmeSessionError:
            rejected_sessions += 1
            return
        except Exception:
            return
        captured.append(session)
        captured_event.set()

    try:
        with sync_playwright() as playwright:
            if cancel_event.is_set():
                raise CaptureError("iCloud capture was cancelled")
            handle = _open_browser(playwright, cdp_url=cdp_url, profile_dir=profile_dir)
            handle.context.on("response", handle_response)
            page = _select_page(handle.context)
            on_waiting()
            with suppress(PlaywrightTimeoutError, PlaywrightError):
                page.goto(target_url, wait_until="domcontentloaded", timeout=20_000)
            with suppress(Exception):
                page.bring_to_front()
            _click_icloud_sign_in(page)
            deadline = time.monotonic() + max(30.0, float(timeout_seconds))
            next_hme_click = 0.0
            next_cookie_check = 0.0
            while time.monotonic() < deadline:
                if cancel_event.is_set():
                    raise CaptureError("iCloud capture was cancelled")
                try:
                    page.wait_for_timeout(250)
                except PlaywrightError:
                    if cancel_event.wait(0.25):
                        raise CaptureError("iCloud capture was cancelled") from None
                if captured_event.is_set() and captured:
                    return captured[0]
                current = time.monotonic()
                if current >= next_cookie_check:
                    next_cookie_check = current + 1.0
                    if _has_authenticated_cookies(handle.context, session_template):
                        mark_authenticated()
                if current >= next_hme_click:
                    next_hme_click = current + 1.5
                    if _click_hide_my_email(page):
                        mark_authenticated()
                try:
                    if not handle.context.pages:
                        raise CaptureError("iCloud capture browser was closed")
                except PlaywrightError as exc:
                    raise CaptureError("iCloud capture browser was closed") from exc
            if list_responses <= 0:
                raise CaptureNoListRequestError("no iCloud HME list request was captured")
            if rejected_sessions > 0:
                raise CaptureSessionRejectedError("iCloud HME list request was rejected")
            raise CaptureError("iCloud capture timed out")
    except CaptureError:
        raise
    except PlaywrightError as exc:
        raise CaptureUnavailableError("iCloud capture browser failed") from exc
    except Exception as exc:
        raise CaptureUnavailableError("iCloud capture browser failed") from exc
    finally:
        if handle is not None:
            with suppress(Exception):
                handle.context.remove_listener("response", handle_response)
            if handle.owned_context:
                with suppress(Exception):
                    handle.context.close()


class CaptureManager:
    def __init__(
        self,
        *,
        cdp_url: str,
        on_session: SessionConsumer,
        on_status: StatusConsumer | None = None,
        get_session_template: SessionTemplateProvider | None = None,
        runner: CaptureRunner = capture_hme_session,
        timeout_seconds: float = DEFAULT_CAPTURE_TIMEOUT_SECONDS,
        profile_dir: str | Path | None = None,
    ) -> None:
        self.cdp_url = str(cdp_url or "").strip()
        self.on_session = on_session
        self.on_status = on_status
        self.get_session_template = get_session_template
        self.runner = runner
        self.timeout_seconds = max(30.0, float(timeout_seconds))
        self.profile_dir = profile_dir
        self._lock = threading.RLock()
        self._job: _CaptureJob | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            status = (
                self._job.status
                if self._job is not None
                else CaptureStatus(state="idle", message="iCloud capture is idle")
            )
        return status.as_dict()

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._job is not None and self._job.status.active:
                raise CaptureBusyError("iCloud capture is already running")
            started_at = _now()
            job = _CaptureJob(
                status=CaptureStatus(
                    state="starting",
                    message="connecting to persistent Chromium",
                    started_at=started_at,
                ),
                cancel_event=threading.Event(),
            )
            thread = threading.Thread(
                target=self._run,
                args=(job,),
                name="icloud-hme-capture",
                daemon=True,
            )
            job.thread = thread
            self._job = job
            thread.start()
            status = job.status
        self._publish(status)
        return status.as_dict()

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            if self._job is None or not self._job.status.active:
                return self.status()
            self._job.cancel_event.set()
            status = CaptureStatus(
                state="cancelling",
                message="cancelling iCloud capture",
                started_at=self._job.status.started_at,
            )
            self._job.status = status
        self._publish(status)
        return status.as_dict()

    def request_stop(self) -> None:
        with self._lock:
            job = self._job
            if job is not None and job.thread is not None and job.thread.is_alive():
                job.cancel_event.set()

    def shutdown(self, *, timeout: float = 10.0) -> bool:
        self.request_stop()
        with self._lock:
            job = self._job
            if job is None or job.thread is None or not job.thread.is_alive():
                return True
            thread = job.thread
        thread.join(max(0.0, float(timeout)))
        return not thread.is_alive()

    def _run(self, job: _CaptureJob) -> None:
        started_at = job.status.started_at

        def on_waiting() -> None:
            self._transition(
                job,
                "waiting_login",
                "finish Apple sign-in in the persistent browser",
            )

        def on_authenticated() -> None:
            self._transition(
                job,
                "verifying",
                "Apple sign-in detected; waiting for HME list",
            )

        try:
            template = (
                self.get_session_template() if self.get_session_template is not None else None
            )
            captured = self.runner(
                cdp_url=self.cdp_url,
                cancel_event=job.cancel_event,
                on_waiting=on_waiting,
                on_authenticated=on_authenticated,
                timeout_seconds=self.timeout_seconds,
                start_url=DEFAULT_CAPTURE_URL,
                session_template=template,
                profile_dir=self.profile_dir,
            )
            self.on_session(captured)
        except CaptureError as exc:
            cancelled = job.cancel_event.is_set()
            self._finish(
                job,
                state="cancelled" if cancelled else "failed",
                message=("iCloud capture cancelled" if cancelled else str(exc)),
                error_code=(None if cancelled else exc.code),
                started_at=started_at,
            )
            return
        except HmeNetworkError:
            self._finish(
                job,
                state="failed",
                message=(
                    "captured session could not be verified because the iCloud "
                    "network request failed"
                ),
                error_code="capture_save_network",
                started_at=started_at,
            )
            return
        except HmeSessionError:
            self._finish(
                job,
                state="failed",
                message="captured session was rejected during iCloud verification",
                error_code="capture_save_session",
                started_at=started_at,
            )
            return
        except HmeError:
            self._finish(
                job,
                state="failed",
                message="captured session received an invalid iCloud verification response",
                error_code="capture_save_validation",
                started_at=started_at,
            )
            return
        except Exception as exc:
            if str(getattr(exc, "code", "")) == "edge_sync_error":
                self._finish(
                    job,
                    state="failed",
                    message=(
                        "iCloud session was saved locally but could not be uploaded "
                        "to the remote server"
                    ),
                    error_code="capture_upload_failed",
                    started_at=started_at,
                )
                return
            self._finish(
                job,
                state="failed",
                message="iCloud capture could not save the validated session",
                error_code="capture_save_failed",
                started_at=started_at,
            )
            return
        self._finish(
            job,
            state="captured",
            message="iCloud HME session captured",
            error_code=None,
            started_at=started_at,
        )

    def _transition(self, job: _CaptureJob, state: str, message: str) -> None:
        with self._lock:
            if self._job is not job or job.cancel_event.is_set():
                return
            status = CaptureStatus(
                state=state,
                message=message,
                started_at=job.status.started_at,
            )
            job.status = status
        self._publish(status)

    def _finish(
        self,
        job: _CaptureJob,
        *,
        state: str,
        message: str,
        error_code: str | None,
        started_at: str | None,
    ) -> None:
        with self._lock:
            if self._job is not job:
                return
            status = CaptureStatus(
                state=state,
                message=message,
                started_at=started_at,
                finished_at=_now(),
                error_code=error_code,
            )
            job.status = status
        self._publish(status)

    def _publish(self, status: CaptureStatus) -> None:
        if self.on_status is not None:
            with suppress(Exception):
                self.on_status(status.as_dict())


__all__ = [
    "CaptureBusyError",
    "CaptureError",
    "CaptureManager",
    "CaptureNoListRequestError",
    "CaptureSessionRejectedError",
    "CaptureStatus",
    "CaptureUnavailableError",
    "DEFAULT_CAPTURE_TIMEOUT_SECONDS",
    "DEFAULT_CAPTURE_URL",
    "capture_hme_session",
]
