"""Auth services."""
from __future__ import annotations
from agent_manager import core as _core


def _credential_store_mode() -> str:
    value = str(_core.read_toml(_core.CONFIG_FILE).get("cli_auth_credentials_store") or "file").strip().lower()
    return value if value in {"file", "keyring", "auto"} else "auto"



def _jwt_payload(token: str) -> dict:
    return _core._jwt_payload_segment(token, 1)



def _jwt_payload_segment(token: str, index: int) -> dict:
    parts = token.split(".")
    if len(parts) <= index:
        return {}
    encoded = parts[index] + "=" * (-len(parts[index]) % 4)
    try:
        payload = _core.json.loads(_core.base64.urlsafe_b64decode(encoded).decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (ValueError, UnicodeDecodeError, _core.json.JSONDecodeError):
        return {}



def _nested_string(payload: dict, paths: list[tuple[str, ...]]) -> str:
    for path in paths:
        value: Any = payload
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""



def _iso_from_timestamp(value: _core.Any) -> str | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if timestamp > 1_000_000_000_000:
        timestamp /= 1000
    if timestamp <= 0:
        return None
    try:
        return _core.datetime.fromtimestamp(timestamp, tz=_core.timezone.utc).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None



def _jwt_expiry(token: str) -> str | None:
    return _core._iso_from_timestamp(_core._jwt_payload(token).get("exp")) if token else None



def _session_expiry(value: _core.Any) -> str | None:
    timestamp = _core._iso_from_timestamp(value)
    if timestamp:
        return timestamp
    if isinstance(value, str) and value.strip():
        try:
            parsed = _core.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_core.timezone.utc)
            return parsed.astimezone().isoformat(timespec="seconds")
        except ValueError:
            return None
    return None



def _subscription_metadata(id_claims: dict, session_meta: dict | None = None) -> dict:
    auth_claims = (
        id_claims.get("https://api.openai.com/auth")
        if isinstance(id_claims.get("https://api.openai.com/auth"), dict)
        else {}
    )
    session_meta = session_meta if isinstance(session_meta, dict) else {}

    def first_time(*values: Any) -> str | None:
        for value in values:
            parsed = _core._session_expiry(value)
            if parsed:
                return parsed
        return None

    return {
        "subscriptionStartedAt": first_time(
            auth_claims.get("chatgpt_subscription_active_start"),
            session_meta.get("subscriptionStartedAt"),
            session_meta.get("subscription_started_at"),
        ),
        "subscriptionExpiresAt": first_time(
            auth_claims.get("chatgpt_subscription_active_until"),
            session_meta.get("subscriptionExpiresAt"),
            session_meta.get("subscription_expires_at"),
            session_meta.get("planExpiresAt"),
            session_meta.get("plan_expires_at"),
        ),
        "subscriptionLastCheckedAt": first_time(
            auth_claims.get("chatgpt_subscription_last_checked"),
            session_meta.get("subscriptionLastCheckedAt"),
            session_meta.get("subscription_last_checked_at"),
        ),
        "subscriptionMetadataSource": "token",
    }



def _subscription_missing_or_expired(value: _core.Any) -> bool:
    parsed = _core._session_expiry(value)
    if not parsed:
        return True
    try:
        expiry = _core.datetime.fromisoformat(parsed)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=_core.timezone.utc)
    except ValueError:
        return True
    return expiry.astimezone(_core.timezone.utc) <= _core.datetime.now(_core.timezone.utc)



def _is_free_plan(*values: _core.Any) -> bool:
    """Return whether any plan value unambiguously represents a free account."""
    for value in values:
        normalized = _core.re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())
        if normalized in {"free", "chatgptfree", "freeplan", "chatgptfreeplan"}:
            return True
    return False



def _clear_subscription_metadata(target: dict) -> None:
    """Remove paid-subscription fields from a free account or its usage snapshot."""
    for field in (
        "subscriptionStartedAt",
        "subscriptionExpiresAt",
        "subscriptionLastCheckedAt",
        "subscriptionMetadataSource",
    ):
        target[field] = None
    target["subscriptionStatus"] = "not_applicable"
    usage = target.get("usage")
    if isinstance(usage, dict):
        for field in (
            "subscriptionExpiresAt",
            "subscriptionMetadataSource",
        ):
            usage[field] = None
        usage["subscriptionStatus"] = "not_applicable"



