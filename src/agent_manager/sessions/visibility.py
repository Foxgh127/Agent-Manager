"""Application boundary for session visibility; all storage belongs to core.CODEX_HOME.

Only opaque inspection tokens and backup IDs cross the application boundary.
The engine's plans (which can contain conversation previews) remain private.
Core is imported lazily because account switching calls this module itself.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tomllib

import agent_manager.sessions.visibility_engine as engine


_BACKUP_ID = re.compile(r"visibility-\d{8}-\d{6}-\d{6}-[0-9a-f]{12}\Z")
_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
_PATCH_FIELDS = frozenset({"model_provider", "has_user_event", "first_user_message", "thread_source", "preview", "cwd", "working_directory"})
MAX_PROVIDER_REPAIR_BATCHES = 8
MAX_PROVIDER_REPAIR_SCAN_BYTES = 384 * 1024 * 1024
MAX_PROVIDER_REPAIR_WRITE_BYTES = 256 * 1024 * 1024


def _core():
    import agent_manager.core
    return agent_manager.core


@contextmanager
def _operation():
    core = _core()
    held = []
    try:
        # Same order as core configuration writes; reentrant during switching.
        for lock in (core.SWITCH_OPERATION_LOCK, core.CONFIG_FILE_LOCK):
            if not lock.acquire(blocking=False):
                raise core.ManagerError("已有切换、配置或会话修复操作正在执行，请稍后重试。")
            held.append(lock)
        yield core
    except engine.SessionVisibilityError as exc:
        raise core.ManagerError(str(exc)) from exc
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        # Do not echo malformed config/manifest contents to the HTTP client.
        raise core.ManagerError("会话可见性存储或参数无效，请重新诊断。") from exc
    finally:
        for lock in reversed(held):
            lock.release()


def _fenced(root: Path, candidate: Path) -> Path:
    """Reject traversal and all symlinks/junctions, including missing-leaf parents."""
    root = Path(os.path.abspath(root))
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise engine.SessionVisibilityError("会话可见性路径越出固定实例目录。") from exc
    current = root
    for part in (None, *relative.parts):
        if part is not None:
            current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise engine.SessionVisibilityError("会话可见性路径不允许符号链接或目录联接。")
    if candidate.resolve(strict=False) != root.resolve(strict=False) / relative:
        raise engine.SessionVisibilityError("会话可见性路径越出固定实例目录。")
    return candidate


def _home(core) -> Path:
    home = Path(core.CODEX_HOME)
    return _fenced(home, home)


def _backup_root(core) -> Path:
    home = _home(core)
    return _fenced(home, Path(core.BACKUPS_DIR) / "session-visibility")


def _options(mode, session_ids):
    if not isinstance(mode, str) or mode not in {"quick", "deep"}:
        raise engine.SessionVisibilityError("mode 仅支持 quick 或 deep。")
    if session_ids is not None:
        if not isinstance(session_ids, (list, tuple)) or len(session_ids) > 1000:
            raise engine.SessionVisibilityError("session_ids 必须为最多 1000 个会话 ID 的列表。")
        if any(not isinstance(item, str) or not item.strip() or len(item) > 256 for item in session_ids):
            raise engine.SessionVisibilityError("会话 ID 无效。")
        session_ids = sorted({item.strip() for item in session_ids})
    return mode, session_ids or None


def _configuration(core):
    path = _fenced(_home(core), _home(core) / "config.toml")
    if path.exists() and path.stat().st_size > 2_000_000:
        raise engine.SessionVisibilityError("Codex 配置超过安全读取上限。")
    content = path.read_bytes() if path.exists() else b""
    config = tomllib.loads(core.decode_toml_bytes(content)) if content else {}
    provider = config.get("model_provider") or "openai"
    if not isinstance(provider, str) or not engine.PROVIDER_PATTERN.fullmatch(provider):
        raise engine.SessionVisibilityError("Codex model_provider 无效。")
    return provider, hashlib.sha256(content).hexdigest()


def _inspect(core, mode, session_ids, target_provider=None, *, check_all_providers=False,
             max_scan_bytes=None, max_repair_bytes=None):
    mode, session_ids = _options(mode, session_ids)
    current_provider, config_digest = _configuration(core)
    report = engine.inspect_session_visibility(
        _home(core), target_provider=target_provider or current_provider,
        mode=mode, session_ids=session_ids, check_all_providers=check_all_providers,
        max_rollouts=engine.DEFAULT_QUICK_MAX_ROLLOUTS,
        max_scan_bytes=engine.DEFAULT_QUICK_SCAN_BYTES if max_scan_bytes is None else max_scan_bytes,
        max_repair_bytes=max_repair_bytes,
    )
    # Bind the engine's content-derived plan to config and the fixed instance.
    material = {"engineToken": report["repairToken"], "configSha256": config_digest,
                "home": str(_home(core)), "currentProvider": current_provider}
    token = hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()
    return report, current_provider, token


def _public(report, current_provider, token=None):
    result = {key: report[key] for key in (
        "schemaVersion", "scanMode", "scanBudget", "inspectedAt", "targetProvider",
        "safeToRepair", "counts", "rollouts", "blockedActionCount", "encryptedContentWarning",
        "completion",
        "desktopCatalog",
    )}
    result.update(mode="inspect", currentProvider=current_provider,
                  sessionIds=report["sessionIds"], repairToken=token)
    result["databases"] = [
        {key: item[key] for key in ("path", "version", "selected", "usable") if key in item}
        for item in report["databases"]
    ]
    result["issues"] = [
        {key: item[key] for key in ("kind", "threadId") if key in item}
        for item in report["issues"]
    ]
    result["excluded"] = [
        {key: item[key] for key in ("threadId", "reason") if key in item}
        for item in report["excluded"]
    ]
    result["plan"] = [
        {"kind": item["kind"], "threadId": item["threadId"],
         "table": item.get("table", "threads") if item["kind"] == "sqlite_update" else None,
         "fields": sorted(item["set"]) if item["kind"] == "sqlite_update" else ["model_provider"],
         "reasons": item.get("reasons", ["provider_mismatch"])}
        for item in report["actions"]
    ]
    return result


def _require_stopped(core):
    if core._require_codex_process_scan_known():
        raise core.ManagerError("会话可见性写入要求先完全停止 Codex，请关闭后重试。")


def _fence_actions(core, actions):
    home = _home(core)
    for action in actions:
        if not isinstance(action, dict):
            raise engine.SessionVisibilityError("会话可见性计划包含无效操作。")
        kind = action.get("kind")
        if kind not in {"sqlite_update", "rollout_meta_update"}:
            raise engine.SessionVisibilityError("会话可见性计划含未知操作。")
        relative = action.get("database") if kind == "sqlite_update" else action.get("path")
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise engine.SessionVisibilityError("会话可见性计划路径无效。")
        target = _fenced(home, home / relative)
        if kind == "sqlite_update":
            table = action.get("table", "threads")
            if not (engine.STATE_DB_PATTERN.fullmatch(target.name)
                    or (table in {"local_thread_catalog", "local_thread_catalog_sync_state"}
                        and target == home / engine.DESKTOP_CATALOG_PATH)):
                raise engine.SessionVisibilityError("会话可见性数据库路径无效。")
            allowed_fields = {"model_provider"} if table == "local_thread_catalog" else {"last_full_reconciled_at"} if table == "local_thread_catalog_sync_state" else _PATCH_FIELDS
            if (table not in {"threads", "local_thread_catalog", "local_thread_catalog_sync_state"}
                    or (table == "local_thread_catalog" and (
                        action.get("idColumn") != "thread_id" or not isinstance(action.get("hostId"), str)
                        or not action["hostId"] or len(action["hostId"]) > 256))
                    or not isinstance(action.get("set"), dict)
                    or not isinstance(action.get("expected"), dict)
                    or not set(action["set"]) <= allowed_fields
                    or set(action["expected"]) != set(action["set"])
                    or (table == "local_thread_catalog_sync_state" and (
                        action.get("idColumn") != "host_id" or not isinstance(action.get("hostId"), str)
                        or not action["hostId"] or len(action["hostId"]) > 256
                        or target != home / engine.DESKTOP_CATALOG_PATH
                        or action.get("set") != {"last_full_reconciled_at": None}))
                    or action.get("idColumn") not in ({"host_id"} if table == "local_thread_catalog_sync_state" else {"id", "thread_id"})):
                raise engine.SessionVisibilityError("会话可见性计划包含不允许修改的字段。")
            for suffix in ("-wal", "-shm", "-journal"):
                _fenced(home, Path(str(target) + suffix))
        elif not engine.ROLLOUT_PATTERN.fullmatch(target.name):
            raise engine.SessionVisibilityError("会话可见性 rollout 路径无效。")


def inspect(mode="quick", session_ids=None):
    """Read-only diagnosis for the fixed instance, including a repairToken."""
    with _operation() as core:
        report, provider, token = _inspect(core, mode, session_ids)
        return _public(report, provider, token)


def _apply(core, report, provider):
    _require_stopped(core)
    parent = _backup_root(core)
    _fence_actions(core, report["actions"])
    result = engine.repair_session_visibility(report, confirm_codex_stopped=True, backup_parent=parent)
    backup_id = Path(result["backupDir"]).name if result.get("backupDir") else None
    completion = result["completion"]
    partial = bool(completion.get("partial"))
    return {"changed": result["changed"], "status": "partial" if partial and result["changed"] else "deferred" if partial else "repaired" if result["changed"] else "unchanged",
            "backupId": backup_id, "sqliteRowsUpdated": result["sqliteRowsUpdated"],
            "rolloutFilesUpdated": result["rolloutFilesUpdated"],
            "completion": completion,
            "message": _completion_message(completion, result["changed"]),
            "verification": _public(result["verification"], provider)}


def _completion_message(completion, changed):
    if not completion.get("partial"):
        return "已同步本次可确认的历史会话。" if changed else "本次诊断未发现需要同步的会话。"
    details = []
    if completion.get("deferredSessions"):
        details.append(f"{completion['deferredSessions']} 个会话待核验")
    if completion.get("catalogMissing"):
        details.append(f"{completion['catalogMissing']} 个会话缺少目录记录")
    if completion.get("remainingActions"):
        details.append("还有下一批待处理项")
    detail = "，".join(details) or "存在未完成项"
    follow_up = ("。已请求 Codex 在下次启动时重建本地目录；启动后重新诊断确认，当前不代表目录已恢复。"
                 if completion.get("catalogRecovery") == "native_rebuild_pending"
                 else "。可在会话可见性面板运行深度诊断；缺失目录暂仅诊断。")
    return ("已完成部分历史会话同步；" if changed else "历史会话同步尚未完成；") + detail + follow_up


def repair(expected_token, mode="quick", session_ids=None):
    """Regenerate a server-side plan and apply only the inspected content."""
    with _operation() as core:
        if not isinstance(expected_token, str) or not _TOKEN.fullmatch(expected_token):
            raise core.ManagerError("修复需要有效的诊断 repairToken。")
        _require_stopped(core)
        report, provider, token = _inspect(core, mode, session_ids)
        if not hmac.compare_digest(expected_token, token):
            raise core.ManagerError("诊断结果已过期或范围不一致，请重新诊断。")
        return _apply(core, report, provider)


def repair_current_provider(session_ids=None):
    """Internal stopped-only repair for the one-click recovery/switch workflow.

    Advance only deferred roots, never the same healthy prefix on every pass.
    Prefix reads reserve three inspections per batch (plan, freshness check and
    verification); full-file validation/backups share a separate write budget.
    A failed batch rolls back all earlier batches through their inverse backups.
    """
    with _operation() as core:
        _require_stopped(core)
        provider, _digest = _configuration(core)
        config = core.read_toml(_home(core) / "config.toml")
        definitions = config.get("model_providers") or {}
        if provider != "openai" and (
            not isinstance(definitions, dict) or not isinstance(definitions.get(provider), dict)
        ):
            raise core.ManagerError("当前模型 Provider 定义缺失，请先重新应用目标账号配置。")
        _mode, requested = _options("quick", session_ids)
        pending = set(requested) if requested else None
        priority_order = {value: index for index, value in enumerate(requested or [])}
        visited, unresolved, missing_catalog, catalog_blocked = set(), set(), set(), set()
        backups, results = [], []
        reserved_scan = planned_writes = 0
        stopped_reason = None
        catalog_unavailable = False
        try:
            for _batch in range(MAX_PROVIDER_REPAIR_BATCHES):
                scan_limit = min(engine.DEFAULT_QUICK_SCAN_BYTES,
                                 (MAX_PROVIDER_REPAIR_SCAN_BYTES - reserved_scan) // 3)
                write_limit = min(engine.DEFAULT_QUICK_REPAIR_BYTES,
                                  MAX_PROVIDER_REPAIR_WRITE_BYTES - planned_writes)
                if scan_limit < 4096 or write_limit <= 0:
                    stopped_reason = "total_work_budget"
                    break
                scope = sorted(pending, key=lambda value: (priority_order.get(value, len(priority_order)), value))[:1000] if pending is not None else None
                report, provider, token = _inspect(core, "quick", scope, check_all_providers=True,
                                                   max_scan_bytes=scan_limit, max_repair_bytes=write_limit)
                reserved_scan += 3 * scan_limit
                if not report["safeToRepair"]:
                    stopped_reason = "unverified_storage"
                    results.append({"changed": False, "sqliteRowsUpdated": 0, "rolloutFilesUpdated": 0,
                                    "verification": _public(report, provider, token)})
                    break
                scan = report["providerScan"]
                for value in scan["scannedSessionIds"] + scan["deferredSessionIds"]:
                    if value not in priority_order:
                        priority_order[value] = len(priority_order)
                scanned = set(scan["scannedSessionIds"])
                deferred = set(scan["deferredSessionIds"])
                missing_catalog.update(scan["catalogMissingIds"])
                catalog_unavailable |= any(item["kind"] == "catalog_unavailable" for item in report["issues"])
                pending = ((pending - set(scope)) if pending is not None else set()) | deferred
                planned = int(report["scanBudget"].get("plannedRepairBytes") or 0)
                planned_writes += planned
                retry = set()
                for item in report["excluded"]:
                    thread_id, reason = item["threadId"], item["reason"]
                    if thread_id in deferred or reason in {"archived", "non_root_agent"}:
                        continue
                    if (reason == "quick_repair_byte_budget" and planned > 0
                            and item.get("requiredBytes", write_limit + 1) <= min(
                                engine.DEFAULT_QUICK_REPAIR_BYTES,
                                MAX_PROVIDER_REPAIR_WRITE_BYTES - planned_writes)):
                        retry.add(thread_id)
                        continue
                    unresolved.add(thread_id)
                    if reason == "catalog_identity_conflict":
                        catalog_blocked.add(thread_id)
                pending.update(retry)
                result = _apply(core, report, provider)
                results.append(result)
                if result.get("backupId"):
                    backups.append(result["backupId"])
                for item in result["verification"]["excluded"]:
                    if item["threadId"] in scanned and item["reason"] not in {
                        "archived", "non_root_agent", "rollout_unverified", "quick_repair_byte_budget"
                    }:
                        unresolved.add(item["threadId"])
                progress = bool(scanned - visited or result["changed"])
                visited.update(scanned)
                if not pending:
                    break
                if not progress:
                    stopped_reason = "no_progress"
                    break
            else:
                stopped_reason = "batch_budget" if pending else None
        except Exception as exc:
            try:
                restore_provider_repair({"backupIds": backups})
            except Exception as rollback_exc:
                failure = core.ManagerError("分批会话修复失败，已完成批次的回滚也失败；请保留会话备份。")
                failure.rollback_failed = True
                raise failure from rollback_exc
            if getattr(exc, "rollback_failed", False) or getattr(exc.__cause__, "rollback_failed", False):
                failure = core.ManagerError("分批会话修复失败，当前批次未能完整回滚；请保留会话备份。")
                failure.rollback_failed = True
                raise failure from exc
            raise core.ManagerError("分批会话修复失败，已完成批次已自动回滚。") from exc
        changed = any(result["changed"] for result in results)
        completion = {"partial": bool(pending or unresolved or missing_catalog or catalog_unavailable or stopped_reason),
                      "deferredSessions": len(unresolved | (pending or set())),
                      "catalogMissing": len(missing_catalog), "catalogBlocked": len(catalog_blocked),
                      "remainingActions": len(pending or ()),
                      "catalogRecovery": ("native_rebuild_pending" if any(
                          item["verification"]["completion"].get("catalogRecovery") == "native_rebuild_pending" for item in results)
                          else "diagnostic_only_no_insert"),
                      "followUpMode": "deep"}
        verification = dict(results[-1]["verification"]) if results else None
        if verification is not None:
            verification["completion"] = completion
        return {"changed": changed,
                "status": "partial" if changed and completion["partial"] else "deferred" if completion["partial"] else "repaired" if changed else "unchanged",
                "backupId": backups[0] if len(backups) == 1 else None, "backupIds": backups,
                "sqliteRowsUpdated": sum(item["sqliteRowsUpdated"] for item in results),
                "rolloutFilesUpdated": sum(item["rolloutFilesUpdated"] for item in results),
                "completion": completion, "verification": verification,
                "batches": len(results), "stopReason": stopped_reason,
                "workBudget": {"reservedPrefixReadBytes": reserved_scan, "plannedRepairBytes": planned_writes},
                "message": _completion_message(completion, changed)}


def restore_provider_repair(result):
    """Roll back the opaque result of repair_current_provider/auto_repair."""
    if not isinstance(result, dict):
        raise _core().ManagerError("会话修复回滚结果无效。")
    backup_ids = result.get("backupIds")
    if backup_ids is None:
        backup_ids = [result["backupId"]] if result.get("backupId") else []
    if (not isinstance(backup_ids, list) or len(backup_ids) > MAX_PROVIDER_REPAIR_BATCHES
            or any(not isinstance(value, str) or not _BACKUP_ID.fullmatch(value) for value in backup_ids)
            or len(set(backup_ids)) != len(backup_ids)):
        raise _core().ManagerError("会话修复回滚备份列表无效。")
    with _operation():
        restored = [restore(backup_id) for backup_id in reversed(backup_ids)]
    if len(restored) == 1:
        return restored[0]
    return {"restored": bool(restored), "backupIds": backup_ids,
            "preservedUnrelatedChanges": all(item["preservedUnrelatedChanges"] for item in restored)}


def restore(backup_id):
    """Undo this repair's fields using a local backup ID, never a caller path."""
    with _operation() as core:
        if not isinstance(backup_id, str) or not _BACKUP_ID.fullmatch(backup_id):
            raise core.ManagerError("无效的会话可见性备份 ID。")
        _require_stopped(core)
        parent = _backup_root(core)
        backup = _fenced(parent, parent / backup_id)
        manifest_path = _fenced(backup, backup / "manifest.json")
        if not manifest_path.is_file() or manifest_path.stat().st_size > 64 * 1024 * 1024:
            raise core.ManagerError("会话可见性备份不存在或清单无效。")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("actions"), list):
            raise core.ManagerError("会话可见性备份清单无效。")
        if Path(str(manifest.get("home") or "")).resolve() != _home(core).resolve():
            raise core.ManagerError("会话可见性备份不属于当前实例。")
        _fence_actions(core, manifest["actions"])
        if not isinstance(manifest.get("files"), list):
            raise core.ManagerError("会话可见性备份文件清单无效。")
        for item in manifest["files"]:
            if not isinstance(item, dict):
                raise core.ManagerError("会话可见性备份文件清单无效。")
            _fenced(backup, backup / str(item.get("backupPath") or ""))
            _fenced(_home(core), _home(core) / str(item.get("relativePath") or ""))
        _require_stopped(core)
        result = engine.restore_session_visibility(
            backup, confirm_codex_stopped=True, expected_codex_home=_home(core),
        )
        return {key: result[key] for key in (
            "restored", "restoreMode", "sqliteRowsRestored", "rolloutFilesRestored", "preservedUnrelatedChanges",
        )} | {"backupId": backup_id}


