"""Processes services."""
from __future__ import annotations
from agent_manager import application as _app


def _quick_restart_token_from_argv(argv: list[str]) -> str:
    prefix = "--quick-restart-token="
    for item in argv:
        if item.startswith(prefix):
            return item[len(prefix) :].strip()
    return ""



def _bind_exclusive_loopback(server: _app.ThreadingHTTPServer) -> None:
    """Bind a loopback listener without Windows' address-sharing semantics."""
    if _app.os.name == "nt" and hasattr(_app.socket, "SO_EXCLUSIVEADDRUSE"):
        server.socket.setsockopt(_app.socket.SOL_SOCKET, _app.socket.SO_EXCLUSIVEADDRUSE, 1)
    _app.ThreadingHTTPServer.server_bind(server)



def _current_process_is_in_windows_job() -> bool:
    """Return whether this process belongs to a Windows Job object."""
    if _app.os.name != "nt":
        return False
    kernel32 = _app.ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = _app.wintypes.HANDLE
    kernel32.IsProcessInJob.argtypes = [_app.wintypes.HANDLE, _app.wintypes.HANDLE, _app.ctypes.POINTER(_app.wintypes.BOOL)]
    kernel32.IsProcessInJob.restype = _app.wintypes.BOOL
    in_job = _app.wintypes.BOOL()
    if not kernel32.IsProcessInJob(kernel32.GetCurrentProcess(), None, _app.ctypes.byref(in_job)):
        raise OSError(_app.ctypes.get_last_error(), "IsProcessInJob failed")
    return bool(in_job.value)



