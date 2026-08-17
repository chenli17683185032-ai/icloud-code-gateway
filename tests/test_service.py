from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from icloud_gateway.config import Settings
from icloud_gateway.database import ConflictError, NotFoundError
from icloud_gateway.hme import HmeError, HmeNetworkError, HmeSessionError, ICloudHmeSession
from icloud_gateway.imap_otp import (
    ImapConfig,
    ImapCredentialsError,
    OtpResult,
    RecentOtpBatch,
    RecentOtpResult,
)
from icloud_gateway.rate_limit import SlidingWindowRateLimiter
from icloud_gateway.security import hash_access_key
from icloud_gateway.service import (
    GatewayBusyError,
    GatewayError,
    GatewayNotAllowedError,
    GatewayNotConfiguredError,
    GatewayRateLimitedError,
    GatewayRetryableError,
    GatewayService,
)

NOW = 1_800_000_000.0


def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        master_key=bytes(range(32)),
        admin_password="correct horse battery staple",
        cookie_secure=False,
        cdp_url="",
    )


def hme_session() -> ICloudHmeSession:
    return ICloudHmeSession(
        host="p123-maildomainws.icloud.com.cn",
        dsid="123",
        client_id="client",
        client_build_number="build",
        client_mastering_number="master",
        cookie=(
            "X-APPLE-DS-WEB-SESSION-TOKEN=session; "
            "X-APPLE-WEBAUTH-USER=user; "
            "X-APPLE-WEBAUTH-TOKEN=token"
        ),
        origin="https://www.icloud.com.cn",
        referer="https://www.icloud.com.cn/icloudplus/",
    )


class FakeHmeClient:
    aliases = []
    created = []
    lifecycle_error = None
    confirm_lifecycle = True
    lifecycle_calls = []

    def __init__(self, _session):
        pass

    def list_aliases(self):
        return [dict(item) for item in self.aliases]

    def create_alias(self, *, label, note):
        if not self.created:
            raise HmeError("rate limited")
        outcome = self.created.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        item = dict(outcome)
        item["label"] = label
        item["note"] = note
        return item

    def _change_state(self, action, anonymous_id, state=None):
        self.lifecycle_calls.append((action, anonymous_id))
        if self.lifecycle_error is not None:
            raise self.lifecycle_error
        if not self.confirm_lifecycle:
            return {}
        if action == "delete":
            self.aliases[:] = [
                item for item in self.aliases if item.get("anonymousId") != anonymous_id
            ]
            return {}
        for item in self.aliases:
            if item.get("anonymousId") == anonymous_id:
                item["isActive"] = state
                break
        return {}

    def deactivate_alias(self, anonymous_id):
        return self._change_state("deactivate", anonymous_id, False)

    def reactivate_alias(self, anonymous_id):
        return self._change_state("reactivate", anonymous_id, True)

    def delete_alias(self, anonymous_id):
        return self._change_state("delete", anonymous_id)


class FakeReader:
    result = None
    recent_batch = RecentOtpBatch(items=(), scanned=0, truncated=False)
    recent_error = None
    checked = []
    recent_calls = []
    last_kwargs = {}

    def __init__(self, config):
        self.config = config

    def check(self, *, timeout):
        self.checked.append((self.config, timeout))

    def find_latest_code(self, alias, **kwargs):
        type(self).last_alias = alias
        type(self).last_kwargs = kwargs
        if self.recent_error is not None:
            raise self.recent_error
        return self.result

    def find_recent_codes(self, aliases, **kwargs):
        self.recent_calls.append((tuple(aliases), kwargs))
        if self.recent_error is not None:
            raise self.recent_error
        return self.recent_batch


@pytest.fixture(autouse=True)
def reset_fakes():
    FakeHmeClient.aliases = []
    FakeHmeClient.created = []
    FakeHmeClient.lifecycle_error = None
    FakeHmeClient.confirm_lifecycle = True
    FakeHmeClient.lifecycle_calls = []
    FakeReader.result = None
    FakeReader.recent_batch = RecentOtpBatch(items=(), scanned=0, truncated=False)
    FakeReader.recent_error = None
    FakeReader.checked = []
    FakeReader.recent_calls = []
    FakeReader.last_kwargs = {}


def service(tmp_path, *, limiter=None, sleeper=lambda _seconds: None) -> GatewayService:
    return GatewayService(
        settings(tmp_path),
        hme_client_factory=FakeHmeClient,
        hme_session_refresher=lambda session: session,
        imap_reader_factory=FakeReader,
        rate_limiter=limiter,
        clock=lambda: NOW,
        sleeper=sleeper,
    )


def configure_imap(value: GatewayService) -> ImapConfig:
    return value.configure_imap(
        {
            "forwarding_email": "forwarding@example.com",
            "host": "imap.example.com",
            "port": 993,
            "username": "forwarding@example.com",
            "password": "app-password",
            "folder": "INBOX",
            "junk_folder": "Junk",
            "proxy": "",
        }
    )


def test_hme_session_is_saved_only_after_read_only_validation(tmp_path) -> None:
    value = service(tmp_path)
    FakeHmeClient.aliases = [{"hme": "one@icloud.com", "anonymousId": "one", "isActive": True}]

    assert value.save_hme_session(hme_session()) == 1
    assert value.get_hme_session() == hme_session()


