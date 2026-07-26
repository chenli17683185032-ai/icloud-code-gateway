from __future__ import annotations

import email.utils
import imaplib
from datetime import UTC, datetime

import pytest

from icloud_gateway.imap_otp import (
    RECIPIENT_HEADERS,
    ImapConfig,
    ImapCredentialsError,
    ImapError,
    ImapOtpReader,
)

NOW = 1_800_000_000.0


def raw_message(
    *,
    recipient: str,
    code: str,
    sender: str = "noreply@service.example",
    date_timestamp: float | None = NOW,
    html: bool = False,
    recipient_header: str = "Delivered-To",
    include_date: bool = True,
) -> bytes:
    date_header = (
        f"Date: {email.utils.formatdate(date_timestamp, usegmt=True)}\r\n"
        if include_date and date_timestamp is not None
        else ""
    )
    content_type = "text/html; charset=utf-8" if html else "text/plain; charset=utf-8"
    body = (
        f"<p>Your verification code is <b>{code}</b></p>"
        if html
        else f"Your verification code is {code}"
    )
    return (
        f"From: Service <{sender}>\r\n"
        "To: forwarding@example.com\r\n"
        f"{recipient_header}: {recipient}\r\n"
        "Subject: Verify your email\r\n"
        f"{date_header}"
        f"Content-Type: {content_type}\r\n\r\n"
        f"{body}\r\n"
    ).encode()


class FakeImap:
    """Stands in for imaplib.IMAP4_SSL.

    Answers UID sets on FETCH the way a real server does, so tests exercise the
    batched protocol rather than a one-UID-at-a-time fallback.
    """

    def __init__(
        self,
        messages=None,
        *,
        login_error=False,
        capabilities=(),
        reject_uid_sets=False,
        reject_combined_search=False,
    ):
        self.messages = dict(messages or {})
        self.login_error = login_error
        self.capabilities = tuple(capabilities)
        self.reject_uid_sets = reject_uid_sets
        self.reject_combined_search = reject_combined_search
        self.logged_out = False
        self.searches: list[tuple] = []
        self.fetches: list[list[str]] = []

    def login(self, _username, _password):
        if self.login_error:
            raise imaplib.IMAP4.error("authentication failed with secret detail")
        return "OK", []

    def select(self, _folder, readonly=False):
        return ("OK", [b"1"]) if readonly else ("NO", [])

    def _fetch_one(self, uid):
        item = self.messages.get(uid)
        if item is None:
            return None
        raw, received_at = item
        internal = datetime.fromtimestamp(received_at, tz=UTC).strftime("%d-%b-%Y %H:%M:%S +0000")
        metadata = f'{uid} (UID {uid} INTERNALDATE "{internal}" BODY[] {{{len(raw)}}}'
        return (metadata.encode("ascii"), raw)

    def uid(self, command, *args):
        if command == "search":
            terms = tuple(str(item) for item in args if item is not None)
            self.searches.append(terms)
            if self.reject_combined_search and "OR" in terms:
                return "NO", []
            return "OK", [" ".join(self.messages).encode("ascii")]
        if command == "fetch":
            uids = [part for part in str(args[0]).split(",") if part]
            self.fetches.append(uids)
            if self.reject_uid_sets and len(uids) > 1:
                return "NO", []
            payload = [item for item in (self._fetch_one(uid) for uid in uids) if item is not None]
            if not payload:
                return "NO", []
            return "OK", [*payload, b")"]
        raise AssertionError((command, args))

    def logout(self):
        self.logged_out = True


def config() -> ImapConfig:
    return ImapConfig(
        forwarding_email="forwarding@example.com",
        host="imap.example.com",
        port=993,
        username="forwarding@example.com",
        password="imap-secret",
    )


def reader(connection: FakeImap) -> ImapOtpReader:
    return ImapOtpReader(config(), connection_factory=lambda _config, _timeout: connection)


def test_reader_matches_exact_alias_and_ignores_other_alias() -> None:
    connection = FakeImap(
        {
            "10": (raw_message(recipient="other@icloud.com", code="111111"), NOW - 3),
            "11": (raw_message(recipient="target@icloud.com", code="222222"), NOW - 2),
        }
    )

    result = reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    assert result.code == "222222"
    assert result.uid == "11"
    assert connection.logged_out


@pytest.mark.parametrize(
    "header",
    (
        "To",
        "Delivered-To",
        "X-Original-To",
        "Envelope-To",
        "Resent-To",
        "X-Envelope-To",
        "X-Apple-Forward-To",
        "X-Apple-Original-Recipient",
    ),
)
def test_reader_accepts_supported_forwarding_headers(header: str) -> None:
    connection = FakeImap(
        {
            "1": (
                raw_message(
                    recipient="target@icloud.com",
                    code="123456",
                    recipient_header=header,
                ),
                NOW,
            )
        }
    )

    result = reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    assert result.code == "123456"


@pytest.mark.parametrize(
    ("age", "expected"),
    ((299, "299299"), (300, "300300"), (301, None)),
)
def test_reader_enforces_the_five_minute_boundary(age: int, expected: str | None) -> None:
    code = f"{age:03d}{age:03d}"
    connection = FakeImap({"1": (raw_message(recipient="target@icloud.com", code=code), NOW - age)})

    result = reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    assert (None if result is None else result.code) == expected


