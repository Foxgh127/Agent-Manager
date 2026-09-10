#!/usr/bin/env python3
"""Codex skills, update and emergency-repair services.

This module deliberately stays separate from the account/routing core.  It is
allowed to inspect Codex installation metadata and user-authored skill files,
but it never reads or returns account tokens and never writes CODEX_CLI_PATH.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
import tomllib
from typing import Any
import urllib.error
import urllib.request
from urllib.parse import urlparse

import tomlkit

import agent_manager.core as core
import agent_manager.updates.desktop as desktop_updates


SKILL_CATALOG_URL = "https://raw.githubusercontent.com/openai/plugins/main/.agents/plugins/marketplace.json"
UPDATE_CACHE_FILE = core.STATE_DIR / "update-cache.json"
SKILLS_CACHE_FILE = core.STATE_DIR / "skills-cache.json"
SKILL_CATALOG_CACHE_FILE = core.STATE_DIR / "skill-catalog-cache.json"
DIAGNOSTICS_CACHE_FILE = core.STATE_DIR / "diagnostics-cache.json"
SKILL_TRASH_DIR = core.STATE_DIR / "skills-trash"
UPDATE_INTERVAL_SECONDS = 6 * 60 * 60
UPDATE_CACHE_STALE_SECONDS = 7 * 24 * 60 * 60
DIAGNOSTICS_CACHE_STALE_SECONDS = 24 * 60 * 60
MAX_SKILL_FILE_BYTES = 1_000_000
# Asset-heavy skills can legitimately contain several thousand templates,
# icons or reference fragments.  Keep the independent entry/byte ceilings as
# the hard safety bounds, but do not reject a normal asset bundle merely for
# crossing the old 2,000-file threshold.
MAX_SKILL_TREE_FILES = 8_000
MAX_SKILL_TREE_ENTRIES = 10_000
MAX_SKILL_TREE_BYTES = 64 * 1024 * 1024
MAX_SKILL_ASSET_SAMPLES = 64
SKILL_CONFIG_WRITE_TIMEOUT_SECONDS = 8
MAX_CATALOG_BYTES = 5_000_000
PLUGIN_INSTALL_PROOF_SECONDS = 300
_REMOTE_METADATA_HOSTS = frozenset(
    filter(
        None,
        (
            (urlparse(SKILL_CATALOG_URL).hostname or "").casefold(),
            "registry.npmjs.org",
            "api.github.com",
            "github.com",
            "raw.githubusercontent.com",
        ),
    )
)

_SKILLS_LOCK = threading.RLock()
_UPDATE_LOCK = threading.RLock()
_REPAIR_LOCK = threading.Lock()
_VERIFIED_PLUGIN_INSTALLS: dict[str, tuple[str, float]] = {}


def _disk_health(path: Path) -> dict:
    try:
        usage = shutil.disk_usage(path if path.exists() else path.parent)
    except OSError as exc:
        return {
            "status": "error",
            "detail": f"无法读取磁盘剩余空间：{str(exc)[:220]}",
            "freeBytes": None,
        }
    warning_floor = 250 * 1024 * 1024
    status = "warning" if usage.free < warning_floor else "ok"
    return {
        "status": status,
        "detail": (
            f"Agent Manager 所在磁盘仅剩 {usage.free / 1024 / 1024:.0f} MB，备份或更新可能失败。"
            if status == "warning"
            else f"磁盘剩余 {usage.free / 1024 / 1024 / 1024:.1f} GB。"
        ),
        "freeBytes": usage.free,
        "totalBytes": usage.total,
    }


def _runtime_overlay_health() -> dict:
    if not core.RUNTIME_OVERLAY_FILE.exists():
        return {
            "status": "ok",
            "active": False,
            "recoverable": False,
            "detail": "没有残留的临时 Codex 配置覆盖。",
        }
    try:
        payload = core._runtime_overlay_read()
    except Exception as exc:
        return {
            "status": "error",
            "active": True,
            "recoverable": False,
            "detail": f"临时配置恢复记录损坏：{str(exc)[:240]}",
        }
    if payload is None:
        return {
            "status": "ok",
            "active": False,
            "recoverable": False,
            "detail": "没有残留的临时 Codex 配置覆盖。",
        }
    try:
        owner_pid = int(payload.get("ownerPid") or 0)
    except (TypeError, ValueError):
        owner_pid = 0
    file_count = len(payload.get("files", {}))
    environment_count = len(payload.get("environment", {}))
    owner_is_current_process = owner_pid == os.getpid()
    owner_is_live_manager = owner_is_current_process or _runtime_owner_is_live_manager(owner_pid)
    if owner_is_live_manager:
        return {
            "status": "ok",
            "active": True,
            "recoverable": False,
            "ownerPid": owner_pid,
            "fileCount": file_count,
            "environmentCount": environment_count,
            "detail": (
                "当前 Agent Manager 正在管理临时 Codex 配置，退出时会恢复。"
                if owner_is_current_process
                else "检测到正在运行的 Agent Manager 正在管理临时 Codex 配置，无需修复。"
            ),
        }
    return {
        "status": "warning",
        "active": True,
        "recoverable": True,
        "ownerPid": owner_pid,
        "fileCount": file_count,
        "environmentCount": environment_count,
        "detail": "发现不属于当前进程的临时 Codex 配置，可安全按恢复记录还原。",
    }


def _runtime_owner_is_live_manager(owner_pid: int) -> bool:
    """Verify an overlay owner through the manager's bounded loopback health endpoint."""
    if owner_pid <= 0:
        return False
    runtime_file = core.STATE_DIR / "app-runtime.json"
    connection: http.client.HTTPConnection | None = None
    try:
        if runtime_file.stat().st_size > 65_536:
            return False
        runtime = json.loads(runtime_file.read_text(encoding="utf-8"))
        if not isinstance(runtime, dict):
            return False
        runtime_pid = int(runtime.get("pid") or 0)
        port = int(runtime.get("port") or 0)
        nonce = str(runtime.get("runtimeNonce") or "")
        if (
            runtime.get("appId") != "openai-agent-manager"
            or runtime_pid != owner_pid
            or not 0 < port <= 65_535
            or len(nonce) < 16
        ):
            return False
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.7)
        connection.request("GET", "/api/health", headers={"Connection": "close"})
        response = connection.getresponse()
        if response.status != 200:
            return False
        raw = response.read(65_537)
        if len(raw) > 65_536:
            return False
        health = json.loads(raw.decode("utf-8"))
        return bool(
            isinstance(health, dict)
            and health.get("ok") is True
            and health.get("appId") == "openai-agent-manager"
            and int(health.get("runtimePid") or 0) == owner_pid
            and secrets.compare_digest(str(health.get("runtimeNonce") or ""), nonce)
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, http.client.HTTPException):
        return False
    finally:
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass


def _validate_remote_metadata_url(url: str) -> None:
    try:
        parsed = urlparse(str(url or ""))
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError as exc:
        raise core.ManagerError("远端元数据地址格式或端口无效。") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not hostname
        or hostname not in _REMOTE_METADATA_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise core.ManagerError("远端元数据地址未通过 HTTPS 与公开域名校验。")
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        return
    if not address.is_global:
        raise core.ManagerError("远端元数据不能指向本机、内网或保留地址。")


class _SafeMetadataRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        _validate_remote_metadata_url(new_url)
        return super().redirect_request(request, fp, code, message, headers, new_url)


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).replace(microsecond=0).isoformat()


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _safe_json(path: Path, default: object) -> object:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default
    return payload


def _canonical(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except OSError:
        return Path(os.path.abspath(os.path.expanduser(str(path))))


def _path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(_canonical(Path(path)))))


def _workspace_cache_key(cwd: Path) -> str:
    return hashlib.sha256(_path_key(cwd).encode("utf-8", errors="replace")).hexdigest()[:24]


def _within(path: Path, root: Path) -> bool:
    try:
        _canonical(path).relative_to(_canonical(root))
        return True
    except ValueError:
        return False


def _skill_id(skill_md: Path) -> str:
    return hashlib.sha256(_path_key(skill_md).encode("utf-8", errors="replace")).hexdigest()[:24]


def _is_reparse_path(path: Path) -> bool:
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError as exc:
        raise core.ManagerError("无法确认技能目录的链接状态。") from exc


def _bounded_skill_files(skill_dir: Path) -> list[Path]:
    """Return a deterministic, bounded set of regular files for one skill.

    Skills are user writable and may accidentally contain a dependency cache,
    repository checkout or directory link.  Inventory refresh must therefore
    never recursively read an unbounded tree.  Links are rejected rather than
    followed so the same inspection is safe for both listing and deletion.
    """
    if _is_reparse_path(skill_dir):
        raise core.ManagerError("技能目录是符号链接或目录联接，请手动检查内容。")
    files: list[Path] = []
    pending = [skill_dir]
    entry_count = 0
    total_bytes = 0
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    entry_count += 1
                    if entry_count > MAX_SKILL_TREE_ENTRIES:
                        raise core.ManagerError("技能目录项目过多，已停止自动扫描，请手动检查内容。")
                    path = Path(entry.path)
                    try:
                        if entry.is_symlink():
                            raise core.ManagerError("技能目录包含符号链接，请手动检查内容。")
                        if _is_reparse_path(path):
                            raise core.ManagerError("技能目录包含符号链接或目录联接，请手动检查内容。")
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        size = int(entry.stat(follow_symlinks=False).st_size)
                    except OSError as exc:
                        raise core.ManagerError("无法完整检查技能目录，请刷新后重试。") from exc
                    files.append(path)
                    total_bytes += max(0, size)
                    if len(files) > MAX_SKILL_TREE_FILES or total_bytes > MAX_SKILL_TREE_BYTES:
                        raise core.ManagerError("技能目录规模异常，已停止自动扫描，请手动检查内容。")
        except core.ManagerError:
            raise
        except OSError as exc:
            raise core.ManagerError("无法完整检查技能目录，请刷新后重试。") from exc
    return sorted(files, key=lambda value: value.relative_to(skill_dir).as_posix().casefold())