def test_hme_session_save_imports_historical_aliases_and_active_alias_can_receive_key(
    tmp_path,
) -> None:
    value = service(tmp_path)
    FakeHmeClient.aliases = [
        {
            "hme": "active@icloud.com",
            "anonymousId": "active-id",
            "label": "Historical active",
            "isActive": True,
        },
        {
            "hme": "inactive@icloud.com",
            "anonymousId": "inactive-id",
            "label": "Historical inactive",
            "isActive": False,
        },
    ]

    assert value.save_hme_session(hme_session()) == 2

    aliases = value.database.list_aliases()
    active = next(item for item in aliases if item["email"] == "active@icloud.com")
    inactive = next(item for item in aliases if item["email"] == "inactive@icloud.com")
    assert active["state"] == "active"
    assert inactive["state"] == "inactive"
    assert value.issue_access_key(active["id"]).access_key.startswith("icg_")
    with pytest.raises(ConflictError):
        value.issue_access_key(inactive["id"])

    class RejectingClient(FakeHmeClient):
        def list_aliases(self):
            raise HmeError("rejected")

    value.hme_client_factory = RejectingClient
    with pytest.raises(HmeError):
        value.save_hme_session(
            ICloudHmeSession(**{**hme_session().as_secret_dict(), "client_id": "new-client"})
        )
    assert value.get_hme_session() == hme_session()


def test_default_hme_client_uses_the_configured_proxy(tmp_path) -> None:
    configured = replace(
        settings(tmp_path),
        hme_proxy="http://user:pass@proxy.example:8080",
    )
    value = GatewayService(configured, imap_reader_factory=FakeReader)

    client = value.hme_client_factory(hme_session())

    assert client.proxy == "http://user:pass@proxy.example:8080"


def test_remote_sync_preserves_local_configuration_and_revokes_missing_alias(tmp_path) -> None:
    value = service(tmp_path)
    FakeHmeClient.aliases = [
        {
            "hme": "one@icloud.com",
            "anonymousId": "one",
            "label": "Remote one",
            "isActive": True,
        },
        {
            "hme": "two@icloud.com",
            "anonymousId": "two",
            "label": "Remote two",
            "isActive": True,
        },
    ]
    value.save_hme_session(hme_session())
    first_sync = value.sync_aliases()
    one = next(item for item in first_sync if item["email"] == "one@icloud.com")
    two = next(item for item in first_sync if item["email"] == "two@icloud.com")
    value.database.update_alias_configuration(
        one["id"],
        label="Person one",
        note="local note",
        sender_filter="service.example",
    )
    issued = value.database.issue_access_key(two["id"])

    FakeHmeClient.aliases = [
        {
            "hme": "one@icloud.com",
            "anonymousId": "one",
            "label": "Changed remotely",
            "isActive": True,
        }
    ]
    second_sync = value.sync_aliases()
    one_after = next(item for item in second_sync if item["email"] == "one@icloud.com")
    two_after = next(item for item in second_sync if item["email"] == "two@icloud.com")

    assert one_after["label"] == "Person one"
    assert one_after["note"] == "local note"
    assert one_after["sender_filter"] == "service.example"
    assert two_after["state"] == "inactive"
    assert two_after["has_access_key"] is False
    assert value.database.find_alias_by_access_key_hash(hash_access_key(issued.access_key)) is None


def test_incomplete_remote_snapshot_does_not_deactivate_existing_aliases(tmp_path) -> None:
    value = service(tmp_path)
    FakeHmeClient.aliases = [
        {"hme": f"keep{index}@icloud.com", "anonymousId": f"keep{index}", "isActive": True}
        for index in range(8)
    ]
    value.save_hme_session(hme_session())
    FakeHmeClient.aliases = [
        {"hme": "keep0@icloud.com", "anonymousId": "keep0", "isActive": True}
    ]

    value.sync_aliases()

    states = {item["email"]: item["state"] for item in value.database.list_aliases()}
    assert states["keep0@icloud.com"] == "active"
    assert states["keep7@icloud.com"] == "active"
    assert any(
        event["event_type"] == "hme_sync" and event["outcome"] == "refused_incomplete"
        for event in value.database.list_audit_events(limit=20)
    )


def test_ensure_remote_aliases_imports_new_emails_when_snapshot_is_stale(tmp_path) -> None:
    value = service(tmp_path)
    FakeHmeClient.aliases = [
        {"hme": "old@icloud.com", "anonymousId": "old", "isActive": True}
    ]
    value.save_hme_session(hme_session())
    with value.database.transaction() as connection:
        connection.execute("UPDATE aliases SET last_synced_at = ?", ("2026-08-01T00:00:00.000Z",))
    FakeHmeClient.aliases = [
        {"hme": "old@icloud.com", "anonymousId": "old", "isActive": True},
        {"hme": "new@icloud.com", "anonymousId": "new", "isActive": True},
    ]

    notice = value.ensure_remote_aliases()

    emails = {item["email"] for item in value.database.list_aliases()}
    assert notice == "sync_done"
    assert emails == {"old@icloud.com", "new@icloud.com"}


def test_refresh_usage_tags_applies_plan_and_ban_rules(tmp_path, monkeypatch) -> None:
    value = service(tmp_path)
    configure_imap(value)
    plan = value.database.upsert_alias(
        email="plan@icloud.com",
        remote_metadata={"anonymousId": "plan", "isActive": True},
    )
    banned = value.database.upsert_alias(
        email="banned@icloud.com",
        remote_metadata={"anonymousId": "banned", "isActive": True},
    )

    def fake_refresh(*, config, aliases, updater, **_kwargs):
        assert config.host == "imap.example.com"
        updater(plan["id"], "gpt 活跃")
        updater(banned["id"], "gpt 封号")
        return {"matched": 2, "updated": 2, "gpt_active": 1, "gpt_banned": 1, "scanned": 3, "classified": 2}

    monkeypatch.setattr("icloud_gateway.service._refresh_usage_tags", fake_refresh)
    stats = value.refresh_usage_tags()

    assert stats["updated"] == 2
    assert value.database.get_alias(plan["id"])["usage_label"] == "gpt 活跃"
    assert value.database.get_alias(banned["id"])["usage_label"] == "gpt 封号"


