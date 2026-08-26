from __future__ import annotations

import stat

import pytest

from icloud_gateway.config import ConfigurationError, Settings
from icloud_gateway.proxy import (
    ProxyConfigurationError,
    parse_proxy_spec,
    proxy_from_environment,
    render_browser_proxy_config,
    write_proxychains_config,
)
from icloud_gateway.proxy import (
    main as proxy_main,
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
    assert proxy.proxychains_line == "socks5 cn-proxy.example 1080 account@example.com p@ssword"
    with pytest.raises(ProxyConfigurationError, match="authentication-free relay"):
        _ = proxy.chromium_url


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


def test_browser_proxy_config_resolves_docker_dns_name(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        "icloud_gateway.proxy.socket.gethostbyname",
        lambda host: "172.30.0.12" if host == "cn-proxy" else "",
    )

    target = tmp_path / "proxychains.conf"
    proxy = render_browser_proxy_config(
        target,
        {
            "BROWSER_PROXY_SERVER": "socks5h://cn-proxy:7891",
            "BROWSER_PROXY_REQUIRED": "1",
        },
    )

    assert proxy is not None
    assert proxy.requests_url == "socks5h://172.30.0.12:7891"
    assert proxy.chromium_url == "socks5://172.30.0.12:7891"
    content = target.read_text(encoding="utf-8")
    assert "socks5 172.30.0.12 7891" in content
    assert "socks5 cn-proxy 7891" not in content


def test_browser_proxy_dns_failure_fails_closed(monkeypatch, tmp_path) -> None:
    def fail_resolution(host: str) -> str:
        raise OSError(f"cannot resolve {host}")

    monkeypatch.setattr(
        "icloud_gateway.proxy.socket.gethostbyname",
        fail_resolution,
    )
    target = tmp_path / "proxychains.conf"

    with pytest.raises(ProxyConfigurationError, match="cannot be resolved"):
        render_browser_proxy_config(
            target,
            {
                "BROWSER_PROXY_SERVER": "socks5h://cn-proxy:7891",
                "BROWSER_PROXY_REQUIRED": "1",
            },
        )

    assert not target.exists()


def test_browser_proxy_authentication_requires_local_relay(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "icloud_gateway.proxy.socket.gethostbyname",
        lambda host: "172.30.0.12",
    )

    with pytest.raises(ProxyConfigurationError, match="authentication-free relay"):
        render_browser_proxy_config(
            tmp_path / "proxychains.conf",
            {
                "BROWSER_PROXY_SERVER": "socks5h://cn-proxy:7891",
                "BROWSER_PROXY_USERNAME": "user",
                "BROWSER_PROXY_PASSWORD": "secret",
                "BROWSER_PROXY_REQUIRED": "1",
            },
        )


def test_proxy_cli_prints_resolved_native_chromium_url(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    monkeypatch.setenv("BROWSER_PROXY_SERVER", "socks5h://cn-proxy:7891")
    monkeypatch.setenv("BROWSER_PROXY_REQUIRED", "1")
    monkeypatch.setattr(
        "icloud_gateway.proxy.socket.gethostbyname",
        lambda host: "172.30.0.12",
    )

    assert proxy_main([str(tmp_path / "proxychains.conf")]) == 0
    assert capsys.readouterr().out.strip() == "socks5://172.30.0.12:7891"


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
    monkeypatch.setenv("ICLOUD_GATEWAY_EDGE_PROXY_SERVER", "socks5h://127.0.0.1:7890")

    settings = Settings.from_environment()
    assert settings.hme_proxy == "http://user:pass@proxy.example:8080"
    assert settings.edge_proxy == "socks5h://127.0.0.1:7890"

    monkeypatch.delenv("ICLOUD_GATEWAY_HME_PROXY_SERVER")
    with pytest.raises(ConfigurationError):
        Settings.from_environment()


def test_settings_validates_operator_sso_token(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(
        "ICLOUD_GATEWAY_MASTER_KEY",
        "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    )
    monkeypatch.setenv("ICLOUD_GATEWAY_ADMIN_PASSWORD", "correct horse battery staple")
    monkeypatch.setenv("ICLOUD_GATEWAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ICLOUD_GATEWAY_OPERATOR_ACCESS_TOKEN", "invalid")

    with pytest.raises(ConfigurationError):
        Settings.from_environment()

    monkeypatch.setenv("ICLOUD_GATEWAY_OPERATOR_ACCESS_TOKEN", f"icg_{'o' * 43}")
    assert Settings.from_environment().operator_access_token == f"icg_{'o' * 43}"
