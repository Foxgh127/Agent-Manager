"""History services."""
from __future__ import annotations
from agent_manager import core as _core


def _history_file_map(root: _core.Path, recent_days: int, remote: bool = False) -> dict[str, _core.Path]:
    base = root if remote else _core.CODEX_HOME
    cutoff = _core.time.time() - recent_days * 86_400 if recent_days > 0 else None
    result: dict[str, _core.Path] = {}
    for bucket in ("sessions", "archived_sessions"):
        folder = base / bucket
        if not folder.is_dir() or folder.is_symlink() or (hasattr(folder, "is_junction") and folder.is_junction()):
            continue
        for current, directories, files in _core.os.walk(folder, followlinks=False):
            directories[:] = [name for name in directories if not (_core.Path(current) / name).is_symlink()
                               and not (hasattr(_core.Path(current) / name, "is_junction") and (_core.Path(current) / name).is_junction())]
            for name in files:
                if not name.lower().endswith(".jsonl"):
                    continue
                path = _core.Path(current) / name
                if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(base.resolve()):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if cutoff is not None and stat.st_mtime < cutoff:
                    continue
                relative = path.relative_to(base).as_posix()
                result[relative] = path
    return result



def history_inventory() -> dict:
    files = _core._history_file_map(_core.CODEX_HOME, 0, remote=True)
    total_bytes = 0
    newest = None
    active = 0
    archived = 0
    for relative, path in files.items():
        try:
            stat = path.stat()
        except OSError:
            continue
        total_bytes += stat.st_size
        newest = max(newest or 0, stat.st_mtime)
        if relative.startswith("archived_sessions/"):
            archived += 1
        else:
            active += 1
    state_threads = None
    db_path = _core.CODEX_HOME / "state_5.sqlite"
    if db_path.is_file():
        try:
            connection = _core.sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=1)
            try:
                state_threads = int(connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0])
            finally:
                connection.close()
        except (_core.sqlite3.Error, TypeError, ValueError):
            state_threads = None
    return {
        "activeFiles": active,
        "archivedFiles": archived,
        "totalFiles": active + archived,
        "totalBytes": total_bytes,
        "newestAt": _core._timestamp_iso(newest),
        "stateThreads": state_threads,
    }



def recommended_history_targets() -> list[dict]:
    candidates: list[tuple[str, _core.Path]] = []
    for env_name, label in (
        ("OneDriveCommercial", "OneDrive 工作账户"),
        ("OneDriveConsumer", "OneDrive 个人账户"),
        ("OneDrive", "OneDrive"),
        ("Dropbox", "Dropbox"),
    ):
        value = _core.os.environ.get(env_name)
        if value:
            candidates.append((label, _core.Path(value) / "Codex History"))
    candidates.extend(
        [
            ("OneDrive", _core.Path.home() / "OneDrive" / "Codex History"),
            ("本地文档", _core.Path.home() / "Documents" / "Codex History"),
        ]
    )
    seen = set()
    result = []
    for label, path in candidates:
        normalized = str(path.expanduser().resolve(strict=False))
        if normalized.casefold() in seen:
            continue
        seen.add(normalized.casefold())
        result.append({"label": label, "path": normalized, "available": path.parent.exists()})
    return result



def _history_sync_root(target: str, create: bool = False) -> _core.Path:
    raw = str(target or "").strip()
    if not raw:
        raise _core.ManagerError("请先选择历史同步文件夹。")
    path = _core.Path(raw).expanduser().resolve(strict=False)
    if path == _core.CODEX_HOME or _core.CODEX_HOME in path.parents:
        raise _core.ManagerError("历史同步目录不能位于 CODEX_HOME 内部。")
    root = path if path.name == _core.HISTORY_SYNC_FOLDER else path / _core.HISTORY_SYNC_FOLDER
    codex_root = _core.CODEX_HOME.resolve()
    if root == codex_root or codex_root in root.parents or root in codex_root.parents:
        raise _core.ManagerError("历史同步目录与 Codex 数据目录冲突。")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root



