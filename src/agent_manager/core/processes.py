"""Processes services."""
from __future__ import annotations
from agent_manager import core as _core


def _trusted_codex_process_path(image: str, executable: str | None, desktop_executable: _core.Path | None) -> bool:
    if not executable:
        return False
    try:
        candidate = _core.Path(executable).resolve()
    except OSError:
        return False
    name = image.casefold()
    desktop_path = desktop_executable.resolve() if desktop_executable else None
    desktop_root = desktop_path.parent if desktop_path else None
    if name in {"chatgpt.exe", "openai.codex.exe"}:
        return bool(desktop_path and candidate == desktop_path)
    if name != "codex.exe":
        return False
    if _core._is_manager_downloaded_codex_path(candidate) or _core._is_desktop_managed_codex_path(candidate):
        return True
    if desktop_root:
        try:
            candidate.relative_to(desktop_root)
            return True
        except ValueError:
            pass
    joined = "/".join(part.casefold() for part in candidate.parts)
    return "/node_modules/@openai/codex/" in joined or "/node_modules/@openai/codex-win32-" in joined



def running_codex_processes() -> list[dict]:
    if _core.os.name != "nt":
        return _core.CodexProcessScan()
    try:
        desktop = _core._detect_codex_windows_app()
    except Exception as exc:
        return _core.CodexProcessScan(
            [],
            known=False,
            error=f"无法读取 Codex Desktop 安装信息（{type(exc).__name__}）。",
        )
    desktop_executable = _core.Path(str((desktop or {}).get("executable") or "")).resolve() if desktop else None
    if desktop and desktop.get("appUserModelId") and (desktop_executable is None or not desktop_executable.is_file()):
        # A persisted AppUserModelId survives Store updates, but its versioned
        # executable path does not. Refresh before using that path as the trust
        # anchor for process detection.
        refreshed = _core._detect_codex_windows_app(force=True)
        if refreshed:
            desktop = refreshed
            desktop_executable = _core.Path(str(refreshed.get("executable") or "")).resolve()

    candidates = _core._running_windows_codex_candidates()
    return _core.CodexProcessScan(
        [
            {**item, "verified": True}
            for item in candidates
            if _core._trusted_codex_process_path(item["name"], item.get("executable"), desktop_executable)
        ],
        known=bool(getattr(candidates, "known", True)),
        error=getattr(candidates, "error", None),
    )



