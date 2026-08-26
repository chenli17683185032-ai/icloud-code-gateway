from __future__ import annotations

from dataclasses import dataclass

import requests

from .config import Settings

OPERATOR_SESSION_COOKIE = "__Host-icg_mailbox"


class OperatorSsoError(RuntimeError):
    pass


@dataclass(frozen=True)
class OperatorSessionCookie:
    header_value: str


class OperatorSsoClient:
    """Exchange the server-only operator token for a browser HttpOnly session."""

    def __init__(
        self,
        settings: Settings,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self.session.trust_env = False
        proxy = str(settings.edge_proxy or settings.hme_proxy or "").strip()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        else:
            self.session.proxies.clear()

    def exchange(self) -> OperatorSessionCookie:
        token = str(self.settings.operator_access_token or "").strip()
        if not token:
            raise OperatorSsoError("operator SSO is not configured")
        base = str(self.settings.edge_base_url or self.settings.public_base_url or "").rstrip("/")
        if not base:
            raise OperatorSsoError("operator SSO endpoint is not configured")
        try:
            response = self.session.post(
                f"{base}/api/operator/session",
                json={"token": token},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "icloud-code-gateway-control/1.0",
                },
                timeout=max(3, int(self.settings.edge_timeout_seconds)),
            )
        except requests.RequestException as exc:
            raise OperatorSsoError("operator SSO request failed") from exc
        if response.status_code != 200:
            raise OperatorSsoError("operator SSO request was rejected")
        cookie = str(response.headers.get("Set-Cookie") or "").strip()
        lower = cookie.casefold()
        if (
            not cookie.startswith(f"{OPERATOR_SESSION_COOKIE}=")
            or "\r" in cookie
            or "\n" in cookie
            or "httponly" not in lower
            or "secure" not in lower
            or "path=/" not in lower
            or "samesite=strict" not in lower
        ):
            raise OperatorSsoError("operator SSO response is invalid")
        return OperatorSessionCookie(header_value=cookie)


__all__ = [
    "OPERATOR_SESSION_COOKIE",
    "OperatorSessionCookie",
    "OperatorSsoClient",
    "OperatorSsoError",
]
