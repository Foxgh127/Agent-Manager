"""Provider settings services."""
from __future__ import annotations
from agent_manager import core as _core


def _provider_runtime_base_url(provider: dict, label: str = "中转站") -> str:
    """Revalidate persisted/imported provider data before sending credentials."""
    base_url = _core._validated_provider_url(provider.get("baseUrl"), f"{label} Base URL")
    resolved = _core._validated_provider_related_url(
        provider.get("resolvedBaseUrl"),
        base_url,
        f"{label} 已探测 API Base URL",
        allow_empty=True,
    )
    return resolved or base_url



def _public_diagnostic_url(value: object) -> str:
    """Remove query and fragment data before an endpoint reaches UI or logs."""

    text = str(value or "").strip()
    try:
        parsed = _core.urllib.parse.urlsplit(text)
        return _core.urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except ValueError:
        return "远端接口"



def save_provider(
    payload: dict,
    original_id: str | None = None,
    *,
    api_key: str | None = None,
) -> dict:
    """Create or edit a provider and optionally persist its key atomically.

    Keeping the optional key in the same settings/secrets transaction prevents
    the UI from reporting a usable provider when DPAPI or the secret-store
    write failed after the settings record had already been committed.
    """

    if not isinstance(payload, dict):
        raise _core.ManagerError("Provider 保存请求必须是对象。")
    with _core.SETTINGS_LOCK, _core.SECRETS_LOCK, _core._settings_file_lock():
        return _core._save_provider_locked(payload, original_id, api_key=api_key)



