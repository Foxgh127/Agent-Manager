"""Files services."""
from __future__ import annotations
from agent_manager import core as _core


def now_iso() -> str:
    return _core.datetime.now(_core.timezone.utc).astimezone().isoformat(timespec="seconds")



def _json_clone(value: _core.Any) -> _core.Any:
    return _core.json.loads(_core.json.dumps(value, ensure_ascii=False))



@_core.contextmanager
def _settings_file_lock(timeout_seconds: float = 15.0):
    """Serialize settings transactions across manager/CLI processes."""

    depth = int(getattr(_core.SETTINGS_FILE_LOCK_STATE, "depth", 0))
    if depth:
        _core.SETTINGS_FILE_LOCK_STATE.depth = depth + 1
        try:
            yield
        finally:
            _core.SETTINGS_FILE_LOCK_STATE.depth = depth
        return

    _core.STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _core.SETTINGS_FILE.with_name("settings.lock")
    handle = open(lock_path, "a+b")
    acquired = False
    try:
        handle.seek(0, _core.os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        deadline = _core.time.monotonic() + max(0.1, float(timeout_seconds))
        if _core.os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if _core.time.monotonic() >= deadline:
                        raise _core.ManagerError("设置正由另一个 Agent Manager 实例更新，请稍后重试。")
                    _core.time.sleep(0.025)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if _core.time.monotonic() >= deadline:
                        raise _core.ManagerError("设置正由另一个 Agent Manager 实例更新，请稍后重试。")
                    _core.time.sleep(0.025)
        _core.SETTINGS_FILE_LOCK_STATE.depth = 1
        yield
    finally:
        _core.SETTINGS_FILE_LOCK_STATE.depth = 0
        if acquired:
            try:
                handle.seek(0)
                if _core.os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()



def slugify(value: str, label: str = "ID") -> str:
    slug = value.strip().lower().replace("-", "_")
    if not _core.re.fullmatch(r"[a-z][a-z0-9_]{0,63}", slug):
        raise _core.ManagerError(f"{label} 必须以字母开头，只能包含小写字母、数字和下划线。")
    return slug



def _validate_provider_env_key(value: str, *, allow_internal: bool = False) -> str:
    env_key = str(value or "").strip()
    if not _core.re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_key):
        raise _core.ManagerError("环境变量名格式无效。")
    if allow_internal and env_key.upper() == _core.AGGREGATE_ENV_KEY:
        return _core.AGGREGATE_ENV_KEY
    if env_key.upper() in _core.PROTECTED_RUNTIME_ENV_KEYS:
        raise _core.ManagerError(f"环境变量 `{env_key}` 属于系统或 Codex 保留项，不能用作 Provider API Key。")
    return env_key



def _validated_provider_secret(value: object, *, allow_empty: bool = False) -> str:
    if value is None or value == "":
        if allow_empty:
            return ""
        raise _core.ManagerError("API Key 不能为空。")
    if not isinstance(value, str):
        raise _core.ManagerError("API Key 必须是字符串。")
    secret = value.strip()
    if not secret and allow_empty:
        return ""
    if not secret:
        raise _core.ManagerError("API Key 不能为空。")
    if len(secret.encode("utf-8", errors="replace")) > 4_096 or any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in secret
    ):
        raise _core.ManagerError("API Key 格式无效：不能包含空白/控制字符，且长度不能超过 4096 字节。")
    return secret



def _safe_imported_provider_env_key(value: str, fallback: str) -> str:
    try:
        return _core._validate_provider_env_key(value)
    except _core.ManagerError:
        return _core._validate_provider_env_key(fallback)



