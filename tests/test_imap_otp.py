from __future__ import annotations

import email.utils
import imaplib
from datetime import UTC, datetime, timedelta

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
    subject: str = "Verify your email",
    body: str | None = None,
) -> bytes:
    date_header = (
        f"Date: {email.utils.formatdate(date_timestamp, usegmt=True)}\r\n"
        if include_date and date_timestamp is not None
        else ""
    )
    content_type = "text/html; charset=utf-8" if html else "text/plain; charset=utf-8"
    message_body = body
    if message_body is None:
        message_body = (
            f"<p>Your verification code is <b>{code}</b></p>"
            if html
            else f"Your verification code is {code}"
        )
    return (
        f"From: Service <{sender}>\r\n"
        "To: forwarding@example.com\r\n"
        f"{recipient_header}: {recipient}\r\n"
        f"Subject: {subject}\r\n"
        f"{date_header}"
        f"Content-Type: {content_type}\r\n\r\n"
        f"{message_body}\r\n"
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
        folders=None,
        unavailable_folders=(),
    ):
        self.folders = (
            {str(name): dict(items) for name, items in folders.items()}
            if folders is not None
            else {"INBOX": dict(messages or {})}
        )
        self.unavailable_folders = {str(name) for name in unavailable_folders}
        self.login_error = login_error
        self.login_calls = 0
        self.logged_out = False
        self.selected_folder = None
        self.select_calls = []
        self.capabilities = tuple(capabilities)
        self.reject_uid_sets = reject_uid_sets
        self.reject_combined_search = reject_combined_search
        self.searches: list[tuple] = []
        self.fetches: list[list[str]] = []

    def login(self, _username, _password):
        self.login_calls += 1
        if self.login_error:
            raise imaplib.IMAP4.error("authentication failed with secret detail")
        return "OK", []

    def select(self, folder, readonly=False):
        name = str(folder)
        self.select_calls.append((name, readonly))
        if not readonly or name in self.unavailable_folders or name not in self.folders:
            self.selected_folder = None
            return "NO", []
        self.selected_folder = name
        return "OK", [str(len(self.folders[name])).encode("ascii")]

    def _fetch_one(self, uid):
        item = self.folders[self.selected_folder].get(uid)
        if item is None:
            return None
        raw, received_at = item
        internal = datetime.fromtimestamp(received_at, tz=UTC).strftime("%d-%b-%Y %H:%M:%S +0000")
        metadata = f'{uid} (UID {uid} INTERNALDATE "{internal}" BODY[] {{{len(raw)}}}'
        return (metadata.encode("ascii"), raw)

    def uid(self, command, *args):
        messages = self.folders[self.selected_folder]
        if command == "search":
            terms = tuple(str(item) for item in args if item is not None)
            self.searches.append(terms)
            if self.reject_combined_search and "OR" in terms:
                return "NO", []
            return "OK", [" ".join(messages).encode("ascii")]
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
    return ImapOtpReader(
        config(),
        connection_factory=lambda _config, _timeout: connection,
        reuse_connection=False,
    )


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


def test_recent_codes_scan_all_managed_aliases_in_one_search() -> None:
    connection = FakeImap(
        {
            "10": (raw_message(recipient="one@icloud.com", code="101010"), NOW - 3),
            "11": (raw_message(recipient="two@icloud.com", code="111111"), NOW - 2),
            "12": (raw_message(recipient="other@icloud.com", code="121212"), NOW - 1),
            "13": (raw_message(recipient="one@icloud.com", code="131313"), NOW),
        }
    )

    batch = reader(connection).find_recent_codes(
        ["one@icloud.com", "two@icloud.com"],
        now_ts=NOW,
    )

    assert [(item.alias, item.code, item.uid) for item in batch.items] == [
        ("one@icloud.com", "131313", "13"),
        ("two@icloud.com", "111111", "11"),
        ("one@icloud.com", "101010", "10"),
    ]
    assert batch.scanned == 4
    assert batch.truncated is False
    assert len(connection.searches) == 1
    assert "HEADER" not in connection.searches[0]
    assert "OR" not in connection.searches[0]
    assert connection.fetches == [["13", "12", "11", "10"]]


def test_recent_codes_report_a_bounded_scan() -> None:
    connection = FakeImap(
        {
            "1": (raw_message(recipient="target@icloud.com", code="111111"), NOW - 2),
            "2": (raw_message(recipient="target@icloud.com", code="222222"), NOW - 1),
            "3": (raw_message(recipient="target@icloud.com", code="333333"), NOW),
        }
    )

    batch = reader(connection).find_recent_codes(
        ["target@icloud.com"],
        now_ts=NOW,
        scan_limit=2,
        result_limit=2,
    )

    assert [item.uid for item in batch.items] == ["3", "2"]
    assert batch.scanned == 2
    assert batch.truncated is True


