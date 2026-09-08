"""Oauth services."""
from __future__ import annotations
from agent_manager import core as _core


def _request_codex_oauth_refresh(refresh_token: str) -> dict:
    endpoint = _core.urllib.parse.urlsplit(_core.CODEX_OAUTH_TOKEN_URL)
    if (
        endpoint.scheme.casefold() != "https"
        or endpoint.hostname != "auth.openai.com"
        or endpoint.port not in {None, 443}
        or endpoint.username
        or endpoint.password
        or endpoint.path != "/oauth/token"
        or endpoint.query
        or endpoint.fragment
    ):
        raise _core.ManagerError("Codex OAuth 续期端点配置无效，已拒绝发送凭据。")
    request = _core.urllib.request.Request(
        _core.CODEX_OAUTH_TOKEN_URL,
        data=_core.json.dumps(
            {
                "client_id": _core.CODEX_OAUTH_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            separators=(",", ":"),
        ).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"codex_cli_rs/{_core._codex_client_version()}",
        },
        method="POST",
    )
    try:
        with _core._open_same_origin_request(
            request,
            timeout=_core.CHATGPT_REQUEST_TIMEOUT_SECONDS,
        ) as response:
            content_length = int(response.headers.get("Content-Length") or 0)
            if content_length > _core.CODEX_OAUTH_TOKEN_RESPONSE_LIMIT_BYTES:
                raise _core.ManagerError("Codex OAuth 续期响应异常过大，已停止读取。")
            raw = response.read(_core.CODEX_OAUTH_TOKEN_RESPONSE_LIMIT_BYTES + 1)
    except _core.urllib.error.HTTPError as exc:
        status = int(exc.code)
        try:
            exc.read(4096)
        finally:
            exc.close()
        raise _core.ManagerError(f"Codex OAuth 续期失败：HTTP {status}。") from exc
    except _core._CHATGPT_TRANSPORT_EXCEPTIONS as exc:
        raise _core.ManagerError("Codex OAuth 续期失败，请检查网络或代理后重试。") from exc
    if len(raw) > _core.CODEX_OAUTH_TOKEN_RESPONSE_LIMIT_BYTES:
        raise _core.ManagerError("Codex OAuth 续期响应异常过大，已停止读取。")
    try:
        payload = _core.json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, _core.json.JSONDecodeError) as exc:
        raise _core.ManagerError("Codex OAuth 续期响应不是有效 JSON。") from exc
    if not isinstance(payload, dict):
        raise _core.ManagerError("Codex OAuth 续期响应格式无效。")
    refreshed: dict[str, str] = {}
    for field in ("id_token", "access_token", "refresh_token"):
        value = payload.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise _core.ManagerError(f"Codex OAuth 续期响应字段 {field} 无效。")
        if len(value.encode("utf-8")) > 100_000:
            raise _core.ManagerError(f"Codex OAuth 续期响应字段 {field} 异常过大。")
        refreshed[field] = value.strip()
    if not refreshed.get("access_token"):
        raise _core.ManagerError("Codex OAuth 续期响应缺少 access_token。")
    return refreshed



def _merge_codex_oauth_auth_bytes(
    base_auth_bytes: bytes,
    *,
    newer_auth_bytes: bytes | None = None,
    refreshed_tokens: dict | None = None,
) -> bytes:
    auth = _core._codex_oauth_auth_document(base_auth_bytes)
    tokens = auth["tokens"]
    if newer_auth_bytes is not None:
        newer = _core._codex_oauth_auth_document(newer_auth_bytes)
        newer_tokens = newer["tokens"]
        for field in ("id_token", "access_token", "refresh_token", "account_id"):
            value = newer_tokens.get(field)
            if isinstance(value, str) and value.strip():
                tokens[field] = value.strip()
        newer_last_refresh = newer.get("last_refresh")
        if isinstance(newer_last_refresh, str) and newer_last_refresh.strip():
            auth["last_refresh"] = newer_last_refresh.strip()
        # The live Codex file is authoritative for the optional mode shape and
        # for current official auxiliary credentials.  Copying only the four
        # OAuth token strings would silently drop a newly-issued Agent Identity
        # cache, or keep the manager's legacy explicit auth_mode forever.
        if "auth_mode" in newer or "authMode" in newer:
            newer_mode = newer.get("auth_mode", newer.get("authMode"))
            auth.pop("authMode", None)
            auth["auth_mode"] = newer_mode
        else:
            auth.pop("auth_mode", None)
            auth.pop("authMode", None)
        if "agent_identity" in newer or "agentIdentity" in newer:
            newer_agent_identity = _core._agent_identity_from_auth(newer)
            if newer_agent_identity is not None:
                auth["agent_identity"] = newer_agent_identity
            else:
                auth.pop("agent_identity", None)
                auth.pop("agentIdentity", None)
    if refreshed_tokens is not None:
        for field in ("id_token", "access_token"):
            value = refreshed_tokens.get(field)
            if isinstance(value, str) and value.strip():
                tokens[field] = value.strip()
        rotated_refresh = refreshed_tokens.get("refresh_token")
        if isinstance(rotated_refresh, str) and rotated_refresh.strip():
            tokens["refresh_token"] = rotated_refresh.strip()
        auth["last_refresh"] = _core.now_iso()
    encoded = _core.json.dumps(auth, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    _core._codex_auth_projection_bytes(encoded)
    return encoded



def _probe_chatgpt_codex_access(access_token: str, account_id: str) -> dict:
    """Verify Codex backend authorization without running a model turn.

    An intentionally incomplete request reaches authorization but is rejected
    during payload validation. HTTP 400/422 therefore proves that the bearer is
    accepted, while 401/403 proves it cannot power Codex inference.
    """
    request = _core.urllib.request.Request(
        _core.CHATGPT_RESPONSES_URL,
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "ChatGPT-Account-Id": account_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"codex_cli_rs/{_core._codex_client_version()}",
            "Originator": "codex_cli_rs",
        },
    )
    status = 0
    compatible: bool | None = None
    attempts = 1 + len(_core.CHATGPT_TRANSIENT_RETRY_DELAYS_SECONDS)
    for attempt in range(attempts):
        try:
            _core._throttle_chatgpt_request()
            with _core._open_same_origin_request(
                request,
                timeout=_core.CHATGPT_REQUEST_TIMEOUT_SECONDS,
            ) as response:
                status = int(getattr(response, "status", 200))
                response.read(4096)
                compatible = True
            break
        except _core.urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                exc.read(4096)
            finally:
                exc.close()
            compatible = True if status in {400, 409, 422, 429} else False if status in {401, 403} else None
            break
        except _core._CHATGPT_TRANSPORT_EXCEPTIONS as exc:
            if attempt < attempts - 1 and _core._is_transient_chatgpt_transport_error(exc):
                _core.time.sleep(_core.CHATGPT_TRANSIENT_RETRY_DELAYS_SECONDS[attempt])
                continue
            raise _core.ManagerError(
                f"Codex 能力探测失败：{_core._chatgpt_transport_error_message(exc)}"
            ) from exc
    return {
        "compatible": compatible,
        "status": status,
        "checkedAt": _core.now_iso(),
        "method": "authorization_only",
    }