def _unique_imported_provider_id(base_id: str, key: str, known_ids: set[str]) -> str:
    """Avoid overwriting distinct imported API keys that share one host id."""

    candidate = _core.slugify(base_id, "Provider ID")
    if candidate not in known_ids:
        return candidate
    digest = _core.hashlib.sha256(str(key or "").encode("utf-8")).hexdigest()[:8]
    marker = f"_{digest}"
    derived = f"{candidate[: max(1, 64 - len(marker))].rstrip('_')}{marker}"
    if derived not in known_ids:
        return derived
    for suffix in range(2, 1_000):
        marker = f"_{digest}_{suffix}"
        derived = f"{candidate[: max(1, 64 - len(marker))].rstrip('_')}{marker}"
        if derived not in known_ids:
            return derived
    raise _core.ManagerError("同一中转站导入的独立 API Key 过多，请分批导入并自定义名称。")



def _imported_provider_fingerprint(provider: dict) -> str:
    """Return a non-reversible identity for exact provider-document duplicates.

    A host is not an account identity: one relay can issue many independent
    keys.  Importing the exact same endpoint/key pair twice, however, should
    not manufacture another account.  The raw key never leaves this helper.
    """

    key = str(provider.get("key") or "").strip()
    base_url = str(provider.get("baseUrl") or "").strip().rstrip("/")
    if not key or not base_url:
        return ""
    try:
        parsed = _core.urllib.parse.urlsplit(base_url)
        normalized_base = _core.urllib.parse.urlunsplit(
            (
                parsed.scheme.casefold(),
                parsed.netloc.casefold(),
                parsed.path.rstrip("/"),
                parsed.query,
                "",
            )
        )
    except ValueError:
        normalized_base = base_url
    key_digest = _core.hashlib.sha256(key.encode("utf-8")).hexdigest()
    return _core.hashlib.sha256(f"{normalized_base}\0{key_digest}".encode("utf-8")).hexdigest()



@_core.contextmanager
def _atomic_write_path_lock(path: _core.Path):
    """Serialize same-process commits to one path without a global write lock."""

    key = _core.os.path.normcase(_core.os.path.abspath(_core.os.fspath(path)))
    with _core._ATOMIC_WRITE_LOCKS_GUARD:
        entry = _core._ATOMIC_WRITE_LOCKS.get(key)
        if entry is None:
            entry = {"lock": _core.threading.RLock(), "users": 0}
            _core._ATOMIC_WRITE_LOCKS[key] = entry
        entry["users"] += 1
    lock = entry["lock"]
    try:
        with lock:
            yield
    finally:
        with _core._ATOMIC_WRITE_LOCKS_GUARD:
            entry["users"] -= 1
            if entry["users"] == 0 and _core._ATOMIC_WRITE_LOCKS.get(key) is entry:
                _core._ATOMIC_WRITE_LOCKS.pop(key, None)



def _is_transient_atomic_write_error(exc: OSError) -> bool:
    """Return whether Windows may release the destination handle shortly."""

    if _core.os.name != "nt":
        return False
    if getattr(exc, "winerror", None) in {5, 32, 33}:
        return True
    # Mocks and some Python/CRT paths expose only errno for WinError 5.
    return isinstance(exc, PermissionError) and exc.errno in {_core.errno.EACCES, _core.errno.EPERM, _core.errno.EBUSY}



def _atomic_retry_delay(failure_index: int) -> float:
    return min(
        _core._ATOMIC_REPLACE_RETRY_BASE_SECONDS * (2**failure_index),
        _core._ATOMIC_REPLACE_RETRY_MAX_SECONDS,
    )



def _replace_atomic_temp(temp_path: _core.Path, path: _core.Path) -> None:
    for attempt in range(_core._ATOMIC_REPLACE_ATTEMPTS):
        try:
            _core.os.replace(temp_path, path)
            return
        except OSError as exc:
            if attempt + 1 >= _core._ATOMIC_REPLACE_ATTEMPTS or not _core._is_transient_atomic_write_error(exc):
                raise
            _core.time.sleep(_core._atomic_retry_delay(attempt))



