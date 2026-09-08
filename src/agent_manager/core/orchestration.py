"""Orchestration services."""
from __future__ import annotations
from agent_manager import core as _core


def _normalize_subagent_routing_update(settings: dict, payload: dict) -> tuple[dict, str]:
    if not isinstance(payload, dict):
        raise _core.ManagerError("子代理路由格式无效。")
    model_records = {item["key"]: item for item in _core._all_model_records(settings)}
    known_keys = set(model_records)
    capabilities = _core._reasoning_capabilities()
    current = _core._json_clone(settings.get("subagentRouting", _core._default_subagent_routing()))
    strategy_id = str(payload.get("strategyId") or current.get("strategyId") or "adaptive")
    strategy = next(
        (item for item in settings.get("strategies", []) if item.get("id") == strategy_id),
        None,
    )
    if strategy is None:
        raise _core.ManagerError("调用策略不存在。")
    prompt = str(payload.get("prompt") or "").strip()
    if strategy_id != "parallel_first":
        prompt = str(strategy.get("instructions") or "").strip()
    if not prompt:
        raise _core.ManagerError("调用策略提示词不能为空。")
    if len(prompt) > 20_000:
        raise _core.ManagerError("调用策略提示词不能超过 20,000 个字符。")
    raw_routes = payload.get("routes", {})
    if not isinstance(raw_routes, dict):
        raise _core.ManagerError("难度路由格式无效。")
    routes = {}
    for level in _core.DIFFICULTIES:
        route = raw_routes.get(level, {})
        models = route.get("models", []) if isinstance(route, dict) else []
        efforts = route.get("efforts", []) if isinstance(route, dict) else []
        if not isinstance(models, list):
            raise _core.ManagerError(f"{_core.DIFFICULTY_META[level]['name']}路由格式无效。")
        if not isinstance(efforts, list):
            raise _core.ManagerError(f"{_core.DIFFICULTY_META[level]['name']}思考程度格式无效。")
        values: list[str] = []
        normalized_efforts: list[str] = []
        for index, item in enumerate(models):
            model_key = str(item).strip()
            if not model_key or model_key in values:
                continue
            requested_effort = str(efforts[index] or "").strip() if index < len(efforts) else ""
            if requested_effort and requested_effort not in _core.VALID_EFFORTS:
                raise _core.ManagerError(f"{_core.DIFFICULTY_META[level]['name']}路由包含无效的思考程度。")
            values.append(model_key)
            normalized_efforts.append(requested_effort)
            if len(values) == 3:
                break
        missing = [item for item in values if item not in known_keys]
        if missing:
            raise _core.ManagerError(f"{_core.DIFFICULTY_META[level]['name']}路由包含已不存在的模型。")
        for model_key, requested_effort in zip(values, normalized_efforts, strict=True):
            if not requested_effort:
                continue
            record = model_records[model_key]
            model_id = str(record.get("id") or "")
            capability = (
                record
                if record.get("reasoningKnown")
                else capabilities.get(model_id)
                if record.get("sourceKind") == "account"
                else None
            )
            supported = capability.get("efforts", []) if capability else []
            if capability is not None and requested_effort not in supported:
                label = str(record.get("name") or model_id)
                raise _core.ManagerError(f"模型 {label} 不支持思考程度 {requested_effort}。")
        routes[level] = {"models": values, "efforts": normalized_efforts}
    return (
        {
            **current,
            "advanced": bool(payload.get("advanced", current.get("advanced", False))),
            "strategyId": strategy_id,
            "prompt": prompt,
            "routes": routes,
        },
        strategy_id,
    )



def save_subagent_routing(payload: dict) -> dict:
    with _core.SWITCH_OPERATION_LOCK, _core.SETTINGS_LOCK, _core._settings_file_lock():
        settings = _core.load_settings()
        routing, strategy_id = _core._normalize_subagent_routing_update(settings, payload)
        settings["subagentRouting"] = routing
        settings["activeStrategyId"] = strategy_id
        _core.save_settings(settings)
        return routing



def _capture_orchestration_transaction_snapshot(settings: dict) -> dict:
    paths = [
        _core.SETTINGS_FILE,
        _core.SECRETS_FILE,
        _core.RUNTIME_OVERLAY_FILE,
        *(path for path, _kind in _core._runtime_overlay_targets()),
    ]
    unique_paths = list(dict.fromkeys(_core.Path(path) for path in paths))
    files = _core._capture_file_bytes(unique_paths)
    try:
        environment = {
            name: _core._read_user_environment(name)
            for name in _core._switch_environment_names(settings)
        }
    except Exception as exc:
        raise _core.ManagerError(f"无法完整读取编排保存前的环境变量：{exc}") from exc
    return {
        "files": files,
        "environment": environment,
        "settingsDocument": settings,
        "settingsValue": _core._json_clone(settings),
    }



