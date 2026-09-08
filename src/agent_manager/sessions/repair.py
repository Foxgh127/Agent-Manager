"""One-click, backed-up session maintenance for the current Codex instance."""
from __future__ import annotations

import threading
from typing import Callable

import agent_manager.core as core
import agent_manager.sessions.history as history
import agent_manager.sessions.visibility as visibility


_REPAIR_LOCK = threading.Lock()
MAX_AUTOMATIC_RECOVERIES = 10


def repair_sessions(*, close_codex: Callable[[], dict]) -> dict:
    """The UI explicitly requests a restart; keep all writes behind the stop gate.

    Recovery journals and visibility repairs retain their own authenticated
    backups. A later failure must not undo an earlier successful journal recovery.
    """
    if not _REPAIR_LOCK.acquire(blocking=False):
        raise core.ManagerError("一键修复正在执行，请等待当前操作完成。")
    try:
        with core._exclusive_switch_operation("session-repair", "current"):
            # Fail before closing a working app if configuration/launch is invalid.
            config = core.read_toml(core.CONFIG_FILE)
            provider = config.get("model_provider") or "openai"
            definitions = config.get("model_providers") or {}
            if provider != "openai" and (not isinstance(definitions, dict) or not isinstance(definitions.get(provider), dict)):
                raise core.ManagerError("当前服务配置不完整，请先重新应用目标账号，再执行会话修复。")
            running = core._require_codex_process_scan_known()
            launch_plan = core.resolve_codex_launch_plan() if running else None
            if launch_plan and (config.get("model_provider") or "openai") == "openai" and not core._official_route_has_overrides(config):
                # Reuse official-source environment isolation when the manager
                # process still inherits credentials from the previous API route.
                launch_plan = {**launch_plan, "officialAccountId": "current"}
            closed = False
            result = {"changed": False, "partial": False, "restarted": False,
                      "recoveries": [], "warnings": [], "visibility": None}
            try:
                if running:
                    close_codex()
                    closed = True
                if core._require_codex_process_scan_known():
                    raise core.ManagerError("Codex 尚未完全退出，未修改会话；请稍后再试。")

                pending = history.list_recoveries(limit=MAX_AUTOMATIC_RECOVERIES)
                for item in pending.get("recoveries", []):
                    if not item.get("recoverable"):
                        result["warnings"].append("一条同步记录无法自动校验，已保留，可在修复详情中查看。")
                        continue
                    try:
                        preview = history.preview_recovery(item["id"])
                        if preview.get("conflicts"):
                            result["warnings"].append("一条同步记录包含后续修改，已保留当前文件，可在修复详情中查看。")
                            continue
                        restored = history.restore_recovery(item["id"], preview["fingerprint"])
                        result["recoveries"].append(restored)
                        result["changed"] |= bool(restored.get("changed"))
                        if restored.get("warning"):
                            result["warnings"].append(restored["warning"])
                    except core.ManagerError as exc:
                        # Restoration handles its own rollback. Stop this batch on
                        # failure, and report the successful earlier work honestly.
                        raise core.ManagerError("同步恢复未完成，已暂停后续修复：" + core._redact_sensitive_text(exc, limit=240)) from exc
                if pending.get("truncated"):
                    result["warnings"].append("同步记录较多，本次处理了首批；再次点击一键修复可继续。")

                # Always run after journal recovery, which may restore old metadata.
                result["visibility"] = visibility.repair_current_provider()
                repaired = result["visibility"]
                result["changed"] |= bool(repaired.get("changed"))
                completion = repaired.get("completion") or {}
                if repaired.get("partial") or completion.get("partial") or repaired.get("status") in {"partial", "deferred", "blocked"}:
                    result["partial"] = True
                if repaired.get("reason") == "repair_unavailable":
                    result["warnings"].append(repaired.get("message") or "部分会话暂时无法修复。")
                result["partial"] |= bool(result["warnings"])
            except Exception as exc:
                result["partial"] = True
                result["warnings"].append(core._redact_sensitive_text(exc, limit=240))
            finally:
                if closed:
                    try:
                        core.launch_codex_app(launch_plan=launch_plan)
                        result["restarted"] = True
                    except Exception as exc:
                        result["partial"] = True
                        result["warnings"].append("Codex 重新打开失败：" + core._redact_sensitive_text(exc, limit=180))

            result["status"] = "partial" if result["partial"] else "repaired" if result["changed"] else "healthy"
            result["message"] = (
                "检查已结束，部分项目仍需处理；已完成的修复和备份均已保留。" if result["partial"]
                else "会话已修复，备份已保留。" if result["changed"]
                else "检查完成，当前会话无需修复。"
            )
            if result["restarted"]:
                result["message"] += " Codex 已重新打开。"
            if ((result.get("visibility") or {}).get("completion") or {}).get("catalogRecovery") == "native_rebuild_pending":
                result["message"] += " 已请求桌面原生重建会话目录；启动后需重新诊断确认，当前仍标记为未完成。"
            return result
    finally:
        _REPAIR_LOCK.release()
