"""Safe, shared validation helpers for HTTP(S) URLs.

The validator intentionally does not perform DNS lookups.  It validates the
URL syntax and classifies literal IP addresses with :mod:`ipaddress`; callers
that connect to a hostname should still resolve and re-check the destination
at connection time if they need SSRF protection against DNS rebinding.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import ParseResult, urlparse, urlunparse
from typing import Collection, Optional, Set


_DEFAULT_SCHEMES = frozenset({"http", "https"})
_MAX_URL_LENGTH = 8_192
_LOCALHOST_NAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
})
_CONTROL_OR_WHITESPACE = re.compile(r"[\x00-\x20\x7f]|\\")


class URLValidationError(ValueError):
    """Raised when a URL fails syntax or safety validation."""


def _error(purpose: str, message: str) -> URLValidationError:
    return URLValidationError(f"Invalid {purpose}: {message}")


def _normalise_schemes(allowed_schemes: Optional[Collection[str]]) -> Set[str]:
    if allowed_schemes is None:
        return set(_DEFAULT_SCHEMES)
    try:
        return {str(scheme).casefold() for scheme in allowed_schemes}
    except TypeError as exc:
        raise TypeError("allowed_schemes must be an iterable of strings") from exc


def _parse_port(parsed: ParseResult, purpose: str) -> int | None:
    try:
        port = parsed.port
    except ValueError as exc:
        raise _error(purpose, f"Port is invalid or out of valid range (1-65535): {exc}") from exc

    # ``urllib.parse`` treats ``https://example.com:`` as having no port.
    # An explicit empty port is malformed and should not silently become the
    # scheme's default port.
    authority = parsed.netloc.rsplit("@", 1)[-1]
    if authority.endswith(":"):
        raise _error(purpose, "Port is invalid or out of valid range (1-65535)")
    if port is not None and not 1 <= port <= 65_535:
        raise _error(purpose, f"Port {port} out of valid range (1-65535)")
    return port


def _hostname(parsed: ParseResult, purpose: str) -> str:
    try:
        hostname = parsed.hostname
    except ValueError as exc:
        raise _error(purpose, f"Invalid hostname: {exc}") from exc
    if not hostname:
        raise _error(purpose, "Invalid hostname")
    if _CONTROL_OR_WHITESPACE.search(hostname):
        raise _error(purpose, "Invalid hostname")
    # IDNA encoding catches malformed Unicode host labels without requiring a
    # DNS lookup.  ``urlparse`` has already separated an IPv6 literal.
    try:
        hostname.encode("idna")
    except UnicodeError as exc:
        raise _error(purpose, "Invalid hostname") from exc
    return hostname


def _literal_ip(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        return None


def _is_loopback_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped is not None and mapped.is_loopback)


def _is_private_ip(hostname: str) -> bool:
    """Return whether a literal host is a non-public IP address.

    Kept as a small compatibility helper for callers of the original fix
    sketch.  Hostnames are intentionally treated as unknown rather than
    resolved here.
    """

    value = str(hostname).strip().strip("[]")
    address = _literal_ip(value)
    return bool(address is not None and _is_non_public_ip(address))


def _is_non_public_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return whether an address should be rejected by strict URL policies."""

    # ``is_private`` covers RFC1918, IPv6 ULA and documentation ranges on
    # current Python versions.  The additional flags cover link-local,
    # multicast, unspecified and shared addresses that are also unsafe as an
    # external service destination.
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        or not address.is_global
    )


def _validate_hostname_safety(
    hostname: str,
    *,
    allow_loopback: bool,
    allow_private_networks: bool,
    purpose: str,
) -> None:
    canonical = hostname.casefold().rstrip(".")
    literal = _literal_ip(canonical)
    hostname_is_loopback = canonical in _LOCALHOST_NAMES
    literal_is_loopback = literal is not None and _is_loopback_ip(literal)

    if not allow_loopback and (hostname_is_loopback or literal_is_loopback):
        raise _error(purpose, "Loopback addresses not allowed")

    # A caller may intentionally permit loopback while blocking other private
    # destinations (for example, a local development provider endpoint).
    if not allow_private_networks and not (hostname_is_loopback or literal_is_loopback and allow_loopback):
        if literal is not None and _is_non_public_ip(literal):
            raise _error(purpose, "Private network addresses not allowed")


