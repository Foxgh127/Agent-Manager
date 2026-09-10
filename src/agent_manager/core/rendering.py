"""Rendering services."""
from __future__ import annotations
from agent_manager import core as _core


def _managed_subagent_specs(settings: dict) -> list[dict]:
    if _core._uses_codex_native_subagent_policy(settings):
        return []
    records = _core.gateway_model_records(settings)
    by_key = {item["key"]: item for item in records}
    main_records = (
        _core.web2api_pool_model_records(settings)
        if settings.get("web2api", {}).get("activeForCodex")
        else _core.selected_model_records(settings)
    )
    default = _core._default_main_record(settings, main_records)
    main_source = str(default.get("sourceId") or "") if default else ""
    routing = settings.get("subagentRouting", {})
    capabilities = _core._reasoning_capabilities()
    specs = []
    for level in _core.DIFFICULTIES:
        route = routing.get("routes", {}).get(level, {})
        raw_keys = [str(item) for item in route.get("models", [])]
        route_efforts = route.get("efforts", []) if isinstance(route.get("efforts"), list) else []
        configured = [
            (
                key,
                str(route_efforts[index] or "").strip() if index < len(route_efforts) else "",
            )
            for index, key in enumerate(raw_keys)
            if key in by_key
        ]
        if not raw_keys and default:
            configured = [(default["key"], _core._default_model_reasoning_effort(default, level))]
        for index, (key, requested_effort) in enumerate(configured[:3], start=1):
            record = by_key[key]
            alias_suffix = _core.hashlib.sha256(
                f"{level}:{index}:{record['key']}".encode("utf-8")
            ).hexdigest()[:8]
            safe_model = _core.re.sub(r"[^A-Za-z0-9._-]+", "-", str(record["id"])).strip("-")[:48] or "model"
            agent_model_alias = f"cam-agent-{level}-{index}-{safe_model}-{alias_suffix}"
            effort = requested_effort if requested_effort in _core.VALID_EFFORTS else None
            source_id = str(record.get("sourceId") or str(record.get("key") or "").split("::", 1)[0])
            capability = (
                record
                if record.get("reasoningKnown")
                else capabilities.get(str(record.get("id") or ""))
                if source_id.startswith("account:")
                else None
            )
            if effort and capability is not None and effort not in capability.get("efforts", []):
                effort = None
            specs.append(
                {
                    "name": f"cam_{level}_{index}",
                    "level": level,
                    "slot": index,
                    "description": (
                        f"{_core.DIFFICULTY_META[level]['name']}级子代理，第 {index} 顺位；"
                        f"由 Agent Manager 路由到 {record['sourceName']} / {record['id']}。"
                    ),
                    "modelKey": key,
                    "model": agent_model_alias,
                    "nativeModel": record["id"],
                    "sourceId": record.get("sourceId") or str(record["key"]).split("::", 1)[0],
                    "sourceName": record["sourceName"],
                    "provider": _core.AGGREGATE_PROVIDER_ID,
                    "effort": effort,
                    "path": _core.AGENTS_DIR / f"cam-{level}-{index}.toml",
                }
            )
    # Current Codex applies bounded role overrides and intentionally inherits
    # the parent's Provider. A per-role model_provider value cannot select a
    # different account. Same-source children use native IDs; cross-source
    # children require one shared gateway Provider at the parent level.
    shared_gateway = bool(
        settings.get("modelWorkspace", {}).get("mode") == "aggregate"
        or settings.get("web2api", {}).get("activeForCodex")
        or any(spec["sourceId"] != main_source for spec in specs)
    )
    for spec in specs:
        spec["routingMode"] = "gateway" if shared_gateway else "native"
        if not shared_gateway:
            spec["model"] = spec["nativeModel"]
            spec["provider"] = None
    return specs



def _subagents_require_shared_gateway(settings: dict) -> bool:
    return any(spec.get("routingMode") == "gateway" for spec in _core._managed_subagent_specs(settings))



def _configuration_model_records(settings: dict) -> list[dict]:
    if settings.get("web2api", {}).get("activeForCodex"):
        return _core.web2api_pool_model_records(settings)
    records = _core.selected_model_records(settings)
    if _core._subagents_require_shared_gateway(settings):
        by_key = {record["key"]: record for record in _core.gateway_model_records(settings)}
        return [by_key.get(record["key"], record) for record in records]
    return records



