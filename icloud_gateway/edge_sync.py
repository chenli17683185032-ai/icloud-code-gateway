from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import requests

from .config import Settings
from .security import validate_access_key


class EdgeSyncError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class EdgeSyncClient:
    """Push alias/token registrations from a local control plane to the cloud edge.

    Edge registrations are email-primary so local and cloud UUIDs can differ safely.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        session: requests.Session | None = None,
        proxy: str | None = None,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        # Local macOS often cannot TLS-direct to Cloudflare; force explicit proxy
        # and ignore ambient HTTP(S)_PROXY so control plane routing is deterministic.
        self.session.trust_env = False
        proxy_url = str(
            proxy
            if proxy is not None
            else settings.edge_proxy or settings.hme_proxy or ""
        ).strip()
        if proxy_url:
            self.session.proxies.update({"http": proxy_url, "https": proxy_url})
        else:
            self.session.proxies.clear()

    def _headers(self) -> dict[str, str]:
        token = str(self.settings.control_plane_token or "").strip()
        if not token:
            raise EdgeSyncError("control plane token is not configured")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "icloud-code-gateway-control/1.0",
        }

    def _url(self, path: str) -> str:
        base = str(self.settings.edge_base_url or "").rstrip("/")
        if not base:
            raise EdgeSyncError("edge base url is not configured")
        if not path.startswith("/"):
            path = "/" + path
        return f"{base}{path}"

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self.session.request(
                method,
                self._url(path),
                headers=self._headers(),
                json=None if payload is None else dict(payload),
                timeout=max(3, int(self.settings.edge_timeout_seconds)),
            )
        except requests.RequestException as exc:
            raise EdgeSyncError(f"edge request failed: {exc}") from exc
        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json()
                if isinstance(body, dict):
                    detail = str(body.get("status") or body.get("detail") or "")
            except ValueError:
                detail = response.text[:200]
            message = detail or f"edge request rejected with HTTP {response.status_code}"
            raise EdgeSyncError(message, status_code=response.status_code)
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise EdgeSyncError("edge response is not JSON") from exc
        if not isinstance(data, dict):
            raise EdgeSyncError("edge response is invalid")
        return data

    @staticmethod
    def _email_path(email: str) -> str:
        value = str(email or "").strip().casefold()
        if not value or "@" not in value:
            raise EdgeSyncError("email is required")
        return quote(value, safe="")

    def upsert_alias(
        self,
        *,
        alias_id: str,
        email: str,
        label: str = "",
        note: str = "",
        sender_filter: str = "",
        state: str = "active",
        access_key: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": str(alias_id),
            "email": str(email).strip().casefold(),
            "label": str(label or email),
            "note": str(note or ""),
            "sender_filter": str(sender_filter or ""),
            "state": str(state or "active"),
        }
        if access_key is not None and str(access_key).strip():
            payload["access_key"] = validate_access_key(str(access_key))
        return self._request("POST", "/control/v1/aliases", payload)

    def issue_access_key(self, *, alias_id: str, email: str, access_key: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/control/v1/aliases/by-email/{self._email_path(email)}/key",
            {"access_key": validate_access_key(access_key), "id": str(alias_id)},
        )

    def revoke_access_key(self, *, email: str) -> dict[str, Any]:
        return self._request(
            "DELETE",
            f"/control/v1/aliases/by-email/{self._email_path(email)}/key",
        )

    def set_alias_state(self, *, email: str, state: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/control/v1/aliases/by-email/{self._email_path(email)}/state",
            {"state": str(state)},
        )

    def delete_alias(self, *, email: str) -> dict[str, Any]:
        return self._request(
            "DELETE",
            f"/control/v1/aliases/by-email/{self._email_path(email)}",
        )


__all__ = ["EdgeSyncClient", "EdgeSyncError"]
