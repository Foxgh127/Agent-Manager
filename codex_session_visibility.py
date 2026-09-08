"""Conservative Codex session visibility inspection and repair.

The public workflow is deliberately two phase::

    report = inspect_session_visibility(codex_home, target_provider="openai")
    result = repair_session_visibility(report, confirm_codex_stopped=True)

Inspection is read-only.  Repair accepts only a fresh inspection token, updates
individual proven user/root sessions, creates a consistent backup first, and
rolls every touched file back if a later write fails.  The module never creates,
deletes, archives, or unarchives a session.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import shutil
import sqlite3
import tempfile
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable, Mapping, Sequence

try:
    import tomllib
except ImportError:  # pragma: no cover - supported application Pythons provide it
    tomllib = None  # type: ignore[assignment]


__all__ = [
    "SessionVisibilityError",
    "inspect_session_visibility",
    "repair_session_visibility",
    "restore_session_visibility",
    "normalize_workspace_path",
]

SCHEMA_VERSION = 1
DEFAULT_BACKUP_DIR = Path("agent-manager") / "backups" / "session-visibility-v96"
STATE_DB_PATTERN = re.compile(r"^state_(\d+)\.sqlite$", re.IGNORECASE)
DESKTOP_CATALOG_PATH = "sqlite/codex-dev.db"
ROLLOUT_PATTERN = re.compile(r"^rollout-.*\.jsonl$", re.IGNORECASE)
PROVIDER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
MAX_ROLLOUT_BYTES = 256 * 1024 * 1024
MAX_JSON_LINE_BYTES = 4 * 1024 * 1024
MAX_THREAD_ROWS = 250_000
SQLITE_BUSY_TIMEOUT_MS = 250
DEFAULT_QUICK_MAX_ROLLOUTS = 512
DEFAULT_QUICK_SCAN_BYTES = 32 * 1024 * 1024
QUICK_PREFIX_BYTES_PER_ROLLOUT = 1024 * 1024
DEFAULT_QUICK_REPAIR_BYTES = 128 * 1024 * 1024
MAX_PREVIEW_CHARS = 4096

_REPAIR_LOCK = threading.Lock()
_ID_COLUMNS = ("id", "thread_id")
_KNOWN_THREAD_COLUMNS = (
    "id",
    "thread_id",
    "rollout_path",
    "model_provider",
    "has_user_event",
    "first_user_message",
    "thread_source",
    "preview",
    "cwd",
    "working_directory",
    "archived",
    "archived_at",
    "source",
    "originator",
    "agent_nickname",
    "agent_role",
    "agent_path",
    "parent_thread_id",
    "parent_id",
)
_NON_ROOT_EXACT = {
    "subagent",
    "sub_agent",
    "internal",
    "ambient_suggestions",
    "compact",
    "review",
    "memory_consolidation",
}
_NON_ROOT_KEYS = {
    "subagent",
    "sub_agent",
    "internal",
    "child_thread_id",
    "parent_thread_id",
    "agent_path",
}
_ROOT_SOURCE_EXACT = {
    "api",
    "app",
    "cli",
    "codex_app",
    "desktop",
    "exec",
    "external",
    "ide",
    "mcp",
    "remote",
    "user",
    "vscode",
    "vs_code",
    "web",
}
_CATALOG_COLUMNS = {
    "host_id", "thread_id", "display_title", "source_created_at", "source_updated_at",
    "cwd", "source_kind", "source_detail", "model_provider", "git_branch",
    "observation_sequence", "missing_candidate", "thread_source", "source_recency_at",
    "pending_observed_title", "project_id", "conversation_origin",
}


class SessionVisibilityError(RuntimeError):
    """Raised when a visibility operation cannot be completed safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _json_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"base64Hex": value.hex()}
    return str(value)


