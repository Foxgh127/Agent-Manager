"""Configuration validation, runtime ready check and session repair"""
from __future__ import annotations
from agent_manager import core as _core


def wait_for_codex_runtime_ready(
    expected_email: str | None = None,
    expected_model: str | None = None,
    timeout_seconds: float = _core.CODEX_RUNTIME_READY_TIMEOUT_SECONDS,
    *,
    launch_plan: dict | None = None,
) -> dict:
    """Verify an isolated App Server probe, not an existing Desktop thread."""
    deadline = _core.time.monotonic() + max(1.0, float(timeout_seconds))
    expected = str(expected_email or "").strip().casefold()
    expected_model_id = str(expected_model or "").strip()
    expected_config = _core.read_toml(_core.CONFIG_FILE)
    if expected and _core._official_route_has_overrides(expected_config):
        raise _core.ManagerError("官方账号配置仍包含 API 地址或认证覆盖，已停止启动回验。")
    probe_plan = dict(launch_plan) if isinstance(launch_plan, dict) else launch_plan
    if isinstance(probe_plan, dict) and not str(probe_plan.get("workspace") or "").strip():
        workspace = _core._recent_codex_workspace()
        if workspace:
            probe_plan["workspace"] = str(workspace)
    probe_options = {"launch_plan": probe_plan} if probe_plan is not None else {}
    last_error = "Codex App Server 尚未就绪"
    attempts = 0
    while _core.time.monotonic() < deadline:
        attempts += 1
        remaining = max(1.0, deadline - _core.time.monotonic())
        try:
            provider_probe = bool(isinstance(probe_plan, dict) and probe_plan.get("apiProviderId"))
            requests = [] if provider_probe else [("account/read", {"refreshToken": False})]
            # A selected model is not guaranteed to be the first item. Asking
            # for one entry made healthy providers fail readiness whenever
            # their default ordering changed.
            requests.append(("model/list", {"cursor": None, "limit": 100 if expected_model_id else 1}))
            if probe_plan is not None:
                requests.append(("config/read", {"includeLayers": True, "cwd": probe_plan.get("workspace")}))
            results = _core.codex_app_server_requests(
                requests,
                timeout=max(2, min(8, int(remaining))),
                **probe_options,
            )
            if provider_probe:
                account_result = {}
                model_result = results[0] if results else {}
                config_result = results[1] if len(results) > 1 else None
            else:
                account_result, model_result = results[:2]
                config_result = results[2] if len(results) > 2 else None
            account = account_result.get("account") if isinstance(account_result, dict) else None
            actual_email = str(account.get("email") or "").strip() if isinstance(account, dict) else ""
            if expected and not actual_email:
                raise _core.ManagerError("Codex 未识别出目标 ChatGPT 登录账号。")
            if expected and actual_email.casefold() != expected:
                raise _core.ManagerError(
                    f"Codex 读取到的账号是 {actual_email}，并非刚切换的 {expected_email}。"
                )
            if probe_plan is not None:
                effective = config_result.get("config") if isinstance(config_result, dict) else None
                if not isinstance(effective, dict) or not _core._probe_configuration_matches(expected_config, effective):
                    raise _core.ManagerError("Codex 探针有效配置与刚写入的账号路由不一致。")
            models = model_result.get("data") if isinstance(model_result, dict) else None
            if not isinstance(models, list):
                raise _core.ManagerError("Codex 模型目录尚未完成初始化。")
            visible_model_ids = {
                str(item.get("id") or item.get("model") or item.get("slug") or "").strip()
                for item in models
                if isinstance(item, dict)
            }
            cursor = (
                model_result.get("nextCursor") or model_result.get("next_cursor")
                if isinstance(model_result, dict)
                else None
            )
            seen_cursors: set[str] = set()
            # Some relays expose hundreds of models.  A healthy selected model
            # may therefore live beyond the first page; only continue paging
            # while it is still missing, and cap the scan to prevent a broken
            # cursor from creating an unbounded readiness loop.
            for _ in range(4):
                if not expected_model_id or expected_model_id in visible_model_ids or not cursor:
                    break
                cursor_key = str(cursor)
                if cursor_key in seen_cursors:
                    break
                seen_cursors.add(cursor_key)
                remaining = max(1.0, deadline - _core.time.monotonic())
                page_result = _core.codex_app_server_request(
                    "model/list",
                    {"cursor": cursor, "limit": 100},
                    timeout=max(2, min(8, int(remaining))),
                    **probe_options,
                )
                page = page_result.get("data") if isinstance(page_result, dict) else None
                if not isinstance(page, list):
                    raise _core.ManagerError("Codex 模型目录分页返回格式无效。")
                models.extend(item for item in page if isinstance(item, dict))
                visible_model_ids.update(
                    str(item.get("id") or item.get("model") or item.get("slug") or "").strip()
                    for item in page
                    if isinstance(item, dict)
                )
                cursor = page_result.get("nextCursor") or page_result.get("next_cursor")
            if expected_model_id and expected_model_id not in visible_model_ids:
                raise _core.ManagerError(f"Codex 模型目录尚未加载目标模型 {expected_model_id}。")
            return {
                "ready": True,
                "email": actual_email or None,
                "modelsVisible": len(models),
                "expectedModel": expected_model_id or None,
                "attempts": attempts,
                "checkedAt": _core.now_iso(),
                "verificationScope": "app_server_probe",
                "effectiveConfigChecked": probe_plan is not None,
                "desktopThreadsChecked": False,
                "message": "Codex 已启动，当前账号与新会话配置检查通过。",
            }
        except _core.ManagerError as exc:
            last_error = _core._redact_sensitive_text(exc, limit=500)
            # A freshly launched App Server can report no account for a short
            # window while it loads the credential store.  That transient is
            # retryable; only an explicit, different identity is conclusive.
            if (
                (expected and "并非刚切换" in last_error)
                or "无法启动 Codex App Server 探针" in last_error
            ):
                break
        except Exception as exc:
            last_error = _core._redact_sensitive_text(exc, limit=500)
        if _core.time.monotonic() < deadline:
            _core.time.sleep(min(0.5, max(0.05, deadline - _core.time.monotonic())))
    raise _core.ManagerError(f"Codex 登录初始化未在限定时间内完成：{last_error}")



