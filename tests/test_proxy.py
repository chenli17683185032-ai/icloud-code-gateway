from __future__ import annotations

import stat

import pytest

from icloud_gateway.config import ConfigurationError, Settings
from icloud_gateway.proxy import (
    ProxyConfigurationError,
    parse_proxy_spec,
    proxy_from_environment,
    write_proxychains_config,
)


def test_proxy_spec_is_shared_by_requests_and_proxychains() -> None:
    proxy = parse_proxy_spec(
        "socks5h://cn-proxy.example:1080",
        username="account@example.com",
        password="p@ssword",
        required=True,
    )

    assert proxy is not None
    assert proxy.requests_url == (
        "socks5h://account%40example.com:p%40ssword@cn-proxy.example:1080"
    )
    assert proxy.proxychains_line == (
        "socks5 cn-proxy.example 1080 account@example.com p@ssword"
    )


def test_proxy_required_and_invalid_values_fail_closed() -> None:
    with pytest.raises(ProxyConfigurationError):
        parse_proxy_spec("", required=True)
    with pytest.raises(ProxyConfigurationError):
        parse_proxy_spec("https://proxy.example:443")
    with pytest.raises(ProxyConfigurationError) as caught:
        parse_proxy_spec(
            "http://proxy.example:8080",
            username="user",
            password="secret value",
        )
    assert "secret value" not in str(caught.value)


def test_proxychains_config_is_private_and_uses_strict_chain(tmp_path) -> None:
    proxy = parse_proxy_spec("http://proxy.example:8080", username="user", password="pass")
    assert proxy is not None

    target = write_proxychains_config(tmp_path / "proxychains.conf", proxy)
    content = target.read_text(encoding="utf-8")

    assert target.stat().st_mode & 0o777 == stat.S_IRUSR | stat.S_IWUSR
    assert "strict_chain" in content
    assert "dynamic_chain" not in content
    assert "http proxy.example 8080 user pass" in content


def test_proxy_environment_supports_the_worker_style_fields() -> None:
    proxy = proxy_from_environment(
        "BROWSER_PROXY",
        {
            "BROWSER_PROXY_SERVER": "http://proxy.example:8080",
            "BROWSER_PROXY_USERNAME": "user",
            "BROWSER_PROXY_PASSWORD": "pass",
            "BROWSER_PROXY_REQUIRED": "1",
        },
    )

    assert proxy is not None
    assert proxy.requests_url == "http://user:pass@proxy.example:8080"


def test_settings_loads_hme_proxy_and_rejects_missing_required_proxy(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "ICLOUD_GATEWAY_MASTER_KEY",
        "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    )
    monkeypatch.setenv("ICLOUD_GATEWAY_ADMIN_PASSWORD", "correct horse battery staple")
    monkeypatch.setenv("ICLOUD_GATEWAY_HME_PROXY_SERVER", "http://proxy.example:8080")
    monkeypatch.setenv("ICLOUD_GATEWAY_HME_PROXY_USERNAME", "user")
    monkeypatch.setenv("ICLOUD_GATEWAY_HME_PROXY_PASSWORD", "pass")
    monkeypatch.setenv("ICLOUD_GATEWAY_HME_PROXY_REQUIRED", "1")

    assert Settings.from_environment().hme_proxy == "http://user:pass@proxy.example:8080"

    monkeypatch.delenv("ICLOUD_GATEWAY_HME_PROXY_SERVER")
    with pytest.raises(ConfigurationError):
        Settings.from_environment()
