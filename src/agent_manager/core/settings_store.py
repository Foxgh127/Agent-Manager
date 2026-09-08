"""Settings store services."""
from __future__ import annotations
from agent_manager import core as _core


def _initial_providers(config: dict) -> list[dict]:
    providers = [
        {
            "id": "openai",
            "name": "OpenAI / Codex 登录",
            "kind": "builtin",
            "baseUrl": "",
            "presetId": "",
            "portalUrl": "",
            "integrationKind": "",
            "modelsEndpoint": "",
            "envKey": "OPENAI_API_KEY",
            "wireApi": "responses",
            "models": [],
            "discoveredAt": None,
            "lastCheckedAt": None,
            "lastCheckStatus": "pending",
            "lastCheckError": None,
            "modelDiscoveryState": "pending",
            "modelDiscoveryError": None,
            "source": "builtin",
        }
    ]
    for provider_id, value in config.get("model_providers", {}).items():
        if not isinstance(value, dict) or provider_id in {"openai", _core.AGGREGATE_PROVIDER_ID}:
            continue
        providers.append(
            {
                "id": provider_id,
                "name": str(value.get("name") or provider_id),
                "kind": "custom",
                "baseUrl": str(value.get("base_url") or ""),
                "presetId": "",
                "portalUrl": "",
                "integrationKind": "",
                "modelsEndpoint": str(value.get("models_endpoint") or value.get("models_url") or ""),
                "envKey": _core._safe_imported_provider_env_key(
                    str(value.get("env_key") or ""),
                    f"{provider_id.upper()}_API_KEY",
                ),
                "wireApi": str(value.get("wire_api") or "responses"),
                "models": [],
                "discoveredAt": None,
                "lastCheckedAt": None,
                "lastCheckStatus": "pending",
                "lastCheckError": None,
                "modelDiscoveryState": "pending",
                "modelDiscoveryError": None,
                "source": "codex",
            }
        )
    if not any(item["id"] == "u_gateway" for item in providers):
        legacy = _core.read_toml(_core.LEGACY_PROFILE_FILE).get("model_providers", {}).get("u_gateway", {})
        if legacy or _core.LEGACY_PROFILE_FILE.exists():
            providers.append(
                {
                    "id": "u_gateway",
                    "name": str(legacy.get("name") or "U Gateway"),
                    "kind": "custom",
                    "baseUrl": str(legacy.get("base_url") or "https://api.u-gatewayapi.asia"),
                    "presetId": "",
                    "portalUrl": "",
                    "integrationKind": "",
                    "modelsEndpoint": str(legacy.get("models_endpoint") or legacy.get("models_url") or ""),
                    "envKey": _core._safe_imported_provider_env_key(
                        str(legacy.get("env_key") or ""),
                        "U_GATEWAY_API_KEY",
                    ),
                    "wireApi": str(legacy.get("wire_api") or "responses"),
                    "models": [],
                    "discoveredAt": None,
                    "lastCheckedAt": None,
                    "lastCheckStatus": "pending",
                    "lastCheckError": None,
                    "modelDiscoveryState": "pending",
                    "modelDiscoveryError": None,
                    "source": "imported",
                }
            )
    legacy_settings = _core.read_json(_core.LEGACY_STATE_DIR / "settings.json", {})
    if isinstance(legacy_settings, dict):
        cached = legacy_settings.get("gateway_models")
        provider = next((item for item in providers if item["id"] == "u_gateway"), None)
        if provider and isinstance(cached, list):
            provider["models"] = [str(item) for item in cached]
    return providers



def _initial_settings() -> dict:
    config = _core.read_toml(_core.CONFIG_FILE)
    providers = _core._initial_providers(config)
    current_provider = str(config.get("model_provider") or "openai")
    if not any(item["id"] == current_provider for item in providers):
        current_provider = "openai"
    current_model = str(config.get("model") or "")
    current_effort = str(config.get("model_reasoning_effort") or "").strip()
    if current_effort not in _core.VALID_EFFORTS:
        current_effort = ""
    agents = [item for item in _core.discover_agents() if not item.get("error")]
    luna_exists = any(item["data"].get("name") == "luna_worker" for item in agents)
    default_agents = ["luna_worker"] if luna_exists else []
    return {
        "schemaVersion": _core.SCHEMA_VERSION,
        "providers": providers,
        "managedProviderIds": [item["id"] for item in providers if item.get("source") == "imported"],
        "mainProfiles": [
            {
                "id": "current",
                "name": "当前主模型",
                "provider": current_provider,
                "model": current_model,
                "effort": current_effort,
            }
        ],
        "activeMainProfileId": "current",
        "strategies": _core.json.loads(_core.json.dumps(_core.DEFAULT_STRATEGIES)),
        "activeStrategyId": "adaptive",
        "routes": {
            level: {
                "enabled": bool(default_agents),
                "agents": list(default_agents),
                "description": _core.DIFFICULTY_META[level]["description"],
            }
            for level in _core.DIFFICULTIES
        },
        "modelWorkspace": _core._default_model_workspace(),
        "subagentRouting": _core._default_subagent_routing(),
        # Tracks only the Codex multi-agent mode hint written by this Manager.
        # It lets native mode restore a pre-existing user hint without claiming
        # ownership of the rest of [features.multi_agent_v2].
        "managedSubagentPolicy": None,
        "runtimeTuning": _core._default_runtime_tuning(),
        "appBehavior": _core._default_app_behavior(),
        "accounts": [],
        # Login-based relay accounts are persisted separately from ordinary
        # hand-entered API Providers.  Only dashboard metadata and encrypted
        # API-key references live here; WebView cookies/tokens never do.
        "relayAccounts": [],
        "accountGroups": _core.json.loads(_core.json.dumps(_core.DEFAULT_ACCOUNT_GROUPS)),
        "web2api": _core._default_web2api_settings(),
        "sessionSync": _core._default_session_sync_settings(),
        "historySync": {
            "target": "",
            "recentDays": 30,
            "direction": "two_way",
            "lastSyncedAt": None,
            "lastSummary": None,
        },
        "lastAppliedAt": None,
        "lastAppliedSummary": None,
    }



