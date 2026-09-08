"""Discovery services."""
from __future__ import annotations
from agent_manager import application as _app


def find_edge() -> _app.Path | None:
    candidates = []
    for key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        root = _app.os.environ.get(key)
        if root:
            candidates.append(_app.Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    return next((path for path in candidates if path.is_file()), None)



def open_browser_window(url: str) -> bool:
    edge = _app.find_edge()
    if edge:
        flags = (
            getattr(_app.subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(_app.subprocess, "DETACHED_PROCESS", 0)
            | getattr(_app.subprocess, "CREATE_NO_WINDOW", 0)
        )
        _app.subprocess.Popen(
            [str(edge), f"--app={url}", "--window-size=1320,860"],
            stdout=_app.subprocess.DEVNULL,
            stderr=_app.subprocess.DEVNULL,
            creationflags=flags if _app.os.name == "nt" else 0,
        )
        return True
    return bool(_app.webbrowser.open(url))



def open_external_browser(url: str) -> bool:
    """Open trusted third-party pages in the user's normal browser profile.

    Unlike ``open_browser_window`` this intentionally avoids Edge app mode:
    provider dashboards should reuse the user's existing browser login state,
    while Agent Manager never reads or stores those browser cookies.
    """

    return bool(_app.webbrowser.open_new_tab(url))



def _report_startup_error(message: str) -> None:
    detail = str(message or "Agent Manager 启动失败。")[:1200]
    try:
        _app.core.atomic_write_json(
            _app.SHUTDOWN_STATUS_FILE,
            {"phase": "startup-error", "at": _app.core.now_iso(), "errors": [detail], "native": True},
        )
    except Exception:
        pass
    if _app.os.name == "nt":
        try:
            _app.ctypes.windll.user32.MessageBoxW(None, detail, f"{_app.core.APP_NAME} 启动失败", 0x10 | 0x00040000)
            return
        except Exception:
            pass
    if _app.sys.stderr is not None:
        print(f"{_app.core.APP_NAME} 启动失败：{detail}", file=_app.sys.stderr, flush=True)



def _activation_token_hash(token: str) -> str:
    return _app.hashlib.sha256(str(token).encode("utf-8", errors="replace")).hexdigest()



def _pid_is_running(process_id: int) -> bool:
    if process_id <= 0:
        return False
    if process_id == _app.os.getpid():
        return True
    if _app.os.name == "nt":
        kernel32 = _app.ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [_app.wintypes.DWORD, _app.wintypes.BOOL, _app.wintypes.DWORD]
        kernel32.OpenProcess.restype = _app.wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [_app.wintypes.HANDLE, _app.ctypes.POINTER(_app.wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = _app.wintypes.BOOL
        kernel32.CloseHandle.argtypes = [_app.wintypes.HANDLE]
        kernel32.CloseHandle.restype = _app.wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, process_id)
        if not handle:
            return False
        try:
            exit_code = _app.wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, _app.ctypes.byref(exit_code))) and exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        _app.os.kill(process_id, 0)
    except (OSError, PermissionError):
        return False
    return True



def _runtime_identity_is_valid(
    runtime: object,
    health: object,
    *,
    require_ui_ready: bool = False,
    require_independent: bool = False,
) -> bool:
    if not isinstance(runtime, dict) or not isinstance(health, dict):
        return False
    try:
        runtime_pid = int(runtime.get("pid") or 0)
        port = int(runtime.get("port") or 0)
    except (TypeError, ValueError):
        return False
    activation_token = str(runtime.get("activationToken") or "")
    control_token = str(runtime.get("controlToken") or "")
    runtime_nonce = str(runtime.get("runtimeNonce") or "")
    if (
        runtime.get("appId") != _app.RUNTIME_APP_ID
        or runtime_pid <= 0
        or not 0 < port <= 65535
        or len(runtime_nonce) < 16
        or len(activation_token) < 16
        or (control_token and len(control_token) < 16)
        or not _app._pid_is_running(runtime_pid)
    ):
        return False
    try:
        health_pid = int(health.get("runtimePid") or 0)
    except (TypeError, ValueError):
        return False
    identity_matches = bool(
        health.get("ok") is True
        and health.get("appId") == _app.RUNTIME_APP_ID
        and health_pid == runtime_pid
        and _app.secrets.compare_digest(str(health.get("runtimeNonce") or ""), runtime_nonce)
        and _app.secrets.compare_digest(
            str(health.get("activationTokenHash") or ""),
            _app._activation_token_hash(activation_token),
        )
        and (
            not control_token
            or _app.secrets.compare_digest(
                str(health.get("controlTokenHash") or ""),
                _app._activation_token_hash(control_token),
            )
        )
    )
    if not identity_matches:
        return False
    if require_ui_ready and not (
        runtime.get("uiReady") is True and health.get("uiReady") is True
    ):
        return False
    if require_independent and not (
        runtime.get("independentLifecycle") is True
        and health.get("independentLifecycle") is True
    ):
        return False
    return True



def _probe_runtime_identity(
    runtime: object,
    timeout_seconds: float = 0.7,
    *,
    require_ui_ready: bool = False,
    require_independent: bool = False,
) -> bool:
    if not isinstance(runtime, dict):
        return False
    try:
        port = int(runtime.get("port") or 0)
        runtime_pid = int(runtime.get("pid") or 0)
        if (
            runtime.get("appId") != _app.RUNTIME_APP_ID
            or not 0 < port <= 65535
            or runtime_pid <= 0
            or len(str(runtime.get("runtimeNonce") or "")) < 16
            or len(str(runtime.get("activationToken") or "")) < 16
            or not _app._pid_is_running(runtime_pid)
        ):
            return False
        with _app.urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health",
            timeout=max(0.1, float(timeout_seconds)),
        ) as response:
            if response.status != 200:
                return False
            raw = response.read(65_537)
            if len(raw) > 65_536:
                return False
            health = _app.json.loads(raw.decode("utf-8"))
    except Exception:
        return False
    return _app._runtime_identity_is_valid(
        runtime,
        health,
        require_ui_ready=require_ui_ready,
        require_independent=require_independent,
    )



