from __future__ import annotations

from pathlib import Path

CLOUDFLARE_RANGE_COUNT = 22


def test_browser_forward_auth_does_not_forward_websocket_upgrade_headers() -> None:
    caddyfile = (Path(__file__).resolve().parents[1] / "Caddyfile").read_text()

    forward_auth = caddyfile.split("forward_auth app:8080 {", 1)[1].split("}", 1)[0]
    assert "uri /admin/api/browser/auth" in forward_auth
    assert "header_up -Connection" in forward_auth
    assert "header_up -Upgrade" in forward_auth
    assert "header_up X-Forwarded-For" not in caddyfile


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
    assert "health_uri /healthz" in caddy_site
    assert "health_headers {" in caddy_site
    assert "Host icloud.yunbay.xyz" in caddy_site
    matcher_line = next(
        line.strip() for line in caddy_site.splitlines() if line.strip().startswith("@cloudflare ")
    )
    matcher_ranges = set(matcher_line.split()[2:])
    trusted_ranges = {
        value
        for line in caddy_site.splitlines()
        if line.strip().startswith("trusted_proxies ")
        for value in line.split()[1:]
    }
    assert len(matcher_ranges) == CLOUDFLARE_RANGE_COUNT
    assert matcher_ranges == trusted_ranges
    assert (
        "request_header @cloudflare X-Forwarded-For {http.request.header.CF-Connecting-IP}"
        in caddy_site
    )
    assert "header_up X-Forwarded-For" not in caddy_site


def test_compose_passes_maintenance_and_batch_environment_to_app() -> None:
    root = Path(__file__).resolve().parents[1]
    base = (root / "docker-compose.yml").read_text()
    server = (root / "docker-compose.server.yml").read_text()
    expected = {
        "ICLOUD_GATEWAY_HME_MAINTENANCE_SECONDS": "21600",
        "ICLOUD_GATEWAY_HME_FRESHNESS_SECONDS": "3600",
        "ICLOUD_GATEWAY_HME_RETRY_MAX_SECONDS": "3600",
        "ICLOUD_GATEWAY_ALIAS_BATCH_LIMIT": "50",
    }

    for name, default in expected.items():
        interpolation = f"{name}: ${{{name}:-{default}}}"
        assert interpolation in base
        assert interpolation in server


def test_proxy_defaults_fail_closed_and_build_context_excludes_secrets() -> None:
    root = Path(__file__).resolve().parents[1]
    compose = (root / "docker-compose.yml").read_text()
    ignored = set((root / ".dockerignore").read_text().splitlines())

    assert compose.count("${CN_PROXY_REQUIRED:-1}") == 2
    assert "${CN_PROXY_REQUIRED:-0}" not in compose
    assert {".env*", "!.env.example", "secrets", "*.pem", "*.key"} <= ignored


def test_browser_uses_native_proxy_and_bounded_process_cleanup() -> None:
    entrypoint = (
        Path(__file__).resolve().parents[1] / "docker" / "browser-entrypoint.sh"
    ).read_text()

    assert 'browser_proxy="$(python3 ' in entrypoint
    assert 'browser_command+=("--proxy-server=$browser_proxy")' in entrypoint
    assert "browser_command=(proxychains4" not in entrypoint
    assert "for _ in {1..50}" in entrypoint
    assert 'kill -KILL "${pids[@]}"' in entrypoint
