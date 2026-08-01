from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from icloud_gateway.config import Settings
from icloud_gateway.database import ConflictError, Database
from icloud_gateway.hme import ICloudHmeSession
from icloud_gateway.jobs import BatchJobManager, request_fingerprint
from icloud_gateway.security import SecretBox
from icloud_gateway.service import GatewayBusyError, GatewayError, GatewayService


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "gateway.sqlite3", SecretBox(bytes(range(32))))
    value.initialize()
    yield value
    value.close()


def _payload(label: str = "Team") -> dict[str, object]:
    return {"count": 1, "label_prefix": label, "note": "", "sender_filter": ""}


def test_job_creation_and_idempotency_conflict(database: Database) -> None:
    payload = _payload()
    fingerprint = request_fingerprint("create_aliases", payload)
    first, created = database.create_batch_job(
        kind="create_aliases",
        action="create",
        fingerprint=fingerprint,
        items=[{"label": "Team", "note": "", "sender_filter": ""}],
        idempotency_key="same-request",
    )
    repeated, repeated_created = database.create_batch_job(
        kind="create_aliases",
        action="create",
        fingerprint=fingerprint,
        items=[{"label": "Team", "note": "", "sender_filter": ""}],
        idempotency_key="same-request",
    )

    assert created is True
    assert repeated_created is False
    assert repeated["id"] == first["id"]
    with pytest.raises(ConflictError):
        database.create_batch_job(
            kind="create_aliases",
            action="create",
            fingerprint=request_fingerprint("create_aliases", _payload("Other")),
            items=[{"label": "Other", "note": "", "sender_filter": ""}],
            idempotency_key="same-request",
        )


def test_interrupted_running_item_requires_reconciliation_and_is_not_requeued(
    database: Database,
) -> None:
    job, _created = database.create_batch_job(
        kind="bulk_aliases",
        action="deactivate",
        fingerprint=b"x" * 32,
        items=[{"alias_id": "alias-one"}, {"alias_id": "alias-two"}],
    )
    database.set_batch_job_status(job["id"], "running")
    database.update_batch_item(
        job["id"], 1, stage="executing", status="running", alias_id="alias-one"
    )

    database.recover_interrupted_batch_jobs()

    recovered = database.get_batch_job(job["id"])
    assert recovered["status"] == "needs_reconcile"
    assert recovered["items"][0]["status"] == "unknown"
    assert recovered["items"][1]["status"] == "queued"
    assert database.next_batch_job() is None


def test_completed_items_survive_restart_without_secret_job_results(database: Database) -> None:
    job, _created = database.create_batch_job(
        kind="bulk_aliases",
        action="issue_keys",
        fingerprint=b"y" * 32,
        items=[{"alias_id": "one"}, {"alias_id": "two"}],
    )
    database.update_batch_item(
        job["id"],
        1,
        stage="completed",
        status="success",
        alias_id="one",
        result={"id": "one", "email": "one@example.com"},
    )
    database.set_batch_job_status(job["id"], "running")
    database.recover_interrupted_batch_jobs()

    recovered = database.get_batch_job(job["id"])
    assert recovered["status"] == "queued"
    assert recovered["items"][0]["status"] == "success"
    assert recovered["items"][1]["status"] == "queued"
    raw = sqlite3.connect(database.path).execute(
        "SELECT result_blob FROM batch_job_items WHERE job_id = ? AND item_index = 1",
        (job["id"],),
    ).fetchone()[0]
    assert b"one@example.com" not in raw
    assert b"icg_" not in raw


class _Gateway:
    def __init__(self, database: Database, tmp_path) -> None:
        self.database = database
        self.settings = Settings(
            data_dir=tmp_path,
            master_key=bytes(range(32)),
            admin_password="correct horse battery staple",
            cookie_secure=False,
            cdp_url="",
        )
        self._hme_lock = __import__("threading").RLock()
        self.calls = 0

    def deactivate_alias(self, _alias_id):
        self.calls += 1
        raise GatewayError("confirmation failed after remote write")


def test_generated_candidate_is_reserved_without_generating_again(
    database: Database, tmp_path
) -> None:
    gateway = _Gateway(database, tmp_path)
    gateway._remote_write_active = False
    gateway._hme_lock = threading.RLock()
    gateway._begin_remote_write = lambda: setattr(gateway, "_remote_write_active", True)
    gateway._finish_remote_write = lambda: setattr(gateway, "_remote_write_active", False)
    gateway._ensure_hme_fresh = lambda: None
    gateway.get_hme_session = lambda: object()
    gateway._close_client = lambda _client: None
    generated = []
    reserved = []

    class Client:
        def generate_alias(self):
            generated.append(True)
            return "new@icloud.com"

        def reserve_alias(self, candidate, *, label, note):
            reserved.append(candidate)
            return {"hme": candidate, "anonymousId": "candidate", "isActive": True}

    gateway.hme_client_factory = lambda _session: Client()
    job, _created = database.create_batch_job(
        kind="create_aliases",
        action="create",
        fingerprint=b"z" * 32,
        items=[{"label": "Team", "note": "", "sender_filter": ""}],
    )
    database.update_batch_item(
        job["id"],
        1,
        stage="generated",
        status="queued",
        result={"candidate": "saved@icloud.com"},
    )
    item = database.get_batch_job(job["id"])["items"][0]

    manager = BatchJobManager(gateway, throttle_seconds=0)
    assert manager._create_alias(item, job_id=job["id"]) is False
    assert generated == []
    assert reserved == ["saved@icloud.com"]
    assert gateway._remote_write_active is False


