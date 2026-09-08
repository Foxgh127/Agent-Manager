"""Network services."""
from __future__ import annotations
from agent_manager import core as _core


def _remote_error_message(error: _core.urllib.error.HTTPError) -> str:
    detail = ""
    try:
        body = error.read(4096).decode("utf-8", errors="replace")
        payload = _core.json.loads(body)
        if isinstance(payload, dict):
            candidate = payload.get("detail") or payload.get("message") or payload.get("error")
            if isinstance(candidate, dict):
                candidate = candidate.get("message") or candidate.get("code")
            if isinstance(candidate, str):
                detail = _core._redact_sensitive_text(candidate, limit=240)
    except Exception:
        pass
    suffix = f"：{detail}" if detail else ""
    return f"远端接口返回 HTTP {error.code}{suffix}"



def _redact_sensitive_text(value: _core.Any, *, limit: int = 500) -> str:
    """Return bounded diagnostic text with common credentials removed.

    Remote providers and operating-system launch helpers are not trusted to
    avoid echoing request headers, callback query values or imported secrets.
    Redaction therefore happens before truncation so even a long credential
    cannot leave a visible prefix in an error card or log record.
    """

    text = _core.re.sub(r"\s+", " ", str(value or "")).strip()[:8_192]
    if not text:
        return ""
    text = _core._SENSITIVE_ASSIGNMENT_PATTERN.sub(r"\1[已隐藏]", text)
    text = _core._SENSITIVE_QUERY_PATTERN.sub(r"\1[已隐藏]", text)
    text = _core._BEARER_PATTERN.sub("Bearer [已隐藏]", text)
    text = _core._JWT_PATTERN.sub("[JWT 已隐藏]", text)
    text = _core._PREFIXED_SECRET_PATTERN.sub("[凭据已隐藏]", text)
    return text[: max(0, int(limit))]



def _is_definitive_credential_error(value: _core.Any) -> bool:
    """Return True only for remote responses that prove a credential is unusable.

    Import previews must not discard accounts because of rate limiting, a TLS
    interruption or a temporary upstream failure.  401/402/403 and the
    equivalent explicit provider messages are terminal for the imported
    credential and can therefore be excluded before any local state is written.
    """

    message = str(value or "").casefold()
    return any(marker in message for marker in _core._DEFINITIVE_CREDENTIAL_ERROR_MARKERS)



def _throttle_chatgpt_request() -> None:
    """Bound process-wide metadata request bursts without touching inference."""

    pass # shared state is explicitly qualified
    with _core.CHATGPT_REQUEST_GATE_LOCK:
        now = _core.time.monotonic()
        delay = _core.CHATGPT_REQUEST_MIN_INTERVAL_SECONDS - (now - _core.CHATGPT_REQUEST_GATE_AT)
        if delay > 0:
            _core.time.sleep(delay)
        _core.CHATGPT_REQUEST_GATE_AT = _core.time.monotonic()



def _chatgpt_transport_reason(error: BaseException) -> BaseException | str:
    reason = error.reason if isinstance(error, _core.urllib.error.URLError) else error
    return reason if isinstance(reason, (BaseException, str)) else str(reason)



def _is_transient_chatgpt_transport_error(error: BaseException) -> bool:
    """Return True only for transport failures that are safe to retry."""

    reason = _core._chatgpt_transport_reason(error)
    if isinstance(reason, _core.ssl.SSLCertVerificationError):
        return False
    if isinstance(
        reason,
        (
            _core.ssl.SSLEOFError,
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
            TimeoutError,
            _core.socket.timeout,
            _core.http.client.RemoteDisconnected,
            _core.http.client.IncompleteRead,
        ),
    ):
        return True
    text = str(reason).casefold()
    return any(
        marker in text
        for marker in (
            "unexpected_eof_while_reading",
            "eof occurred in violation of protocol",
            "remote end closed connection",
            "connection reset by peer",
            "connection was forcibly closed",
            "远程主机强迫关闭",
        )
    )



def _is_transient_chatgpt_error_text(value: _core.Any) -> bool:
    text = str(value or "").casefold()
    if "certificate verify" in text or "证书验证" in text:
        return False
    return any(
        marker in text
        for marker in (
            "unexpected_eof_while_reading",
            "eof occurred in violation of protocol",
            "临时中断了安全连接",
            "连接 chatgpt 超时",
            "remote end closed connection",
            "connection reset by peer",
            "远程主机强迫关闭",
        )
    )