def test_create_aliases_issues_one_unique_key_per_alias(tmp_path) -> None:
    delays = []
    value = service(tmp_path, sleeper=delays.append)
    FakeHmeClient.aliases = []
    value.save_hme_session(hme_session())
    FakeHmeClient.created = [
        {"hme": "one@icloud.com", "anonymousId": "one", "isActive": True},
        {"hme": "two@icloud.com", "anonymousId": "two", "isActive": True},
    ]

    batch = value.create_aliases(
        count=2,
        label_prefix="Person",
        note="gateway",
        sender_filter="service.example",
    )

    assert [item.alias["label"] for item in batch.created] == ["Person 1", "Person 2"]
    assert batch.created[0].issued_key.access_key != batch.created[1].issued_key.access_key
    assert batch.error_code is None
    assert delays == [2.0]


def test_create_aliases_accepts_hard_limit_without_truncation(tmp_path) -> None:
    configured = replace(settings(tmp_path), alias_batch_limit=100)
    value = GatewayService(
        configured,
        hme_client_factory=FakeHmeClient,
        imap_reader_factory=FakeReader,
        sleeper=lambda _seconds: None,
    )
    FakeHmeClient.aliases = []
    value.save_hme_session(hme_session())

    with pytest.raises(ValueError):
        value.create_aliases(count=101, label_prefix="Person")
    assert FakeHmeClient.created == []

    FakeHmeClient.created = [
        {
            "hme": f"person{index}@icloud.com",
            "anonymousId": f"person{index}",
            "isActive": True,
        }
        for index in range(100)
    ]
    batch = value.create_aliases(count=100, label_prefix="Person")
    assert batch.requested_count == 100
    assert batch.succeeded_count == 100
    assert batch.failed_count == 0
    assert len(batch.results) == 100


def test_update_alias_usage_is_audited_and_validated(tmp_path) -> None:
    value = service(tmp_path)
    alias = value.database.upsert_alias(
        email="usage@icloud.com",
        remote_metadata={"anonymousId": "usage", "isActive": True},
    )

    updated = value.update_alias_usage(alias["id"], " Grok ")

    assert updated["usage_label"] == "grok"
    event = value.database.list_audit_events(limit=1)[0]
    assert event["event_type"] == "alias_usage"
    assert event["outcome"] == "updated"
    with pytest.raises(ValueError):
        value.update_alias_usage(alias["id"], "x" * 81)
    assert value.database.get_alias(alias["id"])["usage_label"] == "grok"


def test_create_aliases_only_marks_network_failures_as_unknown(tmp_path) -> None:
    value = service(tmp_path)
    FakeHmeClient.aliases = []
    value.save_hme_session(hme_session())

    FakeHmeClient.created = [HmeNetworkError("timed out")]
    network = value.create_aliases(count=1, label_prefix="Person")
    assert network.results[0].status == "unknown"

    FakeHmeClient.created = [HmeError("rejected before write")]
    rejected = value.create_aliases(count=1, label_prefix="Person")
    assert rejected.results[0].status == "error"


def test_bulk_alias_actions_reject_duplicates_and_preserve_mixed_results(tmp_path) -> None:
    value = service(tmp_path)
    active = value.database.upsert_alias(
        email="active@icloud.com",
        remote_metadata={"anonymousId": "active", "isActive": True},
        state="active",
    )
    inactive = value.database.upsert_alias(
        email="inactive@icloud.com",
        remote_metadata={"anonymousId": "inactive", "isActive": False},
        state="inactive",
    )
    with pytest.raises(ValueError):
        value.bulk_alias_action(action="issue_keys", alias_ids=[active["id"], active["id"]])

    result = value.bulk_alias_action(
        action="issue_keys",
        alias_ids=[active["id"], inactive["id"], "missing"],
    )
    assert [item["status"] for item in result.results] == [
        "success",
        "conflict",
        "not_found",
    ]
    assert result.succeeded_count == 1
    assert result.failed_count == 2
    assert result.results[0]["access_key"].startswith("icg_")


def test_imap_configuration_is_tested_before_it_replaces_the_saved_value(tmp_path) -> None:
    value = service(tmp_path)

    first = configure_imap(value)

    assert first.password == "app-password"
    assert first.junk_folder == "Junk"
    assert len(FakeReader.checked) == 1
    preserved = value.configure_imap(
        {
            "forwarding_email": "forwarding@example.com",
            "host": "imap.example.com",
            "port": 993,
            "username": "forwarding@example.com",
            "password": "",
            "folder": "Archive",
            "junk_folder": "Spam",
            "proxy": "",
        },
        test=False,
    )
    assert preserved.password == "app-password"
    assert preserved.folder == "Archive"
    assert preserved.junk_folder == "Spam"


def test_code_lookup_returns_only_code_and_timestamps(tmp_path) -> None:
    value = service(tmp_path)
    configure_imap(value)
    alias = value.database.upsert_alias(
        email="target@icloud.com",
        remote_metadata={"anonymousId": "target"},
        sender_filter="service.example",
    )
    issued = value.database.issue_access_key(alias["id"])
    FakeReader.result = OtpResult(
        code="246810",
        uid="55",
        received_at=datetime.fromtimestamp(NOW - 2, tz=UTC),
    )

    result = value.lookup_code(issued.access_key, client_ip="203.0.113.9")

    assert result.status == "found"
    assert result.code == "246810"
    assert result.received_at == "2027-01-15T07:59:58Z"
    assert result.expires_at == "2027-01-15T08:04:58Z"
    assert not hasattr(result, "email")
    assert FakeReader.last_kwargs["sender_policy"] == "gpt_grok"


