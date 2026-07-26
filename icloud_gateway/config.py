from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from pathlib import Path

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
    hme_proxy: str = ""
    public_base_url: str = ""
    trusted_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    log_level: str = "INFO"
    admin_session_seconds: int = 8 * 60 * 60
    capture_timeout_seconds: int = 15 * 60
    otp_max_age_seconds: int = 5 * 60
    otp_future_skew_seconds: int = 60
    otp_request_timeout_seconds: int = 20

    @property
    def database_path(self) -> Path:
        return self.data_dir / "gateway.sqlite3"

    @classmethod
    def from_environment(cls) -> Settings:
        data_dir = Path(
            str(os.environ.get("ICLOUD_GATEWAY_DATA_DIR") or "./data").strip()
        ).expanduser()
        master_key = decode_master_key(_required_environment("ICLOUD_GATEWAY_MASTER_KEY"))
        admin_password = _required_environment("ICLOUD_GATEWAY_ADMIN_PASSWORD")
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
        return cls(
            data_dir=data_dir,
            master_key=master_key,
            admin_password=admin_password,
            cookie_secure=_boolean_environment("ICLOUD_GATEWAY_COOKIE_SECURE", True),
            cdp_url=str(os.environ.get("ICLOUD_GATEWAY_CDP_URL") or "").strip(),
            hme_proxy="" if hme_proxy is None else hme_proxy.requests_url,
            public_base_url=public_base_url,
            trusted_hosts=trusted_hosts,
            log_level=log_level,
        )


__all__ = ["ConfigurationError", "Settings", "decode_master_key"]
