from __future__ import annotations

import json
import threading
from pathlib import Path

from icloud_gateway.config import Settings
from icloud_gateway.imap_bridge import ImapMailboxBridge
from icloud_gateway.imap_otp import ImapConfig


RAW = (
    b"From: OpenAI <relay-message@icloud.com>\r\n"
    b"To: hidden.one@icloud.com\r\n"
    b"Subject: code\r\n"
    b"\r\n"
    b"Your OpenAI verification code is 246810.\r\n"
)


class FakeImap:
    def __init__(self, messages: dict[str, bytes]) -> None:
        self.messages = messages
        self.selected = ""

    def login(self, username: str, password: str):
        assert username == "cz@icloud.com"
        assert password == "app-password"
        return "OK", [b"logged-in"]

    def select(self, mailbox: str, readonly: bool = False):
        assert readonly is True
        self.selected = mailbox
        return "OK", [b"1"]

    def uid(self, command: str, *args):
        if command == "search":
            return "OK", [b" ".join(uid.encode() for uid in self.messages)]
        if command == "fetch":
            uid = args[0]
            return "OK", [(b"UID " + uid.encode(), self.messages[uid])]
        raise AssertionError(command)


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakeSession:
    def __init__(self, status_code: int = 200) -> None:
        self.trust_env = True
        self.proxies: dict[str, str] = {}
        self.posts: list[bytes] = []
        self.status_code = status_code

    def post(self, url: str, *, data: bytes, headers: dict[str, str], timeout: int):
        assert url == "https://mailbox.example/control/v1/messages"
        assert headers["Authorization"] == "Bearer control-token-abcdefghijklmnop"
        assert headers["X-Envelope-From"] == "relay-message@icloud.com"
        self.posts.append(data)
        return FakeResponse(self.status_code)


def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        master_key=b"k" * 32,
        admin_password="x" * 16,
        edge_base_url="https://mailbox.example",
        control_plane_token="control-token-abcdefghijklmnop",
        deployment_mode="control",
        imap_bridge_enabled=True,
    )


def config() -> ImapConfig:
    return ImapConfig(
        forwarding_email="cz@icloud.com",
        host="imap.mail.me.com",
        port=993,
        username="cz@icloud.com",
        password="app-password",
        folder="INBOX",
    )


def test_bridge_uploads_new_mail_once(tmp_path: Path) -> None:
    session = FakeSession()
    mailbox = FakeImap({"4": RAW})
    bridge = ImapMailboxBridge(
        settings(tmp_path),
        lambda: config(),
        threading.Event(),
        session=session,
        connection_factory=lambda _config, _timeout: mailbox,
        clock=lambda: 1_800_000_000,
    )
    assert bridge.run_once() == {"uploaded": 1, "skipped": 0}
    assert session.posts == [RAW]
    assert bridge.run_once() == {"uploaded": 0, "skipped": 0}
    assert json.loads((tmp_path / "imap-bridge-cursors.json").read_text()) == {"INBOX": 4}


def test_bridge_keeps_cursor_when_upload_fails(tmp_path: Path) -> None:
    session = FakeSession(status_code=500)
    bridge = ImapMailboxBridge(
        settings(tmp_path),
        lambda: config(),
        threading.Event(),
        session=session,
        connection_factory=lambda _config, _timeout: FakeImap({"4": RAW}),
        clock=lambda: 1_800_000_000,
    )
    assert bridge.run_once() == {"uploaded": 0, "skipped": 0}
    assert session.posts == [RAW]
    assert json.loads((tmp_path / "imap-bridge-cursors.json").read_text()) == {}