def test_create_job_uses_remote_write_fence_and_key_issue_is_busy(tmp_path) -> None:
    entered = threading.Event()
    release = threading.Event()

    class Client:
        def __init__(self, _session):
            pass

        def list_aliases(self):
            return []

        def create_alias(self, *, label, note):
            entered.set()
            assert release.wait(2)
            return {"hme": "new@icloud.com", "anonymousId": "new", "isActive": True}

    gateway = GatewayService(
        Settings(
            data_dir=tmp_path,
            master_key=bytes(range(32)),
            admin_password="correct horse battery staple",
            cookie_secure=False,
            cdp_url="",
        ),
        hme_client_factory=Client,
        start_maintenance=False,
    )
    gateway.database.set_secret(
        "hme_session",
        ICloudHmeSession(
            host="p123-maildomainws.icloud.com.cn",
            dsid="123",
            client_id="client",
            client_build_number="build",
            client_mastering_number="master",
            cookie=(
                "X-APPLE-DS-WEB-SESSION-TOKEN=session; "
                "X-APPLE-WEBAUTH-USER=user; X-APPLE-WEBAUTH-TOKEN=token"
            ),
            origin="https://www.icloud.com.cn",
            referer="https://www.icloud.com.cn/icloudplus/",
        ).as_secret_dict(),
    )
    gateway._last_validated_ts = gateway.clock()
    alias = gateway.database.upsert_alias(
        email="existing@icloud.com", remote_metadata={"anonymousId": "existing"}
    )
    job, _created = gateway.database.create_batch_job(
        kind="create_aliases",
        action="create",
        fingerprint=b"w" * 32,
        items=[{"label": "Team", "note": "", "sender_filter": ""}],
    )
    item = gateway.database.get_batch_job(job["id"])["items"][0]
    generation = gateway._alias_generation
    manager = BatchJobManager(gateway, throttle_seconds=0)
    worker = threading.Thread(target=lambda: manager._create_alias(item, job_id=job["id"]))
    worker.start()
    assert entered.wait(1)
    assert gateway._alias_generation == generation + 1
    with pytest.raises(GatewayBusyError):
        gateway.issue_access_key(alias["id"])
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    gateway.shutdown()


def test_reserve_failure_is_unknown_and_stops_replay(database: Database, tmp_path) -> None:
    gateway = _Gateway(database, tmp_path)
    gateway._remote_write_active = False
    gateway._hme_lock = threading.RLock()
    gateway._begin_remote_write = lambda: setattr(gateway, "_remote_write_active", True)
    gateway._finish_remote_write = lambda: setattr(gateway, "_remote_write_active", False)
    gateway._ensure_hme_fresh = lambda: None
    gateway.get_hme_session = lambda: object()
    gateway._close_client = lambda _client: None

    class Client:
        def generate_alias(self):
            return "candidate@icloud.com"

        def reserve_alias(self, candidate, *, label, note):
            raise RuntimeError("connection lost after request")

    gateway.hme_client_factory = lambda _session: Client()
    job, _created = database.create_batch_job(
        kind="create_aliases",
        action="create",
        fingerprint=b"r" * 32,
        items=[{"label": "Team", "note": "", "sender_filter": ""}],
    )
    item = database.get_batch_job(job["id"])["items"][0]

    assert BatchJobManager(gateway)._create_alias(item, job_id=job["id"]) is True
    recovered = database.get_batch_job(job["id"])["items"][0]
    assert recovered["status"] == "unknown"
    assert recovered["result"] == {"candidate": "candidate@icloud.com"}


def test_shutdown_reports_a_blocked_worker(database: Database, tmp_path) -> None:
    gateway = _Gateway(database, tmp_path)
    manager = BatchJobManager(gateway)
    release = threading.Event()
    manager._thread = threading.Thread(target=release.wait, daemon=True)
    manager._thread.start()

    started = time.monotonic()
    assert manager.shutdown(timeout=0.05) is False
    assert time.monotonic() - started < 0.5
    assert database.quick_check() == "ok"
    release.set()
    manager._thread.join(1)


def test_unknown_remote_write_stops_worker_without_replay(database: Database, tmp_path) -> None:
    gateway = _Gateway(database, tmp_path)
    manager = BatchJobManager(gateway, throttle_seconds=0)
    job, _created = manager.create_bulk_job(
        action="deactivate",
        alias_ids=["one", "two"],
        confirmed=True,
        idempotency_key=None,
    )
    manager.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        current = database.get_batch_job(job["id"])
        if current["status"] == "needs_reconcile":
            break
        time.sleep(0.01)
    manager.shutdown()

    current = database.get_batch_job(job["id"])
    assert current["status"] == "needs_reconcile"
    assert current["items"][0]["status"] == "unknown"
    assert current["items"][1]["status"] == "queued"
    assert gateway.calls == 1
