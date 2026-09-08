"""Switching services."""
from __future__ import annotations
from agent_manager import application as _app


def activate_web2api_for_codex(runtime: _app.ManagerRuntime, preferred_account_id: str | None = None) -> dict:
    target = preferred_account_id or "pool"
    with _app.core._exclusive_switch_operation("web2api-activate", target):
        return _app._activate_web2api_for_codex(runtime, preferred_account_id)



def _activate_web2api_for_codex(runtime: _app.ManagerRuntime, preferred_account_id: str | None = None) -> dict:
    settings_before = _app.core.load_settings()
    transaction_snapshot = _app.core._capture_switch_transaction_snapshot(settings_before)
    config_before = _app.json.loads(_app.json.dumps(settings_before.get("web2api", _app.core._default_web2api_settings())))
    launch_plan = None
    generated_key = None
    was_running = runtime.web2api.status()["running"]
    service_started = False
    closed = None
    launch_attempted = False
    session_visibility = None
    try:
        if preferred_account_id:
            preferred = next(
                (
                    item
                    for item in settings_before.get("accounts", [])
                    if str(item.get("id")) == preferred_account_id
                ),
                None,
            )
            if not preferred:
                raise _app.core.ManagerError("账号不存在。")
            if preferred.get("sourceType") != "web_session":
                raise _app.core.ManagerError("只有 Web Session 账号需要通过本地转换模式启动。")
            if not _app.core._account_codex_compatible(preferred) or _app.core.account_invalid_reason(preferred):
                raise _app.core.ManagerError("该 Web Session 尚未通过 Codex 推理授权检测，不能用于对话。")
            if not preferred.get("models"):
                raise _app.core.ManagerError("该 Web Session 尚未同步到可用模型，请刷新账号后重试。")
        _app.core.validate_web2api_codex_pool(_app.core.load_settings(), preferred_account_id)
        launch_plan = _app.core.resolve_codex_launch_plan()
        if not _app.core.service_secret_configured("web2api"):
            generated_key = _app.core.rotate_web2api_key()
        runtime.web2api.start()
        service_started = True
        closed = _app._close_codex_processes_safely()
        _app.core.set_web2api_codex_active(True, preferred_account_id)
        applied = _app.core.apply_configuration(False)
        session_sync = _app.core.auto_sync_sessions_after_switch(_app.core.AGGREGATE_PROVIDER_ID)
        session_visibility = _app.core._repair_switch_session_visibility()
        session_sync["visibility"] = session_visibility
        launch_attempted = True
        launch = _app.core.launch_codex_app(launch_plan=launch_plan)
        expected_model = str(_app.core.read_toml(_app.core.CONFIG_FILE).get("model") or "")
        launch["readiness"] = _app.core.wait_for_codex_runtime_ready(
            expected_model=expected_model or None, launch_plan=launch_plan,
        )
        launch["retryCount"] = 0
        status = runtime.web2api.status()
    except Exception as exc:
        recovery_errors = []
        if launch_attempted:
            try:
                if _app.core.running_codex_processes():
                    _app._close_codex_processes_safely(timeout_seconds=8)
            except Exception as cleanup_exc:
                recovery_errors.append(f"关闭未通过验证的 Codex：{str(cleanup_exc)[:300]}")
        if service_started and not was_running:
            try:
                runtime.web2api.stop(disable=not bool(config_before.get("enabled")))
            except Exception as cleanup_exc:
                recovery_errors.append(f"停止本地反代：{str(cleanup_exc)[:300]}")
        recovery_errors.extend(_app.core._restore_switch_transaction_snapshot(transaction_snapshot))
        try:
            _app.core._restore_switch_session_visibility(session_visibility)
        except Exception as recovery_exc:
            recovery_errors.append(f"恢复会话标记：{str(recovery_exc)[:300]}")
        try:
            if closed is not None and launch_plan and not launch_attempted and not recovery_errors:
                _app.core.launch_codex_app(launch_plan=launch_plan)
        except Exception as recovery_exc:
            recovery_errors.append(str(recovery_exc)[:300])
        detail = f"；恢复原状态也失败：{'；'.join(recovery_errors)}" if recovery_errors else "；已原样回滚"
        if launch_attempted:
            detail += "；为避免循环重启，未再次启动 Codex"
        if isinstance(exc, _app.core.ManagerError):
            raise _app.core.ManagerError(f"启用本地反代并切换 Codex 失败：{exc}{detail}") from exc
        raise _app.core.ManagerError(f"启用本地反代并切换 Codex 失败：{exc}{detail}") from exc
    return {
        "status": status,
        "generatedKey": generated_key,
        "closed": closed,
        "applied": applied,
        "sessionSync": session_sync,
        "launch": launch,
        "mode": "web_session_local_conversion" if preferred_account_id else "web2api_pool",
        "preferredAccountId": preferred_account_id,
        "verified": True,
    }