def focus_process_window(process_id: int) -> bool:
    if _app.os.name != "nt":
        return False
    found: list[tuple[int, int]] = []
    user32 = _app.ctypes.windll.user32

    class Rect(_app.ctypes.Structure):
        _fields_ = [
            ("left", _app.wintypes.LONG),
            ("top", _app.wintypes.LONG),
            ("right", _app.wintypes.LONG),
            ("bottom", _app.wintypes.LONG),
        ]

    def callback(hwnd: int, _lparam: int) -> bool:
        pid = _app.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, _app.ctypes.byref(pid))
        if pid.value == process_id and user32.GetWindowTextLengthW(hwnd) > 0:
            title_length = int(user32.GetWindowTextLengthW(hwnd))
            title_buffer = _app.ctypes.create_unicode_buffer(title_length + 1)
            user32.GetWindowTextW(hwnd, title_buffer, title_length + 1)
            rect = Rect()
            user32.GetWindowRect(hwnd, _app.ctypes.byref(rect))
            width = max(0, int(rect.right - rect.left))
            height = max(0, int(rect.bottom - rect.top))
            score = width * height
            if title_buffer.value.strip().casefold() == _app.core.APP_NAME.casefold():
                score += 100_000_000
            if user32.IsWindowVisible(hwnd):
                score += 10_000_000
            if not user32.GetWindow(hwnd, 4):  # GW_OWNER
                score += 1_000_000
            found.append((score, hwnd))
        return True

    enum_callback = _app.ctypes.WINFUNCTYPE(_app.wintypes.BOOL, _app.wintypes.HWND, _app.wintypes.LPARAM)(callback)
    user32.EnumWindows(enum_callback, 0)
    if not found:
        return False
    hwnd = max(found, key=lambda item: item[0])[1]
    user32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
    user32.ShowWindow(hwnd, 9)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    return True