def _save_provider_locked(
    payload: dict,
    original_id: str | None = None,
    *,
    api_key: str | None = None,
) -> dict:
    if not isinstance(payload, dict):
        raise _core.ManagerError("Provider 保存请求必须是对象。")
    settings = _core.load_settings()
    group_id = str(payload.get("groupId") or "relay")
    _core._account_group(settings, group_id)
    provider_id = _core.slugify(str(payload.get("id", "")), "Provider ID")
    if provider_id == "openai":
        raise _core.ManagerError("内置 OpenAI Provider 不能覆盖。")
    name = str(payload.get("name", "")).strip()
    base_url = _core._validated_provider_url(payload.get("baseUrl"), "Base URL")
    env_key = str(payload.get("envKey", "")).strip()
    if not name:
        raise _core.ManagerError("Provider 名称不能为空。")
    env_key = _core._validate_provider_env_key(env_key)
    original_id = original_id or provider_id
    existing = next((item for item in settings["providers"] if item.get("id") == original_id), None)
    conflicting_env = next(
        (
            item
            for item in settings.get("providers", [])
            if item.get("kind") == "custom"
            and str(item.get("id") or "") != original_id
            and str(item.get("envKey") or "").casefold() == env_key.casefold()
        ),
        None,
    )
    if conflicting_env:
        raise _core.ManagerError(
            f"环境变量 `{env_key}` 已由中转站 `{conflicting_env.get('name') or conflicting_env.get('id')}` 使用。"
        )
    raw_preset_id = (
        payload.get("presetId")
        if "presetId" in payload
        else (existing or {}).get("presetId")
    )
    # Schema 13 removed the public quick-fill catalog. Only an explicit id
    # already present in legacy/imported data may use these migration rules;
    # a new manual Provider is stored exactly as the user entered it.
    preset = _core._provider_portal_preset(raw_preset_id, allow_empty=True)
    raw_portal_url = (
        payload.get("portalUrl")
        if "portalUrl" in payload
        else (existing or {}).get("portalUrl")
    )
    preset_id = str(preset.get("id") or "") if preset else ""
    if (
        preset_id == "hajimi"
        and base_url.rstrip("/").casefold() in {"https://hajimi.chat", "https://hajimi.chat/v1"}
    ):
        base_url = _core._validated_provider_url(preset.get("baseUrl"), "Base URL")
    if not str(raw_portal_url or "").strip() and preset is not None:
        raw_portal_url = preset.get("dashboardUrl")
    portal_url = _core._validated_provider_portal_url(
        raw_portal_url,
        allow_empty=True,
        preset=preset,
    )
    integration_kind = str(
        payload.get("integrationKind")
        if "integrationKind" in payload
        else (existing or {}).get("integrationKind")
        or (preset or {}).get("integrationKind")
        or ""
    ).strip().casefold()
    if integration_kind not in {"", "new_api", "sub2api"}:
        raise _core.ManagerError("中转站集成类型无效。")
    raw_balance_endpoint = (
        payload.get("balanceEndpoint")
        if "balanceEndpoint" in payload
        else (existing or {}).get("balanceEndpoint")
    )
    if raw_balance_endpoint is None and existing is None and preset is not None:
        raw_balance_endpoint = preset.get("balanceEndpoint")
    balance_endpoint = _core._validated_provider_related_url(
        raw_balance_endpoint,
        base_url,
        "余额接口",
        allow_empty=True,
        allow_query=True,
    )
    raw_models_endpoint = (
        payload.get("modelsEndpoint")
        if "modelsEndpoint" in payload
        else (existing or {}).get("modelsEndpoint")
    )
    if raw_models_endpoint is None and existing is None and preset is not None:
        raw_models_endpoint = preset.get("modelsEndpoint")
    models_endpoint = _core._validated_provider_related_url(
        raw_models_endpoint,
        base_url,
        "模型目录接口",
        allow_empty=True,
        allow_query=True,
    )
    if provider_id != original_id and any(item.get("id") == provider_id for item in settings["providers"]):
        raise _core.ManagerError(f"Provider ID 已存在：{provider_id}")
    if provider_id != original_id:
        for agent in _core.discover_agents():
            if not agent.get("error") and agent["data"].get("model_provider") == original_id:
                raise _core.ManagerError("该 Provider 正被 Agent 使用，请保持 ID 不变或先修改 Agent。")
    base_url_changed = bool(existing and str(existing.get("baseUrl") or "") != base_url)
    resolved_base_url = str(payload.get("resolvedBaseUrl") or "").strip()
    if not resolved_base_url and existing and not base_url_changed:
        resolved_base_url = str(existing.get("resolvedBaseUrl") or "").strip()
    if resolved_base_url:
        resolved_base_url = _core._validated_provider_related_url(
            resolved_base_url,
            base_url,
            "已探测 API Base URL",
        )
    normalized_key = _core._validated_provider_secret(api_key, allow_empty=True)
    secrets_payload = (
        _core._secret_store()
        if normalized_key or provider_id != original_id
        else None
    )
    stored_key = ""
    if existing and normalized_key and isinstance(secrets_payload, dict):
        encoded = secrets_payload.get("providers", {}).get(original_id)
        if encoded:
            try:
                stored_key = _core.dpapi_unprotect(_core.base64.b64decode(encoded, validate=True))
            except Exception as exc:
                raise _core.ManagerError(
                    f"无法解密 Provider `{original_id}` 的现有 API Key。"
                ) from exc
    record_models = (
        [str(item) for item in payload.get("models", []) if str(item).strip()]
        if isinstance(payload.get("models"), list)
        else list(existing.get("models", []))
        if existing
        else []
    )
    raw_model_capabilities = (
        payload.get("modelCapabilities")
        if "modelCapabilities" in payload
        else (existing or {}).get("modelCapabilities")
        if not base_url_changed
        else {}
    )
    model_capabilities = _core._normalize_provider_model_capabilities(
        raw_model_capabilities,
        record_models,
    )
    record = {
        **(_core.json.loads(_core.json.dumps(existing)) if existing else {}),
        "id": provider_id,
        "name": name,
        "kind": "custom",
        "baseUrl": base_url,
        "resolvedBaseUrl": resolved_base_url,
        "presetId": preset_id,
        "portalUrl": portal_url,
        "integrationKind": integration_kind,
        "envKey": env_key,
        "modelsEndpoint": models_endpoint,
        "balanceEndpoint": balance_endpoint,
        "wireApi": "responses",
        "models": record_models,
        "modelCapabilities": model_capabilities,
        "modelReasoningOverrides": _core._normalize_provider_model_capabilities(
            payload.get("modelReasoningOverrides", (existing or {}).get("modelReasoningOverrides", {}) if not base_url_changed else {}),
            record_models,
        ),
        "modelCapabilitiesRefreshedAt": (
            _core.now_iso()
            if "modelCapabilities" in payload and model_capabilities
            else existing.get("modelCapabilitiesRefreshedAt")
            if existing and not base_url_changed
            else None
        ),
        "discoveredAt": existing.get("discoveredAt") if existing and not base_url_changed else None,
        "lastCheckedAt": existing.get("lastCheckedAt") if existing and not base_url_changed else None,
        "lastCheckStatus": existing.get("lastCheckStatus") if existing and not base_url_changed else "pending",
        "lastCheckError": existing.get("lastCheckError") if existing and not base_url_changed else None,
        "modelDiscoveryState": (
            existing.get("modelDiscoveryState")
            if existing and not base_url_changed
            else "pending"
        ),
        "modelDiscoveryError": (
            existing.get("modelDiscoveryError")
            if existing and not base_url_changed
            else None
        ),
        "balance": existing.get("balance") if existing else None,
        "balanceUpdatedAt": existing.get("balanceUpdatedAt") if existing else None,
        "balanceError": existing.get("balanceError") if existing else None,
        "sourceType": str(
            payload.get("sourceType")
            if "sourceType" in payload
            else (existing or {}).get("sourceType")
            or "manual_api"
        ).strip() or "manual_api",
        "relayAccountId": str(
            payload.get("relayAccountId")
            if "relayAccountId" in payload
            else (existing or {}).get("relayAccountId")
            or ""
        ).strip(),
        "relayKeyId": str(
            payload.get("relayKeyId")
            if "relayKeyId" in payload
            else (existing or {}).get("relayKeyId")
            or ""
        ).strip(),
        "relayGroupId": str(
            payload.get("relayGroupId")
            if "relayGroupId" in payload
            else (existing or {}).get("relayGroupId")
            or ""
        ).strip(),
        "relayGroupName": str(
            payload.get("relayGroupName")
            if "relayGroupName" in payload
            else (existing or {}).get("relayGroupName")
            or ""
        ).strip()[:120],
        "relayPlatform": str(
            payload.get("relayPlatform")
            if "relayPlatform" in payload
            else (existing or {}).get("relayPlatform")
            or ""
        ).strip()[:80],
        "relayRateMultiplier": (
            _core._number_value(payload.get("relayRateMultiplier"))
            if "relayRateMultiplier" in payload
            else (existing or {}).get("relayRateMultiplier")
        ),
        "relayEndpointId": str(
            payload.get("relayEndpointId")
            if "relayEndpointId" in payload
            else (existing or {}).get("relayEndpointId")
            or ""
        ).strip(),
        "relayEndpointName": str(
            payload.get("relayEndpointName")
            if "relayEndpointName" in payload
            else (existing or {}).get("relayEndpointName")
            or ""
        ).strip()[:120],
        "proxyEnabled": bool((existing or {}).get("proxyEnabled", False)),
        "source": "manager",
        "groupId": group_id if payload.get("groupId") else str((existing or {}).get("groupId") or "relay"),
        "updatedAt": _core.now_iso(),
    }
    for transient_field in (
        "_idWasExplicit",
        "key",
        "apiKey",
        "api_key",
        "OPENAI_API_KEY",
        "fetchModels",
        "activate",
        "originalId",
    ):
        record.pop(transient_field, None)
    previous_revision, previous_applied_revision = _core._provider_runtime_revisions(existing)
    runtime_changed = bool(
        existing is None
        or provider_id != original_id
        or _core._provider_runtime_signature(existing) != _core._provider_runtime_signature(record)
        or (normalized_key and normalized_key != stored_key)
    )
    record["runtimeRevision"] = (
        1
        if existing is None
        else previous_revision + (1 if runtime_changed else 0)
    )
    record["appliedRuntimeRevision"] = (
        0 if existing is None else previous_applied_revision
    )
    if existing:
        index = settings["providers"].index(existing)
        settings["providers"][index] = record
    else:
        settings["providers"].append(record)
    managed = set(settings.get("managedProviderIds", []))
    managed.discard(original_id)
    managed.add(provider_id)
    settings["managedProviderIds"] = sorted(managed)
    if provider_id != original_id:
        for profile in settings["mainProfiles"]:
            if profile.get("provider") == original_id:
                profile["provider"] = provider_id
        _core._rename_model_source_references(
            settings,
            f"provider:{original_id}",
            f"provider:{provider_id}",
        )
        web2api = settings.setdefault("web2api", _core._default_web2api_settings())
        web2api["providerIds"] = list(
            dict.fromkeys(
                provider_id if str(item) == original_id else str(item)
                for item in web2api.get("providerIds", [])
            )
        )
        web2api["sourceOrder"] = list(
            dict.fromkeys(
                f"provider:{provider_id}" if str(item) == f"provider:{original_id}" else str(item)
                for item in web2api.get("sourceOrder", [])
            )
        )
    settings_before = _core.SETTINGS_FILE.read_bytes() if _core.SETTINGS_FILE.is_file() else None
    secrets_before = _core.SECRETS_FILE.read_bytes() if _core.SECRETS_FILE.is_file() else None
    if provider_id != original_id:
        if secrets_payload is None:
            secrets_payload = _core._secret_store()
        encoded_key = secrets_payload["providers"].pop(original_id, None)
        if encoded_key:
            secrets_payload["providers"][provider_id] = encoded_key
    if normalized_key:
        if secrets_payload is None:
            secrets_payload = _core._secret_store()
        secrets_payload["providers"][provider_id] = _core.base64.b64encode(
            _core.dpapi_protect(normalized_key)
        ).decode("ascii")
    try:
        _core.save_settings(settings)
        if secrets_payload is not None:
            _core.atomic_write_json(_core.SECRETS_FILE, secrets_payload)
    except Exception:
        if settings_before is None:
            _core.SETTINGS_FILE.unlink(missing_ok=True)
        else:
            _core.atomic_write_bytes(_core.SETTINGS_FILE, settings_before)
        if secrets_before is None:
            _core.SECRETS_FILE.unlink(missing_ok=True)
        else:
            _core.atomic_write_bytes(_core.SECRETS_FILE, secrets_before)
        raise
    return record



