from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from icloud_gateway.config import Settings
from icloud_gateway.database import ConflictError, Database
from icloud_gateway.hme import HmeNetworkError, HmeRateLimitedError, ICloudHmeSession
from icloud_gateway.jobs import BatchJobManager, request_fingerprint
from icloud_gateway.security import SecretBox
from icloud_gateway.service import (
    GatewayBusyError,
    GatewayError,
    GatewayRetryableError,
    GatewayService,
)


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


def test_create_alias_job_accepts_one_hundred_serial_items(database: Database, tmp_path) -> None:
    manager = BatchJobManager(_Gateway(database, tmp_path), throttle_seconds=0)

    job, created = manager.create_alias_job(
        count=100,
        label_prefix="Team",
        note="",
        sender_filter="",
        idempotency_key=None,
    )

    assert created is True
    assert job["requested"] == 100
    stored = database.get_batch_job(job["id"])
    assert len(stored["items"]) == 100
    assert stored["items"][0]["input"]["label"] == "Team 1"
    assert stored["items"][-1]["input"]["label"] == "Team 100"
    assert {item["status"] for item in stored["items"]} == {"queued"}
    with pytest.raises(ValueError):
        manager.create_alias_job(
            count=101,
            label_prefix="Team",
            note="",
            sender_filter="",
            idempotency_key=None,
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


def test_interrupted_create_candidate_is_requeued_for_read_reconciliation(
    database: Database,
) -> None:
    job, _created = database.create_batch_job(
        kind="create_aliases",
        action="create",
        fingerprint=b"k" * 32,
        items=[
            {"label": "Team 1", "note": "", "sender_filter": ""},
            {"label": "Team 2", "note": "", "sender_filter": ""},
        ],
    )
    database.update_batch_item(
        job["id"],
        1,
        stage="generated",
        status="queued",
        result={"candidate": "saved@icloud.com"},
    )
    database.update_batch_item(job["id"], 1, stage="reserving", status="running")
    database.set_batch_job_status(job["id"], "running")

    database.recover_interrupted_batch_jobs()

    recovered = database.get_batch_job(job["id"])
    assert recovered["status"] == "queued"
    assert recovered["current"] == 0
    assert recovered["items"][0]["status"] == "queued"
    assert recovered["items"][0]["stage"] == "reconciling"
    assert recovered["items"][0]["result"] == {
        "candidate": "saved@icloud.com",
        "reconcile_before_reserve": True,
    }
    assert recovered["items"][1]["status"] == "queued"
    assert database.next_batch_job()["id"] == job["id"]


def test_stopped_create_job_only_resumes_after_explicit_candidate_reconciliation(
    database: Database,
) -> None:
    job, _created = database.create_batch_job(
        kind="create_aliases",
        action="create",
        fingerprint=b"m" * 32,
        items=[
            {"label": "Team 1", "note": "", "sender_filter": ""},
            {"label": "Team 2", "note": "", "sender_filter": ""},
        ],
    )
    database.update_batch_item(
        job["id"],
        1,
        stage="unknown",
        status="unknown",
        result={"candidate": "saved@icloud.com"},
        error="remote write outcome is unknown",
    )
    database.set_batch_job_status(job["id"], "needs_reconcile")

    assert database.next_batch_job() is None

    resumed = database.resume_reconcilable_create_job(job["id"])

    assert resumed["status"] == "queued"
    assert resumed["current"] == 0
    assert resumed["items"][0]["status"] == "queued"
    assert resumed["items"][0]["stage"] == "reconciling"
    assert resumed["items"][0]["result"]["reconcile_before_reserve"] is True
    assert resumed["items"][1]["status"] == "queued"
    assert database.next_batch_job()["id"] == job["id"]


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
    raw = (
        sqlite3.connect(database.path)
        .execute(
            "SELECT result_blob FROM batch_job_items WHERE job_id = ? AND item_index = 1",
            (job["id"],),
        )
        .fetchone()[0]
    )
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


def test_reconciliation_job_remains_visible_with_normalized_item_error(
    database: Database, tmp_path
) -> None:
    job, _created = database.create_batch_job(
        kind="bulk_aliases",
        action="deactivate",
        fingerprint=b"v" * 32,
        items=[{"alias_id": "one"}, {"alias_id": "two"}],
    )
    database.set_batch_job_status(job["id"], "running")
    database.update_batch_item(job["id"], 1, stage="executing", status="running", alias_id="one")
    database.recover_interrupted_batch_jobs()

    visible = BatchJobManager(_Gateway(database, tmp_path)).active_jobs()

    assert len(visible) == 1
    assert visible[0]["status"] == "needs_reconcile"
    assert visible[0]["results"][0]["error"] == "interrupted_in_flight"
    assert visible[0]["results"][1]["status"] == "queued"


def test_batch_job_claim_is_atomic_across_database_connections(
    database: Database,
) -> None:
    second_database = Database(database.path, SecretBox(bytes(range(32))))
    second_database.initialize()
    job, _created = database.create_batch_job(
        kind="bulk_aliases",
        action="issue_keys",
        fingerprint=b"c" * 32,
        items=[{"alias_id": "one"}],
    )
    barrier = threading.Barrier(2)
    results = []
    results_lock = threading.Lock()

    def claim(value: Database) -> None:
        barrier.wait()
        claimed = value.next_batch_job()
        with results_lock:
            results.append(claimed)

    first_thread = threading.Thread(target=claim, args=(database,))
    second_thread = threading.Thread(target=claim, args=(second_database,))
    try:
        first_thread.start()
        second_thread.start()
        first_thread.join(2)
        second_thread.join(2)

        assert not first_thread.is_alive()
        assert not second_thread.is_alive()
        claimed = [item for item in results if item is not None]
        assert len(claimed) == 1
        assert claimed[0]["id"] == job["id"]
        assert claimed[0]["status"] == "running"
    finally:
        second_database.close()


def test_two_managers_share_one_worker_owner_and_one_remote_side_effect(
    database: Database, tmp_path
) -> None:
    second_database = Database(database.path, SecretBox(bytes(range(32))))
    second_database.initialize()
    alias = database.upsert_alias(
        email="one@example.com", remote_metadata={"anonymousId": "one"}, state="active"
    )
    job, _created = database.create_batch_job(
        kind="bulk_aliases",
        action="deactivate",
        fingerprint=b"o" * 32,
        items=[{"alias_id": alias["id"]}],
    )
    calls = 0
    calls_lock = threading.Lock()
    entered = threading.Event()
    release = threading.Event()

    def deactivate(_alias_id):
        nonlocal calls
        with calls_lock:
            calls += 1
            entered.set()
        assert release.wait(2)
        return {"id": "one", "email": "one@example.com", "label": "One"}

    first_gateway = _Gateway(database, tmp_path)
    second_gateway = _Gateway(second_database, tmp_path)
    first_gateway.deactivate_alias = deactivate
    second_gateway.deactivate_alias = deactivate
    first = BatchJobManager(first_gateway, throttle_seconds=0)
    second = BatchJobManager(second_gateway, throttle_seconds=0)
    try:
        first.start()
        second.start()
        assert entered.wait(1)
        time.sleep(0.1)

        assert calls == 1
        assert sum((first.owns_worker, second.owns_worker)) == 1
        assert first.shutdown(timeout=0.05) is False
        assert first.owns_worker is True
        assert second.owns_worker is False
        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if database.get_batch_job(job["id"])["status"] == "completed":
                break
            time.sleep(0.01)
        assert database.get_batch_job(job["id"])["status"] == "completed"
        assert calls == 1
    finally:
        release.set()
        first.shutdown(timeout=2)
        second.shutdown(timeout=2)
        second_database.close()


def test_stop_during_freshness_keeps_unstarted_reserve_queued(tmp_path) -> None:
    freshness_entered = threading.Event()
    release_freshness = threading.Event()
    generated = []
    reserved = []

    class Client:
        def __init__(self, _session):
            pass

        def list_aliases(self):
            return []

        def generate_alias(self):
            generated.append(True)
            return "candidate@icloud.com"

        def reserve_alias(self, candidate, *, label, note):
            reserved.append((candidate, label, note))
            return {
                "hme": candidate,
                "anonymousId": "candidate",
                "isActive": True,
            }

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
    gateway.save_hme_session(
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
        )
    )

    def blocking_freshness():
        freshness_entered.set()
        assert release_freshness.wait(2)

    gateway._ensure_hme_fresh = blocking_freshness
    manager = BatchJobManager(gateway, throttle_seconds=0)
    job, _created = manager.create_alias_job(
        count=1,
        label_prefix="Team",
        note="",
        sender_filter="",
        idempotency_key=None,
    )
    try:
        manager.start()
        assert freshness_entered.wait(1)
        manager.request_stop()
        gateway.request_stop()
        release_freshness.set()
        assert manager.shutdown(timeout=2)

        current = gateway.database.get_batch_job(job["id"])
        assert current["status"] == "queued"
        assert current["items"][0]["status"] == "queued"
        assert generated == []
        assert reserved == []
    finally:
        release_freshness.set()
        manager.shutdown(timeout=2)
        gateway.shutdown()


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
    gateway._persist_rotated_hme_session = lambda _original, _candidate: False
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
    assert manager._create_alias(item, job_id=job["id"]) == "ok"
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


