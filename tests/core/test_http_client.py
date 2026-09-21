from __future__ import annotations

from email.message import Message
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from agent_manager.core.http_client import (
    ResponseTooLargeError,
    SafeHTTPClient,
    SameOriginRedirectHandler,
)


def _response(payload: bytes, *, content_length: int | None = None) -> Mock:
    response = Mock()
    response.read.return_value = payload
    response.headers = Message()
    if content_length is not None:
        response.headers["Content-Length"] = str(content_length)
    return response


def test_basic_get_request_closes_response() -> None:
    client = SafeHTTPClient(timeout=10)
    response = _response(b'{"status": "ok"}')
    with patch("agent_manager.core.http_client.urlopen", return_value=response) as opener:
        result = client.get("https://api.example.com/status")

    assert result == b'{"status": "ok"}'
    opener.assert_called_once()
    assert opener.call_args.kwargs["timeout"] == 10
    response.close.assert_called_once_with()


def test_post_json_does_not_mutate_headers() -> None:
    client = SafeHTTPClient()
    response = _response(b'{"created": true}')
    headers = {"X-Test": "yes"}

    with patch("agent_manager.core.http_client.urlopen", return_value=response) as opener:
        result = client.post_json("https://api.example.com/create", {"name": "test"}, headers)

    request = opener.call_args.args[0]
    assert isinstance(request, Request)
    assert request.get_method() == "POST"
    assert request.data == b'{"name": "test"}'
    assert request.headers["Content-type"] == "application/json"
    assert headers == {"X-Test": "yes"}
    assert result == {"created": True}


def test_same_origin_handler_allows_relative_and_default_port_redirects() -> None:
    handler = SameOriginRedirectHandler("https://EXAMPLE.com")
    request = Request("https://example.com/start")
    fp = Mock()

    redirected = handler.redirect_request(request, fp, 302, "Found", {}, "/next")
    same_default_port = handler.redirect_request(
        request, fp, 302, "Found", {}, "https://example.com:443/next"
    )

    assert redirected.full_url == "https://example.com/next"
    assert same_default_port.full_url == "https://example.com:443/next"


def test_same_origin_handler_rejects_cross_origin_and_scheme_downgrade() -> None:
    handler = SameOriginRedirectHandler("https://example.com")
    request = Request("https://example.com/start")
    fp = Mock()

    with pytest.raises(HTTPError, match="Cross-origin"):
        handler.redirect_request(request, fp, 302, "Found", {}, "https://malicious.test/")
    with pytest.raises(HTTPError, match="Cross-origin"):
        handler.redirect_request(request, fp, 302, "Found", {}, "http://example.com/")


def test_declared_response_limit_closes_response_before_raising() -> None:
    client = SafeHTTPClient(max_response_bytes=4)
    response = _response(b"too large", content_length=9)

    with patch("agent_manager.core.http_client.urlopen", return_value=response):
        with pytest.raises(ResponseTooLargeError):
            client.get("https://api.example.com/status")

    response.close.assert_called_once_with()
    response.read.assert_not_called()


def test_streaming_open_enforces_limit_and_context_closes() -> None:
    client = SafeHTTPClient(max_response_bytes=4)
    response = _response(b"12345")

    with patch("agent_manager.core.http_client.urlopen", return_value=response):
        with client.open(Request("https://api.example.com/status")) as opened:
            with pytest.raises(ResponseTooLargeError):
                opened.read()

    response.close.assert_called_once_with()