def test_recent_codes_enforce_past_and_future_time_boundaries() -> None:
    connection = FakeImap(
        {
            "1": (raw_message(recipient="target@icloud.com", code="111111"), NOW - 301),
            "2": (raw_message(recipient="target@icloud.com", code="222222"), NOW - 300),
            "3": (raw_message(recipient="target@icloud.com", code="333333"), NOW + 60),
            "4": (raw_message(recipient="target@icloud.com", code="444444"), NOW + 61),
        }
    )

    batch = reader(connection).find_recent_codes(
        ["target@icloud.com"],
        now_ts=NOW,
    )

    assert [(item.uid, item.code) for item in batch.items] == [
        ("3", "333333"),
        ("2", "222222"),
    ]


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


def test_reader_rejects_six_digit_number_without_verification_context() -> None:
    connection = FakeImap(
        {
            "1": (
                raw_message(
                    recipient="target@icloud.com",
                    code="123456",
                    subject="Account notification",
                    body="Account 123456 signed in successfully.",
                ),
                NOW,
            )
        }
    )

    assert reader(connection).find_latest_code("target@icloud.com", now_ts=NOW) is None


def test_reader_prefers_the_candidate_nearest_to_verification_context() -> None:
    connection = FakeImap(
        {
            "1": (
                raw_message(
                    recipient="target@icloud.com",
                    code="222222",
                    subject="Sign-in details",
                    body="Verification details: order 111111; your code is 222222.",
                ),
                NOW,
            )
        }
    )

    result = reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    assert result.code == "222222"


@pytest.mark.parametrize(
    ("body", "code"),
    (
        ("您的认证码为 333333，请勿泄露。", "333333"),
        ("444444 是您的确认码，请在五分钟内使用。", "444444"),
        ("<p>一次性密码：<strong>555555</strong></p>", "555555"),
        ("<p>您的临时代码：<strong>666777</strong></p>", "666777"),
    ),
)
def test_reader_accepts_chinese_and_html_verification_context(body: str, code: str) -> None:
    connection = FakeImap(
        {
            "1": (
                raw_message(
                    recipient="target@icloud.com",
                    code=code,
                    subject="账户通知",
                    body=body,
                    html=body.startswith("<"),
                ),
                NOW,
            )
        }
    )

    result = reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    assert result.code == code


def test_reader_ignores_html_layout_whitespace_when_scoring_context() -> None:
    layout_gap = "\n" + ((" " * 40) + "\n") * 5
    body = (
        "<table><tr><td>Your verification code</td></tr>"
        f"{layout_gap}<tr><td>666666</td></tr></table>"
    )
    connection = FakeImap(
        {
            "1": (
                raw_message(
                    recipient="target@icloud.com",
                    code="666666",
                    subject="Sign-in notice",
                    body=body,
                    html=True,
                ),
                NOW,
            )
        }
    )

    result = reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    assert result.code == "666666"


def test_old_imap_configuration_defaults_to_one_folder() -> None:
    values = config().as_secret_dict()
    values.pop("junk_folder")

    value = ImapConfig.from_mapping(values)

    assert value.folder == "INBOX"
    assert value.junk_folder == ""


def test_imap_configuration_rejects_invalid_junk_folder() -> None:
    values = config().as_secret_dict()
    values["junk_folder"] = "Junk\r\nINBOX"

    with pytest.raises(ImapCredentialsError):
        ImapConfig.from_mapping(values)


def test_reader_returns_latest_code_across_primary_and_junk_folders() -> None:
    connection = FakeImap(
        folders={
            "INBOX": {"10": (raw_message(recipient="target@icloud.com", code="101010"), NOW - 20)},
            "Junk": {"2": (raw_message(recipient="target@icloud.com", code="202020"), NOW - 2)},
        }
    )
    value = ImapOtpReader(
        ImapConfig(**{**config().as_secret_dict(), "junk_folder": "Junk"}),
        connection_factory=lambda _config, _timeout: connection,
    )

    result = value.find_latest_code("target@icloud.com", now_ts=NOW)

    assert result.code == "202020"
    assert connection.login_calls == 1
    assert connection.select_calls == [
        ("INBOX", True),
        ("Junk", True),
        ("INBOX", True),
        ("Junk", True),
    ]


