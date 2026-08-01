from __future__ import annotations

import json
import threading

import pytest
import requests

from icloud_gateway.hme import (
    HmeClient,
    HmeError,
    HmeNetworkError,
    HmeSessionError,
    ICloudHmeSession,
    merge_set_cookie_headers,
    parse_hme_request,
    parse_hme_session_import,
    validate_icloud_setup_session,
)

URL = (
    "https://p123-maildomainws.icloud.com.cn/v2/hme/list"
    "?clientBuildNumber=2420Project42"
    "&clientMasteringNumber=2420B17"
    "&clientId=client-123"
    "&dsid=123456789"
)
COOKIE = (
    "X-APPLE-DS-WEB-SESSION-TOKEN=session-secret; "
    "X-APPLE-WEBAUTH-USER=user-secret; "
    "X-APPLE-WEBAUTH-TOKEN=token-secret"
)


class FakeResponse:
    def __init__(self, body, *, status_code=200, headers=None):
        self.body = body
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self.body


def session() -> ICloudHmeSession:
    return parse_hme_session_import(
        f"curl '{URL}' -H 'Cookie: {COOKIE}' "
        "-H 'Origin: https://www.icloud.com.cn' "
        "-H 'Referer: https://www.icloud.com.cn/icloudplus/'"
    )


def test_curl_import_extracts_minimum_validated_session() -> None:
    parsed = session()

    assert parsed.host == "p123-maildomainws.icloud.com.cn"
    assert parsed.dsid == "123456789"
    assert parsed.client_id == "client-123"
    assert parsed.cookie == COOKIE
    assert parsed.origin == "https://www.icloud.com.cn"


def test_har_import_prefers_the_list_request_and_accepts_cookie_items() -> None:
    document = {
        "log": {
            "entries": [
                {
                    "request": {
                        "url": URL,
                        "headers": [
                            {"name": "Origin", "value": "https://www.icloud.com.cn"},
                            {
                                "name": "Referer",
                                "value": "https://www.icloud.com.cn/icloudplus/",
                            },
                        ],
                        "cookies": [
                            {"name": name, "value": value}
                            for name, value in (
                                part.strip().split("=", 1) for part in COOKIE.split(";")
                            )
                        ],
                    }
                }
            ]
        }
    }

    parsed = parse_hme_session_import(json.dumps(document))

    assert parsed.cookie == COOKIE


def test_request_capture_can_build_cookie_header_from_browser_cookie_objects() -> None:
    parsed = parse_hme_request(
        URL,
        {
            "origin": "https://www.icloud.com.cn",
            "referer": "https://www.icloud.com.cn/icloudplus/",
        },
        cookies=[
            {"name": name, "value": value}
            for name, value in (part.strip().split("=", 1) for part in COOKIE.split(";"))
        ],
    )

    assert parsed.cookie == COOKIE


@pytest.mark.parametrize(
    "bad_url",
    [
        URL.replace("https://", "http://"),
        URL.replace("p123-maildomainws.icloud.com.cn", "evil.example"),
        URL.replace("/v2/hme/list", "/setup/ws/1/validate"),
        URL.replace("p123-maildomainws.icloud.com.cn", "icloud.com.cn.evil.example"),
    ],
)
def test_import_rejects_non_whitelisted_hosts_and_paths(bad_url: str) -> None:
    with pytest.raises(HmeSessionError):
        parse_hme_session_import(f"curl '{bad_url}' -H 'Cookie: {COOKIE}'")


def test_session_mapping_requires_all_core_cookies() -> None:
    value = session().as_secret_dict()
    value["cookie"] = "X-APPLE-WEBAUTH-USER=only-one"

    with pytest.raises(HmeSessionError):
        ICloudHmeSession.from_mapping(value)