def _token_client_id(token: str) -> str:
    claims = _core._jwt_payload(token)
    for key in ("client_id", "azp"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    audience = claims.get("aud")
    if isinstance(audience, str):
        return audience.strip()
    if isinstance(audience, list):
        return next((str(item).strip() for item in audience if str(item).strip()), "")
    return ""



def _synthetic_web_session_id_token(
    email: str,
    account_id: str,
    plan: str,
    user_id: str,
    expires: _core.Any,
) -> str:
    """Build the local claims-only token accepted by Codex's external-token parser.

    This does not grant permissions or refresh access. It only supplies identity
    claims that are absent from /api/auth/session exports; inference capability
    is still verified separately against the Codex backend.
    """
    issued_at = int(_core.time.time())
    expiry_iso = _core._session_expiry(expires)
    expiry = int(_core.datetime.fromisoformat(expiry_iso).timestamp()) if expiry_iso else issued_at + 90 * 24 * 3600
    auth_claims: dict[str, Any] = {"chatgpt_account_id": account_id}
    if plan:
        auth_claims["chatgpt_plan_type"] = plan
    if user_id:
        auth_claims.update({"chatgpt_user_id": user_id, "user_id": user_id})
    payload: dict[str, Any] = {
        "iat": issued_at,
        "exp": expiry,
        "https://api.openai.com/auth": auth_claims,
    }
    if email:
        payload["email"] = email

    def encode(value: dict) -> str:
        raw = _core.json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return _core.base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none', 'typ': 'JWT', 'cam_synthetic': True})}.{encode(payload)}.synthetic"



def _normalize_agent_identity_storage(value: _core.Any) -> str | dict:
    """Normalize the two AgentIdentityStorage forms accepted by official Codex."""
    if isinstance(value, str):
        jwt = value.strip()
        if not _core._looks_like_jwt(jwt):
            raise _core.ManagerError("Agent Identity JWT 格式无效。")
        if len(jwt.encode("utf-8")) > 256_000:
            raise _core.ManagerError("Agent Identity JWT 异常过大，已拒绝导入。")
        claims = _core._jwt_payload(jwt)
        required = ("agent_runtime_id", "agent_private_key", "account_id", "chatgpt_user_id", "plan_type")
        missing = [name for name in required if not str(claims.get(name) or "").strip()]
        if missing:
            raise _core.ManagerError(f"Agent Identity JWT 缺少必需字段：{', '.join(missing)}。")
        _core._validate_agent_identity_private_key(str(claims["agent_private_key"]))
        return jwt
    if not isinstance(value, dict):
        raise _core.ManagerError("Agent Identity 必须是 JWT 字符串或凭据对象。")

    def first(*names: str) -> Any:
        for name in names:
            if name in value and value[name] is not None:
                return value[name]
        return None

    record = {
        "agent_runtime_id": str(first("agent_runtime_id", "agentRuntimeId") or "").strip(),
        "agent_private_key": str(
            first("agent_private_key", "agentPrivateKey", "private_key", "privateKey") or ""
        ).strip(),
        "account_id": str(
            first("account_id", "accountId", "chatgpt_account_id", "chatgptAccountId") or ""
        ).strip(),
        "chatgpt_user_id": str(
            first("chatgpt_user_id", "chatgptUserId", "user_id", "userId") or ""
        ).strip(),
        "email": str(first("email") or "").strip(),
        "plan_type": str(first("plan_type", "planType", "plan") or "").strip(),
    }
    missing = [
        name
        for name in ("agent_runtime_id", "agent_private_key", "account_id", "chatgpt_user_id", "plan_type")
        if not record[name]
    ]
    if missing:
        raise _core.ManagerError(f"Agent Identity 缺少必需字段：{', '.join(missing)}。")
    if len(record["agent_runtime_id"]) > 512 or len(record["account_id"]) > 512:
        raise _core.ManagerError("Agent Identity 标识字段异常过长，已拒绝导入。")
    if len(record["agent_private_key"].encode("utf-8")) > 64_000:
        raise _core.ManagerError("Agent Identity 私钥异常过大，已拒绝导入。")
    _core._validate_agent_identity_private_key(record["agent_private_key"])

    fedramp = first("chatgpt_account_is_fedramp", "chatgptAccountIsFedramp", "is_fedramp", "isFedramp")
    if isinstance(fedramp, str):
        fedramp = fedramp.strip().casefold() in {"1", "true", "yes", "on"}
    record["chatgpt_account_is_fedramp"] = bool(fedramp)
    task_id = str(first("task_id", "taskId") or "").strip()
    if task_id:
        if len(task_id) > 2_048:
            raise _core.ManagerError("Agent Identity task_id 异常过长，已拒绝导入。")
        record["task_id"] = task_id
    return record



def _validate_agent_identity_private_key(value: str) -> None:
    """Validate the PKCS#8 Ed25519 key shape consumed by official Codex."""
    try:
        der = _core.base64.b64decode(value.strip(), validate=True)
    except (ValueError, TypeError) as exc:
        raise _core.ManagerError("Agent Identity 私钥不是有效的 Base64 PKCS#8 数据。") from exc
    ed25519_oid = b"\x06\x03\x2b\x65\x70"
    private_key_octets = b"\x04\x22\x04\x20"
    if not (48 <= len(der) <= 512 and der.startswith(b"\x30") and ed25519_oid in der and private_key_octets in der):
        raise _core.ManagerError("Agent Identity 私钥不是 Codex 可解析的 Ed25519 PKCS#8 密钥。")



def _looks_like_personal_access_token(value: str) -> bool:
    token = value.strip()
    if token.casefold().startswith("bearer "):
        token = token[7:].strip()
    return token.startswith("at-") and len(token) >= 12 and not any(character.isspace() for character in token)



def _looks_like_agent_identity_jwt(value: str) -> bool:
    if not _core._looks_like_jwt(value):
        return False
    claims = _core._jwt_payload(value)
    required = ("agent_runtime_id", "agent_private_key", "account_id", "chatgpt_user_id", "plan_type")
    return all(str(claims.get(name) or "").strip() for name in required)



def _agent_identity_from_auth(auth: dict) -> str | dict | None:
    value: Any = auth.get("agent_identity", auth.get("agentIdentity"))
    credentials = auth.get("credentials") if isinstance(auth.get("credentials"), dict) else None
    if value is None and credentials:
        value = credentials.get("agent_identity", credentials.get("agentIdentity"))
        if value is None and any(
            key in credentials
            for key in ("agent_runtime_id", "agentRuntimeId", "agent_private_key", "agentPrivateKey")
        ):
            value = credentials
    if value is None and any(
        key in auth for key in ("agent_runtime_id", "agentRuntimeId", "agent_private_key", "agentPrivateKey")
    ):
        value = auth
    raw_mode = _core.re.sub(
        r"[^a-z0-9]",
        "",
        str(auth.get("auth_mode") or auth.get("authMode") or "").casefold(),
    )
    if value is None and raw_mode == "agentidentity":
        raw_token = auth.get("token")
        if isinstance(raw_token, str) and raw_token.strip():
            value = raw_token
    if value is None:
        return None
    return _core._normalize_agent_identity_storage(value)



def _auth_bytes_support_codex(auth_bytes: bytes) -> bool:
    """Check whether an imported ChatGPT payload has persistent Codex OAuth material.

    Browser Web Session exports commonly contain an access token that can read
    quota/catalog endpoints but is rejected by the Codex responses endpoint.
    Native Codex snapshots include both ID and refresh tokens from the Codex
    OAuth client. API-key snapshots are independently supported.
    """
    try:
        auth = _core.json.loads(auth_bytes.decode("utf-8"))
    except (UnicodeDecodeError, _core.json.JSONDecodeError):
        return False
    if not isinstance(auth, dict):
        return False
    api_key = str(auth.get("OPENAI_API_KEY") or "").strip()
    personal_access_token = str(auth.get("personal_access_token") or "").strip()
    try:
        agent_identity = _core._agent_identity_from_auth(auth)
    except _core.ManagerError:
        return False
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    token_material = any(
        str(tokens.get(key) or "").strip()
        for key in ("id_token", "access_token", "refresh_token", "account_id")
    )
    # Current Codex may cache a managed Agent Identity alongside persistent
    # ChatGPT OAuth tokens.  That record is auxiliary in ChatGPT mode, not a
    # second credential family.  API keys and PATs remain mutually exclusive
    # with every other primary family.
    primary_families = (
        bool(api_key),
        bool(personal_access_token),
        bool(token_material),
        bool(agent_identity and not token_material),
    )
    if sum(primary_families) > 1:
        return False
    if api_key or personal_access_token or (agent_identity and not token_material):
        return True
    id_token = str(tokens.get("id_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    access_token = str(tokens.get("access_token") or "").strip()
    if not id_token or not refresh_token:
        return False
    if refresh_token.casefold() in {
        "__missing_refresh_token__",
        "placeholder",
        "missing",
        "none",
        "null",
        "n/a",
        "dummy",
    }:
        return False
    header = _core._jwt_payload_segment(id_token, 0)
    if id_token.endswith(".synthetic") or header.get("cam_synthetic") or header.get("cpa_synthetic"):
        return False
    client_id = _core._token_client_id(access_token) or _core._token_client_id(id_token)
    return not client_id or client_id == _core.CODEX_OAUTH_CLIENT_ID



def _codex_auth_projection_bytes(auth_bytes: bytes) -> bytes:
    """Project an internal account snapshot to the official Codex auth schema.

    Account snapshots may carry manager-only metadata and legacy exporter
    markers, so they cannot be copied verbatim into ``~/.codex/auth.json``.
    Native Codex snapshots are different: ``auth_mode`` is officially optional
    and newer releases add fields such as the managed ``agent_identity`` cache.
    Preserve that official shape instead of rebuilding every snapshot from an
    old fixed field list; otherwise switching through Agent Manager can make
    Desktop show a reduced account surface or discard data added by Codex.
    """
    try:
        auth = _core.json.loads(auth_bytes.decode("utf-8"))
    except (UnicodeDecodeError, _core.json.JSONDecodeError) as exc:
        raise _core.ManagerError("账号快照中的 auth.json 无法解析。") from exc
    if not isinstance(auth, dict):
        raise _core.ManagerError("账号快照中的 auth.json 必须是 JSON 对象。")

    for field in ("OPENAI_API_KEY", "personal_access_token", "auth_mode", "authMode", "last_refresh"):
        if field in auth and auth[field] is not None and not isinstance(auth[field], str):
            raise _core.ManagerError(f"账号快照字段 {field} 必须是字符串。")
    if "tokens" in auth and auth["tokens"] is not None and not isinstance(auth["tokens"], dict):
        raise _core.ManagerError("账号快照字段 tokens 必须是对象。")

    api_key = str(auth.get("OPENAI_API_KEY") or "").strip()
    personal_access_token = str(auth.get("personal_access_token") or "").strip()
    agent_identity = _core._agent_identity_from_auth(auth)
    bedrock_api_key = auth.get("bedrock_api_key")
    bedrock_access_keys = auth.get("bedrock_access_keys")
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    for field in ("id_token", "access_token", "refresh_token", "account_id"):
        if field in tokens and tokens[field] is not None and not isinstance(tokens[field], str):
            raise _core.ManagerError(f"账号快照字段 tokens.{field} 必须是字符串。")
    token_material = any(
        str(tokens.get(key) or "").strip()
        for key in ("id_token", "access_token", "refresh_token", "account_id")
    )
    families = [
        name
        for name, present in (
            ("API Key", bool(api_key)),
            ("Personal Access Token", bool(personal_access_token)),
            # Managed ChatGPT auth is allowed to persist a derived Agent
            # Identity record beside OAuth tokens in current official Codex.
            ("Agent Identity", bool(agent_identity and not token_material)),
            ("OAuth", bool(token_material)),
            ("Bedrock API Key", bedrock_api_key is not None),
            ("Bedrock Access Keys", bedrock_access_keys is not None),
        )
        if present
    ]
    if len(families) > 1:
        raise _core.ManagerError(f"账号快照混合了多种凭据（{'、'.join(families)}），已拒绝写入 Codex。")

    raw_mode = str(auth.get("auth_mode") or auth.get("authMode") or "").strip()
    normalized_mode = _core.re.sub(r"[^a-z0-9]", "", raw_mode.casefold())
    allowed_modes = {
        "": {""},
        "api_key": {"", "apikey"},
        "pat": {"", "pat", "personalaccesstoken"},
        "agent": {"", "agentidentity"},
        "oauth": {"", "oauth", "chatgpt", "chatgptauthtokens"},
        "bedrock_api_key": {"", "bedrockapikey"},
        "bedrock_access_keys": {"", "bedrockaccesskeys"},
    }
    family = (
        "api_key"
        if api_key
        else "pat"
        if personal_access_token
        else "oauth"
        if token_material
        else "agent"
        if agent_identity
        else "bedrock_api_key"
        if bedrock_api_key is not None
        else "bedrock_access_keys"
        if bedrock_access_keys is not None
        else ""
    )
    if normalized_mode not in allowed_modes[family]:
        raise _core.ManagerError(f"账号快照包含与凭据不匹配的 auth_mode：{raw_mode or '空'}。")

    if api_key:
        projected = {"auth_mode": "apikey", "OPENAI_API_KEY": api_key}
        return _core.json.dumps(projected, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    if personal_access_token:
        projected = {"OPENAI_API_KEY": None, "personal_access_token": personal_access_token}
        return _core.json.dumps(projected, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    if agent_identity and not token_material:
        projected = {
            "auth_mode": "agentIdentity",
            "OPENAI_API_KEY": None,
            "agent_identity": agent_identity,
        }
        return _core.json.dumps(projected, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    if bedrock_api_key is not None or bedrock_access_keys is not None:
        raise _core.ManagerError(
            "该快照使用新版 Codex Bedrock 凭据；Agent Manager 尚不接管这类凭据，"
            "已保留原文件且拒绝切换。"
        )

    if not token_material:
        raise _core.ManagerError("账号快照缺少可写入 Codex 的 OAuth Token。")
    if not _core._auth_bytes_support_codex(auth_bytes):
        raise _core.ManagerError(
            "该快照不是可持久使用的 Codex OAuth 凭据（需要真实 id_token、refresh_token 与兼容的 OAuth 客户端）。"
        )
    access_token = str(tokens.get("access_token") or "").strip()
    id_token = str(tokens.get("id_token") or "").strip()
    if not access_token:
        raise _core.ManagerError("账号快照缺少 access_token，无法切换到 Codex。")
    if not id_token:
        raise _core.ManagerError("账号快照缺少 id_token，无法切换到 Codex。")

    projected = {
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": id_token,
            "access_token": access_token,
            # Codex's OAuth parser requires the key even for a short-lived
            # access-token credential that has no refresh chain.
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "account_id": str(tokens.get("account_id") or "").strip() or None,
        },
    }
    # Persistent ChatGPT OAuth resolves to Chatgpt when auth_mode is absent in
    # current Codex (AuthDotJson::resolved_mode). Always emit that native form,
    # including for legacy Cockpit/manager snapshots that explicitly declared
    # chatgptAuthTokens. Keeping the redundant marker made Codex Desktop render
    # the reduced API-style account menu even though the OAuth tokens were valid.
    last_refresh = str(auth.get("last_refresh") or "").strip()
    if last_refresh:
        projected["last_refresh"] = last_refresh
    if agent_identity is not None:
        projected["agent_identity"] = agent_identity
    return _core.json.dumps(projected, ensure_ascii=False, separators=(",", ":")).encode("utf-8")



def _api_key_auth_projection_bytes(api_key: str) -> bytes:
    """Build the one canonical API-key auth document accepted by Codex."""
    key = str(api_key or "").strip()
    if not key:
        raise _core.ManagerError("中转站 API Key 为空。")
    return _core._codex_auth_projection_bytes(
        _core.json.dumps(
            {"auth_mode": "apikey", "OPENAI_API_KEY": key},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )



def _account_codex_compatible(account: dict) -> bool:
    if account.get("codexCompatible") is True and account.get("quotaOnly") is not True:
        return True
    if account.get("codexCompatible") is False or account.get("quotaOnly") is True:
        return False
    return account.get("sourceType") != "web_session"



def _chatgpt_organization_id(access_token: str) -> str:
    claims = _core._jwt_payload(access_token)
    auth_claims = (
        claims.get("https://api.openai.com/auth")
        if isinstance(claims.get("https://api.openai.com/auth"), dict)
        else {}
    )
    for key in (
        "organization_id",
        "chatgpt_organization_id",
        "chatgpt_org_id",
        "org_id",
        "poid",
        "POID",
    ):
        value = auth_claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    organizations = auth_claims.get("organizations")
    if isinstance(organizations, list):
        records = [item for item in organizations if isinstance(item, dict)]
        preferred = next((item for item in records if item.get("is_default") is True), None)
        for item in (preferred, records[0] if records else None):
            value = item.get("id") if isinstance(item, dict) else None
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""



def _chatgpt_credentials_from_auth_bytes(auth_bytes: bytes) -> dict:
    try:
        auth = _core.json.loads(auth_bytes.decode("utf-8"))
    except (UnicodeDecodeError, _core.json.JSONDecodeError) as exc:
        raise _core.ManagerError("账号认证快照不是有效的 UTF-8 JSON。") from exc
    if not isinstance(auth, dict):
        raise _core.ManagerError("账号认证快照必须是 JSON 对象。")
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    session_meta = auth.get("session_meta") if isinstance(auth.get("session_meta"), dict) else {}
    access_token = str(tokens.get("access_token") or auth.get("personal_access_token") or "").strip()
    if not access_token:
        raise _core.ManagerError("该账号没有可用于探测额度和模型的 access_token。")
    access_claims = _core._jwt_payload(access_token)
    id_claims = _core._jwt_payload(str(tokens.get("id_token") or ""))
    account_id = (
        str(tokens.get("account_id") or "").strip()
        or str(
            session_meta.get("accountId")
            or session_meta.get("account_id")
            or session_meta.get("chatgpt_account_id")
            or ""
        ).strip()
        or _core._nested_string(
        access_claims,
        [
            ("https://api.openai.com/auth", "chatgpt_account_id"),
            ("https://api.openai.com/auth", "account_id"),
            ("chatgpt_account_id",),
            ("account_id",),
        ],
        )
        or _core._nested_string(
        id_claims,
        [
            ("https://api.openai.com/auth", "chatgpt_account_id"),
            ("https://api.openai.com/auth", "account_id"),
            ("chatgpt_account_id",),
            ("account_id",),
        ],
        )
    )
    if not account_id:
        raise _core.ManagerError("该账号 Token 中缺少 ChatGPT Account ID。")
    subscription = _core._subscription_metadata(id_claims, session_meta)
    return {
        "accessToken": access_token,
        "accountId": account_id,
        "tokenExpiresAt": _core._jwt_expiry(access_token)
        or _core._jwt_expiry(str(tokens.get("id_token") or ""))
        or _core._session_expiry(session_meta.get("expires")),
        **subscription,
    }



def _codex_oauth_auth_document(auth_bytes: bytes) -> dict:
    try:
        auth = _core.json.loads(auth_bytes.decode("utf-8"))
    except (UnicodeDecodeError, _core.json.JSONDecodeError) as exc:
        raise _core.ManagerError("账号 OAuth 快照不是有效的 UTF-8 JSON。") from exc
    if not isinstance(auth, dict):
        raise _core.ManagerError("账号 OAuth 快照必须是 JSON 对象。")
    tokens = auth.get("tokens")
    if not isinstance(tokens, dict):
        raise _core.ManagerError("账号 OAuth 快照缺少 tokens 对象。")
    return auth



def _codex_oauth_refresh_due(auth_bytes: bytes) -> bool:
    auth = _core._codex_oauth_auth_document(auth_bytes)
    tokens = auth["tokens"]
    access_token = str(tokens.get("access_token") or "").strip()
    expires_at = _core._parsed_datetime(_core._jwt_expiry(access_token))
    if expires_at is not None:
        return expires_at <= _core.datetime.now(_core.timezone.utc) + _core.timedelta(
            seconds=_core.CODEX_OAUTH_REFRESH_WINDOW_SECONDS
        )
    last_refresh = _core._parsed_datetime(auth.get("last_refresh"))
    return bool(
        last_refresh
        and last_refresh
        <= _core.datetime.now(_core.timezone.utc) - _core.timedelta(seconds=_core.CODEX_OAUTH_REFRESH_FALLBACK_SECONDS)
    )



def _codex_oauth_auth_is_newer(candidate_auth_bytes: bytes, baseline_auth_bytes: bytes) -> bool:
    candidate = _core._codex_oauth_auth_document(candidate_auth_bytes)
    baseline = _core._codex_oauth_auth_document(baseline_auth_bytes)
    # Rotation time also orders opaque access tokens. Never allow a stale JWT
    # with a longer expiry to replace a credential explicitly refreshed later.
    candidate_refresh = _core._parsed_datetime(candidate.get("last_refresh"))
    baseline_refresh = _core._parsed_datetime(baseline.get("last_refresh"))
    if candidate_refresh and baseline_refresh and candidate_refresh != baseline_refresh:
        return candidate_refresh > baseline_refresh
    candidate_expiry = _core._parsed_datetime(
        _core._jwt_expiry(str(candidate["tokens"].get("access_token") or ""))
    )
    baseline_expiry = _core._parsed_datetime(
        _core._jwt_expiry(str(baseline["tokens"].get("access_token") or ""))
    )
    if candidate_expiry is not None or baseline_expiry is not None:
        if candidate_expiry is None:
            return False
        if baseline_expiry is None:
            return True
        if candidate_expiry != baseline_expiry:
            return candidate_expiry > baseline_expiry
    return bool(
        candidate_refresh
        and (baseline_refresh is None or candidate_refresh > baseline_refresh)
    )

