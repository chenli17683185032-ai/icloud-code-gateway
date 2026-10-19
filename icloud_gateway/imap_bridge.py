"""Push Apple IMAP mail into the existing mailbox API.

The public site and verification-code API stay on the mailbox worker.
This loop only replaces Cloudflare Email Routing as the ingest path.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable

import requests

from .config import Settings
from .imap_otp import (
    ImapConfig,
    ImapError,
    _create_imap_connection,
    _logout,
    _mailbox_argument,
    _uid_sort_key,
    _uids_from_search,
)

LOGGER = logging.getLogger("icloud_gateway.imap_bridge")
_FROM_RE = re.compile(r"^from:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_POLL_SECONDS = 5.0
_SEED_DAYS = 2
_CYCLE_LIMIT = 20
_MAX_MESSAGE_BYTES = 8_000_000


class ImapMailboxBridge:
    def __init__(
        self,
        settings: Settings,
        config_provider: Callable[[], ImapConfig | None],
        stop_event: threading.Event,
        *,
        session: requests.Session | None = None,
        connection_factory: Callable[[ImapConfig, float], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.config_provider = config_provider
        self.stop_event = stop_event
        self.connection_factory = connection_factory or _create_imap_connection
        self.clock = clock
        self.session = session or requests.Session()
        self.session.trust_env = False
        proxy = str(settings.edge_proxy or "").strip()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        else:
            self.session.proxies.clear()
        self.cursor_path = Path(settings.data_dir) / "imap-bridge-cursors.json"

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                LOGGER.warning("imap bridge cycle failed", exc_info=True)
            self.stop_event.wait(_POLL_SECONDS)

    def run_once(self) -> dict[str, int]:
        config = self.config_provider()
        if config is None:
            return {"uploaded": 0, "skipped": 0}
        connection = self.connection_factory(config, 20.0)
        uploaded = 0
        skipped = 0
        try:
            status, _ = connection.login(config.username, config.password)
            if str(status).upper() != "OK":
                raise ImapError("IMAP rejected login")
            cursors = self._read_cursors()
            for folder in config.scan_folders:
                pushed, ignored = self._sync_folder(connection, folder, cursors)
                uploaded += pushed
                skipped += ignored
            self._write_cursors(cursors)
        finally:
            _logout(connection)
        if uploaded or skipped:
            LOGGER.info("imap bridge uploaded=%s skipped=%s", uploaded, skipped)
        return {"uploaded": uploaded, "skipped": skipped}

    def _sync_folder(
        self,
        connection: Any,
        folder: str,
        cursors: dict[str, int],
    ) -> tuple[int, int]:
        status, _ = connection.select(_mailbox_argument(folder), readonly=True)
        if str(status).upper() != "OK":
            return 0, 0
        cursor = int(cursors.get(folder) or 0)
        if cursor <= 0:
            since = time.strftime(
                "%d-%b-%Y",
                time.gmtime(self.clock() - _SEED_DAYS * 86400),
            )
            status, data = connection.uid("search", None, "SINCE", since)
        else:
            status, data = connection.uid("search", None, "UID", f"{cursor + 1}:*")
        if status is None or str(status).upper() != "OK":
            return 0, 0
        uids = sorted(_uids_from_search(data), key=_uid_sort_key)
        uids = [uid for uid in uids if uid.isdigit() and int(uid) > cursor]
        if not uids:
            return 0, 0
        if cursor <= 0 and len(uids) > _CYCLE_LIMIT:
            uids = uids[-_CYCLE_LIMIT:]
        uploaded = 0
        skipped = 0
        highest = cursor
        for uid in uids[:_CYCLE_LIMIT]:
            raw = self._fetch_raw(connection, uid)
            if raw is None:
                break
            if len(raw) > _MAX_MESSAGE_BYTES:
                skipped += 1
                highest = int(uid)
                continue
            if not self._upload(raw):
                break
            uploaded += 1
            highest = int(uid)
        if highest > cursor:
            cursors[folder] = highest
        return uploaded, skipped

    def _fetch_raw(self, connection: Any, uid: str) -> bytes | None:
        try:
            status, data = connection.uid("fetch", uid, "(BODY.PEEK[])")
        except Exception:
            return None
        if str(status).upper() != "OK":
            return None
        return _raw_from_fetch(data)

    def _upload(self, raw: bytes) -> bool:
        base = str(self.settings.edge_base_url or "").rstrip("/")
        token = str(self.settings.control_plane_token or "").strip()
        if not base or not token:
            return False
        try:
            response = self.session.post(
                f"{base}/control/v1/messages",
                data=raw,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "message/rfc822",
                    "Accept": "application/json",
                    "X-Envelope-From": _envelope_from(raw),
                    "User-Agent": "icloud-code-gateway-imap-bridge/1.0",
                },
                timeout=max(3, int(self.settings.edge_timeout_seconds)),
            )
        except requests.RequestException:
            return False
        return response.status_code == 200

    def _read_cursors(self) -> dict[str, int]:
        try:
            payload = json.loads(self.cursor_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        cursors: dict[str, int] = {}
        for key, value in payload.items():
            if isinstance(key, str) and isinstance(value, int) and value >= 0:
                cursors[key] = value
        return cursors

    def _write_cursors(self, cursors: dict[str, int]) -> None:
        temporary = self.cursor_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(cursors, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.cursor_path)


def _raw_from_fetch(data: Any) -> bytes | None:
    if not isinstance(data, (list, tuple)):
        return None
    for item in data:
        if not isinstance(item, tuple):
            continue
        for part in item[1:]:
            if isinstance(part, bytes) and b"\n" in part:
                return part
    return None


def _envelope_from(raw: bytes) -> str:
    head = raw.split(b"\r\n\r\n", 1)[0].replace(b"\n", b"\r\n")
    text = head.decode("utf-8", "replace")
    unfolded = re.sub(r"\r\n[ \t]+", " ", text)
    match = _FROM_RE.search(unfolded)
    if not match:
        return "imap-bridge@icloud.com"
    _name, address = parseaddr(match.group(1))
    address = address.strip().lower()
    if address.count("@") == 1 and not any(character.isspace() for character in address):
        return address
    return "imap-bridge@icloud.com"
