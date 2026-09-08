"""Persistent, provider-scoped model picker preferences, separate from discovery."""

import agent_manager.core as core
import agent_manager.sessions.live_selection


def save_reasoning(provider_id: str, model_id: str, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise core.ManagerError("模型设置必须是对象。")
    mode = str(payload.get("mode") or "auto")
    if mode not in {"auto", "custom", "disabled"}:
        raise core.ManagerError("思考强度模式无效。")
    raw = payload.get("efforts", [])
    if (
        not isinstance(raw, list)
        or len(raw) > len(core.VALID_EFFORTS)
        or any(item not in core.VALID_EFFORTS for item in raw)
    ):
        raise core.ManagerError("思考强度列表无效。")
    efforts = list(dict.fromkeys(raw))
    if mode == "custom" and not efforts:
        raise core.ManagerError("请至少选择一个思考档位。")
    default = str(payload.get("defaultEffort") or "")
    if default and default not in efforts:
        raise core.ManagerError("默认思考强度必须在已选档位中。")
    with (
        core.SWITCH_OPERATION_LOCK,
        core.CONFIG_FILE_LOCK,
        core.RUNTIME_OVERLAY_LOCK,
        core.SETTINGS_LOCK,
        core.SECRETS_LOCK,
        core._settings_file_lock(),
    ):
        settings = core.load_settings()
        original_settings = core._json_clone(settings)
        provider = core.provider_by_id(provider_id, settings)
        if provider.get("kind") != "custom" or model_id not in provider.get(
            "models", []
        ):
            raise core.ManagerError("该模型不属于当前中转站，请刷新后重试。")
        old = core._json_clone(provider.get("modelReasoningOverrides") or {})
        preferences = core._normalize_provider_model_capabilities(
            old, provider.get("models", [])
        )
        provider["modelReasoningOverrides"] = preferences
        if mode == "auto":
            preferences.pop(model_id, None)
        else:
            preferences[model_id] = {
                "reasoningKnown": True,
                "reasoningSupported": mode != "disabled",
                "efforts": efforts if mode == "custom" else [],
                "defaultEffort": default if mode == "custom" else "",
            }
        if old == preferences:
            return {"changed": False, "applied": False, "restartRequired": False}
        revision, _ = core._provider_runtime_revisions(provider)
        provider["runtimeRevision"] = revision + 1
        live = agent_manager.sessions.live_selection.inspect_live_selection(settings)
        active_id = str(settings.get("modelWorkspace", {}).get("activeSourceId") or "")
        apply_now = bool(
            active_id == f"provider:{provider_id}" and live.get("sourceId") == active_id
        )
        if not apply_now:
            backup = core.backup_file(core.SETTINGS_FILE)
            core.save_settings(settings)
            return {
                "changed": True,
                "applied": False,
                "restartRequired": True,
                "backup": str(backup) if backup else None,
            }
        core._preflight_orchestration_artifacts(settings)
        snapshot = core._capture_orchestration_transaction_snapshot(settings)
        snapshot["settingsValue"] = original_settings
        try:
            applied = core._apply_configuration_locked(settings=settings)
        except Exception as exc:
            rollback = core._restore_switch_transaction_snapshot(
                snapshot, process_state_checked=True
            )
            detail = f"；还原异常：{'；'.join(rollback)}" if rollback else ""
            raise core.ManagerError(f"模型配置保存失败：{exc}{detail}") from exc
        return {
            "changed": True,
            "applied": True,
            "restartRequired": True,
            "backups": applied.get("backups", []),
        }
