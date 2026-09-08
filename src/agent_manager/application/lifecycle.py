"""Lifecycle services."""
from __future__ import annotations
from agent_manager import application as _app


def _read_shutdown_status() -> dict:
    """Return one bounded, UI-safe lifecycle record for restart diagnostics."""
    if not _app.SHUTDOWN_STATUS_FILE.is_file():
        return {}
    try:
        with _app.SHUTDOWN_STATUS_FILE.open("rb") as stream:
            raw = stream.read(_app.MAX_RUNTIME_FILE_BYTES + 1)
        if len(raw) > _app.MAX_RUNTIME_FILE_BYTES:
            return {}
        payload = _app.json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, _app.json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    raw_timing = payload.get("restartTiming")
    restart_timing = {
        str(key)[:80]: value
        for key, value in raw_timing.items()
        if isinstance(key, str) and isinstance(value, (bool, int, float, str))
    } if isinstance(raw_timing, dict) else {}
    return {
        "pid": int(payload.get("pid") or 0),
        "phase": str(payload.get("phase") or "")[:80],
        "at": str(payload.get("at") or "")[:80],
        "errors": [_app.core._redact_sensitive_text(item, limit=300) for item in errors[:8]],
        "native": bool(payload.get("native")),
        "restartTiming": restart_timing,
    }



def open_existing_runtime() -> bool:
    if not _app.RUNTIME_FILE.exists():
        return False
    try:
        runtime = _app._read_runtime_file()
        if _app._probe_runtime_identity(runtime):
            opened = _app.request_existing_window(runtime)
            if not opened and bool(runtime.get("native")):
                opened = _app.focus_process_window(int(runtime.get("pid", 0)))
            return opened
    except Exception:
        return False
    return False



def idle_watchdog(server: _app.ManagerServer, timeout_seconds: int = 14_400) -> None:
    while True:
        _app.time.sleep(30)
        if _app.time.time() - server.last_request_at > timeout_seconds:
            _app.request_application_shutdown(server)
            return



def _cleanup_runtime() -> None:
    try:
        runtime = _app._read_runtime_file()
        if runtime.get("pid") == _app.os.getpid():
            _app.RUNTIME_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    _app._release_instance_mutex()



def _write_runtime_discovery(server: _app.ManagerServer) -> None:
    control_token = str(getattr(server, "control_token", "") or "")
    session = getattr(getattr(server, "runtime", None), "configuration_session", {})
    timing = session.get("restartTiming", {}) if isinstance(session, dict) else {}
    _app.core.atomic_write_json(
        _app.RUNTIME_FILE,
        {
            "pid": _app.os.getpid(),
            "port": int(server.server_address[1]),
            "appId": _app.RUNTIME_APP_ID,
            "runtimeNonce": server.runtime_nonce,
            "startedAt": str(getattr(server, "started_at", "") or _app.core.now_iso()),
            "native": bool(server.native_window),
            "uiReady": bool(
                getattr(server, "ui_ready", None)
                and server.ui_ready.is_set()
            ),
            "restartTiming": {
                key: timing[key]
                for key in ("activationMs", "configurationReapplied", "fastOverlayAdopted")
                if key in timing
            } if isinstance(timing, dict) else {},
            "independentLifecycle": _app._manager_lifecycle_is_independent(),
            "lifecycleIsolation": _app._WINDOWS_JOB_ISOLATION_STATUS,
            "activationToken": server.activation_token,
            **(
                {
                    "controlToken": control_token,
                    "controlApi": {
                        "quickRestartPath": _app.CONTROL_QUICK_RESTART_PATH,
                        "header": _app.CONTROL_HEADER_NAME,
                    },
                }
                if control_token
                else {}
            ),
        },
    )



def _mark_ui_ready(server: _app.ManagerServer) -> None:
    """Publish readiness only after the visible UI and config are active."""
    server.ui_ready.set()
    try:
        _app._write_runtime_discovery(server)
    except Exception:
        server.ui_ready.clear()
        raise



def _write_restart_handoff(token: str, metadata: dict | None = None) -> None:
    details = dict(metadata or {})
    details.pop("token", None)
    details.pop("parentPid", None)
    details.pop("requestedAtEpoch", None)
    details.pop("requestedAt", None)
    _app.core.atomic_write_json(
        _app.RESTART_HANDOFF_FILE,
        {
            "protocolVersion": 2,
            **details,
            "token": token,
            "parentPid": _app.os.getpid(),
            "requestedAtEpoch": _app.time.time(),
            "requestedAt": _app.core.now_iso(),
        },
    )