def _skill_fingerprint_from_files(root: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    asset_files = [
        item
        for item in files
        if (relative := item.relative_to(root)).parts
        and relative.parts[0].casefold() == "assets"
    ]
    if len(asset_files) <= MAX_SKILL_ASSET_SAMPLES:
        sampled_assets = set(asset_files)
    else:
        sampled_assets = {
            asset_files[index * (len(asset_files) - 1) // (MAX_SKILL_ASSET_SAMPLES - 1)]
            for index in range(MAX_SKILL_ASSET_SAMPLES)
        }
    for item in files:
        relative_path = item.relative_to(root)
        relative = relative_path.as_posix().encode("utf-8", errors="replace")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        if _is_reparse_path(item):
            raise core.ManagerError("技能内容在校验期间变成了符号链接或目录联接。")
        try:
            stat_result = item.stat(follow_symlinks=False)
        except OSError as exc:
            raise core.ManagerError("无法完整读取技能指纹，请刷新后重试。") from exc
        if not stat.S_ISREG(stat_result.st_mode):
            raise core.ManagerError("技能目录包含非普通文件，请手动检查内容。")
        size = stat_result.st_size
        digest.update(int(size).to_bytes(8, "big", signed=False))
        digest.update(int(stat_result.st_mtime_ns).to_bytes(8, "big", signed=False))
        digest.update(int(stat_result.st_ctime_ns).to_bytes(8, "big", signed=False))
        digest.update(int(stat_result.st_ino).to_bytes(8, "big", signed=False))
        digest.update(int(stat_result.st_mode).to_bytes(8, "big", signed=False))
        # Assets are often large collections of icons, templates and font
        # fragments. Reading every byte made inventory take tens of seconds on
        # Windows, while size+mtime alone can be restored after an edit. Hash a
        # bounded prefix/suffix sample; instruction, script and reference files
        # keep the full content hash.
        is_asset = bool(relative_path.parts) and relative_path.parts[0].casefold() == "assets"
        if is_asset and item not in sampled_assets:
            continue
        if is_asset:
            try:
                with item.open("rb") as stream:
                    prefix = stream.read(4_096)
                    suffix = b""
                    middle = b""
                    if size > 4_096:
                        stream.seek(max(0, size - 4_096))
                        suffix = stream.read(4_096)
                    if size > 12_288:
                        span = max(1, size - 4_096)
                        offset = int.from_bytes(hashlib.sha256(relative).digest()[:8], "big") % span
                        stream.seek(offset)
                        middle = stream.read(4_096)
                after = item.stat(follow_symlinks=False)
            except OSError as exc:
                raise core.ManagerError("无法完整读取技能资产指纹，请刷新后重试。") from exc
            if (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
                after.st_ino,
                after.st_mode,
            ) != (
                stat_result.st_size,
                stat_result.st_mtime_ns,
                stat_result.st_ctime_ns,
                stat_result.st_ino,
                stat_result.st_mode,
            ):
                raise core.ManagerError("技能资产在指纹校验期间发生了变化。")
            digest.update(prefix)
            digest.update(middle)
            digest.update(suffix)
        elif size <= MAX_SKILL_FILE_BYTES:
            try:
                content = item.read_bytes()
                after = item.stat(follow_symlinks=False)
            except OSError as exc:
                raise core.ManagerError("无法完整读取技能指纹，请刷新后重试。") from exc
            if (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
                after.st_ino,
                after.st_mode,
            ) != (
                stat_result.st_size,
                stat_result.st_mtime_ns,
                stat_result.st_ctime_ns,
                stat_result.st_ino,
                stat_result.st_mode,
            ):
                raise core.ManagerError("技能内容在指纹校验期间发生了变化。")
            digest.update(content)
    return digest.hexdigest()


def _skill_fingerprint(skill_md: Path) -> str:
    root = skill_md.parent
    return _skill_fingerprint_from_files(root, _bounded_skill_files(root))


def _bounded_skill_tree(skill_dir: Path) -> list[Path]:
    """Reject unexpectedly large trees before a mutable skill is quarantined.

    A skill can live under a user-writable repository.  Walking an unbounded
    tree while holding the skills mutation lock would let an accidental nested
    checkout or generated directory freeze the manager.  Symlinks are not
    followed, and the limits are deliberately generous for normal skills.
    """
    return _bounded_skill_files(skill_dir)


def _frontmatter(skill_md: Path) -> tuple[dict[str, str], list[str]]:
    errors: list[str] = []
    try:
        if skill_md.stat().st_size > MAX_SKILL_FILE_BYTES:
            return {}, ["SKILL.md 超过 1 MB，未读取。"]
        text = skill_md.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        return {}, [f"无法读取 SKILL.md：{str(exc)[:180]}"]
    if not text.startswith("---"):
        return {}, ["SKILL.md 缺少 YAML frontmatter。"]
    end = text.find("\n---", 3)
    if end < 0:
        return {}, ["SKILL.md frontmatter 没有结束标记。"]
    result: dict[str, str] = {}
    for raw_line in text[3:end].splitlines():
        match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)$", raw_line.strip())
        if not match:
            continue
        value = match.group(2).strip().strip('"\'')
        result[match.group(1)] = value
    if not result.get("name"):
        errors.append("frontmatter 缺少 name。")
    if not result.get("description"):
        errors.append("frontmatter 缺少 description。")
    return result, errors


def _configured_skill_states() -> dict[str, bool]:
    if not core.CONFIG_FILE.is_file():
        return {}
    try:
        parsed = core.read_toml(core.CONFIG_FILE)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}
    configs = parsed.get("skills", {}).get("config", []) if isinstance(parsed.get("skills"), dict) else []
    states: dict[str, bool] = {}
    if isinstance(configs, list):
        for item in configs:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            states[_path_key(str(item["path"]))] = bool(item.get("enabled", True))
    return states


def _scope_for_path(skill_md: Path, cwd: Path) -> tuple[str, bool, str]:
    home = Path.home()
    official_user = home / ".agents" / "skills"
    legacy_user = core.CODEX_HOME / "skills"
    plugin_cache = core.CODEX_HOME / "plugins" / "cache"
    if _within(skill_md, plugin_cache) or ".system" in {part.casefold() for part in skill_md.parts}:
        return "system", False, "plugin_or_system"
    if _within(skill_md, official_user):
        return "user", True, "official_user"
    if _within(skill_md, legacy_user):
        return "user", True, "legacy_codex_home"
    current = _canonical(cwd)
    for parent in (current, *current.parents):
        if _within(skill_md, parent / ".agents" / "skills"):
            return "repo", True, "repository"
    return "unknown", False, "external"


def _skill_record(
    skill_md: Path,
    cwd: Path,
    source: dict | None = None,
    configured_states: dict[str, bool] | None = None,
) -> dict:
    skill_md = _canonical(skill_md)
    metadata, errors = _frontmatter(skill_md)
    scope, mutable, source_kind = _scope_for_path(skill_md, cwd)
    states = configured_states if configured_states is not None else _configured_skill_states()
    source = source or {}
    source_scope = str(source.get("scope") or "").casefold()
    if source_scope in {"system", "admin"}:
        scope, mutable = source_scope, False
    elif source_scope in {"user", "repo"}:
        scope = source_scope
    enabled = bool(source.get("enabled", states.get(_path_key(skill_md), True)))
    try:
        skill_files = _bounded_skill_files(skill_md.parent)
        fingerprint = _skill_fingerprint_from_files(skill_md.parent, skill_files)
        scripts_root = skill_md.parent / "scripts"
        has_scripts = any(_within(item, scripts_root) for item in skill_files)
    except core.ManagerError as exc:
        # Keep the rest of the inventory usable.  An abnormal tree is visible
        # but immutable until the user fixes it and refreshes the list.
        errors.append(str(exc))
        mutable = False
        fingerprint = ""
        has_scripts = False
    return {
        "id": _skill_id(skill_md),
        "name": str(source.get("name") or metadata.get("name") or skill_md.parent.name),
        "description": str(source.get("description") or metadata.get("description") or ""),
        "path": str(skill_md),
        "directory": str(skill_md.parent),
        "scope": scope,
        "sourceKind": source_kind,
        "enabled": enabled,
        "mutable": bool(mutable and not skill_md.parent.is_symlink()),
        "managed": bool(mutable),
        "hasScripts": has_scripts,
        "errors": [*errors, *[str(item) for item in source.get("errors", []) if str(item)]],
        "fingerprint": fingerprint,
    }


def _candidate_skill_roots(cwd: Path) -> list[Path]:
    roots = [Path.home() / ".agents" / "skills", core.CODEX_HOME / "skills"]
    current = _canonical(cwd)
    for parent in (current, *current.parents):
        roots.append(parent / ".agents" / "skills")
    plugin_root = core.CODEX_HOME / "plugins" / "cache"
    if plugin_root.is_dir():
        try:
            roots.extend(item for item in plugin_root.glob("*/*/skills") if item.is_dir())
        except OSError:
            pass
    unique: dict[str, Path] = {}
    for root in roots:
        unique.setdefault(_path_key(root), root)
    return list(unique.values())


def _fallback_skills(cwd: Path) -> tuple[list[dict], list[str]]:
    result: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()
    configured_states = _configured_skill_states()
    for root in _candidate_skill_roots(cwd):
        if not root.is_dir():
            continue
        try:
            candidates = list(root.glob("*/SKILL.md"))
            if (root / "SKILL.md").is_file():
                candidates.append(root / "SKILL.md")
        except OSError as exc:
            errors.append(f"无法扫描 {root}：{str(exc)[:160]}")
            continue
        for candidate in candidates:
            key = _path_key(candidate)
            if key in seen:
                continue
            seen.add(key)
            result.append(_skill_record(candidate, cwd, configured_states=configured_states))
    return result, errors


def _app_server_skills(cwd: Path, force: bool) -> tuple[list[dict], list[str]]:
    response = core.codex_app_server_request(
        "skills/list",
        {"cwds": [str(cwd)], "forceReload": bool(force)},
        timeout=25,
    )
    groups: list[dict] = []
    if isinstance(response, dict):
        for key in ("data", "items", "results"):
            value = response.get(key)
            if isinstance(value, list):
                groups = [item for item in value if isinstance(item, dict)]
                break
        if not groups and isinstance(response.get("skills"), list):
            groups = [response]
    records: list[dict] = []
    errors: list[str] = []
    configured_states = _configured_skill_states()
    for group in groups:
        errors.extend(str(item) for item in group.get("errors", []) if str(item))
        for raw in group.get("skills", []):
            if not isinstance(raw, dict) or not raw.get("path"):
                continue
            path = Path(str(raw["path"]))
            skill_md = path if path.name.casefold() == "skill.md" else path / "SKILL.md"
            if skill_md.is_file():
                records.append(_skill_record(skill_md, cwd, raw, configured_states))
    return records, errors


def _empty_skill_inventory(cwd: Path) -> dict:
    return {
        "skills": [],
        "count": 0,
        "enabled": 0,
        "source": "cache_miss",
        "cwd": str(cwd),
        "errors": [],
        "appServerError": None,
        "restartRequiredAfterChange": True,
        "checkedAt": None,
        "cached": True,
        "needsManualRefresh": True,
    }


def _cached_skill_inventory(cwd: Path) -> dict:
    payload = _safe_json(SKILLS_CACHE_FILE, {})
    workspaces = payload.get("workspaces", {}) if isinstance(payload, dict) else {}
    cached = workspaces.get(_workspace_cache_key(cwd)) if isinstance(workspaces, dict) else None
    if not isinstance(cached, dict) or not isinstance(cached.get("skills"), list):
        return _empty_skill_inventory(cwd)
    return {
        **cached,
        "cwd": str(cwd),
        "cached": True,
        "needsManualRefresh": False,
    }


def _store_skill_inventory(cwd: Path, inventory: dict) -> None:
    payload = _safe_json(SKILLS_CACHE_FILE, {})
    payload = payload if isinstance(payload, dict) else {}
    workspaces = payload.get("workspaces") if isinstance(payload.get("workspaces"), dict) else {}
    workspaces[_workspace_cache_key(cwd)] = {
        **inventory,
        "cwd": str(cwd),
        "checkedAt": _iso(),
        "cached": False,
        "needsManualRefresh": False,
    }
    core.atomic_write_json(
        SKILLS_CACHE_FILE,
        {"schemaVersion": 1, "updatedAt": _iso(), "workspaces": workspaces},
    )


