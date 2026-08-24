from __future__ import annotations

import json
import random
import re
import shlex
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

import requests

CORE_SESSION_COOKIE_NAMES = frozenset(
    {
        "X-APPLE-DS-WEB-SESSION-TOKEN",
        "X-APPLE-WEBAUTH-USER",
        "X-APPLE-WEBAUTH-TOKEN",
    }
)
_HME_HOST_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,62})-maildomainws\.icloud\.com(?:\.cn)?$",
    re.IGNORECASE,
)
_HME_PATHS = frozenset(
    {
        "/v2/hme/list",
        "/v1/hme/generate",
        "/v1/hme/reserve",
        "/v1/hme/activate",
        "/v1/hme/deactivate",
        "/v1/hme/delete",
        "/v1/hme/reactivate",
    }
)
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
_SETUP_HOSTS = frozenset({"setup.icloud.com", "setup.icloud.com.cn"})
_SETUP_VALIDATE_PATH = "/setup/ws/1/validate"
_SETUP_AUTH_ERROR_CODES = frozenset(
    {
        "AUTHENTICATION_FAILED",
        "INVALID_AUTH_TOKEN",
        "SESSION_EXPIRED",
        "UNAUTHORIZED",
    }
)


class HmeError(RuntimeError):
    pass


class HmeSessionError(HmeError):
    pass


class HmeNetworkError(HmeError):
    pass


class HmeRateLimitedError(HmeError):
    """Apple rejected an HME write because the account creation quota was hit."""

    def __init__(
        self,
        message: str = "iCloud HME creation is rate limited",
        *,
        code: str = "-41015",
        retry_after_seconds: int = 30 * 60,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "-41015").strip() or "-41015"
        self.retry_after_seconds = max(0, int(retry_after_seconds))


@dataclass(frozen=True)
class ICloudHmeSession:
    host: str
    dsid: str
    client_id: str
    client_build_number: str
    client_mastering_number: str
    cookie: str
    lang_code: str = "en-us"
    origin: str = "https://www.icloud.com"
    referer: str = "https://www.icloud.com/"
    user_agent: str = _DEFAULT_USER_AGENT

    def as_secret_dict(self) -> dict[str, str]:
        return {
            "host": self.host,
            "dsid": self.dsid,
            "client_id": self.client_id,
            "client_build_number": self.client_build_number,
            "client_mastering_number": self.client_mastering_number,
            "cookie": self.cookie,
            "lang_code": self.lang_code,
            "origin": self.origin,
            "referer": self.referer,
            "user_agent": self.user_agent,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ICloudHmeSession:
        try:
            session = cls(
                host=str(value["host"]).strip().lower(),
                dsid=str(value["dsid"]).strip(),
                client_id=str(value["client_id"]).strip(),
                client_build_number=str(value["client_build_number"]).strip(),
                client_mastering_number=str(value["client_mastering_number"]).strip(),
                cookie=str(value["cookie"]).strip(),
                lang_code=str(value.get("lang_code") or "en-us").strip(),
                origin=str(value.get("origin") or "https://www.icloud.com").strip(),
                referer=str(value.get("referer") or "https://www.icloud.com/").strip(),
                user_agent=str(value.get("user_agent") or _DEFAULT_USER_AGENT).strip(),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HmeSessionError("iCloud HME session is incomplete") from exc
        _validate_session(session)
        return session


def parse_hme_session_import(text: str) -> ICloudHmeSession:
    source = str(text or "").strip()
    if not source:
        raise HmeSessionError("paste an iCloud HME cURL command or HAR document")
    try:
        document = json.loads(source)
    except json.JSONDecodeError:
        return _parse_curl(source)
    if not isinstance(document, Mapping) or not isinstance(document.get("log"), Mapping):
        raise HmeSessionError("session import must be a cURL command or HAR document")
    return _parse_har(document)


def parse_hme_request(
    url: str,
    headers: Mapping[str, Any],
    *,
    cookies: Any = None,
) -> ICloudHmeSession:
    if _validated_hme_url(str(url or "")).path != "/v2/hme/list":
        raise HmeSessionError("captured iCloud HME request is not the list endpoint")
    normalized_headers = {
        str(name).strip().casefold(): str(value).strip()
        for name, value in dict(headers or {}).items()
        if str(name).strip() and str(value).strip()
    }
    cookie = normalized_headers.get("cookie", "")
    if not cookie:
        pairs: list[str] = []
        if isinstance(cookies, Mapping) and not {"name", "value"}.issubset(cookies):
            iterable: Any = [{"name": name, "value": value} for name, value in cookies.items()]
        elif isinstance(cookies, Mapping):
            iterable = [cookies]
        else:
            iterable = cookies or ()
        for item in iterable:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "").strip()
            if name:
                pairs.append(f"{name}={value}")
        cookie = "; ".join(pairs)
    normalized_headers["cookie"] = cookie
    return _session_from_request(str(url or ""), cookie, normalized_headers)


def _parse_curl(text: str) -> ICloudHmeSession:
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError as exc:
        raise HmeSessionError("cURL command has invalid quoting") from exc
    if not tokens or tokens[0].rsplit("/", 1)[-1].casefold() not in {"curl", "curl.exe"}:
        raise HmeSessionError("session import is not a cURL command")

    url = ""
    headers: dict[str, str] = {}
    cookie = ""
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"-H", "--header"} and index + 1 < len(tokens):
            index += 1
            _record_header(headers, tokens[index])
        elif token.startswith("--header="):
            _record_header(headers, token.split("=", 1)[1])
        elif token in {"-b", "--cookie"} and index + 1 < len(tokens):
            index += 1
            cookie = tokens[index]
        elif token.startswith("--cookie="):
            cookie = token.split("=", 1)[1]
        elif token == "--url" and index + 1 < len(tokens):
            index += 1
            url = tokens[index]
        elif token.startswith("https://") and not url:
            url = token
        index += 1
    cookie = cookie or headers.get("cookie", "")
    return _session_from_request(url, cookie, headers)


