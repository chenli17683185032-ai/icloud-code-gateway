from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .browser_capture import CaptureManager
from .config import Settings
from .database import ConflictError, Database, DatabaseError, IssuedAccessKey, NotFoundError
from .edge_sync import EdgeSyncClient, EdgeSyncError
from .hme import (
    HmeClient,
    HmeError,
    HmeNetworkError,
    HmeSessionError,
    ICloudHmeSession,
    parse_hme_session_import,
    validate_icloud_setup_session,
)
from .imap_otp import (
    PUBLIC_OTP_SENDER_POLICY,
    ImapConfig,
    ImapCredentialsError,
    ImapError,
    ImapOtpReader,
)
from .mail_tags import refresh_usage_tags as _refresh_usage_tags
from .mailbox_watcher import MailboxWatcher
from .rate_limit import SlidingWindowRateLimiter
from .security import SecretBox, SecurityError, hash_access_key

_LOOKUP_OUTCOME_VIEW = {
    "found": ("已返回验证码", "success"),
    "no_code": ("暂无验证码", "waiting"),
    "not_found": ("暂无验证码", "waiting"),
    "not_configured": ("IMAP 未配置", "error"),
    "imap_invalid": ("IMAP 凭据失效", "error"),
    "imap_error": ("IMAP 查询失败", "error"),
    "invalid_key": ("无效 Key", "muted"),
}
_BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")

HME_SETTING_KEY = "hme_session"
IMAP_SETTING_KEY = "imap_config"
HME_CONFIRM_ATTEMPTS = 3
HME_CONFIRM_DELAY_SECONDS = 0.5


class GatewayError(RuntimeError):
    code = "gateway_error"


class GatewayNotConfiguredError(GatewayError):
    code = "not_configured"


class GatewayRateLimitedError(GatewayError):
    code = "rate_limited"

    def __init__(self, retry_after: int) -> None:
        super().__init__("request rate limit exceeded")
        self.retry_after = max(1, int(retry_after))


class GatewayBusyError(GatewayError):
    code = "busy"


class GatewayRetryableError(GatewayError):
    code = "retryable"


class GatewayStoppingError(GatewayRetryableError):
    pass


class GatewayNotAllowedError(GatewayError):
    code = "not_allowed"


class GatewayEdgeSyncError(GatewayError):
    code = "edge_sync_error"


@dataclass(frozen=True)
class CodeLookupResult:
    status: str
    code: str = ""
    received_at: str | None = None
    expires_at: str | None = None
    retry_after: int | None = None


@dataclass(frozen=True)
class PreparedLookup:
    """A key already rate-limited and resolved to its alias.

    Long polling re-checks the index many times per request; without this split
    each re-check would spend another token from the 12-per-minute key limiter.
    """

    alias_id: str
    email: str
    sender_filter: str
    client_ip: str


@dataclass(frozen=True)
class CreatedAlias:
    alias: dict[str, Any]
    issued_key: IssuedAccessKey


@dataclass(frozen=True)
class AliasBatchItemResult:
    index: int
    status: str
    alias: dict[str, Any] | None = None
    access_key: str | None = None


@dataclass(frozen=True)
class AliasBatchResult:
    requested_count: int
    created: tuple[CreatedAlias, ...]
    results: tuple[AliasBatchItemResult, ...]
    error_code: str | None = None

    @property
    def succeeded_count(self) -> int:
        return len(self.created)

    @property
    def failed_count(self) -> int:
        return self.requested_count - self.succeeded_count


@dataclass(frozen=True)
class BulkAliasActionResult:
    requested_count: int
    results: tuple[dict[str, Any], ...]

    @property
    def succeeded_count(self) -> int:
        return sum(item["status"] == "success" for item in self.results)

    @property
    def failed_count(self) -> int:
        return self.requested_count - self.succeeded_count