def test_admin_recent_codes_include_aliases_without_access_keys(tmp_path) -> None:
    value = service(tmp_path)
    configure_imap(value)
    unkeyed = value.database.upsert_alias(
        email="unkeyed@icloud.com",
        remote_metadata={"anonymousId": "unkeyed"},
        label="Unkeyed",
    )
    keyed = value.database.upsert_alias(
        email="keyed@icloud.com",
        remote_metadata={"anonymousId": "keyed"},
        label="Keyed",
    )
    value.database.issue_access_key(keyed["id"])
    FakeReader.recent_batch = RecentOtpBatch(
        items=(
            RecentOtpResult(
                alias="unkeyed@icloud.com",
                code="123456",
                uid="10",
                received_at=datetime.fromtimestamp(NOW - 1, tz=UTC),
            ),
            RecentOtpResult(
                alias="keyed@icloud.com",
                code="654321",
                uid="9",
                received_at=datetime.fromtimestamp(NOW - 2, tz=UTC),
            ),
        ),
        scanned=12,
        truncated=False,
    )

    result = value.admin_recent_codes()

    assert [item["alias_id"] for item in result["codes"]] == [unkeyed["id"], keyed["id"]]
    assert [item["code"] for item in result["codes"]] == ["123456", "654321"]
    assert result["codes"][0]["received_at_display"] == "2027-01-15 15:59:59"
    assert result["scanned"] == 12
    assert result["truncated"] is False
    aliases, kwargs = FakeReader.recent_calls[-1]
    assert set(aliases) == {"unkeyed@icloud.com", "keyed@icloud.com"}
    assert kwargs["max_age_seconds"] == 1800
    event = value.database.list_audit_events(limit=1)[0]
    assert event["event_type"] == "admin_code_scan"
    assert event["outcome"] == "found"
    assert "123456" not in str(event)


def test_admin_recent_codes_fail_closed_and_release_the_imap_slot(tmp_path) -> None:
    value = service(tmp_path)
    configure_imap(value)
    value.database.upsert_alias(
        email="target@icloud.com",
        remote_metadata={"anonymousId": "target"},
    )
    FakeReader.recent_error = ImapCredentialsError("expired")

    with pytest.raises(GatewayNotConfiguredError):
        value.admin_recent_codes()

    event = value.database.list_audit_events(limit=1)[0]
    assert event["event_type"] == "admin_code_scan"
    assert event["outcome"] == "imap_invalid"
    FakeReader.recent_error = None
    result = value.admin_recent_codes()
    assert result["codes"] == []
    assert result["by_alias"] == {}
    assert result["scanned"] == 0
    assert result["truncated"] is False
    assert result["scope"] == "single"


def test_invalid_key_and_no_code_have_distinct_safe_states(tmp_path) -> None:
    value = service(tmp_path)
    configure_imap(value)
    alias = value.database.upsert_alias(
        email="target@icloud.com", remote_metadata={"anonymousId": "target"}
    )
    issued = value.database.issue_access_key(alias["id"])

    invalid = value.lookup_code("not-a-key", client_ip="203.0.113.9")
    waiting = value.lookup_code(issued.access_key, client_ip="203.0.113.9")

    assert invalid.status == "invalid_key"
    assert waiting.status == "waiting"
    assert waiting.retry_after == 5


def test_dashboard_summarizes_bounded_lookup_history_without_sensitive_values(tmp_path) -> None:
    value = service(tmp_path)
    configure_imap(value)
    alias = value.database.upsert_alias(
        email="target@icloud.com", remote_metadata={"anonymousId": "target"}
    )
    issued = value.database.issue_access_key(alias["id"])

    value.lookup_code("not-a-key", client_ip="203.0.113.8")
    value.lookup_code(issued.access_key, client_ip="203.0.113.9")
    timestamp = datetime.now(UTC).replace(microsecond=0)
    timestamp_iso = timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with value.database.transaction() as connection:
        connection.execute(
            "UPDATE audit_events SET created_at = ? WHERE event_type = 'code_lookup'",
            (timestamp_iso,),
        )
    dashboard = value.dashboard()

    assert dashboard["query_counts"] == {"shown": 2, "aliases": 1}
    assert [event["outcome"] for event in dashboard["query_history"]] == [
        "no_code",
        "invalid_key",
    ]
    assert dashboard["query_history"][0]["alias_email"] == "target@icloud.com"
    assert dashboard["query_history"][1]["alias_email"] is None
    assert {event["created_at_display"] for event in dashboard["query_history"]} == {
        timestamp.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    }
    serialized = str(dashboard["query_history"])
    assert issued.access_key not in serialized
    assert "203.0.113.9" not in serialized


def test_empty_remote_list_does_not_deactivate_every_alias(tmp_path) -> None:
    gateway = service(tmp_path)
    FakeHmeClient.aliases = [
        {"hme": "one@icloud.com", "anonymousId": "one", "isActive": True},
        {"hme": "two@icloud.com", "anonymousId": "two", "isActive": True},
    ]
    gateway.save_hme_session(hme_session())
    gateway.sync_aliases()
    for alias in gateway.database.list_aliases():
        gateway.database.issue_access_key(alias["id"])

    FakeHmeClient.aliases = []
    with pytest.raises(GatewayError):
        gateway.sync_aliases()

    aliases = gateway.database.list_aliases()
    assert [item["state"] for item in aliases] == ["active", "active"]
    assert all(item["has_access_key"] for item in aliases)