def remove_provider(provider_id: str) -> None:
    with _core.SETTINGS_LOCK, _core.SECRETS_LOCK, _core._settings_file_lock():
        if provider_id == "openai":
            raise _core.ManagerError("内置 OpenAI Provider 不能删除。")
        settings = _core.load_settings()
        provider = _core.provider_by_id(provider_id, settings)
        source_id = f"provider:{provider_id}"
        active_source_id = str(settings.get("modelWorkspace", {}).get("activeSourceId") or "")
        configured_base_url = str(_core.read_toml(_core.CONFIG_FILE).get("openai_base_url") or "").rstrip("/")
        provider_base_urls = {
            str(provider.get("baseUrl") or "").rstrip("/"),
            str(provider.get("resolvedBaseUrl") or "").rstrip("/"),
        }
        provider_base_urls.discard("")
        if active_source_id == source_id or (
            configured_base_url and configured_base_url in provider_base_urls
        ):
            raise _core.ManagerError("该中转站当前正用于 Codex，请先切换到官方账号或其他中转站后再删除。")
        for record in _core.discover_agents():
            manager_owned = False
            if record["path"].is_file():
                try:
                    manager_owned = _core._looks_like_managed_agent(record["path"].read_bytes())
                except OSError:
                    manager_owned = False
            if (
                not record.get("error")
                and record["data"].get("model_provider") == provider_id
                and not manager_owned
            ):
                raise _core.ManagerError(f"该 Provider 正被 Agent `{record['data'].get('name')}` 使用。")
        snapshot = _core._capture_file_bytes((_core.SETTINGS_FILE, _core.SECRETS_FILE))
        settings["providers"] = [item for item in settings["providers"] if item.get("id") != provider_id]
        relay_account_ids = {
            str(item.get("id") or "")
            for item in settings.get("relayAccounts", [])
            if str(item.get("providerId") or "") == provider_id
        }
        settings["relayAccounts"] = [
            item
            for item in settings.get("relayAccounts", [])
            if str(item.get("providerId") or "") != provider_id
        ]
        removed_profiles = {
            item.get("id")
            for item in settings.get("mainProfiles", [])
            if item.get("provider") == provider_id
        }
        settings["mainProfiles"] = [
            item for item in settings.get("mainProfiles", []) if item.get("provider") != provider_id
        ]
        if settings.get("activeMainProfileId") in removed_profiles:
            fallback = next(
                (item for item in settings.get("mainProfiles", []) if item.get("provider") == "openai"),
                None,
            )
            settings["activeMainProfileId"] = fallback.get("id") if fallback else ""
        web2api = settings.setdefault("web2api", _core._default_web2api_settings())
        web2api["providerIds"] = [
            item for item in web2api.get("providerIds", []) if item != provider_id
        ]
        web2api["sourceOrder"] = [
            item for item in web2api.get("sourceOrder", []) if item != source_id
        ]
        _core._remove_model_source_references(settings, {source_id})
        secrets_payload = _core._secret_store()
        secrets_payload["providers"].pop(provider_id, None)
        for relay_account_id in relay_account_ids:
            secrets_payload.setdefault("relayAccounts", {}).pop(relay_account_id, None)
        try:
            _core.save_settings(settings)
            _core.atomic_write_json(_core.SECRETS_FILE, secrets_payload)
        except Exception as exc:
            rollback_errors = _core._restore_file_bytes(snapshot)
            if rollback_errors:
                raise _core.ManagerError(
                    f"删除 Provider 失败：{exc}；回滚也未完成：{'；'.join(rollback_errors)}"
                ) from exc
            raise