def _update_cached_skill(cwd: Path, record: dict | None, skill_id: str) -> None:
    cached = _cached_skill_inventory(cwd)
    if cached.get("needsManualRefresh"):
        return
    records = [item for item in cached.get("skills", []) if str(item.get("id")) != str(skill_id)]
    if record is not None:
        records.append(record)
    records.sort(
        key=lambda item: (
            item.get("scope", ""),
            str(item.get("name", "")).casefold(),
            str(item.get("path", "")).casefold(),
        )
    )
    _store_skill_inventory(
        cwd,
        {
            **{
                key: value
                for key, value in cached.items()
                if key not in {"skills", "count", "enabled", "cached", "needsManualRefresh"}
            },
            "skills": records,
            "count": len(records),
            "enabled": sum(1 for item in records if item.get("enabled")),
        },
    )


def list_skills(cwd: str | Path | None = None, force: bool = False) -> dict:
    cwd_path = _canonical(Path(cwd or os.getcwd()))
    if not force:
        return _cached_skill_inventory(cwd_path)
    app_server_error = None
    try:
        records, errors = _app_server_skills(cwd_path, force)
        source = "codex_app_server"
    except Exception as exc:
        records, errors = [], []
        source = "filesystem_fallback"
        app_server_error = str(exc)[:300]
    fallback, fallback_errors = _fallback_skills(cwd_path)
    by_path = {_path_key(item["path"]): item for item in records}
    for item in fallback:
        by_path.setdefault(_path_key(item["path"]), item)
    records = sorted(by_path.values(), key=lambda item: (item["scope"], item["name"].casefold(), item["path"].casefold()))
    result = {
        "skills": records,
        "count": len(records),
        "enabled": sum(1 for item in records if item["enabled"]),
        "source": source,
        "cwd": str(cwd_path),
        "errors": [*errors, *fallback_errors],
        "appServerError": app_server_error,
        "restartRequiredAfterChange": True,
        "checkedAt": _iso(),
        "cached": False,
        "needsManualRefresh": False,
    }
    try:
        _store_skill_inventory(cwd_path, result)
    except Exception:
        result["errors"].append("技能列表已读取，但本地缓存写入失败。")
        result["cacheWarning"] = "请在磁盘恢复可写后手动刷新技能列表。"
    return result


def _find_skill(skill_id: str, cwd: str | Path | None = None, *, force: bool = False) -> dict:
    inventory = list_skills(cwd=cwd, force=force)
    for item in inventory["skills"]:
        if secrets.compare_digest(str(item["id"]), str(skill_id)):
            return item
    # A first-run cache miss must still discover newly installed skills.  Once
    # an inventory exists, callers such as enable/disable should not turn a
    # constant-time config change into another recursive tree scan.
    if not force:
        for item in list_skills(cwd=cwd, force=True)["skills"]:
            if secrets.compare_digest(str(item["id"]), str(skill_id)):
                return item
    raise core.ManagerError("没有找到这个技能；它可能已被移动或删除。")


def _write_skill_config(skill_md: Path, enabled: bool | None) -> None:
    with _SKILLS_LOCK, core.CONFIG_FILE_LOCK, core.RUNTIME_OVERLAY_LOCK:
        original_bytes = core.CONFIG_FILE.read_bytes() if core.CONFIG_FILE.is_file() else None
        try:
            original = core.decode_toml_bytes(original_bytes or b"")
        except UnicodeDecodeError as exc:
            raise core.ManagerError("Codex config.toml 不是有效 UTF-8，未修改技能设置。") from exc
        try:
            doc = tomlkit.parse(original) if original.strip() else tomlkit.document()
        except Exception as exc:
            raise core.ManagerError(f"Codex config.toml 无法解析，未修改技能设置：{exc}") from exc
        skills = doc.get("skills")
        if skills is None:
            skills = tomlkit.table()
            doc["skills"] = skills
        elif not hasattr(skills, "get"):
            raise core.ManagerError("Codex config.toml 中 skills 必须是表，未覆盖现有配置。")
        configs = skills.get("config")
        if configs is None:
            configs = tomlkit.aot()
            skills["config"] = configs
        elif not isinstance(configs, list):
            raise core.ManagerError("Codex config.toml 中 skills.config 必须是表数组，未覆盖现有配置。")
        if any(not hasattr(item, "get") for item in configs):
            raise core.ManagerError("Codex config.toml 中 skills.config 包含无效条目，未修改。")
        target_key = _path_key(skill_md)
        matches = [index for index, item in enumerate(configs) if item.get("path") and _path_key(str(item.get("path"))) == target_key]
        for index in reversed(matches[1:]):
            del configs[index]
        if enabled is None:
            for index in reversed(matches[:1]):
                del configs[index]
        elif matches:
            configs[matches[0]]["path"] = str(_canonical(skill_md))
            configs[matches[0]]["enabled"] = bool(enabled)
        else:
            entry = tomlkit.table()
            entry.add("path", str(_canonical(skill_md)))
            entry.add("enabled", bool(enabled))
            configs.append(entry)
        if len(configs) == 0:
            del skills["config"]
        rendered = tomlkit.dumps(doc)
        try:
            tomllib.loads(rendered)
        except tomllib.TOMLDecodeError as exc:
            raise core.ManagerError(f"技能配置生成失败，未写入：{exc}") from exc
        overlay_before = (
            core.RUNTIME_OVERLAY_FILE.read_bytes()
            if core.RUNTIME_OVERLAY_FILE.is_file()
            else None
        )
        overlay = core._runtime_overlay_read()
        overlay_tracks_config = False
        if overlay:
            relative = core._runtime_overlay_relative(core.CONFIG_FILE)
            overlay_tracks_config = relative in overlay.get("files", {})
            if overlay_tracks_config and int(overlay.get("ownerPid") or 0) != os.getpid():
                raise core.ManagerError("运行时配置覆盖不属于当前管理器，请重启后再修改技能状态。")
        rendered_bytes = rendered.encode("utf-8")
        try:
            if original_bytes != rendered_bytes:
                current_bytes = core.CONFIG_FILE.read_bytes() if core.CONFIG_FILE.is_file() else None
                if current_bytes != original_bytes:
                    raise core.ManagerError("Codex config.toml 在编辑期间发生变化，请重试。")
                core.backup_file(core.CONFIG_FILE)
                core.atomic_write_text(core.CONFIG_FILE, rendered)
            if not core.CONFIG_FILE.is_file() or core.CONFIG_FILE.read_bytes() != rendered_bytes:
                raise core.ManagerError("技能配置写入后回验不一致。")
            if overlay_tracks_config and not core._runtime_overlay_rebase_user_file(
                core.CONFIG_FILE,
                rendered_bytes,
            ):
                raise core.ManagerError("运行时配置覆盖基线未能同步技能设置。")
        except Exception as exc:
            rollback_errors = core._restore_file_bytes(
                {
                    core.CONFIG_FILE: original_bytes,
                    core.RUNTIME_OVERLAY_FILE: overlay_before,
                }
            )
            detail = f"；回滚异常：{'；'.join(rollback_errors)}" if rollback_errors else ""
            raise core.ManagerError(f"技能配置更新失败，已回滚：{str(exc)[:240]}{detail}") from exc


def set_skill_enabled(skill_id: str, enabled: bool, cwd: str | Path | None = None) -> dict:
    if not isinstance(skill_id, str) or not re.fullmatch(r"[a-f0-9]{24}", skill_id):
        raise core.ManagerError("技能 ID 格式无效。")
    if not isinstance(enabled, bool):
        raise core.ManagerError("技能启用状态必须是布尔值。")
    item = _find_skill(skill_id, cwd)
    skill_md = _canonical(Path(item["path"]))
    if not skill_md.is_file() or not secrets.compare_digest(_skill_id(skill_md), str(skill_id)):
        raise core.ManagerError("技能已被移动或删除，请手动刷新技能目录后重试。")
    if item["scope"] in {"system", "admin"}:
        raise core.ManagerError("系统或管理员技能不能由 Agent Manager 修改。")
    # Validate and persist locally before asking App Server to refresh its
    # effective view. This prevents a malformed existing config from being
    # overwritten by an external mutation before our fail-closed parser runs.
    _write_skill_config(skill_md, enabled)
    app_server_error = None
    try:
        result = core.codex_app_server_request(
            "skills/config/write",
            {"path": str(skill_md), "enabled": bool(enabled)},
            timeout=SKILL_CONFIG_WRITE_TIMEOUT_SECONDS,
        )
        effective = bool(result.get("effectiveEnabled", enabled)) if isinstance(result, dict) else bool(enabled)
    except Exception as exc:
        app_server_error = str(exc)[:300]
        effective = bool(enabled)
    # App Server may normalize the file or report a policy-adjusted effective
    # state. Re-assert the verified local form and rebase an active overlay.
    _write_skill_config(skill_md, effective)
    # Config writes do not change the skill tree.  Preserve the validated
    # inventory record and update only its effective state instead of forcing
    # a second app-server request plus a full directory fingerprint.
    refreshed = {**item, "enabled": effective}
    cache_warning = None
    try:
        _update_cached_skill(_canonical(Path(cwd or os.getcwd())), refreshed, skill_id)
    except Exception:
        cache_warning = "技能已更新，但本地列表缓存未能同步；请手动刷新。"
    return {
        "skill": refreshed,
        "restartRequired": True,
        "appServerFallback": app_server_error,
        "cacheWarning": cache_warning,
    }


