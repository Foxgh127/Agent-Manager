"""Switching services."""
from __future__ import annotations
from agent_manager import core as _core


class _SwitchProgressReporter:
    """Emit bounded, user-facing switch stages and retain real timings."""

    def __init__(self, callback: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.callback = callback
        self.started_at = _core.time.perf_counter()
        self.phase_started_at = self.started_at
        self.phase: str | None = None
        self.timings: dict[str, int] = {}

    def emit(
        self,
        phase: str,
        progress: int,
        message: str,
        *,
        status: str = "running",
        **details: Any,
    ) -> None:
        now = _core.time.perf_counter()
        if self.phase and self.phase != phase and self.phase not in {"completed", "failed"}:
            self.timings[self.phase] = max(0, round((now - self.phase_started_at) * 1000))
        if self.phase != phase:
            self.phase = phase
            self.phase_started_at = now
        payload = {
            "phase": phase,
            "progress": max(0, min(100, int(progress))),
            "message": str(message or ""),
            "status": status,
            "elapsedMs": max(0, round((now - self.started_at) * 1000)),
            "timings": dict(self.timings),
            **details,
        }
        if callable(self.callback):
            try:
                self.callback(payload)
            except Exception:
                # Progress reporting is observational and must never make an
                # otherwise safe account transaction fail.
                pass

    def completed(self, message: str = "Codex 已完成启动与身份回验") -> None:
        self.emit("completed", 100, message, status="completed")

    def failed(
        self,
        error: BaseException | str,
        *,
        failed_phase: str | None = None,
        message: str = "切换未完成，原账号与配置已安全恢复",
        recovery_state: str = "restored",
        can_retry: bool = True,
    ) -> None:
        failed_phase = failed_phase or self.phase
        detail = _core._redact_sensitive_text(error, limit=360)
        self.emit(
            "failed",
            100,
            message,
            status="error",
            error=detail,
            failedPhase=failed_phase,
            recoveryState=recovery_state,
            canRetry=bool(can_retry),
        )

    def summary(self) -> dict:
        return {
            "totalMs": max(0, round((_core.time.perf_counter() - self.started_at) * 1000)),
            "stages": dict(self.timings),
        }



def _prepare_account_switch_target(settings: dict, account_id: str) -> tuple[dict, dict[str, bytes | None]]:
    account = next((item for item in settings.get("accounts", []) if item.get("id") == account_id), None)
    if not account:
        raise _core.ManagerError("账号不存在。")
    if account.get("sourceType") == "web_session":
        if not _core._account_codex_compatible(account):
            raise _core.ManagerError(
                "该账号是浏览器 Web Session，只能查询额度和模型目录；"
                "它没有通过 Codex 推理授权检测。"
            )
        raise _core.ManagerError(
            "该 Web Session 需要通过管理器的本地转换反代启动，不能把合成认证文件直接写入 Codex。"
        )
    if not _core._account_codex_compatible(account):
        raise _core.ManagerError(
            "该账号是浏览器 Web Session，只能查询额度和模型目录；"
            "它不包含 Codex OAuth 凭据，无法切换后用于对话。"
        )
    target_files = dict(_core._decode_snapshot_files(_core._load_account_snapshot(account_id)))
    target_files["auth.json"] = _core._codex_auth_projection_bytes(target_files["auth.json"] or b"")
    live_cap_sid_path = _core.CODEX_HOME / "cap_sid"
    if target_files.get("cap_sid") is None and live_cap_sid_path.is_file():
        target_files["cap_sid"] = live_cap_sid_path.read_bytes()
    return account, target_files



def _account_model_source(settings: dict, account_id: str) -> dict:
    source_id = f"account:{account_id}"
    source = next((item for item in _core.model_sources(settings) if item.get("id") == source_id), None)
    if not source or not source.get("available") or not source.get("models"):
        raise _core.ManagerError("该官方账号没有可用模型，请先刷新账号后重试。")
    return source



def switch_codex_account(
    account_id: str,
    force: bool = False,
    *,
    credentials_preflighted: bool = False,
) -> dict:
    if _core._credential_store_mode() == "keyring":
        raise _core.ManagerError("当前 Codex 使用 keyring 凭据存储，auth.json 快照切换不可用。")
    processes = _core.running_codex_processes()
    if processes and not force:
        names = ", ".join(f"{item['name']} ({item['pid']})" for item in processes[:4])
        raise _core.ManagerError(f"检测到正在运行的 Codex 进程：{names}。关闭后重试，或明确确认强制切换。")
    preflight_settings = _core.load_settings()
    preflight_account = next(
        (item for item in preflight_settings.get("accounts", []) if item.get("id") == account_id),
        None,
    )
    if not preflight_account:
        raise _core.ManagerError("账号不存在。")
    if (
        not credentials_preflighted
        and preflight_account.get("authMode") == "chatgpt"
        and preflight_account.get("sourceType") == "codex_auth"
    ):
        _core._account_chatgpt_credentials(account_id)
    with _core._exclusive_switch_operation("account", account_id):
        if _core._credential_store_mode() == "keyring":
            raise _core.ManagerError("当前 Codex 使用 keyring 凭据存储，auth.json 快照切换不可用。")
        processes = _core._require_known_codex_processes()
        if processes and not force:
            names = ", ".join(f"{item['name']} ({item['pid']})" for item in processes[:4])
            raise _core.ManagerError(f"检测到正在运行的 Codex 进程：{names}。关闭后重试，或明确确认强制切换。")

        settings = _core.load_settings()
        account = next((item for item in settings.get("accounts", []) if item.get("id") == account_id), None)
        if not account:
            raise _core.ManagerError("账号不存在。")
        account, target_files = _core._prepare_account_switch_target(settings, account_id)
        target_source = _core._account_model_source(settings, account_id)
        snapshot = _core._capture_switch_transaction_snapshot(settings)
        backup_dir: Path | None = None
        try:
            try:
                live_snapshot, live_identity = _core._read_live_snapshot()
            except _core.ManagerError:
                live_snapshot, live_identity = None, None
            changed = not (
                live_identity
                and _core._account_matches_identity(account, live_identity)
                and _core._live_auth_files_match(target_files)
            )
            if changed and live_snapshot and live_identity:
                live_record = _core._find_account_for_identity(settings, live_identity)
                if live_record and live_record.get("id") != account_id:
                    _core._store_account_snapshot(live_record["id"], live_snapshot)
                    live_record["updatedAt"] = _core.now_iso()
            if changed:
                stamp = _core.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                backup_dir = _core.BACKUPS_DIR / f"auth-switch-{stamp}"
                backup_dir.mkdir(parents=True, exist_ok=False)
                for name in _core.AUTH_FILES:
                    content = snapshot["files"].get(_core.CODEX_HOME / name)
                    if content is not None:
                        _core.atomic_write_bytes(backup_dir / name, content)
                _core.atomic_write_json(
                    backup_dir / "manifest.json",
                    {"createdAt": _core.now_iso(), "targetAccountId": account_id, "files": list(_core.AUTH_FILES)},
                )
                _core._restore_auth_files(target_files)
                _core._runtime_overlay_record_applied(paths=[_core.CODEX_HOME / name for name in _core.AUTH_FILES])
                if not _core._live_auth_files_match(target_files):
                    _core.time.sleep(0.05)
                    if not _core._live_auth_files_match(target_files):
                        raise _core.ManagerError("认证文件在切换后被其他 Codex 进程改写。")
                _, switched_identity = _core._read_live_snapshot()
                if not _core._account_matches_identity(account, switched_identity):
                    raise _core.ManagerError("切换后的账号身份与目标快照不一致。")

            activation_at = _core.now_iso()
            account["lastUsedAt"] = activation_at
            account["updatedAt"] = activation_at
            workspace = _core._select_workspace_source(settings, target_source, independent=True)
            profile = _core._active_main(settings)
            profile["provider"] = "openai"
            selected_key = str(workspace.get("defaultModelKey") or "")
            selected_model = next(
                (item for item in target_source.get("models", []) if str(item.get("key") or "") == selected_key),
                target_source["models"][0],
            )
            profile["model"] = str(selected_model.get("id") or profile.get("model") or "")
            web2api = settings.setdefault("web2api", _core._default_web2api_settings())
            web2api["activeForCodex"] = False
            web2api["activeAccountId"] = None
            _core.save_settings(settings)
            try:
                _core.record_direct_account_activation(account_id, activation_at)
            except (_core.ManagerError, OSError):
                pass
            _core._reset_switch_caches()
            session_sync = _core.auto_sync_sessions_after_switch("openai")
            result = {
                "changed": changed,
                "account": account,
                "sessionSync": session_sync,
            }
            if backup_dir is not None:
                result["backupPath"] = str(backup_dir)
            if not changed:
                result["message"] = "该账号已经是当前账号。"
            return result
        except Exception as exc:
            rollback_errors = _core._restore_switch_transaction_snapshot(snapshot)
            detail = f"；回滚回验异常：{'；'.join(rollback_errors)}" if rollback_errors else ""
            if isinstance(exc, _core.ManagerError):
                raise _core.ManagerError(f"账号切换未保持稳定，已自动回滚并原样恢复：{exc}{detail}") from exc
            raise _core.ManagerError(f"账号切换失败，已自动回滚并原样恢复：{exc}{detail}") from exc



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
    probe_options = {"launch_plan": launch_plan} if launch_plan is not None else {}
    last_error = "Codex App Server 尚未就绪"
    attempts = 0
    while _core.time.monotonic() < deadline:
        attempts += 1
        remaining = max(1.0, deadline - _core.time.monotonic())
        try:
            requests = [
                    ("account/read", {"refreshToken": False}),
                    # A selected model is not guaranteed to be the first item.
                    # Asking for one entry made healthy providers fail readiness
                    # whenever their default ordering changed.
                    ("model/list", {"cursor": None, "limit": 100 if expected_model_id else 1}),
                ]
            if launch_plan is not None:
                requests.append(("config/read", {"includeLayers": True, "cwd": launch_plan.get("workspace")}))
            results = _core.codex_app_server_requests(
                requests,
                timeout=max(2, min(8, int(remaining))),
                **probe_options,
            )
            account_result, model_result = results[:2]
            account = account_result.get("account") if isinstance(account_result, dict) else None
            actual_email = str(account.get("email") or "").strip() if isinstance(account, dict) else ""
            if expected and not actual_email:
                raise _core.ManagerError("Codex 未识别出目标 ChatGPT 登录账号。")
            if expected and actual_email.casefold() != expected:
                raise _core.ManagerError(
                    f"Codex 读取到的账号是 {actual_email}，并非刚切换的 {expected_email}。"
                )
            if launch_plan is not None:
                effective = results[2].get("config") if len(results) > 2 and isinstance(results[2], dict) else None
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
                "effectiveConfigChecked": launch_plan is not None,
                "desktopThreadsChecked": False,
                "message": "Codex 已启动，当前账号与新会话配置检查通过。",
            }
        except _core.ManagerError as exc:
            last_error = _core._redact_sensitive_text(exc, limit=500)
            # A freshly launched App Server can report no account for a short
            # window while it loads the credential store.  That transient is
            # retryable; only an explicit, different identity is conclusive.
            if expected and "并非刚切换" in last_error:
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