def remove_relay_account(account_id: str) -> None:
    account_id = _core.slugify(account_id, "中转站账号 ID")
    settings = _core.load_settings()
    account = next(
        (item for item in settings.get("relayAccounts", []) if str(item.get("id")) == account_id),
        None,
    )
    if not account:
        raise _core.ManagerError("中转站账号不存在。")
    provider_id = str(account.get("providerId") or "").strip()
    if provider_id and any(str(item.get("id") or "") == provider_id for item in settings.get("providers", [])):
        _core.remove_provider(provider_id)
        return
    with _core.SETTINGS_LOCK, _core.SECRETS_LOCK, _core._settings_file_lock():
        snapshot = _core._capture_file_bytes((_core.SETTINGS_FILE, _core.SECRETS_FILE))
        try:
            latest = _core.load_settings()
            latest["relayAccounts"] = [
                item
                for item in latest.get("relayAccounts", [])
                if str(item.get("id") or "") != account_id
            ]
            secrets_payload = _core._secret_store()
            secrets_payload.setdefault("relayAccounts", {}).pop(account_id, None)
            _core.save_settings(latest)
            _core.atomic_write_json(_core.SECRETS_FILE, secrets_payload)
        except Exception:
            _core._restore_file_bytes(snapshot)
            raise



def remove_model_sources_batch(account_ids: list[str], provider_ids: list[str]) -> dict:
    if not isinstance(account_ids, list) or not isinstance(provider_ids, list):
        raise _core.ManagerError("批量删除参数必须是数组。")
    accounts = list(dict.fromkeys(str(item) for item in account_ids if str(item).strip()))
    providers = list(dict.fromkeys(str(item) for item in provider_ids if str(item).strip()))
    if not accounts and not providers:
        raise _core.ManagerError("请至少选择一个账号。")
    if len(accounts) + len(providers) > 100:
        raise _core.ManagerError("单次最多删除 100 个账号。")
    deleted_accounts = []
    deleted_providers = []
    failed = []
    for account_id in accounts:
        try:
            _core.remove_codex_account(account_id)
            deleted_accounts.append(account_id)
        except Exception as exc:
            failed.append(
                {
                    "kind": "account",
                    "id": account_id,
                    "error": _core._redact_sensitive_text(exc, limit=320),
                }
            )
    for provider_id in providers:
        try:
            _core.remove_provider(provider_id)
            deleted_providers.append(provider_id)
        except Exception as exc:
            failed.append(
                {
                    "kind": "provider",
                    "id": provider_id,
                    "error": _core._redact_sensitive_text(exc, limit=320),
                }
            )
    return {
        "deleted": len(deleted_accounts) + len(deleted_providers),
        "deletedAccounts": deleted_accounts,
        "deletedProviders": deleted_providers,
        "failed": failed,
    }



def export_api_provider(provider_id: str) -> dict:
    settings = _core.load_settings()
    provider = _core.provider_by_id(provider_id, settings)
    if provider.get("kind") != "custom":
        raise _core.ManagerError("内置 Provider 不支持导出。")
    return {
        "format": "codex-agent-manager-api-account",
        "version": 1,
        "exportedAt": _core.now_iso(),
        "containsSecrets": True,
        "provider": {
            "id": provider["id"],
            "name": provider["name"],
            "baseUrl": provider["baseUrl"],
            "resolvedBaseUrl": provider.get("resolvedBaseUrl") or "",
            "presetId": provider.get("presetId") or "",
            "portalUrl": provider.get("portalUrl") or "",
            "integrationKind": provider.get("integrationKind") or "",
            "envKey": provider["envKey"],
            "groupId": provider.get("groupId") or "relay",
            "modelsEndpoint": provider.get("modelsEndpoint") or "",
            "balanceEndpoint": provider.get("balanceEndpoint") or "",
            "models": list(provider.get("models", [])),
            "proxyEnabled": bool(provider.get("proxyEnabled")),
            "key": _core.load_provider_key(provider_id, required=True),
        },
    }



