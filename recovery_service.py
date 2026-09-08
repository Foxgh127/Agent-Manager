"""Local, encrypted restore points with preview, validation and safety backups."""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import threading
import tomllib
import uuid

import agent_manager_core as core

FORMAT = "agent-manager-restore-point"
VERSION = 2
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ENVELOPE_BYTES = 128 * 1024 * 1024
POINT_ID = re.compile(r"^point-\d{8}-\d{6}-[a-f0-9]{32}$")
LOCK = threading.RLock()


def _directory() -> Path:
    return core.STATE_DIR / "restore-points"


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _json_loads(value: str | bytes) -> object:
    return json.loads(value, object_pairs_hook=_unique_json_object)


def _path_fingerprint(path: Path) -> str:
    normalized = os.path.normcase(os.path.abspath(os.fspath(path.resolve(strict=False))))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _source() -> dict:
    return {
        "application": FORMAT,
        "codexHome": _path_fingerprint(core.CODEX_HOME),
        "stateDirectory": _path_fingerprint(core.STATE_DIR),
        "settingsSchemaVersion": core.SCHEMA_VERSION,
    }


def _targets(scope: str, *, include_agents: bool = True) -> dict[str, tuple[str, Path]]:
    if scope == "usage":
        return {"usage": ("本地网关用量", core.STATE_DIR / "web2api-usage.json")}
    if scope != "configuration":
        raise core.ManagerError("备份范围无效。")
    targets = {
        "settings": ("账号、分组与调度设置", core.SETTINGS_FILE),
        "provider-vault": ("账号和 API 凭据保险库", core.SECRETS_FILE),
        "codex-config": ("Codex 配置", core.CONFIG_FILE),
        "codex-rules": ("Codex 全局规则", core.AGENTS_FILE),
        "totp-vault": ("2FA 保险库", core.STATE_DIR / "toolbox-totp-items.json"),
        "mail-vault": ("邮箱保险库", core.STATE_DIR / "toolbox-email-accounts.json"),
        "claude-profiles": ("Claude 账号设置", core.STATE_DIR / "claude-desktop-profiles.json"),
        "claude-vault": ("Claude 凭据保险库", core.STATE_DIR / "claude-desktop-secrets.json"),
    }
    if include_agents and core.AGENTS_DIR.is_dir():
        agents = sorted(core.AGENTS_DIR.glob("*.toml"))
        if len(agents) > 200:
            raise core.ManagerError("Agent 配置超过 200 个，无法创建有界备份。")
        for path in agents:
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,120}\.toml", path.name):
                targets["agent:" + path.name] = ("子代理 " + path.stem, path)
    return targets


def _path_for(key: str, scope: str) -> tuple[str, Path]:
    targets = _targets(scope, include_agents=False)
    if key in targets:
        return targets[key]
    if scope == "configuration" and key.startswith("agent:") and re.fullmatch(r"[A-Za-z0-9_.-]{1,120}\.toml", key[6:]):
        return "子代理 " + key[6:-5], core.AGENTS_DIR / key[6:]
    raise core.ManagerError("备份包含未知文件类型，未执行恢复。")


def _safe_target(path: Path) -> None:
    try:
        roots = [core.CODEX_HOME.resolve(strict=False), core.STATE_DIR.resolve(strict=False)]
        resolved = path.resolve(strict=False)
        current = Path(os.path.abspath(os.fspath(path)))
    except (OSError, RuntimeError) as exc:
        raise core.ManagerError("无法确认备份文件的安全路径。") from exc
    if not any(resolved.is_relative_to(root) for root in roots):
        raise core.ManagerError("备份文件路径超出 Codex 与管理器目录。")
    reached_root = False
    while current != current.parent:
        try:
            is_link = current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction())
        except OSError as exc:
            raise core.ManagerError("无法确认备份文件的链接状态。") from exc
        if is_link:
            raise core.ManagerError("备份与恢复不跟随符号链接或目录联接。")
        if any(current == root for root in roots):
            reached_root = True
            break
        current = current.parent
    if not reached_root:
        raise core.ManagerError("备份文件路径无法归入受信任目录。")


