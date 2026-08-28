from __future__ import annotations

import fcntl
import hashlib
import json
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, BinaryIO

from .database import ConflictError, Database, NotFoundError
from .hme import (
    HmeError,
    HmeNetworkError,
    HmeRateLimitedError,
    HmeSessionError,
    ICloudHmeSession,
)
from .service import GatewayError, GatewayRetryableError, GatewayStoppingError

TERMINAL_JOB_STATUSES = {
    "completed",
    "partial",
    "failed",
    "needs_reconcile",
    "cancelled",
}
DEFAULT_HME_RATE_LIMIT_COOLDOWN_SECONDS = 30 * 60
DEFAULT_TRANSIENT_RETRY_BASE_SECONDS = 15
DEFAULT_TRANSIENT_RETRY_MAX_SECONDS = 15 * 60
_INTERNAL_RESULT_FIELDS = {
    "candidate",
    "reconcile_before_reserve",
    "transient_retry_attempts",
}


def request_fingerprint(kind: str, payload: Mapping[str, Any]) -> bytes:
    canonical = json.dumps(
        {"kind": kind, "payload": dict(payload)},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).digest()


class BatchJobManager:
    def __init__(
        self,
        gateway: Any,
        *,
        throttle_seconds: float = 2.0,
        rate_limit_cooldown_seconds: float | None = None,
        transient_retry_base_seconds: float = DEFAULT_TRANSIENT_RETRY_BASE_SECONDS,
        transient_retry_max_seconds: float | None = None,
    ) -> None:
        self.gateway = gateway
        self.database: Database = gateway.database
        self.throttle_seconds = max(0.0, float(throttle_seconds))
        if rate_limit_cooldown_seconds is None:
            configured_cooldown = getattr(
                getattr(gateway, "settings", None),
                "hme_create_cooldown_seconds",
                DEFAULT_HME_RATE_LIMIT_COOLDOWN_SECONDS,
            )
            self.rate_limit_cooldown_seconds = max(0.0, float(configured_cooldown))
        else:
            self.rate_limit_cooldown_seconds = max(0.0, float(rate_limit_cooldown_seconds))
        self.transient_retry_base_seconds = max(0.0, float(transient_retry_base_seconds))
        configured_retry_max = (
            getattr(getattr(gateway, "settings", None), "hme_retry_max_seconds", None)
            if transient_retry_max_seconds is None
            else transient_retry_max_seconds
        )
        if configured_retry_max is None:
            configured_retry_max = DEFAULT_TRANSIENT_RETRY_MAX_SECONDS
        self.transient_retry_max_seconds = max(
            self.transient_retry_base_seconds,
            float(configured_retry_max),
        )
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._owner_lock: BinaryIO | None = None
        self._owner = threading.Event()

    @property
    def owns_worker(self) -> bool:
        return self._owner.is_set()

    def start(self) -> None:
        if self._thread is not None:
            return
        lock_path = self.database.path.with_name(f"{self.database.path.name}.batch-worker.lock")
        self._owner_lock = lock_path.open("a+b")
        with suppress(OSError):
            lock_path.chmod(0o600)
        try:
            if self._try_acquire_owner():
                self.database.recover_interrupted_batch_jobs()
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="icloud-batch-jobs",
                daemon=True,
            )
            self._thread.start()
        except Exception:
            self._release_owner()
            raise
        self._wake.set()

    def request_stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def shutdown(self, *, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        self.request_stop()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(max(0.0, deadline - time.monotonic()))
        return thread is None or not thread.is_alive()

    def _try_acquire_owner(self) -> bool:
        if self._owner.is_set():
            return True
        if self._owner_lock is None:
            return False
        try:
            fcntl.flock(self._owner_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        self._owner.set()
        return True

    def _release_owner(self) -> None:
        owner_lock = self._owner_lock
        if owner_lock is None:
            return
        if self._owner.is_set():
            with suppress(OSError):
                fcntl.flock(owner_lock.fileno(), fcntl.LOCK_UN)
        self._owner.clear()
        with suppress(OSError):
            owner_lock.close()
        self._owner_lock = None

    def _stopping(self) -> bool:
        return self._stop.is_set() or bool(getattr(self.gateway, "stop_requested", False))

    def _raise_if_stopping(self) -> None:
        if self._stopping():
            raise GatewayStoppingError("batch worker is shutting down")

    def create_alias_job(
        self,
        *,
        count: int,
        label_prefix: str,
        note: str,
        sender_filter: str,
        idempotency_key: str | None,
    ) -> tuple[dict[str, Any], bool]:
        requested = int(count)
        if requested < 1 or requested > min(self.gateway.settings.alias_batch_limit, 100):
            raise ValueError("alias batch count is outside the configured limit")
        prefix = str(label_prefix or "").strip()
        if not prefix or len(prefix) > 140:
            raise ValueError("label prefix is invalid")
        payload = {
            "count": requested,
            "label_prefix": prefix,
            "note": str(note or ""),
            "sender_filter": str(sender_filter or ""),
        }
        items = [
            {
                "label": prefix if requested == 1 else f"{prefix} {index}",
                "note": payload["note"],
                "sender_filter": payload["sender_filter"],
            }
            for index in range(1, requested + 1)
        ]
        job, created = self.database.create_batch_job(
            kind="create_aliases",
            action="create",
            fingerprint=request_fingerprint("create_aliases", payload),
            items=items,
            idempotency_key=idempotency_key,
        )
        if created:
            self._wake.set()
        return job, created

    def create_bulk_job(
        self,
        *,
        action: str,
        alias_ids: list[str] | tuple[str, ...],
        confirmed: bool,
        idempotency_key: str | None,
    ) -> tuple[dict[str, Any], bool]:
        ids = [str(alias_id).strip() for alias_id in alias_ids]
        if not ids or len(ids) > 100 or any(not alias_id for alias_id in ids):
            raise ValueError("alias IDs are invalid")
        if len(set(ids)) != len(ids):
            raise ValueError("alias IDs must be unique")
        if action not in {"issue_keys", "reveal_keys", "deactivate", "delete"}:
            raise ValueError("bulk alias action is invalid")
        if action in {"deactivate", "delete"} and not confirmed:
            raise ValueError("bulk alias action requires confirmation")
        if action in {"deactivate", "delete"}:
            required_state = "active" if action == "deactivate" else "inactive"
            try:
                aliases = [self.database.get_alias(alias_id) for alias_id in ids]
            except NotFoundError as exc:
                raise ValueError("bulk alias contains an unknown ID") from exc
            if any(alias["state"] != required_state for alias in aliases):
                raise ValueError(f"bulk {action} requires {required_state} aliases")
        payload = {"action": action, "alias_ids": ids, "confirmed": bool(confirmed)}
        job, created = self.database.create_batch_job(
            kind="bulk_aliases",
            action=action,
            fingerprint=request_fingerprint("bulk_aliases", payload),
            items=[{"alias_id": alias_id} for alias_id in ids],
            idempotency_key=idempotency_key,
        )
        if created:
            self._wake.set()
        return job, created

    def public_job(self, job_id: str, *, reveal_keys: bool = False) -> dict[str, Any]:
        return self._public_job(self.database.get_batch_job(job_id), reveal_keys=reveal_keys)

    def active_jobs(self) -> list[dict[str, Any]]:
        return [
            self._public_job(job, reveal_keys=False)
            for job in self.database.list_active_batch_jobs()
        ]

    def _public_job(self, job: Mapping[str, Any], *, reveal_keys: bool) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for item in job["items"]:
            value: dict[str, Any] = {
                "index": item["index"],
                "id": item["alias_id"],
                "stage": item["stage"],
                "status": item["status"],
            }
            item_error = self._public_item_error(item)
            if item_error is not None:
                value["error"] = item_error
            result = item.get("result")
            if isinstance(result, Mapping):
                value.update(
                    {
                        key: entry
                        for key, entry in result.items()
                        if key not in _INTERNAL_RESULT_FIELDS
                    }
                )
            alias_id = value.get("id")
            if (
                reveal_keys
                and alias_id
                and item["status"] == "success"
                and (
                    job["kind"] == "create_aliases"
                    or job["action"] in {"issue_keys", "reveal_keys"}
                )
            ):
                try:
                    alias = self.database.get_alias(str(alias_id))
                    value.update(email=alias["email"], label=alias["label"])
                    value["access_key"] = self.database.reveal_access_key(str(alias_id))
                except (NotFoundError, ConflictError):
                    value["key_status"] = "unavailable"
            results.append(value)
        public = {
            "job_id": job["id"],
            "kind": job["kind"],
            "action": job["action"],
            "status": job["status"],
            "requested": job["requested"],
            "succeeded": job["succeeded"],
            "failed": job["failed"],
            "current": job["current"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "error": job["error"],
            "results": results,
            "public_url": self.gateway.settings.public_base_url,
        }
        wait = self._wait_public_fields(job)
        if wait is not None:
            public.update(wait)
        return public

    @staticmethod
    def _public_item_error(item: Mapping[str, Any]) -> str | None:
        if not item.get("error"):
            return None
        if item.get("status") == "unknown":
            if str(item.get("error")) == "interrupted during an in-flight operation":
                return "interrupted_in_flight"
            return "remote_write_unknown"
        if str(item.get("error") or "").startswith("rate_limited"):
            return "rate_limited"
        if str(item.get("error") or "").startswith("retryable:"):
            return "retrying"
        if item.get("error") in {"not_found", "conflict"}:
            return str(item["error"])
        if str(item.get("error")) == "remote write was not attempted":
            return "remote_write_not_attempted"
        return "operation_failed"

    def _wait_public_fields(self, job: Mapping[str, Any]) -> dict[str, Any] | None:
        for item in job.get("items") or []:
            result = item.get("result")
            if not isinstance(result, Mapping):
                continue
            wait_reason = str(result.get("wait_reason") or "")
            if wait_reason not in {"rate_limited", "transient_error"}:
                continue
            if item.get("status") != "queued":
                continue
            resume_at = str(result.get("resume_at") or "").strip()
            remaining = self._seconds_until(resume_at)
            public = {
                "wait_reason": wait_reason,
                "resume_at": resume_at or None,
                "retry_after_seconds": max(0, remaining if remaining is not None else 0),
            }
            if wait_reason == "rate_limited":
                public["cooldown_code"] = str(result.get("code") or "-41015")
            else:
                public["retry_kind"] = str(result.get("retry_kind") or "network")
                try:
                    retry_attempts = int(result.get("transient_retry_attempts") or 1)
                except (TypeError, ValueError):
                    retry_attempts = 1
                public["retry_attempts"] = max(1, retry_attempts)
            return public
        return None

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _seconds_until(self, value: str | None) -> int | None:
        parsed = self._parse_timestamp(value)
        if parsed is None:
            return None
        return max(0, int((parsed - datetime.now(UTC)).total_seconds()))

    def _format_timestamp(self, when: datetime) -> str:
        return when.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _rate_limit_resume_at(self, *, seconds: float | None = None) -> str:
        delay = max(
            0.0,
            float(self.rate_limit_cooldown_seconds if seconds is None else seconds),
        )
        return self._format_timestamp(datetime.now(UTC) + timedelta(seconds=delay))

    def _item_is_cooling_down(self, item: Mapping[str, Any]) -> bool:
        result = item.get("result")
        if not isinstance(result, Mapping):
            return False
        if str(result.get("wait_reason") or "") != "rate_limited":
            return False
        # Cooldown disabled globally: ignore any previously parked resume_at.
        if self.rate_limit_cooldown_seconds <= 0:
            return False
        remaining = self._seconds_until(str(result.get("resume_at") or ""))
        return remaining is not None and remaining > 0

    def _clear_rate_limit_wait(self, job_id: str, item: Mapping[str, Any]) -> Mapping[str, Any]:
        result = item.get("result")
        if not isinstance(result, Mapping):
            return item
        if str(result.get("wait_reason") or "") != "rate_limited":
            return item
        cleaned = {
            key: value
            for key, value in result.items()
            if key not in {"wait_reason", "resume_at", "code", "retry_after_seconds"}
        }
        # Keep any generated candidate across the cooldown window.
        # Always write a result payload so COALESCE does not leave wait metadata behind.
        self.database.update_batch_item(
            job_id,
            int(item["index"]),
            stage="generated" if cleaned.get("candidate") else "queued",
            status="queued",
            result=cleaned,
            error=None,
        )
        refreshed = self.database.get_batch_job(job_id)
        for entry in refreshed["items"]:
            if int(entry["index"]) == int(item["index"]):
                return entry
        return item

    def _item_is_retrying(self, item: Mapping[str, Any]) -> bool:
        result = item.get("result")
        if not isinstance(result, Mapping):
            return False
        if str(result.get("wait_reason") or "") != "transient_error":
            return False
        remaining = self._seconds_until(str(result.get("resume_at") or ""))
        return remaining is not None and remaining > 0

    def _clear_transient_wait(self, job_id: str, item: Mapping[str, Any]) -> Mapping[str, Any]:
        result = item.get("result")
        if not isinstance(result, Mapping):
            return item
        if str(result.get("wait_reason") or "") != "transient_error":
            return item
        cleaned = {
            key: value
            for key, value in result.items()
            if key not in {"wait_reason", "resume_at", "retry_kind", "retry_after_seconds"}
        }
        if cleaned.get("reconcile_before_reserve"):
            stage = "reconciling"
        elif cleaned.get("candidate"):
            stage = "generated"
        else:
            stage = "queued"
        self.database.update_batch_item(
            job_id,
            int(item["index"]),
            stage=stage,
            status="queued",
            result=cleaned,
            error=None,
        )
        refreshed = self.database.get_batch_job(job_id)
        for entry in refreshed["items"]:
            if int(entry["index"]) == int(item["index"]):
                return entry
        return item

    def _transient_retry_delay(self, attempt: int) -> float:
        if self.transient_retry_base_seconds <= 0:
            return 0.0
        exponent = min(max(0, int(attempt) - 1), 10)
        return min(
            self.transient_retry_max_seconds,
            self.transient_retry_base_seconds * (2**exponent),
        )

    def _pause_for_transient_retry(
        self,
        *,
        job_id: str,
        item: Mapping[str, Any],
        retry_kind: str,
        reconcile_before_reserve: bool = False,
    ) -> None:
        index = int(item["index"])
        saved_result = (
            dict(item.get("result") or {}) if isinstance(item.get("result"), Mapping) else {}
        )
        for key in ("wait_reason", "resume_at", "retry_kind", "retry_after_seconds"):
            saved_result.pop(key, None)
        attempts = max(0, int(saved_result.get("transient_retry_attempts") or 0)) + 1
        delay = self._transient_retry_delay(attempts)
        resume_at = self._rate_limit_resume_at(seconds=delay)
        saved_result.update(
            {
                "wait_reason": "transient_error",
                "resume_at": resume_at,
                "retry_kind": str(retry_kind or "network"),
                "retry_after_seconds": int(delay),
                "transient_retry_attempts": attempts,
            }
        )
        if reconcile_before_reserve:
            saved_result["reconcile_before_reserve"] = True
        stage = (
            "waiting_reconcile" if saved_result.get("reconcile_before_reserve") else "waiting_retry"
        )
        self.database.update_batch_item(
            job_id,
            index,
            stage=stage,
            status="queued",
            result=saved_result,
            error=f"retryable:{saved_result['retry_kind']}",
        )
        self.database.set_batch_job_status(
            job_id,
            "queued",
            error=f"temporary HME failure; retrying at {resume_at}",
        )
        self.database.record_audit_event("alias_create", "retrying")

    def _pause_for_rate_limit(
        self,
        *,
        job_id: str,
        item: Mapping[str, Any],
        code: str,
        retry_after_seconds: float | None = None,
    ) -> None:
        index = int(item["index"])
        saved_result = item.get("result") if isinstance(item.get("result"), Mapping) else {}
        delay = max(
            0.0,
            float(
                self.rate_limit_cooldown_seconds
                if retry_after_seconds is None
                else retry_after_seconds
            ),
        )
        resume_at = self._rate_limit_resume_at(seconds=delay)
        payload = {
            **dict(saved_result),
            "wait_reason": "rate_limited",
            "resume_at": resume_at,
            "code": str(code or "-41015"),
            "retry_after_seconds": int(delay),
        }
        self.database.update_batch_item(
            job_id,
            index,
            stage="waiting_quota",
            status="queued",
            result=payload,
            error=f"rate_limited:{payload['code']}",
        )
        if delay > 0:
            status_error = (
                f"Apple HME rate limited ({payload['code']}); cooling down until {resume_at}"
            )
        else:
            status_error = f"Apple HME rate limited ({payload['code']}); retrying without cooldown"
        self.database.set_batch_job_status(
            job_id,
            "queued",
            error=status_error,
        )
        self.database.record_audit_event("alias_create", "rate_limited")

    def _wait_for_resume(self, resume_at: str | None) -> bool:
        """Wait until resume_at or stop. Returns True if stop interrupted the wait."""
        while not self._stopping():
            remaining = self._seconds_until(resume_at)
            if remaining is None or remaining <= 0:
                return False
            if self._stop.wait(min(1.0, float(remaining))):
                return True
        return True

    def _worker_loop(self) -> None:
        recovered = self._owner.is_set()
        try:
            while not self._stop.is_set():
                if not self._owner.is_set():
                    if not self._try_acquire_owner():
                        self._wake.wait(timeout=0.25)
                        self._wake.clear()
                        continue
                    self.database.recover_interrupted_batch_jobs()
                    recovered = True
                if not recovered:
                    self.database.recover_interrupted_batch_jobs()
                    recovered = True
                job = self.database.next_batch_job()
                if job is None:
                    self._wake.wait(timeout=1.0)
                    self._wake.clear()
                    continue
                try:
                    self._run_job(job)
                except Exception:
                    with suppress(Exception):
                        self.database.set_batch_job_status(
                            job["id"], "failed", error="batch worker failed safely"
                        )
        finally:
            self._release_owner()

    def _run_job(self, job: Mapping[str, Any]) -> None:
        self.database.set_batch_job_status(job["id"], "running")
        index = 0
        while index < len(job["items"]):
            if self._stopping():
                self.database.set_batch_job_status(job["id"], "queued")
                return
            item = job["items"][index]
            if item["status"] != "queued":
                index += 1
                continue
            # Drop legacy cooldown metadata immediately when cooldown is disabled.
            if (
                self.rate_limit_cooldown_seconds <= 0
                and isinstance(item.get("result"), Mapping)
                and str(item["result"].get("wait_reason") or "") == "rate_limited"
            ):
                item = self._clear_rate_limit_wait(job["id"], item)
                self.database.set_batch_job_status(job["id"], "running", error=None)
                job = self.database.get_batch_job(job["id"])
            if self._item_is_cooling_down(item):
                result = item.get("result") if isinstance(item.get("result"), Mapping) else {}
                resume_at = str(result.get("resume_at") or "")
                self.database.set_batch_job_status(
                    job["id"],
                    "queued",
                    error=(
                        f"Apple HME rate limited ({result.get('code') or '-41015'}); "
                        f"cooling down until {resume_at}"
                    ),
                )
                if self._wait_for_resume(resume_at):
                    self.database.set_batch_job_status(job["id"], "queued")
                    return
                if self._stopping():
                    self.database.set_batch_job_status(job["id"], "queued")
                    return
                self.database.set_batch_job_status(job["id"], "running", error=None)
                item = self._clear_rate_limit_wait(job["id"], item)
                job = self.database.get_batch_job(job["id"])
                # Retry the same item after cooldown metadata is cleared.
                continue
            if self._item_is_retrying(item):
                result = item.get("result") if isinstance(item.get("result"), Mapping) else {}
                resume_at = str(result.get("resume_at") or "")
                self.database.set_batch_job_status(
                    job["id"],
                    "queued",
                    error=f"temporary HME failure; retrying at {resume_at}",
                )
                if self._wait_for_resume(resume_at):
                    self.database.set_batch_job_status(job["id"], "queued")
                    return
                if self._stopping():
                    self.database.set_batch_job_status(job["id"], "queued")
                    return
                self.database.set_batch_job_status(job["id"], "running", error=None)
                item = self._clear_transient_wait(job["id"], item)
                job = self.database.get_batch_job(job["id"])
                continue
            if (
                isinstance(item.get("result"), Mapping)
                and str(item["result"].get("wait_reason") or "") == "transient_error"
            ):
                self.database.set_batch_job_status(job["id"], "running", error=None)
                item = self._clear_transient_wait(job["id"], item)
                job = self.database.get_batch_job(job["id"])
                continue
            outcome = self._run_item(job, item)
            if outcome == "unknown":
                self.database.set_batch_job_status(
                    job["id"],
                    "needs_reconcile",
                    error="remote write outcome is unknown; automatic replay stopped",
                )
                return
            if outcome == "rate_limited":
                # Item is parked as queued + waiting_quota; wait then retry same index.
                refreshed = self.database.get_batch_job(job["id"])
                parked = next(
                    (
                        entry
                        for entry in refreshed["items"]
                        if int(entry["index"]) == int(item["index"])
                    ),
                    None,
                )
                resume_at = None
                retry_after = 0
                if parked is not None and isinstance(parked.get("result"), Mapping):
                    resume_at = str(parked["result"].get("resume_at") or "")
                    try:
                        retry_after = max(0, int(parked["result"].get("retry_after_seconds") or 0))
                    except (TypeError, ValueError):
                        retry_after = 0
                if self._wait_for_resume(resume_at):
                    return
                if self._stopping():
                    self.database.set_batch_job_status(job["id"], "queued")
                    return
                # When cooldown is disabled, still pace retries with the normal item throttle.
                if (
                    retry_after <= 0
                    and self.throttle_seconds > 0
                    and self._stop.wait(self.throttle_seconds)
                ):
                    self.database.set_batch_job_status(job["id"], "queued")
                    return
                self.database.set_batch_job_status(job["id"], "running", error=None)
                job = self.database.get_batch_job(job["id"])
                continue
            if outcome == "retry":
                refreshed = self.database.get_batch_job(job["id"])
                parked = next(
                    (
                        entry
                        for entry in refreshed["items"]
                        if int(entry["index"]) == int(item["index"])
                    ),
                    None,
                )
                resume_at = None
                retry_after = 0
                if parked is not None and isinstance(parked.get("result"), Mapping):
                    resume_at = str(parked["result"].get("resume_at") or "")
                    try:
                        retry_after = max(0, int(parked["result"].get("retry_after_seconds") or 0))
                    except (TypeError, ValueError):
                        retry_after = 0
                if self._wait_for_resume(resume_at):
                    return
                if self._stopping():
                    self.database.set_batch_job_status(job["id"], "queued")
                    return
                if (
                    retry_after <= 0
                    and self.throttle_seconds > 0
                    and self._stop.wait(self.throttle_seconds)
                ):
                    self.database.set_batch_job_status(job["id"], "queued")
                    return
                self.database.set_batch_job_status(job["id"], "running", error=None)
                job = self.database.get_batch_job(job["id"])
                continue
            if self._stopping():
                self.database.set_batch_job_status(job["id"], "queued")
                return
            if self._stop.wait(self.throttle_seconds):
                self.database.set_batch_job_status(job["id"], "queued")
                return
            job = self.database.get_batch_job(job["id"])
            index += 1
        completed = self.database.get_batch_job(job["id"])
        if completed["succeeded"] == completed["requested"]:
            status = "completed"
        elif completed["succeeded"]:
            status = "partial"
        else:
            status = "failed"
        self.database.set_batch_job_status(job["id"], status)

    def _run_item(self, job: Mapping[str, Any], item: Mapping[str, Any]) -> str:
        if job["kind"] == "create_aliases":
            return self._create_alias(item, job_id=job["id"])
        return "unknown" if self._bulk_alias(item, job_id=job["id"], action=job["action"]) else "ok"

    def _close_create_client(self, client: Any, original_session: Any) -> None:
        if client is None:
            return
        rotated_session = getattr(client, "session", original_session)
        if isinstance(original_session, ICloudHmeSession) and isinstance(
            rotated_session, ICloudHmeSession
        ):
            with suppress(Exception):
                self.gateway._persist_rotated_hme_session(original_session, rotated_session)
        self.gateway._close_client(client)

    def _validated_candidate_snapshot(self, candidate: str) -> tuple[str, dict[str, Any] | None]:
        """Return found/absent/unavailable from a read-only, complete-enough snapshot."""

        refreshed_auth = False
        while True:
            self._raise_if_stopping()
            session = None
            client = None
            try:
                session = self.gateway.get_hme_session()
                if session is None:
                    return "unavailable", None
                client = self.gateway.hme_client_factory(session)
                remote_aliases = client.list_aliases()
                validator = getattr(self.gateway, "_validated_remote_aliases", None)
                if callable(validator):
                    remote_aliases = validator(remote_aliases)
                else:
                    if not isinstance(remote_aliases, list) or any(
                        not isinstance(remote, Mapping) for remote in remote_aliases
                    ):
                        raise HmeError("iCloud HME returned an invalid alias snapshot")
                    remote_aliases = [dict(remote) for remote in remote_aliases]
            except HmeSessionError:
                if refreshed_auth:
                    return "unavailable", None
                refresh = getattr(self.gateway, "_refresh_hme_session", None)
                if not callable(refresh):
                    return "unavailable", None
                try:
                    refresh(during_write=True)
                except Exception:
                    return "unavailable", None
                refreshed_auth = True
                continue
            except (HmeNetworkError, HmeError, GatewayError, OSError, TimeoutError):
                return "unavailable", None
            except Exception:
                return "unavailable", None
            finally:
                if client is not None:
                    self._close_create_client(client, session)

            match = next(
                (
                    dict(remote)
                    for remote in remote_aliases
                    if str(remote.get("hme") or remote.get("email") or "").strip().casefold()
                    == candidate
                ),
                None,
            )
            if match is not None:
                return "found", match
            snapshot_ids = {
                str(remote.get("anonymousId") or "").strip() for remote in remote_aliases
            }
            try:
                known_ids = {
                    str(remote.get("anonymousId") or "").strip()
                    for alias in self.database.list_aliases()
                    for remote in [alias.get("remote_metadata")]
                    if isinstance(remote, Mapping) and str(remote.get("anonymousId") or "").strip()
                }
            except Exception:
                return "unavailable", None
            if known_ids.issubset(snapshot_ids):
                return "absent", None
            return "unavailable", None

    @staticmethod
    def _candidate_item(
        item: Mapping[str, Any],
        candidate: str,
        *,
        reconcile_before_reserve: bool,
    ) -> dict[str, Any]:
        result = dict(item.get("result") or {}) if isinstance(item.get("result"), Mapping) else {}
        for key in ("wait_reason", "resume_at", "retry_kind", "retry_after_seconds"):
            result.pop(key, None)
        if candidate:
            result["candidate"] = candidate
        else:
            result.pop("candidate", None)
        if reconcile_before_reserve:
            result["reconcile_before_reserve"] = True
        else:
            result.pop("reconcile_before_reserve", None)
        if reconcile_before_reserve:
            stage = "reconciling"
        elif candidate:
            stage = "generated"
        else:
            stage = "queued"
        return {
            **dict(item),
            "result": result,
            "stage": stage,
            "status": "queued",
        }

    def _complete_created_alias(
        self,
        *,
        job_id: str,
        index: int,
        values: Mapping[str, Any],
        remote: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str]:
        email = str(remote.get("hme") or remote.get("email") or "").strip().casefold()
        if email.count("@") != 1 or not str(remote.get("anonymousId") or "").strip():
            raise HmeError("iCloud HME reserve response is incomplete")
        alias = self.database.upsert_alias(
            email=email,
            remote_metadata=remote,
            label=str(values["label"]),
            note=str(values["note"]),
            sender_filter=str(values["sender_filter"]),
            state="inactive" if remote.get("isActive") is False else "active",
        )
        issued = self.database.issue_access_key(alias["id"])
        self.database.update_batch_item(
            job_id,
            index,
            stage="completed",
            status="success",
            alias_id=alias["id"],
            result={"id": alias["id"], "email": alias["email"], "label": alias["label"]},
        )
        self.database.record_audit_event("alias_create", "succeeded", alias_id=alias["id"])
        return self.database.get_alias(alias["id"]), issued.access_key

    def _create_alias(self, item: Mapping[str, Any], *, job_id: str) -> str:
        index = int(item["index"])
        values = dict(item["input"])
        client = None
        session = None
        candidate = ""
        remote_write_started = False
        remote_write_attempted = False
        try:
            self._raise_if_stopping()
            self.gateway._ensure_hme_fresh()
            self._raise_if_stopping()
            self.gateway._begin_remote_write()
            remote_write_started = True
            with self.gateway._hme_lock:
                session = self.gateway.get_hme_session()
                if session is None:
                    raise HmeSessionError("iCloud HME session is not configured")
                client = self.gateway.hme_client_factory(session)
                if hasattr(client, "generate_alias") and hasattr(client, "reserve_alias"):
                    saved_result = item.get("result")
                    candidate = (
                        str(saved_result.get("candidate") or "").strip().casefold()
                        if isinstance(saved_result, Mapping)
                        else ""
                    )
                    if not candidate:
                        self.database.update_batch_item(
                            job_id, index, stage="generating", status="running"
                        )
                        try:
                            candidate = client.generate_alias()
                        except HmeRateLimitedError as exc:
                            self._pause_for_rate_limit(
                                job_id=job_id,
                                item=item,
                                code=exc.code,
                                retry_after_seconds=exc.retry_after_seconds,
                            )
                            return "rate_limited"
                        candidate = str(candidate or "").strip().casefold()
                        if candidate.count("@") != 1:
                            raise HmeError("iCloud HME generate response is invalid")
                        self.database.update_batch_item(
                            job_id,
                            index,
                            stage="generated",
                            status="queued",
                            result={"candidate": candidate},
                        )
                        item = {
                            **dict(item),
                            "result": {"candidate": candidate},
                            "stage": "generated",
                            "status": "queued",
                        }
                    if (
                        isinstance(item.get("result"), Mapping)
                        and item["result"].get("reconcile_before_reserve") is True
                    ):
                        self._close_create_client(client, session)
                        client = None
                        resolution, reconciled = self._validated_candidate_snapshot(candidate)
                        if resolution == "found" and reconciled is not None:
                            created, access_key = self._complete_created_alias(
                                job_id=job_id,
                                index=index,
                                values=values,
                                remote=reconciled,
                            )
                        elif resolution == "unavailable":
                            retry_item = self._candidate_item(
                                item, candidate, reconcile_before_reserve=True
                            )
                            self._pause_for_transient_retry(
                                job_id=job_id,
                                item=retry_item,
                                retry_kind="reconcile",
                                reconcile_before_reserve=True,
                            )
                            return "retry"
                        else:
                            item = self._candidate_item(
                                item, candidate, reconcile_before_reserve=False
                            )
                            self.database.update_batch_item(
                                job_id,
                                index,
                                stage="generated",
                                status="queued",
                                result=item["result"],
                                error=None,
                            )
                            session = self.gateway.get_hme_session()
                            if session is None:
                                raise HmeSessionError("iCloud HME session is not configured")
                            client = self.gateway.hme_client_factory(session)
                    else:
                        created = None
                        access_key = ""
                    if client is not None:
                        self._raise_if_stopping()
                        self.database.update_batch_item(
                            job_id, index, stage="reserving", status="running"
                        )
                        remote_write_attempted = True
                        try:
                            remote = client.reserve_alias(
                                candidate, label=values["label"], note=values["note"]
                            )
                        except HmeRateLimitedError as exc:
                            # generate already succeeded; hold the candidate and cool down.
                            self._pause_for_rate_limit(
                                job_id=job_id,
                                item=self._candidate_item(
                                    item, candidate, reconcile_before_reserve=False
                                ),
                                code=exc.code,
                                retry_after_seconds=exc.retry_after_seconds,
                            )
                            return "rate_limited"
                        except (HmeNetworkError, HmeSessionError, HmeError) as exc:
                            self._close_create_client(client, session)
                            client = None
                            resolution, reconciled = self._validated_candidate_snapshot(candidate)
                            if resolution == "found" and reconciled is not None:
                                created, access_key = self._complete_created_alias(
                                    job_id=job_id,
                                    index=index,
                                    values=values,
                                    remote=reconciled,
                                )
                            else:
                                retry_item = self._candidate_item(
                                    item, candidate, reconcile_before_reserve=True
                                )
                                if isinstance(exc, HmeSessionError):
                                    retry_kind = "session"
                                elif isinstance(exc, HmeNetworkError):
                                    retry_kind = "network"
                                else:
                                    retry_kind = "remote"
                                if resolution == "absent":
                                    retry_kind = "reserve_unconfirmed"
                                self._pause_for_transient_retry(
                                    job_id=job_id,
                                    item=retry_item,
                                    retry_kind=retry_kind,
                                    reconcile_before_reserve=True,
                                )
                                return "retry"
                        else:
                            created, access_key = self._complete_created_alias(
                                job_id=job_id,
                                index=index,
                                values=values,
                                remote=remote,
                            )
                else:
                    self.database.update_batch_item(
                        job_id, index, stage="reserving", status="running"
                    )
                    remote_write_attempted = True
                    try:
                        remote = client.create_alias(label=values["label"], note=values["note"])
                    except HmeRateLimitedError as exc:
                        self._pause_for_rate_limit(
                            job_id=job_id,
                            item=item,
                            code=exc.code,
                            retry_after_seconds=exc.retry_after_seconds,
                        )
                        return "rate_limited"
                    created, access_key = self._complete_created_alias(
                        job_id=job_id,
                        index=index,
                        values=values,
                        remote=remote,
                    )
                self._close_create_client(client, session)
                client = None
            # Register the new alias and its key on the cloud edge immediately, but
            # outside the HME lock so Apple traffic is never blocked. A failed push
            # is audited by _push_alias_to_edge and healed by the periodic
            # edge reconcile loop; it must not fail the locally-successful item.
            with suppress(Exception):
                self.gateway._push_alias_to_edge(created, access_key=access_key, action="upsert")
            return "ok"
        except GatewayStoppingError:
            prior_reconcile = bool(
                isinstance(item.get("result"), Mapping)
                and item["result"].get("reconcile_before_reserve") is True
            )
            needs_reconcile = bool(candidate and (remote_write_attempted or prior_reconcile))
            retry_item = self._candidate_item(
                item,
                candidate,
                reconcile_before_reserve=needs_reconcile,
            )
            self.database.update_batch_item(
                job_id,
                index,
                stage=str(retry_item["stage"]),
                status="queued",
                result=retry_item["result"],
                error=None,
            )
            return "stopped"
        except (HmeNetworkError, HmeSessionError) as exc:
            if isinstance(exc, HmeSessionError):
                refresh = getattr(self.gateway, "_refresh_hme_session", None)
                if callable(refresh):
                    with suppress(Exception):
                        refresh(during_write=remote_write_started)
            prior_reconcile = bool(
                isinstance(item.get("result"), Mapping)
                and item["result"].get("reconcile_before_reserve") is True
            )
            needs_reconcile = bool(candidate and (remote_write_attempted or prior_reconcile))
            retry_item = self._candidate_item(
                item,
                candidate,
                reconcile_before_reserve=needs_reconcile,
            )
            self._pause_for_transient_retry(
                job_id=job_id,
                item=retry_item,
                retry_kind="session" if isinstance(exc, HmeSessionError) else "network",
                reconcile_before_reserve=needs_reconcile,
            )
            return "retry"
        except Exception as exc:
            if not remote_write_attempted and self._stopping():
                return "failed"
            if remote_write_attempted:
                self.database.update_batch_item(
                    job_id,
                    index,
                    stage="unknown",
                    status="unknown",
                    error="remote write outcome is unknown",
                )
                self.database.record_audit_event("alias_create", "unknown")
                return "unknown"
            self.database.update_batch_item(
                job_id, index, stage="failed", status="failed", error=type(exc).__name__
            )
            self.database.record_audit_event("alias_create", "error")
            return "failed"
        finally:
            if client is not None:
                self._close_create_client(client, session)
            if remote_write_started:
                self.gateway._finish_remote_write()

    def _bulk_alias(self, item: Mapping[str, Any], *, job_id: str, action: str) -> bool:
        index = int(item["index"])
        alias_id = str(item["input"]["alias_id"])
        remote_write = action in {"deactivate", "delete"}
        try:
            self._raise_if_stopping()
            if action in {"deactivate", "delete"}:
                required_state = "active" if action == "deactivate" else "inactive"
                alias = self.database.get_alias(alias_id)
                if alias["state"] != required_state:
                    raise ConflictError(f"alias must be {required_state}")
            self.database.update_batch_item(
                job_id, index, stage="executing", status="running", alias_id=alias_id
            )
            if action == "issue_keys":
                self.gateway.issue_access_key(alias_id)
                alias = self.database.get_alias(alias_id)
            elif action == "reveal_keys":
                self.gateway.reveal_access_key(alias_id)
                alias = self.database.get_alias(alias_id)
            elif action == "deactivate":
                alias = self.gateway.deactivate_alias(alias_id)
            else:
                alias = self.database.get_alias(alias_id)
                self.gateway.delete_alias(alias_id, confirmation=alias["email"])
            result = {"id": alias_id, "email": alias["email"]}
            if action != "delete":
                result["label"] = alias["label"]
            self.database.update_batch_item(
                job_id,
                index,
                stage="completed",
                status="success",
                alias_id=alias_id,
                result=result,
            )
            return False
        except GatewayStoppingError:
            self.database.update_batch_item(
                job_id,
                index,
                stage="queued",
                status="queued",
                alias_id=alias_id,
            )
            return False
        except NotFoundError:
            self.database.update_batch_item(
                job_id, index, stage="failed", status="failed", alias_id=alias_id, error="not_found"
            )
            return False
        except ConflictError:
            self.database.update_batch_item(
                job_id, index, stage="failed", status="failed", alias_id=alias_id, error="conflict"
            )
            return False
        except GatewayRetryableError as exc:
            self.database.update_batch_item(
                job_id,
                index,
                stage="failed",
                status="failed",
                alias_id=alias_id,
                error=type(exc).__name__,
            )
            return False
        except (HmeNetworkError, HmeSessionError, HmeError, GatewayError) as exc:
            status = "unknown" if remote_write else "failed"
            self.database.update_batch_item(
                job_id,
                index,
                stage=status,
                status=status,
                alias_id=alias_id,
                error=type(exc).__name__,
            )
            return remote_write
        except Exception as exc:
            self.database.update_batch_item(
                job_id,
                index,
                stage="failed",
                status="failed",
                alias_id=alias_id,
                error=type(exc).__name__,
            )
            return False


__all__ = ["BatchJobManager", "TERMINAL_JOB_STATUSES", "request_fingerprint"]
