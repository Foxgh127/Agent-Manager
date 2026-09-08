"""Pure, offline OAuth portability contract (Cockpit Tools 1.3.42).

The portable document is an auth.json, not a machine/account-store backup.
Only explicitly allowed credential and presentation fields are copied. JWT
payloads are decoded solely to reject contradictory identities: no signature,
server validity, subscription, or refresh-token usability is established here.
Refresh-token rotation on either computer can invalidate an earlier export.
Callers must choose the latest authoritative token bundle before exporting and
must independently verify imported credentials on the destination computer.

Verified upstream: src-tauri/src/modules/codex_account_import.rs,
import_from_json (1528, 1582), export_accounts (1671), and
extract_codex_tokens_from_value (3047). Unknown auth.json fields are ignored by
the upstream token path; the manager namespace therefore cannot select a route.
"""
from __future__ import annotations

import base64
import binascii
import json
from typing import Any

METADATA_KEY = "codex_agent_manager"
PORTABLE_FORMAT = "codex-agent-manager-account"
MAX_DOCUMENT_BYTES = 8_000_000
MAX_ACCOUNTS = 1000
_AUTH_CLAIM = "https://api.openai.com/auth"
_TOKEN_ALIASES = {
    "id_token": ("id_token", "idToken"),
    "access_token": ("access_token", "accessToken"),
    "refresh_token": ("refresh_token", "refreshToken"),
    "account_id": ("account_id", "accountId"),
}


