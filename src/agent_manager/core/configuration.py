"""Configuration services."""
from __future__ import annotations
from agent_manager import core as _core


def apply_configuration(sync_secrets: bool = False) -> dict:
    # Coordinate ordinary Apply with account switching, direct TOML edits and
    # restore points. Their writes must not interleave between model/Agent files.
    with _core.SWITCH_OPERATION_LOCK, _core.CONFIG_FILE_LOCK:
        return _core._apply_configuration_locked(sync_secrets)



def _apply_configuration_locked(
    sync_secrets: bool = False,
    *,
    settings: dict | None = None,
) -> dict:
    if settings is None:
        settings = _core.load_settings()
    secrets_to_sync = []
    workspace = settings.get("modelWorkspace", _core._default_model_workspace())
    proxy_active = bool(settings.get("web2api", {}).get("activeForCodex"))
    records = _core._configuration_model_records(settings)
    native_official_catalog = _core._use_native_official_model_catalog(settings, records)
    specs = _core._managed_subagent_specs(settings)
    aggregate_needed = workspace.get("mode") == "aggregate" or proxy_active or any(spec.get("routingMode") == "gateway" for spec in specs)
    if aggregate_needed:
        if not _core.service_secret_configured("web2api"):
            _core.rotate_web2api_key()
        aggregate_key = _core.ensure_internal_gateway_secret()
        secrets_to_sync.append(
            (_core._validate_provider_env_key(_core.AGGREGATE_ENV_KEY, allow_internal=True), aggregate_key)
        )
        settings.setdefault("web2api", _core._default_web2api_settings())["enabled"] = True
    main_record = _core._default_main_record(settings, records)
    provider_bridge = bool(
        main_record
        and main_record.get("sourceKind") == "provider"
        and workspace.get("mode") == "independent"
        and not proxy_active
    )
    if main_record and main_record.get("sourceKind") == "provider":
        provider = _core.provider_by_id(main_record["sourceRecordId"], settings)
        key = _core.load_provider_key(provider["id"], required=True)
        secrets_to_sync.append((_core._validate_provider_env_key(provider["envKey"]), key or ""))
    elif not main_record:
        legacy_profile = _core._active_main(settings)
        if legacy_profile.get("provider") != "openai":
            provider = _core.provider_by_id(legacy_profile["provider"], settings)
            key = _core.load_provider_key(provider["id"], required=True)
            secrets_to_sync.append((_core._validate_provider_env_key(provider["envKey"]), key or ""))
    if sync_secrets:
        for provider in settings.get("providers", []):
            if provider.get("kind") == "custom" and _core.provider_key_configured(provider["id"]):
                secrets_to_sync.append(
                    (
                        _core._validate_provider_env_key(provider["envKey"]),
                        _core.load_provider_key(provider["id"]) or "",
                    )
                )
    overlay_targets = _core._runtime_overlay_targets()
    overlay_environment = list(dict(secrets_to_sync))
    _core._runtime_overlay_capture_files(overlay_targets)
    _core._runtime_overlay_capture_environment(overlay_environment)
    config_after = _core.build_codex_config(settings)
    agents_after = _core.build_agents_file(settings)
    config_before = _core.read_toml_text(_core.CONFIG_FILE)
    agents_before = _core.AGENTS_FILE.read_text(encoding="utf-8") if _core.AGENTS_FILE.exists() else ""
    next_subagent_policy = _core._next_managed_subagent_policy(settings, config_before)
    backups = []
    written_agents = []
    removed_agents = []
    catalog_changed = False
    environment_changed = False
    if workspace.get("syncToCodex", True) and records and not native_official_catalog:
        catalog, _ = _core.build_synced_model_catalog(settings)
        catalog_text = _core.json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
        before = _core.MODEL_CATALOG_FILE.read_text(encoding="utf-8") if _core.MODEL_CATALOG_FILE.exists() else ""
        if before != catalog_text:
            catalog_changed = True
            backup = _core.backup_file(_core.MODEL_CATALOG_FILE)
            if backup:
                backups.append(str(backup))
            _core.atomic_write_text(_core.MODEL_CATALOG_FILE, catalog_text)
            _core._runtime_overlay_record_applied(paths=[_core.MODEL_CATALOG_FILE])
    for spec in specs:
        content = _core._render_managed_agent(spec)
        _core.tomllib.loads(content)
        before = spec["path"].read_text(encoding="utf-8") if spec["path"].exists() else ""
        if before != content:
            backup = _core.backup_file(spec["path"])
            if backup:
                backups.append(str(backup))
            _core.atomic_write_text(spec["path"], content)
            _core._runtime_overlay_record_applied(paths=[spec["path"]])
            written_agents.append(str(spec["path"]))
    active_agent_paths = {spec["path"].resolve() for spec in specs}
    for candidate, kind in overlay_targets:
        if kind != "managed_agent" or candidate.resolve() in active_agent_paths or not candidate.is_file():
            continue
        current = candidate.read_bytes()
        if not _core._looks_like_managed_agent(current):
            continue
        backup = _core.backup_file(candidate)
        if backup:
            backups.append(str(backup))
        candidate.unlink(missing_ok=True)
        _core._runtime_overlay_record_applied(paths=[candidate])
        removed_agents.append(str(candidate))
    if config_before != config_after:
        backup = _core.backup_file(_core.CONFIG_FILE)
        if backup:
            backups.append(str(backup))
        _core.atomic_write_text(_core.CONFIG_FILE, config_after)
        _core._runtime_overlay_record_applied(paths=[_core.CONFIG_FILE])
    if agents_before != agents_after:
        backup = _core.backup_file(_core.AGENTS_FILE)
        if backup:
            backups.append(str(backup))
        _core.atomic_write_text(_core.AGENTS_FILE, agents_after)
        _core._runtime_overlay_record_applied(paths=[_core.AGENTS_FILE])
    synced = []
    for env_key, secret in dict(secrets_to_sync).items():
        _core._validate_provider_env_key(
            env_key,
            allow_internal=env_key.upper() == _core.AGGREGATE_ENV_KEY,
        )
        if _core._read_user_environment(env_key) != secret:
            environment_changed = True
            _core._sync_user_environment(env_key, secret)
        _core._runtime_overlay_record_applied(environment=[env_key])
        synced.append(env_key)
    cleared_environment = _core._clear_inactive_provider_environment_overrides(settings, synced)
    if cleared_environment:
        environment_changed = True
    _core._runtime_overlay_record_applied(
        paths=[path for path, _ in overlay_targets],
        environment=overlay_environment,
    )
    settings["managedProviderIds"] = sorted(
        item["id"] for item in settings["providers"] if item.get("kind") == "custom"
    )
    settings["managedAgentNames"] = [spec["name"] for spec in specs]
    settings["managedSubagentPolicy"] = next_subagent_policy
    active_source_id = str(workspace.get("activeSourceId") or "")
    if active_source_id.startswith("provider:"):
        active_provider_id = active_source_id.split(":", 1)[1]
        active_provider = next(
            (
                item
                for item in settings.get("providers", [])
                if isinstance(item, dict)
                and item.get("kind") == "custom"
                and str(item.get("id") or "") == active_provider_id
            ),
            None,
        )
        if active_provider:
            runtime_revision, _applied_revision = _core._provider_runtime_revisions(active_provider)
            active_provider["runtimeRevision"] = runtime_revision
            active_provider["appliedRuntimeRevision"] = runtime_revision
    settings["lastAppliedAt"] = _core.now_iso()
    settings["lastAppliedSummary"] = {
        "mainProfileId": settings["activeMainProfileId"],
        "mode": workspace.get("mode"),
        "models": len(records),
        "nativeOfficialCatalog": native_official_catalog,
        "strategyId": settings["activeStrategyId"],
        "managedAgents": len(specs),
    }
    _core.save_settings(settings)
    # Also migrate/prune known legacy config history on an unchanged Apply.
    import agent_manager.config.backups
    agent_manager.config.backups.prune_automatic()
    changed = (
        config_before != config_after
        or agents_before != agents_after
        or bool(written_agents)
        or bool(removed_agents)
        or catalog_changed
        or environment_changed
    )
    return {
        "changed": changed,
        "restartRequired": changed,
        "backups": backups,
        "syncedEnvKeys": synced,
        "clearedEnvKeys": cleared_environment,
        "managedAgents": written_agents,
        "removedManagedAgents": removed_agents,
        "modelCatalog": (
            str(_core.MODEL_CATALOG_FILE)
            if records and not native_official_catalog
            else None
        ),
        "nativeOfficialCatalog": native_official_catalog,
        "gatewayRequired": aggregate_needed,
        "apiBridge": provider_bridge,
    }



