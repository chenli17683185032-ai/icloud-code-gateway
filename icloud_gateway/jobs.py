from __future__ import annotations

import fcntl
import hashlib
import json
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from typing import Any, BinaryIO

from .database import ConflictError, Database, NotFoundError
from .hme import HmeError, HmeNetworkError, HmeSessionError, ICloudHmeSession
from .service import GatewayError, GatewayStoppingError

TERMINAL_JOB_STATUSES = {
    "completed",
    "partial",
    "failed",
    "needs_reconcile",
    "cancelled",
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
    def __init__(self, gateway: Any, *, throttle_seconds: float = 2.0) -> None:
        self.gateway = gateway
        self.database: Database = gateway.database
        self.throttle_seconds = max(0.0, float(throttle_seconds))
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
                value.update({key: entry for key, entry in result.items() if key != "candidate"})
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
        return {
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

    @staticmethod
    def _public_item_error(item: Mapping[str, Any]) -> str | None:
        if not item.get("error"):
            return None
        if item.get("status") == "unknown":
            if str(item.get("error")) == "interrupted during an in-flight operation":
                return "interrupted_in_flight"
            return "remote_write_unknown"
        if item.get("error") in {"not_found", "conflict"}:
            return str(item["error"])
        if str(item.get("error")) == "remote write was not attempted":
            return "remote_write_not_attempted"
        return "operation_failed"

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
        for item in job["items"]:
            if self._stopping():
                self.database.set_batch_job_status(job["id"], "queued")
                return
            if item["status"] != "queued":
                continue
            unknown = self._run_item(job, item)
            if unknown:
                self.database.set_batch_job_status(
                    job["id"],
                    "needs_reconcile",
                    error="remote write outcome is unknown; automatic replay stopped",
                )
                return
            if self._stopping():
                self.database.set_batch_job_status(job["id"], "queued")
                return
            if self._stop.wait(self.throttle_seconds):
                self.database.set_batch_job_status(job["id"], "queued")
                return
        completed = self.database.get_batch_job(job["id"])
        if completed["succeeded"] == completed["requested"]:
            status = "completed"
        elif completed["succeeded"]:
            status = "partial"
        else:
            status = "failed"
        self.database.set_batch_job_status(job["id"], status)

    def _run_item(self, job: Mapping[str, Any], item: Mapping[str, Any]) -> bool:
        if job["kind"] == "create_aliases":
            return self._create_alias(item, job_id=job["id"])
        return self._bulk_alias(item, job_id=job["id"], action=job["action"])

    def _create_alias(self, item: Mapping[str, Any], *, job_id: str) -> bool:
        index = int(item["index"])
        values = dict(item["input"])
        client = None
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
                        str(saved_result.get("candidate") or "").strip()
                        if isinstance(saved_result, Mapping)
                        else ""
                    )
                    if not candidate:
                        self.database.update_batch_item(
                            job_id, index, stage="generating", status="running"
                        )
                        candidate = client.generate_alias()
                        self.database.update_batch_item(
                            job_id,
                            index,
                            stage="generated",
                            status="queued",
                            result={"candidate": candidate},
                        )
                    self.database.update_batch_item(
                        job_id, index, stage="reserving", status="running"
                    )
                    remote_write_attempted = True
                    remote = client.reserve_alias(
                        candidate, label=values["label"], note=values["note"]
                    )
                else:
                    self.database.update_batch_item(
                        job_id, index, stage="reserving", status="running"
                    )
                    remote_write_attempted = True
                    remote = client.create_alias(label=values["label"], note=values["note"])
                email = str(remote.get("hme") or remote.get("email") or "").strip().casefold()
                if email.count("@") != 1 or not str(remote.get("anonymousId") or "").strip():
                    raise HmeError("iCloud HME reserve response is incomplete")
                alias = self.database.upsert_alias(
                    email=email,
                    remote_metadata=remote,
                    label=values["label"],
                    note=values["note"],
                    sender_filter=values["sender_filter"],
                    state="inactive" if remote.get("isActive") is False else "active",
                )
                self.database.issue_access_key(alias["id"])
                rotated_session = getattr(client, "session", session)
                if isinstance(rotated_session, ICloudHmeSession):
                    self.gateway._persist_rotated_hme_session(session, rotated_session)
                self.database.update_batch_item(
                    job_id,
                    index,
                    stage="completed",
                    status="success",
                    alias_id=alias["id"],
                    result={"id": alias["id"], "email": alias["email"], "label": alias["label"]},
                )
                self.database.record_audit_event("alias_create", "succeeded", alias_id=alias["id"])
                return False
        except (HmeNetworkError, HmeSessionError):
            if remote_write_attempted:
                self.database.update_batch_item(
                    job_id,
                    index,
                    stage="unknown",
                    status="unknown",
                    error="remote write outcome is unknown",
                )
                self.database.record_audit_event("alias_create", "unknown")
                return True
            self.database.update_batch_item(
                job_id,
                index,
                stage="failed",
                status="failed",
                error="remote write was not attempted",
            )
            self.database.record_audit_event("alias_create", "error")
            return False
        except Exception as exc:
            if not remote_write_attempted and self._stopping():
                return False
            if remote_write_attempted:
                self.database.update_batch_item(
                    job_id,
                    index,
                    stage="unknown",
                    status="unknown",
                    error="remote write outcome is unknown",
                )
                self.database.record_audit_event("alias_create", "unknown")
                return True
            self.database.update_batch_item(
                job_id, index, stage="failed", status="failed", error=type(exc).__name__
            )
            self.database.record_audit_event("alias_create", "error")
            return False
        finally:
            if client is not None:
                self.gateway._close_client(client)
            if remote_write_started:
                self.gateway._finish_remote_write()

    def _bulk_alias(self, item: Mapping[str, Any], *, job_id: str, action: str) -> bool:
        index = int(item["index"])
        alias_id = str(item["input"]["alias_id"])
        remote_write = action in {"deactivate", "delete"}
        try:
            self._raise_if_stopping()
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
