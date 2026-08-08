from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from icloud_gateway.config import Settings
from icloud_gateway.hme import HmeError, ICloudHmeSession
from icloud_gateway.imap_otp import (
    ImapConfig,
    ImapCredentialsError,
    OtpResult,
    RecentOtpBatch,
    RecentOtpResult,
)
from icloud_gateway.security import AdminSessionCodec, hash_access_key
from icloud_gateway.service import (
    GatewayNotConfiguredError,
    GatewayRateLimitedError,
    GatewayService,
)
from icloud_gateway.web import ADMIN_COOKIE, MAX_REQUEST_BYTES, create_app

NOW = 1_800_000_000.0


class FakeHmeClient:
    aliases: list[dict] = []
    created: list[dict | Exception] = []
    list_error: Exception | None = None
    lifecycle_calls: list[tuple[str, str]] = []

    def __init__(self, _session):
        pass

    def list_aliases(self):
        if self.list_error is not None:
            raise self.list_error
        return [dict(item) for item in self.aliases]

    def create_alias(self, *, label, note):
        if not self.created:
            raise HmeError("no prepared alias")
        result = self.created.pop(0)
        if isinstance(result, Exception):
            raise result
        value = dict(result)
        value["label"] = label
        value["note"] = note
        return value

    def _change_state(self, action, anonymous_id, state=None):
        self.lifecycle_calls.append((action, anonymous_id))
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


class FakeImapReader:
    result: OtpResult | None = None
    recent_batch = RecentOtpBatch(items=(), scanned=0, truncated=False)
    check_error: Exception | None = None

    def __init__(self, config):
        self.config = config

    def check(self, *, timeout):
        if self.check_error is not None:
            raise self.check_error

    def find_latest_code(self, alias, **kwargs):
        return self.result

    def find_recent_codes(self, aliases, **kwargs):
        return self.recent_batch


@pytest.fixture(autouse=True)
def reset_fakes():
    FakeHmeClient.aliases = []
    FakeHmeClient.created = []
    FakeHmeClient.list_error = None
    FakeHmeClient.lifecycle_calls = []
    FakeImapReader.result = None
    FakeImapReader.recent_batch = RecentOtpBatch(items=(), scanned=0, truncated=False)
    FakeImapReader.check_error = None


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        master_key=bytes(range(32)),
        admin_password="correct horse battery staple",
        cookie_secure=False,
        cdp_url="",
        trusted_hosts=("testserver", "localhost", "127.0.0.1"),
    )


@pytest.fixture
def service(settings) -> GatewayService:
    return GatewayService(
        settings,
        hme_client_factory=FakeHmeClient,
        imap_reader_factory=FakeImapReader,
        clock=lambda: NOW,
        sleeper=lambda _seconds: None,
    )


@pytest.fixture
def client(settings, service):
    app = create_app(settings, service=service)
    with TestClient(app, base_url="http://testserver") as value:
        yield value


def _configure_imap(service: GatewayService, *, host: str = "imap.example.com") -> ImapConfig:
    return service.configure_imap(
        {
            "forwarding_email": "forwarding@example.com",
            "host": host,
            "port": 993,
            "username": "forwarding@example.com",
            "password": "app-password",
            "folder": "INBOX",
            "junk_folder": "Junk",
            "proxy": "",
        }
    )