def configuration_status(settings: dict | None = None) -> dict:
    # Apply writes several files under this lock. A health read must observe
    # either side of that transaction, never its partially written contents.
    with _core.CONFIG_FILE_LOCK:
        return _configuration_status_locked(settings)


def _configuration_status_locked(settings: dict | None = None) -> dict:
    settings = settings or _core.load_settings()
    config = _core.read_toml(_core.CONFIG_FILE)
    expected = _core.tomllib.loads(_core.build_codex_config(settings))
    main_active = (
        config.get("model") == expected.get("model")
        and config.get("model_reasoning_effort") == expected.get("model_reasoning_effort")
        and (config.get("model_provider") or "openai") == (expected.get("model_provider") or "openai")
        and config.get("openai_base_url") == expected.get("openai_base_url")
        and config.get("model_catalog_json") == expected.get("model_catalog_json")
    )
    agents_text = _core.AGENTS_FILE.read_text(encoding="utf-8") if _core.AGENTS_FILE.exists() else ""
    # Native policy legitimately has no managed block. Compare the rendered
    # result instead of requiring a marker (or accepting any stale same-ID
    # block after its routes have been edited).
    strategy_active = agents_text == _core.build_agents_file(settings)
    workspace = settings.get("modelWorkspace", _core._default_model_workspace())
    selected_records = _core.selected_model_records(settings)
    context_active = config.get("model_context_window") == expected.get("model_context_window")
    tuning = _core._normalize_runtime_tuning(settings.get("runtimeTuning"))
    expected_ceilings = {
        str(record.get("slug") or record.get("id")): ceiling
        for record in selected_records
        if (ceiling := _core._managed_context_catalog_ceiling(record, tuning))
    }
    if expected_ceilings and config.get("model_catalog_json") == str(_core.MODEL_CATALOG_FILE):
        actual_catalog = _core.read_json(_core.MODEL_CATALOG_FILE, {})
        catalog_items = actual_catalog.get("models") if isinstance(actual_catalog, dict) else None
        actual_models = {
            str(item.get("slug")): item for item in catalog_items
            if isinstance(item, dict)
        } if isinstance(catalog_items, list) else {}
        for slug, ceiling in expected_ceilings.items():
            current_max = actual_models.get(slug, {}).get("max_context_window")
            if isinstance(current_max, bool) or not isinstance(current_max, int) or current_max < ceiling:
                context_active = False
                break
    return {
        "mainActive": main_active,
        "strategyActive": strategy_active,
        "contextActive": context_active,
        "fullyApplied": main_active and strategy_active and context_active,
        "mode": workspace.get("mode", "independent"),
        "activeSourceId": str(next((item["sourceId"] for item in selected_records), workspace.get("activeSourceId") or "")),
        "modelCount": len(selected_records),
        "modelCountScope": "all_sources" if workspace.get("mode") == "aggregate" else "current_account",
    }



