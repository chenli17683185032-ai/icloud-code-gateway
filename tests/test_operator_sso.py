from __future__ import annotations

from pathlib import Path

import pytest

from icloud_gateway.config import Settings
from icloud_gateway.operator_sso import (
    OPERATOR_SESSION_COOKIE,
    OperatorSsoClient,
    OperatorSsoError,
)


class _Response:
    def __init__(self, *, status_code: int = 200, cookie: str = "") -> None:
        self.status_code = status_code
        self.headers = {"Set-Cookie": cookie} if cookie else {}


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.trust_env = True
        self.proxies: dict[str, str] = {}
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.response


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "data_dir": tmp_path,
        "master_key": bytes(range(32)),
        "admin_password": "correct horse battery staple",
        "cookie_secure": False,
        "edge_base_url": "https://icloud.example.test",
        "operator_access_token": f"icg_{'o' * 43}",
    }
    values.update(overrides)
    return Settings(**values)


def test_operator_sso_exchanges_server_token_for_strict_cookie(tmp_path: Path) -> None:
    cookie = (
        f"{OPERATOR_SESSION_COOKIE}=session; Path=/; Max-Age=900; "
        "HttpOnly; Secure; SameSite=Strict"
    )
    session = _Session(_Response(cookie=cookie))
    client = OperatorSsoClient(_settings(tmp_path), session=session)

    issued = client.exchange()

    assert issued.header_value == cookie
    assert session.trust_env is False
    assert session.calls[0]["url"] == "https://icloud.example.test/api/operator/session"
    assert session.calls[0]["json"] == {"token": f"icg_{'o' * 43}"}


@pytest.mark.parametrize(
    ("status_code", "cookie"),
    (
        (401, ""),
        (200, "wrong=session; Path=/; HttpOnly; Secure; SameSite=Strict"),
        (200, f"{OPERATOR_SESSION_COOKIE}=session; Path=/; Secure; SameSite=Strict"),
    ),
)
def test_operator_sso_rejects_failed_or_weak_responses(
    tmp_path: Path, status_code: int, cookie: str
) -> None:
    client = OperatorSsoClient(
        _settings(tmp_path),
        session=_Session(_Response(status_code=status_code, cookie=cookie)),
    )

    with pytest.raises(OperatorSsoError):
        client.exchange()


def test_operator_sso_requires_server_only_token(tmp_path: Path) -> None:
    client = OperatorSsoClient(
        _settings(tmp_path, operator_access_token=""),
        session=_Session(_Response()),
    )

    with pytest.raises(OperatorSsoError):
        client.exchange()