def delete_skill(skill_id: str, expected_fingerprint: str, cwd: str | Path | None = None) -> dict:
    if not isinstance(skill_id, str) or not re.fullmatch(r"[a-f0-9]{24}", skill_id):
        raise core.ManagerError("技能 ID 格式无效。")
    if not isinstance(expected_fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_fingerprint):
        raise core.ManagerError("技能指纹格式无效，请重新预览。")
    item = _find_skill(skill_id, cwd)
    if not item.get("mutable"):
        raise core.ManagerError("这个技能由系统、管理员或插件管理，不能从这里删除。")
    skill_md = Path(item["path"])
    skill_dir = skill_md.parent
    if _is_reparse_path(skill_dir):
        raise core.ManagerError("符号链接技能不能自动删除，请手动处理链接。")
    allowed = [Path.home() / ".agents" / "skills", core.CODEX_HOME / "skills"]
    current = _canonical(Path(cwd or os.getcwd()))
    allowed.extend(parent / ".agents" / "skills" for parent in (current, *current.parents))
    if not any(_within(skill_dir, root) for root in allowed):
        raise core.ManagerError("技能路径不在允许删除的用户或项目目录内。")
    SKILL_TRASH_DIR.mkdir(parents=True, exist_ok=True)
    destination = SKILL_TRASH_DIR / f"{int(time.time())}-{skill_dir.name}-{secrets.token_hex(4)}"
    with _SKILLS_LOCK:
        # Re-check the tree under the same lock that performs the move.  The
        # inventory fingerprint may be cached and the user can edit a skill
        # between opening the confirmation dialog and clicking delete.
        if not skill_md.is_file() or _is_reparse_path(skill_md) or _is_reparse_path(skill_md.parent):
            raise core.ManagerError("技能目录已发生变化，请刷新列表再删除。")
        skill_files = _bounded_skill_tree(skill_dir)
        current_fingerprint = _skill_fingerprint_from_files(skill_dir, skill_files)
        if not secrets.compare_digest(current_fingerprint, str(expected_fingerprint or "")):
            raise core.ManagerError("技能内容在确认后发生了变化，请刷新列表再删除。")
        try:
            os.replace(skill_dir, destination)
        except OSError as exc:
            raise core.ManagerError(f"无法把技能移动到可恢复隔离区：{exc}") from exc
        try:
            quarantined_files = _bounded_skill_tree(destination)
            quarantined_fingerprint = _skill_fingerprint_from_files(destination, quarantined_files)
            if not secrets.compare_digest(quarantined_fingerprint, current_fingerprint):
                raise core.ManagerError("技能进入隔离区后内容校验不一致。")
        except Exception as quarantine_error:
            try:
                os.replace(destination, skill_dir)
            except OSError as restore_error:
                raise core.ManagerError(
                    "技能隔离后校验失败，且目录未能自动恢复。"
                    f" 可恢复位置：{destination}；校验错误：{str(quarantine_error)[:180]}；"
                    f"恢复错误：{str(restore_error)[:180]}"
                ) from quarantine_error
            raise core.ManagerError(
                f"技能隔离后校验失败，目录已恢复到原位置：{str(quarantine_error)[:240]}"
            ) from quarantine_error
        try:
            _write_skill_config(skill_md, None)
        except Exception as config_error:
            try:
                os.replace(destination, skill_dir)
            except OSError as restore_error:
                raise core.ManagerError(
                    "技能配置更新失败，且目录未能从隔离区自动恢复。"
                    f" 可恢复位置：{destination}；配置错误：{str(config_error)[:180]}；"
                    f"恢复错误：{str(restore_error)[:180]}"
                ) from config_error
            raise core.ManagerError(
                f"技能配置更新失败，目录已恢复到原位置：{str(config_error)[:240]}"
            ) from config_error
    cache_warning = None
    try:
        _update_cached_skill(current, None, skill_id)
    except Exception:
        cache_warning = "技能已移入隔离区，但本地列表缓存未能同步；请手动刷新。"
    return {
        "deleted": True,
        "skillId": skill_id,
        "quarantinePath": str(destination),
        "recoverable": True,
        "restartRequired": True,
        "cacheWarning": cache_warning,
    }


def _https_json(url: str, max_bytes: int, timeout: float = 20.0) -> dict:
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_CATALOG_BYTES:
        raise core.ManagerError("远端元数据大小限制无效。")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= float(timeout) <= 120:
        raise core.ManagerError("远端元数据超时配置无效。")
    _validate_remote_metadata_url(url)
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Agent-Manager/7.1.8"})
    try:
        opener = urllib.request.build_opener(_SafeMetadataRedirectHandler())
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise core.ManagerError(f"远端服务返回 HTTP {exc.code}。") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise core.ManagerError(f"无法连接远端服务：{str(exc)[:180]}") from exc
    if len(raw) > max_bytes:
        raise core.ManagerError("远端响应超过安全大小限制。")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise core.ManagerError("远端元数据不是有效 JSON。") from exc
    if not isinstance(payload, dict):
        raise core.ManagerError("远端元数据结构无效。")
    return payload


def _official_catalog_records() -> tuple[str, list[dict]]:
    """Read the public OpenAI directory used to supplement app-server rows.

    The desktop App Server intentionally exposes only the installable subset
    available to its bundled runtime (currently about 40 entries).  The
    public marketplace is the complete browseable directory, so entries that
    do not have a verified local install route are still useful in the UI but
    are marked as browse-only.
    """
    payload = _https_json(SKILL_CATALOG_URL, MAX_CATALOG_BYTES)
    records: list[dict] = []
    raw_plugins = payload.get("plugins", [])
    if not isinstance(raw_plugins, list):
        raise core.ManagerError("官方插件目录缺少有效的 plugins 数组。")
    for item in raw_plugins[:500]:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        name = str(item["name"]).strip()
        if not name:
            continue
        policy = item.get("policy") if isinstance(item.get("policy"), dict) else {}
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        products = policy.get("products") if isinstance(policy.get("products"), list) else []
        records.append(
            {
                "id": name,
                "name": name,
                "pluginName": name,
                "marketplaceName": "openai-curated",
                "marketplaceDisplayName": "OpenAI 官方目录",
                "marketplacePath": None,
                "remoteMarketplaceName": "openai-curated",
                "description": "",
                "category": str(item.get("category") or "Other"),
                "products": [str(value) for value in products],
                "authentication": str(policy.get("authentication") or "UNKNOWN"),
                "sourcePath": str(source.get("path") or ""),
                "repository": "https://github.com/openai/plugins",
                "official": True,
                "kind": "plugin",
                "installable": False,
                "installHint": "当前 Codex 运行时没有返回此条目的可验证安装入口；可先查看详情，升级运行时后再安装。",
            }
        )
    return str(payload.get("name") or "openai-curated"), records


def _plugin_catalog_from_app_server(force: bool) -> dict:
    with _SKILLS_LOCK:
        _VERIFIED_PLUGIN_INSTALLS.clear()
    response = core.codex_app_server_request(
        "plugin/list",
        {"cwds": [str(_canonical(Path.cwd()))], "forceRefetch": bool(force)},
        timeout=45,
    )
    if not isinstance(response, dict):
        raise core.ManagerError("Codex App Server 返回了无效的插件目录。")
    items: list[dict] = []
    for marketplace in response.get("marketplaces", []):
        if not isinstance(marketplace, dict):
            continue
        marketplace_name = str(marketplace.get("name") or "").strip()
        marketplace_path = str(marketplace.get("path") or "").strip() or None
        marketplace_interface = marketplace.get("interface") if isinstance(marketplace.get("interface"), dict) else {}
        for plugin in marketplace.get("plugins", []):
            if not isinstance(plugin, dict):
                continue
            plugin_name = str(plugin.get("name") or "").strip()
            plugin_id = str(plugin.get("id") or "").strip() or (
                f"{plugin_name}@{marketplace_name}" if plugin_name and marketplace_name else ""
            )
            if not plugin_name or not plugin_id:
                continue
            interface = plugin.get("interface") if isinstance(plugin.get("interface"), dict) else {}
            install_policy = str(plugin.get("installPolicy") or "AVAILABLE")
            availability = str(plugin.get("availability") or "AVAILABLE")
            items.append(
                {
                    "id": plugin_id,
                    "name": str(interface.get("displayName") or plugin_name),
                    "pluginName": plugin_name,
                    "marketplaceName": marketplace_name,
                    "marketplaceDisplayName": str(marketplace_interface.get("displayName") or marketplace_name),
                    "marketplacePath": marketplace_path,
                    "remoteMarketplaceName": None if marketplace_path else marketplace_name,
                    "description": str(interface.get("longDescription") or interface.get("shortDescription") or ""),
                    "category": str(interface.get("category") or "Other"),
                    "capabilities": [str(value) for value in interface.get("capabilities", []) if str(value)],
                    "logoUrl": str(interface.get("logoUrl") or interface.get("logoDarkUrl") or "") or None,
                    "installed": bool(plugin.get("installed")),
                    "enabled": bool(plugin.get("enabled")),
                    "version": plugin.get("version"),
                    "localVersion": plugin.get("localVersion"),
                    "installPolicy": install_policy,
                    "availability": availability,
                    "authentication": str(plugin.get("authPolicy") or "ON_USE"),
                    "official": (
                        marketplace_name.casefold() in {"openai-curated", "openai"}
                        or marketplace_name.casefold().startswith("openai-")
                    ),
                    "kind": "plugin",
                    "installable": install_policy != "NOT_AVAILABLE" and availability != "DISABLED_BY_ADMIN",
                }
            )
    supplement_error = None
    try:
        public_name, public_items = _official_catalog_records()
        known_names = {
            str(item.get("pluginName") or item.get("name") or "").strip().casefold()
            for item in items
        }
        for item in public_items:
            plugin_name = str(item.get("pluginName") or item.get("name") or "").strip().casefold()
            if plugin_name and plugin_name not in known_names:
                items.append(item)
                known_names.add(plugin_name)
    except Exception as exc:
        public_name = "openai-curated"
        supplement_error = str(exc)[:300]
    items.sort(key=lambda item: (not item["official"], item["category"], item["name"].casefold()))
    verified = {
        str(item["id"]): (_plugin_install_identity(item), time.monotonic() + PLUGIN_INSTALL_PROOF_SECONDS)
        for item in items
        if item.get("installable") and (item.get("marketplacePath") or item.get("remoteMarketplaceName"))
    }
    with _SKILLS_LOCK:
        _VERIFIED_PLUGIN_INSTALLS.clear()
        _VERIFIED_PLUGIN_INSTALLS.update(verified)
    errors = [
        str(item.get("message") or item)
        for item in response.get("marketplaceLoadErrors", [])
        if item
    ]
    if supplement_error:
        errors.append(f"官方目录补充失败：{supplement_error}")
    return {
        "checkedAt": _iso(),
        "source": "codex_app_server+openai_curated",
        "name": "Codex Plugin Marketplace",
        "items": items,
        "count": len(items),
        "featuredPluginIds": [str(value) for value in response.get("featuredPluginIds", []) if str(value)],
        "catalogName": public_name,
        "errors": errors,
        "error": None,
        "stale": bool(supplement_error),
    }


def public_skill_catalog(force: bool = False) -> dict:
    cached = _safe_json(SKILL_CATALOG_CACHE_FILE, {})
    cached = cached if isinstance(cached, dict) else {}
    if not force:
        if isinstance(cached.get("items"), list):
            return {**cached, "cached": True, "needsManualRefresh": False}
        return {
            "checkedAt": None,
            "source": None,
            "name": "openai-curated",
            "items": [],
            "count": 0,
            "error": None,
            "stale": False,
            "cached": True,
            "needsManualRefresh": True,
        }
    try:
        result = _plugin_catalog_from_app_server(force)
        try:
            core.atomic_write_json(SKILL_CATALOG_CACHE_FILE, result)
            cache_warning = None
        except Exception:
            cache_warning = "插件目录已刷新，但本地缓存写入失败。"
        return {
            **result,
            "cached": False,
            "needsManualRefresh": False,
            "cacheWarning": cache_warning,
        }
    except Exception as app_server_exc:
        with _SKILLS_LOCK:
            _VERIFIED_PLUGIN_INSTALLS.clear()
        app_server_error = str(app_server_exc)[:300]
    try:
        catalog_name, plugins = _official_catalog_records()
        result = {
            "checkedAt": _iso(),
            "source": SKILL_CATALOG_URL,
            "name": catalog_name,
            "items": plugins,
            "count": len(plugins),
            "error": None,
            "appServerError": app_server_error,
            "stale": False,
        }
        try:
            core.atomic_write_json(SKILL_CATALOG_CACHE_FILE, result)
            cache_warning = None
        except Exception:
            cache_warning = "插件目录已读取，但本地缓存写入失败。"
        return {
            **result,
            "cached": False,
            "needsManualRefresh": False,
            "cacheWarning": cache_warning,
        }
    except Exception as exc:
        if cached.get("items"):
            return {
                **cached,
                "cached": True,
                "stale": True,
                "error": str(exc)[:300],
                "needsManualRefresh": False,
            }
        raise