def subagent_runtime_summary(settings: dict) -> dict:
    specs = _core._managed_subagent_specs(settings)
    gateway = any(spec.get("routingMode") == "gateway" for spec in specs)
    codex_native = _core._uses_codex_native_subagent_policy(settings)
    mode = "gateway" if gateway else "native" if specs or codex_native else "unconfigured"
    return {
        "mode": mode, "roleCount": len(specs), "requiresSharedGateway": gateway,
        "providerInherited": True,
        "policySource": "codex" if codex_native else "agent_manager",
        "managed": bool(specs),
        "message": (
            "跨账号子代理与主会话共享本地网关，配置变更后需要重新载入 Codex。"
            if gateway
            else "子代理继承主会话 Provider，直接使用同一账号的原生模型。"
            if specs
            else "跟随 Codex 原生委派策略；Agent Manager 未注册、启用或禁止子代理。"
            if codex_native
            else "尚未配置可用子代理。"
        ),
    }



def managed_subagent_model_records(settings: dict | None = None) -> list[dict]:
    """Return private model aliases used only by generated child agents."""
    settings = settings or _core.load_settings()
    by_key = {item["key"]: item for item in _core.gateway_model_records(settings)}
    records = []
    for spec in _core._managed_subagent_specs(settings):
        if spec.get("routingMode") != "gateway":
            continue
        record = by_key.get(str(spec.get("modelKey") or ""))
        if not record:
            continue
        records.append(
            {
                **record,
                "slug": spec["model"],
                "displayName": f"{_core.DIFFICULTY_META[spec['level']]['name']}子代理 · {record['displayName']}",
                "subagentAlias": True,
                "subagentLevel": spec["level"],
                "subagentSlot": spec["slot"],
            }
        )
    return records



def _default_main_record(settings: dict, records: list[dict]) -> dict | None:
    if not records:
        return None
    requested = str(settings.get("modelWorkspace", {}).get("defaultModelKey") or "")
    return next((item for item in records if item["key"] == requested), None) or records[0]



def _managed_provider_base_urls(settings: dict) -> set[str]:
    return {
        str(item.get("resolvedBaseUrl") or item.get("baseUrl") or "").strip()
        for item in settings.get("providers", [])
        if isinstance(item, dict) and item.get("kind") == "custom"
    } - {""}



def _codex_provider_card_name(settings: dict, provider: dict) -> str:
    relay_id = str(provider.get("relayAccountId") or "")
    account = next((item for item in settings.get("relayAccounts", [])
                    if str(item.get("id") or "") == relay_id), None) if relay_id else None
    name = (account.get("name") or account.get("siteName")) if account else None
    return str(name or provider.get("name") or provider.get("id") or "Agent Manager").strip()[:160]



def _codex_gateway_provider_name(settings: dict, main_record: dict | None) -> str:
    """Label the gateway with the selected API card, without changing identity."""
    if main_record and main_record.get("sourceKind") == "provider":
        provider_id = str(main_record.get("sourceRecordId") or "")
    elif not main_record:
        source_id = str(settings.get("modelWorkspace", {}).get("activeSourceId") or "")
        provider_id = source_id.split(":", 1)[1] if source_id.startswith("provider:") else ""
    else:
        provider_id = ""
    provider = next(
        (item for item in settings.get("providers", []) if str(item.get("id") or "") == provider_id),
        None,
    ) if provider_id else None
    if provider:
        return _core._codex_provider_card_name(settings, provider)
    return str((main_record or {}).get("sourceName") or "Agent Manager").strip() or "Agent Manager"