def _restart_handoff_payload(token: str | None) -> dict | None:
    token = str(token or "").strip()
    if not token:
        return None
    try:
        payload = _app.json.loads(_app.RESTART_HANDOFF_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    expected = str(payload.get("token") or "")
    if not expected or not _app.secrets.compare_digest(expected, token):
        return None
    return payload



def _restart_handoff_is_valid(token: str | None) -> bool:
    payload = _app._restart_handoff_payload(token)
    if payload is None:
        return False
    try:
        requested_at = float(payload.get("requestedAtEpoch") or 0)
    except (TypeError, ValueError):
        return False
    return 0 <= _app.time.time() - requested_at <= _app.RESTART_HANDOFF_TIMEOUT_SECONDS



def _consume_restart_handoff(token: str | None, *, delete: bool = True) -> bool:
    payload = _app._restart_handoff_payload(token)
    if payload is None:
        return False
    try:
        requested_at = float(payload.get("requestedAtEpoch") or 0)
    except (TypeError, ValueError):
        return False
    valid = 0 <= _app.time.time() - requested_at <= _app.RESTART_HANDOFF_TIMEOUT_SECONDS
    if delete:
        _app.RESTART_HANDOFF_FILE.unlink(missing_ok=True)
    return valid



def _mark_restart_child_staged(token: str) -> dict | None:
    """Publish that extraction/imports completed and the child is at the mutex."""
    payload = _app._restart_handoff_payload(token)
    if payload is None or not _app._restart_handoff_is_valid(token):
        return None
    payload.update(
        {
            "childPid": _app.os.getpid(),
            "childStagedAtEpoch": _app.time.time(),
            "childBuild": _app._current_manager_build_fingerprint(),
        }
    )
    _app.core.atomic_write_json(_app.RESTART_HANDOFF_FILE, payload)
    return payload



def _arm_restart_readiness_deadline(token: str) -> dict:
    payload = _app._restart_handoff_payload(token)
    if payload is None or not payload.get("childStagedAtEpoch"):
        raise _app.core.ManagerError("新管理器未完成启动预热，快速重启已取消。")
    payload["readyDeadlineEpoch"] = _app.time.time() + _app.RESTART_REPLACEMENT_READY_SECONDS
    _app.core.atomic_write_json(_app.RESTART_HANDOFF_FILE, payload)
    return payload



def _take_restart_handoff(token: str | None) -> dict | None:
    payload = _app._restart_handoff_payload(token)
    if payload is None or not _app._restart_handoff_is_valid(token):
        return None
    _app.RESTART_HANDOFF_FILE.unlink(missing_ok=True)
    return payload



def _executable_version(path: _app.Path) -> tuple[int, int, int, int]:
    match = _app.re.fullmatch(
        r"(?:AgentManager|CodexAgentManager)-(\d+)\.(\d+)\.(\d+)(?:[.-](\d+))?\.exe",
        path.name,
        _app.re.IGNORECASE,
    )
    filename_version = tuple(int(value or 0) for value in match.groups()) if match else (0, 0, 0, 0)
    if _app.os.name != "nt" or not path.is_file():
        return filename_version
    try:
        version = _app.ctypes.windll.version
        size = int(version.GetFileVersionInfoSizeW(str(path), None))
        if size <= 0:
            return filename_version
        buffer = _app.ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return filename_version
        value = _app.ctypes.c_void_p()
        length = _app.wintypes.UINT()
        if not version.VerQueryValueW(buffer, "\\", _app.ctypes.byref(value), _app.ctypes.byref(length)):
            return filename_version

        class FixedFileInfo(_app.ctypes.Structure):
            _fields_ = [
                ("signature", _app.wintypes.DWORD),
                ("struct_version", _app.wintypes.DWORD),
                ("file_version_ms", _app.wintypes.DWORD),
                ("file_version_ls", _app.wintypes.DWORD),
            ]

        fixed = _app.ctypes.cast(value, _app.ctypes.POINTER(FixedFileInfo)).contents
        if fixed.signature != 0xFEEF04BD:
            return filename_version
        return (
            fixed.file_version_ms >> 16,
            fixed.file_version_ms & 0xFFFF,
            fixed.file_version_ls >> 16,
            fixed.file_version_ls & 0xFFFF,
        )
    except Exception:
        return filename_version



def _manager_build_fingerprint(path: _app.Path | None = None) -> str:
    """Identify configuration-generating code without trusting a filename."""
    digest = _app.hashlib.sha256()
    digest.update(f"quick-restart-v2\0schema={getattr(_app.core, 'SCHEMA_VERSION', 0)}\0".encode("ascii"))
    if getattr(_app.sys, "frozen", False):
        candidates = [_app.Path(path or _app.sys.executable).resolve()]
    else:
        candidates = sorted(_app.Path(_app.__file__).resolve().parents[1].rglob("*.py"))
    try:
        for index, candidate in enumerate(candidates):
            digest.update(f"file={index}\0".encode("ascii"))
            with candidate.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()



def _current_manager_build_fingerprint() -> str:
    return _app._manager_build_fingerprint(
        _app.Path(_app.sys.executable).resolve() if getattr(_app.sys, "frozen", False) else None
    )



def _runtime_overlay_matches_last_apply() -> bool:
    """Verify every journaled managed value still has its last applied hash."""
    try:
        with _app.core.RUNTIME_OVERLAY_LOCK:
            payload = _app.core._runtime_overlay_read()
            if not payload or int(payload.get("ownerPid") or 0) != _app.os.getpid():
                return False
            files = payload.get("files")
            environment = payload.get("environment")
            if not isinstance(files, dict) or not files:
                return False
            if not isinstance(environment, dict):
                return False
            root = _app.core.CODEX_HOME.resolve()
            for relative, record in files.items():
                if not isinstance(record, dict) or not str(record.get("appliedHash") or ""):
                    return False
                candidate = (root / str(relative)).resolve()
                candidate.relative_to(root)
                current = candidate.read_bytes() if candidate.is_file() else None
                if not _app.secrets.compare_digest(
                    str(record["appliedHash"]),
                    str(_app.core._overlay_value_hash(current)),
                ):
                    return False
            for name, record in environment.items():
                if not isinstance(record, dict) or not str(record.get("appliedHash") or ""):
                    return False
                current = _app.core._read_user_environment(str(name))
                if not _app.secrets.compare_digest(
                    str(record["appliedHash"]),
                    str(_app.core._overlay_value_hash(current)),
                ):
                    return False
    except Exception:
        return False
    return True



def _restart_configuration_fingerprint() -> str:
    """Hash saved settings and model capabilities, excluding cache freshness."""
    digest = _app.hashlib.sha256()
    try:
        for index, path in enumerate((_app.core.SETTINGS_FILE, _app.core.MODELS_CACHE_FILE)):
            digest.update(str(index).encode("ascii"))
            if not path.is_file():
                digest.update(b"<absent>")
                continue
            content = _app.json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(content, dict):
                return ""
            if index == 1:
                # Codex routinely rewrites these even when its model records
                # are identical. Neither field affects generated configuration.
                content.pop("fetched_at", None)
                content.pop("etag", None)
            digest.update(_app.json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    except (OSError, ValueError):
        return ""
    return digest.hexdigest()



def _quick_restart_can_adopt_without_apply(handoff: dict | None) -> bool:
    """Use the fast path only for an unchanged build and unchanged overlay."""
    if not isinstance(handoff, dict) or handoff.get("protocolVersion") != 2:
        return False
    source_build = str(handoff.get("sourceBuild") or "")
    target_build = str(handoff.get("targetBuild") or "")
    current_build = _app._current_manager_build_fingerprint()
    return bool(
        handoff.get("sourceOverlayCurrent") is True
        and source_build
        and _app.secrets.compare_digest(source_build, target_build)
        and _app.secrets.compare_digest(target_build, current_build)
        and bool(handoff.get("sourceStateFingerprint"))
        and _app.secrets.compare_digest(str(handoff["sourceStateFingerprint"]), _app._restart_configuration_fingerprint())
        and _app._runtime_overlay_matches_last_apply()
    )



def _manager_restart_command(token: str) -> tuple[list[str], _app.Path]:
    if getattr(_app.sys, "frozen", False):
        current = _app.Path(_app.sys.executable).resolve()
        candidates = [current]
        current_epoch = _app.executable_release_epoch(current)
        for pattern in ("AgentManager*.exe", "CodexAgentManager*.exe"):
            for candidate in current.parent.glob(pattern):
                if _app.executable_release_epoch(candidate) != current_epoch:
                    continue
                if _app.re.fullmatch(
                    r"(?:AgentManager|CodexAgentManager)(?:-[0-9][A-Za-z0-9_.-]*)?\.exe",
                    candidate.name,
                    _app.re.IGNORECASE,
                ):
                    candidates.append(candidate.resolve())
        target = max(
            dict.fromkeys(candidates),
            key=lambda path: (
                _app._executable_version(path),
                path.stat().st_mtime_ns if path.is_file() else 0,
                path.name.casefold() == "agentmanager.exe",
            ),
        )
        # Use the ``--option=value`` form so a URL-safe token that happens to
        # begin with '-' can never be parsed as another command-line option.
        return [str(target), f"--quick-restart-token={token}"], target.parent
    return [_app.sys.executable, "-m", "agent_manager", f"--quick-restart-token={token}"], _app.app_paths.development_root() or _app.Path.home()



def _start_restarted_manager(
    token: str,
    *,
    command: list[str] | None = None,
    workdir: _app.Path | None = None,
    source_pid: int | None = None,
    source_nonce: str | None = None,
) -> dict:
    if command is None or workdir is None:
        command, workdir = _app._manager_restart_command(token)
    flags = 0
    if _app.os.name == "nt":
        flags = getattr(_app.subprocess, "CREATE_NO_WINDOW", 0) | getattr(_app.subprocess, "DETACHED_PROCESS", 0)
    launch_environment = _app.os.environ.copy()
    # A PyInstaller one-file child inherits bootloader state from its parent.
    # Without this reset, relaunching the same EXE can reuse the old temporary
    # extraction directory; when the old parent exits, the new GUI assets are
    # deleted underneath the restarted manager.  Force a fully independent
    # bootloader parent for every update/restart handoff.
    launch_environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    if (
        _app.os.name == "nt"
        and getattr(_app.sys, "frozen", False)
        and _app._manager_lifecycle_is_independent()
    ):
        launch_environment[_app.TRUSTED_RESTART_ENV] = "1"
    process = _app.subprocess.Popen(
        command,
        cwd=str(workdir),
        close_fds=True,
        creationflags=flags,
        env=launch_environment,
    )
    return {
        "process": process,
        "command": list(command),
        "workdir": _app.Path(workdir),
        "token": token,
        "sourcePid": int(source_pid or _app.os.getpid()),
        "sourceNonce": str(source_nonce or ""),
        "startedMonotonic": _app.time.monotonic(),
    }



def _wait_for_restart_child_staged(
    launch: dict,
    timeout_seconds: float = _app.RESTART_STAGE_TIMEOUT_SECONDS,
) -> dict:
    process = launch["process"]
    token = str(launch.get("token") or "")
    deadline = _app.time.monotonic() + max(0.5, min(float(timeout_seconds), 45.0))
    launcher_exit_code: int | None = None
    last_error = "新管理器仍在解包和加载模块"
    while _app.time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            launcher_exit_code = int(exit_code)
            if launcher_exit_code != 0:
                raise _app.core.ManagerError(f"新管理器启动器提前退出，代码 {launcher_exit_code}。")
        payload = _app._restart_handoff_payload(token)
        if payload is not None and payload.get("childStagedAtEpoch"):
            child_build = str(payload.get("childBuild") or "")
            target_build = str(payload.get("targetBuild") or "")
            if target_build and not _app.secrets.compare_digest(child_build, target_build):
                raise _app.core.ManagerError("新管理器预热进程与选定程序不一致，快速重启已取消。")
            return {
                "staged": True,
                "childPid": int(payload.get("childPid") or 0),
                "launcherExitCode": launcher_exit_code,
                "stageMs": round(
                    (_app.time.monotonic() - float(launch["startedMonotonic"])) * 1000,
                    1,
                ),
            }
        if payload is None:
            last_error = "快速重启交接凭据已失效"
            break
        _app.time.sleep(_app.RESTART_POLL_INTERVAL_SECONDS)
    raise _app.core.ManagerError(f"新管理器未能在旧管理器离线前完成预热：{last_error}。")



def _wait_for_restarted_manager(
    launch: dict,
    ready_timeout_seconds: float = _app.RESTART_REPLACEMENT_READY_SECONDS
    + _app.RESTART_REPLACEMENT_EXIT_GRACE_SECONDS,
) -> dict:
    process = launch["process"]
    command = list(launch["command"])
    deadline = _app.time.monotonic() + max(3.0, min(float(ready_timeout_seconds), 45.0))
    last_error = "新进程尚未写入运行状态"
    launcher_exit_code: int | None = None
    while _app.time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            launcher_exit_code = int(exit_code)
            # A packaged manager may perform one CREATE_BREAKAWAY_FROM_JOB or
            # ShellExecute relay before the real runtime starts. That relay
            # exits with code 0 by design. Readiness belongs to the verified
            # runtime-file + health handshake, not to the short-lived launcher
            # PID returned by Popen. Non-zero still fails immediately.
            if launcher_exit_code != 0:
                raise _app.core.ManagerError(f"新管理器启动器提前退出，代码 {launcher_exit_code}。")
        try:
            runtime = _app._read_runtime_file()
            port = int(runtime.get("port") or 0)
            runtime_pid = int(runtime.get("pid") or 0)
            runtime_nonce = str(runtime.get("runtimeNonce") or "")
            if runtime_pid == int(launch.get("sourcePid") or 0):
                last_error = "旧管理器仍在释放运行状态"
                _app.time.sleep(_app.RESTART_POLL_INTERVAL_SECONDS)
                continue
            if launch.get("sourceNonce") and _app.secrets.compare_digest(
                runtime_nonce,
                str(launch["sourceNonce"]),
            ):
                last_error = "运行状态仍属于旧管理器"
                _app.time.sleep(_app.RESTART_POLL_INTERVAL_SECONDS)
                continue
            if not bool(runtime.get("uiReady")):
                last_error = "新管理器窗口尚未就绪"
            elif not bool(runtime.get("independentLifecycle")):
                last_error = "新管理器尚未完成与 Codex 的生命周期隔离"
            if port > 0 and runtime_pid > 0 and _app._probe_runtime_identity(
                runtime,
                require_ui_ready=True,
                require_independent=True,
            ):
                return {
                    "pid": process.pid,
                    "runtimePid": runtime_pid,
                    "port": port,
                    "executable": command[0],
                    "launcherExitCode": launcher_exit_code,
                    "ready": True,
                    "activation": dict(runtime.get("restartTiming") or {}),
                    "readyMs": round(
                        (_app.time.monotonic() - float(launch["startedMonotonic"])) * 1000,
                        1,
                    ),
                    "offlineMs": round(
                        (_app.time.monotonic() - float(launch.get("offlineMonotonic") or launch["startedMonotonic"]))
                        * 1000,
                        1,
                    ),
                }
        except Exception as exc:
            last_error = str(exc)[:240]
        _app.time.sleep(_app.RESTART_POLL_INTERVAL_SECONDS)
    relay_detail = "；启动中继已正常退出" if launcher_exit_code == 0 else ""
    raise _app.core.ManagerError(f"新管理器在限定时间内未就绪{relay_detail}：{last_error}")



def _launch_restarted_manager(token: str, ready_timeout_seconds: float = 20.0) -> dict:
    """Compatibility wrapper for callers that do not use prewarmed handoff."""
    launch = _app._start_restarted_manager(token)
    return _app._wait_for_restarted_manager(launch, ready_timeout_seconds)



def _write_shutdown_status(server: _app.ManagerServer, phase: str, errors: list[str] | None = None) -> None:
    try:
        _app.core.atomic_write_json(
            _app.SHUTDOWN_STATUS_FILE,
            {
                "pid": _app.os.getpid(),
                "phase": phase,
                "runtimeNonce": str(getattr(server, "runtime_nonce", "")),
                "at": _app.core.now_iso(),
                "errors": list(errors or []),
                "native": bool(server.native_window),
                "restartTiming": dict(getattr(server, "quick_restart_timing", {}) or {}),
                "restorationComplete": bool(
                    getattr(server, "shutdown_result", {}).get("restorationComplete")
                ),
                "preserved": bool(getattr(server, "shutdown_result", {}).get("preserved")),
            },
        )
    except Exception:
        pass



def _forced_exit_watchdog(server: _app.ManagerServer) -> None:
    finished = server.shutdown_finished.wait(_app.FORCED_EXIT_TIMEOUT_SECONDS)
    if server.shutdown_aborted.is_set():
        return
    if not finished:
        # A timeout is not permission to leave the overlay or gateway orphaned.
        # The existing cleanup worker still owns recovery and the instance lock.
        _app._write_shutdown_status(server, "shutdown-timeout", ["退出清理尚未完成，管理器继续等待安全恢复。"])
        return
    if finished:
        # Give WebView2 and the Python interpreter a brief opportunity to tear
        # down normally.  If any third-party non-daemon thread remains, force a
        # clean process boundary after all application state has been restored.
        _app.time.sleep(1.5)
    if server.shutdown_aborted.is_set():
        return
    result = getattr(server, "shutdown_result", {})
    if not (result.get("completed") and (result.get("restorationComplete") or result.get("preserved"))):
        return
    # Keep the completed/completed-with-errors status and restore evidence for
    # external maintenance helpers; do not replace it with an ambiguous phase.
    _app._cleanup_runtime()
    _app.os._exit(1 if getattr(server, "shutdown_errors", []) else 0)



def _restart_readiness_watchdog(server: _app.ManagerServer, handoff: dict | None) -> None:
    """Gracefully retire a replacement that never becomes UI-ready."""
    if not isinstance(handoff, dict):
        return
    try:
        deadline_epoch = float(handoff.get("readyDeadlineEpoch") or 0)
    except (TypeError, ValueError):
        return
    remaining = deadline_epoch - _app.time.time()
    if deadline_epoch <= 0 or remaining > _app.RESTART_HANDOFF_TIMEOUT_SECONDS:
        return
    if server.ui_ready.wait(max(0.0, remaining)):
        return
    if server.shutdown_started.is_set():
        return
    server.allow_forced_process_exit = False
    _app._write_shutdown_status(
        server,
        "quick-restart-replacement-not-ready",
        ["新管理器未在交接期限内完成窗口激活，已安全退出并交回旧管理器。"],
    )
    _app.request_application_exit_only(server)



def _schedule_restart_readiness_watchdog(
    server: _app.ManagerServer,
    handoff: dict | None,
) -> _app.threading.Thread | None:
    if not isinstance(handoff, dict) or not handoff.get("readyDeadlineEpoch"):
        return None
    worker = _app.threading.Thread(
        target=_app._restart_readiness_watchdog,
        args=(server, handoff),
        name="agent-manager-restart-readiness",
        daemon=True,
    )
    worker.start()
    return worker



def _stop_server_mutations(server: object) -> None:
    callback = getattr(server, "stop_accepting_mutations", None)
    if callable(callback):
        callback()



def _reopen_server_mutations(server: object) -> None:
    callback = getattr(server, "reopen_mutations", None)
    if callable(callback):
        callback()



def _wait_for_server_mutations(server: object) -> bool:
    callback = getattr(server, "wait_for_mutations", None)
    return True if not callable(callback) else bool(callback(_app.MUTATION_DRAIN_TIMEOUT_SECONDS))



def prepare_application_update(server):
    """Explicit one-click update continues even after navigating away from Settings."""
    service = server.runtime.get_app_updates()
    if not service._operation.acquire(blocking=False):
        raise _app.core.ManagerError("更新正在处理，请勿重复操作。")
    try:
        if not service.status().get("canInstall"):
            raise _app.core.ManagerError("请先成功检查、下载并校验更新，并使用 Windows EXE 版本安装。")
        from agent_manager.updates.installer import prepare_install, launch_install
        prepared = prepare_install(service, target=_app.sys.executable, state_dir=_app.core.STATE_DIR,
            shutdown_status=_app.SHUTDOWN_STATUS_FILE, source_pid=_app.os.getpid(),
            source_nonce=server.runtime_nonce, runtime_file=_app.RUNTIME_FILE)
        result = launch_install(prepared)
    except Exception:
        service._operation.release()
        raise
    try:
        service.monitor_installation(prepared["statusFile"], prepared["spec"]["installId"])
    except Exception:
        service._operation.release()
        raise
    # Let a triggering HTTP mutation finish before the guarded shutdown drains requests.
    timer = _app.threading.Timer(0.25, _app.request_application_shutdown, args=(server,))
    timer.daemon = True
    timer.start()
    return result



def request_application_shutdown(server: _app.ManagerServer) -> bool:
    """Start exactly one non-daemon cleanup worker for every exit source."""
    if not server.claim_shutdown():
        return False
    server.exit_only_requested = False
    worker = _app.threading.Thread(
        target=_app.shutdown_application,
        args=(server, True),
        name="codex-agent-manager-shutdown",
        daemon=False,
    )
    worker.start()
    return True



def request_application_restart(server: _app.ManagerServer) -> bool:
    """Restart only the manager process and preserve the running Codex session."""
    if not bool(server.runtime.configuration_session.get("active")):
        raise _app.core.ManagerError("当前处于安全修复模式；请先完成紧急修复，再执行快速重启。")
    if not server.claim_shutdown():
        return False
    token = _app.secrets.token_urlsafe(32)
    launch = None
    try:
        command, workdir = _app._manager_restart_command(token)
        source_build = _app._current_manager_build_fingerprint()
        target_build = (
            _app._manager_build_fingerprint(_app.Path(command[0]).resolve())
            if getattr(_app.sys, "frozen", False)
            else source_build
        )
        if not source_build or not target_build:
            raise _app.core.ManagerError("无法校验管理器程序版本，快速重启已取消。")
        _app._write_restart_handoff(
            token,
            {
                "sourceBuild": source_build,
                "targetBuild": target_build,
                "sourceOverlayCurrent": _app._runtime_overlay_matches_last_apply(),
                "preserveExternalSelection": _app.live_selection.preserve_live_configuration_on_restart(),
                "sourceRuntimeNonce": str(getattr(server, "runtime_nonce", "") or ""),
                "sourceStateFingerprint": str(server.runtime.configuration_session.get("configurationStateFingerprint") or ""),
            },
        )
        launch = _app._start_restarted_manager(
            token,
            command=command,
            workdir=workdir,
            source_pid=_app.os.getpid(),
            source_nonce=str(getattr(server, "runtime_nonce", "") or ""),
        )
        prepare = getattr(server.runtime, "prepare_for_restart", None)
        prepared = prepare() if callable(prepare) else {}
        staged = _app._wait_for_restart_child_staged(launch)
        handoff = _app._restart_handoff_payload(token)
    except Exception as exc:
        payload = _app._restart_handoff_payload(token)
        if payload is not None:
            _app.RESTART_HANDOFF_FILE.unlink(missing_ok=True)
        resume = getattr(server.runtime, "resume_after_failed_restart", None)
        if callable(resume):
            try:
                resume()
            except Exception:
                pass
        server.quick_restart_requested = False
        server.quick_restart_token = None
        if hasattr(server, "quick_restart_launch"):
            server.quick_restart_launch = None
        if hasattr(server, "quick_restart_handoff"):
            server.quick_restart_handoff = None
        server.force_exit = False
        with server.shutdown_lock:
            server.shutdown_started.clear()
        _app._reopen_server_mutations(server)
        _app._write_shutdown_status(server, "quick-restart-preflight-failed", [str(exc)[:300]])
        if isinstance(exc, _app.core.ManagerError):
            raise
        raise _app.core.ManagerError(f"新管理器预热失败：{str(exc)[:300]}") from exc
    server.quick_restart_requested = True
    server.quick_restart_token = token
    server.quick_restart_launch = launch
    server.quick_restart_handoff = handoff
    server.quick_restart_timing = {
        **({"prepareMs": prepared.get("elapsedMs")} if isinstance(prepared, dict) else {}),
        **staged,
        "buildChanged": not _app.secrets.compare_digest(source_build, target_build),
    }
    worker = _app.threading.Thread(
        target=_app.shutdown_application,
        args=(server, True, True),
        name="codex-agent-manager-quick-restart",
        daemon=False,
    )
    worker.start()
    return True



def _recover_unexpected_native_window_exit(server: _app.ManagerServer, error: object | None = None) -> bool:
    """Preserve Codex and hand the UI to a fresh manager after WebView2 disappears.

    ``webview.start`` returning is not proof that the user requested a full
    application shutdown.  WebView2 can terminate its event loop after a GPU,
    profile, or host-window failure.  The old path ran the full finalizer in
    that case, which restored the overlay and deliberately closed Codex.  Let
    an already-claimed shutdown finish, otherwise perform one quick-restart
    handoff. If that handoff cannot be started, the guarded finalizer owns
    recovery; an unexpected window failure is not a manager-only exit request.
    """
    shutdown_started = getattr(server, "shutdown_started", None)
    shutdown_finished = getattr(server, "shutdown_finished", None)
    shutdown_aborted = getattr(server, "shutdown_aborted", None)
    if isinstance(shutdown_started, _app.threading.Event) and shutdown_started.is_set():
        if isinstance(shutdown_finished, _app.threading.Event):
            shutdown_finished.wait(_app.NATIVE_WINDOW_RECOVERY_WAIT_SECONDS)
        if not isinstance(shutdown_aborted, _app.threading.Event) or not shutdown_aborted.is_set():
            # The claimed worker owns cleanup even if the bounded wait elapsed.
            # Running another finalizer concurrently is the dangerous case.
            return True

    configuration_session = getattr(getattr(server, "runtime", None), "configuration_session", {})
    if not isinstance(configuration_session, dict) or not configuration_session.get("active"):
        return False

    detail = str(error or "WebView2 事件循环意外结束")[:500]
    recovery_errors = [f"桌面窗口异常退出：{detail}"]
    if _app.os.environ.get(_app.NATIVE_WINDOW_RECOVERY_ENV) != "1":
        _app.os.environ[_app.NATIVE_WINDOW_RECOVERY_ENV] = "1"
        _app._write_shutdown_status(server, "native-window-restart", recovery_errors)
        try:
            started = _app.request_application_restart(server)
            if started:
                if isinstance(shutdown_finished, _app.threading.Event):
                    completed = shutdown_finished.wait(_app.NATIVE_WINDOW_RECOVERY_WAIT_SECONDS)
                else:
                    completed = True
                aborted = isinstance(shutdown_aborted, _app.threading.Event) and shutdown_aborted.is_set()
                if completed and not aborted:
                    return True
                if isinstance(shutdown_started, _app.threading.Event) and shutdown_started.is_set() and not aborted:
                    # A non-daemon restart worker is still in charge. Do not race
                    # it with the synchronous application finalizer.
                    return True
                recovery_errors.append("管理器自动重启未能完成，将执行安全退出与配置恢复。")
        except Exception as exc:
            recovery_errors.append(f"管理器自动重启失败：{str(exc)[:300]}")
    else:
        recovery_errors.append("WebView2 在自动恢复后的新进程中再次退出，已停止循环重启。")

    server.exit_only_requested = False
    server.native_window_lost = True
    _app._write_shutdown_status(server, "native-window-recovery-required", recovery_errors)
    _app._report_startup_error(
        "Agent Manager 桌面窗口异常退出且自动重启未完成；"
        "将尝试关闭 Codex 并恢复原始配置。恢复失败时保留管理服务与恢复记录。"
    )
    return False



def request_application_exit_only(server: _app.ManagerServer) -> bool:
    """Exit the manager without closing Codex or restoring its active overlay."""
    if not server.claim_shutdown():
        return False
    server.exit_only_requested = True
    _app.threading.Thread(
        target=_app.shutdown_application,
        args=(server,),
        kwargs={"claimed": True, "exit_only": True},
        name="agent-manager-exit-only",
        daemon=False,
    ).start()
    return True



def _handle_startup_activation_failure(server: _app.ManagerServer, phase: str, error: object) -> None:
    surface = "桌面窗口" if phase.startswith("native") else "浏览器窗口"
    configuration_session = getattr(getattr(server, "runtime", None), "configuration_session", {})
    if isinstance(configuration_session, dict) and configuration_session.get("active"):
        # The visible surface and configuration session are already usable.
        # A later readiness/discovery write failure must not turn an existing
        # Codex process into collateral damage through the full-exit path.
        message = (
            f"{surface}就绪状态发布失败；窗口与当前 Codex 会话保持可用："
            f"{str(error)[:700]}"
        )
        _app._write_shutdown_status(server, phase, [message])
        _app._report_startup_error(message)
        return
    message = f"{surface}初始化失败，已开始回滚 Codex 配置：{str(error)[:700]}"
    _app._write_shutdown_status(server, phase, [message])
    _app._report_startup_error(message)
    server.force_exit = True
    _app.request_application_shutdown(server)



def handle_native_window_closing(server: _app.ManagerServer, window: object) -> bool | None:
    if server.force_exit:
        started = getattr(server, "shutdown_started", None)
        if (
            isinstance(started, _app.threading.Event) and started.is_set()
            and not getattr(getattr(server, "runtime", None), "_closed", False)
        ):
            # Repeated title-bar clicks while cleanup is running must not
            # destroy the repair window before restoration has been verified.
            return False
        return None
    try:
        behavior = _app.core.load_settings().get("appBehavior", {})
    except Exception as exc:
        _app._write_shutdown_status(server, "close-settings-unavailable", [str(exc)[:300]])
        behavior = {}
    if behavior.get("closeToTray") and not server.force_exit:
        if server.ensure_tray():
            window.hide()
        return False
    if not server.force_exit:
        # Keep the window until guarded cleanup succeeds; a blocked restore
        # must leave a usable repair surface rather than a hidden process.
        _app.request_application_shutdown(server)
        return False
    return None



def shutdown_application(
    server: _app.ManagerServer,
    claimed: bool = False,
    quick_restart: bool = False,
    exit_only: bool = False,
) -> None:
    if not claimed and not server.claim_shutdown():
        return
    server.force_exit = True
    errors: list[str] = []
    server.shutdown_errors = errors
    server.shutdown_result = {}
    abort_shutdown = False
    blocked_phase = "blocked-restoration"
    _app._write_shutdown_status(server, "quick-restart-starting" if quick_restart else "starting")
    if server.allow_forced_process_exit:
        _app.threading.Thread(
            target=_app._forced_exit_watchdog,
            args=(server,),
            name="codex-agent-manager-exit-watchdog",
            daemon=True,
        ).start()
    try:
        try:
            server.stop_tray()
        except Exception as exc:
            errors.append(f"停止托盘：{str(exc)[:300]}")
        # Stop accepting new work before restoring the Codex files.  This also
        # releases browser-only serve_forever loops even if a later cleanup
        # operation fails.
        try:
            server.shutdown()
        except Exception as exc:
            errors.append(f"停止管理服务：{str(exc)[:300]}")
        if not _app._wait_for_server_mutations(server):
            errors.append(
                f"仍有修改请求在 {_app.MUTATION_DRAIN_TIMEOUT_SECONDS:g} 秒内未结束；"
                "为避免与 Codex 配置还原并发，已取消本次退出。"
            )
            abort_shutdown = True
            blocked_phase = "blocked-active-mutations"
        else:
            try:
                if quick_restart:
                    result = server.runtime.close_for_restart()
                elif exit_only:
                    result = server.runtime.close_for_restart(exit_only=True)
                else:
                    result = server.runtime.close()
                server.shutdown_result = result
                errors.extend(str(item) for item in result.get("errors", []) if str(item))
                if result.get("completed") is False:
                    abort_shutdown = True
                    if result.get("blocked") == "codex-or-workers":
                        blocked_phase = "blocked-codex-or-workers"
            except Exception as exc:
                action = (
                    "保留快速重启状态"
                    if quick_restart
                    else "保留 Codex 运行状态"
                    if exit_only
                    else "还原运行状态"
                )
                errors.append(f"{action}：{str(exc)[:300]}")
                abort_shutdown = True
        if server.native_window and not abort_shutdown and not quick_restart:
            try:
                import webview

                window = server.native_window_object
                if window is not None:
                    window.destroy()
                elif webview.windows:
                    webview.windows[0].destroy()
            except Exception as exc:
                errors.append(f"关闭桌面窗口：{str(exc)[:300]}")
    finally:
        recovered = False
        if abort_shutdown:
            if quick_restart:
                token = str(getattr(server, "quick_restart_token", "") or "")
                if _app._restart_handoff_payload(token) is not None:
                    _app.RESTART_HANDOFF_FILE.unlink(missing_ok=True)
                resume = getattr(server.runtime, "resume_after_failed_restart", None)
                if callable(resume):
                    resume()
                server.quick_restart_requested = False
                server.quick_restart_token = None
                server.quick_restart_launch = None
            server.force_exit = False
            server.shutdown_aborted.set()
            with server.shutdown_lock:
                server.shutdown_started.clear()
            _app._reopen_server_mutations(server)
            _app.threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.25},
                name="codex-agent-manager-server-recovery",
                daemon=False,
            ).start()
            if getattr(server, "native_window_lost", False):
                # A failed WebView event loop cannot host the repair UI. Reuse
                # browser mode and the same authenticated management service.
                server.native_window = False
                server.native_window_object = None
                try:
                    _app._write_runtime_discovery(server)
                    if not _app.open_browser_window(server.ui_url()):
                        errors.append("无法自动打开修复页面；请重新打开 Agent Manager 访问保留的服务。")
                except Exception as exc:
                    errors.append(f"打开修复页面：{str(exc)[:300]}")
            if server.native_window_object is not None:
                try:
                    server.native_window_object.show()
                except Exception:
                    pass
            server.ensure_tray()
            _app._write_shutdown_status(server, blocked_phase, errors)
            server.shutdown_finished.set()
            recovered = True
        elif quick_restart:
            launch = getattr(server, "quick_restart_launch", None)
            if isinstance(launch, dict):
                launch["offlineMonotonic"] = _app.time.monotonic()
            try:
                if isinstance(launch, dict):
                    server.quick_restart_handoff = _app._arm_restart_readiness_deadline(str(server.quick_restart_token or ""))
                _app._cleanup_runtime()
                replacement = (
                    _app._wait_for_restarted_manager(launch)
                    if isinstance(launch, dict)
                    else _app._launch_restarted_manager(str(server.quick_restart_token or ""))
                )
                if isinstance(replacement, dict):
                    timing = getattr(server, "quick_restart_timing", None)
                    if not isinstance(timing, dict):
                        timing = {}
                        server.quick_restart_timing = timing
                    timing.update(
                        {
                            key: replacement[key]
                            for key in ("readyMs", "offlineMs", "runtimePid", "activation")
                            if key in replacement
                        }
                    )
            except Exception as exc:
                _app.RESTART_HANDOFF_FILE.unlink(missing_ok=True)
                errors.append(f"重新启动管理器：{str(exc)[:300]}")
                try:
                    _app._cleanup_runtime()
                    acquired = _app._acquire_instance_mutex()
                    retry_deadline = _app.time.monotonic() + _app.RESTART_REPLACEMENT_EXIT_GRACE_SECONDS
                    while not acquired and _app.time.monotonic() < retry_deadline:
                        _app.time.sleep(_app.RESTART_POLL_INTERVAL_SECONDS)
                        acquired = _app._acquire_instance_mutex()
                    if not acquired:
                        raise _app.core.ManagerError("单实例锁已被其他管理器占用。")
                    server.runtime = _app.ManagerRuntime(
                        quick_restart=True,
                        restart_handoff=getattr(server, "quick_restart_handoff", None),
                    )
                    if hasattr(server.runtime, "set_radar_alert_notifier"):
                        server.runtime.set_radar_alert_notifier(server.notify_radar_alert)
                except Exception as recovery_exc:
                    errors.append(f"恢复旧管理器：{str(recovery_exc)[:300]}")
                else:
                    server.force_exit = False
                    server.quick_restart_requested = False
                    server.quick_restart_token = None
                    server.shutdown_aborted.set()
                    with server.shutdown_lock:
                        server.shutdown_started.clear()
                    _app._reopen_server_mutations(server)
                    _app.threading.Thread(
                        target=server.serve_forever,
                        kwargs={"poll_interval": 0.25},
                        name="codex-agent-manager-restart-recovery",
                        daemon=False,
                    ).start()
                    try:
                        _app._write_runtime_discovery(server)
                    except Exception as runtime_exc:
                        errors.append(f"恢复运行状态文件：{str(runtime_exc)[:300]}")
                    if server.native_window_object is not None:
                        try:
                            server.native_window_object.show()
                        except Exception:
                            pass
                    server.ensure_tray()
                    _app._write_shutdown_status(server, "quick-restart-rolled-back", errors)
                    server.shutdown_finished.set()
                    recovered = True
            if not recovered:
                try:
                    server.server_close()
                except Exception as exc:
                    errors.append(f"释放管理端口：{str(exc)[:300]}")
                if server.native_window and server.native_window_object is not None:
                    try:
                        server.native_window_object.destroy()
                    except Exception as exc:
                        errors.append(f"关闭旧桌面窗口：{str(exc)[:300]}")
        else:
            try:
                server.server_close()
            except Exception as exc:
                errors.append(f"释放管理端口：{str(exc)[:300]}")
            _app._cleanup_runtime()
        if not recovered:
            _app._write_shutdown_status(server, "completed-with-errors" if errors else "completed", errors)
            server.shutdown_finished.set()