def _plugin_install_identity(item: dict) -> str:
    fields = [
        item.get("id"),
        item.get("pluginName"),
        item.get("marketplacePath"),
        item.get("remoteMarketplaceName"),
        bool(item.get("installable")),
    ]
    return hashlib.sha256(
        json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _verified_plugin_install(item: dict) -> bool:
    plugin_id = str(item.get("id") or "")
    with _SKILLS_LOCK:
        proof = _VERIFIED_PLUGIN_INSTALLS.get(plugin_id)
    return bool(
        proof
        and proof[1] >= time.monotonic()
        and secrets.compare_digest(proof[0], _plugin_install_identity(item))
    )


def install_public_plugin(plugin_id: str, force_refresh: bool = False) -> dict:
    if not isinstance(plugin_id, str):
        raise core.ManagerError("插件 ID 必须是字符串。")
    plugin_id = plugin_id.strip()
    if not plugin_id or len(plugin_id) > 256 or any(ord(character) < 32 for character in plugin_id):
        raise core.ManagerError("请选择要安装的技能或插件。")
    catalog = public_skill_catalog(force=force_refresh)
    item = next((record for record in catalog.get("items", []) if str(record.get("id")) == plugin_id), None)
    if not isinstance(item, dict) or not _verified_plugin_install(item):
        catalog = _plugin_catalog_from_app_server(True)
        try:
            core.atomic_write_json(SKILL_CATALOG_CACHE_FILE, catalog)
        except Exception:
            pass
        item = next((record for record in catalog.get("items", []) if str(record.get("id")) == plugin_id), None)
    if not isinstance(item, dict):
        raise core.ManagerError("该插件已不在当前官方目录中，请刷新技能大厅。")
    if not item.get("installable"):
        raise core.ManagerError(str(item.get("installHint") or "当前 Codex 运行时不支持安装此插件。"))
    params: dict[str, Any] = {
        "pluginName": str(item.get("pluginName") or ""),
        "installAttemptId": secrets.token_hex(16),
    }
    if not params["pluginName"] or len(params["pluginName"]) > 256 or any(
        ord(character) < 32 for character in params["pluginName"]
    ):
        raise core.ManagerError("插件目录返回了无效的安装名称。")
    if item.get("marketplacePath"):
        params["marketplacePath"] = str(item["marketplacePath"])
    elif item.get("remoteMarketplaceName"):
        params["remoteMarketplaceName"] = str(item["remoteMarketplaceName"])
    else:
        raise core.ManagerError("插件目录缺少可验证的 Marketplace 标识，未安装。")
    try:
        result = core.codex_app_server_request("plugin/install", params, timeout=180)
    except Exception as exc:
        with _SKILLS_LOCK:
            _VERIFIED_PLUGIN_INSTALLS.pop(plugin_id, None)
        raise core.ManagerError(
            "插件安装调用未完成，实际状态未知；请刷新插件目录后核对，勿立即重复安装。"
        ) from exc
    if not isinstance(result, dict) or result.get("installed") is False or result.get("error"):
        with _SKILLS_LOCK:
            _VERIFIED_PLUGIN_INSTALLS.pop(plugin_id, None)
        raise core.ManagerError("Codex 未确认插件安装完成；请刷新技能列表核对实际状态。")
    with _SKILLS_LOCK:
        _VERIFIED_PLUGIN_INSTALLS.pop(plugin_id, None)
    return {
        "installed": True,
        "pluginId": plugin_id,
        "authPolicy": result.get("authPolicy") if isinstance(result, dict) else None,
        "appsNeedingAuth": result.get("appsNeedingAuth", []) if isinstance(result, dict) else [],
        "restartRequired": True,
        "message": "安装完成；新任务会载入该插件提供的技能。",
    }


def _npm_installation() -> dict:
    runtime = core._discover_node_npm_runtime()
    if not runtime:
        return {"installed": False, "version": None, "packageRoot": None, "npmCommand": None}
    npm_command = list(runtime["npmCommand"])
    package_root = core._npm_global_prefix(npm_command)
    roots = []
    if package_root:
        roots.extend((package_root / "node_modules", package_root))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            [*npm_command, "root", "--global"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=flags,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            roots.insert(0, Path(completed.stdout.strip()))
    except (OSError, subprocess.TimeoutExpired):
        pass
    package_json = next(
        (root / "@openai" / "codex" / "package.json" for root in roots if (root / "@openai" / "codex" / "package.json").is_file()),
        None,
    )
    version = None
    if package_json:
        payload = _safe_json(package_json, {})
        version = str(payload.get("version") or "") if isinstance(payload, dict) else ""
    return {
        "installed": bool(package_json),
        "version": version or None,
        "packageRoot": str(package_json.parent) if package_json else None,
        "npmCommand": npm_command,
        "node": runtime.get("node"),
    }


def _version_tuple(value: object) -> tuple[int, ...]:
    match = re.match(r"^(\d+(?:\.\d+){1,3})", str(value or "").strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def _empty_update_center_status() -> dict:
    return {
        "schemaVersion": 2,
        "checkedAt": None,
        "lastSuccessAt": None,
        "nextCheckAt": None,
        "failureCount": 0,
        "components": {
            "desktop": {
                "kind": "desktop",
                "installed": None,
                "installedVersion": None,
                "availableVersion": None,
                "updateAvailable": None,
                "updateState": "unknown",
                "source": "built_in_updater",
                "manualInApp": False,
                "supported": False,
                "message": "刷新状态后检查 Microsoft Store 更新",
            },
            "cli": {
                "kind": "cli",
                "installed": None,
                "installedVersion": None,
                "availableVersion": None,
                "updateAvailable": None,
                "updateState": "unknown",
                "canAutoUpdate": False,
            },
        },
        "runtime": None,
        "lastError": None,
        "stale": False,
        "cached": True,
        "needsManualCheck": True,
        "manualOnly": True,
    }


def _normalize_update_cache(cache: dict) -> dict:
    normalized = dict(cache)
    components = cache.get("components") if isinstance(cache.get("components"), dict) else {}
    desktop = dict(components.get("desktop")) if isinstance(components.get("desktop"), dict) else {}
    desktop_installed = desktop.get("installed")
    if desktop.get("source") != "microsoft_store_cli":
        desktop.update({"availableVersion": None, "updateAvailable": None,
                        "updateState": "not_installed" if desktop_installed is False else "not_checked",
                        "source": "microsoft_store", "manualInApp": False, "canAutoUpdate": False,
                        "message": "刷新状态后检查 Microsoft Store 更新"})
    checked_at = _parse_time(desktop.get("checkedAt"))
    if checked_at and (_now() - checked_at).total_seconds() > UPDATE_INTERVAL_SECONDS:
        desktop.update(updateAvailable=None, updateState="stale", message="更新检查已过期，请刷新状态")
    cli = dict(components.get("cli")) if isinstance(components.get("cli"), dict) else {}
    installed_version = _version_tuple(cli.get("installedVersion"))
    available_version = _version_tuple(cli.get("availableVersion"))
    comparable = bool(installed_version and available_version)
    update_available = available_version > installed_version if comparable else None
    cli_installed = cli.get("installed")
    cli.update(
        {
            "kind": "cli",
            "updateAvailable": update_available,
            "updateState": (
                "not_installed"
                if cli_installed is False
                else "available"
                if update_available is True
                else "current"
                if update_available is False
                else "unknown"
            ),
        }
    )
    normalized.update(
        {
            "schemaVersion": 2,
            "components": {"desktop": desktop, "cli": cli},
            "nextCheckAt": None,
            "manualOnly": True,
        }
    )
    return normalized


def update_center_status(force: bool = False) -> dict:
    with _UPDATE_LOCK:
        cache = _safe_json(UPDATE_CACHE_FILE, {})
        cache = cache if isinstance(cache, dict) else {}
        if not force:
            if cache.get("components"):
                cache = _normalize_update_cache(cache)
                last_success = _parse_time(cache.get("lastSuccessAt"))
                return {
                    **cache,
                    "cached": True,
                    "stale": bool(
                        last_success
                        and (_now() - last_success).total_seconds() > UPDATE_CACHE_STALE_SECONDS
                    ),
                    "needsManualCheck": False,
                    "manualOnly": True,
                }
            return _empty_update_center_status()
        checked_at = _now()
        try:
            failure_count = max(0, min(int(cache.get("failureCount") or 0), 1_000_000))
        except (TypeError, ValueError, OverflowError):
            failure_count = 0
        try:
            runtime = core.codex_runtime_status(force=True)
            npm = _npm_installation()
            registry = core._registry_json("https://registry.npmjs.org/@openai%2Fcodex/latest")
            latest_cli = str(registry.get("version") or "").strip()
            installed_cli = str(npm.get("version") or "").strip() or None
            installed_version = _version_tuple(installed_cli)
            available_version = _version_tuple(latest_cli)
            comparable = bool(installed_version and available_version)
            update_available = available_version > installed_version if comparable else None
            cli_installed = bool(npm.get("installed") or runtime.get("available"))
            cli_state = (
                "not_installed"
                if not cli_installed
                else "available"
                if update_available is True
                else "current"
                if update_available is False
                else "unknown"
            )
            desktop = runtime.get("desktop") if isinstance(runtime.get("desktop"), dict) else None
            components = {
                "desktop": desktop_updates.check(desktop),
                "cli": {
                    "kind": "cli",
                    "ownership": "npm" if npm.get("installed") else runtime.get("source"),
                    "installed": cli_installed,
                    "installedVersion": installed_cli,
                    "availableVersion": latest_cli or None,
                    "updateAvailable": update_available,
                    "updateState": cli_state,
                    "canAutoUpdate": bool(npm.get("installed") and npm.get("npmCommand")),
                    "runtimeSource": runtime.get("source"),
                    "command": runtime.get("command"),
                },
            }
            result = {
                "schemaVersion": 2,
                "checkedAt": _iso(checked_at),
                "lastSuccessAt": _iso(checked_at),
                "nextCheckAt": None,
                "failureCount": 0,
                "components": components,
                "runtime": runtime,
                "lastError": None,
                "stale": False,
                "needsManualCheck": False,
                "manualOnly": True,
            }
        except Exception as exc:
            failure_count += 1
            result = {
                **(_normalize_update_cache(cache) if cache.get("components") else _empty_update_center_status()),
                "schemaVersion": 2,
                "checkedAt": _iso(checked_at),
                "nextCheckAt": None,
                "failureCount": failure_count,
                "lastError": str(exc)[:400],
                "stale": True,
                "needsManualCheck": False,
                "manualOnly": True,
            }
        try:
            core.atomic_write_json(UPDATE_CACHE_FILE, result)
            cache_warning = None
        except Exception:
            cache_warning = "更新状态已读取，但本地缓存写入失败。"
        return {**result, "cached": False, "cacheWarning": cache_warning}


def update_cli() -> dict:
    with _UPDATE_LOCK:
        status = update_center_status(force=True)
        if status.get("lastError") or status.get("stale"):
            raise core.ManagerError("官方版本检查未成功，未使用旧缓存执行 Codex CLI 更新。")
        cli = status.get("components", {}).get("cli", {})
        if cli.get("updateAvailable") is False:
            return {
                "updated": False,
                "noop": True,
                "version": cli.get("installedVersion"),
                "status": status,
                "desktopUnaffected": True,
                "message": "Codex CLI 已是最新版，无需更新。",
            }
        if cli.get("updateAvailable") is not True:
            raise core.ManagerError("无法可靠比较 Codex CLI 的已安装版本与官方版本，未执行更新。")
        if not cli.get("canAutoUpdate"):
            raise core.ManagerError("当前 Codex CLI 不是由 npm 管理，不能自动覆盖；可用“扫描并修复”部署受管运行时。")
        latest = str(cli.get("availableVersion") or "").strip()
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", latest):
            raise core.ManagerError("官方 npm 返回的 Codex CLI 版本号无效。")
        npm = _npm_installation()
        raw_npm_command = npm.get("npmCommand")
        if not isinstance(raw_npm_command, (list, tuple)):
            raise core.ManagerError("npm 启动命令格式无效。")
        npm_command = list(raw_npm_command)
        if not npm_command:
            raise core.ManagerError("没有找到可用的 npm。")
        if any(not isinstance(part, str) or not part or "\x00" in part for part in npm_command):
            raise core.ManagerError("npm 启动命令格式无效。")
        probed_version = str(npm.get("version") or "").strip()
        expected_installed = str(cli.get("installedVersion") or "").strip()
        if npm.get("installed") is False or (
            probed_version and expected_installed and probed_version != expected_installed
        ):
            raise core.ManagerError("Codex CLI 安装状态在检查后发生变化，未执行更新。")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            completed = subprocess.run(
                [
                    *npm_command,
                    "install",
                    "--global",
                    f"@openai/codex@{latest}",
                    "--registry=https://registry.npmjs.org",
                    "--no-audit",
                    "--no-fund",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
                creationflags=flags,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            core.invalidate_codex_version_cache()
            raise core.ManagerError("更新 Codex CLI 失败或超时；安装状态可能已变化，请重新检查。") from exc
        if completed.returncode != 0:
            core.invalidate_codex_version_cache()
            detail = core._redact_sensitive_text(
                (completed.stderr or completed.stdout or "未知错误").strip()[-700:],
                limit=700,
            )
            raise core.ManagerError(f"npm 更新 Codex CLI 失败：{detail}")
        # This operation intentionally does not create or modify CODEX_CLI_PATH.
        core.invalidate_codex_version_cache()
        refreshed = update_center_status(force=True)
        refreshed_cli = refreshed.get("components", {}).get("cli", {})
        if (
            refreshed.get("lastError")
            or str(refreshed_cli.get("installedVersion") or "").strip() != latest
            or refreshed_cli.get("updateAvailable") is not False
        ):
            raise core.ManagerError("npm 已返回成功，但未能确认 Codex CLI 已更新到目标版本，请重新检查。")
        return {"updated": True, "version": latest, "status": refreshed, "desktopUnaffected": True}


def open_desktop_update() -> dict:
    """Check and apply the exact installed Store package without opening Codex."""
    desktop = core._detect_codex_windows_app(force=True) if os.name == "nt" else None
    if not desktop:
        raise core.ManagerError("没有检测到 Codex Desktop。")
    try:
        result = desktop_updates.apply(desktop, detector=core._detect_codex_windows_app)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        raise core.ManagerError(str(exc)) from exc
    with _UPDATE_LOCK:
        cached = _normalize_update_cache(_safe_json(UPDATE_CACHE_FILE, {}) or {})
        cached.setdefault("components", {})["desktop"] = result["component"]
        cached["checkedAt"] = _iso(_now())
        core.atomic_write_json(UPDATE_CACHE_FILE, cached)
    return {**result, "status": cached}


def _check(check_id: str, title: str, status: str, detail: str, **extra: object) -> dict:
    auto_fixable = bool(extra.pop("autoFixable", False)) and status in {"warning", "error"}
    return {
        "id": check_id,
        "title": title,
        "status": status,
        "detail": detail,
        "autoFixable": auto_fixable,
        **extra,
    }


def _empty_diagnostics() -> dict:
    return {
        "schemaVersion": 2,
        "checkedAt": None,
        "checks": [],
        "summary": {"ok": 0, "warning": 0, "error": 0},
        "healthy": None,
        "repairableCount": 0,
        "enableRepair": False,
        "configurationStatus": None,
        "cached": True,
        "needsManualCheck": True,
    }


def _normalize_cached_diagnostics(cached: dict) -> dict:
    repairable_ids = {
        "generated_configuration",
        "settings_references",
        "runtime_overlay",
        "claude_transaction",
        "radar_cache",
        "stale_subagents",
        "runtime_model",
    }
    # Overlay ownership changes with the process lifecycle and is too dynamic
    # to trust from the startup cache.  Re-evaluate it on every read so a
    # warning captured before the current Manager adopted the overlay cannot
    # survive after the owner is known to be alive.
    overlay = _runtime_overlay_health()
    live_overlay_check = _check(
        "runtime_overlay",
        "临时配置恢复记录",
        overlay["status"],
        overlay["detail"],
        autoFixable=overlay["status"] == "warning" and overlay.get("recoverable", False),
        action="restore_runtime_overlay",
        data=overlay,
    )
    checks = []
    overlay_seen = False
    config_status = None
    for raw in cached.get("checks", []):
        if not isinstance(raw, dict):
            continue
        if raw.get("id") == "runtime_overlay":
            item = dict(live_overlay_check)
            overlay_seen = True
        elif raw.get("id") == "generated_configuration":
            item, config_status = _live_configuration_check()
        else:
            item = dict(raw)
        status = str(item.get("status") or "warning")
        item["autoFixable"] = bool(
            item.get("id") in repairable_ids
            and status in {"warning", "error"}
            and item.get("action")
            and isinstance(item.get("data"), dict)
            and item["data"].get("recoverable", True)
        )
        checks.append(item)
    if not overlay_seen:
        checks.append(live_overlay_check)
    counts = {"ok": 0, "warning": 0, "error": 0}
    for item in checks:
        status = str(item.get("status") or "warning")
        counts[status] = counts.get(status, 0) + 1
    repairable_count = sum(1 for item in checks if item.get("autoFixable"))
    checked_at = _parse_time(cached.get("checkedAt"))
    stale = checked_at is None or (_now() - checked_at).total_seconds() > DIAGNOSTICS_CACHE_STALE_SECONDS
    normalized = {
        **cached,
        "schemaVersion": 2,
        "checks": checks,
        "summary": counts,
        "healthy": counts["error"] == 0 and counts["warning"] == 0,
        "repairableCount": repairable_count,
        "enableRepair": repairable_count > 0,
        "cached": True,
        "needsManualCheck": stale,
        "stale": stale,
    }
    normalized["configurationStatus"] = merge_configuration_diagnostics(
        config_status,
        normalized,
    )
    return normalized


def _live_configuration_check() -> tuple[dict, dict | None]:
    try:
        status = core.configuration_status()
        healthy = bool(status.get("fullyApplied"))
        return _check(
            "generated_configuration", "生成配置一致性", "ok" if healthy else "warning",
            "模型与子代理配置已同步。" if healthy else "生成配置需要重新同步。",
            autoFixable=not healthy, action="reapply_configuration", data=status,
            checkedAt=_iso(),
        ), status
    except Exception as exc:
        return _check(
            "generated_configuration", "生成配置一致性", "error",
            f"无法验证生成配置：{core._redact_sensitive_text(exc, limit=260)}",
            autoFixable=False, action="manual_configuration_repair", checkedAt=_iso(),
        ), None


def merge_configuration_diagnostics(configuration_status: dict | None, diagnostics: dict | None) -> dict:
    status = dict(configuration_status or {})
    diagnostics = diagnostics if isinstance(diagnostics, dict) else _empty_diagnostics()
    return {
        **status,
        "diagnosticsCheckedAt": diagnostics.get("checkedAt"),
        "diagnosticsHealthy": diagnostics.get("healthy"),
        "diagnosticSummary": diagnostics.get("summary", {"ok": 0, "warning": 0, "error": 0}),
        "repairableCount": int(diagnostics.get("repairableCount") or 0),
        "enableRepair": bool(diagnostics.get("enableRepair")),
        "diagnosticsCached": bool(diagnostics.get("cached")),
        "diagnosticsStale": bool(diagnostics.get("stale")),
        "diagnosticsNeedsManualCheck": bool(diagnostics.get("needsManualCheck")),
    }


def run_emergency_checks(force: bool = False) -> dict:
    if not force:
        cached = _safe_json(DIAGNOSTICS_CACHE_FILE, {})
        if isinstance(cached, dict) and isinstance(cached.get("checks"), list):
            return _normalize_cached_diagnostics(cached)
        return _empty_diagnostics()

    checks: list[dict] = []
    settings: dict | None = None
    settings_error: Exception | None = None
    try:
        settings = core.load_settings()
    except Exception as exc:
        settings_error = exc
    try:
        if core.CONFIG_FILE.is_file():
            core.read_toml(core.CONFIG_FILE)
        checks.append(_check("config_toml", "Codex 配置文件", "ok", "config.toml 可以正常解析。", autoFixable=False))
    except Exception as exc:
        checks.append(_check("config_toml", "Codex 配置文件", "error", f"config.toml 无法解析：{str(exc)[:260]}", autoFixable=False))

    try:
        runtime = core.codex_runtime_status(force=True)
    except Exception as exc:
        runtime = {"available": False, "source": "unknown", "error": str(exc)[:260]}
    if runtime.get("available"):
        checks.append(
            _check(
                "codex_runtime",
                "Codex 运行时",
                "ok",
                f"已识别 {runtime.get('source')} 运行时。",
                autoFixable=False,
                data=runtime,
            )
        )
    else:
        checks.append(
            _check(
                "codex_runtime",
                "Codex 运行时",
                "error",
                str(runtime.get("error") or "没有找到可用运行时。"),
                autoFixable=False,
                action="manual_runtime_repair",
                data=runtime,
            )
        )

    unsafe = runtime.get("unsafeCliOverride") if isinstance(runtime.get("unsafeCliOverride"), dict) else {}
    if unsafe.get("detected"):
        checks.append(
            _check(
                "unsafe_cli_override",
                "CODEX_CLI_PATH 安全检查",
                "error",
                "检测到不安全或失效的 CODEX_CLI_PATH；按安全策略仅报告，不自动修改。",
                autoFixable=False,
                action="manual_environment_repair",
            )
        )
    else:
        checks.append(_check("unsafe_cli_override", "CODEX_CLI_PATH 安全检查", "ok", "没有检测到 .cmd/.bat/.ps1 或失效路径覆盖。", autoFixable=False))

    config_status: dict | None = None
    try:
        config_status = core.configuration_status()
        status = "ok" if config_status.get("fullyApplied") else "warning"
        checks.append(_check("generated_configuration", "生成配置一致性", status, "模型与子代理配置已完成结构校验。" if status == "ok" else "生成配置需要重新同步。", autoFixable=status != "ok", action="reapply_configuration", data=config_status))
    except Exception as exc:
        checks.append(
            _check(
                "generated_configuration",
                "生成配置一致性",
                "error",
                f"无法验证生成配置：{str(exc)[:260]}",
                autoFixable=False,
                action="manual_configuration_repair",
            )
        )

    try:
        model_health = core.runtime_model_health(settings)
        checks.append(
            _check(
                "runtime_model",
                "Codex 当前模型",
                model_health["status"],
                model_health["detail"],
                autoFixable=bool(model_health.get("recoverable")),
                action="repair_runtime_model",
                data=model_health,
            )
        )
    except Exception as exc:
        checks.append(
            _check(
                "runtime_model",
                "Codex 当前模型",
                "warning",
                f"无法验证当前模型：{str(exc)[:260]}",
                autoFixable=False,
            )
        )

    if settings_error is not None:
        checks.append(
            _check(
                "settings_references",
                "账号、模型与号池引用",
                "error",
                f"无法读取或迁移设置：{str(settings_error)[:260]}",
                autoFixable=False,
                action="manual_settings_restore",
            )
        )
    else:
        try:
            references = core.settings_reference_integrity(settings)
            status = "ok" if references["healthy"] else "error"
            detail = (
                "账号、Provider、主模型、号池与子代理引用均有效。"
                if status == "ok"
                else f"发现 {references['issueCount']} 个悬空或异常引用。"
            )
            checks.append(
                _check(
                    "settings_references",
                    "账号、模型与号池引用",
                    status,
                    detail,
                    autoFixable=bool(references["repairableCount"] and not references["nonRepairableCount"]),
                    action="repair_settings_references",
                    data={**references, "recoverable": not references["nonRepairableCount"]},
                )
            )
        except Exception as exc:
            checks.append(
                _check(
                    "settings_references",
                    "账号、模型与号池引用",
                    "error",
                    f"无法检查设置引用：{str(exc)[:260]}",
                    autoFixable=False,
                    action="manual_settings_restore",
                )
            )

    overlay = _runtime_overlay_health()
    checks.append(
        _check(
            "runtime_overlay",
            "临时配置恢复记录",
            overlay["status"],
            overlay["detail"],
            autoFixable=overlay["status"] == "warning" and overlay.get("recoverable", False),
            action="restore_runtime_overlay",
            data=overlay,
        )
    )

    try:
        sessions = core.session_storage_health()
        session_detail = (
            f"会话索引正常：{sessions['threadCount']} 条索引、已抽查 {sessions['rolloutCount']} 个 JSONL。"
            if sessions["status"] == "ok"
            else "；".join(str(item.get("detail") or "") for item in sessions["issues"][:3])
        )
        checks.append(
            _check(
                "session_storage",
                "Codex 会话存储",
                sessions["status"],
                session_detail or "会话存储需要人工检查。",
                autoFixable=False,
                action="manual_session_restore",
                data=sessions,
            )
        )
    except Exception as exc:
        checks.append(
            _check(
                "session_storage",
                "Codex 会话存储",
                "error",
                f"无法只读检查会话存储：{str(exc)[:260]}",
                autoFixable=False,
            )
        )

    try:
        stale_subagents = core.stale_subagent_health(settings)
        checks.append(
            _check(
                "stale_subagents",
                "卡死子代理与孤儿任务",
                stale_subagents["status"],
                stale_subagents["detail"],
                autoFixable=bool(stale_subagents.get("recoverable")),
                action="archive_stale_subagents",
                data=stale_subagents,
            )
        )
    except Exception as exc:
        checks.append(
            _check(
                "stale_subagents",
                "卡死子代理与孤儿任务",
                "warning",
                f"无法安全检查子代理终态：{str(exc)[:260]}",
                autoFixable=False,
                action="manual_subagent_check",
            )
        )

    try:
        import agent_manager.integrations.claude as claude

        transaction = claude.transaction_health()
        registry = claude.configuration_health()
        transaction_status = str(transaction.get("status") or "warning")
        registry_status = str(registry.get("status") or "warning")
        status = "error" if "error" in {transaction_status, registry_status} else "warning" if "warning" in {transaction_status, registry_status} else "ok"
        detail_parts = [str(transaction.get("detail") or "")]
        detail_parts.extend(str(item) for item in registry.get("issues", [])[:2])
        checks.append(
            _check(
                "claude_transaction",
                "Claude 配置事务",
                status,
                "；".join(item for item in detail_parts if item) or "Claude 配置状态正常。",
                autoFixable=transaction.get("recoverable", False) and registry.get("healthy", False),
                action="recover_claude_transaction",
                data={"recoverable": bool(transaction.get("recoverable", False)), "transaction": transaction, "registry": registry},
            )
        )
    except Exception as exc:
        checks.append(
            _check(
                "claude_transaction",
                "Claude 配置事务",
                "error",
                f"无法检查 Claude 配置事务：{str(exc)[:260]}",
                autoFixable=False,
            )
        )

    try:
        import agent_manager.integrations.toolbox as toolbox

        toolbox_health = toolbox.storage_health()
        checks.append(
            _check(
                "toolbox_storage",
                "工具箱加密存储",
                toolbox_health["status"],
                "TOTP 与邮箱加密存储结构正常。" if toolbox_health["healthy"] else "；".join(item["detail"] for item in toolbox_health["issues"][:3]),
                autoFixable=False,
                action="manual_toolbox_restore",
                data=toolbox_health,
            )
        )
    except Exception as exc:
        checks.append(
            _check(
                "toolbox_storage",
                "工具箱加密存储",
                "error",
                f"无法检查工具箱存储：{str(exc)[:260]}",
                autoFixable=False,
            )
        )

    try:
        import agent_manager.integrations.radar as radar

        radar_health = radar.RadarCache().health()
        checks.append(
            _check(
                "radar_cache",
                "Codex 雷达缓存",
                radar_health["status"],
                radar_health["detail"],
                autoFixable=not radar_health["healthy"],
                action="clear_corrupt_radar_cache",
                data={**radar_health, "recoverable": not radar_health["healthy"]},
            )
        )
    except Exception as exc:
        checks.append(
            _check(
                "radar_cache",
                "Codex 雷达缓存",
                "error",
                f"无法检查雷达缓存：{str(exc)[:260]}",
                autoFixable=False,
            )
        )

    auth_path = core.CODEX_HOME / "auth.json"
    if not auth_path.exists():
        checks.append(_check("auth_shape", "Codex 登录缓存", "warning", "当前没有 auth.json；切换账号后会自动写入。", autoFixable=False))
    else:
        try:
            readable = auth_path.is_file() and auth_path.stat().st_size > 0
            checks.append(
                _check(
                    "auth_shape",
                    "Codex 登录缓存",
                    "ok" if readable else "warning",
                    "检测到登录缓存；按安全策略未读取或返回凭据内容。"
                    if readable
                    else "登录缓存为空或不可用。",
                    autoFixable=False,
                )
            )
        except OSError as exc:
            checks.append(_check("auth_shape", "Codex 登录缓存", "error", f"无法检查 auth.json 文件状态：{str(exc)[:240]}", autoFixable=False))

    # A status refresh must remain a quick local health check.  Asking the
    # Codex App Server to force-reload every skill can consume its full request
    # timeout even when the cached/filesystem inventory is already sufficient
    # to detect malformed SKILL.md files.  Skill management has its own manual
    # refresh path for authoritative App Server discovery.
    skills = list_skills(force=False)
    if skills.get("needsManualRefresh"):
        skill_cwd = _canonical(Path(os.getcwd()))
        fallback_skills, fallback_errors = _fallback_skills(skill_cwd)
        skills = {
            **skills,
            "skills": fallback_skills,
            "count": len(fallback_skills),
            "enabled": sum(1 for item in fallback_skills if item.get("enabled")),
            "errors": fallback_errors,
            "source": "filesystem_fallback",
        }
    skill_errors = sum(1 for item in skills["skills"] if item.get("errors"))
    checks.append(_check("skills", "Codex 技能", "warning" if skill_errors else "ok", f"发现 {skills['count']} 个技能，{skill_errors} 个存在结构问题。", autoFixable=False, data={"count": skills["count"], "errors": skill_errors}))

    writable = core.STATE_DIR.exists() and os.access(core.STATE_DIR, os.W_OK)
    checks.append(_check("state_directory", "Agent Manager 数据目录", "ok" if writable else "error", "数据目录可写。" if writable else "数据目录不可写，设置和备份可能无法保存。", autoFixable=False, path=str(core.STATE_DIR)))
    disk = _disk_health(core.STATE_DIR)
    checks.append(
        _check(
            "disk_space",
            "备份与更新磁盘空间",
            disk["status"],
            disk["detail"],
            autoFixable=False,
            data=disk,
        )
    )

    counts = {"ok": 0, "warning": 0, "error": 0}
    for item in checks:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    repairable_count = sum(1 for item in checks if item.get("autoFixable"))
    result = {
        "schemaVersion": 2,
        "checkedAt": _iso(),
        "checks": checks,
        "summary": counts,
        "healthy": counts["error"] == 0 and counts["warning"] == 0,
        "repairableCount": repairable_count,
        "enableRepair": repairable_count > 0,
        "configurationStatus": merge_configuration_diagnostics(config_status, {
            "checkedAt": _iso(),
            "healthy": counts["error"] == 0 and counts["warning"] == 0,
            "summary": counts,
            "repairableCount": repairable_count,
            "enableRepair": repairable_count > 0,
            "cached": False,
            "needsManualCheck": False,
        }),
        "cached": False,
        "needsManualCheck": False,
    }
    try:
        core.atomic_write_json(DIAGNOSTICS_CACHE_FILE, result)
    except Exception:
        result["cacheWarning"] = "诊断已完成，但本地缓存写入失败。"
    return result


def _generated_configuration_files() -> dict[Path, str]:
    settings = core.load_settings()
    workspace = settings.get("modelWorkspace", core._default_model_workspace())
    proxy_active = bool(settings.get("web2api", {}).get("activeForCodex"))
    records = core.web2api_pool_model_records(settings) if proxy_active else core.selected_model_records(settings)
    files = {
        core.CONFIG_FILE: core.build_codex_config(settings),
        core.AGENTS_FILE: core.build_agents_file(settings),
    }
    for spec in core._managed_subagent_specs(settings):
        files[Path(spec["path"])] = core._render_managed_agent(spec)
    if workspace.get("syncToCodex", True) and records:
        catalog, _ = core.build_synced_model_catalog(settings)
        files[core.MODEL_CATALOG_FILE] = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
    for path, content in files.items():
        if path.suffix.casefold() == ".toml":
            tomllib.loads(content)
    return files


def _capture_repair_backup(files: dict[Path, object]) -> list[dict]:
    snapshots = []
    for path in files:
        if _is_reparse_path(path):
            raise core.ManagerError(f"修复目标是符号链接或目录联接，已停止：{path}")
        existed = path.is_file()
        content = path.read_bytes() if existed else None
        backup = core.backup_file(path) if existed else None
        snapshots.append(
            {
                "path": path,
                "existed": existed,
                "content": content,
                "backupPath": str(backup) if backup else None,
            }
        )
    return snapshots


def _restore_repair_backup(snapshots: list[dict]) -> list[str]:
    errors = []
    for snapshot in snapshots:
        try:
            path = Path(snapshot["path"])
        except Exception as exc:
            errors.append(f"未知修复文件：{str(exc)[:300]}")
            continue
        try:
            if snapshot.get("existed"):
                core.atomic_write_bytes(path, snapshot.get("content") or b"")
            else:
                path.unlink(missing_ok=True)
            actual = path.read_bytes() if path.is_file() else None
            expected = snapshot.get("content") if snapshot.get("existed") else None
            if actual != expected:
                raise core.ManagerError("回滚后的文件内容校验失败。")
        except Exception as exc:
            errors.append(f"{path}：{str(exc)[:300]}")
    return errors


def _capture_repair_environment(enabled: bool) -> dict[str, str | None]:
    if not enabled:
        return {}
    overlay = core._runtime_overlay_read()
    names = list((overlay or {}).get("environment", {}))
    if len(names) > 128 or any(
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name)
        for name in names
    ):
        raise core.ManagerError("运行时覆盖包含无效环境变量名，未执行自动修复。")
    return {name: core._read_user_environment(name) for name in names}


def _restore_repair_environment(snapshot: dict[str, str | None]) -> list[str]:
    errors = []
    for name, value in snapshot.items():
        try:
            if value is None:
                core._remove_user_environment(name)
            else:
                core._sync_user_environment(name, value)
            if core._read_user_environment(name) != value:
                raise core.ManagerError("环境变量回滚校验失败。")
        except Exception as exc:
            errors.append(f"环境变量 {name}：{str(exc)[:300]}")
    return errors


def _repair_generated_configuration(files: dict[Path, str] | None = None) -> dict:
    files = files or _generated_configuration_files()
    changed_paths = []
    for path, content in files.items():
        before = path.read_text(encoding="utf-8") if path.is_file() else ""
        if before == content:
            continue
        core.atomic_write_text(path, content)
        changed_paths.append(path)
    return {"changed": bool(changed_paths), "files": [str(path) for path in changed_paths]}


def apply_emergency_repairs(check_ids: list[str]) -> dict:
    if not isinstance(check_ids, list) or len(check_ids) > 32 or any(
        not isinstance(item, str) for item in check_ids
    ):
        raise core.ManagerError("紧急修复检查项必须是至多 32 个字符串组成的数组。")
    if not _REPAIR_LOCK.acquire(blocking=False):
        raise core.ManagerError("紧急修复正在执行，请等待当前事务完成。")
    try:
        return _apply_emergency_repairs_locked(check_ids)
    finally:
        _REPAIR_LOCK.release()


def _apply_emergency_repairs_locked(check_ids: list[str]) -> dict:
    requested = list(dict.fromkeys(str(item) for item in check_ids if str(item).strip()))
    before = run_emergency_checks(force=False)
    cached_checks = {
        str(item.get("id")): item
        for item in before.get("checks", [])
        if isinstance(item, dict)
    }
    eligible = [
        check_id
        for check_id in requested
        if cached_checks.get(check_id, {}).get("status") in {"warning", "error"}
        and cached_checks.get(check_id, {}).get("autoFixable")
    ]
    if not eligible:
        return {
            "before": before,
            "preflight": None,
            "results": [],
            "after": before,
            "rechecked": False,
            "noop": True,
            "rolledBack": False,
            "message": "当前已检查状态没有可自动修复的异常，未执行任何修改。",
        }

    preflight = run_emergency_checks(force=True)
    preflight_checks = {
        str(item.get("id")): item
        for item in preflight.get("checks", [])
        if isinstance(item, dict)
    }
    eligible = [
        check_id
        for check_id in eligible
        if preflight_checks.get(check_id, {}).get("status") in {"warning", "error"}
        and preflight_checks.get(check_id, {}).get("autoFixable")
    ]
    if not eligible:
        return {
            "before": before,
            "preflight": preflight,
            "results": [],
            "after": preflight,
            "rechecked": True,
            "noop": True,
            "rolledBack": False,
            "message": "预检确认异常已不存在，未执行任何修改。",
        }

    generated_files = _generated_configuration_files() if "generated_configuration" in eligible else {}
    files = dict(generated_files)
    if "settings_references" in eligible:
        files.setdefault(core.SETTINGS_FILE, "")
    if "runtime_model" in eligible:
        files.setdefault(core.SETTINGS_FILE, "")
        files.setdefault(core.CONFIG_FILE, "")
        files.setdefault(core.RUNTIME_OVERLAY_FILE, "")
    if "runtime_overlay" in eligible:
        files.setdefault(core.RUNTIME_OVERLAY_FILE, "")
        files.setdefault(core.RUNTIME_RESTORE_STATUS_FILE, "")
    if "claude_transaction" in eligible:
        import agent_manager.integrations.claude as claude

        files.setdefault(claude.TRANSACTION_FILE, "")
        for path in claude.configuration_paths().values():
            files.setdefault(Path(path), "")
        files.setdefault(claude.REGISTRY_FILE, "")
    if "radar_cache" in eligible:
        import agent_manager.integrations.radar as radar

        files.setdefault(radar._default_cache_path(), "")
    snapshots = _capture_repair_backup(files)
    environment_snapshot = _capture_repair_environment("runtime_overlay" in eligible)
    backup_paths = [item["backupPath"] for item in snapshots if item.get("backupPath")]
    results: list[dict] = []
    archived_subagents: list[str] = []
    try:
        for check_id in eligible:
            if check_id == "generated_configuration":
                data = _repair_generated_configuration(generated_files)
                results.append(
                    {
                        "id": check_id,
                        "status": "fixed" if data.get("changed") else "noop",
                        "detail": "已重新生成可验证的 Codex 配置文件。" if data.get("changed") else "配置文件无需修改。",
                        "data": data,
                    }
                )
            elif check_id == "settings_references":
                data = core.repair_settings_references()
                results.append(
                    {
                        "id": check_id,
                        "status": "fixed" if data.get("changed") else "noop",
                        "detail": (
                            f"已清理 {data.get('removed', 0)} 个悬空账号、Provider、号池或子代理引用。"
                            if data.get("changed")
                            else "设置引用无需修改。"
                        ),
                        "data": data,
                    }
                )
            elif check_id == "runtime_model":
                candidate_key = str(
                    preflight_checks.get(check_id, {}).get("data", {}).get("candidateKey") or ""
                )
                data = core.repair_runtime_model_selection(candidate_key)
                results.append(
                    {
                        "id": check_id,
                        "status": "fixed",
                        "detail": f"已切换到 Codex 当前目录确认可用的模型 {data.get('model') or ''}。",
                        "data": data,
                    }
                )
            elif check_id == "runtime_overlay":
                data = core.restore_runtime_configuration_overlay(force=False)
                if not data.get("restored"):
                    remaining = [
                        *data.get("remainingFiles", []),
                        *data.get("remainingEnvironment", []),
                        *data.get("conflicts", []),
                    ]
                    raise core.ManagerError(
                        "临时配置恢复未完成：" + "、".join(str(item) for item in remaining[:5])
                    )
                results.append(
                    {
                        "id": check_id,
                        "status": "fixed",
                        "detail": "已按加密恢复记录还原临时 Codex 配置。",
                        "data": data,
                    }
                )
            elif check_id == "claude_transaction":
                import agent_manager.integrations.claude as claude

                data = claude.recover_incomplete_transaction()
                if data.get("errors"):
                    raise core.ManagerError("Claude 配置事务恢复失败：" + "；".join(data["errors"]))
                results.append(
                    {
                        "id": check_id,
                        "status": "fixed" if data.get("recovered") else "noop",
                        "detail": "已从加密快照恢复 Claude Desktop 配置。" if data.get("recovered") else "没有待恢复的 Claude 事务。",
                        "data": data,
                    }
                )
            elif check_id == "radar_cache":
                import agent_manager.integrations.radar as radar

                data = radar.RadarCache().clear_corrupt()
                results.append(
                    {
                        "id": check_id,
                        "status": "fixed" if data.get("changed") else "noop",
                        "detail": "已清理损坏的雷达缓存；下次仅在启动或手动刷新时重新获取。" if data.get("changed") else "雷达缓存无需修改。",
                        "data": data,
                    }
                )
            elif check_id == "stale_subagents":
                candidate_ids = [
                    str(item.get("threadId") or "")
                    for item in preflight_checks.get(check_id, {}).get("data", {}).get("candidates", [])
                    if isinstance(item, dict)
                ]
                data = core.cleanup_stale_subagents(candidate_ids)
                archived_subagents = [str(item) for item in data.get("archived", []) if str(item)]
                results.append(
                    {
                        "id": check_id,
                        "status": "fixed" if data.get("changed") else "noop",
                        "detail": (
                            f"已归档 {data.get('count', 0)} 个卡死或失去终态的子代理任务；未删除会话，可在会话管理中恢复。"
                            if data.get("changed")
                            else "没有仍需清理的子代理任务。"
                        ),
                        "data": data,
                    }
                )
            else:
                raise core.ManagerError(f"检查项 {check_id} 没有可回滚的自动修复实现。")
        after = run_emergency_checks(force=True)
        after_checks = {
            str(item.get("id")): item
            for item in after.get("checks", [])
            if isinstance(item, dict)
        }
        unresolved = [
            check_id
            for check_id in eligible
            if after_checks.get(check_id, {}).get("status") in {"warning", "error"}
        ]
        if unresolved:
            raise core.ManagerError("复检仍发现可修复异常：" + "、".join(unresolved))
        repaired_paths = [
            Path(path)
            for result in results
            if result.get("id") == "generated_configuration"
            for path in (result.get("data", {}).get("files", []) if isinstance(result.get("data"), dict) else [])
        ]
        if repaired_paths:
            core._runtime_overlay_record_applied(paths=repaired_paths)
        return {
            "before": before,
            "preflight": preflight,
            "results": results,
            "after": after,
            "rechecked": True,
            "noop": all(item.get("status") == "noop" for item in results),
            "rolledBack": False,
            "backups": backup_paths,
        }
    except Exception as exc:
        rollback_errors = _restore_repair_backup(snapshots)
        rollback_errors.extend(_restore_repair_environment(environment_snapshot))
        for offset in range(len(archived_subagents), 0, -100):
            batch = archived_subagents[max(0, offset - 100) : offset]
            try:
                core.manage_codex_threads("restore", batch)
            except Exception as restore_exc:
                rollback_errors.append(
                    f"恢复 {len(batch)} 个子代理任务：{str(restore_exc)[:300]}"
                )
        try:
            rolled_back = run_emergency_checks(force=True)
            rechecked = True
        except Exception as recheck_exc:
            rolled_back = preflight
            rechecked = False
            rollback_errors.append(f"回滚后复检：{str(recheck_exc)[:300]}")
        rollback_complete = not rollback_errors
        results.append(
            {
                "id": "transaction",
                "status": "rolled_back" if rollback_complete else "rollback_failed",
                "detail": str(exc)[:400],
            }
        )
        return {
            "before": before,
            "preflight": preflight,
            "results": results,
            "after": rolled_back,
            "rechecked": rechecked,
            "noop": False,
            "rolledBack": rollback_complete,
            "rollbackAttempted": True,
            "rollbackErrors": rollback_errors,
            "backups": backup_paths,
            "error": str(exc)[:400],
        }
