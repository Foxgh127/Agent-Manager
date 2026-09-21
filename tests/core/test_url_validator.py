"""Focused tests for the shared URL validation helpers."""

from __future__ import annotations

import pytest

from agent_manager.core.url_validator import (
    URLValidationError,
    normalize_url,
    validate_provider_portal_url,
    validate_provider_url,
    validate_redirect_url,
    validate_url,
    validate_webhook_url,
)


def test_valid_urls_are_returned_unchanged() -> None:
    for url in (
        "https://example.com",
        "http://localhost:3000",
        "https://[2001:db8::1]/api?mode=test",
    ):
        assert validate_url(url) == url


def test_scheme_host_and_unsafe_authority_validation() -> None:
    with pytest.raises(URLValidationError, match="Scheme.*not allowed"):
        validate_url("ftp://example.com")
    with pytest.raises(URLValidationError, match="Missing host"):
        validate_url("https://")
    with pytest.raises(URLValidationError, match="username or password"):
        validate_url("https://user:password@example.com")
    with pytest.raises(URLValidationError, match="unsafe characters"):
        validate_url("https://example.com\\@evil.example")


def test_loopback_and_private_network_options_use_ipaddress() -> None:
    with pytest.raises(URLValidationError, match="Loopback.*not allowed"):
        validate_url("http://localhost", allow_loopback=False)
    with pytest.raises(URLValidationError, match="Loopback.*not allowed"):
        validate_url("http://[::1]", allow_loopback=False)

    for url in (
        "http://10.0.0.1",
        "http://172.16.0.1",
        "http://192.168.1.1",
        "http://[fd00::1]",
        "http://169.254.1.1",
    ):
        with pytest.raises(URLValidationError, match="Private network.*not allowed"):
            validate_url(url, allow_private_networks=False)


def test_port_validation() -> None:
    validate_url("https://example.com:8443", require_port=8443)
    with pytest.raises(URLValidationError, match="Port must be"):
        validate_url("https://example.com:443", require_port=8443)
    with pytest.raises(URLValidationError, match="out of valid range"):
        validate_url("https://example.com:70000")
    with pytest.raises(URLValidationError, match="out of valid range"):
        validate_url("https://example.com:")


def test_specialized_validators_apply_safe_defaults() -> None:
    assert validate_provider_url("http://localhost:8080")
    assert validate_provider_portal_url("https://portal.example.com")
    assert validate_webhook_url("https://hooks.example.com")

    with pytest.raises(URLValidationError):
        validate_provider_portal_url("http://localhost")
    with pytest.raises(URLValidationError):
        validate_provider_portal_url("https://192.168.1.1")
    with pytest.raises(URLValidationError, match="Scheme.*not allowed"):
        validate_webhook_url("http://hooks.example.com")


def test_redirects_compare_normalized_origins() -> None:
    base = "https://EXAMPLE.com:443/page1"
    redirect = "https://example.com/page2"
    assert validate_redirect_url(redirect, base) == redirect

    with pytest.raises(URLValidationError, match="Cross-origin"):
        validate_redirect_url("https://malicious.example/page", base)
    with pytest.raises(URLValidationError, match="Cross-origin"):
        validate_redirect_url("http://example.com/page", base)
    with pytest.raises(URLValidationError, match="Cross-origin"):
        validate_redirect_url("https://example.com:8443/page", base)


def test_normalize_url_lowercases_authority_and_removes_default_ports() -> None:
    assert normalize_url("HTTP://EXAMPLE.COM") == "http://example.com/"
    assert normalize_url("https://example.com/path/") == "https://example.com/path"
    assert normalize_url("http://example.com:80/") == "http://example.com/"
    assert normalize_url("https://[2001:0db8::1]:443/") == "https://[2001:db8::1]/"
