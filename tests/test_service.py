from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from icloud_gateway.config import Settings
from icloud_gateway.hme import HmeError, ICloudHmeSession
from icloud_gateway.imap_otp import ImapConfig, OtpResult
from icloud_gateway.rate_limit import SlidingWindowRateLimiter
from icloud_gateway.security import hash_access_key
from icloud_gateway.service import (
    GatewayError,
    GatewayRateLimitedError,
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

    def __init__(self, _session):
        pass

    def list_aliases(self):
        return [dict(item) for item in self.aliases]

    def create_alias(self, *, label, note):
        if not self.created:
            raise HmeError("rate limited")
        item = dict(self.created.pop(0))
        item["label"] = label
        item["note"] = note
        return item


class FakeReader:
    result = None
    checked = []

    def __init__(self, config):
        self.config = config

    def check(self, *, timeout):
        self.checked.append((self.config, timeout))

    def find_latest_code(self, alias, **kwargs):
        self.last_alias = alias
        self.last_kwargs = kwargs
        return self.result


@pytest.fixture(autouse=True)
def reset_fakes():
    FakeHmeClient.aliases = []
    FakeHmeClient.created = []
    FakeReader.result = None
    FakeReader.checked = []


def service(tmp_path, *, limiter=None, sleeper=lambda _seconds: None) -> GatewayService:
    return GatewayService(
        settings(tmp_path),
        hme_client_factory=FakeHmeClient,
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
            "proxy": "",
        }
    )


def test_hme_session_is_saved_only_after_read_only_validation(tmp_path) -> None:
    value = service(tmp_path)
    FakeHmeClient.aliases = [{"hme": "one@icloud.com", "anonymousId": "one"}]

    assert value.save_hme_session(hme_session()) == 1
    assert value.get_hme_session() == hme_session()

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


def test_imap_configuration_is_tested_before_it_replaces_the_saved_value(tmp_path) -> None:
    value = service(tmp_path)

    first = configure_imap(value)

    assert first.password == "app-password"
    assert len(FakeReader.checked) == 1
    preserved = value.configure_imap(
        {
            "forwarding_email": "forwarding@example.com",
            "host": "imap.example.com",
            "port": 993,
            "username": "forwarding@example.com",
            "password": "",
            "folder": "Archive",
            "proxy": "",
        },
        test=False,
    )
    assert preserved.password == "app-password"
    assert preserved.folder == "Archive"


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
