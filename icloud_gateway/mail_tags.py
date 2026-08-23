from __future__ import annotations

import imaplib
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from email.header import decode_header, make_header
from email.message import Message
from typing import Any

from .database import _clean_usage_label
from .imap_otp import (
    RECIPIENT_HEADERS,
    _create_imap_connection,
    _is_gpt_sender,
    _mailbox_argument,
    _message_body,
    _messages_from_fetch,
)

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
# A server-side SUBJECT match only works if the server decodes MIME-encoded
# headers, which is not guaranteed and silently drops mail when it does not.
# Sweeping the sender finds the same messages either way; classification still
# happens locally against the decoded subject.
_SENDER_QUERIES = (
    ("FROM", "openai.com"),
)
_HEADER_FIELDS = (
    "(UID BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DELIVERED-TO X-ORIGINAL-TO"
    " ENVELOPE-TO RESENT-TO X-ENVELOPE-TO X-APPLE-FORWARD-TO"
    " X-APPLE-ORIGINAL-RECIPIENT)])"
)
_HEADER_BATCH = 60
_BODY_BATCH = 20
# Every OpenAI mail now carries a signal, so the body pass can no longer be
# assumed small. Bound it per folder and report the remainder rather than
# letting one click sit on the mailbox for minutes.
_BODY_FETCH_LIMIT = 400

ConnectionFactory = Callable[..., Any]


def merge_usage(existing: str, *, plan: bool, banned: bool, used: bool = False) -> str:
    tokens = set(_clean_usage_label(existing).split()) if existing else set()
    # The three account states are mutually exclusive, so clear the two that
    # can be superseded before deciding which one applies now.
    tokens.discard("活跃")
    tokens.discard("已使用")
    if plan or banned or used:
        tokens.add("gpt")
    if banned:
        tokens.add("封号")
    elif plan:
        tokens.add("活跃")
    elif used:
        tokens.add("已使用")
    return _clean_usage_label(" ".join(sorted(tokens)))


def classify_message(message: Message) -> set[str]:
    subject = _decode_header(str(message.get("Subject") or "")).casefold()
    kinds: set[str] = set()
    from_header = " ".join(str(value) for value in message.get_all("From", []))
    if _is_gpt_sender(from_header):
        # Any mail from OpenAI means the alias was used to register, even if
        # the account never subscribed. Signup and verification-code mail is
        # the permanent record of that; the audit log only keeps seven days,
        # and matching on subjects alone misses every state we did not list.
        kinds.add("used")
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
    # An iCloud forward often carries the alias only in the body, so a
    # header-only scan silently skipped those accounts. Empty for the
    # header-only fetch pass, which keeps that pass cheap.
    body = _message_body(message)
    if body:
        values.append(body)
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
) -> tuple[dict[str, set[str]], int, int, int]:
    combined: dict[str, set[str]] = {}
    scanned = 0
    classified = 0
    deferred = 0
    for folder in folders:
        hits, folder_scanned, folder_classified, folder_deferred = _scan_folder(
            connection, folder, known
        )
        scanned += folder_scanned
        classified += folder_classified
        deferred += folder_deferred
        for address, kinds in hits.items():
            combined.setdefault(address, set()).update(kinds)
    return combined, scanned, classified, deferred


def apply_usage_hits(
    aliases: list[Mapping[str, Any]],
    hits: Mapping[str, set[str]],
    updater: Callable[[str, str], Any],
) -> dict[str, int]:
    by_email = {str(item["email"]).casefold(): item for item in aliases}
    updated = 0
    plan_only = 0
    banned = 0
    used_only = 0
    for address, kinds in hits.items():
        alias = by_email.get(address)
        if alias is None:
            continue
        next_label = merge_usage(
            str(alias.get("usage_label") or ""),
            plan="plan" in kinds,
            banned="ban" in kinds,
            used="used" in kinds,
        )
        if next_label == str(alias.get("usage_label") or ""):
            continue
        updater(str(alias["id"]), next_label)
        updated += 1
        if "ban" in kinds:
            banned += 1
        elif "plan" in kinds:
            plan_only += 1
        elif "used" in kinds:
            used_only += 1
    return {
        "matched": len(hits),
        "updated": updated,
        "gpt_active": plan_only,
        "gpt_banned": banned,
        "gpt_used": used_only,
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
        # scan_folders, not folders: QQ files a lot of this mail into Junk.
        hits, scanned, classified, deferred = scan_usage_hits(
            connection, config.scan_folders, known
        )
    finally:
        with suppress(Exception):
            connection.logout()
    stats = apply_usage_hits(aliases, hits, updater)
    stats["scanned"] = scanned
    stats["classified"] = classified
    stats["deferred"] = deferred
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


def _fetch_messages(
    connection: imaplib.IMAP4,
    uids: list[str],
    spec: str,
) -> dict[str, Message]:
    if not uids:
        return {}
    try:
        status, data = connection.uid("FETCH", ",".join(uids), spec)
    except Exception:
        return {}
    if str(status).upper() != "OK" or not data:
        return {}
    parsed = _messages_from_fetch(data, fallback_uid=uids[0] if len(uids) == 1 else "")
    return {uid: message for uid, (message, _internal) in parsed.items()}


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
    for key, value in (*_SEARCH_QUERIES, *_SENDER_QUERIES):
        for uid in _search_all(connection, [key, value]):
            if uid not in seen:
                seen.add(uid)
                uids.append(uid)

    hits: dict[str, set[str]] = {}
    classified = 0
    scanned = 0
    # Messages that classify but whose alias was not in any header. Their body
    # is fetched in a second pass so the cheap header sweep stays cheap.
    needs_body: dict[str, set[str]] = {}

    for start in range(0, len(uids), _HEADER_BATCH):
        chunk = uids[start : start + _HEADER_BATCH]
        for uid, message in _fetch_messages(connection, chunk, _HEADER_FIELDS).items():
            scanned += 1
            kinds = classify_message(message)
            if not kinds:
                continue
            classified += 1
            found = extract_hidden_emails(message, known)
            if found:
                for address in found:
                    hits.setdefault(address, set()).update(kinds)
            else:
                needs_body[uid] = kinds

    # Newest first: a truncated pass should keep the most recent accounts.
    pending = sorted(needs_body, key=lambda uid: int(uid) if uid.isdigit() else 0, reverse=True)
    deferred = max(0, len(pending) - _BODY_FETCH_LIMIT)
    pending = pending[:_BODY_FETCH_LIMIT]
    for start in range(0, len(pending), _BODY_BATCH):
        chunk = pending[start : start + _BODY_BATCH]
        for uid, message in _fetch_messages(connection, chunk, "(UID BODY.PEEK[])").items():
            kinds = needs_body.get(uid)
            if not kinds:
                continue
            for address in extract_hidden_emails(message, known):
                hits.setdefault(address, set()).update(kinds)

    return hits, scanned, classified, deferred


__all__ = [
    "apply_usage_hits",
    "classify_message",
    "extract_hidden_emails",
    "merge_usage",
    "refresh_usage_tags",
    "scan_usage_hits",
]
