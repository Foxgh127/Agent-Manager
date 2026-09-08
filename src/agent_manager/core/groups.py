"""Groups services."""
from __future__ import annotations
from agent_manager import core as _core


def _account_group(settings: dict, group_id: str) -> dict:
    group = next((item for item in settings.get("accountGroups", []) if item.get("id") == group_id), None)
    if not group:
        raise _core.ManagerError("账号分组不存在。")
    return group



def save_account_group(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise _core.ManagerError("分组内容必须是对象。")
    with _core.SETTINGS_LOCK, _core._settings_file_lock():
        return _core._save_account_group_locked(payload)



def _save_account_group_locked(payload: dict) -> dict:
    settings = _core.load_settings()
    original_id = str(payload.get("originalId") or "").strip()
    existing = next((item for item in settings.get("accountGroups", []) if item.get("id") == original_id), None)
    if original_id and existing is None:
        raise _core.ManagerError("要修改的分组已不存在，请刷新后重试。")
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 32:
        raise _core.ManagerError("分组名称不能为空且不能超过 32 个字符。")
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise _core.ManagerError("分组名称不能包含控制字符。")
    if existing and existing.get("system"):
        group_id = original_id
    else:
        requested_id = str(payload.get("id") or "").strip()
        group_id = _core.slugify(requested_id, "分组 ID") if requested_id else original_id or f"group_{_core.uuid.uuid4().hex[:10]}"
    duplicate = next(
        (item for item in settings.get("accountGroups", []) if item.get("id") == group_id and item is not existing),
        None,
    )
    if duplicate:
        raise _core.ManagerError("分组 ID 已存在。")
    color = str(payload.get("color") or (existing or {}).get("color") or "slate")
    if color not in _core.VALID_GROUP_COLORS:
        color = "slate"
    record = {
        "id": group_id,
        "name": name,
        "color": color,
        "sortOrder": int((existing or {}).get("sortOrder", len(settings.get("accountGroups", [])))),
        "system": bool((existing or {}).get("system", False)),
    }
    if existing:
        existing.update(record)
        if original_id != group_id:
            for collection in ("accounts", "providers", "relayAccounts"):
                for item in settings.get(collection, []):
                    if item.get("groupId") == original_id:
                        item["groupId"] = group_id
                        item["updatedAt"] = _core.now_iso()
    else:
        settings.setdefault("accountGroups", []).append(record)
    _core.save_settings(settings)
    return record



def remove_account_group(group_id: str) -> dict:
    with _core.SETTINGS_LOCK, _core._settings_file_lock():
        return _core._remove_account_group_locked(group_id)



def _remove_account_group_locked(group_id: str) -> dict:
    settings = _core.load_settings()
    group = _core._account_group(settings, group_id)
    if group.get("system"):
        raise _core.ManagerError("内置分组不能删除。")
    moved = 0
    for account in settings.get("accounts", []):
        if account.get("groupId") == group_id:
            account["groupId"] = "official"
            account["updatedAt"] = _core.now_iso()
            moved += 1
    moved_providers = 0
    for provider in settings.get("providers", []):
        if provider.get("groupId") == group_id:
            provider["groupId"] = "official" if provider.get("kind") == "builtin" else "relay"
            moved_providers += 1
    for relay_account in settings.get("relayAccounts", []):
        if relay_account.get("groupId") == group_id:
            relay_account["groupId"] = "relay"
            relay_account["updatedAt"] = _core.now_iso()
    settings["accountGroups"] = [item for item in settings["accountGroups"] if item.get("id") != group_id]
    _core.save_settings(settings)
    return {"movedAccounts": moved, "movedProviders": moved_providers}



def assign_sources_to_group(group_id: str, account_ids: list[str], provider_ids: list[str]) -> dict:
    if not isinstance(account_ids, list) or not isinstance(provider_ids, list):
        raise _core.ManagerError("要移动的账号与 Provider 必须是数组。")
    if any(not isinstance(item, str) for item in [*account_ids, *provider_ids]):
        raise _core.ManagerError("账号与 Provider ID 必须是字符串。")
    with _core.SETTINGS_LOCK, _core._settings_file_lock():
        return _core._assign_sources_to_group_locked(group_id, account_ids, provider_ids)



def _assign_sources_to_group_locked(group_id: str, account_ids: list[str], provider_ids: list[str]) -> dict:
    settings = _core.load_settings()
    _core._account_group(settings, group_id)
    requested_accounts = {str(account_id) for account_id in account_ids if str(account_id).strip()}
    requested_providers = {str(provider_id) for provider_id in provider_ids if str(provider_id).strip()}
    if len(requested_accounts) + len(requested_providers) > 100:
        raise _core.ManagerError("单次最多移动 100 个账号。")
    known_accounts = {str(item.get("id")) for item in settings.get("accounts", [])}
    known_providers = {str(item.get("id")) for item in settings.get("providers", [])}
    missing_accounts = requested_accounts - known_accounts
    missing_providers = requested_providers - known_providers
    if missing_accounts:
        raise _core.ManagerError(f"账号不存在：{', '.join(sorted(missing_accounts)[:4])}")
    if missing_providers:
        raise _core.ManagerError(f"中转站不存在：{', '.join(sorted(missing_providers)[:4])}")
    changed_accounts = 0
    for account in settings.get("accounts", []):
        if account.get("id") in requested_accounts and account.get("groupId") != group_id:
            account["groupId"] = group_id
            account["updatedAt"] = _core.now_iso()
            changed_accounts += 1
    changed_providers = 0
    for provider in settings.get("providers", []):
        if provider.get("id") in requested_providers and provider.get("groupId") != group_id:
            provider["groupId"] = group_id
            provider["updatedAt"] = _core.now_iso()
            changed_providers += 1
    requested_relay_ids = {
        str(provider.get("relayAccountId") or "")
        for provider in settings.get("providers", [])
        if str(provider.get("id") or "") in requested_providers
        and str(provider.get("relayAccountId") or "")
    }
    requested_relay_ids.update(
        str(item.get("id") or "") for item in settings.get("relayAccounts", [])
        if str(item.get("providerId") or "") in requested_providers
    )
    for relay_account in settings.get("relayAccounts", []):
        if str(relay_account.get("id") or "") in requested_relay_ids:
            relay_account["groupId"] = group_id
            relay_account["updatedAt"] = _core.now_iso()
    for provider in settings.get("providers", []):
        if str(provider.get("relayAccountId") or "") in requested_relay_ids and provider.get("groupId") != group_id:
            provider["groupId"] = group_id
            provider["updatedAt"] = _core.now_iso()
            changed_providers += 1
    _core.save_settings(settings)
    return {
        "changed": changed_accounts + changed_providers,
        "changedAccounts": changed_accounts,
        "changedProviders": changed_providers,
        "groupId": group_id,
    }



def update_codex_account_metadata(account_id: str, payload: dict) -> dict:
    """Edit user-owned account metadata without touching encrypted credentials."""
    if not isinstance(payload, dict):
        raise _core.ManagerError("账号编辑内容必须是对象。")
    with _core.SETTINGS_LOCK:
        settings = _core.load_settings()
        account = next(
            (item for item in settings.get("accounts", []) if str(item.get("id")) == account_id),
            None,
        )
        if not account:
            raise _core.ManagerError("账号不存在。")
        label = str(payload.get("label") or "").strip()
        if not label:
            label = str(account.get("email") or account.get("name") or account_id).strip()
        if len(label) > 120:
            raise _core.ManagerError("账号名称不能超过 120 个字符。")
        group_id = str(payload.get("groupId") or account.get("groupId") or "official").strip()
        _core._account_group(settings, group_id)
        account["label"] = label
        account["groupId"] = group_id
        account["updatedAt"] = _core.now_iso()
        _core.save_settings(settings)
        return _core.json.loads(_core.json.dumps(account))



def set_account_proxy_enabled(account_id: str, enabled: bool) -> dict:
    settings = _core.load_settings()
    account = next((item for item in settings.get("accounts", []) if item.get("id") == account_id), None)
    if not account:
        raise _core.ManagerError("账号不存在。")
    if account.get("authMode") != "chatgpt":
        raise _core.ManagerError("只有 ChatGPT Token 账号可以加入 Web2API 账号池。")
    if enabled and not _core._account_codex_compatible(account):
        raise _core.ManagerError("该 Web Session 仅支持额度查询，不能加入 Codex API 号池。")
    account["proxyEnabled"] = bool(enabled)
    account["proxyRequested"] = bool(enabled)
    account["updatedAt"] = _core.now_iso()
    member_ids = list(settings.setdefault("web2api", _core._default_web2api_settings()).get("accountIds", []))
    if enabled:
        if account_id not in member_ids:
            member_ids.append(account_id)
    else:
        member_ids = [item for item in member_ids if item != account_id]
    settings["web2api"]["accountIds"] = member_ids
    source_id = f"account:{account_id}"
    source_order = [str(item) for item in settings["web2api"].get("sourceOrder", [])]
    if enabled and source_id not in source_order:
        source_order.append(source_id)
    elif not enabled:
        source_order = [item for item in source_order if item != source_id]
    settings["web2api"]["sourceOrder"] = source_order
    _core.save_settings(settings)
    return account



def set_provider_proxy_enabled(provider_id: str, enabled: bool) -> dict:
    settings = _core.load_settings()
    provider = next(
        (
            item
            for item in settings.get("providers", [])
            if item.get("id") == provider_id and item.get("kind") == "custom"
        ),
        None,
    )
    if not provider:
        raise _core.ManagerError("API Provider 不存在。")
    if enabled and not _core.provider_key_configured(provider_id):
        raise _core.ManagerError("该 API Provider 尚未保存 API Key。")
    if enabled and not provider.get("models"):
        raise _core.ManagerError("该 API Provider 尚无可用模型，请先刷新模型目录。")
    web2api = settings.setdefault("web2api", _core._default_web2api_settings())
    provider_ids = [str(item) for item in web2api.get("providerIds", [])]
    if enabled and provider_id not in provider_ids:
        provider_ids.append(provider_id)
    elif not enabled:
        provider_ids = [item for item in provider_ids if item != provider_id]
    source_id = f"provider:{provider_id}"
    source_order = [str(item) for item in web2api.get("sourceOrder", [])]
    if enabled and source_id not in source_order:
        source_order.append(source_id)
    elif not enabled:
        source_order = [item for item in source_order if item != source_id]
    provider["proxyEnabled"] = bool(enabled)
    provider["updatedAt"] = _core.now_iso()
    web2api["providerIds"] = provider_ids
    web2api["sourceOrder"] = source_order
    _core.save_settings(settings)
    return provider



def set_accounts_proxy_enabled_batch(account_ids: list[str], enabled: bool) -> dict:
    if not isinstance(account_ids, list):
        raise _core.ManagerError("accountIds 必须是数组。")
    requested = list(dict.fromkeys(str(item) for item in account_ids if str(item).strip()))
    if not requested:
        raise _core.ManagerError("请至少选择一个账号。")
    if len(requested) > 100:
        raise _core.ManagerError("单次最多调整 100 个账号。")
    settings = _core.load_settings()
    accounts = {str(item.get("id")): item for item in settings.get("accounts", [])}
    missing = [item for item in requested if item not in accounts]
    if missing:
        raise _core.ManagerError(f"账号不存在：{', '.join(missing[:4])}")
    unsupported = [
        item
        for item in requested
        if accounts[item].get("authMode") != "chatgpt"
        or (enabled and not _core._account_codex_compatible(accounts[item]))
    ]
    if unsupported:
        labels = [str(accounts[item].get("label") or item) for item in unsupported[:4]]
        raise _core.ManagerError(f"以下账号没有可用的 Codex 推理凭据，不能加入 API：{', '.join(labels)}")
    pool = list(settings.setdefault("web2api", _core._default_web2api_settings()).get("accountIds", []))
    if enabled:
        for account_id in requested:
            if account_id not in pool:
                pool.append(account_id)
    else:
        requested_set = set(requested)
        pool = [item for item in pool if item not in requested_set]
    now = _core.now_iso()
    for account_id in requested:
        accounts[account_id]["proxyEnabled"] = bool(enabled)
        accounts[account_id]["proxyRequested"] = bool(enabled)
        accounts[account_id]["updatedAt"] = now
    settings["web2api"]["accountIds"] = pool
    source_order = [str(item) for item in settings["web2api"].get("sourceOrder", [])]
    requested_sources = [f"account:{account_id}" for account_id in requested]
    if enabled:
        source_order.extend(item for item in requested_sources if item not in source_order)
    else:
        source_order = [item for item in source_order if item not in set(requested_sources)]
    settings["web2api"]["sourceOrder"] = source_order
    _core.save_settings(settings)
    return {
        "changed": len(requested),
        "enabled": bool(enabled),
        "accountIds": requested,
        "pool": pool,
        "sourceOrder": source_order,
    }



def set_providers_proxy_enabled_batch(provider_ids: list[str], enabled: bool) -> dict:
    if not isinstance(provider_ids, list):
        raise _core.ManagerError("providerIds 必须是数组。")
    requested = list(dict.fromkeys(str(item) for item in provider_ids if str(item).strip()))
    if not requested:
        raise _core.ManagerError("请至少选择一个 API Provider。")
    if len(requested) > 100:
        raise _core.ManagerError("单次最多调整 100 个 API Provider。")
    settings = _core.load_settings()
    providers = {
        str(item.get("id")): item
        for item in settings.get("providers", [])
        if item.get("kind") == "custom" and item.get("id")
    }
    missing = [item for item in requested if item not in providers]
    if missing:
        raise _core.ManagerError(f"API Provider 不存在：{', '.join(missing[:4])}")
    if enabled:
        unsupported = [
            provider_id
            for provider_id in requested
            if not _core.provider_key_configured(provider_id) or not providers[provider_id].get("models")
        ]
        if unsupported:
            labels = [str(providers[item].get("name") or item) for item in unsupported[:4]]
            raise _core.ManagerError(f"以下 Provider 缺少 Key 或模型目录，不能加入 API：{', '.join(labels)}")
    web2api = settings.setdefault("web2api", _core._default_web2api_settings())
    pool = [str(item) for item in web2api.get("providerIds", [])]
    if enabled:
        pool.extend(item for item in requested if item not in pool)
    else:
        requested_set = set(requested)
        pool = [item for item in pool if item not in requested_set]
    requested_sources = [f"provider:{item}" for item in requested]
    source_order = [str(item) for item in web2api.get("sourceOrder", [])]
    if enabled:
        source_order.extend(item for item in requested_sources if item not in source_order)
    else:
        requested_source_set = set(requested_sources)
        source_order = [item for item in source_order if item not in requested_source_set]
    now = _core.now_iso()
    for provider_id in requested:
        providers[provider_id]["proxyEnabled"] = bool(enabled)
        providers[provider_id]["updatedAt"] = now
    web2api["providerIds"] = pool
    web2api["sourceOrder"] = source_order
    _core.save_settings(settings)
    return {
        "changed": len(requested),
        "enabled": bool(enabled),
        "providerIds": requested,
        "providerPool": pool,
        "sourceOrder": source_order,
    }



def save_web2api_settings(payload: dict) -> dict:
    settings = _core.load_settings()
    current = settings.setdefault("web2api", _core._default_web2api_settings())
    try:
        port = int(payload.get("port", current.get("port", 17860)))
    except (TypeError, ValueError) as exc:
        raise _core.ManagerError("Web2API 端口必须是数字。") from exc
    if port < 1024 or port > 65535:
        raise _core.ManagerError("Web2API 端口必须在 1024 到 65535 之间。")
    routing = str(payload.get("routing") or current.get("routing") or "ordered")
    if routing not in _core.VALID_WEB2API_ROUTING:
        raise _core.ManagerError("Web2API 调度模式无效。")
    account_ids = payload.get("accountIds", current.get("accountIds", []))
    if not isinstance(account_ids, list):
        raise _core.ManagerError("Web2API 账号池格式无效。")
    valid_accounts = {
        item.get("id")
        for item in settings.get("accounts", [])
        if item.get("authMode") == "chatgpt" and _core._account_codex_compatible(item)
    }
    selected = []
    for account_id in account_ids:
        value = str(account_id)
        if value not in valid_accounts:
            raise _core.ManagerError(f"Web2API 账号不可用：{value}")
        if value not in selected:
            selected.append(value)
    provider_ids = payload.get("providerIds", current.get("providerIds", []))
    if not isinstance(provider_ids, list):
        raise _core.ManagerError("Web2API Provider 号池格式无效。")
    valid_providers = {
        str(item.get("id"))
        for item in settings.get("providers", [])
        if item.get("kind") == "custom"
        and item.get("id")
        and _core.provider_key_configured(str(item.get("id")))
        and item.get("models")
    }
    selected_providers = []
    for provider_id in provider_ids:
        value = str(provider_id)
        if value not in valid_providers:
            raise _core.ManagerError(f"Web2API Provider 不可用：{value}")
        if value not in selected_providers:
            selected_providers.append(value)
    default_sources = [
        *(f"account:{item}" for item in selected),
        *(f"provider:{item}" for item in selected_providers),
    ]
    valid_sources = set(default_sources)
    requested_order = payload.get("sourceOrder", current.get("sourceOrder", []))
    if not isinstance(requested_order, list):
        raise _core.ManagerError("Web2API 号池顺序格式无效。")
    source_order = list(
        dict.fromkeys(str(item) for item in requested_order if str(item) in valid_sources)
    )
    source_order.extend(item for item in default_sources if item not in source_order)
    current.update(
        {
            "bindHost": "127.0.0.1",
            "port": port,
            "routing": routing,
            "accountIds": selected,
            "providerIds": selected_providers,
            "sourceOrder": source_order,
        }
    )
    for account in settings.get("accounts", []):
        account["proxyEnabled"] = account.get("id") in selected
    for provider in settings.get("providers", []):
        if provider.get("kind") == "custom":
            provider["proxyEnabled"] = provider.get("id") in selected_providers
    _core.save_settings(settings)
    return current



def validate_web2api_codex_pool(
    settings: dict | None = None,
    preferred_account_id: str | None = None,
) -> dict:
    settings = settings or _core.load_settings()
    config = settings.get("web2api", _core._default_web2api_settings())
    preferred = str(preferred_account_id or "").strip() or None
    member_ids = {preferred} if preferred else {str(item) for item in config.get("accountIds", [])}
    usable_accounts = [
        item
        for item in settings.get("accounts", [])
        if item.get("id") in member_ids
        and item.get("authMode") == "chatgpt"
        and _core._account_codex_compatible(item)
        and not _core.account_invalid_reason(item)
        and item.get("models")
    ]
    usable_providers = [] if preferred else [
        item
        for item in settings.get("providers", [])
        if item.get("kind") == "custom"
        and item.get("id") in {str(value) for value in config.get("providerIds", [])}
        and _core.provider_key_configured(str(item.get("id")))
        and item.get("models")
    ]
    if not usable_accounts and not usable_providers:
        raise _core.ManagerError("API 号池没有可用于 Codex 的账号或 Provider；请先加入有效成员并刷新模型。")
    snapshots = []
    for account in usable_accounts:
        try:
            _core._decode_snapshot_files(_core._load_account_snapshot(str(account.get("id"))))
            snapshots.append(str(account.get("id")))
        except _core.ManagerError:
            continue
    if usable_accounts and not snapshots and not usable_providers:
        raise _core.ManagerError("API 号池账号缺少可用凭据快照；请重新导入账号。")
    models = _core.web2api_pool_model_records(settings, account_ids=member_ids)
    if not models:
        raise _core.ManagerError("API 号池没有可用于 Codex 的模型；请先刷新账号。")
    return {
        "accounts": snapshots,
        "providers": [str(item.get("id")) for item in usable_providers],
        "models": [str(item.get("id")) for item in models],
    }



def set_web2api_codex_active(active: bool, preferred_account_id: str | None = None) -> dict:
    settings = _core.load_settings()
    config = settings.setdefault("web2api", _core._default_web2api_settings())
    preferred = str(preferred_account_id or "").strip() or None
    if active:
        _core.validate_web2api_codex_pool(settings, preferred)
    config["activeForCodex"] = bool(active)
    config["activeAccountId"] = preferred if active else None
    _core.save_settings(settings)
    if not active:
        active_source = str(settings.get("modelWorkspace", {}).get("activeSourceId") or "")
        if active_source.startswith("account:"):
            try:
                _core.record_direct_account_activation(
                    active_source.split(":", 1)[1],
                    source="web2api_deactivated",
                )
            except (_core.ManagerError, OSError):
                pass
    return config