def _relaunch_frozen_manager_outside_parent_job(argv: list[str]) -> bool:
    """Move the packaged manager out of a parent app's kill-on-close Job.

    Agent Manager is often opened from a Codex terminal or tool call. Windows
    then places it in the same Job object even after the immediate shell parent
    exits. Closing Codex can consequently kill the manager without running any
    cleanup. A one-time CREATE_BREAKAWAY_FROM_JOB handoff gives the manager an
    independent lifecycle before it acquires its mutex or touches Codex files.
    """
    pass # shared state is explicitly qualified
    if _app.os.name != "nt" or not getattr(_app.sys, "frozen", False):
        _app._WINDOWS_JOB_ISOLATION_STATUS = "not_required"
        return False
    # A quick-restart child may safely inherit the already isolated manager
    # lifecycle.  Bind this exception to the private, short-lived handoff
    # token so a user-controlled environment variable cannot bypass the
    # normal Windows Job isolation path.
    trusted_restart = _app.os.environ.pop(_app.TRUSTED_RESTART_ENV, None) == "1"
    restart_token = _app._quick_restart_token_from_argv(argv)
    if trusted_restart and restart_token and _app._restart_handoff_is_valid(restart_token):
        _app.os.environ.pop(_app.JOB_BREAKAWAY_ENV, None)
        _app._WINDOWS_JOB_ISOLATION_STATUS = "restart_inherited"
        return False
    # CREATE_BREAKAWAY_FROM_JOB either fails CreateProcess or creates the child
    # outside the source Job. Windows or the PyInstaller bootloader may then
    # assign that child to a different Job, so a second IsProcessInJob probe
    # cannot be used to infer that breakaway failed.
    if _app.os.environ.get(_app.JOB_BREAKAWAY_ENV) == "1":
        _app.os.environ.pop(_app.JOB_BREAKAWAY_ENV, None)
        _app._WINDOWS_JOB_ISOLATION_STATUS = "breakaway_completed"
        return False
    if not _app._current_process_is_in_windows_job():
        _app.os.environ.pop(_app.JOB_BREAKAWAY_ENV, None)
        _app._WINDOWS_JOB_ISOLATION_STATUS = "already_independent"
        return False
    launch_environment = _app.os.environ.copy()
    launch_environment[_app.JOB_BREAKAWAY_ENV] = "1"
    # A PyInstaller one-file child must unpack independently; otherwise the
    # short-lived attached bootloader can delete the new process's resources.
    launch_environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    creation_flags = (
        getattr(_app.subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
        | getattr(_app.subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(_app.subprocess, "DETACHED_PROCESS", 0)
    )
    try:
        _app.subprocess.Popen(
            [_app.sys.executable, *argv],
            cwd=str(_app.Path(_app.sys.executable).resolve().parent),
            env=launch_environment,
            stdin=_app.subprocess.DEVNULL,
            stdout=_app.subprocess.DEVNULL,
            stderr=_app.subprocess.DEVNULL,
            close_fds=True,
            creationflags=creation_flags,
        )
    except OSError as exc:
        # Some Codex/enterprise Jobs do not grant CREATE_BREAKAWAY_FROM_JOB.
        # Ask the already-independent Windows shell to perform one activation
        # instead of making the app appear to flash and disappear.  The child
        # carries a private one-shot marker so this fallback cannot recurse.
        parameters = _app.subprocess.list2cmdline([*argv, _app.JOB_SHELL_HANDOFF_ARG])
        previous_reset_environment = _app.os.environ.get("PYINSTALLER_RESET_ENVIRONMENT")
        _app.os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        try:
            try:
                shell_result = int(
                    _app.ctypes.windll.shell32.ShellExecuteW(
                        None,
                        "open",
                        str(_app.Path(_app.sys.executable).resolve()),
                        parameters,
                        str(_app.Path(_app.sys.executable).resolve().parent),
                        1,
                    )
                )
            except Exception:
                shell_result = 0
        finally:
            if previous_reset_environment is None:
                _app.os.environ.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
            else:
                _app.os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = previous_reset_environment
        if shell_result > 32:
            _app._WINDOWS_JOB_ISOLATION_STATUS = "shell_handoff_started"
            return True
        raise _app.core.ManagerError(
            "Agent Manager 无法脱离父应用的 Windows Job；为避免关闭 Codex 时被连带终止，"
            "系统 Shell 兜底也未能启动。本次没有继续运行，请从资源管理器重新打开。"
        ) from exc
    _app._WINDOWS_JOB_ISOLATION_STATUS = "handoff_started"
    return True



def _consume_shell_job_handoff(argv: list[str]) -> tuple[list[str], bool]:
    """Consume the private Shell fallback marker and verify the new process."""
    pass # shared state is explicitly qualified
    if _app.JOB_SHELL_HANDOFF_ARG not in argv:
        return list(argv), False
    cleaned = [item for item in argv if item != _app.JOB_SHELL_HANDOFF_ARG]
    _app.os.environ.pop(_app.JOB_BREAKAWAY_ENV, None)
    try:
        independent = not _app._current_process_is_in_windows_job()
    except OSError:
        independent = False
    _app._WINDOWS_JOB_ISOLATION_STATUS = (
        "shell_handoff_completed" if independent else "shell_handoff_unverified"
    )
    return cleaned, True



def _manager_lifecycle_is_independent() -> bool:
    if _app.os.name != "nt":
        return True
    if _app._WINDOWS_JOB_ISOLATION_STATUS in {
        "already_independent",
        "breakaway_completed",
        "restart_inherited",
        "shell_handoff_completed",
        "not_required",
    }:
        return True
    try:
        return not _app._current_process_is_in_windows_job()
    except OSError:
        return False



def _close_codex_processes_safely(timeout_seconds: float | None = None) -> dict:
    """Close Codex only when the packaged manager cannot be killed with it."""
    if (
        _app.os.name == "nt"
        and getattr(_app.sys, "frozen", False)
        and not _app._manager_lifecycle_is_independent()
    ):
        raise _app.core.ManagerError(
            "Agent Manager 当前未与 Codex 完成进程生命周期隔离；"
            "为避免重启 Codex 时连带关闭管理器，本次操作已取消。"
            "请从资源管理器重新打开 AgentManager.exe 后重试。"
        )
    if timeout_seconds is None:
        return _app.core.close_codex_processes()
    return _app.core.close_codex_processes(timeout_seconds=timeout_seconds)



class OAuthCallbackServer(_app.ThreadingHTTPServer):
    """Small, bounded OAuth callback listener hardened against slow peers."""

    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]):
        self._thread_slots = _app.threading.BoundedSemaphore(_app.MAX_OAUTH_CALLBACK_THREADS)
        super().__init__(address, handler)

    def server_bind(self) -> None:
        _app._bind_exclusive_loopback(self)

    def process_request(self, request: object, client_address: object) -> None:
        if not self._thread_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._thread_slots.release()
            raise

    def process_request_thread(self, request: object, client_address: object) -> None:
        try:
            if hasattr(request, "settimeout"):
                request.settimeout(_app.OAUTH_CALLBACK_CLIENT_TIMEOUT_SECONDS)
            super().process_request_thread(request, client_address)
        finally:
            self._thread_slots.release()