def _finalize_application_server(
    server: _app.ManagerServer,
    server_thread: _app.threading.Thread | None = None,
) -> list[str]:
    """Use the same guarded cleanup when a native/browser server loop returns."""
    started = getattr(server, "shutdown_started", None)
    aborted = getattr(server, "shutdown_aborted", None)
    if isinstance(started, _app.threading.Event) and started.is_set():
        # Browser serve_forever returns as soon as the worker stops HTTP. It
        # must not concurrently restore files or release that worker's mutex.
        server.shutdown_finished.wait(_app.NATIVE_WINDOW_RECOVERY_WAIT_SECONDS)
        return list(getattr(server, "shutdown_errors", []))
    if isinstance(aborted, _app.threading.Event) and aborted.is_set():
        return list(getattr(server, "shutdown_errors", []))
    _app.shutdown_application(
        server,
        quick_restart=bool(getattr(server, "quick_restart_requested", False)),
        exit_only=bool(getattr(server, "exit_only_requested", False)),
    )
    if server_thread is not None and server_thread is not _app.threading.current_thread():
        server_thread.join(timeout=3)
    return list(getattr(server, "shutdown_errors", []))



def _create_startup_runtime(
    *,
    open_window: bool,
    quick_restart: bool,
    restart_handoff: dict | None,
) -> _app.ManagerRuntime:
    """Create a runtime while preserving a visible repair path on bootstrap errors."""
    bootstrap_error: Exception | None = None
    try:
        _app.core.ensure_state()
        import agent_manager.config.recovery
        agent_manager.config.recovery.normalize_encoding_if_needed()
    except Exception as exc:
        # A quick-restart child must fail before publishing readiness so the
        # still-live source manager can resume ownership of its overlay.
        if not open_window or quick_restart:
            raise
        bootstrap_error = exc
    runtime = _app.ManagerRuntime(
        quick_restart=quick_restart,
        defer_configuration=bool(open_window),
        restart_handoff=restart_handoff,
    )
    if bootstrap_error is not None:
        runtime.enter_startup_recovery(bootstrap_error)
    return runtime



