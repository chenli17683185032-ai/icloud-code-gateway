from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .browser_capture import CaptureManager
from .config import Settings
from .database import ConflictError, Database, IssuedAccessKey
from .hme import (
    HmeClient,
    HmeError,
    HmeSessionError,
    ICloudHmeSession,
    parse_hme_session_import,
)
from .imap_otp import (
    ImapConfig,
    ImapCredentialsError,
    ImapError,
    ImapOtpReader,
)
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


@dataclass(frozen=True)
class CodeLookupResult:
    status: str
    code: str = ""
    received_at: str | None = None
    expires_at: str | None = None
    retry_after: int | None = None


@dataclass(frozen=True)
class CreatedAlias:
    alias: dict[str, Any]
    issued_key: IssuedAccessKey


@dataclass(frozen=True)
class AliasBatchResult:
    requested_count: int
    created: tuple[CreatedAlias, ...]
    error_code: str | None = None


HmeClientFactory = Callable[[ICloudHmeSession], HmeClient]
ImapReaderFactory = Callable[[ImapConfig], ImapOtpReader]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp() -> str:
    return _utc_now().isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
        imap_reader_factory: ImapReaderFactory = ImapOtpReader,
        rate_limiter: SlidingWindowRateLimiter | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], Any] = time.sleep,
    ) -> None:
        self.settings = settings
        self.secret_box = SecretBox(settings.master_key)
        self.database = database or Database(settings.database_path, self.secret_box)
        self.database.initialize()
        self.hme_client_factory = hme_client_factory or (
            lambda session: HmeClient(session, proxy=settings.hme_proxy)
        )
        self.imap_reader_factory = imap_reader_factory
        self.rate_limiter = rate_limiter or SlidingWindowRateLimiter()
        self.clock = clock
        self.sleeper = sleeper
        self._hme_lock = threading.RLock()
        self._imap_slots = threading.BoundedSemaphore(4)
        self.capture_manager = CaptureManager(
            cdp_url=settings.cdp_url,
            on_session=self.save_hme_session,
            get_session_template=self.get_hme_session,
            timeout_seconds=settings.capture_timeout_seconds,
        )

    def shutdown(self) -> None:
        self.capture_manager.shutdown(timeout=10.0)
        self.database.close()

    def get_hme_session(self) -> ICloudHmeSession | None:
        value = self.database.get_secret(HME_SETTING_KEY)
        return None if value is None else ICloudHmeSession.from_mapping(value)

    def save_hme_session(self, session: ICloudHmeSession) -> int:
        with self._hme_lock:
            client = self.hme_client_factory(session)
            aliases = self._validated_remote_aliases(client.list_aliases())
            self.database.set_secret(HME_SETTING_KEY, session.as_secret_dict())
            self._reconcile_remote_aliases(aliases)
            self.database.record_audit_event("hme_session", "saved")
            return len(aliases)

    def import_hme_session(self, source: str) -> int:
        session = parse_hme_session_import(source)
        return self.save_hme_session(session)

    def sync_aliases(self) -> list[dict[str, Any]]:
        with self._hme_lock:
            client = self._hme_client()
            remote_aliases = self._validated_remote_aliases(client.list_aliases())
            self._reconcile_remote_aliases(remote_aliases)
            self.database.record_audit_event("hme_sync", "succeeded")
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
        self.database.finish_remote_sync(seen, synced_at=synced_at)

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
        bounded_count = max(1, min(int(count), 5))
        prefix = str(label_prefix or "").strip()
        if not prefix or len(prefix) > 140:
            raise ValueError("label prefix is invalid")
        with self._hme_lock:
            session = self.get_hme_session()
            if session is None:
                raise GatewayNotConfiguredError("iCloud HME session is not configured")
            client = self.hme_client_factory(session)
            created: list[CreatedAlias] = []
            for index in range(bounded_count):
                try:
                    label = prefix if bounded_count == 1 else f"{prefix} {index + 1}"
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
                    created.append(CreatedAlias(alias=alias, issued_key=issued))
                    self.database.record_audit_event(
                        "alias_create", "succeeded", alias_id=alias["id"]
                    )
                except Exception:
                    self.database.record_audit_event("alias_create", "failed")
                    if not created:
                        raise
                    return AliasBatchResult(
                        requested_count=bounded_count,
                        created=tuple(created),
                        error_code="batch_stopped",
                    )
                if index + 1 < bounded_count:
                    self.sleeper(2.0)
            return AliasBatchResult(
                requested_count=bounded_count,
                created=tuple(created),
            )

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
        return config

    def get_imap_config(self) -> ImapConfig | None:
        value = self.database.get_secret(IMAP_SETTING_KEY)
        return None if value is None else ImapConfig.from_mapping(value)

    def test_imap(self) -> None:
        config = self.get_imap_config()
        if config is None:
            raise GatewayNotConfiguredError("IMAP is not configured")
        self.imap_reader_factory(config).check(timeout=self.settings.otp_request_timeout_seconds)

    def issue_access_key(self, alias_id: str) -> IssuedAccessKey:
        with self._hme_lock:
            issued = self.database.issue_access_key(alias_id)
            self.database.record_audit_event("access_key", "issued", alias_id=str(alias_id))
            return issued

    def reveal_access_key(self, alias_id: str) -> str:
        with self._hme_lock:
            access_key = self.database.reveal_access_key(alias_id)
            self.database.record_audit_event("access_key", "revealed", alias_id=str(alias_id))
            return access_key

    def revoke_access_key(self, alias_id: str) -> None:
        with self._hme_lock:
            self.database.revoke_access_key(alias_id)
            self.database.record_audit_event("access_key", "revoked", alias_id=str(alias_id))

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

    def deactivate_alias(self, alias_id: str) -> dict[str, Any]:
        with self._hme_lock:
            _alias, anonymous_id = self._remote_alias(alias_id, state="active")
            client = self._hme_client()
            client.deactivate_alias(anonymous_id)
            remote_aliases = self._confirmed_remote_aliases(
                client,
                anonymous_id,
                expected_active=False,
            )
            self._reconcile_remote_aliases(remote_aliases)
            self.database.record_audit_event(
                "alias_deactivate", "succeeded", alias_id=str(alias_id)
            )
            return self.database.get_alias(alias_id)

    def reactivate_alias(self, alias_id: str) -> dict[str, Any]:
        with self._hme_lock:
            _alias, anonymous_id = self._remote_alias(alias_id, state="inactive")
            client = self._hme_client()
            client.reactivate_alias(anonymous_id)
            remote_aliases = self._confirmed_remote_aliases(
                client,
                anonymous_id,
                expected_active=True,
            )
            self._reconcile_remote_aliases(remote_aliases)
            self.database.record_audit_event(
                "alias_reactivate", "succeeded", alias_id=str(alias_id)
            )
            return self.database.get_alias(alias_id)

    def delete_alias(self, alias_id: str, *, confirmation: str) -> None:
        with self._hme_lock:
            alias, anonymous_id = self._remote_alias(alias_id, state="inactive")
            if str(confirmation or "").strip().casefold() != alias["email"].casefold():
                raise ConflictError("alias deletion confirmation does not match")
            client = self._hme_client()
            client.delete_alias(anonymous_id)
            remote_aliases = self._confirmed_remote_aliases(
                client,
                anonymous_id,
                expected_absent=True,
            )
            self._reconcile_remote_aliases(remote_aliases)
            self.database.delete_alias(alias_id)
            self.database.record_audit_event("alias_delete", "succeeded")

    def lookup_code(self, access_key: str, *, client_ip: str) -> CodeLookupResult:
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
            return CodeLookupResult(status="invalid_key")
        key_identifier = digest.hex()
        key_decision = self.rate_limiter.check(
            "code-key", key_identifier, limit=12, window_seconds=60
        )
        if not key_decision.allowed:
            raise GatewayRateLimitedError(key_decision.retry_after)
        alias = self.database.find_alias_by_access_key_hash(digest)
        if alias is None:
            self._audit_lookup("invalid_key", client_ip=ip_key)
            return CodeLookupResult(status="invalid_key")
        config = self.get_imap_config()
        if config is None:
            self._audit_lookup("not_configured", alias_id=alias["id"], client_ip=ip_key)
            raise GatewayNotConfiguredError("IMAP is not configured")
        if not self._imap_slots.acquire(timeout=0.1):
            raise GatewayBusyError("IMAP reader is busy")
        try:
            result = self.imap_reader_factory(config).find_latest_code(
                alias["email"],
                now_ts=float(self.clock()),
                max_age_seconds=self.settings.otp_max_age_seconds,
                future_skew_seconds=self.settings.otp_future_skew_seconds,
                sender_filter=alias["sender_filter"],
                timeout=self.settings.otp_request_timeout_seconds,
            )
        except ImapCredentialsError:
            self._audit_lookup("imap_invalid", alias_id=alias["id"], client_ip=ip_key)
            raise GatewayNotConfiguredError("IMAP is unavailable") from None
        except ImapError:
            self._audit_lookup("imap_error", alias_id=alias["id"], client_ip=ip_key)
            raise GatewayError("IMAP lookup failed") from None
        finally:
            self._imap_slots.release()
        if result is None:
            self._audit_lookup("no_code", alias_id=alias["id"], client_ip=ip_key)
            return CodeLookupResult(status="waiting", retry_after=5)
        expires_at = result.received_at + timedelta(seconds=self.settings.otp_max_age_seconds)
        self._audit_lookup("found", alias_id=alias["id"], client_ip=ip_key)
        return CodeLookupResult(
            status="found",
            code=result.code,
            received_at=result.received_at.isoformat().replace("+00:00", "Z"),
            expires_at=expires_at.isoformat().replace("+00:00", "Z"),
        )

    def admin_recent_codes(self) -> dict[str, Any]:
        config = self.get_imap_config()
        if config is None:
            self.database.record_audit_event("admin_code_scan", "not_configured")
            raise GatewayNotConfiguredError("IMAP is not configured")
        aliases = self.database.list_aliases()
        aliases_by_email = {item["email"].casefold(): item for item in aliases}
        if not self._imap_slots.acquire(timeout=0.1):
            self.database.record_audit_event("admin_code_scan", "busy")
            raise GatewayBusyError("IMAP reader is busy")
        try:
            batch = self.imap_reader_factory(config).find_recent_codes(
                tuple(aliases_by_email),
                now_ts=float(self.clock()),
                max_age_seconds=self.settings.otp_max_age_seconds,
                future_skew_seconds=self.settings.otp_future_skew_seconds,
                timeout=self.settings.otp_request_timeout_seconds,
            )
        except ImapCredentialsError:
            self.database.record_audit_event("admin_code_scan", "imap_invalid")
            raise GatewayNotConfiguredError("IMAP is unavailable") from None
        except ImapError:
            self.database.record_audit_event("admin_code_scan", "imap_error")
            raise GatewayError("IMAP lookup failed") from None
        finally:
            self._imap_slots.release()

        codes: list[dict[str, Any]] = []
        for item in batch.items:
            alias = aliases_by_email.get(item.alias.casefold())
            if alias is None:
                continue
            received_at = item.received_at
            if received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=UTC)
            codes.append(
                {
                    "alias_id": alias["id"],
                    "email": alias["email"],
                    "label": alias["label"],
                    "code": item.code,
                    "received_at": received_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                    "received_at_display": received_at.astimezone(_BEIJING_TIMEZONE).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                }
            )
        outcome = "truncated" if batch.truncated else ("found" if codes else "empty")
        self.database.record_audit_event("admin_code_scan", outcome)
        return {
            "codes": codes,
            "scanned": batch.scanned,
            "truncated": batch.truncated,
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
            "hme": {
                "configured": hme_session is not None,
                "host": None if hme_session is None else hme_session.host,
                "error": hme_error,
            },
            "imap": {
                "configured": imap_config is not None,
                "host": None if imap_config is None else imap_config.host,
                "forwarding_email": (None if imap_config is None else imap_config.forwarding_email),
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
    "AliasBatchResult",
    "CodeLookupResult",
    "CreatedAlias",
    "GatewayBusyError",
    "GatewayError",
    "GatewayNotConfiguredError",
    "GatewayRateLimitedError",
    "GatewayService",
]