def _hash_file(path: _core.Path) -> str:
    digest = _core.hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()



def _hash_file_prefix(path: _core.Path, size: int) -> str:
    digest = _core.hashlib.sha256()
    remaining = size
    with path.open("rb") as stream:
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise _core.ManagerError("会话文件在读取期间变短，请重新预览。")
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()



def _files_equal(left: _core.Path, right: _core.Path) -> bool:
    left_stat = left.stat()
    right_stat = right.stat()
    if left_stat.st_size != right_stat.st_size:
        return False
    # Cloud clients and restored archives can preserve size and mtime while
    # the contents differ. Equality must never hide a divergent conversation.
    return _core._hash_file(left) == _core._hash_file(right)



def _is_file_prefix(shorter: _core.Path, longer: _core.Path) -> bool:
    if shorter.stat().st_size >= longer.stat().st_size:
        return False
    with shorter.open("rb") as left, longer.open("rb") as right:
        while chunk := left.read(1024 * 1024):
            if right.read(len(chunk)) != chunk:
                return False
    return True



def _history_plan(target: str, direction: str, recent_days: int) -> tuple[_core.Path, list[dict]]:
    if direction not in {"two_way", "push", "pull"}:
        raise _core.ManagerError("历史同步方向无效。")
    if recent_days < 0 or recent_days > 3650:
        raise _core.ManagerError("历史同步天数必须在 0 到 3650 之间。")
    remote_root = _core._history_sync_root(target, create=False)
    local = _core._history_file_map(_core.CODEX_HOME, recent_days, remote=True)
    remote = _core._history_file_map(remote_root, recent_days, remote=True) if remote_root.is_dir() else {}
    operations: list[dict] = []

    def add(action: str, relative: str, source: Path | None, destination: Path | None, reason: str) -> None:
        size = source.stat().st_size if source and source.is_file() else 0
        operations.append(
            {
                "action": action,
                "relative": relative,
                "source": source,
                "destination": destination,
                "bytes": size,
                "reason": reason,
                "sourceHash": _core._hash_file_prefix(source, size) if source else None,
                "sourceMtimeNs": source.stat().st_mtime_ns if source else None,
                "destinationHash": _core._hash_file(destination) if destination and destination.is_file() else None,
            }
        )

    for relative in sorted(set(local) | set(remote)):
        local_path = local.get(relative)
        remote_path = remote.get(relative)
        if local_path and not remote_path:
            if direction in {"two_way", "push"}:
                add("copy_to_remote", relative, local_path, remote_root / relative, "远端缺少")
            continue
        if remote_path and not local_path:
            if direction in {"two_way", "pull"}:
                add("copy_to_local", relative, remote_path, _core.CODEX_HOME / relative, "本机缺少")
            continue
        if not local_path or not remote_path or _core._files_equal(local_path, remote_path):
            continue
        if _core._is_file_prefix(remote_path, local_path):
            if direction in {"two_way", "push"}:
                add("replace_remote", relative, local_path, remote_path, "本机记录是远端的完整续写")
            elif direction == "pull":
                add("conflict", relative, None, None, "本机记录更新，拉取不会覆盖")
            continue
        if _core._is_file_prefix(local_path, remote_path):
            if direction in {"two_way", "pull"}:
                add("replace_local", relative, remote_path, local_path, "远端记录是本机的完整续写")
            elif direction == "push":
                add("conflict", relative, None, None, "远端记录更新，推送不会覆盖")
            continue
        add("conflict", relative, None, None, "两端内容分叉，已保留原文件")
    return remote_root, operations



def preview_history_sync(payload: dict) -> dict:
    import agent_manager.sessions.history
    return agent_manager.sessions.history.preview(payload)



def _atomic_copy_file(source: _core.Path, destination: _core.Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = _core.tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".sync", dir=destination.parent)
    temp_path = _core.Path(temp_name)
    try:
        with _core.os.fdopen(handle, "wb") as target_stream, source.open("rb") as source_stream:
            _core.shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
            target_stream.flush()
            _core.os.fsync(target_stream.fileno())
        _core.shutil.copystat(source, temp_path)
        _core.os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)