def _running_windows_codex_candidates() -> list[dict]:
    """Enumerate paths only; never call install discovery or classify ownership."""
    if _core.os.name != "nt":
        return _core.CodexProcessScan()
    target_names = {"codex.exe", "chatgpt.exe", "openai.codex.exe"}

    def process_image_path(pid: int) -> str | None:
        handle = None
        try:
            kernel32 = _core.ctypes.windll.kernel32
            open_process = kernel32.OpenProcess
            open_process.argtypes = [_core.wintypes.DWORD, _core.wintypes.BOOL, _core.wintypes.DWORD]
            open_process.restype = _core.wintypes.HANDLE
            query_image = kernel32.QueryFullProcessImageNameW
            query_image.argtypes = [_core.wintypes.HANDLE, _core.wintypes.DWORD, _core.wintypes.LPWSTR, _core.ctypes.POINTER(_core.wintypes.DWORD)]
            query_image.restype = _core.wintypes.BOOL
            handle = open_process(0x1000, False, int(pid))
            if not handle:
                return None
            capacity = _core.wintypes.DWORD(32768)
            buffer = _core.ctypes.create_unicode_buffer(capacity.value)
            if not query_image(handle, 0, buffer, _core.ctypes.byref(capacity)):
                return None
            value = str(buffer.value).strip()
            return value or None
        except Exception:
            return None
        finally:
            if handle:
                try:
                    _core.ctypes.windll.kernel32.CloseHandle(handle)
                except Exception:
                    pass

    class ProcessEntry32W(_core.ctypes.Structure):
        _fields_ = [
            ("dwSize", _core.wintypes.DWORD),
            ("cntUsage", _core.wintypes.DWORD),
            ("th32ProcessID", _core.wintypes.DWORD),
            ("th32DefaultHeapID", _core.ctypes.c_size_t),
            ("th32ModuleID", _core.wintypes.DWORD),
            ("cntThreads", _core.wintypes.DWORD),
            ("th32ParentProcessID", _core.wintypes.DWORD),
            ("pcPriClassBase", _core.wintypes.LONG),
            ("dwFlags", _core.wintypes.DWORD),
            ("szExeFile", _core.wintypes.WCHAR * 260),
        ]

    unverified_candidates = False
    try:
        kernel32 = _core.ctypes.windll.kernel32
        create_snapshot = kernel32.CreateToolhelp32Snapshot
        create_snapshot.argtypes = [_core.wintypes.DWORD, _core.wintypes.DWORD]
        create_snapshot.restype = _core.wintypes.HANDLE
        process_first = kernel32.Process32FirstW
        process_first.argtypes = [_core.wintypes.HANDLE, _core.ctypes.POINTER(ProcessEntry32W)]
        process_first.restype = _core.wintypes.BOOL
        process_next = kernel32.Process32NextW
        process_next.argtypes = [_core.wintypes.HANDLE, _core.ctypes.POINTER(ProcessEntry32W)]
        process_next.restype = _core.wintypes.BOOL
        snapshot = create_snapshot(0x00000002, 0)
        invalid_handle = _core.ctypes.c_void_p(-1).value
        if snapshot in {None, invalid_handle}:
            raise OSError("CreateToolhelp32Snapshot failed")
        processes = []
        try:
            entry = ProcessEntry32W()
            entry.dwSize = _core.ctypes.sizeof(ProcessEntry32W)
            has_entry = bool(process_first(snapshot, _core.ctypes.byref(entry)))
            while has_entry:
                image = str(entry.szExeFile).strip()
                if image.casefold() in target_names:
                    pid = int(entry.th32ProcessID)
                    executable = process_image_path(pid)
                    if not executable:
                        unverified_candidates = True
                        has_entry = bool(process_next(snapshot, _core.ctypes.byref(entry)))
                        continue
                    processes.append(
                        {
                            "name": image,
                            "pid": str(pid),
                            "parentPid": str(int(entry.th32ParentProcessID)),
                            "executable": executable,
                            "pathVerified": True,
                        }
                    )
                has_entry = bool(process_next(snapshot, _core.ctypes.byref(entry)))
        finally:
            kernel32.CloseHandle(snapshot)
        if unverified_candidates:
            return _core.CodexProcessScan(
                processes,
                known=False,
                error="无法读取一个或多个同名 Codex 进程的可执行路径。",
            )
        return _core.CodexProcessScan(processes)
    except Exception as exc:
        # Toolhelp is the fast path. Keep tasklist as a diagnostic fallback for
        # restricted Windows environments, but never treat its output as
        # authority because it does not expose a trusted executable path.
        flags = getattr(_core.subprocess, "CREATE_NO_WINDOW", 0)
        tasklist_output = ""
        try:
            tasklist = _core.subprocess.run(
                ["tasklist.exe", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                creationflags=flags,
            )
            tasklist_output = str(tasklist.stdout or "")
        except Exception as tasklist_exc:
            tasklist_output = f"tasklist failed: {type(tasklist_exc).__name__}"
        matching = any(name in tasklist_output.casefold() for name in target_names)
        detail = (
            "tasklist 检测到可能的 Codex 进程，但无法验证可执行路径。"
            if matching
            else "Windows 进程枚举失败，无法确认 Codex 是否仍在运行。"
        )
        return _core.CodexProcessScan(
            [],
            known=False,
            error=f"{detail}（{type(exc).__name__}）",
        )



def _require_known_codex_processes(processes: list[dict] | None = None) -> list[dict]:
    records = _core.running_codex_processes() if processes is None else processes
    if getattr(records, "known", True) is False:
        detail = str(getattr(records, "error", "") or "").strip()
        suffix = f"：{detail}" if detail else "。"
        raise _core.CodexProcessScanError(f"无法可靠检测 Codex 进程，已停止本次修改操作{suffix}")
    return records



def _require_known_codex_processes_with_retry(
    *,
    attempts: int,
    retry_interval: float = 0.12,
    context: str = "检测期间",
) -> tuple[list[dict], int]:
    """Wait briefly for an authoritative Windows process snapshot.

    Electron renderers and the packaged app-server can disappear between the
    Toolhelp snapshot and ``QueryFullProcessImageNameW``.  That race must never
    be treated as permission to modify state or kill an unverified process, but
    it also should not force the user to click the same safe operation twice.
    Retry the read-only scan a bounded number of times and proceed only after a
    fully authoritative snapshot is obtained.
    """
    try:
        total_attempts = max(1, min(int(attempts), 20))
    except (TypeError, ValueError):
        total_attempts = 1
    try:
        interval = max(0.02, min(float(retry_interval), 0.5))
    except (TypeError, ValueError):
        interval = 0.12
    last_error = ""
    for attempt in range(total_attempts):
        records = _core.running_codex_processes()
        if getattr(records, "known", True) is not False:
            return list(records), attempt
        last_error = str(getattr(records, "error", "") or "").strip()
        if attempt + 1 < total_attempts:
            _core.time.sleep(interval)
    suffix = f"：{last_error}" if last_error else "。"
    raise _core.CodexProcessScanError(
        f"无法可靠检测 Codex 进程，已停止本次修改操作{suffix}"
        f"（{context}已自动复查 {total_attempts} 次；为避免误关其他同名程序，没有继续修改。）"
    )



def _require_codex_process_scan_known() -> list[dict]:
    return _core._require_known_codex_processes()



def _codex_launch_process_observation() -> tuple[list[dict], str | None]:
    """Return verified launch evidence without treating a transient partial scan as fatal.

    Closing or rewriting Codex state still requires a fully authoritative scan.
    Startup is read-only: a verified Desktop root is sufficient evidence even
    if a newly-created renderer is temporarily protected from path queries.
    """
    scan = _core.running_codex_processes()
    records = list(scan)
    error = (
        str(getattr(scan, "error", "") or "").strip() or None
        if getattr(scan, "known", True) is False
        else None
    )
    return records, error



def close_codex_processes(timeout_seconds: float = 10.0) -> dict:
    """Close only verified Codex app/CLI processes without opening a console.

    Never use ``taskkill /T`` here. Agent Manager may have been launched from a
    Codex tool or terminal, and Windows can retain that ancestry or Job membership
    after the intermediate shell exits. Tree termination can therefore kill the
    manager itself or another unrelated descendant. Ask the already path-verified
    Codex roots to exit first so Electron cannot keep respawning renderer children,
    then re-read every PID before the optional child-first force pass.
    """
    if _core.os.name != "nt":
        return {"requested": [], "closed": [], "forced": [], "alreadyStopped": True}
    try:
        timeout = max(1.0, min(float(timeout_seconds), 30.0))
    except (TypeError, ValueError):
        timeout = 10.0
    initial, scan_retries = _core._require_known_codex_processes_with_retry(
        attempts=4,
        retry_interval=0.08,
        context="切换预检",
    )
    if not initial:
        return {"requested": [], "closed": [], "forced": [], "alreadyStopped": True}
    targets = {
        str(item.get("pid")): {
            "name": str(item.get("name")),
            "parentPid": str(item.get("parentPid") or ""),
        }
        for item in initial
        if str(item.get("pid") or "").isdigit() and str(item.get("name") or "")
    }
    if not targets:
        raise _core.ManagerError("检测到了 Codex 进程，但无法读取安全的进程 ID。")
    flags = getattr(_core.subprocess, "CREATE_NO_WINDOW", 0)
    started = _core.time.monotonic()
    deadline = started + timeout
    # Include taskkill itself in both budgets. Previously every PID could spend
    # eight seconds before the nominal graceful/force wait even began.
    graceful_deadline = started + min(3.0, timeout / 2)

    def terminate(pids: list[str], *, force: bool, until: float) -> list[str]:
        issued = []
        # Bounded exact-PID batches avoid one shell/process launch per renderer.
        # Never use /T, /IM or any image-name/wildcard termination.
        for offset in range(0, len(pids), 64):
            remaining_seconds = until - _core.time.monotonic()
            if remaining_seconds <= 0:
                break
            batch = pids[offset:offset + 64]
            command = ["taskkill.exe", *(["/F"] if force else [])]
            for pid in batch:
                command.extend(["/PID", pid])
            issued.extend(batch)
            try:
                _core.subprocess.run(
                    command,
                    stdin=_core.subprocess.DEVNULL,
                    stdout=_core.subprocess.DEVNULL,
                    stderr=_core.subprocess.DEVNULL,
                    timeout=min(8.0, remaining_seconds),
                    creationflags=flags,
                )
            except (OSError, _core.subprocess.TimeoutExpired):
                # A successful/failed command is not evidence of process exit.
                pass
        return issued

    def process_depth(pid: str, records: dict[str, dict]) -> int:
        depth = 0
        seen = {pid}
        parent = str(records.get(pid, {}).get("parentPid") or "")
        while parent in records and parent not in seen:
            seen.add(parent)
            depth += 1
            parent = str(records.get(parent, {}).get("parentPid") or "")
        return depth

    def depth_batches(pids, records, *, reverse=False):
        groups = {}
        for pid in pids:
            groups.setdefault(process_depth(pid, records), []).append(pid)
        batches = []
        for depth in sorted(groups, reverse=reverse):
            ordered = sorted(groups[depth], key=int, reverse=reverse)
            batches.extend(ordered[offset:offset + 64] for offset in range(0, len(ordered), 64))
        return batches

    def records_for(processes):
        return {
            str(item.get("pid")): {
                "name": str(item.get("name")),
                "parentPid": str(item.get("parentPid") or ""),
            }
            for item in processes
            if str(item.get("pid") or "").isdigit() and str(item.get("name") or "")
        }

    def observe(until, context):
        nonlocal scan_retries
        # Reserve only the retry sleeps that still fit. At expiry, perform one
        # authoritative scan: a deadline must never turn unknown into stopped.
        attempts = max(1, min(12, int(max(0.0, until - _core.time.monotonic()) / 0.12) + 1))
        current, retries = _core._require_known_codex_processes_with_retry(
            attempts=attempts, context=context,
        )
        scan_retries += retries
        return current

    root_pids = [pid for pid, item in targets.items() if item["parentPid"] not in targets]
    for batch in depth_batches(targets, targets):
        terminate(batch, force=False, until=graceful_deadline)

    while True:
        current = observe(graceful_deadline, "等待 Codex 退出时")
        current_records = records_for(current)
        if not current or _core.time.monotonic() >= graceful_deadline:
            break
        newly_discovered = [pid for pid in current_records if pid not in targets]
        targets.update({pid: current_records[pid] for pid in newly_discovered})
        for batch in depth_batches(newly_discovered, targets, reverse=True):
            terminate(batch, force=False, until=graceful_deadline)
        _core.time.sleep(max(0.0, min(0.15, graceful_deadline - _core.time.monotonic())))

    forced = []
    if current:
        targets.update({pid: record for pid, record in current_records.items() if pid not in targets})
        for batch in depth_batches(current_records, current_records, reverse=True):
            if _core.time.monotonic() >= deadline:
                break
            # Revalidate after each potentially slow batch. Only records still
            # path-verified by the scanner and matching the same image qualify.
            fresh = records_for(observe(deadline, "强制关闭前的回验中"))
            verified = [
                pid for pid in batch if pid in fresh
                and fresh[pid]["name"].casefold() == current_records[pid]["name"].casefold()
            ]
            forced.extend(terminate(verified, force=True, until=deadline))
        while True:
            current = observe(deadline, "强制关闭后的回验中")
            if not current or _core.time.monotonic() >= deadline:
                break
            _core.time.sleep(max(0.0, min(0.15, deadline - _core.time.monotonic())))
    remaining = {pid: record["name"] for pid, record in records_for(current).items()}
    if remaining:
        labels = ", ".join(f"{name} ({pid})" for pid, name in remaining.items())
        raise _core.ManagerError(f"无法自动关闭 Codex：{labels}。请手动关闭后重试。")
    return {
        "requested": initial,
        "closed": list(targets),
        "rootPids": root_pids,
        "forced": forced,
        "processScanRetries": scan_retries,
        "alreadyStopped": False,
    }



def current_auth_state(settings: dict | None = None, force: bool = False) -> dict:
    settings = settings or _core.load_settings()
    auth_path = _core.CODEX_HOME / "auth.json"
    try:
        auth_stamp = auth_path.stat().st_mtime_ns
    except OSError:
        auth_stamp = 0
    try:
        settings_stamp = _core.SETTINGS_FILE.stat().st_mtime_ns
    except OSError:
        settings_stamp = 0
    try:
        config_stamp = _core.CONFIG_FILE.stat().st_mtime_ns
    except OSError:
        config_stamp = 0
    cache_key = (str(_core.CODEX_HOME), auth_stamp, settings_stamp, config_stamp)
    with _core.AUTH_STATE_CACHE_LOCK:
        if (
            not force
            and _core.AUTH_STATE_CACHE.get("key") == cache_key
            and isinstance(_core.AUTH_STATE_CACHE.get("value"), dict)
            and _core.time.monotonic() - float(_core.AUTH_STATE_CACHE.get("at", 0)) < 2.0
        ):
            return _core.json.loads(_core.json.dumps(_core.AUTH_STATE_CACHE["value"]))
    credential_store = _core._credential_store_mode()
    try:
        _, identity = _core._read_live_snapshot()
        error = None
    except _core.ManagerError as exc:
        identity = None
        error = _core._redact_sensitive_text(exc, limit=320)
    active_id = None
    if identity:
        active = _core._find_account_for_identity(settings, identity)
        active_id = active.get("id") if active else None
    process_scan = _core.running_codex_processes()
    result = {
        "signedIn": identity is not None,
        "authMode": identity.get("authMode") if identity else None,
        "email": identity.get("email") if identity else "",
        "name": identity.get("name") if identity else "",
        "plan": identity.get("plan") if identity else "",
        "display": identity.get("display") if identity else "未登录",
        "activeAccountId": active_id,
        "declaredAuthMode": identity.get("declaredAuthMode") if identity else "",
        "authContractValid": identity.get("authContractValid") if identity else False,
        "requiresReapply": bool(
            identity
            and active_id
            and identity.get("authMode") == "chatgpt"
            and not identity.get("authContractValid")
        ),
        "credentialStore": credential_store,
        "snapshotSupported": credential_store != "keyring",
        "error": error,
        "runningProcesses": list(process_scan),
        "processScanKnown": bool(getattr(process_scan, "known", True)),
        "processScanError": getattr(process_scan, "error", None),
    }
    import agent_manager.sessions.live_selection

    result["liveSelection"] = agent_manager.sessions.live_selection.inspect_live_selection(settings, auth=result)
    with _core.AUTH_STATE_CACHE_LOCK:
        _core.AUTH_STATE_CACHE.update({"key": cache_key, "at": _core.time.monotonic(), "value": _core.json.loads(_core.json.dumps(result))})
    return result