HmeClientFactory = Callable[[ICloudHmeSession], HmeClient]
HmeSessionRefresher = Callable[[ICloudHmeSession], ICloudHmeSession]
ImapReaderFactory = Callable[[ImapConfig], ImapOtpReader]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp() -> str:
    return _utc_now().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _iso_timestamp(value: float) -> str:
    return (
        datetime.fromtimestamp(value, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _beijing_timestamp(value: str) -> str:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(_BEIJING_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


class GatewayService:
    def __init__(
        self,
        settings: Settings,
        *,
        database: Database | None = None,
        hme_client_factory: HmeClientFactory | None = None,
        hme_session_refresher: HmeSessionRefresher | None = None,
        imap_reader_factory: ImapReaderFactory = ImapOtpReader,
        rate_limiter: SlidingWindowRateLimiter | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], Any] = time.sleep,
        start_maintenance: bool = True,
        edge_sync_client: EdgeSyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.secret_box = SecretBox(settings.master_key)
        self.database = database or Database(settings.database_path, self.secret_box)
        self.database.initialize()
        self.hme_client_factory = hme_client_factory or (
            lambda session: HmeClient(session, proxy=settings.hme_proxy)
        )
        self._stop_event = threading.Event()
        self.hme_session_refresher = hme_session_refresher or (
            lambda session: validate_icloud_setup_session(
                session,
                proxy=settings.hme_proxy,
                stop_event=self._stop_event,
            )
        )
        self.imap_reader_factory = imap_reader_factory
        self.rate_limiter = rate_limiter or SlidingWindowRateLimiter()
        self.clock = clock
        self.sleeper = sleeper
        self.edge_sync_client = edge_sync_client
        if self.edge_sync_client is None and settings.is_control and settings.edge_sync_enabled:
            self.edge_sync_client = EdgeSyncClient(settings)
        self._hme_lock = threading.RLock()
        self._remote_write_lock = threading.Lock()
        self._remote_write_active = False
        self._alias_generation = 0
        self._refresh_condition = threading.Condition(threading.Lock())
        self._refreshing = False
        self._refresh_generation = 0
        self._refresh_error: Exception | None = None
        self._state_lock = threading.RLock()
        self._hme_state: dict[str, Any] = {
            "state": "not_configured" if self.get_hme_session() is None else "degraded",
            "last_validated_at": None,
            "last_attempt_at": None,
            "next_attempt_at": None,
            "last_error_kind": None,
        }
        self._last_validated_ts: float | None = None
        self._maintenance_thread: threading.Thread | None = None
        # Separate quotas: an admin full scan used to consume the same four
        # slots as public lookups and could starve paying buyers outright.
        self._imap_slots = threading.BoundedSemaphore(4)
        self._admin_imap_slots = threading.BoundedSemaphore(2)
        self._admin_code_cache_lock = threading.Lock()
        self._admin_code_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.capture_manager = CaptureManager(
            cdp_url=settings.cdp_url,
            on_session=self.save_hme_session,
            on_status=self._capture_status_changed,
            get_session_template=self.get_hme_session,
            timeout_seconds=settings.capture_timeout_seconds,
            profile_dir=settings.browser_profile_dir,
        )
        self._bootstrap_imap_from_environment()
        # One warm mailbox connection serves every reader. Constructed always so
        # callers can query it, but only started where codes are actually served;
        # an unstarted watcher reports `ready is False` and every read falls back
        # to the original on-demand scan.
        self.mailbox_watcher = MailboxWatcher(self._watcher_imap_config)
        if start_maintenance:
            self.mailbox_watcher.start()
        self._edge_push_lock = threading.Lock()
        self._edge_reconcile_thread: threading.Thread | None = None
        if start_maintenance and settings.manages_hme:
            self._maintenance_thread = threading.Thread(
                target=self._maintenance_loop,
                name="icloud-hme-maintenance",
                daemon=True,
            )
            self._maintenance_thread.start()
        if (
            start_maintenance
            and settings.is_control
            and settings.edge_sync_enabled
            and self.edge_sync_client is not None
            and settings.edge_reconcile_seconds > 0
        ):
            self._edge_reconcile_thread = threading.Thread(
                target=self._edge_reconcile_loop,
                name="icloud-edge-reconcile",
                daemon=True,
            )
            self._edge_reconcile_thread.start()

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def request_stop(self) -> None:
        with self._hme_lock:
            self._stop_event.set()
        with self._refresh_condition:
            self._refresh_condition.notify_all()
        self.capture_manager.request_stop()

    def shutdown(self, *, timeout: float = 10.0, close_database: bool = True) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        self.request_stop()
        self.mailbox_watcher.stop(timeout=max(0.0, deadline - time.monotonic()))
        thread = self._maintenance_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(max(0.0, deadline - time.monotonic()))
        if thread is not None and thread.is_alive():
            return False
        reconcile = self._edge_reconcile_thread
        if (
            reconcile is not None
            and reconcile.is_alive()
            and reconcile is not threading.current_thread()
        ):
            reconcile.join(max(0.0, deadline - time.monotonic()))
        if reconcile is not None and reconcile.is_alive():
            return False
        if not self.capture_manager.shutdown(timeout=max(0.0, deadline - time.monotonic())):
            return False
        if close_database:
            self.database.close()
        return True

    def get_hme_session(self) -> ICloudHmeSession | None:
        value = self.database.get_secret(HME_SETTING_KEY)
        return None if value is None else ICloudHmeSession.from_mapping(value)

    @staticmethod
    def _close_client(client: Any) -> None:
        close = getattr(client, "close", None)
        if callable(close):
            with suppress(Exception):
                close()

    def _persist_rotated_hme_session(
        self,
        original: ICloudHmeSession,
        candidate: ICloudHmeSession,
    ) -> bool:
        if candidate == original:
            return False
        with self._hme_lock:
            current = self.get_hme_session()
            if current != original:
                self.database.record_audit_event("hme_cookie_rotation", "discarded_stale")
                return False
            self.database.set_secret(HME_SETTING_KEY, candidate.as_secret_dict())
            self.database.record_audit_event("hme_cookie_rotation", "saved")
            return True

    def _set_hme_state(
        self,
        state: str,
        *,
        error_kind: str | None = None,
        next_attempt_ts: float | None = None,
        validated: bool = False,
    ) -> None:
        now = float(self.clock())
        with self._state_lock:
            self._hme_state.update(
                {
                    "state": state,
                    "last_attempt_at": _iso_timestamp(now),
                    "next_attempt_at": (
                        None if next_attempt_ts is None else _iso_timestamp(next_attempt_ts)
                    ),
                    "last_error_kind": error_kind,
                }
            )
            if validated:
                self._last_validated_ts = now
                self._hme_state["last_validated_at"] = _iso_timestamp(now)

    def hme_status(self) -> dict[str, Any]:
        try:
            session = self.get_hme_session()
            configured = session is not None
        except (HmeSessionError, SecurityError):
            session = None
            configured = False
        with self._state_lock:
            status = dict(self._hme_state)
            last_validated = self._last_validated_ts
        status["configured"] = configured
        if not configured:
            status["state"] = "not_configured"

        now = float(self.clock())
        freshness = max(300, int(self.settings.hme_freshness_seconds))
        maintenance = max(300, int(self.settings.hme_maintenance_interval_seconds))
        state = str(status.get("state") or "not_configured")

        # Disk persistence is independent of Apple validity.
        db_path = self.settings.database_path
        profile_dir = self.settings.browser_profile_dir
        status["persisted_on_disk"] = bool(configured and db_path.is_file())
        status["database_path"] = str(db_path)
        status["browser_profile_dir"] = None if profile_dir is None else str(profile_dir)
        status["browser_profile_persisted"] = bool(
            profile_dir is not None and Path(profile_dir).exists()
        )
        status["capture_mode"] = (
            "cdp"
            if self.settings.cdp_url
            else ("local_profile" if profile_dir is not None else "unavailable")
        )
        status["freshness_seconds"] = freshness
        status["maintenance_interval_seconds"] = maintenance
        status["last_validated_at_display"] = (
            None
            if not status.get("last_validated_at")
            else _beijing_timestamp(str(status["last_validated_at"]))
        )
        status["next_attempt_at_display"] = (
            None
            if not status.get("next_attempt_at")
            else _beijing_timestamp(str(status["next_attempt_at"]))
        )

        age_seconds = None if last_validated is None else max(0, int(now - float(last_validated)))
        status["seconds_since_validated"] = age_seconds
        if last_validated is None:
            status["fresh_until_at"] = None
            status["fresh_until_at_display"] = None
            status["seconds_until_freshness_check"] = None
        else:
            remaining = max(0, int(float(last_validated) + freshness - now))
            status["seconds_until_freshness_check"] = remaining
            fresh_until = float(last_validated) + freshness
            status["fresh_until_at"] = _iso_timestamp(fresh_until)
            status["fresh_until_at_display"] = _beijing_timestamp(status["fresh_until_at"])

        # What the operator should do next. "refreshing" alone is NOT re-login.
        if state == "not_configured":
            status["health_level"] = "warn"
            status["operator_action"] = "login_required"
            status["operator_hint"] = "尚未保存 Session，请点“登录更新”完成 Apple 登录。"
        elif state == "reauth_required":
            status["health_level"] = "error"
            status["operator_action"] = "login_required"
            status["operator_hint"] = "Apple 已拒绝当前 Session，需要重新点“登录更新”。"
        elif state == "refreshing":
            status["health_level"] = "info"
            status["operator_action"] = "wait"
            status["operator_hint"] = "后台正在刷新/校验 Session，通常无需你操作；不是已丢失。"
        elif state == "degraded":
            status["health_level"] = "warn"
            status["operator_action"] = "retry_later"
            status["operator_hint"] = "网络或代理暂时异常；磁盘上的 Session 还在，可稍后再试。"
        elif state == "ready" and status.get("persisted_on_disk"):
            status["health_level"] = "good"
            status["operator_action"] = "none"
            status["operator_hint"] = "Session 已落盘，可直接创建；看到“正在刷新”不等于要重登。"
        else:
            status["health_level"] = "warn"
            status["operator_action"] = "check"
            status["operator_hint"] = "Session 状态异常，请检查本地数据目录或重新登录更新。"

        # keep host handy when configured
        if session is not None and not status.get("host"):
            status["host"] = session.host
        return status

    def _capture_status_changed(self, status: dict[str, Any]) -> None:
        state = str(status.get("state") or "")
        if state in {"starting", "verifying"}:
            self._set_hme_state("refreshing", error_kind="authentication")
        elif state in {"waiting_login", "failed"}:
            self._set_hme_state("reauth_required", error_kind="authentication")

    def _validated_alias_snapshot(self, session: ICloudHmeSession) -> list[dict[str, Any]]:
        client = self.hme_client_factory(session)
        try:
            return self._validated_remote_aliases(client.list_aliases())
        finally:
            self._close_client(client)

    def _begin_alias_snapshot(self, *, allow_during_write: bool = False) -> int:
        with self._hme_lock:
            if self._remote_write_active and not allow_during_write:
                raise GatewayBusyError("alias lifecycle operation is in progress")
            return self._alias_generation

    def _commit_alias_snapshot(
        self,
        remote_aliases: list[dict[str, Any]],
        generation: int,
        *,
        event_type: str,
        event_outcome: str,
        session: ICloudHmeSession | None = None,
        allow_during_write: bool = False,
    ) -> bool:
        with self._hme_lock:
            if (
                self._remote_write_active and not allow_during_write
            ) or generation != self._alias_generation:
                self.database.record_audit_event(event_type, "discarded_stale")
                return False
            if session is not None:
                self.database.set_secret(HME_SETTING_KEY, session.as_secret_dict())
            if not allow_during_write:
                self._reconcile_remote_aliases(remote_aliases)
            self._alias_generation += 1
            self.database.record_audit_event(event_type, event_outcome)
            return True

    def _begin_remote_write(self) -> None:
        if self._stop_event.is_set():
            raise GatewayStoppingError("gateway is shutting down")
        if not self._remote_write_lock.acquire(blocking=False):
            raise GatewayBusyError("alias lifecycle operation is already in progress")
        try:
            with self._hme_lock:
                if self._stop_event.is_set():
                    raise GatewayStoppingError("gateway is shutting down")
                self._remote_write_active = True
                self._alias_generation += 1
        except Exception:
            self._remote_write_lock.release()
            raise

    def _finish_remote_write(self) -> None:
        with self._hme_lock:
            self._remote_write_active = False
        self._remote_write_lock.release()

    def _refresh_hme_session(self, *, during_write: bool = False) -> ICloudHmeSession:
        with self._refresh_condition:
            if self._stop_event.is_set():
                raise GatewayStoppingError("gateway is shutting down")
            generation = self._refresh_generation
            if self._refreshing:
                while self._refreshing and not self._stop_event.is_set():
                    self._refresh_condition.wait(timeout=1.0)
                if self._refresh_generation != generation:
                    if self._refresh_error is not None:
                        raise self._refresh_error
                    refreshed = self.get_hme_session()
                    if refreshed is None:
                        raise GatewayNotConfiguredError("iCloud HME session is not configured")
                    return refreshed
            self._refreshing = True
            self._refresh_error = None
        self._set_hme_state("refreshing")
        error: Exception | None = None
        result: ICloudHmeSession | None = None
        try:
            alias_generation = self._begin_alias_snapshot(allow_during_write=during_write)
            old_session = self.get_hme_session()
            if old_session is None:
                raise GatewayNotConfiguredError("iCloud HME session is not configured")
            candidate = self.hme_session_refresher(old_session)
            if self._stop_event.is_set():
                raise GatewayStoppingError("gateway is shutting down")
            aliases = self._validated_alias_snapshot(candidate)
            if self._stop_event.is_set():
                raise GatewayStoppingError("gateway is shutting down")
            committed = self._commit_alias_snapshot(
                aliases,
                alias_generation,
                event_type="hme_session",
                event_outcome="refreshed",
                session=candidate,
                allow_during_write=during_write,
            )
            if committed:
                self._set_hme_state("ready", validated=True)
                result = candidate
            else:
                current = self.get_hme_session()
                if current is None:
                    raise GatewayNotConfiguredError("iCloud HME session is not configured")
                result = current
        except HmeSessionError as exc:
            error = exc
            self._set_hme_state("reauth_required", error_kind="authentication")
            if self.settings.capture_configured:
                with suppress(Exception):
                    self.capture_manager.start()
        except (HmeNetworkError, HmeError, OSError, TimeoutError) as exc:
            error = exc
            self._set_hme_state("degraded", error_kind="network")
        except Exception as exc:
            error = exc
            self._set_hme_state("degraded", error_kind="internal")
        finally:
            with self._refresh_condition:
                self._refresh_error = error
                self._refreshing = False
                self._refresh_generation += 1
                self._refresh_condition.notify_all()
        if error is not None:
            raise error
        assert result is not None
        return result

    def _ensure_hme_fresh(self) -> None:
        if self._stop_event.is_set():
            raise GatewayStoppingError("gateway is shutting down")
        with self._state_lock:
            last_validated = self._last_validated_ts
        if last_validated is None or float(self.clock()) - last_validated >= float(
            self.settings.hme_freshness_seconds
        ):
            self._refresh_hme_session()

    def _refresh_after_write_rejection(self) -> None:
        with suppress(Exception):
            self._refresh_hme_session(during_write=True)
        raise GatewayRetryableError(
            "iCloud rejected authentication before confirming the write; retry explicitly"
        )

    def _maintenance_loop(self) -> None:
        delay = float(self.settings.hme_maintenance_interval_seconds)
        backoff = 300.0
        while not self._stop_event.wait(delay):
            try:
                self._refresh_hme_session()
            except GatewayNotConfiguredError:
                delay = float(self.settings.hme_maintenance_interval_seconds)
            except HmeSessionError:
                delay = float(self.settings.hme_maintenance_interval_seconds)
            except Exception:
                delay = min(float(self.settings.hme_retry_max_seconds), backoff)
                backoff = min(float(self.settings.hme_retry_max_seconds), backoff * 2.0)
                self._set_hme_state(
                    "degraded",
                    error_kind="network",
                    next_attempt_ts=float(self.clock()) + delay,
                )
            else:
                backoff = 300.0
                delay = float(self.settings.hme_maintenance_interval_seconds)
                with self._state_lock:
                    self._hme_state["next_attempt_at"] = _iso_timestamp(float(self.clock()) + delay)

    def save_hme_session(self, session: ICloudHmeSession) -> int:
        alias_generation = self._begin_alias_snapshot()
        aliases = self._validated_alias_snapshot(session)
        committed = self._commit_alias_snapshot(
            aliases,
            alias_generation,
            event_type="hme_session",
            event_outcome="saved",
            session=session,
        )
        if committed:
            self._set_hme_state("ready", validated=True)
        return len(aliases)

    def import_hme_session(self, source: str) -> int:
        session = parse_hme_session_import(source)
        return self.save_hme_session(session)

    def sync_aliases(self) -> list[dict[str, Any]]:
        alias_generation = self._begin_alias_snapshot()
        session = self.get_hme_session()
        if session is None:
            raise GatewayNotConfiguredError("iCloud HME session is not configured")
        try:
            remote_aliases = self._validated_alias_snapshot(session)
        except HmeSessionError:
            self._refresh_hme_session()
            return self.database.list_aliases()
        committed = self._commit_alias_snapshot(
            remote_aliases,
            alias_generation,
            event_type="hme_sync",
            event_outcome="succeeded",
        )
        if committed:
            self._set_hme_state("ready", validated=True)
        return self.database.list_aliases()

    def _hme_client(self) -> HmeClient:
        session = self.get_hme_session()
        if session is None:
            raise GatewayNotConfiguredError("iCloud HME session is not configured")
        return self.hme_client_factory(session)

    def _validated_remote_aliases(
        self,
        remote_aliases: list[dict[str, Any]],
        *,
        allow_empty: bool = False,
    ) -> list[dict[str, Any]]:
        validated: list[dict[str, Any]] = []
        seen_emails: set[str] = set()
        seen_remote_ids: set[str] = set()
        for item in remote_aliases:
            if not isinstance(item, Mapping):
                raise GatewayError("iCloud HME returned an invalid alias snapshot")
            remote = dict(item)
            email = str(remote.get("hme") or remote.get("email") or "").strip().casefold()
            anonymous_id = str(remote.get("anonymousId") or "").strip()
            label = str(remote.get("label") or email).strip()
            note = str(remote.get("note") or "").strip()
            local_part, separator, domain = email.rpartition("@")
            if (
                email.count("@") != 1
                or not separator
                or not local_part
                or not domain
                or "." not in domain
                or len(email) > 254
                or any(character.isspace() for character in email)
                or not anonymous_id
                or len(anonymous_id) > 256
                or "\r" in anonymous_id
                or "\n" in anonymous_id
                or not isinstance(remote.get("isActive"), bool)
                or not label
                or len(label) > 160
                or "\r" in label
                or "\n" in label
                or len(note) > 500
                or "\r" in note
                or "\n" in note
                or email in seen_emails
                or anonymous_id in seen_remote_ids
            ):
                raise GatewayError("iCloud HME returned an invalid alias snapshot")
            seen_emails.add(email)
            seen_remote_ids.add(anonymous_id)
            validated.append(remote)
        if not validated and self.database.count_remote_aliases() and not allow_empty:
            self.database.record_audit_event("hme_sync", "refused_empty")
            raise GatewayError("iCloud HME returned no aliases; refusing to deactivate all")
        return validated

    def _reconcile_remote_aliases(self, remote_aliases: list[dict[str, Any]]) -> None:
        synced_at = _timestamp()
        seen: list[str] = []
        for remote in remote_aliases:
            email = str(remote.get("hme") or remote.get("email") or "").strip().casefold()
            self.database.sync_remote_alias(
                email=email,
                remote_metadata=remote,
                synced_at=synced_at,
            )
            seen.append(email)
        known_remote = self.database.count_remote_aliases()
        missing = max(0, known_remote - len(seen))
        shrink_limit = max(5, int(known_remote * 0.1))
        if missing > shrink_limit:
            self.database.record_audit_event("hme_sync", "refused_incomplete")
            return
        self.database.finish_remote_sync(seen, synced_at=synced_at)

    def ensure_remote_aliases(self) -> str:
        """Pull Apple HME into the local list when the snapshot is stale.

        Returns a notice code, or an empty string when nothing should be flashed.
        """
        if not self.settings.manages_hme:
            return ""
        try:
            if self.get_hme_session() is None:
                return ""
        except (HmeSessionError, SecurityError):
            return ""
        latest = self.database.latest_alias_synced_at()
        if latest:
            try:
                synced = datetime.fromisoformat(latest.replace("Z", "+00:00"))
            except ValueError:
                synced = None
            if synced is not None:
                if synced.tzinfo is None:
                    synced = synced.replace(tzinfo=UTC)
                if (datetime.now(UTC) - synced.astimezone(UTC)).total_seconds() < 30.0:
                    return ""
        before = self.database.count_remote_aliases()
        try:
            self._refresh_hme_session()
        except GatewayBusyError:
            return ""
        except GatewayNotConfiguredError:
            return ""
        except (GatewayError, HmeError, HmeSessionError, DatabaseError):
            return "sync_error"
        after = self.database.count_remote_aliases()
        if after > before:
            return "sync_done"
        return ""

    def _confirmed_remote_aliases(
        self,
        client: HmeClient,
        anonymous_id: str,
        *,
        expected_active: bool | None = None,
        expected_absent: bool = False,
    ) -> list[dict[str, Any]]:
        known_remote_ids = {
            str(remote.get("anonymousId") or "").strip()
            for alias in self.database.list_aliases()
            for remote in [alias.get("remote_metadata")]
            if isinstance(remote, Mapping) and str(remote.get("anonymousId") or "").strip()
        }
        required_remote_ids = (
            known_remote_ids - {anonymous_id} if expected_absent else known_remote_ids
        )
        allow_empty = expected_absent and self.database.count_remote_aliases() <= 1
        for attempt in range(HME_CONFIRM_ATTEMPTS):
            remote_aliases = self._validated_remote_aliases(
                client.list_aliases(),
                allow_empty=allow_empty,
            )
            snapshot_remote_ids = {
                str(remote.get("anonymousId") or "").strip() for remote in remote_aliases
            }
            snapshot_is_complete = required_remote_ids.issubset(snapshot_remote_ids)
            match = next(
                (
                    remote
                    for remote in remote_aliases
                    if str(remote.get("anonymousId") or "").strip() == anonymous_id
                ),
                None,
            )
            if expected_absent and match is None and snapshot_is_complete:
                return remote_aliases
            if (
                not expected_absent
                and match is not None
                and match.get("isActive") is expected_active
                and snapshot_is_complete
            ):
                return remote_aliases
            if attempt + 1 < HME_CONFIRM_ATTEMPTS:
                self.sleeper(HME_CONFIRM_DELAY_SECONDS)
        raise GatewayError("iCloud HME did not confirm the requested alias state")

    def _remote_alias(self, alias_id: str, *, state: str) -> tuple[dict[str, Any], str]:
        alias = self.database.get_alias(alias_id)
        if alias["state"] != state:
            raise ConflictError(f"alias must be {state}")
        remote = alias.get("remote_metadata")
        anonymous_id = (
            str(remote.get("anonymousId") or "").strip() if isinstance(remote, Mapping) else ""
        )
        if not anonymous_id:
            raise ConflictError("alias is not managed by iCloud HME")
        return alias, anonymous_id

    def create_aliases(
        self,
        *,
        count: int,
        label_prefix: str,
        note: str = "",
        sender_filter: str = "",
    ) -> AliasBatchResult:
        requested_count = int(count)
        if requested_count < 1 or requested_count > min(self.settings.alias_batch_limit, 100):
            raise ValueError("alias batch count is outside the configured limit")
        prefix = str(label_prefix or "").strip()
        if not prefix or len(prefix) > 140:
            raise ValueError("label prefix is invalid")
        self._ensure_hme_fresh()
        with self._hme_lock:
            session = self.get_hme_session()
            if session is None:
                raise GatewayNotConfiguredError("iCloud HME session is not configured")
            client = self.hme_client_factory(session)
            created: list[CreatedAlias] = []
            results: list[AliasBatchItemResult] = []
            for index in range(requested_count):
                try:
                    label = prefix if requested_count == 1 else f"{prefix} {index + 1}"
                    remote = client.create_alias(label=label, note=note)
                    email = str(remote.get("hme") or remote.get("email") or "").strip().casefold()
                    if email.count("@") != 1 or not str(remote.get("anonymousId") or "").strip():
                        raise HmeError("iCloud HME reserve response is incomplete")
                    alias = self.database.upsert_alias(
                        email=email,
                        remote_metadata=remote,
                        label=label,
                        note=note,
                        sender_filter=sender_filter,
                        state=("inactive" if remote.get("isActive") is False else "active"),
                        synced_at=_timestamp(),
                    )
                    issued = self.database.issue_access_key(alias["id"])
                    self._push_alias_to_edge(
                        alias, access_key=issued.access_key, action="issue_key"
                    )
                    created.append(CreatedAlias(alias=alias, issued_key=issued))
                    results.append(
                        AliasBatchItemResult(
                            index=index + 1,
                            status="success",
                            alias=alias,
                            access_key=issued.access_key,
                        )
                    )
                    self.database.record_audit_event(
                        "alias_create", "succeeded", alias_id=alias["id"]
                    )
                except HmeSessionError:
                    self.database.record_audit_event("alias_create", "auth_rejected")
                    self._close_client(client)
                    self._refresh_after_write_rejection()
                except HmeNetworkError:
                    self.database.record_audit_event("alias_create", "unknown")
                    results.append(AliasBatchItemResult(index=index + 1, status="unknown"))
                    results.extend(
                        AliasBatchItemResult(index=remaining + 1, status="error")
                        for remaining in range(index + 1, requested_count)
                    )
                    self._close_client(client)
                    return AliasBatchResult(
                        requested_count=requested_count,
                        created=tuple(created),
                        results=tuple(results),
                        error_code="batch_stopped",
                    )
                except Exception:
                    self.database.record_audit_event("alias_create", "error")
                    results.append(AliasBatchItemResult(index=index + 1, status="error"))
                    results.extend(
                        AliasBatchItemResult(index=remaining + 1, status="error")
                        for remaining in range(index + 1, requested_count)
                    )
                    self._close_client(client)
                    return AliasBatchResult(
                        requested_count=requested_count,
                        created=tuple(created),
                        results=tuple(results),
                        error_code="batch_stopped",
                    )
                if index + 1 < requested_count:
                    self.sleeper(2.0)
            self._close_client(client)
            return AliasBatchResult(
                requested_count=requested_count,
                created=tuple(created),
                results=tuple(results),
            )

    def bulk_alias_action(
        self,
        *,
        action: str,
        alias_ids: list[str] | tuple[str, ...],
        confirmed: bool = False,
    ) -> BulkAliasActionResult:
        ids = [str(alias_id).strip() for alias_id in alias_ids]
        if (
            not ids
            or len(ids) > min(self.settings.alias_batch_limit, 100)
            or any(not alias_id for alias_id in ids)
        ):
            raise ValueError("alias IDs are invalid")
        if len(set(ids)) != len(ids):
            raise ValueError("alias IDs must be unique")
        if action not in {"issue_keys", "reveal_keys", "deactivate", "delete"}:
            raise ValueError("bulk alias action is invalid")
        if action in {"deactivate", "delete"} and not confirmed:
            raise ValueError("bulk alias action requires confirmation")

        results: list[dict[str, Any]] = []
        with self._hme_lock:
            for alias_id in ids:
                try:
                    if action == "issue_keys":
                        issued = self.issue_access_key(alias_id)
                        alias = self.database.get_alias(alias_id)
                        result = {
                            "id": alias_id,
                            "status": "success",
                            "email": alias["email"],
                            "label": alias["label"],
                            "access_key": issued.access_key,
                        }
                    elif action == "reveal_keys":
                        access_key = self.reveal_access_key(alias_id)
                        alias = self.database.get_alias(alias_id)
                        result = {
                            "id": alias_id,
                            "status": "success",
                            "email": alias["email"],
                            "label": alias["label"],
                            "access_key": access_key,
                        }
                    elif action == "deactivate":
                        alias = self.deactivate_alias(alias_id)
                        result = {
                            "id": alias_id,
                            "status": "success",
                            "email": alias["email"],
                        }
                    else:
                        alias = self.database.get_alias(alias_id)
                        self.delete_alias(alias_id, confirmation=alias["email"])
                        result = {
                            "id": alias_id,
                            "status": "success",
                            "email": alias["email"],
                        }
                except NotFoundError:
                    result = {"id": alias_id, "status": "not_found"}
                except ConflictError:
                    result = {"id": alias_id, "status": "conflict"}
                except (HmeError, HmeSessionError, GatewayError):
                    result = {"id": alias_id, "status": "unknown"}
                except Exception:
                    result = {"id": alias_id, "status": "error"}
                results.append(result)
        return BulkAliasActionResult(requested_count=len(ids), results=tuple(results))

    def configure_imap(self, values: Mapping[str, Any], *, test: bool = True) -> ImapConfig:
        existing = self.get_imap_config()
        payload = dict(values)
        clear_proxy = bool(payload.pop("clear_proxy", False))
        for secret_name in ("password",):
            if not str(payload.get(secret_name) or "").strip() and existing is not None:
                payload[secret_name] = getattr(existing, secret_name)
        if clear_proxy:
            payload["proxy"] = ""
        elif not str(payload.get("proxy") or "").strip() and existing is not None:
            payload["proxy"] = existing.proxy
        config = ImapConfig.from_mapping(payload)
        if test:
            self.imap_reader_factory(config).check(
                timeout=self.settings.otp_request_timeout_seconds
            )
        self.database.set_secret(IMAP_SETTING_KEY, config.as_secret_dict())
        self.database.record_audit_event("imap_config", "saved")
        self.mailbox_watcher.refresh_soon()
        return config

    def _watcher_imap_config(self) -> ImapConfig | None:
        """Config provider for the watcher thread; never raises into the loop."""
        try:
            return self.get_imap_config()
        except (ImapCredentialsError, SecurityError, DatabaseError):
            return None

    def get_imap_config(self) -> ImapConfig | None:
        value = self.database.get_secret(IMAP_SETTING_KEY)
        if value is None:
            return None
        # Never inject the HME proxy here. Whether the mailbox is reachable
        # directly is a property of the mailbox, not of the Apple API: the
        # production edge reaches QQ IMAP from Germany in about two seconds
        # while the CN proxy cannot reach it at all, so inheriting turned
        # every public lookup into imap_error. Set a proxy on the IMAP form
        # when one is actually needed.
        return ImapConfig.from_mapping(value)

    def test_imap(self) -> None:
        config = self.get_imap_config()
        if config is None:
            raise GatewayNotConfiguredError("IMAP is not configured")
        self.imap_reader_factory(config).check(timeout=self.settings.otp_request_timeout_seconds)

    def _bootstrap_imap_from_environment(self) -> None:
        """Load IMAP settings from env when the local DB has no saved config.

        Used by the local control plane so App passwords can ship in the
        private creds file without forcing a public OTP surface.
        """
        if self.get_imap_config() is not None:
            return
        password = str(os.environ.get("ICLOUD_GATEWAY_IMAP_PASSWORD") or "").strip()
        username = str(os.environ.get("ICLOUD_GATEWAY_IMAP_USERNAME") or "").strip()
        forwarding = str(
            os.environ.get("ICLOUD_GATEWAY_IMAP_FORWARDING_EMAIL") or username or ""
        ).strip()
        if not password or not username or not forwarding:
            return
        host = str(
            os.environ.get("ICLOUD_GATEWAY_IMAP_HOST") or "imap.mail.me.com"
        ).strip()
        port_raw = str(os.environ.get("ICLOUD_GATEWAY_IMAP_PORT") or "993").strip()
        folder = str(os.environ.get("ICLOUD_GATEWAY_IMAP_FOLDER") or "INBOX").strip()
        junk_folder = str(os.environ.get("ICLOUD_GATEWAY_IMAP_JUNK_FOLDER") or "").strip()
        proxy = str(os.environ.get("ICLOUD_GATEWAY_IMAP_PROXY") or "").strip()
        test_raw = str(os.environ.get("ICLOUD_GATEWAY_IMAP_BOOTSTRAP_TEST") or "0").strip()
        should_test = test_raw.casefold() in {"1", "true", "yes", "on"}
        try:
            port = int(port_raw or 993)
            self.configure_imap(
                {
                    "forwarding_email": forwarding,
                    "host": host,
                    "port": port,
                    "username": username,
                    "password": password,
                    "folder": folder,
                    "junk_folder": junk_folder,
                    "proxy": proxy,
                },
                test=should_test,
            )
            self.database.record_audit_event("imap_config", "bootstrapped")
        except Exception:
            # Leave the local console bootable even if IMAP bootstrap fails;
            # the admin page can still save/test credentials manually.
            self.database.record_audit_event("imap_config", "bootstrap_failed")

    def _require_hme_management(self) -> None:
        if not self.settings.manages_hme:
            raise GatewayNotAllowedError("HME management is disabled in edge mode")

    def _require_public_otp(self) -> None:
        if not self.settings.serves_public_otp:
            raise GatewayNotAllowedError("public OTP is disabled in control mode")

    def _require_admin_otp(self) -> None:
        # Admin IMAP reads are allowed on control/full/edge so the local
        # control console can show live codes next to each alias.
        return

    def _push_alias_to_edge(
        self,
        alias: dict[str, Any],
        *,
        access_key: str | None = None,
        action: str = "upsert",
    ) -> None:
        if self.edge_sync_client is None or not self.settings.edge_sync_enabled:
            return
        try:
            email = str(alias.get("email") or "").strip()
            if action == "delete":
                self.edge_sync_client.delete_alias(email=email)
                return
            if action == "revoke_key":
                self.edge_sync_client.revoke_access_key(email=email)
                return
            if action == "issue_key":
                if not access_key:
                    raise GatewayEdgeSyncError("access key is required for edge issue")
                self.edge_sync_client.issue_access_key(
                    alias_id=str(alias["id"]),
                    email=email,
                    access_key=access_key,
                )
                return
            self.edge_sync_client.upsert_alias(
                alias_id=str(alias["id"]),
                email=email,
                label=str(alias.get("label") or email),
                note=str(alias.get("note") or ""),
                sender_filter=str(alias.get("sender_filter") or ""),
                state=str(alias.get("state") or "active"),
                access_key=access_key,
            )
        except EdgeSyncError as exc:
            self.database.record_audit_event(
                "edge_sync",
                "failed",
                alias_id=str(alias.get("id") or "") or None,
            )
            raise GatewayEdgeSyncError(str(exc)) from exc
        self.database.record_audit_event(
            "edge_sync",
            action,
            alias_id=str(alias.get("id") or "") or None,
        )

    def register_control_alias(
        self,
        *,
        alias_id: str = "",
        email: str,
        label: str = "",
        note: str = "",
        sender_filter: str = "",
        state: str = "active",
        access_key: str | None = None,
    ) -> dict[str, Any]:
        """Edge/control registration path. Does not touch Apple HME."""
        if state not in {"active", "inactive"}:
            raise ValueError("alias state is invalid")
        alias = self.database.upsert_alias(
            email=email,
            remote_metadata=None,
            label=label or email,
            note=note,
            sender_filter=sender_filter,
            state=state,
        )
        if access_key and state == "active":
            self.database.import_access_key(alias["id"], access_key)
            alias = self.database.get_alias(alias["id"])
        elif state == "inactive":
            with suppress(Exception):
                self.database.revoke_access_key(alias["id"])
            alias = self.database.get_alias(alias["id"])
        self.database.record_audit_event(
            "control_register",
            "upserted",
            alias_id=str(alias["id"]),
        )
        if alias_id:
            alias = {**alias, "external_id": str(alias_id)}
        return alias

    def register_control_access_key_by_email(self, email: str, access_key: str) -> IssuedAccessKey:
        alias = self.database.upsert_alias(
            email=email,
            remote_metadata=None,
            label=email,
            note="",
            sender_filter="",
            state="active",
        )
        issued = self.database.import_access_key(alias["id"], access_key)
        self.database.record_audit_event(
            "control_register",
            "key_imported",
            alias_id=str(alias["id"]),
        )
        return issued

    def register_control_state_by_email(self, email: str, state: str) -> dict[str, Any]:
        alias = self.database.upsert_alias(
            email=email,
            remote_metadata=None,
            label=email,
            note="",
            sender_filter="",
            state=state,
        )
        if state == "inactive":
            with suppress(Exception):
                self.database.revoke_access_key(alias["id"])
        alias = self.database.get_alias(alias["id"])
        self.database.record_audit_event(
            "control_register",
            f"state_{state}",
            alias_id=str(alias["id"]),
        )
        return alias

    def register_control_delete_by_email(self, email: str) -> None:
        # Upsert-less delete: list and match email.
        target = None
        needle = str(email or "").strip().casefold()
        for item in self.database.list_aliases():
            if str(item.get("email") or "").casefold() == needle:
                target = item
                break
        if target is None:
            raise NotFoundError("alias not found")
        alias_id = str(target["id"])
        # Audit first: aliases.id is a foreign key for audit events.
        self.database.record_audit_event(
            "control_register",
            "deleted",
            alias_id=alias_id,
        )
        self.database.delete_alias(alias_id)

    def issue_access_key(self, alias_id: str) -> IssuedAccessKey:
        if self.settings.is_edge:
            raise GatewayNotAllowedError("edge mode only imports keys from the control plane")
        if self._remote_write_active:
            raise GatewayBusyError("alias lifecycle operation is in progress")
        with self._hme_lock:
            if self._remote_write_active:
                raise GatewayBusyError("alias lifecycle operation is in progress")
            issued = self.database.issue_access_key(alias_id)
            self.database.record_audit_event("access_key", "issued", alias_id=str(alias_id))
            alias = self.database.get_alias(alias_id)
            self._push_alias_to_edge(alias, access_key=issued.access_key, action="issue_key")
            return issued

    def push_all_access_keys_to_edge(self) -> dict[str, int]:
        """Reconcile every active local alias (with key when available) to the edge.

        Keyless active aliases are registered too so the edge alias set matches the
        local control plane; the edge keeps any access key it already holds when no
        key is supplied. Pushes run in parallel: each upsert is an independent,
        idempotent HTTP call, so ordering does not matter and a full reconcile of
        ~450 aliases finishes in seconds instead of minutes.
        """
        if self.edge_sync_client is None or not self.settings.edge_sync_enabled:
            raise GatewayNotAllowedError("edge sync is not enabled")
        if self.settings.is_edge:
            raise GatewayNotAllowedError("edge mode cannot push to itself")
        with self._edge_push_lock:
            skipped = 0
            payloads: list[tuple[dict[str, Any], str | None]] = []
            for alias in self.database.list_aliases():
                if str(alias.get("state") or "") != "active":
                    skipped += 1
                    continue
                access_key: str | None = None
                if bool(alias.get("has_access_key")):
                    # Hash-only legacy keys cannot be revealed; still register the
                    # alias and leave the key already stored on the edge untouched.
                    with suppress(Exception):
                        access_key = self.database.reveal_access_key(str(alias["id"]))
                payloads.append((alias, access_key))

            client = self.edge_sync_client

            def _push(entry: tuple[dict[str, Any], str | None]) -> None:
                alias, key = entry
                client.upsert_alias(
                    alias_id=str(alias["id"]),
                    email=str(alias.get("email") or ""),
                    label=str(alias.get("label") or alias.get("email") or ""),
                    note=str(alias.get("note") or ""),
                    sender_filter=str(alias.get("sender_filter") or ""),
                    state="active",
                    access_key=key,
                )

            succeeded = 0
            failed = 0
            if payloads:
                # Probe with a single push first: when the edge is unreachable we
                # fail fast instead of burning a timeout per alias.
                try:
                    _push(payloads[0])
                    succeeded += 1
                except EdgeSyncError as exc:
                    if exc.status_code is None:
                        failed = len(payloads)
                        self.database.record_audit_event(
                            "edge_sync", "backfill_failed", alias_id=str(payloads[0][0]["id"])
                        )
                        self.database.record_audit_event("edge_sync", "backfill_done")
                        return {
                            "succeeded": 0,
                            "failed": failed,
                            "skipped": skipped,
                            "total": failed + skipped,
                        }
                    failed += 1
                    self.database.record_audit_event(
                        "edge_sync", "backfill_failed", alias_id=str(payloads[0][0]["id"])
                    )
                except Exception:
                    failed += 1
                    self.database.record_audit_event(
                        "edge_sync", "backfill_failed", alias_id=str(payloads[0][0]["id"])
                    )
                with ThreadPoolExecutor(max_workers=8, thread_name_prefix="edge-push") as pool:
                    futures = {pool.submit(_push, entry): entry for entry in payloads[1:]}
                    for future in as_completed(futures):
                        alias, _key = futures[future]
                        try:
                            future.result()
                        except Exception:
                            failed += 1
                            self.database.record_audit_event(
                                "edge_sync", "backfill_failed", alias_id=str(alias["id"])
                            )
                        else:
                            succeeded += 1
            self.database.record_audit_event(
                "edge_sync",
                "backfill_done",
            )
        return {
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
            "total": succeeded + failed + skipped,
        }

    def _edge_reconcile_loop(self) -> None:
        """Background self-healing: keep the edge alias/key set converged to local."""
        # First pass shortly after startup so a restart heals missed pushes quickly.
        if self._stop_event.wait(30.0):
            return
        while True:
            with suppress(Exception):
                self.push_all_access_keys_to_edge()
            if self._stop_event.wait(max(300, int(self.settings.edge_reconcile_seconds))):
                return

    def reveal_access_key(self, alias_id: str) -> str:
        with self._hme_lock:
            access_key = self.database.reveal_access_key(alias_id)
            self.database.record_audit_event("access_key", "revealed", alias_id=str(alias_id))
            return access_key

    def revoke_access_key(self, alias_id: str) -> None:
        if self.settings.is_edge:
            raise GatewayNotAllowedError("edge mode only revokes keys via the control plane")
        with self._hme_lock:
            alias = self.database.get_alias(alias_id)
            self.database.revoke_access_key(alias_id)
            self.database.record_audit_event("access_key", "revoked", alias_id=str(alias_id))
            self._push_alias_to_edge(alias, action="revoke_key")

    def update_alias(
        self,
        alias_id: str,
        *,
        label: str,
        note: str,
        sender_filter: str,
    ) -> dict[str, Any]:
        alias = self.database.update_alias_configuration(
            alias_id,
            label=label,
            note=note,
            sender_filter=sender_filter,
        )
        self.database.record_audit_event("alias_config", "updated", alias_id=str(alias_id))
        return alias

    def update_alias_usage(self, alias_id: str, usage_label: str) -> dict[str, Any]:
        alias = self.database.update_alias_usage(alias_id, usage_label)
        outcome = "cleared" if not alias["usage_label"] else "updated"
        self.database.record_audit_event("alias_usage", outcome, alias_id=str(alias_id))
        return alias

    def refresh_usage_tags(self) -> dict[str, int]:
        config = self.get_imap_config()
        if config is None:
            raise GatewayNotConfiguredError("IMAP is not configured")
        aliases = self.database.list_aliases()
        stats = _refresh_usage_tags(
            config=config,
            aliases=aliases,
            updater=self.database.update_alias_usage,
        )
        self.database.record_audit_event("usage_tags", "refreshed")
        return stats

    def deactivate_alias(self, alias_id: str) -> dict[str, Any]:
        self._ensure_hme_fresh()
        self._begin_remote_write()
        client: HmeClient | None = None
        try:
            with self._hme_lock:
                _alias, anonymous_id = self._remote_alias(alias_id, state="active")
                client = self._hme_client()
            try:
                client.deactivate_alias(anonymous_id)
            except HmeSessionError:
                self._refresh_after_write_rejection()
            remote_aliases = self._confirmed_remote_aliases(
                client,
                anonymous_id,
                expected_active=False,
            )
            with self._hme_lock:
                self._reconcile_remote_aliases(remote_aliases)
                self._alias_generation += 1
                self.database.record_audit_event(
                    "alias_deactivate", "succeeded", alias_id=str(alias_id)
                )
                alias = self.database.get_alias(alias_id)
                self._push_alias_to_edge(alias)
                return alias
        finally:
            if client is not None:
                self._close_client(client)
            self._finish_remote_write()

    def reactivate_alias(self, alias_id: str) -> dict[str, Any]:
        self._ensure_hme_fresh()
        self._begin_remote_write()
        client: HmeClient | None = None
        try:
            with self._hme_lock:
                _alias, anonymous_id = self._remote_alias(alias_id, state="inactive")
                client = self._hme_client()
            try:
                client.reactivate_alias(anonymous_id)
            except HmeSessionError:
                self._refresh_after_write_rejection()
            remote_aliases = self._confirmed_remote_aliases(
                client,
                anonymous_id,
                expected_active=True,
            )
            with self._hme_lock:
                self._reconcile_remote_aliases(remote_aliases)
                self._alias_generation += 1
                self.database.record_audit_event(
                    "alias_reactivate", "succeeded", alias_id=str(alias_id)
                )
                alias = self.database.get_alias(alias_id)
                self._push_alias_to_edge(alias)
                return alias
        finally:
            if client is not None:
                self._close_client(client)
            self._finish_remote_write()

    def delete_alias(self, alias_id: str, *, confirmation: str) -> None:
        self._ensure_hme_fresh()
        self._begin_remote_write()
        client: HmeClient | None = None
        try:
            with self._hme_lock:
                alias, anonymous_id = self._remote_alias(alias_id, state="inactive")
                if str(confirmation or "").strip().casefold() != alias["email"].casefold():
                    raise ConflictError("alias deletion confirmation does not match")
                client = self._hme_client()
            try:
                client.delete_alias(anonymous_id)
            except HmeSessionError:
                self._refresh_after_write_rejection()
            remote_aliases = self._confirmed_remote_aliases(
                client,
                anonymous_id,
                expected_absent=True,
            )
            with self._hme_lock:
                self._reconcile_remote_aliases(remote_aliases)
                deleted = {"id": alias_id, "email": alias["email"]}
                self.database.delete_alias(alias_id)
                self._alias_generation += 1
                self.database.record_audit_event("alias_delete", "succeeded")
                self._push_alias_to_edge(deleted, action="delete")
        finally:
            if client is not None:
                self._close_client(client)
            self._finish_remote_write()

    def prepare_lookup(self, access_key: str, *, client_ip: str) -> PreparedLookup | None:
        """Charge the rate limiters once and resolve the key to its alias.

        Returns None for an unusable key, which the caller reports as
        `invalid_key`; both cases are already audited here.
        """
        self._require_public_otp()
        ip_key = str(client_ip or "unknown")
        ip_decision = self.rate_limiter.check("code-ip", ip_key, limit=30, window_seconds=60)
        if not ip_decision.allowed:
            raise GatewayRateLimitedError(ip_decision.retry_after)
        try:
            digest = hash_access_key(access_key)
        except SecurityError:
            invalid_decision = self.rate_limiter.check(
                "invalid-key-ip", ip_key, limit=10, window_seconds=300
            )
            if not invalid_decision.allowed:
                raise GatewayRateLimitedError(invalid_decision.retry_after) from None
            self._audit_lookup("invalid_key", client_ip=ip_key)
            return None
        key_identifier = digest.hex()
        key_decision = self.rate_limiter.check(
            "code-key", key_identifier, limit=12, window_seconds=60
        )
        if not key_decision.allowed:
            raise GatewayRateLimitedError(key_decision.retry_after)
        alias = self.database.find_alias_by_access_key_hash(digest)
        if alias is None:
            self._audit_lookup("invalid_key", client_ip=ip_key)
            return None
        if self.get_imap_config() is None:
            self._audit_lookup("not_configured", alias_id=str(alias["id"]), client_ip=ip_key)
            raise GatewayNotConfiguredError("IMAP is not configured")
        return PreparedLookup(
            alias_id=str(alias["id"]),
            email=str(alias["email"]),
            sender_filter=str(alias["sender_filter"] or ""),
            client_ip=ip_key,
        )

    def _found_result(self, code: str, received_at: datetime) -> CodeLookupResult:
        expires_at = received_at + timedelta(seconds=self.settings.otp_max_age_seconds)
        return CodeLookupResult(
            status="found",
            code=code,
            received_at=received_at.isoformat().replace("+00:00", "Z"),
            expires_at=expires_at.isoformat().replace("+00:00", "Z"),
        )

    def read_indexed_code(self, prepared: PreparedLookup) -> CodeLookupResult | None:
        """Answer from the warm mailbox index: no IMAP, no audit on a miss.

        A long poll calls this repeatedly, so misses stay silent; only the final
        give-up is audited by `note_lookup_exhausted`.
        """
        watcher = self.mailbox_watcher
        if not watcher.ready:
            return None
        found = watcher.latest(
            prepared.email,
            now_ts=float(self.clock()),
            max_age_seconds=self.settings.otp_max_age_seconds,
            future_skew_seconds=self.settings.otp_future_skew_seconds,
            sender_filter=prepared.sender_filter,
            sender_policy=PUBLIC_OTP_SENDER_POLICY,
        )
        if found is None:
            return None
        self._audit_lookup("found", alias_id=prepared.alias_id, client_ip=prepared.client_ip)
        return self._found_result(found.code, found.received_at_utc)

    def read_scanned_code(self, prepared: PreparedLookup) -> CodeLookupResult:
        """On-demand IMAP scan, used whenever the watcher is not warm."""
        config = self.get_imap_config()
        if config is None:
            self._audit_lookup(
                "not_configured", alias_id=prepared.alias_id, client_ip=prepared.client_ip
            )
            raise GatewayNotConfiguredError("IMAP is not configured")
        if not self._imap_slots.acquire(timeout=0.1):
            raise GatewayBusyError("IMAP reader is busy")
        try:
            result = self.imap_reader_factory(config).find_latest_code(
                prepared.email,
                now_ts=float(self.clock()),
                max_age_seconds=self.settings.otp_max_age_seconds,
                future_skew_seconds=self.settings.otp_future_skew_seconds,
                sender_filter=prepared.sender_filter,
                sender_policy=PUBLIC_OTP_SENDER_POLICY,
                timeout=self.settings.otp_request_timeout_seconds,
            )
        except ImapCredentialsError:
            self._audit_lookup(
                "imap_invalid", alias_id=prepared.alias_id, client_ip=prepared.client_ip
            )
            raise GatewayNotConfiguredError("IMAP is unavailable") from None
        except ImapError:
            self._audit_lookup(
                "imap_error", alias_id=prepared.alias_id, client_ip=prepared.client_ip
            )
            raise GatewayError("IMAP lookup failed") from None
        finally:
            self._imap_slots.release()
        if result is None:
            return self.note_lookup_exhausted(prepared)
        self._audit_lookup("found", alias_id=prepared.alias_id, client_ip=prepared.client_ip)
        return self._found_result(result.code, result.received_at)

    def note_lookup_exhausted(self, prepared: PreparedLookup) -> CodeLookupResult:
        self._audit_lookup("no_code", alias_id=prepared.alias_id, client_ip=prepared.client_ip)
        return CodeLookupResult(status="waiting", retry_after=5)

    def lookup_code(self, access_key: str, *, client_ip: str) -> CodeLookupResult:
        prepared = self.prepare_lookup(access_key, client_ip=client_ip)
        if prepared is None:
            return CodeLookupResult(status="invalid_key")
        indexed = self.read_indexed_code(prepared)
        if indexed is not None:
            return indexed
        if self.mailbox_watcher.ready:
            # The index is authoritative while warm; rescanning would just
            # re-read the same mailbox the watcher already ingested.
            return self.note_lookup_exhausted(prepared)
        return self.read_scanned_code(prepared)

    @property
    def admin_scan_timeout_seconds(self) -> int:
        """Upper bound on one admin IMAP scan.

        The HTTP handler must allow strictly more than this, or a tuned-down
        `otp_request_timeout_seconds` makes the request 503 while the scan is
        still running.
        """
        return max(12, min(25, int(self.settings.otp_request_timeout_seconds)))

    def _admin_code_payload(self, alias: Mapping[str, Any], *, code: str, received_at: datetime) -> dict[str, Any]:
        value = received_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return {
            "alias_id": alias["id"],
            "email": alias["email"],
            "label": alias["label"],
            "code": code,
            "received_at": value.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "received_at_display": value.astimezone(_BEIJING_TIMEZONE).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        }

    def admin_recent_codes(self, alias_ids: Sequence[str] | None = None) -> dict[str, Any]:
        self._require_admin_otp()
        config = self.get_imap_config()
        if config is None:
            self.database.record_audit_event("admin_code_scan", "not_configured")
            raise GatewayNotConfiguredError("IMAP is not configured")
        aliases = self.database.list_aliases()
        wanted_ids = tuple(
            dict.fromkeys(str(item or "").strip() for item in (alias_ids or ()) if str(item or "").strip())
        )
        if wanted_ids:
            wanted = set(wanted_ids)
            aliases = [item for item in aliases if str(item["id"]) in wanted]
            if not aliases:
                self.database.record_audit_event("admin_code_scan", "not_found")
                raise NotFoundError("alias not found")
        aliases_by_email = {item["email"].casefold(): item for item in aliases}
        if not aliases_by_email:
            return {"codes": [], "by_alias": {}, "scanned": 0, "truncated": False, "scope": "none"}

        # Local admin needs a wider window than the public 5-minute API: iCloud
        # forward to QQ can lag, and the operator often copies the mail a bit late.
        admin_max_age = max(int(self.settings.otp_max_age_seconds), 30 * 60)

        # While the watcher is warm every alias is answered from memory, so the
        # console can show codes for the whole list continuously instead of
        # making the operator pin one alias and wait on a scan.
        if self.mailbox_watcher.ready:
            now = float(self.clock())
            indexed = self.mailbox_watcher.snapshot(
                tuple(aliases_by_email),
                now_ts=now,
                max_age_seconds=admin_max_age,
                future_skew_seconds=self.settings.otp_future_skew_seconds,
            )
            codes: list[dict[str, Any]] = []
            by_alias: dict[str, dict[str, Any]] = {}
            for email, entry in indexed.items():
                alias = aliases_by_email.get(email)
                if alias is None:
                    continue
                payload = self._admin_code_payload(
                    alias, code=entry.code, received_at=entry.received_at_utc
                )
                codes.append(payload)
                by_alias[str(alias["id"])] = payload
            codes.sort(key=lambda item: item["received_at"], reverse=True)
            # Deliberately unaudited. The console refreshes this every few
            # seconds and it touches no mailbox, so recording it would add a row
            # per tick (tens of thousands a day with a tab left open) and drown
            # the real scan/lookup history it shares a table with.
            return {
                "codes": codes,
                "by_alias": by_alias,
                "scanned": len(aliases),
                "truncated": False,
                "scope": "single" if len(aliases) == 1 else "all",
                "alias_ids": [str(item["id"]) for item in aliases],
                "max_age_seconds": admin_max_age,
                "source": "watcher",
            }

        # Positive cache only. Never cache empty results — that made the UI look
        # "stuck" with no code while a mail was already in the mailbox.
        if len(aliases) == 1:
            alias = aliases[0]
            alias_id = str(alias["id"])
            now = float(self.clock())
            with self._admin_code_cache_lock:
                cached = self._admin_code_cache.get(alias_id)
            # 4s TTL coalesces concurrent tabs polling the same alias without
            # noticeably delaying a fresh code (poll interval is 5s anyway).
            if cached is not None and (now - cached[0]) <= 4.0 and cached[1]:
                payload = cached[1]
                return {
                    "codes": [payload],
                    "by_alias": {alias_id: payload},
                    "scanned": 0,
                    "truncated": False,
                    "scope": "single",
                    "alias_ids": [alias_id],
                    "cached": True,
                }

        if not self._admin_imap_slots.acquire(timeout=0.1):
            self.database.record_audit_event("admin_code_scan", "busy")
            raise GatewayBusyError("IMAP reader is busy")
        scanned = 0
        truncated = False
        codes = []
        by_alias = {}
        try:
            reader = self.imap_reader_factory(config)
            if len(aliases) == 1:
                alias = aliases[0]
                alias_id = str(alias["id"])
                now = float(self.clock())
                result = reader.find_latest_code(
                    alias["email"],
                    now_ts=now,
                    max_age_seconds=admin_max_age,
                    future_skew_seconds=self.settings.otp_future_skew_seconds,
                    sender_filter="",  # local UI should never hide a code via sender filter
                    timeout=max(6, min(15, int(self.settings.otp_request_timeout_seconds))),
                )
                scanned = 1 if result is not None else 0
                if result is not None:
                    payload = self._admin_code_payload(
                        alias, code=result.code, received_at=result.received_at
                    )
                    codes.append(payload)
                    by_alias[alias_id] = payload
                    with self._admin_code_cache_lock:
                        self._admin_code_cache[alias_id] = (now, payload)
            else:
                batch = reader.find_recent_codes(
                    tuple(aliases_by_email),
                    now_ts=float(self.clock()),
                    max_age_seconds=admin_max_age,
                    future_skew_seconds=self.settings.otp_future_skew_seconds,
                    timeout=self.admin_scan_timeout_seconds,
                    scan_limit=120,
                    result_limit=80,
                )
                scanned = batch.scanned
                truncated = batch.truncated
                for item in batch.items:
                    alias = aliases_by_email.get(item.alias.casefold())
                    if alias is None:
                        continue
                    payload = self._admin_code_payload(
                        alias, code=item.code, received_at=item.received_at
                    )
                    codes.append(payload)
                    by_alias.setdefault(str(alias["id"]), payload)
        except ImapCredentialsError:
            self.database.record_audit_event("admin_code_scan", "imap_invalid")
            raise GatewayNotConfiguredError("IMAP is unavailable") from None
        except ImapError:
            self.database.record_audit_event("admin_code_scan", "imap_error")
            raise GatewayError("IMAP lookup failed") from None
        finally:
            self._admin_imap_slots.release()

        outcome = "truncated" if truncated else ("found" if codes else "empty")
        self.database.record_audit_event("admin_code_scan", outcome)
        return {
            "codes": codes,
            "by_alias": by_alias,
            "scanned": scanned,
            "truncated": truncated,
            "scope": "single" if len(aliases) == 1 else "all",
            "alias_ids": [str(item["id"]) for item in aliases],
            "max_age_seconds": admin_max_age,
        }


    def dashboard(self) -> dict[str, Any]:
        hme_session: ICloudHmeSession | None
        imap_config: ImapConfig | None
        hme_error = ""
        imap_error = ""
        try:
            hme_session = self.get_hme_session()
        except (HmeSessionError, SecurityError):
            hme_session = None
            hme_error = "saved HME session cannot be loaded"
        try:
            imap_config = self.get_imap_config()
        except (ImapCredentialsError, SecurityError):
            imap_config = None
            imap_error = "saved IMAP configuration cannot be loaded"
        aliases = self.database.list_aliases()
        query_history = self.database.list_code_lookup_events(limit=100)
        for event in query_history:
            label, tone = _LOOKUP_OUTCOME_VIEW.get(event["outcome"], ("查询失败", "error"))
            event["outcome_label"] = label
            event["outcome_tone"] = tone
            event["created_at_display"] = _beijing_timestamp(event["created_at"])
        return {
            "deployment_mode": self.settings.deployment_mode,
            "edge_sync": {
                "enabled": bool(
                    self.settings.edge_sync_enabled and self.edge_sync_client is not None
                ),
                "edge_base_url": self.settings.edge_base_url or None,
            },
            "hme": {
                **self.hme_status(),
                "host": None if hme_session is None else hme_session.host,
                "error": hme_error,
            },
            "local_runtime": {
                "data_dir": str(self.settings.data_dir),
                "database_path": str(self.settings.database_path),
                "browser_profile_dir": (
                    None
                    if self.settings.browser_profile_dir is None
                    else str(self.settings.browser_profile_dir)
                ),
                "admin_open": bool(self.settings.admin_open),
                "capture_configured": bool(self.settings.capture_configured),
            },
            "imap": {
                "configured": imap_config is not None,
                "host": None if imap_config is None else imap_config.host,
                "forwarding_email": (None if imap_config is None else imap_config.forwarding_email),
                "folder": None if imap_config is None else imap_config.folder,
                "junk_folder": None if imap_config is None else imap_config.junk_folder,
                "error": imap_error,
            },
            "capture": self.capture_manager.status(),
            "aliases": aliases,
            "counts": {
                "total": len(aliases),
                "active": sum(item["state"] == "active" for item in aliases),
                "keyed": sum(item["has_access_key"] for item in aliases),
            },
            "query_history": query_history,
            "query_counts": {
                "shown": len(query_history),
                "aliases": len(
                    {
                        event["alias_email"]
                        for event in query_history
                        if event["alias_email"] is not None
                    }
                ),
            },
            "audit": self.database.list_audit_events(limit=40),
        }

    def _audit_lookup(
        self,
        outcome: str,
        *,
        client_ip: str,
        alias_id: str | None = None,
    ) -> None:
        ip_digest = self.secret_box.digest(client_ip, "audit-ip").hex()[:16]
        self.database.record_audit_event(
            "code_lookup",
            outcome,
            alias_id=alias_id,
            ip_digest=ip_digest,
        )


__all__ = [
    "AliasBatchItemResult",
    "AliasBatchResult",
    "BulkAliasActionResult",
    "CodeLookupResult",
    "CreatedAlias",
    "GatewayBusyError",
    "GatewayEdgeSyncError",
    "GatewayNotAllowedError",
    "GatewayError",
    "GatewayNotConfiguredError",
    "GatewayRateLimitedError",
    "GatewayRetryableError",
    "GatewayStoppingError",
    "GatewayService",
]
