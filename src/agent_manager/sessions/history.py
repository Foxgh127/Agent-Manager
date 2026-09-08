"""Content-verified history synchronization with preview and rollback journals."""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import socket
import threading
import tempfile
import time
from typing import Any

import agent_manager.core as core

SYNC_LOCK = threading.Lock()
PLAN_LOCK = threading.Lock()
PLAN_CACHE: dict[str, tuple[float, tuple]] = {}
PLAN_TTL_SECONDS = 300
_DESTINATION_UNCHECKED = object()
JOURNAL_FORMAT = "agent-manager-history-transaction"
JOURNAL_VERSION = 2
RECOVERY_ID = re.compile(r"^history-sync-\d{8}-\d{6}-\d{6}$")
RECOVERY_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
MAX_RECOVERY_SCAN = 500
MAX_RECOVERIES = 100
MAX_RECOVERY_OPERATIONS = 1_000
MAX_RECOVERY_PREVIEW_ROWS = 500
MAX_RECOVERY_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_RECOVERY_FILE_BYTES = 1024 * 1024 * 1024
MAX_RECOVERY_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
_RECOVERABLE_STATUSES = {"running", "rollback_incomplete"}
_FINAL_STATUSES = {"complete", "rolled_back", "recovered"}
_JOURNAL_STATUSES = _RECOVERABLE_STATUSES | _FINAL_STATUSES
_JOURNAL_STATES = {"prepared", "written"}
_RECOVERY_ACTIONS = {
    "copy_to_local": "local",
    "replace_local": "local",
    "copy_to_remote": "remote",
    "replace_remote": "remote",
}


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _path_identity(path: Path) -> str:
    normalized = os.path.normcase(os.path.abspath(os.fspath(path.resolve(strict=False))))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _machine_binding() -> dict[str, str | int]:
    return {
        "version": 1,
        "host": hashlib.sha256(socket.gethostname().encode("utf-8", errors="replace")).hexdigest(),
        "codexHome": _path_identity(core.CODEX_HOME),
        "backupsDirectory": _path_identity(core.BACKUPS_DIR),
    }


def _fallback_integrity_key(*, create: bool) -> bytes:
    """Non-Windows test/dev fallback; production Windows journals use DPAPI."""
    key_path = core.BACKUPS_DIR / ".history-recovery.key"
    _reject_link_components(core.BACKUPS_DIR)
    _safe_path(key_path, core.BACKUPS_DIR)
    if key_path.is_file() and not _is_link_or_junction(key_path):
        raw = key_path.read_bytes()
        if len(raw) == 32:
            return raw
        raise core.ManagerError("会话恢复完整性密钥无效，旧证据只能人工处理。")
    if not create:
        raise core.ManagerError("缺少会话恢复完整性密钥，旧证据只能人工处理。")
    core.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    _reject_link_components(core.BACKUPS_DIR)
    raw = secrets.token_bytes(32)
    try:
        handle = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _fallback_integrity_key(create=False)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        key_path.unlink(missing_ok=True)
        raise
    _safe_path(key_path, core.BACKUPS_DIR)
    return raw


def _integrity_for(payload: dict, *, create: bool) -> dict[str, str]:
    canonical = _canonical_json(payload)
    if os.name == "nt":
        try:
            protected = core.dpapi_protect(canonical)
        except Exception as exc:
            raise core.ManagerError("无法创建本机绑定的会话恢复记录。") from exc
        return {"method": "windows-dpapi-v1", "protected": base64.b64encode(protected).decode("ascii")}
    key = _fallback_integrity_key(create=create)
    return {"method": "local-hmac-sha256-v1", "digest": hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()}


