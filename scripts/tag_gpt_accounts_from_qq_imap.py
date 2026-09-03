#!/usr/bin/env python3
"""Scan QQ IMAP for GPT plan / ban mail and persist usage tags.

Read-only IMAP: BODY.PEEK[] so messages stay unseen.
Writes only local usage_label; never prints secrets, bodies, or keys.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from icloud_gateway.config import decode_master_key
from icloud_gateway.database import Database
from icloud_gateway.imap_otp import ImapConfig
from icloud_gateway.mail_tags import refresh_usage_tags
from icloud_gateway.security import SecretBox


def _load_env(path: Path) -> dict[str, str]:
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


def _imap_config(env: dict[str, str]) -> ImapConfig:
    return ImapConfig(
        forwarding_email=env.get("ICLOUD_GATEWAY_IMAP_FORWARDING_EMAIL")
        or env.get("ICLOUD_GATEWAY_IMAP_USERNAME")
        or "",
        host=env.get("ICLOUD_GATEWAY_IMAP_HOST") or "imap.qq.com",
        port=int(env.get("ICLOUD_GATEWAY_IMAP_PORT") or 993),
        username=env.get("ICLOUD_GATEWAY_IMAP_USERNAME") or "",
        password=env.get("ICLOUD_GATEWAY_IMAP_PASSWORD") or "",
        folder=env.get("ICLOUD_GATEWAY_IMAP_FOLDER") or "INBOX",
        junk_folder=env.get("ICLOUD_GATEWAY_IMAP_JUNK_FOLDER") or "",
        proxy=env.get("ICLOUD_GATEWAY_IMAP_PROXY") or "",
    )


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    if str(project) not in sys.path:
        sys.path.insert(0, str(project))

    runtime = Path.home() / ".icloud-code-gateway"
    data_dir = runtime / "data"
    env: dict[str, str] = {}
    env.update(_load_env(project / ".env"))
    env.update(_load_env(project / "icloud-control-plane.env"))
    env.update(_load_env(runtime / "icloud-control-plane.env"))
    override = os.environ.get("ICLOUD_GATEWAY_CREDENTIALS_FILE", "").strip()
    if override:
        env.update(_load_env(Path(override).expanduser()))
    master = (
        env.get("ICLOUD_GATEWAY_MASTER_KEY")
        or env.get("LOCAL_ICLOUD_GATEWAY_MASTER_KEY")
        or os.environ.get("ICLOUD_GATEWAY_MASTER_KEY")
        or ""
    )
    if not master:
        print("missing master key", file=sys.stderr)
        return 2
    if not (data_dir / "gateway.sqlite3").is_file():
        print("live database missing", file=sys.stderr)
        return 2

    database = Database(data_dir / "gateway.sqlite3", SecretBox(decode_master_key(master)))
    database.initialize()
    aliases = database.list_aliases()
    config = _imap_config(env)
    config.validate()
    stats = refresh_usage_tags(
        config=config,
        aliases=aliases,
        updater=database.update_alias_usage,
    )
    print(
        "scanned_headers={scanned} classified={classified} matched_aliases={matched} "
        "updated={updated} gpt_active={gpt_active} gpt_banned={gpt_banned}".format(**stats)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
