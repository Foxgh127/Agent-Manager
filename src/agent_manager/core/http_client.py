"""Small, bounded HTTP helpers used by Agent Manager.

The project has a number of callers that historically used ``urllib.request``
directly.  ``SafeHTTPClient`` keeps that model (requests are built with
``urllib.request.Request`` and methods return response bytes), while adding a
few invariants that are easy to miss at each call site:

* redirects can be restricted to the request's origin;
* response bodies are bounded before and while they are read; and
* every response, including an error response, is closed by the client.

This module deliberately does not keep a connection pool.  A client is cheap
to create and the short-lived opener makes its lifetime and TLS settings
explicit at each call site.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
import ssl
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
    urlopen,
)


DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
"""Maximum response body size used by :class:`SafeHTTPClient` by default."""


class HTTPClientError(RuntimeError):
    """Base class for errors raised by the safety checks in this module."""


class ResponseTooLargeError(HTTPClientError):
    """Raised when an HTTP response exceeds the configured byte limit."""

    def __init__(
        self,
        url: str,
        limit: int,
        *,
        declared_size: int | None = None,
        actual_size: int | None = None,
    ) -> None:
        self.url = url
        self.limit = limit
        self.declared_size = declared_size
        self.actual_size = actual_size
        detail = f"HTTP response exceeds the {limit} byte limit"
        if declared_size is not None:
            detail += f" (Content-Length: {declared_size})"
        elif actual_size is not None:
            detail += f" (read at least {actual_size} bytes)"
        super().__init__(f"{detail}: {url}")


def _validated_timeout(value: float | int) -> float:
    """Validate a timeout without silently accepting NaN or infinity."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout must be a positive number")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be a positive finite number")
    return value