def _verify_integrity(document: dict) -> None:
    integrity = document.get("integrity")
    if not isinstance(integrity, dict):
        raise core.ManagerError("事务日志没有本机完整性证明。")
    payload = dict(document)
    payload.pop("integrity", None)
    canonical = _canonical_json(payload)
    method = integrity.get("method")
    if method == "windows-dpapi-v1" and os.name == "nt":
        protected = integrity.get("protected")
        if not isinstance(protected, str) or len(protected) > MAX_RECOVERY_MANIFEST_BYTES * 2:
            raise core.ManagerError("事务日志完整性证明无效。")
        try:
            decoded = base64.b64decode(protected, validate=True)
            authenticated = core.dpapi_unprotect(decoded)
        except Exception as exc:
            raise core.ManagerError("事务日志不属于当前 Windows 用户或已被篡改。") from exc
        if not secrets.compare_digest(authenticated, canonical):
            raise core.ManagerError("事务日志已被篡改。")
        return
    if method == "local-hmac-sha256-v1" and os.name != "nt":
        digest = integrity.get("digest")
        if not isinstance(digest, str) or not RECOVERY_FINGERPRINT.fullmatch(digest):
            raise core.ManagerError("事务日志完整性证明无效。")
        expected = hmac.new(_fallback_integrity_key(create=False), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
        if not secrets.compare_digest(digest, expected):
            raise core.ManagerError("事务日志已被篡改。")
        return
    raise core.ManagerError("事务日志的本机完整性证明不受支持。")


def _write_protected_json(path: Path, payload: dict) -> None:
    document = dict(payload)
    document.pop("integrity", None)
    document["integrity"] = _integrity_for(document, create=True)
    core.atomic_write_json(path, document)
    payload["integrity"] = document["integrity"]


def _write_journal(path: Path, journal: dict) -> None:
    _safe_path(path, core.BACKUPS_DIR)
    _write_protected_json(path, journal)


def _options(payload: dict) -> tuple[str, str, int]:
    if not isinstance(payload, dict):
        raise core.ManagerError("历史同步参数必须是对象。")
    raw_days = payload.get("recentDays", 30)
    try:
        if isinstance(raw_days, bool) or (isinstance(raw_days, float) and not raw_days.is_integer()):
            raise ValueError()
        days = int(raw_days)
    except (TypeError, ValueError, OverflowError) as exc:
        raise core.ManagerError("历史同步天数必须是 0 到 3650 之间的整数。") from exc
    return str(payload.get("target") or ""), str(payload.get("direction") or "two_way"), days


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _reject_link_components(path: Path) -> None:
    current = Path(os.path.abspath(path))
    while True:
        if _is_link_or_junction(current):
            raise core.ManagerError("会话同步不跟随符号链接或目录联接。")
        if current == current.parent:
            return
        current = current.parent


def _validate_requested_root(target: str) -> None:
    raw = Path(target).expanduser()
    candidate = raw if raw.name == core.HISTORY_SYNC_FOLDER else raw / core.HISTORY_SYNC_FOLDER
    _reject_link_components(candidate)


def _safe_path(path: Path, root: Path) -> None:
    raw_root = Path(os.path.abspath(root))
    raw_path = Path(os.path.abspath(path))
    if not raw_path.is_relative_to(raw_root):
        raise core.ManagerError("会话同步路径超出所选目录，已停止操作。")
    current = raw_path
    while True:
        if _is_link_or_junction(current):
            raise core.ManagerError("会话同步不跟随符号链接或目录联接。")
        if current == raw_root:
            break
        current = current.parent
    resolved_root = raw_root.resolve(strict=False)
    if not raw_path.resolve(strict=False).is_relative_to(resolved_root):
        raise core.ManagerError("会话同步路径超出所选目录，已停止操作。")


def _fingerprint(root: Path, direction: str, days: int, operations: list[dict]) -> str:
    rows = [
        {
            key: row.get(key)
            for key in ("action", "relative", "bytes", "sourceHash", "sourceMtimeNs", "destinationHash")
        }
        for row in operations
    ]
    return hashlib.sha256(json.dumps([str(root), direction, days, rows], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _plan(payload: dict) -> tuple[str, str, int, Path, list[dict], str]:
    target, direction, days = _options(payload)
    _validate_requested_root(target)
    root, operations = core._history_plan(target, direction, days)
    for item in operations:
        if item["action"] == "conflict":
            continue
        to_local = item["action"] in {"copy_to_local", "replace_local"}
        _safe_path(item["source"], root if to_local else core.CODEX_HOME)
        _safe_path(item["destination"], core.CODEX_HOME if to_local else root)
    return target, direction, days, root, operations, _fingerprint(root, direction, days, operations)


def preview(payload: dict) -> dict:
    plan = _plan(payload)
    _target, direction, days, root, operations, fingerprint = plan
    with PLAN_LOCK:
        for key in list(PLAN_CACHE):
            if time.monotonic() - PLAN_CACHE[key][0] > PLAN_TTL_SECONDS:
                PLAN_CACHE.pop(key)
        while len(PLAN_CACHE) >= 10:
            PLAN_CACHE.pop(next(iter(PLAN_CACHE)))
        PLAN_CACHE[fingerprint] = (time.monotonic(), plan)
    counts: dict[str, int] = {}
    for row in operations:
        counts[row["action"]] = counts.get(row["action"], 0) + 1
    return {
        "target": str(root), "direction": direction, "recentDays": days,
        "fingerprint": fingerprint, "counts": counts,
        "copyBytes": sum(row["bytes"] for row in operations if row["action"] != "conflict"),
        "operations": [{key: row[key] for key in ("action", "relative", "bytes", "reason")} for row in operations[:200]],
        "truncated": len(operations) > 200,
        "requiresCodexClosed": bool(counts.get("copy_to_local") or counts.get("replace_local")),
        "snapshotAt": core.now_iso(),
    }


def _matches(path: Path, expected: str | None) -> bool:
    if path.is_symlink():
        return False
    if expected is None:
        return not path.exists()
    return path.is_file() and secrets.compare_digest(core._hash_file(path), expected)


def _matches_source(row: dict) -> bool:
    path = row["source"]
    return (not _is_link_or_junction(path) and path.is_file() and path.stat().st_size >= row["bytes"]
            and secrets.compare_digest(core._hash_file_prefix(path, row["bytes"]), row["sourceHash"]))


def _copy_snapshot(
    source: Path,
    destination: Path,
    size: int,
    expected_hash: str,
    mtime_ns: int | None = None,
    expected_destination_hash: str | None | object = _DESTINATION_UNCHECKED,
    destination_root: Path | None = None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".history-", suffix=".sync", dir=destination.parent)
    temp_path = Path(temporary)
    try:
        digest = hashlib.sha256()
        remaining = size
        with os.fdopen(handle, "wb") as target, source.open("rb") as stream:
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise core.ManagerError("会话在复制期间变短，未发布不完整备份。")
                target.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            target.flush()
            os.fsync(target.fileno())
        if not secrets.compare_digest(digest.hexdigest(), expected_hash):
            raise core.ManagerError("会话内容在复制期间被改写，未发布不完整备份。")
        if mtime_ns is not None:
            os.utime(temp_path, ns=(mtime_ns, mtime_ns))
        # The source copy can take seconds. Revalidate after it completes so
        # an editor or cloud client cannot be silently overwritten in that gap.
        if destination_root is not None:
            _safe_path(destination, destination_root)
        if expected_destination_hash is not _DESTINATION_UNCHECKED and not _matches(
            destination, expected_destination_hash
        ):
            raise core.ManagerError("目标会话已被其他程序修改，未覆盖新内容。")
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)


def perform(payload: dict) -> dict:
    if not SYNC_LOCK.acquire(blocking=False):
        raise core.ManagerError("另一个会话同步正在执行，请等待完成后重试。")
    try:
        return _perform_locked(payload)
    finally:
        SYNC_LOCK.release()


def _perform_locked(payload: dict) -> dict:
    requested_target, requested_direction, requested_days = _options(payload)
    expected = str(payload.get("expectedFingerprint") or "")
    cached = None
    if expected:
        with PLAN_LOCK:
            cached = PLAN_CACHE.get(expected)
        if cached is None or time.monotonic() - cached[0] > PLAN_TTL_SECONDS:
            with PLAN_LOCK:
                if PLAN_CACHE.get(expected) is cached:
                    PLAN_CACHE.pop(expected, None)
            raise core.ManagerError("同步预览已过期或已执行，请重新预览。")
        target, direction, days, root, operations, fingerprint = cached[1]
        _validate_requested_root(requested_target)
        if (core._history_sync_root(requested_target) != root or requested_direction != direction or requested_days != days):
            raise core.ManagerError("同步参数与预览不同，请重新预览。")
    else:
        target, direction, days, root, operations, fingerprint = _plan(payload)
    local_writes = any(row["action"] in {"copy_to_local", "replace_local"} for row in operations)
    if local_writes and core.running_codex_processes():
        raise core.ManagerError("拉取会写入本机会话。请先关闭 Codex，或改用仅推送进行备份。")
    root.mkdir(parents=True, exist_ok=True)
    if expected:
        # Consume only once all non-mutating preconditions pass. Failed option
        # validation and a still-running Codex can reuse the reviewed plan.
        with PLAN_LOCK:
            if PLAN_CACHE.get(expected) is cached:
                PLAN_CACHE.pop(expected, None)
    transaction_id = "history-sync-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    journal_root = core.BACKUPS_DIR / transaction_id
    journal_path = journal_root / "manifest.json"
    journal: dict[str, Any] = {
        "format": JOURNAL_FORMAT, "version": JOURNAL_VERSION,
        "id": transaction_id, "createdAt": core.now_iso(), "status": "running",
        "machineBinding": _machine_binding(),
        "target": str(root), "direction": direction, "operations": [],
    }
    completed: list[dict] = []
    rollback_records: list[dict] = []
    changes = [row for row in operations if row["action"] != "conflict"]
    try:
        if changes:
            core.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
            _reject_link_components(core.BACKUPS_DIR)
            journal_root.mkdir(exist_ok=False)
            _safe_path(journal_root, core.BACKUPS_DIR)
            _write_journal(journal_path, journal)
        for row in changes:
            source, destination = row["source"], row["destination"]
            to_local = row["action"] in {"copy_to_local", "replace_local"}
            destination_root = core.CODEX_HOME if to_local else root
            _safe_path(source, root if to_local else core.CODEX_HOME)
            _safe_path(destination, destination_root)
            if not _matches_source(row) or not _matches(destination, row["destinationHash"]):
                raise core.ManagerError("会话文件在预览后发生变化，已停止并开始恢复。")
            backup = journal_root / ("local" if to_local else "remote") / row["relative"] if row["destinationHash"] else None
            if backup is not None:
                _safe_path(backup, journal_root)
                core._atomic_copy_file(destination, backup)
                if not _matches(backup, row["destinationHash"]):
                    raise core.ManagerError("会话备份校验失败，未覆盖目标文件。")
            # Write-ahead intent makes an interrupted process diagnosable.
            side = "local" if to_local else "remote"
            before_stat = backup.stat() if backup is not None else None
            record = {
                "action": row["action"], "side": side, "relative": row["relative"],
                "destination": str(destination), "destinationRoot": str(destination_root),
                "backup": str(backup) if backup else None,
                "backupRelative": f"{side}/{row['relative']}" if backup else None,
                "beforeHash": row["destinationHash"],
                "beforeBytes": before_stat.st_size if before_stat else 0,
                "beforeMtimeNs": before_stat.st_mtime_ns if before_stat else None,
                "afterHash": row["sourceHash"], "afterBytes": row["bytes"],
                "afterMtimeNs": row.get("sourceMtimeNs"), "state": "prepared",
            }
            journal["operations"].append(record)
            _write_journal(journal_path, journal)
            rollback_records.append({**row, "backup": backup, "writtenHash": row["sourceHash"],
                                     "destinationRoot": destination_root})
            # Re-check after making a backup; other processes can write while
            # a large file is being copied even with our local sync gate held.
            if not _matches(destination, row["destinationHash"]):
                raise core.ManagerError("目标会话已被其他程序修改，未覆盖新内容。")
            _copy_snapshot(source, destination, row["bytes"], row["sourceHash"], row.get("sourceMtimeNs"),
                           row["destinationHash"], destination_root)
            completed.append({**row, "backup": backup, "writtenHash": row["sourceHash"], "destinationRoot": destination_root})
            if not _matches(destination, row["sourceHash"]):
                raise core.ManagerError("目标文件在复制后被修改，已停止并保留外部修改。")
            record["state"] = "written"
            _write_journal(journal_path, journal)
        current_files = core._history_file_map(root, 0, remote=True)
        core.atomic_write_json(root / "manifest.json", {
            "format": core.HISTORY_SYNC_FOLDER, "version": 1, "updatedAt": core.now_iso(),
            "machine": hashlib.sha256(socket.gethostname().encode()).hexdigest()[:12],
            "files": len(current_files), "bytes": sum(path.stat().st_size for path in current_files.values()),
        })
    except BaseException as exc:
        rollback_errors = []
        for row in reversed(rollback_records):
            try:
                destination = row["destination"]
                _safe_path(destination, row["destinationRoot"])
                if _matches(destination, row["destinationHash"]):
                    continue
                if not _matches(destination, row["writtenHash"]):
                    raise core.ManagerError("目标出现外部修改，保留新内容与备份")
                if row["backup"] is not None:
                    core._atomic_copy_file(row["backup"], destination)
                else:
                    destination.unlink(missing_ok=True)
            except Exception as restore_error:
                rollback_errors.append(f"{row['relative']}：{core._redact_sensitive_text(restore_error, limit=180)}")
        journal.update({"status": "rollback_incomplete" if rollback_errors else "rolled_back", "errors": rollback_errors})
        if changes:
            try:
                _write_journal(journal_path, journal)
            except Exception:
                pass
        if not isinstance(exc, Exception):
            raise
        detail = "；".join(rollback_errors) if rollback_errors else "已恢复本次已写入的会话文件"
        raise core.ManagerError(f"会话同步未完成：{core._redact_sensitive_text(exc, limit=220)}；{detail}。") from exc

    pulled = sum(row["action"] in {"copy_to_local", "replace_local"} for row in completed)
    warning = None
    index = None
    if pulled:
        try:
            index = core.refresh_codex_history_index()
        except core.ManagerError as exc:
            warning = core._redact_sensitive_text(exc, limit=320)
    summary = {"completed": len(completed), "conflicts": sum(row["action"] == "conflict" for row in operations),
               "copiedBytes": sum(row["bytes"] for row in completed), "pulled": pulled, "pushed": len(completed) - pulled,
               "index": index, "warning": warning, "backupId": transaction_id if changes else None,
               "backupPath": str(journal_root) if changes else None}
    if changes:
        journal.update({"status": "complete", "completedAt": core.now_iso(), "summary": summary})
        try:
            _write_journal(journal_path, journal)
        except (OSError, core.ManagerError) as exc:
            summary["warning"] = f"会话文件已同步，但完成记录未能写入：{core._redact_sensitive_text(exc, limit=180)}"
    try:
        with core.SETTINGS_LOCK:
            settings = core.load_settings()
            settings["historySync"] = {"target": target, "recentDays": days, "direction": direction, "lastSyncedAt": core.now_iso(), "lastSummary": summary}
            core.save_settings(settings)
    except (OSError, core.ManagerError) as exc:
        summary["warning"] = "；".join(filter(None, [summary["warning"], f"会话文件已同步，但设置记录未能保存：{core._redact_sensitive_text(exc, limit=180)}"]))
    return summary


def _recovery_root(recovery_id: str) -> Path:
    if not isinstance(recovery_id, str) or not RECOVERY_ID.fullmatch(recovery_id):
        raise core.ManagerError("会话恢复记录编号无效。")
    _reject_link_components(core.BACKUPS_DIR)
    root = core.BACKUPS_DIR / recovery_id
    _safe_path(root, core.BACKUPS_DIR)
    if _is_link_or_junction(root):
        raise core.ManagerError("会话恢复记录是符号链接或目录联接，只能人工处理。")
    if not root.is_dir():
        raise core.ManagerError("会话恢复记录不存在。")
    return root


def _read_recovery_document(root: Path) -> tuple[dict, bytes]:
    path = root / "manifest.json"
    _safe_path(path, core.BACKUPS_DIR)
    if _is_link_or_junction(path) or not path.is_file():
        raise core.ManagerError("事务日志缺失或是链接，只能人工处理。")
    try:
        size = path.stat().st_size
        if size <= 0 or size > MAX_RECOVERY_MANIFEST_BYTES:
            raise core.ManagerError("事务日志大小超出自动恢复限制，只能人工处理。")
        raw = path.read_bytes()
        if len(raw) != size or _is_link_or_junction(path):
            raise core.ManagerError("事务日志在读取期间发生变化，只能人工处理。")
        document = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_json_object)
    except core.ManagerError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise core.ManagerError("事务日志无法安全读取，只能人工处理。") from exc
    if not isinstance(document, dict):
        raise core.ManagerError("事务日志格式无效，只能人工处理。")
    return document, raw


def _validated_relative(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or len(value) > 600 or "\\" in value or any(ord(ch) < 32 for ch in value):
        raise core.ManagerError("事务日志包含无效的会话相对路径。")
    relative = PurePosixPath(value)
    if relative.is_absolute() or relative.as_posix() != value or len(relative.parts) < 2:
        raise core.ManagerError("事务日志包含路径穿越内容。")
    if relative.parts[0] not in {"sessions", "archived_sessions"} or any(part in {"", ".", ".."} for part in relative.parts):
        raise core.ManagerError("事务日志包含路径穿越内容。")
    if not relative.name.lower().endswith(".jsonl"):
        raise core.ManagerError("事务日志包含非会话文件。")
    return relative.parts


def _validated_hash(value: object, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not RECOVERY_FINGERPRINT.fullmatch(value):
        raise core.ManagerError("事务日志包含无效的文件校验值。")
    return value


def _validated_size(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_RECOVERY_FILE_BYTES:
        raise core.ManagerError(f"事务日志中的{name}超出自动恢复限制。")
    return value


def _validated_journal(recovery_id: str) -> tuple[dict, Path, bytes, list[dict]]:
    root = _recovery_root(recovery_id)
    document, raw = _read_recovery_document(root)
    if document.get("format") != JOURNAL_FORMAT:
        raise core.ManagerError("该目录不是受支持的会话同步事务日志，只能人工处理。")
    if document.get("version") != JOURNAL_VERSION:
        if document.get("version") == 1:
            raise core.ManagerError("旧版事务日志没有本机完整性证明，只能人工处理，未执行自动恢复。")
        raise core.ManagerError("事务日志版本不受支持，只能人工处理。")
    if document.get("id") != recovery_id:
        raise core.ManagerError("事务日志编号与受控目录不一致，疑似被篡改。")
    _verify_integrity(document)
    if document.get("machineBinding") != _machine_binding():
        raise core.ManagerError("事务日志不属于当前管理器目录或本机环境。")
    status = document.get("status")
    if status not in _JOURNAL_STATUSES:
        raise core.ManagerError("事务日志状态无效，只能人工处理。")
    created_at = document.get("createdAt")
    if not isinstance(created_at, str) or not created_at or len(created_at) > 80:
        raise core.ManagerError("事务日志创建时间无效，只能人工处理。")
    direction = document.get("direction")
    if direction not in {"two_way", "push", "pull"}:
        raise core.ManagerError("事务日志同步方向无效，只能人工处理。")
    target_value = document.get("target")
    if not isinstance(target_value, str) or not target_value or len(target_value) > 32_768:
        raise core.ManagerError("事务日志同步目标无效，只能人工处理。")
    try:
        target = core._history_sync_root(target_value, create=False)
        _reject_link_components(target)
    except (OSError, RuntimeError, core.ManagerError) as exc:
        raise core.ManagerError("事务日志同步目标不再是安全的受控路径，只能人工处理。") from exc
    if _path_identity(target) != _path_identity(Path(target_value)):
        raise core.ManagerError("事务日志同步目标已改变，只能人工处理。")
    raw_operations = document.get("operations")
    if not isinstance(raw_operations, list) or len(raw_operations) > MAX_RECOVERY_OPERATIONS:
        raise core.ManagerError("事务日志操作数量超出自动恢复限制。")
    operations = []
    destinations: set[str] = set()
    declared_total = 0
    for raw_operation in raw_operations:
        if not isinstance(raw_operation, dict):
            raise core.ManagerError("事务日志操作格式无效。")
        action = raw_operation.get("action")
        expected_side = _RECOVERY_ACTIONS.get(action)
        side = raw_operation.get("side")
        if expected_side is None or side != expected_side:
            raise core.ManagerError("事务日志操作方向无效。")
        parts = _validated_relative(raw_operation.get("relative"))
        relative = PurePosixPath(*parts).as_posix()
        destination_root = core.CODEX_HOME if side == "local" else target
        destination = destination_root.joinpath(*parts)
        _safe_path(destination, destination_root)
        if _path_identity(Path(str(raw_operation.get("destinationRoot") or ""))) != _path_identity(destination_root):
            raise core.ManagerError("事务日志目标根目录不一致，疑似被篡改。")
        if _path_identity(Path(str(raw_operation.get("destination") or ""))) != _path_identity(destination):
            raise core.ManagerError("事务日志目标文件不一致，疑似被篡改。")
        destination_key = os.path.normcase(os.path.abspath(destination))
        if destination_key in destinations:
            raise core.ManagerError("事务日志重复指向同一会话文件。")
        destinations.add(destination_key)
        before_hash = _validated_hash(raw_operation.get("beforeHash"), optional=True)
        after_hash = _validated_hash(raw_operation.get("afterHash"))
        before_bytes = _validated_size(raw_operation.get("beforeBytes"), name="备份大小")
        after_bytes = _validated_size(raw_operation.get("afterBytes"), name="同步后大小")
        before_mtime = raw_operation.get("beforeMtimeNs")
        after_mtime = raw_operation.get("afterMtimeNs")
        if before_mtime is not None and (isinstance(before_mtime, bool) or not isinstance(before_mtime, int) or before_mtime < 0):
            raise core.ManagerError("事务日志备份时间无效。")
        if after_mtime is not None and (isinstance(after_mtime, bool) or not isinstance(after_mtime, int) or after_mtime < 0):
            raise core.ManagerError("事务日志同步时间无效。")
        backup_relative = raw_operation.get("backupRelative")
        backup_value = raw_operation.get("backup")
        backup = None
        if before_hash is None:
            if before_bytes != 0 or before_mtime is not None or backup_relative is not None or backup_value is not None:
                raise core.ManagerError("事务日志对原本不存在的文件声明了伪造备份。")
        else:
            expected_backup_relative = f"{side}/{relative}"
            if backup_relative != expected_backup_relative:
                raise core.ManagerError("事务日志备份相对路径不一致，疑似被篡改。")
            backup = root.joinpath(*PurePosixPath(expected_backup_relative).parts)
            _safe_path(backup, root)
            if _path_identity(Path(str(backup_value or ""))) != _path_identity(backup):
                raise core.ManagerError("事务日志备份路径不一致，疑似被篡改。")
        if raw_operation.get("state") not in _JOURNAL_STATES:
            raise core.ManagerError("事务日志写入状态无效。")
        if before_hash is not None and secrets.compare_digest(before_hash, after_hash):
            raise core.ManagerError("事务日志包含没有内容变化的写入。")
        declared_total += before_bytes + after_bytes
        if declared_total > MAX_RECOVERY_TOTAL_BYTES:
            raise core.ManagerError("事务日志文件总量超出自动恢复限制。")
        operations.append({
            **raw_operation,
            "relative": relative,
            "side": side,
            "destinationRootPath": destination_root,
            "destinationPath": destination,
            "backupPath": backup,
            "beforeHash": before_hash,
            "afterHash": after_hash,
            "beforeBytes": before_bytes,
            "afterBytes": after_bytes,
        })
    return document, root, raw, operations


def _manual_recovery_entry(recovery_id: str, root: Path, reason: str, document: dict | None = None) -> dict:
    created_at = document.get("createdAt") if isinstance(document, dict) else None
    if not isinstance(created_at, str) or len(created_at) > 80:
        created_at = None
    return {
        "id": recovery_id,
        "status": "manual_review",
        "createdAt": created_at,
        "recoverable": False,
        "reason": core._redact_sensitive_text(reason, limit=240),
        "operationCount": None,
        "evidencePath": str(root),
    }


def list_recoveries(limit: int = 50) -> dict:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > MAX_RECOVERIES:
        raise core.ManagerError(f"恢复记录数量必须是 1 到 {MAX_RECOVERIES} 之间的整数。")
    directory = core.BACKUPS_DIR
    _reject_link_components(directory)
    if not directory.exists():
        return {"recoveries": [], "invalidCount": 0, "truncated": False, "directory": str(directory)}
    if not directory.is_dir() or _is_link_or_junction(directory):
        raise core.ManagerError("会话备份目录不是安全的本机目录。")
    candidates: list[str] = []
    scan_truncated = False
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not RECOVERY_ID.fullmatch(entry.name):
                    continue
                if len(candidates) >= MAX_RECOVERY_SCAN:
                    scan_truncated = True
                    break
                candidates.append(entry.name)
    except OSError as exc:
        raise core.ManagerError("无法读取会话恢复记录目录。") from exc
    result = []
    invalid = 0
    for recovery_id in sorted(candidates, reverse=True):
        root = directory / recovery_id
        document = None
        try:
            if _is_link_or_junction(root) or not root.is_dir():
                raise core.ManagerError("恢复记录目录是链接或不是普通目录，只能人工处理。")
            document, _raw = _read_recovery_document(root)
            journal, _root, _raw, operations = _validated_journal(recovery_id)
            if journal["status"] in _FINAL_STATUSES:
                continue
            result.append({
                "id": recovery_id,
                "status": journal["status"],
                "createdAt": journal["createdAt"],
                "recoverable": True,
                "reason": "检测到未完成的会话同步，可先预览再恢复。",
                "operationCount": len(operations),
                "evidencePath": str(root),
            })
        except (core.ManagerError, OSError, ValueError, TypeError) as exc:
            invalid += 1
            result.append(_manual_recovery_entry(recovery_id, root, str(exc), document))
    truncated = scan_truncated or len(result) > limit
    return {"recoveries": result[:limit], "invalidCount": invalid, "truncated": truncated, "directory": str(directory)}


def _stable_file_snapshot(path: Path, root: Path, budget: list[int]) -> dict[str, Any]:
    _safe_path(path, root)
    if _is_link_or_junction(path):
        raise core.ManagerError("恢复目标或备份是符号链接/目录联接，未读取。")
    if not path.exists():
        return {"hash": None, "bytes": 0, "mtimeNs": None}
    if not path.is_file():
        raise core.ManagerError("恢复目标或备份不是普通文件。")
    try:
        before = path.stat()
        if before.st_size > MAX_RECOVERY_FILE_BYTES:
            raise core.ManagerError("恢复文件大小超出自动处理限制。")
        budget[0] += before.st_size
        if budget[0] > MAX_RECOVERY_TOTAL_BYTES:
            raise core.ManagerError("恢复预览总读取量超出自动处理限制。")
        first = core._hash_file(path)
        middle = path.stat()
        second = core._hash_file(path)
        after = path.stat()
    except core.ManagerError:
        raise
    except OSError as exc:
        raise core.ManagerError("无法稳定读取恢复目标或备份。") from exc
    stamps = {(item.st_size, item.st_mtime_ns) for item in (before, middle, after)}
    if len(stamps) != 1 or not secrets.compare_digest(first, second):
        raise core.ManagerError("恢复目标或备份在读取期间发生变化，请稍后重试。")
    _safe_path(path, root)
    return {"hash": first, "bytes": before.st_size, "mtimeNs": before.st_mtime_ns}


def _preview_recovery_loaded(document: dict, root: Path, operations: list[dict]) -> tuple[dict, list[dict]]:
    if document["status"] not in _RECOVERABLE_STATUSES:
        raise core.ManagerError("该同步事务已经结束，不需要自动恢复。")
    budget = [0]
    rows = []
    public_rows = []
    for operation in operations:
        backup_info = {"hash": None, "bytes": 0, "mtimeNs": None}
        if operation["backupPath"] is not None:
            backup_info = _stable_file_snapshot(operation["backupPath"], root, budget)
            if (backup_info["hash"] != operation["beforeHash"] or
                    backup_info["bytes"] != operation["beforeBytes"]):
                raise core.ManagerError(f"原始备份校验失败：{operation['relative']}；只能人工处理。")
        current_info = _stable_file_snapshot(operation["destinationPath"], operation["destinationRootPath"], budget)
        current_hash = current_info["hash"]
        before_hash = operation["beforeHash"]
        after_hash = operation["afterHash"]
        if current_hash == before_hash:
            action = "unchanged"
        elif current_hash == after_hash:
            action = "remove" if before_hash is None else "restore"
        else:
            action = "conflict"
        row = {**operation, "current": current_info, "backupInfo": backup_info, "recoveryAction": action}
        rows.append(row)
        public_rows.append({
            "side": operation["side"], "relative": operation["relative"], "state": operation["state"],
            "currentHash": current_hash, "currentBytes": current_info["bytes"],
            "currentMtimeNs": current_info["mtimeNs"],
            "backupHash": backup_info["hash"], "backupBytes": backup_info["bytes"],
            "backupMtimeNs": backup_info["mtimeNs"], "syncedHash": after_hash,
            "action": action, "conflict": action == "conflict",
        })
    unsigned = dict(document)
    unsigned.pop("integrity", None)
    fingerprint_payload = {
        "format": JOURNAL_FORMAT,
        "version": JOURNAL_VERSION,
        "journal": hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest(),
        "files": [
            {
                "side": row["side"], "relative": row["relative"], "currentHash": row["currentHash"],
                "currentBytes": row["currentBytes"], "currentMtimeNs": row["currentMtimeNs"],
                "backupHash": row["backupHash"], "backupBytes": row["backupBytes"],
                "backupMtimeNs": row["backupMtimeNs"], "action": row["action"],
            }
            for row in public_rows
        ],
    }
    fingerprint = hashlib.sha256(_canonical_json(fingerprint_payload).encode("utf-8")).hexdigest()
    changed = sum(row["action"] in {"restore", "remove"} for row in public_rows)
    conflicts = sum(row["conflict"] for row in public_rows)
    preview_result = {
        "recovery": {
            "id": document["id"], "status": document["status"], "createdAt": document["createdAt"],
            "target": document["target"], "direction": document["direction"], "operationCount": len(operations),
        },
        "files": public_rows[:MAX_RECOVERY_PREVIEW_ROWS],
        "truncated": len(public_rows) > MAX_RECOVERY_PREVIEW_ROWS,
        "changed": changed,
        "conflicts": conflicts,
        "fingerprint": fingerprint,
        "requiresCodexClosed": True,
    }
    return preview_result, rows


def preview_recovery(recovery_id: str) -> dict:
    document, root, _raw, operations = _validated_journal(recovery_id)
    result, _rows = _preview_recovery_loaded(document, root, operations)
    return result


def _snapshot_matches(path: Path, root: Path, expected: dict[str, Any]) -> bool:
    try:
        return _stable_file_snapshot(path, root, [0]) == expected
    except core.ManagerError:
        return False


def _post_recovery_snapshot(row: dict) -> dict[str, Any]:
    if row["recoveryAction"] == "unchanged":
        return row["current"]
    if row["beforeHash"] is None:
        return {"hash": None, "bytes": 0, "mtimeNs": None}
    return {
        "hash": row["beforeHash"],
        "bytes": row["beforeBytes"],
        "mtimeNs": row.get("beforeMtimeNs"),
    }


def _new_safety_root() -> tuple[str, Path]:
    core.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    _reject_link_components(core.BACKUPS_DIR)
    for _attempt in range(10):
        safety_id = "history-recovery-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + secrets.token_hex(4)
        root = core.BACKUPS_DIR / safety_id
        try:
            root.mkdir()
        except FileExistsError:
            continue
        _safe_path(root, core.BACKUPS_DIR)
        return safety_id, root
    raise core.ManagerError("无法创建唯一的恢复前安全备份目录。")


def _create_recovery_safety(
    recovery_id: str,
    source_root: Path,
    source_manifest_raw: bytes,
    rows: list[dict],
) -> tuple[dict, Path]:
    safety_id, safety_root = _new_safety_root()
    try:
        source_copy = safety_root / "source-manifest.json"
        _safe_path(source_copy, safety_root)
        core.atomic_write_bytes(source_copy, source_manifest_raw)
        source_hash = hashlib.sha256(source_manifest_raw).hexdigest()
        if not _matches(source_copy, source_hash):
            raise core.ManagerError("原始事务日志证据复制校验失败。")
        files = []
        for row in rows:
            if row["recoveryAction"] not in {"restore", "remove"}:
                continue
            destination = row["destinationPath"]
            current = row["current"]
            evidence_relative = f"current/{row['side']}/{row['relative']}"
            evidence = safety_root.joinpath(*PurePosixPath(evidence_relative).parts)
            _safe_path(evidence, safety_root)
            _copy_snapshot(
                destination, evidence, current["bytes"], current["hash"], current["mtimeNs"],
                None, safety_root,
            )
            if not _matches(evidence, current["hash"]):
                raise core.ManagerError(f"恢复前内容备份校验失败：{row['relative']}")
            if not _matches(destination, current["hash"]):
                raise core.ManagerError(f"恢复目标在安全备份期间发生变化：{row['relative']}")
            files.append({
                "side": row["side"], "relative": row["relative"], "present": True,
                "size": current["bytes"], "sha256": current["hash"], "evidenceRelative": evidence_relative,
            })
        safety = {
            "format": "agent-manager-history-recovery-safety", "version": 1,
            "id": safety_id, "createdAt": core.now_iso(), "status": "prepared",
            "machineBinding": _machine_binding(), "sourceRecoveryId": recovery_id,
            "sourceEvidence": "source-manifest.json", "sourceManifestHash": source_hash,
            "sourcePathHash": _path_identity(source_root), "files": files,
        }
        _write_protected_json(safety_root / "manifest.json", safety)
        stored_safety, _stored_raw = _read_recovery_document(safety_root)
        _verify_integrity(stored_safety)
        if stored_safety != safety or not _matches(source_copy, source_hash):
            raise core.ManagerError("恢复前安全证据发布校验失败。")
        return safety, safety_root
    except BaseException as exc:
        if isinstance(exc, Exception):
            raise core.ManagerError(
                f"恢复前安全备份未完成，目标文件未修改。人工证据保留在：{safety_root}"
            ) from exc
        raise


def _rollback_recovery(rows: list[dict], attempted: list[dict], safety_root: Path) -> list[str]:
    errors = []
    for row in reversed(attempted):
        try:
            destination = row["destinationPath"]
            current_hash = row["current"]["hash"]
            if _snapshot_matches(destination, row["destinationRootPath"], row["current"]):
                continue
            allowed_snapshot = _post_recovery_snapshot(row)
            if not _snapshot_matches(destination, row["destinationRootPath"], allowed_snapshot):
                raise core.ManagerError("检测到外部修改，已保留该内容与恢复前证据")
            evidence_relative = f"current/{row['side']}/{row['relative']}"
            evidence = safety_root.joinpath(*PurePosixPath(evidence_relative).parts)
            _safe_path(evidence, safety_root)
            _copy_snapshot(
                evidence, destination, row["current"]["bytes"], current_hash, row["current"]["mtimeNs"],
                allowed_snapshot["hash"], row["destinationRootPath"],
            )
            if not _snapshot_matches(destination, row["destinationRootPath"], row["current"]):
                raise core.ManagerError("恢复操作回滚校验失败")
        except Exception as exc:
            errors.append(f"{row['relative']}：{core._redact_sensitive_text(exc, limit=160)}")
    return errors


def _acquire_recovery_locks() -> list[Any]:
    # Keep the same core lock order as configuration recovery and acquire all
    # gates without waiting. This avoids an account switch launching Codex
    # after the closed-process check and cannot deadlock with an active sync.
    acquired = []
    for lock, message in (
        (core.SWITCH_OPERATION_LOCK, "账号切换或恢复操作正在执行，请完成后重试。"),
        (core.SETTINGS_LOCK, "管理器设置正在更新，请完成后重试。"),
        (SYNC_LOCK, "另一个会话同步或恢复正在执行，请等待完成后重试。"),
    ):
        if not lock.acquire(blocking=False):
            for held in reversed(acquired):
                held.release()
            raise core.ManagerError(message)
        acquired.append(lock)
    return acquired


def restore_recovery(recovery_id: str, expected_fingerprint: str) -> dict:
    if not isinstance(expected_fingerprint, str) or not RECOVERY_FINGERPRINT.fullmatch(expected_fingerprint):
        raise core.ManagerError("请先预览会话恢复内容。")
    acquired_locks = _acquire_recovery_locks()
    try:
        document, root, source_manifest_raw, operations = _validated_journal(recovery_id)
        current_preview, rows = _preview_recovery_loaded(document, root, operations)
        if not secrets.compare_digest(current_preview["fingerprint"], expected_fingerprint):
            raise core.ManagerError("会话文件或事务证据在预览后已变化，请重新预览。")
        if core.running_codex_processes():
            raise core.ManagerError("恢复会话前请先关闭 Codex，当前任务不会被自动结束。")
        if current_preview["conflicts"]:
            raise core.ManagerError("部分会话在同步后被其他程序修改，已保留现有内容；请人工处理冲突。")
        # Recheck after the process gate so waiting for Codex to exit cannot
        # make a stale preview authoritative.
        latest_preview, latest_rows = _preview_recovery_loaded(document, root, operations)
        if not secrets.compare_digest(latest_preview["fingerprint"], expected_fingerprint):
            raise core.ManagerError("会话文件或事务证据在预览后已变化，请重新预览。")
        safety, safety_root = _create_recovery_safety(recovery_id, root, source_manifest_raw, latest_rows)
        attempted: list[dict] = []
        try:
            for row in latest_rows:
                action = row["recoveryAction"]
                if action not in {"restore", "remove"}:
                    continue
                if not _snapshot_matches(row["destinationPath"], row["destinationRootPath"], row["current"]):
                    raise core.ManagerError(f"目标在恢复期间被其他程序修改：{row['relative']}")
                attempted.append(row)
                if action == "restore":
                    backup = row["backupPath"]
                    if not _snapshot_matches(backup, root, row["backupInfo"]):
                        raise core.ManagerError(f"原始备份在恢复期间被修改：{row['relative']}")
                    _copy_snapshot(
                        backup, row["destinationPath"], row["beforeBytes"], row["beforeHash"],
                        row.get("beforeMtimeNs"), row["current"]["hash"], row["destinationRootPath"],
                    )
                else:
                    _safe_path(row["destinationPath"], row["destinationRootPath"])
                    if not _matches(row["destinationPath"], row["current"]["hash"]):
                        raise core.ManagerError(f"目标在移除前被其他程序修改：{row['relative']}")
                    row["destinationPath"].unlink()
                if not _snapshot_matches(
                    row["destinationPath"], row["destinationRootPath"], _post_recovery_snapshot(row)
                ):
                    raise core.ManagerError(f"恢复后的文件校验失败：{row['relative']}")
            for row in latest_rows:
                if not _snapshot_matches(
                    row["destinationPath"], row["destinationRootPath"], _post_recovery_snapshot(row)
                ):
                    raise core.ManagerError(f"恢复完成前检测到外部修改：{row['relative']}")
        except BaseException as exc:
            errors = _rollback_recovery(latest_rows, attempted, safety_root)
            safety.update({
                "status": "rollback_incomplete" if errors else "rolled_back",
                "failedAt": core.now_iso(),
                "errors": errors,
                "failure": core._redact_sensitive_text(exc, limit=220),
            })
            try:
                _write_protected_json(safety_root / "manifest.json", safety)
            except Exception as journal_error:
                errors.append("安全记录更新失败：" + core._redact_sensitive_text(journal_error, limit=140))
            if not isinstance(exc, Exception):
                raise
            detail = "；".join(errors) if errors else "已回滚本次恢复写入"
            raise core.ManagerError(
                f"会话恢复未完成：{core._redact_sensitive_text(exc, limit=200)}；{detail}。恢复前证据：{safety_root}"
            ) from exc
        safety.update({"status": "complete", "completedAt": core.now_iso(), "changed": latest_preview["changed"]})
        warning = None
        try:
            _write_protected_json(safety_root / "manifest.json", safety)
        except Exception as exc:
            warning = "会话已恢复，但安全记录完成状态未能写入：" + core._redact_sensitive_text(exc, limit=180)
        source_manifest_path = root / "manifest.json"
        try:
            _latest_document, latest_manifest_raw = _read_recovery_document(root)
            if latest_manifest_raw != source_manifest_raw:
                raise core.ManagerError("原事务日志在恢复期间发生变化，保留原内容")
            document.update({
                "status": "recovered", "recoveredAt": core.now_iso(),
                "recoverySafetyId": safety["id"],
            })
            _write_journal(source_manifest_path, document)
            finalized, _finalized_root, _finalized_raw, _finalized_operations = _validated_journal(recovery_id)
            if finalized.get("status") != "recovered" or finalized.get("recoverySafetyId") != safety["id"]:
                raise core.ManagerError("原事务恢复状态发布校验失败")
        except Exception as exc:
            warning = "；".join(filter(None, [warning, "会话已恢复，但原事务状态未能更新：" + core._redact_sensitive_text(exc, limit=180)]))
        if any(row["side"] == "local" and row["recoveryAction"] in {"restore", "remove"} for row in latest_rows):
            try:
                core.refresh_codex_history_index()
            except core.ManagerError as exc:
                warning = "；".join(filter(None, [warning, "会话已恢复，但历史索引刷新失败：" + core._redact_sensitive_text(exc, limit=180)]))
        return {
            "restored": True,
            "changed": latest_preview["changed"],
            "recoveryId": recovery_id,
            "safetyId": safety["id"],
            "safetyPath": str(safety_root),
            "warning": warning,
            "message": "会话已恢复到本次同步前的内容。" if latest_preview["changed"] else "会话已处于同步前状态，事务已标记为恢复。",
        }
    finally:
        for lock in reversed(acquired_locks):
            lock.release()