def _parse_har(document: Mapping[str, Any]) -> ICloudHmeSession:
    entries = document.get("log", {}).get("entries", [])
    if not isinstance(entries, list):
        raise HmeSessionError("HAR log.entries must be a list")
    fallback: Mapping[str, Any] | None = None
    selected: Mapping[str, Any] | None = None
    for entry in entries:
        request = entry.get("request") if isinstance(entry, Mapping) else None
        if not isinstance(request, Mapping):
            continue
        try:
            parsed = _validated_hme_url(str(request.get("url") or ""))
        except HmeSessionError:
            continue
        fallback = fallback or request
        if parsed.path == "/v2/hme/list":
            selected = request
            break
    selected = selected or fallback
    if selected is None:
        raise HmeSessionError("HAR contains no iCloud HME request")

    headers: dict[str, str] = {}
    for item in selected.get("headers", []):
        if isinstance(item, Mapping):
            name = str(item.get("name") or "").strip().casefold()
            value = str(item.get("value") or "").strip()
            if name and value:
                headers[name] = value
    cookie = headers.get("cookie", "")
    if not cookie:
        pairs = []
        for item in selected.get("cookies", []):
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "").strip()
            if name:
                pairs.append(f"{name}={value}")
        cookie = "; ".join(pairs)
    return _session_from_request(str(selected.get("url") or ""), cookie, headers)


def _record_header(headers: dict[str, str], raw: str) -> None:
    name, separator, value = str(raw or "").partition(":")
    if separator and name.strip() and value.strip():
        headers[name.strip().casefold()] = value.strip()