def export_relay_account(account_id: str) -> dict:
    """Export one relay login as an explicit, portable account-level package.

    Unlike the implicit support/configuration bundle, this user-triggered
    export intentionally contains plaintext API keys and the renewable website
    session.  Import re-encrypts both with DPAPI for the destination Windows
    user, retaining the relay identity, per-Key groups and refresh capability.
    """

    account_id = _core.slugify(account_id, "中转站账号 ID")
    settings = _core.load_settings()
    account = next(
        (
            item
            for item in settings.get("relayAccounts", [])
            if str(item.get("id") or "") == account_id
        ),
        None,
    )
    if not account:
        raise _core.ManagerError("中转站账号不存在。")
    key_secrets: dict[str, str] = {}
    for key in account.get("keys", []):
        if not isinstance(key, dict) or not str(key.get("id") or ""):
            continue
        key_id = str(key.get("id") or "")
        secret = _core.load_relay_account_key(account_id, key_id, required=False)
        if secret:
            key_secrets[key_id] = secret
    if not key_secrets:
        raise _core.ManagerError("该中转站账号没有可导出的 Codex API Key。")
    dashboard_session = _core.load_relay_account_dashboard_session(account_id, required=False)
    preview = {
        key: _core.json.loads(_core.json.dumps(account.get(key)))
        for key in (
            "adapter",
            "adapterLabel",
            "siteName",
            "portalUrl",
            "origin",
            "integrationKind",
            "user",
            "balance",
            "models",
            "groups",
            "keys",
            "apiEndpoints",
            "supportsCreate",
            "remoteKeyCount",
            "remoteGroupCount",
            "detectedAt",
        )
    }
    preview["keysAuthoritative"] = True
    preview["groupsAuthoritative"] = True
    provider_id = str(account.get("providerId") or "")
    group = next(
        (
            item
            for item in settings.get("accountGroups", [])
            if str(item.get("id") or "") == str(account.get("groupId") or "")
        ),
        {},
    )
    web2api = settings.get("web2api") if isinstance(settings.get("web2api"), dict) else {}
    return {
        "format": "codex-agent-manager-relay-account",
        "version": 1,
        "exportedAt": _core.now_iso(),
        "containsSecrets": True,
        "containsDashboardSession": bool(dashboard_session),
        "label": account.get("siteName") or "中转站账号",
        "group": {"id": account.get("groupId"), "name": group.get("name")},
        "relayAccount": {
            "id": account_id,
            "groupId": account.get("groupId") or "relay",
            "selectedKeyId": account.get("selectedKeyId") or "",
            "selectedEndpointId": account.get("selectedEndpointId") or "",
            "proxyEnabled": provider_id in set(web2api.get("providerIds", [])),
            "preview": preview,
            "keySecrets": key_secrets,
            "dashboardSession": dashboard_session,
        },
    }



def export_codex_account_to_downloads(account_id: str) -> dict:
    settings = _core.load_settings()
    account = next((item for item in settings.get("accounts", []) if item.get("id") == account_id), None)
    if not account:
        raise _core.ManagerError("账号不存在。")
    label = str(account.get("label") or account.get("email") or account_id)
    return _core.save_json_export_to_downloads(_core.export_codex_account(account_id), label)



def export_api_provider_to_downloads(provider_id: str) -> dict:
    provider = _core.provider_by_id(provider_id)
    label = str(provider.get("name") or provider_id)
    return _core.save_json_export_to_downloads(_core.export_api_provider(provider_id), label)



def export_relay_account_to_downloads(account_id: str) -> dict:
    settings = _core.load_settings()
    account = next(
        (
            item
            for item in settings.get("relayAccounts", [])
            if str(item.get("id") or "") == str(account_id)
        ),
        None,
    )
    if not account:
        raise _core.ManagerError("中转站账号不存在。")
    label = str(account.get("siteName") or account_id)
    return _core.save_json_export_to_downloads(_core.export_relay_account(account_id), label)



def _select_workspace_source(settings: dict, source: dict, *, independent: bool = False) -> dict:
    workspace = settings.setdefault("modelWorkspace", _core._default_model_workspace())
    source_id = str(source.get("id") or "")
    model_keys = [str(item.get("key") or "") for item in source.get("models", []) if item.get("key")]
    previous_default = str(workspace.get("defaultModelKey") or "")
    if independent:
        workspace["mode"] = "independent"
    workspace["activeSourceId"] = source_id
    workspace["defaultModelKey"] = (
        previous_default if previous_default in model_keys else model_keys[0] if model_keys else ""
    )
    # A previous account's explicit selection must not filter every model out
    # of the newly selected source. Keep past preferences, but seed all models
    # from this source when selectAll is disabled.
    if not bool(workspace.get("selectAll", True)):
        selected = [str(item) for item in workspace.get("selectedModels", []) if str(item).strip()]
        selected.extend(item for item in model_keys if item not in selected)
        workspace["selectedModels"] = list(dict.fromkeys(selected))
    return workspace