def runtime_model_health(settings: dict | None = None, *, max_pages: int = 5) -> dict:
    """Validate the configured main model against Codex's effective catalog."""
    settings = settings or _core.load_settings()
    process_scan = _core.running_codex_processes()
    process_scan_known = bool(getattr(process_scan, "known", True))
    codex_running = len(process_scan) > 0
    workspace = settings.get("modelWorkspace", _core._default_model_workspace())
    proxy_active = bool(settings.get("web2api", {}).get("activeForCodex"))
    records = _core.web2api_pool_model_records(settings) if proxy_active else _core.selected_model_records(settings)
    if not records:
        return {
            "status": "warning",
            "healthy": False,
            "recoverable": False,
            "configuredModel": None,
            "detail": "当前选择范围没有可用模型；请刷新账号或中转站后重新选择。",
        }
    use_alias = workspace.get("mode") == "aggregate" or proxy_active
    key_by_runtime_id = {
        str((item.get("slug") if use_alias else item.get("id")) or ""): str(item.get("key") or "")
        for item in records
        if str((item.get("slug") if use_alias else item.get("id")) or "").strip()
        and str(item.get("key") or "").strip()
    }
    configured = str(_core.read_toml(_core.CONFIG_FILE).get("model") or "").strip()
    if not configured:
        return {
            "status": "warning",
            "healthy": False,
            "recoverable": False,
            "configuredModel": None,
            "detail": "当前没有可验证的默认模型；请先刷新目标账号或中转站的模型目录。",
        }
    runtime_models: list[dict] = []
    cursor = None
    try:
        for _ in range(max(1, min(int(max_pages), 10))):
            result = _core.codex_app_server_request(
                "model/list",
                {"cursor": cursor, "limit": 100},
                timeout=20,
            )
            page = result.get("data") if isinstance(result, dict) else None
            if not isinstance(page, list):
                raise _core.ManagerError("Codex 模型目录返回格式无效。")
            runtime_models.extend(item for item in page if isinstance(item, dict))
            cursor = result.get("nextCursor") or result.get("next_cursor")
            if not cursor:
                break
    except Exception as exc:
        return {
            "status": "ok",
            "healthy": True,
            "recoverable": False,
            "checked": False,
            "configuredModel": configured,
            "detail": (
                "本次未能读取 Codex 运行时模型目录，已保留当前模型且不据此判定故障："
                f"{_core._redact_sensitive_text(exc, limit=200)}"
            ),
        }
    visible_ids = {
        str(item.get("id") or item.get("model") or item.get("slug") or "").strip()
        for item in runtime_models
        if str(item.get("id") or item.get("model") or item.get("slug") or "").strip()
    }
    if not visible_ids:
        return {
            "status": "ok",
            "healthy": True,
            "recoverable": False,
            "checked": False,
            "configuredModel": configured,
            "visibleModels": 0,
            "detail": "Codex 本次返回了空模型目录；已保留当前模型，不执行自动切换。",
        }
    if configured in visible_ids and configured in key_by_runtime_id:
        return {
            "status": "ok",
            "healthy": True,
            "recoverable": False,
            "configuredModel": configured,
            "visibleModels": len(visible_ids),
            "detail": f"当前模型 {configured} 已由 Codex 运行时目录确认可用。",
        }
    defaults = [
        str(item.get("id") or item.get("model") or item.get("slug") or "").strip()
        for item in runtime_models
        if item.get("isDefault")
    ]
    fallback_ids = [*defaults, *sorted(visible_ids)]
    candidate_id = next((value for value in fallback_ids if value in key_by_runtime_id), "")
    return {
        "status": "error",
        "healthy": False,
        "recoverable": bool(candidate_id) and process_scan_known and not codex_running,
        "configuredModel": configured,
        "candidateModel": candidate_id or None,
        "candidateKey": key_by_runtime_id.get(candidate_id) if candidate_id else None,
        "visibleModels": len(visible_ids),
        "detail": (
            "Codex 当前选择与模型目录不一致，但无法可靠确认进程状态；"
            "为避免覆盖运行中的配置，本次不提供自动修复。"
            if candidate_id and not process_scan_known
            else f"Codex 当前选择与模型目录不一致；关闭 Codex 后可恢复到 {candidate_id}。"
            if candidate_id and codex_running
            else f"Codex 当前选择与模型目录不一致；可恢复到 {candidate_id}。"
            if candidate_id
            else f"Codex 当前模型 {configured} 无法与所选来源及运行时目录共同确认，未执行自动切换。"
        ),
    }



