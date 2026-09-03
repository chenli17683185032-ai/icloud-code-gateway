#!/usr/bin/env python3
"""Backfill local control-plane access keys to cloud edge.

Usage:
  PYTHONPATH=. .venv/bin/python scripts/sync-local-keys-to-edge.py
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _local_proxy_is_listening(proxy_url: str) -> bool:
    try:
        parsed = urlsplit(proxy_url)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if host not in {"127.0.0.1", "localhost", "::1"} or port is None:
        return False
    try:
        connection = socket.create_connection((host, port), timeout=0.5)
    except OSError:
        return False
    connection.close()
    return True


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    if str(project) not in sys.path:
        sys.path.insert(0, str(project))

    home = Path.home()
    runtime = home / ".icloud-code-gateway"
    data_dir = runtime / "data"
    # 凭据文件位置不写死在仓库里：优先 ICLOUD_GATEWAY_CREDENTIALS_FILE，否则按
    # 运行时目录 / 项目目录 / 项目上级目录依次查找。
    override = os.environ.get("ICLOUD_GATEWAY_CREDENTIALS_FILE", "").strip()
    creds_candidates = tuple(
        path
        for path in (
            Path(override).expanduser() if override else None,
            runtime / "icloud-control-plane.env",
            project / "icloud-control-plane.env",
            project.parent / "icloud-control-plane.env",
        )
        if path is not None
    )
    creds = next((path for path in creds_candidates if path.is_file()), creds_candidates[0])
    local_env = project / ".env"
    values = {}
    values.update(_load_env_file(local_env))
    values.update(_load_env_file(creds))

    def pick(*names: str, default: str = "") -> str:
        for name in names:
            if os.environ.get(name):
                return str(os.environ[name])
            if values.get(name):
                return values[name]
        return default

    master = pick("LOCAL_ICLOUD_GATEWAY_MASTER_KEY", "ICLOUD_GATEWAY_MASTER_KEY")
    control_token = pick("ICLOUD_GATEWAY_CONTROL_PLANE_TOKEN")
    admin_password = pick(
        "LOCAL_ICLOUD_GATEWAY_ADMIN_PASSWORD",
        "ICLOUD_GATEWAY_ADMIN_PASSWORD",
        default="local-open-no-password",
    )
    if not master or not control_token:
        print("缺少 MASTER_KEY 或 CONTROL_PLANE_TOKEN", file=sys.stderr)
        return 2

    os.environ["ICLOUD_GATEWAY_MASTER_KEY"] = master
    os.environ["ICLOUD_GATEWAY_ADMIN_PASSWORD"] = admin_password
    os.environ["ICLOUD_GATEWAY_ADMIN_OPEN"] = "1"
    os.environ["ICLOUD_GATEWAY_CONTROL_PLANE_TOKEN"] = control_token
    os.environ["ICLOUD_GATEWAY_DATA_DIR"] = str(data_dir)
    os.environ["ICLOUD_GATEWAY_BROWSER_PROFILE_DIR"] = str(runtime / "browser-profile")
    os.environ["ICLOUD_GATEWAY_DEPLOYMENT_MODE"] = "control"
    os.environ["ICLOUD_GATEWAY_EDGE_BASE_URL"] = pick(
        "ICLOUD_GATEWAY_EDGE_BASE_URL", default="https://icloud.yunbay.xyz"
    )
    os.environ["ICLOUD_GATEWAY_EDGE_SYNC_ENABLED"] = "1"
    os.environ["ICLOUD_GATEWAY_COOKIE_SECURE"] = "0"
    # Local direct TLS to Cloudflare can be reset. Prefer FlClash's current 7890
    # mixed port while retaining compatibility with the older 7897 setup.
    edge_proxy = pick("ICLOUD_GATEWAY_EDGE_PROXY_SERVER", "ICLOUD_GATEWAY_EDGE_PROXY")
    if not edge_proxy:
        edge_proxy = next(
            (
                candidate
                for candidate in (
                    "socks5h://127.0.0.1:7890",
                    "socks5h://127.0.0.1:7897",
                )
                if _local_proxy_is_listening(candidate)
            ),
            "socks5h://127.0.0.1:7890",
        )
    os.environ["ICLOUD_GATEWAY_EDGE_PROXY_SERVER"] = edge_proxy
    os.environ["ICLOUD_GATEWAY_EDGE_PROXY_REQUIRED"] = "0"

    from icloud_gateway.config import Settings
    from icloud_gateway.service import GatewayService

    settings = Settings.from_environment()
    service = GatewayService(settings, start_maintenance=False)
    print("data_dir:", settings.data_dir)
    print("edge:", settings.edge_base_url)
    print("edge_proxy:", settings.edge_proxy or "(none)")
    print("edge_sync_enabled:", settings.edge_sync_enabled)
    print("pushing local access keys ...")
    result = service.push_all_access_keys_to_edge()
    print("result:", result)
    return 0 if int(result.get("failed") or 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