def select_model_source(source_id: str) -> dict:
    settings = _core.load_settings()
    source = next((item for item in _core.model_sources(settings) if item.get("id") == source_id), None)
    if not source:
        raise _core.ManagerError("账号或模型服务不存在。")
    if not source.get("available") or not source.get("models"):
        raise _core.ManagerError("该账号或模型服务没有可用模型，请先刷新后重试。")
    if source["kind"] == "account":
        result = _core.switch_codex_account(source["recordId"])
        latest = _core.load_settings()
        latest_source = next((item for item in _core.model_sources(latest) if item.get("id") == source_id), source)
        _core._select_workspace_source(latest, latest_source)
        _core.save_settings(latest)
        return {"source": source_id, "switch": result, "workspace": latest["modelWorkspace"]}
    workspace = _core._select_workspace_source(settings, source)
    _core.save_settings(settings)
    return {"source": source_id, "changed": True, "workspace": workspace}



def _provider_target_is_active(settings: dict, provider: dict, source: dict, key: str) -> bool:
    workspace = settings.get("modelWorkspace", {})
    web2api = settings.get("web2api", {})
    config = _core.read_toml(_core.CONFIG_FILE)
    model_ids = {str(item.get("id") or "") for item in source.get("models", [])}
    model_keys = {str(item.get("key") or "") for item in source.get("models", [])}
    selected = {str(item) for item in workspace.get("selectedModels", [])}
    env_key = _core._validate_provider_env_key(str(provider.get("envKey") or ""))
    shared_gateway = _core._subagents_require_shared_gateway(settings)
    active_environment: list[str] = [] if shared_gateway else [env_key]
    if _core._managed_subagent_specs(settings):
        active_environment.append(_core.AGGREGATE_ENV_KEY)
    expected_base_url = _core._provider_runtime_base_url(provider)
    providers = config.get("model_providers") if isinstance(config.get("model_providers"), dict) else {}
    active_provider = providers.get(str(provider.get("id") or ""))
    active_provider = active_provider if isinstance(active_provider, dict) else {}
    return bool(
        workspace.get("mode") == "independent"
        and not _core._provider_runtime_requires_reapply(settings, provider)
        and workspace.get("activeSourceId") == source.get("id")
        and (bool(workspace.get("selectAll", True)) or model_keys.issubset(selected))
        and not web2api.get("activeForCodex")
        and _core._switch_runtime_model_matches(settings, config, source, str(provider.get("id") or ""))
        and not str(config.get("openai_base_url") or "")
        and str(active_provider.get("base_url") or "") == expected_base_url
        and str(active_provider.get("env_key") or "") == env_key
        and (shared_gateway or _core._read_user_environment(env_key) == key)
        and not _core._inactive_provider_environment_overrides(settings, active_environment)
    )