def validate_url(
    url: str,
    allowed_schemes: Optional[Set[str]] = None,
    allow_loopback: bool = True,
    allow_private_networks: bool = True,
    require_port: Optional[int] = None,
    purpose: str = "URL",
    *,
    allow_userinfo: bool = False,
    allow_query: bool = True,
    allow_fragment: bool = True,
    max_length: int = _MAX_URL_LENGTH,
) -> str:
    """Validate an absolute HTTP-style URL and return it unchanged.

    The defaults retain the fix-document behaviour: HTTP and HTTPS are
    accepted, and local/private destinations are allowed for provider URLs.
    Strict wrappers below disable those destinations for portal and webhook
    addresses.  Credentials in a URL are rejected by default because they
    are easily leaked through logs and redirects; callers must opt in
    explicitly when a URL consumer truly supports them.
    """

    if not isinstance(url, str) or not url:
        raise _error(purpose, "URL must be a non-empty string")
    if url != url.strip() or _CONTROL_OR_WHITESPACE.search(url):
        raise _error(purpose, "URL contains whitespace or unsafe characters")
    try:
        encoded_length = len(url.encode("utf-8"))
    except UnicodeError as exc:
        raise _error(purpose, "URL is not valid UTF-8") from exc
    if isinstance(max_length, bool) or not isinstance(max_length, int) or max_length < 1:
        raise ValueError("max_length must be a positive integer")
    if encoded_length > max_length:
        raise _error(purpose, f"URL exceeds maximum length of {max_length} bytes")

    schemes = _normalise_schemes(allowed_schemes)
    try:
        parsed = urlparse(url)
    except (TypeError, ValueError) as exc:
        raise _error(purpose, f"Failed to parse URL: {exc}") from exc

    scheme = parsed.scheme.casefold()
    if scheme not in schemes:
        allowed = ", ".join(sorted(schemes))
        raise _error(purpose, f"Scheme '{parsed.scheme}' not allowed. Allowed: {allowed}")
    if not parsed.netloc:
        raise _error(purpose, "Missing host")

    try:
        hostname = _hostname(parsed, purpose)
    except URLValidationError:
        raise
    if parsed.username is not None or parsed.password is not None:
        if not allow_userinfo:
            raise _error(purpose, "URL must not contain a username or password")
    if parsed.query and not allow_query:
        raise _error(purpose, "Query parameters are not allowed")
    if parsed.fragment and not allow_fragment:
        raise _error(purpose, "Fragments are not allowed")

    port = _parse_port(parsed, purpose)
    if require_port is not None:
        if isinstance(require_port, bool) or not isinstance(require_port, int) or not 1 <= require_port <= 65_535:
            raise ValueError("require_port must be an integer from 1 to 65535")
        if port != require_port:
            raise _error(purpose, f"Port must be {require_port}, got {port}")

    _validate_hostname_safety(
        hostname,
        allow_loopback=allow_loopback,
        allow_private_networks=allow_private_networks,
        purpose=purpose,
    )
    return url


def _origin(parsed: ParseResult) -> tuple[str, str, int]:
    hostname = str(parsed.hostname or "").casefold().rstrip(".")
    literal = _literal_ip(hostname)
    if literal is not None:
        hostname = str(literal)
    try:
        port = parsed.port
    except ValueError as exc:
        raise URLValidationError("Invalid URL origin port") from exc
    scheme = parsed.scheme.casefold()
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, hostname, port


def validate_provider_url(url: str) -> str:
    """Validate a provider API endpoint URL."""

    return validate_url(
        url,
        allowed_schemes={"http", "https"},
        allow_loopback=True,
        allow_private_networks=True,
        allow_fragment=False,
        purpose="provider endpoint",
    )


def validate_provider_portal_url(url: str) -> str:
    """Validate a provider portal URL exposed to a user."""

    return validate_url(
        url,
        allow_loopback=False,
        allow_private_networks=False,
        allow_fragment=False,
        purpose="provider portal",
    )


def validate_webhook_url(url: str) -> str:
    """Validate a public HTTPS webhook URL."""

    return validate_url(
        url,
        allowed_schemes={"https"},
        allow_loopback=False,
        allow_private_networks=False,
        allow_fragment=False,
        purpose="webhook",
    )


def validate_redirect_url(url: str, base_url: str) -> str:
    """Allow redirects only when both URLs share the same origin."""

    # Validate both sides before comparing their origins.  This rejects
    # malformed authorities and credentials that a raw ``netloc`` comparison
    # can otherwise mishandle.
    validated_base = validate_url(
        base_url,
        purpose="base URL",
        allow_userinfo=False,
    )
    validated_redirect = validate_url(
        url,
        purpose="redirect URL",
        allow_userinfo=False,
    )
    try:
        base_parsed = urlparse(validated_base)
        redirect_parsed = urlparse(validated_redirect)
        base_origin = _origin(base_parsed)
        redirect_origin = _origin(redirect_parsed)
    except (TypeError, ValueError, URLValidationError) as exc:
        raise URLValidationError(f"Cross-origin redirect: invalid URL origin ({exc})") from exc

    if base_origin[0] != redirect_origin[0]:
        raise URLValidationError("Cross-origin redirect: scheme mismatch")
    if base_origin[1:] != redirect_origin[1:]:
        raise URLValidationError("Cross-origin redirect: host or port mismatch")
    return url


def normalize_url(url: str) -> str:
    """Return a stable URL spelling while preserving path/query/fragment."""

    validate_url(url, purpose="URL", allow_userinfo=False)
    parsed = urlparse(url)
    hostname = str(parsed.hostname or "").casefold().rstrip(".")
    literal = _literal_ip(hostname)
    if literal is not None:
        hostname = str(literal)
    if literal is not None and literal.version == 6:
        hostname = f"[{hostname}]"

    try:
        port = parsed.port
    except ValueError as exc:
        raise URLValidationError(f"Invalid URL: {exc}") from exc
    scheme = parsed.scheme.casefold()
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    authority = hostname
    if port is not None and not default_port:
        authority = f"{authority}:{port}"

    path = parsed.path.rstrip("/") or "/"
    return urlunparse((scheme, authority, path, parsed.params, parsed.query, parsed.fragment))