def deactivate_web2api_for_codex(runtime: _app.ManagerRuntime) -> dict:
    with _app.core._exclusive_switch_operation("web2api-deactivate", "openai"):
        return _app._deactivate_web2api_for_codex(runtime)



def _deactivate_web2api_for_codex(runtime: _app.ManagerRuntime) -> dict:
    settings_before = _app.core.load_settings()
    transaction_snapshot = _app.core._capture_switch_transaction_snapshot(settings_before)
    config_before = _app.json.loads(_app.json.dumps(settings_before.get("web2api", _app.core._default_web2api_settings())))
    active = bool(config_before.get("activeForCodex"))
    if not active:
        applied = _app.core.apply_configuration(False)
        if applied.get("gatewayRequired"):
            status = runtime.web2api.status() if runtime.web2api.status()["running"] else runtime.web2api.start()
            return {
                "status": status,
                "closed": None,
                "applied": applied,
                "launch": None,
                "keptForSubagents": True,
            }
        return {
            "status": runtime.web2api.stop(),
            "closed": None,
            "applied": applied,
            "launch": None,
            "keptForSubagents": False,
        }
    launch_plan = _app.core.resolve_codex_launch_plan()
    closed = _app._close_codex_processes_safely()
    launch_attempted = False
    session_visibility = None
    try:
        _app.core.set_web2api_codex_active(False)
        applied = _app.core.apply_configuration(False)
        session_sync = _app.core.auto_sync_sessions_after_switch("openai")
        session_visibility = _app.core._repair_switch_session_visibility()
        session_sync["visibility"] = session_visibility
        if applied.get("gatewayRequired"):
            status = runtime.web2api.status() if runtime.web2api.status()["running"] else runtime.web2api.start()
        else:
            status = runtime.web2api.stop()
        launch_attempted = True
        launch = _app.core.launch_codex_app(launch_plan=launch_plan)
        expected_model = str(_app.core.read_toml(_app.core.CONFIG_FILE).get("model") or "")
        launch["readiness"] = _app.core.wait_for_codex_runtime_ready(
            expected_model=expected_model or None, launch_plan=launch_plan,
        )
        launch["retryCount"] = 0
    except Exception as exc:
        recovery_errors = []
        if launch_attempted:
            try:
                if _app.core.running_codex_processes():
                    _app._close_codex_processes_safely(timeout_seconds=8)
            except Exception as cleanup_exc:
                recovery_errors.append(f"关闭未通过验证的 Codex：{str(cleanup_exc)[:300]}")
        recovery_errors.extend(_app.core._restore_switch_transaction_snapshot(transaction_snapshot))
        try:
            _app.core._restore_switch_session_visibility(session_visibility)
        except Exception as recovery_exc:
            recovery_errors.append(f"恢复会话标记：{str(recovery_exc)[:300]}")
        try:
            if not runtime.web2api.status()["running"]:
                runtime.web2api.start()
            if not launch_attempted and not recovery_errors:
                _app.core.launch_codex_app(launch_plan=launch_plan)
        except Exception as recovery_exc:
            recovery_errors.append(str(recovery_exc)[:300])
        detail = f"；恢复反代状态也失败：{'；'.join(recovery_errors)}" if recovery_errors else "；已原样回滚"
        if launch_attempted:
            detail += "；为避免循环重启，未再次启动 Codex"
        if isinstance(exc, _app.core.ManagerError):
            raise _app.core.ManagerError(f"退出反代模式失败：{exc}{detail}") from exc
        raise _app.core.ManagerError(f"退出反代模式失败：{exc}{detail}") from exc
    return {
        "status": status,
        "closed": closed,
        "applied": applied,
        "sessionSync": session_sync,
        "launch": launch,
        "keptForSubagents": bool(applied.get("gatewayRequired")),
        "verified": True,
    }



def rotate_web2api_key_for_runtime(runtime: _app.ManagerRuntime) -> dict:
    # HTTP authentication reads the stored key for every request. Codex uses a
    # distinct private key, so public rotation never requires terminating it.
    api_key = _app.core.rotate_web2api_key()
    return {
        "apiKey": api_key,
        "status": runtime.web2api.status(),
        "closed": None,
        "applied": None,
        "launch": None,
    }



def _ensure_runtime_gateway(runtime: _app.ManagerRuntime):
    if runtime.web2api.status().get("running"):
        return
    try:
        return runtime.web2api.start()
    except Exception as exc:
        try:
            runtime.web2api.stop(disable=False)
        except Exception as cleanup_exc:
            raise _app.core.ManagerError(
                f"本地路由启动失败，清理未完成：{_app.core._redact_sensitive_text(cleanup_exc, limit=180)}"
            ) from exc
        raise



def save_orchestration_for_runtime(runtime: _app.ManagerRuntime, payload: dict) -> dict:
    return _app.core.save_orchestration_and_apply(payload, ensure_gateway=lambda: _app._ensure_runtime_gateway(runtime))

