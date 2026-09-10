"""Account import services."""
from __future__ import annotations
from agent_manager import core as _core


def _account_record_from_import(settings: dict, payload: dict, identity: dict) -> dict:
    group_id = str(payload.get("groupId") or "official").strip()
    _core._account_group(settings, group_id)
    source_type = str(payload.get("sourceType") or "codex_auth").strip()
    if source_type not in _core.VALID_ACCOUNT_SOURCES:
        raise _core.ManagerError("账号来源类型无效。")
    codex_compatible = source_type != "web_session"
    original_id = str(payload.get("originalId") or "").strip()
    duplicate = _core._find_account_for_identity(settings, identity)
    existing = next((item for item in settings.get("accounts", []) if item.get("id") == original_id), None)
    if existing and duplicate and str(existing.get("id")) != str(duplicate.get("id")):
        raise _core.ManagerError(
            "originalId 指向的账号与导入凭据身份不一致，且该身份已属于另一账号；已拒绝覆盖。"
        )
    target = existing or duplicate
    account_id = target.get("id") if target else f"account_{_core.uuid.uuid4().hex[:10]}"
    label = str(payload.get("label") or "").strip() or (target or {}).get("label") or identity["display"]
    if len(label) > 120:
        raise _core.ManagerError("账号名称不能超过 120 个字符。")
    imported_at = target.get("importedAt") if target else _core.now_iso()
    imported_models = (
        [str(item).strip() for item in payload.get("models", []) if str(item).strip()]
        if isinstance(payload.get("models"), list)
        else []
    )
    record = {
        "id": account_id,
        "label": label,
        "email": identity["email"],
        "name": identity["name"],
        "plan": identity["plan"],
        "planLabel": _core._normalize_plan_label(identity["plan"]),
        "importFormat": identity.get("importFormat") or (target or {}).get("importFormat") or "",
        "authMode": identity["authMode"],
        "credentialKind": identity.get("credentialKind") or identity["authMode"],
        "refreshCapable": bool(identity.get("refreshCapable")),
        "tokenExpiresAt": identity.get("tokenExpiresAt"),
        "subscriptionStartedAt": identity.get("subscriptionStartedAt")
        or (target or {}).get("subscriptionStartedAt"),
        "subscriptionExpiresAt": identity.get("subscriptionExpiresAt")
        or _core._session_expiry(payload.get("subscriptionExpiresAt"))
        or (target or {}).get("subscriptionExpiresAt"),
        "subscriptionLastCheckedAt": identity.get("subscriptionLastCheckedAt")
        or (target or {}).get("subscriptionLastCheckedAt"),
        "subscriptionMetadataSource": identity.get("subscriptionMetadataSource")
        or (target or {}).get("subscriptionMetadataSource"),
        "subscriptionStatus": (target or {}).get("subscriptionStatus"),
        "fingerprint": identity["fingerprint"],
        "accountId": identity.get("accountId") or (target or {}).get("accountId") or "",
        "createdAt": target.get("createdAt") if target else imported_at,
        "importedAt": imported_at,
        "updatedAt": _core.now_iso(),
        "lastUsedAt": target.get("lastUsedAt") if target else None,
        "lastRefreshedAt": target.get("lastRefreshedAt") if target else None,
        "refreshState": "pending" if identity["authMode"] == "chatgpt" else "ready",
        "refreshErrors": {},
        "usage": target.get("usage") if target else None,
        "models": imported_models or (target.get("models", []) if target else []),
        "modelsLastCheckedAt": (
            payload.get("modelsLastCheckedAt")
            or (target or {}).get("modelsLastCheckedAt")
        ),
        "modelsRefreshedAt": (
            payload.get("modelsRefreshedAt")
            or (target or {}).get("modelsRefreshedAt")
        ),
        "groupId": group_id,
        "sourceType": source_type,
        "codexCompatible": codex_compatible,
        "quotaOnly": not codex_compatible,
        "credentialCapability": "codex" if codex_compatible else "quota_only",
        "proxyRequested": bool(payload.get("proxyEnabled", (target or {}).get("proxyRequested", False))),
        "proxyEnabled": bool(payload.get("proxyEnabled", (target or {}).get("proxyEnabled", False)))
        and identity["authMode"] == "chatgpt"
        and codex_compatible,
    }
    token_plan = _core._plan_snapshot_metadata({
        "plan": identity.get("plan"),
        "subscriptionExpiresAt": identity.get("subscriptionExpiresAt") or _core._session_expiry(payload.get("subscriptionExpiresAt")),
        "subscriptionLastCheckedAt": identity.get("subscriptionLastCheckedAt"),
    }, source="token")
    same_identity = bool(target and _core._account_matches_identity(target, identity))
    plan_candidates = [token_plan]
    if same_identity:
        plan_candidates.append(_core._plan_snapshot_metadata(target))
        if isinstance(target.get("usage"), dict):
            plan_candidates.append(_core._plan_snapshot_metadata(target["usage"]))
    selected_plan = _core.select_plan_metadata(*plan_candidates)
    if selected_plan:
        record.update(selected_plan)
        selected_expiry = _core._session_expiry(selected_plan["planEvidence"].get("expiresAt"))
        if selected_expiry:
            record["subscriptionExpiresAt"] = selected_expiry
        if isinstance(record.get("usage"), dict):
            record["usage"] = {**record["usage"], **selected_plan}
    if _core._is_free_plan(record.get("plan"), record.get("planLabel")):
        _core._clear_subscription_metadata(record)
    return record



def _sync_account_proxy_membership(settings: dict, record: dict) -> None:
    member_ids = list(settings.setdefault("web2api", _core._default_web2api_settings()).get("accountIds", []))
    account_id = str(record["id"])
    if record["proxyEnabled"]:
        if account_id not in member_ids:
            member_ids.append(account_id)
    else:
        member_ids = [item for item in member_ids if item != account_id]
    settings["web2api"]["accountIds"] = member_ids



def _encrypted_account_snapshot(snapshot: dict) -> str:
    plaintext = _core.json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    return _core.base64.b64encode(_core.dpapi_protect(plaintext)).decode("ascii")



def save_codex_account(payload: dict, auth_bytes: bytes | None = None, cap_sid_bytes: bytes | None = None) -> dict:
    if _core._credential_store_mode() == "keyring":
        raise _core.ManagerError("当前 Codex 使用 keyring 凭据存储，不能通过 auth.json 快照切换。")
    if auth_bytes is None:
        snapshot, identity = _core._read_live_snapshot()
    else:
        snapshot, identity = _core._snapshot_from_bytes(auth_bytes, cap_sid_bytes)
    encrypted_snapshot = _core._encrypted_account_snapshot(snapshot)
    with _core.SETTINGS_LOCK, _core.SECRETS_LOCK, _core._settings_file_lock():
        settings = _core.load_settings()
        record = _core._account_record_from_import(settings, payload, identity)
        secrets_existed = _core.SECRETS_FILE.exists()
        secrets_before = _core.SECRETS_FILE.read_bytes() if secrets_existed else None
        secrets_payload = _core._secret_store()
        secrets_payload["accounts"][record["id"]] = encrypted_snapshot
        _core._merge_by_id(settings.setdefault("accounts", []), record)
        _core._sync_account_proxy_membership(settings, record)
        try:
            # Persist credentials first so settings never advertise a missing snapshot.
            _core.atomic_write_json(_core.SECRETS_FILE, secrets_payload)
            _core.save_settings(settings)
        except Exception:
            if secrets_before is None:
                _core.SECRETS_FILE.unlink(missing_ok=True)
            else:
                _core.atomic_write_bytes(_core.SECRETS_FILE, secrets_before)
            raise
    if identity["authMode"] == "chatgpt" and not payload.get("deferRefresh"):
        return _core.refresh_codex_account(record["id"])
    return record



