from __future__ import annotations

import sqlite3
import threading

import pytest

import icloud_gateway.database as database_module
from icloud_gateway.database import ConflictError, Database
from icloud_gateway.security import SecretBox, hash_access_key

MASTER_KEY = bytes(reversed(range(32)))


@pytest.fixture
def database(tmp_path):
    value = Database(tmp_path / "gateway.sqlite3", SecretBox(MASTER_KEY))
    value.initialize()
    yield value
    value.close()


def test_database_round_trips_encrypted_settings_and_passes_quick_check(database) -> None:
    database.set_secret(
        "imap",
        {
            "host": "imap.example.com",
            "username": "forward@example.com",
            "password": "app-password-canary",
        },
    )

    assert database.quick_check() == "ok"
    assert database.get_secret("imap") == {
        "host": "imap.example.com",
        "username": "forward@example.com",
        "password": "app-password-canary",
    }
    raw = database.path.read_bytes()
    assert b"app-password-canary" not in raw
    assert b"forward@example.com" not in raw


def test_alias_email_and_remote_metadata_are_encrypted_at_rest(database) -> None:
    alias = database.upsert_alias(
        email="Alias-One@iCloud.com",
        remote_metadata={"anonymousId": "remote-secret", "isActive": True},
        label="Person one",
    )

    assert alias["email"] == "alias-one@icloud.com"
    assert alias["remote_metadata"]["anonymousId"] == "remote-secret"
    raw = database.path.read_bytes()
    assert b"alias-one@icloud.com" not in raw
    assert b"remote-secret" not in raw


def test_issuing_a_new_key_atomically_revokes_the_previous_key(database) -> None:
    alias = database.upsert_alias(
        email="target@icloud.com",
        remote_metadata={"anonymousId": "remote-id"},
        label="Target",
    )
    first = database.issue_access_key(alias["id"])
    assert (
        database.find_alias_by_access_key_hash(hash_access_key(first.access_key))["id"]
        == alias["id"]
    )

    second = database.issue_access_key(alias["id"])

    assert database.find_alias_by_access_key_hash(hash_access_key(first.access_key)) is None
    assert (
        database.find_alias_by_access_key_hash(hash_access_key(second.access_key))["id"]
        == alias["id"]
    )
    assert database.get_alias(alias["id"])["access_key_hint"] == second.hint


def test_revoked_or_inactive_alias_cannot_be_resolved(database) -> None:
    alias = database.upsert_alias(
        email="target@icloud.com",
        remote_metadata={"anonymousId": "remote-id"},
    )
    issued = database.issue_access_key(alias["id"])
    digest = hash_access_key(issued.access_key)

    database.revoke_access_key(alias["id"])
    assert database.find_alias_by_access_key_hash(digest) is None

    database.issue_access_key(alias["id"])
    database.set_alias_state(alias["id"], "inactive")
    assert database.get_alias(alias["id"])["has_access_key"] is False
    with pytest.raises(ConflictError):
        database.issue_access_key(alias["id"])


def test_upsert_preserves_access_key_while_refreshing_remote_state(database) -> None:
    alias = database.upsert_alias(
        email="target@icloud.com",
        remote_metadata={"anonymousId": "remote-id", "isActive": True},
        label="Original",
    )
    issued = database.issue_access_key(alias["id"])

    refreshed = database.upsert_alias(
        email="target@icloud.com",
        remote_metadata={"anonymousId": "remote-id", "isActive": True, "label": "Remote"},
        label="Updated",
        synced_at="2026-07-25T12:00:00.000Z",
    )

    assert refreshed["id"] == alias["id"]
    assert refreshed["label"] == "Updated"
    assert refreshed["has_access_key"] is True
    assert (
        database.find_alias_by_access_key_hash(hash_access_key(issued.access_key))["id"]
        == alias["id"]
    )


def test_audit_events_never_need_sensitive_payloads(database) -> None:
    alias = database.upsert_alias(email="target@icloud.com", remote_metadata=None)
    database.record_audit_event(
        "code_lookup", "not_found", alias_id=alias["id"], ip_digest="4f8a1c"
    )

    event = database.list_audit_events(limit=1)[0]

    assert event["event_type"] == "code_lookup"
    assert event["outcome"] == "not_found"
    assert set(event) == {"id", "event_type", "alias_id", "outcome", "ip_digest", "created_at"}


def test_connection_is_reused_per_thread_and_reopened_after_close(database) -> None:
    first = database._connect()
    assert database._connect() is first

    worker_matches_main: list[bool] = []

    def probe_worker_connection() -> None:
        worker_matches_main.append(database._connect() is first)
        database.close()

    worker = threading.Thread(target=probe_worker_connection)
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert worker_matches_main == [False]

    database.close()
    assert database._connect() is not first


def test_reused_connection_rolls_back_a_failed_transaction(database) -> None:
    with (
        pytest.raises(RuntimeError, match="rollback probe"),
        database.transaction() as connection,
    ):
        connection.execute("INSERT INTO metadata(key, value) VALUES('rollback_probe', 'present')")
        raise RuntimeError("rollback probe")

    row = (
        database._connect()
        .execute("SELECT value FROM metadata WHERE key = 'rollback_probe'")
        .fetchone()
    )
    assert row is None


def test_reused_connection_rolls_back_when_commit_fails(database) -> None:
    connection = database._connect()
    connection.execute("PRAGMA defer_foreign_keys = ON")

    with pytest.raises(sqlite3.IntegrityError), database.transaction() as transaction:
        transaction.execute(
            """
            INSERT INTO audit_events(event_type, alias_id, outcome, created_at)
            VALUES('commit_probe', 'missing-alias', 'failed', '2026-07-26T00:00:00.000Z')
            """
        )

    assert connection.in_transaction is False
    database.record_audit_event("after_commit_failure", "succeeded")


def test_aliases_are_listed_active_first(database) -> None:
    database.upsert_alias(
        email="inactive@icloud.com",
        remote_metadata=None,
        state="inactive",
    )
    database.upsert_alias(
        email="active@icloud.com",
        remote_metadata=None,
        state="active",
    )

    assert [item["state"] for item in database.list_aliases()] == ["active", "inactive"]


def test_audit_retention_is_enforced_during_long_lived_operation(database, monkeypatch) -> None:
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO audit_events(event_type, outcome, created_at)
            VALUES('expired', 'old', '2000-01-01T00:00:00.000Z')
            """
        )
    monkeypatch.setattr(database_module, "AUDIT_PURGE_INTERVAL", 1)

    database.record_audit_event("current", "kept")

    remaining = (
        database._connect().execute("SELECT event_type FROM audit_events ORDER BY id").fetchall()
    )
    assert [str(row["event_type"]) for row in remaining] == ["current"]


def test_schema_rejects_an_unknown_version(tmp_path) -> None:
    path = tmp_path / "gateway.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO metadata(key, value) VALUES('schema_version', '999')")
    connection.commit()
    connection.close()

    with pytest.raises(Exception, match="schema version"):
        Database(path, SecretBox(MASTER_KEY)).initialize()
