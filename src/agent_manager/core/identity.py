"""Identity services."""
from __future__ import annotations
from agent_manager import core as _core


def _identity_from_auth_bytes(auth_bytes: bytes) -> dict:
    if len(auth_bytes) > 1_000_000:
        raise _core.ManagerError("auth.json 超过 1 MB，已拒绝导入。")
    try:
        auth = _core.json.loads(auth_bytes.decode("utf-8"))
    except (UnicodeDecodeError, _core.json.JSONDecodeError) as exc:
        raise _core.ManagerError("auth.json 不是有效的 UTF-8 JSON。") from exc
    if not isinstance(auth, dict):
        raise _core.ManagerError("auth.json 必须是 JSON 对象。")

    for field in ("OPENAI_API_KEY", "personal_access_token", "auth_mode", "authMode"):
        if field in auth and auth[field] is not None and not isinstance(auth[field], str):
            raise _core.ManagerError(f"auth.json 字段 {field} 必须是字符串。")
    if "tokens" in auth and auth["tokens"] is not None and not isinstance(auth["tokens"], dict):
        raise _core.ManagerError("auth.json 字段 tokens 必须是对象。")

    agent_identity = _core._agent_identity_from_auth(auth)
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    for field in ("id_token", "access_token", "refresh_token", "account_id"):
        if field in tokens and tokens[field] is not None and not isinstance(tokens[field], str):
            raise _core.ManagerError(f"auth.json 字段 tokens.{field} 必须是字符串。")
    session_meta = auth.get("session_meta") if isinstance(auth.get("session_meta"), dict) else {}
    id_token = str(tokens.get("id_token") or "")
    personal_access_token = str(auth.get("personal_access_token") or "").strip()
    access_token = str(tokens.get("access_token") or personal_access_token)
    claims = _core._jwt_payload(id_token)
    access_claims = _core._jwt_payload(access_token)
    agent_claims = _core._jwt_payload(agent_identity) if isinstance(agent_identity, str) else {}
    agent_record = agent_identity if isinstance(agent_identity, dict) else agent_claims
    token_material = any(
        str(tokens.get(key) or "").strip()
        for key in ("id_token", "access_token", "refresh_token", "account_id")
    )
    # Current Codex stores a derived Agent Identity cache beside native OAuth
    # credentials. OAuth claims remain authoritative; the cache may only fill
    # missing display/account metadata and never changes the primary family.
    identity_agent_record = agent_record
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
        claims,
        [
            ("https://api.openai.com/auth", "chatgpt_account_id"),
            ("https://api.openai.com/auth", "account_id"),
            ("chatgpt_account_id",),
            ("account_id",),
        ],
        )
        or (
            str(identity_agent_record.get("account_id") or identity_agent_record.get("accountId") or "").strip()
            if isinstance(identity_agent_record, dict)
            else ""
        )
    )
    api_key = str(auth.get("OPENAI_API_KEY") or "").strip()
    auth_mode = (
        "agent_identity"
        if agent_identity and not token_material
        else
        "personal_access_token"
        if personal_access_token
        else
        "chatgpt"
        if token_material or account_id or id_token
        else "apikey"
        if api_key
        else "unknown"
    )
    if auth_mode == "unknown":
        raise _core.ManagerError("auth.json 中没有可识别的 ChatGPT Token 或 API Key。")
    declared_auth_mode = str(auth.get("auth_mode") or auth.get("authMode") or "").strip()
    normalized_declared_auth_mode = _core.re.sub(
        r"[^a-z0-9]",
        "",
        declared_auth_mode.casefold(),
    )
    # Current Desktop/App Server can infer ChatGPT OAuth from the token bundle
    # when older auth.json files omit ``auth_mode``.  Only an explicit,
    # contradictory declaration needs re-application; absence alone is not an
    # API login and must not be inferred from the lower-left menu appearance.
    auth_contract_valid = not (
        auth_mode == "chatgpt"
        and bool(normalized_declared_auth_mode)
        and normalized_declared_auth_mode != "chatgpt"
    )

    email = (
        _core._nested_string(
        claims or access_claims,
        [
            ("email",),
            ("https://api.openai.com/profile", "email"),
            ("https://api.openai.com/auth", "email"),
        ],
    ) or str(session_meta.get("email") or "").strip() or (
        str(identity_agent_record.get("email") or "").strip()
        if isinstance(identity_agent_record, dict)
        else ""
    )
    )
    name = _core._nested_string(
        claims or access_claims,
        [
            ("name",),
            ("https://api.openai.com/profile", "name"),
        ],
    ) or str(session_meta.get("name") or "").strip()
    plan = (
        _core._nested_string(
        claims or access_claims,
        [
            ("chatgpt_plan_type",),
            ("plan_type",),
            ("https://api.openai.com/auth", "chatgpt_plan_type"),
        ],
    ) or str(session_meta.get("plan") or "").strip() or (
        str(identity_agent_record.get("plan_type") or identity_agent_record.get("planType") or "").strip()
        if isinstance(identity_agent_record, dict)
        else ""
    )
    )
    subscription = _core._subscription_metadata(claims or access_claims, session_meta)
    runtime_identity = (
        str(identity_agent_record.get("agent_runtime_id") or identity_agent_record.get("agentRuntimeId") or "").strip()
        if isinstance(identity_agent_record, dict) and not token_material
        else ""
    )
    principal_id = _core._nested_string(
        claims or access_claims,
        [
            ("sub",),
            ("user_id",),
            ("https://api.openai.com/auth", "user_id"),
            ("https://api.openai.com/auth", "chatgpt_user_id"),
        ],
    ) or str(session_meta.get("userId") or session_meta.get("user_id") or "").strip() or (
        str(
            identity_agent_record.get("chatgpt_user_id")
            or identity_agent_record.get("chatgptUserId")
            or identity_agent_record.get("user_id")
            or identity_agent_record.get("userId")
            or ""
        ).strip()
        if isinstance(identity_agent_record, dict)
        else ""
    )
    legacy_identity_material = (
        runtime_identity
        or account_id
        or email.casefold()
        or api_key
        or personal_access_token
        or _core.hashlib.sha256(auth_bytes).hexdigest()
    )
    # A Team workspace account id is shared by every member.  Older builds used
    # that workspace id alone, so a batch of different Team users collapsed to
    # one fingerprint.  Bind ChatGPT credentials to both workspace and user
    # identity while keeping a legacy alias for existing encrypted snapshots.
    if auth_mode == "chatgpt" and not runtime_identity:
        user_identity = principal_id or email.casefold()
        identity_material = (
            f"{account_id.casefold()}|{user_identity.casefold()}"
            if account_id and user_identity
            else user_identity
            or account_id
            or _core.hashlib.sha256(auth_bytes).hexdigest()
        )
    else:
        identity_material = legacy_identity_material
    fingerprint = _core.hashlib.sha256(f"{auth_mode}:{identity_material}".encode("utf-8")).hexdigest()
    legacy_fingerprint = _core.hashlib.sha256(
        f"{auth_mode}:{legacy_identity_material}".encode("utf-8")
    ).hexdigest()
    legacy_fingerprints = []
    if legacy_fingerprint != fingerprint:
        legacy_fingerprints.append(legacy_fingerprint)
    if agent_identity and token_material and isinstance(agent_record, dict):
        # Builds before the OAuth-cache fix treated this auxiliary record as
        # the primary family. Accept that historical snapshot fingerprint once
        # so existing encrypted accounts remain switchable after upgrading.
        old_runtime_identity = str(
            agent_record.get("agent_runtime_id") or agent_record.get("agentRuntimeId") or ""
        ).strip()
        old_account_id = str(
            agent_record.get("account_id") or agent_record.get("accountId") or account_id
        ).strip()
        old_email = str(agent_record.get("email") or email).strip()
        old_identity_material = (
            old_runtime_identity
            or old_account_id
            or old_email.casefold()
            or api_key
            or personal_access_token
            or _core.hashlib.sha256(auth_bytes).hexdigest()
        )
        old_agent_fingerprint = _core.hashlib.sha256(
            f"agent_identity:{old_identity_material}".encode("utf-8")
        ).hexdigest()
        if old_agent_fingerprint not in legacy_fingerprints and old_agent_fingerprint != fingerprint:
            legacy_fingerprints.append(old_agent_fingerprint)
    display = email or name or (
        "OpenAI API Key"
        if auth_mode == "apikey"
        else "Codex Agent Identity"
        if auth_mode == "agent_identity"
        else "Codex Personal Access Token"
        if auth_mode == "personal_access_token"
        else "ChatGPT 账号"
    )
    return {
        "authMode": auth_mode,
        "declaredAuthMode": declared_auth_mode,
        "authContractValid": auth_contract_valid,
        "accountId": account_id,
        "email": email,
        "name": name,
        "plan": plan,
        "tokenExpiresAt": _core._jwt_expiry(access_token) or _core._jwt_expiry(id_token) or _core._session_expiry(session_meta.get("expires")),
        "refreshCapable": bool(
            auth_mode == "chatgpt"
            and str(tokens.get("refresh_token") or "").strip()
            and str(tokens.get("refresh_token") or "").strip().casefold()
            not in {"__missing_refresh_token__", "placeholder", "missing", "none", "null", "n/a", "dummy"}
        ),
        **subscription,
        "fingerprint": fingerprint,
        "legacyFingerprints": legacy_fingerprints,
        "principalId": principal_id,
        "display": display,
        "importFormat": str(session_meta.get("importFormat") or "").strip(),
    }



