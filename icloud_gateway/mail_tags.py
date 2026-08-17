from __future__ import annotations

import email
import imaplib
import re
from collections.abc import Callable, Mapping
from email.header import decode_header, make_header
from email.message import Message
from email.policy import default
from typing import Any

from .database import _clean_usage_label
from .imap_otp import RECIPIENT_HEADERS, _create_imap_connection, _mailbox_argument

PLAN_SUBJECTS = (
    "chatgpt - your new plan",
    "welcome to chatgpt plus",
)
BAN_SUBJECTS = (
    "openai - access deactivated",
    "openai api - access deactivated",
)
HIDDEN_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@(?:icloud\.com|me\.com|privaterelay\.appleid\.com)",
    re.IGNORECASE,
)
_SEARCH_QUERIES = (
    ("SUBJECT", "ChatGPT - Your new plan"),
    ("SUBJECT", "Welcome to ChatGPT Plus"),
    ("SUBJECT", "OpenAI - Access Deactivated"),
    ("SUBJECT", "OpenAI API - Access Deactivated"),
)

ConnectionFactory = Callable[..., Any]


def merge_usage(existing: str, *, plan: bool, banned: bool) -> str:
    tokens = set(_clean_usage_label(existing).split()) if existing else set()
    tokens.discard("活跃")
    if plan:
        tokens.add("gpt")
    if banned:
        tokens.add("封号")
    elif plan:
        tokens.add("活跃")
    return _clean_usage_label(" ".join(sorted(tokens)))


def classify_message(message: Message) -> set[str]:
    subject = _decode_header(str(message.get("Subject") or "")).casefold()
    kinds: set[str] = set()
    if any(hint in subject for hint in PLAN_SUBJECTS):
        kinds.add("plan")
    if any(hint in subject for hint in BAN_SUBJECTS):
        kinds.add("ban")
    return kinds


def extract_hidden_emails(message: Message, known: set[str]) -> set[str]:
    values: list[str] = []
    for name in RECIPIENT_HEADERS:
        values.extend(str(value) for value in message.get_all(name, []))
    values.append(str(message.get("Subject") or ""))
    found: set[str] = set()
    for match in HIDDEN_EMAIL_RE.findall(" ".join(values)):
        address = match.strip().casefold()
        if address in known:
            found.add(address)
    return found


def scan_usage_hits(
    connection: imaplib.IMAP4,
    folders: tuple[str, ...] | list[str],
    known: set[str],
) -> tuple[dict[str, set[str]], int, int]:
    combined: dict[str, set[str]] = {}
    scanned = 0
    classified = 0
    for folder in folders:
        hits, folder_scanned, folder_classified = _scan_folder(connection, folder, known)
        scanned += folder_scanned
        classified += folder_classified
        for address, kinds in hits.items():
            combined.setdefault(address, set()).update(kinds)
    return combined, scanned, classified


def apply_usage_hits(
    aliases: list[Mapping[str, Any]],
    hits: Mapping[str, set[str]],
    updater: Callable[[str, str], Any],
) -> dict[str, int]:
    by_email = {str(item["email"]).casefold(): item for item in aliases}
    updated = 0
    plan_only = 0
    banned = 0
    for address, kinds in hits.items():
        alias = by_email.get(address)
        if alias is None:
            continue
        next_label = merge_usage(
            str(alias.get("usage_label") or ""),
            plan="plan" in kinds,
            banned="ban" in kinds,
        )
        if next_label == str(alias.get("usage_label") or ""):
            continue
        updater(str(alias["id"]), next_label)
        updated += 1
        if "ban" in kinds:
            banned += 1
        elif "plan" in kinds:
            plan_only += 1
    return {
        "matched": len(hits),
        "updated": updated,
        "gpt_active": plan_only,
        "gpt_banned": banned,
    }


def refresh_usage_tags(
    *,
    config: Any,
    aliases: list[Mapping[str, Any]],
    updater: Callable[[str, str], Any],
    connection_factory: ConnectionFactory | None = None,
    timeout: float = 60.0,
) -> dict[str, int]:
    known = {str(item["email"]).casefold() for item in aliases}
    factory = connection_factory or _create_imap_connection
    connection = factory(config, timeout)
    try:
        status, _ = connection.login(config.username, config.password)
        if str(status).upper() != "OK":
            raise RuntimeError("IMAP login failed")
        hits, scanned, classified = scan_usage_hits(connection, config.folders, known)
    finally:
        try:
            connection.logout()
        except Exception:
            pass
    stats = apply_usage_hits(aliases, hits, updater)
    stats["scanned"] = scanned
    stats["classified"] = classified
    return stats


def _decode_header(value: str) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _search_all(connection: imaplib.IMAP4, terms: list[str]) -> list[str]:
    status, data = connection.uid("SEARCH", None, *terms)
    if str(status).upper() != "OK":
        return []
    blob = data[0] if data else b""
    if not blob:
        return []
    return [token.decode("ascii") for token in blob.split() if token]


def _scan_folder(
    connection: imaplib.IMAP4,
    folder: str,
    known: set[str],
) -> tuple[dict[str, set[str]], int, int]:
    status, _ = connection.select(_mailbox_argument(folder), readonly=True)
    if str(status).upper() != "OK":
        raise RuntimeError(f"cannot select folder {folder}")
    uids: list[str] = []
    seen: set[str] = set()
    for key, value in _SEARCH_QUERIES:
        for uid in _search_all(connection, [key, value]):
            if uid not in seen:
                seen.add(uid)
                uids.append(uid)
    hits: dict[str, set[str]] = {}
    classified = 0
    scanned = 0
    batch = 40
    for start in range(0, len(uids), batch):
        chunk = uids[start : start + batch]
        status, data = connection.uid(
            "FETCH",
            ",".join(chunk),
            "(UID BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DELIVERED-TO X-ORIGINAL-TO ENVELOPE-TO RESENT-TO X-ENVELOPE-TO X-APPLE-FORWARD-TO X-APPLE-ORIGINAL-RECIPIENT)])",
        )
        if str(status).upper() != "OK" or not data:
            continue
        for item in data:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            raw = item[1]
            if not isinstance(raw, (bytes, bytearray)):
                continue
            scanned += 1
            message = email.message_from_bytes(bytes(raw), policy=default)
            kinds = classify_message(message)
            if not kinds:
                continue
            classified += 1
            for address in extract_hidden_emails(message, known):
                hits.setdefault(address, set()).update(kinds)
    return hits, scanned, classified


__all__ = [
    "apply_usage_hits",
    "classify_message",
    "extract_hidden_emails",
    "merge_usage",
    "refresh_usage_tags",
    "scan_usage_hits",
]