def _apply_root_runtime_tuning(doc: object, tuning: dict) -> None:
    managed_fields = set(tuning.get("managedFields") or [])
    if not managed_fields:
        return
    context_window = int(tuning.get("modelContextWindow") or 0)
    compact_limit = int(tuning.get("autoCompactTokenLimit") or 0)
    if "modelContextWindow" in managed_fields:
        if context_window:
            doc["model_context_window"] = context_window
        else:
            doc.pop("model_context_window", None)
    if "autoCompactTokenLimit" in managed_fields:
        if compact_limit:
            doc["model_auto_compact_token_limit"] = compact_limit
        else:
            doc.pop("model_auto_compact_token_limit", None)
    if "autoCompactScope" in managed_fields:
        # Codex applies this scope to both an explicit threshold and the
        # model-provided automatic threshold.  Keep its ownership independent
        # so resetting one control cannot erase the other control's value.
        doc["model_auto_compact_token_limit_scope"] = tuning["autoCompactScope"]
    if "mcpOptionalStartupGraceMs" in managed_fields:
        mcp_grace = int(tuning.get("mcpOptionalStartupGraceMs", -1))
        if mcp_grace >= 0:
            doc["mcp_optional_startup_grace_ms"] = mcp_grace
        else:
            doc.pop("mcp_optional_startup_grace_ms", None)
    if "webSearch" in managed_fields:
        if tuning.get("webSearch"):
            doc["web_search"] = tuning["webSearch"]
        else:
            doc.pop("web_search", None)
    if "serviceTier" in managed_fields:
        if tuning.get("serviceTier"):
            doc["service_tier"] = tuning["serviceTier"]
        else:
            doc.pop("service_tier", None)
    if "webSearchContextSize" in managed_fields:
        tools = doc.get("tools")
        web_search_tool = tools.get("web_search") if isinstance(tools, _core.Mapping) else None
        context_size = str(tuning.get("webSearchContextSize") or "")
        if context_size:
            if not isinstance(tools, _core.Mapping):
                tools = _core.tomlkit.table()
                doc["tools"] = tools
            if not isinstance(web_search_tool, _core.Mapping):
                web_search_tool = _core.tomlkit.table()
                tools["web_search"] = web_search_tool
            web_search_tool["context_size"] = context_size
        elif isinstance(web_search_tool, _core.Mapping):
            web_search_tool.pop("context_size", None)
            if not web_search_tool:
                tools.pop("web_search", None)
            if not tools:
                doc.pop("tools", None)
    if "preventIdleSleep" in managed_fields:
        features = doc.get("features")
        if bool(tuning.get("preventIdleSleep", False)):
            if not isinstance(features, _core.Mapping):
                features = _core.tomlkit.table()
                doc["features"] = features
            features["prevent_idle_sleep"] = True
        elif isinstance(features, _core.Mapping):
            features.pop("prevent_idle_sleep", None)
            if not features:
                doc.pop("features", None)



def _apply_provider_runtime_tuning(table: object, tuning: dict, *, aggregate: bool = False) -> None:
    managed_fields = set(tuning.get("managedFields") or [])
    vpn_managed = "vpnCompatibility" in managed_fields
    vpn_mode = bool(tuning.get("vpnCompatibility")) if vpn_managed else False
    if vpn_managed and vpn_mode:
        table["stream_max_retries"] = 0
        table["supports_websockets"] = False
    elif vpn_managed:
        # Turning the repair off restores Codex/provider defaults instead of
        # replacing them with another set of manager-owned magic numbers.
        table.pop("stream_max_retries", None)
        if not aggregate:
            table.pop("supports_websockets", None)
    # The local aggregate gateway exposes Responses over HTTP/SSE only and
    # must advertise that transport fact regardless of the visual control.
    if aggregate:
        table["supports_websockets"] = False



def _use_native_official_model_catalog(settings: dict, records: list[dict]) -> bool:
    """Let Codex own an unfiltered official account's live model catalog.

    Cockpit intentionally avoids writing ``model_catalog_json`` for official
    OAuth accounts.  A manager-generated static catalog is still required for
    relays, aggregate aliases, curated subsets, explicit extended context,
    and the catalog-level VPN compatibility override. For an unmodified official
    account, however, it only freezes staged model rollouts until the next
    manager refresh.
    """

    workspace = settings.get("modelWorkspace", {})
    if (
        not workspace.get("syncToCodex", True)
        or workspace.get("mode") != "independent"
        or settings.get("web2api", {}).get("activeForCodex")
        or _core._subagents_require_shared_gateway(settings)
        or not records
    ):
        return False
    source_id = str(workspace.get("activeSourceId") or "")
    if not source_id.startswith("account:"):
        return False
    account_id = source_id.split(":", 1)[1]
    account = next(
        (
            item
            for item in settings.get("accounts", [])
            if isinstance(item, dict) and str(item.get("id") or "") == account_id
        ),
        None,
    )
    if (
        not account
        or account.get("authMode") != "chatgpt"
        or not _core._account_codex_compatible(account)
        or any(
            item.get("sourceKind") != "account"
            or str(item.get("sourceRecordId") or "") != account_id
            for item in records
        )
    ):
        return False
    available_ids = {
        model_id
        for value in account.get("models", [])
        if (model_id := _core._bounded_model_id(value))
    }
    selected_ids = {
        model_id
        for item in records
        if (model_id := _core._bounded_model_id(item.get("id")))
    }
    if not available_ids or selected_ids != available_ids:
        return False
    tuning = _core._normalize_runtime_tuning(settings.get("runtimeTuning"))
    managed_fields = set(tuning.get("managedFields") or [])
    # Explicit extended context needs the matching catalog ceiling too.
    # Resetting the control to model default returns ownership to Codex.
    if any(_core._managed_context_catalog_ceiling(record, tuning) for record in records):
        return False
    return not (
        "vpnCompatibility" in managed_fields
        and bool(tuning.get("vpnCompatibility"))
    )



