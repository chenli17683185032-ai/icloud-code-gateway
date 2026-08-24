"""Continuously ingest the forwarding mailbox so reads never touch IMAP.

Before this existed every code lookup opened its own IMAP session and rescanned
the same mailbox, so cost grew with `viewers x poll rate` instead of with the
number of new mails. One watcher now keeps a single connection warm, parses each
message exactly once, and indexes it by every recipient address it carries.
Both the public lookup and the admin view read that index in memory.

Detection uses adaptive polling rather than a hand-rolled IDLE: imaplib has no
IDLE support, the QQ mailbox is normally scanned across two folders (INBOX plus
Junk) so an IDLE on one of them would starve the other, and a stuck IDLE fails
far worse than a cheap SEARCH. Polling speeds up while someone is actually
waiting on a code and backs off when nobody is.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .imap_otp import (
    ImapConfig,
    ImapCredentialsError,
    ImapError,
    _create_imap_connection,
    _logout,
    _mailbox_argument,
    _messages_from_fetch,
    _uid_sort_key,
    _uids_from_search,
    parse_message,
    sender_matches_filter,
    sender_matches_policy,
)

# Poll faster while a request is parked on a long poll, slower when idle. Each
# cycle is one SEARCH per folder on an already-authenticated socket.
ACTIVE_POLL_SECONDS = 1.0
IDLE_POLL_SECONDS = 3.0
# Keep codes slightly longer than the widest read window (admin uses 30 minutes).
DEFAULT_RETENTION_SECONDS = 35 * 60
# A cold start seeds from the retention window so a code that landed seconds
# before boot is still served.
SEED_LIMIT = 60
# Bound on messages pulled in a single cycle, so a backlog cannot stall the loop.
CYCLE_FETCH_LIMIT = 60
FETCH_BATCH_SIZE = 20
MAX_CODES_PER_ALIAS = 5
RECONNECT_MIN_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 60.0
CONNECT_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class IndexedCode:
    code: str
    received_at: float
    uid: str
    folder: str
    from_header: str

    @property
    def received_at_utc(self) -> datetime:
        return datetime.fromtimestamp(self.received_at, tz=UTC)


class MailboxWatcher:
    def __init__(
        self,
        config_provider: Callable[[], ImapConfig | None],
        *,
        connection_factory: Callable[[ImapConfig, float], Any] | None = None,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
        active_poll_seconds: float = ACTIVE_POLL_SECONDS,
        idle_poll_seconds: float = IDLE_POLL_SECONDS,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config_provider = config_provider
        self.connection_factory = connection_factory or _create_imap_connection
        self.retention_seconds = max(60.0, float(retention_seconds))
        self.active_poll_seconds = max(0.1, float(active_poll_seconds))
        self.idle_poll_seconds = max(self.active_poll_seconds, float(idle_poll_seconds))
        self.clock = clock
        self.monotonic = monotonic

        self._lock = threading.Lock()
        self._index: dict[str, list[IndexedCode]] = {}
        self._cursors: dict[tuple[str, str], int] = {}
        self._listeners: dict[int, Callable[[], None]] = {}
        self._listener_seq = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._connection: Any | None = None
        self._connection_key: tuple | None = None
        self._last_success: float | None = None
        self._last_error: str = ""
        self._consecutive_failures = 0

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="icloud-mailbox-watcher",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout)))
        self._close_connection()

    def refresh_soon(self) -> None:
        """Ask the loop to run a cycle now (used after IMAP settings change)."""
        self._wake.set()

    @property
    def ready(self) -> bool:
        """True when the index is fresh enough to answer instead of IMAP.

        Anything staler than a few poll cycles means the loop is reconnecting,
        so callers must fall back to an on-demand scan rather than report a
        missing code that may well exist.
        """
        thread = self._thread
        if thread is None or not thread.is_alive():
            return False
        with self._lock:
            last = self._last_success
        if last is None:
            return False
        return (self.monotonic() - last) <= (self.idle_poll_seconds * 3 + 30.0)

    def status(self) -> dict[str, Any]:
        with self._lock:
            last = self._last_success
            tracked = len(self._index)
            error = self._last_error
        thread = self._thread
        return {
            "running": bool(thread is not None and thread.is_alive()),
            "ready": self.ready,
            "tracked_aliases": tracked,
            "seconds_since_success": (None if last is None else self.monotonic() - last),
            "last_error": error,
        }

    # ------------------------------------------------------------------ listeners

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a thread-safe callback fired after new codes are indexed.

        Returns an unsubscribe callable. A registered listener also tells the
        loop that somebody is waiting, which shortens the poll interval.
        """
        with self._lock:
            self._listener_seq += 1
            token = self._listener_seq
            self._listeners[token] = callback
        self._wake.set()

        def unsubscribe() -> None:
            with self._lock:
                self._listeners.pop(token, None)

        return unsubscribe

    def _notify(self) -> None:
        with self._lock:
            listeners = list(self._listeners.values())
        for callback in listeners:
            try:
                callback()
            except Exception:
                # A listener whose loop already closed must not kill ingestion.
                continue

    # ------------------------------------------------------------------ reads

    def latest(
        self,
        email: str,
        *,
        now_ts: float,
        max_age_seconds: int,
        future_skew_seconds: int = 60,
        sender_filter: str = "",
        sender_policy: str = "",
    ) -> IndexedCode | None:
        target = str(email or "").strip().casefold()
        if not target:
            return None
        oldest = float(now_ts) - max(1, int(max_age_seconds))
        newest = float(now_ts) + max(0, int(future_skew_seconds))
        with self._lock:
            entries = list(self._index.get(target, ()))
        best: IndexedCode | None = None
        for entry in entries:
            if entry.received_at < oldest or entry.received_at > newest:
                continue
            if not sender_matches_filter(entry.from_header, sender_filter):
                continue
            if not sender_matches_policy(entry.from_header, sender_policy):
                continue
            if best is None or (entry.received_at, _uid_sort_key(entry.uid)) > (
                best.received_at,
                _uid_sort_key(best.uid),
            ):
                best = entry
        return best

    def snapshot(
        self,
        emails: Sequence[str],
        *,
        now_ts: float,
        max_age_seconds: int,
        future_skew_seconds: int = 60,
        sender_policy: str = "",
    ) -> dict[str, IndexedCode]:
        result: dict[str, IndexedCode] = {}
        for email in emails:
            found = self.latest(
                email,
                now_ts=now_ts,
                max_age_seconds=max_age_seconds,
                future_skew_seconds=future_skew_seconds,
                sender_policy=sender_policy,
            )
            if found is not None:
                result[str(email or "").strip().casefold()] = found
        return result

    # ------------------------------------------------------------------ loop

    def _run(self) -> None:
        while not self._stop.is_set():
            config = self._safe_config()
            if config is None:
                self._sleep(self.idle_poll_seconds)
                continue
            try:
                connection = self._ensure_connection(config)
                added = self._ingest(connection, config)
                with self._lock:
                    self._last_success = self.monotonic()
                    self._last_error = ""
                    self._consecutive_failures = 0
                if added:
                    self._notify()
            except (ImapError, ImapCredentialsError, OSError, TimeoutError) as exc:
                self._handle_failure(exc)
                continue
            except Exception as exc:  # defensive: the loop must never die
                self._handle_failure(exc)
                continue
            self._sleep(self._poll_interval())

    def _poll_interval(self) -> float:
        with self._lock:
            waiting = bool(self._listeners)
        return self.active_poll_seconds if waiting else self.idle_poll_seconds

    def _sleep(self, seconds: float) -> None:
        self._wake.wait(timeout=max(0.05, float(seconds)))
        self._wake.clear()

    def _safe_config(self) -> ImapConfig | None:
        try:
            return self.config_provider()
        except Exception:
            return None

    def _handle_failure(self, exc: Exception) -> None:
        self._close_connection()
        with self._lock:
            self._consecutive_failures += 1
            failures = self._consecutive_failures
            self._last_error = type(exc).__name__
        backoff = min(RECONNECT_MAX_SECONDS, RECONNECT_MIN_SECONDS * (2 ** min(failures, 6)))
        self._stop.wait(timeout=backoff)

    # ------------------------------------------------------------------ connection

    @staticmethod
    def _config_key(config: ImapConfig) -> tuple:
        return (
            config.host,
            int(config.port),
            config.username,
            config.password,
            config.proxy,
            config.scan_folders,
        )

    def _ensure_connection(self, config: ImapConfig) -> Any:
        key = self._config_key(config)
        if self._connection is not None and self._connection_key == key:
            return self._connection
        # Credentials or folders changed; the cursors belong to the old mailbox.
        self._close_connection()
        connection = self.connection_factory(config, CONNECT_TIMEOUT_SECONDS)
        try:
            status, _ = connection.login(config.username, config.password)
        except Exception as exc:
            _logout(connection)
            raise ImapCredentialsError("IMAP rejected login") from exc
        if str(status).upper() != "OK":
            _logout(connection)
            raise ImapCredentialsError("IMAP rejected login")
        self._connection = connection
        self._connection_key = key
        with self._lock:
            self._cursors = {}
        return connection

    def _close_connection(self) -> None:
        connection = self._connection
        self._connection = None
        self._connection_key = None
        if connection is not None:
            _logout(connection)

    # ------------------------------------------------------------------ ingest

    def _ingest(self, connection: Any, config: ImapConfig) -> bool:
        added = False
        for folder in config.scan_folders:
            try:
                added |= self._ingest_folder(connection, config, folder)
            except ImapError:
                # One unavailable folder must not stop the other; a hard
                # connection fault surfaces on the next command anyway.
                continue
        self._prune()
        return added

    def _ingest_folder(self, connection: Any, config: ImapConfig, folder: str) -> bool:
        status, _ = connection.select(_mailbox_argument(folder), readonly=True)
        if str(status).upper() != "OK":
            raise ImapError("IMAP folder is unavailable")

        cursor_key = (self._connection_key_host(config), folder)
        with self._lock:
            cursor = self._cursors.get(cursor_key)

        uids = self._seed_uids(connection) if cursor is None else self._new_uids(connection, cursor)
        if not uids:
            return False

        uids = uids[:CYCLE_FETCH_LIMIT]
        highest = max(int(uid) for uid in uids if uid.isdigit())
        added = self._index_messages(connection, uids, folder)
        with self._lock:
            previous = self._cursors.get(cursor_key) or 0
            self._cursors[cursor_key] = max(previous, highest)
        return added

    @staticmethod
    def _connection_key_host(config: ImapConfig) -> str:
        return f"{config.host}:{config.port}/{config.username}"

    def _seed_uids(self, connection: Any) -> list[str]:
        oldest = self.clock() - self.retention_seconds
        # Back off a day: SEARCH SINCE is day-granular and resolved in the
        # mailbox timezone, so a UTC date can exclude still-valid mail.
        since = datetime.fromtimestamp(oldest - 86400, tz=UTC).strftime("%d-%b-%Y")
        status, data = self._search(connection, ["SINCE", since])
        if status is None or str(status).upper() != "OK":
            raise ImapError("IMAP search failed")
        found = sorted(_uids_from_search(data), key=_uid_sort_key, reverse=True)
        return found[:SEED_LIMIT]

    def _new_uids(self, connection: Any, cursor: int) -> list[str]:
        status, data = self._search(connection, ["UID", f"{cursor + 1}:*"])
        if status is None or str(status).upper() != "OK":
            raise ImapError("IMAP search failed")
        # `n:*` is inclusive of the highest UID even when it is below n, so the
        # server can echo an already-seen message. Filter by the cursor.
        found = [uid for uid in _uids_from_search(data) if uid.isdigit() and int(uid) > cursor]
        return sorted(found, key=_uid_sort_key, reverse=True)

    @staticmethod
    def _search(connection: Any, terms: Sequence[str]) -> tuple[Any, Any]:
        try:
            return connection.uid("search", None, *terms)
        except Exception:
            return None, []

    def _index_messages(self, connection: Any, uids: Sequence[str], folder: str) -> bool:
        added = False
        for start in range(0, len(uids), FETCH_BATCH_SIZE):
            batch = list(uids[start : start + FETCH_BATCH_SIZE])
            fetched = self._fetch(connection, batch)
            for uid, (message, internal_timestamp) in fetched.items():
                received_at = internal_timestamp
                if received_at is None:
                    continue
                parsed = parse_message(message, received_at=received_at)
                if not parsed.code or not parsed.recipients:
                    continue
                entry = IndexedCode(
                    code=parsed.code,
                    received_at=parsed.received_at,
                    uid=uid,
                    folder=folder,
                    from_header=parsed.from_header,
                )
                if self._store(parsed.recipients, entry):
                    added = True
        return added

    @staticmethod
    def _fetch(connection: Any, uids: Sequence[str]) -> dict[str, tuple[Any, float | None]]:
        if not uids:
            return {}
        try:
            status, data = connection.uid(
                "fetch",
                ",".join(uids),
                "(UID INTERNALDATE BODY.PEEK[])",
            )
        except Exception:
            return {}
        if str(status).upper() != "OK":
            return {}
        return _messages_from_fetch(data, fallback_uid=uids[0] if len(uids) == 1 else "")

    def _store(self, recipients: frozenset[str], entry: IndexedCode) -> bool:
        added = False
        with self._lock:
            for recipient in recipients:
                bucket = self._index.setdefault(recipient, [])
                if any(item.uid == entry.uid and item.folder == entry.folder for item in bucket):
                    continue
                bucket.append(entry)
                bucket.sort(
                    key=lambda item: (item.received_at, _uid_sort_key(item.uid)),
                    reverse=True,
                )
                del bucket[MAX_CODES_PER_ALIAS:]
                added = True
        return added

    def _prune(self) -> None:
        cutoff = self.clock() - self.retention_seconds
        with self._lock:
            for recipient in list(self._index):
                kept = [item for item in self._index[recipient] if item.received_at >= cutoff]
                if kept:
                    self._index[recipient] = kept
                else:
                    del self._index[recipient]


__all__ = ["IndexedCode", "MailboxWatcher"]
