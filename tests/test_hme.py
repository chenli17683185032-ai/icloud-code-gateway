from __future__ import annotations

import json

import pytest

from icloud_gateway.hme import (
    HmeClient,
    HmeError,
    HmeSessionError,
    ICloudHmeSession,
    parse_hme_request,
    parse_hme_session_import,
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
    def __init__(self, body, *, status_code=200):
        self.body = body
        self.status_code = status_code

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
