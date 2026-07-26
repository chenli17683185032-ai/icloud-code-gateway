from __future__ import annotations

import email.utils
import imaplib
from datetime import UTC, datetime

import pytest

from icloud_gateway.imap_otp import (
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
    def __init__(self, messages=None, *, login_error=False):
        self.messages = dict(messages or {})
        self.login_error = login_error
        self.logged_out = False

    def login(self, _username, _password):
        if self.login_error:
            raise imaplib.IMAP4.error("authentication failed with secret detail")
        return "OK", []

    def select(self, _folder, readonly=False):
        return ("OK", [b"1"]) if readonly else ("NO", [])

    def uid(self, command, *args):
        if command == "search":
            return "OK", [" ".join(self.messages).encode("ascii")]
        if command == "fetch":
            uid = str(args[0])
            item = self.messages.get(uid)
            if item is None:
                return "NO", []
            raw, received_at = item
            internal = datetime.fromtimestamp(received_at, tz=UTC).strftime(
                "%d-%b-%Y %H:%M:%S +0000"
            )
            metadata = f'{uid} (UID {uid} INTERNALDATE "{internal}")'.encode("ascii")
            return "OK", [(metadata, raw)]
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