def _probe_configuration_matches(expected: dict, effective: dict) -> bool:
    provider = str(expected.get("model_provider") or "openai")
    if str(effective.get("model_provider") or "openai") != provider:
        return False
    if expected.get("model") and effective.get("model") != expected.get("model"):
        return False
    if provider == "openai":
        return bool(not _core._official_route_has_overrides(effective)
                    and (expected.get("cli_auth_credentials_store") != "file"
                         or str(effective.get("cli_auth_credentials_store") or "file") == "file"))
    expected_tables = expected.get("model_providers") or {}
    effective_tables = effective.get("model_providers") or {}
    if not isinstance(expected_tables, dict) or not isinstance(effective_tables, dict):
        return False
    wanted, actual = expected_tables.get(provider), effective_tables.get(provider)
    return bool(isinstance(wanted, dict) and isinstance(actual, dict)
                and wanted.get("base_url") == actual.get("base_url")
                and wanted.get("env_key") == actual.get("env_key")
                and bool(wanted.get("requires_openai_auth")) == bool(actual.get("requires_openai_auth"))
                and (wanted.get("wire_api") or "responses") == (actual.get("wire_api") or "responses")
                and all(wanted.get(key) == actual.get(key) for key in (
                    "experimental_bearer_token", "http_headers", "env_http_headers")))



def _provider_auth_overrides(table: dict) -> bool:
    if not hasattr(table, "get"):
        return True
    if table.get("experimental_bearer_token") or table.get("requires_openai_auth") is True:
        return True
    for field in ("http_headers", "env_http_headers"):
        headers = table.get(field)
        if headers:
            if not hasattr(headers, "keys"):
                return True
            if any(str(name).strip().casefold().replace("_", "-") in _core._PROVIDER_CREDENTIAL_HEADERS for name in headers):
                return True
    return False



def _clear_provider_auth_overrides(table) -> None:
    table.pop("experimental_bearer_token", None)
    table["requires_openai_auth"] = False
    for field in ("http_headers", "env_http_headers"):
        headers = table.get(field)
        if not hasattr(headers, "keys"):
            table.pop(field, None)
            continue
        for name in list(headers):
            if str(name).strip().casefold().replace("_", "-") in _core._PROVIDER_CREDENTIAL_HEADERS:
                headers.pop(name, None)
        if not headers:
            table.pop(field, None)



def _switch_runtime_model_matches(settings: dict, config: dict, source: dict, direct_provider: str) -> bool:
    """Validate the selected upstream identity separately from its local transport."""
    active_provider = str(config.get("model_provider") or "openai")
    model = str(config.get("model") or "")
    model_ids = {str(item.get("id") or "") for item in source.get("models", [])}
    if _core._subagents_require_shared_gateway(settings):
        if active_provider != _core.AGGREGATE_PROVIDER_ID:
            return False
        table = config.get("model_providers", {}).get(_core.AGGREGATE_PROVIDER_ID, {})
        port = int(settings.get("web2api", {}).get("port", 17860))
        if table.get("base_url") != f"http://127.0.0.1:{port}/v1" or table.get("env_key") != _core.AGGREGATE_ENV_KEY:
            return False
        if _core._provider_auth_overrides(table):
            return False
        route = _core.resolve_model_route(model, settings)
        return bool(route and not route.get("subagentAlias")
                    and route.get("sourceKind") == source.get("kind")
                    and str(route.get("sourceRecordId")) == str(source.get("recordId"))
                    and str(route.get("id") or "") in model_ids)
    if direct_provider == "openai" and _core._official_route_has_overrides(config):
        return False
    if direct_provider != "openai" and _core._provider_auth_overrides(config.get("model_providers", {}).get(direct_provider, {})):
        return False
    return active_provider == direct_provider and model in model_ids



