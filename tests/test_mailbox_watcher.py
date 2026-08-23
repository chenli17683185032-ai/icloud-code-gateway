from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_imap_otp import NOW, raw_message

from icloud_gateway.imap_otp import ImapConfig, ImapCredentialsError
from icloud_gateway.mailbox_watcher import MailboxWatcher


class FakeMailbox:
    """Minimal IMAP server stand-in for the watcher loop."""

    def __init__(self, folders=None, *, login_error=False, unavailable_folders=()):
        self.folders = {
            str(name): dict(items) for name, items in (folders or {"INBOX": {}}).items()
        }
        self.unavailable_folders = {str(name) for name in unavailable_folders}
        self.login_error = login_error
        self.login_calls = 0
        self.logged_out = False
        self.selected = None
        self.searches: list[tuple] = []
        self.fetches: list[list[str]] = []

    def add(self, folder: str, uid: str, raw: bytes, received_at: float) -> None:
        self.folders.setdefault(folder, {})[uid] = (raw, received_at)

    def login(self, _username, _password):
        self.login_calls += 1
        if self.login_error:
            raise ImapCredentialsError("nope")
        return "OK", []

    def select(self, folder, readonly=False):
        name = str(folder)
        if not readonly or name in self.unavailable_folders or name not in self.folders:
            self.selected = None
            return "NO", []
        self.selected = name
        return "OK", [b"1"]

    def uid(self, command, *args):
        messages = self.folders[self.selected]
        if command == "search":
            terms = tuple(str(item) for item in args if item is not None)
            self.searches.append(terms)
            # A real server answers `n:*` with the highest UID even when it is
            # below n, so always echo everything and let the watcher filter.
            return "OK", [" ".join(messages).encode("ascii")]
        if command == "fetch":
            uids = [part for part in str(args[0]).split(",") if part]
            self.fetches.append(uids)
            payload = []
            for uid in uids:
                item = messages.get(uid)
                if item is None:
                    continue
                raw, received_at = item
                stamp = datetime.fromtimestamp(received_at, tz=UTC).strftime(
                    "%d-%b-%Y %H:%M:%S +0000"
                )
                metadata = f'{uid} (UID {uid} INTERNALDATE "{stamp}" BODY[] {{{len(raw)}}}'
                payload.append((metadata.encode("ascii"), raw))
            if not payload:
                return "NO", []
            return "OK", [*payload, b")"]
        raise AssertionError((command, args))

    def logout(self):
        self.logged_out = True


def config(**overrides) -> ImapConfig:
    values = {
        "forwarding_email": "forwarding@example.com",
        "host": "imap.example.com",
        "port": 993,
        "username": "forwarding@example.com",
        "password": "imap-secret",
    }
    values.update(overrides)
    return ImapConfig(**values)


def watcher(mailbox: FakeMailbox, *, imap_config=None, **overrides) -> MailboxWatcher:
    resolved = imap_config or config()
    return MailboxWatcher(
        lambda: resolved,
        connection_factory=lambda _config, _timeout: mailbox,
        clock=lambda: NOW,
        **overrides,
    )


def cycle(value: MailboxWatcher, mailbox: FakeMailbox, imap_config=None) -> bool:
    resolved = imap_config or config()
    connection = value._ensure_connection(resolved)
    return value._ingest(connection, resolved)


def test_ingest_indexes_message_by_recipient_alias() -> None:
    mailbox = FakeMailbox(
        {"INBOX": {"7": (raw_message(recipient="target@icloud.com", code="123456"), NOW - 10)}}
    )
    value = watcher(mailbox)

    assert cycle(value, mailbox) is True

    found = value.latest("target@icloud.com", now_ts=NOW, max_age_seconds=300)
    assert found is not None
    assert found.code == "123456"
    assert found.uid == "7"
    assert found.folder == "INBOX"