def test_invalid_remote_snapshot_is_rejected_before_local_state_changes(tmp_path) -> None:
    gateway = service(tmp_path)
    FakeHmeClient.aliases = [
        {"hme": "one@icloud.com", "anonymousId": "one", "isActive": True},
        {"hme": "two@icloud.com", "anonymousId": "two", "isActive": True},
    ]
    gateway.save_hme_session(hme_session())
    one = next(
        item for item in gateway.database.list_aliases() if item["email"] == "one@icloud.com"
    )
    issued = gateway.issue_access_key(one["id"])

    FakeHmeClient.aliases = [
        {"hme": "one@icloud.com", "anonymousId": "one", "isActive": False},
        {"hme": "broken", "anonymousId": "two", "isActive": False},
    ]

    with pytest.raises(GatewayError):
        gateway.sync_aliases()

    unchanged = gateway.database.get_alias(one["id"])
    assert unchanged["state"] == "active"
    assert gateway.database.find_alias_by_access_key_hash(hash_access_key(issued.access_key))


def test_remote_lifecycle_changes_require_readback_before_local_mutation(tmp_path) -> None:
    gateway = service(tmp_path)
    FakeHmeClient.aliases = [
        {"hme": "person@icloud.com", "anonymousId": "person", "isActive": True}
    ]
    gateway.save_hme_session(hme_session())
    alias = gateway.database.list_aliases()[0]
    issued = gateway.issue_access_key(alias["id"])

    FakeHmeClient.confirm_lifecycle = False
    with pytest.raises(GatewayError):
        gateway.deactivate_alias(alias["id"])

    unchanged = gateway.database.get_alias(alias["id"])
    assert unchanged["state"] == "active"
    assert gateway.database.find_alias_by_access_key_hash(hash_access_key(issued.access_key))
    assert FakeHmeClient.lifecycle_calls == [("deactivate", "person")]

    FakeHmeClient.confirm_lifecycle = True
    deactivated = gateway.deactivate_alias(alias["id"])
    assert deactivated["state"] == "inactive"
    assert (
        gateway.database.find_alias_by_access_key_hash(hash_access_key(issued.access_key)) is None
    )

    reactivated = gateway.reactivate_alias(alias["id"])
    assert reactivated["state"] == "active"


def test_remote_lifecycle_closes_client_when_confirmation_fails(tmp_path) -> None:
    gateway = service(tmp_path)
    FakeHmeClient.aliases = [
        {"hme": "person@icloud.com", "anonymousId": "person", "isActive": True}
    ]
    gateway.save_hme_session(hme_session())
    alias = gateway.database.list_aliases()[0]
    closed = []

    class CloseTrackingClient(FakeHmeClient):
        def close(self):
            closed.append(True)

    gateway.hme_client_factory = CloseTrackingClient
    FakeHmeClient.confirm_lifecycle = False

    with pytest.raises(GatewayError):
        gateway.deactivate_alias(alias["id"])

    assert closed == [True]


def test_remote_lifecycle_rejects_partial_confirmation_snapshot(tmp_path) -> None:
    gateway = service(tmp_path)
    FakeHmeClient.aliases = [
        {"hme": "one@icloud.com", "anonymousId": "one", "isActive": True},
        {"hme": "two@icloud.com", "anonymousId": "two", "isActive": True},
    ]
    gateway.save_hme_session(hme_session())
    one = next(
        item for item in gateway.database.list_aliases() if item["email"] == "one@icloud.com"
    )
    issued = gateway.issue_access_key(one["id"])

    class PartialConfirmationClient(FakeHmeClient):
        def deactivate_alias(self, anonymous_id):
            result = super().deactivate_alias(anonymous_id)
            self.aliases[:] = [
                item for item in self.aliases if item.get("anonymousId") == anonymous_id
            ]
            return result

    gateway.hme_client_factory = PartialConfirmationClient

    with pytest.raises(GatewayError):
        gateway.deactivate_alias(one["id"])

    unchanged = gateway.database.get_alias(one["id"])
    assert unchanged["state"] == "active"
    assert gateway.database.find_alias_by_access_key_hash(hash_access_key(issued.access_key))


@pytest.mark.parametrize(
    ("action", "target_active", "expected_state"),
    [
        ("deactivate", True, "inactive"),
        ("reactivate", False, "active"),
        ("delete", False, None),
    ],
)
def test_lifecycle_commits_the_same_complete_confirmation_snapshot(
    tmp_path, action, target_active, expected_state
) -> None:
    gateway = service(tmp_path)
    FakeHmeClient.aliases = [
        {
            "hme": "target@icloud.com",
            "anonymousId": "target",
            "isActive": target_active,
        },
        {"hme": "keeper@icloud.com", "anonymousId": "keeper", "isActive": True},
        {
            "hme": "unrelated@icloud.com",
            "anonymousId": "unrelated",
            "isActive": True,
        },
    ]
    gateway.save_hme_session(hme_session())
    aliases = {item["email"]: item for item in gateway.database.list_aliases()}
    target = aliases["target@icloud.com"]
    unrelated = aliases["unrelated@icloud.com"]
    issued = gateway.issue_access_key(unrelated["id"])
    list_calls = []

    class CompleteThenPartialClient(FakeHmeClient):
        def list_aliases(self):
            snapshot = super().list_aliases()
            list_calls.append([item["anonymousId"] for item in snapshot])
            if len(list_calls) > 1:
                return [item for item in snapshot if item.get("anonymousId") != "unrelated"]
            return snapshot

    gateway.hme_client_factory = CompleteThenPartialClient
    try:
        if action == "deactivate":
            gateway.deactivate_alias(target["id"])
        elif action == "reactivate":
            gateway.reactivate_alias(target["id"])
        else:
            gateway.delete_alias(target["id"], confirmation=target["email"])

        assert len(list_calls) == 1
        unchanged = gateway.database.get_alias(unrelated["id"])
        assert unchanged["state"] == "active"
        assert gateway.database.find_alias_by_access_key_hash(hash_access_key(issued.access_key))
        if expected_state is None:
            with pytest.raises(NotFoundError):
                gateway.database.get_alias(target["id"])
        else:
            assert gateway.database.get_alias(target["id"])["state"] == expected_state
    finally:
        gateway.shutdown()