def _preflight_orchestration_artifacts(settings: dict) -> None:
    config = _core.build_codex_config(settings)
    _core.tomllib.loads(config)
    _core.build_agents_file(settings).encode("utf-8")
    records = _core._configuration_model_records(settings)
    workspace = settings.get("modelWorkspace", _core._default_model_workspace())
    if workspace.get("syncToCodex", True) and records and not _core._use_native_official_model_catalog(settings, records):
        catalog, _catalog_records = _core.build_synced_model_catalog(settings)
        _core.json.dumps(catalog, ensure_ascii=False)
    for spec in _core._managed_subagent_specs(settings):
        _core.tomllib.loads(_core._render_managed_agent(spec))



def save_orchestration_and_apply(
    payload: dict,
    *,
    ensure_gateway: _core.Any = None,
) -> dict:
    """Validate and commit the orchestration page as one recoverable unit."""

    if not isinstance(payload, dict):
        raise _core.ManagerError("编排保存请求必须是对象。")
    workspace_payload = payload.get("modelWorkspace")
    routing_payload = payload.get("subagentRouting")
    tuning_payload = payload.get("runtimeTuning")
    config_payload = payload.get("codexConfig")
    if not isinstance(workspace_payload, dict):
        raise _core.ManagerError("编排保存缺少有效的主模型设置。")
    if not isinstance(routing_payload, dict):
        raise _core.ManagerError("编排保存缺少有效的子代理路由。")
    if tuning_payload is not None and not isinstance(tuning_payload, dict):
        raise _core.ManagerError("Codex 运行参数格式无效。")
    if config_payload is not None and not isinstance(config_payload, dict):
        raise _core.ManagerError("Codex 配置保存请求无效。")
    if ensure_gateway is not None and not callable(ensure_gateway):
        raise _core.ManagerError("网关启动回调无效。")

    with (
        _core.SWITCH_OPERATION_LOCK,
        _core.CONFIG_FILE_LOCK,
        _core.RUNTIME_OVERLAY_LOCK,
        _core.SETTINGS_LOCK,
        _core.SECRETS_LOCK,
        _core._settings_file_lock(),
    ):
        raw_settings, _migrated = _core._read_and_migrate_settings_locked()
        settings = _core.SettingsDocument(_core._json_clone(raw_settings), baseline=raw_settings)
        workspace = _core._normalize_model_workspace_update(settings, workspace_payload)
        tuning = _core._normalize_runtime_tuning_update(settings, tuning_payload or {})
        routing, strategy_id = _core._normalize_subagent_routing_update(settings, routing_payload)
        if config_payload is not None:
            previous_raw, proposed_text, _encoded = _core._prepare_codex_config_document(config_payload)
            proposed_common, edited_fields = _core._config_runtime_edits(previous_raw, proposed_text)
            conflicts = {
                field for field in edited_fields
                if field in (tuning_payload or {}) and tuning.get(field) != proposed_common.get(field)
            }
            if conflicts:
                raise _core.ManagerError("可视化运行参数与 TOML 草稿存在冲突，请保留一种修改后再保存。")
            # An explicit TOML edit relinquishes the previous UI override.
            # Otherwise Apply would silently overwrite what the user just saved.
            released = edited_fields.difference(tuning_payload or {})
            for field in released:
                tuning[field] = proposed_common[field]
            tuning["managedFields"] = [field for field in tuning["managedFields"] if field not in released]
            tuning["configManaged"] = bool(tuning["managedFields"])

        candidate = _core.SettingsDocument(
            _core._json_clone(settings),
            baseline=getattr(settings, "_baseline", settings),
        )
        candidate["modelWorkspace"] = workspace
        candidate["runtimeTuning"] = tuning
        candidate["subagentRouting"] = routing
        candidate["activeStrategyId"] = strategy_id
        _core._preflight_orchestration_artifacts(candidate)
        orchestration_settings_changed = any(
            candidate.get(key) != settings.get(key)
            for key in ("modelWorkspace", "runtimeTuning", "subagentRouting", "activeStrategyId")
        )

        snapshot = _core._capture_orchestration_transaction_snapshot(settings)
        retained_backups = []
        try:
            for path in (_core.SETTINGS_FILE, _core.SECRETS_FILE):
                backup = _core.backup_file(path)
                if backup:
                    retained_backups.append(str(backup))
            config_result = (
                _core._save_codex_config_document_locked(config_payload, sync_runtime_ownership=False)
                if config_payload is not None
                else None
            )
            result = _core._apply_configuration_locked(settings=candidate)
            requested_config_changed = bool(config_result and config_result.get("changed"))
            if orchestration_settings_changed or requested_config_changed:
                result["changed"] = True
                result["restartRequired"] = True
            result["backups"] = list(
                dict.fromkeys(
                    [
                        *retained_backups,
                        *([str(config_result["backupPath"])] if config_result and config_result.get("backupPath") else []),
                        *result.get("backups", []),
                    ]
                )
            )
            document = _core.codex_config_document()
            response = {
                "result": result,
                "runtimeTuning": tuning,
                "document": document,
            }
            if result.get("gatewayRequired") and ensure_gateway is not None:
                ensure_gateway()
            return response
        except BaseException as exc:
            rollback_errors = _core._restore_switch_transaction_snapshot(
                snapshot,
                process_state_checked=True,
            )
            if not isinstance(exc, Exception):
                raise
            detail = f"；回滚异常：{'；'.join(rollback_errors)}" if rollback_errors else ""
            raise _core.ManagerError(f"编排保存失败：{exc}{detail}") from exc