def repair_runtime_model_selection(candidate_key: str) -> dict:
    candidate = str(candidate_key or "").strip()
    if not candidate:
        raise _core.ManagerError("没有可验证的替代模型，请重新检查。")
    with _core._exclusive_switch_operation("model-repair", candidate):
        snapshot = _core._capture_file_bytes((_core.SETTINGS_FILE, _core.CONFIG_FILE, _core.RUNTIME_OVERLAY_FILE))
        try:
            settings = _core.load_settings()
            workspace = settings.setdefault("modelWorkspace", _core._default_model_workspace())
            proxy_active = bool(settings.get("web2api", {}).get("activeForCodex"))
            records = _core.web2api_pool_model_records(settings) if proxy_active else _core.selected_model_records(settings)
            record = next((item for item in records if str(item.get("key") or "") == candidate), None)
            if not record:
                raise _core.ManagerError("建议的替代模型已经不在当前选择范围，请重新检查。")
            workspace["defaultModelKey"] = candidate
            if not workspace.get("selectAll", True):
                workspace["selectedModels"] = list(
                    dict.fromkeys([*workspace.get("selectedModels", []), candidate])
                )
            profile = _core._active_main(settings)
            profile["model"] = str(record.get("id") or profile.get("model") or "")
            rendered = _core.build_codex_config(settings)
            expected = str(
                (
                    record.get("slug")
                    if workspace.get("mode") == "aggregate" or proxy_active
                    else record.get("id")
                )
                or ""
            )
            parsed = _core.tomllib.loads(rendered)
            if str(parsed.get("model") or "") != expected:
                raise _core.ManagerError("替代模型生成结果与当前选择不一致。")
            before = snapshot.get(_core.CONFIG_FILE)
            _core.save_settings(settings)
            if before != rendered.encode("utf-8"):
                _core.backup_file(_core.CONFIG_FILE)
                _core.atomic_write_text(_core.CONFIG_FILE, rendered)
                _core._runtime_overlay_record_applied(paths=[_core.CONFIG_FILE])
            configured = str(_core.read_toml(_core.CONFIG_FILE).get("model") or "")
            if configured != expected:
                raise _core.ManagerError("替代模型写入后回验不一致。")
            return {
                "changed": before != rendered.encode("utf-8"),
                "model": configured,
                "files": [str(_core.SETTINGS_FILE), str(_core.CONFIG_FILE)],
            }
        except Exception as exc:
            rollback_errors = _core._restore_file_bytes(snapshot)
            detail = f"；回滚回验异常：{'；'.join(rollback_errors)}" if rollback_errors else ""
            if isinstance(exc, _core.ManagerError):
                raise _core.ManagerError(f"模型修复未保持稳定，已原样回滚：{exc}{detail}") from exc
            raise _core.ManagerError(f"模型修复失败，已原样回滚：{exc}{detail}") from exc



