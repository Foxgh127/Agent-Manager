"""Official account integration and launch"""
from __future__ import annotations
from agent_manager import core as _core

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
        visibility_error = _core._redact_sensitive_text(exc, limit=240)
        if "恢复冲突" in visibility_error:
            # The catalog/session repair deliberately refuses to overwrite a
            # newer Codex update. Report that as preserved newer state rather
            # than implying that conversation history was lost.
            rollback_errors.append(
                f"恢复历史对话标记：已保留后续更新（{visibility_error}）"
            )
        else:
            rollback_errors.append(f"恢复历史对话标记：{visibility_error}")
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
        if all("已保留后续更新" in item for item in rollback_errors):
            return f"；历史对话标记已保留后续更新：{'；'.join(rollback_errors)}"
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