def run_server(
    port: int,
    open_window: bool,
    native_window: bool,
    quick_restart_token: str | None = None,
) -> int:
    quick_restart = _app._consume_restart_handoff(quick_restart_token, delete=False)
    restart_handoff = _app._restart_handoff_payload(quick_restart_token) if quick_restart else None
    if restart_handoff is not None:
        try:
            restart_handoff = _app._mark_restart_child_staged(str(quick_restart_token or ""))
        except Exception:
            return 1
    acquired = _app._acquire_instance_mutex()
    if quick_restart and not acquired:
        # A prewarmed replacement deliberately reaches this point while the old
        # manager is still serving.  It waits for the parent to drain mutations
        # and release the mutex, and exits on cancellation without touching the
        # overlay or attempting to terminate either process.
        deadline = _app.time.monotonic() + _app.RESTART_MUTEX_WAIT_SECONDS
        while _app.time.monotonic() < deadline and not acquired:
            if restart_handoff is not None and not _app._restart_handoff_is_valid(quick_restart_token):
                break
            _app.time.sleep(_app.RESTART_POLL_INTERVAL_SECONDS)
            acquired = _app._acquire_instance_mutex()
    if not acquired:
        if quick_restart:
            return 1
        if open_window and not quick_restart:
            for _ in range(_app.EXISTING_INSTANCE_WAKE_ATTEMPTS):
                if _app.open_existing_runtime():
                    return 0
                _app.time.sleep(_app.EXISTING_INSTANCE_WAKE_INTERVAL_SECONDS)
        message = (
            "检测到另一个 Agent Manager 进程占用了单实例锁，但无法验证或唤醒它。"
            "请稍候重试；若持续出现，请在任务管理器结束残留的 AgentManager 进程后重新打开。"
        )
        if open_window:
            _app._report_startup_error(message)
        return 1
    if restart_handoff is not None:
        restart_handoff = _app._take_restart_handoff(quick_restart_token)
        if restart_handoff is None:
            _app._release_instance_mutex()
            return 1
    runtime = None
    server = None
    try:
        # When a user-facing window is requested, do not touch Codex until the
        # window loop has actually started.  This keeps startup failures
        # recoverable and prevents a headless half-start from closing Codex.
        runtime = _app._create_startup_runtime(
            open_window=open_window,
            quick_restart=quick_restart,
            restart_handoff=restart_handoff,
        )
        server = _app.ManagerServer(("127.0.0.1", port), _app.RequestHandler, runtime)
        if hasattr(runtime, "set_radar_alert_notifier"):
            runtime.set_radar_alert_notifier(server.notify_radar_alert)
        server.allow_forced_process_exit = True
        server.native_window = bool(open_window and native_window)
        _app._write_runtime_discovery(server)
        if quick_restart:
            _app._schedule_restart_readiness_watchdog(server, restart_handoff)
    except Exception as exc:
        if runtime is not None:
            try:
                runtime.close_for_restart() if quick_restart else runtime.close()
            except Exception:
                pass
        if server is not None:
            try:
                server.server_close()
            except Exception:
                pass
        _app._cleanup_runtime()
        if open_window:
            _app._report_startup_error(f"管理服务初始化失败；已尝试安全清理，未完成的配置恢复记录将保留：{str(exc)[:700]}")
        raise
    ui_url = server.ui_url()

    if not open_window:
        if _app.sys.stdout is not None:
            print(ui_url, flush=True)
        _app.threading.Thread(target=_app.idle_watchdog, args=(server,), daemon=True).start()
        _app._schedule_release_maintenance()
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            _app._finalize_application_server(server)
        return 1 if getattr(server, "shutdown_errors", []) else 0

    if native_window:
        server_thread = None
        try:
            import webview

            server_thread = _app.threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True)
            server_thread.start()
            _app._schedule_release_maintenance()
            window = webview.create_window(
                _app.core.APP_NAME,
                ui_url,
                width=1320,
                height=860,
                min_size=(940, 640),
                text_select=True,
                background_color="#111816",
            )
            server.native_window_object = window

            def handle_closing() -> bool | None:
                return _app.handle_native_window_closing(server, window)

            window.events.closing += handle_closing
            activation_attempted = _app.threading.Event()

            def activate_after_window_ready() -> None:
                # pywebview runs this callback only after the GUI event loop is
                # active.  Explicitly restore the window before touching Codex;
                # a persisted minimized WebView2 state must never produce a
                # headless manager that has already closed the user's Codex.
                activation_attempted.set()
                try:
                    from agent_manager.platform.window_theme import sync_server_appearance
                    sync_server_appearance(server)
                    if not server.show_native_window():
                        raise _app.core.ManagerError("Agent Manager 未检测到可见窗口，已取消应用运行配置。")
                    server.window_activation_requested.clear()
                    server.runtime.activate_configuration_session()
                    _app._mark_ui_ready(server)
                except Exception as exc:
                    _app._handle_startup_activation_failure(server, "native-window-activation-error", exc)
                    return
                try:
                    if _app.core.load_settings().get("appBehavior", {}).get("closeToTray"):
                        server.ensure_tray()
                except Exception:
                    # The window remains usable and can surface configuration
                    # errors through /api/state even if tray setup is unavailable.
                    pass

            webview.start(activate_after_window_ready, gui="edgechromium", private_mode=False)
            if not activation_attempted.is_set():
                message = "WebView2 事件循环在窗口就绪前退出，将执行安全清理与配置恢复。"
                _app._write_shutdown_status(server, "native-window-not-ready", [message])
                _app._report_startup_error(message)
                _app._finalize_application_server(server, server_thread)
                return 1
            if _app._recover_unexpected_native_window_exit(server):
                return 0
            _app._finalize_application_server(server, server_thread)
            return 1 if getattr(server, "shutdown_errors", []) else 0
        except Exception as exc:
            # Once the native path has started its own server loop, falling
            # through to the browser path would start serve_forever a second
            # time.  That was another way to leave a headless process behind.
            if server_thread is not None:
                if _app._recover_unexpected_native_window_exit(server, exc):
                    return 0
                _app._write_shutdown_status(server, "native-window-error", [str(exc)[:500]])
                if not bool(getattr(server, "exit_only_requested", False)):
                    _app._report_startup_error(f"桌面窗口启动失败，将尝试安全清理与配置恢复：{str(exc)[:700]}")
                _app._finalize_application_server(server, server_thread)
                return 1

    def open_browser_and_activate() -> None:
        opened = False
        try:
            opened = _app.open_browser_window(ui_url)
            if not opened:
                raise _app.core.ManagerError("系统未能打开 Agent Manager 浏览器窗口。")
            # Browser mode has no window-ready event.  Only activate when the
            # operating system accepted the browser launch request.
            server.runtime.activate_configuration_session()
            _app._mark_ui_ready(server)
        except Exception as exc:
            _app._handle_startup_activation_failure(server, "browser-window-activation-error", exc)

    browser_timer = _app.threading.Timer(0.25, open_browser_and_activate)
    browser_timer.daemon = True
    browser_timer.start()
    _app.threading.Thread(target=_app.idle_watchdog, args=(server,), daemon=True).start()
    _app._schedule_release_maintenance()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        _app._finalize_application_server(server)
    return 1 if getattr(server, "shutdown_errors", []) else 0