@pytest.mark.parametrize(
    ("offset", "accepted"),
    ((60, True), (61, False)),
)
def test_reader_caps_future_clock_skew(offset: int, accepted: bool) -> None:
    connection = FakeImap(
        {
            "1": (
                raw_message(recipient="target@icloud.com", code="606060"),
                NOW + offset,
            )
        }
    )

    result = reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    assert (result is not None) is accepted


def test_reader_uses_internaldate_instead_of_a_forged_header_date() -> None:
    connection = FakeImap(
        {
            "1": (
                raw_message(
                    recipient="target@icloud.com",
                    code="909090",
                    date_timestamp=NOW,
                ),
                NOW - 301,
            )
        }
    )

    assert reader(connection).find_latest_code("target@icloud.com", now_ts=NOW) is None


def test_reader_returns_the_latest_received_message_not_the_largest_uid() -> None:
    connection = FakeImap(
        {
            "90": (raw_message(recipient="target@icloud.com", code="909090"), NOW - 1),
            "100": (raw_message(recipient="target@icloud.com", code="100100"), NOW - 10),
        }
    )

    result = reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    assert result.code == "909090"
    assert result.uid == "90"


def test_reader_extracts_html_and_honors_sender_domain_filter() -> None:
    connection = FakeImap(
        {
            "1": (
                raw_message(
                    recipient="target@icloud.com",
                    code="111111",
                    sender="noreply@other.example",
                    html=True,
                ),
                NOW,
            ),
            "2": (
                raw_message(
                    recipient="target@icloud.com",
                    code="222222",
                    sender="verify@mail.service.example",
                    html=True,
                ),
                NOW - 1,
            ),
        }
    )

    result = reader(connection).find_latest_code(
        "target@icloud.com", now_ts=NOW, sender_filter="service.example"
    )

    assert result.code == "222222"


def test_imap_auth_error_is_sanitized() -> None:
    connection = FakeImap(login_error=True)

    with pytest.raises(ImapCredentialsError) as caught:
        reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    assert "secret detail" not in str(caught.value)


def mailbox(count: int, *, match_from: int = 0) -> dict:
    return {
        str(uid): (
            raw_message(
                recipient=("target@icloud.com" if uid >= match_from else "other@icloud.com"),
                code=f"{uid:06d}",
            ),
            NOW - (100 - uid),
        )
        for uid in range(1, count + 1)
    }


def test_recipient_headers_are_searched_in_one_round_trip() -> None:
    connection = FakeImap(mailbox(3))

    reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    assert len(connection.searches) == 1
    terms = connection.searches[0]
    assert terms.count("OR") == len(RECIPIENT_HEADERS) - 1
    for header in RECIPIENT_HEADERS:
        assert header in terms


def test_combined_search_falls_back_to_one_search_per_header() -> None:
    connection = FakeImap(mailbox(3), reject_combined_search=True)

    result = reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    assert result is not None
    assert len(connection.searches) == 1 + len(RECIPIENT_HEADERS)


def test_candidates_are_fetched_in_batches_not_one_message_per_round_trip() -> None:
    connection = FakeImap(mailbox(120))

    result = reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    assert result is not None
    assert len(connection.searches) + len(connection.fetches) == 6
    assert len(connection.fetches) == 5
    assert sum(len(batch) for batch in connection.fetches) == 120


def test_batched_fetch_falls_back_to_single_uids_when_the_server_refuses_sets() -> None:
    connection = FakeImap(mailbox(3), reject_uid_sets=True)

    result = reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    assert result is not None
    # The set is attempted once per FETCH syntax before dropping to single UIDs.
    assert [len(batch) for batch in connection.fetches] == [3, 3, 1, 1, 1]


@pytest.mark.parametrize(
    "capabilities",
    (("IMAP4REV1", "WITHIN"), (b"IMAP4REV1", b"WITHIN")),
)
def test_within_capability_narrows_the_search_window(capabilities) -> None:
    without = FakeImap(mailbox(2))
    with_within = FakeImap(mailbox(2), capabilities=capabilities)

    reader(without).find_latest_code("target@icloud.com", now_ts=NOW)
    reader(with_within).find_latest_code("target@icloud.com", now_ts=NOW)

    assert "YOUNGER" not in without.searches[0]
    terms = with_within.searches[0]
    assert "YOUNGER" in terms
    # 300s OTP window plus a wide allowance for server clock skew.
    assert terms[terms.index("YOUNGER") + 1] == "1200"


def test_lookup_timeout_is_a_total_deadline_across_imap_operations() -> None:
    connection = FakeImap()
    ticks = iter((10.0, 10.2, 11.1))
    value = ImapOtpReader(
        config(),
        connection_factory=lambda _config, _timeout: connection,
        monotonic=lambda: next(ticks),
    )

    with pytest.raises(ImapError, match="timed out"):
        value.find_latest_code("target@icloud.com", now_ts=NOW, timeout=1)

    assert connection.logged_out is True