def _set_main_profile_reasoning_effort(
    doc: object,
    settings: dict,
    profile: dict,
    main_record: dict | None,
) -> None:
    provider_id = str(profile.get("provider") or "openai")
    model_id = str(
        main_record.get("id")
        if isinstance(main_record, dict) and main_record.get("id")
        else profile.get("model") or ""
    ).strip()
    effort = _core._normalize_main_profile_effort(
        settings,
        provider_id,
        model_id,
        profile.get("effort"),
        main_record=main_record,
        reject_conflict=False,
    )
    if effort:
        doc["model_reasoning_effort"] = effort
    else:
        # Empty means model/provider default.  Also clear a stale value written
        # by an earlier Agent Manager version.
        doc.pop("model_reasoning_effort", None)



def _multi_agent_v2_mode_hint(doc: object) -> tuple[bool, str | None]:
    features = doc.get("features") if hasattr(doc, "get") else None
    multi_agent_v2 = features.get("multi_agent_v2") if hasattr(features, "get") else None
    if not hasattr(multi_agent_v2, "get") or "multi_agent_mode_hint_text" not in multi_agent_v2:
        return False, None
    return True, str(multi_agent_v2.get("multi_agent_mode_hint_text") or "")



def _ensure_multi_agent_v2_table(doc: object) -> object:
    features = doc.get("features") if hasattr(doc, "get") else None
    if features is None:
        features = _core.tomlkit.table()
        doc["features"] = features
    elif not hasattr(features, "get"):
        raise _core.ManagerError("Codex 配置中的 [features] 不是有效表格。")
    multi_agent_v2 = features.get("multi_agent_v2")
    if multi_agent_v2 is None:
        multi_agent_v2 = _core.tomlkit.table()
        features["multi_agent_v2"] = multi_agent_v2
    elif isinstance(multi_agent_v2, bool):
        enabled = multi_agent_v2
        multi_agent_v2 = _core.tomlkit.table()
        multi_agent_v2["enabled"] = enabled
        features["multi_agent_v2"] = multi_agent_v2
    elif not hasattr(multi_agent_v2, "get"):
        raise _core.ManagerError("Codex 配置中的 features.multi_agent_v2 格式无效。")
    return multi_agent_v2