def _account_matches_identity(account: dict, identity: dict) -> bool:
    if "chatgpt" in {account.get("authMode"), identity.get("authMode")}:
        # A workspace ID is shared by Team members. Even an exact cached or
        # imported fingerprint must not override conflicting visible identity.
        for field in ("email", "principalId", "accountId"):
            stored_value = str(account.get(field) or "").strip().casefold()
            live_value = str(identity.get(field) or "").strip().casefold()
            if stored_value and live_value and stored_value != live_value:
                return False
    stored = str(account.get("fingerprint") or "")
    current = str(identity.get("fingerprint") or "")
    if not stored or not current:
        return False
    if stored == current:
        return True
    legacy = {str(value) for value in identity.get("legacyFingerprints", []) if str(value)}
    if stored not in legacy:
        return False
    # A legacy Team fingerprint may be shared across different members.  Only
    # migrate it when the visible principal still agrees.
    identity_email = str(identity.get("email") or "").strip().casefold()
    account_email = str(account.get("email") or "").strip().casefold()
    return bool(identity_email and account_email and identity_email == account_email)



def _find_account_for_identity(settings: dict, identity: dict) -> dict | None:
    accounts = settings.get("accounts", [])
    exact = next(
        (
            item
            for item in accounts
            if str(item.get("fingerprint") or "") == str(identity.get("fingerprint") or "")
            and _core._account_matches_identity(item, identity)
        ),
        None,
    )
    if exact:
        return exact
    return next((item for item in accounts if _core._account_matches_identity(item, identity)), None)



def _import_value(payloads: list[dict], paths: tuple[tuple[str, ...], ...]) -> _core.Any:
    """Return the first useful value from common export-schema paths."""
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for path in paths:
            current: Any = payload
            for key in path:
                if not isinstance(current, dict) or key not in current:
                    current = None
                    break
                current = current[key]
            if current is not None and (not isinstance(current, str) or current.strip()):
                return current
    return None



def _import_string(payloads: list[dict], paths: tuple[tuple[str, ...], ...]) -> str:
    value = _core._import_value(payloads, paths)
    return str(value).strip() if value is not None else ""



def _import_credential_string(
    payloads: list[dict],
    paths: tuple[tuple[str, ...], ...],
    label: str,
) -> str:
    value = _core._import_value(payloads, paths)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _core.ManagerError(f"{label} 必须是字符串。")
    return value.strip()