def test_successful_item_persists_rotated_session_for_the_next_item(tmp_path) -> None:
    original = ICloudHmeSession(
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
    )
    rotated = ICloudHmeSession.from_mapping(
        {**original.as_secret_dict(), "cookie": original.cookie.replace("token", "rotated")}
    )
    sessions_seen = []

    class Client:
        def __init__(self, session):
            self.session = session
            sessions_seen.append(session)

        def generate_alias(self):
            return f"new-{len(sessions_seen)}@icloud.com"

        def reserve_alias(self, candidate, *, label, note):
            self.session = rotated
            return {"hme": candidate, "anonymousId": candidate, "isActive": True}

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
    gateway.database.set_secret("hme_session", original.as_secret_dict())
    gateway._last_validated_ts = gateway.clock()
    manager = BatchJobManager(gateway, throttle_seconds=0)
    job, _created = manager.create_alias_job(
        count=2,
        label_prefix="Team",
        note="",
        sender_filter="",
        idempotency_key=None,
    )
    try:
        manager._run_job(gateway.database.get_batch_job(job["id"]))

        current = gateway.database.get_batch_job(job["id"])
        assert current["status"] == "completed"
        assert [session.cookie for session in sessions_seen] == [original.cookie, rotated.cookie]
        assert gateway.get_hme_session() == rotated
    finally:
        gateway.shutdown()