@pytest.mark.parametrize("operation", ["deactivate", "reactivate", "delete"])
def test_stop_after_freshness_prevents_lifecycle_write(tmp_path, operation) -> None:
    initial_active = operation == "deactivate"
    gateway = service(tmp_path)
    FakeHmeClient.aliases = [
        {
            "hme": "person@icloud.com",
            "anonymousId": "person",
            "isActive": initial_active,
        }
    ]
    gateway.save_hme_session(hme_session())
    alias = gateway.database.list_aliases()[0]
    freshness_entered = threading.Event()
    release_freshness = threading.Event()
    errors = []

    def blocking_freshness():
        freshness_entered.set()
        assert release_freshness.wait(2)

    gateway._ensure_hme_fresh = blocking_freshness

    def run_operation():
        try:
            if operation == "deactivate":
                gateway.deactivate_alias(alias["id"])
            elif operation == "reactivate":
                gateway.reactivate_alias(alias["id"])
            else:
                gateway.delete_alias(alias["id"], confirmation=alias["email"])
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=run_operation)
    worker.start()
    assert freshness_entered.wait(1)
    gateway.request_stop()
    release_freshness.set()
    worker.join(2)

    assert not worker.is_alive()
    assert FakeHmeClient.lifecycle_calls == []
    assert len(errors) == 1
    assert isinstance(errors[0], GatewayRetryableError)
    gateway.shutdown()


def test_permanent_delete_requires_inactive_state_confirmation_and_remote_absence(
    tmp_path,
) -> None:
    gateway = service(tmp_path)
    FakeHmeClient.aliases = [
        {"hme": "person@icloud.com", "anonymousId": "person", "isActive": False}
    ]
    gateway.save_hme_session(hme_session())
    alias = gateway.database.list_aliases()[0]

    with pytest.raises(ConflictError):
        gateway.delete_alias(alias["id"], confirmation="wrong@icloud.com")
    assert FakeHmeClient.lifecycle_calls == []

    FakeHmeClient.confirm_lifecycle = False
    with pytest.raises(GatewayError):
        gateway.delete_alias(alias["id"], confirmation="person@icloud.com")
    assert gateway.database.get_alias(alias["id"])["state"] == "inactive"
    assert FakeHmeClient.lifecycle_calls == [("delete", "person")]

    FakeHmeClient.confirm_lifecycle = True
    gateway.delete_alias(alias["id"], confirmation="person@icloud.com")
    with pytest.raises(NotFoundError):
        gateway.database.get_alias(alias["id"])


def test_hme_refresh_is_singleflight_and_read_recovers_transparently(tmp_path) -> None:
    entered = threading.Event()
    release = threading.Event()
    refresh_calls = []
    old = hme_session()
    new = ICloudHmeSession(**{**old.as_secret_dict(), "client_id": "refreshed"})

    class ExpiringClient(FakeHmeClient):
        def __init__(self, current):
            self.current = current

        def list_aliases(self):
            if self.current.client_id != "refreshed":
                raise HmeSessionError("expired")
            return []

    def refresher(_session):
        refresh_calls.append(1)
        entered.set()
        release.wait(2)
        return new

    gateway = GatewayService(
        settings(tmp_path),
        hme_client_factory=ExpiringClient,
        hme_session_refresher=refresher,
        imap_reader_factory=FakeReader,
        start_maintenance=False,
    )
    gateway.database.set_secret("hme_session", old.as_secret_dict())
    results = []
    errors = []

    def run_sync():
        try:
            results.append(gateway.sync_aliases())
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run_sync) for _ in range(4)]
    for thread in threads:
        thread.start()
    assert entered.wait(1)
    release.set()
    for thread in threads:
        thread.join(2)

    assert errors == []
    assert results == [[], [], [], []]
    assert len(refresh_calls) == 1
    assert gateway.get_hme_session() == new
    assert gateway.hme_status()["state"] == "ready"
    gateway.shutdown()


def test_stale_sync_snapshot_cannot_reactivate_a_deactivated_alias(tmp_path) -> None:
    snapshot_read = threading.Event()
    release_snapshot = threading.Event()
    block_sync = [False]

    class BlockingSyncClient(FakeHmeClient):
        def list_aliases(self):
            snapshot = super().list_aliases()
            if block_sync[0]:
                block_sync[0] = False
                snapshot_read.set()
                assert release_snapshot.wait(2)
            return snapshot

    gateway = GatewayService(
        settings(tmp_path),
        hme_client_factory=BlockingSyncClient,
        imap_reader_factory=FakeReader,
        start_maintenance=False,
    )
    FakeHmeClient.aliases = [
        {"hme": "person@icloud.com", "anonymousId": "person", "isActive": True}
    ]
    gateway.save_hme_session(hme_session())
    alias = gateway.database.list_aliases()[0]
    issued = gateway.issue_access_key(alias["id"])
    block_sync[0] = True

    sync_thread = threading.Thread(target=gateway.sync_aliases)
    sync_thread.start()
    assert snapshot_read.wait(1)

    deactivated = gateway.deactivate_alias(alias["id"])
    release_snapshot.set()
    sync_thread.join(2)

    assert not sync_thread.is_alive()
    assert deactivated["state"] == "inactive"
    stored = gateway.database.get_alias(alias["id"])
    assert stored["state"] == "inactive"
    assert stored["has_access_key"] is False
    assert (
        gateway.database.find_alias_by_access_key_hash(hash_access_key(issued.access_key)) is None
    )
    assert gateway.database.list_audit_events(limit=1)[0]["outcome"] == "discarded_stale"
    gateway.shutdown()


