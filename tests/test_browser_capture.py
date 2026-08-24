from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from icloud_gateway.browser_capture import (
    CaptureBusyError,
    CaptureError,
    CaptureManager,
    _open_browser,
    _resolve_cdp_endpoint,
    _validate_capture_url,
)
from icloud_gateway.hme import HmeNetworkError, ICloudHmeSession


def captured_session() -> ICloudHmeSession:
    return ICloudHmeSession(
        host="p123-maildomainws.icloud.com.cn",
        dsid="123",
        client_id="client",
        client_build_number="build",
        client_mastering_number="master",
        cookie=(
            "X-APPLE-DS-WEB-SESSION-TOKEN=session; "
            "X-APPLE-WEBAUTH-USER=user; "
            "X-APPLE-WEBAUTH-TOKEN=token"
        ),
        origin="https://www.icloud.com.cn",
        referer="https://www.icloud.com.cn/icloudplus/",
    )


def wait_for_terminal(manager: CaptureManager, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = manager.status()
        if not status["active"]:
            return status
        time.sleep(0.01)
    raise AssertionError("capture did not finish")


def test_capture_url_is_restricted_to_icloud_landing_and_plus_pages() -> None:
    assert _validate_capture_url("https://www.icloud.com.cn/icloudplus/")
    with pytest.raises(CaptureError):
        _validate_capture_url("https://evil.example/icloudplus/")
    with pytest.raises(CaptureError):
        _validate_capture_url("http://www.icloud.com.cn/icloudplus/")


def test_remote_browser_connection_is_marked_as_not_owned(monkeypatch) -> None:
    context = object()
    monkeypatch.setattr(
        "icloud_gateway.browser_capture.socket.gethostbyname",
        lambda hostname: "172.20.0.2" if hostname == "browser" else hostname,
    )

    class Browser:
        contexts = [context]

    class Chromium:
        @staticmethod
        def connect_over_cdp(endpoint, timeout):
            assert endpoint == "http://172.20.0.2:9222"
            assert timeout == 20_000
            return Browser()

    class Playwright:
        chromium = Chromium()

    handle = _open_browser(Playwright(), cdp_url="http://browser:9222", profile_dir=None)

    assert handle.context is context
    assert handle.owned_context is False


def test_cdp_resolution_preserves_unresolved_or_non_http_endpoints(monkeypatch) -> None:
    monkeypatch.setattr(
        "icloud_gateway.browser_capture.socket.gethostbyname",
        lambda _hostname: (_ for _ in ()).throw(OSError),
    )

    assert _resolve_cdp_endpoint("http://browser:9222") == "http://browser:9222"
    assert _resolve_cdp_endpoint("ws://browser:9222/devtools/browser/id") == (
        "ws://browser:9222/devtools/browser/id"
    )


def test_local_browser_profile_is_private_and_owned(tmp_path: Path) -> None:
    context = object()

    class Chromium:
        @staticmethod
        def launch_persistent_context(profile, **kwargs):
            assert Path(profile) == (tmp_path / "profile").resolve()
            assert kwargs["headless"] is False
            return context

    class Playwright:
        chromium = Chromium()

    handle = _open_browser(Playwright(), cdp_url="", profile_dir=tmp_path / "profile")

    assert handle.context is context
    assert handle.owned_context is True
    assert (tmp_path / "profile").stat().st_mode & 0o777 == 0o700


def test_manager_runs_capture_and_publishes_terminal_status() -> None:
    sessions = []
    statuses = []

    def runner(**kwargs):
        kwargs["on_waiting"]()
        kwargs["on_authenticated"]()
        return captured_session()

    manager = CaptureManager(
        cdp_url="http://browser:9222",
        on_session=sessions.append,
        on_status=statuses.append,
        runner=runner,
    )

    started = manager.start()
    terminal = wait_for_terminal(manager)

    assert started["state"] in {"starting", "waiting_login", "verifying", "captured"}
    assert terminal["state"] == "captured"
    assert sessions == [captured_session()]
    assert any(item["state"] == "waiting_login" for item in statuses)


def test_manager_rejects_concurrent_capture_and_cancels_cleanly() -> None:
    entered = threading.Event()

    def runner(**kwargs):
        kwargs["on_waiting"]()
        entered.set()
        kwargs["cancel_event"].wait(1.0)
        raise CaptureError("cancelled")

    manager = CaptureManager(
        cdp_url="http://browser:9222",
        on_session=lambda _session: None,
        runner=runner,
    )
    manager.start()
    assert entered.wait(1.0)

    with pytest.raises(CaptureBusyError):
        manager.start()

    cancelling = manager.cancel()
    terminal = wait_for_terminal(manager)

    assert cancelling["state"] == "cancelling"
    assert terminal["state"] == "cancelled"
    assert manager.shutdown()


def test_manager_sanitizes_unexpected_save_failure() -> None:
    def runner(**_kwargs):
        return captured_session()

    def save(_session):
        raise RuntimeError("database-password-canary")

    manager = CaptureManager(
        cdp_url="http://browser:9222",
        on_session=save,
        runner=runner,
    )
    manager.start()

    terminal = wait_for_terminal(manager)

    assert terminal["state"] == "failed"
    assert terminal["error_code"] == "capture_save_failed"
    assert "database-password-canary" not in terminal["message"]


def test_manager_distinguishes_network_failure_after_capture() -> None:
    def runner(**_kwargs):
        return captured_session()

    def save(_session):
        raise HmeNetworkError("proxy-password-canary")

    manager = CaptureManager(
        cdp_url="http://browser:9222",
        on_session=save,
        runner=runner,
    )
    manager.start()

    terminal = wait_for_terminal(manager)

    assert terminal["state"] == "failed"
    assert terminal["error_code"] == "capture_save_network"
    assert "proxy-password-canary" not in terminal["message"]