def _cleanup_atomic_temp(temp_path: _core.Path) -> None:
    """Best-effort bounded cleanup which never hides the original write error."""

    for attempt in range(_core._ATOMIC_REPLACE_ATTEMPTS):
        try:
            temp_path.unlink(missing_ok=True)
            return
        except OSError as exc:
            if attempt + 1 >= _core._ATOMIC_REPLACE_ATTEMPTS or not _core._is_transient_atomic_write_error(exc):
                return
            _core.time.sleep(_core._atomic_retry_delay(attempt))



def atomic_write_text(path: _core.Path, content: str) -> None:
    path = _core.Path(path)
    with _core._atomic_write_path_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = _core.tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp_path = _core.Path(temp_name)
        try:
            with _core.os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                _core.os.fsync(stream.fileno())
            _core._replace_atomic_temp(temp_path, path)
        finally:
            _core._cleanup_atomic_temp(temp_path)



def atomic_write_bytes(path: _core.Path, content: bytes) -> None:
    path = _core.Path(path)
    with _core._atomic_write_path_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = _core.tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp_path = _core.Path(temp_name)
        try:
            with _core.os.fdopen(handle, "wb") as stream:
                stream.write(content)
                stream.flush()
                _core.os.fsync(stream.fileno())
            _core._replace_atomic_temp(temp_path, path)
        finally:
            _core._cleanup_atomic_temp(temp_path)



def atomic_write_json(path: _core.Path, payload: object) -> None:
    _core.atomic_write_text(path, _core.json.dumps(payload, ensure_ascii=False, indent=2) + "\n")



def _capture_file_bytes(paths: list[_core.Path] | tuple[_core.Path, ...]) -> dict[_core.Path, bytes | None]:
    try:
        return {path: path.read_bytes() if path.is_file() else None for path in paths}
    except OSError as exc:
        raise _core.ManagerError(f"无法完整读取事务前状态：{exc}") from exc



def _restore_file_bytes(snapshot: dict[_core.Path, bytes | None]) -> list[str]:
    errors = []
    for path, content in snapshot.items():
        try:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                _core.atomic_write_bytes(path, content)
        except OSError as exc:
            errors.append(f"{path.name}: {str(exc)[:180]}")
    for path, expected in snapshot.items():
        try:
            actual = path.read_bytes() if path.is_file() else None
        except OSError as exc:
            errors.append(f"回验 {path.name}: {str(exc)[:180]}")
            continue
        if actual != expected:
            errors.append(f"回验 {path.name}: 内容未恢复")
    return errors



class _WindowsGuid(_core.ctypes.Structure):
    _fields_ = [
        ("Data1", _core.wintypes.DWORD),
        ("Data2", _core.wintypes.WORD),
        ("Data3", _core.wintypes.WORD),
        ("Data4", _core.ctypes.c_ubyte * 8),
    ]



def _windows_guid(value: str) -> _core._WindowsGuid:
    parsed = _core.uuid.UUID(value)
    node = parsed.node.to_bytes(6, "big")
    return _core._WindowsGuid(
        parsed.time_low,
        parsed.time_mid,
        parsed.time_hi_version,
        (_core.ctypes.c_ubyte * 8)(parsed.clock_seq_hi_variant, parsed.clock_seq_low, *node),
    )



def _windows_downloads_directory() -> _core.Path | None:
    """Resolve the redirected Windows Downloads known folder without shelling out."""
    if _core.os.name != "nt":
        return None
    value = _core.ctypes.c_wchar_p()
    try:
        shell32 = _core.ctypes.windll.shell32
        folder_id = _core._windows_guid(_core.WINDOWS_DOWNLOADS_FOLDER_ID)
        shell32.SHGetKnownFolderPath.argtypes = [
            _core.ctypes.POINTER(_core._WindowsGuid),
            _core.wintypes.DWORD,
            _core.wintypes.HANDLE,
            _core.ctypes.POINTER(_core.ctypes.c_wchar_p),
        ]
        shell32.SHGetKnownFolderPath.restype = _core.ctypes.c_long
        result = shell32.SHGetKnownFolderPath(_core.ctypes.byref(folder_id), 0, None, _core.ctypes.byref(value))
        if result != 0 or not value.value:
            return None
        return _core.Path(value.value).expanduser().resolve()
    except (AttributeError, OSError, ValueError):
        return None
    finally:
        if value.value:
            try:
                _core.ctypes.windll.ole32.CoTaskMemFree(value)
            except (AttributeError, OSError):
                pass