def _truthy_sqlite(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().casefold() not in {"", "0", "false", "no", "null", "none"}


def _nonempty(value: Any) -> bool:
    return value is not None and bool(str(value).strip())


def _validate_provider(provider: str) -> str:
    value = str(provider or "").strip()
    if not PROVIDER_PATTERN.fullmatch(value):
        raise SessionVisibilityError("目标 provider 为空或包含不安全字符。")
    return value


def _read_target_provider(home: Path) -> str:
    config_path = home / "config.toml"
    if not config_path.is_file():
        raise SessionVisibilityError("未提供 target_provider，且实例缺少 config.toml。")
    if tomllib is None:
        raise SessionVisibilityError("当前 Python 缺少 tomllib，无法读取 config.toml。")
    try:
        with config_path.open("rb") as stream:
            config = tomllib.load(stream)
    except (OSError, ValueError) as exc:
        raise SessionVisibilityError(f"无法读取 Codex config.toml：{exc}") from exc
    return _validate_provider(str(config.get("model_provider") or "openai"))


def normalize_workspace_path(value: Any) -> str | None:
    """Normalize safe Windows extended/trailing syntax without resolving the path."""

    raw = str(value or "").strip()
    if not raw:
        return None
    lowered = raw.casefold()
    if lowered.startswith("\\\\?\\unc\\"):
        raw = "\\\\" + raw[8:]
    elif lowered.startswith("\\\\?\\"):
        raw = raw[4:]

    is_windows = raw.startswith(("\\\\", "//")) or (
        len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha()
    )
    if not is_windows:
        if raw != os.path.sep:
            raw = raw.rstrip("/\\") or raw
        return raw

    drive, tail = ntpath.splitdrive(raw)
    if drive and not tail:
        return drive + "\\"
    if drive and tail in {"\\", "/"}:
        return drive + tail
    if raw.startswith(("\\\\", "//")):
        parsed = PureWindowsPath(raw)
        # A UNC share root has one anchor and no ordinary components.  Preserve
        # its root separator; deeper paths can safely lose trailing separators.
        if len(parsed.parts) <= 1:
            return raw
    return raw.rstrip("/\\") or raw


def _path_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_rollout_path(home: Path, value: Any) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    lowered = raw.casefold()
    if lowered.startswith("\\\\?\\unc\\"):
        raw = "\\\\" + raw[8:]
    elif lowered.startswith("\\\\?\\"):
        raw = raw[4:]
    # Codex stores forward-slash relative paths on some Windows builds.
    candidate = Path(raw.replace("/", os.sep).replace("\\", os.sep))
    if not candidate.is_absolute():
        candidate = home / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        return None
    return resolved if _path_within(home, resolved) else None


def _relative(home: Path, path: Path) -> str:
    return path.resolve(strict=False).relative_to(home).as_posix()


def _stat_fingerprint(path: Path, *, include_sidecars: bool = False) -> dict[str, Any]:
    def item(target: Path) -> dict[str, Any] | None:
        try:
            stat = target.stat()
        except OSError:
            return None
        return {
            "size": stat.st_size,
            "mtimeNs": stat.st_mtime_ns,
            "inode": getattr(stat, "st_ino", 0),
        }

    result: dict[str, Any] = {"main": item(path)}
    if include_sidecars:
        wal = item(Path(str(path) + "-wal"))
        # A read-only connection to a stopped WAL database creates an empty
        # WAL and updates SHM reader locks. Neither changes persisted content.
        # Fingerprint the database and nonempty WAL; treating SHM timestamps
        # as writes makes every stopped-instance inspection fail on Windows.
        result["wal"] = wal if wal and wal["size"] else None
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _marks_non_root(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if normalized in _NON_ROOT_KEYS:
                return True
            if _marks_non_root(nested):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_marks_non_root(item) for item in value)
    text = str(value).strip()
    if not text:
        return False
    if text.startswith(("{", "[")):
        try:
            return _marks_non_root(json.loads(text))
        except (TypeError, ValueError):
            pass
    lowered = text.casefold().replace("-", "_").replace(" ", "_")
    return (
        lowered in _NON_ROOT_EXACT
        or lowered.startswith("subagent_")
        or lowered.startswith("sub_agent_")
        or lowered.startswith("internal_")
    )


def _marks_root_source(value: Any) -> bool:
    """Accept only explicit, known root-session sources as positive evidence."""

    if value is None:
        return False
    if isinstance(value, Mapping):
        keys = {str(key).strip().casefold().replace("-", "_") for key in value}
        return bool(keys & _ROOT_SOURCE_EXACT) and not _marks_non_root(value)
    text = str(value).strip()
    if not text:
        return False
    if text.startswith(("{", "[", '"')):
        try:
            return _marks_root_source(json.loads(text))
        except (TypeError, ValueError):
            pass
    return text.casefold().replace("-", "_").replace(" ", "_") in _ROOT_SOURCE_EXACT


def _is_user_event(record: Mapping[str, Any]) -> bool:
    record_type = str(record.get("type") or "").strip().casefold()
    payload = record.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    payload_type = str(payload.get("type") or "").strip().casefold()
    role = str(payload.get("role") or record.get("role") or "").strip().casefold()
    if record_type in {"user_message", "user_input"}:
        return True
    if payload_type in {"user_message", "user_input"}:
        return True
    if role == "user" and record_type in {"message", "response_item", "event_msg", "input_item"}:
        return True
    return False


def _user_message_text(record: Mapping[str, Any]) -> str | None:
    """Only actual user-message events supply previews, never injected input context."""
    payload = record.get("payload")
    payload = payload if isinstance(payload, Mapping) else record
    event_type = str(payload.get("type") or "").casefold()
    if not (
        record.get("type") in {"user_message", "user_input"}
        or (record.get("type") == "event_msg" and event_type in {"user_message", "user_input"})
    ):
        return None
    value = payload.get("message", payload.get("text"))
    if isinstance(value, str) and value.strip():
        return value.strip()[:MAX_PREVIEW_CHARS]
    return None


def _meta_non_root(payload: Mapping[str, Any]) -> bool:
    return any(_marks_non_root(payload.get(key)) for key in ("source", "originator", "thread_source")) or any(
        _nonempty(payload.get(key)) for key in ("agent_nickname", "agent_role", "agent_path", "parent_thread_id", "parent_id")
    )


def _scan_rollout(home: Path, path: Path, archived: bool) -> dict[str, Any]:
    relative = _relative(home, path)
    result: dict[str, Any] = {
        "path": relative,
        "absolutePath": str(path),
        "archived": archived,
        "valid": False,
        "threadId": None,
        "provider": None,
        "providerPresent": False,
        "providerValue": None,
        "cwd": None,
        "nonRoot": False,
        "hasUserEvent": False,
        "firstUserMessage": None,
        "encryptedContentCount": 0,
        "sessionMetaLine": None,
        "sessionMetaLineSha256": None,
        "fingerprint": _stat_fingerprint(path),
        "sha256": None,
        "scannedBytes": 0,
        "scanComplete": False,
        "warning": None,
    }
    try:
        file_size = path.stat().st_size
        if file_size > MAX_ROLLOUT_BYTES:
            result["warning"] = f"rollout 超过 {MAX_ROLLOUT_BYTES} 字节安全扫描上限"
            return result
        digest = hashlib.sha256()
        session_meta_count = 0
        with path.open("rb") as stream:
            for line_number, raw_line in enumerate(stream):
                result["scannedBytes"] += len(raw_line)
                digest.update(raw_line)
                result["encryptedContentCount"] += raw_line.count(b'"encrypted_content"')
                if len(raw_line) > MAX_JSON_LINE_BYTES:
                    continue
                line_body = raw_line.rstrip(b"\r\n")
                try:
                    record = json.loads(line_body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(record, Mapping):
                    continue
                if _is_user_event(record):
                    result["hasUserEvent"] = True
                if result["firstUserMessage"] is None:
                    result["firstUserMessage"] = _user_message_text(record)
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, Mapping):
                    continue
                session_meta_count += 1
                if result["sessionMetaLine"] is not None:
                    continue
                thread_id = payload.get("id") or payload.get("session_id")
                result.update(
                    {
                        "valid": bool(str(thread_id or "").strip()),
                        "threadId": str(thread_id).strip() if thread_id else None,
                        "provider": str(payload.get("model_provider") or "").strip() or None,
                        "providerPresent": "model_provider" in payload,
                        "providerValue": _json_scalar(payload.get("model_provider")),
                        "cwd": normalize_workspace_path(payload.get("cwd")),
                        "nonRoot": _meta_non_root(payload),
                        "rootSource": _marks_root_source(payload.get("source"))
                        or str(payload.get("thread_source") or "").strip().casefold() == "user",
                        "sessionMetaLine": line_number,
                        "sessionMetaLineSha256": hashlib.sha256(raw_line).hexdigest(),
                    }
                )
        result["sha256"] = digest.hexdigest()
        result["scanComplete"] = True
        result["sessionMetaCount"] = session_meta_count
        if not result["valid"] and not result["warning"]:
            result["warning"] = "未找到带会话 ID 的 session_meta"
        elif session_meta_count > 1:
            result["valid"] = False
            result["warning"] = "包含多个 session_meta，无法唯一确认会话身份"
    except OSError as exc:
        result["warning"] = f"rollout 无法读取：{exc}"
    return result


def _discover_rollouts(home: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for directory_name, archived in (("sessions", False), ("archived_sessions", True)):
        root = home / directory_name
        if not root.is_dir():
            continue
        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            directories[:] = sorted(
                name for name in directories if not (current_path / name).is_symlink()
            )
            for name in sorted(files):
                path = current_path / name
                if not ROLLOUT_PATTERN.fullmatch(name) or path.is_symlink() or not path.is_file():
                    continue
                result.append(_scan_rollout(home, path.resolve(), archived))
    return result


def _scan_rollout_prefix(
    home: Path,
    path: Path,
    archived: bool,
    byte_limit: int,
) -> dict[str, Any]:
    """Read only the bounded JSONL prefix needed for quick eligibility checks."""

    relative = _relative(home, path)
    result: dict[str, Any] = {
        "path": relative,
        "absolutePath": str(path),
        "archived": archived,
        "valid": False,
        "threadId": None,
        "provider": None,
        "providerPresent": False,
        "providerValue": None,
        "cwd": None,
        "nonRoot": False,
        "rootSource": False,
        "hasUserEvent": False,
        "firstUserMessage": None,
        "encryptedContentCount": 0,
        "sessionMetaLine": None,
        "sessionMetaLineSha256": None,
        "fingerprint": _stat_fingerprint(path),
        "sha256": None,
        "scannedBytes": 0,
        "scanComplete": False,
        "sessionMetaCount": 0,
        "warning": None,
    }
    try:
        file_size = path.stat().st_size
        remaining = max(0, min(int(byte_limit), QUICK_PREFIX_BYTES_PER_ROLLOUT))
        line_number = 0
        with path.open("rb") as stream:
            while remaining > 0:
                read_size = min(MAX_JSON_LINE_BYTES + 1, remaining)
                raw_line = stream.readline(read_size)
                if not raw_line:
                    result["scanComplete"] = True
                    break
                result["scannedBytes"] += len(raw_line)
                remaining -= len(raw_line)
                result["encryptedContentCount"] += raw_line.count(b'"encrypted_content"')
                complete_line = raw_line.endswith(b"\n") or stream.tell() == file_size
                if not complete_line:
                    result["warning"] = "quick 前缀在超长 JSONL 行中截断"
                    break
                body = raw_line.rstrip(b"\r\n")
                try:
                    record = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    line_number += 1
                    continue
                if not isinstance(record, Mapping):
                    line_number += 1
                    continue
                if _is_user_event(record):
                    result["hasUserEvent"] = True
                if result["firstUserMessage"] is None:
                    result["firstUserMessage"] = _user_message_text(record)
                if record.get("type") == "session_meta" and isinstance(
                    record.get("payload"), Mapping
                ):
                    payload = record["payload"]
                    result["sessionMetaCount"] += 1
                    if result["sessionMetaLine"] is None:
                        thread_id = payload.get("id") or payload.get("session_id")
                        result.update(
                            {
                                "valid": bool(str(thread_id or "").strip()),
                                "threadId": str(thread_id).strip() if thread_id else None,
                                "provider": str(payload.get("model_provider") or "").strip()
                                or None,
                                "providerPresent": "model_provider" in payload,
                                "providerValue": _json_scalar(payload.get("model_provider")),
                                "cwd": normalize_workspace_path(payload.get("cwd")),
                                "nonRoot": _meta_non_root(payload),
                                "rootSource": _marks_root_source(payload.get("source"))
                                or str(payload.get("thread_source") or "").strip().casefold()
                                == "user",
                                "sessionMetaLine": line_number,
                                "sessionMetaLineSha256": hashlib.sha256(raw_line).hexdigest(),
                            }
                        )
                line_number += 1
            else:
                result["scanComplete"] = stream.tell() == file_size
        if result["scannedBytes"] >= file_size:
            result["scanComplete"] = True
        if result["sessionMetaCount"] > 1:
            result["valid"] = False
            result["warning"] = "包含多个 session_meta，无法唯一确认会话身份"
        if not result["valid"] and not result["warning"]:
            result["warning"] = (
                "quick 前缀未找到带会话 ID 的 session_meta"
                if not result["scanComplete"]
                else "未找到带会话 ID 的 session_meta"
            )
    except OSError as exc:
        result["warning"] = f"rollout 无法读取：{exc}"
    return result


def _row_may_need_quick_scan(row: Mapping[str, Any], columns: set[str], provider: str) -> bool:
    if row.get("_catalogProviderMismatch"):
        return True
    if "model_provider" in columns and str(row.get("model_provider") or "") != provider:
        return True
    if "has_user_event" in columns and not _truthy_sqlite(
        row.get("has_user_event")
    ):
        return True
    if "thread_source" in columns and not _nonempty(row.get("thread_source")):
        return True
    if "preview" in columns and not _nonempty(row.get("preview")):
        return True
    if "first_user_message" in columns and not _nonempty(row.get("first_user_message")):
        return True
    cwd_column = (
        "cwd" if "cwd" in columns else "working_directory" if "working_directory" in columns else None
    )
    if cwd_column and _nonempty(row.get(cwd_column)):
        normalized = normalize_workspace_path(row[cwd_column])
        if normalized and normalized != str(row[cwd_column]):
            return True
    return False


def _path_is_archived(home: Path, path: Path) -> bool:
    try:
        return path.relative_to(home).parts[0].casefold() == "archived_sessions"
    except (ValueError, IndexError):
        return False


def _discover_quick_rollouts(
    home: Path,
    databases: Sequence[Mapping[str, Any]],
    provider: str,
    selected_ids: set[str],
    include_archived: bool,
    *,
    max_rollouts: int,
    max_scan_bytes: int,
    check_all_providers: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]], bool]:
    """Scan only rollout references belonging to potentially repairable DB rows."""

    child_ids = {
        str(thread_id)
        for database in databases
        for thread_id in database.get("childThreadIds", [])
    }
    candidates: dict[str, tuple[Path, bool, str, int]] = {}
    issues: list[dict[str, Any]] = []
    blocking = False
    for database in (item for item in databases if item.get("selected")):
        id_column = str(database["idColumn"])
        columns = set(database["columns"])
        for row in database["rows"]:
            thread_id = str(row.get(id_column) or "").strip()
            if not thread_id or (selected_ids and thread_id not in selected_ids):
                continue
            if _row_non_root_reasons(row, child_ids, thread_id):
                continue
            if _row_archived(row, None) and not include_archived:
                continue
            if not check_all_providers and not _row_may_need_quick_scan(row, columns, provider):
                continue
            raw_path = row.get("rollout_path")
            resolved = _resolve_rollout_path(home, raw_path)
            if (
                resolved is None
                or not resolved.is_file()
                or resolved.is_symlink()
                or not ROLLOUT_PATTERN.fullmatch(resolved.name)
            ):
                issues.append(
                    {
                        "kind": "quick_rollout_unavailable",
                        "threadId": thread_id,
                        "detail": "候选会话缺少安全、可读取的 rollout_path；仅跳过此会话",
                    }
                )
                blocking = True
                continue
            archived = _path_is_archived(home, resolved)
            priority = (0 if row.get("_catalogProviderMismatch")
                        or ("model_provider" in columns and str(row.get("model_provider") or "") != provider)
                        else 1 if _row_may_need_quick_scan(row, columns, provider) else 2)
            candidates[os.path.normcase(str(resolved))] = (resolved, archived, thread_id, priority)

    ordered = sorted(candidates.values(), key=lambda item: (item[3], _relative(home, item[0])))
    all_candidate_ids = [item[2] for item in ordered]
    if len(ordered) > max_rollouts:
        blocking = True
        issues.append(
            {
                "kind": "quick_rollout_count_budget",
                "detail": f"候选 rollout {len(ordered)} 个，超过 quick 上限 {max_rollouts} 个",
            }
        )
        ordered = ordered[:max_rollouts]
    result: list[dict[str, Any]] = []
    scanned_ids: list[str] = []
    used_bytes = 0
    for path, archived, thread_id, _priority in ordered:
        remaining = max_scan_bytes - used_bytes
        if remaining <= 0:
            blocking = True
            issues.append(
                {
                    "kind": "quick_rollout_byte_budget",
                    "threadId": thread_id,
                    "detail": f"quick rollout 前缀扫描达到 {max_scan_bytes} 字节总上限",
                }
            )
            break
        item = _scan_rollout_prefix(home, path, archived, remaining)
        used_bytes += int(item.get("scannedBytes") or 0)
        result.append(item)
        scanned_ids.append(thread_id)
        if not item.get("valid"):
            blocking = True
            issues.append(
                {
                    "kind": "quick_rollout_invalid",
                    "threadId": thread_id,
                    "path": item["path"],
                    "detail": item.get("warning") or "quick 前缀无法确认 session_meta",
                }
            )
    budget = {
        "maxRollouts": max_rollouts,
        "candidateRollouts": len(candidates),
        "scannedRollouts": len(result),
        "maxScanBytes": max_scan_bytes,
        "scannedBytes": used_bytes,
        "truncated": blocking and len(result) < len(candidates),
        "deferredRollouts": max(0, len(candidates) - len(result)),
        "maxRepairBytes": DEFAULT_QUICK_REPAIR_BYTES,
        "_scannedSessionIds": scanned_ids,
        "_deferredSessionIds": all_candidate_ids[len(scanned_ids):],
    }
    return result, budget, issues, blocking