def _official_route_has_overrides(config: dict) -> bool:
    """A built-in provider name alone does not establish a native OAuth route."""
    if config.get("openai_base_url"):
        return True
    chatgpt_base = str(config.get("chatgpt_base_url") or "").strip().rstrip("/")
    if chatgpt_base and chatgpt_base not in {
        "https://chatgpt.com", "https://chatgpt.com/backend-api",
        "https://chat.openai.com", "https://chat.openai.com/backend-api",
    }:
        return True
    providers = config.get("model_providers")
    table = providers.get("openai") if isinstance(providers, dict) else None
    # Current Codex reserves built-in IDs; older runtimes accepted this table.
    # Neither a stale endpoint nor a credential override belongs to a selected
    # official snapshot. Do not interpret unrelated custom provider tables.
    return bool(isinstance(table, dict) and (table.get("requires_openai_auth") is False
        or (table.get("wire_api") and table.get("wire_api") != "responses") or any(table.get(key) for key in (
        "base_url", "env_key", "experimental_bearer_token", "http_headers", "env_http_headers",
    ))))



def _ensure_switch_gateway(required: bool, ensure_gateway: _core.Callable | None) -> None:
    if required and ensure_gateway:
        ensure_gateway()



def _repair_switch_session_visibility() -> dict:
    import agent_manager.sessions.visibility
    if not _core.load_settings().get("sessionSync", {}).get("enabled", True):
        return {"changed": False, "status": "skipped", "reason": "disabled"}
    target = str(_core.read_toml(_core.CONFIG_FILE).get("model_provider") or "openai")
    return agent_manager.sessions.visibility.auto_repair(target, check_all_providers=True)



def _restore_switch_session_visibility(visibility: dict | None) -> None:
    if isinstance(visibility, dict) and visibility.get("backupId"):
        import agent_manager.sessions.visibility
        agent_manager.sessions.visibility.restore_provider_repair(visibility)



def _official_account_target_is_active(
    settings: dict,
    account: dict,
    target_files: dict[str, bytes | None],
    source: dict,
) -> bool:
    if _core._credential_store_mode() != "file":
        return False
    if any(_core.os.environ.get(name) for name in _core._OFFICIAL_AUTH_ENV_OVERRIDES):
        return False
    try:
        _live_snapshot, live_identity = _core._read_live_snapshot()
    except _core.ManagerError:
        return False
    workspace = settings.get("modelWorkspace", {})
    web2api = settings.get("web2api", {})
    config = _core.read_toml(_core.CONFIG_FILE)
    model_ids = {str(item.get("id") or "") for item in source.get("models", [])}
    model_keys = {str(item.get("key") or "") for item in source.get("models", [])}
    selected = {str(item) for item in workspace.get("selectedModels", [])}
    active_environment = [_core.AGGREGATE_ENV_KEY] if _core._managed_subagent_specs(settings) else []
    return bool(
        live_identity
        and _core._account_matches_identity(account, live_identity)
        and _core._live_auth_files_match(target_files)
        and workspace.get("mode") == "independent"
        and workspace.get("activeSourceId") == source.get("id")
        and (bool(workspace.get("selectAll", True)) or model_keys.issubset(selected))
        and not web2api.get("activeForCodex")
        and _core._switch_runtime_model_matches(settings, config, source, "openai")
        and not _core._inactive_provider_environment_overrides(settings, active_environment)
    )



def _apply_official_account_configuration(account_id: str) -> dict:
    settings = _core.load_settings()
    source = _core._account_model_source(settings, account_id)
    workspace = _core._select_workspace_source(settings, source, independent=True)
    profile = _core._active_main(settings)
    profile["provider"] = "openai"
    selected_key = str(workspace.get("defaultModelKey") or "")
    selected_model = next(
        (item for item in source.get("models", []) if str(item.get("key") or "") == selected_key),
        source["models"][0],
    )
    profile["model"] = str(selected_model.get("id") or profile.get("model") or "")
    web2api = settings.setdefault("web2api", _core._default_web2api_settings())
    web2api["activeForCodex"] = False
    web2api["activeAccountId"] = None
    _core.save_settings(settings)
    applied = _core.apply_configuration(False)
    applied_config = _core.read_toml(_core.CONFIG_FILE)
    if not _core._switch_runtime_model_matches(settings, applied_config, source, "openai"):
        raise _core.ManagerError("写入回验失败：Codex 路由没有指向所选官方账号和模型。")
    expected_models = {str(item.get("id") or "") for item in source.get("models", [])}
    applied_model = str(applied_config.get("model") or "")
    if _core._inactive_provider_environment_overrides(settings, applied.get("syncedEnvKeys", [])):
        raise _core.ManagerError("写入回验失败：旧中转站环境覆盖仍处于活动状态。")
    return {
        "workspace": workspace,
        "applied": applied,
        "model": applied_model,
        "models": sorted(expected_models),
    }