def user_downloads_directory() -> _core.Path:
    directory = _core._windows_downloads_directory() or (_core.Path.home() / "Downloads")
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _core.ManagerError(f"无法访问系统下载目录：{exc}") from exc
    if not directory.is_dir():
        raise _core.ManagerError("系统下载目录不可用。")
    return directory



def _safe_export_filename(value: str) -> str:
    raw = str(value or "").strip()
    raw = _core.re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "-", raw).strip(" .-")
    if raw.casefold().endswith(".json"):
        raw = raw[:-5].rstrip(" .-")
    raw = raw[:96].rstrip(" .-") or "codex-account"
    return f"{raw}.json"



def save_json_export_to_downloads(payload: dict, suggested_name: str) -> dict:
    if not isinstance(payload, dict):
        raise _core.ManagerError("导出内容格式无效。")
    directory = _core.user_downloads_directory()
    file_name = _core._safe_export_filename(suggested_name)
    stem = _core.Path(file_name).stem
    target = directory / file_name
    for index in range(2, 1_002):
        if not target.exists():
            break
        target = directory / f"{stem} ({index}).json"
    else:
        target = directory / f"{stem}-{_core.datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.json"
    try:
        _core.atomic_write_json(target, payload)
    except OSError as exc:
        raise _core.ManagerError(f"无法写入下载目录：{exc}") from exc
    return {
        "path": str(target),
        "fileName": target.name,
        "directory": str(directory),
        "savedAt": _core.now_iso(),
    }



def read_json(path: _core.Path, default: _core.Any) -> _core.Any:
    try:
        with path.open("rb") as stream:
            raw = stream.read(_core.STATE_JSON_MAX_BYTES + 1)
    except FileNotFoundError:
        return default
    if len(raw) > _core.STATE_JSON_MAX_BYTES:
        raise _core.ManagerError(
            f"JSON 文件过大，已拒绝读取：{path}（上限 {_core.STATE_JSON_MAX_BYTES // (1024 * 1024)} MB）"
        )
    try:
        return _core.json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, _core.json.JSONDecodeError) as exc:
        raise _core.ManagerError(f"JSON 文件格式无效：{path}") from exc



def _safe_account_activation_events(payload: _core.Any) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        return []
    events: list[dict] = []
    for raw in payload["events"][-_core.MAX_ACCOUNT_ACTIVATION_EVENTS:]:
        if not isinstance(raw, dict):
            continue
        account_id = str(raw.get("accountId") or "").strip()[:200]
        timestamp = str(raw.get("timestamp") or "").strip()[:80]
        if not account_id or len(timestamp) < 20:
            continue
        events.append(
            {
                "accountId": account_id,
                "timestamp": timestamp,
                "source": str(raw.get("source") or "manager_switch").strip()[:40],
            }
        )
    return events



