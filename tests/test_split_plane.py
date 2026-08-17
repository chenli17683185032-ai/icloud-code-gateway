from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from icloud_gateway.config import ConfigurationError, Settings, decode_master_key
from icloud_gateway.edge_sync import EdgeSyncClient, EdgeSyncError
from icloud_gateway.security import generate_access_key
from icloud_gateway.service import GatewayService
from icloud_gateway.web import create_app


def _master_key() -> bytes:
    return decode_master_key("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "data_dir": tmp_path,
        "master_key": _master_key(),
        "admin_password": "x" * 16,
        "cookie_secure": False,
        "trusted_hosts": ("testserver", "localhost", "127.0.0.1"),
        "deployment_mode": "full",
        "control_plane_token": "control-token-abcdefghijklmnop",
        "edge_base_url": "https://edge.example.com",
        "edge_sync_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def test_edge_mode_registers_alias_and_serves_public_code_mapping(tmp_path: Path):
    settings = _settings(tmp_path, deployment_mode="edge", edge_sync_enabled=False)
    service = GatewayService(settings, start_maintenance=False)
    app = create_app(settings, service=service)
    client = TestClient(app)

    access_key = generate_access_key()
    response = client.post(
        "/control/v1/aliases",
        headers={"Authorization": "Bearer control-token-abcdefghijklmnop"},
        json={
            "id": "local-1",
            "email": "hidden.one@icloud.com",
            "label": "one",
            "state": "active",
            "access_key": access_key,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["email"] == "hidden.one@icloud.com"
    assert body["has_access_key"] is True

    # invalid token rejected
    bad = client.post(
        "/control/v1/aliases",
        headers={"Authorization": "Bearer wrong-token"},
        json={"email": "x@icloud.com", "state": "active"},
    )
    assert bad.status_code == 401

    # public page exists for edge
    homepage = client.get("/")
    assert homepage.status_code == 200
    assert "仅 GPT / Grok" in homepage.text

    # lookup without IMAP is unavailable (not invalid_key)
    code = client.post("/api/code", json={"access_key": access_key})
    assert code.status_code == 503
    assert code.json()["status"] == "unavailable"

    # revoke and re-issue by email
    rev = client.delete(
        "/control/v1/aliases/by-email/hidden.one@icloud.com/key",
        headers={"Authorization": "Bearer control-token-abcdefghijklmnop"},
    )
    assert rev.status_code == 200
    new_key = generate_access_key()
    issue = client.post(
        "/control/v1/aliases/by-email/hidden.one@icloud.com/key",
        headers={"Authorization": "Bearer control-token-abcdefghijklmnop"},
        json={"access_key": new_key},
    )
    assert issue.status_code == 200

    service.shutdown(timeout=1, close_database=True)


def test_control_mode_hides_public_otp_and_requires_edge_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ICLOUD_GATEWAY_MASTER_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    monkeypatch.setenv("ICLOUD_GATEWAY_ADMIN_PASSWORD", "x" * 16)
    monkeypatch.setenv("ICLOUD_GATEWAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ICLOUD_GATEWAY_DEPLOYMENT_MODE", "control")
    monkeypatch.setenv("ICLOUD_GATEWAY_CONTROL_PLANE_TOKEN", "control-token-abcdefghijklmnop")
    monkeypatch.delenv("ICLOUD_GATEWAY_EDGE_BASE_URL", raising=False)
    with pytest.raises(ConfigurationError):
        Settings.from_environment()

    settings = _settings(
        tmp_path,
        deployment_mode="control",
        edge_base_url="https://icloud.yunbay.xyz",
        edge_sync_enabled=False,
    )
    service = GatewayService(settings, start_maintenance=False)
    app = create_app(settings, service=service)
    client = TestClient(app)
    assert client.get("/").status_code in {303, 200}
    # public code endpoint disabled
    response = client.post("/api/code", json={"access_key": generate_access_key()})
    assert response.status_code == 404
    service.shutdown(timeout=1, close_database=True)


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload=None):
        self.status_code = status_code
        self._payload = payload or {"status": "ok"}
        self.content = b"{}"
        self.text = "{}"

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.calls = []
        self.trust_env = True
        self.proxies = {}

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        return _FakeResponse()


def test_control_plane_pushes_issued_key_to_edge(tmp_path: Path):
    fake = _FakeSession()
    settings = _settings(
        tmp_path,
        deployment_mode="control",
        edge_base_url="https://icloud.yunbay.xyz",
        edge_sync_enabled=True,
    )
    edge = EdgeSyncClient(settings, session=fake)
    service = GatewayService(settings, start_maintenance=False, edge_sync_client=edge)

    alias = service.database.upsert_alias(
        email="local.hidden@icloud.com",
        remote_metadata={"anonymousId": "a1", "hme": "local.hidden@icloud.com", "isActive": True},
        label="local",
        state="active",
    )
    issued = service.issue_access_key(alias["id"])
    assert issued.access_key.startswith("icg_")
    assert fake.calls
    call = fake.calls[-1]
    assert call["method"] == "POST"
    assert call["url"].endswith("/control/v1/aliases/by-email/local.hidden%40icloud.com/key")
    assert call["json"]["access_key"] == issued.access_key
    assert call["headers"]["Authorization"].startswith("Bearer ")

    service.shutdown(timeout=1, close_database=True)


def test_edge_sync_error_surfaces(tmp_path: Path):
    class BoomSession(_FakeSession):
        def request(self, method, url, headers=None, json=None, timeout=None):
            self.calls.append({"method": method, "url": url})
            return _FakeResponse(status_code=502, payload={"status": "edge_down"})

    settings = _settings(
        tmp_path,
        deployment_mode="control",
        edge_base_url="https://icloud.yunbay.xyz",
        edge_sync_enabled=True,
    )
    edge = EdgeSyncClient(settings, session=BoomSession())
    with pytest.raises(EdgeSyncError):
        edge.upsert_alias(
            alias_id="1",
            email="a@icloud.com",
            label="a",
            state="active",
            access_key=generate_access_key(),
        )
