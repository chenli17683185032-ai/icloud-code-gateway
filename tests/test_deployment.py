from __future__ import annotations

from pathlib import Path


def test_browser_forward_auth_does_not_forward_websocket_upgrade_headers() -> None:
    caddyfile = (Path(__file__).resolve().parents[1] / "Caddyfile").read_text()

    forward_auth = caddyfile.split("forward_auth app:8080 {", 1)[1].split("}", 1)[0]
    assert "uri /admin/api/browser/auth" in forward_auth
    assert "header_up -Connection" in forward_auth
    assert "header_up -Upgrade" in forward_auth


def test_shared_server_overlay_keeps_caddy_and_proxy_boundaries_separate() -> None:
    root = Path(__file__).resolve().parents[1]
    overlay = (root / "docker-compose.server.yml").read_text()
    caddy_site = (root / "deploy" / "Caddyfile.icloud.yunbay.xyz").read_text()

    assert "metacubex/mihomo:v1.19.28" in overlay
    assert "standalone-caddy" in overlay
    assert "external: true" in overlay
    assert "icloud-code-gateway-app" in overlay
    assert "icloud-code-gateway-browser" in overlay
    assert "forward_auth icloud-code-gateway-app:8080" in caddy_site
    assert "header_up -Connection" in caddy_site
    assert "header_up -Upgrade" in caddy_site
    assert "reverse_proxy icloud-code-gateway-browser:6080" in caddy_site


def test_browser_uses_native_proxy_and_bounded_process_cleanup() -> None:
    entrypoint = (
        Path(__file__).resolve().parents[1] / "docker" / "browser-entrypoint.sh"
    ).read_text()

    assert 'browser_proxy="$(python3 ' in entrypoint
    assert 'browser_command+=("--proxy-server=$browser_proxy")' in entrypoint
    assert "browser_command=(proxychains4" not in entrypoint
    assert "for _ in {1..50}" in entrypoint
    assert 'kill -KILL "${pids[@]}"' in entrypoint