def test_message_is_indexed_under_every_recipient_it_carries() -> None:
    mailbox = FakeMailbox(
        {"INBOX": {"1": (raw_message(recipient="target@icloud.com", code="222222"), NOW - 5)}}
    )
    value = watcher(mailbox)
    cycle(value, mailbox)

    # raw_message also addresses the forwarding mailbox in To.
    assert value.latest("target@icloud.com", now_ts=NOW, max_age_seconds=300).code == "222222"
    assert value.latest("forwarding@example.com", now_ts=NOW, max_age_seconds=300).code == "222222"


def test_second_cycle_only_fetches_new_uids() -> None:
    mailbox = FakeMailbox(
        {"INBOX": {"1": (raw_message(recipient="target@icloud.com", code="111111"), NOW - 20)}}
    )
    value = watcher(mailbox)
    cycle(value, mailbox)
    assert mailbox.fetches == [["1"]]

    # Nothing new: the server still echoes UID 1 for `2:*`, and it must be skipped.
    assert cycle(value, mailbox) is False
    assert mailbox.fetches == [["1"]]

    mailbox.add("INBOX", "2", raw_message(recipient="target@icloud.com", code="333333"), NOW - 1)
    assert cycle(value, mailbox) is True
    assert mailbox.fetches == [["1"], ["2"]]
    assert value.latest("target@icloud.com", now_ts=NOW, max_age_seconds=300).code == "333333"


def test_latest_prefers_newest_arrival_not_highest_uid() -> None:
    mailbox = FakeMailbox(
        {
            "INBOX": {
                "90": (raw_message(recipient="target@icloud.com", code="909090"), NOW - 1),
                "100": (raw_message(recipient="target@icloud.com", code="100100"), NOW - 30),
            }
        }
    )
    value = watcher(mailbox)
    cycle(value, mailbox)

    found = value.latest("target@icloud.com", now_ts=NOW, max_age_seconds=300)
    assert found.code == "909090"
    assert found.uid == "90"


def test_public_sender_policy_filters_what_admin_view_still_sees() -> None:
    mailbox = FakeMailbox(
        {
            "INBOX": {
                "1": (
                    raw_message(
                        recipient="target@icloud.com",
                        code="654321",
                        sender="hello@cursor.com",
                    ),
                    NOW - 5,
                )
            }
        }
    )
    value = watcher(mailbox)
    cycle(value, mailbox)

    # Admin view has no sender policy.
    assert value.latest("target@icloud.com", now_ts=NOW, max_age_seconds=1800).code == "654321"
    # Buyers only ever receive GPT/Grok codes.
    assert (
        value.latest(
            "target@icloud.com",
            now_ts=NOW,
            max_age_seconds=300,
            sender_policy="gpt_grok",
        )
        is None
    )


def test_public_sender_policy_accepts_openai_sender() -> None:
    mailbox = FakeMailbox(
        {
            "INBOX": {
                "1": (
                    raw_message(
                        recipient="target@icloud.com",
                        code="654321",
                        sender="noreply@tm.openai.com",
                    ),
                    NOW - 5,
                )
            }
        }
    )
    value = watcher(mailbox)
    cycle(value, mailbox)

    found = value.latest(
        "target@icloud.com",
        now_ts=NOW,
        max_age_seconds=300,
        sender_policy="gpt_grok",
    )
    assert found is not None
    assert found.code == "654321"


def test_alias_sender_filter_is_applied_at_read_time() -> None:
    mailbox = FakeMailbox(
        {
            "INBOX": {
                "1": (
                    raw_message(
                        recipient="target@icloud.com",
                        code="777777",
                        sender="verify@mail.service.example",
                    ),
                    NOW - 5,
                )
            }
        }
    )
    value = watcher(mailbox)
    cycle(value, mailbox)

    assert (
        value.latest(
            "target@icloud.com",
            now_ts=NOW,
            max_age_seconds=300,
            sender_filter="service.example",
        ).code
        == "777777"
    )
    assert (
        value.latest(
            "target@icloud.com",
            now_ts=NOW,
            max_age_seconds=300,
            sender_filter="other.example",
        )
        is None
    )