def _migrate_legacy_secret() -> None:
    if _core.SECRETS_FILE.exists():
        return
    legacy_path = _core.LEGACY_STATE_DIR / "u-gateway-key.json"
    legacy = _core.read_json(legacy_path, None)
    if isinstance(legacy, dict) and legacy.get("scheme") == "windows-dpapi-current-user-v1" and legacy.get("ciphertext"):
        _core.atomic_write_json(
            _core.SECRETS_FILE,
            {"scheme": "windows-dpapi-current-user-v1", "providers": {"u_gateway": legacy["ciphertext"]}},
        )



def ensure_state() -> None:
    _core.STATE_DIR.mkdir(parents=True, exist_ok=True)
    _core.AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    _core.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    _core._migrate_legacy_secret()
    if not _core.SETTINGS_FILE.exists():
        _core.atomic_write_json(_core.SETTINGS_FILE, _core._initial_settings())



def _migrate_settings(settings: dict) -> tuple[dict, bool]:
    raw_version = settings.get("schemaVersion")
    try:
        version = int(raw_version)
    except (TypeError, ValueError):
        version = 0
    # Schema 1 predates the first public migration test, but its top-level
    # document is still structurally compatible with the defaults below.  A
    # missing version is accepted only for an empty or recognisably legacy
    # document; unknown future versions continue to fail closed instead of
    # silently discarding fields the current build does not understand.
    if version == 0 and (
        not settings
        or any(
            key in settings
            for key in (
                "providers",
                "mainProfiles",
                "accounts",
                "accountGroups",
                "modelWorkspace",
                "routes",
            )
        )
    ):
        version = 1
    # Every persisted version from the first supported schema through the
    # current schema must remain loadable.  Normalising numeric strings also
    # keeps hand-edited/exported settings compatible across older builds.
    if version not in set(range(1, _core.SCHEMA_VERSION + 1)):
        raise _core.ManagerError("设置文件版本不兼容。")

    normalized = _core.json.loads(_core.json.dumps(settings))
    normalized.setdefault("accounts", [])
    relay_accounts = normalized.setdefault("relayAccounts", [])
    if not isinstance(relay_accounts, list):
        relay_accounts = []
        normalized["relayAccounts"] = relay_accounts
    history_sync = normalized.setdefault(
        "historySync",
        {"target": "", "recentDays": 30, "direction": "two_way", "lastSyncedAt": None, "lastSummary": None},
    )
    history_sync.setdefault("direction", "two_way")
    history_sync.setdefault("lastSyncedAt", None)
    history_sync.setdefault("lastSummary", None)

    groups = normalized.setdefault("accountGroups", _core.json.loads(_core.json.dumps(_core.DEFAULT_ACCOUNT_GROUPS)))
    if not isinstance(groups, list):
        groups = _core.json.loads(_core.json.dumps(_core.DEFAULT_ACCOUNT_GROUPS))
        normalized["accountGroups"] = groups
    known_group_ids = {str(item.get("id")) for item in groups if isinstance(item, dict)}
    for default_group in _core.DEFAULT_ACCOUNT_GROUPS:
        if default_group["id"] not in known_group_ids:
            groups.append(dict(default_group))
        else:
            built_in = next(item for item in groups if isinstance(item, dict) and item.get("id") == default_group["id"])
            built_in["system"] = True
            if default_group["id"] == "free" and built_in.get("name") == "白嫖账号":
                built_in["name"] = "日抛账号"
            built_in.setdefault("sortOrder", default_group["sortOrder"])
            built_in.setdefault("color", default_group["color"])
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            continue
        group.setdefault("sortOrder", index)
        group.setdefault("color", "slate")
        group.setdefault("system", False)

    valid_group_ids = {str(item.get("id")) for item in groups if isinstance(item, dict) and item.get("id")}
    normalized_relay_accounts: list[dict] = []
    seen_relay_account_ids: set[str] = set()
    for relay_account in relay_accounts:
        if not isinstance(relay_account, dict):
            continue
        relay_account_id = str(relay_account.get("id") or "").strip()
        if not relay_account_id or relay_account_id in seen_relay_account_ids:
            continue
        seen_relay_account_ids.add(relay_account_id)
        relay_account["id"] = relay_account_id
        if relay_account.get("groupId") not in valid_group_ids:
            relay_account["groupId"] = "relay"
        if not isinstance(relay_account.get("keys"), list):
            relay_account["keys"] = []
        if not isinstance(relay_account.get("groups"), list):
            relay_account["groups"] = []
        if not isinstance(relay_account.get("apiEndpoints"), list):
            relay_account["apiEndpoints"] = []
        if not isinstance(relay_account.get("models"), list):
            relay_account["models"] = []
        relay_account.setdefault("sourceType", "relay_account")
        relay_account.setdefault("selectedKeyId", "")
        relay_account.setdefault("selectedEndpointId", "")
        relay_account.setdefault("providerId", "")
        relay_account["keys"] = [
            item
            for item in relay_account["keys"]
            if isinstance(item, dict)
            and _core._relay_is_codex_compatible(
                item.get("groupPlatform"),
                f"{item.get('group') or ''} {item.get('name') or ''}",
                item.get("models"),
            )
        ]
        relay_account["groups"] = [
            item
            for item in relay_account["groups"]
            if isinstance(item, dict)
            and _core._relay_is_codex_compatible(item.get("platform"), item.get("name"))
        ]
        relay_account["apiEndpoints"] = [
            item
            for item in relay_account["apiEndpoints"]
            if isinstance(item, dict) and _core._relay_endpoint_is_codex_compatible(item)
        ]
        relay_account["models"] = [
            model
            for model in relay_account["models"]
            if _core._relay_is_codex_compatible("", model)
        ]
        for relay_key in relay_account["keys"]:
            if not isinstance(relay_key.get("models"), list):
                relay_key["models"] = []
                continue
            relay_key["models"] = [
                model
                for model in relay_key["models"]
                if _core._relay_is_codex_compatible("", model)
            ]
        configured_key_ids = {
            str(item.get("id") or "")
            for item in relay_account["keys"]
            if str(item.get("id") or "")
        }
        if str(relay_account.get("selectedKeyId") or "") not in configured_key_ids:
            relay_account["selectedKeyId"] = next(
                (
                    str(item.get("id") or "")
                    for item in relay_account["keys"]
                    if str(item.get("id") or "")
                ),
                "",
            )
        endpoint_ids = {
            str(item.get("id") or "")
            for item in relay_account["apiEndpoints"]
            if str(item.get("id") or "")
        }
        if str(relay_account.get("selectedEndpointId") or "") not in endpoint_ids:
            relay_account["selectedEndpointId"] = next(
                (
                    str(item.get("id") or "")
                    for item in relay_account["apiEndpoints"]
                    if str(item.get("id") or "")
                ),
                "",
            )
        relay_account.setdefault("remoteKeyCount", len(relay_account["keys"]))
        relay_account.setdefault("remoteGroupCount", len(relay_account["groups"]))
        relay_account.setdefault("importedAt", relay_account.get("updatedAt") or _core.now_iso())
        normalized_relay_accounts.append(relay_account)
    normalized["relayAccounts"] = normalized_relay_accounts
    for account in normalized.get("accounts", []):
        if not isinstance(account, dict):
            continue
        for retired_field in ("planVariantOverride", "planVariantConfirmedAt", "planVariantExpiresAt"):
            account.pop(retired_field, None)
        if account.get("groupId") not in valid_group_ids:
            account["groupId"] = "official"
        if account.get("sourceType") not in _core.VALID_ACCOUNT_SOURCES:
            account["sourceType"] = "codex_auth"
        codex_compatible = bool(
            account.get(
                "codexCompatible",
                account.get("sourceType") != "web_session",
            )
        )
        account["codexCompatible"] = codex_compatible
        account["quotaOnly"] = not codex_compatible
        account["credentialCapability"] = (
            "codex_short_lived"
            if codex_compatible and account.get("sourceType") == "web_session"
            else "codex"
            if codex_compatible
            else "quota_only"
        )
        account.setdefault(
            "refreshCapable",
            bool(account.get("authMode") == "chatgpt" and account.get("sourceType") == "codex_auth"),
        )
        requested_proxy = bool(account.get("proxyRequested", account.get("proxyEnabled", False)))
        account["proxyRequested"] = requested_proxy
        account["proxyEnabled"] = bool(account.get("proxyEnabled", False)) and codex_compatible
        account["planLabel"] = _core._normalize_plan_label(account.get("planLabel") or account.get("plan"))
        if _core._is_free_plan(account.get("plan"), account.get("planLabel")):
            _core._clear_subscription_metadata(account)
        account["importedAt"] = account.get("importedAt") or account.get("createdAt") or account.get("updatedAt")
        account.setdefault(
            "subscriptionMetadataSource",
            "token" if account.get("subscriptionExpiresAt") else None,
        )
        usage = account.get("usage")
        if isinstance(usage, dict):
            windows = [window for window in (usage.get("weekly"), usage.get("hourly")) if isinstance(window, dict)]
            if windows:
                usage["weekly"] = max(enumerate(windows), key=_core._quota_window_rank)[1]
            else:
                usage["weekly"] = None
            usage.pop("hourly", None)
            usage.pop("hourlyUnlimited", None)
            usage.setdefault("resetCredits", None)
        if version != _core.SCHEMA_VERSION and account.get("authMode") == "chatgpt":
            account["lastRefreshedAt"] = None
            account["refreshState"] = "pending"

    web2api = normalized.setdefault("web2api", _core._default_web2api_settings())
    if not isinstance(web2api, dict):
        web2api = _core._default_web2api_settings()
        normalized["web2api"] = web2api
    for key, value in _core._default_web2api_settings().items():
        web2api.setdefault(key, _core.json.loads(_core.json.dumps(value)))
    web2api["bindHost"] = "127.0.0.1"
    if web2api.get("routing") not in _core.VALID_WEB2API_ROUTING:
        web2api["routing"] = "ordered"
    if not isinstance(web2api.get("accountIds"), list):
        web2api["accountIds"] = []
    web2api["accountIds"] = [
        account_id for account_id in web2api["accountIds"]
        if isinstance(account_id, str)
        and any(
            item.get("id") == account_id and item.get("codexCompatible", True)
            for item in normalized["accounts"]
        )
    ]
    if not isinstance(web2api.get("providerIds"), list):
        web2api["providerIds"] = []
    valid_provider_ids = {
        str(item.get("id"))
        for item in normalized.get("providers", [])
        if isinstance(item, dict)
        and item.get("kind") == "custom"
        and item.get("id")
    }
    web2api["providerIds"] = list(
        dict.fromkeys(
            str(provider_id)
            for provider_id in web2api["providerIds"]
            if str(provider_id) in valid_provider_ids
        )
    )
    valid_source_ids = {
        *(f"account:{account_id}" for account_id in web2api["accountIds"]),
        *(f"provider:{provider_id}" for provider_id in web2api["providerIds"]),
    }
    raw_source_order = web2api.get("sourceOrder")
    if not isinstance(raw_source_order, list):
        raw_source_order = []
    source_order = list(
        dict.fromkeys(
            str(source_id)
            for source_id in raw_source_order
            if str(source_id) in valid_source_ids
        )
    )
    source_order.extend(
        source_id
        for source_id in (
            *(f"account:{account_id}" for account_id in web2api["accountIds"]),
            *(f"provider:{provider_id}" for provider_id in web2api["providerIds"]),
        )
        if source_id not in source_order
    )
    web2api["sourceOrder"] = source_order
    active_account_id = str(web2api.get("activeAccountId") or "").strip() or None
    active_account = next(
        (
            item
            for item in normalized["accounts"]
            if item.get("id") == active_account_id
            and item.get("authMode") == "chatgpt"
            and _core._account_codex_compatible(item)
        ),
        None,
    )
    web2api["activeAccountId"] = active_account_id if active_account else None
    if not web2api.get("activeForCodex"):
        web2api["activeAccountId"] = None
    for account in normalized["accounts"]:
        if account.get("proxyEnabled") and account.get("id") not in web2api["accountIds"]:
            web2api["accountIds"].append(account["id"])

    session_sync = normalized.setdefault("sessionSync", _core._default_session_sync_settings())
    if not isinstance(session_sync, dict):
        session_sync = _core._default_session_sync_settings()
        normalized["sessionSync"] = session_sync
    for key, value in _core._default_session_sync_settings().items():
        session_sync.setdefault(key, value)

    model_workspace = normalized.setdefault("modelWorkspace", _core._default_model_workspace())
    if not isinstance(model_workspace, dict):
        model_workspace = _core._default_model_workspace()
        normalized["modelWorkspace"] = model_workspace
    for key, value in _core._default_model_workspace().items():
        model_workspace.setdefault(key, _core.json.loads(_core.json.dumps(value)))
    if model_workspace.get("mode") not in {"independent", "aggregate"}:
        model_workspace["mode"] = "independent"
    if not isinstance(model_workspace.get("selectedModels"), list):
        model_workspace["selectedModels"] = []
    model_workspace["selectedModels"] = list(
        dict.fromkeys(str(item) for item in model_workspace["selectedModels"] if str(item).strip())
    )

    subagent_routing = normalized.setdefault("subagentRouting", _core._default_subagent_routing())
    if not isinstance(subagent_routing, dict):
        subagent_routing = _core._default_subagent_routing()
        normalized["subagentRouting"] = subagent_routing
    for key, value in _core._default_subagent_routing().items():
        subagent_routing.setdefault(key, _core.json.loads(_core.json.dumps(value)))
    managed_subagent_policy = normalized.get("managedSubagentPolicy")
    if managed_subagent_policy is not None and not isinstance(managed_subagent_policy, dict):
        normalized["managedSubagentPolicy"] = None
    if str(subagent_routing.get("prompt") or "").strip() in {"", _core.PRE_V10_DEFAULT_CALL_STRATEGY_PROMPT}:
        subagent_routing["prompt"] = _core.DEFAULT_CALL_STRATEGY_PROMPT
    sub_routes = subagent_routing.setdefault("routes", {})
    if not isinstance(sub_routes, dict):
        sub_routes = {}
        subagent_routing["routes"] = sub_routes
    for level in _core.DIFFICULTIES:
        route = sub_routes.setdefault(level, {"models": [], "efforts": []})
        if not isinstance(route, dict):
            route = {"models": [], "efforts": []}
            sub_routes[level] = route
        raw_models = route.get("models", [])
        raw_efforts = route.get("efforts")
        has_explicit_efforts = isinstance(raw_efforts, list)
        normalized_models: list[str] = []
        normalized_efforts: list[str] = []
        if isinstance(raw_models, list):
            for index, item in enumerate(raw_models):
                model_key = str(item).strip()
                if not model_key or model_key in normalized_models:
                    continue
                requested_effort = (
                    str(raw_efforts[index] or "").strip()
                    if has_explicit_efforts and index < len(raw_efforts)
                    else _core.DEFAULT_EFFORT_BY_DIFFICULTY[level]
                )
                normalized_models.append(model_key)
                normalized_efforts.append(requested_effort if requested_effort in _core.VALID_EFFORTS else "")
                if len(normalized_models) == 3:
                    break
        route["models"] = normalized_models
        route["efforts"] = normalized_efforts

    normalized["runtimeTuning"] = _core._normalize_runtime_tuning(
        normalized.get("runtimeTuning")
    )

    app_behavior = normalized.setdefault("appBehavior", _core._default_app_behavior())
    if not isinstance(app_behavior, dict):
        app_behavior = _core._default_app_behavior()
        normalized["appBehavior"] = app_behavior
    app_behavior["closeToTray"] = bool(app_behavior.get("closeToTray", False))
    app_behavior["radarMonitoring"] = app_behavior.get("radarMonitoring") is True
    if app_behavior.get("appearance") not in {"light", "dark", "system"}:
        app_behavior["appearance"] = "system"
    app_behavior["usageRange"] = _core._normalize_usage_range(app_behavior.get("usageRange"))
    try:
        quota_refresh_minutes = int(app_behavior.get("quotaRefreshMinutes", 10))
    except (TypeError, ValueError):
        quota_refresh_minutes = 10
    # Version 9 exposed a 2-minute mode that could create avoidable bursts when
    # several accounts were imported.  Preserve the feature while moving that
    # unsafe legacy value to the conservative default.
    if quota_refresh_minutes == 2:
        quota_refresh_minutes = 10
    app_behavior["quotaRefreshMinutes"] = (
        quota_refresh_minutes if quota_refresh_minutes in _core.VALID_QUOTA_REFRESH_MINUTES else 10
    )
    try:
        mail_health_hours = int(app_behavior.get("mailHealthCheckHours", 24))
    except (TypeError, ValueError):
        mail_health_hours = 24
    app_behavior["mailHealthCheckHours"] = (
        mail_health_hours if mail_health_hours in _core.VALID_MAIL_HEALTH_CHECK_HOURS else 24
    )

    for provider in normalized.get("providers", []):
        if isinstance(provider, dict):
            fallback_group = "official" if provider.get("kind") == "builtin" else "relay"
            if provider.get("groupId") not in valid_group_ids:
                provider["groupId"] = fallback_group
            legacy_hajimi_base = str(provider.get("baseUrl") or "").strip().rstrip("/").casefold()
            raw_hajimi_preset = str(provider.get("presetId") or "").strip().casefold()
            if (
                provider.get("kind") == "custom"
                and legacy_hajimi_base in {"https://hajimi.chat", "https://hajimi.chat/v1"}
                and (
                    raw_hajimi_preset == "hajimi"
                    or (version < 13 and raw_hajimi_preset == "")
                )
            ):
                # Hajimi split its dashboard and API hosts.  Providers created
                # from the older curated shortcut otherwise keep calling the
                # SPA host and receive HTML instead of OpenAI-compatible JSON.
                provider["baseUrl"] = "https://api.hajimi.chat/v1"
                if str(provider.get("modelsEndpoint") or "").strip().casefold() in {
                    "",
                    "https://hajimi.chat/v1/models",
                }:
                    provider["modelsEndpoint"] = "https://api.hajimi.chat/v1/models"
                if str(provider.get("balanceEndpoint") or "").strip().casefold() in {
                    "",
                    "https://hajimi.chat/v1/usage",
                }:
                    provider["balanceEndpoint"] = "https://api.hajimi.chat/v1/usage"
                provider["resolvedBaseUrl"] = ""
                provider["lastCheckStatus"] = "pending"
                provider["modelDiscoveryState"] = "pending"
            provider.setdefault(
                "modelsEndpoint",
                provider.get("models_endpoint") or provider.get("models_url") or "",
            )
            provider["modelsEndpoint"] = str(provider.get("modelsEndpoint") or "").strip()
            detected_preset = (
                _core.detect_provider_portal_preset(
                    provider.get("baseUrl"),
                    provider.get("portalUrl"),
                )
                if version < 13
                else None
            )
            raw_preset_id = str(provider.get("presetId") or "").strip().casefold()
            known_preset = next(
                (item for item in _core.PROVIDER_PORTAL_PRESETS if item["id"] == raw_preset_id),
                detected_preset,
            )
            provider["presetId"] = str((known_preset or {}).get("id") or "")
            provider["portalUrl"] = str(
                provider.get("portalUrl")
                or (known_preset or {}).get("dashboardUrl")
                or ""
            ).strip()
            integration_kind = str(
                provider.get("integrationKind")
                or (known_preset or {}).get("integrationKind")
                or ""
            ).strip().casefold()
            provider["integrationKind"] = integration_kind if integration_kind in {"", "sub2api"} else ""
            provider.setdefault("balanceEndpoint", "")
            provider.setdefault("balance", None)
            provider.setdefault("balanceUpdatedAt", None)
            provider.setdefault("balanceError", None)
            provider.setdefault("sourceType", "manual_api")
            provider.setdefault("relayAccountId", "")
            provider.setdefault("relayKeyId", "")
            provider.setdefault("relayGroupId", "")
            provider.setdefault("relayGroupName", "")
            provider.setdefault("relayPlatform", "")
            provider.setdefault("relayRateMultiplier", None)
            provider.setdefault("relayEndpointId", "")
            provider.setdefault("relayEndpointName", "")
            provider.setdefault("lastCheckedAt", None)
            provider.setdefault("lastCheckStatus", "pending")
            provider.setdefault("lastCheckError", None)
            provider.setdefault("modelDiscoveryState", provider.get("lastCheckStatus") or "pending")
            provider.setdefault("modelDiscoveryError", provider.get("lastCheckError"))
            provider.setdefault("modelCapabilitiesRefreshedAt", None)
            if provider.get("kind") == "custom":
                # Schema 14 replaces the noisy updatedAt/lastAppliedAt heuristic
                # with an explicit runtime revision.  Existing providers start
                # aligned: startup applies their current configuration, while
                # every subsequent key/endpoint edit increments the revision.
                try:
                    runtime_revision = max(1, int(provider.get("runtimeRevision") or 1))
                except (TypeError, ValueError):
                    runtime_revision = 1
                provider["runtimeRevision"] = runtime_revision
                if version < 14 or "appliedRuntimeRevision" not in provider:
                    provider["appliedRuntimeRevision"] = runtime_revision
                else:
                    try:
                        provider["appliedRuntimeRevision"] = min(
                            runtime_revision,
                            max(0, int(provider.get("appliedRuntimeRevision") or 0)),
                        )
                    except (TypeError, ValueError):
                        provider["appliedRuntimeRevision"] = 0
            # Releases before 6.0 treated any /models failure as a fatal
            # provider error.  Many compatible relays intentionally reject
            # that optional endpoint while still serving configured models.
            # Repair the persisted state on load, but never soften an explicit
            # credential/billing rejection.
            provider_models = [
                str(item).strip()
                for item in provider.get("models", [])
                if str(item).strip()
            ] if isinstance(provider.get("models"), list) else []
            if provider.get("kind") == "custom":
                provider["modelCapabilities"] = _core._normalize_provider_model_capabilities(
                    provider.get("modelCapabilities"),
                    provider_models,
                )
            discovery_error = str(
                provider.get("modelDiscoveryError")
                or provider.get("lastCheckError")
                or ""
            )
            credential_rejection = bool(
                _core.re.search(
                    r"(?:\bHTTP\s+(?:401|402)\b|unauthori[sz]ed|payment\s+required|"
                    r"invalid(?:ated)?\s+(?:api\s+key|token)|deactivated[_\s-]*workspace|"
                    r"account\s+deactivated)",
                    discovery_error,
                    flags=_core.re.IGNORECASE,
                )
            )
            if (
                provider.get("kind") == "custom"
                and provider_models
                and str(
                    provider.get("modelDiscoveryState")
                    or provider.get("lastCheckStatus")
                    or ""
                ).casefold() == "error"
                and not credential_rejection
            ):
                provider["lastCheckStatus"] = "stale"
                provider["modelDiscoveryState"] = "stale"
            provider["proxyEnabled"] = str(provider.get("id") or "") in set(web2api["providerIds"])

    # Strategies are application-owned presets.  Refresh their names and fixed
    # policies on upgrade while retaining only the user's custom-mode prompt.
    previous_strategies = {
        str(item.get("id")): item
        for item in normalized.get("strategies", [])
        if isinstance(item, dict) and item.get("id")
    }
    normalized_strategies = _core.json.loads(_core.json.dumps(_core.DEFAULT_STRATEGIES))
    custom = previous_strategies.get("parallel_first")
    if custom and str(custom.get("instructions") or "").strip():
        old_text = str(custom.get("instructions") or "").strip()
        legacy_parallel = "当任务可以拆成两个以上边界清楚、互不依赖且可分别验收的工作单元时"
        if not old_text.startswith(legacy_parallel):
            normalized_strategies[2]["instructions"] = old_text
    normalized["strategies"] = normalized_strategies
    active_strategy = next(
        (
            item
            for item in normalized_strategies
            if item.get("id") == subagent_routing.get("strategyId")
        ),
        normalized_strategies[0],
    )
    if active_strategy.get("id") != "parallel_first":
        subagent_routing["prompt"] = active_strategy["instructions"]
    normalized["schemaVersion"] = _core.SCHEMA_VERSION
    return normalized, normalized != settings



