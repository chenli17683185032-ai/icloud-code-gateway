from __future__ import annotations

from pathlib import Path

CLOUDFLARE_RANGE_COUNT = 22


def test_app_healthcheck_uses_public_host_header() -> None:
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()

    assert "ICLOUD_GATEWAY_PUBLIC_BASE_URL" in dockerfile
    assert "headers={'Host':host}" in dockerfile
    assert "http://127.0.0.1:8080/healthz" in dockerfile


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
    assert "legacy-browser" in overlay
    assert "ICLOUD_GATEWAY_CDP_URL: ${ICLOUD_GATEWAY_CDP_URL:-}" in overlay
    assert "BROWSER_PROXY_SERVER: ${BROWSER_PROXY_SERVER:-}" in overlay
    assert "BROWSER_PROXY_REQUIRED: ${BROWSER_PROXY_REQUIRED:-0}" in overlay
    assert (
        "ICLOUD_GATEWAY_HME_PROXY_SERVER: ${ICLOUD_GATEWAY_HME_PROXY_SERVER:-}"
        in overlay
    )
    assert (
        "ICLOUD_GATEWAY_HME_PROXY_REQUIRED: ${ICLOUD_GATEWAY_HME_PROXY_REQUIRED:-0}"
        in overlay
    )
    assert "condition: service_started" not in overlay
    # Edge still needs the China egress: IMAP inherits the HME proxy there.
    assert "cn-proxy" in overlay
    assert "forward_auth icloud-code-gateway-app:8080" not in caddy_site
    assert "reverse_proxy icloud-code-gateway-browser:6080" not in caddy_site
    assert "reverse_proxy icloud-code-gateway-app:8080" in caddy_site
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
        "ICLOUD_GATEWAY_ALIAS_BATCH_LIMIT": "100",
    }

    for name, default in expected.items():
        interpolation = f"{name}: ${{{name}:-{default}}}"
        assert interpolation in base
        assert interpolation in server


def test_cloudflare_routes_leave_control_admin_and_static_assets_on_vps() -> None:
    config = (
        Path(__file__).resolve().parents[1]
        / "cloudflare-mailbox"
        / "wrangler.jsonc"
    ).read_text()

    assert '"pattern": "icloud.yunbay.xyz/*"' not in config
    for pattern in (
        "icloud.yunbay.xyz/",
        "icloud.yunbay.xyz/app.js",
        "icloud.yunbay.xyz/app.css",
        "icloud.yunbay.xyz/favicon.svg",
        "icloud.yunbay.xyz/healthz",
        "icloud.yunbay.xyz/readyz",
        "icloud.yunbay.xyz/api/*",
        "icloud.yunbay.xyz/control/*",
        "icloud.yunbay.xyz/admin/mail*",
        "icloud.yunbay.xyz/admin/app.js",
    ):
        assert f'"pattern": "{pattern}"' in config
    assert '"pattern": "icloud.yunbay.xyz/admin*"' not in config
    assert '"pattern": "icloud.yunbay.xyz/static/*"' not in config


def test_app_does_not_depend_on_the_browser_so_the_edge_profile_can_drop_it() -> None:
    root = Path(__file__).resolve().parents[1]
    base = (root / "docker-compose.yml").read_text()

    app_block = base.split("\n  app:", 1)[1].split("\n  caddy:", 1)[0]
    # Compose merges depends_on across files rather than replacing it, so a
    # browser dependency declared in the base would still start Chromium on the
    # VPS despite the overlay's legacy-browser profile.
    assert "    depends_on:" not in app_block.splitlines()


def test_local_control_only_auto_enables_a_live_proxy() -> None:
    launcher = (
        Path(__file__).resolve().parents[1] / "scripts" / "run-local-control.sh"
    ).read_text()

    assert '"socks5h://127.0.0.1:7890"' in launcher
    assert '"socks5h://127.0.0.1:7897"' in launcher
    assert 'LIVE_LOCAL_PROXY="$(find_live_local_proxy || true)"' in launcher
    assert 'export ICLOUD_GATEWAY_HME_PROXY_SERVER="$LIVE_LOCAL_PROXY"' in launcher
    assert 'export ICLOUD_GATEWAY_EDGE_PROXY_SERVER="$LIVE_LOCAL_PROXY"' in launcher
    assert 'export ICLOUD_GATEWAY_EDGE_PROXY_SERVER="$EDGE_PROXY_DEFAULT"' in launcher
    assert (
        'ICLOUD_GATEWAY_HME_PROXY_SERVER="${ICLOUD_GATEWAY_HME_PROXY_SERVER:-$EDGE_PROXY_DEFAULT}"'
        not in launcher
    )
    assert 'read_env ICLOUD_GATEWAY_EDGE_BASE_URL "$CREDS_FILE"' in launcher
    assert 'read_env ICLOUD_GATEWAY_PUBLIC_BASE_URL "$CREDS_FILE"' in launcher
    assert 'export ICLOUD_GATEWAY_PUBLIC_BASE_URL="$PUBLIC_URL"' in launcher
    assert 'read_env ICLOUD_GATEWAY_IMAP_ENABLED "$CREDS_FILE"' in launcher
    assert "Cloudflare Email Worker 接管" in launcher


def test_manual_edge_sync_prefers_current_flclash_port() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "sync-local-keys-to-edge.py"
    ).read_text()

    assert '"socks5h://127.0.0.1:7890"' in script
    assert '"socks5h://127.0.0.1:7897"' in script
    assert 'os.environ["ICLOUD_GATEWAY_EDGE_PROXY_SERVER"] = edge_proxy' in script
    assert 'os.environ["ICLOUD_GATEWAY_HME_PROXY_SERVER"]' not in script


def test_edge_browser_cleanup_is_scoped_and_preserves_recovery_assets() -> None:
    cleanup = (
        Path(__file__).resolve().parents[1] / "scripts" / "remove-edge-browser.sh"
    ).read_text()

    assert '"$deployment_mode" != "edge"' in cleanup
    assert "com.docker.compose.project" in cleanup
    assert "com.docker.compose.service" in cleanup
    assert "--profile legacy-browser stop -t 10 browser" in cleanup
    assert "--profile legacy-browser rm -f browser" in cleanup
    assert "docker volume rm" not in cleanup
    assert "docker image rm" not in cleanup


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