def test_time_window_bounds_are_enforced_on_read() -> None:
    mailbox = FakeMailbox(
        {"INBOX": {"1": (raw_message(recipient="target@icloud.com", code="121212"), NOW - 600)}}
    )
    value = watcher(mailbox)
    cycle(value, mailbox)

    assert value.latest("target@icloud.com", now_ts=NOW, max_age_seconds=300) is None
    assert value.latest("target@icloud.com", now_ts=NOW, max_age_seconds=1800).code == "121212"


def test_junk_folder_is_scanned_for_qq_without_explicit_configuration() -> None:
    mailbox = FakeMailbox(
        {
            "INBOX": {},
            "Junk": {"4": (raw_message(recipient="target@icloud.com", code="404040"), NOW - 3)},
        }
    )
    qq = config(host="imap.qq.com", forwarding_email="a@qq.com", username="a@qq.com")
    value = watcher(mailbox, imap_config=qq)

    cycle(value, mailbox, qq)

    assert value.latest("target@icloud.com", now_ts=NOW, max_age_seconds=300).code == "404040"


def test_one_unavailable_folder_does_not_stop_the_other() -> None:
    mailbox = FakeMailbox(
        {
            "INBOX": {"1": (raw_message(recipient="target@icloud.com", code="101010"), NOW - 5)},
            "Junk": {},
        },
        unavailable_folders={"Junk"},
    )
    qq = config(host="imap.qq.com", forwarding_email="a@qq.com", username="a@qq.com")
    value = watcher(mailbox, imap_config=qq)

    cycle(value, mailbox, qq)

    assert value.latest("target@icloud.com", now_ts=NOW, max_age_seconds=300).code == "101010"


def test_prune_drops_entries_past_retention() -> None:
    mailbox = FakeMailbox(
        {"INBOX": {"1": (raw_message(recipient="target@icloud.com", code="151515"), NOW - 100)}}
    )
    value = watcher(mailbox, retention_seconds=60)

    cycle(value, mailbox)

    assert value.latest("target@icloud.com", now_ts=NOW, max_age_seconds=1800) is None


def test_listener_fires_only_when_new_codes_land() -> None:
    mailbox = FakeMailbox(
        {"INBOX": {"1": (raw_message(recipient="target@icloud.com", code="161616"), NOW - 5)}}
    )
    value = watcher(mailbox)
    fired = []
    unsubscribe = value.add_listener(lambda: fired.append(1))

    assert cycle(value, mailbox) is True

    # The loop notifies once a cycle added codes; emulate what _run does.
    value._notify()
    assert len(fired) == 1

    unsubscribe()
    value._notify()
    assert len(fired) == 1


def test_mail_without_a_code_is_not_indexed() -> None:
    mailbox = FakeMailbox(
        {
            "INBOX": {
                "1": (
                    raw_message(
                        recipient="target@icloud.com",
                        code="123456",
                        subject="Account notification",
                        body="Account 123456 signed in successfully.",
                    ),
                    NOW - 5,
                )
            }
        }
    )
    value = watcher(mailbox)

    assert cycle(value, mailbox) is False
    assert value.latest("target@icloud.com", now_ts=NOW, max_age_seconds=1800) is None


def test_watcher_is_not_ready_before_a_successful_cycle() -> None:
    mailbox = FakeMailbox()
    value = watcher(mailbox)

    assert value.ready is False
    assert value.status()["running"] is False


def test_rejected_login_surfaces_as_credentials_error() -> None:
    mailbox = FakeMailbox(login_error=True)
    value = watcher(mailbox)

    with pytest.raises(ImapCredentialsError):
        value._ensure_connection(config())


def test_changing_credentials_reconnects_and_resets_cursors() -> None:
    mailbox = FakeMailbox(
        {"INBOX": {"1": (raw_message(recipient="target@icloud.com", code="181818"), NOW - 5)}}
    )
    value = watcher(mailbox)
    cycle(value, mailbox)
    assert mailbox.login_calls == 1

    rotated = config(password="rotated-secret")
    cycle(value, mailbox, rotated)

    assert mailbox.login_calls == 2
    # Cursor was reset, so the existing message is re-read rather than skipped.
    assert mailbox.fetches == [["1"], ["1"]]
