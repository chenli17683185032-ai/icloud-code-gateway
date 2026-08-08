from __future__ import annotations

import base64
import email
import html
import imaplib
import re
import ssl
import time
import urllib.parse
from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from email.policy import default
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

import socks

RECIPIENT_HEADERS = (
    "To",
    "Delivered-To",
    "X-Original-To",
    "Envelope-To",
    "Resent-To",
    "X-Envelope-To",
    "X-Apple-Forward-To",
    "X-Apple-Original-Recipient",
)
_OTP_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
# Grok / xAI commonly emails alphanumeric codes like A1B-2C3.
_GROK_OTP_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z0-9]{3}-[A-Za-z0-9]{3})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_GROK_SENDER_RE = re.compile(
    r"(?:^|@|\.)(?:x\.ai|xai\.com|grok\.com|xai)\b|\bgrok\b",
    re.IGNORECASE,
)
_CODE_CONTEXT_RE = re.compile(
    r"\b(?:verification|verify|one[- ]time(?:[- ](?:password|code))?|otp|code|"
    r"passcode|security[- ]code|confirmation(?:[- ]code)?|"
    r"authentication[- ]code|auth[- ]code|pin|login[- ]code|sign[- ]?in[- ]code|"
    r"sign[- ]?up[- ]code|access[- ]code|grok|xai|x\.ai)\b|"
    r"\u9a8c\u8bc1\u7801|\u6821\u9a8c\u7801|\u52a8\u6001\u7801|"
    r"\u5b89\u5168(?:\u7801|\u4ee3\u7801)|\u8ba4\u8bc1\u7801|"
    r"\u786e\u8ba4\u7801|\u4e34\u65f6(?:\u7801|\u4ee3\u7801)|"
    r"\u4e00\u6b21\u6027(?:\u5bc6\u7801|\u4ee3\u7801|\u9a8c\u8bc1\u7801)?",
    re.IGNORECASE,
)
_CODE_CONTEXT_WINDOW = 80
_GROK_CONTEXT_WINDOW = 160
_INTERNALDATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"', re.IGNORECASE)
_UID_RE = re.compile(rb"\bUID (\d+)", re.IGNORECASE)
_FETCH_SPECS = ("(UID INTERNALDATE BODY.PEEK[])", "(UID INTERNALDATE RFC822)")
# One FETCH per candidate turns a 120-message scan into 120 round trips, which
# alone can exceed the public lookup budget on a remote mailbox.
_FETCH_BATCH_SIZE = 25
# SEARCH SINCE only has day granularity. Where the server supports RFC 5032
# WITHIN, YOUNGER narrows the candidate set to the OTP window plus a wide
# allowance for server clock skew, rather than everything since midnight.
_YOUNGER_SKEW_ALLOWANCE_SECONDS = 900


class ImapError(RuntimeError):
    pass


class ImapCredentialsError(ImapError):
    pass