def _hme_session(*, client_id: str = "client") -> ICloudHmeSession:
    return ICloudHmeSession(
        host="p123-maildomainws.icloud.com.cn",
        dsid="123",
        client_id=client_id,
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


def _session_curl(session: ICloudHmeSession) -> str:
    return (
        "curl "
        f"'https://{session.host}/v2/hme/list?dsid={session.dsid}"
        f"&clientId={session.client_id}"
        f"&clientBuildNumber={session.client_build_number}"
        f"&clientMasteringNumber={session.client_mastering_number}' "
        f"-H 'Cookie: {session.cookie}' "
        f"-H 'Origin: {session.origin}' "
        f"-H 'Referer: {session.referer}'"
    )


def _login(client: TestClient, settings: Settings) -> str:
    response = client.post(
        "/admin/login",
        data={"password": settings.admin_password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    token = client.cookies.get(ADMIN_COOKIE)
    assert token
    return (
        AdminSessionCodec(
            settings.master_key,
            lifetime_seconds=settings.admin_session_seconds,
        )
        .decode(token)
        .csrf_token
    )


def _create_keyed_alias(service: GatewayService):
    alias = service.database.upsert_alias(
        email="target@icloud.com",
        remote_metadata={"anonymousId": "target", "isActive": True},
        label="Target",
    )
    return alias, service.database.issue_access_key(alias["id"])


def test_public_code_api_states_and_safe_response_contract(client, service) -> None:
    _configure_imap(service)
    _alias, issued = _create_keyed_alias(service)
    FakeImapReader.result = OtpResult(
        code="246810",
        uid="55",
        received_at=datetime.fromtimestamp(NOW - 2, tz=UTC),
    )

    found = client.post("/api/code", json={"access_key": issued.access_key})
    assert found.status_code == 200
    assert found.json() == {
        "status": "found",
        "code": "246810",
        "received_at": "2027-01-15T07:59:58Z",
        "expires_at": "2027-01-15T08:04:58Z",
        "retry_after": None,
    }

    FakeImapReader.result = None
    waiting = client.post("/api/code", json={"access_key": issued.access_key})
    assert waiting.status_code == 200
    assert waiting.json()["status"] == "waiting"
    assert waiting.json()["retry_after"] == 5

    invalid = client.post("/api/code", json={"access_key": "not-a-key"})
    assert invalid.status_code == 404
    assert invalid.json() == {"status": "invalid_key"}


def test_public_api_maps_rate_limit_unavailable_and_timeout(client, service, monkeypatch) -> None:
    def rate_limited(_access_key, *, client_ip):
        raise GatewayRateLimitedError(17)

    monkeypatch.setattr(service, "lookup_code", rate_limited)
    response = client.post("/api/code", json={"access_key": "x"})
    assert response.status_code == 429
    assert response.json() == {"status": "rate_limited", "retry_after": 17}
    assert response.headers["retry-after"] == "17"

    def unavailable(_access_key, *, client_ip):
        raise GatewayNotConfiguredError("not configured")

    monkeypatch.setattr(service, "lookup_code", unavailable)
    response = client.post("/api/code", json={"access_key": "x"})
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}

    async def timed_out(*_args, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr("icloud_gateway.web.asyncio.to_thread", timed_out)
    response = client.post("/api/code", json={"access_key": "x"})
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_sensitive_routes_have_security_headers_and_no_input_echo(client) -> None:
    response = client.post("/api/code", json={"access_key": "secret-input-canary" * 20})

    assert response.status_code == 422
    assert response.json() == {"status": "invalid_request"}
    assert "secret-input-canary" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_trusted_host_and_request_size_limit(client) -> None:
    rejected_host = client.get("/", headers={"host": "attacker.example"})
    assert rejected_host.status_code == 400

    oversized = client.post(
        "/api/code",
        content=b"x" * (MAX_REQUEST_BYTES + 1),
        headers={"content-type": "application/json"},
    )
    assert oversized.status_code == 413
    assert oversized.json() == {"status": "request_too_large"}
    assert oversized.headers["cache-control"] == "no-store"
    assert oversized.headers["x-frame-options"] == "DENY"


def test_admin_login_cookie_expiry_and_csrf(client, settings, service) -> None:
    wrong = client.post("/admin/login", data={"password": "wrong"})
    assert wrong.status_code == 401

    csrf = _login(client, settings)
    cookie = client.cookies.get(ADMIN_COOKIE)
    assert cookie
    set_cookie = client.post(
        "/admin/login",
        data={"password": settings.admin_password},
        follow_redirects=False,
    ).headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie
    assert "Path=/admin" in set_cookie
    csrf = (
        AdminSessionCodec(
            settings.master_key,
            lifetime_seconds=settings.admin_session_seconds,
        )
        .decode(client.cookies.get(ADMIN_COOKIE))
        .csrf_token
    )
    capture = client.get("/admin/api/capture/status")
    assert capture.status_code == 200
    assert capture.json()["state_label"] == "待机"
    assert capture.json()["message_label"] == "未启动捕获。"

    alias = service.database.upsert_alias(
        email="target@icloud.com",
        remote_metadata={"anonymousId": "target"},
    )
    missing_csrf = client.post(f"/admin/api/aliases/{alias['id']}/key")
    assert missing_csrf.status_code == 403
    issued = client.post(
        f"/admin/api/aliases/{alias['id']}/key",
        headers={"X-CSRF-Token": csrf},
    )
    assert issued.status_code == 200

    client.cookies.clear()
    expired, _session = AdminSessionCodec(
        settings.master_key,
        lifetime_seconds=settings.admin_session_seconds,
    ).issue(now=1)
    client.cookies.set(ADMIN_COOKIE, expired, path="/admin")
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_admin_browser_auth_and_dashboard_link(settings, service) -> None:
    browser_settings = replace(settings, cdp_url="http://127.0.0.1:9222")
    app = create_app(browser_settings, service=service)

    with TestClient(app, base_url="http://testserver") as browser_client:
        denied = browser_client.get(
            "/admin/api/browser/auth",
            follow_redirects=False,
        )
        assert denied.status_code == 303
        assert denied.headers["location"] == "/admin/login"

        _login(browser_client, browser_settings)
        allowed = browser_client.get("/admin/api/browser/auth")
        assert allowed.status_code == 204
        assert allowed.headers["cache-control"] == "no-store"

        dashboard = browser_client.get("/admin")
        assert dashboard.status_code == 200
        assert "/admin/browser/vnc.html?" in dashboard.text
        assert "打开 iCloud 浏览器" in dashboard.text


def test_admin_local_profile_capture_enabled_without_cdp(settings, service, tmp_path) -> None:
    local_settings = replace(
        settings,
        cdp_url="",
        browser_profile_dir=tmp_path / "browser-profile",
    )
    app = create_app(local_settings, service=service)

    with TestClient(app, base_url="http://testserver") as client:
        _login(client, local_settings)
        dashboard = client.get("/admin")
        assert dashboard.status_code == 200
        assert "本机持久 Chromium" in dashboard.text
        assert "/admin/browser/vnc.html?" not in dashboard.text
        assert 'action="/admin/hme/capture/start"' in dashboard.text
        capture_form = dashboard.text.split('action="/admin/hme/capture/start"', 1)[1][:220]
        assert "disabled" not in capture_form


def test_admin_dashboard_has_dedicated_lookup_history_section(client, settings, service) -> None:
    alias = service.database.upsert_alias(
        email="queried@icloud.com",
        remote_metadata={"anonymousId": "queried", "isActive": True},
    )
    service.database.record_audit_event(
        "code_lookup",
        "found",
        alias_id=alias["id"],
        ip_digest="4f8a1c2d3e4f5a6b",
    )
    timestamp = datetime.now(UTC).replace(microsecond=0)
    timestamp_iso = timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    timestamp_display = timestamp.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with service.database.transaction() as connection:
        connection.execute(
            "UPDATE audit_events SET created_at = ? WHERE event_type = 'code_lookup'",
            (timestamp_iso,),
        )
    _login(client, settings)

    response = client.get("/admin")

    assert response.status_code == 200
    assert 'href="#query-history"' in response.text
    assert 'id="query-history"' in response.text
    assert "查询记录" in response.text
    assert "隐藏邮箱" in response.text
    assert "查询结果" in response.text
    assert "来源指纹" in response.text
    assert "查询时间（北京时间）" in response.text
    assert "queried@icloud.com" in response.text
    assert "已返回验证码" in response.text
    assert "4f8a1c2d3e4f5a6b" in response.text
    assert f'datetime="{timestamp_iso}"' in response.text
    assert timestamp_display in response.text
    assert "data-local-time" not in response.text


def test_admin_dashboard_round_trips_optional_junk_folder(client, settings, service) -> None:
    csrf = _login(client, settings)

    saved = client.post(
        "/admin/imap",
        data={
            "csrf_token": csrf,
            "forwarding_email": "forwarding@example.com",
            "host": "imap.example.com",
            "port": "993",
            "username": "forwarding@example.com",
            "password": "app-password",
            "folder": "INBOX",
            "junk_folder": "Junk",
            "proxy": "",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == "/admin?notice=imap_saved"

    dashboard = client.get("/admin")

    assert dashboard.status_code == 200
    assert 'name="junk_folder"' in dashboard.text
    assert 'value="Junk"' in dashboard.text
    assert service.get_imap_config().junk_folder == "Junk"


def test_admin_can_reveal_current_key_but_dashboard_never_embeds_it(
    client, settings, service
) -> None:
    csrf = _login(client, settings)
    alias = service.database.upsert_alias(
        email="person@icloud.com",
        remote_metadata={"anonymousId": "person"},
        label="Person",
    )

    first = client.post(
        f"/admin/api/aliases/{alias['id']}/key",
        headers={"X-CSRF-Token": csrf},
    ).json()["access_key"]
    dashboard = client.get("/admin")
    assert first not in dashboard.text
    assert first[-4:] in dashboard.text
    assert "查看完整密钥" in dashboard.text

    missing_csrf = client.post(f"/admin/api/aliases/{alias['id']}/key/reveal")
    assert missing_csrf.status_code == 403
    revealed = client.post(
        f"/admin/api/aliases/{alias['id']}/key/reveal",
        headers={"X-CSRF-Token": csrf},
    )
    assert revealed.status_code == 200
    assert revealed.json()["access_key"] == first
    assert revealed.headers["cache-control"] == "no-store"

    second_response = client.post(
        f"/admin/api/aliases/{alias['id']}/key",
        headers={"X-CSRF-Token": csrf},
    )
    second = second_response.json()["access_key"]
    assert first not in second_response.text
    assert service.database.find_alias_by_access_key_hash(hash_access_key(first)) is None
    assert service.database.find_alias_by_access_key_hash(hash_access_key(second)) is not None
    revealed_second = client.post(
        f"/admin/api/aliases/{alias['id']}/key/reveal",
        headers={"X-CSRF-Token": csrf},
    )
    assert revealed_second.json()["access_key"] == second

    revoked = client.delete(
        f"/admin/api/aliases/{alias['id']}/key",
        headers={"X-CSRF-Token": csrf},
    )
    assert revoked.status_code == 200
    assert service.database.find_alias_by_access_key_hash(hash_access_key(second)) is None


def test_legacy_hash_only_key_requires_explicit_rotation_before_admin_reveal(
    client, settings, service
) -> None:
    alias = service.database.upsert_alias(
        email="legacy@icloud.com",
        remote_metadata={"anonymousId": "legacy"},
        label="Legacy",
    )
    service.database.issue_access_key(alias["id"])
    with service.database.transaction() as connection:
        connection.execute(
            "UPDATE aliases SET access_key_blob = NULL WHERE id = ?",
            (alias["id"],),
        )
    csrf = _login(client, settings)

    dashboard = client.get("/admin")
    response = client.post(
        f"/admin/api/aliases/{alias['id']}/key/reveal",
        headers={"X-CSRF-Token": csrf},
    )

    assert "轮换后可查看" in dashboard.text
    assert response.status_code == 409
    assert response.json() == {"status": "conflict"}


def test_admin_recent_codes_do_not_require_alias_access_keys(client, settings, service) -> None:
    _configure_imap(service)
    alias = service.database.upsert_alias(
        email="unkeyed@icloud.com",
        remote_metadata={"anonymousId": "unkeyed", "isActive": True},
        label="No public key",
    )
    FakeImapReader.recent_batch = RecentOtpBatch(
        items=(
            RecentOtpResult(
                alias="unkeyed@icloud.com",
                code="246810",
                uid="44",
                received_at=datetime.fromtimestamp(NOW - 2, tz=UTC),
            ),
        ),
        scanned=8,
        truncated=False,
    )

    unauthenticated = client.post(
        "/admin/api/codes/recent",
        headers={"X-CSRF-Token": "missing-session"},
    )
    assert unauthenticated.status_code == 401
    csrf = _login(client, settings)
    missing_csrf = client.post("/admin/api/codes/recent")
    assert missing_csrf.status_code == 403

    response = client.post(
        "/admin/api/codes/recent",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "status": "ok",
        "codes": [
            {
                "alias_id": alias["id"],
                "email": "unkeyed@icloud.com",
                "label": "No public key",
                "code": "246810",
                "received_at": "2027-01-15T07:59:58Z",
                "received_at_display": "2027-01-15 15:59:58",
            }
        ],
        "scanned": 8,
        "truncated": False,
    }
    dashboard = client.get("/admin")
    assert "246810" not in dashboard.text
    assert 'id="admin-codes"' in dashboard.text
    assert "刷新验证码" in dashboard.text


def test_admin_can_manage_imported_alias_lifecycle_with_csrf_and_confirmation(
    client, settings, service
) -> None:
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
    service.save_hme_session(_hme_session())
    aliases = service.database.list_aliases()
    active = next(item for item in aliases if item["state"] == "active")
    inactive = next(item for item in aliases if item["state"] == "inactive")
    csrf = _login(client, settings)

    dashboard = client.get("/admin")
    assert "从 iCloud 导入 / 刷新" in dashboard.text
    assert "停用 Alias" in dashboard.text
    assert "永久删除 Alias" in dashboard.text
    assert 'id="delete-alias-modal"' in dashboard.text

    missing_csrf = client.post(
        f"/admin/api/aliases/{active['id']}/deactivate",
        json={"confirmed": True},
    )
    assert missing_csrf.status_code == 403

    unconfirmed = client.post(
        f"/admin/api/aliases/{active['id']}/deactivate",
        json={"confirmed": False},
        headers={"X-CSRF-Token": csrf},
    )
    assert unconfirmed.status_code == 422
    assert FakeHmeClient.lifecycle_calls == []

    deactivated = client.post(
        f"/admin/api/aliases/{active['id']}/deactivate",
        json={"confirmed": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["state"] == "inactive"

    reactivated = client.post(
        f"/admin/api/aliases/{active['id']}/reactivate",
        json={"confirmed": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert reactivated.status_code == 200
    assert reactivated.json()["state"] == "active"

    wrong_confirmation = client.request(
        "DELETE",
        f"/admin/api/aliases/{inactive['id']}",
        json={"confirmation": "wrong@icloud.com"},
        headers={"X-CSRF-Token": csrf},
    )
    assert wrong_confirmation.status_code == 409

    deleted = client.request(
        "DELETE",
        f"/admin/api/aliases/{inactive['id']}",
        json={"confirmation": "inactive@icloud.com"},
        headers={"X-CSRF-Token": csrf},
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"status": "deleted"}


def test_partial_alias_batch_returns_already_created_keys(client, settings, service) -> None:
    FakeHmeClient.aliases = []
    service.save_hme_session(_hme_session())
    FakeHmeClient.created = [
        {"hme": "one@icloud.com", "anonymousId": "one", "isActive": True},
        HmeError("rate limited"),
    ]
    csrf = _login(client, settings)

    response = client.post(
        "/admin/api/aliases",
        json={"count": 2, "label_prefix": "Person", "note": "", "sender_filter": ""},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    job_id = response.json()["job_id"]
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        status = client.get(f"/admin/api/jobs/{job_id}")
        if status.json()["status"] in {"partial", "failed", "needs_reconcile"}:
            break
        time.sleep(0.01)
    assert status.json()["status"] == "needs_reconcile"
    assert status.json()["succeeded"] == 1
    assert status.json()["results"][0]["email"] == "one@icloud.com"
    assert "access_key" not in status.text
    missing_csrf = client.post(f"/admin/api/jobs/{job_id}/results")
    assert missing_csrf.status_code == 403
    revealed = client.post(
        f"/admin/api/jobs/{job_id}/results",
        headers={"X-CSRF-Token": csrf},
    )
    assert revealed.status_code == 200
    assert revealed.headers["cache-control"] == "no-store"
    assert revealed.json()["results"][0]["access_key"].startswith("icg_")


def test_bulk_alias_api_authentication_validation_and_mixed_results(
    client, settings, service
) -> None:
    active = service.database.upsert_alias(
        email="active@icloud.com",
        remote_metadata={"anonymousId": "active", "isActive": True},
        state="active",
    )
    inactive = service.database.upsert_alias(
        email="inactive@icloud.com",
        remote_metadata={"anonymousId": "inactive", "isActive": False},
        state="inactive",
    )
    unauthenticated = client.post(
        "/admin/api/aliases/bulk",
        json={"action": "issue_keys", "alias_ids": [active["id"]]},
    )
    assert unauthenticated.status_code == 401
    csrf = _login(client, settings)
    duplicate = client.post(
        "/admin/api/aliases/bulk",
        json={"action": "issue_keys", "alias_ids": [active["id"], active["id"]]},
        headers={"X-CSRF-Token": csrf},
    )
    assert duplicate.status_code == 422
    response = client.post(
        "/admin/api/aliases/bulk",
        json={
            "action": "issue_keys",
            "alias_ids": [active["id"], inactive["id"], "missing"],
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    job_id = response.json()["job_id"]
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        payload = client.get(f"/admin/api/jobs/{job_id}").json()
        if payload["status"] in {"completed", "partial", "failed"}:
            break
        time.sleep(0.01)
    assert payload["requested"] == 3
    assert payload["succeeded"] == 1
    assert payload["failed"] == 2
    assert [item["status"] for item in payload["results"]] == [
        "success",
        "failed",
        "failed",
    ]
    assert [item.get("error") for item in service.database.get_batch_job(job_id)["items"]] == [
        None,
        "conflict",
        "not_found",
    ]


def test_dashboard_exports_effective_alias_batch_limit(client, settings, service) -> None:
    service.database.upsert_alias(
        email="active@icloud.com",
        remote_metadata={"anonymousId": "active", "isActive": True},
        state="active",
    )
    _login(client, settings)
    response = client.get("/admin")

    assert response.status_code == 200
    assert '<meta name="alias-batch-limit" content="100">' in response.text
    assert 'data-alias-batch-limit="100"' in response.text
    assert 'data-alias-state="active"' in response.text


def test_bulk_delete_rejects_active_aliases_before_job_creation(client, settings, service) -> None:
    active = service.database.upsert_alias(
        email="active@icloud.com",
        remote_metadata={"anonymousId": "active", "isActive": True},
        state="active",
    )
    csrf = _login(client, settings)

    response = client.post(
        "/admin/api/aliases/bulk",
        json={"action": "delete", "alias_ids": [active["id"]], "confirmed": True},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 422
    assert service.database.list_active_batch_jobs() == []


def test_bulk_alias_api_enforces_hard_limit(client, settings, service) -> None:
    csrf = _login(client, settings)
    response = client.post(
        "/admin/api/aliases/bulk",
        json={"action": "issue_keys", "alias_ids": [f"alias-{index}" for index in range(101)]},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 422


def test_batch_job_api_idempotency_conflict_and_poll_csrf_no_store(
    client, settings, service
) -> None:
    service.save_hme_session(_hme_session())
    FakeHmeClient.created = [
        {"hme": "queued@icloud.com", "anonymousId": "queued", "isActive": True}
    ]
    csrf = _login(client, settings)
    payload = {"count": 1, "label_prefix": "Queued"}
    first = client.post(
        "/admin/api/aliases",
        json=payload,
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "request-1"},
    )
    repeated = client.post(
        "/admin/api/aliases",
        json=payload,
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "request-1"},
    )
    conflict = client.post(
        "/admin/api/aliases",
        json={"count": 1, "label_prefix": "Different"},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "request-1"},
    )
    missing_csrf = client.post("/admin/api/aliases", json={"count": 1, "label_prefix": "Forbidden"})

    assert first.status_code == repeated.status_code == 202
    assert first.json()["job_id"] == repeated.json()["job_id"]
    assert conflict.status_code == 409
    assert missing_csrf.status_code == 403
    polled = client.get(f"/admin/api/jobs/{first.json()['job_id']}")
    assert polled.status_code == 200
    assert polled.headers["cache-control"] == "no-store"
    assert "Idempotency-Key" not in str(service.database.get_batch_job(first.json()["job_id"]))


def test_create_alias_api_accepts_one_hundred_and_rejects_more(client, settings, service) -> None:
    csrf = _login(client, settings)
    accepted = client.post(
        "/admin/api/aliases",
        json={"count": 100, "label_prefix": "Person"},
        headers={"X-CSRF-Token": csrf},
    )
    hard = client.post(
        "/admin/api/aliases",
        json={"count": 101, "label_prefix": "Person"},
        headers={"X-CSRF-Token": csrf},
    )
    assert accepted.status_code == 202
    assert accepted.json()["requested"] == 100
    assert len(service.database.get_batch_job(accepted.json()["job_id"])["items"]) == 100
    assert hard.status_code == 422
    assert FakeHmeClient.created == []


def test_alias_usage_api_requires_admin_csrf_and_escapes_custom_values(
    client, settings, service
) -> None:
    alias = service.database.upsert_alias(
        email="usage@icloud.com",
        remote_metadata={"anonymousId": "usage", "isActive": True},
        label="Usage",
    )
    csrf = _login(client, settings)

    missing_csrf = client.post(
        f"/admin/api/aliases/{alias['id']}/usage",
        json={"usage_label": "gpt"},
    )
    assert missing_csrf.status_code == 403

    fixed = client.post(
        f"/admin/api/aliases/{alias['id']}/usage",
        json={"usage_label": "GPT"},
        headers={"X-CSRF-Token": csrf},
    )
    assert fixed.status_code == 200
    assert fixed.json()["usage_label"] == "gpt"

    custom_value = '<script>alert("usage")</script>'
    custom = client.post(
        f"/admin/api/aliases/{alias['id']}/usage",
        json={"usage_label": custom_value},
        headers={"X-CSRF-Token": csrf},
    )
    assert custom.status_code == 200
    assert custom.json()["usage_label"] == custom_value

    dashboard = client.get("/admin")
    assert custom_value not in dashboard.text
    assert "&lt;script&gt;alert" in dashboard.text
    assert "alias-email-copy" in dashboard.text
    assert "usage-choice" in dashboard.text

    for invalid in ("line\nbreak", "nul\x00byte", "x" * 81):
        response = client.post(
            f"/admin/api/aliases/{alias['id']}/usage",
            json={"usage_label": invalid},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 422
    assert service.database.get_alias(alias["id"])["usage_label"] == custom_value

    cleared = client.post(
        f"/admin/api/aliases/{alias['id']}/usage",
        json={"usage_label": ""},
        headers={"X-CSRF-Token": csrf},
    )
    assert cleared.status_code == 200
    assert cleared.json()["usage_label"] == ""

    missing = client.post(
        "/admin/api/aliases/missing/usage",
        json={"usage_label": "grok"},
        headers={"X-CSRF-Token": csrf},
    )
    assert missing.status_code == 404

    client.cookies.clear()
    unauthorized = client.post(
        f"/admin/api/aliases/{alias['id']}/usage",
        json={"usage_label": "gpt"},
        headers={"X-CSRF-Token": csrf},
    )
    assert unauthorized.status_code == 401


def test_failed_hme_and_imap_updates_preserve_previous_values(client, settings, service) -> None:
    original_hme = _hme_session()
    service.save_hme_session(original_hme)
    original_imap = _configure_imap(service)
    csrf = _login(client, settings)

    FakeHmeClient.list_error = HmeError("session rejected")
    hme_response = client.post(
        "/admin/hme/import",
        data={"csrf_token": csrf, "session_import": _session_curl(_hme_session(client_id="new"))},
        follow_redirects=False,
    )
    assert hme_response.status_code == 303
    assert hme_response.headers["location"] == "/admin?notice=hme_error"
    assert service.get_hme_session() == original_hme

    FakeHmeClient.list_error = None
    FakeHmeClient.aliases = [
        {"hme": "broken@icloud.com", "anonymousId": "broken", "isActive": "yes"}
    ]
    malformed_response = client.post(
        "/admin/hme/import",
        data={
            "csrf_token": csrf,
            "session_import": _session_curl(_hme_session(client_id="new")),
        },
        follow_redirects=False,
    )
    assert malformed_response.status_code == 303
    assert malformed_response.headers["location"] == "/admin?notice=hme_error"
    assert service.get_hme_session() == original_hme

    FakeImapReader.check_error = ImapCredentialsError("login rejected")
    imap_response = client.post(
        "/admin/imap",
        data={
            "csrf_token": csrf,
            "forwarding_email": "new@example.com",
            "host": "new-imap.example.com",
            "port": "993",
            "username": "new@example.com",
            "password": "new-password",
            "folder": "INBOX",
            "junk_folder": "Spam",
            "proxy": "",
        },
        follow_redirects=False,
    )
    assert imap_response.status_code == 303
    assert imap_response.headers["location"] == "/admin?notice=imap_error"
    assert service.get_imap_config() == original_imap


def test_chunked_body_cannot_bypass_the_request_size_limit(client) -> None:
    def oversized_chunks():
        for _ in range(3):
            yield b"x" * (1024 * 1024)

    response = client.post("/api/code", content=oversized_chunks())

    assert response.status_code == 413
    assert response.json() == {"status": "request_too_large"}


def test_lifespan_broadcasts_both_stops_before_waiting_and_keeps_database_open(
    settings,
) -> None:
    gateway = GatewayService(
        settings,
        hme_client_factory=FakeHmeClient,
        imap_reader_factory=FakeImapReader,
        start_maintenance=False,
    )
    app = create_app(settings, service=gateway)
    jobs = app.state.jobs
    events = []
    jobs.start = lambda: events.append(("jobs_start", None))
    jobs.request_stop = lambda: events.append(("jobs_stop", None))
    gateway.request_stop = lambda: events.append(("gateway_stop", None))
    jobs.shutdown = lambda *, timeout: events.append(("jobs_wait", timeout)) or False
    gateway.shutdown = lambda *, timeout, close_database=True: (
        events.append(("gateway_wait", (timeout, close_database))) or True
    )

    try:
        with TestClient(app, base_url="http://testserver"):
            pass

        assert [event[0] for event in events] == [
            "jobs_start",
            "jobs_stop",
            "gateway_stop",
            "jobs_wait",
            "gateway_wait",
        ]
        assert events[-1][1][1] is False
        assert gateway.database.quick_check() == "ok"
    finally:
        gateway.database.close()


def test_chunked_body_within_the_limit_still_reaches_the_route(client, service) -> None:
    def small_chunks():
        yield b'{"access_key":'
        yield b' "not-a-valid-key"}'

    response = client.post(
        "/api/code",
        content=small_chunks(),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 404
    assert response.json() == {"status": "invalid_key"}


def test_http2_style_stream_without_length_is_still_limited(settings, service) -> None:
    app = create_app(settings, service=service)

    async def request() -> list[dict]:
        incoming = [
            {"type": "http.request", "body": b"x" * (1024 * 1024), "more_body": True},
            {"type": "http.request", "body": b"x" * (1024 * 1024), "more_body": True},
            {"type": "http.request", "body": b"x", "more_body": False},
        ]
        outgoing: list[dict] = []

        async def receive() -> dict:
            return incoming.pop(0)

        async def send(message: dict) -> None:
            outgoing.append(message)

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "2",
                "method": "POST",
                "scheme": "https",
                "path": "/api/code",
                "raw_path": b"/api/code",
                "query_string": b"",
                "root_path": "",
                "headers": [
                    (b"host", b"testserver"),
                    (b"content-type", b"application/json"),
                ],
                "client": ("203.0.113.9", 44321),
                "server": ("testserver", 443),
                "state": {},
            },
            receive,
            send,
        )
        return outgoing

    messages = asyncio.run(request())
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )

    assert start["status"] == 413
    assert body == b'{"status":"request_too_large"}'


@pytest.mark.parametrize("token", (b"", b"wrong-token", b"\xe9\xe9"))
def test_non_matching_csrf_tokens_are_rejected_without_a_server_error(
    client, settings, service, token: bytes
) -> None:
    # Headers arrive as latin-1, so a non-ASCII byte reaches the comparison as a
    # non-ASCII str.
    codec = AdminSessionCodec(settings.master_key, lifetime_seconds=3600)
    cookie, _session = codec.issue()
    client.cookies.set(ADMIN_COOKIE, cookie)

    response = client.post(
        "/admin/api/aliases",
        json={"count": 1, "label_prefix": "x"},
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 403


def test_non_ascii_form_csrf_token_is_rejected_without_a_server_error(
    client, settings, service
) -> None:
    codec = AdminSessionCodec(settings.master_key, lifetime_seconds=3600)
    cookie, _session = codec.issue()
    client.cookies.set(ADMIN_COOKIE, cookie)

    response = client.post("/admin/logout", data={"csrf_token": "令牌"})

    assert response.status_code == 403


def test_non_ascii_admin_password_is_rejected_rather_than_raising(client) -> None:
    response = client.post("/admin/login", data={"password": "错误的中文密码错误的中文密码"})

    assert response.status_code == 401


def test_admin_script_redirects_expired_sessions_without_generic_action_errors() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "icloud_gateway" / "static" / "admin.js"
    ).read_text()

    assert "class AuthenticationRequiredError extends Error" in script
    assert script.count("instanceof AuthenticationRequiredError") >= 9
    assert "邮箱：${item.email}；网站：${url}；密钥：${item.access_key}" in script
    assert 'createCopyButton(emailList(items), "一键复制")' in script
    assert 'createCopyButton(standardParameterList(items), "导出信息")' in script
    assert "localStorage" not in script
    assert "sessionStorage" not in script
    assert "window.prompt" not in script
    assert "localStorage" not in script
    assert "sessionStorage" not in script
    assert "code.textContent = item.code" in script
    assert 'querySelector("#delete-alias-form")' in script
    assert "response.status === 401 || response.status === 403" in script
    assert 'window.location.assign("/admin/login")' in script
    assert 'meta[name="alias-batch-limit"]' in script
    assert "alreadySelected" in script
    assert "aliasIds.length > aliasBatchLimit" in script
    assert "单次最多选择 ${aliasBatchLimit} 项" in script
    assert "明确失败 ${counts.failed} 项" in script
    assert "远端结果不确定 ${counts.unknown} 项" in script
    assert "尚未开始 ${counts.queued} 项" in script
    assert "async function writeClipboard" in script
    assert 'document.execCommand("copy")' in script
    assert 'querySelectorAll(".alias-email-copy")' in script
    assert "/usage`" in script
    assert "usageCurrent.textContent" in script
    assert "innerHTML" not in script


def test_admin_static_assets_are_versioned_and_revalidated(client, settings) -> None:
    _login(client, settings)

    dashboard = client.get("/admin")
    script_path = next(
        part.split('"', 1)[0]
        for part in dashboard.text.split('src="')[1:]
        if part.startswith("/static/admin.js?v=")
    )
    stylesheet_path = next(
        part.split('"', 1)[0]
        for part in dashboard.text.split('href="')[1:]
        if part.startswith("/static/app.css?v=")
    )

    assert len(script_path.rsplit("=", 1)[1]) == 16
    assert len(stylesheet_path.rsplit("=", 1)[1]) == 16
    assert client.get(script_path).headers["cache-control"] == "no-cache"
    assert client.get(stylesheet_path).headers["cache-control"] == "no-cache"


def test_admin_script_summarizes_partial_unknown_job_and_standard_fields() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = Path(__file__).resolve().parents[1] / "icloud_gateway" / "static" / "admin.js"
    harness = r"""
const noop = () => {};
global.document = {querySelector: () => null, querySelectorAll: () => [], addEventListener: noop};
global.window = {location: {origin: "https://gateway.example"}, addEventListener: noop};
const {
  emailList,
  jobCounts,
  jobSummary,
  standardParameterList,
  standardParameters,
} = require(process.argv[1]);
const job = {
  status: "needs_reconcile",
  requested: 10,
  results: [
    ...Array.from({length: 5}, () => ({status: "success"})),
    {status: "unknown"},
    ...Array.from({length: 4}, () => ({status: "queued"})),
  ],
};
const counts = jobCounts(job);
const expected = {success: 5, failed: 0, unknown: 1, queued: 4, running: 0, cancelled: 0};
if (JSON.stringify(counts) !== JSON.stringify(expected)) process.exit(1);
const summary = jobSummary(job);
if (!summary.includes("成功 5 项")) process.exit(2);
if (!summary.includes("远端结果不确定 1 项")) process.exit(3);
if (!summary.includes("尚未开始 4 项")) process.exit(4);
const items = [
  {
    email: "one@icloud.com",
    access_key: "icg_one",
    public_url: "https://gateway.example",
  },
  {
    email: "two@icloud.com",
    access_key: "icg_two",
    public_url: "https://gateway.example",
  },
];
const expectedOutput = "邮箱：one@icloud.com；网站：https://gateway.example；密钥：icg_one";
if (standardParameters(items[0]) !== expectedOutput) process.exit(5);
const expectedEmails = "one@icloud.com\ntwo@icloud.com";
if (emailList(items) !== expectedEmails) process.exit(6);
const expectedExport = [
  "邮箱：one@icloud.com；网站：https://gateway.example；密钥：icg_one",
  "邮箱：two@icloud.com；网站：https://gateway.example；密钥：icg_two",
].join("\n");
if (standardParameterList(items) !== expectedExport) process.exit(7);
"""
    subprocess.run([node, "-e", harness, str(script)], check=True)


def test_admin_script_does_not_resume_terminal_reconcile_jobs() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = Path(__file__).resolve().parents[1] / "icloud_gateway" / "static" / "admin.js"
    harness = """
const noop = () => {};
global.document = {querySelector: () => null, querySelectorAll: () => [], addEventListener: noop};
global.window = {location: {origin: "https://example.test"}, addEventListener: noop};
const {firstNonTerminalJob} = require(process.argv[1]);
const terminal = [
  {job_id: "reconcile", status: "needs_reconcile"},
  {job_id: "failed", status: "failed"},
  {job_id: "completed", status: "completed"},
];
if (firstNonTerminalJob(terminal) !== undefined) process.exit(1);
const queued = {job_id: "queued", status: "queued"};
if (firstNonTerminalJob([...terminal, queued]) !== queued) process.exit(2);
"""
    subprocess.run([node, "-e", harness, str(script)], check=True)


def test_admin_script_computes_remaining_selection_budget() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = Path(__file__).resolve().parents[1] / "icloud_gateway" / "static" / "admin.js"
    harness = """
const noop = () => {};
global.document = {querySelector: () => null, querySelectorAll: () => [], addEventListener: noop};
global.window = {location: {origin: "https://example.test"}, addEventListener: noop};
const {limitedVisibleSelectionCount} = require(process.argv[1]);
if (limitedVisibleSelectionCount(0, 159, 50) !== 50) process.exit(1);
if (limitedVisibleSelectionCount(48, 20, 50) !== 2) process.exit(2);
if (limitedVisibleSelectionCount(50, 20, 50) !== 0) process.exit(3);
"""
    subprocess.run([node, "-e", harness, str(script)], check=True)


def test_admin_script_classifies_usage_values() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = Path(__file__).resolve().parents[1] / "icloud_gateway" / "static" / "admin.js"
    harness = """
const noop = () => {};
global.document = {querySelector: () => null, querySelectorAll: () => [], addEventListener: noop};
global.window = {location: {origin: "https://example.test"}, addEventListener: noop};
const {usageKind} = require(process.argv[1]);
if (usageKind("") !== "empty") process.exit(1);
if (usageKind("gpt") !== "gpt") process.exit(2);
if (usageKind("grok") !== "grok") process.exit(3);
if (usageKind("内部工具") !== "custom") process.exit(4);
"""
    subprocess.run([node, "-e", harness, str(script)], check=True)


def test_every_template_icon_exists() -> None:
    package = Path(__file__).resolve().parents[1] / "icloud_gateway"
    referenced = set()
    for path in (package / "templates").glob("*.html"):
        text = path.read_text(encoding="utf-8")
        for name in (
            "ban",
            "circle-alert",
            "circle-check",
            "cloud",
            "copy",
            "eye",
            "eye-off",
            "key-round",
            "log-in",
            "log-out",
            "mail",
            "plus",
            "radio-tower",
            "refresh-cw",
            "rotate-ccw",
            "save",
            "search",
            "settings",
            "shield-check",
            "upload",
            "x",
        ):
            if f"{name}.svg" in text or name in {"circle-alert", "circle-check"}:
                referenced.add(name)
    referenced.update({"eye-off"})
    missing = [
        name
        for name in sorted(referenced)
        if not (package / "static/icons" / f"{name}.svg").is_file()
    ]
    assert missing == []


def test_admin_open_mode_skips_password(settings, service) -> None:
    open_settings = replace(settings, admin_open=True, admin_password="")
    app = create_app(open_settings, service=service)
    with TestClient(app, base_url="http://testserver") as client:
        login = client.get("/admin/login", follow_redirects=False)
        assert login.status_code == 303
        assert login.headers["location"] == "/admin"
        assert client.cookies.get(ADMIN_COOKIE)

        dashboard = client.get("/admin")
        assert dashboard.status_code == 200
        assert "iCloud 验证码网关" in dashboard.text

        # form posts should not require a real CSRF token in open mode
        resp = client.post(
            "/admin/hme/capture/cancel",
            data={"csrf_token": "anything"},
            follow_redirects=False,
        )
        assert resp.status_code == 303


def test_admin_one_click_edge_sync_feedback(settings, service) -> None:
    class FakeEdge:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def upsert_alias(self, **kwargs):
            self.calls.append(str(kwargs.get("email") or ""))
            return {"status": "ok"}

    control_settings = replace(
        settings,
        deployment_mode="control",
        control_plane_token="control-token-control-token-123456",
        edge_base_url="https://icloud.example.test",
        edge_sync_enabled=True,
    )
    edge = FakeEdge()
    control_service = GatewayService(
        control_settings,
        hme_client_factory=FakeHmeClient,
        imap_reader_factory=FakeImapReader,
        clock=lambda: NOW,
        sleeper=lambda _seconds: None,
        start_maintenance=False,
        edge_sync_client=edge,
    )
    alias = control_service.database.upsert_alias(
        email="user1@icloud.com",
        remote_metadata={
            "anonymousId": "anon-1",
            "hme": "user1@icloud.com",
            "isActive": True,
        },
        label="demo",
        state="active",
    )
    issued = control_service.database.issue_access_key(alias["id"])
    assert issued.access_key

    app = create_app(control_settings, service=control_service)
    with TestClient(app, base_url="http://testserver") as client:
        _login(client, control_settings)
        page = client.get("/admin")
        assert page.status_code == 200
        assert "同步到云端" in page.text
        assert 'action="/admin/edge/sync"' in page.text
        csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]

        resp = client.post(
            "/admin/edge/sync",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "notice=edge_sync_ok" in location
        assert "ok=1" in location

        done = client.get(location)
        assert done.status_code == 200
        assert "云端同步成功" in done.text
        assert "已推送 1 个密钥" in done.text
        assert edge.calls == ["user1@icloud.com"]


def test_admin_one_click_edge_sync_disabled_reports_error(settings, service) -> None:
    disabled = replace(
        settings,
        deployment_mode="control",
        edge_sync_enabled=False,
        control_plane_token="control-token-control-token-123456",
        edge_base_url="https://icloud.example.test",
    )
    app = create_app(disabled, service=service)
    with TestClient(app, base_url="http://testserver") as client:
        _login(client, disabled)
        page = client.get("/admin")
        csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        resp = client.post(
            "/admin/edge/sync",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "notice=edge_sync_error" in resp.headers["location"]
        body = client.get(resp.headers["location"])
        assert "云端同步失败" in body.text
