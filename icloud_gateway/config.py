from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .proxy import ProxyConfigurationError, proxy_from_environment


class ConfigurationError(RuntimeError):
    pass


def _required_environment(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required")
    return value


def _boolean_environment(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = str(raw).strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean")


def _integer_environment(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def decode_master_key(value: str) -> bytes:
    try:
        decoded = base64.urlsafe_b64decode(str(value).strip().encode("ascii"))
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise ConfigurationError("ICLOUD_GATEWAY_MASTER_KEY is invalid") from exc
    if len(decoded) != 32:
        raise ConfigurationError("ICLOUD_GATEWAY_MASTER_KEY must decode to 32 bytes")
    return decoded


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    master_key: bytes
    admin_password: str
    cookie_secure: bool = True
    cdp_url: str = ""
    browser_profile_dir: Path | None = None
    hme_proxy: str = ""
    public_base_url: str = ""
    trusted_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    log_level: str = "INFO"
    admin_session_seconds: int = 30 * 24 * 60 * 60
    capture_timeout_seconds: int = 15 * 60
    otp_max_age_seconds: int = 5 * 60
    otp_future_skew_seconds: int = 60
    otp_request_timeout_seconds: int = 20
    hme_maintenance_interval_seconds: int = 6 * 60 * 60
    hme_freshness_seconds: int = 60 * 60
    hme_retry_max_seconds: int = 60 * 60
    alias_batch_limit: int = 100
    # After Apple HME returns -41015, wait this long before continuing create jobs.
    # Matches community tooling (hidemyemail-generator): about 30 minutes.
    # 0 means no extra cooldown; keep retrying immediately with the normal throttle.
    hme_create_cooldown_seconds: int = 30 * 60
    deployment_mode: Literal["full", "control", "edge"] = "full"
    control_plane_token: str = ""
    edge_base_url: str = ""
    edge_proxy: str = ""
    operator_access_token: str = ""
    edge_sync_enabled: bool = True
    edge_timeout_seconds: int = 20
    # Local control can upload a newly captured Apple session to the remote
    # control server. Public/server deployments leave this disabled to avoid
    # recursively posting a session back to themselves.
    hme_session_upload_enabled: bool = False
    # Control mode: periodically reconcile all active aliases/keys to the edge so a
    # missed creation-time push heals itself. 0 disables the background loop.
    edge_reconcile_seconds: int = 30 * 60
    # Local-only convenience: skip admin password UI/auth. Must never be enabled on
    # public edge/full deployments.
    admin_open: bool = False

    @property
    def database_path(self) -> Path:
        return self.data_dir / "gateway.sqlite3"

    @property
    def capture_configured(self) -> bool:
        """True when capture can run via remote CDP or a local persistent profile."""
        return bool(self.cdp_url) or self.browser_profile_dir is not None

    @classmethod
    def from_environment(cls) -> Settings:
        data_dir = Path(
            str(os.environ.get("ICLOUD_GATEWAY_DATA_DIR") or "./data").strip()
        ).expanduser()
        master_key = decode_master_key(_required_environment("ICLOUD_GATEWAY_MASTER_KEY"))
        admin_open = _boolean_environment("ICLOUD_GATEWAY_ADMIN_OPEN", False)
        admin_password = str(os.environ.get("ICLOUD_GATEWAY_ADMIN_PASSWORD") or "").strip()
        if admin_open:
            # Password is unused in open-local mode; keep empty or pass-through for tests.
            pass
        else:
            if not admin_password:
                raise ConfigurationError("ICLOUD_GATEWAY_ADMIN_PASSWORD is required")
            if len(admin_password) < 16:
                raise ConfigurationError(
                    "ICLOUD_GATEWAY_ADMIN_PASSWORD must contain at least 16 characters"
                )
        trusted_hosts = tuple(
            value.strip()
            for value in str(
                os.environ.get("ICLOUD_GATEWAY_TRUSTED_HOSTS") or "localhost,127.0.0.1"
            ).split(",")
            if value.strip()
        )
        if not trusted_hosts:
            raise ConfigurationError("ICLOUD_GATEWAY_TRUSTED_HOSTS is empty")
        public_base_url = (
            str(os.environ.get("ICLOUD_GATEWAY_PUBLIC_BASE_URL") or "").strip().rstrip("/")
        )
        if public_base_url and not public_base_url.startswith(("https://", "http://")):
            raise ConfigurationError("ICLOUD_GATEWAY_PUBLIC_BASE_URL is invalid")
        log_level = str(os.environ.get("ICLOUD_GATEWAY_LOG_LEVEL") or "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("ICLOUD_GATEWAY_LOG_LEVEL is invalid")
        try:
            hme_proxy = proxy_from_environment("ICLOUD_GATEWAY_HME_PROXY")
        except ProxyConfigurationError as exc:
            raise ConfigurationError("ICLOUD_GATEWAY_HME_PROXY is invalid") from exc
        try:
            edge_proxy = proxy_from_environment("ICLOUD_GATEWAY_EDGE_PROXY")
        except ProxyConfigurationError as exc:
            raise ConfigurationError("ICLOUD_GATEWAY_EDGE_PROXY is invalid") from exc
        mode = str(os.environ.get("ICLOUD_GATEWAY_DEPLOYMENT_MODE") or "full").strip().casefold()
        if mode not in {"full", "control", "edge"}:
            raise ConfigurationError(
                "ICLOUD_GATEWAY_DEPLOYMENT_MODE must be full, control, or edge"
            )
        control_plane_token = str(
            os.environ.get("ICLOUD_GATEWAY_CONTROL_PLANE_TOKEN") or ""
        ).strip()
        if mode in {"control", "edge"} and len(control_plane_token) < 24:
            raise ConfigurationError(
                "ICLOUD_GATEWAY_CONTROL_PLANE_TOKEN must contain at least 24 characters "
                "when deployment mode is control or edge"
            )
        edge_base_url = (
            str(os.environ.get("ICLOUD_GATEWAY_EDGE_BASE_URL") or "").strip().rstrip("/")
        )
        operator_access_token = str(
            os.environ.get("ICLOUD_GATEWAY_OPERATOR_ACCESS_TOKEN") or ""
        ).strip()
        if operator_access_token and (
            len(operator_access_token) != 47 or not operator_access_token.startswith("icg_")
        ):
            raise ConfigurationError("ICLOUD_GATEWAY_OPERATOR_ACCESS_TOKEN is invalid")
        if mode == "control":
            if not edge_base_url:
                raise ConfigurationError("ICLOUD_GATEWAY_EDGE_BASE_URL is required in control mode")
            if not edge_base_url.startswith(("https://", "http://")):
                raise ConfigurationError("ICLOUD_GATEWAY_EDGE_BASE_URL is invalid")
        elif edge_base_url and not edge_base_url.startswith(("https://", "http://")):
            raise ConfigurationError("ICLOUD_GATEWAY_EDGE_BASE_URL is invalid")
        profile_raw = str(os.environ.get("ICLOUD_GATEWAY_BROWSER_PROFILE_DIR") or "").strip()
        browser_profile_dir = Path(profile_raw).expanduser() if profile_raw else None
        cdp_url = str(os.environ.get("ICLOUD_GATEWAY_CDP_URL") or "").strip()
        if mode == "edge":
            # Cloud edge is IMAP + access keys only. Chromium stays on the
            # local control plane even if a leftover CDP URL is still in .env.
            cdp_url = ""
            browser_profile_dir = None
        return cls(
            data_dir=data_dir,
            master_key=master_key,
            admin_password=admin_password,
            cookie_secure=_boolean_environment("ICLOUD_GATEWAY_COOKIE_SECURE", True),
            cdp_url=cdp_url,
            browser_profile_dir=browser_profile_dir,
            hme_proxy="" if hme_proxy is None else hme_proxy.requests_url,
            public_base_url=public_base_url,
            trusted_hosts=trusted_hosts,
            log_level=log_level,
            admin_session_seconds=_integer_environment(
                "ICLOUD_GATEWAY_ADMIN_SESSION_SECONDS",
                30 * 24 * 60 * 60,
                minimum=60 * 60,
                maximum=90 * 24 * 60 * 60,
            ),
            hme_maintenance_interval_seconds=_integer_environment(
                "ICLOUD_GATEWAY_HME_MAINTENANCE_SECONDS",
                6 * 60 * 60,
                minimum=300,
                maximum=7 * 24 * 60 * 60,
            ),
            hme_freshness_seconds=_integer_environment(
                "ICLOUD_GATEWAY_HME_FRESHNESS_SECONDS",
                60 * 60,
                minimum=300,
                maximum=24 * 60 * 60,
            ),
            hme_retry_max_seconds=_integer_environment(
                "ICLOUD_GATEWAY_HME_RETRY_MAX_SECONDS",
                60 * 60,
                minimum=300,
                maximum=24 * 60 * 60,
            ),
            alias_batch_limit=_integer_environment(
                "ICLOUD_GATEWAY_ALIAS_BATCH_LIMIT", 100, minimum=1, maximum=100
            ),
            hme_create_cooldown_seconds=_integer_environment(
                "ICLOUD_GATEWAY_HME_CREATE_COOLDOWN_SECONDS",
                30 * 60,
                minimum=0,
                maximum=6 * 60 * 60,
            ),
            deployment_mode=mode,  # type: ignore[arg-type]
            control_plane_token=control_plane_token,
            edge_base_url=edge_base_url,
            edge_proxy="" if edge_proxy is None else edge_proxy.requests_url,
            operator_access_token=operator_access_token,
            edge_sync_enabled=_boolean_environment("ICLOUD_GATEWAY_EDGE_SYNC_ENABLED", True),
            edge_timeout_seconds=_integer_environment(
                "ICLOUD_GATEWAY_EDGE_TIMEOUT_SECONDS", 20, minimum=3, maximum=120
            ),
            hme_session_upload_enabled=_boolean_environment(
                "ICLOUD_GATEWAY_HME_SESSION_UPLOAD_ENABLED", False
            ),
            edge_reconcile_seconds=_integer_environment(
                "ICLOUD_GATEWAY_EDGE_RECONCILE_SECONDS",
                30 * 60,
                minimum=0,
                maximum=24 * 60 * 60,
            ),
            admin_open=admin_open,
        )

    @property
    def is_edge(self) -> bool:
        return self.deployment_mode == "edge"

    @property
    def is_control(self) -> bool:
        return self.deployment_mode == "control"

    @property
    def manages_hme(self) -> bool:
        return self.deployment_mode in {"full", "control"}

    @property
    def serves_public_otp(self) -> bool:
        return self.deployment_mode in {"full", "edge"}


__all__ = ["ConfigurationError", "Settings", "decode_master_key"]