@dataclass(frozen=True)
class ImapConfig:
    forwarding_email: str
    host: str
    port: int
    username: str
    password: str
    folder: str = "INBOX"
    junk_folder: str = ""
    proxy: str = ""

    def validate(self) -> None:
        for name, value in (
            ("forwarding_email", self.forwarding_email),
            ("host", self.host),
            ("username", self.username),
            ("password", self.password),
            ("folder", self.folder),
        ):
            if not value or any(character in value for character in "\r\n\x00"):
                raise ValueError(f"{name} is invalid")
        if any(character in self.junk_folder for character in "\r\n\x00"):
            raise ValueError("junk_folder is invalid")
        if self.forwarding_email.count("@") != 1:
            raise ValueError("forwarding_email is invalid")
        if not 1 <= int(self.port) <= 65535:
            raise ValueError("IMAP port is invalid")
        if any(character.isspace() for character in self.host):
            raise ValueError("IMAP host is invalid")
        if self.proxy:
            _proxy_spec(self.proxy)

    def as_secret_dict(self) -> dict[str, Any]:
        return {
            "forwarding_email": self.forwarding_email,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "password": self.password,
            "folder": self.folder,
            "junk_folder": self.junk_folder,
            "proxy": self.proxy,
        }

    @property
    def folders(self) -> tuple[str, ...]:
        if self.junk_folder and self.junk_folder != self.folder:
            return (self.folder, self.junk_folder)
        return (self.folder,)

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> ImapConfig:
        try:
            config = cls(
                forwarding_email=str(value["forwarding_email"]).strip().casefold(),
                host=str(value["host"]).strip(),
                port=int(value.get("port") or 993),
                username=str(value["username"]).strip(),
                password=str(value["password"]),
                folder=str(value.get("folder") or "INBOX").strip(),
                junk_folder=str(value.get("junk_folder") or "").strip(),
                proxy=str(value.get("proxy") or "").strip(),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ImapCredentialsError("IMAP configuration is incomplete") from exc
        try:
            config.validate()
        except ValueError as exc:
            raise ImapCredentialsError("IMAP configuration is invalid") from exc
        return config


@dataclass(frozen=True)
class OtpResult:
    code: str
    uid: str
    received_at: datetime


@dataclass(frozen=True)
class RecentOtpResult:
    alias: str
    code: str
    uid: str
    received_at: datetime


@dataclass(frozen=True)
class RecentOtpBatch:
    items: tuple[RecentOtpResult, ...]
    scanned: int
    truncated: bool


ConnectionFactory = Callable[[ImapConfig, float], Any]


class ImapOtpReader:
    def __init__(
        self,
        config: ImapConfig,
        *,
        connection_factory: ConnectionFactory | None = None,
        scan_limit: int = 120,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        config.validate()
        self.config = config
        self.connection_factory = connection_factory or _create_imap_connection
        self.scan_limit = max(1, min(int(scan_limit), 500))
        self.monotonic = monotonic

    def check(self, *, timeout: float = 15.0) -> None:
        bounded_timeout = max(1.0, min(float(timeout), 30.0))
        deadline = self.monotonic() + bounded_timeout
        connection = self._login(bounded_timeout)
        try:
            for folder in self.config.folders:
                self._select_folder(connection, folder, deadline)
        finally:
            _logout(connection)

    def find_latest_code(
        self,
        alias: str,
        *,
        now_ts: float,
        max_age_seconds: int = 300,
        future_skew_seconds: int = 60,
        sender_filter: str = "",
        timeout: float = 20.0,
    ) -> OtpResult | None:
        target = _normalize_email(alias)
        oldest = float(now_ts) - max(1, int(max_age_seconds))
        newest = float(now_ts) + max(0, int(future_skew_seconds))
        bounded_timeout = max(1.0, min(float(timeout), 30.0))
        deadline = self.monotonic() + bounded_timeout
        connection = self._login(bounded_timeout)
        candidates: list[OtpResult] = []
        last_folder_error: ImapError | None = None
        try:
            folder_uids: list[tuple[str, list[str]]] = []
            for folder in self.config.folders:
                try:
                    self._select_folder(connection, folder, deadline)
                    uids = self._candidate_uids(
                        connection,
                        target,
                        oldest,
                        float(now_ts),
                        deadline,
                    )
                except ImapError as exc:
                    last_folder_error = exc
                    continue
                folder_uids.append((folder, uids))
            if not folder_uids:
                raise last_folder_error or ImapError("IMAP folder is unavailable")

            selected_folders, _truncated = _allocate_folder_uids(
                folder_uids,
                self.scan_limit,
            )
            completed_folders = 0
            for folder, uids in selected_folders:
                if not uids:
                    completed_folders += 1
                    continue
                try:
                    self._select_folder(connection, folder, deadline)
                    folder_candidates = self._latest_results_from_uids(
                        connection,
                        uids=uids,
                        target=target,
                        oldest=oldest,
                        newest=newest,
                        sender_filter=sender_filter,
                        deadline=deadline,
                    )
                except ImapError as exc:
                    last_folder_error = exc
                    continue
                candidates.extend(folder_candidates)
                completed_folders += 1
            if completed_folders == 0:
                raise last_folder_error or ImapError("IMAP folder is unavailable")
            if not candidates:
                return None
            return max(candidates, key=lambda item: (item.received_at, _uid_sort_key(item.uid)))
        finally:
            _logout(connection)

    def find_recent_codes(
        self,
        aliases: Sequence[str],
        *,
        now_ts: float,
        max_age_seconds: int = 300,
        future_skew_seconds: int = 60,
        timeout: float = 20.0,
        scan_limit: int = 500,
        result_limit: int = 500,
    ) -> RecentOtpBatch:
        targets = tuple(dict.fromkeys(_normalize_email(alias) for alias in aliases))
        if not targets:
            return RecentOtpBatch(items=(), scanned=0, truncated=False)
        oldest = float(now_ts) - max(1, int(max_age_seconds))
        newest = float(now_ts) + max(0, int(future_skew_seconds))
        bounded_timeout = max(1.0, min(float(timeout), 30.0))
        bounded_scan = max(1, min(int(scan_limit), 500))
        bounded_results = max(1, min(int(result_limit), 500))
        deadline = self.monotonic() + bounded_timeout
        connection = self._login(bounded_timeout)
        candidates: list[RecentOtpResult] = []
        last_folder_error: ImapError | None = None
        try:
            folder_uids: list[tuple[str, list[str]]] = []
            for folder in self.config.folders:
                try:
                    self._select_folder(connection, folder, deadline)
                    window = list(self._window_terms(connection, oldest, float(now_ts) - oldest))
                    self._set_operation_timeout(connection, deadline)
                    status, data = self._search(connection, window)
                    if status is None or str(status).upper() != "OK":
                        raise ImapError("IMAP search failed")
                    uids = sorted(
                        _uids_from_search(data),
                        key=_uid_sort_key,
                        reverse=True,
                    )
                except ImapError as exc:
                    last_folder_error = exc
                    continue
                folder_uids.append((folder, uids))
            if not folder_uids:
                raise last_folder_error or ImapError("IMAP folder is unavailable")

            selected_folders, truncated = _allocate_folder_uids(folder_uids, bounded_scan)
            scanned = 0
            completed_folders = 0
            for folder, uids in selected_folders:
                if not uids:
                    completed_folders += 1
                    continue
                folder_candidates: list[RecentOtpResult] = []
                try:
                    self._select_folder(connection, folder, deadline)
                    for uid, message, internal_timestamp in self._fetch_messages(
                        connection,
                        uids,
                        deadline,
                    ):
                        timestamp = internal_timestamp
                        if timestamp is None:
                            timestamp = _message_timestamp(message)
                        if timestamp is None or timestamp < oldest or timestamp > newest:
                            continue
                        code = _extract_message_code(message)
                        if not code:
                            continue
                        received_at = datetime.fromtimestamp(timestamp, tz=UTC)
                        for alias in targets:
                            if _message_matches_alias(message, alias):
                                folder_candidates.append(
                                    RecentOtpResult(
                                        alias=alias,
                                        code=code,
                                        uid=uid,
                                        received_at=received_at,
                                    )
                                )
                except ImapError as exc:
                    last_folder_error = exc
                    continue
                candidates.extend(folder_candidates)
                scanned += len(uids)
                completed_folders += 1
            if completed_folders == 0:
                raise last_folder_error or ImapError("IMAP folder is unavailable")

            candidates.sort(
                key=lambda item: (item.received_at, _uid_sort_key(item.uid)),
                reverse=True,
            )
            if len(candidates) > bounded_results:
                truncated = True
                candidates = candidates[:bounded_results]
            return RecentOtpBatch(
                items=tuple(candidates),
                scanned=scanned,
                truncated=truncated,
            )
        finally:
            _logout(connection)

    def _latest_results_from_uids(
        self,
        connection: Any,
        *,
        uids: Sequence[str],
        target: str,
        oldest: float,
        newest: float,
        sender_filter: str,
        deadline: float,
    ) -> list[OtpResult]:
        candidates: list[OtpResult] = []
        for uid, message, internal_timestamp in self._fetch_messages(
            connection,
            uids,
            deadline,
        ):
            if not _message_matches_alias(message, target):
                continue
            if sender_filter and not _message_matches_sender(message, sender_filter):
                continue
            timestamp = internal_timestamp
            if timestamp is None:
                timestamp = _message_timestamp(message)
            if timestamp is None or timestamp < oldest or timestamp > newest:
                continue
            code = _extract_message_code(message)
            if not code:
                continue
            candidates.append(
                OtpResult(
                    code=code,
                    uid=uid,
                    received_at=datetime.fromtimestamp(timestamp, tz=UTC),
                )
            )
        return candidates

    def _select_folder(self, connection: Any, folder: str, deadline: float) -> None:
        self._set_operation_timeout(connection, deadline)
        try:
            status, _ = connection.select(_mailbox_argument(folder), readonly=True)
        except Exception as exc:
            raise ImapError("IMAP folder is unavailable") from exc
        if str(status).upper() != "OK":
            raise ImapError("IMAP folder is unavailable")

    def _login(self, timeout: float) -> Any:
        try:
            connection = self.connection_factory(self.config, max(1.0, min(float(timeout), 30.0)))
        except (OSError, TimeoutError, socks.ProxyError) as exc:
            raise ImapError("IMAP connection failed") from exc
        try:
            status, _ = connection.login(self.config.username, self.config.password)
        except imaplib.IMAP4.error as exc:
            _logout(connection)
            raise ImapCredentialsError("IMAP rejected login") from exc
        except Exception as exc:
            _logout(connection)
            raise ImapError("IMAP login failed") from exc
        if str(status).upper() != "OK":
            _logout(connection)
            raise ImapCredentialsError("IMAP rejected login")
        return connection

    def _candidate_uids(
        self,
        connection: Any,
        alias: str,
        oldest: float,
        now_ts: float,
        deadline: float,
    ) -> list[str]:
        matched: set[str] = set()
        window = list(self._window_terms(connection, oldest, now_ts - oldest))
        combined = _recipient_search_terms(alias)
        self._set_operation_timeout(connection, deadline)
        status, data = self._search(connection, window + combined)
        searched = status is not None and str(status).upper() == "OK"
        if searched:
            matched.update(_uids_from_search(data))
        else:
            # Not every server accepts a deeply nested OR; fall back to one
            # SEARCH per recipient header.
            for header in RECIPIENT_HEADERS:
                self._set_operation_timeout(connection, deadline)
                status, data = self._search(connection, [*window, "HEADER", header, alias])
                if status is None or str(status).upper() != "OK":
                    continue
                searched = True
                matched.update(_uids_from_search(data))
        if not matched:
            self._set_operation_timeout(connection, deadline)
            status, data = self._search(connection, window)
            if status is None:
                if searched:
                    return []
                raise ImapError("IMAP search failed")
            if str(status).upper() == "OK":
                matched.update(_uids_from_search(data))
        return sorted(matched, key=_uid_sort_key, reverse=True)

    @staticmethod
    def _window_terms(connection: Any, oldest: float, window_seconds: float) -> tuple[str, ...]:
        since_date = datetime.fromtimestamp(oldest, tz=UTC).strftime("%d-%b-%Y")
        terms = ("SINCE", since_date)
        if not _supports_within(connection):
            return terms
        age = int(max(0.0, float(window_seconds))) + _YOUNGER_SKEW_ALLOWANCE_SECONDS
        return (*terms, "YOUNGER", str(age))

    @staticmethod
    def _search(connection: Any, terms: Sequence[str]) -> tuple[Any, Any]:
        try:
            return connection.uid("search", None, *terms)
        except Exception:
            return None, []

    def _fetch_messages(
        self,
        connection: Any,
        uids: Sequence[str],
        deadline: float,
    ) -> Iterator[tuple[str, Message, float | None]]:
        for start in range(0, len(uids), _FETCH_BATCH_SIZE):
            batch = list(uids[start : start + _FETCH_BATCH_SIZE])
            fetched = self._fetch_batch(connection, batch, deadline)
            if fetched is None and len(batch) > 1:
                # A server that rejects UID sets still answers one UID at a time.
                fetched = {}
                for uid in batch:
                    single = self._fetch_batch(connection, [uid], deadline)
                    if single:
                        fetched.update(single)
            for uid in batch:
                item = (fetched or {}).get(uid)
                if item is not None:
                    yield (uid, item[0], item[1])

    def _fetch_batch(
        self,
        connection: Any,
        uids: Sequence[str],
        deadline: float,
    ) -> dict[str, tuple[Message, float | None]] | None:
        if not uids:
            return {}
        uid_set = ",".join(uids)
        for fetch_spec in _FETCH_SPECS:
            self._set_operation_timeout(connection, deadline)
            try:
                status, data = connection.uid("fetch", uid_set, fetch_spec)
            except Exception:
                continue
            if str(status).upper() != "OK":
                continue
            parsed = _messages_from_fetch(data, fallback_uid=uids[0] if len(uids) == 1 else "")
            if parsed:
                return parsed
        return None

    def _set_operation_timeout(self, connection: Any, deadline: float) -> None:
        remaining = float(deadline) - float(self.monotonic())
        if remaining <= 0:
            raise ImapError("IMAP lookup timed out")
        sock = getattr(connection, "sock", None)
        if sock is not None and hasattr(sock, "settimeout"):
            sock.settimeout(max(0.1, remaining))


def _normalize_email(value: str) -> str:
    address = str(value or "").strip().casefold()
    if address.count("@") != 1 or any(character.isspace() for character in address):
        raise ValueError("alias is invalid")
    return address


def _encode_imap_mailbox(value: str) -> str:
    encoded: list[str] = []
    unicode_run: list[str] = []

    def flush_unicode() -> None:
        if not unicode_run:
            return
        raw = "".join(unicode_run).encode("utf-16be")
        token = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
        encoded.append(f"&{token}-")
        unicode_run.clear()

    for character in value:
        if " " <= character <= "~":
            flush_unicode()
            encoded.append("&-" if character == "&" else character)
        else:
            unicode_run.append(character)
    flush_unicode()
    return "".join(encoded)


def _mailbox_argument(value: str) -> str:
    if not value or any(character in value for character in "\r\n\x00"):
        raise ValueError("IMAP folder is invalid")
    encoded = _encode_imap_mailbox(value)
    if re.fullmatch(r"[A-Za-z0-9._/-]+", encoded):
        return encoded
    escaped = encoded.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _allocate_folder_uids(
    folder_uids: Sequence[tuple[str, Sequence[str]]],
    limit: int,
) -> tuple[list[tuple[str, list[str]]], bool]:
    selected = [[] for _folder, _uids in folder_uids]
    positions = [0 for _folder, _uids in folder_uids]
    remaining = max(0, int(limit))
    while remaining:
        progressed = False
        for index, (_folder, uids) in enumerate(folder_uids):
            if positions[index] >= len(uids):
                continue
            selected[index].append(str(uids[positions[index]]))
            positions[index] += 1
            remaining -= 1
            progressed = True
            if remaining == 0:
                break
        if not progressed:
            break
    truncated = any(
        position < len(uids)
        for position, (_folder, uids) in zip(positions, folder_uids, strict=True)
    )
    return [
        (folder, selected[index]) for index, (folder, _uids) in enumerate(folder_uids)
    ], truncated


def _message_matches_alias(message: Message, alias: str) -> bool:
    target = _normalize_email(alias)
    values: list[str] = []
    for name in RECIPIENT_HEADERS:
        values.extend(str(value) for value in message.get_all(name, []))
    for _display_name, address in getaddresses(values):
        if str(address or "").strip().casefold() == target:
            return True
    pattern = re.compile(
        rf"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{{|}}~-]){re.escape(target)}"
        r"(?![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])",
        re.IGNORECASE,
    )
    return any(pattern.search(value) for value in values)


def _message_matches_sender(message: Message, sender_filter: str) -> bool:
    expected = str(sender_filter or "").strip().casefold()
    if not expected:
        return True
    senders = [
        str(address or "").strip().casefold()
        for _name, address in getaddresses(message.get_all("From", []))
        if str(address or "").strip()
    ]
    if "@" in expected and not expected.startswith("@"):
        return expected in senders
    domain = expected.removeprefix("@")
    return any(
        sender.rsplit("@", 1)[-1] == domain or sender.rsplit("@", 1)[-1].endswith(f".{domain}")
        for sender in senders
        if "@" in sender
    )


def _extract_message_code(message: Message) -> str:
    subject = str(message.get("Subject") or "")
    body = _message_body(message)
    from_header = " ".join(str(value) for value in message.get_all("From", []))
    text = re.sub(r"\s+", " ", f"{subject}\n{body}")
    grokish = _is_grok_message(from_header=from_header, subject=subject, body=body)
    contexts = tuple(_CODE_CONTEXT_RE.finditer(text))
    candidates: list[tuple[int, int, int, int, str]] = []

    def consider(match: re.Match[str], *, kind_rank: int, window: int, normalize) -> None:
        value = normalize(match.group(1))
        if not value:
            return
        if not contexts:
            if grokish and kind_rank == 0:
                # Grok mail often places the XXX-XXX code on its own with weak wording.
                candidates.append((window, kind_rank, 0, match.start(), value))
            return
        for context in contexts:
            if context.end() <= match.start():
                distance = match.start() - context.end()
                direction = 0
            elif match.end() <= context.start():
                distance = context.start() - match.end()
                direction = 1
            else:
                distance = 0
                direction = 0
            if distance <= window:
                candidates.append((distance, kind_rank, direction, match.start(), value))

    for match in _GROK_OTP_RE.finditer(text):
        consider(
            match,
            kind_rank=0 if grokish else 1,
            window=_GROK_CONTEXT_WINDOW if grokish else _CODE_CONTEXT_WINDOW,
            normalize=lambda value: value.upper(),
        )
    for match in _OTP_RE.finditer(text):
        consider(
            match,
            kind_rank=1 if grokish else 0,
            window=_CODE_CONTEXT_WINDOW,
            normalize=str,
        )
    if not candidates and grokish:
        standalone = re.search(
            r"(?m)^\s*([A-Za-z0-9]{3}-[A-Za-z0-9]{3})\s*$",
            f"{subject}\n{body}",
        )
        if standalone:
            return standalone.group(1).upper()
    if not candidates:
        return ""
    return min(candidates)[-1]


def _is_grok_message(*, from_header: str, subject: str, body: str) -> bool:
    haystack = f"{from_header}\n{subject}\n{body}"
    return _GROK_SENDER_RE.search(haystack) is not None


def _message_body(message: Message) -> str:
    parts: list[str] = []
    candidates = message.walk() if message.is_multipart() else (message,)
    for part in candidates:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        try:
            value = part.get_content()
        except (LookupError, UnicodeError):
            raw = part.get_payload(decode=True) or b""
            value = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
        text = str(value or "")
        if part.get_content_type() == "text/html":
            text = html.unescape(re.sub(r"<[^>]+>", " ", text))
        parts.append(text)
    return "\n".join(parts)


def _message_timestamp(message: Message) -> float | None:
    raw = str(message.get("Date") or "").strip()
    if not raw:
        return None
    try:
        value = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if value.tzinfo is None:
        return None
    return value.timestamp()


def _internal_timestamp(metadata: bytes) -> float | None:
    match = _INTERNALDATE_RE.search(metadata)
    if match is None:
        return None
    try:
        value = datetime.strptime(match.group(1).decode("ascii"), "%d-%b-%Y %H:%M:%S %z")
    except (UnicodeDecodeError, ValueError):
        return None
    return value.timestamp()


def _recipient_search_terms(alias: str) -> list[str]:
    """Build one OR-composed SEARCH key covering every recipient header.

    IMAP OR takes exactly two keys, so N terms nest as
    `OR t1 OR t2 ... OR t(n-1) tn`.
    """
    terms: list[str] = ["HEADER", RECIPIENT_HEADERS[-1], alias]
    for header in reversed(RECIPIENT_HEADERS[:-1]):
        terms = ["OR", "HEADER", header, alias, *terms]
    return terms


def _supports_within(connection: Any) -> bool:
    capabilities = getattr(connection, "capabilities", ())
    if not isinstance(capabilities, (list, tuple, set, frozenset)):
        return False
    return any(
        (item.decode("ascii", errors="ignore") if isinstance(item, bytes) else str(item))
        .strip()
        .upper()
        == "WITHIN"
        for item in capabilities
    )


def _messages_from_fetch(
    data: Any,
    *,
    fallback_uid: str = "",
) -> dict[str, tuple[Message, float | None]]:
    result: dict[str, tuple[Message, float | None]] = {}
    if not isinstance(data, (list, tuple)):
        return result
    for item in data:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        metadata = item[0] if isinstance(item[0], bytes) else b""
        raw = next((part for part in item[1:] if isinstance(part, bytes) and b"\n" in part), None)
        if raw is None:
            continue
        match = _UID_RE.search(metadata)
        uid = match.group(1).decode("ascii") if match else fallback_uid
        if not uid:
            continue
        result[uid] = (
            email.message_from_bytes(raw, policy=default),
            _internal_timestamp(metadata),
        )
    return result


def _uids_from_search(data: Any) -> set[str]:
    result: set[str] = set()
    if not isinstance(data, (list, tuple)):
        return result
    for item in data:
        if isinstance(item, bytes):
            result.update(part.decode("ascii") for part in item.split() if part.isdigit())
        elif isinstance(item, str):
            result.update(part for part in item.split() if part.isdigit())
    return result


def _uid_sort_key(value: str) -> tuple[int, str]:
    return (int(value), value) if value.isdigit() else (-1, value)


def _create_imap_connection(config: ImapConfig, timeout: float) -> Any:
    if config.proxy:
        return _ProxyIMAP4SSL(
            config.host,
            config.port,
            proxy_url=config.proxy,
            timeout=timeout,
        )
    return imaplib.IMAP4_SSL(config.host, config.port, timeout=timeout)


class _ProxyIMAP4SSL(imaplib.IMAP4_SSL):
    def __init__(
        self,
        host: str,
        port: int,
        *,
        proxy_url: str,
        ssl_context: ssl.SSLContext | None = None,
        timeout: float | None = None,
    ) -> None:
        self.proxy_url = proxy_url
        super().__init__(host, port, ssl_context=ssl_context, timeout=timeout)

    def _create_socket(self, timeout: float | None):
        proxy_type, host, port, rdns, username, password = _proxy_spec(self.proxy_url)
        raw_socket = socks.socksocket()
        try:
            raw_socket.set_proxy(
                proxy_type,
                addr=host,
                port=port,
                rdns=rdns,
                username=username or None,
                password=password or None,
            )
            raw_socket.settimeout(timeout)
            raw_socket.connect((self.host, self.port))
            return self.ssl_context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise


def _proxy_spec(value: str) -> tuple[int, str, int, bool, str, str]:
    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError("mailbox proxy is invalid") from exc
    proxy_types = {
        "socks5": (socks.SOCKS5, False),
        "socks5h": (socks.SOCKS5, True),
        "http": (socks.HTTP, True),
    }
    scheme = parsed.scheme.casefold()
    if scheme not in proxy_types or not parsed.hostname or port is None:
        raise ValueError("mailbox proxy must be HTTP or SOCKS5 with host and port")
    proxy_type, rdns = proxy_types[scheme]
    return (
        proxy_type,
        str(parsed.hostname),
        int(port),
        rdns,
        urllib.parse.unquote(parsed.username or ""),
        urllib.parse.unquote(parsed.password or ""),
    )


def _logout(connection: Any) -> None:
    with suppress(Exception):
        connection.logout()


__all__ = [
    "ImapConfig",
    "ImapCredentialsError",
    "ImapError",
    "ImapOtpReader",
    "OtpResult",
    "RECIPIENT_HEADERS",
    "RecentOtpBatch",
    "RecentOtpResult",
]