def validate_configuration(run_doctor: bool = True) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    settings = _core.load_settings()
    provider_ids = {item["id"] for item in settings["providers"]} | {_core.AGGREGATE_PROVIDER_ID}
    try:
        profile = _core._active_main(settings)
        if profile["provider"] not in provider_ids:
            errors.append("当前主模型引用了不存在的 Provider。")
        if not profile.get("model"):
            errors.append("当前主模型为空。")
        if profile["provider"] != "openai" and not _core.provider_key_configured(profile["provider"]):
            errors.append(f"当前主模型 Provider `{profile['provider']}` 尚未配置 API Key。")
    except _core.ManagerError as exc:
        errors.append(_core._redact_sensitive_text(exc, limit=320))

    agents = _core.discover_agents()
    agent_names = set()
    for record in agents:
        if record.get("error"):
            errors.append(record["error"])
            continue
        data = record["data"]
        name = data.get("name")
        if not name:
            errors.append(f"{record['path']} 缺少 name。")
            continue
        if name in agent_names:
            errors.append(f"Agent 名称重复：{name}")
        agent_names.add(name)
        provider = data.get("model_provider") or "openai"
        if provider not in provider_ids:
            errors.append(f"Agent `{name}` 引用了不存在的 Provider `{provider}`。")
        if not data.get("model") or not data.get("description") or not data.get("developer_instructions"):
            errors.append(f"Agent `{name}` 缺少模型、说明或 Instructions。")
    for level, route in settings.get("routes", {}).items():
        if not isinstance(route, dict):
            errors.append(f"{level} 路由格式无效。")
            continue
        if not route.get("enabled", False):
            continue
        route_agents = route.get("agents", [])
        if not isinstance(route_agents, list):
            errors.append(f"{level} 路由的 Agent 列表格式无效。")
            continue
        for name in route_agents:
            if name not in agent_names:
                errors.append(f"{level} 路由引用了不存在的 Agent `{name}`。")

    stored_account_ids = set(_core._secret_store().get("accounts", {}))
    for account in settings.get("accounts", []):
        if not account.get("id") or not account.get("label"):
            errors.append("账号中心存在缺少 ID 或名称的记录。")
        elif account["id"] not in stored_account_ids:
            errors.append(f"账号 `{account['label']}` 缺少加密凭据快照。")
    if settings.get("accounts") and _core._credential_store_mode() == "keyring":
        warnings.append("Codex 当前使用 keyring；已保存的 auth.json 账号快照暂时不能切换。")
    sync_target = str(settings.get("historySync", {}).get("target") or "").strip()
    if sync_target:
        try:
            _core._history_sync_root(sync_target, create=False)
        except _core.ManagerError as exc:
            errors.append(_core._redact_sensitive_text(exc, limit=320))

    try:
        _core.tomllib.loads(_core.build_codex_config(settings))
        _core.read_toml(_core.CONFIG_FILE) if _core.CONFIG_FILE.exists() else None
    except Exception as exc:
        errors.append(f"Codex TOML 校验失败：{_core._redact_sensitive_text(exc, limit=320)}")

    doctor_summary = "未运行"
    if run_doctor:
        try:
            result = _core.run_codex_capture(["--strict-config", "doctor", "--json"], timeout=60)
            output = (result.stdout + result.stderr).strip()
            try:
                report = _core.json.loads(result.stdout)
            except (TypeError, _core.json.JSONDecodeError):
                report = None
            checks = report.get("checks") if isinstance(report, dict) else None
            if isinstance(checks, dict):
                rows = [item for item in checks.values() if isinstance(item, dict)]
                status_counts = {
                    status: sum(
                        1 for item in rows if str(item.get("status") or "").casefold() == status
                    )
                    for status in ("ok", "warn", "fail")
                }
                doctor_summary = (
                    f"{status_counts['ok']} ok | {status_counts['warn']} warn | "
                    f"{status_counts['fail']} fail"
                )
                blocking_categories = {
                    "auth",
                    "config",
                    "install",
                    "mcp",
                    "runtime",
                    "sandbox",
                    "state",
                    "threads",
                }
                for item in rows:
                    status = str(item.get("status") or "").casefold()
                    if status not in {"warn", "fail", "error"}:
                        continue
                    category = str(item.get("category") or "").casefold()
                    check_id = str(item.get("id") or category or "doctor")
                    summary = str(item.get("summary") or "检查未通过")
                    detail = _core._redact_sensitive_text(f"{check_id}：{summary}", limit=320)
                    if status in {"fail", "error"} and category in blocking_categories:
                        errors.append(f"Codex Doctor 阻断项：{detail}")
                    else:
                        warnings.append(f"Codex Doctor 非阻断提示：{detail}")
            else:
                summary = [
                    line.strip()
                    for line in output.splitlines()
                    if _core.re.search(r"\d+ ok|warn|fail", line)
                ]
                doctor_summary = summary[-1] if summary else (output[-500:] or f"exit {result.returncode}")
                if result.returncode != 0:
                    failed_checks = [
                        line.strip()
                        for line in output.splitlines()
                        if _core.re.search(r"\[(?:XX|fail|error)\]", line, flags=_core.re.IGNORECASE)
                    ]
                    nonblocking = [
                        line for line in failed_checks if _core.re.search(r"terminal.*TERM=dumb", line, _core.re.IGNORECASE)
                    ]
                    blocking = [line for line in failed_checks if line not in nonblocking]
                    warnings.extend(f"Codex Doctor 非阻断提示：{line}" for line in nonblocking)
                    if blocking or not failed_checks:
                        detail = blocking[0] if blocking else doctor_summary
                        errors.append(f"Codex Doctor 未通过：{detail}")
        except Exception as exc:
            warnings.append(f"无法运行 Codex Doctor：{exc}")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "doctor": doctor_summary,
        "checkedAt": _core.now_iso(),
        "agentCount": len(agents),
    }