def list_backups():
    """List bounded, path-checked restore IDs without exposing backup payloads."""
    with _operation() as core:
        parent = _backup_root(core)
        if not parent.exists():
            return {"backups": [], "truncated": False}
        backups = []
        truncated = False
        remaining_bytes = 64 * 1024 * 1024
        with os.scandir(parent) as entries:
            for index, entry in enumerate(entries):
                if index >= 1000:
                    truncated = True
                    break
                if not _BACKUP_ID.fullmatch(entry.name):
                    continue
                try:
                    backup = _fenced(parent, parent / entry.name)
                    manifest_path = _fenced(backup, backup / "manifest.json")
                    size = manifest_path.stat().st_size
                    if size > remaining_bytes:
                        truncated = True
                        continue
                    remaining_bytes -= size
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if (not isinstance(manifest, dict)
                            or manifest.get("schemaVersion") != engine.SCHEMA_VERSION
                            or manifest.get("status") not in {"applied", "restored"}
                            or Path(str(manifest.get("home") or "")).resolve() != _home(core).resolve()):
                        continue
                    created_at = manifest.get("createdAt")
                    if not isinstance(created_at, str) or len(created_at) > 50:
                        continue
                    created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00")).isoformat()
                    backups.append({"backupId": entry.name, "createdAt": created_at})
                    if len(backups) >= 100:
                        truncated = True
                        break
                except (engine.SessionVisibilityError, OSError, ValueError, TypeError):
                    # Untrusted or incomplete entries are never offered as restore IDs.
                    continue
        backups.sort(key=lambda item: item["backupId"], reverse=True)
        return {"backups": backups, "truncated": truncated}