def _rollback_failed_switch(
    snapshot: dict,
    launch_plan: dict,
    *,
    closed: dict | None,
    launch_attempted: bool,
    close_processes_callback: _core.Callable[..., dict] | None = None,
    state_mutated: bool = True,
    session_visibility: dict | None = None,
) -> str:
    close_processes_callback = close_processes_callback or _core.close_codex_processes
    rollback_errors: list[str] = []
    cleaned_failed_runtime = False
    failed_runtime_present = False
    failed_runtime_probe_ok = not launch_attempted
    process_state_unknown = False
    process_state_checked = not launch_attempted and isinstance(closed, dict)
    previously_running = bool(
        isinstance(closed, dict)
        and not closed.get("alreadyStopped", False)
        and (closed.get("requested") or closed.get("closed"))
    )
    if launch_attempted:
        try:
            failed_runtime_records, _retries = _core._require_known_codex_processes_with_retry(
                attempts=8,
                context="失败恢复前",
            )
            failed_runtime_present = bool(failed_runtime_records)
            failed_runtime_probe_ok = True
            if failed_runtime_present:
                close_processes_callback(timeout_seconds=8)
                cleaned_failed_runtime = True
            process_state_checked = True
        except _core.CodexProcessScanError as exc:
            process_state_unknown = True
            rollback_errors.append(f"关闭未通过验证的 Codex：{str(exc)[:240]}")
        except Exception as exc:
            rollback_errors.append(f"关闭未通过验证的 Codex：{str(exc)[:240]}")
    if process_state_unknown:
        if not state_mutated:
            return "；目标配置尚未写入，原账号与配置未改变；Codex 退出状态暂无法确认，可直接重试"
        return "；未恢复旧配置：无法可靠确认 Codex 已停止，请先关闭 Codex 后重试"
    if state_mutated:
        rollback_errors.extend(
            _core._restore_switch_transaction_snapshot(
                snapshot,
                process_state_checked=process_state_checked,
            )
        )
    recovery_launch_error = None
    try:
        _core._restore_switch_session_visibility(session_visibility)
    except Exception as exc:
        rollback_errors.append(f"恢复历史对话标记：{_core._redact_sensitive_text(exc, limit=240)}")
    # A readiness timeout can mean either a still-running misconfigured
    # process or a process that crashed during startup.  Recover only in the
    # latter case, after restoring the transaction snapshot, and never retry a
    # live failed process in a loop.
    should_relaunch_original = previously_running and (
        not launch_attempted or (failed_runtime_probe_ok and not failed_runtime_present)
    )
    if should_relaunch_original and not rollback_errors:
        try:
            _core.launch_codex_app(launch_plan=launch_plan)
        except Exception as exc:
            recovery_launch_error = str(exc)[:240]
    if recovery_launch_error:
        rollback_errors.append(f"恢复后单次启动原 Codex：{recovery_launch_error}")
    if rollback_errors:
        return f"；回滚回验异常：{'；'.join(rollback_errors)}"
    if not state_mutated:
        if launch_attempted:
            runtime_note = "；已关闭未通过验证的 Codex" if cleaned_failed_runtime else ""
            return (
                f"；目标配置尚未写入，原账号与配置未改变{runtime_note}；"
                "为避免循环重启，未再次启动 Codex"
            )
        return "；目标配置尚未写入，原账号与配置未改变"
    if should_relaunch_original:
        return "；已原样回滚，并仅启动一次原 Codex"
    if launch_attempted:
        runtime_note = "并关闭未通过验证的 Codex" if cleaned_failed_runtime else ""
        return f"；已原样回滚{runtime_note}；为避免循环重启，未再次启动 Codex"
    return "；已原样回滚"



