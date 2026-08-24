#!/usr/bin/env python3
"""Backfill local control-plane access keys to cloud edge.

Usage:
  PYTHONPATH=. .venv/bin/python scripts/sync-local-keys-to-edge.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


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


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    if str(project) not in sys.path:
        sys.path.insert(0, str(project))

    home = Path.home()
    runtime = home / ".icloud-code-gateway"
    data_dir = runtime / "data"
    creds_candidates = (
        home / "Desktop" / "鲨鱼工具库" / "云贝平台" / "服务器相关" / "icloud-control-plane.env",
        project / "icloud-control-plane.env",
        project.parent / "icloud-control-plane.env",
        home / "Desktop" / "鲨鱼工具库" / "iCloud管理工具" / "icloud-control-plane.env",
        home / "Desktop" / "云贝" / "服务器相关" / "icloud-control-plane.env",
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
    # Critical: local direct TLS to Cloudflare often fails; sync via Clash.
    os.environ["ICLOUD_GATEWAY_HME_PROXY_SERVER"] = pick(
        "ICLOUD_GATEWAY_HME_PROXY_SERVER", default="socks5h://127.0.0.1:7897"
    )
    os.environ["ICLOUD_GATEWAY_HME_PROXY_REQUIRED"] = "0"

    from icloud_gateway.config import Settings
    from icloud_gateway.service import GatewayService

    settings = Settings.from_environment()
    service = GatewayService(settings, start_maintenance=False)
    print("data_dir:", settings.data_dir)
    print("edge:", settings.edge_base_url)
    print("proxy:", settings.hme_proxy or "(none)")
    print("edge_sync_enabled:", settings.edge_sync_enabled)
    print("pushing local access keys ...")
    result = service.push_all_access_keys_to_edge()
    print("result:", result)
    return 0 if int(result.get("failed") or 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