def test_stale_client_cookie_cannot_overwrite_a_newer_saved_session(tmp_path) -> None:
    gateway = GatewayService(
        Settings(
            data_dir=tmp_path,
            master_key=bytes(range(32)),
            admin_password="correct horse battery staple",
            cookie_secure=False,
            cdp_url="",
        ),
        start_maintenance=False,
    )
    original = ICloudHmeSession(
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
    )
    stale_rotation = ICloudHmeSession.from_mapping(
        {**original.as_secret_dict(), "cookie": original.cookie.replace("token", "stale")}
    )
    newer = ICloudHmeSession.from_mapping(
        {**original.as_secret_dict(), "cookie": original.cookie.replace("token", "newer")}
    )
    gateway.database.set_secret("hme_session", newer.as_secret_dict())
    try:
        assert gateway._persist_rotated_hme_session(original, stale_rotation) is False
        assert gateway.get_hme_session() == newer
    finally:
        gateway.shutdown()


def test_lost_reserve_response_is_reconciled_without_duplicate_write(tmp_path) -> None:
    remote: list[dict[str, object]] = []
    reserve_calls = 0
    list_calls = 0

    class Client:
        def __init__(self, session):
            self.session = session

        def close(self):
            pass

        def list_aliases(self):
            nonlocal list_calls
            list_calls += 1
            return list(remote)

        def generate_alias(self):
            return "candidate@icloud.com"

        def reserve_alias(self, candidate, *, label, note):
            nonlocal reserve_calls
            reserve_calls += 1
            created = {
                "hme": candidate,
                "anonymousId": "candidate",
                "isActive": True,
                "label": label,
                "note": note,
            }
            remote.append(created)
            raise HmeNetworkError("connection lost after request")

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
    manager = BatchJobManager(
        gateway,
        throttle_seconds=0,
        transient_retry_base_seconds=0,
        transient_retry_max_seconds=0,
    )
    job, _created = manager.create_alias_job(
        count=1,
        label_prefix="Team",
        note="",
        sender_filter="",
        idempotency_key=None,
    )
    try:
        manager._run_job(gateway.database.get_batch_job(job["id"]))

        current = gateway.database.get_batch_job(job["id"])
        assert current["status"] == "completed"
        assert current["succeeded"] == 1
        assert reserve_calls == 1
        assert list_calls == 1
        assert len(remote) == 1
    finally:
        gateway.shutdown()