def _session_from_request(url: str, cookie: str, headers: Mapping[str, str]) -> ICloudHmeSession:
    parsed = _validated_hme_url(url)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

    def query_value(name: str) -> str:
        values = query.get(name)
        value = str(values[0]).strip() if values else ""
        if not value:
            raise HmeSessionError(f"iCloud HME request is missing {name}")
        return value

    origin = str(headers.get("origin") or _default_origin(parsed.hostname or "")).strip()
    referer = str(headers.get("referer") or f"{origin}/").strip()
    user_agent = str(headers.get("user-agent") or _DEFAULT_USER_AGENT).strip()
    session = ICloudHmeSession(
        host=str(parsed.hostname or "").lower(),
        dsid=query_value("dsid"),
        client_id=query_value("clientId"),
        client_build_number=query_value("clientBuildNumber"),
        client_mastering_number=query_value("clientMasteringNumber"),
        cookie=str(cookie or "").strip(),
        origin=origin,
        referer=referer,
        user_agent=user_agent,
    )
    _validate_session(session)
    return session


def _validated_hme_url(value: str) -> urllib.parse.ParseResult:
    try:
        parsed = urllib.parse.urlparse(str(value or "").strip())
        port = parsed.port
    except ValueError as exc:
        raise HmeSessionError("iCloud HME request URL is invalid") from exc
    host = str(parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not _HME_HOST_RE.fullmatch(host)
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise HmeSessionError("iCloud HME request host is not allowed")
    if parsed.path not in _HME_PATHS:
        raise HmeSessionError("iCloud HME request path is not allowed")
    return parsed


def _default_origin(host: str) -> str:
    return (
        "https://www.icloud.com.cn" if host.endswith(".icloud.com.cn") else "https://www.icloud.com"
    )


def _validate_session(session: ICloudHmeSession) -> None:
    _validated_hme_url(f"https://{session.host}/v2/hme/list")
    for name, value in (
        ("dsid", session.dsid),
        ("client_id", session.client_id),
        ("client_build_number", session.client_build_number),
        ("client_mastering_number", session.client_mastering_number),
        ("cookie", session.cookie),
        ("origin", session.origin),
        ("referer", session.referer),
        ("user_agent", session.user_agent),
    ):
        if not value or "\r" in value or "\n" in value:
            raise HmeSessionError(f"iCloud HME session {name} is invalid")
    cookie_names = {
        part.partition("=")[0].strip()
        for part in session.cookie.split(";")
        if part.partition("=")[0].strip()
    }
    if CORE_SESSION_COOKIE_NAMES - cookie_names:
        raise HmeSessionError("iCloud HME session Cookie is incomplete")
    expected_origin = _default_origin(session.host)
    if session.origin.rstrip("/") != expected_origin:
        raise HmeSessionError("iCloud HME session Origin is invalid")
    if not session.referer.startswith(f"{expected_origin}/"):
        raise HmeSessionError("iCloud HME session Referer is invalid")


Requester = Callable[..., Any]
Sleeper = Callable[[float], Any]
Jitter = Callable[[], float]


def _cookie_pairs(cookie_header: str) -> tuple[list[str], dict[str, str]]:
    order: list[str] = []
    values: dict[str, str] = {}
    for part in str(cookie_header or "").split(";"):
        name, separator, value = part.partition("=")
        name = name.strip()
        if not separator or not name or "\r" in name or "\n" in name:
            continue
        if name not in values:
            order.append(name)
        values[name] = value.strip()
    return order, values


def merge_set_cookie_headers(cookie_header: str, set_cookie_headers: list[str]) -> str:
    order, values = _cookie_pairs(cookie_header)
    for header in set_cookie_headers:
        combined = re.split(
            r",(?=\s*[!#$%&'*+\-.^_`|~0-9A-Za-z]+=)",
            str(header or ""),
        )
        for cookie in combined:
            first = cookie.split(";", 1)[0]
            name, separator, value = first.partition("=")
            name = name.strip()
            if (
                not separator
                or not name
                or "\r" in name
                or "\n" in name
                or "\r" in value
                or "\n" in value
            ):
                continue
            if name not in values:
                order.append(name)
            values[name] = value.strip()
    return "; ".join(f"{name}={values[name]}" for name in order)


def _set_cookie_headers(response: Any) -> list[str]:
    raw_headers = getattr(getattr(response, "raw", None), "headers", None)
    if raw_headers is not None:
        getter = getattr(raw_headers, "getlist", None) or getattr(raw_headers, "get_all", None)
        if callable(getter):
            values = getter("Set-Cookie")
            if values:
                return [str(value) for value in values]
    headers = getattr(response, "headers", None)
    if isinstance(headers, Mapping):
        value = headers.get("Set-Cookie") or headers.get("set-cookie")
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value]
        if value:
            return [str(value)]
    return []