def test_stale_sync_snapshot_cannot_restore_a_deleted_alias(tmp_path) -> None:
    snapshot_read = threading.Event()
    release_snapshot = threading.Event()
    block_sync = [False]

    class BlockingSyncClient(FakeHmeClient):
        def list_aliases(self):
            snapshot = super().list_aliases()
            if block_sync[0]:
                block_sync[0] = False
                snapshot_read.set()
                assert release_snapshot.wait(2)
            return snapshot

    gateway = GatewayService(
        settings(tmp_path),
        hme_client_factory=BlockingSyncClient,
        imap_reader_factory=FakeReader,
        start_maintenance=False,
    )
    FakeHmeClient.aliases = [
        {"hme": "person@icloud.com", "anonymousId": "person", "isActive": False}
    ]
    gateway.save_hme_session(hme_session())
    alias = gateway.database.list_aliases()[0]
    block_sync[0] = True

    sync_thread = threading.Thread(target=gateway.sync_aliases)
    sync_thread.start()
    assert snapshot_read.wait(1)

    gateway.delete_alias(alias["id"], confirmation=alias["email"])
    release_snapshot.set()
    sync_thread.join(2)

    assert not sync_thread.is_alive()
    with pytest.raises(NotFoundError):
        gateway.database.get_alias(alias["id"])
    assert gateway.database.list_aliases() == []
    assert gateway.database.list_audit_events(limit=1)[0]["outcome"] == "discarded_stale"
    gateway.shutdown()


def test_stale_refresh_validation_does_not_replace_session_or_alias_state(tmp_path) -> None:
    refresh_read = threading.Event()
    release_refresh = threading.Event()
    old = hme_session()
    candidate = ICloudHmeSession(**{**old.as_secret_dict(), "client_id": "refreshed"})

    class BlockingRefreshClient(FakeHmeClient):
        def __init__(self, current):
            self.current = current

        def list_aliases(self):
            snapshot = super().list_aliases()
            if self.current.client_id == "refreshed":
                refresh_read.set()
                assert release_refresh.wait(2)
            return snapshot

    gateway = GatewayService(
        settings(tmp_path),
        hme_client_factory=BlockingRefreshClient,
        hme_session_refresher=lambda _session: candidate,
        imap_reader_factory=FakeReader,
        start_maintenance=False,
    )
    FakeHmeClient.aliases = [
        {"hme": "person@icloud.com", "anonymousId": "person", "isActive": True}
    ]
    gateway.save_hme_session(old)
    alias = gateway.database.list_aliases()[0]
    result = []

    refresh_thread = threading.Thread(target=lambda: result.append(gateway._refresh_hme_session()))
    refresh_thread.start()
    assert refresh_read.wait(1)

    gateway.deactivate_alias(alias["id"])
    release_refresh.set()
    refresh_thread.join(2)

    assert not refresh_thread.is_alive()
    assert result == [old]
    assert gateway.get_hme_session() == old
    assert gateway.database.get_alias(alias["id"])["state"] == "inactive"
    assert gateway.database.list_audit_events(limit=1)[0]["outcome"] == "discarded_stale"
    gateway.shutdown()


def test_key_issuance_fails_closed_while_remote_lifecycle_write_is_in_flight(tmp_path) -> None:
    write_started = threading.Event()
    release_write = threading.Event()

    class BlockingDeactivateClient(FakeHmeClient):
        def deactivate_alias(self, anonymous_id):
            write_started.set()
            assert release_write.wait(2)
            return super().deactivate_alias(anonymous_id)

    gateway = GatewayService(
        settings(tmp_path),
        hme_client_factory=BlockingDeactivateClient,
        imap_reader_factory=FakeReader,
        start_maintenance=False,
    )
    FakeHmeClient.aliases = [
        {"hme": "person@icloud.com", "anonymousId": "person", "isActive": True}
    ]
    gateway.save_hme_session(hme_session())
    alias = gateway.database.list_aliases()[0]

    lifecycle_thread = threading.Thread(target=lambda: gateway.deactivate_alias(alias["id"]))
    lifecycle_thread.start()
    assert write_started.wait(1)

    with pytest.raises(GatewayBusyError):
        gateway.issue_access_key(alias["id"])

    release_write.set()
    lifecycle_thread.join(2)
    assert not lifecycle_thread.is_alive()
    assert gateway.database.get_alias(alias["id"])["state"] == "inactive"
    gateway.shutdown()


def test_write_auth_rejection_refreshes_but_does_not_replay(tmp_path) -> None:
    old = hme_session()
    new = ICloudHmeSession(**{**old.as_secret_dict(), "client_id": "refreshed"})
    writes = []

    class RejectingWriteClient(FakeHmeClient):
        def __init__(self, current):
            self.current = current

        def list_aliases(self):
            return [dict(item) for item in self.aliases]

        def deactivate_alias(self, anonymous_id):
            writes.append((self.current.client_id, anonymous_id))
            raise HmeSessionError("expired")

    gateway = GatewayService(
        settings(tmp_path),
        hme_client_factory=RejectingWriteClient,
        hme_session_refresher=lambda _session: new,
        imap_reader_factory=FakeReader,
        start_maintenance=False,
    )
    FakeHmeClient.aliases = [
        {"hme": "person@icloud.com", "anonymousId": "person", "isActive": True}
    ]
    gateway.save_hme_session(old)
    alias = gateway.database.list_aliases()[0]

    with pytest.raises(GatewayRetryableError):
        gateway.deactivate_alias(alias["id"])

    assert writes == [("client", "person")]
    assert gateway.get_hme_session() == new
    assert gateway.database.get_alias(alias["id"])["state"] == "active"
    gateway.shutdown()