def _chatgpt_transport_error_message(error: BaseException) -> str:
    reason = _core._chatgpt_transport_reason(error)
    if isinstance(reason, _core.ssl.SSLCertVerificationError):
        return "ChatGPT 安全证书验证失败，请检查系统时间、代理或证书设置。"
    if isinstance(reason, (TimeoutError, _core.socket.timeout)):
        return "连接 ChatGPT 超时，请检查网络或代理设置。"
    if _core._is_transient_chatgpt_transport_error(error):
        return "ChatGPT 临时中断了安全连接；已自动重试仍未成功，请稍后再刷新。"
    if isinstance(reason, _core.ssl.SSLError):
        return "无法建立 ChatGPT 安全连接，请检查网络、代理或系统证书设置。"
    detail = _core._redact_sensitive_text(reason, limit=240)
    return f"无法连接 ChatGPT：{detail}" if detail else "无法连接 ChatGPT，请检查网络或代理设置。"



def _request_chatgpt_json(
    url: str,
    access_token: str,
    account_id: str,
    *,
    query: dict | None = None,
    method: str = "GET",
    payload: dict | None = None,
    include_account_id: bool = True,
    extra_headers: dict[str, str] | None = None,
) -> dict:
    if query:
        url = f"{url}?{_core.urllib.parse.urlencode(query)}"
    body = None
    if payload is not None:
        body = _core.json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request_headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Referer": "https://chatgpt.com/",
        "User-Agent": f"{_core.APP_NAME}/1.1",
    }
    if include_account_id:
        request_headers.update(
            {
                "ChatGPT-Account-Id": account_id,
                "OpenAI-Beta": "codex-1",
                "Originator": _core.APP_NAME,
            }
        )
    if extra_headers:
        request_headers.update(extra_headers)
    request = _core.urllib.request.Request(
        url,
        data=body,
        method=method,
        headers=request_headers,
    )
    retry_delays = _core.CHATGPT_TRANSIENT_RETRY_DELAYS_SECONDS if method.upper() in {"GET", "HEAD"} else ()
    attempts = 1 + len(retry_delays)
    raw = b""
    for attempt in range(attempts):
        try:
            _core._throttle_chatgpt_request()
            with _core._open_same_origin_request(
                request,
                timeout=_core.CHATGPT_REQUEST_TIMEOUT_SECONDS,
            ) as response:
                content_length = int(response.headers.get("Content-Length") or 0)
                if content_length > _core.CHATGPT_RESPONSE_LIMIT_BYTES:
                    raise _core.ManagerError("远端接口响应异常过大，已停止读取。")
                raw = response.read(_core.CHATGPT_RESPONSE_LIMIT_BYTES + 1)
            break
        except _core.urllib.error.HTTPError as exc:
            message = _core._remote_error_message(exc)
            exc.close()
            raise _core.ManagerError(message) from exc
        except _core._CHATGPT_TRANSPORT_EXCEPTIONS as exc:
            if attempt < attempts - 1 and _core._is_transient_chatgpt_transport_error(exc):
                _core.time.sleep(retry_delays[attempt])
                continue
            raise _core.ManagerError(_core._chatgpt_transport_error_message(exc)) from exc
    if len(raw) > _core.CHATGPT_RESPONSE_LIMIT_BYTES:
        raise _core.ManagerError("远端接口响应异常过大，已停止读取。")
    try:
        payload = _core.json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, _core.json.JSONDecodeError) as exc:
        raise _core.ManagerError("远端接口返回了无效 JSON。") from exc
    if not isinstance(payload, dict):
        raise _core.ManagerError("远端接口返回的数据结构无效。")
    return payload



def _fetch_chatgpt_json(
    url: str,
    access_token: str,
    account_id: str,
    query: dict | None = None,
    *,
    include_account_id: bool = True,
    extra_headers: dict[str, str] | None = None,
) -> dict:
    return _core._request_chatgpt_json(
        url,
        access_token,
        account_id,
        query=query,
        include_account_id=include_account_id,
        extra_headers=extra_headers,
    )



def _post_chatgpt_json(url: str, access_token: str, account_id: str, payload: dict) -> dict:
    return _core._request_chatgpt_json(
        url,
        access_token,
        account_id,
        method="POST",
        payload=payload,
    )