def _looks_like_jwt(value: str) -> bool:
    token = value.strip()
    if token.casefold().startswith("bearer "):
        token = token[7:].strip()
    parts = token.split(".")
    return len(parts) == 3 and all(parts[:2]) and all(_core.re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in parts[:2])



def _decode_import_value(value: _core.Any, *, max_layers: int = 4) -> _core.Any:
    """Decode JSON that has been stringified by one or more export tools."""
    current = value
    for _ in range(max_layers):
        if not isinstance(current, str):
            break
        raw = current.strip().lstrip("\ufeff\u200b\u200c\u200d\u2060")
        if not raw:
            return ""
        bearer = raw[7:].strip() if raw.casefold().startswith("bearer ") else raw
        if _core._looks_like_personal_access_token(bearer):
            return {"auth_mode": "personalAccessToken", "personal_access_token": bearer}
        if _core._looks_like_jwt(bearer):
            if _core._looks_like_agent_identity_jwt(bearer):
                return {"auth_mode": "agentIdentity", "agent_identity": bearer}
            return {"accessToken": bearer, "token_source_mode": "web_session"}
        if _core.re.fullmatch(r"sk-[A-Za-z0-9_.-]{8,}", raw):
            return {"OPENAI_API_KEY": raw}
        try:
            decoded = _core.json.loads(raw)
        except _core.json.JSONDecodeError:
            break
        if decoded == current:
            break
        current = decoded
    return current



def _credential_shaped(payload: _core.Any) -> bool:
    if not isinstance(payload, dict):
        return False
    direct = {
        "OPENAI_API_KEY",
        "accessToken",
        "access_token",
        "idToken",
        "id_token",
        "refreshToken",
        "refresh_token",
        "sessionToken",
        "session_token",
        "apiKey",
        "api_key",
        "bearerToken",
        "bearer_token",
        "authorization",
        "personal_access_token",
        "personalAccessToken",
        "at_token",
        "agent_identity",
        "agentIdentity",
        "agent_runtime_id",
        "agentRuntimeId",
        "agent_private_key",
        "agentPrivateKey",
    }
    if direct.intersection(payload):
        return True
    if isinstance(payload.get("token"), str) and (
        _core._looks_like_jwt(str(payload["token"]))
        or _core._looks_like_personal_access_token(str(payload["token"]))
    ):
        return True
    for key in ("tokens", "token", "credentials"):
        nested = payload.get(key)
        if isinstance(nested, dict) and direct.intersection(nested):
            return True
        if isinstance(nested, dict) and any(
            field in nested for field in ("apiKey", "api_key", "chatgpt_account_id", "account_id")
        ):
            return True
    return False



def _provider_models_from_import(value: _core.Any) -> list[str]:
    models: list[str] = []
    seen: set[str] = set()
    # Manager exports use a flat list. Bound it before allocating entry tuples.
    entries = (
        (("", entry) for entry in value[:_core.MAX_MODEL_CATALOG_ITEMS])
        if isinstance(value, list)
        else _core._model_entries(value)[:_core.MAX_MODEL_CATALOG_ITEMS]
    )
    for fallback, entry in entries:
        model = _core._model_id_from_entry(entry, fallback)
        if model and model not in seen:
            seen.add(model)
            models.append(model)
    return models