class PortableAccountError(ValueError):
    """A malformed or contradictory portable OAuth document (never secrets)."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PortableAccountError("账号 JSON 包含重复字段。")
        result[key] = value
    return result


def _decode(raw: Any) -> Any:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise PortableAccountError("账号 JSON 不是 UTF-8。") from None
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_DOCUMENT_BYTES:
            raise PortableAccountError("账号 JSON 超出大小限制。")
        try:
            return json.loads(raw.lstrip("\ufeff"), object_pairs_hook=_pairs)
        except (ValueError, RecursionError):
            raise PortableAccountError("账号 JSON 无效或包含重复字段。") from None
    return raw


def _text(value: Any, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PortableAccountError(f"{field} 必须是字符串。")
    value = value.strip()
    if len(value) > 131_072 or any(ord(char) < 32 for char in value):
        raise PortableAccountError(f"{field} 格式无效。")
    return value


def _consistent(contexts: list[dict], aliases: tuple[str, ...], field: str) -> str:
    values = {_text(context[key], field) for context in contexts for key in aliases if key in context}
    values.discard("")
    if len(values) > 1:
        raise PortableAccountError(f"{field} 包含相互冲突的值。")
    return next(iter(values), "")


def _unwrap(raw: Any) -> dict:
    document = _decode(raw)
    if not isinstance(document, dict):
        raise PortableAccountError("单个账号必须是 JSON 对象；数组请使用批量导入。")
    _assert_oauth_source(document)
    # Backward compatibility is intentionally limited to our own old envelope.
    if document.get("format") == PORTABLE_FORMAT and "authJson" in document:
        nested = _decode(document["authJson"])
        if not isinstance(nested, dict):
            raise PortableAccountError("authJson 必须是 JSON 对象。")
        if any(key in document for key in ("tokens", "access_token", "id_token")):
            raise PortableAccountError("账号同时包含包装凭据和顶层凭据。")
        _assert_oauth_source(nested)
        return nested
    return document


def _assert_oauth_source(document: dict) -> None:
    for field in ("sourceType", "source_type", "token_source_mode"):
        marker = document.get(field)
        if isinstance(marker, str) and marker.lower() in {"web_session", "session", "chatgpt_web_session"}:
            raise PortableAccountError("网页会话不能作为官方 OAuth 账号导出。")
    metadata = document.get("session_meta")
    if isinstance(metadata, dict) and metadata.get("credentialCapability") in {
        "quota_only", "unverified_web_session", "codex_short_lived",
    }:
        raise PortableAccountError("受限网页会话不能作为官方 OAuth 账号导出。")


def _claims(token: str, field: str) -> dict:
    if not token:
        return {}
    # Official access tokens are JWTs. Opaque strings cannot establish a safe
    # OAuth identity and belong to the separate API-key/PAT import pathways.
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise PortableAccountError(f"{field} 不是可识别的 OAuth JWT。")
    try:
        payload = base64.b64decode(parts[1] + "=" * (-len(parts[1]) % 4), altchars=b"-_", validate=True)
        claims = json.loads(payload, object_pairs_hook=_pairs)
    except (ValueError, binascii.Error, UnicodeDecodeError, RecursionError):
        raise PortableAccountError(f"{field} 的 JWT 内容无效。") from None
    if not isinstance(claims, dict):
        raise PortableAccountError(f"{field} 的 JWT 内容必须是对象。")
    return claims


def _identity(claims: dict) -> dict[str, str]:
    auth = claims.get(_AUTH_CLAIM) or {}
    if not isinstance(auth, dict):
        raise PortableAccountError("JWT 的账号身份字段无效。")
    return {field: _text(value, "JWT identity") for field, value in {
        "sub": claims.get("sub"), "iss": claims.get("iss"),
        "chatgpt_user_id": auth.get("chatgpt_user_id"),
        "user_id": auth.get("user_id"),
        "account_id": auth.get("chatgpt_account_id"),
    }.items()}


def _validated_account_id(tokens: dict, *, allow_opaque_access: bool = False) -> str:
    opaque_access = "." not in tokens["access_token"]
    access = _identity({} if opaque_access and allow_opaque_access else _claims(tokens["access_token"], "access_token"))
    identity = _identity(_claims(tokens["id_token"], "id_token"))
    same_user = False
    for field in ("sub", "chatgpt_user_id", "user_id"):
        # Subject identifiers are issuer-scoped; never compare different issuers.
        if field == "sub" and access["iss"] != identity["iss"]:
            continue
        if access[field] and identity[field]:
            if access[field] != identity[field]:
                raise PortableAccountError("access_token 与 id_token 的用户身份不一致。")
            same_user = True
    access_account, id_account = access["account_id"], identity["account_id"]
    if access_account and id_account and access_account != id_account and not same_user:
        raise PortableAccountError("OAuth JWT 的账号身份不一致。")
    selected = tokens.get("account_id") or ""
    # The access token's workspace scope governs the request header. A stale ID
    # token for the same user can refer to another organization and is not proof
    # of another user; do not reject that legitimate organization switch.
    authoritative = access_account or id_account
    if selected and authoritative and selected != authoritative:
        raise PortableAccountError("account_id 与 OAuth JWT 的账号身份不一致。")
    return selected or authoritative


def normalize_portable_oauth_auth(raw: Any) -> dict:
    """Return a fresh, canonical OAuth auth.json; reject conflicting identity.

    Accept standard auth.json, Cockpit token records (nested or flat tokens,
    including its camelCase aliases), and the previous manager envelope.
    This is intentionally an OAuth-only helper, not a general import detector.
    """
    document = _unwrap(raw)
    mode = _consistent([document], ("auth_mode", "authMode"), "auth_mode").lower()
    if mode not in {"", "chatgpt", "oauth", "chatgptauthtokens", "chatgpt_auth_tokens"}:
        raise PortableAccountError("此导出仅支持官方 OAuth 账号。")
    if any(_text(document.get(key), "OPENAI_API_KEY") for key in ("OPENAI_API_KEY", "api_key", "apiKey")):
        raise PortableAccountError("OAuth 账号同时包含 API Key，已拒绝混合导出。")
    container = document.get("tokens", {})
    if not isinstance(container, dict):
        raise PortableAccountError("tokens 必须是 JSON 对象。")
    contexts = [container, document]
    tokens = {field: _consistent(contexts, aliases, field) for field, aliases in _TOKEN_ALIASES.items()}
    if not tokens["access_token"]:
        raise PortableAccountError("OAuth 账号缺少 access_token。")
    # Explicit OAuth bundles can rotate to an opaque access token. A current
    # ID token and refresh credential preserve the known identity; local
    # parsing still does not certify server acceptance.
    account_id = _validated_account_id(tokens, allow_opaque_access=bool(
        mode in {"chatgpt", "oauth", "chatgptauthtokens", "chatgpt_auth_tokens"}
        and tokens["id_token"] and tokens["refresh_token"]
    ))
    # Full tokens are kept together; never obtain a missing refresh token from
    # another account/cache. Missing refresh credentials imply no renewal claim.
    result = {"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": {
        "id_token": tokens["id_token"], "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"] or None,
    }}
    if account_id:
        result["tokens"]["account_id"] = account_id
    last_refresh = _text(document.get("last_refresh"), "last_refresh")
    if last_refresh:
        result["last_refresh"] = last_refresh
    return result


def _metadata_text(value: Any, limit: int = 256) -> str | None:
    if isinstance(value, str) and 0 < len(value.strip()) <= limit and not any(ord(c) < 32 for c in value):
        return value.strip()
    return None


def _safe_metadata(source: dict) -> dict:
    result = {}
    for key in ("label", "importedAt", "exportedAt", "subscriptionExpiresAt", "modelsLastCheckedAt", "modelsRefreshedAt"):
        value = _metadata_text(source.get(key), 120 if key == "label" else 128)
        if value is not None:
            result[key] = value
    group = source.get("group")
    if isinstance(group, dict):
        result["group"] = {key: value for key in ("id", "name") if (value := _metadata_text(group.get(key), 120))}
    models = source.get("models")
    if isinstance(models, list):
        result["models"] = list(dict.fromkeys(value for item in models[:1000] if (value := _metadata_text(item, 160))))
    return result


def portable_account_metadata(raw: Any) -> dict:
    """Read allowlisted display/cache hints, never remote-validity or routing.

    The caller still validates dates, group IDs, model IDs, and plan overrides
    using its normal import policy. These are untrusted presentation hints.
    """
    document = _decode(raw)
    if not isinstance(document, dict):
        return {}
    metadata = document.get(METADATA_KEY)
    if isinstance(metadata, dict) and metadata.get("format") == PORTABLE_FORMAT and metadata.get("version") == 2:
        return _safe_metadata(metadata)
    if document.get("format") == PORTABLE_FORMAT and "authJson" in document:
        return _safe_metadata(document)
    return {}


def build_portable_account_export(auth: Any, metadata: dict | None = None) -> dict:
    """Export only selected OAuth credentials and namespaced presentation hints.

    Never exports cap_sid, DPAPI blobs, keyrings, agent private keys, route
    configuration, model catalogs with secret fields, or cached valid status.
    """
    result = normalize_portable_oauth_auth(auth)
    hints = portable_account_metadata(auth)
    if isinstance(metadata, dict):
        hints.update(_safe_metadata(metadata))
    result[METADATA_KEY] = {"format": PORTABLE_FORMAT, "version": 2, **hints}
    return result


def normalize_portable_oauth_accounts(raw: Any) -> list[dict]:
    """Normalize a single account or Cockpit's exported token-record array.

    Fail the entire batch on an unsupported/malformed item instead of silently
    dropping accounts. Limits are separate from the caller's UI batch limits.
    """
    document = _decode(raw)
    items = document if isinstance(document, list) else [document]
    if not items or len(items) > MAX_ACCOUNTS:
        raise PortableAccountError("账号数组为空或超出数量限制。")
    return [build_portable_account_export(item) for item in items]