def main(argv: list[str] | None = None) -> int:
    raw_argv = list(_app.sys.argv[1:] if argv is None else argv)
    raw_argv, shell_handoff = _app._consume_shell_job_handoff(raw_argv)
    parser = _app.argparse.ArgumentParser(description=_app.__doc__)
    parser.add_argument("--version", action="version", version=_app.VERSION)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--browser", action="store_true", help="Use Edge/default browser instead of WebView2.")
    parser.add_argument("--quick-restart-token", help=_app.argparse.SUPPRESS)
    args = parser.parse_args(raw_argv)
    # A normal second click only needs to reveal the already independent live
    # instance. Do that before another PyInstaller Job handoff; otherwise a
    # harmless window activation incurs a second one-file extraction and can
    # look like a failed or frozen launch. Quick restarts and explicit CLI
    # modes still take the full lifecycle path.
    if not shell_handoff and not raw_argv and _app.open_existing_runtime():
        return 0
    try:
        if not shell_handoff and _app._relaunch_frozen_manager_outside_parent_job(raw_argv):
            return 0
    except Exception as exc:
        _app._report_startup_error(str(exc))
        return 1
    if not args.no_open and not (_app.STATIC_DIR / "index.html").is_file():
        _app._report_startup_error("界面文件缺失，请使用完整安装包；源码环境请先运行 npm run build --prefix frontend。")
        return 1
    if not args.browser and not _app.native_webview_available():
        args.browser = True
    return _app.run_server(
        args.port,
        not args.no_open,
        not args.browser,
        args.quick_restart_token,
    )

