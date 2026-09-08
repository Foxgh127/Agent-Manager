#!/usr/bin/env python3
"""Transactional Claude Desktop profile management.

Claude Desktop and Claude Code are separate surfaces.  This module only manages
Claude Desktop's documented 1P/3P configuration files.  It never captures or
switches Anthropic OAuth sessions and it does not modify Claude Code settings.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import threading
from typing import Any
import urllib.parse
import uuid

import agent_manager.core as core


PROFILE_ID = "00000000-0000-4000-8000-000000a6e17a"
PROFILE_NAME = "Agent Manager"
REGISTRY_FILE = core.STATE_DIR / "claude-desktop-profiles.json"
SECRETS_FILE = core.STATE_DIR / "claude-desktop-secrets.json"
TRANSACTION_FILE = core.STATE_DIR / "claude-desktop-transaction.json"
MAX_JSON_DOCUMENT_BYTES = 32 * 1024 * 1024
_LOCK = threading.RLock()
_SAFE_ROUTE = re.compile(r"^(?:anthropic/)?claude-(?:sonnet|opus|haiku|fable)-[A-Za-z0-9._-]+$", re.I)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _local_app_data() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))


def _pick_claude_dir(threep: bool) -> Path:
    root = _local_app_data()
    exact = root / ("Claude-3p" if threep else "Claude")
    if exact.exists():
        return exact
    candidates = []
    try:
        for item in root.iterdir():
            name = item.name.casefold()
            if item.is_dir() and name.startswith("claude") and (("-3p" in name) == threep):
                candidates.append(item)
    except OSError:
        pass
    return sorted(candidates, key=lambda value: value.name.casefold())[0] if candidates else exact


def configuration_paths() -> dict[str, Path]:
    normal = _pick_claude_dir(False)
    threep = _pick_claude_dir(True)
    library = threep / "configLibrary"
    return {
        "normalConfig": normal / "claude_desktop_config.json",
        "threepConfig": threep / "claude_desktop_config.json",
        "library": library,
        "profile": library / f"{PROFILE_ID}.json",
        "meta": library / "_meta.json",
    }


def _read_json(path: Path, default: Any, *, strict: bool = False) -> Any:
    if not path.exists():
        return default
    try:
        size = path.stat().st_size
        if size <= 0 or size > MAX_JSON_DOCUMENT_BYTES:
            raise ValueError("file size outside safe bounds")
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        if strict:
            raise core.ManagerError(f"Claude 状态文件 {path.name} 损坏或过大。") from exc
        return default
    return value


def _json_object(path: Path, *, strict: bool = False) -> dict:
    value = _read_json(path, {}, strict=strict)
    if strict and not isinstance(value, dict):
        raise core.ManagerError(f"Claude 配置文件 {path.name} 必须是 JSON 对象，已保留原文件。")
    return value if isinstance(value, dict) else {}


def _registry() -> dict:
    value = _read_json(REGISTRY_FILE, {}, strict=True)
    if not isinstance(value, dict):
        raise core.ManagerError("Claude Desktop Provider 列表不是 JSON 对象。")
    value.setdefault("schemaVersion", 1)
    value.setdefault("profiles", [])
    if not isinstance(value["profiles"], list) or any(not isinstance(item, dict) for item in value["profiles"]):
        raise core.ManagerError("Claude Desktop Provider 列表格式无效，已保留原文件。")
    return value


def _secret_store() -> dict:
    value = _read_json(SECRETS_FILE, {}, strict=True)
    if not isinstance(value, dict):
        raise core.ManagerError("Claude Desktop 密钥索引不是 JSON 对象。")
    value.setdefault("scheme", "windows-dpapi-current-user-v1")
    value.setdefault("profiles", {})
    if not isinstance(value["profiles"], dict):
        raise core.ManagerError("Claude Desktop 密钥索引格式无效，已保留原文件。")
    return value


def _encrypt(text: str) -> str:
    if os.name != "nt":
        raise core.ManagerError("Claude Desktop 账号密钥存储目前仅支持 Windows。")
    return base64.b64encode(core.dpapi_protect(text)).decode("ascii")


def _decrypt(ciphertext: str) -> str:
    try:
        return core.dpapi_unprotect(base64.b64decode(ciphertext)).strip()
    except Exception as exc:
        raise core.ManagerError("Claude Desktop Provider 密钥无法由当前 Windows 用户解密。") from exc


def _validate_base_url(value: object) -> str:
    return core._validated_provider_url(str(value or ""), label="Claude Desktop Base URL")


def _normalize_models(value: object) -> list[dict]:
    rows = value if isinstance(value, list) else []
    result: list[dict] = []
    seen: set[str] = set()
    for raw in rows[:40]:
        if isinstance(raw, str):
            raw = {"name": raw}
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("id") or "").strip()
        if not name or not _SAFE_ROUTE.fullmatch(name):
            raise core.ManagerError(
                f"Claude Desktop 直连模型 `{name or '空值'}` 不安全；只接受 claude-sonnet/opus/haiku/fable-*。"
            )
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "name": name,
                "labelOverride": str(raw.get("labelOverride") or "").strip() or None,
                "supports1m": bool(raw.get("supports1m")),
            }
        )
    return result


def save_profile(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise core.ManagerError("Claude Desktop Provider 数据无效。")
    profile_id = str(payload.get("id") or f"claude_{uuid.uuid4().hex[:16]}").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{2,63}", profile_id):
        raise core.ManagerError("Claude Desktop Provider ID 格式无效。")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise core.ManagerError("请输入 Claude Desktop Provider 名称。")
    base_url = _validate_base_url(payload.get("baseUrl"))
    api_key = core._validated_provider_secret(payload.get("apiKey"), allow_empty=True)
    models = _normalize_models(payload.get("models"))
    with _LOCK:
        _require_transaction_recovery()
        registry = _registry()
        secret_store = _secret_store()
        snapshot = core._capture_file_bytes((SECRETS_FILE, REGISTRY_FILE))
        existing = next((item for item in registry["profiles"] if item.get("id") == profile_id), None)
        if not api_key and not existing:
            raise core.ManagerError("请输入 Anthropic 兼容 API Key。")
        if api_key:
            secret_store["profiles"][profile_id] = _encrypt(api_key)
        now = _now_iso()
        record = {
            "id": profile_id,
            "name": name,
            "baseUrl": base_url,
            "models": models,
            "mode": "direct",
            "createdAt": (existing or {}).get("createdAt") or now,
            "updatedAt": now,
        }
        registry["profiles"] = [item for item in registry["profiles"] if item.get("id") != profile_id]
        registry["profiles"].append(record)
        try:
            core.atomic_write_json(SECRETS_FILE, secret_store)
            core.atomic_write_json(REGISTRY_FILE, registry)
        except Exception as exc:
            rollback_errors = core._restore_file_bytes(snapshot)
            if rollback_errors:
                raise core.ManagerError(
                    f"保存 Claude Desktop Provider 失败：{exc}；回滚也未完成：{'；'.join(rollback_errors)}"
                ) from exc
            raise
    return {**record, "apiKeyConfigured": True}


def _applied_id(paths: dict[str, Path]) -> str | None:
    value = _json_object(paths["meta"]).get("appliedId")
    return str(value) if value else None


def list_profiles() -> dict:
    paths = configuration_paths()
    registry = _registry()
    secrets_store = _secret_store().get("profiles", {})
    applied = _applied_id(paths)
    profiles = [
        {
            "id": "official",
            "name": "Claude Desktop 官方登录",
            "mode": "official",
            "active": applied != PROFILE_ID,
            "apiKeyConfigured": False,
            "description": "恢复 Claude Desktop 原生 1P 登录；不读取或切换 OAuth 会话。",
        }
    ]
    for item in registry.get("profiles", []):
        if not isinstance(item, dict):
            continue
        profiles.append(
            {
                **item,
                "active": applied == PROFILE_ID and registry.get("activeProfileId") == item.get("id"),
                "apiKeyConfigured": bool(secrets_store.get(str(item.get("id") or ""))),
            }
        )
    return {
        "profiles": profiles,
        "activeProfileId": registry.get("activeProfileId") if applied == PROFILE_ID else "official",
        "paths": {key: str(value) for key, value in paths.items()},
        "desktopRunning": bool(_running_claude_desktop_processes()),
        "supportsOauthSwitching": False,
    }


def _running_claude_desktop_processes() -> list[dict]:
    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('Claude.exe','Claude Desktop.exe') } | "
        "Select-Object ProcessId,Name,ExecutablePath | ConvertTo-Json -Compress"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            creationflags=flags,
        )
        raw = json.loads(completed.stdout or "[]") if completed.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    rows = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
    result = []
    for row in rows:
        path = str(row.get("ExecutablePath") or "")
        normalized = path.replace("/", "\\").casefold()
        if path and ("\\claude\\" in normalized or "\\windowsapps\\" in normalized):
            result.append({"pid": row.get("ProcessId"), "name": row.get("Name"), "path": path})
    return result


def _transaction_paths(paths: dict[str, Path]) -> dict[str, Path]:
    return {
        "normalConfig": paths["normalConfig"],
        "threepConfig": paths["threepConfig"],
        "profile": paths["profile"],
        "meta": paths["meta"],
        "registry": REGISTRY_FILE,
    }


def _snapshot(paths: dict[str, Path]) -> dict:
    files = []
    for key, path in _transaction_paths(paths).items():
        if path.is_file() and path.stat().st_size > MAX_JSON_DOCUMENT_BYTES:
            raise core.ManagerError(f"Claude 配置文件 {path.name} 超过安全快照上限。")
        content = path.read_bytes() if path.is_file() else None
        files.append(
            {
                "key": key,
                "path": str(path),
                "exists": content is not None,
                "content": _encrypt(base64.b64encode(content).decode("ascii")) if content is not None else None,
            }
        )
    return {"schemaVersion": 1, "createdAt": _now_iso(), "files": files}


def _normalized_path(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _validated_transaction_journal(journal: object) -> dict:
    if not isinstance(journal, dict) or journal.get("schemaVersion") != 1:
        raise core.ManagerError("Claude Desktop 事务日志格式无效。")
    raw_files = journal.get("files")
    if not isinstance(raw_files, list):
        raise core.ManagerError("Claude Desktop 事务日志缺少文件快照。")
    expected = _transaction_paths(configuration_paths())
    required = {"normalConfig", "threepConfig", "profile", "meta", "registry"}
    seen: set[str] = set()
    files = []
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise core.ManagerError("Claude Desktop 事务日志包含无效文件项。")
        key = str(raw.get("key") or "")
        if key not in expected or key in seen:
            raise core.ManagerError("Claude Desktop 事务日志包含未知或重复文件项。")
        if _normalized_path(str(raw.get("path") or "")) != _normalized_path(expected[key]):
            raise core.ManagerError("Claude Desktop 事务日志文件路径与当前配置不匹配。")
        exists = raw.get("exists")
        content = raw.get("content")
        if not isinstance(exists, bool) or (exists and not isinstance(content, str)):
            raise core.ManagerError("Claude Desktop 事务日志文件内容无效。")
        if not exists and content is not None:
            raise core.ManagerError("Claude Desktop 事务日志缺失文件包含意外内容。")
        seen.add(key)
        files.append({**raw, "key": key, "path": str(expected[key])})
    if not required.issubset(seen):
        raise core.ManagerError("Claude Desktop 事务日志文件快照不完整。")
    return {**journal, "files": files}


def _restore_snapshot(journal: dict) -> list[str]:
    try:
        journal = _validated_transaction_journal(journal)
    except core.ManagerError as exc:
        return [str(exc)]
    errors = []
    for item in journal.get("files", []):
        try:
            path = Path(str(item["path"]))
            if item.get("exists"):
                content = base64.b64decode(_decrypt(str(item.get("content") or "")))
                core.atomic_write_bytes(path, content)
            else:
                path.unlink(missing_ok=True)
        except Exception as exc:
            errors.append(f"{item.get('key')}: {str(exc)[:180]}")
    return errors


def recover_incomplete_transaction() -> dict:
    if not TRANSACTION_FILE.is_file():
        return {"recovered": False, "errors": []}
    try:
        size = TRANSACTION_FILE.stat().st_size
        if size <= 0 or size > MAX_JSON_DOCUMENT_BYTES:
            raise core.ManagerError("Claude Desktop 事务日志大小异常。")
        journal = json.loads(TRANSACTION_FILE.read_text(encoding="utf-8"))
        journal = _validated_transaction_journal(journal)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, core.ManagerError) as exc:
        return {"recovered": False, "errors": [f"Claude Desktop 事务日志损坏：{str(exc)[:240]}"]}
    errors = _restore_snapshot(journal)
    if not errors:
        try:
            TRANSACTION_FILE.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"删除事务日志：{str(exc)[:180]}")
    return {"recovered": not errors, "errors": errors}


def transaction_health() -> dict:
    """Inspect the journal without decrypting snapshots or mutating Claude files."""

    if not TRANSACTION_FILE.exists():
        return {
            "healthy": True,
            "status": "ok",
            "pending": False,
            "recoverable": False,
            "detail": "没有未完成的 Claude Desktop 配置事务。",
        }
    try:
        size = TRANSACTION_FILE.stat().st_size
        if size <= 0 or size > 32 * 1024 * 1024:
            raise core.ManagerError("Claude Desktop 事务日志大小异常。")
        journal = json.loads(TRANSACTION_FILE.read_text(encoding="utf-8"))
        journal = _validated_transaction_journal(journal)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, core.ManagerError) as exc:
        return {
            "healthy": False,
            "status": "error",
            "pending": True,
            "recoverable": False,
            "detail": f"Claude Desktop 事务日志损坏：{str(exc)[:240]}",
        }
    return {
        "healthy": False,
        "status": "warning",
        "pending": True,
        "recoverable": True,
        "operation": str(journal.get("operation") or "profile_apply"),
        "createdAt": journal.get("createdAt"),
        "fileCount": len(journal.get("files", [])),
        "detail": "发现未完成的 Claude Desktop 配置事务，可从加密快照恢复。",
    }


def configuration_health() -> dict:
    """Validate registry/secret references without decrypting API keys."""

    issues: list[str] = []
    try:
        raw_registry = _read_json(REGISTRY_FILE, {}, strict=True) if REGISTRY_FILE.exists() else {}
        if not isinstance(raw_registry, dict):
            raise core.ManagerError("Claude Desktop Provider 列表不是 JSON 对象。")
        profiles = raw_registry.get("profiles", [])
        if not isinstance(profiles, list):
            raise core.ManagerError("Claude Desktop Provider 列表格式无效。")
    except core.ManagerError as exc:
        return {"healthy": False, "status": "error", "issues": [str(exc)], "repairable": False}
    try:
        raw_secrets = _read_json(SECRETS_FILE, {}, strict=True) if SECRETS_FILE.exists() else {}
        if not isinstance(raw_secrets, dict):
            raise core.ManagerError("Claude Desktop 密钥索引不是 JSON 对象。")
        encrypted = raw_secrets.get("profiles", {})
        if not isinstance(encrypted, dict):
            raise core.ManagerError("Claude Desktop 密钥索引格式无效。")
    except core.ManagerError as exc:
        return {"healthy": False, "status": "error", "issues": [str(exc)], "repairable": False}
    profile_ids = [
        str(item.get("id") or "").strip()
        for item in profiles if isinstance(item, dict)
    ]
    if len(profile_ids) != len(profiles) or not all(profile_ids):
        issues.append("Claude Desktop Provider 存在无效或缺失 ID。")
    if len(profile_ids) != len(set(profile_ids)):
        issues.append("Claude Desktop Provider 存在重复 ID。")
    missing_keys = sorted(set(profile_ids) - set(encrypted))
    orphan_keys = sorted(set(encrypted) - set(profile_ids))
    if missing_keys:
        issues.append(f"{len(missing_keys)} 个 Claude Desktop Provider 缺少加密 API Key。")
    if orphan_keys:
        issues.append(f"{len(orphan_keys)} 个 Claude Desktop 加密密钥没有对应 Provider。")
    active = str(raw_registry.get("activeProfileId") or "official")
    if active != "official" and active not in set(profile_ids):
        issues.append(f"Claude Desktop 当前 Provider `{active}` 已不存在。")
    return {
        "healthy": not issues,
        "status": "ok" if not issues else "error",
        "issues": issues,
        "repairable": False,
        "profileCount": len(profile_ids),
        "encryptedKeyCount": len(encrypted),
    }


def _require_transaction_recovery() -> None:
    recovery = recover_incomplete_transaction()
    if recovery["errors"]:
        raise core.ManagerError(
            "上次 Claude Desktop 配置恢复未完成，请先在紧急修复中处理："
            + "；".join(recovery["errors"])
        )


def _write_mode(path: Path, mode: str) -> None:
    value = _json_object(path, strict=True)
    value["deploymentMode"] = mode
    core.atomic_write_json(path, value)


def _write_meta(path: Path, enabled: bool) -> None:
    value = _json_object(path, strict=True)
    if not isinstance(value.get("entries", []), list):
        raise core.ManagerError("Claude 配置目录条目格式无效，已保留原文件。")
    entries = [item for item in value.get("entries", []) if isinstance(item, dict) and item.get("id") != PROFILE_ID]
    if enabled:
        entries.append({"id": PROFILE_ID, "name": PROFILE_NAME})
        value["appliedId"] = PROFILE_ID
    elif value.get("appliedId") == PROFILE_ID:
        next_id = next((str(item.get("id")) for item in entries if item.get("id")), None)
        if next_id:
            value["appliedId"] = next_id
        else:
            value.pop("appliedId", None)
    value["entries"] = entries
    core.atomic_write_json(path, value)


def _remove_managed_enterprise_fields(path: Path) -> None:
    if not path.exists():
        return
    value = _json_object(path, strict=True)
    enterprise = value.get("enterpriseConfig")
    if isinstance(enterprise, dict):
        for key in (
            "disableDeploymentModeChooser",
            "inferenceGatewayApiKey",
            "inferenceGatewayAuthScheme",
            "inferenceGatewayBaseUrl",
            "inferenceProvider",
        ):
            enterprise.pop(key, None)
        if not enterprise:
            value.pop("enterpriseConfig", None)
    core.atomic_write_json(path, value)


def _transactional_apply(operation, commit=None) -> None:
    paths = configuration_paths()
    if _running_claude_desktop_processes():
        raise core.ManagerError("请先完全退出 Claude Desktop，再切换 Provider；Agent Manager 不会强制结束它。")
    _require_transaction_recovery()
    journal = _snapshot(paths)
    core.atomic_write_json(TRANSACTION_FILE, journal)
    try:
        operation(paths)
        if commit is not None:
            commit()
        TRANSACTION_FILE.unlink(missing_ok=True)
    except Exception as exc:
        rollback_errors = _restore_snapshot(journal)
        if not rollback_errors:
            try:
                TRANSACTION_FILE.unlink(missing_ok=True)
            except OSError as unlink_exc:
                rollback_errors.append(f"删除事务日志：{str(unlink_exc)[:180]}")
        if not rollback_errors:
            raise
        raise core.ManagerError(f"Claude Desktop 配置失败：{exc}；回滚也未完成：{'；'.join(rollback_errors)}") from exc


def apply_profile(profile_id: str) -> dict:
    profile_id = str(profile_id or "").strip()
    if profile_id == "official":
        return restore_official()
    with _LOCK:
        _require_transaction_recovery()
        registry = _registry()
        profile = next((item for item in registry["profiles"] if item.get("id") == profile_id), None)
        if not isinstance(profile, dict):
            raise core.ManagerError("Claude Desktop Provider 不存在。")
        ciphertext = _secret_store().get("profiles", {}).get(profile_id)
        if not ciphertext:
            raise core.ManagerError("该 Claude Desktop Provider 缺少 API Key。")
        api_key = _decrypt(str(ciphertext))
        base_url = _validate_base_url(profile.get("baseUrl"))
        models = _normalize_models(profile.get("models"))

        def operation(paths: dict[str, Path]) -> None:
            _write_mode(paths["normalConfig"], "3p")
            _write_mode(paths["threepConfig"], "3p")
            gateway = {
                "coworkEgressAllowedHosts": [urllib.parse.urlsplit(base_url).hostname],
                "disableDeploymentModeChooser": True,
                "inferenceGatewayApiKey": api_key,
                "inferenceGatewayAuthScheme": "bearer",
                "inferenceGatewayBaseUrl": base_url,
                "inferenceProvider": "gateway",
            }
            if models:
                gateway["inferenceModels"] = [
                    ({key: value for key, value in item.items() if value is not None and key != "supports1m"} | ({"supports1m": True} if item["supports1m"] else {}))
                    if item.get("labelOverride") or item.get("supports1m")
                    else item["name"]
                    for item in models
                ]
            core.atomic_write_json(paths["profile"], gateway)
            _write_meta(paths["meta"], True)
            if _json_object(paths["normalConfig"]).get("deploymentMode") != "3p":
                raise core.ManagerError("Claude Desktop 主配置写入后校验失败。")
            if _json_object(paths["profile"]).get("inferenceGatewayBaseUrl") != base_url:
                raise core.ManagerError("Claude Desktop Provider 写入后校验失败。")

        registry["activeProfileId"] = profile_id
        _transactional_apply(operation, lambda: core.atomic_write_json(REGISTRY_FILE, registry))
    return {"applied": True, "profileId": profile_id, "restartRequired": True}


def restore_official() -> dict:
    with _LOCK:
        _require_transaction_recovery()
        registry = _registry()

        def operation(paths: dict[str, Path]) -> None:
            _write_mode(paths["normalConfig"], "1p")
            _write_mode(paths["threepConfig"], "1p")
            _remove_managed_enterprise_fields(paths["threepConfig"])
            paths["profile"].unlink(missing_ok=True)
            _write_meta(paths["meta"], False)
            if _json_object(paths["normalConfig"]).get("deploymentMode") != "1p":
                raise core.ManagerError("Claude Desktop 官方模式恢复后校验失败。")

        registry["activeProfileId"] = "official"
        _transactional_apply(operation, lambda: core.atomic_write_json(REGISTRY_FILE, registry))
    return {"applied": True, "profileId": "official", "restartRequired": True}


def delete_profile(profile_id: str) -> dict:
    profile_id = str(profile_id or "").strip()
    if not profile_id or profile_id == "official":
        raise core.ManagerError("官方 Claude Desktop 模式不能删除。")
    with _LOCK:
        _require_transaction_recovery()
        registry = _registry()
        existed = any(item.get("id") == profile_id for item in registry["profiles"])
        if not existed:
            raise core.ManagerError("Claude Desktop Provider 不存在。")
        if registry.get("activeProfileId") == profile_id and _applied_id(configuration_paths()) == PROFILE_ID:
            restore_official()
            registry = _registry()
        snapshot = core._capture_file_bytes((SECRETS_FILE, REGISTRY_FILE))
        registry["profiles"] = [item for item in registry["profiles"] if item.get("id") != profile_id]
        secret_store = _secret_store()
        secret_store["profiles"].pop(profile_id, None)
        try:
            core.atomic_write_json(SECRETS_FILE, secret_store)
            core.atomic_write_json(REGISTRY_FILE, registry)
        except Exception as exc:
            rollback_errors = core._restore_file_bytes(snapshot)
            if rollback_errors:
                raise core.ManagerError(
                    f"删除 Claude Desktop Provider 失败：{exc}；回滚也未完成：{'；'.join(rollback_errors)}"
                ) from exc
            raise
    return {"deleted": True, "profileId": profile_id}


def preview_claude_code_import() -> dict:
    path = Path.home() / ".claude" / "settings.json"
    value = _json_object(path)
    env = value.get("env") if isinstance(value.get("env"), dict) else {}
    base_url = str(env.get("ANTHROPIC_BASE_URL") or "").strip()
    has_key = bool(str(env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or "").strip())
    model_values = [env.get(key) for key in ("ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL")]
    models = [str(value).strip() for value in model_values if str(value or "").strip() and _SAFE_ROUTE.fullmatch(str(value).strip())]
    return {
        "available": bool(base_url and has_key),
        "source": str(path),
        "name": "从 Claude Code 导入",
        "baseUrl": base_url or None,
        "apiKeyConfigured": has_key,
        "models": list(dict.fromkeys(models)),
        "warning": "只读取 Claude Code 的第三方 Provider 环境项；不会导入或切换 Claude OAuth。",
    }