def test_client_lists_and_creates_alias_using_generate_then_reserve() -> None:
    calls = []

    def requester(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if "/v2/hme/list" in url:
            return FakeResponse(
                {"success": True, "result": {"hmeEmails": [{"hme": "old@icloud.com"}]}}
            )
        if "/v1/hme/generate" in url:
            return FakeResponse({"success": True, "result": {"hme": "new@icloud.com"}})
        return FakeResponse(
            {
                "success": True,
                "result": {
                    "hme": {
                        "hme": "new@icloud.com",
                        "anonymousId": "remote-id",
                        "isActive": True,
                    }
                },
            }
        )

    client = HmeClient(session(), requester=requester)

    assert client.list_aliases()[0]["hme"] == "old@icloud.com"
    created = client.create_alias(label="Person 7", note="sender bound")

    assert created["anonymousId"] == "remote-id"
    assert [call[0] for call in calls] == ["GET", "POST", "POST"]
    assert json.loads(calls[-1][2]["data"]) == {
        "hme": "new@icloud.com",
        "label": "Person 7",
        "note": "sender bound",
    }


def test_client_rejects_partial_alias_list_and_sends_lifecycle_payloads() -> None:
    calls = []
    responses = [
        FakeResponse(
            {
                "success": True,
                "result": {"hmeEmails": [{"hme": "valid@icloud.com"}, "invalid"]},
            }
        ),
        FakeResponse({"success": True, "result": {}}),
        FakeResponse({"success": True, "result": {}}),
        FakeResponse({"success": True, "result": {}}),
    ]

    def requester(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return responses.pop(0)

    client = HmeClient(session(), requester=requester)

    with pytest.raises(HmeError):
        client.list_aliases()
    client.deactivate_alias("remote-id")
    client.reactivate_alias("remote-id")
    client.delete_alias("remote-id")

    assert [call[0] for call in calls] == ["GET", "POST", "POST", "POST"]
    assert [call[1].split("?", 1)[0].rsplit("/v1/hme", 1)[-1] for call in calls[1:]] == [
        "/deactivate",
        "/reactivate",
        "/delete",
    ]
    assert all(json.loads(call[2]["data"]) == {"anonymousId": "remote-id"} for call in calls[1:])


def test_client_rejects_non_list_alias_collection() -> None:
    client = HmeClient(
        session(),
        requester=lambda *_args, **_kwargs: FakeResponse(
            {"success": True, "result": {"hmeEmails": {}}}
        ),
    )

    with pytest.raises(HmeError):
        client.list_aliases()


def test_client_errors_never_include_cookie_or_upstream_response_text() -> None:
    def requester(*_args, **_kwargs):
        return FakeResponse(
            {
                "success": False,
                "error": {"errorMessage": "response-secret-canary"},
            }
        )

    with pytest.raises(HmeError) as caught:
        HmeClient(session(), requester=requester).generate_alias()

    assert "session-secret" not in str(caught.value)
    assert "response-secret-canary" not in str(caught.value)


def test_client_maps_body_auth_rejection_to_session_error() -> None:
    with pytest.raises(HmeSessionError):
        HmeClient(
            session(),
            requester=lambda *_args, **_kwargs: FakeResponse(
                {"success": False, "error": {"code": "SESSION_EXPIRED"}}
            ),
        ).list_aliases()


def test_client_routes_hme_requests_through_the_configured_proxy() -> None:
    calls = []

    def requester(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse({"success": True, "result": {"hmeEmails": []}})

    client = HmeClient(
        session(),
        proxy="socks5h://user:pass@proxy.example:1080",
        requester=requester,
    )

    assert client.list_aliases() == []
    assert calls[0][2]["proxies"] == {
        "http": "socks5h://user:pass@proxy.example:1080",
        "https": "socks5h://user:pass@proxy.example:1080",
    }


def test_expired_status_maps_to_session_error() -> None:
    with pytest.raises(HmeSessionError):
        HmeClient(
            session(), requester=lambda *_args, **_kwargs: FakeResponse({}, status_code=421)
        ).list_aliases()


@pytest.mark.parametrize("status", [401, 403, 421])
def test_setup_validate_auth_status_is_session_error(status: int) -> None:
    with pytest.raises(HmeSessionError):
        validate_icloud_setup_session(
            session(),
            requester=lambda *_args, **_kwargs: FakeResponse({}, status_code=status),
        )


def test_setup_validate_merges_rotated_cookies_and_preserves_others() -> None:
    calls = []

    def requester(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse(
            {"dsInfo": {"appleId": "user@example.com"}},
            headers={"Set-Cookie": "X-APPLE-WEBAUTH-TOKEN=rotated-token; Path=/; Secure"},
        )

    refreshed = validate_icloud_setup_session(session(), requester=requester)

    assert "X-APPLE-WEBAUTH-TOKEN=rotated-token" in refreshed.cookie
    assert "X-APPLE-WEBAUTH-USER=user-secret" in refreshed.cookie
    assert refreshed is not session()
    assert calls[0][0] == "POST"
    assert calls[0][1].startswith("https://setup.icloud.com.cn/setup/ws/1/validate?")
    assert calls[0][2]["allow_redirects"] is False
    assert calls[0][2]["data"] == "null"
    assert calls[0][2]["headers"]["Content-Type"] == "application/json"


def test_set_cookie_fallback_splits_combined_headers_without_splitting_expires() -> None:
    merged = merge_set_cookie_headers(
        COOKIE,
        [
            "X-APPLE-WEBAUTH-TOKEN=rotated; Expires=Wed, 21 Oct 2037 07:28:00 GMT; Path=/, "
            "NEW-COOKIE=new-value; Path=/; Secure"
        ],
    )

    assert "X-APPLE-WEBAUTH-TOKEN=rotated" in merged
    assert "NEW-COOKIE=new-value" in merged
    assert "21 Oct" not in merged


def test_setup_validate_retries_network_errors_without_misclassifying_auth() -> None:
    attempts = []
    delays = []

    def requester(*_args, **_kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise requests.ConnectionError("secret upstream detail")
        return FakeResponse({"dsInfo": {"appleId": "user@example.com"}})

    refreshed = validate_icloud_setup_session(
        session(), requester=requester, sleeper=delays.append, jitter=lambda: 0.0
    )

    assert refreshed == session()
    assert len(attempts) == 3
    assert delays == [0.5, 1.0]

    with pytest.raises(HmeNetworkError) as caught:
        validate_icloud_setup_session(
            session(),
            requester=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                requests.ConnectionError("session-secret")
            ),
            sleeper=lambda _delay: None,
            jitter=lambda: 0.0,
        )
    assert not isinstance(caught.value, HmeSessionError)
    assert "session-secret" not in str(caught.value)


def test_setup_validate_stop_event_interrupts_retry_wait() -> None:
    stop_event = threading.Event()
    calls = []

    def requester(*_args, **_kwargs):
        calls.append(1)
        raise requests.ConnectionError("offline")

    stop_event.set()
    with pytest.raises(HmeNetworkError, match="cancelled"):
        validate_icloud_setup_session(session(), requester=requester, stop_event=stop_event)
    assert calls == []


def test_setup_validate_rejects_non_allowlisted_endpoint_without_request() -> None:
    called = []
    with pytest.raises(HmeSessionError):
        validate_icloud_setup_session(
            session(),
            setup_url="https://setup.icloud.com.evil.example/setup/ws/1/validate",
            requester=lambda *_args, **_kwargs: called.append(True),
        )
    assert called == []