def _release_executable_name(path: _app.Path) -> bool:
    return bool(
        _app.re.fullmatch(
            r"(?:AgentManager|CodexAgentManager)(?:-[0-9][A-Za-z0-9_.-]*)?\.exe",
            path.name,
            _app.re.IGNORECASE,
        )
    )



def _promote_and_cleanup_release_executable(retry_seconds: float = 8.0) -> dict:
    """Promote a locked-build fallback to the canonical portable filename.

    Windows cannot overwrite a running canonical EXE.  The build therefore
    may stage ``AgentManager-<version>.exe`` and quick-restart into it.  Once
    the old process has released the file lock, the new process copies its
    exact bytes to ``AgentManager.exe``.  A later canonical start removes the
    no-longer-running fallback, leaving one distributable again.
    """
    if not getattr(_app.sys, "frozen", False):
        return {"promoted": False, "cleaned": [], "reason": "not_frozen"}
    current = _app.Path(_app.sys.executable).resolve()
    if not _app._release_executable_name(current):
        return {"promoted": False, "cleaned": [], "reason": "unmanaged_name"}
    canonical = current.with_name("AgentManager.exe")
    release_deadline = _app.time.monotonic() + max(0.1, min(float(retry_seconds), 150.0))
    promoted = False
    errors: list[str] = []
    if current.name.casefold() != canonical.name.casefold():
        temporary = canonical.with_name(f".{canonical.name}.{_app.os.getpid()}.updating")
        try:
            _app.shutil.copy2(current, temporary)
            if temporary.stat().st_size != current.stat().st_size:
                raise OSError("复制后的文件大小不一致")
            if _app.core._hash_file(temporary) != _app.core._hash_file(current):
                raise OSError("复制后的程序校验值不一致")
            # The old PyInstaller bootloader can outlive its Python child for
            # a brief moment after that child releases the instance mutex. In
            # that interval Windows still locks AgentManager.exe. Keep the
            # already-verified staging copy and retry only the atomic replace;
            # this avoids both a stale canonical shortcut and repeated 38 MB
            # copies while keeping startup delay strictly bounded.
            deadline = release_deadline
            while True:
                try:
                    _app.os.replace(temporary, canonical)
                    promoted = True
                    break
                except OSError:
                    if _app.time.monotonic() >= deadline:
                        raise
                    _app.time.sleep(0.1)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            errors.append(f"晋升主程序：{str(exc)[:260]}")
    cleaned: list[str] = []
    # A running versioned executable cannot delete itself.  The canonical
    # process on the next handoff/start performs this bounded cleanup.
    if current.name.casefold() == canonical.name.casefold():
        for pattern in ("AgentManager-*.exe", "CodexAgentManager*.exe"):
            for candidate in current.parent.glob(pattern):
                try:
                    resolved = candidate.resolve()
                    if resolved == current or not _app._release_executable_name(resolved):
                        continue
                    cleanup_deadline = release_deadline
                    while True:
                        try:
                            candidate.unlink()
                            break
                        except OSError:
                            if _app.time.monotonic() >= cleanup_deadline:
                                raise
                            _app.time.sleep(0.1)
                    cleaned.append(candidate.name)
                except Exception as exc:
                    errors.append(f"清理 {candidate.name}：{str(exc)[:180]}")
    manifest_updated = False
    if canonical.is_file():
        try:
            digest = _app.hashlib.sha256()
            with canonical.open("rb") as executable:
                for chunk in iter(lambda: executable.read(1024 * 1024), b""):
                    digest.update(chunk)
            _app.core.atomic_write_text(
                canonical.with_name("SHA256.txt"),
                f"{digest.hexdigest().upper()}  {canonical.name}\n",
            )
            manifest_updated = True
        except Exception as exc:
            errors.append(f"更新校验清单：{str(exc)[:180]}")
    return {
        "promoted": promoted,
        "canonical": str(canonical),
        "cleaned": cleaned,
        "manifestUpdated": manifest_updated,
        "errors": errors,
    }