def switch_codex_account_and_launch(
    account_id: str,
    progress_callback: _core.Callable[[dict[str, _core.Any]], None] | None = None,
    close_processes_callback: _core.Callable[..., dict] | None = None,
    *,
    ensure_gateway: _core.Callable | None = None,
    force_reapply: bool = False,
) -> dict:
    """Atomically switch to one official account with at most one relaunch."""
    close_processes_callback = close_processes_callback or _core.close_codex_processes
    reporter = _core._SwitchProgressReporter(progress_callback)
    reporter.emit("preflight", 4, "正在检查账号、模型与 Codex 启动器")
    if _core._credential_store_mode() == "keyring":
        raise _core.ManagerError("当前 Codex 使用 keyring 凭据存储，auth.json 快照切换不可用。")
    # Refresh an inactive OAuth snapshot before taking the switch/file locks and
    # before closing Desktop. A slow network renewal must not leave the user
    # staring at a closed Codex window, and it must not deadlock a background
    # quota worker that is ready to commit under SETTINGS_LOCK.
    preflight_settings = _core.load_settings()
    preflight_account, preflight_files = _core._prepare_account_switch_target(
        preflight_settings,
        account_id,
    )
    preflight_source = _core._account_model_source(preflight_settings, account_id)
    preflight_active = _core._official_account_target_is_active(
        preflight_settings,
        preflight_account,
        preflight_files,
        preflight_source,
    )
    if (
        (not preflight_active or force_reapply)
        and preflight_account.get("authMode") == "chatgpt"
        and preflight_account.get("sourceType") == "codex_auth"
    ):
        reporter.emit("credentials", 14, "正在校验并刷新官方 OAuth 凭据")
        _core._account_chatgpt_credentials(account_id)
    reporter.emit("prepared", 22, "预检通过，正在准备安全切换")
    launch_plan = _core.resolve_codex_launch_plan()
    launch_plan = {**launch_plan, "officialAccountId": account_id}
    with _core._exclusive_switch_operation("account-launch", account_id):
        settings = _core.load_settings()
        account, target_files = _core._prepare_account_switch_target(settings, account_id)
        source = _core._account_model_source(settings, account_id)
        if not force_reapply and _core._official_account_target_is_active(settings, account, target_files, source):
            _core._ensure_switch_gateway(str(_core.read_toml(_core.CONFIG_FILE).get("model_provider") or "") == _core.AGGREGATE_PROVIDER_ID, ensure_gateway)
            unchanged = {
                "changed": False,
                "account": account,
                "message": "该账号已经是当前账号。",
            }
            # Selecting the highlighted account is also the explicit "open
            # Codex with this account" action.  Do not turn it into a no-op
            # merely because the files already match when Desktop is closed.
            if _core.running_codex_processes():
                reporter.completed("官方账号文件与路由配置一致；Codex 正在运行，已有对话尚未验证")
                return {
                    "accountSwitch": unchanged,
                    "closed": None,
                    "launch": None,
                    "verified": True,
                    "verificationScope": "saved_configuration",
                    "desktopThreadsChecked": False,
                    "performance": reporter.summary(),
                }
            snapshot = _core._capture_switch_transaction_snapshot(settings)
            session_visibility = None
            launch_attempted = False
            try:
                session_visibility = _core._repair_switch_session_visibility()
                reporter.emit("launching", 72, "账号已生效，正在启动 Codex")
                launch_attempted = True
                launch = _core.launch_codex_app(launch_plan=launch_plan)
                expected_email = str(account.get("email") or "").strip()
                if not expected_email and "@" in str(account.get("label") or ""):
                    expected_email = str(account.get("label") or "").strip()
                expected_model = str(_core.read_toml(_core.CONFIG_FILE).get("model") or "").strip()
                reporter.emit("verifying", 88, "Codex 已出现，正在核对账号与模型")
                launch["readiness"] = _core.wait_for_codex_runtime_ready(
                    expected_email or None,
                    expected_model or None,
                    launch_plan={**launch_plan, "executable": launch.get("executable") or launch_plan.get("executable")},
                )
                launch["retryCount"] = 0
                unchanged["sessionSync"] = {"visibility": session_visibility}
                reporter.completed()
                return {
                    "accountSwitch": unchanged,
                    "closed": None,
                    "launch": launch,
                    "verified": True,
                    "performance": reporter.summary(),
                }
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
                raise _core.ManagerError(f"账号已选中但 Codex 启动验证失败：{exc}{detail}") from exc
        snapshot = _core._capture_switch_transaction_snapshot(settings)
        closed = None
        launch_attempted = False
        state_mutated = False
        session_visibility = None
        try:
            reporter.emit("closing", 30, "正在安全关闭旧 Codex；Agent Manager 会继续运行")
            closed = close_processes_callback()
            reporter.emit("writing", 48, "正在写入官方 ChatGPT 登录身份")
            state_mutated = True
            result = _core.switch_codex_account(
                account_id,
                force=True,
                credentials_preflighted=True,
            )
            reporter.emit("configuring", 62, "正在清理 API 覆盖并应用官方模型配置")
            configured = _core._apply_official_account_configuration(account_id)
            _core._ensure_switch_gateway(bool(configured["applied"].get("gatewayRequired")), ensure_gateway)
            if "sessionSync" not in result:
                result["sessionSync"] = _core.auto_sync_sessions_after_switch("openai")
            session_visibility = _core._repair_switch_session_visibility()
            result["sessionSync"]["visibility"] = session_visibility
            launch_attempted = True
            reporter.emit("launching", 76, "配置已写入，正在启动 Codex")
            launch = _core.launch_codex_app(launch_plan=launch_plan)
            expected_email = str(account.get("email") or "").strip()
            if not expected_email and "@" in str(account.get("label") or ""):
                expected_email = str(account.get("label") or "").strip()
            reporter.emit("verifying", 90, "Codex 已出现，正在核对官方账号与可用模型")
            readiness = _core.wait_for_codex_runtime_ready(
                expected_email or None,
                configured.get("model") or None,
                launch_plan={**launch_plan, "executable": launch.get("executable") or launch_plan.get("executable")},
            )
            launch["readiness"] = readiness
            launch["retryCount"] = 0
            reporter.completed()
            return {
                "accountSwitch": result,
                "closed": closed,
                "launch": launch,
                "workspace": configured["workspace"],
                "applied": configured["applied"],
                "model": configured["model"],
                "verified": True,
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
            raise _core.ManagerError(f"账号切换或启动失败：{exc}{detail}") from exc



def provider_by_id(provider_id: str, settings: dict | None = None) -> dict:
    settings = settings or _core.load_settings()
    provider = next((item for item in settings.get("providers", []) if item.get("id") == provider_id), None)
    if not provider:
        raise _core.ManagerError(f"找不到 Provider：{provider_id}")
    return provider



def _provider_portal_preset(preset_id: object, *, allow_empty: bool = False) -> dict | None:
    normalized = str(preset_id or "").strip().casefold()
    if not normalized and allow_empty:
        return None
    preset = next((item for item in _core.PROVIDER_PORTAL_PRESETS if item["id"] == normalized), None)
    if preset is None:
        raise _core.ManagerError("旧版中转站兼容记录无效，请重新填写 API 信息。")
    return preset



def _url_hostname(value: object) -> str:
    try:
        return str(_core.urllib.parse.urlsplit(str(value or "").strip()).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""



def detect_provider_portal_preset(*values: object) -> dict | None:
    """Match only exact curated hosts; lookalike suffixes must never match."""

    hostnames = {_core._url_hostname(value) for value in values}
    hostnames.discard("")
    for preset in _core.PROVIDER_PORTAL_PRESETS:
        allowed = {str(host).casefold().rstrip(".") for host in preset.get("hosts", ())}
        if hostnames & allowed:
            return preset
    return None



def _validated_provider_url(
    value: object,
    label: str,
    *,
    allow_empty: bool = False,
    allow_query: bool = False,
) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text and allow_empty:
        return ""
    if len(text.encode("utf-8", errors="replace")) > 4_096:
        raise _core.ManagerError(f"{label} 过长，已拒绝连接。")
    try:
        parsed = _core.urllib.parse.urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise _core.ManagerError(f"{label} 端口或 URL 格式无效。") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise _core.ManagerError(f"{label} 必须是完整的 HTTP(S) 地址。")
    if parsed.username is not None or parsed.password is not None:
        raise _core.ManagerError(f"{label} 不能在 URL 中包含用户名或密码。")
    if parsed.fragment:
        raise _core.ManagerError(f"{label} 不能包含 #fragment。")
    if parsed.query and not allow_query:
        raise _core.ManagerError(f"{label} 不能包含查询参数。")
    if port is not None and not 1 <= port <= 65535:
        raise _core.ManagerError(f"{label} 端口必须在 1 到 65535 之间。")
    hostname = str(parsed.hostname).strip().casefold().rstrip(".")
    loopback = hostname == "localhost"
    literal_address = None
    try:
        literal_address = _core.ipaddress.ip_address(hostname)
        loopback = literal_address.is_loopback
    except ValueError:
        pass
    if literal_address is not None and not loopback and (
        literal_address.is_unspecified
        or literal_address.is_link_local
        or literal_address.is_multicast
        or literal_address.is_reserved
    ):
        raise _core.ManagerError(f"{label} 指向不安全的保留或链路本地地址，已拒绝连接。")
    if parsed.scheme == "http" and not loopback:
        raise _core.ManagerError(f"{label} 会携带 API Key，远程地址必须使用 HTTPS；只有本机回环地址允许 HTTP。")
    return text



def _validated_provider_portal_url(
    value: object,
    *,
    allow_empty: bool = False,
    preset: dict | None = None,
) -> str:
    portal_url = _core._validated_provider_url(
        value,
        "官网 / 控制台地址",
        allow_empty=allow_empty,
    )
    if not portal_url:
        return ""
    if preset is not None:
        hostname = _core._url_hostname(portal_url)
        allowed = {str(host).casefold().rstrip(".") for host in preset.get("hosts", ())}
        if hostname not in allowed:
            raise _core.ManagerError("官网 / 控制台地址与所选快捷模板不匹配，已拒绝打开可疑站点。")
    return portal_url



def provider_portal_url(provider_id: str) -> str:
    provider = _core.provider_by_id(provider_id)
    portal_url = _core._validated_provider_portal_url(
        provider.get("portalUrl"),
        allow_empty=True,
        preset=_core._provider_portal_preset(provider.get("presetId"), allow_empty=True),
    )
    if not portal_url:
        raise _core.ManagerError("该中转站尚未配置官网 / 控制台地址。")
    return portal_url



def _provider_url_origin(value: str) -> tuple[str, str, int]:
    """Return a normalized origin for an already validated provider URL."""
    try:
        parsed = _core.urllib.parse.urlsplit(value)
        scheme = parsed.scheme.casefold()
        hostname = str(parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port or (443 if scheme == "https" else 80)
    except (TypeError, ValueError) as exc:
        raise _core.ManagerError("远端服务返回了无效的跳转地址，已拒绝继续发送凭据。") from exc
    if scheme not in {"http", "https"} or not hostname or not 1 <= int(port) <= 65_535:
        raise _core.ManagerError("远端服务返回了无效的跳转地址，已拒绝继续发送凭据。")
    return scheme, hostname, port



class _SameOriginRedirectHandler(_core.urllib.request.HTTPRedirectHandler):
    """Keep redirects on the credential's original origin.

    ``urllib`` copies ordinary request headers, including ``Authorization``,
    when it follows a redirect. Provider endpoints are user-configurable, so
    a cross-origin redirect must be rejected before a credential can leave the
    configured service.
    """

    def __init__(self, original_url: str):
        super().__init__()
        self._origin = _core._provider_url_origin(original_url)

    def redirect_request(self, request, fp, code, message, headers, new_url):
        if _core._provider_url_origin(new_url) != self._origin:
            raise _core.ManagerError("远端服务尝试跳转到其他来源，已拒绝继续发送凭据。")
        return super().redirect_request(request, fp, code, message, headers, new_url)



def _open_same_origin_request(request: _core.urllib.request.Request, *, timeout: float):
    """Open one request while rejecting cross-origin redirects."""

    opener = _core.urllib.request.build_opener(
        _core.urllib.request.ProxyHandler(_core._effective_url_proxies()),
        _core._SameOriginRedirectHandler(request.full_url),
    )
    return opener.open(request, timeout=timeout)



def _effective_url_proxies() -> dict[str, str]:
    """Return environment proxies with a Windows system-proxy fallback.

    CPython stops consulting the Windows Internet Settings registry as soon as
    *any* proxy-related environment variable exists. A parent process that
    exports only ``NO_PROXY`` therefore makes ``urllib`` silently bypass an
    otherwise enabled Windows HTTPS proxy. Codex Desktop still follows the
    system proxy in that situation, which made account quota probes time out
    while the same account continued to work in Codex.

    Keep explicit HTTP(S) environment proxies authoritative, then fill only
    missing schemes from the current Windows registry. The mapping is read on
    every opener construction so changing VPN/proxy modes does not require an
    Agent Manager restart.
    """

    try:
        proxies = {
            str(key).casefold(): str(value)
            for key, value in _core.urllib.request.getproxies().items()
            if str(key).strip() and str(value).strip()
        }
    except (OSError, ValueError):
        proxies = {}
    if _core.os.name != "nt":
        return proxies
    registry_reader = getattr(_core.urllib.request, "getproxies_registry", None)
    if not callable(registry_reader):
        return proxies
    try:
        registry_proxies = registry_reader()
    except (OSError, ValueError):
        registry_proxies = {}
    if isinstance(registry_proxies, dict):
        for key, value in registry_proxies.items():
            scheme = str(key).casefold().strip()
            endpoint = str(value).strip()
            if scheme and endpoint and scheme not in proxies:
                proxies[scheme] = endpoint
    return proxies



def _validated_provider_related_url(
    value: object,
    base_url: str,
    label: str,
    *,
    allow_empty: bool = False,
    allow_query: bool = False,
) -> str:
    related = _core._validated_provider_url(
        value,
        label,
        allow_empty=allow_empty,
        allow_query=allow_query,
    )
    if not related:
        return ""
    if _core._provider_url_origin(related) != _core._provider_url_origin(base_url):
        raise _core.ManagerError(f"{label} 必须与 Base URL 使用同一来源，避免 API Key 被发送到其他站点。")
    return related

