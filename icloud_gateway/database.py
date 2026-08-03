from __future__ import annotations

import hmac
import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .security import (
    SecretBox,
    SecurityError,
    generate_access_key,
    hash_access_key,
    validate_access_key,
)

SCHEMA_VERSION = 1
AUDIT_RETENTION_DAYS = 7
# The lifespan hook only purges at startup, so a long-lived process trims the
# audit log on write as well.
AUDIT_PURGE_INTERVAL = 500


class DatabaseError(RuntimeError):
    pass


class NotFoundError(DatabaseError):
    pass


class ConflictError(DatabaseError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _retention_cutoff(days: int) -> str:
    return (
        (datetime.now(UTC) - timedelta(days=max(1, int(days))))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _normalize_email(value: str) -> str:
    email = str(value or "").strip().casefold()
    if email.count("@") != 1 or any(character.isspace() for character in email):
        raise ValueError("email is invalid")
    local, domain = email.rsplit("@", 1)
    if not local or not domain or "." not in domain:
        raise ValueError("email is invalid")
    return email


def _clean_text(value: Any, *, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit or "\r" in text or "\n" in text:
        raise ValueError("text is invalid")
    return text


@dataclass(frozen=True)
class IssuedAccessKey:
    alias_id: str
    access_key: str
    hint: str


class Database:
    def __init__(self, path: str | Path, secret_box: SecretBox) -> None:
        self.path = Path(path).expanduser()
        self.secret_box = secret_box
        self._local = threading.local()
        self._audit_lock = threading.Lock()
        self._audit_writes = 0

    def initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with suppress(OSError):
            self.path.parent.chmod(0o700)
        self._connect().executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_blob BLOB NOT NULL,
                    purpose TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS aliases (
                    id TEXT PRIMARY KEY,
                    email_hash BLOB NOT NULL UNIQUE,
                    email_blob BLOB NOT NULL,
                    remote_blob BLOB,
                    label TEXT NOT NULL,
                    note TEXT NOT NULL,
                    sender_filter TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('active', 'inactive')),
                    access_key_hash BLOB UNIQUE,
                    access_key_hint TEXT,
                    access_key_blob BLOB,
                    key_issued_at TEXT,
                    key_revoked_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_synced_at TEXT
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    alias_id TEXT REFERENCES aliases(id) ON DELETE SET NULL,
                    alias_email_blob BLOB,
                    outcome TEXT NOT NULL,
                    ip_digest TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS audit_events_created_at_idx
                    ON audit_events(created_at);
                CREATE INDEX IF NOT EXISTS audit_events_alias_id_idx
                    ON audit_events(alias_id, created_at);
                CREATE INDEX IF NOT EXISTS audit_events_type_id_idx
                    ON audit_events(event_type, id DESC);

                CREATE TABLE IF NOT EXISTS batch_jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('create_aliases', 'bulk_aliases')),
                    action TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('queued', 'running', 'completed', 'partial', 'failed',
                                   'needs_reconcile', 'cancelled')
                    ),
                    idempotency_key_hash BLOB,
                    request_fingerprint BLOB NOT NULL,
                    requested INTEGER NOT NULL,
                    succeeded INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    current INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT
                );

                CREATE UNIQUE INDEX IF NOT EXISTS batch_jobs_idempotency_idx
                    ON batch_jobs(kind, idempotency_key_hash)
                    WHERE idempotency_key_hash IS NOT NULL;
                CREATE INDEX IF NOT EXISTS batch_jobs_active_idx
                    ON batch_jobs(status, created_at);

                CREATE TABLE IF NOT EXISTS batch_job_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES batch_jobs(id) ON DELETE CASCADE,
                    item_index INTEGER NOT NULL,
                    alias_id TEXT,
                    input_blob BLOB NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('queued', 'running', 'success', 'failed', 'unknown', 'cancelled')
                    ),
                    result_blob BLOB,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT,
                    UNIQUE(job_id, item_index)
                );

                CREATE INDEX IF NOT EXISTS batch_job_items_job_idx
                    ON batch_job_items(job_id, item_index);
                """
        )
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if current is None:
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(current["value"]) != SCHEMA_VERSION:
                raise DatabaseError("database schema version is unsupported")
            alias_columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(aliases)")
            }
            if "access_key_blob" not in alias_columns:
                connection.execute("ALTER TABLE aliases ADD COLUMN access_key_blob BLOB")
            audit_columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(audit_events)")
            }
            if "alias_email_blob" not in audit_columns:
                connection.execute("ALTER TABLE audit_events ADD COLUMN alias_email_blob BLOB")
            connection.execute(
                """
                UPDATE audit_events
                SET alias_email_blob = (
                    SELECT aliases.email_blob
                    FROM aliases
                    WHERE aliases.id = audit_events.alias_id
                )
                WHERE alias_email_blob IS NULL AND alias_id IS NOT NULL
                """
            )
        with suppress(OSError):
            self.path.chmod(0o600)

    def quick_check(self) -> str:
        row = self._connect().execute("PRAGMA quick_check").fetchone()
        return str(row[0] if row else "")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except Exception:
            with suppress(sqlite3.Error):
                connection.rollback()
            raise

    def set_secret(self, key: str, value: Mapping[str, Any]) -> None:
        name = _clean_text(key, limit=120)
        if not name:
            raise ValueError("setting key is required")
        purpose = f"setting:{name}"
        plaintext = json.dumps(
            dict(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        encrypted = self.secret_box.encrypt(plaintext, purpose)
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO settings(key, value_blob, purpose, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_blob = excluded.value_blob,
                    purpose = excluded.purpose,
                    updated_at = excluded.updated_at
                """,
                (name, sqlite3.Binary(encrypted), purpose, _now()),
            )

    def get_secret(self, key: str) -> dict[str, Any] | None:
        name = _clean_text(key, limit=120)
        row = (
            self._connect()
            .execute("SELECT value_blob, purpose FROM settings WHERE key = ?", (name,))
            .fetchone()
        )
        if row is None:
            return None
        plaintext = self.secret_box.decrypt(bytes(row["value_blob"]), str(row["purpose"]))
        try:
            value = json.loads(plaintext)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DatabaseError("encrypted setting is invalid") from exc
        if not isinstance(value, dict):
            raise DatabaseError("encrypted setting is invalid")
        return value

    def upsert_alias(
        self,
        *,
        email: str,
        remote_metadata: Mapping[str, Any] | None,
        label: str = "",
        note: str = "",
        sender_filter: str = "",
        state: str = "active",
        synced_at: str | None = None,
    ) -> dict[str, Any]:
        normalized_email = _normalize_email(email)
        clean_label = _clean_text(label or normalized_email, limit=160)
        clean_note = _clean_text(note, limit=500)
        clean_sender = _clean_text(sender_filter, limit=254).casefold()
        clean_state = str(state or "").strip()
        if clean_state not in {"active", "inactive"}:
            raise ValueError("alias state is invalid")
        email_hash = self.secret_box.digest(normalized_email, "alias-email-index")
        email_blob = self.secret_box.encrypt(normalized_email.encode("utf-8"), "alias-email")
        remote_blob = None
        if remote_metadata is not None:
            remote_blob = self.secret_box.encrypt(
                json.dumps(
                    dict(remote_metadata),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8"),
                "alias-remote",
            )
        timestamp = _now()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM aliases WHERE email_hash = ?", (sqlite3.Binary(email_hash),)
            ).fetchone()
            if existing is None:
                alias_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO aliases(
                        id, email_hash, email_blob, remote_blob, label, note,
                        sender_filter, state, created_at, updated_at, last_synced_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        alias_id,
                        sqlite3.Binary(email_hash),
                        sqlite3.Binary(email_blob),
                        None if remote_blob is None else sqlite3.Binary(remote_blob),
                        clean_label,
                        clean_note,
                        clean_sender,
                        clean_state,
                        timestamp,
                        timestamp,
                        synced_at,
                    ),
                )
            else:
                alias_id = str(existing["id"])
                connection.execute(
                    """
                    UPDATE aliases
                    SET email_blob = ?,
                        remote_blob = COALESCE(?, remote_blob),
                        label = ?, note = ?, sender_filter = ?, state = ?,
                        access_key_hash = CASE
                            WHEN ? = 'inactive' THEN NULL ELSE access_key_hash END,
                        access_key_hint = CASE
                            WHEN ? = 'inactive' THEN NULL ELSE access_key_hint END,
                        access_key_blob = CASE
                            WHEN ? = 'inactive' THEN NULL ELSE access_key_blob END,
                        key_revoked_at = CASE
                            WHEN ? = 'inactive' THEN ? ELSE key_revoked_at END,
                        updated_at = ?, last_synced_at = COALESCE(?, last_synced_at)
                    WHERE id = ?
                    """,
                    (
                        sqlite3.Binary(email_blob),
                        None if remote_blob is None else sqlite3.Binary(remote_blob),
                        clean_label,
                        clean_note,
                        clean_sender,
                        clean_state,
                        clean_state,
                        clean_state,
                        clean_state,
                        clean_state,
                        timestamp,
                        timestamp,
                        synced_at,
                        alias_id,
                    ),
                )
        return self.get_alias(alias_id)

    def sync_remote_alias(
        self,
        *,
        email: str,
        remote_metadata: Mapping[str, Any],
        synced_at: str,
    ) -> dict[str, Any]:
        normalized_email = _normalize_email(email)
        email_hash = self.secret_box.digest(normalized_email, "alias-email-index")
        email_blob = self.secret_box.encrypt(normalized_email.encode(), "alias-email")
        remote = dict(remote_metadata)
        remote_blob = self.secret_box.encrypt(
            json.dumps(
                remote,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode(),
            "alias-remote",
        )
        state = "inactive" if remote.get("isActive") is False else "active"
        remote_label = _clean_text(remote.get("label") or normalized_email, limit=160)
        remote_note = _clean_text(remote.get("note") or "", limit=500)
        timestamp = _now()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM aliases WHERE email_hash = ?",
                (sqlite3.Binary(email_hash),),
            ).fetchone()
            if existing is None:
                alias_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO aliases(
                        id, email_hash, email_blob, remote_blob, label, note,
                        sender_filter, state, created_at, updated_at, last_synced_at
                    ) VALUES(?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?)
                    """,
                    (
                        alias_id,
                        sqlite3.Binary(email_hash),
                        sqlite3.Binary(email_blob),
                        sqlite3.Binary(remote_blob),
                        remote_label,
                        remote_note,
                        state,
                        timestamp,
                        timestamp,
                        synced_at,
                    ),
                )
            else:
                alias_id = str(existing["id"])
                connection.execute(
                    """
                    UPDATE aliases
                    SET email_blob = ?, remote_blob = ?, state = ?,
                        access_key_hash = CASE
                            WHEN ? = 'inactive' THEN NULL ELSE access_key_hash END,
                        access_key_hint = CASE
                            WHEN ? = 'inactive' THEN NULL ELSE access_key_hint END,
                        access_key_blob = CASE
                            WHEN ? = 'inactive' THEN NULL ELSE access_key_blob END,
                        key_revoked_at = CASE
                            WHEN ? = 'inactive' THEN ? ELSE key_revoked_at END,
                        updated_at = ?, last_synced_at = ?
                    WHERE id = ?
                    """,
                    (
                        sqlite3.Binary(email_blob),
                        sqlite3.Binary(remote_blob),
                        state,
                        state,
                        state,
                        state,
                        state,
                        timestamp,
                        timestamp,
                        synced_at,
                        alias_id,
                    ),
                )
        return self.get_alias(alias_id)

    def count_remote_aliases(self) -> int:
        row = (
            self._connect()
            .execute("SELECT COUNT(*) AS total FROM aliases WHERE remote_blob IS NOT NULL")
            .fetchone()
        )
        return 0 if row is None else int(row["total"])

    def finish_remote_sync(self, seen_emails: list[str], *, synced_at: str) -> int:
        digests = [
            self.secret_box.digest(_normalize_email(email), "alias-email-index")
            for email in seen_emails
        ]
        timestamp = _now()
        with self.transaction() as connection:
            if digests:
                placeholders = ",".join("?" for _ in digests)
                query = f"""
                    UPDATE aliases
                    SET state = 'inactive', access_key_hash = NULL,
                        access_key_hint = NULL, access_key_blob = NULL, key_revoked_at = ?,
                        updated_at = ?, last_synced_at = ?
                    WHERE remote_blob IS NOT NULL
                      AND email_hash NOT IN ({placeholders})
                """
                parameters: tuple[Any, ...] = (
                    timestamp,
                    timestamp,
                    synced_at,
                    *(sqlite3.Binary(value) for value in digests),
                )
            else:
                query = """
                    UPDATE aliases
                    SET state = 'inactive', access_key_hash = NULL,
                        access_key_hint = NULL, access_key_blob = NULL, key_revoked_at = ?,
                        updated_at = ?, last_synced_at = ?
                    WHERE remote_blob IS NOT NULL
                """
                parameters = (timestamp, timestamp, synced_at)
            cursor = connection.execute(query, parameters)
            return int(cursor.rowcount)

    def list_aliases(self) -> list[dict[str, Any]]:
        rows = (
            self._connect()
            .execute(
                """
                SELECT * FROM aliases
                ORDER BY state ASC, created_at DESC, id
                """
            )
            .fetchall()
        )
        return [self._alias_from_row(row) for row in rows]

    def get_alias(self, alias_id: str) -> dict[str, Any]:
        row = (
            self._connect()
            .execute("SELECT * FROM aliases WHERE id = ?", (str(alias_id),))
            .fetchone()
        )
        if row is None:
            raise NotFoundError("alias not found")
        return self._alias_from_row(row)

    def find_alias_by_access_key_hash(self, digest: bytes) -> dict[str, Any] | None:
        row = (
            self._connect()
            .execute(
                """
                SELECT * FROM aliases
                WHERE access_key_hash = ?
                  AND key_revoked_at IS NULL
                  AND state = 'active'
                """,
                (sqlite3.Binary(digest),),
            )
            .fetchone()
        )
        return None if row is None else self._alias_from_row(row)

    def import_access_key(self, alias_id: str, access_key: str) -> IssuedAccessKey:
        clean_alias_id = str(alias_id)
        key = validate_access_key(access_key)
        digest = hash_access_key(key)
        hint = key[-4:]
        encrypted = self.secret_box.encrypt(
            key.encode("ascii"),
            f"alias-access-key:{clean_alias_id}",
        )
        timestamp = _now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM aliases WHERE id = ?", (clean_alias_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("alias not found")
            if str(row["state"]) != "active":
                raise ConflictError("inactive alias cannot receive an access key")
            conflict = connection.execute(
                """
                SELECT id FROM aliases
                WHERE access_key_hash = ?
                  AND id != ?
                """,
                (sqlite3.Binary(digest), clean_alias_id),
            ).fetchone()
            if conflict is not None:
                raise ConflictError("access key already assigned to another alias")
            connection.execute(
                """
                UPDATE aliases
                SET access_key_hash = ?, access_key_hint = ?, access_key_blob = ?,
                    key_issued_at = ?, key_revoked_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    sqlite3.Binary(digest),
                    hint,
                    sqlite3.Binary(encrypted),
                    timestamp,
                    timestamp,
                    clean_alias_id,
                ),
            )
        return IssuedAccessKey(alias_id=clean_alias_id, access_key=key, hint=hint)

    def issue_access_key(self, alias_id: str) -> IssuedAccessKey:
        clean_alias_id = str(alias_id)
        access_key = generate_access_key()
        digest = hash_access_key(access_key)
        hint = access_key[-4:]
        encrypted = self.secret_box.encrypt(
            access_key.encode("ascii"),
            f"alias-access-key:{clean_alias_id}",
        )
        timestamp = _now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM aliases WHERE id = ?", (clean_alias_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("alias not found")
            if str(row["state"]) != "active":
                raise ConflictError("inactive alias cannot receive an access key")
            connection.execute(
                """
                UPDATE aliases
                SET access_key_hash = ?, access_key_hint = ?, access_key_blob = ?,
                    key_issued_at = ?, key_revoked_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    sqlite3.Binary(digest),
                    hint,
                    sqlite3.Binary(encrypted),
                    timestamp,
                    timestamp,
                    clean_alias_id,
                ),
            )
        return IssuedAccessKey(alias_id=clean_alias_id, access_key=access_key, hint=hint)

    def reveal_access_key(self, alias_id: str) -> str:
        clean_alias_id = str(alias_id)
        row = (
            self._connect()
            .execute(
                """
                SELECT state, access_key_hash, access_key_blob, key_revoked_at
                FROM aliases
                WHERE id = ?
                """,
                (clean_alias_id,),
            )
            .fetchone()
        )
        if row is None:
            raise NotFoundError("alias not found")
        if (
            str(row["state"]) != "active"
            or row["access_key_hash"] is None
            or row["key_revoked_at"] is not None
        ):
            raise ConflictError("access key is not active")
        if row["access_key_blob"] is None:
            raise ConflictError("access key cannot be recovered; rotate it")
        try:
            plaintext = self.secret_box.decrypt(
                bytes(row["access_key_blob"]),
                f"alias-access-key:{clean_alias_id}",
            )
            access_key = validate_access_key(plaintext.decode("ascii"))
        except (SecurityError, UnicodeDecodeError) as exc:
            raise DatabaseError("encrypted access key is invalid") from exc
        if not hmac.compare_digest(
            hash_access_key(access_key),
            bytes(row["access_key_hash"]),
        ):
            raise DatabaseError("encrypted access key does not match its hash")
        return access_key

    def revoke_access_key(self, alias_id: str) -> None:
        timestamp = _now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE aliases
                SET access_key_hash = NULL, access_key_hint = NULL, access_key_blob = NULL,
                    key_revoked_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, str(alias_id)),
            )
            if cursor.rowcount != 1:
                raise NotFoundError("alias not found")

    def update_alias_configuration(
        self,
        alias_id: str,
        *,
        label: str,
        note: str,
        sender_filter: str,
    ) -> dict[str, Any]:
        clean_label = _clean_text(label, limit=160)
        if not clean_label:
            raise ValueError("label is required")
        clean_note = _clean_text(note, limit=500)
        clean_sender = _clean_text(sender_filter, limit=254).casefold()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE aliases
                SET label = ?, note = ?, sender_filter = ?, updated_at = ?
                WHERE id = ?
                """,
                (clean_label, clean_note, clean_sender, _now(), str(alias_id)),
            )
            if cursor.rowcount != 1:
                raise NotFoundError("alias not found")
        return self.get_alias(alias_id)

    def set_alias_state(self, alias_id: str, state: str) -> None:
        clean_state = str(state or "").strip()
        if clean_state not in {"active", "inactive"}:
            raise ValueError("alias state is invalid")
        timestamp = _now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE aliases
                SET state = ?,
                    access_key_hash = CASE WHEN ? = 'inactive' THEN NULL ELSE access_key_hash END,
                    access_key_hint = CASE WHEN ? = 'inactive' THEN NULL ELSE access_key_hint END,
                    access_key_blob = CASE WHEN ? = 'inactive' THEN NULL ELSE access_key_blob END,
                    key_revoked_at = CASE WHEN ? = 'inactive' THEN ? ELSE key_revoked_at END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    clean_state,
                    clean_state,
                    clean_state,
                    clean_state,
                    clean_state,
                    timestamp,
                    timestamp,
                    str(alias_id),
                ),
            )
            if cursor.rowcount != 1:
                raise NotFoundError("alias not found")

    def delete_alias(self, alias_id: str) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM aliases WHERE id = ?",
                (str(alias_id),),
            )
            if cursor.rowcount != 1:
                raise NotFoundError("alias not found")

    def create_batch_job(
        self,
        *,
        kind: str,
        action: str,
        fingerprint: bytes,
        items: list[Mapping[str, Any]],
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        clean_kind = _clean_text(kind, limit=40)
        clean_action = _clean_text(action, limit=40)
        if clean_kind not in {"create_aliases", "bulk_aliases"} or not items:
            raise ValueError("batch job is invalid")
        key_hash = None
        if idempotency_key is not None:
            clean_key = _clean_text(idempotency_key, limit=200)
            if not clean_key:
                raise ValueError("idempotency key is invalid")
            key_hash = self.secret_box.digest(clean_key, "batch-idempotency-key")
        timestamp = _now()
        job_id = str(uuid.uuid4())
        with self.transaction() as connection:
            if key_hash is not None:
                existing = connection.execute(
                    """
                    SELECT * FROM batch_jobs
                    WHERE kind = ? AND idempotency_key_hash = ?
                    """,
                    (clean_kind, sqlite3.Binary(key_hash)),
                ).fetchone()
                if existing is not None:
                    if not hmac.compare_digest(bytes(existing["request_fingerprint"]), fingerprint):
                        raise ConflictError("idempotency key payload does not match")
                    return self._batch_job_from_row(existing, connection=connection), False
            connection.execute(
                """
                INSERT INTO batch_jobs(
                    id, kind, action, status, idempotency_key_hash, request_fingerprint,
                    requested, succeeded, failed, current, created_at, updated_at
                ) VALUES(?, ?, ?, 'queued', ?, ?, ?, 0, 0, 0, ?, ?)
                """,
                (
                    job_id,
                    clean_kind,
                    clean_action,
                    None if key_hash is None else sqlite3.Binary(key_hash),
                    sqlite3.Binary(fingerprint),
                    len(items),
                    timestamp,
                    timestamp,
                ),
            )
            for index, item in enumerate(items, start=1):
                plaintext = json.dumps(
                    dict(item), ensure_ascii=False, separators=(",", ":"), sort_keys=True
                ).encode("utf-8")
                encrypted = self.secret_box.encrypt(plaintext, "batch-job-input")
                connection.execute(
                    """
                    INSERT INTO batch_job_items(
                        job_id, item_index, alias_id, input_blob, stage, status,
                        created_at, updated_at
                    ) VALUES(?, ?, ?, ?, 'queued', 'queued', ?, ?)
                    """,
                    (
                        job_id,
                        index,
                        item.get("alias_id"),
                        sqlite3.Binary(encrypted),
                        timestamp,
                        timestamp,
                    ),
                )
            row = connection.execute("SELECT * FROM batch_jobs WHERE id = ?", (job_id,)).fetchone()
            assert row is not None
            return self._batch_job_from_row(row, connection=connection), True

    def get_batch_job(self, job_id: str) -> dict[str, Any]:
        connection = self._connect()
        row = connection.execute("SELECT * FROM batch_jobs WHERE id = ?", (str(job_id),)).fetchone()
        if row is None:
            raise NotFoundError("batch job not found")
        return self._batch_job_from_row(row, connection=connection)

    def list_active_batch_jobs(self) -> list[dict[str, Any]]:
        connection = self._connect()
        rows = connection.execute(
            """
            SELECT * FROM batch_jobs
            WHERE status IN ('queued', 'running', 'needs_reconcile')
            ORDER BY created_at, id
            """
        ).fetchall()
        return [self._batch_job_from_row(row, connection=connection) for row in rows]

    def next_batch_job(self) -> dict[str, Any] | None:
        with self.transaction() as connection:
            selected = connection.execute(
                """
                SELECT id FROM batch_jobs
                WHERE status = 'queued'
                ORDER BY created_at, id
                LIMIT 1
                """
            ).fetchone()
            if selected is None:
                return None
            timestamp = _now()
            claimed = connection.execute(
                """
                UPDATE batch_jobs
                SET status = 'running', error = NULL, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (timestamp, selected["id"]),
            )
            if claimed.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM batch_jobs WHERE id = ?", (selected["id"],)
            ).fetchone()
            assert row is not None
            return self._batch_job_from_row(row, connection=connection)

    def set_batch_job_status(self, job_id: str, status: str, *, error: str | None = None) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE batch_jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                (status, error, _now(), str(job_id)),
            )
            if cursor.rowcount != 1:
                raise NotFoundError("batch job not found")

    def update_batch_item(
        self,
        job_id: str,
        item_index: int,
        *,
        stage: str,
        status: str,
        alias_id: str | None = None,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        result_blob = None
        if result is not None:
            plaintext = json.dumps(
                dict(result), ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
            result_blob = self.secret_box.encrypt(plaintext, "batch-job-result")
        timestamp = _now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE batch_job_items
                SET stage = ?, status = ?, alias_id = COALESCE(?, alias_id),
                    result_blob = COALESCE(?, result_blob), error = ?, updated_at = ?
                WHERE job_id = ? AND item_index = ?
                """,
                (
                    stage,
                    status,
                    alias_id,
                    None if result_blob is None else sqlite3.Binary(result_blob),
                    error,
                    timestamp,
                    str(job_id),
                    int(item_index),
                ),
            )
            if cursor.rowcount != 1:
                raise NotFoundError("batch job item not found")
            self._refresh_batch_job_counts(connection, str(job_id), timestamp)

    def recover_interrupted_batch_jobs(self) -> None:
        timestamp = _now()
        with self.transaction() as connection:
            interrupted = connection.execute(
                """
                SELECT job_id, item_index FROM batch_job_items
                WHERE status = 'running'
                """
            ).fetchall()
            for row in interrupted:
                connection.execute(
                    """
                    UPDATE batch_job_items
                    SET status = 'unknown', stage = 'unknown',
                        error = 'interrupted during an in-flight operation', updated_at = ?
                    WHERE job_id = ? AND item_index = ?
                    """,
                    (timestamp, row["job_id"], row["item_index"]),
                )
                connection.execute(
                    """
                    UPDATE batch_jobs
                    SET status = 'needs_reconcile',
                        error = 'an in-flight operation was interrupted; automatic replay stopped',
                        updated_at = ? WHERE id = ?
                    """,
                    (timestamp, row["job_id"]),
                )
            queued = connection.execute(
                """
                SELECT id FROM batch_jobs
                WHERE status = 'running' AND id NOT IN (
                    SELECT DISTINCT job_id FROM batch_job_items WHERE status = 'unknown'
                )
                """
            ).fetchall()
            for row in queued:
                connection.execute(
                    "UPDATE batch_jobs SET status = 'queued', updated_at = ? WHERE id = ?",
                    (timestamp, row["id"]),
                )

    def _refresh_batch_job_counts(
        self, connection: sqlite3.Connection, job_id: str, timestamp: str
    ) -> None:
        counts = connection.execute(
            """
            SELECT
                SUM(status = 'success') AS succeeded,
                SUM(status IN ('failed', 'unknown', 'cancelled')) AS failed,
                SUM(status != 'queued') AS current
            FROM batch_job_items WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        connection.execute(
            """
            UPDATE batch_jobs SET succeeded = ?, failed = ?, current = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                int(counts["succeeded"] or 0),
                int(counts["failed"] or 0),
                int(counts["current"] or 0),
                timestamp,
                job_id,
            ),
        )

    def _batch_job_from_row(
        self, row: sqlite3.Row, *, connection: sqlite3.Connection
    ) -> dict[str, Any]:
        item_rows = connection.execute(
            """
            SELECT item_index, alias_id, input_blob, stage, status, result_blob,
                   created_at, updated_at, error
            FROM batch_job_items WHERE job_id = ? ORDER BY item_index
            """,
            (row["id"],),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for item in item_rows:
            input_value = json.loads(
                self.secret_box.decrypt(bytes(item["input_blob"]), "batch-job-input")
            )
            result_value = None
            if item["result_blob"] is not None:
                result_value = json.loads(
                    self.secret_box.decrypt(bytes(item["result_blob"]), "batch-job-result")
                )
            items.append(
                {
                    "index": int(item["item_index"]),
                    "alias_id": item["alias_id"],
                    "input": input_value,
                    "stage": str(item["stage"]),
                    "status": str(item["status"]),
                    "result": result_value,
                    "created_at": str(item["created_at"]),
                    "updated_at": str(item["updated_at"]),
                    "error": item["error"],
                }
            )
        return {
            "id": str(row["id"]),
            "kind": str(row["kind"]),
            "action": str(row["action"]),
            "status": str(row["status"]),
            "requested": int(row["requested"]),
            "succeeded": int(row["succeeded"]),
            "failed": int(row["failed"]),
            "current": int(row["current"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "error": row["error"],
            "items": items,
        }

    def record_audit_event(
        self,
        event_type: str,
        outcome: str,
        *,
        alias_id: str | None = None,
        ip_digest: str | None = None,
    ) -> None:
        clean_type = _clean_text(event_type, limit=80)
        clean_outcome = _clean_text(outcome, limit=80)
        clean_ip = _clean_text(ip_digest, limit=80) if ip_digest else None
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO audit_events(
                    event_type, alias_id, alias_email_blob, outcome, ip_digest, created_at
                )
                VALUES(
                    ?, ?, (SELECT email_blob FROM aliases WHERE id = ?), ?, ?, ?
                )
                """,
                (clean_type, alias_id, alias_id, clean_outcome, clean_ip, _now()),
            )
        if self._audit_purge_is_due():
            self.purge_old_audit_events(days=AUDIT_RETENTION_DAYS)

    def _audit_purge_is_due(self) -> bool:
        with self._audit_lock:
            self._audit_writes += 1
            if self._audit_writes < AUDIT_PURGE_INTERVAL:
                return False
            self._audit_writes = 0
        return True

    def list_audit_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        rows = (
            self._connect()
            .execute(
                """
                SELECT id, event_type, alias_id, outcome, ip_digest, created_at
                FROM audit_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (bounded,),
            )
            .fetchall()
        )
        return [dict(row) for row in rows]

    def list_code_lookup_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        cutoff = _retention_cutoff(AUDIT_RETENTION_DAYS)
        rows = (
            self._connect()
            .execute(
                """
                SELECT id, alias_id, alias_email_blob, outcome, ip_digest, created_at
                FROM audit_events
                WHERE event_type = 'code_lookup' AND created_at >= ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (cutoff, bounded),
            )
            .fetchall()
        )
        events: list[dict[str, Any]] = []
        for row in rows:
            alias_email = None
            if row["alias_email_blob"] is not None:
                try:
                    alias_email = self.secret_box.decrypt(
                        bytes(row["alias_email_blob"]), "alias-email"
                    ).decode("utf-8")
                except (SecurityError, UnicodeDecodeError) as exc:
                    raise DatabaseError("audit alias email is invalid") from exc
            events.append(
                {
                    "id": int(row["id"]),
                    "alias_id": None if row["alias_id"] is None else str(row["alias_id"]),
                    "alias_email": alias_email,
                    "outcome": str(row["outcome"]),
                    "ip_digest": None if row["ip_digest"] is None else str(row["ip_digest"]),
                    "created_at": str(row["created_at"]),
                }
            )
        return events

    def purge_old_audit_events(self, *, days: int = 7) -> int:
        cutoff = _retention_cutoff(days)
        with self.transaction() as connection:
            cursor = connection.execute("DELETE FROM audit_events WHERE created_at < ?", (cutoff,))
            return int(cursor.rowcount)

    def _alias_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        email = self.secret_box.decrypt(bytes(row["email_blob"]), "alias-email").decode("utf-8")
        remote: dict[str, Any] | None = None
        if row["remote_blob"] is not None:
            try:
                decoded = json.loads(
                    self.secret_box.decrypt(bytes(row["remote_blob"]), "alias-remote").decode(
                        "utf-8"
                    )
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DatabaseError("alias remote metadata is invalid") from exc
            if not isinstance(decoded, dict):
                raise DatabaseError("alias remote metadata is invalid")
            remote = decoded
        return {
            "id": str(row["id"]),
            "email": email,
            "remote_metadata": remote,
            "label": str(row["label"]),
            "note": str(row["note"]),
            "sender_filter": str(row["sender_filter"]),
            "state": str(row["state"]),
            "has_access_key": row["access_key_hash"] is not None,
            "access_key_recoverable": (
                row["access_key_hash"] is not None and row["access_key_blob"] is not None
            ),
            "access_key_hint": (
                None if row["access_key_hint"] is None else str(row["access_key_hint"])
            ),
            "key_issued_at": row["key_issued_at"],
            "key_revoked_at": row["key_revoked_at"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "last_synced_at": row["last_synced_at"],
        }

    def close(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            self._local.connection = None
            with suppress(sqlite3.Error):
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            return connection
        # isolation_level=None keeps sqlite3 out of transaction management so that
        # transaction() owns every BEGIN/COMMIT on a connection that now outlives
        # the call. One connection per thread also satisfies check_same_thread.
        connection = sqlite3.connect(self.path, timeout=15.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        self._local.connection = connection
        return connection


__all__ = [
    "ConflictError",
    "Database",
    "DatabaseError",
    "IssuedAccessKey",
    "NotFoundError",
    "SCHEMA_VERSION",
]