def restore_orchestration_defaults() -> dict:
    settings = _core.load_settings()
    sources = _core.model_sources(settings)
    existing_active = str(settings.get("modelWorkspace", {}).get("activeSourceId") or "")
    active_source = next((item for item in sources if item.get("id") == existing_active), None)
    if not active_source:
        active_source = next((item for item in sources if item.get("active")), None)
    if not active_source:
        active_source = next((item for item in sources if item.get("available")), None)
    workspace = _core._default_model_workspace()
    if active_source:
        workspace["activeSourceId"] = str(active_source.get("id") or "")
        models = active_source.get("models") if isinstance(active_source.get("models"), list) else []
        workspace["defaultModelKey"] = str(models[0].get("key") or "") if models else ""
    routing = _core._default_subagent_routing()
    settings["modelWorkspace"] = workspace
    settings["subagentRouting"] = routing
    settings["activeStrategyId"] = routing["strategyId"]
    # "Restore defaults" must be self-contained.  Older installations can
    # retain a legacy custom main profile (for example codex_local_access)
    # whose secret never existed on a second computer.  Falling back to that
    # profile makes a safe reset fail with a misleading API-key error.
    profiles = [item for item in settings.get("mainProfiles", []) if isinstance(item, dict)]
    profile = next((item for item in profiles if item.get("provider") == "openai"), None)
    if profile is None:
        profile = next((item for item in profiles if item.get("id") == "current"), None)
    if profile is None:
        profile = {"id": "current", "name": "当前主模型"}
        profiles.append(profile)
    source_models = active_source.get("models", []) if active_source else []
    source_model_id = (
        str(source_models[0].get("id") or "")
        if source_models and isinstance(source_models[0], dict)
        else ""
    )
    preserved_model = str(profile.get("model") or "")
    if preserved_model.startswith("cam-"):
        preserved_model = ""
    profile.update(
        {
            "name": str(profile.get("name") or "当前主模型"),
            "provider": "openai",
            "model": source_model_id or preserved_model,
            "effort": (
                str(profile.get("effort"))
                if str(profile.get("effort")) in _core.VALID_EFFORTS
                else ""
            ),
        }
    )
    settings["mainProfiles"] = profiles
    settings["activeMainProfileId"] = str(profile["id"])
    web2api = settings.setdefault("web2api", _core._default_web2api_settings())
    web2api["activeForCodex"] = False
    web2api["activeAccountId"] = None
    _core.save_settings(settings)
    return {"modelWorkspace": workspace, "subagentRouting": routing}



def save_app_behavior(payload: dict) -> dict:
    with _core.SETTINGS_LOCK, _core._settings_file_lock():
        return _core._save_app_behavior_locked(payload)



def _save_app_behavior_locked(payload: dict) -> dict:
    settings = _core.load_settings()
    behavior = settings.setdefault("appBehavior", _core._default_app_behavior())
    if "usageRange" in payload:
        behavior["usageRange"] = _core._normalize_usage_range(payload["usageRange"], strict=True)
    if "appearance" in payload:
        if payload["appearance"] not in ("light", "dark", "system"):
            raise _core.ManagerError("外观仅支持浅色、深色或跟随系统。")
        behavior["appearance"] = payload["appearance"]
    if "closeToTray" in payload:
        behavior["closeToTray"] = bool(payload.get("closeToTray"))
    if "radarMonitoring" in payload:
        if not isinstance(payload["radarMonitoring"], bool):
            raise _core.ManagerError("后台雷达监测开关必须是布尔值。")
        behavior["radarMonitoring"] = payload["radarMonitoring"]
    if "quotaRefreshMinutes" in payload:
        try:
            quota_refresh_minutes = int(payload.get("quotaRefreshMinutes"))
        except (TypeError, ValueError) as exc:
            raise _core.ManagerError("额度自动刷新间隔无效。") from exc
        if quota_refresh_minutes not in _core.VALID_QUOTA_REFRESH_MINUTES:
            allowed = "、".join(str(item) for item in _core.VALID_QUOTA_REFRESH_MINUTES)
            raise _core.ManagerError(f"额度自动刷新间隔仅支持 {allowed} 分钟。")
        behavior["quotaRefreshMinutes"] = quota_refresh_minutes
    if "mailHealthCheckHours" in payload:
        try:
            mail_health_hours = int(payload.get("mailHealthCheckHours"))
        except (TypeError, ValueError) as exc:
            raise _core.ManagerError("邮箱自动健康检查间隔无效。") from exc
        if mail_health_hours not in _core.VALID_MAIL_HEALTH_CHECK_HOURS:
            allowed = "、".join(str(item) for item in _core.VALID_MAIL_HEALTH_CHECK_HOURS)
            raise _core.ManagerError(f"邮箱自动健康检查间隔仅支持 {allowed} 小时。")
        behavior["mailHealthCheckHours"] = mail_health_hours
    _core.save_settings(settings)
    return behavior