def record_direct_account_activation(
    account_id: str,
    timestamp: str | None = None,
    source: str = "manager_switch",
) -> dict:
    """Append a non-sensitive account activation marker for usage attribution."""
    account_id = str(account_id or "").strip()
    if not account_id:
        raise _core.ManagerError("账号切换记录缺少账号 ID。")
    settings = _core.load_settings()
    if not any(str(item.get("id") or "") == account_id for item in settings.get("accounts", [])):
        raise _core.ManagerError("账号切换记录指向不存在的账号。")
    event = {
        "accountId": account_id[:200],
        "timestamp": str(timestamp or _core.now_iso()).strip()[:80],
        "source": str(source or "manager_switch").strip()[:40],
    }
    with _core.ACCOUNT_ACTIVATION_HISTORY_LOCK:
        try:
            existing = _core._safe_account_activation_events(
                _core.read_json(_core.ACCOUNT_ACTIVATION_HISTORY_FILE, {"events": []})
            )
        except _core.ManagerError:
            existing = []
        if not existing or any(
            existing[-1].get(key) != event.get(key) for key in ("accountId", "timestamp")
        ):
            existing.append(event)
        _core.atomic_write_json(
            _core.ACCOUNT_ACTIVATION_HISTORY_FILE,
            {
                "schemaVersion": 1,
                "updatedAt": _core.now_iso(),
                "events": existing[-_core.MAX_ACCOUNT_ACTIVATION_EVENTS:],
            },
        )
    return event



def account_activation_timeline(settings: dict | None = None) -> list[dict]:
    """Return known direct-account activations without reading any credentials.

    Auth-switch backup manifests provide historical evidence for existing
    installations; the dedicated bounded ledger preserves future switches even
    when old backups are pruned.
    """
    settings = settings or _core.load_settings()
    known_ids = {
        str(item.get("id") or "")
        for item in settings.get("accounts", [])
        if str(item.get("id") or "")
    }
    events: list[dict] = []
    with _core.ACCOUNT_ACTIVATION_HISTORY_LOCK:
        try:
            events.extend(
                _core._safe_account_activation_events(
                    _core.read_json(_core.ACCOUNT_ACTIVATION_HISTORY_FILE, {"events": []})
                )
            )
        except _core.ManagerError:
            pass
    try:
        manifests = list(_core.BACKUPS_DIR.glob("auth-switch-*/manifest.json"))
    except OSError:
        manifests = []
    for manifest in manifests[-_core.BACKUP_MAX_FILES:]:
        try:
            raw = _core.read_json(manifest, {})
        except (_core.ManagerError, OSError):
            continue
        if not isinstance(raw, dict):
            continue
        events.append(
            {
                "accountId": str(raw.get("targetAccountId") or "")[:200],
                "timestamp": str(raw.get("createdAt") or "")[:80],
                "source": "auth_switch_backup",
            }
        )
    # lastUsedAt is an exact marker for the most recent successful use of each
    # account, even on installations created before the activation ledger.
    for account in settings.get("accounts", []):
        if not isinstance(account, dict) or not account.get("lastUsedAt"):
            continue
        events.append(
            {
                "accountId": str(account.get("id") or "")[:200],
                "timestamp": str(account.get("lastUsedAt") or "")[:80],
                "source": "account_last_used",
            }
        )
    unique: dict[tuple[str, str], dict] = {}
    for event in events:
        account_id = str(event.get("accountId") or "")
        timestamp = str(event.get("timestamp") or "")
        if account_id not in known_ids or len(timestamp) < 20:
            continue
        unique[(timestamp, account_id)] = event
    return sorted(unique.values(), key=lambda item: (item["timestamp"], item["accountId"]))



def decode_toml_bytes(raw: bytes) -> str:
    import agent_manager.config.recovery
    return agent_manager.config.recovery.decode_toml(raw)



def read_toml_text(path: _core.Path) -> str:
    import agent_manager.config.recovery
    # Preserve the text-mode reader's newline contract after BOM decoding.
    # Otherwise a Windows write_text adds a second CR to preserved CRLF lines.
    return agent_manager.config.recovery.read_text(path).replace("\r\n", "\n")



def read_toml(path: _core.Path) -> dict:
    try:
        return _core.tomllib.loads(_core.read_toml_text(path))
    except FileNotFoundError:
        return {}
    except (UnicodeError, _core.tomllib.TOMLDecodeError) as exc:
        raise _core.ManagerError(f"TOML 文件格式无效：{path}: {exc}") from exc