def _normalized_managed_subagent_policy(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    applied = value.get("appliedHint")
    if not isinstance(applied, str):
        return None
    baseline_present = bool(value.get("baselinePresent"))
    baseline = value.get("baselineHint")
    if baseline_present and not isinstance(baseline, str):
        return None
    return {
        "appliedHint": applied,
        "baselinePresent": baseline_present,
        "baselineHint": baseline if baseline_present else None,
        "baselineWasBool": bool(value.get("baselineWasBool")),
        "baselineEnabledPresent": bool(value.get("baselineEnabledPresent")),
        "baselineEnabled": value.get("baselineEnabled") if isinstance(value.get("baselineEnabled"), bool) else None,
    }



def _apply_managed_subagent_mode_hint(doc: object, settings: dict) -> None:
    """Apply or release only Agent Manager's owned V2 policy hint."""

    desired = _core._managed_subagent_mode_hint(settings)
    ownership = _core._normalized_managed_subagent_policy(settings.get("managedSubagentPolicy"))
    current_present, current_hint = _core._multi_agent_v2_mode_hint(doc)
    if desired is not None:
        table = _core._ensure_multi_agent_v2_table(doc)
        table["multi_agent_mode_hint_text"] = desired
        table["enabled"] = True
        return
    if not ownership or not current_present or current_hint != ownership["appliedHint"]:
        return
    table = _core._ensure_multi_agent_v2_table(doc)
    if ownership["baselinePresent"]:
        table["multi_agent_mode_hint_text"] = ownership["baselineHint"]
    else:
        table.pop("multi_agent_mode_hint_text", None)
    if table.get("enabled") is True:
        if ownership["baselineEnabledPresent"]:
            table["enabled"] = ownership["baselineEnabled"]
        else:
            table.pop("enabled", None)
    features = doc.get("features")
    if ownership["baselineWasBool"] and set(table) == {"enabled"}:
        features["multi_agent_v2"] = bool(table["enabled"])
    elif not table:
        features.pop("multi_agent_v2", None)
    if not features:
        doc.pop("features", None)



def _next_managed_subagent_policy(settings: dict, original_config: str) -> dict | None:
    """Return ownership state to persist after a successful config write."""

    desired = _core._managed_subagent_mode_hint(settings)
    if desired is None:
        return None
    try:
        original_doc = _core.tomlkit.parse(original_config) if original_config.strip() else _core.tomlkit.document()
    except Exception:
        return None
    current_present, current_hint = _core._multi_agent_v2_mode_hint(original_doc)
    previous = _core._normalized_managed_subagent_policy(settings.get("managedSubagentPolicy"))
    if previous and current_present and current_hint == previous["appliedHint"]:
        baseline_present = previous["baselinePresent"]
        baseline_hint = previous["baselineHint"]
        baseline_was_bool = previous["baselineWasBool"]
        enabled_present = previous["baselineEnabledPresent"]
        enabled_value = previous["baselineEnabled"]
    else:
        baseline_present = current_present
        baseline_hint = current_hint if current_present else None
        feature = original_doc.get("features", {}).get("multi_agent_v2")
        baseline_was_bool = isinstance(feature, bool)
        enabled_present = baseline_was_bool or (hasattr(feature, "get") and "enabled" in feature)
        enabled_value = feature if baseline_was_bool else feature.get("enabled") if hasattr(feature, "get") else None
    return {
        "appliedHint": desired,
        "baselinePresent": baseline_present,
        "baselineHint": baseline_hint,
        "baselineWasBool": baseline_was_bool,
        "baselineEnabledPresent": bool(enabled_present),
        "baselineEnabled": enabled_value,
    }



def build_codex_config(settings: dict) -> str:
    original = _core.read_toml_text(_core.CONFIG_FILE)
    try:
        doc = _core.tomlkit.parse(original) if original.strip() else _core.tomlkit.document()
    except Exception as exc:
        raise _core.ManagerError(f"无法解析 {_core.CONFIG_FILE}：{exc}") from exc
    workspace = settings.get("modelWorkspace", _core._default_model_workspace())
    runtime_tuning = _core._normalize_runtime_tuning(settings.get("runtimeTuning"))
    proxy_active = bool(settings.get("web2api", {}).get("activeForCodex"))
    records = _core._configuration_model_records(settings)
    child_gateway = _core._subagents_require_shared_gateway(settings)
    native_official_catalog = _core._use_native_official_model_catalog(settings, records)
    main_record = _core._default_main_record(settings, records)
    profile = _core._active_main(settings)
    official_snapshot = bool(main_record and main_record.get("sourceKind") == "account")
    native_official = bool(
        not proxy_active and workspace.get("mode") != "aggregate"
        and (official_snapshot or (not main_record and profile.get("provider") == "openai"))
    )
    if native_official:
        # Explicit application of an official selection must also remove
        # endpoints left by accounts that are no longer in Manager's cards.
        doc.pop("openai_base_url", None)
        existing_providers = doc.get("model_providers")
        if existing_providers is not None:
            existing_providers.pop("openai", None)
        if official_snapshot:
            # The selected credential is an auth.json snapshot. Auto can prefer
            # a different OS-keyring identity; bind this application to file.
            doc["cli_auth_credentials_store"] = "file"
    provider_bridge_url = ""
    # A Manager-applied source must not inherit a second ChatGPT service host.
    # Native defaults still govern cloud/account endpoints.
    doc.pop("chatgpt_base_url", None)
    if main_record:
        doc["model"] = main_record["slug"] if workspace.get("mode") == "aggregate" or proxy_active or child_gateway else main_record["id"]
        if workspace.get("mode") == "aggregate" or proxy_active or child_gateway:
            doc["model_provider"] = _core.AGGREGATE_PROVIDER_ID
        elif main_record["sourceKind"] == "provider":
            provider = _core.provider_by_id(main_record["sourceRecordId"], settings)
            provider_bridge_url = _core._provider_runtime_base_url(provider)
            if not provider_bridge_url:
                raise _core.ManagerError("当前中转站缺少可用的 API 地址。")
            # Codex 0.149+ no longer lets a custom endpoint inherit the API
            # key from the shared auth.json slot.  Keep official ChatGPT auth
            # isolated and route API accounts through their own provider table
            # plus env_key, which is the current documented configuration.
            doc["model_provider"] = str(provider["id"])
            doc.pop("openai_base_url", None)
        else:
            doc.pop("model_provider", None)
    else:
        if child_gateway:
            raise _core.ManagerError("跨账号子代理需要先选择一个可用的主账号或主模型。")
        fallback_model = str(profile.get("model") or "").strip()
        if fallback_model:
            doc["model"] = fallback_model
        else:
            # Let Codex choose its own current default when neither the active
            # account nor the local runtime has supplied a model catalog yet.
            doc.pop("model", None)
        if proxy_active:
            doc["model_provider"] = _core.AGGREGATE_PROVIDER_ID
        elif profile["provider"] == "openai":
            doc.pop("model_provider", None)
        else:
            doc["model_provider"] = profile["provider"]
    _core._set_main_profile_reasoning_effort(doc, settings, profile, main_record)
    # Codex V2 normally derives delegation policy from the main effort
    # (Ultra is proactive; other efforts require an explicit request). Managed
    # strategies supply their selected policy as a custom hint so automatic and
    # manual routing stay stable across efforts. Native mode releases that hint.
    _core._apply_managed_subagent_mode_hint(doc, settings)
    if not provider_bridge_url and str(doc.get("openai_base_url") or "") in _core._managed_provider_base_urls(settings):
        doc.pop("openai_base_url", None)
    _core._apply_root_runtime_tuning(doc, runtime_tuning)
    if workspace.get("syncToCodex", True) and records and not native_official_catalog:
        doc["model_catalog_json"] = str(_core.MODEL_CATALOG_FILE)
    elif str(doc.get("model_catalog_json") or "") == str(_core.MODEL_CATALOG_FILE):
        doc.pop("model_catalog_json", None)

    providers_table = doc.get("model_providers")
    if providers_table is None:
        providers_table = _core.tomlkit.table()
        doc["model_providers"] = providers_table
    current_ids = {item["id"] for item in settings["providers"] if item.get("kind") == "custom"}
    for previous_id in settings.get("managedProviderIds", []):
        if previous_id not in current_ids:
            providers_table.pop(previous_id, None)
    provider_env_keys: set[str] = set()
    for provider in settings["providers"]:
        if provider.get("kind") != "custom":
            continue
        provider_id = str(provider.get("id") or "")
        if _core.slugify(provider_id, "Provider ID") != provider_id:
            raise _core.ManagerError(f"Provider ID 不是规范格式：{provider_id}")
        provider_name = _core._codex_provider_card_name(settings, provider)
        if not provider_name:
            raise _core.ManagerError(f"Provider `{provider_id}` 缺少名称。")
        env_key = _core._validate_provider_env_key(str(provider.get("envKey") or ""))
        normalized_env_key = env_key.casefold()
        if normalized_env_key in provider_env_keys:
            raise _core.ManagerError(f"多个 Provider 共用了环境变量 `{env_key}`，请为每个中转站设置独立变量名。")
        provider_env_keys.add(normalized_env_key)
        table = providers_table.get(provider_id)
        if table is None:
            table = _core.tomlkit.table()
            providers_table[provider_id] = table
        table["name"] = provider_name
        table["base_url"] = _core._provider_runtime_base_url(provider)
        table["env_key"] = env_key
        table["env_key_instructions"] = f"Set {env_key} for {provider_name}."
        table["wire_api"] = "responses"
        _core._clear_provider_auth_overrides(table)
        _core._apply_provider_runtime_tuning(table, runtime_tuning)
    managed_specs = _core._managed_subagent_specs(settings)
    aggregate_needed = workspace.get("mode") == "aggregate" or proxy_active or child_gateway
    if aggregate_needed:
        aggregate = providers_table.get(_core.AGGREGATE_PROVIDER_ID)
        if aggregate is None:
            aggregate = _core.tomlkit.table()
            providers_table[_core.AGGREGATE_PROVIDER_ID] = aggregate
        port = int(settings.get("web2api", {}).get("port", 17860))
        aggregate["name"] = _core._codex_gateway_provider_name(settings, main_record)
        aggregate["base_url"] = f"http://127.0.0.1:{port}/v1"
        aggregate["env_key"] = _core.AGGREGATE_ENV_KEY
        aggregate["env_key_instructions"] = "Agent Manager 会在应用配置时自动同步此本地密钥。"
        aggregate["wire_api"] = "responses"
        _core._clear_provider_auth_overrides(aggregate)
        _core._apply_provider_runtime_tuning(aggregate, runtime_tuning, aggregate=True)
    elif _core.AGGREGATE_PROVIDER_ID in providers_table:
        providers_table.pop(_core.AGGREGATE_PROVIDER_ID, None)

    agents_table = doc.get("agents")
    if agents_table is None and managed_specs:
        agents_table = _core.tomlkit.table()
        doc["agents"] = agents_table
    # 5.7.x wrote a distinctive fixed 3-child / depth-1 policy (plus a V2
    # four-slot hint) into every config.  Merely stopping future writes would
    # leave existing installs capped forever, so remove that exact legacy
    # manager-owned combination once.  Other user/runtime values are retained.
    managed_names = {str(item) for item in settings.get("managedAgentNames", [])}
    legacy_manager_evidence = bool(
        agents_table
        and any(
            _core._manager_owned_agent_table(name, table, managed_names)
            for name, table in agents_table.items()
        )
    )
    legacy_fixed_policy = legacy_manager_evidence and (
        int(agents_table.get("max_concurrent_threads_per_session", 0) or 0) == 3
        and int(agents_table.get("max_depth", 0) or 0) == 1
    )
    if legacy_fixed_policy:
        agents_table.pop("max_concurrent_threads_per_session", None)
        agents_table.pop("max_depth", None)
        features_table = doc.get("features")
        multi_agent_v2 = (
            features_table.get("multi_agent_v2")
            if features_table is not None and hasattr(features_table, "get")
            else None
        )
        if (
            multi_agent_v2 is not None
            and hasattr(multi_agent_v2, "get")
            and int(multi_agent_v2.get("max_concurrent_threads_per_session", 0) or 0) == 4
        ):
            multi_agent_v2.pop("max_concurrent_threads_per_session", None)
    # Concurrency and nesting are runtime/user policy, not generated routing
    # data.  Preserve both the current and legacy Codex keys verbatim.  The
    # lifecycle block below prevents duplicate/stale work without silently
    # forcing every machine to the same fixed child count.
    current_names = {spec["name"] for spec in managed_specs}
    if agents_table is not None:
        for previous_name in list(agents_table.keys()):
            if (
                previous_name not in current_names
                and _core._manager_owned_agent_table(
                    previous_name,
                    agents_table.get(previous_name),
                    managed_names,
                )
            ):
                agents_table.pop(previous_name, None)
    for spec in managed_specs:
        table = agents_table.get(spec["name"])
        if table is None:
            table = _core.tomlkit.table()
            agents_table[spec["name"]] = table
        elif not _core._manager_owned_agent_table(spec["name"], table, managed_names):
            raise _core.ManagerError(
                f"Codex 已存在用户管理的 Agent `{spec['name']}`；"
                "Agent Manager 不会覆盖它，请先为该用户 Agent 改名。"
            )
        table["description"] = spec["description"]
        table["config_file"] = str(spec["path"])
    if agents_table is not None and not agents_table:
        doc.pop("agents", None)
    return _core.tomlkit.dumps(doc)



def build_routing_block(settings: dict) -> str:
    routing = settings.get("subagentRouting", _core._default_subagent_routing())
    strategy = next(
        (item for item in settings.get("strategies", []) if item.get("id") == routing.get("strategyId")),
        _core._active_strategy(settings),
    )
    specs = _core._managed_subagent_specs(settings)
    tuning = _core._normalize_runtime_tuning(settings.get("runtimeTuning"))
    if _core._uses_codex_native_subagent_policy(settings):
        # Absence is intentional: Codex can apply its own effort-derived
        # policy, including native proactive behavior at Ultra.
        return ""
    lines = [
        _core.MANAGED_BLOCK_START,
        "## Agent Manager routing",
        "",
        f"Active delegation policy: **{strategy['name']}** (`{strategy['id']}`).",
        "",
        "### Benefit-gated invocation protocol",
        "",
        "When the current Codex mode, the user, and applicable instructions allow subagents, follow this protocol:",
        "1. Apply the benefit gate below. Keep the work local when delegation would use more context, latency, or verification than it saves.",
        "2. Classify only the bounded work unit as simple, normal, hard, or expert using the policy below.",
        "3. Invoke the first configured Agent for that level. Do not invoke multiple Agents for the same simple/normal unit.",
        "4. If that Agent cannot start because its model/provider is unavailable, try the next fallback; never retry a completed result.",
        "   Explicit pre-start provider capacity, model-not-found/unsupported, provider-auth, and provider-rate-limit errors are availability failures. Mark that attempt terminal and consume its error before trying exactly the next configured fallback. A local agent-limit is different: drain existing children and use the released runtime capacity; do not cycle providers to evade it. An uncertain spawn outcome must be reconciled before another attempt. Do not misclassify a worker's task-quality failure as availability.",
        "5. A task-quality failure is not an availability failure: return it to the primary agent instead of looping.",
        "6. The primary agent keeps the goal and performs final integration and verification.",
        "",
        "### Editable call strategy",
        "",
        str(routing.get("prompt") or strategy.get("instructions") or _core.DEFAULT_CALL_STRATEGY_PROMPT).strip(),
    ]
    planning_mode = tuning.get("planningMode") if tuning.get("planningManaged") else "auto"
    if planning_mode == "clarify":
        lines.extend(
            [
                "",
                "### Planning interaction",
                "",
                "Before implementation, inspect the available context and identify choices that would materially change the result. Ask the user to choose only when such a choice remains unresolved; otherwise state the assumption briefly and proceed. Do not turn routine or reversible details into blocking questions.",
            ]
        )
    elif planning_mode == "plan_first":
        lines.extend(
            [
                "",
                "### Planning interaction",
                "",
                "For implementation work, first inspect the relevant repository context and form a short executable plan. Ask the user to choose among options only when the decision materially changes scope, compatibility, risk, or user-visible behavior. After the choice or a safe assumption, execute the plan through verification instead of stopping at the plan.",
            ]
        )
    lines.extend(
        [
        "",
        "### Subagent lifecycle safety",
        "",
        _core.SUBAGENT_LIFECYCLE_SAFETY,
        "",
        "### Ordered difficulty routes",
        ]
    )
    for level in _core.DIFFICULTIES:
        meta = _core.DIFFICULTY_META[level]
        level_specs = [item for item in specs if item["level"] == level]
        lines.extend(["", f"- **{meta['name']} (`{level}`)**: {meta['description']}"])
        if not level_specs:
            legacy_route = settings.get("routes", {}).get(level, {})
            legacy_agents = legacy_route.get("agents", []) if legacy_route.get("enabled") else []
            if legacy_agents:
                for name in legacy_agents:
                    lines.append(f"  - Fallback 1: invoke legacy Agent `{name}`.")
            else:
                lines.append("  - No model is configured; keep this work in the primary agent.")
            continue
        for spec in level_specs:
            effort_label = f"reasoning `{spec['effort']}`" if spec.get("effort") else "reasoning `model default`"
            lines.append(
                f"  - Fallback {spec['slot']}: invoke `{spec['name']}` — {spec['sourceName']} / "
                f"`{spec['model']}` / {effort_label}."
            )
    lines.extend(
        [
            "",
            "Do not silently substitute an Agent from another difficulty. After all configured fallbacks fail to start, keep the work in the primary agent and report the constraint.",
            _core.MANAGED_BLOCK_END,
        ]
    )
    return "\n".join(lines).strip() + "\n"



def build_agents_file(settings: dict) -> str:
    original = _core.AGENTS_FILE.read_text(encoding="utf-8") if _core.AGENTS_FILE.exists() else ""
    pattern = _core.re.compile(
        _core.re.escape(_core.MANAGED_BLOCK_START) + r".*?" + _core.re.escape(_core.MANAGED_BLOCK_END) + r"\s*",
        _core.re.DOTALL,
    )
    if _core._uses_codex_native_subagent_policy(settings) and not pattern.search(original) and _core.LEGACY_AGENTS_TEXT not in original:
        return original
    base = pattern.sub("", original).strip()
    if _core.LEGACY_AGENTS_TEXT in base:
        base = base.replace(_core.LEGACY_AGENTS_TEXT, "").strip()
    block = _core.build_routing_block(settings).strip()
    if not block:
        return (base + "\n") if base else ""
    return ((base + "\n\n") if base else "") + block + "\n"



def preview_apply() -> dict:
    settings = _core.load_settings()
    config_before = _core.read_toml_text(_core.CONFIG_FILE)
    agents_before = _core.AGENTS_FILE.read_text(encoding="utf-8") if _core.AGENTS_FILE.exists() else ""
    config_after = _core.build_codex_config(settings)
    agents_after = _core.build_agents_file(settings)
    diff = "".join(
        _core.difflib.unified_diff(
            config_before.splitlines(keepends=True),
            config_after.splitlines(keepends=True),
            fromfile=str(_core.CONFIG_FILE),
            tofile=str(_core.CONFIG_FILE),
        )
    )
    diff += "".join(
        _core.difflib.unified_diff(
            agents_before.splitlines(keepends=True),
            agents_after.splitlines(keepends=True),
            fromfile=str(_core.AGENTS_FILE),
            tofile=str(_core.AGENTS_FILE),
        )
    )
    return {"diff": diff or "没有待应用的变化。", "changed": config_before != config_after or agents_before != agents_after}