def perform_history_sync(payload: dict) -> dict:
    import agent_manager.sessions.history
    return agent_manager.sessions.history.perform(payload)



def save_history_sync_settings(payload: dict) -> dict:
    target = str(payload.get("target") or "").strip()
    recent_days = int(payload.get("recentDays", 30))
    direction = str(payload.get("direction") or "two_way")
    if recent_days < 0 or recent_days > 3650:
        raise _core.ManagerError("历史同步天数必须在 0 到 3650 之间。")
    if direction not in {"two_way", "push", "pull"}:
        raise _core.ManagerError("历史同步方向无效。")
    if target:
        _core._history_sync_root(target, create=False)
    settings = _core.load_settings()
    current = settings.get("historySync", {})
    settings["historySync"] = {
        "target": target,
        "recentDays": recent_days,
        "direction": direction,
        "lastSyncedAt": current.get("lastSyncedAt"),
        "lastSummary": current.get("lastSummary"),
    }
    _core.save_settings(settings)
    return settings["historySync"]



def history_state(include_threads: bool = True) -> dict:
    settings = _core.load_settings()
    threads = []
    thread_error = None
    if include_threads:
        try:
            threads = _core.list_codex_thread_groups(active_limit=500, archived_limit=500)
        except _core.ManagerError as exc:
            thread_error = _core._redact_sensitive_text(exc, limit=320)
    return {
        "inventory": _core.history_inventory(),
        "sync": settings.get("historySync", {}),
        "recommendedTargets": _core.recommended_history_targets(),
        "threads": threads,
        "threadError": thread_error,
    }



def export_bundle() -> dict:
    settings = _core.load_settings()
    exported_settings = _core.json.loads(_core.json.dumps(settings))
    exported_settings["lastAppliedAt"] = None
    exported_settings["lastAppliedSummary"] = None
    # Credentials are machine/user bound and sync destinations are local policy.
    exported_settings["accounts"] = []
    exported_settings["web2api"] = _core._default_web2api_settings()
    exported_settings["sessionSync"] = _core._default_session_sync_settings()
    exported_settings["historySync"] = {
        "target": "",
        "recentDays": int(settings.get("historySync", {}).get("recentDays", 30)),
        "direction": str(settings.get("historySync", {}).get("direction") or "two_way"),
        "lastSyncedAt": None,
        "lastSummary": None,
    }
    agents = []
    for record in _core.discover_agents():
        if not record.get("error"):
            agents.append({"fileName": record["path"].name, "content": record["path"].read_text(encoding="utf-8")})
    return {
        "format": "codex-agent-manager-bundle",
        "version": 1,
        "exportedAt": _core.now_iso(),
        "containsSecrets": False,
        "settings": exported_settings,
        "agents": agents,
    }