def test_unavailable_reconciliation_keeps_create_item_queued_without_candidate_leak(
    tmp_path,
) -> None:
    class Client:
        def __init__(self, session):
            self.session = session

        def close(self):
            pass

        def list_aliases(self):
            raise HmeNetworkError("read path unavailable")

        def generate_alias(self):
            return "candidate@icloud.com"

        def reserve_alias(self, candidate, *, label, note):
            raise HmeNetworkError("connection lost after request")

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
    manager = BatchJobManager(
        gateway,
        throttle_seconds=0,
        transient_retry_base_seconds=60,
        transient_retry_max_seconds=60,
    )
    job, _created = manager.create_alias_job(
        count=1,
        label_prefix="Team",
        note="",
        sender_filter="",
        idempotency_key=None,
    )
    try:
        item = gateway.database.get_batch_job(job["id"])["items"][0]

        assert manager._create_alias(item, job_id=job["id"]) == "retry"

        current = gateway.database.get_batch_job(job["id"])
        assert current["status"] == "queued"
        assert current["items"][0]["status"] == "queued"
        assert current["items"][0]["stage"] == "waiting_reconcile"
        public = manager.public_job(job["id"])
        assert public["wait_reason"] == "transient_error"
        assert public["retry_kind"] == "network"
        assert public["retry_after_seconds"] >= 0
        assert "candidate" not in public["results"][0]
        assert "reconcile_before_reserve" not in public["results"][0]
    finally:
        gateway.shutdown()


def test_fifty_item_job_retries_absent_candidate_and_finishes(tmp_path) -> None:
    remote: list[dict[str, object]] = []
    generate_calls = 0
    reserve_calls = 0
    failed_candidate = ""

    class Client:
        def __init__(self, session):
            self.session = session

        def close(self):
            pass

        def list_aliases(self):
            return list(remote)

        def generate_alias(self):
            nonlocal generate_calls
            generate_calls += 1
            return f"candidate-{generate_calls}@icloud.com"

        def reserve_alias(self, candidate, *, label, note):
            nonlocal failed_candidate, reserve_calls
            reserve_calls += 1
            if reserve_calls == 6:
                failed_candidate = candidate
                raise HmeNetworkError("connection lost before response")
            created = {
                "hme": candidate,
                "anonymousId": candidate,
                "isActive": True,
                "label": label,
                "note": note,
            }
            remote.append(created)
            return created

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
    manager = BatchJobManager(
        gateway,
        throttle_seconds=0,
        transient_retry_base_seconds=0,
        transient_retry_max_seconds=0,
    )
    job, _created = manager.create_alias_job(
        count=50,
        label_prefix="Team",
        note="",
        sender_filter="",
        idempotency_key=None,
    )
    try:
        manager._run_job(gateway.database.get_batch_job(job["id"]))

        current = gateway.database.get_batch_job(job["id"])
        assert current["status"] == "completed"
        assert current["succeeded"] == 50
        assert all(item["status"] == "success" for item in current["items"])
        assert generate_calls == 50
        assert reserve_calls == 51
        assert failed_candidate
        assert sum(item["hme"] == failed_candidate for item in remote) == 1
        assert len(remote) == 50
    finally:
        gateway.shutdown()