def _read(path: Path) -> bytes | None:
    _safe_target(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise core.ManagerError("备份目标不是普通文件。")
    with path.open("rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    _safe_target(path)
    if len(raw) > MAX_FILE_BYTES:
        raise core.ManagerError("单个备份文件超过 32 MB，请先检查其大小。")
    return raw


def _replace(path: Path, raw: bytes | None) -> None:
    _safe_target(path)
    if raw is None:
        path.unlink(missing_ok=True)
    else:
        core.atomic_write_bytes(path, raw)
    _safe_target(path)


def _hash(raw: bytes | None) -> str | None:
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def _point_path(point_id: str) -> Path:
    if not isinstance(point_id, str) or not POINT_ID.fullmatch(point_id):
        raise core.ManagerError("恢复点标识无效。")
    path = _directory() / (point_id + ".cam-backup")
    _safe_target(path)
    return path


def _metadata(document: dict) -> dict:
    if not isinstance(document, dict):
        raise core.ManagerError("恢复点清单必须是对象。")
    manifest = document.get("manifest")
    if document.get("format") != FORMAT or type(document.get("version")) is not int or document.get("version") != VERSION or not isinstance(manifest, dict):
        raise core.ManagerError("恢复点格式或版本不受支持。")
    if not POINT_ID.fullmatch(str(manifest.get("id") or "")) or not isinstance(manifest.get("scope"), str) or manifest.get("scope") not in {"configuration", "usage"}:
        raise core.ManagerError("恢复点清单无效。")
    source = manifest.get("source")
    expected_source = _source()
    if (not isinstance(source, dict)
            or source.get("application") != expected_source["application"]
            or not isinstance(source.get("codexHome"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", source["codexHome"])
            or not secrets.compare_digest(source["codexHome"], expected_source["codexHome"])
            or not isinstance(source.get("stateDirectory"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", source["stateDirectory"])
            or not secrets.compare_digest(source["stateDirectory"], expected_source["stateDirectory"])):
        raise core.ManagerError("恢复点不属于当前 Agent Manager 数据目录。")
    source_schema = source.get("settingsSchemaVersion")
    if type(source_schema) is not int or not 1 <= source_schema <= core.SCHEMA_VERSION:
        raise core.ManagerError("恢复点来自不受支持的设置版本。")
    files = manifest.get("files")
    required_keys = set(_targets(manifest["scope"], include_agents=False))
    if not isinstance(files, list) or not required_keys or not len(required_keys) <= len(files) <= 208:
        raise core.ManagerError("恢复点文件清单无效。")
    seen_keys = set()
    seen_paths = set()
    total = 0
    for row in files:
        if (not isinstance(row, dict) or not isinstance(row.get("key"), str)
                or not isinstance(row.get("present"), bool)
                or type(row.get("size")) is not int or not 0 <= row["size"] <= MAX_FILE_BYTES):
            raise core.ManagerError("恢复点文件元数据无效。")
        key = row["key"]
        _label, target = _path_for(key, manifest["scope"])
        target_identity = os.path.normcase(os.path.abspath(os.fspath(target)))
        row_label = row.get("label")
        if key in seen_keys or target_identity in seen_paths:
            raise core.ManagerError("恢复点包含重复或不一致的文件目标。")
        if (not isinstance(row_label, str) or not row_label or len(row_label) > 120
                or any(ord(char) < 32 for char in row_label)):
            raise core.ManagerError("恢复点文件标签无效。")
        seen_keys.add(key)
        seen_paths.add(target_identity)
        total += row["size"]
        if row["present"]:
            if not re.fullmatch(r"[a-f0-9]{64}", str(row.get("sha256") or "")):
                raise core.ManagerError("恢复点缺少文件校验值。")
        elif row.get("sha256") is not None or row["size"] != 0:
            raise core.ManagerError("恢复点的缺失文件元数据无效。")
    if not required_keys.issubset(seen_keys) or (manifest["scope"] == "usage" and seen_keys != required_keys) or total > MAX_TOTAL_BYTES:
        raise core.ManagerError("恢复点文件范围不完整或超过安全限制。")
    name = manifest.get("name")
    reason = manifest.get("reason")
    created_at = manifest.get("createdAt")
    if (not isinstance(name, str) or not name or len(name) > 120
            or any(ord(char) < 32 for char in name)):
        raise core.ManagerError("恢复点名称无效。")
    if (not isinstance(reason, str) or not reason or len(reason) > 64
            or any(ord(char) < 32 for char in reason)):
        raise core.ManagerError("恢复点来源说明无效。")
    if not isinstance(created_at, str) or not created_at or len(created_at) > 64:
        raise core.ManagerError("恢复点创建时间无效。")
    try:
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise core.ManagerError("恢复点创建时间无效。") from exc
    return manifest


def _document(point_id: str, *, include_payload: bool = True) -> dict:
    path = _point_path(point_id)
    try:
        with path.open("rb") as stream:
            header = stream.readline(128 * 1024 + 1)
            if len(header) > 128 * 1024 or not header.endswith(b"\n"):
                raise core.ManagerError("恢复点清单超过安全体积限制。")
            document = _json_loads(header.decode("utf-8"))
            if not isinstance(document, dict):
                raise core.ManagerError("恢复点清单必须是对象。")
            if include_payload:
                raw = stream.read(MAX_ENVELOPE_BYTES + 1)
                if len(raw) > MAX_ENVELOPE_BYTES:
                    raise core.ManagerError("恢复点超过安全体积限制。")
                document["protected"] = raw.decode("ascii")
        _safe_target(path)
        if not isinstance(document, dict) or _metadata(document)["id"] != point_id:
            raise core.ManagerError("恢复点与清单标识不一致。")
        return document
    except (OSError, ValueError) as exc:
        raise core.ManagerError("恢复点缺失或损坏，未修改当前配置。") from exc


def _decode(document: dict) -> tuple[dict, dict[str, bytes | None]]:
    manifest = _metadata(document)
    try:
        encoded_envelope = document["protected"]
        if not isinstance(encoded_envelope, str):
            raise ValueError("protected envelope is not text")
        protected = base64.b64decode(encoded_envelope, validate=True)
        payload = _json_loads(core.dpapi_unprotect(protected))
        if (not isinstance(payload, dict)
                or payload.get("format") != document.get("format")
                or type(payload.get("version")) is not int
                or payload.get("version") != document.get("version")
                or payload.get("manifest") != manifest):
            raise ValueError("manifest differs from authenticated payload")
        payload_files = payload.get("files")
        if not isinstance(payload_files, dict):
            raise ValueError("files payload is not an object")
        files = {}
        total = 0
        seen = set()
        for row in manifest["files"]:
            key = row["key"]
            if key in seen:
                raise ValueError("duplicate entry")
            seen.add(key)
            _path_for(key, manifest["scope"])
            encoded = payload_files[key]
            if encoded is not None and not isinstance(encoded, str):
                raise ValueError("file payload is not text")
            raw = base64.b64decode(encoded, validate=True) if encoded is not None else None
            size = len(raw) if raw is not None else 0
            total += size
            if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or size != row["size"] or _hash(raw) != row["sha256"] or (raw is not None) != row["present"]:
                raise ValueError("integrity check failed")
            files[key] = raw
        if set(payload_files) != seen:
            raise ValueError("unlisted entries")
        return manifest, files
    except (KeyError, TypeError, ValueError, OSError, core.ManagerError) as exc:
        raise core.ManagerError("恢复点校验或解密失败；需要创建备份的 Windows 用户，且备份不能被修改。") from exc


def create(scope: str = "configuration", name: str = "", *, reason: str = "manual") -> dict:
    name = str(name or "").strip()
    reason = str(reason or "").strip()
    if len(name) > 120 or any(ord(char) < 32 for char in name):
        raise core.ManagerError("备份名称须为 120 字以内的单行文本。")
    if not reason or len(reason) > 64 or any(ord(char) < 32 for char in reason):
        raise core.ManagerError("备份来源说明须为 64 字以内的单行文本。")
    with LOCK, core.SWITCH_OPERATION_LOCK, core.CONFIG_FILE_LOCK, core.RUNTIME_OVERLAY_LOCK, core.SETTINGS_LOCK, core.SECRETS_LOCK, core._settings_file_lock():
        return _create_locked(scope, name, reason)


def _create_locked(scope: str, name: str, reason: str, *, raw_files: dict[str, bytes | None] | None = None) -> dict:
    targets = _targets(scope)
    files = dict(raw_files) if raw_files is not None else {key: _read(path) for key, (_label, path) in targets.items()}
    if any(not isinstance(key, str) or raw is not None and not isinstance(raw, bytes) for key, raw in files.items()):
        raise core.ManagerError("备份文件内容无效。")
    for key in files:
        _path_for(key, scope)
    if sum(len(raw) for raw in files.values() if raw is not None) > MAX_TOTAL_BYTES:
        raise core.ManagerError("配置备份超过 64 MB，请先检查异常文件。")
    point_id = "point-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex
    manifest = {"id": point_id, "scope": scope, "name": name or ("配置与账号备份" if scope == "configuration" else "用量统计备份"),
                "reason": reason, "createdAt": core.now_iso(), "source": _source(), "files": [
                    {"key": key, "label": _path_for(key, scope)[0], "present": raw is not None, "size": len(raw) if raw is not None else 0, "sha256": _hash(raw)}
                    for key, raw in files.items()]}
    payload = {"format": FORMAT, "version": VERSION, "manifest": manifest,
               "files": {key: base64.b64encode(raw).decode("ascii") if raw is not None else None for key, raw in files.items()}}
    protected = base64.b64encode(core.dpapi_protect(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))).decode("ascii")
    document = {"format": FORMAT, "version": VERSION, "manifest": manifest, "protected": protected}
    # Verify before publishing a new restore point in the visible list.
    _decode(document)
    header = json.dumps({key: value for key, value in document.items() if key != "protected"}, ensure_ascii=False, separators=(",", ":"))
    point_path = _point_path(point_id)
    try:
        core.atomic_write_bytes(point_path, header.encode("utf-8") + b"\n" + protected.encode("ascii"))
        verified_manifest, verified_files = _decode(_document(point_id))
        if verified_manifest != manifest or verified_files != files:
            raise core.ManagerError("恢复点落盘回验失败。")
    except Exception:
        try:
            point_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return {**manifest, "totalBytes": sum(row["size"] for row in manifest["files"]), "encrypted": True}


def list_points() -> dict:
    with LOCK:
        directory = _directory()
        _safe_target(directory)
        points = []
        invalid = 0
        if directory.is_dir():
            for path in sorted(directory.glob("point-*.cam-backup"), reverse=True)[:200]:
                try:
                    manifest, _files = _decode(_document(path.stem))
                    points.append({**manifest, "totalBytes": sum(int(row.get("size") or 0) for row in manifest["files"]), "encrypted": True})
                except (core.ManagerError, TypeError, ValueError, OSError):
                    invalid += 1
    return {"points": points, "invalidCount": invalid, "directory": str(directory), "machineBound": True}


def _preview_for(manifest: dict, files: dict[str, bytes | None], current_files: dict[str, bytes | None]) -> dict:
    rows = []
    fingerprints = {}
    for key, raw in files.items():
        label, _path = _path_for(key, manifest["scope"])
        current = current_files[key]
        fingerprints[key] = _hash(current)
        rows.append({"key": key, "label": label, "currentBytes": len(current) if current is not None else 0,
                     "backupBytes": len(raw) if raw is not None else 0,
                     "action": "unchanged" if current == raw else "remove" if raw is None else "restore" if current is not None else "create"})
    fingerprint_payload = {"format": FORMAT, "version": VERSION, "manifest": manifest, "current": fingerprints}
    fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"point": manifest, "files": rows, "changed": sum(row["action"] != "unchanged" for row in rows),
            "fingerprint": fingerprint, "requiresCodexClosed": manifest["scope"] == "configuration"}


def _capture_current(files: dict[str, bytes | None], scope: str) -> dict[str, bytes | None]:
    return {key: _read(_path_for(key, scope)[1]) for key in files}


def preview(point_id: str) -> dict:
    with LOCK:
        manifest, files = _decode(_document(point_id))
        return _preview_for(manifest, files, _capture_current(files, manifest["scope"]))


def _validate_restored_files(files: dict[str, bytes | None], scope: str) -> None:
    for key, raw in files.items():
        if raw is None:
            continue
        _label, path = _path_for(key, scope)
        try:
            if path.suffix == ".toml":
                tomllib.loads(raw.decode("utf-8-sig"))
            elif path.suffix == ".json":
                document = _json_loads(raw.decode("utf-8-sig"))
                if not isinstance(document, dict):
                    raise ValueError("object required")
                if key == "settings" and int(document.get("schemaVersion", 0)) > core.SCHEMA_VERSION:
                    raise ValueError("newer settings schema")
            elif path.suffix == ".md":
                raw.decode("utf-8-sig")
        except (ValueError, TypeError) as exc:
            raise core.ManagerError("备份内的配置格式无效或来自更高版本，未执行恢复。") from exc


def _runtime_overlay_plan(files: dict[str, bytes | None], scope: str) -> list[tuple[Path, bytes | None]]:
    if scope != "configuration":
        return []
    payload = core._runtime_overlay_read()
    if not payload:
        return []
    plan = []
    for key, raw in files.items():
        if key not in {"codex-config", "codex-rules"} and not key.startswith("agent:"):
            continue
        path = _path_for(key, scope)[1]
        relative = core._runtime_overlay_relative(path)
        if relative in payload["files"]:
            plan.append((path, raw))
    if plan and int(payload.get("ownerPid") or 0) != os.getpid():
        raise core.ManagerError("运行时配置覆盖不属于当前管理器进程，请重启 Agent Manager 后再恢复。")
    return plan


def _rollback_targets(
    attempted: list[str],
    files: dict[str, bytes | None],
    before: dict[str, bytes | None],
    scope: str,
    transaction_states: dict[str, bytes | None],
) -> list[str]:
    errors = []
    for key in reversed(attempted):
        try:
            path = _path_for(key, scope)[1]
            current = _read(path)
            if current == before[key]:
                continue
            allowed_current = [files[key]]
            if key in transaction_states:
                allowed_current.append(transaction_states[key])
            if not any(current == expected for expected in allowed_current):
                raise core.ManagerError("检测到外部修改，已保留该内容和安全备份")
            _replace(path, before[key])
            if _read(path) != before[key]:
                raise core.ManagerError("回滚后的文件校验失败")
        except Exception as restore_error:
            errors.append(f"{key}: {core._redact_sensitive_text(restore_error, limit=140)}")
    return errors


def restore(point_id: str, expected_fingerprint: str) -> dict:
    if not isinstance(expected_fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_fingerprint):
        raise core.ManagerError("请先预览恢复内容。")
    with LOCK, core.SWITCH_OPERATION_LOCK, core.CONFIG_FILE_LOCK, core.RUNTIME_OVERLAY_LOCK, core.SETTINGS_LOCK, core.SECRETS_LOCK, core._settings_file_lock():
        manifest, files = _decode(_document(point_id))
        _validate_restored_files(files, manifest["scope"])
        before = _capture_current(files, manifest["scope"])
        current_preview = _preview_for(manifest, files, before)
        if not secrets.compare_digest(current_preview["fingerprint"], expected_fingerprint):
            raise core.ManagerError("当前配置在预览后已变化，请重新预览。")
        if manifest["scope"] == "configuration" and core.running_codex_processes():
            raise core.ManagerError("恢复配置前请先关闭 Codex，当前任务不会被自动结束。")
        if not current_preview["changed"]:
            return {"restored": False, "changed": 0, "message": "当前文件与恢复点一致。"}
        overlay_path = core.RUNTIME_OVERLAY_FILE
        overlay_before = None
        overlay_plan = []
        if manifest["scope"] == "configuration":
            overlay_before = _read(overlay_path)
            overlay_plan = _runtime_overlay_plan(files, manifest["scope"])
        safety = _create_locked(manifest["scope"], "恢复前安全备份", "before_restore", raw_files=before)
        attempted = []
        written = []
        transaction_states = {}
        overlay_attempted = False
        try:
            for key, raw in files.items():
                _label, path = _path_for(key, manifest["scope"])
                if before[key] == raw:
                    continue
                if _read(path) != before[key]:
                    raise core.ManagerError("文件在恢复期间被其他程序修改。")
                attempted.append(key)
                _replace(path, raw)
                if _read(path) != raw:
                    raise core.ManagerError("恢复后的文件校验失败。")
                written.append(key)
            if overlay_plan:
                if _read(overlay_path) != overlay_before:
                    raise core.ManagerError("运行时配置覆盖在恢复期间被其他程序修改。")
                overlay_attempted = True
                for path, raw in overlay_plan:
                    if _read(path) != raw:
                        raise core.ManagerError("文件在运行时基线重建前被其他程序修改。")
                    settings_before_rebase = None
                    track_settings = path == core.CONFIG_FILE and "settings" in files
                    if track_settings:
                        settings_path = _path_for("settings", manifest["scope"])[1]
                        settings_before_rebase = _read(settings_path)
                        if settings_before_rebase != files["settings"]:
                            raise core.ManagerError("设置文件在运行时基线重建前被其他程序修改。")
                    try:
                        if not core._runtime_overlay_rebase_user_file(path, raw):
                            raise core.ManagerError("运行时配置覆盖基线未能重建。")
                    finally:
                        if track_settings:
                            settings_after_rebase = _read(settings_path)
                            if settings_after_rebase != settings_before_rebase:
                                transaction_states["settings"] = settings_after_rebase
                                if "settings" not in attempted:
                                    attempted.append("settings")
            for key in files:
                expected = transaction_states.get(key, files[key])
                if _read(_path_for(key, manifest["scope"])[1]) != expected:
                    raise core.ManagerError(f"恢复完成前检测到文件被其他程序修改：{key}。")
        except Exception as exc:
            errors = []
            if overlay_attempted:
                try:
                    _replace(overlay_path, overlay_before)
                    if _read(overlay_path) != overlay_before:
                        raise core.ManagerError("运行时恢复基线回滚校验失败")
                except Exception as overlay_error:
                    errors.append("runtime-overlay: " + core._redact_sensitive_text(overlay_error, limit=140))
            errors.extend(_rollback_targets(attempted, files, before, manifest["scope"], transaction_states))
            detail = "；".join(errors) if errors else "已回滚本次写入"
            raise core.ManagerError(f"恢复未完成：{core._redact_sensitive_text(exc, limit=180)}；{detail}。安全备份：{safety['id']}") from exc
        core.invalidate_codex_version_cache()
        return {"restored": True, "changed": len(written), "safetyBackupId": safety["id"], "scope": manifest["scope"], "requiresApply": manifest["scope"] == "configuration"}


def delete(point_id: str) -> dict:
    with LOCK:
        path = _point_path(point_id)
        _document(point_id, include_payload=False)
        path.unlink()
    return {"deleted": True, "id": point_id}