def _validated_setup_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlparse(str(value or "").strip())
        port = parsed.port
    except ValueError as exc:
        raise HmeSessionError("iCloud setup validate URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or str(parsed.hostname or "").casefold() not in _SETUP_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path != _SETUP_VALIDATE_PATH
        or parsed.params
        or parsed.fragment
    ):
        raise HmeSessionError("iCloud setup validate URL is not allowed")
    return urllib.parse.urlunparse(
        ("https", str(parsed.hostname).casefold(), parsed.path, "", "", "")
    )


def _setup_auth_rejected(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False
    candidates: list[Any] = [payload.get("errorCode"), payload.get("code")]
    error = payload.get("error")
    if isinstance(error, Mapping):
        candidates.extend((error.get("errorCode"), error.get("code")))
    return any(str(value or "").strip().upper() in _SETUP_AUTH_ERROR_CODES for value in candidates)


def validate_icloud_setup_session(
    session: ICloudHmeSession,
    *,
    proxy: str = "",
    timeout: float = 30.0,
    requester: Requester | None = None,
    attempts: int = 3,
    sleeper: Sleeper = time.sleep,
    jitter: Jitter = random.random,
    setup_url: str | None = None,
    stop_event: Any = None,
) -> ICloudHmeSession:
    """Run Apple's idempotent setup validate and return a rotated immutable session."""
    _validate_session(session)
    if stop_event is not None and stop_event.is_set():
        raise HmeNetworkError("iCloud setup validate was cancelled")
    host = "setup.icloud.com.cn" if session.host.endswith(".icloud.com.cn") else "setup.icloud.com"
    endpoint = _validated_setup_url(setup_url or f"https://{host}{_SETUP_VALIDATE_PATH}")
    query = urllib.parse.urlencode(
        {
            "clientBuildNumber": session.client_build_number,
            "clientMasteringNumber": session.client_mastering_number,
            "clientId": session.client_id,
        }
    )
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": session.origin,
        "Referer": session.referer,
        "User-Agent": session.user_agent,
        "Cookie": session.cookie,
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    http: requests.Session | None = None
    request = requester
    if request is None:
        http = requests.Session()
        http.trust_env = False
        request = http.request
    try:
        response: Any = None
        bounded_attempts = max(1, min(int(attempts), 5))
        for attempt in range(bounded_attempts):
            try:
                response = request(
                    "POST",
                    f"{endpoint}?{query}",
                    headers=headers,
                    data="null",
                    proxies=proxies,
                    timeout=max(1.0, min(float(timeout), 60.0)),
                    allow_redirects=False,
                )
                break
            except (requests.RequestException, OSError, TimeoutError) as exc:
                if attempt + 1 >= bounded_attempts:
                    raise HmeNetworkError("iCloud setup validate network request failed") from exc
                delay = min(8.0, 0.5 * (2**attempt)) + max(0.0, min(float(jitter()), 1.0)) * 0.25
                if stop_event is not None:
                    if stop_event.wait(delay):
                        raise HmeNetworkError("iCloud setup validate was cancelled") from exc
                else:
                    sleeper(delay)
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code in {401, 403, 421}:
            raise HmeSessionError("iCloud setup validate rejected the session")
        if 300 <= status_code < 400:
            raise HmeError("iCloud setup validate returned a redirect")
        if status_code < 200 or status_code >= 300:
            raise HmeError(f"iCloud setup validate returned HTTP {status_code or 'unknown'}")
        try:
            body = response.json()
        except (TypeError, ValueError) as exc:
            raise HmeError("iCloud setup validate response is not JSON") from exc
        if _setup_auth_rejected(body):
            raise HmeSessionError("iCloud setup validate rejected the session")
        if not isinstance(body, Mapping):
            raise HmeError("iCloud setup validate response is invalid")
        ds_info = body.get("dsInfo")
        if not isinstance(ds_info, Mapping) or not str(ds_info.get("appleId") or "").strip():
            raise HmeError("iCloud setup validate response is invalid")
        cookie = merge_set_cookie_headers(session.cookie, _set_cookie_headers(response))
        refreshed = replace(session, cookie=cookie)
        _validate_session(refreshed)
        return refreshed
    finally:
        if http is not None:
            http.close()


class HmeClient:
    def __init__(
        self,
        session: ICloudHmeSession,
        *,
        proxy: str = "",
        timeout: float = 20.0,
        requester: Requester | None = None,
    ) -> None:
        _validate_session(session)
        self.session = session
        self.proxy = str(proxy or "").strip()
        self.timeout = max(1.0, min(float(timeout), 60.0))
        self._http: requests.Session | None = None
        if requester is None:
            self._http = requests.Session()
            self._http.trust_env = False
            self.requester = self._http.request
        else:
            self.requester = requester

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def __enter__(self) -> HmeClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def list_settings(self, *, start_from: int | None = None) -> dict[str, Any]:
        query = None
        if start_from is not None and int(start_from) > 0:
            query = {"startFrom": str(int(start_from))}
        response = self._request("GET", "/v2/hme/list", query=query)
        result = response.get("result")
        if not isinstance(result, dict):
            raise HmeError("iCloud HME list response is invalid")
        return result

    def list_aliases(self) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        start_from = 0
        for _ in range(50):
            result = self.list_settings(start_from=start_from or None)
            page = _hme_email_page(result)
            added = 0
            for item in page:
                remote_id = str(item.get("anonymousId") or "").strip()
                key = (
                    remote_id or str(item.get("hme") or item.get("email") or "").strip().casefold()
                )
                if not key or key in seen_ids:
                    continue
                seen_ids.add(key)
                collected.append(dict(item))
                added += 1
            if added == 0:
                break
            next_from = _optional_int(result.get("startFrom"))
            has_more = result.get("hasMore")
            if has_more is False:
                break
            if next_from is not None and next_from > start_from:
                start_from = next_from
                continue
            if has_more is True or len(page) >= 100:
                start_from = start_from + len(page)
                continue
            break
        return collected

    def generate_alias(self) -> str:
        response = self._request("POST", "/v1/hme/generate", {"langCode": self.session.lang_code})
        value = (response.get("result") or {}).get("hme")
        email = str(value or "").strip().casefold()
        if email.count("@") != 1:
            raise HmeError("iCloud HME generate response is invalid")
        return email

    def reserve_alias(self, email: str, *, label: str, note: str = "") -> dict[str, Any]:
        response = self._request(
            "POST",
            "/v1/hme/reserve",
            {
                "hme": str(email).strip(),
                "label": str(label).strip(),
                "note": str(note).strip(),
            },
        )
        alias = (response.get("result") or {}).get("hme")
        if not isinstance(alias, Mapping):
            raise HmeError("iCloud HME reserve response is invalid")
        return dict(alias)

    def create_alias(self, *, label: str, note: str = "") -> dict[str, Any]:
        candidate = self.generate_alias()
        return self.reserve_alias(candidate, label=label, note=note)

    def deactivate_alias(self, anonymous_id: str) -> dict[str, Any]:
        return self._change_alias("/v1/hme/deactivate", anonymous_id)

    def reactivate_alias(self, anonymous_id: str) -> dict[str, Any]:
        return self._change_alias("/v1/hme/reactivate", anonymous_id)

    def delete_alias(self, anonymous_id: str) -> dict[str, Any]:
        return self._change_alias("/v1/hme/delete", anonymous_id)

    def _change_alias(self, path: str, anonymous_id: str) -> dict[str, Any]:
        remote_id = str(anonymous_id or "").strip()
        if not remote_id or len(remote_id) > 256 or "\r" in remote_id or "\n" in remote_id:
            raise ValueError("iCloud HME anonymous ID is invalid")
        response = self._request("POST", path, {"anonymousId": remote_id})
        result = response.get("result")
        return dict(result) if isinstance(result, Mapping) else {}

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        query: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        params = {
            "clientBuildNumber": self.session.client_build_number,
            "clientMasteringNumber": self.session.client_mastering_number,
            "clientId": self.session.client_id,
            "dsid": self.session.dsid,
        }
        if query:
            params.update({str(key): str(value) for key, value in query.items() if str(value)})
        params = urllib.parse.urlencode(params)
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        try:
            response = self.requester(
                method,
                f"https://{self.session.host}{path}?{params}",
                headers=self._headers(),
                data=self._payload(payload),
                proxies=proxies,
                timeout=self.timeout,
            )
        except (requests.RequestException, OSError, TimeoutError) as exc:
            raise HmeNetworkError("iCloud HME network request failed") from exc
        rotated_cookie = merge_set_cookie_headers(
            self.session.cookie, _set_cookie_headers(response)
        )
        if rotated_cookie != self.session.cookie:
            self.session = replace(self.session, cookie=rotated_cookie)
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code in {401, 403, 421}:
            raise HmeSessionError("iCloud HME session is expired or rejected")
        if status_code < 200 or status_code >= 300:
            raise HmeError(f"iCloud HME request returned HTTP {status_code or 'unknown'}")
        try:
            body = response.json()
        except (TypeError, ValueError) as exc:
            raise HmeError("iCloud HME response is not JSON") from exc
        if not isinstance(body, dict):
            raise HmeError("iCloud HME response is invalid")
        if body.get("success") is not True:
            code = _safe_error_code(body)
            if code.upper() in _SETUP_AUTH_ERROR_CODES:
                raise HmeSessionError("iCloud HME session is expired or rejected")
            if _is_rate_limited_code(code):
                raise HmeRateLimitedError(
                    f"iCloud HME creation is rate limited ({code or '-41015'})",
                    code=code or "-41015",
                )
            suffix = f" ({code})" if code else ""
            raise HmeError(f"iCloud HME rejected the request{suffix}")
        return body

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "*/*",
            "Content-Type": "text/plain",
            "Cookie": self.session.cookie,
            "Origin": self.session.origin,
            "Referer": self.session.referer,
            "User-Agent": self.session.user_agent,
        }

    @staticmethod
    def _payload(payload: Mapping[str, Any] | None) -> str | None:
        return (
            json.dumps(dict(payload), ensure_ascii=True, separators=(",", ":"))
            if payload is not None
            else None
        )


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _hme_email_page(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    aliases = result.get("hmeEmails")
    if aliases is None:
        aliases = []
    if not isinstance(aliases, list):
        raise HmeError("iCloud HME alias list is invalid")
    if any(not isinstance(item, Mapping) for item in aliases):
        raise HmeError("iCloud HME alias list is incomplete")
    return [item for item in aliases if isinstance(item, Mapping)]


def _safe_error_code(payload: Mapping[str, Any]) -> str:
    candidates: list[Any] = [payload.get("errorCode"), payload.get("code")]
    error = payload.get("error")
    if isinstance(error, Mapping):
        candidates.extend((error.get("errorCode"), error.get("code")))
    for value in candidates:
        text = str(value or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", text):
            return text
    return ""


def _is_rate_limited_code(code: str) -> bool:
    text = str(code or "").strip()
    if not text:
        return False
    # Community / Apple private API: -41015 is the observed Hide My Email throttle.
    return text.lstrip("-") == "41015"


__all__ = [
    "CORE_SESSION_COOKIE_NAMES",
    "HmeClient",
    "HmeError",
    "HmeNetworkError",
    "HmeRateLimitedError",
    "HmeSessionError",
    "ICloudHmeSession",
    "merge_set_cookie_headers",
    "parse_hme_request",
    "parse_hme_session_import",
    "validate_icloud_setup_session",
]
