from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_ENVELOPE_MAGIC = b"ICG1"
_NONCE_SIZE = 12
_ACCESS_KEY_RE = re.compile(r"^icg_[A-Za-z0-9_-]{43}$")


class SecurityError(RuntimeError):
    pass


class InvalidSessionError(SecurityError):
    pass


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    text = str(value or "").strip()
    padding = "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode((text + padding).encode("ascii"))


def derive_key(master_key: bytes, purpose: str) -> bytes:
    if len(master_key) != 32:
        raise ValueError("master_key must contain 32 bytes")
    if not purpose:
        raise ValueError("purpose is required")
    return hmac.new(master_key, purpose.encode("utf-8"), hashlib.sha256).digest()


class SecretBox:
    def __init__(self, master_key: bytes) -> None:
        if len(master_key) != 32:
            raise ValueError("master_key must contain 32 bytes")
        self._master_key = bytes(master_key)

    def encrypt(self, plaintext: bytes, purpose: str) -> bytes:
        if not isinstance(plaintext, bytes):
            raise TypeError("plaintext must be bytes")
        nonce = secrets.token_bytes(_NONCE_SIZE)
        ciphertext = AESGCM(self._master_key).encrypt(
            nonce,
            plaintext,
            purpose.encode("utf-8"),
        )
        return _ENVELOPE_MAGIC + nonce + ciphertext

    def decrypt(self, envelope: bytes, purpose: str) -> bytes:
        if not isinstance(envelope, bytes):
            raise TypeError("envelope must be bytes")
        minimum = len(_ENVELOPE_MAGIC) + _NONCE_SIZE + 16
        if len(envelope) < minimum or not envelope.startswith(_ENVELOPE_MAGIC):
            raise SecurityError("encrypted value is invalid")
        nonce_start = len(_ENVELOPE_MAGIC)
        nonce_end = nonce_start + _NONCE_SIZE
        try:
            return AESGCM(self._master_key).decrypt(
                envelope[nonce_start:nonce_end],
                envelope[nonce_end:],
                purpose.encode("utf-8"),
            )
        except Exception:
            raise SecurityError("encrypted value cannot be decrypted") from None

    def digest(self, value: str, purpose: str) -> bytes:
        return hmac.new(
            derive_key(self._master_key, purpose),
            str(value).encode("utf-8"),
            hashlib.sha256,
        ).digest()


def generate_access_key() -> str:
    return f"icg_{_b64encode(secrets.token_bytes(32))}"


def validate_access_key(value: str) -> str:
    key = str(value or "").strip()
    if not _ACCESS_KEY_RE.fullmatch(key):
        raise SecurityError("access key is invalid")
    return key


def hash_access_key(value: str) -> bytes:
    key = validate_access_key(value)
    return hashlib.sha256(key.encode("ascii")).digest()


@dataclass(frozen=True)
class AdminSession:
    expires_at: int
    csrf_token: str


class AdminSessionCodec:
    def __init__(self, master_key: bytes, *, lifetime_seconds: int) -> None:
        self._signing_key = derive_key(master_key, "admin-session")
        self._lifetime_seconds = max(60, int(lifetime_seconds))

    def issue(self, *, now: int | None = None) -> tuple[str, AdminSession]:
        issued_at = int(time.time() if now is None else now)
        session = AdminSession(
            expires_at=issued_at + self._lifetime_seconds,
            csrf_token=_b64encode(secrets.token_bytes(24)),
        )
        payload = {
            "v": 1,
            "iat": issued_at,
            "exp": session.expires_at,
            "csrf": session.csrf_token,
            "nonce": _b64encode(secrets.token_bytes(12)),
        }
        encoded = _b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signature = _b64encode(
            hmac.new(self._signing_key, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        return f"{encoded}.{signature}", session

    def decode(self, token: str, *, now: int | None = None) -> AdminSession:
        encoded, separator, signature = str(token or "").partition(".")
        if not separator or not encoded or not signature:
            raise InvalidSessionError("admin session is invalid")
        expected = hmac.new(self._signing_key, encoded.encode("ascii"), hashlib.sha256).digest()
        try:
            supplied = _b64decode(signature)
        except Exception:
            raise InvalidSessionError("admin session is invalid") from None
        if not hmac.compare_digest(expected, supplied):
            raise InvalidSessionError("admin session is invalid")
        try:
            payload: Mapping[str, Any] = json.loads(_b64decode(encoded))
            version = int(payload.get("v") or 0)
            expires_at = int(payload.get("exp") or 0)
            csrf_token = str(payload.get("csrf") or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            raise InvalidSessionError("admin session is invalid") from None
        current = int(time.time() if now is None else now)
        if version != 1 or expires_at <= current or not csrf_token:
            raise InvalidSessionError("admin session is expired")
        return AdminSession(expires_at=expires_at, csrf_token=csrf_token)


def verify_admin_password(expected: str, supplied: str) -> bool:
    # compare_digest rejects str operands that are not pure ASCII, so a non-ASCII
    # admin password would raise instead of returning False.
    return hmac.compare_digest(
        str(expected).encode("utf-8"),
        str(supplied or "").encode("utf-8"),
    )


__all__ = [
    "AdminSession",
    "AdminSessionCodec",
    "InvalidSessionError",
    "SecretBox",
    "SecurityError",
    "derive_key",
    "generate_access_key",
    "hash_access_key",
    "validate_access_key",
    "verify_admin_password",
]