def _schedule_release_maintenance(delay_seconds: float = 0.25) -> _app.threading.Thread | None:
    """Promote/clean release files only after the health server can answer.

    During quick restart the old manager deliberately stays alive until the
    new process passes its authenticated health handshake.  Its PyInstaller
    bootloader keeps the old EXE locked for that same interval.  Running the
    promotion synchronously before ``serve_forever`` would therefore create a
    circular wait.  The daemon starts after the server loop is scheduled, so
    the old process can exit and release the canonical file first.
    """
    if not getattr(_app.sys, "frozen", False):
        return None

    def maintain() -> None:
        _app.time.sleep(max(0.0, min(float(delay_seconds), 2.0)))
        try:
            # The old process may wait for WebView/UI readiness before it
            # exits. Keep replacement in this daemon, but cover the complete
            # handoff deadline instead of giving up after eight seconds.
            result = _app._promote_and_cleanup_release_executable(_app.RESTART_HANDOFF_TIMEOUT_SECONDS + 15)
        except Exception as exc:
            result = {"promoted": False, "errors": [_app.core._redact_sensitive_text(exc, limit=300)]}
        result["checkedAt"] = _app.core.now_iso()
        try:
            _app.core.atomic_write_json(_app.core.STATE_DIR / "last-release-maintenance.json", result)
        except OSError:
            pass

    worker = _app.threading.Thread(
        target=maintain,
        name="agent-manager-release-maintenance",
        daemon=True,
    )
    worker.start()
    return worker



def _instance_mutex_name() -> str:
    scope = str(_app.core.STATE_DIR).casefold().encode("utf-8", errors="replace")
    digest = _app.hashlib.sha256(scope).hexdigest()[:20]
    return f"Local\\CodexAgentManager-{digest}"



def _acquire_instance_mutex() -> bool:
    """Hold a named kernel object before creating any runtime overlay."""
    pass # shared state is explicitly qualified
    if _app._INSTANCE_MUTEX_HANDLE is not None:
        return True
    if _app.os.name != "nt":
        return True
    kernel32 = _app.ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [_app.ctypes.c_void_p, _app.wintypes.BOOL, _app.wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = _app.wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [_app.wintypes.HANDLE]
    kernel32.CloseHandle.restype = _app.wintypes.BOOL
    _app.ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, _app._instance_mutex_name())
    if not handle:
        raise OSError(_app.ctypes.get_last_error(), "无法创建管理器单实例互斥锁")
    if _app.ctypes.get_last_error() == _app.ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _app._INSTANCE_MUTEX_HANDLE = int(handle)
    _app._INSTANCE_MUTEX_KERNEL32 = kernel32
    return True



def _release_instance_mutex() -> None:
    pass # shared state is explicitly qualified
    handle = _app._INSTANCE_MUTEX_HANDLE
    kernel32 = _app._INSTANCE_MUTEX_KERNEL32
    _app._INSTANCE_MUTEX_HANDLE = None
    _app._INSTANCE_MUTEX_KERNEL32 = None
    if handle is not None and kernel32 is not None:
        try:
            kernel32.CloseHandle(_app.wintypes.HANDLE(handle))
        except Exception:
            pass