def _origin_key(url: str) -> tuple[str, str, int]:
    """Return a normalized ``(scheme, host, port)`` origin tuple.

    ``urllib.parse.ParseResult.netloc`` is not sufficient for an origin
    comparison: it is case-sensitive, treats the default port as distinct,
    and includes user information.  User information is rejected because it
    is not part of a safe origin policy.
    """

    if not isinstance(url, str) or not url.strip():
        raise ValueError("origin must be a non-empty URL")
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("origin must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("origin must not contain user information")
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("origin contains an invalid port") from exc
    if not hostname:
        raise ValueError("origin must include a host")
    hostname = hostname.rstrip(".").lower()
    if not hostname:
        raise ValueError("origin must include a host")
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, hostname, port


def _format_origin(origin: tuple[str, str, int]) -> str:
    scheme, hostname, port = origin
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    default_port = 443 if scheme == "https" else 80
    suffix = "" if port == default_port else f":{port}"
    return f"{scheme}://{host}{suffix}"


def _close_response(response: Any) -> None:
    """Close a urllib response while tolerating light-weight test doubles."""

    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            # A close failure must never mask the request or parsing error.
            pass


class SameOriginRedirectHandler(HTTPRedirectHandler):
    """Only follow redirects that remain on the configured HTTP origin.

    The comparison includes scheme, normalized hostname, and effective port.
    Thus an HTTPS to HTTP redirect is rejected even when the host is the same,
    while ``https://example.test`` and ``https://example.test:443`` are treated
    as the same origin.
    """

    def __init__(self, allowed_origin: str, *, max_redirections: int = 10) -> None:
        super().__init__()
        normalized = _origin_key(allowed_origin)
        # Keep the public attribute useful for callers that used the original
        # implementation, which exposed the host/port portion.
        self.allowed_origin = urlparse(allowed_origin).netloc
        self._allowed_origin_key = normalized
        if isinstance(max_redirections, bool) or not isinstance(max_redirections, int):
            raise ValueError("max_redirections must be a positive integer")
        if max_redirections < 1:
            raise ValueError("max_redirections must be a positive integer")
        self.max_redirections = max_redirections

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        request_url = getattr(req, "full_url", None)
        if not request_url:
            getter = getattr(req, "get_full_url", None)
            request_url = getter() if callable(getter) else ""
        absolute_url = urljoin(request_url, newurl)
        try:
            new_origin = _origin_key(absolute_url)
        except (AttributeError, TypeError, ValueError) as exc:
            raise HTTPError(
                absolute_url,
                code,
                f"Invalid redirect URL: {absolute_url}",
                headers,
                fp,
            ) from exc
        if new_origin != self._allowed_origin_key:
            raise HTTPError(
                absolute_url,
                code,
                "Cross-origin redirect not allowed: "
                f"{_format_origin(self._allowed_origin_key)} -> {_format_origin(new_origin)}",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, absolute_url)


class _BoundedHTTPResponse:
    """A closeable, urllib-compatible response view with a byte budget."""

    def __init__(self, response: Any, url: str, max_response_bytes: int | None) -> None:
        self._response = response
        self._url = url
        self._limit = max_response_bytes
        self._bytes_read = 0
        self._closed = False

    def __getattr__(self, name: str) -> Any:
        if name in {"close", "read", "readinto", "readline", "readlines"}:
            raise AttributeError(name)
        return getattr(self._response, name)

    @property
    def closed(self) -> bool:
        # Lightweight mocks and a few urllib adapters expose ``closed`` as a
        # truthy sentinel rather than a boolean. Only an explicit ``True``
        # means the underlying response is closed.
        underlying = getattr(self._response, "closed", False)
        return self._closed or underlying is True

    @property
    def headers(self) -> Any:
        return getattr(self._response, "headers", None)

    def _read_limited(self, size: int | None = None) -> bytes:
        if self.closed:
            raise ValueError("I/O operation on closed HTTP response")
        reader = getattr(self._response, "read", None)
        if not callable(reader):
            raise TypeError("HTTP response does not provide read()")
        limit = self._limit
        if limit is None:
            raw = reader() if size is None else reader(size)
        else:
            remaining = limit - self._bytes_read
            if remaining < 0:
                self.close()
                raise ResponseTooLargeError(self._url, limit, actual_size=self._bytes_read)
            requested = remaining + 1 if size is None or size < 0 else min(size, remaining + 1)
            try:
                raw = reader(requested)
            except TypeError:
                raw = reader()
        if not isinstance(raw, (bytes, bytearray, memoryview)):
            self.close()
            raise TypeError("HTTP response read() must return bytes")
        body = bytes(raw)
        if limit is not None and self._bytes_read + len(body) > limit:
            self._bytes_read += len(body)
            self.close()
            raise ResponseTooLargeError(self._url, limit, actual_size=self._bytes_read)
        self._bytes_read += len(body)
        return body

    def read(self, size: int = -1) -> bytes:
        return self._read_limited(size)

    def readinto(self, buffer: Any) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed HTTP response")
        body = self._read_limited(len(buffer))
        buffer[: len(body)] = body
        return len(body)

    def readline(self, size: int = -1) -> bytes:
        if self.closed:
            raise ValueError("I/O operation on closed HTTP response")
        reader = getattr(self._response, "readline", None)
        if not callable(reader):
            return self.read(size)
        if self._limit is None:
            return reader() if size < 0 else reader(size)
        remaining = self._limit - self._bytes_read
        if remaining <= 0:
            return b""
        raw = reader() if size < 0 else reader(min(size, remaining))
        if not isinstance(raw, (bytes, bytearray, memoryview)):
            self.close()
            raise TypeError("HTTP response readline() must return bytes")
        body = bytes(raw)
        if len(body) > remaining:
            self._bytes_read += len(body)
            self.close()
            raise ResponseTooLargeError(self._url, self._limit, actual_size=self._bytes_read)
        self._bytes_read += len(body)
        return body

    def __iter__(self):
        while True:
            line = self.readline()
            if not line:
                return
            yield line

    def readlines(self, hint: int = -1) -> list[bytes]:
        lines: list[bytes] = []
        total = 0
        while hint < 0 or total < hint:
            line = self.readline()
            if not line:
                break
            lines.append(line)
            total += len(line)
        return lines

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _close_response(self._response)

    def __enter__(self) -> "_BoundedHTTPResponse":
        if self.closed:
            raise ValueError("I/O operation on closed HTTP response")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.close()
        return False

class SafeHTTPClient:
    """A bounded urllib-style HTTP client.

    ``get`` and ``post`` return response bytes, matching the small interface
    in the fix plan and making migration from ``urlopen(...).read()`` simple.
    ``request`` is available for callers that need another method.  The
    response object is never exposed, so the client can close it reliably.
    """

    def __init__(
        self,
        enforce_same_origin: bool = False,
        origin: Optional[str] = None,
        timeout: int | float = 30,
        proxy: Optional[str] = None,
        verify_ssl: bool = True,
        user_agent: str = "AgentManager/1.2.3",
        max_response_bytes: int | None = DEFAULT_MAX_RESPONSE_BYTES,
        *,
        max_response_size: int | None = None,
        max_redirects: int = 10,
        opener: OpenerDirector | None = None,
    ) -> None:
        self.enforce_same_origin = bool(enforce_same_origin)
        self.origin = origin
        self.timeout = _validated_timeout(timeout)
        self.verify_ssl = bool(verify_ssl)
        self.user_agent = str(user_agent)

        if max_response_size is not None:
            if max_response_bytes != DEFAULT_MAX_RESPONSE_BYTES and max_response_bytes != max_response_size:
                raise ValueError("max_response_bytes and max_response_size disagree")
            max_response_bytes = max_response_size
        if max_response_bytes is not None:
            if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int):
                raise ValueError("max_response_bytes must be a positive integer or None")
            if max_response_bytes <= 0:
                raise ValueError("max_response_bytes must be a positive integer or None")
        self.max_response_bytes = max_response_bytes

        if isinstance(max_redirects, bool) or not isinstance(max_redirects, int) or max_redirects < 1:
            raise ValueError("max_redirects must be a positive integer")
        self.max_redirects = max_redirects
        if self.enforce_same_origin and origin is None:
            raise ValueError("origin is required when enforce_same_origin is enabled")
        if self.enforce_same_origin:
            # Validate once during construction so a malformed policy cannot
            # be deferred until the first request.
            self._origin_key = _origin_key(origin)  # type: ignore[arg-type]
        else:
            self._origin_key = None

        self.ssl_context = (
            ssl.create_default_context()
            if self.verify_ssl
            else ssl._create_unverified_context()
        )

        if opener is not None:
            self.opener = opener
        else:
            handlers = []
            if proxy:
                handlers.append(ProxyHandler({"http": proxy, "https": proxy}))
            if self.enforce_same_origin:
                handlers.append(
                    SameOriginRedirectHandler(origin, max_redirections=max_redirects)  # type: ignore[arg-type]
                )
            # A custom HTTPS handler is needed when the caller disables
            # verification; an opener also lets proxy/redirect handlers share
            # the same TLS policy.
            if proxy or self.enforce_same_origin or not self.verify_ssl:
                handlers.append(HTTPSHandler(context=self.ssl_context))
            self.opener = build_opener(*handlers) if handlers else None

    def _build_request(
        self,
        url: str,
        method: str = "GET",
        data: bytes | bytearray | memoryview | str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Request:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        method = str(method).upper()
        if isinstance(data, str):
            data = data.encode("utf-8")
        request_headers = {"User-Agent": self.user_agent}
        if headers:
            request_headers.update(dict(headers))
        return Request(url, data=data, headers=request_headers, method=method)

    def _open(self, request: Request):
        if self.opener is not None:
            # OpenerDirector.open accepts timeout but not context.  The
            # HTTPSHandler installed above owns the context for opener calls.
            return self.opener.open(request, timeout=self.timeout)
        return urlopen(request, timeout=self.timeout, context=self.ssl_context)

    def open(
        self,
        request: Request | str,
        timeout: int | float | None = None,
    ) -> _BoundedHTTPResponse:
        """Open a request and return a bounded context-manageable response."""
        if isinstance(request, str):
            request = self._build_request(request)
        elif not isinstance(request, Request):
            if not hasattr(request, "full_url") and not hasattr(request, "get_full_url"):
                raise TypeError("request must be a URL string or urllib Request")
        request_timeout = self.timeout if timeout is None else _validated_timeout(timeout)
        try:
            if self.opener is not None:
                response = self.opener.open(request, timeout=request_timeout)
            else:
                response = urlopen(request, timeout=request_timeout, context=self.ssl_context)
        except HTTPError as exc:
            _close_response(exc)
            raise
        url = getattr(request, "full_url", None) or request.get_full_url()
        limit = self.max_response_bytes
        declared_size = self._content_length(response)
        if limit is not None and declared_size is not None and declared_size > limit:
            _close_response(response)
            raise ResponseTooLargeError(url, limit, declared_size=declared_size)
        return _BoundedHTTPResponse(response, url, limit)

    @staticmethod
    def _content_length(response: Any) -> int | None:
        headers = getattr(response, "headers", None)
        value = None
        if headers is not None:
            getter = getattr(headers, "get", None)
            if callable(getter):
                value = getter("Content-Length")
            if value is None:
                getter = getattr(headers, "getheader", None)
                if callable(getter):
                    value = getter("Content-Length")
        if value is None:
            getter = getattr(response, "getheader", None)
            if callable(getter):
                value = getter("Content-Length")
        if value is None:
            return None
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    def _read_response(self, response: Any, url: str) -> bytes:
        try:
            limit = self.max_response_bytes
            declared_size = self._content_length(response)
            if limit is not None and declared_size is not None and declared_size > limit:
                raise ResponseTooLargeError(url, limit, declared_size=declared_size)

            reader = getattr(response, "read", None)
            if not callable(reader):
                raise TypeError("HTTP response does not provide read()")
            if limit is None:
                raw = reader()
            else:
                # Reading one byte over the limit detects chunked/unknown
                # length responses without buffering an unbounded body.
                try:
                    raw = reader(limit + 1)
                except TypeError:
                    # A few urllib-compatible test doubles expose only
                    # read().  We still enforce the limit after the read.
                    raw = reader()
            if not isinstance(raw, (bytes, bytearray, memoryview)):
                raise TypeError("HTTP response read() must return bytes")
            body = bytes(raw)
            if limit is not None and len(body) > limit:
                raise ResponseTooLargeError(url, limit, actual_size=len(body))
            return body
        finally:
            _close_response(response)

    def urlopen(
        self,
        request: Request | str,
        timeout: int | float | None = None,
    ) -> _BoundedHTTPResponse:
        """Compatibility alias matching the common urllib call spelling."""
        return self.open(request, timeout=timeout)

    def request(
        self,
        url: str,
        method: str = "GET",
        data: bytes | bytearray | memoryview | str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        request = self._build_request(url, method, data, headers)
        with self.open(request) as response:
            return response.read()

    def get(self, url: str, headers: Mapping[str, str] | None = None) -> bytes:
        return self.request(url, "GET", headers=headers)

    def post(
        self,
        url: str,
        data: bytes | bytearray | memoryview | str,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        return self.request(url, "POST", data=data, headers=headers)

    def get_json(self, url: str, headers: Mapping[str, str] | None = None) -> Any:
        return json.loads(self.get(url, headers).decode("utf-8"))

    def post_json(
        self,
        url: str,
        json_data: Any,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        request_headers = dict(headers or {})
        if not any(str(name).lower() == "content-type" for name in request_headers):
            request_headers["Content-Type"] = "application/json"
        data = json.dumps(json_data).encode("utf-8")
        return json.loads(self.post(url, data, request_headers).decode("utf-8"))


# Public alias for callers that want to type or inspect the returned
# streaming response without depending on the implementation name.
BoundedHTTPResponse = _BoundedHTTPResponse


def create_same_origin_client(
    origin: str,
    timeout: int | float = 30,
    **kwargs: Any,
) -> SafeHTTPClient:
    return SafeHTTPClient(
        enforce_same_origin=True,
        origin=origin,
        timeout=timeout,
        **kwargs,
    )


def create_registry_client(proxy: Optional[str] = None, **kwargs: Any) -> SafeHTTPClient:
    return SafeHTTPClient(
        timeout=60,
        proxy=proxy,
        user_agent="AgentManager-Registry/1.2.3",
        **kwargs,
    )


def create_api_client(verify_ssl: bool = True, **kwargs: Any) -> SafeHTTPClient:
    return SafeHTTPClient(timeout=30, verify_ssl=verify_ssl, **kwargs)


__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "HTTPClientError",
    "ResponseTooLargeError",
    "BoundedHTTPResponse",
    "SameOriginRedirectHandler",
    "SafeHTTPClient",
    "create_api_client",
    "create_registry_client",
    "create_same_origin_client",
    "HTTPError",
    "URLError",
]