def backup_file(path: _core.Path) -> _core.Path | None:
    if path.absolute() == _core.CONFIG_FILE.absolute():
        import agent_manager.config.backups
        return agent_manager.config.backups.backup_current()
    if not path.exists():
        return None
    _core.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _core.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = _core.BACKUPS_DIR / f"{path.name}.{stamp}.bak"
    _core.shutil.copy2(path, destination)
    _core._prune_file_backups()
    return destination



def _prune_file_backups() -> None:
    """Bound automatic file backups without touching history-sync directories."""

    try:
        backups = sorted(
            (item for item in _core.BACKUPS_DIR.glob("*.bak")
             if item.is_file() and not item.name.startswith("config.toml")),
            key=lambda item: item.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        return
    kept_bytes = 0
    for index, item in enumerate(backups):
        try:
            size = item.stat().st_size
            keep = index < _core.BACKUP_MAX_FILES and kept_bytes + size <= _core.BACKUP_MAX_BYTES
            if keep:
                kept_bytes += size
            else:
                item.unlink(missing_ok=True)
        except OSError:
            continue



def _codex_config_entry_meta(key: str) -> tuple[str, str]:
    normalized = _core.re.sub(r"^model_providers\.[^.]+\.", "model_providers.*.", key)
    if key in _core.CODEX_CONFIG_FIELD_META:
        return _core.CODEX_CONFIG_FIELD_META[key]
    if normalized in _core.CODEX_CONFIG_FIELD_META:
        return _core.CODEX_CONFIG_FIELD_META[normalized]
    if key.startswith("features."):
        return ("功能开关", "Codex 实验性或可选功能的开关。")
    if key.startswith("agents."):
        return ("代理配置", "Codex 多代理运行时使用的配置字段。")
    label = key.rsplit(".", 1)[-1].replace("_", " ")
    return (label, "当前 config.toml 中的自定义字段。")



def _codex_config_value_type(value: _core.Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, (_core.datetime, _core.date_value, _core.time_value)):
        return "datetime"
    if isinstance(value, dict):
        return "table"
    return "unknown"



def _json_safe_toml_value(value: _core.Any) -> _core.Any:
    if isinstance(value, (_core.datetime, _core.date_value, _core.time_value)):
        return value.isoformat()
    if isinstance(value, list):
        return [_core._json_safe_toml_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _core._json_safe_toml_value(item) for key, item in value.items()}
    return value



def _json_toml_value(value: _core.Any, *, depth: int = 0) -> bool:
    if depth > 16:
        return False
    if isinstance(value, bool) or isinstance(value, str) or isinstance(value, int):
        return True
    if isinstance(value, float):
        return _core.math.isfinite(value)
    if isinstance(value, list):
        return all(_core._json_toml_value(item, depth=depth + 1) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str)
            and bool(key)
            and _core._json_toml_value(item, depth=depth + 1)
            for key, item in value.items()
        )
    return False



def _flatten_codex_config(value: _core.Any, path: tuple[str, ...] = ()) -> list[dict]:
    if isinstance(value, dict):
        entries: list[dict] = []
        for key, item in value.items():
            entries.extend(_core._flatten_codex_config(item, (*path, str(key))))
        return entries
    key = ".".join(path)
    value_type = _core._codex_config_value_type(value)
    label, description = _core._codex_config_entry_meta(key)
    return [
        {
            "path": list(path),
            "key": key,
            "section": path[0] if len(path) > 1 else "root",
            "label": label,
            "description": description,
            "type": value_type,
            "value": _core._json_safe_toml_value(value),
            "editable": value_type in {"boolean", "integer", "float", "string", "datetime"}
            or (value_type == "array" and _core._json_toml_value(value)),
        }
    ]