def test_recent_codes_share_one_login_and_one_total_limit_across_folders() -> None:
    connection = FakeImap(
        folders={
            "INBOX": {
                "1": (raw_message(recipient="target@icloud.com", code="101010"), NOW - 4),
                "3": (raw_message(recipient="target@icloud.com", code="303030"), NOW - 2),
            },
            "Junk": {
                "2": (raw_message(recipient="target@icloud.com", code="202020"), NOW - 3),
                "4": (raw_message(recipient="target@icloud.com", code="404040"), NOW - 1),
            },
        }
    )
    value = ImapOtpReader(
        ImapConfig(**{**config().as_secret_dict(), "junk_folder": "Junk"}),
        connection_factory=lambda _config, _timeout: connection,
    )

    batch = value.find_recent_codes(
        ["target@icloud.com"],
        now_ts=NOW,
        scan_limit=3,
    )

    assert [item.uid for item in batch.items] == ["4", "3", "1"]
    assert batch.scanned == 3
    assert batch.truncated is True
    assert connection.login_calls == 1
    assert len(connection.searches) == 2
    assert sum(len(items) for items in connection.fetches) == 3


@pytest.mark.parametrize(
    ("unavailable", "expected"),
    (({"INBOX"}, "202020"), ({"Junk"}, "101010")),
)
def test_recent_codes_degrade_when_one_folder_is_unavailable(
    unavailable: set[str], expected: str
) -> None:
    connection = FakeImap(
        folders={
            "INBOX": {"1": (raw_message(recipient="target@icloud.com", code="101010"), NOW - 2)},
            "Junk": {"2": (raw_message(recipient="target@icloud.com", code="202020"), NOW - 1)},
        },
        unavailable_folders=unavailable,
    )
    value = ImapOtpReader(
        ImapConfig(**{**config().as_secret_dict(), "junk_folder": "Junk"}),
        connection_factory=lambda _config, _timeout: connection,
    )

    batch = value.find_recent_codes(["target@icloud.com"], now_ts=NOW)

    assert [item.code for item in batch.items] == [expected]


@pytest.mark.parametrize(
    ("unavailable", "expected"),
    (({"INBOX"}, "202020"), ({"Junk"}, "101010")),
)
def test_reader_degrades_when_one_configured_folder_is_unavailable(
    unavailable: set[str], expected: str
) -> None:
    connection = FakeImap(
        folders={
            "INBOX": {"1": (raw_message(recipient="target@icloud.com", code="101010"), NOW - 2)},
            "Junk": {"2": (raw_message(recipient="target@icloud.com", code="202020"), NOW - 1)},
        },
        unavailable_folders=unavailable,
    )
    value = ImapOtpReader(
        ImapConfig(**{**config().as_secret_dict(), "junk_folder": "Junk"}),
        connection_factory=lambda _config, _timeout: connection,
    )

    result = value.find_latest_code("target@icloud.com", now_ts=NOW)

    assert result.code == expected


def test_reader_fails_closed_when_all_configured_folders_are_unavailable() -> None:
    connection = FakeImap(
        folders={"INBOX": {}, "Junk": {}},
        unavailable_folders={"INBOX", "Junk"},
    )
    value = ImapOtpReader(
        ImapConfig(**{**config().as_secret_dict(), "junk_folder": "Junk"}),
        connection_factory=lambda _config, _timeout: connection,
    )

    with pytest.raises(ImapError, match="folder is unavailable"):
        value.find_latest_code("target@icloud.com", now_ts=NOW)


def test_connection_check_requires_every_configured_folder() -> None:
    connection = FakeImap(
        folders={"INBOX": {}, "Junk": {}},
        unavailable_folders={"Junk"},
    )
    value = ImapOtpReader(
        ImapConfig(**{**config().as_secret_dict(), "junk_folder": "Junk"}),
        connection_factory=lambda _config, _timeout: connection,
    )

    with pytest.raises(ImapError, match="folder is unavailable"):
        value.check()

    assert connection.select_calls == [("INBOX", True), ("Junk", True)]


@pytest.mark.parametrize(
    ("folder", "argument"),
    (
        ("Deleted Messages", '"Deleted Messages"'),
        ('Folder "One"', '"Folder \\"One\\""'),
        ("R&D", '"R&-D"'),
        ("\u5df2\u5220\u9664", '"&XfJSIJZk-"'),
    ),
)
def test_connection_check_encodes_configured_mailbox_name(folder: str, argument: str) -> None:
    connection = FakeImap(folders={argument: {}})
    values = config().as_secret_dict()
    values["folder"] = folder
    value = ImapOtpReader(
        ImapConfig(**values),
        connection_factory=lambda _config, _timeout: connection,
    )

    value.check()

    assert connection.select_calls == [(argument, True)]


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


