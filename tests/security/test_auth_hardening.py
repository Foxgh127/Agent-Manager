from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from agent_manager.accounts.reauthentication import validate_email
from agent_manager.core.auth import (
    clear_jwt_public_keys,
    load_jwt_public_key,
    parse_jwt_claims,
    validate_jwt_with_signature,
)
from agent_manager.core.credential_store import dpapi_protect, dpapi_unprotect


def _b64(value: object) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@pytest.fixture(autouse=True)
def clear_keys() -> None:
    clear_jwt_public_keys()
    yield
    clear_jwt_public_keys()


def test_hs256_jwt_requires_registered_key_and_rejects_tampering() -> None:
    key = b"unit-test-secret"
    load_jwt_public_key("test", key)
    header = _b64({"alg": "HS256", "kid": "test", "typ": "JWT"})
    payload = _b64({"sub": "user-1", "exp": 4_000_000_000})
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = base64.urlsafe_b64encode(hmac.new(key, signing_input, hashlib.sha256).digest()).decode("ascii").rstrip("=")
    token = f"{header}.{payload}.{signature}"

    assert validate_jwt_with_signature(token, now=1_000_000_000)["sub"] == "user-1"
    with pytest.raises(ValueError, match="signature"):
        validate_jwt_with_signature(token[:-1] + ("a" if token[-1] != "a" else "b"), now=1_000_000_000)


def test_jwt_never_falls_back_for_unknown_key_or_none_algorithm() -> None:
    unsigned = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64({'sub': 'user'})}."
    with pytest.raises(ValueError, match="algorithm"):
        validate_jwt_with_signature(unsigned)
    signed = f"{_b64({'alg': 'HS256', 'kid': 'missing'})}.{_b64({'sub': 'user'})}.abc"
    with pytest.raises(ValueError, match="Unknown JWT key"):
        validate_jwt_with_signature(signed)
    assert parse_jwt_claims(unsigned, verify_signature=False)["sub"] == "user"


def test_dpapi_rejects_invalid_input_before_platform_check() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        dpapi_protect("")
    with pytest.raises(ValueError, match="cannot be empty"):
        dpapi_unprotect(b"")
    with pytest.raises(ValueError, match="too short"):
        dpapi_unprotect(b"short")
    with pytest.raises(ValueError, match="bytes or str"):
        dpapi_protect(123)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="too large"):
        dpapi_protect("x" * (1024 * 1024 + 1))


def test_email_validation_is_bounded_and_syntactic() -> None:
    assert validate_email("user@example.com") is True
    assert validate_email(" test.user+tag@example.co.uk ") is True
    for candidate in ("", "not-an-email", "@example.com", "user@", "user@example", "a" * 250 + "@example.com"):
        assert validate_email(candidate) is False