def switch_api_provider_and_launch(
    provider_id: str,
    progress_callback: _core.Callable[[dict[str, _core.Any]], None] | None = None,
    close_processes_callback: _core.Callable[..., dict] | None = None,
    *,
    ensure_gateway: _core.Callable | None = None,
    force_reapply: bool = False,
) -> dict:
    """Atomically apply one custom Provider with at most one relaunch."""
    close_processes_callback = close_processes_callback or _core.close_codex_processes
    reporter = _core._SwitchProgressReporter(progress_callback)
    reporter.emit("preflight", 4, "正在检查中转站、模型与 Codex 启动器")
    with _core._exclusive_switch_operation("provider-launch", provider_id):
        settings_before = _core.load_settings()
        provider = _core.provider_by_id(provider_id, settings_before)
        if provider.get("kind") != "custom":
            raise _core.ManagerError("只有已导入的中转站 API 可以直接切换。")
        _core._provider_runtime_base_url(provider)
        key = _core.load_provider_key(provider_id, required=True) or ""
        provider_env_key = _core._validate_provider_env_key(str(provider.get("envKey") or ""))
        models = [str(item).strip() for item in provider.get("models", []) if str(item).strip()]
        if not models:
            raise _core.ManagerError("该中转站没有可用模型，请先刷新模型目录。")
        # A relay is allowed to hide its model catalog endpoint.  Once the
        # user has supplied (or imported) a model ID, the catalog probe is
        # metadata only and must never be a prerequisite for switching.  The
        # runtime endpoint is validated above and the model list is validated
        # by ``model_sources`` below.  This also prevents a harmless 403 from
        # turning a usable relay into a failed account switch.
        source_id = f"provider:{provider_id}"
        source = next((item for item in _core.model_sources(settings_before) if item.get("id") == source_id), None)
        if not source or not source.get("available") or not source.get("models"):
            raise _core.ManagerError("该中转站的 Key 或模型目录不可用，请编辑后重试。")
        reporter.emit("credentials", 16, "API Key 与模型目录已通过本地预检")
        launch_plan = _core.resolve_codex_launch_plan()
        launch_plan = {**launch_plan, "apiProviderId": provider_id}
        reporter.emit("prepared", 22, "预检通过，正在准备安全切换")
        if not force_reapply and _core._provider_target_is_active(settings_before, provider, source, key):
            _core._ensure_switch_gateway(str(_core.read_toml(_core.CONFIG_FILE).get("model_provider") or "") == _core.AGGREGATE_PROVIDER_ID, ensure_gateway)
            applied_model = str(_core.read_toml(_core.CONFIG_FILE).get("model") or "")
            launch = None
            if not _core.running_codex_processes():
                snapshot = _core._capture_switch_transaction_snapshot(settings_before)
                session_visibility = None
                launch_attempted = False
                try:
                    session_visibility = _core._repair_switch_session_visibility()
                    reporter.emit("launching", 72, "中转站已生效，正在启动 Codex")
                    launch_attempted = True
                    launch = _core.launch_codex_app(
                        launch_plan=launch_plan,
                        env_overrides={provider_env_key: key},
                    )
                    reporter.emit("verifying", 88, "Codex 已出现，正在核对 API 模型")
                    launch["readiness"] = _core.wait_for_codex_runtime_ready(expected_model=applied_model, launch_plan=launch_plan)
                    launch["retryCount"] = 0
                except Exception as exc:
                    failed_phase = reporter.phase
                    reporter.emit("recovering", 96, "启动验证失败，正在恢复切换前状态")
                    detail = _core._rollback_failed_switch(
                        snapshot,
                        launch_plan,
                        closed=None,
                        launch_attempted=launch_attempted,
                        close_processes_callback=close_processes_callback,
                        state_mutated=False,
                        session_visibility=session_visibility,
                    )
                    reporter.failed(
                        exc,
                        failed_phase=failed_phase,
                        message="启动未完成，原账号与配置未改变",
                        recovery_state="unchanged",
                    )
                    raise _core.ManagerError(f"中转站启动失败：{exc}{detail}") from exc
            reporter.completed(
                "该中转站已经生效，Codex 正在运行" if launch is None else "Codex 已完成 API 模型回验"
            )
            return {
                "source": source_id,
                "provider": provider_id,
                "model": applied_model,
                "workspace": settings_before.get("modelWorkspace", {}),
                "applied": {"changed": False, "restartRequired": False},
                "closed": None,
                "launch": launch,
                "sessionSync": None,
                "verified": True,
                "changed": False,
                "performance": reporter.summary(),
            }
        snapshot = _core._capture_switch_transaction_snapshot(settings_before)
        closed = None
        launch_attempted = False
        state_mutated = False
        session_visibility = None
        try:
            reporter.emit("closing", 30, "正在安全关闭旧 Codex；Agent Manager 会继续运行")
            closed = close_processes_callback()
            reporter.emit("writing", 48, "正在写入 API 身份与目标服务地址")
            state_mutated = True
            latest = _core.load_settings()
            latest_source = next(
                (item for item in _core.model_sources(latest) if item.get("id") == source_id),
                None,
            )
            if not latest_source or not latest_source.get("available") or not latest_source.get("models"):
                raise _core.ManagerError("中转站在应用前已变为不可用，已停止切换。")
            workspace = _core._select_workspace_source(latest, latest_source, independent=True)
            profile = _core._active_main(latest)
            profile["provider"] = provider_id
            selected_key = str(workspace.get("defaultModelKey") or "")
            selected_model = next(
                (item for item in latest_source.get("models", []) if str(item.get("key") or "") == selected_key),
                latest_source["models"][0],
            )
            profile["model"] = str(selected_model.get("id") or profile.get("model") or "")
            web2api = latest.setdefault("web2api", _core._default_web2api_settings())
            web2api["activeForCodex"] = False
            web2api["activeAccountId"] = None
            _core.save_settings(latest)
            reporter.emit("configuring", 62, "正在应用模型配置并清理旧 Provider 覆盖")
            applied = _core.apply_configuration(False)
            applied_config = _core.read_toml(_core.CONFIG_FILE)
            if not _core._switch_runtime_model_matches(latest, applied_config, latest_source, provider_id):
                raise _core.ManagerError("写入回验失败：Codex 路由没有指向目标中转站和模型。")
            if str(applied_config.get("openai_base_url") or ""):
                raise _core.ManagerError("写入回验失败：仍残留会污染官方登录的旧式 API 地址覆盖。")
            expected_base_url = _core._provider_runtime_base_url(provider)
            applied_providers = applied_config.get("model_providers")
            applied_providers = applied_providers if isinstance(applied_providers, dict) else {}
            applied_provider = applied_providers.get(provider_id)
            applied_provider = applied_provider if isinstance(applied_provider, dict) else {}
            if str(applied_provider.get("base_url") or "") != expected_base_url:
                raise _core.ManagerError("写入回验失败：目标中转站 Provider 地址不一致。")
            if str(applied_provider.get("env_key") or "") != provider_env_key:
                raise _core.ManagerError("写入回验失败：目标中转站 Provider 凭据变量不一致。")
            expected_models = {
                str(item.get("id") or "")
                for item in latest_source.get("models", [])
                if item.get("id")
            }
            applied_model = str(applied_config.get("model") or "")
            if not _core._subagents_require_shared_gateway(latest) and _core._read_user_environment(provider_env_key) != key:
                raise _core.ManagerError("写入回验失败：中转站 API Key 没有同步到独立 Provider。")
            for name in _core.AUTH_FILES:
                auth_path = _core.CODEX_HOME / name
                expected_auth = snapshot.get("files", {}).get(auth_path)
                actual_auth = auth_path.read_bytes() if auth_path.is_file() else None
                if actual_auth != expected_auth:
                    raise _core.ManagerError("写入回验失败：第三方切换意外改动了官方登录缓存。")
            if _core._inactive_provider_environment_overrides(latest, applied.get("syncedEnvKeys", [])):
                raise _core.ManagerError("写入回验失败：旧中转站环境覆盖仍处于活动状态。")
            session_sync = _core.auto_sync_sessions_after_switch(provider_id)
            _core._ensure_switch_gateway(bool(applied.get("gatewayRequired")), ensure_gateway)
            session_visibility = _core._repair_switch_session_visibility()
            session_sync["visibility"] = session_visibility
            launch_attempted = True
            reporter.emit("launching", 76, "配置已写入，正在启动 Codex")
            launch = _core.launch_codex_app(
                launch_plan=launch_plan,
                env_overrides={provider_env_key: key},
            )
            reporter.emit("verifying", 90, "Codex 已出现，正在核对 API 模型与服务地址")
            readiness = _core.wait_for_codex_runtime_ready(expected_model=applied_model, launch_plan=launch_plan)
            launch["readiness"] = readiness
            launch["retryCount"] = 0
            reporter.completed("Codex 已完成中转站模型回验")
            return {
                "source": source_id,
                "provider": provider_id,
                "model": applied_model,
                "workspace": workspace,
                "applied": applied,
                "closed": closed,
                "launch": launch,
                "sessionSync": session_sync,
                "verified": True,
                "changed": True,
                "performance": reporter.summary(),
            }
        except Exception as exc:
            failed_phase = reporter.phase
            reporter.emit("recovering", 96, "切换未通过验证，正在恢复原账号与配置")
            detail = _core._rollback_failed_switch(
                snapshot,
                launch_plan,
                closed=closed,
                launch_attempted=launch_attempted,
                close_processes_callback=close_processes_callback,
                state_mutated=state_mutated,
                session_visibility=session_visibility,
            )
            reporter.failed(
                exc,
                failed_phase=failed_phase,
                message=(
                    "切换未完成，原账号与配置已安全恢复"
                    if state_mutated
                    else "切换未完成，原账号与配置未改变"
                ),
                recovery_state="restored" if state_mutated else "unchanged",
            )
            raise _core.ManagerError(f"中转站切换失败：{exc}{detail}") from exc