def request_existing_window(runtime: dict) -> bool:
    """Ask the live instance to reveal its window via a narrow local token."""
    try:
        port = int(runtime["port"])
        token = str(runtime.get("activationToken") or "")
        if not token:
            return False
        request = _app.urllib.request.Request(
            f"http://127.0.0.1:{port}/api/window/show",
            data=b"{}",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Agent-Manager-Activation": token,
            },
        )
        with _app.urllib.request.urlopen(request, timeout=1.2) as response:
            raw = response.read(_app.MAX_EXISTING_INSTANCE_RESPONSE_BYTES + 1)
            status = response.status
        if len(raw) > _app.MAX_EXISTING_INSTANCE_RESPONSE_BYTES:
            return False
        payload = _app.json.loads(raw.decode("utf-8"))
        return status == 200 and bool(
            payload.get("shown") or payload.get("opened") or payload.get("queued")
        )
    except Exception:
        return False



def request_existing_quick_restart(runtime: dict | None = None) -> dict:
    """Use the runtime-file control capability to restart only the manager.

    This is intentionally narrower than the authenticated browser API. It is
    suitable for local maintenance automation because possession of the
    runtime-file token grants no account/configuration read or write access.
    """

    record = _app._read_runtime_file() if runtime is None else dict(runtime)
    token = str(record.get("controlToken") or "")
    if len(token) < 16:
        raise _app.core.ManagerError("当前 Agent Manager 版本尚未提供本机快速重启接口。")
    if not _app._probe_runtime_identity(
        record,
        require_ui_ready=True,
        require_independent=True,
    ):
        raise _app.core.ManagerError("无法验证正在运行的 Agent Manager，未执行快速重启。")
    try:
        port = int(record.get("port") or 0)
    except (TypeError, ValueError) as exc:
        raise _app.core.ManagerError("Agent Manager 本机控制端口无效。") from exc
    request = _app.urllib.request.Request(
        f"http://127.0.0.1:{port}{_app.CONTROL_QUICK_RESTART_PATH}",
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            _app.CONTROL_HEADER_NAME: token,
        },
    )
    try:
        with _app.urllib.request.urlopen(request, timeout=_app.RESTART_STAGE_TIMEOUT_SECONDS + 8) as response:
            raw = response.read(_app.MAX_EXISTING_INSTANCE_RESPONSE_BYTES + 1)
            status = int(response.status)
    except _app.urllib.error.HTTPError as exc:
        try:
            detail = _app.json.loads(exc.read(_app.MAX_EXISTING_INSTANCE_RESPONSE_BYTES).decode("utf-8"))
        except Exception:
            detail = {}
        finally:
            exc.close()
        raise _app.core.ManagerError(str(detail.get("error") or f"快速重启请求失败：HTTP {exc.code}")) from exc
    except (OSError, _app.urllib.error.URLError, TimeoutError) as exc:
        raise _app.core.ManagerError("无法连接正在运行的 Agent Manager 快速重启接口。") from exc
    if len(raw) > _app.MAX_EXISTING_INSTANCE_RESPONSE_BYTES:
        raise _app.core.ManagerError("快速重启接口返回的数据异常过大。")
    try:
        payload = _app.json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, _app.json.JSONDecodeError) as exc:
        raise _app.core.ManagerError("快速重启接口返回了无效数据。") from exc
    if status != 200 or not isinstance(payload, dict) or payload.get("ok") is not True:
        raise _app.core.ManagerError(str((payload or {}).get("error") or "快速重启请求未被接受。"))
    return payload



def _read_runtime_file() -> dict:
    """Read the small single-instance handoff file without unbounded I/O."""
    with _app.RUNTIME_FILE.open("rb") as stream:
        raw = stream.read(_app.MAX_RUNTIME_FILE_BYTES + 1)
    if len(raw) > _app.MAX_RUNTIME_FILE_BYTES:
        raise _app.core.ManagerError("Agent Manager 运行状态文件异常过大，已拒绝读取。")
    try:
        payload = _app.json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, _app.json.JSONDecodeError) as exc:
        raise _app.core.ManagerError("Agent Manager 运行状态文件格式无效。") from exc
    if not isinstance(payload, dict):
        raise _app.core.ManagerError("Agent Manager 运行状态文件结构无效。")
    return payload