def test_rate_limited_create_cools_down_then_continues(database: Database, tmp_path) -> None:
    gateway = _Gateway(database, tmp_path)
    gateway._remote_write_active = False
    gateway._hme_lock = threading.RLock()
    gateway._begin_remote_write = lambda: setattr(gateway, "_remote_write_active", True)
    gateway._finish_remote_write = lambda: setattr(gateway, "_remote_write_active", False)
    gateway._ensure_hme_fresh = lambda: None
    gateway.get_hme_session = lambda: object()
    gateway._close_client = lambda _client: None
    gateway._persist_rotated_hme_session = lambda _original, _candidate: False
    generate_calls = 0
    reserve_calls = 0

    class Client:
        def generate_alias(self):
            nonlocal generate_calls
            generate_calls += 1
            if generate_calls == 2:
                raise HmeRateLimitedError(code="-41015", retry_after_seconds=60)
            return f"new-{generate_calls}@icloud.com"

        def reserve_alias(self, candidate, *, label, note):
            nonlocal reserve_calls
            reserve_calls += 1
            return {"hme": candidate, "anonymousId": candidate, "isActive": True}

    gateway.hme_client_factory = lambda _session: Client()
    # Tiny cooldown so the unit test can resume without sleeping 30 minutes.
    manager = BatchJobManager(gateway, throttle_seconds=0, rate_limit_cooldown_seconds=60)
    sleeps: list[float] = []

    def fake_wait(timeout=None):
        sleeps.append(float(timeout or 0.0))
        # Expire cooldown after the first wait slice is observed.
        if len(sleeps) == 1:
            job_id = job["id"]
            item = database.get_batch_job(job_id)["items"][1]
            result = dict(item["result"] or {})
            result["resume_at"] = "2000-01-01T00:00:00.000Z"
            database.update_batch_item(
                job_id,
                2,
                stage="waiting_quota",
                status="queued",
                result=result,
                error=item.get("error"),
            )
        return False

    manager._stop = threading.Event()
    manager._stop.wait = fake_wait  # type: ignore[method-assign]
    job, _created = manager.create_alias_job(
        count=3,
        label_prefix="Team",
        note="",
        sender_filter="",
        idempotency_key=None,
    )

    manager._run_job(database.get_batch_job(job["id"]))

    current = database.get_batch_job(job["id"])
    assert current["status"] == "completed"
    assert current["succeeded"] == 3
    assert generate_calls >= 3
    assert reserve_calls == 3
    assert sleeps  # cooled down instead of needs_reconcile
    assert all(item["status"] == "success" for item in current["items"])
    public = manager.public_job(job["id"])
    assert public["status"] == "completed"


def test_rate_limited_generate_keeps_item_queued_for_resume(database: Database, tmp_path) -> None:
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
            raise HmeRateLimitedError(code="-41015", retry_after_seconds=120)

        def reserve_alias(self, candidate, *, label, note):
            raise AssertionError("reserve must not run after generate rate limit")

    gateway.hme_client_factory = lambda _session: Client()
    manager = BatchJobManager(gateway, throttle_seconds=0, rate_limit_cooldown_seconds=120)
    job, _created = database.create_batch_job(
        kind="create_aliases",
        action="create",
        fingerprint=b"q" * 32,
        items=[{"label": "Team", "note": "", "sender_filter": ""}],
    )
    item = database.get_batch_job(job["id"])["items"][0]

    assert manager._create_alias(item, job_id=job["id"]) == "rate_limited"
    recovered = database.get_batch_job(job["id"])
    assert recovered["status"] == "queued"
    assert recovered["items"][0]["status"] == "queued"
    assert recovered["items"][0]["stage"] == "waiting_quota"
    assert recovered["items"][0]["result"]["wait_reason"] == "rate_limited"
    assert recovered["items"][0]["result"]["code"] == "-41015"
    public = manager.public_job(job["id"])
    assert public["wait_reason"] == "rate_limited"
    assert public["retry_after_seconds"] >= 0


