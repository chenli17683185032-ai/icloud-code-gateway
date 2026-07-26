from __future__ import annotations

import base64

import pytest

from icloud_gateway.config import ConfigurationError, decode_master_key
from icloud_gateway.security import (
    AdminSessionCodec,
    InvalidSessionError,
    SecretBox,
    SecurityError,
    generate_access_key,
    hash_access_key,
    validate_access_key,
    verify_admin_password,
)

MASTER_KEY = bytes(range(32))


def test_master_key_requires_exactly_32_decoded_bytes() -> None:
    encoded = base64.urlsafe_b64encode(MASTER_KEY).decode("ascii")

    assert decode_master_key(encoded) == MASTER_KEY
    with pytest.raises(ConfigurationError):
        decode_master_key(base64.urlsafe_b64encode(b"short").decode("ascii"))


def test_secret_box_binds_ciphertext_to_its_purpose() -> None:
    box = SecretBox(MASTER_KEY)
    envelope = box.encrypt(b"sensitive", "imap-config")

    assert box.decrypt(envelope, "imap-config") == b"sensitive"
    assert b"sensitive" not in envelope
    with pytest.raises(SecurityError):
        box.decrypt(envelope, "hme-session")


def test_access_keys_are_high_entropy_and_validate_before_hashing() -> None:
    first = generate_access_key()
    second = generate_access_key()

    assert first.startswith("icg_")
    assert len(first) == 47
    assert first != second
    assert validate_access_key(first) == first
    assert hash_access_key(first) != hash_access_key(second)
    with pytest.raises(SecurityError):
        hash_access_key("short")


def test_admin_session_is_signed_expiring_and_contains_csrf() -> None:
    codec = AdminSessionCodec(MASTER_KEY, lifetime_seconds=120)
    token, issued = codec.issue(now=1_000)

    decoded = codec.decode(token, now=1_050)

    assert decoded == issued
    assert decoded.csrf_token
    with pytest.raises(InvalidSessionError):
        codec.decode(token, now=1_121)
    encoded, signature = token.split(".", 1)
    with pytest.raises(InvalidSessionError):
        codec.decode(f"{encoded}x.{signature}", now=1_050)


def test_admin_password_comparison_has_exact_semantics() -> None:
    assert verify_admin_password("correct horse battery staple", "correct horse battery staple")
    assert not verify_admin_password(
        "correct horse battery staple", "correct horse battery stapler"
    )