def _account_models_from_import_candidate(payload: _core.Any) -> list[str]:
    """Preserve optional model metadata carried beside exported credentials."""
    if not isinstance(payload, dict):
        return []
    contexts = [payload]
    for key in ("account", "metadata", "meta", "profile", "config"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            contexts.append(nested)
    value = _core._import_value(
        contexts,
        (
            ("models",),
            ("availableModels",),
            ("available_models",),
            ("modelCatalog",),
            ("model_catalog",),
            ("codexModels",),
            ("codex_models",),
        ),
    )
    models = _core._provider_models_from_import(value)
    selected_model = _core._import_string(
        contexts,
        (("model",), ("modelId",), ("model_id",), ("defaultModel",), ("default_model",)),
    )
    if selected_model and selected_model not in models:
        models.insert(0, selected_model)
    return models[:2_000]



def _imported_group_id(payload: _core.Any) -> str:
    if not isinstance(payload, dict):
        return ""
    direct = payload.get("groupId") if "groupId" in payload else payload.get("group_id")
    if direct is None and isinstance(payload.get("group"), dict):
        direct = payload["group"].get("id")
    return str(direct or "").strip()



def _batch_target_group_id(settings: dict, root: dict, document: dict, default: str = "official") -> str:
    if "groupId" in root:
        raw_group = root.get("groupId")
        if not isinstance(raw_group, str) or not raw_group.strip():
            raise _core.ManagerError("批量导入目标 groupId 必须是非空字符串。")
        group_id = raw_group.strip()
    else:
        group_id = str(document.get("groupId") or default).strip() or default
    _core._account_group(settings, group_id)
    return group_id



def _provider_export_shaped(payload: _core.Any) -> bool:
    if not isinstance(payload, dict):
        return False
    contexts = [payload]
    for key in ("provider", "config", "credentials", "env"):
        if isinstance(payload.get(key), dict):
            contexts.append(payload[key])
    raw_key = _core._import_value(
        contexts,
        (
            ("openai_api_key",),
            ("OPENAI_API_KEY",),
            ("apiKey",),
            ("api_key",),
            ("experimental_bearer_token",),
        ),
    )
    key = raw_key.strip() if isinstance(raw_key, str) else ""
    base_url = _core._import_string(
        contexts,
        (
            ("api_base_url",),
            ("apiBaseUrl",),
            ("baseUrl",),
            ("base_url",),
            ("OPENAI_BASE_URL",),
            ("endpoint",),
        ),
    )
    auth_mode = _core.re.sub(
        r"[^a-z0-9]",
        "",
        _core._import_string(contexts, (("auth_mode",), ("authMode",), ("type",))).casefold(),
    )
    provider_markers = {
        "api_provider_mode",
        "api_provider_id",
        "api_provider_name",
        "api_model_catalog",
        "api_base_url",
        "model_providers",
    }
    return bool(key and (base_url or auth_mode == "apikey") and (provider_markers.intersection(payload) or base_url))



def _provider_from_import_candidate(payload: _core.Any) -> dict | None:
    if not _core._provider_export_shaped(payload):
        return None
    contexts = [payload]
    for key_name in ("provider", "config", "credentials", "env"):
        if isinstance(payload.get(key_name), dict):
            contexts.append(payload[key_name])
    key = _core._import_credential_string(
        contexts,
        (
            ("openai_api_key",),
            ("OPENAI_API_KEY",),
            ("apiKey",),
            ("api_key",),
            ("experimental_bearer_token",),
        ),
        "API Key",
    )
    key = _core._validated_provider_secret(key)
    base_url = _core._import_string(
        contexts,
        (
            ("api_base_url",),
            ("apiBaseUrl",),
            ("baseUrl",),
            ("base_url",),
            ("OPENAI_BASE_URL",),
            ("endpoint",),
        ),
    ).rstrip("/")
    provider_mode = _core._import_string(contexts, (("api_provider_mode",), ("providerMode",))).casefold()
    if not base_url and provider_mode in {"", "openai_builtin", "openai"}:
        base_url = "https://api.openai.com/v1"
    parsed = _core.urllib.parse.urlparse(base_url)
    host_label = str(parsed.hostname or "api_provider").replace(".", "_")
    raw_id = _core._import_string(
        contexts,
        (("api_provider_id",), ("providerId",), ("provider_id",)),
    )
    slug_source = raw_id or host_label or "api_provider"
    provider_id = _core.re.sub(r"[^a-z0-9_]+", "_", slug_source.casefold().replace("-", "_"))[:64].strip("_")
    if not provider_id or not provider_id[0].isalpha():
        provider_id = f"provider_{provider_id or 'imported'}"[:64]
    name = _core._import_string(
        contexts,
        (("api_provider_name",), ("providerName",), ("provider_name",), ("name",), ("label",), ("email",)),
    ) or str(parsed.hostname or provider_id)
    model_value = _core._import_value(
        contexts,
        (
            ("api_model_catalog",),
            ("modelCatalog",),
            ("model_catalog",),
            ("models",),
        ),
    )
    imported_catalog = _core._parse_provider_model_catalog(model_value)
    models = imported_catalog["models"]
    model = _core._import_string(contexts, (("api_model",), ("model",), ("model_id",), ("modelId",)))
    if model and model not in models:
        models.insert(0, model)
    env_key = _core._safe_imported_provider_env_key(
        _core._import_string(contexts, (("envKey",), ("env_key",), ("api_env_key",))),
        f"{provider_id.upper()}_API_KEY",
    )
    return {
        "id": provider_id,
        # Import-only metadata. save_provider() persists an explicit allow-list,
        # so this marker never reaches settings or exported account material.
        "_idWasExplicit": bool(raw_id),
        "name": name[:160],
        "baseUrl": base_url,
        "envKey": env_key,
        "key": key,
        "models": models,
        "modelCapabilities": imported_catalog["modelCapabilities"],
        "model": model or (models[0] if models else ""),
        "wireApi": "responses",
        "presetId": _core._import_string(
            contexts,
            (("presetId",), ("preset_id",), ("api_provider_preset",)),
        ),
        "portalUrl": _core._import_string(
            contexts,
            (("portalUrl",), ("portal_url",), ("dashboardUrl",), ("dashboard_url",), ("website",)),
        ),
        "integrationKind": _core._import_string(
            contexts,
            (("integrationKind",), ("integration_kind",), ("adapter",)),
        ),
        "modelsEndpoint": _core._import_string(
            contexts,
            (("modelsEndpoint",), ("models_endpoint",), ("models_url",), ("modelsUrl",)),
        ),
        "balanceEndpoint": _core._import_string(
            contexts,
            (("balanceEndpoint",), ("balance_endpoint",), ("api_balance_endpoint",)),
        ),
        "importSource": "cockpit_or_openai_compatible",
    }



def _detect_import_format(document: dict, candidate: dict, source_type: str) -> str:
    format_name = str(document.get("format") or candidate.get("format") or "").strip()
    if format_name:
        return format_name[:80]
    if isinstance(candidate.get("credentials"), dict):
        return "Sub2API / credentials"
    if isinstance(candidate.get("providerSpecificData"), dict) or "provider" in candidate:
        return "9Router"
    if isinstance(candidate.get("tokens"), dict) and isinstance(candidate.get("meta"), dict):
        return "Codex Manager"
    if isinstance(candidate.get("tokens"), dict):
        refresh = str(candidate["tokens"].get("refresh_token") or candidate["tokens"].get("refreshToken") or "")
        return (
            "AxonHub / auth.json"
            if refresh.casefold()
            in {"placeholder", "missing", "__missing_refresh_token__", "none", "null", "n/a", "dummy"}
            else "Codex auth.json"
        )
    if str(candidate.get("type") or "").casefold() == "codex":
        return "CPA / Cockpit"
    return "Web Session" if source_type == "web_session" else "OpenAI API Key"



def _normalize_import_auth_payload(raw: str | dict) -> tuple[bytes, str]:
    if isinstance(raw, str) and not raw.strip():
        raise _core.ManagerError("请选择或粘贴账号 JSON。")
    if not isinstance(raw, (str, dict)):
        raise _core.ManagerError("账号 JSON 必须是对象。")
    raw_size = len(raw.encode("utf-8", errors="replace")) if isinstance(raw, str) else len(
        _core.json.dumps(raw, ensure_ascii=False).encode("utf-8")
    )
    if raw_size > _core.MAX_IMPORT_DOCUMENT_BYTES:
        raise _core.ManagerError(f"单个账号超过 {_core.MAX_IMPORT_DOCUMENT_BYTES // 1_000_000} MB，已拒绝导入。")
    # The normalizer builds fresh canonical dictionaries and never edits the
    # source. Serializing an entire model catalog again just to copy it is wasteful.
    document = _core._decode_import_value(raw)
    if isinstance(document, list):
        raise _core.ManagerError("检测到多个账号，请使用批量导入。")
    if not isinstance(document, dict):
        raise _core.ManagerError("账号内容不是可识别的 JSON、JWT 或 API Key。")

    source_type = "codex_auth"
    candidate = document
    # Exporters frequently wrap credentials several times or encode a JSON
    # object as a JSON string. Unwrap only known container keys and keep the
    # original document as a metadata context.
    for _ in range(8):
        changed = False
        for key in (
            "authJson",
            "auth_json",
            "auth",
            "session",
            "session_json",
            "webSession",
            "web_session",
            "payload",
            "result",
            "data",
        ):
            nested = _core._decode_import_value(candidate.get(key))
            if not isinstance(nested, dict):
                continue
            if key in {"payload", "result", "data"} and (_core._credential_shaped(candidate) or not _core._credential_shaped(nested)):
                continue
            candidate = nested
            changed = True
            if key in {"session", "session_json", "webSession", "web_session"}:
                source_type = "web_session"
            break
        if not changed:
            break

    contexts = [candidate]
    if candidate is not document:
        contexts.append(document)

    source_marker = _core._import_string(
        contexts,
        (("token_source_mode",), ("sourceType",), ("source_type",), ("authType",), ("auth_type",)),
    ).casefold()
    if source_marker in {
        "chatgpt_web_session",
        "web_session",
        "session",
    }:
        source_type = "web_session"

    existing_session_meta = candidate.get("session_meta") if isinstance(candidate.get("session_meta"), dict) else {}
    if str(existing_session_meta.get("credentialCapability") or "").strip().casefold() in {
        "quota_only",
        "unverified_web_session",
        "codex_short_lived",
    }:
        source_type = "web_session"

    raw_auth_mode = _core._import_credential_string(
        contexts,
        (("auth_mode",), ("authMode",)),
        "auth_mode",
    )
    normalized_auth_mode = _core.re.sub(r"[^a-z0-9]", "", raw_auth_mode.casefold())
    known_import_modes = {
        "",
        "apikey",
        "oauth",
        "chatgpt",
        "chatgptauthtokens",
        "pat",
        "personalaccesstoken",
        "agentidentity",
    }
    if normalized_auth_mode not in known_import_modes:
        raise _core.ManagerError(f"检测到不受支持的 auth_mode：{raw_auth_mode}。为避免导入后被 Codex 判定为未登录，已停止导入。")
    agent_identity = None
    for context in contexts:
        agent_identity = _core._agent_identity_from_auth(context)
        if agent_identity is not None:
            break
    if normalized_auth_mode == "agentidentity" and agent_identity is None:
        raise _core.ManagerError("auth_mode 声明为 Agent Identity，但账号中没有完整的 Agent Identity 凭据。")

    api_key = _core._import_credential_string(
        contexts,
        (
            ("OPENAI_API_KEY",),
            ("openaiApiKey",),
            ("openai_api_key",),
            ("apiKey",),
            ("api_key",),
            ("credentials", "OPENAI_API_KEY"),
            ("credentials", "apiKey"),
            ("credentials", "api_key"),
        ),
        "OPENAI_API_KEY",
    )
    if api_key:
        api_key = _core._validated_provider_secret(api_key)
    personal_access_token = _core._import_credential_string(
        contexts,
        (
            ("personal_access_token",),
            ("personalAccessToken",),
            ("at_token",),
            ("credentials", "personal_access_token"),
            ("credentials", "personalAccessToken"),
            ("credentials", "at_token"),
        ),
        "Personal Access Token",
    )
    access_token = _core._import_credential_string(
        contexts,
        (
            ("tokens", "access_token"),
            ("tokens", "accessToken"),
            ("token", "access_token"),
            ("token", "accessToken"),
            ("credentials", "access_token"),
            ("credentials", "accessToken"),
            ("access_token",),
            ("accessToken",),
            ("bearerToken",),
            ("bearer_token",),
            ("authorization",),
        ),
        "access_token",
    )
    raw_token = _core._import_value(contexts, (("token",),))
    if isinstance(raw_token, str) and normalized_auth_mode != "agentidentity":
        raw_token = raw_token.strip()
        if not personal_access_token and _core._looks_like_personal_access_token(raw_token):
            personal_access_token = raw_token
        elif not access_token and (
            _core._looks_like_jwt(raw_token) or normalized_auth_mode in {"pat", "personalaccesstoken"}
        ):
            access_token = raw_token
    if access_token.casefold().startswith("bearer "):
        access_token = access_token[7:].strip()
    if normalized_auth_mode in {"pat", "personalaccesstoken"} and not personal_access_token:
        personal_access_token = access_token
    id_token = _core._import_credential_string(
        contexts,
        (
            ("tokens", "id_token"),
            ("tokens", "idToken"),
            ("token", "id_token"),
            ("token", "idToken"),
            ("credentials", "id_token"),
            ("credentials", "idToken"),
            ("id_token",),
            ("idToken",),
        ),
        "id_token",
    )
    refresh_token = _core._import_credential_string(
        contexts,
        (
            ("tokens", "refresh_token"),
            ("tokens", "refreshToken"),
            ("token", "refresh_token"),
            ("token", "refreshToken"),
            ("credentials", "refresh_token"),
            ("credentials", "refreshToken"),
            ("refresh_token",),
            ("refreshToken",),
        ),
        "refresh_token",
    )
    session_token = _core._import_credential_string(
        contexts,
        (("sessionToken",), ("session_token",), ("credentials", "sessionToken"), ("credentials", "session_token")),
        "sessionToken",
    )
    if personal_access_token.casefold().startswith("bearer "):
        personal_access_token = personal_access_token[7:].strip()
    if (
        not agent_identity
        and access_token
        and not id_token
        and not refresh_token
        and not session_token
        and _core._looks_like_agent_identity_jwt(access_token)
    ):
        agent_identity = _core._normalize_agent_identity_storage(access_token)
        access_token = ""
    if (
        not agent_identity
        and not personal_access_token
        and access_token
        and not id_token
        and not refresh_token
        and not session_token
        and _core._looks_like_personal_access_token(access_token)
    ):
        personal_access_token = access_token
        access_token = ""
    oauth_fields_present = bool(access_token or id_token or refresh_token)
    oauth_probe = _core.json.dumps(
        {
            "OPENAI_API_KEY": None,
            "tokens": {
                "access_token": access_token,
                "id_token": id_token,
                "refresh_token": refresh_token,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    complete_native_oauth = bool(
        access_token
        and not session_token
        and _core._auth_bytes_support_codex(oauth_probe)
    )
    oauth_agent_cache = agent_identity
    if agent_identity and complete_native_oauth:
        for context in contexts:
            if "agent_identity" in context or "agentIdentity" in context:
                raw_cache = context.get("agent_identity", context.get("agentIdentity"))
                oauth_agent_cache = (
                    _core.json.loads(_core.json.dumps(raw_cache))
                    if isinstance(raw_cache, dict)
                    else str(raw_cache).strip()
                )
                break
    if agent_identity and (
        api_key
        or personal_access_token
        or session_token
        or (oauth_fields_present and not complete_native_oauth)
    ):
        raise _core.ManagerError("账号同时包含 Agent Identity 与其他凭据，凭据归属不明确，已停止导入。")
    if api_key and (personal_access_token or access_token or id_token or refresh_token or session_token):
        raise _core.ManagerError("账号同时包含 API Key 与 ChatGPT Token，凭据归属不明确，已停止导入。")
    if personal_access_token and (id_token or refresh_token or session_token):
        raise _core.ManagerError("账号同时包含 Personal Access Token 与 OAuth/Web Session 凭据，已停止导入。")
    credential_family = (
        "apikey"
        if api_key
        else "personalaccesstoken"
        if personal_access_token
        else "agentidentity"
        if agent_identity and not complete_native_oauth
        else "chatgpt"
        if oauth_fields_present or session_token
        else ""
    )
    allowed_declared_modes = {
        "apikey": {"", "apikey"},
        "personalaccesstoken": {"", "pat", "personalaccesstoken"},
        "agentidentity": {"", "agentidentity"},
        "chatgpt": {"", "oauth", "chatgpt", "chatgptauthtokens"},
    }
    if normalized_auth_mode not in allowed_declared_modes.get(credential_family, {""}):
        raise _core.ManagerError(
            f"auth_mode `{raw_auth_mode or '空'}` 与导入的 {credential_family or '未知'} 凭据不匹配。"
        )
    if agent_identity and not complete_native_oauth:
        agent_record = agent_identity if isinstance(agent_identity, dict) else _core._jwt_payload(agent_identity)
        session_meta = dict(existing_session_meta)
        session_meta.update(
            {
                "accountId": str(
                    agent_record.get("account_id") or agent_record.get("accountId") or ""
                ).strip(),
                "email": str(agent_record.get("email") or "").strip(),
                "plan": str(
                    agent_record.get("plan_type") or agent_record.get("planType") or ""
                ).strip(),
                "credentialCapability": "codex",
                "importFormat": _core._detect_import_format(document, candidate, source_type),
            }
        )
        canonical = {
            "auth_mode": "agentIdentity",
            "OPENAI_API_KEY": None,
            "agent_identity": agent_identity,
            "session_meta": session_meta,
            "last_refresh": _core._import_value(contexts, (("last_refresh",), ("lastRefresh",)))
            or _core.now_iso(),
        }
    elif api_key:
        canonical = {"auth_mode": "apikey", "OPENAI_API_KEY": api_key}
    else:
        is_personal_access_token = bool(personal_access_token)
        if is_personal_access_token:
            # Explicit PAT markers take precedence over generic access_token
            # fields emitted alongside them by Cockpit/CPA exporters.
            access_token = personal_access_token
        if not access_token:
            if session_token:
                raise _core.ManagerError("检测到浏览器 sessionToken，但缺少 accessToken；sessionToken 不能伪装成 OAuth refresh_token。")
            raise _core.ManagerError("账号中缺少 accessToken；支持 auth.json、CPA、Sub2API、9Router、AxonHub 与 Web Session 导出。")
        access_claims = _core._jwt_payload(access_token)
        id_claims = _core._jwt_payload(id_token)
        account_id = _core._import_string(
            contexts,
            (
                ("tokens", "account_id"),
                ("tokens", "accountId"),
                ("token", "account_id"),
                ("credentials", "chatgpt_account_id"),
                ("credentials", "account_id"),
                ("credentials", "accountId"),
                ("providerSpecificData", "chatgpt_account_id"),
                ("providerSpecificData", "account_id"),
                ("meta", "account_id"),
                ("meta", "accountId"),
                ("extra", "account_id"),
                ("account", "id"),
                ("account", "account_id"),
                ("chatgptAccountId",),
                ("chatgpt_account_id",),
                ("accountId",),
                ("account_id",),
            ),
        ) or _core._nested_string(
            access_claims,
            [
                ("https://api.openai.com/auth", "chatgpt_account_id"),
                ("https://api.openai.com/auth", "account_id"),
                ("chatgpt_account_id",),
                ("account_id",),
            ],
        ) or _core._nested_string(
            id_claims,
            [
                ("https://api.openai.com/auth", "chatgpt_account_id"),
                ("https://api.openai.com/auth", "account_id"),
                ("chatgpt_account_id",),
                ("account_id",),
            ],
        )
        email = _core._import_string(
            contexts,
            (
                ("user", "email"),
                ("account", "email"),
                ("credentials", "email"),
                ("providerSpecificData", "email"),
                ("meta", "email"),
                ("extra", "email"),
                ("email",),
            ),
        ) or _core._nested_string(
            id_claims or access_claims,
            [("email",), ("https://api.openai.com/profile", "email"), ("https://api.openai.com/auth", "email")],
        )
        name = _core._import_string(
            contexts,
            (("user", "name"), ("account", "name"), ("meta", "name"), ("extra", "name"), ("name",)),
        ) or _core._nested_string(id_claims or access_claims, [("name",), ("https://api.openai.com/profile", "name")])
        plan = _core._import_string(
            contexts,
            (
                ("account", "plan"),
                ("account", "planType"),
                ("account", "plan_type"),
                ("account", "subscription_plan"),
                ("credentials", "plan_type"),
                ("credentials", "plan"),
                ("providerSpecificData", "plan_type"),
                ("meta", "plan"),
                ("extra", "plan"),
                ("plan",),
                ("planType",),
                ("plan_type",),
            ),
        ) or _core._nested_string(
            id_claims or access_claims,
            [("chatgpt_plan_type",), ("plan_type",), ("https://api.openai.com/auth", "chatgpt_plan_type")],
        )
        user_id = _core._import_string(
            contexts,
            (
                ("user", "id"),
                ("credentials", "user_id"),
                ("meta", "user_id"),
                ("userId",),
                ("user_id",),
                ("chatgptUserId",),
                ("chatgpt_user_id",),
            ),
        )
        expires = _core._import_value(
            contexts,
            (
                ("expires",),
                ("expiresAt",),
                ("expires_at",),
                ("tokens", "expires_at"),
                ("credentials", "expires_at"),
                ("providerSpecificData", "expiresAt"),
            ),
        )
        if not id_token and not is_personal_access_token:
            id_token = _core._synthetic_web_session_id_token(email, account_id, plan, user_id, expires)
            source_type = "web_session"
        session_meta = {
            key: value
            for key, value in existing_session_meta.items()
            if key
            in {
                "email",
                "name",
                "plan",
                "expires",
                "authProvider",
                "subscriptionStartedAt",
                "subscriptionExpiresAt",
                "subscriptionLastCheckedAt",
                "subscriptionMetadataSource",
                "credentialCapability",
                "importFormat",
                "accountId",
                "account_id",
                "chatgpt_account_id",
            }
        }
        session_meta.update(
            {
                "email": email or session_meta.get("email") or "",
                "name": name or session_meta.get("name") or "",
                "plan": plan or session_meta.get("plan") or "",
                "expires": expires or session_meta.get("expires"),
                "accountId": account_id
                or session_meta.get("accountId")
                or session_meta.get("account_id")
                or session_meta.get("chatgpt_account_id")
                or "",
                "authProvider": _core._import_value(contexts, (("authProvider",), ("auth_provider",), ("provider",)))
                or session_meta.get("authProvider"),
                "subscriptionStartedAt": _core._import_value(
                    contexts,
                    (
                        ("subscriptionStartedAt",),
                        ("subscription_started_at",),
                        ("account", "subscriptionStartedAt"),
                        ("account", "subscription_started_at"),
                        ("account", "activeFrom"),
                        ("account", "active_from"),
                    ),
                )
                or session_meta.get("subscriptionStartedAt"),
                "subscriptionExpiresAt": _core._import_value(
                    contexts,
                    (
                        ("subscriptionExpiresAt",),
                        ("subscription_expires_at",),
                        ("planExpiresAt",),
                        ("plan_expires_at",),
                        ("account", "subscriptionExpiresAt"),
                        ("account", "subscription_expires_at"),
                        ("account", "planExpiresAt"),
                        ("account", "plan_expires_at"),
                        ("account", "validUntil"),
                        ("account", "valid_until"),
                    ),
                )
                or session_meta.get("subscriptionExpiresAt"),
                "subscriptionLastCheckedAt": _core._import_value(
                    contexts,
                    (
                        ("subscriptionLastCheckedAt",),
                        ("subscription_last_checked_at",),
                        ("account", "subscriptionLastCheckedAt"),
                        ("account", "subscription_last_checked_at"),
                    ),
                )
                or session_meta.get("subscriptionLastCheckedAt"),
            }
        )
        if is_personal_access_token:
            canonical = {
                "OPENAI_API_KEY": None,
                "personal_access_token": personal_access_token,
                "session_meta": session_meta,
                "last_refresh": _core._import_value(contexts, (("last_refresh",), ("lastRefresh",))) or _core.now_iso(),
            }
        else:
            canonical = {
                "OPENAI_API_KEY": None,
                "tokens": {
                    "access_token": access_token,
                    "id_token": id_token,
                    "refresh_token": refresh_token,
                    "account_id": account_id,
                },
                "session_meta": session_meta,
                "last_refresh": _core._import_value(contexts, (("last_refresh",), ("lastRefresh",))) or _core.now_iso(),
            }
            if agent_identity:
                canonical["agent_identity"] = oauth_agent_cache
        session_meta["importFormat"] = _core._detect_import_format(document, candidate, source_type)
    encoded = _core.json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    canonical_tokens = canonical.get("tokens") if isinstance(canonical.get("tokens"), dict) else {}
    if (
        canonical_tokens.get("access_token")
        and not str(canonical.get("OPENAI_API_KEY") or "").strip()
        and not _core._auth_bytes_support_codex(encoded)
    ):
        source_type = "web_session"
        session_meta = canonical.setdefault("session_meta", {})
        if isinstance(session_meta, dict):
            session_meta["credentialCapability"] = "unverified_web_session"
        canonical.setdefault("last_refresh", _core.now_iso())
        encoded = _core.json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if source_type == "web_session" and _core._auth_bytes_support_codex(encoded):
        source_type = "codex_auth"
        if isinstance(canonical.get("session_meta"), dict):
            canonical["session_meta"]["credentialCapability"] = "codex"
            encoded = _core.json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if source_type == "web_session":
        credentials = _core._chatgpt_credentials_from_auth_bytes(encoded)
        expires_at = _core._session_expiry(credentials.get("tokenExpiresAt"))
        if expires_at:
            parsed_expiry = _core.datetime.fromisoformat(expires_at)
            if parsed_expiry.tzinfo is None:
                parsed_expiry = parsed_expiry.replace(tzinfo=_core.timezone.utc)
            if parsed_expiry.astimezone(_core.timezone.utc) <= _core.datetime.now(_core.timezone.utc) + _core.timedelta(minutes=1):
                raise _core.ManagerError("Web Session 已过期，无法同步到 Codex；请重新获取后再导入。")
    identity = _core._identity_from_auth_bytes(encoded)
    if identity.get("authMode") == "chatgpt" and not identity.get("accountId"):
        raise _core.ManagerError("账号中缺少 ChatGPT Account ID；无法查询额度、模型或在 Codex 中切换。")
    # Preview/import success must mean the exact bytes later projected into
    # Codex are structurally valid.  This catches exporter drift at the import
    # boundary instead of after the user closes Codex and switches accounts.
    if source_type != "web_session":
        _core._codex_auth_projection_bytes(encoded)
        tokens = canonical.get("tokens") if isinstance(canonical.get("tokens"), dict) else {}
        if identity.get("authMode") == "chatgpt" and all(
            isinstance(tokens.get(key), str) and len(tokens[key].split(".")) == 3
            for key in ("access_token", "id_token")
        ):
            from agent_manager.accounts.portability import normalize_portable_oauth_auth, PortableAccountError
            try:
                normalize_portable_oauth_auth(canonical)
            except PortableAccountError as exc:
                raise _core.ManagerError(str(exc)) from exc
    return encoded, source_type



def _resolved_import_source_type(auth_bytes: bytes, detected: str, hinted: _core.Any) -> str:
    hint = str(hinted or "").strip()
    compatible = _core._auth_bytes_support_codex(auth_bytes)
    if compatible:
        return "codex_auth"
    if detected == "web_session" or hint == "web_session":
        return "web_session"
    return detected



def _snapshot_from_bytes(auth_bytes: bytes, cap_sid_bytes: bytes | None) -> tuple[dict, dict]:
    identity = _core._identity_from_auth_bytes(auth_bytes)
    if cap_sid_bytes is not None and len(cap_sid_bytes) > 256_000:
        raise _core.ManagerError("cap_sid 文件异常过大，已拒绝保存。")
    snapshot = {
        "version": 1,
        "files": [
            {"name": "auth.json", "present": True, "content": _core.base64.b64encode(auth_bytes).decode("ascii")},
            {
                "name": "cap_sid",
                "present": cap_sid_bytes is not None,
                "content": _core.base64.b64encode(cap_sid_bytes or b"").decode("ascii"),
            },
        ],
        "fingerprint": identity["fingerprint"],
    }
    return snapshot, identity



def _read_live_snapshot() -> tuple[dict, dict]:
    auth_path = _core.CODEX_HOME / "auth.json"
    if not auth_path.is_file():
        raise _core.ManagerError("当前 Codex 没有可保存的 auth.json。")
    cap_path = _core.CODEX_HOME / "cap_sid"
    return _core._snapshot_from_bytes(auth_path.read_bytes(), cap_path.read_bytes() if cap_path.is_file() else None)



def _store_account_snapshot(account_id: str, snapshot: dict) -> None:
    with _core.SECRETS_LOCK, _core._settings_file_lock():
        payload = _core._secret_store()
        payload["accounts"][account_id] = _core._encrypted_account_snapshot(snapshot)
        _core.atomic_write_json(_core.SECRETS_FILE, payload)



def _load_account_snapshot(account_id: str) -> dict:
    encoded = _core._secret_store()["accounts"].get(account_id)
    if not encoded:
        raise _core.ManagerError("该账号的加密快照不存在。")
    try:
        snapshot = _core.json.loads(_core.dpapi_unprotect(_core.base64.b64decode(encoded, validate=True)))
    except Exception as exc:
        raise _core.ManagerError("无法解密该账号快照；DPAPI 快照不能跨 Windows 用户复制。") from exc
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1 or not isinstance(snapshot.get("files"), list):
        raise _core.ManagerError("账号快照格式无效。")
    return snapshot



def _decode_snapshot_files(snapshot: dict) -> dict[str, bytes | None]:
    decoded: dict[str, bytes | None] = {}
    for item in snapshot.get("files", []):
        if not isinstance(item, dict) or item.get("name") not in _core.AUTH_FILES:
            raise _core.ManagerError("账号快照包含不受支持的认证文件。")
        name = str(item["name"])
        if name in decoded:
            raise _core.ManagerError("账号快照包含重复认证文件。")
        if not item.get("present"):
            decoded[name] = None
            continue
        try:
            decoded[name] = _core.base64.b64decode(str(item.get("content") or ""), validate=True)
        except ValueError as exc:
            raise _core.ManagerError("账号快照包含无效的 Base64 数据。") from exc
    if "auth.json" not in decoded or decoded["auth.json"] is None:
        raise _core.ManagerError("账号快照缺少 auth.json。")
    decoded.setdefault("cap_sid", None)
    identity = _core._identity_from_auth_bytes(decoded["auth.json"] or b"")
    snapshot_fingerprint = str(snapshot.get("fingerprint") or "")
    accepted_fingerprints = {
        str(identity.get("fingerprint") or ""),
        *(
            str(value)
            for value in identity.get("legacyFingerprints", [])
            if str(value)
        ),
    }
    if snapshot_fingerprint not in accepted_fingerprints:
        raise _core.ManagerError("账号快照身份校验失败。")
    return decoded