def _parsed_datetime(value: _core.Any) -> _core.datetime | None:
    if not value:
        return None
    try:
        parsed = _core.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_core.timezone.utc)
    return parsed.astimezone(_core.timezone.utc)



def _provider_runtime_signature(provider: dict | None) -> tuple[str, ...]:
    """Return only fields that change what Codex sends at runtime.

    Dashboard metadata (balance, display group, portal URL, refresh time, and
    local card grouping) is intentionally excluded.  Those fields used to
    toggle the "needs reapply" badge after every account refresh.
    """

    record = provider if isinstance(provider, dict) else {}
    return tuple(
        _core.json.dumps(record.get(field), ensure_ascii=False, separators=(",", ":"))
        if field in {"models", "modelCapabilities"}
        else str(record.get(field) or "").strip()
        for field in _core._PROVIDER_RUNTIME_FIELDS
    )



def _provider_probe_token(provider: dict) -> tuple[int, tuple[str, ...]]:
    current, _applied = _core._provider_runtime_revisions(provider)
    return current, _core._provider_runtime_signature(provider)



def _provider_probe_is_current(settings: dict, provider_id: str, token: tuple[int, tuple[str, ...]]) -> bool:
    current = next(
        (
            item
            for item in settings.get("providers", [])
            if isinstance(item, dict) and str(item.get("id") or "") == provider_id
        ),
        None,
    )
    return bool(current and _core._provider_probe_token(current) == token)