def import_codex_account(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise _core.ManagerError("账号导入请求必须是对象。")
    raw = payload.get("authJson")
    imported_models = _core._account_models_from_import_candidate(raw)
    auth_bytes, source_type = _core._normalize_import_auth_payload(raw if isinstance(raw, (str, dict)) else "")
    source_type = _core._resolved_import_source_type(auth_bytes, source_type, payload.get("sourceType"))
    cap_sid_bytes = None
    if payload.get("capSidBase64"):
        try:
            cap_sid_bytes = _core.base64.b64decode(str(payload["capSidBase64"]), validate=True)
        except ValueError as exc:
            raise _core.ManagerError("cap_sid Base64 无效。") from exc
    normalized_payload = {
        **payload,
        "sourceType": source_type,
        "models": imported_models or payload.get("models", []),
    }
    return _core.save_codex_account(normalized_payload, auth_bytes, cap_sid_bytes)



def _decode_many_json_documents(value: str, item_number: int) -> list[_core.Any]:
    from agent_manager.accounts.import_formats import strict_loads, unique_pairs, ImportFormatError
    if len(value.encode("utf-8", errors="replace")) > _core.MAX_BATCH_IMPORT_TEXT_BYTES:
        raise _core.ManagerError(f"第 {item_number} 项超过 {_core.MAX_BATCH_IMPORT_TEXT_BYTES // 1_000_000} MB，已拒绝解析。")
    raw = value.strip().lstrip("\ufeff\u200b\u200c\u200d\u2060")
    if not raw:
        raise _core.ManagerError(f"第 {item_number} 项内容为空。")
    # A normal export is already one complete JSON document. Avoid scanning
    # every pretty-printed model row as a possible key/value credential dump.
    try:
        return [_core._decode_import_value(strict_loads(raw))]
    except ImportFormatError as exc:
        raise _core.ManagerError(str(exc)) from None
    except _core.json.JSONDecodeError:
        pass
    standalone = _core._decode_import_value(raw)
    if isinstance(standalone, dict):
        return [standalone]
    # Plain key/value dumps are common in community conversion tools. Only
    # recognize credential-related names so arbitrary prose is never imported.
    key_value: dict[str, str] = {}
    known_keys = {
        "OPENAI_API_KEY",
        "openai_api_key", "openaiApiKey", "api_key", "apiKey", "api-key",
        "OPENAI_BASE_URL", "baseUrl", "baseURL", "base_url", "base-url",
        "api_base_url", "apiBaseUrl", "endpoint", "model", "name",
        "accessToken",
        "access_token",
        "idToken",
        "id_token",
        "refreshToken",
        "refresh_token",
        "accountId",
        "account_id",
        "chatgptAccountId",
        "chatgpt_account_id",
        "email",
        "plan",
        "planType",
        "plan_type",
        "sessionToken",
        "session_token",
        "personal_access_token",
        "personalAccessToken",
        "at_token",
        "token",
        "auth_mode",
        "authMode",
        "agent_runtime_id",
        "agentRuntimeId",
        "agent_private_key",
        "agentPrivateKey",
        "chatgpt_user_id",
        "chatgptUserId",
        "chatgpt_account_is_fedramp",
        "chatgptAccountIsFedramp",
        "task_id",
        "taskId",
    }
    for line in raw.splitlines():
        match = _core.re.match(r"^\s*(?:export\s+)?['\"]?([A-Za-z_][A-Za-z0-9_-]*)['\"]?\s*(?:=|:)\s*(.+?)\s*[,;]?\s*$", line)
        if not match or match.group(1) not in known_keys:
            continue
        parsed_value = match.group(2).strip()
        if len(parsed_value) >= 2 and parsed_value[0] == parsed_value[-1] and parsed_value[0] in {"'", '"'}:
            parsed_value = parsed_value[1:-1]
        if parsed_value:
            if match.group(1) in key_value and key_value[match.group(1)] != parsed_value:
                raise _core.ManagerError("文本中同一字段包含冲突值，请拆分为独立账号对象。")
            key_value[match.group(1)] = parsed_value

    if key_value and _core._credential_shaped(key_value) and not _core.re.search(r"[\{\[]", raw):
        return [key_value]

    decoder = _core.json.JSONDecoder(object_pairs_hook=unique_pairs)
    documents: list[Any] = []
    cursor = 0
    first_error: _core.json.JSONDecodeError | None = None
    jwt_pattern = _core.re.compile(
        r"(?i)(?:\bBearer\s+)?(eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]+)"
    )
    pat_pattern = _core.re.compile(
        r"(?<![A-Za-z0-9_-])(at-[A-Za-z0-9._~+/=-]{8,})(?![A-Za-z0-9._~+/=-])"
    )
    while cursor < len(raw):
        while cursor < len(raw) and (
            raw[cursor].isspace() or raw[cursor] in "\ufeff\u200b\u200c\u200d\u2060,;"
        ):
            cursor += 1
        if cursor >= len(raw):
            break
        try:
            document, end = decoder.raw_decode(raw, cursor)
        except _core.json.JSONDecodeError as exc:
            first_error = first_error or exc
            # Recover at the next object/array or a standalone bearer token.
            next_object_positions = [position for position in (raw.find("{", cursor + 1), raw.find("[", cursor + 1)) if position >= 0]
            next_object = min(next_object_positions) if next_object_positions else -1
            jwt_match = jwt_pattern.search(raw, cursor)
            pat_match = pat_pattern.search(raw, cursor)
            token_matches = [match for match in (jwt_match, pat_match) if match]
            token_match = min(token_matches, key=lambda match: match.start()) if token_matches else None
            if token_match and (next_object < 0 or token_match.start() < next_object):
                if token_match.re is pat_pattern:
                    documents.append(
                        {
                            "auth_mode": "personalAccessToken",
                            "personal_access_token": token_match.group(1),
                        }
                    )
                else:
                    token = token_match.group(1)
                    documents.append(
                        {"accessToken": token, "token_source_mode": "web_session"}
                        if not _core._looks_like_agent_identity_jwt(token)
                        else {"auth_mode": "agentIdentity", "agent_identity": token}
                    )
                cursor = token_match.end()
                continue
            if next_object >= 0:
                cursor = next_object
                continue
            break
        decoded = _core._decode_import_value(document)
        documents.append(decoded)
        cursor = end
    if not documents and key_value and _core._credential_shaped(key_value):
        documents.append(key_value)
    if not documents:
        if first_error:
            raise _core.ManagerError(
                f"第 {item_number} 项在第 {first_error.lineno} 行附近无法识别；请检查括号、引号或分隔符。"
            ) from first_error
        raise _core.ManagerError(f"第 {item_number} 项没有可识别的账号。")
    return documents



def _expand_import_candidates(value: _core.Any) -> list[_core.Any]:
    """Find credential objects inside bounded, arbitrarily named export wrappers."""
    from agent_manager.accounts.import_formats import NON_ACCOUNT_CONTAINERS, container_name
    nodes = 0

    def visit(current: Any, depth: int) -> list[Any]:
        nonlocal nodes
        nodes += 1
        if nodes > _core.MAX_IMPORT_NODES:
            raise _core.ManagerError("导入内容的节点过多，可能不是账号导出文件。")
        if depth > _core.MAX_IMPORT_NESTING:
            raise _core.ManagerError(f"导入内容嵌套超过 {_core.MAX_IMPORT_NESTING} 层，已停止解析。")
        current = _core._decode_import_value(current)
        if isinstance(current, list):
            expanded: list[Any] = []
            for item in current:
                expanded.extend(visit(item, depth + 1))
                if len(expanded) > _core.MAX_BATCH_IMPORT_ACCOUNTS:
                    raise _core.ManagerError(f"展开后超过 {_core.MAX_BATCH_IMPORT_ACCOUNTS} 个账号，请分批导入。")
            return expanded
        if not isinstance(current, dict):
            return [current]
        wrapped_auth = _core._decode_import_value(current.get("authJson")) if "authJson" in current else None
        if current.get("format") in {
            "codex-agent-manager-account",
            "codex-agent-manager-api-account",
            "codex-agent-manager-relay-account",
        } or _core._provider_export_shaped(current) or _core._credential_shaped(current) or _core._credential_shaped(wrapped_auth):
            return [current]

        expanded = []
        preferred = ("accounts", "items", "records", "results", "profiles", "data", "payload", "result")
        visited_keys: set[str] = set()
        for key in preferred:
            if key not in current:
                continue
            visited_keys.add(key)
            nested = current[key]
            if isinstance(nested, (dict, list, str)):
                found = visit(nested, depth + 1)
                if any(
                    isinstance(item, dict) and (
                        str(item.get("format") or "") in {
                            "codex-agent-manager-account",
                            "codex-agent-manager-api-account",
                            "codex-agent-manager-relay-account",
                        }
                        or _core._provider_export_shaped(item) or _core._credential_shaped(item)
                    )
                    for item in found
                ):
                    expanded.extend(found)
        if not expanded:
            for key, nested in current.items():
                if key in visited_keys or container_name(key) in NON_ACCOUNT_CONTAINERS or not isinstance(nested, (dict, list, str)):
                    continue
                found = visit(nested, depth + 1)
                if any(
                    isinstance(item, dict)
                    and (
                        _core._credential_shaped(item)
                        or _core._provider_export_shaped(item)
                        or item.get("format")
                        in {
                            "codex-agent-manager-account",
                            "codex-agent-manager-api-account",
                            "codex-agent-manager-relay-account",
                        }
                    )
                    for item in found
                ):
                    expanded.extend(found)
        return expanded or [current]

    return visit(value, 0)



def _relay_export_import_parts(document: object) -> dict:
    """Validate and unpack one explicit portable relay-account export."""

    if not isinstance(document, dict) or document.get("format") != "codex-agent-manager-relay-account":
        raise _core.ManagerError("中转站账号导出文件格式无效。")
    if document.get("version") != 1:
        raise _core.ManagerError("中转站账号导出文件版本不受支持。")
    exported = document.get("relayAccount")
    if not isinstance(exported, dict):
        raise _core.ManagerError("中转站账号导出文件缺少账号内容。")
    preview = exported.get("preview")
    if not isinstance(preview, dict):
        raise _core.ManagerError("中转站账号导出文件缺少站点快照。")
    raw_secrets = exported.get("keySecrets")
    if not isinstance(raw_secrets, dict):
        raise _core.ManagerError("中转站账号导出文件缺少 API Key。")
    if len(raw_secrets) > 200:
        raise _core.ManagerError("中转站账号导出文件包含过多 API Key。")
    preview_key_ids = {
        str(item.get("id") or "")
        for item in preview.get("keys", [])
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    secrets_by_id: dict[str, str] = {}
    for raw_key_id, raw_secret in raw_secrets.items():
        key_id = _core._relay_secret_key_id(raw_key_id)
        secret = str(raw_secret or "").strip()
        if key_id not in preview_key_ids or not secret:
            continue
        if len(secret) > 512 or any(char.isspace() for char in secret):
            raise _core.ManagerError("中转站账号导出文件中的 API Key 格式无效。")
        secrets_by_id[key_id] = secret
    if not secrets_by_id:
        raise _core.ManagerError("中转站账号导出文件没有可用的 Codex API Key。")

    requested_account_id = str(exported.get("id") or "").strip() or None
    account_id, portal_url, origin = _core._relay_account_identity(preview, requested_account_id)
    selected_key_id = str(exported.get("selectedKeyId") or "").strip()
    selected_endpoint_id = str(exported.get("selectedEndpointId") or "").strip()
    # Reuse the production normalizer as a fail-closed schema and URL check.
    _core._normalize_relay_account_snapshot(
        preview,
        account_id=account_id,
        portal_url=portal_url,
        origin=origin,
        group_id="relay",
        selected_key_id=selected_key_id,
        selected_endpoint_id=selected_endpoint_id,
        provider_id="relay_export_validation",
        existing=None,
        configured_key_ids=set(secrets_by_id),
    )
    dashboard_session = exported.get("dashboardSession")
    if dashboard_session is not None:
        dashboard_session = _core._normalize_relay_dashboard_session(dashboard_session)
        if _core._provider_url_origin(dashboard_session.get("origin")) != _core._provider_url_origin(origin):
            raise _core.ManagerError("中转站网页登录凭据与导出账号不属于同一站点。")
    return {
        "accountId": account_id,
        "preview": _core.json.loads(_core.json.dumps(preview)),
        "keySecrets": secrets_by_id,
        "dashboardSession": dashboard_session,
        "selectedKeyId": selected_key_id,
        "selectedEndpointId": selected_endpoint_id,
        "groupId": str(exported.get("groupId") or "relay").strip() or "relay",
        "proxyEnabled": bool(exported.get("proxyEnabled")),
    }



def _batch_documents(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        raise _core.ManagerError("批量导入请求必须是对象。")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise _core.ManagerError("批量导入需要 items 数组。")
    if not raw_items:
        raise _core.ManagerError("请选择至少一个账号文件。")
    if len(raw_items) > _core.MAX_BATCH_IMPORT_ITEMS:
        raise _core.ManagerError(f"单次最多读取 {_core.MAX_BATCH_IMPORT_ITEMS} 个文件或粘贴项。")
    documents: list[dict] = []
    total_input_bytes = 0
    parsed_cache: dict[str, list[Any]] = {}
    relay_parts_cache: dict[str, dict] = {}
    for index, item in enumerate(raw_items):
        label = ""
        item_cap_sid = ""
        item_source_type = ""
        item_group_id = _core._imported_group_id(item)
        value: Any = item
        if isinstance(item, dict) and "authJson" in item and item.get("format") != "codex-agent-manager-account":
            label = str(item.get("label") or "").strip()
            item_cap_sid = str(item.get("capSidBase64") or "")
            item_source_type = str(item.get("sourceType") or "")
            value = item.get("authJson")
        try:
            item_bytes = len(value.encode("utf-8", errors="replace")) if isinstance(value, str) else len(
                _core.json.dumps(value, ensure_ascii=False).encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            raise _core.ManagerError(f"第 {index + 1} 项包含无法序列化的内容。") from exc
        total_input_bytes += item_bytes
        if item_bytes > _core.MAX_BATCH_IMPORT_TEXT_BYTES or total_input_bytes > _core.MAX_BATCH_IMPORT_TEXT_BYTES:
            raise _core.ManagerError(f"单次导入内容总计不能超过 {_core.MAX_BATCH_IMPORT_TEXT_BYTES // 1_000_000} MB。")
        if isinstance(value, str):
            parse_key = _core.hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
            if parse_key not in parsed_cache:
                try:
                    parsed_cache[parse_key] = _core._decode_many_json_documents(value, index + 1)
                except (_core.ManagerError, ValueError) as exc:
                    documents.append({"kind": "account", "authJson": {}, "parseError": _core._redact_sensitive_text(exc, limit=320)})
                    continue
            parsed_documents = parsed_cache[parse_key]
        else:
            parsed_documents = [value]
        for parsed in parsed_documents:
            try:
                candidates = _core._expand_import_candidates(parsed)
            except (_core.ManagerError, ValueError) as exc:
                documents.append({"kind": "account", "authJson": {}, "parseError": _core._redact_sensitive_text(exc, limit=320)})
                continue
            multiple = len(candidates) > 1 or len(parsed_documents) > 1
            for candidate in candidates:
                if isinstance(candidate, dict) and isinstance(candidate.get("codex_agent_manager"), dict):
                    from agent_manager.accounts.portability import portable_account_metadata, normalize_portable_oauth_auth, PortableAccountError
                    try:
                        hints = portable_account_metadata(candidate)
                        candidate = {**normalize_portable_oauth_auth(candidate), **hints}
                    except PortableAccountError as exc:
                        documents.append({"kind": "account", "authJson": {}, "parseError": str(exc)})
                        continue
                if (
                    isinstance(candidate, dict)
                    and candidate.get("format") == "codex-agent-manager-relay-account"
                ):
                    relay_key = _core.hashlib.sha256(
                        _core.json.dumps(candidate, ensure_ascii=False, sort_keys=True).encode("utf-8")
                    ).hexdigest()
                    if relay_key not in relay_parts_cache:
                        try:
                            relay_parts_cache[relay_key] = _core._relay_export_import_parts(candidate)
                        except (_core.ManagerError, ValueError) as exc:
                            documents.append({"kind": "account", "authJson": {}, "parseError": _core._redact_sensitive_text(exc, limit=320)})
                            continue
                    parts = relay_parts_cache[relay_key]
                    documents.append(
                        {
                            "kind": "relay",
                            "relay": parts,
                            "groupId": str(parts.get("groupId") or item_group_id or "relay"),
                            "label": str(
                                parts["preview"].get("siteName")
                                or _core.urllib.parse.urlsplit(parts["preview"].get("origin") or "").hostname
                                or "中转站账号"
                            ),
                        }
                    )
                    if len(documents) > _core.MAX_BATCH_IMPORT_ACCOUNTS:
                        raise _core.ManagerError(f"展开后超过 {_core.MAX_BATCH_IMPORT_ACCOUNTS} 个账号，请分批导入。")
                    continue
                if (
                    isinstance(candidate, dict)
                    and candidate.get("format") == "codex-agent-manager-api-account"
                    and isinstance(candidate.get("provider"), dict)
                ):
                    provider = _core.json.loads(_core.json.dumps(candidate["provider"]))
                    provider["_idWasExplicit"] = True
                    documents.append(
                        {
                            "kind": "provider",
                            "provider": provider,
                            "groupId": _core._imported_group_id(provider) or _core._imported_group_id(candidate) or item_group_id,
                            "label": str(provider.get("name") or provider.get("id") or "API 中转站"),
                        }
                    )
                    if len(documents) > _core.MAX_BATCH_IMPORT_ACCOUNTS:
                        raise _core.ManagerError(f"展开后超过 {_core.MAX_BATCH_IMPORT_ACCOUNTS} 个账号，请分批导入。")
                    continue
                native_export = isinstance(candidate, dict) and candidate.get("format") == "codex-agent-manager-account"
                try:
                    imported_provider = None if native_export else _core._provider_from_import_candidate(candidate)
                except (_core.ManagerError, ValueError) as exc:
                    documents.append({"kind": "account", "authJson": {}, "parseError": _core._redact_sensitive_text(exc, limit=320)})
                    continue
                if imported_provider is not None:
                    documents.append(
                        {
                            "kind": "provider",
                            "provider": imported_provider,
                            "groupId": _core._imported_group_id(imported_provider) or _core._imported_group_id(candidate) or item_group_id,
                            "label": str(imported_provider.get("name") or imported_provider.get("id") or "API 中转站"),
                        }
                    )
                    if len(documents) > _core.MAX_BATCH_IMPORT_ACCOUNTS:
                        raise _core.ManagerError(f"展开后超过 {_core.MAX_BATCH_IMPORT_ACCOUNTS} 个账号，请分批导入。")
                    continue
                candidate_label = str(candidate.get("label") or "").strip() if isinstance(candidate, dict) else ""
                # Keep a third-party wrapper around ``authJson`` intact.  The
                # normalizer knows how to unwrap it while retaining outer
                # identity metadata (email/user id/plan).  Dropping that
                # metadata made different Team members sharing one workspace
                # id look like duplicates during preview and bulk import.
                wrapped_auth = candidate
                imported_models = _core._account_models_from_import_candidate(candidate)
                if native_export:
                    if candidate.get("version") != 1:
                        documents.append({"kind": "account", "authJson": {}, "parseError": "账号导出文件版本不受支持。"})
                        continue
                    if not isinstance(candidate.get("authJson"), (dict, str)):
                        documents.append({"kind": "account", "authJson": {}, "parseError": "账号导出文件缺少 authJson 凭据。"})
                        continue
                    # Catalog metadata is retained in the batch record, but is
                    # not credential material and need not enter normalization.
                    wrapped_auth = {key: value for key, value in candidate.items() if key != "models"}
                documents.append(
                    {
                        "kind": "account",
                        "authJson": wrapped_auth,
                        "groupId": _core._imported_group_id(candidate) or item_group_id,
                        "label": candidate_label or ("" if multiple else label),
                        "capSidBase64": (
                            str(candidate.get("capSidBase64") or "")
                            if isinstance(candidate, dict) and candidate.get("capSidBase64")
                            else ""
                            if multiple
                            else item_cap_sid
                        ),
                        "sourceType": (
                            str(candidate.get("sourceType") or "")
                            if isinstance(candidate, dict) and candidate.get("sourceType")
                            else ""
                            if multiple
                            else item_source_type
                        ),
                        "models": imported_models,
                        "subscriptionExpiresAt": candidate.get("subscriptionExpiresAt") if isinstance(candidate, dict) else None,
                        "modelsLastCheckedAt": (
                            str(candidate.get("modelsLastCheckedAt") or "")
                            if isinstance(candidate, dict)
                            else ""
                        ),
                        "modelsRefreshedAt": (
                            str(candidate.get("modelsRefreshedAt") or "")
                            if isinstance(candidate, dict)
                            else ""
                        ),
                    }
                )
                if len(documents) > _core.MAX_BATCH_IMPORT_ACCOUNTS:
                    raise _core.ManagerError(f"展开后超过 {_core.MAX_BATCH_IMPORT_ACCOUNTS} 个账号，请分批导入。")
    if len(documents) > _core.MAX_BATCH_IMPORT_ACCOUNTS:
        raise _core.ManagerError(f"展开后超过 {_core.MAX_BATCH_IMPORT_ACCOUNTS} 个账号，请分批导入。")
    return documents



def _batch_auth_identity(document: dict, cache: dict) -> tuple[bytes, str, dict]:
    """Reuse exact credential normalization within this request only.

    Identity alone is deliberately not a cache key: a revoked token and its
    replacement can belong to the same account and must be validated separately.
    """
    if document.get("parseError"):
        raise _core.ManagerError(str(document["parseError"]))
    raw = document.get("authJson")
    encoded = (
        raw.encode("utf-8", errors="replace")
        if isinstance(raw, str)
        else _core.json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    key = _core.hashlib.sha256(encoded).hexdigest()
    if key not in cache:
        auth_bytes, detected = _core._normalize_import_auth_payload(raw)
        cache[key] = (auth_bytes, detected, _core._identity_from_auth_bytes(auth_bytes))
    auth_bytes, detected, identity = cache[key]
    source_type = _core._resolved_import_source_type(auth_bytes, detected, document.get("sourceType"))
    return auth_bytes, source_type, identity



def _preview_chatgpt_credential_status(auth_bytes: bytes) -> dict:
    """Perform one metadata-only validation for an imported ChatGPT account.

    The usage endpoint consumes no model tokens.  Only definitive authentication
    failures make the row invalid; network errors, 429 and 5xx remain selectable
    with a warning so a temporary outage cannot destroy a large batch import.
    """

    try:
        credentials = _core._chatgpt_credentials_from_auth_bytes(auth_bytes)
    except Exception as exc:
        return {
            "status": "unverified",
            "checked": False,
            "warning": (
                "未执行远程凭据检查："
                f"{_core._redact_sensitive_text(exc, limit=220)}"
            ),
            "error": "",
        }
    try:
        _core._fetch_chatgpt_json(
            _core.CHATGPT_USAGE_URL,
            credentials["accessToken"],
            credentials["accountId"],
        )
    except Exception as exc:
        message = _core._redact_sensitive_text(exc, limit=240)
        if _core._is_definitive_credential_error(message):
            return {
                "status": "invalid",
                "checked": True,
                "warning": "",
                "error": f"已自动排除失效凭据：{message}",
            }
        return {
            "status": "unverified",
            "checked": True,
            "warning": f"远程检查暂不可用，未自动排除：{message}",
            "error": "",
        }
    return {
        "status": "valid",
        "checked": True,
        "warning": "",
        "error": "",
    }



def preview_codex_accounts_batch(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise _core.ManagerError("批量导入预览请求必须是对象。")
    settings = _core.load_settings()
    if "groupId" in payload:
        _core._batch_target_group_id(settings, payload, {})
    existing_providers = {str(item.get("id")) for item in settings.get("providers", [])}
    existing_relay_accounts = {
        str(item.get("id")) for item in settings.get("relayAccounts", [])
    }
    batch_fingerprints: set[str] = set()
    batch_providers: set[str] = set()
    batch_provider_fingerprints: set[str] = set()
    batch_relay_accounts: set[str] = set()
    items = []
    validate_remote = bool(payload.get("validateRemote"))
    remote_probe_inputs: dict[str, bytes] = {}
    remote_probe_indices: dict[str, list[int]] = {}
    account_identity_indices: dict[str, list[int]] = {}
    account_base_warnings: dict[int, str] = {}
    auth_cache: dict = {}
    for index, document in enumerate(_core._batch_documents(payload)):
        target_group_id = _core._batch_target_group_id(settings, payload, document)
        if document.get("kind") == "relay":
            relay = document.get("relay") if isinstance(document.get("relay"), dict) else {}
            preview = relay.get("preview") if isinstance(relay.get("preview"), dict) else {}
            account_id = str(relay.get("accountId") or "")
            duplicate_in_batch = account_id in batch_relay_accounts
            if account_id:
                batch_relay_accounts.add(account_id)
            keys = relay.get("keySecrets") if isinstance(relay.get("keySecrets"), dict) else {}
            origin = str(preview.get("origin") or preview.get("portalUrl") or "")
            items.append(
                {
                    "index": index,
                    "kind": "relay",
                    "groupId": target_group_id,
                    "name": str(document.get("label") or preview.get("siteName") or "中转站账号"),
                    "email": origin,
                    "sourceType": "relay_account",
                    "planLabel": "网页登录中转站",
                    "importFormat": "可迁移中转站账号包",
                    "modelsCount": len(preview.get("models") or [])
                    if isinstance(preview.get("models"), list)
                    else 0,
                    "duplicate": account_id in existing_relay_accounts,
                    "duplicateInBatch": duplicate_in_batch,
                    "proxyEligible": True,
                    "tokenExpiresAt": None,
                    "codexCompatible": True,
                    "quotaOnly": False,
                    "capabilityLabel": f"{len(keys)} 个 Codex Key · 可保留账号级同步",
                    "warning": "",
                    "valid": bool(account_id and keys),
                    "error": "" if account_id and keys else "中转站账号包缺少可用 Key。",
                    "remoteValidation": "unverified",
                    "remoteChecked": False,
                }
            )
            continue
        if document.get("kind") == "provider":
            provider = document.get("provider") if isinstance(document.get("provider"), dict) else {}
            provider_id = str(provider.get("id") or "").strip()
            base_url = str(provider.get("baseUrl") or "").strip()
            name = str(provider.get("name") or provider_id or "API 中转站").strip()
            error = ""
            normalized_provider_id = provider_id
            provider_fingerprint = _core._imported_provider_fingerprint(provider)
            duplicate_in_batch = bool(
                provider_fingerprint and provider_fingerprint in batch_provider_fingerprints
            )
            try:
                normalized_provider_id = _core.slugify(provider_id, "Provider ID")
                if not duplicate_in_batch and (
                    normalized_provider_id in batch_providers
                    or (
                        normalized_provider_id in existing_providers
                        and not bool(provider.get("_idWasExplicit"))
                    )
                ):
                    normalized_provider_id = _core._unique_imported_provider_id(
                        normalized_provider_id,
                        str(provider.get("key") or ""),
                        existing_providers | batch_providers,
                    )
                if not name:
                    raise _core.ManagerError("中转站缺少名称。")
                base_url = _core._validated_provider_url(base_url, "中转站 Base URL")
                _core._validate_provider_env_key(
                    str(provider.get("envKey") or f"{normalized_provider_id.upper()}_API_KEY")
                )
                balance_endpoint = str(provider.get("balanceEndpoint") or "").strip()
                _core._validated_provider_related_url(
                    balance_endpoint,
                    base_url,
                    "中转站余额接口",
                    allow_empty=True,
                    allow_query=True,
                )
                _core._validated_provider_related_url(
                    provider.get("modelsEndpoint"),
                    base_url,
                    "中转站模型目录接口",
                    allow_empty=True,
                    allow_query=True,
                )
                _core._validated_provider_related_url(
                    provider.get("resolvedBaseUrl"),
                    base_url,
                    "中转站已探测 API Base URL",
                    allow_empty=True,
                )
                preset = _core._provider_portal_preset(provider.get("presetId"), allow_empty=True)
                if preset is None:
                    preset = _core.detect_provider_portal_preset(
                        base_url,
                        provider.get("portalUrl"),
                    )
                _core._validated_provider_portal_url(
                    provider.get("portalUrl"),
                    allow_empty=True,
                    preset=preset,
                )
                if str(provider.get("integrationKind") or "").strip().casefold() not in {"", "sub2api"}:
                    raise _core.ManagerError("中转站集成类型无效。")
                if not str(provider.get("key") or "").strip():
                    raise _core.ManagerError("中转站导出文件缺少 API Key。")
            except _core.ManagerError as exc:
                error = _core._redact_sensitive_text(exc, limit=320)
            if normalized_provider_id and not duplicate_in_batch:
                batch_providers.add(normalized_provider_id)
            if provider_fingerprint and not error:
                batch_provider_fingerprints.add(provider_fingerprint)
            items.append(
                {
                    "index": index,
                    "kind": "provider",
                    "groupId": target_group_id,
                    "name": name,
                    "email": base_url,
                    "sourceType": "api_provider",
                    "credentialKind": "api_key",
                    "planLabel": "中转站",
                    "modelsCount": len(provider.get("models") or []) if isinstance(provider.get("models"), list) else 0,
                    "duplicate": bool(
                        provider.get("_idWasExplicit")
                        and normalized_provider_id in existing_providers
                    ),
                    "duplicateInBatch": duplicate_in_batch,
                    "proxyEligible": False,
                    "tokenExpiresAt": None,
                    "codexCompatible": False,
                    "quotaOnly": False,
                    "capabilityLabel": "API 中转站",
                    "warning": "",
                    "valid": not error,
                    "error": error,
                    "remoteValidation": "unverified",
                    "remoteChecked": False,
                }
            )
            continue
        try:
            auth_bytes, source_type, identity = _core._batch_auth_identity(document, auth_cache)
            cap_sid = str(document.get("capSidBase64") or "")
            if cap_sid:
                try:
                    decoded_cap_sid = _core.base64.b64decode(cap_sid, validate=True)
                except ValueError as exc:
                    raise _core.ManagerError("cap_sid Base64 无效。") from exc
                if len(decoded_cap_sid) > 256_000:
                    raise _core.ManagerError("cap_sid 文件异常过大。")
            fingerprint = str(identity.get("fingerprint") or "")
            duplicate_in_batch = bool(fingerprint and fingerprint in batch_fingerprints)
            if fingerprint:
                batch_fingerprints.add(fingerprint)
            web_session = source_type == "web_session"
            free_plan = _core._is_free_plan(identity.get("plan"), _core._normalize_plan_label(identity.get("plan")))
            short_lived_oauth = identity.get("credentialKind") == "oauth_access_token"
            base_warning = (
                "OAuth access token 可用于短期反代；未验证远端有效性，缺少续期链时到期后需重新导入。"
                if short_lived_oauth
                else
                "Free Web Session 无 Codex 推理权限，仅导入额度和模型目录。"
                if web_session and free_plan
                else "将本地转换为外部 Token 格式，并用无模型调用的授权探测确认是否可用于 Codex。"
                if web_session
                else ""
            )
            item = {
                    "index": index,
                    "kind": "account",
                    "groupId": target_group_id,
                    "name": str(document.get("label") or identity["display"]),
                    "email": identity.get("email") or "",
                    "sourceType": source_type,
                    "authMode": identity.get("authMode") or "",
                    "credentialKind": identity.get("credentialKind") or "",
                    "refreshCapable": bool(identity.get("refreshCapable")),
                    "planLabel": _core._normalize_plan_label(identity.get("plan")),
                    "importFormat": identity.get("importFormat") or "",
                    "duplicate": _core._find_account_for_identity(settings, identity) is not None,
                    "duplicateInBatch": duplicate_in_batch,
                    "proxyEligible": identity.get("authMode") == "chatgpt",
                    "codexCompatible": source_type != "web_session",
                    "quotaOnly": web_session,
                    "compatibilityPending": web_session and not free_plan,
                    "capabilityLabel": (
                        "OAuth access token · 待验证"
                        if short_lived_oauth
                        else
                        "Free · 仅额度查询"
                        if web_session and free_plan
                        else "导入后自动检测 Codex 权限"
                        if web_session
                        else "Agent Identity · 可直接切换"
                        if identity.get("authMode") == "agent_identity"
                        else "Personal Access Token · 可直接切换"
                        if identity.get("authMode") == "personal_access_token"
                        else "可用于 Codex"
                    ),
                    "warning": (
                        "本批次前面已有同一账号；默认只选择第一份，避免重复写入。"
                        if duplicate_in_batch
                        else base_warning
                    ),
                    "tokenExpiresAt": identity.get("tokenExpiresAt"),
                    "valid": True,
                    "error": "",
                    "remoteValidation": (
                        "pending"
                        if validate_remote and identity.get("authMode") == "chatgpt"
                        else "unverified"
                        if identity.get("authMode") == "chatgpt"
                        else "not_applicable"
                    ),
                }
            items.append(item)
            item_position = len(items) - 1
            account_base_warnings[item_position] = base_warning
            if fingerprint:
                account_identity_indices.setdefault(fingerprint, []).append(item_position)
            if validate_remote and identity.get("authMode") == "chatgpt":
                # The same account can appear with an old revoked token and a
                # freshly exported replacement.  Deduplicate only byte-identical
                # credentials; grouping by account fingerprint would let the
                # stale token incorrectly condemn the replacement as well.
                probe_key = _core.hashlib.sha256(auth_bytes).hexdigest()
                remote_probe_inputs.setdefault(probe_key, auth_bytes)
                remote_probe_indices.setdefault(probe_key, []).append(item_position)
        except Exception as exc:
            items.append(
                {
                    "index": index,
                    "kind": "account",
                    "name": str(document.get("label") or f"第 {index + 1} 个账号"),
                    "email": "",
                    "sourceType": "unknown",
                    "authMode": "",
                    "planLabel": "",
                    "duplicate": False,
                    "duplicateInBatch": False,
                    "proxyEligible": False,
                    "codexCompatible": False,
                    "quotaOnly": False,
                    "capabilityLabel": "无法识别",
                    "warning": "",
                    "tokenExpiresAt": None,
                    "valid": False,
                    "error": _core._redact_sensitive_text(exc, limit=320),
                    "remoteValidation": "not_applicable",
                }
            )

    if remote_probe_inputs:
        probe_results: dict[str, dict] = {}
        worker_count = max(1, min(2, len(remote_probe_inputs)))

        def invoke(probe_key: str) -> dict:
            return _core._preview_chatgpt_credential_status(remote_probe_inputs[probe_key])

        probe_keys = list(remote_probe_inputs)
        next_probe = 0
        stop_scheduling = False
        with _core.ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="import-preview",
        ) as executor:
            pending = {}

            def fill_pending() -> None:
                nonlocal next_probe
                while (
                    not stop_scheduling
                    and len(pending) < worker_count
                    and next_probe < len(probe_keys)
                ):
                    probe_key = probe_keys[next_probe]
                    next_probe += 1
                    pending[executor.submit(invoke, probe_key)] = probe_key

            fill_pending()
            while pending:
                completed, _ = _core.wait(tuple(pending), return_when=_core.FIRST_COMPLETED)
                for future in completed:
                    probe_key = pending.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {
                            "status": "unverified",
                            "checked": False,
                            "warning": (
                                "远程检查暂不可用，未自动排除："
                                f"{_core._redact_sensitive_text(exc, limit=220)}"
                            ),
                            "error": "",
                        }
                    probe_results[probe_key] = result
                    # A checked-but-unverified result means the shared remote
                    # endpoint is rate-limited, unavailable or returning an
                    # unexpected response.  Do not hammer it once per remaining
                    # account; leave unscheduled rows selectable instead.
                    if result.get("status") == "unverified" and result.get("checked"):
                        stop_scheduling = True
                fill_pending()

        if stop_scheduling:
            for probe_key in probe_keys:
                probe_results.setdefault(
                    probe_key,
                    {
                        "status": "unverified",
                        "checked": False,
                        "warning": (
                            "为避免频繁请求，检测到远端限流或网络异常后已停止继续检查；"
                            "该账号未自动排除。"
                        ),
                        "error": "",
                    },
                )

        for probe_key, item_indices in remote_probe_indices.items():
            result = probe_results.get(probe_key) or {
                "status": "unverified",
                "warning": "远程检查未返回结果，未自动排除。",
                "error": "",
            }
            for item_index in item_indices:
                item = items[item_index]
                item["remoteValidation"] = str(result.get("status") or "unverified")
                item["remoteChecked"] = bool(result.get("checked"))
                if result.get("status") == "invalid":
                    item["valid"] = False
                    item["credentialInvalid"] = True
                    item["error"] = str(result.get("error") or "已自动排除失效凭据。")
                    item["warning"] = ""
                elif result.get("status") == "valid":
                    item["credentialInvalid"] = False
                    item["capabilityLabel"] = f"{item.get('capabilityLabel') or '可用于 Codex'} · 凭据有效"
                elif result.get("warning"):
                    item["remoteWarning"] = str(result["warning"]).strip()

        # Invalid rows do not reserve an identity's duplicate slot.  This lets
        # a later valid export of the same account remain selectable when an
        # earlier stale token was rejected with 401/402.
        for item_indices in account_identity_indices.values():
            have_selectable = False
            for item_index in item_indices:
                item = items[item_index]
                if not item.get("valid"):
                    item["duplicateInBatch"] = False
                    item["warning"] = ""
                    continue
                item["duplicateInBatch"] = have_selectable
                duplicate_warning = (
                    "本批次前面已有同一账号；默认只选择第一份，避免重复写入。"
                    if have_selectable
                    else account_base_warnings.get(item_index, "")
                )
                item["warning"] = "；".join(
                    value
                    for value in (
                        duplicate_warning,
                        str(item.pop("remoteWarning", "") or "").strip(),
                    )
                    if value
                )
                have_selectable = True
    valid = sum(1 for item in items if item["valid"])
    unique = sum(1 for item in items if item["valid"] and not item.get("duplicateInBatch"))
    return {
        "items": items,
        "total": len(items),
        "valid": valid,
        "unique": unique,
        "invalid": len(items) - valid,
        "duplicatesInBatch": valid - unique,
        "remoteChecked": sum(
            1
            for item in items
            if item.get("remoteChecked")
        ),
        "remoteInvalid": sum(1 for item in items if item.get("credentialInvalid")),
    }



def import_codex_accounts_batch(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise _core.ManagerError("批量导入请求必须是对象。")
    settings = _core.load_settings()
    if "groupId" in payload:
        _core._batch_target_group_id(settings, payload, {})
    if "proxyEnabled" in payload and not isinstance(payload.get("proxyEnabled"), bool):
        raise _core.ManagerError("proxyEnabled 必须是布尔值。")
    proxy_enabled = bool(payload.get("proxyEnabled"))
    proxy_selection_explicit = "proxyEnabled" in payload
    imported: list[dict] = []
    imported_providers = []
    imported_relay_accounts = []
    failed = []
    skipped_duplicates = []
    documents = _core._batch_documents(payload)
    selected_raw = payload.get("selectedIndices")
    if selected_raw is not None and not isinstance(selected_raw, list):
        raise _core.ManagerError("selectedIndices 必须是数组。")
    if selected_raw is not None and any(type(value) is not int for value in selected_raw):
        raise _core.ManagerError("selectedIndices 只能包含整数。")
    selected = None if selected_raw is None else set(selected_raw)
    if selected_raw is not None and len(selected) != len(selected_raw):
        raise _core.ManagerError("selectedIndices 不能包含重复序号。")
    if selected is not None and not selected:
        raise _core.ManagerError("请至少选择一个可导入账号。")
    if selected is not None and any(index < 0 or index >= len(documents) for index in selected):
        raise _core.ManagerError("导入选择中包含不存在的账号。")
    prepared: list[dict] = []
    provider_documents: list[tuple[int, dict]] = []
    relay_documents: list[tuple[int, dict]] = []
    batch_fingerprints: set[str] = set()
    batch_provider_ids: set[str] = set()
    batch_provider_fingerprints: set[str] = set()
    batch_relay_ids: set[str] = set()
    auth_cache: dict = {}
    for index, document in enumerate(documents):
        if selected is not None and index not in selected:
            continue
        try:
            target_group_id = _core._batch_target_group_id(settings, payload, document)
            document = {**document, "targetGroupId": target_group_id}
            if document.get("kind") == "relay":
                relay = document.get("relay") if isinstance(document.get("relay"), dict) else {}
                relay_id = str(relay.get("accountId") or "")
                if relay_id in batch_relay_ids:
                    skipped_duplicates.append({"index": index, "reason": "本批次中转站账号重复"})
                    continue
                if relay_id:
                    batch_relay_ids.add(relay_id)
                relay_documents.append((index, document))
                continue
            if document.get("kind") == "provider":
                raw_provider = document.get("provider") if isinstance(document.get("provider"), dict) else {}
                provider_id = str(raw_provider.get("id") or "").strip().casefold()
                provider_fingerprint = _core._imported_provider_fingerprint(raw_provider)
                if provider_fingerprint and provider_fingerprint in batch_provider_fingerprints:
                    skipped_duplicates.append({"index": index, "reason": "本批次中 API 账号重复"})
                    continue
                if provider_fingerprint:
                    batch_provider_fingerprints.add(provider_fingerprint)
                known_provider_ids = batch_provider_ids | {
                    str(item.get("id") or "") for item in settings.get("providers", [])
                }
                if provider_id and (
                    provider_id in batch_provider_ids
                    or (provider_id in known_provider_ids and not bool(raw_provider.get("_idWasExplicit")))
                ):
                    provider_id = _core._unique_imported_provider_id(
                        provider_id,
                        str(raw_provider.get("key") or ""),
                        known_provider_ids,
                    )
                    raw_provider = {**raw_provider, "id": provider_id}
                    raw_provider["envKey"] = _core._safe_imported_provider_env_key(
                        "",
                        f"{provider_id.upper()}_API_KEY",
                    )
                    document = {**document, "provider": raw_provider}
                if provider_id:
                    batch_provider_ids.add(provider_id)
                provider_documents.append((index, document))
                continue
            auth_bytes, source_type, identity = _core._batch_auth_identity(document, auth_cache)
            cap_sid_bytes = None
            if document.get("capSidBase64"):
                try:
                    cap_sid_bytes = _core.base64.b64decode(str(document["capSidBase64"]), validate=True)
                except ValueError as exc:
                    raise _core.ManagerError("cap_sid Base64 无效。") from exc
            if cap_sid_bytes is not None and len(cap_sid_bytes) > 256_000:
                raise _core.ManagerError("cap_sid 文件异常过大，已拒绝保存。")
            fingerprint = str(identity.get("fingerprint") or "")
            if fingerprint and fingerprint in batch_fingerprints:
                skipped_duplicates.append({"index": index, "reason": "本批次中账号重复"})
                continue
            if fingerprint:
                batch_fingerprints.add(fingerprint)
            snapshot, identity = _core._snapshot_from_bytes(auth_bytes, cap_sid_bytes)
            prepared.append(
                {
                    "index": index,
                    "snapshot": snapshot,
                    "identity": identity,
                    "payload": {
                        "label": document.get("label") or "",
                        "groupId": target_group_id,
                        "proxyEnabled": proxy_enabled,
                        "sourceType": source_type,
                        "subscriptionExpiresAt": document.get("subscriptionExpiresAt"),
                        "models": document.get("models") if isinstance(document.get("models"), list) else [],
                        "modelsLastCheckedAt": document.get("modelsLastCheckedAt") or None,
                        "modelsRefreshedAt": document.get("modelsRefreshedAt") or None,
                    },
                }
            )
        except Exception as exc:
            failed.append({"index": index, "error": _core._redact_sensitive_text(exc, limit=320)})

    encrypted_prepared = []
    if prepared and _core._credential_store_mode() == "keyring":
        raise _core.ManagerError("当前 Codex 使用 keyring 凭据存储，不能通过 auth.json 快照切换。")
    for item in prepared:
        try:
            encrypted_prepared.append({**item, "encrypted": _core._encrypted_account_snapshot(item["snapshot"])})
        except Exception as exc:
            failed.append(
                {
                    "index": item["index"],
                    "error": f"加密账号快照失败：{_core._redact_sensitive_text(exc, limit=280)}",
                }
            )

    if encrypted_prepared:
        with _core.SETTINGS_LOCK, _core.SECRETS_LOCK, _core._settings_file_lock():
            current_settings = _core.load_settings()
            secrets_existed = _core.SECRETS_FILE.exists()
            secrets_before = _core.SECRETS_FILE.read_bytes() if secrets_existed else None
            secrets_payload = _core._secret_store()
            committed: list[dict] = []
            for item in encrypted_prepared:
                try:
                    record = _core._account_record_from_import(current_settings, item["payload"], item["identity"])
                    secrets_payload["accounts"][record["id"]] = item["encrypted"]
                    _core._merge_by_id(current_settings.setdefault("accounts", []), record)
                    _core._sync_account_proxy_membership(current_settings, record)
                    committed.append(record)
                except Exception as exc:
                    failed.append(
                        {
                            "index": item["index"],
                            "error": _core._redact_sensitive_text(exc, limit=320),
                        }
                    )
            if committed:
                try:
                    # One encrypted-store write plus one settings write keeps a
                    # thousand-account batch linear instead of repeatedly
                    # rewriting the complete files for every account.
                    _core.atomic_write_json(_core.SECRETS_FILE, secrets_payload)
                    _core.save_settings(current_settings)
                except Exception:
                    if secrets_before is None:
                        _core.SECRETS_FILE.unlink(missing_ok=True)
                    else:
                        _core.atomic_write_bytes(_core.SECRETS_FILE, secrets_before)
                    raise
                imported.extend(committed)

    for index, document in provider_documents:
        try:
            provider_payload = _core.json.loads(_core.json.dumps(document.get("provider") or {}))
            provider_payload.pop("_idWasExplicit", None)
            provider_payload.update(
                {
                    "groupId": str(document.get("targetGroupId") or "official"),
                    "fetchModels": not bool(provider_payload.get("models")),
                    "activate": False,
                    # Keep the import dialog's “同步到 API” choice for API
                    # provider documents as well as OAuth/token accounts.  The
                    # provider branch used to silently drop this value, leaving
                    # a successfully imported relay outside the local pool.
                    "proxyEnabled": (
                        proxy_enabled
                        if proxy_selection_explicit
                        else bool(provider_payload.get("proxyEnabled"))
                    ),
                }
            )
            imported_providers.append(_core.import_api_account(provider_payload))
        except Exception as exc:
            failed.append({"index": index, "error": _core._redact_sensitive_text(exc, limit=320)})

    for index, document in relay_documents:
        try:
            relay = document.get("relay") if isinstance(document.get("relay"), dict) else {}
            target_proxy_enabled = (
                proxy_enabled
                if proxy_selection_explicit
                else bool(relay.get("proxyEnabled"))
            )
            result = _core.import_relay_account(
                _core.json.loads(_core.json.dumps(relay.get("preview") or {})),
                dict(relay.get("keySecrets") or {}),
                group_id=str(document.get("targetGroupId") or "relay"),
                proxy_enabled=target_proxy_enabled,
                endpoint_id=relay.get("selectedEndpointId"),
                selected_key_id=relay.get("selectedKeyId"),
                relay_account_id=relay.get("accountId"),
            )
            dashboard_session = relay.get("dashboardSession")
            if isinstance(dashboard_session, dict):
                _core.store_relay_account_dashboard_session(
                    str((result.get("account") or {}).get("id") or relay.get("accountId") or ""),
                    dashboard_session,
                )
                result["dashboardSessionImported"] = True
            else:
                result["dashboardSessionImported"] = False
            imported_relay_accounts.append(result)
        except Exception as exc:
            failed.append({"index": index, "error": _core._redact_sensitive_text(exc, limit=320)})

    refresh_account_ids = [str(item["id"]) for item in imported if item.get("authMode") == "chatgpt"]
    if refresh_account_ids and not payload.get("deferRefresh"):
        _core.refresh_codex_accounts(refresh_account_ids)
        current = {item.get("id"): item for item in _core.load_settings().get("accounts", [])}
        imported = [current.get(item.get("id"), item) for item in imported]
    return {
        "imported": imported,
        "importedProviders": imported_providers,
        "importedRelayAccounts": imported_relay_accounts,
        "failed": failed,
        "skippedDuplicates": skipped_duplicates,
        "refreshAccountIds": refresh_account_ids if payload.get("deferRefresh") else [],
        "total": (
            len(imported)
            + len(imported_providers)
            + len(imported_relay_accounts)
            + len(failed)
            + len(skipped_duplicates)
        ),
    }



def _codex_client_version() -> str:
    """Return the semantic version expected by the Codex models endpoint."""
    match = _core.re.search(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", _core.codex_version())
    return match.group(0) if match else "0.1.0"