def test_ten_item_job_reports_five_success_one_unknown_and_four_queued(
    database: Database, tmp_path
) -> None:
    gateway = _Gateway(database, tmp_path)
    manager = BatchJobManager(gateway, throttle_seconds=0)
    job, _created = manager.create_alias_job(
        count=10,
        label_prefix="Team",
        note="",
        sender_filter="",
        idempotency_key=None,
    )
    calls = 0

    def run_item(_job, item):
        nonlocal calls
        calls += 1
        if calls <= 5:
            database.update_batch_item(
                job["id"],
                item["index"],
                stage="completed",
                status="success",
                alias_id=f"alias-{calls}",
                result={"id": f"alias-{calls}"},
            )
            return "ok"
        database.update_batch_item(
            job["id"],
            item["index"],
            stage="unknown",
            status="unknown",
            error="remote write outcome is unknown",
        )
        return "unknown"

    manager._run_item = run_item
    manager._run_job(database.get_batch_job(job["id"]))

    current = database.get_batch_job(job["id"])
    assert current["status"] == "needs_reconcile"
    assert current["succeeded"] == 5
    assert current["current"] == 6
    assert [item["status"] for item in current["items"]] == [
        "success",
        "success",
        "success",
        "success",
        "success",
        "unknown",
        "queued",
        "queued",
        "queued",
        "queued",
    ]
    assert calls == 6


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
    first = database.upsert_alias(
        email="one@icloud.com", remote_metadata={"anonymousId": "one"}, state="active"
    )
    second = database.upsert_alias(
        email="two@icloud.com", remote_metadata={"anonymousId": "two"}, state="active"
    )
    gateway = _Gateway(database, tmp_path)
    manager = BatchJobManager(gateway, throttle_seconds=0)
    job, _created = manager.create_bulk_job(
        action="deactivate",
        alias_ids=[first["id"], second["id"]],
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


def test_bulk_delete_rejects_active_aliases_before_creating_a_job(
    database: Database, tmp_path
) -> None:
    alias = database.upsert_alias(
        email="active@icloud.com", remote_metadata={"anonymousId": "active"}, state="active"
    )
    manager = BatchJobManager(_Gateway(database, tmp_path), throttle_seconds=0)

    with pytest.raises(ValueError, match="requires inactive aliases"):
        manager.create_bulk_job(
            action="delete",
            alias_ids=[alias["id"]],
            confirmed=True,
            idempotency_key=None,
        )

    assert database.list_active_batch_jobs() == []


def test_retryable_auth_rejection_is_failed_not_unknown(database: Database, tmp_path) -> None:
    alias = database.upsert_alias(
        email="active@icloud.com", remote_metadata={"anonymousId": "active"}, state="active"
    )
    gateway = _Gateway(database, tmp_path)
    gateway.deactivate_alias = lambda _alias_id: (_ for _ in ()).throw(
        GatewayRetryableError("authentication refreshed; retry explicitly")
    )
    manager = BatchJobManager(gateway, throttle_seconds=0)
    job, _created = manager.create_bulk_job(
        action="deactivate",
        alias_ids=[alias["id"]],
        confirmed=True,
        idempotency_key=None,
    )

    manager._run_job(database.get_batch_job(job["id"]))

    current = database.get_batch_job(job["id"])
    assert current["status"] == "failed"
    assert current["items"][0]["status"] == "failed"
    assert current["items"][0]["error"] == "GatewayRetryableError"