def _normalize_model_workspace_update(settings: dict, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise _core.ManagerError("主模型设置格式无效。")
    workspace = _core._json_clone(settings.get("modelWorkspace", _core._default_model_workspace()))
    mode = str(payload.get("mode") or workspace.get("mode") or "independent")
    if mode not in {"independent", "aggregate"}:
        raise _core.ManagerError("主模型模式无效。")
    source_ids = {item["id"] for item in _core.model_sources(settings)}
    active_source = str(payload.get("activeSourceId", workspace.get("activeSourceId") or ""))
    if active_source and active_source not in source_ids:
        raise _core.ManagerError("当前选择的账号或服务已经不存在。")
    known_keys = {item["key"] for item in _core._all_model_records(settings)}
    raw_selected = payload.get("selectedModels", workspace.get("selectedModels", []))
    if not isinstance(raw_selected, list):
        raise _core.ManagerError("主模型选择格式无效。")
    selected = list(dict.fromkeys(str(item) for item in raw_selected if str(item) in known_keys))
    select_all = bool(payload.get("selectAll", False))
    default_key = str(payload.get("defaultModelKey", workspace.get("defaultModelKey") or ""))
    effective_keys = known_keys if select_all else set(selected)
    if default_key and default_key not in effective_keys:
        default_key = ""
    return {
        **workspace,
        "mode": mode,
        "activeSourceId": active_source,
        "selectAll": select_all,
        "selectedModels": selected,
        "defaultModelKey": default_key,
        "syncToCodex": bool(payload.get("syncToCodex", True)),
    }



def save_model_workspace(payload: dict) -> dict:
    with _core.SWITCH_OPERATION_LOCK, _core.SETTINGS_LOCK, _core._settings_file_lock():
        settings = _core.load_settings()
        workspace = _core._normalize_model_workspace_update(settings, payload)
        settings["modelWorkspace"] = workspace
        _core.save_settings(settings)
        return workspace



def _normalize_runtime_tuning_update(settings: dict, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise _core.ManagerError("Codex 运行参数格式无效。")
    current = _core._normalize_runtime_tuning(settings.get("runtimeTuning"))
    config_fields = set(_core.MANAGED_RUNTIME_TUNING_FIELDS)
    candidate = {**current, **payload}
    managed_fields = list(current.get("managedFields") or [])
    for key in _core.MANAGED_RUNTIME_TUNING_FIELDS:
        if key in payload and key not in managed_fields:
            managed_fields.append(key)
    release = payload.get("releaseManagedFields")
    if release is True:
        managed_fields = []
    elif isinstance(release, list):
        released = {str(item) for item in release}
        if not released.issubset(config_fields):
            raise _core.ManagerError("要释放的 Codex 常用配置字段无效。")
        managed_fields = [key for key in managed_fields if key not in released]
    candidate["managedFields"] = managed_fields
    candidate["configManaged"] = bool(managed_fields)
    if config_fields.intersection(payload):
        candidate["enabled"] = True
    if "planningMode" in payload:
        candidate["planningManaged"] = True
    tuning = _core._normalize_runtime_tuning(candidate, strict=True)
    if (
        "mcpOptionalStartupGraceMs" in payload
        and int(tuning.get("mcpOptionalStartupGraceMs", -1)) >= 0
        and not _core._codex_supports_mcp_optional_startup_grace()
    ):
        raise _core.ManagerError(
            "自定义可选 MCP 启动等待需要 Codex CLI 0.151.0 或更新版本；请先在设置中更新 Codex。"
        )
    return tuning



def save_runtime_tuning(payload: dict) -> dict:
    with _core.SWITCH_OPERATION_LOCK, _core.SETTINGS_LOCK, _core._settings_file_lock():
        settings = _core.load_settings()
        tuning = _core._normalize_runtime_tuning_update(settings, payload)
        settings["runtimeTuning"] = tuning
        _core.save_settings(settings)
        return tuning