def _connect_read_only(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 0")
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")]


def _inspect_catalog(connection: sqlite3.Connection) -> dict[str, Any]:
    """Recognize the upstream desktop catalog; never infer a remote/local host."""
    info = list(connection.execute("PRAGMA table_info(local_thread_catalog)"))
    if not info:
        return {"status": "absent", "rows": {}}
    names = {str(item[1]) for item in info}
    required = {"host_id", "thread_id", "display_title", "source_created_at", "source_updated_at",
                "cwd", "source_kind", "model_provider", "observation_sequence"}
    primary = [str(item[1]) for item in sorted(info, key=lambda item: item[5]) if item[5]]
    if (not required <= names or not names <= _CATALOG_COLUMNS
            or primary != ["host_id", "thread_id"]
            or connection.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name='local_thread_catalog'").fetchone()):
        return {"status": "unsupported_schema", "rows": {}}
    host_columns = set(_table_columns(connection, "local_thread_catalog_hosts"))
    if not {"host_id", "host_kind"} <= host_columns:
        return {"status": "ambiguous_host", "rows": {}}
    hosts = connection.execute(
        "SELECT host_id FROM local_thread_catalog_hosts WHERE lower(host_kind) = 'local' LIMIT 2"
    ).fetchall()
    if len(hosts) != 1 or not _nonempty(hosts[0][0]):
        return {"status": "ambiguous_host", "rows": {}}
    host_id = str(hosts[0][0])
    if connection.execute("SELECT COUNT(*) FROM local_thread_catalog").fetchone()[0] > MAX_THREAD_ROWS:
        return {"status": "row_budget", "rows": {}}
    selected = sorted(names & {"host_id", "thread_id", "model_provider", "source_kind", "thread_source", "missing_candidate", "cwd", "conversation_origin"})
    rows = [dict(item) for item in connection.execute(
        "SELECT " + ", ".join(map(_quote_identifier, selected)) + " FROM local_thread_catalog"
    )]
    # A duplicate ID on another host is conflicting identity evidence, even if
    # the local primary key itself would permit a targeted update.
    remote_ids = {str(item["thread_id"]) for item in rows if item["host_id"] != host_id}
    return {"status": "supported", "hostId": host_id, "remoteIds": sorted(remote_ids),
            "rows": {str(item["thread_id"]): item for item in rows if item["host_id"] == host_id}}


def _inspect_desktop_catalog(home: Path) -> dict[str, Any]:
    """The desktop catalog lives separately from the App Server state database."""
    path = home / DESKTOP_CATALOG_PATH
    if not path.exists():
        return {"status": "absent", "rows": {}}
    result = {"status": "unavailable", "rows": {}, "path": DESKTOP_CATALOG_PATH}
    if path.is_symlink() or path.parent.is_symlink() or not _path_within(home, path.resolve()):
        return result
    before = _stat_fingerprint(path, include_sidecars=True)
    try:
        connection = _connect_read_only(path)
        try:
            check = connection.execute("PRAGMA quick_check(1)").fetchone()
            if check and str(check[0]).casefold() == "ok":
                result.update(_inspect_catalog(connection))
        finally:
            connection.close()
    except sqlite3.Error:
        pass
    result["fingerprint"] = _stat_fingerprint(path, include_sidecars=True)
    if before != result["fingerprint"]:
        result.update(status="changed_during_inspection", rows={})
    return result


def _collect_child_thread_ids(connection: sqlite3.Connection) -> set[str]:
    result: set[str] = set()
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    for table in sorted(
        tables & {"thread_spawn_edges", "thread_relations", "spawn_edges", "agent_job_items"}
    ):
        columns = _table_columns(connection, table)
        child_column = next(
            (
                name
                for name in ("child_thread_id", "child_id", "assigned_thread_id")
                if name in columns
            ),
            None,
        )
        if child_column is None:
            continue
        sql = (
            f"SELECT {_quote_identifier(child_column)} FROM {_quote_identifier(table)} "
            f"WHERE {_quote_identifier(child_column)} IS NOT NULL"
        )
        for row in connection.execute(sql):
            value = str(row[0] or "").strip()
            if value:
                result.add(value)
    return result


def _inspect_database(home: Path, path: Path, version: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": _relative(home, path),
        "absolutePath": str(path),
        "version": version,
        "location": "sqlite" if path.parent == home / "sqlite" else "root",
        "selected": False,
        "usable": False,
        "columns": [],
        "idColumn": None,
        "rowCount": 0,
        "rows": [],
        "childThreadIds": [],
        "fingerprint": _stat_fingerprint(path, include_sidecars=True),
        "warning": None,
    }
    before = result["fingerprint"]
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect_read_only(path)
        check = connection.execute("PRAGMA quick_check(1)").fetchone()
        if not check or str(check[0]).casefold() != "ok":
            result["warning"] = f"SQLite quick_check 失败：{check[0] if check else '无结果'}"
            return result
        columns = _table_columns(connection, "threads")
        id_column = next((name for name in _ID_COLUMNS if name in columns), None)
        if not columns or id_column is None:
            result["warning"] = "不是可识别的 Codex threads schema"
            return result
        selected_columns = [name for name in _KNOWN_THREAD_COLUMNS if name in columns]
        if id_column not in selected_columns:
            selected_columns.insert(0, id_column)
        count = int(connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0])
        if count > MAX_THREAD_ROWS:
            result["warning"] = f"threads 超过 {MAX_THREAD_ROWS} 行安全扫描上限"
            return result
        sql = "SELECT " + ", ".join(map(_quote_identifier, selected_columns)) + " FROM threads"
        rows = [
            {key: _json_scalar(row[key]) for key in selected_columns}
            for row in connection.execute(sql)
        ]
        result.update(
            {
                "usable": True,
                "columns": columns,
                "idColumn": id_column,
                "rowCount": count,
                "rows": rows,
                "childThreadIds": sorted(_collect_child_thread_ids(connection)),
                "catalog": _inspect_catalog(connection),
            }
        )
    except sqlite3.Error as exc:
        result["warning"] = f"SQLite 只读检查失败：{exc}"
    finally:
        if connection is not None:
            connection.close()
    after = _stat_fingerprint(path, include_sidecars=True)
    if before != after:
        result["usable"] = False
        result["warning"] = "SQLite 在诊断期间发生变化，请在 Codex 停止后重试"
    result["fingerprint"] = after
    return result


def _discover_databases(home: Path) -> list[dict[str, Any]]:
    paths: list[tuple[Path, int]] = []
    for directory in (home / "sqlite", home):
        if not directory.is_dir():
            continue
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for path in entries:
            match = STATE_DB_PATTERN.fullmatch(path.name)
            if match and path.is_file() and not path.is_symlink():
                paths.append((path.resolve(), int(match.group(1))))
    unique: dict[str, tuple[Path, int]] = {}
    for path, version in paths:
        unique[os.path.normcase(str(path))] = (path, version)
    databases = [_inspect_database(home, path, version) for path, version in unique.values()]
    highest_discovered = max((item["version"] for item in databases), default=None)
    usable = [
        item
        for item in databases
        if item["usable"] and item["version"] == highest_discovered
    ]
    if usable:
        preferred = [item for item in usable if item["location"] == "sqlite"] or usable
        # A single database is the writable source of truth.  Duplicate files of
        # the same version are reported but left untouched.
        selected = max(
            preferred,
            key=lambda item: (
                (item["fingerprint"].get("main") or {}).get("mtimeNs", 0),
                item["rowCount"],
                item["path"],
            ),
        )
        selected["selected"] = True
    return sorted(databases, key=lambda item: (-item["version"], item["path"]))


def _row_non_root_reasons(row: Mapping[str, Any], child_ids: set[str], thread_id: str) -> list[str]:
    reasons: list[str] = []
    if thread_id in child_ids:
        reasons.append("spawn_edge_child")
    for column in ("agent_nickname", "agent_role", "agent_path", "parent_thread_id", "parent_id"):
        if _nonempty(row.get(column)):
            reasons.append(column)
    for column in ("source", "thread_source", "originator"):
        if _marks_non_root(row.get(column)):
            reasons.append(f"{column}_marks_non_root")
    return sorted(set(reasons))


def _row_archived(row: Mapping[str, Any], rollout: Mapping[str, Any] | None) -> bool:
    return (
        _truthy_sqlite(row.get("archived"))
        or _truthy_sqlite(row.get("archived_at"))
        or bool(rollout and rollout.get("archived"))
    )


def _rollout_for_row(
    home: Path,
    row: Mapping[str, Any],
    thread_id: str,
    by_path: Mapping[str, dict[str, Any]],
    by_id: Mapping[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any] | None, str | None]:
    raw_path = row.get("rollout_path")
    if _nonempty(raw_path):
        resolved = _resolve_rollout_path(home, raw_path)
        if resolved is None:
            return None, "rollout_path 超出实例目录"
        direct = by_path.get(os.path.normcase(str(resolved)))
        if direct is not None:
            if direct.get("threadId") != thread_id:
                return None, "rollout_path 的 session_meta ID 与数据库不一致"
            return direct, None
        return None, "数据库引用的 rollout 不存在或不是普通 JSONL"
    matches = by_id.get(thread_id, [])
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, "同一会话存在多个 rollout，无法安全推断"
    return None, "未找到对应 rollout"


def _plan_token(report: Mapping[str, Any]) -> str:
    material = {
        "schemaVersion": report["schemaVersion"],
        "home": report["home"],
        "targetProvider": report["targetProvider"],
        "scanMode": report["scanMode"],
        "scanBudget": report["scanBudget"],
        "includeArchived": report["includeArchived"],
        "sessionIds": report["sessionIds"],
        "checkAllProviders": bool(report.get("checkAllProviders")),
        "desktopCatalog": report.get("desktopCatalog"),
        "databaseFingerprints": [
            {"path": item["path"], "selected": item["selected"], "fingerprint": item["fingerprint"]}
            for item in report["databases"]
        ],
        "actions": report["actions"],
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def inspect_session_visibility(
    codex_home: str | os.PathLike[str],
    target_provider: str | None = None,
    *,
    session_ids: Sequence[str] | None = None,
    include_archived: bool = False,
    mode: str = "quick",
    max_rollouts: int = DEFAULT_QUICK_MAX_ROLLOUTS,
    max_scan_bytes: int = DEFAULT_QUICK_SCAN_BYTES,
    check_all_providers: bool = False,
    max_repair_bytes: int | None = None,
) -> dict[str, Any]:
    """Return a JSON-serializable, read-only visibility diagnosis and repair plan."""

    home = Path(codex_home).expanduser().resolve(strict=False)
    if not home.is_dir():
        raise SessionVisibilityError(f"Codex 实例目录不存在：{home}")
    provider = _validate_provider(target_provider) if target_provider else _read_target_provider(home)
    scan_mode = str(mode or "quick").strip().casefold()
    if scan_mode not in {"quick", "deep"}:
        raise SessionVisibilityError("mode 仅支持 quick 或 deep。")
    try:
        rollout_limit = max(1, min(int(max_rollouts), 100_000))
        byte_limit = max(4_096, min(int(max_scan_bytes), 1024 * 1024 * 1024))
        repair_limit = max(0, min(int(DEFAULT_QUICK_REPAIR_BYTES if max_repair_bytes is None else max_repair_bytes),
                                  DEFAULT_QUICK_REPAIR_BYTES))
    except (TypeError, ValueError) as exc:
        raise SessionVisibilityError("quick 扫描预算必须是整数。") from exc
    selected_ids = sorted({str(value).strip() for value in (session_ids or []) if str(value).strip()})
    selected_id_set = set(selected_ids)

    databases = _discover_databases(home)
    selected_databases = [item for item in databases if item["selected"]]
    desktop_catalog = _inspect_desktop_catalog(home)
    for database in selected_databases:
        embedded = database.get("catalog", {"status": "absent", "rows": {}})
        if desktop_catalog["status"] != "absent":
            if embedded["status"] == "absent":
                database["catalog"] = desktop_catalog
            else:
                # Two catalog authorities require explicit diagnosis, not guessing.
                database["catalog"] = {"status": "multiple_catalogs", "rows": {}}
    for database in selected_databases:
        catalog = database.get("catalog", {})
        if catalog.get("status") == "supported":
            for row in database["rows"]:
                cached = catalog["rows"].get(str(row.get(database["idColumn"]) or ""))
                row["_catalogProviderMismatch"] = bool(cached and str(cached.get("model_provider") or "") != provider)
    quick_issues: list[dict[str, Any]] = []
    quick_blocking = False
    if scan_mode == "deep":
        rollouts = _discover_rollouts(home)
        scan_budget = {
            "maxRollouts": None,
            "candidateRollouts": len(rollouts),
            "scannedRollouts": len(rollouts),
            "maxScanBytes": None,
            "scannedBytes": sum(int(item.get("scannedBytes") or 0) for item in rollouts),
            "truncated": any(not item.get("scanComplete") for item in rollouts),
        }
    else:
        rollouts, scan_budget, quick_issues, quick_blocking = _discover_quick_rollouts(
            home,
            databases,
            provider,
            selected_id_set,
            include_archived,
            max_rollouts=rollout_limit,
            max_scan_bytes=byte_limit,
            check_all_providers=check_all_providers,
        )
        scan_budget["maxRepairBytes"] = repair_limit
    provider_scan = {"scannedSessionIds": scan_budget.pop("_scannedSessionIds", []),
                     "deferredSessionIds": scan_budget.pop("_deferredSessionIds", [])}
    by_path = {
        os.path.normcase(str(Path(item["absolutePath"]).resolve(strict=False))): item
        for item in rollouts
    }
    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rollout in rollouts:
        if rollout.get("threadId"):
            by_id[str(rollout["threadId"])].append(rollout)

    all_child_ids = {
        str(thread_id)
        for database in databases
        for thread_id in database.get("childThreadIds", [])
    }
    actions: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = list(quick_issues)
    rollout_actions: dict[str, dict[str, Any]] = {}
    repair_bytes = 0
    catalog_missing_ids: set[str] = set()
    catalog_blocked_ids: set[str] = set()

    for database in databases:
        if database["warning"]:
            issues.append(
                {
                    "kind": "database_unusable" if not database["usable"] else "database_warning",
                    "path": database["path"],
                    "detail": database["warning"],
                }
            )
        if database["usable"] and not database["selected"]:
            issues.append(
                {
                    "kind": "superseded_database",
                    "path": database["path"],
                    "detail": "不是最新首选 state 数据库；仅诊断，不写入",
                }
            )

    for rollout in rollouts:
        if rollout.get("warning"):
            issues.append(
                {"kind": "rollout_warning", "path": rollout["path"], "detail": rollout["warning"]}
            )

    for database in selected_databases:
        id_column = str(database["idColumn"])
        columns = set(database["columns"])
        catalog = database.get("catalog", {"status": "absent", "rows": {}})
        if catalog["status"] not in {"absent", "supported"}:
            issues.append({"kind": "catalog_unavailable", "detail": catalog["status"]})
        id_counts: dict[str, int] = defaultdict(int)
        for row in database["rows"]:
            id_counts[str(row.get(id_column) or "")] += 1
        for row in database["rows"]:
            thread_id = str(row.get(id_column) or "").strip()
            if not thread_id or (selected_id_set and thread_id not in selected_id_set):
                continue
            if id_counts[thread_id] != 1:
                excluded.append({"threadId": thread_id, "reason": "ambiguous_thread_identity"})
                continue
            preliminary_non_root = _row_non_root_reasons(row, all_child_ids, thread_id)
            if scan_mode == "quick" and preliminary_non_root:
                excluded.append(
                    {
                        "threadId": thread_id,
                        "reason": "non_root_agent",
                        "evidence": preliminary_non_root,
                    }
                )
                continue
            if scan_mode == "quick" and _row_archived(row, None) and not include_archived:
                excluded.append({"threadId": thread_id, "reason": "archived"})
                continue
            cached = catalog.get("rows", {}).get(thread_id)
            if (catalog["status"] == "supported" and cached is None
                    and not preliminary_non_root and not _row_archived(row, None)
                    and (_marks_root_source(row.get("source")) or str(row.get("thread_source") or "").casefold() == "user")):
                catalog_missing_ids.add(thread_id)
            if scan_mode == "quick" and not check_all_providers and not _row_may_need_quick_scan(row, columns, provider):
                continue
            rollout, rollout_warning = _rollout_for_row(home, row, thread_id, by_path, by_id)
            if rollout_warning:
                issues.append(
                    {"kind": "rollout_reference", "threadId": thread_id, "detail": rollout_warning}
                )
            non_root_reasons = preliminary_non_root
            if rollout and rollout.get("nonRoot"):
                non_root_reasons.append("rollout_source_marks_non_root")
            if non_root_reasons:
                excluded.append(
                    {"threadId": thread_id, "reason": "non_root_agent", "evidence": sorted(set(non_root_reasons))}
                )
                continue
            archived = _row_archived(row, rollout)
            if archived and not include_archived:
                excluded.append({"threadId": thread_id, "reason": "archived"})
                continue

            strong_user_evidence = bool(rollout and rollout.get("hasUserEvent")) or _nonempty(
                row.get("first_user_message")
            )
            user_evidence = strong_user_evidence or _truthy_sqlite(
                row.get("has_user_event")
            ) or _nonempty(row.get("preview"))
            explicit_root = (
                str(row.get("thread_source") or "").strip().casefold() == "user"
                or _marks_root_source(row.get("source"))
                or bool(rollout and rollout.get("rootSource"))
            )
            if not user_evidence or not explicit_root:
                excluded.append(
                    {
                        "threadId": thread_id,
                        "reason": (
                            "insufficient_user_evidence"
                            if not user_evidence
                            else "ambiguous_root_identity"
                        ),
                        "detail": rollout_warning,
                    }
                )
                continue

            # A row's flags cannot override missing, unscanned or conflicting
            # rollout evidence.  Exclude this session while retaining other work.
            if not rollout or not rollout.get("valid") or len(by_id.get(thread_id, [])) != 1:
                excluded.append({"threadId": thread_id, "reason": "rollout_unverified"})
                continue
            if (catalog["status"] == "supported" and (
                thread_id in catalog.get("remoteIds", [])
                or (cached and (_marks_non_root(cached.get("source_kind"))
                                or _marks_non_root(cached.get("thread_source"))
                                or _truthy_sqlite(cached.get("missing_candidate"))
                                or (_nonempty(cached.get("conversation_origin"))
                                    and str(cached.get("conversation_origin")).casefold() != "local")
                                or not (_marks_root_source(cached.get("source_kind"))
                                        or str(cached.get("thread_source") or "").casefold() == "user")
                                or (_nonempty(cached.get("cwd")) and _nonempty(row.get("cwd"))
                                    and normalize_workspace_path(cached["cwd"]) != normalize_workspace_path(row["cwd"]))))
            )):
                excluded.append({"threadId": thread_id, "reason": "catalog_identity_conflict"})
                catalog_blocked_ids.add(thread_id)
                continue
            updates: dict[str, Any] = {}
            reasons: list[str] = []
            if "model_provider" in columns and str(row.get("model_provider") or "") != provider:
                updates["model_provider"] = provider
                reasons.append("provider_mismatch")
            if (
                "has_user_event" in columns
                and not _truthy_sqlite(row.get("has_user_event"))
                and strong_user_evidence
            ):
                updates["has_user_event"] = 1
                reasons.append("user_event_flag_missing")
            if (
                "thread_source" in columns
                and not _nonempty(row.get("thread_source"))
                and strong_user_evidence
            ):
                updates["thread_source"] = "user"
                reasons.append("user_thread_source_missing")
            first_message = str(row.get("first_user_message") or "").strip() or rollout.get("firstUserMessage")
            if not first_message and (
                ("preview" in columns and not _nonempty(row.get("preview")))
                or ("preview" not in columns and "first_user_message" in columns)
            ):
                excluded.append({"threadId": thread_id, "reason": "user_preview_unavailable"})
            if ("first_user_message" in columns and not _nonempty(row.get("first_user_message"))
                    and rollout.get("firstUserMessage")):
                updates["first_user_message"] = rollout["firstUserMessage"]
                reasons.append("first_user_message_missing")
            if (
                "preview" in columns
                and not _nonempty(row.get("preview"))
                and first_message
            ):
                updates["preview"] = first_message
                reasons.append("preview_missing")
            cwd_column = "cwd" if "cwd" in columns else "working_directory" if "working_directory" in columns else None
            if cwd_column and _nonempty(row.get(cwd_column)):
                normalized_cwd = normalize_workspace_path(row[cwd_column])
                if normalized_cwd and normalized_cwd != str(row[cwd_column]):
                    updates[cwd_column] = normalized_cwd
                    reasons.append("windows_cwd_noncanonical")

            needs_write = bool(updates or rollout.get("provider") != provider
                               or (cached and str(cached.get("model_provider") or "") != provider))
            if scan_mode == "quick" and needs_write:
                size = int((rollout["fingerprint"].get("main") or {}).get("size", 0))
                if repair_bytes + size > repair_limit:
                    excluded.append({"threadId": thread_id, "reason": "quick_repair_byte_budget", "requiredBytes": size})
                    issues.append({"kind": "quick_repair_byte_budget", "threadId": thread_id})
                    scan_budget["truncated"] = True
                    continue
                repair_bytes += size

            if updates:
                actions.append(
                    {
                        "kind": "sqlite_update",
                        "database": database["path"],
                        "threadId": thread_id,
                        "idColumn": id_column,
                        "expected": {key: row.get(key) for key in updates},
                        "set": updates,
                        "reasons": reasons,
                        "archived": archived,
                        "rolloutEvidence": {"path": rollout["path"], "fingerprint": rollout["fingerprint"],
                                            "lineSha256": rollout["sessionMetaLineSha256"]},
                    }
                )

            if cached and str(cached.get("model_provider") or "") != provider:
                actions.append({
                    "kind": "sqlite_update", "table": "local_thread_catalog",
                    "database": catalog.get("path", database["path"]), "threadId": thread_id,
                    "idColumn": "thread_id", "hostId": catalog["hostId"],
                    "expected": {"model_provider": cached.get("model_provider")},
                    "set": {"model_provider": provider}, "reasons": ["catalog_provider_mismatch"],
                    "archived": False,
                    "identityEvidence": {key: cached[key] for key in ("source_kind", "thread_source", "cwd", "missing_candidate", "conversation_origin") if key in cached},
                    "rolloutEvidence": {"path": rollout["path"], "fingerprint": rollout["fingerprint"],
                                        "lineSha256": rollout["sessionMetaLineSha256"]},
                })

            if (
                rollout
                and rollout.get("valid")
                and rollout.get("provider") != provider
                and rollout.get("sessionMetaLine") is not None
            ):
                action = {
                    "kind": "rollout_meta_update",
                    "path": rollout["path"],
                    "threadId": thread_id,
                    "lineIndex": rollout["sessionMetaLine"],
                    "expectedLineSha256": rollout["sessionMetaLineSha256"],
                    "expectedFileFingerprint": rollout["fingerprint"],
                    "expectedFileSha256": rollout.get("sha256"),
                    "fromProvider": rollout.get("provider"),
                    "providerPresent": rollout.get("providerPresent", False),
                    "originalProviderValue": rollout.get("providerValue"),
                    "toProvider": provider,
                    "archived": archived,
                    "containsEncryptedContent": rollout.get("encryptedContentCount", 0) > 0,
                }
                existing = rollout_actions.get(rollout["path"])
                if existing and existing != action:
                    issues.append(
                        {
                            "kind": "ambiguous_rollout",
                            "path": rollout["path"],
                            "detail": "多个 thread 行对同一 rollout 给出了不同修复目标，已跳过",
                        }
                    )
                    rollout_actions.pop(rollout["path"], None)
                elif not existing:
                    rollout_actions[rollout["path"]] = action

    actions.extend(rollout_actions.values())
    actions.sort(key=lambda item: (item["kind"], item.get("database", item.get("path", "")), item["threadId"]))
    deferred_ids = {item["threadId"] for item in excluded if item["reason"] not in {"archived", "non_root_agent"}}
    catalog_missing_ids.difference_update(item["threadId"] for item in excluded if item["reason"] in {"archived", "non_root_agent"})
    blocked_action_count = len(deferred_ids)
    if scan_mode == "quick":
        scan_budget["plannedRepairBytes"] = repair_bytes
    encrypted_rollouts = [item for item in rollouts if item.get("encryptedContentCount", 0) > 0]
    safe = (
        len(selected_databases) == 1
        and all(item["usable"] for item in selected_databases)
    )
    report: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "mode": "inspect",
        "scanMode": scan_mode,
        "scanBudget": scan_budget,
        "inspectedAt": _utc_now(),
        "home": str(home),
        "targetProvider": provider,
        "includeArchived": bool(include_archived),
        "sessionIds": selected_ids,
        "checkAllProviders": bool(check_all_providers),
        "desktopCatalog": {key: desktop_catalog[key] for key in ("status", "path", "fingerprint") if key in desktop_catalog},
        "providerScan": {**provider_scan, "catalogMissingIds": sorted(catalog_missing_ids)},
        "safeToRepair": safe,
        "databases": databases,
        "rollouts": {
            "scanned": len(rollouts),
            "scope": "all" if scan_mode == "deep" else "repair_candidates",
            "active": sum(not item["archived"] for item in rollouts),
            "archived": sum(bool(item["archived"]) for item in rollouts),
            "invalid": sum(not item["valid"] for item in rollouts),
            "encrypted": len(encrypted_rollouts),
            "encryptedScanComplete": scan_mode == "deep"
            and all(bool(item.get("scanComplete")) for item in rollouts),
        },
        "actions": actions,
        "blockedActionCount": blocked_action_count,
        "completion": {
            "partial": bool(deferred_ids or scan_budget.get("truncated") or catalog_missing_ids
                            or any(item["kind"] == "catalog_unavailable" for item in issues)),
            "deferredSessions": len(deferred_ids),
            "catalogMissing": len(catalog_missing_ids),
            "catalogBlocked": len(catalog_blocked_ids),
            "catalogRecovery": "diagnostic_only_no_insert",
            "followUpMode": "deep",
        },
        "excluded": excluded,
        "issues": issues,
        "counts": {
            "sqliteRows": sum(item["kind"] == "sqlite_update" for item in actions),
            "rolloutFiles": sum(item["kind"] == "rollout_meta_update" for item in actions),
            "subagentsExcluded": sum(item["reason"] == "non_root_agent" for item in excluded),
            "archivedExcluded": sum(item["reason"] == "archived" for item in excluded),
        },
        "encryptedContentWarning": (
            "检测到 encrypted_content；修复只改 session_meta 行并逐字节保留正文，"
            "但跨 provider 续聊仍可能需要原凭据。"
            if encrypted_rollouts
            else None
        ),
    }
    report["repairToken"] = _plan_token(report)
    # Raw rows and absolute rollout paths are implementation detail and may hold
    # user content.  Keep only schema/fingerprint evidence in the public report.
    for database in report["databases"]:
        database.pop("rows", None)
        database.pop("childThreadIds", None)
        database.pop("absolutePath", None)
        database.pop("catalog", None)
    return report


def _inspection_arguments(report: Mapping[str, Any]) -> dict[str, Any]:
    arguments = {
        "codex_home": report.get("home"),
        "target_provider": report.get("targetProvider"),
        "session_ids": report.get("sessionIds") or None,
        "include_archived": bool(report.get("includeArchived")),
        "mode": str(report.get("scanMode") or "quick"),
        "check_all_providers": bool(report.get("checkAllProviders")),
    }
    budget = report.get("scanBudget")
    if arguments["mode"] == "quick" and isinstance(budget, Mapping):
        arguments["max_rollouts"] = budget.get("maxRollouts", DEFAULT_QUICK_MAX_ROLLOUTS)
        arguments["max_scan_bytes"] = budget.get("maxScanBytes", DEFAULT_QUICK_SCAN_BYTES)
        arguments["max_repair_bytes"] = budget.get("maxRepairBytes", DEFAULT_QUICK_REPAIR_BYTES)
    return arguments


def _manifest_write(path: Path, manifest: Mapping[str, Any]) -> None:
    payload = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _preflight_write_locks(home: Path, actions: Sequence[Mapping[str, Any]]) -> None:
    for relative in sorted({str(item["database"]) for item in actions if item["kind"] == "sqlite_update"}):
        path = (home / Path(relative)).resolve(strict=False)
        connection = sqlite3.connect(path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        try:
            connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
        except sqlite3.Error as exc:
            raise SessionVisibilityError(
                f"Codex SQLite 正在被使用，未开始备份或修复：{relative}：{exc}"
            ) from exc
        finally:
            connection.close()


def _create_backup(
    report: Mapping[str, Any],
    *,
    backup_parent: str | os.PathLike[str] | None,
) -> tuple[Path, dict[str, Any]]:
    home = Path(str(report["home"])).resolve()
    parent = Path(backup_parent).expanduser().resolve(strict=False) if backup_parent else home / DEFAULT_BACKUP_DIR
    parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    backup_dir = parent / f"visibility-{stamp}-{str(report['repairToken'])[:12]}"
    backup_dir.mkdir(parents=False, exist_ok=False)
    manifest: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "createdAt": _utc_now(),
        "home": str(home),
        "targetProvider": report["targetProvider"],
        "repairToken": report["repairToken"],
        "status": "prepared",
        "actions": report["actions"],
        "files": [],
    }
    try:
        db_paths = sorted(
            {str(item["database"]) for item in report["actions"] if item["kind"] == "sqlite_update"}
        )
        rollout_paths = sorted(
            {str(item["path"]) for item in report["actions"] if item["kind"] == "rollout_meta_update"}
        )
        rollout_hashes = {
            str(item["path"]): str(item.get("expectedFileSha256") or "")
            for item in report["actions"]
            if item["kind"] == "rollout_meta_update"
        }
        for relative in db_paths:
            source_path = (home / Path(relative)).resolve()
            target_path = backup_dir / "db" / Path(relative)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            source = _connect_read_only(source_path)
            destination = sqlite3.connect(target_path)
            try:
                source.backup(destination)
                check = destination.execute("PRAGMA quick_check(1)").fetchone()
                if not check or str(check[0]).casefold() != "ok":
                    raise SessionVisibilityError(f"SQLite 备份校验失败：{relative}")
            finally:
                destination.close()
                source.close()
            manifest["files"].append(
                {
                    "kind": "sqlite",
                    "relativePath": relative,
                    "backupPath": target_path.relative_to(backup_dir).as_posix(),
                    "sha256": _sha256_file(target_path),
                    "originalFingerprint": _stat_fingerprint(source_path, include_sidecars=True),
                }
            )
        for relative in rollout_paths:
            source_path = (home / Path(relative)).resolve()
            target_path = backup_dir / "files" / Path(relative)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
            backup_hash = _sha256_file(target_path)
            if not rollout_hashes.get(relative) or backup_hash != rollout_hashes[relative]:
                raise SessionVisibilityError(
                    f"rollout 在全量校验与备份之间发生变化：{relative}"
                )
            manifest["files"].append(
                {
                    "kind": "rollout",
                    "relativePath": relative,
                    "backupPath": target_path.relative_to(backup_dir).as_posix(),
                    "sha256": backup_hash,
                    "originalFingerprint": _stat_fingerprint(source_path),
                }
            )
        _manifest_write(backup_dir / "manifest.json", manifest)
        return backup_dir, manifest
    except Exception:
        # This directory contains no authoritative state until manifest creation
        # succeeds.  Leave it for diagnosis rather than silently pruning evidence.
        raise


def _sqlite_target(action: Mapping[str, Any]) -> tuple[str, str, list[Any]]:
    table = str(action.get("table") or "threads")
    if table == "threads" and action.get("idColumn") in _ID_COLUMNS:
        return table, f"{_quote_identifier(str(action['idColumn']))} = ?", [action["threadId"]]
    if (table == "local_thread_catalog" and action.get("idColumn") == "thread_id"
            and isinstance(action.get("hostId"), str) and action["hostId"]
            and set(action.get("set", {})) == {"model_provider"}
            and set(action.get("expected", {})) == {"model_provider"}):
        evidence = action.get("identityEvidence")
        if (not isinstance(evidence, dict) or "source_kind" not in evidence
                or not set(evidence) <= {"source_kind", "thread_source", "cwd", "missing_candidate", "conversation_origin"}):
            raise SessionVisibilityError("目录计划缺少可核验的身份信息。")
        where = '"host_id" = ? AND "thread_id" = ?' + "".join(f" AND {_quote_identifier(key)} IS ?" for key in evidence)
        return table, where, [action["hostId"], action["threadId"], *evidence.values()]
    raise SessionVisibilityError("SQLite 定点计划包含未知表或身份键。")


def _apply_sqlite_actions(home: Path, actions: Sequence[Mapping[str, Any]]) -> int:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for action in actions:
        if action["kind"] == "sqlite_update":
            grouped[str(action["database"])].append(action)
    updated = 0
    for relative, database_actions in sorted(grouped.items()):
        path = (home / Path(relative)).resolve()
        stat = path.stat()
        connection = sqlite3.connect(path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
            connection.execute("BEGIN IMMEDIATE")
            for action in database_actions:
                table, where, identity = _sqlite_target(action)
                expected = dict(action["expected"])
                updates = dict(action["set"])
                read_columns = sorted(set(expected) | set(updates))
                sql = (
                    "SELECT "
                    + ", ".join(map(_quote_identifier, read_columns))
                    + f" FROM {_quote_identifier(table)} WHERE {where}"
                )
                row = connection.execute(sql, identity).fetchone()
                if row is None:
                    raise SessionVisibilityError(f"修复前会话行已消失：{action['threadId']}")
                for column, value in expected.items():
                    if row[column] != value:
                        raise SessionVisibilityError(
                            f"修复计划已过期：{action['threadId']} 的 {column} 已变化"
                        )
                assignments = ", ".join(f"{_quote_identifier(key)} = ?" for key in updates)
                values = list(updates.values()) + identity
                cursor = connection.execute(
                    f"UPDATE {_quote_identifier(table)} SET {assignments} WHERE {where}",
                    values,
                )
                if cursor.rowcount != 1:
                    raise SessionVisibilityError(f"SQLite 定点更新行数异常：{action['threadId']}")
                updated += 1
            connection.commit()
        except Exception:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()
            try:
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            except OSError as exc:
                raise SessionVisibilityError(f"无法保留 SQLite 修改时间：{relative}：{exc}") from exc
    return updated


def _split_line_ending(raw_line: bytes) -> tuple[bytes, bytes]:
    if raw_line.endswith(b"\r\n"):
        return raw_line[:-2], b"\r\n"
    if raw_line.endswith(b"\n"):
        return raw_line[:-1], b"\n"
    return raw_line, b""


def _materialize_rollout_action_hashes(
    home: Path, actions: Sequence[Mapping[str, Any]]
) -> set[str]:
    """Hash and validate whole selected files before backup; isolate bad identities."""

    deferred: set[str] = set()
    checked: dict[str, Mapping[str, Any]] = {}
    for action in actions:
        proof = action.get("rolloutEvidence") if action.get("kind") == "sqlite_update" else {
            "path": action.get("path"), "fingerprint": action.get("expectedFileFingerprint"),
            "lineSha256": action.get("expectedLineSha256"),
        }
        if not isinstance(proof, Mapping):
            continue
        relative = str(proof["path"])
        path = (home / Path(relative)).resolve(strict=False)
        if not _path_within(home, path) or not path.is_file() or path.is_symlink():
            raise SessionVisibilityError(f"rollout 全量校验目标无效：{relative}")
        if _stat_fingerprint(path) != proof["fingerprint"]:
            raise SessionVisibilityError(f"rollout 在 quick 诊断后发生变化：{relative}")
        if relative not in checked:
            checked[relative] = _scan_rollout(home, path, bool(action.get("archived")))
        evidence = checked[relative]
        if _stat_fingerprint(path) != proof["fingerprint"]:
            raise SessionVisibilityError(f"rollout 在全量校验期间发生变化：{relative}")
        if (not evidence["valid"] or not evidence["scanComplete"] or evidence["nonRoot"]
                or evidence["threadId"] != action["threadId"]
                or evidence["sessionMetaLineSha256"] != proof["lineSha256"]):
            deferred.add(str(action["threadId"]))
            continue
        if action.get("kind") == "rollout_meta_update":
            action["expectedFileSha256"] = evidence["sha256"]  # type: ignore[index]
            action["containsEncryptedContent"] = evidence["encryptedContentCount"] > 0  # type: ignore[index]
    return deferred


def _apply_rollout_action(home: Path, action: Mapping[str, Any]) -> None:
    path = (home / Path(str(action["path"]))).resolve()
    if _stat_fingerprint(path) != action["expectedFileFingerprint"]:
        raise SessionVisibilityError(f"rollout 在诊断后发生变化：{action['path']}")
    expected_hash = action.get("expectedFileSha256")
    if not expected_hash or _sha256_file(path) != expected_hash:
        raise SessionVisibilityError(f"rollout 全量哈希已变化：{action['path']}")
    stat = path.stat()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".visibility.tmp", dir=path.parent)
    replaced = False
    try:
        with path.open("rb") as source, os.fdopen(descriptor, "wb") as destination:
            found = False
            for line_number, raw_line in enumerate(source):
                if line_number != int(action["lineIndex"]):
                    destination.write(raw_line)
                    continue
                if hashlib.sha256(raw_line).hexdigest() != action["expectedLineSha256"]:
                    raise SessionVisibilityError(f"session_meta 行已变化：{action['path']}")
                body, ending = _split_line_ending(raw_line)
                try:
                    record = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise SessionVisibilityError(f"session_meta 已无法解析：{action['path']}") from exc
                payload = record.get("payload") if isinstance(record, dict) else None
                if (
                    record.get("type") != "session_meta"
                    or not isinstance(payload, dict)
                    or str(payload.get("id") or payload.get("session_id") or "") != action["threadId"]
                ):
                    raise SessionVisibilityError(f"session_meta 身份已变化：{action['path']}")
                provider_present = "model_provider" in payload
                if (
                    provider_present != bool(action.get("providerPresent"))
                    or _json_scalar(payload.get("model_provider")) != action.get("originalProviderValue")
                ):
                    raise SessionVisibilityError(f"session_meta provider 已变化：{action['path']}")
                payload["model_provider"] = action["toProvider"]
                destination.write(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    + ending
                )
                found = True
            if not found:
                raise SessionVisibilityError(f"未找到待修复 session_meta：{action['path']}")
            destination.flush()
            os.fsync(destination.fileno())
        shutil.copymode(path, temporary_name)
        os.replace(temporary_name, path)
        replaced = True
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    finally:
        if not replaced and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _safe_manifest_paths(backup_dir: Path, manifest: Mapping[str, Any]) -> tuple[Path, list[dict[str, Any]]]:
    home = Path(str(manifest.get("home") or "")).resolve(strict=False)
    if not home.is_dir():
        raise SessionVisibilityError("备份清单中的 Codex 实例目录不存在。")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise SessionVisibilityError("备份清单缺少 files。")
    checked: list[dict[str, Any]] = []
    for raw in files:
        if not isinstance(raw, dict):
            raise SessionVisibilityError("备份清单含无效文件项。")
        relative = Path(str(raw.get("relativePath") or ""))
        backup_relative = Path(str(raw.get("backupPath") or ""))
        target = (home / relative).resolve(strict=False)
        source = (backup_dir / backup_relative).resolve(strict=False)
        if not _path_within(home, target) or not _path_within(backup_dir, source) or not source.is_file():
            raise SessionVisibilityError("备份清单含越界或缺失路径。")
        if _sha256_file(source) != raw.get("sha256"):
            raise SessionVisibilityError(f"备份文件校验失败：{relative.as_posix()}")
        item = dict(raw)
        item["target"] = target
        item["source"] = source
        checked.append(item)
    return home, checked


def _read_rollout_meta_payload(
    path: Path, line_index: int
) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    with path.open("rb") as stream:
        for current_index, raw_line in enumerate(stream):
            if current_index != line_index:
                continue
            body, _ending = _split_line_ending(raw_line)
            try:
                record = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SessionVisibilityError(f"恢复预检无法解析 session_meta：{path}") from exc
            payload = record.get("payload") if isinstance(record, dict) else None
            if record.get("type") != "session_meta" or not isinstance(payload, dict):
                raise SessionVisibilityError(f"恢复预检目标行不再是 session_meta：{path}")
            return raw_line, record, payload
    raise SessionVisibilityError(f"恢复预检找不到 session_meta 行：{path}")


def _provider_state(payload: Mapping[str, Any], action: Mapping[str, Any]) -> str:
    thread_id = str(payload.get("id") or payload.get("session_id") or "")
    if thread_id != action["threadId"]:
        return "conflict"
    present = "model_provider" in payload
    value = _json_scalar(payload.get("model_provider"))
    original_present = bool(action.get("providerPresent"))
    original_value = action.get("originalProviderValue")
    if present and value == action["toProvider"]:
        return "applied"
    if present == original_present and value == original_value:
        return "restored"
    return "conflict"


def _rewrite_rollout_provider_transition(
    home: Path, action: Mapping[str, Any], *, reverse: bool
) -> None:
    """Reverse or reapply one provider field while preserving every other byte/field."""

    path = (home / Path(str(action["path"]))).resolve()
    stat = path.stat()
    _raw, _record, current_payload = _read_rollout_meta_payload(path, int(action["lineIndex"]))
    expected_state = "applied" if reverse else "restored"
    if _provider_state(current_payload, action) != expected_state:
        raise SessionVisibilityError(f"rollout provider 恢复状态冲突：{action['path']}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".visibility-restore.tmp", dir=path.parent
    )
    replaced = False
    try:
        with path.open("rb") as source, os.fdopen(descriptor, "wb") as destination:
            found = False
            for line_number, raw_line in enumerate(source):
                if line_number != int(action["lineIndex"]):
                    destination.write(raw_line)
                    continue
                body, ending = _split_line_ending(raw_line)
                record = json.loads(body.decode("utf-8"))
                payload = record["payload"]
                if reverse:
                    if action.get("providerPresent"):
                        payload["model_provider"] = action.get("originalProviderValue")
                    else:
                        payload.pop("model_provider", None)
                else:
                    payload["model_provider"] = action["toProvider"]
                destination.write(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    + ending
                )
                found = True
            if not found:
                raise SessionVisibilityError(f"找不到待恢复 session_meta：{action['path']}")
            destination.flush()
            os.fsync(destination.fileno())
        shutil.copymode(path, temporary_name)
        os.replace(temporary_name, path)
        replaced = True
        # Keep the mtime observed immediately before this restore.  In
        # particular, do not erase a newer mtime caused by post-launch appends.
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    finally:
        if not replaced and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _group_sqlite_actions(actions: Iterable[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for action in actions:
        if action.get("kind") == "sqlite_update":
            grouped[str(action["database"])].append(action)
    return grouped


def _preflight_targeted_restore(home: Path, actions: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    states: dict[str, str] = {}
    for action in actions:
        if action.get("kind") != "rollout_meta_update":
            continue
        path = (home / Path(str(action["path"]))).resolve()
        _raw, _record, payload = _read_rollout_meta_payload(path, int(action["lineIndex"]))
        state = _provider_state(payload, action)
        if state == "conflict":
            raise SessionVisibilityError(
                f"恢复冲突：{action['path']} 的 session_meta provider 已被后续操作修改"
            )
        states[f"rollout:{action['path']}"] = state

    for relative, database_actions in sorted(_group_sqlite_actions(actions).items()):
        path = (home / Path(relative)).resolve()
        connection = sqlite3.connect(
            path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
            for action in database_actions:
                table, where, identity = _sqlite_target(action)
                columns = sorted(set(action["expected"]) | set(action["set"]))
                sql = (
                    "SELECT "
                    + ", ".join(map(_quote_identifier, columns))
                    + f" FROM {_quote_identifier(table)} WHERE {where}"
                )
                row = connection.execute(sql, identity).fetchone()
                if row is None:
                    raise SessionVisibilityError(
                        f"恢复冲突：目标会话行已不存在：{action['threadId']}"
                    )
                column_states: list[str] = []
                for column in columns:
                    current = row[column]
                    if current == action["expected"].get(column):
                        column_states.append("restored")
                    elif current == action["set"].get(column):
                        column_states.append("applied")
                    else:
                        raise SessionVisibilityError(
                            f"恢复冲突：{action['threadId']} 的 {column} 已被后续操作修改"
                        )
                states[f"sqlite:{relative}:{table}:{action['threadId']}"] = (
                    "restored" if all(value == "restored" for value in column_states) else "applied"
                )
        finally:
            connection.close()
    return states


def _apply_sqlite_restore(home: Path, actions: Sequence[Mapping[str, Any]]) -> int:
    restored = 0
    for relative, database_actions in sorted(_group_sqlite_actions(actions).items()):
        path = (home / Path(relative)).resolve()
        stat = path.stat()
        connection = sqlite3.connect(
            path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        committed = False
        try:
            connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
            connection.execute("BEGIN IMMEDIATE")
            for action in database_actions:
                table, where, identity = _sqlite_target(action)
                expected = dict(action["expected"])
                applied = dict(action["set"])
                columns = sorted(set(expected) | set(applied))
                sql = (
                    "SELECT "
                    + ", ".join(map(_quote_identifier, columns))
                    + f" FROM {_quote_identifier(table)} WHERE {where}"
                )
                row = connection.execute(sql, identity).fetchone()
                if row is None:
                    raise SessionVisibilityError(
                        f"恢复时目标会话行已不存在：{action['threadId']}"
                    )
                changed_columns = []
                for column in columns:
                    if row[column] == expected.get(column):
                        continue
                    if row[column] != applied.get(column):
                        raise SessionVisibilityError(
                            f"恢复时检测到并发修改：{action['threadId']} 的 {column}"
                        )
                    changed_columns.append(column)
                if not changed_columns:
                    continue
                assignments = ", ".join(
                    f"{_quote_identifier(column)} = ?" for column in changed_columns
                )
                values = [expected[column] for column in changed_columns] + identity
                cursor = connection.execute(
                    f"UPDATE {_quote_identifier(table)} SET {assignments} WHERE {where}",
                    values,
                )
                if cursor.rowcount != 1:
                    raise SessionVisibilityError(
                        f"SQLite 定点恢复行数异常：{action['threadId']}"
                    )
                restored += 1
            connection.commit()
            committed = True
        except Exception:
            if not committed:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            raise
        finally:
            connection.close()
            # Preserve the mtime present immediately before restore, including a
            # newer timestamp written by a short-lived post-switch Codex launch.
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    return restored


def _restore_targeted(backup_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    home, files = _safe_manifest_paths(backup_dir, manifest)
    raw_actions = manifest.get("actions")
    if not isinstance(raw_actions, list):
        raise SessionVisibilityError("备份清单缺少定点恢复 actions。")
    actions = [item for item in raw_actions if isinstance(item, dict)]
    if len(actions) != len(raw_actions):
        raise SessionVisibilityError("备份清单包含无效恢复 action。")
    allowed = {
        ("sqlite_update" if item["kind"] == "sqlite" else "rollout_meta_update", item["relativePath"])
        for item in files
    }
    for action in actions:
        kind = str(action.get("kind") or "")
        relative = str(
            action.get("database") if kind == "sqlite_update" else action.get("path") or ""
        )
        if (kind, relative) not in allowed:
            raise SessionVisibilityError("备份清单 action 未被对应备份文件覆盖。")
        target = (home / Path(relative)).resolve(strict=False)
        if not _path_within(home, target):
            raise SessionVisibilityError("备份清单 action 路径越出实例目录。")
    _preflight_write_locks(home, actions)
    states = _preflight_targeted_restore(home, actions)

    reversed_rollouts: list[Mapping[str, Any]] = []
    try:
        for action in actions:
            if action.get("kind") != "rollout_meta_update":
                continue
            if states.get(f"rollout:{action['path']}") == "restored":
                continue
            _rewrite_rollout_provider_transition(home, action, reverse=True)
            reversed_rollouts.append(action)
        sqlite_count = _apply_sqlite_restore(home, actions)
    except Exception as exc:
        compensation_errors: list[str] = []
        for action in reversed(reversed_rollouts):
            try:
                _rewrite_rollout_provider_transition(home, action, reverse=False)
            except Exception as compensation_exc:
                compensation_errors.append(str(compensation_exc))
        if compensation_errors:
            raise SessionVisibilityError(
                f"定点恢复失败：{exc}；rollout 补偿也失败：{'；'.join(compensation_errors)}"
            ) from exc
        raise

    manifest["status"] = "restored"
    manifest["restoredAt"] = _utc_now()
    manifest["restoreMode"] = "targeted_inverse_patch"
    _manifest_write(backup_dir / "manifest.json", manifest)
    return {
        "restored": True,
        "home": str(home),
        "backupDir": str(backup_dir),
        "restoreMode": "targeted_inverse_patch",
        "sqliteRowsRestored": sqlite_count,
        "rolloutFilesRestored": len(reversed_rollouts),
        "preservedUnrelatedChanges": True,
    }


def _verify_applied_actions(home: Path, actions: Sequence[Mapping[str, Any]]) -> None:
    """Verify this transaction's fields; subsequent scan batches may still differ."""
    for relative, database_actions in _group_sqlite_actions(actions).items():
        connection = _connect_read_only(home / relative)
        try:
            for action in database_actions:
                table, where, identity = _sqlite_target(action)
                columns = list(action["set"])
                rows = connection.execute(
                    "SELECT " + ", ".join(map(_quote_identifier, columns))
                    + f" FROM {_quote_identifier(table)} WHERE {where}", identity,
                ).fetchall()
                if len(rows) != 1 or any(rows[0][key] != action["set"][key] for key in columns):
                    raise SessionVisibilityError(f"修复后字段回验失败：{action['threadId']}")
        finally:
            connection.close()
    for action in actions:
        if action["kind"] == "rollout_meta_update":
            _raw, _record, payload = _read_rollout_meta_payload(home / action["path"], int(action["lineIndex"]))
            if _provider_state(payload, action) != "applied":
                raise SessionVisibilityError(f"修复后 rollout 回验失败：{action['threadId']}")


def repair_session_visibility(
    inspection: Mapping[str, Any],
    *,
    confirm_codex_stopped: bool = False,
    backup_parent: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Apply a fresh inspection plan after Codex has been fully stopped."""

    if not confirm_codex_stopped:
        raise SessionVisibilityError("修复写入要求调用方先停止目标 Codex 实例。")
    if int(inspection.get("schemaVersion", 0)) != SCHEMA_VERSION:
        raise SessionVisibilityError("诊断结果版本不受支持。")
    if not inspection.get("repairToken"):
        raise SessionVisibilityError("修复必须使用 inspect_session_visibility 的完整结果。")
    if not inspection.get("safeToRepair"):
        raise SessionVisibilityError("诊断结果包含歧义或不可用数据库，拒绝写入。")
    if not _REPAIR_LOCK.acquire(blocking=False):
        raise SessionVisibilityError("已有会话可见性修复正在执行。")
    backup_dir: Path | None = None
    manifest: dict[str, Any] | None = None
    try:
        current = inspect_session_visibility(**_inspection_arguments(inspection))
        if current["repairToken"] != inspection["repairToken"]:
            raise SessionVisibilityError("诊断结果已过期；会话存储发生变化，请重新诊断。")
        actions = current["actions"]
        if not actions:
            return {
                "changed": False,
                "backupDir": None,
                "sqliteRowsUpdated": 0,
                "rolloutFilesUpdated": 0,
                "completion": current["completion"],
                "verification": current,
            }
        home = Path(current["home"]).resolve()
        _preflight_write_locks(home, actions)
        deferred_after_validation = _materialize_rollout_action_hashes(home, actions)
        if deferred_after_validation:
            actions = [action for action in actions if action["threadId"] not in deferred_after_validation]
            current["actions"] = actions
            current["counts"]["sqliteRows"] = sum(action["kind"] == "sqlite_update" for action in actions)
            current["counts"]["rolloutFiles"] = sum(action["kind"] == "rollout_meta_update" for action in actions)
            current["blockedActionCount"] += len(deferred_after_validation)
            current["completion"]["partial"] = True
            current["completion"]["deferredSessions"] += len(deferred_after_validation)
            current["excluded"].extend({"reason": "full_rollout_identity_unverified", "threadId": value}
                                        for value in sorted(deferred_after_validation))
            current["issues"].extend({"kind": "full_rollout_identity_unverified", "threadId": value}
                                     for value in sorted(deferred_after_validation))
            if not actions:
                return {"changed": False, "backupDir": None, "sqliteRowsUpdated": 0,
                        "rolloutFilesUpdated": 0, "completion": current["completion"], "verification": current}
        backup_dir, manifest = _create_backup(current, backup_parent=backup_parent)
        try:
            sqlite_count = _apply_sqlite_actions(home, actions)
            rollout_count = 0
            for action in actions:
                if action["kind"] == "rollout_meta_update":
                    _apply_rollout_action(home, action)
                    rollout_count += 1
            _verify_applied_actions(home, actions)
            verification = inspect_session_visibility(**_inspection_arguments(current))
            completion = dict(verification["completion"])
            completion["remainingActions"] = len(verification["actions"])
            completion["deferredSessions"] += len(deferred_after_validation)
            completion["partial"] = bool(completion["partial"] or verification["actions"] or deferred_after_validation)
            verification["completion"] = completion
            verification["issues"].extend({"kind": "full_rollout_identity_unverified", "threadId": value}
                                           for value in sorted(deferred_after_validation))
            manifest["status"] = "applied"
            manifest["appliedAt"] = _utc_now()
            manifest["sqliteRowsUpdated"] = sqlite_count
            manifest["rolloutFilesUpdated"] = rollout_count
            _manifest_write(backup_dir / "manifest.json", manifest)
            return {
                "changed": True,
                "backupDir": str(backup_dir),
                "sqliteRowsUpdated": sqlite_count,
                "rolloutFilesUpdated": rollout_count,
                "completion": completion,
                "verification": verification,
            }
        except Exception as exc:
            try:
                _restore_targeted(backup_dir, manifest)
            except Exception as rollback_exc:
                failure = SessionVisibilityError(
                    f"会话可见性修复失败：{exc}；自动回滚失败：{rollback_exc}；备份：{backup_dir}"
                )
                failure.rollback_failed = True
                raise failure from exc
            raise SessionVisibilityError(
                f"会话可见性修复失败：{exc}；已自动回滚；备份：{backup_dir}"
            ) from exc
    finally:
        _REPAIR_LOCK.release()


def restore_session_visibility(
    backup_dir: str | os.PathLike[str],
    *,
    confirm_codex_stopped: bool = False,
    expected_codex_home: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Reverse only this repair's fields while preserving later unrelated writes."""

    if not confirm_codex_stopped:
        raise SessionVisibilityError("恢复要求调用方先停止目标 Codex 实例。")
    root = Path(backup_dir).expanduser().resolve(strict=False)
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SessionVisibilityError(f"无法读取会话可见性备份清单：{exc}") from exc
    if int(manifest.get("schemaVersion", 0)) != SCHEMA_VERSION:
        raise SessionVisibilityError("备份清单版本不受支持。")
    if expected_codex_home is not None:
        expected = Path(expected_codex_home).expanduser().resolve(strict=False)
        actual = Path(str(manifest.get("home") or "")).resolve(strict=False)
        if actual != expected:
            raise SessionVisibilityError("备份所属实例与 expected_codex_home 不一致。")
    if not _REPAIR_LOCK.acquire(blocking=False):
        raise SessionVisibilityError("已有会话可见性修复或恢复正在执行。")
    try:
        return _restore_targeted(root, manifest)
    finally:
        _REPAIR_LOCK.release()