class FastHeaderMissImap(FakeImap):
    """Server where the common delivery headers carry nothing for the alias."""

    FAST_HEADERS = ("To", "Delivered-To", "X-Original-To")

    def uid(self, command, *args):
        if command == "search":
            terms = tuple(str(item) for item in args if item is not None)
            targets_fast_header = any(
                terms[index] == "HEADER" and terms[index + 1] in self.FAST_HEADERS
                for index in range(len(terms) - 1)
            )
            if targets_fast_header and "OR" not in terms:
                self.searches.append(terms)
                return "OK", [b""]
        return super().uid(command, *args)


def test_common_recipient_header_resolves_in_one_round_trip() -> None:
    connection = FakeImap(mailbox(3))

    reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    # QQ and iCloud both set To, so the fast path stops at the first hit rather
    # than paying for the OR-composed search across all eight headers.
    assert len(connection.searches) == 1
    terms = connection.searches[0]
    assert "HEADER" in terms
    assert "To" in terms
    assert "OR" not in terms


def test_or_composed_search_covers_every_header_when_the_fast_path_misses() -> None:
    connection = FastHeaderMissImap(mailbox(3))

    result = reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    assert result is not None
    combined = next(terms for terms in connection.searches if "OR" in terms)
    assert combined.count("OR") == len(RECIPIENT_HEADERS) - 1
    for header in RECIPIENT_HEADERS:
        assert header in combined


def test_combined_search_falls_back_to_one_search_per_header() -> None:
    connection = FastHeaderMissImap(mailbox(3), reject_combined_search=True)

    result = reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    assert result is not None
    per_header = [terms for terms in connection.searches if "OR" not in terms]
    for header in RECIPIENT_HEADERS:
        assert any(header in terms for terms in per_header)


def test_candidates_are_fetched_in_batches_not_one_message_per_round_trip() -> None:
    connection = FakeImap(mailbox(120))

    result = reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    assert result is not None
    # A single-alias lookup examines a bounded newest-first window instead of
    # dumping the whole SINCE set, which is what made "copy an already
    # generated email" feel like a full inbox scan.
    assert sum(len(batch) for batch in connection.fetches) == 24
    assert len(connection.fetches) == 3
    assert connection.fetches[0] == [str(uid) for uid in range(120, 112, -1)]


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


def test_reader_extracts_grok_alphanumeric_code_from_xai_sender() -> None:
    connection = FakeImap(
        {
            "1": (
                raw_message(
                    recipient="target@icloud.com",
                    code="A1B-2C3",
                    sender="noreply@x.ai",
                    subject="Your Grok verification code",
                    body=(
                        "Use this code to continue signing in to Grok.\n\n"
                        "A1B-2C3\n\n"
                        "This code expires soon."
                    ),
                ),
                NOW,
            )
        }
    )

    result = reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    assert result is not None
    assert result.code == "A1B-2C3"


def test_reader_extracts_grok_code_from_html_mail() -> None:
    connection = FakeImap(
        {
            "1": (
                raw_message(
                    recipient="target@icloud.com",
                    code="Z9Y-8X7",
                    sender="security@mail.x.ai",
                    subject="Grok security code",
                    body=(
                        "<html><body>"
                        "<p>Your Grok login code is:</p>"
                        '<p style="font-size:28px"><b>z9y-8x7</b></p>'
                        "</body></html>"
                    ),
                    html=True,
                ),
                NOW,
            )
        }
    )

    result = reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    assert result is not None
    assert result.code == "Z9Y-8X7"


def test_reader_prefers_grok_code_over_nearby_numeric_noise() -> None:
    connection = FakeImap(
        {
            "1": (
                raw_message(
                    recipient="target@icloud.com",
                    code="Q1W-E2R",
                    sender="no-reply@x.ai",
                    subject="xAI verification",
                    body=(
                        "Ticket 123456 is unrelated.\n"
                        "Your verification code is Q1W-E2R.\n"
                        "Reference 654321 also unrelated."
                    ),
                ),
                NOW,
            )
        }
    )

    result = reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    assert result is not None
    assert result.code == "Q1W-E2R"