def test_shutdown_interrupts_maintenance_thread(tmp_path) -> None:
    configured = replace(settings(tmp_path), hme_maintenance_interval_seconds=300)
    gateway = GatewayService(
        configured,
        hme_client_factory=FakeHmeClient,
        imap_reader_factory=FakeReader,
    )
    thread = gateway._maintenance_thread
    assert thread is not None and thread.is_alive()

    started = time.monotonic()
    gateway.shutdown()

    assert time.monotonic() - started < 2
    assert not thread.is_alive()


def test_shutdown_has_one_deadline_and_keeps_database_open_if_worker_stuck(tmp_path) -> None:
    configured = replace(settings(tmp_path), hme_maintenance_interval_seconds=300)
    gateway = GatewayService(
        configured,
        hme_client_factory=FakeHmeClient,
        imap_reader_factory=FakeReader,
        start_maintenance=False,
    )
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    thread.start()
    gateway._maintenance_thread = thread
    closed = []
    original_close = gateway.database.close
    gateway.database.close = lambda: closed.append(True)

    started = time.monotonic()
    assert gateway.shutdown(timeout=0.05) is False

    assert time.monotonic() - started < 0.5
    assert closed == []
    assert gateway.database.quick_check() == "ok"
    release.set()
    thread.join(1)
    gateway.database.close = original_close
    assert gateway.shutdown(timeout=0.5) is True


def test_key_dimension_rate_limit_does_not_depend_on_ip_limit(tmp_path) -> None:
    now = [100.0]
    limiter = SlidingWindowRateLimiter(clock=lambda: now[0])
    value = service(tmp_path, limiter=limiter)
    configure_imap(value)
    alias = value.database.upsert_alias(
        email="target@icloud.com", remote_metadata={"anonymousId": "target"}
    )
    issued = value.database.issue_access_key(alias["id"])

    for index in range(12):
        value.lookup_code(issued.access_key, client_ip=f"203.0.113.{index}")

    with pytest.raises(GatewayRateLimitedError) as caught:
        value.lookup_code(issued.access_key, client_ip="203.0.113.99")
    assert caught.value.retry_after == 60


def test_admin_recent_codes_work_in_control_mode(tmp_path) -> None:
    configured = replace(
        settings(tmp_path),
        deployment_mode="control",
        control_plane_token="control-token-abcdefghijklmnop",
        edge_base_url="https://edge.example.com",
        edge_sync_enabled=False,
    )
    value = GatewayService(
        configured,
        imap_reader_factory=FakeReader,
        start_maintenance=False,
    )
    configure_imap(value)
    alias = value.database.upsert_alias(
        email="control@icloud.com",
        remote_metadata={"anonymousId": "control"},
        label="Control",
    )
    FakeReader.result = OtpResult(
        code="112233",
        uid="7",
        received_at=datetime.fromtimestamp(NOW - 3, tz=UTC),
    )

    result = value.admin_recent_codes()

    assert result["scope"] == "single"
    assert result["codes"][0]["code"] == "112233"
    assert result["by_alias"][alias["id"]]["code"] == "112233"
    with pytest.raises(GatewayNotAllowedError):
        # public OTP remains disabled in control mode
        value.lookup_code("icg_" + ("a" * 43), client_ip="127.0.0.1")


def test_imap_bootstrap_from_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ICLOUD_GATEWAY_IMAP_USERNAME", "forwarding@example.com")
    monkeypatch.setenv("ICLOUD_GATEWAY_IMAP_PASSWORD", "app-password")
    monkeypatch.setenv("ICLOUD_GATEWAY_IMAP_FORWARDING_EMAIL", "forwarding@example.com")
    monkeypatch.setenv("ICLOUD_GATEWAY_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("ICLOUD_GATEWAY_IMAP_BOOTSTRAP_TEST", "0")
    value = GatewayService(
        settings(tmp_path),
        imap_reader_factory=FakeReader,
        start_maintenance=False,
    )
    config = value.get_imap_config()
    assert config is not None
    assert config.username == "forwarding@example.com"
    assert config.host == "imap.example.com"


def test_admin_recent_codes_single_alias_uses_latest_lookup(tmp_path) -> None:
    value = service(tmp_path)
    configure_imap(value)
    alias = value.database.upsert_alias(
        email="focused@icloud.com",
        remote_metadata={"anonymousId": "focused"},
        label="Focused",
    )
    value.database.upsert_alias(
        email="other@icloud.com",
        remote_metadata={"anonymousId": "other"},
        label="Other",
    )
    FakeReader.result = OtpResult(
        code="778899",
        uid="99",
        received_at=datetime.fromtimestamp(NOW - 1, tz=UTC),
    )
    FakeReader.recent_batch = RecentOtpBatch(items=(), scanned=0, truncated=False)

    result = value.admin_recent_codes(alias_ids=[alias["id"]])

    assert result["scope"] == "single"
    assert result["codes"][0]["code"] == "778899"
    assert result["by_alias"][alias["id"]]["email"] == "focused@icloud.com"
    assert FakeReader.recent_calls == []