def import_bundle(bundle: dict, mode: str = "merge") -> dict:
    if not isinstance(bundle, dict) or bundle.get("format") != "codex-agent-manager-bundle" or bundle.get("version") != 1:
        raise _core.ManagerError("不是有效的 Agent Manager 配置包。")
    if bundle.get("containsSecrets"):
        raise _core.ManagerError("配置包声明包含密钥，出于安全原因已拒绝导入。请使用不含密钥的标准配置包。")
    incoming = bundle.get("settings")
    if not isinstance(incoming, dict):
        raise _core.ManagerError("配置包版本不兼容。")
    try:
        incoming, _ = _core._migrate_settings(_core._json_clone(incoming))
    except _core.ManagerError as exc:
        raise _core.ManagerError(f"配置包版本不兼容：{exc}") from exc
    if mode not in {"merge", "replace"}:
        raise _core.ManagerError("导入模式无效。")

    staged_agents: list[tuple[_core.Path, str]] = []
    seen_files: set[str] = set()
    total_agent_bytes = 0
    for item in bundle.get("agents", []):
        if not isinstance(item, dict):
            continue
        file_name = _core.Path(str(item.get("fileName", ""))).name
        content = str(item.get("content", ""))
        file_key = file_name.casefold()
        if not _core.re.fullmatch(r"[A-Za-z0-9_-]+\.toml", file_name):
            raise _core.ManagerError(f"配置包中的 Agent 文件名无效：{file_name}")
        if file_key in seen_files:
            raise _core.ManagerError(f"配置包中的 Agent 文件名重复：{file_name}")
        seen_files.add(file_key)
        encoded_size = len(content.encode("utf-8"))
        total_agent_bytes += encoded_size
        if encoded_size > 1_000_000 or total_agent_bytes > 10_000_000 or len(staged_agents) >= 200:
            raise _core.ManagerError("配置包中的 Agent 文件数量或大小超过安全限制。")
        try:
            parsed_agent = _core.tomllib.loads(content)
        except _core.tomllib.TOMLDecodeError as exc:
            raise _core.ManagerError(f"Agent 文件格式无效：{file_name}") from exc
        if not isinstance(parsed_agent, dict) or not parsed_agent.get("name"):
            raise _core.ManagerError(f"Agent 文件缺少 name：{file_name}")
        staged_agents.append((_core.AGENTS_DIR / file_name, content))

    current = _core.load_settings()
    merged_groups = _core.json.loads(_core.json.dumps(current.get("accountGroups", _core.DEFAULT_ACCOUNT_GROUPS)))
    for group in incoming.get("accountGroups", []):
        if isinstance(group, dict) and group.get("id"):
            _core._merge_by_id(merged_groups, group)
    if mode == "replace":
        next_settings = _core.json.loads(_core.json.dumps(incoming))
        # Never remove local credential references or sync policy through a portable bundle.
        next_settings["accounts"] = current.get("accounts", [])
        next_settings["historySync"] = current.get("historySync", {})
        next_settings["web2api"] = current.get("web2api", _core._default_web2api_settings())
        next_settings["sessionSync"] = current.get("sessionSync", _core._default_session_sync_settings())
        next_settings["accountGroups"] = merged_groups
    else:
        next_settings = _core.json.loads(_core.json.dumps(current))
        for key in ("providers", "mainProfiles", "strategies"):
            for item in incoming.get(key, []):
                if item.get("id") == "openai" and key == "providers":
                    continue
                _core._merge_by_id(next_settings[key], item)
        next_settings["routes"] = incoming.get("routes", next_settings["routes"])
        next_settings["activeMainProfileId"] = incoming.get("activeMainProfileId", next_settings["activeMainProfileId"])
        next_settings["activeStrategyId"] = incoming.get("activeStrategyId", next_settings["activeStrategyId"])
        next_settings["managedProviderIds"] = sorted(
            set(next_settings.get("managedProviderIds", [])) | set(incoming.get("managedProviderIds", []))
        )
        next_settings["accountGroups"] = merged_groups
    next_settings["lastAppliedAt"] = None
    next_settings["lastAppliedSummary"] = None
    # Validate generated configuration before touching any live file.
    _core.build_codex_config(next_settings)
    snapshots = {path: path.read_bytes() if path.is_file() else None for path, _ in staged_agents}
    written: list[str] = []
    with _core.SETTINGS_LOCK:
        try:
            for path, content in staged_agents:
                _core.backup_file(path)
                _core.atomic_write_text(path, content)
                written.append(str(path))
            _core.save_settings(next_settings)
        except Exception:
            rollback_errors = []
            for path, original in snapshots.items():
                try:
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        _core.atomic_write_bytes(path, original)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{path.name}: {rollback_exc}")
            if rollback_errors:
                raise _core.ManagerError("配置包导入失败，且部分 Agent 文件回滚失败：" + "；".join(rollback_errors))
            raise
    return {"agentsImported": len(written), "mode": mode, "migratedToSchema": _core.SCHEMA_VERSION}