def test_reader_public_policy_only_returns_gpt_and_grok_senders() -> None:
    connection = FakeImap(
        {
            "1": (
                raw_message(
                    recipient="target@icloud.com",
                    code="111111",
                    sender="hello@cursor.com",
                    subject="Your Cursor verification code",
                    body="Your verification code is 111111",
                ),
                NOW,
            ),
            "2": (
                raw_message(
                    recipient="target@icloud.com",
                    code="222222",
                    sender="noreply@tm.openai.com",
                    subject="Your ChatGPT verification code",
                    body="Your verification code is 222222",
                ),
                NOW - 1,
            ),
        }
    )

    result = reader(connection).find_latest_code(
        "target@icloud.com",
        now_ts=NOW,
        sender_policy="gpt_grok",
    )

    assert result is not None
    assert result.code == "222222"

    grok = FakeImap(
        {
            "1": (
                raw_message(
                    recipient="target@icloud.com",
                    code="A1B-2C3",
                    sender="noreply@x.ai",
                    subject="Your Grok verification code",
                    body="Use this code to continue signing in to Grok.\n\nA1B-2C3",
                ),
                NOW,
            )
        }
    )
    grok_result = reader(grok).find_latest_code(
        "target@icloud.com",
        now_ts=NOW,
        sender_policy="gpt_grok",
    )
    assert grok_result is not None
    assert grok_result.code == "A1B-2C3"

    cursor_only = FakeImap(
        {
            "1": (
                raw_message(
                    recipient="target@icloud.com",
                    code="333333",
                    sender="hello@cursor.com",
                    subject="Your Cursor verification code",
                    body="Your verification code is 333333",
                ),
                NOW,
            )
        }
    )
    assert (
        reader(cursor_only).find_latest_code(
            "target@icloud.com",
            now_ts=NOW,
            sender_policy="gpt_grok",
        )
        is None
    )


def test_reader_still_accepts_six_digit_codes_for_non_grok_mail() -> None:
    connection = FakeImap(
        {
            "1": (
                raw_message(
                    recipient="target@icloud.com",
                    code="777777",
                    sender="noreply@service.example",
                    subject="Verify your email",
                    body="Your verification code is 777777",
                ),
                NOW,
            )
        }
    )

    result = reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    assert result is not None
    assert result.code == "777777"


def test_reader_rejects_xxx_xxx_without_context_for_non_grok_mail() -> None:
    connection = FakeImap(
        {
            "1": (
                raw_message(
                    recipient="target@icloud.com",
                    code="AAA-BBB",
                    sender="noreply@other.example",
                    subject="Package tracking",
                    body="Tracking update AAA-BBB for your order.",
                ),
                NOW,
            )
        }
    )

    assert reader(connection).find_latest_code("target@icloud.com", now_ts=NOW) is None


def test_since_window_backs_off_a_day_for_mailbox_local_time() -> None:
    connection = FakeImap(mailbox(2))

    reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    terms = connection.searches[0]
    since_value = terms[terms.index("SINCE") + 1]
    # SEARCH SINCE is day-granular and servers resolve it against the mailbox
    # timezone. QQ runs on CST, so a UTC-derived date silently dropped mail for
    # the hours around Beijing midnight.
    oldest = datetime.fromtimestamp(NOW - 300, tz=UTC)
    assert since_value == (oldest - timedelta(days=1)).strftime("%d-%b-%Y")


def test_public_lookup_scans_qq_junk_without_explicit_configuration() -> None:
    connection = FakeImap(
        folders={
            "INBOX": {},
            "Junk": {"2": (raw_message(recipient="target@icloud.com", code="202020"), NOW - 2)},
        }
    )
    values = config().as_secret_dict()
    values["host"] = "imap.qq.com"
    value = ImapOtpReader(
        ImapConfig(**values),
        connection_factory=lambda _config, _timeout: connection,
        reuse_connection=False,
    )

    # Admin reads already compensated for QQ filing HME forwards into Junk;
    # buyers were told "waiting" for a code the operator could see.
    result = value.find_latest_code("target@icloud.com", now_ts=NOW)

    assert result.code == "202020"


def test_header_and_text_miss_only_peeks_the_newest_window() -> None:
    class HeaderMissImap(FakeImap):
        def uid(self, command, *args):
            if command == "search":
                terms = tuple(str(item) for item in args if item is not None)
                self.searches.append(terms)
                if "HEADER" in terms or "TEXT" in terms:
                    return "OK", [b""]
            return super().uid(command, *args)

    connection = HeaderMissImap(mailbox(40))

    result = reader(connection).find_latest_code("target@icloud.com", now_ts=NOW)

    assert result is not None
    # Bounded fallback: newest 12 only, never the whole SINCE set.
    fetched = [uid for batch in connection.fetches for uid in batch]
    assert fetched == [str(uid) for uid in range(40, 28, -1)]
    assert any("TEXT" in terms for terms in connection.searches)