def _provider_runtime_revisions(provider: dict | None) -> tuple[int, int]:
    record = provider if isinstance(provider, dict) else {}
    try:
        current = max(1, int(record.get("runtimeRevision") or 1))
    except (TypeError, ValueError):
        current = 1
    try:
        applied = max(0, int(record.get("appliedRuntimeRevision") or 0))
    except (TypeError, ValueError):
        applied = 0
    return current, min(applied, current)



def _provider_runtime_requires_reapply(settings: dict, provider: dict) -> bool:
    provider_id = str(provider.get("id") or "")
    active_source_id = str(settings.get("modelWorkspace", {}).get("activeSourceId") or "")
    current, applied = _core._provider_runtime_revisions(provider)
    return bool(
        provider_id
        and active_source_id == f"provider:{provider_id}"
        and current > applied
    )



def _timestamp_is_stale(value: _core.Any, ttl_seconds: int) -> bool:
    parsed = _core._parsed_datetime(value)
    if parsed is None:
        return True
    return (_core.datetime.now(_core.timezone.utc) - parsed).total_seconds() >= max(1, int(ttl_seconds))



def _recover_unexpired_reset_credits_from_backups(settings: dict) -> int:
    """Recover details erased by the pre-v10 sparse-response merge bug.

    Recovery is deliberately narrow: the live record must have no authoritative
    detail list, the backup must match the same internal account id, and at
    least one backed-up card must have a parseable future expiry.  A confirmed
    empty detail list (for example after an explicit consume) is never changed.
    """

    candidates = [
        account
        for account in settings.get("accounts", [])
        if isinstance(account, dict)
        and not bool(((account.get("usage") or {}).get("resetCredits") or {}).get("detailsAvailable"))
    ]
    if not candidates or not _core.BACKUPS_DIR.exists():
        return 0
    wanted = {str(account.get("id")): account for account in candidates if account.get("id")}
    recovered = 0
    now = _core.datetime.now(_core.timezone.utc)
    backup_paths = sorted(
        _core.BACKUPS_DIR.glob("settings.json.*.bak"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:64]
    for path in backup_paths:
        if not wanted:
            break
        try:
            backup = _core.read_json(path, {})
        except (_core.ManagerError, OSError):
            continue
        if not isinstance(backup, dict):
            continue
        for old_account in backup.get("accounts", []):
            if not isinstance(old_account, dict):
                continue
            account_id = str(old_account.get("id") or "")
            live = wanted.get(account_id)
            if live is None:
                continue
            old_reset = ((old_account.get("usage") or {}).get("resetCredits") or {})
            old_credits = old_reset.get("credits") if isinstance(old_reset, dict) else None
            if not isinstance(old_credits, list) or not old_credits:
                continue
            valid_credits = [
                credit
                for credit in old_credits
                if isinstance(credit, dict)
                and (expiry := _core._parsed_datetime(credit.get("expiresAt"))) is not None
                and expiry > now
            ]
            if not valid_credits:
                continue
            try:
                backed_up_count = max(0, int(old_reset.get("availableCount") or 0))
            except (TypeError, ValueError):
                backed_up_count = 0
            usage = live.setdefault("usage", {})
            usage["resetCredits"] = {
                **_core.json.loads(_core.json.dumps(old_reset)),
                "availableCount": max(backed_up_count, len(valid_credits)),
                "credits": valid_credits,
                "detailsAvailable": True,
                "stale": True,
                "recoveredFromBackupAt": _core.now_iso(),
            }
            live.setdefault("refreshErrors", {})["resetCredits"] = (
                "已从本地备份恢复仍在有效期内的重置卡；下次官方确认前不会被空响应覆盖。"
            )
            wanted.pop(account_id, None)
            recovered += 1
    return recovered



def _keyed_settings_list(value: _core.Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    ids = [str(item.get("id") or "") for item in value if isinstance(item, dict)]
    return len(ids) == len(value) and all(ids) and len(set(ids)) == len(ids)



def _mergeable_keyed_settings_lists(baseline: _core.Any, incoming: _core.Any, current: _core.Any) -> bool:
    if not all(isinstance(value, list) for value in (baseline, incoming, current)):
        return False
    values = (baseline, incoming, current)
    return any(_core._keyed_settings_list(value) for value in values) and all(
        not value or _core._keyed_settings_list(value) for value in values
    )



def _merge_settings_delta(baseline: _core.Any, incoming: _core.Any, current: _core.Any) -> _core.Any:
    """Apply the incoming-vs-baseline delta to the latest current value."""

    if baseline == incoming:
        return _core._json_clone(current)
    if isinstance(baseline, dict) and isinstance(incoming, dict):
        live = current if isinstance(current, dict) else {}
        merged = _core._json_clone(live)
        for key in baseline.keys() - incoming.keys():
            merged.pop(key, None)
        for key, value in incoming.items():
            if key not in baseline:
                merged[key] = _core._json_clone(value)
                continue
            merged[key] = _core._merge_settings_delta(
                baseline[key],
                value,
                live.get(key, _core._MISSING_SETTING),
            )
        return merged
    normalized_current = [] if current is _core._MISSING_SETTING else current
    if _core._mergeable_keyed_settings_lists(baseline, incoming, normalized_current):
        live_list = normalized_current
        baseline_by_id = {str(item["id"]): item for item in baseline}
        incoming_by_id = {str(item["id"]): item for item in incoming}
        live_by_id = {str(item["id"]): item for item in live_list}
        removed = set(baseline_by_id) - set(incoming_by_id)
        merged_by_id = {
            item_id: _core._json_clone(item)
            for item_id, item in live_by_id.items()
            if item_id not in removed
        }
        for item_id, item in incoming_by_id.items():
            if item_id not in baseline_by_id:
                merged_by_id[item_id] = _core._json_clone(item)
            else:
                merged_by_id[item_id] = _core._merge_settings_delta(
                    baseline_by_id[item_id],
                    item,
                    live_by_id.get(item_id, _core._MISSING_SETTING),
                )
        ordered_ids = list(incoming_by_id)
        ordered_ids.extend(item_id for item_id in live_by_id if item_id not in incoming_by_id and item_id not in removed)
        return [merged_by_id[item_id] for item_id in ordered_ids if item_id in merged_by_id]
    return _core._json_clone(incoming)



def _read_and_migrate_settings_locked() -> tuple[dict, bool]:
    settings = _core.read_json(_core.SETTINGS_FILE, {})
    if not isinstance(settings, dict):
        raise _core.ManagerError("设置文件必须是 JSON 对象。")
    try:
        previous_schema = int(settings.get("schemaVersion") or 0)
    except (TypeError, ValueError):
        previous_schema = 0
    settings, changed = _core._migrate_settings(settings)
    if previous_schema < 10 and _core._recover_unexpired_reset_credits_from_backups(settings):
        changed = True
    settings["schemaVersion"] = _core.SCHEMA_VERSION
    return settings, changed



def load_settings() -> _core.SettingsDocument:
    _core.ensure_state()
    with _core.SETTINGS_LOCK, _core._settings_file_lock():
        settings, changed = _core._read_and_migrate_settings_locked()
        if changed:
            _core.backup_file(_core.SETTINGS_FILE)
            _core.atomic_write_json(_core.SETTINGS_FILE, settings)
        return _core.SettingsDocument(_core._json_clone(settings), baseline=settings)



def save_settings(settings: dict) -> None:
    if not isinstance(settings, dict):
        raise _core.ManagerError("设置文件必须是 JSON 对象。")
    # Callers may construct an initial settings document and save it before
    # any preceding load_settings() call (first run, tests, or recovery).  The
    # merge path still needs a valid on-disk baseline in that case.
    _core.ensure_state()
    settings["schemaVersion"] = _core.SCHEMA_VERSION
    with _core.SETTINGS_LOCK, _core._settings_file_lock():
        current, migrated = _core._read_and_migrate_settings_locked()
        baseline = getattr(settings, "_baseline", None)
        next_settings = (
            _core._merge_settings_delta(baseline, settings, current)
            if isinstance(baseline, dict)
            else _core._json_clone(settings)
        )
        next_settings["schemaVersion"] = _core.SCHEMA_VERSION
        if migrated or next_settings != current:
            _core.atomic_write_json(_core.SETTINGS_FILE, next_settings)
        if isinstance(settings, _core.SettingsDocument):
            settings.clear()
            settings.update(_core._json_clone(next_settings))
            settings._baseline = _core._json_clone(next_settings)