def auto_repair(target_provider, *, check_all_providers=False):
    """Best-effort quick repair during switching; routine failures do not block launch.

    A successful result's backupId can be passed to restore after Codex is stopped
    when the outer switch transaction needs to roll back.
    """
    core = _core()
    try:
        with _operation():
            if not isinstance(target_provider, str) or not engine.PROVIDER_PATTERN.fullmatch(target_provider):
                raise core.ManagerError("自动会话修复 target_provider 无效。")
            _require_stopped(core)
            report, provider, token = _inspect(core, "quick", None, target_provider,
                                               check_all_providers=check_all_providers)
            public = _public(report, provider, token)
            if not report["safeToRepair"] or not report["actions"]:
                ambiguous = not report["safeToRepair"] or any(
                    item.get("reason") in {"ambiguous_root_identity", "insufficient_user_evidence"}
                    for item in report["excluded"]
                )
                reason = "no_history" if not report["databases"] else "ambiguous" if ambiguous else "no_changes"
                completion = report["completion"]
                deferred = bool(report["safeToRepair"] and completion.get("partial"))
                return {"changed": False, "status": "deferred" if deferred else "skipped", "reason": "partial_scan" if deferred else reason,
                        "backupId": None, "verification": public, "completion": completion,
                        "message": _completion_message(completion, False) if deferred else ""}
            return _apply(core, report, provider)
    except core.ManagerError as exc:
        if getattr(exc.__cause__, "rollback_failed", False):
            raise
        return {"changed": False, "status": "skipped", "reason": "repair_unavailable",
                "backupId": None, "message": str(exc)}
