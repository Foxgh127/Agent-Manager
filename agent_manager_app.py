#!/usr/bin/env python3
"""Desktop and local web application for Agent Manager."""

from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
from urllib.parse import unquote, urlparse
import urllib.request
import webbrowser

import agent_manager_core as core
import app_update_service as app_updates
import claude_manager_service as claude
import codex_live_selection as live_selection
import codex_maintenance_service as maintenance
import history_sync_service as history_sync
import radar_service as radar
import recovery_service as recovery
import recovery_coordinator
import relay_portal_service as relay_portal
import toolbox_service as toolbox
from web2api_service import Web2APIManager


RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
STATIC_DIR = RESOURCE_ROOT / "gui" / "dist"
RUNTIME_FILE = core.STATE_DIR / "app-runtime.json"
SHUTDOWN_STATUS_FILE = core.STATE_DIR / "last-app-shutdown.json"
RESTART_HANDOFF_FILE = core.STATE_DIR / "app-restart-handoff.json"
# Keep a larger ceiling for the bounded account-batch import format, while
# using a much smaller default for ordinary control-plane JSON.  A local UI
# is still an HTTP server: an authenticated but compromised browser should
# not be able to reserve tens of megabytes across every worker thread.
MAX_BODY_BYTES = 32_000_000
DEFAULT_JSON_BODY_BYTES = 8_000_000
MAX_IMPORT_BODY_BYTES = 24_000_000
MAX_CONFIG_BODY_BYTES = 2_000_000
MAX_RESPONSE_BYTES = 32_000_000
MAX_EXISTING_INSTANCE_RESPONSE_BYTES = 65_536
MAX_RUNTIME_FILE_BYTES = 65_536
MAX_MANAGEMENT_THREADS = 32
MANAGEMENT_CLIENT_TIMEOUT_SECONDS = 15.0
MAX_OAUTH_CALLBACK_THREADS = 8
OAUTH_CALLBACK_CLIENT_TIMEOUT_SECONDS = 10.0
FORCED_EXIT_TIMEOUT_SECONDS = 45.0
MUTATION_DRAIN_TIMEOUT_SECONDS = 15.0
RESTART_HANDOFF_TIMEOUT_SECONDS = 120.0
RESTART_STAGE_TIMEOUT_SECONDS = 20.0
RESTART_REPLACEMENT_READY_SECONDS = 20.0
RESTART_REPLACEMENT_EXIT_GRACE_SECONDS = 4.0
RESTART_MUTEX_WAIT_SECONDS = 30.0
RESTART_POLL_INTERVAL_SECONDS = 0.05
NATIVE_WINDOW_RECOVERY_WAIT_SECONDS = 35.0
OAUTH_LOGIN_TIMEOUT_SECONDS = 10 * 60
UI_BOOTSTRAP_TOKEN_TTL_SECONDS = 60.0
UI_BOOTSTRAP_MAX_EXCHANGES = 3
EXISTING_INSTANCE_WAKE_ATTEMPTS = 40
EXISTING_INSTANCE_WAKE_INTERVAL_SECONDS = 0.2
ERROR_ALREADY_EXISTS = 183
RUNTIME_APP_ID = "openai-agent-manager"
CONTROL_HEADER_NAME = "X-Agent-Manager-Control"
CONTROL_QUICK_RESTART_PATH = "/api/control/quick-restart"
JOB_BREAKAWAY_ENV = "AGENT_MANAGER_JOB_BREAKAWAY_ATTEMPT"
JOB_SHELL_HANDOFF_ARG = "--agent-manager-shell-handoff"
TRUSTED_RESTART_ENV = "AGENT_MANAGER_TRUSTED_RESTART"
NATIVE_WINDOW_RECOVERY_ENV = "AGENT_MANAGER_NATIVE_WINDOW_RECOVERY"
_WINDOWS_JOB_ISOLATION_STATUS = "unknown"
_INSTANCE_MUTEX_HANDLE: int | None = None
_INSTANCE_MUTEX_KERNEL32: object | None = None


def _quick_restart_token_from_argv(argv: list[str]) -> str:
    prefix = "--quick-restart-token="
    for item in argv:
        if item.startswith(prefix):
            return item[len(prefix) :].strip()
    return ""


def _bind_exclusive_loopback(server: ThreadingHTTPServer) -> None:
    """Bind a loopback listener without Windows' address-sharing semantics."""
    if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    ThreadingHTTPServer.server_bind(server)


def _current_process_is_in_windows_job() -> bool:
    """Return whether this process belongs to a Windows Job object."""
    if os.name != "nt":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    in_job = wintypes.BOOL()
    if not kernel32.IsProcessInJob(kernel32.GetCurrentProcess(), None, ctypes.byref(in_job)):
        raise OSError(ctypes.get_last_error(), "IsProcessInJob failed")
    return bool(in_job.value)


def _relaunch_frozen_manager_outside_parent_job(argv: list[str]) -> bool:
    """Move the packaged manager out of a parent app's kill-on-close Job.

    Agent Manager is often opened from a Codex terminal or tool call. Windows
    then places it in the same Job object even after the immediate shell parent
    exits. Closing Codex can consequently kill the manager without running any
    cleanup. A one-time CREATE_BREAKAWAY_FROM_JOB handoff gives the manager an
    independent lifecycle before it acquires its mutex or touches Codex files.
    """
    global _WINDOWS_JOB_ISOLATION_STATUS
    if os.name != "nt" or not getattr(sys, "frozen", False):
        _WINDOWS_JOB_ISOLATION_STATUS = "not_required"
        return False
    # A quick-restart child may safely inherit the already isolated manager
    # lifecycle.  Bind this exception to the private, short-lived handoff
    # token so a user-controlled environment variable cannot bypass the
    # normal Windows Job isolation path.
    trusted_restart = os.environ.pop(TRUSTED_RESTART_ENV, None) == "1"
    restart_token = _quick_restart_token_from_argv(argv)
    if trusted_restart and restart_token and _restart_handoff_is_valid(restart_token):
        os.environ.pop(JOB_BREAKAWAY_ENV, None)
        _WINDOWS_JOB_ISOLATION_STATUS = "restart_inherited"
        return False
    # CREATE_BREAKAWAY_FROM_JOB either fails CreateProcess or creates the child
    # outside the source Job. Windows or the PyInstaller bootloader may then
    # assign that child to a different Job, so a second IsProcessInJob probe
    # cannot be used to infer that breakaway failed.
    if os.environ.get(JOB_BREAKAWAY_ENV) == "1":
        os.environ.pop(JOB_BREAKAWAY_ENV, None)
        _WINDOWS_JOB_ISOLATION_STATUS = "breakaway_completed"
        return False
    if not _current_process_is_in_windows_job():
        os.environ.pop(JOB_BREAKAWAY_ENV, None)
        _WINDOWS_JOB_ISOLATION_STATUS = "already_independent"
        return False
    launch_environment = os.environ.copy()
    launch_environment[JOB_BREAKAWAY_ENV] = "1"
    # A PyInstaller one-file child must unpack independently; otherwise the
    # short-lived attached bootloader can delete the new process's resources.
    launch_environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    creation_flags = (
        getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
    )
    try:
        subprocess.Popen(
            [sys.executable, *argv],
            cwd=str(Path(sys.executable).resolve().parent),
            env=launch_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creation_flags,
        )
    except OSError as exc:
        # Some Codex/enterprise Jobs do not grant CREATE_BREAKAWAY_FROM_JOB.
        # Ask the already-independent Windows shell to perform one activation
        # instead of making the app appear to flash and disappear.  The child
        # carries a private one-shot marker so this fallback cannot recurse.
        parameters = subprocess.list2cmdline([*argv, JOB_SHELL_HANDOFF_ARG])
        previous_reset_environment = os.environ.get("PYINSTALLER_RESET_ENVIRONMENT")
        os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        try:
            try:
                shell_result = int(
                    ctypes.windll.shell32.ShellExecuteW(
                        None,
                        "open",
                        str(Path(sys.executable).resolve()),
                        parameters,
                        str(Path(sys.executable).resolve().parent),
                        1,
                    )
                )
            except Exception:
                shell_result = 0
        finally:
            if previous_reset_environment is None:
                os.environ.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
            else:
                os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = previous_reset_environment
        if shell_result > 32:
            _WINDOWS_JOB_ISOLATION_STATUS = "shell_handoff_started"
            return True
        raise core.ManagerError(
            "Agent Manager 无法脱离父应用的 Windows Job；为避免关闭 Codex 时被连带终止，"
            "系统 Shell 兜底也未能启动。本次没有继续运行，请从资源管理器重新打开。"
        ) from exc
    _WINDOWS_JOB_ISOLATION_STATUS = "handoff_started"
    return True


def _consume_shell_job_handoff(argv: list[str]) -> tuple[list[str], bool]:
    """Consume the private Shell fallback marker and verify the new process."""
    global _WINDOWS_JOB_ISOLATION_STATUS
    if JOB_SHELL_HANDOFF_ARG not in argv:
        return list(argv), False
    cleaned = [item for item in argv if item != JOB_SHELL_HANDOFF_ARG]
    os.environ.pop(JOB_BREAKAWAY_ENV, None)
    try:
        independent = not _current_process_is_in_windows_job()
    except OSError:
        independent = False
    _WINDOWS_JOB_ISOLATION_STATUS = (
        "shell_handoff_completed" if independent else "shell_handoff_unverified"
    )
    return cleaned, True


def _manager_lifecycle_is_independent() -> bool:
    if os.name != "nt":
        return True
    if _WINDOWS_JOB_ISOLATION_STATUS in {
        "already_independent",
        "breakaway_completed",
        "restart_inherited",
        "shell_handoff_completed",
        "not_required",
    }:
        return True
    try:
        return not _current_process_is_in_windows_job()
    except OSError:
        return False


def _close_codex_processes_safely(timeout_seconds: float | None = None) -> dict:
    """Close Codex only when the packaged manager cannot be killed with it."""
    if (
        os.name == "nt"
        and getattr(sys, "frozen", False)
        and not _manager_lifecycle_is_independent()
    ):
        raise core.ManagerError(
            "Agent Manager 当前未与 Codex 完成进程生命周期隔离；"
            "为避免重启 Codex 时连带关闭管理器，本次操作已取消。"
            "请从资源管理器重新打开 AgentManager.exe 后重试。"
        )
    if timeout_seconds is None:
        return core.close_codex_processes()
    return core.close_codex_processes(timeout_seconds=timeout_seconds)


class OAuthCallbackServer(ThreadingHTTPServer):
    """Small, bounded OAuth callback listener hardened against slow peers."""

    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]):
        self._thread_slots = threading.BoundedSemaphore(MAX_OAUTH_CALLBACK_THREADS)
        super().__init__(address, handler)

    def server_bind(self) -> None:
        _bind_exclusive_loopback(self)

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
                request.settimeout(OAUTH_CALLBACK_CLIENT_TIMEOUT_SECONDS)
            super().process_request_thread(request, client_address)
        finally:
            self._thread_slots.release()


def _release_executable_name(path: Path) -> bool:
    return bool(
        re.fullmatch(
            r"(?:AgentManager|CodexAgentManager)(?:-[0-9][A-Za-z0-9_.-]*)?\.exe",
            path.name,
            re.IGNORECASE,
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
    if not getattr(sys, "frozen", False):
        return {"promoted": False, "cleaned": [], "reason": "not_frozen"}
    current = Path(sys.executable).resolve()
    if not _release_executable_name(current):
        return {"promoted": False, "cleaned": [], "reason": "unmanaged_name"}
    canonical = current.with_name("AgentManager.exe")
    release_deadline = time.monotonic() + max(0.1, min(float(retry_seconds), 150.0))
    promoted = False
    errors: list[str] = []
    if current.name.casefold() != canonical.name.casefold():
        temporary = canonical.with_name(f".{canonical.name}.{os.getpid()}.updating")
        try:
            shutil.copy2(current, temporary)
            if temporary.stat().st_size != current.stat().st_size:
                raise OSError("复制后的文件大小不一致")
            if core._hash_file(temporary) != core._hash_file(current):
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
                    os.replace(temporary, canonical)
                    promoted = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
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
                    if resolved == current or not _release_executable_name(resolved):
                        continue
                    cleanup_deadline = release_deadline
                    while True:
                        try:
                            candidate.unlink()
                            break
                        except OSError:
                            if time.monotonic() >= cleanup_deadline:
                                raise
                            time.sleep(0.1)
                    cleaned.append(candidate.name)
                except Exception as exc:
                    errors.append(f"清理 {candidate.name}：{str(exc)[:180]}")
    manifest_updated = False
    if canonical.is_file():
        try:
            digest = hashlib.sha256()
            with canonical.open("rb") as executable:
                for chunk in iter(lambda: executable.read(1024 * 1024), b""):
                    digest.update(chunk)
            core.atomic_write_text(
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


def _schedule_release_maintenance(delay_seconds: float = 0.25) -> threading.Thread | None:
    """Promote/clean release files only after the health server can answer.

    During quick restart the old manager deliberately stays alive until the
    new process passes its authenticated health handshake.  Its PyInstaller
    bootloader keeps the old EXE locked for that same interval.  Running the
    promotion synchronously before ``serve_forever`` would therefore create a
    circular wait.  The daemon starts after the server loop is scheduled, so
    the old process can exit and release the canonical file first.
    """
    if not getattr(sys, "frozen", False):
        return None

    def maintain() -> None:
        time.sleep(max(0.0, min(float(delay_seconds), 2.0)))
        try:
            # The old process may wait for WebView/UI readiness before it
            # exits. Keep replacement in this daemon, but cover the complete
            # handoff deadline instead of giving up after eight seconds.
            result = _promote_and_cleanup_release_executable(RESTART_HANDOFF_TIMEOUT_SECONDS + 15)
        except Exception as exc:
            result = {"promoted": False, "errors": [core._redact_sensitive_text(exc, limit=300)]}
        result["checkedAt"] = core.now_iso()
        try:
            core.atomic_write_json(core.STATE_DIR / "last-release-maintenance.json", result)
        except OSError:
            pass

    worker = threading.Thread(
        target=maintain,
        name="agent-manager-release-maintenance",
        daemon=True,
    )
    worker.start()
    return worker


def _instance_mutex_name() -> str:
    scope = str(core.STATE_DIR).casefold().encode("utf-8", errors="replace")
    digest = hashlib.sha256(scope).hexdigest()[:20]
    return f"Local\\CodexAgentManager-{digest}"


def _acquire_instance_mutex() -> bool:
    """Hold a named kernel object before creating any runtime overlay."""
    global _INSTANCE_MUTEX_HANDLE, _INSTANCE_MUTEX_KERNEL32
    if _INSTANCE_MUTEX_HANDLE is not None:
        return True
    if os.name != "nt":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, _instance_mutex_name())
    if not handle:
        raise OSError(ctypes.get_last_error(), "无法创建管理器单实例互斥锁")
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _INSTANCE_MUTEX_HANDLE = int(handle)
    _INSTANCE_MUTEX_KERNEL32 = kernel32
    return True


def _release_instance_mutex() -> None:
    global _INSTANCE_MUTEX_HANDLE, _INSTANCE_MUTEX_KERNEL32
    handle = _INSTANCE_MUTEX_HANDLE
    kernel32 = _INSTANCE_MUTEX_KERNEL32
    _INSTANCE_MUTEX_HANDLE = None
    _INSTANCE_MUTEX_KERNEL32 = None
    if handle is not None and kernel32 is not None:
        try:
            kernel32.CloseHandle(wintypes.HANDLE(handle))
        except Exception:
            pass


class OAuthDeviceLogin:
    """Own one local PKCE browser login without depending on a CLI child process."""

    ACTIVE_STATUSES = {"starting", "waiting", "exchanging"}
    CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
    AUTH_ENDPOINT = "https://auth.openai.com/oauth/authorize"
    TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
    SCOPES = (
        "openid profile email offline_access "
        "api.connectors.read api.connectors.invoke"
    )
    CALLBACK_PORTS = (1455,)
    MAX_TOKEN_RESPONSE_BYTES = 512_000
    MAX_CALLBACK_INPUT_BYTES = 16_384
    MAX_CALLBACK_CODE_BYTES = 8_192
    MAX_CALLBACK_FIELD_BYTES = 2_048

    def __init__(
        self,
        timeout_seconds: float = OAUTH_LOGIN_TIMEOUT_SECONDS,
        on_account_saved: object | None = None,
    ) -> None:
        self.lock = threading.RLock()
        self.timeout_seconds = max(10.0, float(timeout_seconds))
        self.deadline_monotonic: float | None = None
        self.callback_server: ThreadingHTTPServer | None = None
        self.callback_thread: threading.Thread | None = None
        self.callback_server_login_id: str | None = None
        self.timeout_thread: threading.Thread | None = None
        self.login_id: str | None = None
        self.callback_port: int | None = None
        self.expected_state: str | None = None
        self.code_verifier: str | None = None
        self.authorization_code: str | None = None
        self.account_payload: dict | None = None
        self.exchange_started = False
        self.on_account_saved = on_account_saved
        self.data = self._new_state("idle")

    @staticmethod
    def _new_state(status: str) -> dict:
        return {
            "status": status,
            "loginId": None,
            "startedAt": None,
            "expiresAt": None,
            "finishedAt": None,
            "url": None,
            "callbackUrl": None,
            "port": None,
            "browserOpened": False,
            "browserError": None,
            "error": None,
            "account": None,
        }

    def state(self) -> dict:
        self._expire_if_due()
        with self.lock:
            return json.loads(json.dumps(self.data))

    def _clear_secrets_locked(self) -> None:
        self.expected_state = None
        self.code_verifier = None
        self.authorization_code = None
        self.account_payload = None
        self.exchange_started = False
        self.deadline_monotonic = None

    def _stop_callback_server(self, *, expected_login_id: str | None = None) -> None:
        with self.lock:
            if (
                expected_login_id is not None
                and self.callback_server_login_id != expected_login_id
            ):
                return
            server = self.callback_server
            thread = self.callback_thread
            self.callback_server = None
            self.callback_thread = None
            self.callback_server_login_id = None
            if server is not None:
                if thread and thread.is_alive():
                    try:
                        server.shutdown()
                    except Exception:
                        pass
                try:
                    server.server_close()
                except Exception:
                    pass
            if thread and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=2)

    def _finish(
        self,
        status: str,
        *,
        expected_login_id: str | None = None,
        error: str | None = None,
        account: dict | None = None,
    ) -> bool:
        with self.lock:
            if expected_login_id is not None and self.login_id != expected_login_id:
                return False
            if self.data.get("status") not in self.ACTIVE_STATUSES:
                return False
            finished_login_id = self.login_id
            self.data.update(
                {
                    "status": status,
                    "finishedAt": core.now_iso(),
                    "error": core._redact_sensitive_text(error, limit=500) if error else None,
                    "account": account,
                }
            )
            self._clear_secrets_locked()
            self._stop_callback_server(expected_login_id=finished_login_id)
            return True

    def _expire_if_due(self) -> None:
        with self.lock:
            login_id = self.login_id
            due = bool(
                self.data.get("status") in self.ACTIVE_STATUSES
                and self.deadline_monotonic is not None
                and time.monotonic() >= self.deadline_monotonic
            )
            if due:
                self._finish(
                    "expired",
                    expected_login_id=login_id,
                    error="OAuth 登录已超时，请重新开始。",
                )

    @staticmethod
    def _pkce_pair() -> tuple[str, str]:
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode("ascii").rstrip("=")
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return verifier, challenge

    @classmethod
    def _authorization_url(cls, redirect_uri: str, challenge: str, state: str) -> str:
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": cls.CLIENT_ID,
                "redirect_uri": redirect_uri,
                "scope": cls.SCOPES,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "id_token_add_organizations": "true",
                "codex_cli_simplified_flow": "true",
                "state": state,
                "originator": "codex_cli_rs",
            }
        )
        return f"{cls.AUTH_ENDPOINT}?{query}"

    @staticmethod
    def _success_html(ok: bool, detail: str) -> bytes:
        color = "#22c997" if ok else "#ff7b72"
        title = "授权已接收" if ok else "授权未完成"
        safe_detail = (
            str(detail)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{title}</title>
<style>
body{{margin:0;background:#0d1714;color:#eef8f4;font-family:Segoe UI,Microsoft YaHei,sans-serif;
display:grid;place-items:center;min-height:100vh}}
main{{max-width:560px;padding:42px;border:1px solid #254c40;border-radius:24px;background:#12231e;text-align:center}}
h1{{color:{color};font-size:30px}}p{{color:#a9c2b9;line-height:1.7}}
</style></head><body><main><h1>{title}</h1><p>{safe_detail}</p>
<p>现在可以关闭此页面并返回 Agent Manager。</p></main></body></html>""".encode("utf-8")

    def _bind_callback_server(self, login_id: str) -> tuple[ThreadingHTTPServer, int]:
        owner = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_GET(self) -> None:
                try:
                    result = owner.submit_callback(self.path, expected_login_id=login_id)
                except (core.ManagerError, ValueError) as exc:
                    body = owner._success_html(False, core._redact_sensitive_text(exc, limit=360))
                    self.send_response(400)
                else:
                    accepted = result.get("status") == "exchanging"
                    detail = (
                        "账号凭据正在安全写入本机。"
                        if accepted
                        else str(result.get("error") or "本次授权已结束。")
                    )
                    body = owner._success_html(accepted, detail)
                    self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

        last_error = None
        for port in self.CALLBACK_PORTS:
            try:
                server = OAuthCallbackServer(("127.0.0.1", port), CallbackHandler)
                return server, port
            except OSError as exc:
                last_error = exc
        raise core.ManagerError(
            "无法启动 OAuth 回调监听器：本机 1455 端口已被占用。"
            "请先取消其他 Codex / VS Code 登录任务，再重新开始。"
        ) from last_error

    def _parse_callback_locked(self, callback_value: str) -> tuple[str, str | None]:
        raw = str(callback_value or "").strip()
        if not raw:
            raise core.ManagerError("请粘贴完整回调地址。")
        try:
            raw_size = len(raw.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise core.ManagerError("OAuth 回调地址包含无效 Unicode 字符。") from exc
        if raw_size > self.MAX_CALLBACK_INPUT_BYTES:
            raise core.ManagerError("OAuth 回调地址过长，已拒绝处理。")
        if any(ord(character) < 32 or ord(character) == 127 for character in raw):
            raise core.ManagerError("OAuth 回调地址包含无效控制字符。")
        port = self.callback_port
        expected_state = self.expected_state
        status = str(self.data.get("status") or "")
        if status not in {"waiting", "exchanging"} or not port or not expected_state:
            raise core.ManagerError("当前没有可接收回调的 OAuth 登录。")
        if raw.startswith("/"):
            raw = f"http://localhost:{port}{raw}"
        elif "://" not in raw:
            raw = f"http://localhost:{port}/auth/callback?{raw.lstrip('?')}"
        try:
            parsed = urlparse(raw)
            parsed_port = parsed.port
        except ValueError as exc:
            raise core.ManagerError("OAuth 回调地址格式无效。") from exc
        if (
            parsed.scheme != "http"
            or str(parsed.hostname or "").casefold() not in {"localhost", "127.0.0.1"}
            or parsed_port != port
            or parsed.path != "/auth/callback"
            or parsed.params
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise core.ManagerError(
                f"回调地址必须是 http://localhost:{port}/auth/callback?code=...&state=..."
            )
        if "#" in raw:
            raise core.ManagerError("OAuth 回调地址不能包含 fragment。")
        if re.search(r"%(?![0-9A-Fa-f]{2})", parsed.query):
            raise core.ManagerError("OAuth 回调查询参数包含无效百分号编码。")
        try:
            pairs = urllib.parse.parse_qsl(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=20,
            )
        except ValueError as exc:
            raise core.ManagerError("OAuth 回调查询参数格式无效。") from exc
        params: dict[str, list[str]] = {}
        for key, value in pairs:
            params.setdefault(key, []).append(value)
        for key in ("code", "state", "error", "error_description", "error_uri"):
            if len(params.get(key, [])) > 1:
                raise core.ManagerError(f"OAuth 回调包含重复的 {key} 参数。")
        state = str((params.get("state") or [""])[0])
        if not state or not secrets.compare_digest(state, expected_state):
            raise core.ManagerError("回调 state 校验失败，请粘贴当前这次授权产生的地址。")
        code = str((params.get("code") or [""])[0])
        oauth_error = str((params.get("error") or [""])[0])
        if code and oauth_error:
            raise core.ManagerError("OAuth 回调不能同时包含 code 和 error 参数。")
        if oauth_error:
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", oauth_error):
                raise core.ManagerError("OAuth 回调 error 参数格式无效。")
            for key in ("error_description", "error_uri"):
                value = str((params.get(key) or [""])[0])
                if len(value.encode("utf-8")) > self.MAX_CALLBACK_FIELD_BYTES:
                    raise core.ManagerError(f"OAuth 回调 {key} 参数过长，已拒绝处理。")
            return "", oauth_error
        if not code:
            raise core.ManagerError("回调地址缺少 code 参数。")
        if len(code.encode("utf-8")) > self.MAX_CALLBACK_CODE_BYTES:
            raise core.ManagerError("OAuth 回调 code 参数过长，已拒绝处理。")
        if any(ord(character) < 32 or ord(character) == 127 for character in code):
            raise core.ManagerError("OAuth 回调 code 参数包含无效控制字符。")
        return code, None

    def _parse_callback(self, callback_value: str) -> str:
        with self.lock:
            code, oauth_error = self._parse_callback_locked(callback_value)
            if oauth_error:
                raise core.ManagerError(f"OAuth 授权未完成：{oauth_error}。")
            return code

    def submit_callback(self, callback_value: str, expected_login_id: str | None = None) -> dict:
        with self.lock:
            if expected_login_id and self.login_id != expected_login_id:
                raise core.ManagerError("OAuth 登录会话不匹配，请重新开始。")
            self._expire_if_due()
            if expected_login_id and self.login_id != expected_login_id:
                raise core.ManagerError("OAuth 登录会话不匹配，请重新开始。")
            code, oauth_error = self._parse_callback_locked(callback_value)
            login_id = self.login_id
            if not login_id:
                raise core.ManagerError("OAuth 登录会话已经结束。")
            if oauth_error:
                if self.data.get("status") != "waiting" or self.exchange_started:
                    raise core.ManagerError("OAuth Token 交换已经开始，不能再提交授权错误。")
                message = (
                    "OAuth 授权已取消。"
                    if oauth_error == "access_denied"
                    else f"OAuth 授权失败：{oauth_error}。"
                )
                self._finish(
                    "cancelled" if oauth_error == "access_denied" else "error",
                    expected_login_id=login_id,
                    error=None if oauth_error == "access_denied" else message,
                )
                result = json.loads(json.dumps(self.data))
                if oauth_error == "access_denied":
                    result["error"] = message
                return result
            if self.exchange_started:
                return json.loads(json.dumps(self.data))
            self.authorization_code = code
            self.exchange_started = True
            self.data["status"] = "exchanging"
            exchange_thread = threading.Thread(
                target=self._exchange_and_save,
                args=(login_id, self._exchange_code),
                name=f"codex-agent-manager-oauth-exchange-{login_id}",
                daemon=True,
            )
            try:
                exchange_thread.start()
            except Exception as exc:
                self._finish(
                    "error",
                    expected_login_id=login_id,
                    error="无法启动 OAuth Token 交换任务。",
                )
                raise core.ManagerError("无法启动 OAuth Token 交换任务。") from exc
            return json.loads(json.dumps(self.data))

    def _exchange_code(self, code: str, verifier: str, port: int) -> dict:
        redirect_uri = f"http://localhost:{port}/auth/callback"
        request = urllib.request.Request(
            self.TOKEN_ENDPOINT,
            data=urllib.parse.urlencode(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": self.CLIENT_ID,
                    "code_verifier": verifier,
                }
            ).encode("ascii"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with core._open_same_origin_request(request, timeout=30) as response:
                raw = response.read(self.MAX_TOKEN_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            try:
                exc.close()
            finally:
                raise core.ManagerError(f"OAuth Token 交换失败：HTTP {status_code}。") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise core.ManagerError("OAuth Token 交换失败，请检查网络或代理后重试。") from exc
        if len(raw) > self.MAX_TOKEN_RESPONSE_BYTES:
            raise core.ManagerError("OAuth Token 响应过大，已拒绝处理。")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise core.ManagerError("OAuth Token 响应不是有效 JSON。") from exc
        if not isinstance(payload, dict):
            raise core.ManagerError("OAuth Token 响应格式无效。")
        tokens = {}
        for key in ("id_token", "access_token", "refresh_token"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                if len(value) > 100_000:
                    raise core.ManagerError("OAuth Token 字段过大，已拒绝处理。")
                tokens[key] = value
        if not tokens.get("id_token") or not tokens.get("access_token"):
            raise core.ManagerError("OAuth Token 响应缺少必要凭据。")
        return tokens

    def _exchange_and_save(self, login_id: str, exchange_code: object | None = None) -> None:
        try:
            with self.lock:
                if self.login_id != login_id or self.data.get("status") != "exchanging":
                    return
                code = str(self.authorization_code or "")
                verifier = str(self.code_verifier or "")
                port = int(self.callback_port or 0)
                account_payload = dict(self.account_payload or {})
            if not code or not verifier or not port:
                raise core.ManagerError("OAuth 登录状态不完整，请重新开始。")
            exchange = exchange_code if callable(exchange_code) else self._exchange_code
            tokens = exchange(code, verifier, port)
            with self.lock:
                if self.login_id != login_id or self.data.get("status") != "exchanging":
                    return
                if (
                    self.deadline_monotonic is None
                    or time.monotonic() >= self.deadline_monotonic
                ):
                    self._finish(
                        "expired",
                        expected_login_id=login_id,
                        error="OAuth 登录已超时，请重新开始。",
                    )
                    return
                auth_bytes = json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "OPENAI_API_KEY": None,
                        "tokens": {
                            "id_token": tokens["id_token"],
                            "access_token": tokens["access_token"],
                            "refresh_token": tokens.get("refresh_token"),
                        },
                        "last_refresh": core.now_iso(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                # Saving is the local commit point. Holding the lifecycle lock
                # prevents cancel/start from changing generations midway through
                # an otherwise valid credential write.
                if account_payload.get("reauthAccountId"):
                    import oauth_reauthentication
                    account = oauth_reauthentication.reauthenticate(account_payload["reauthAccountId"], auth_bytes)
                else:
                    account = core.save_codex_account(account_payload, auth_bytes=auth_bytes)
                account_summary = {"id": account.get("id"), "label": account.get("label")}
                self._finish(
                    "completed",
                    expected_login_id=login_id,
                    account=account_summary,
                )
            account_id = str(account.get("id") or "")
            if account_id and callable(self.on_account_saved):
                try:
                    self.on_account_saved([account_id])
                except Exception:
                    # The credential is already saved. A background metadata
                    # scheduling failure must never turn a successful login
                    # into a failed OAuth result.
                    pass
        except Exception as exc:
            self._finish(
                "error",
                expected_login_id=login_id,
                error=core._redact_sensitive_text(exc, limit=500),
            )

    def _watch_timeout(self, login_id: str) -> None:
        while True:
            with self.lock:
                if self.login_id != login_id or self.data.get("status") not in self.ACTIVE_STATUSES:
                    return
                deadline = self.deadline_monotonic
            remaining = (deadline or 0.0) - time.monotonic()
            if remaining <= 0:
                self._finish(
                    "expired",
                    expected_login_id=login_id,
                    error="OAuth 登录已超时，请重新开始。",
                )
                return
            time.sleep(min(remaining, 0.25))

    def _prepare_for_start(self) -> None:
        with self.lock:
            self._expire_if_due()
            active = self.data.get("status") in self.ACTIVE_STATUSES
            listener_alive = bool(self.callback_thread and self.callback_thread.is_alive())
            login_id = self.login_id
            if active and listener_alive:
                raise core.ManagerError("已有 OAuth 登录正在进行；可以继续、手动提交回调或先取消。")
            if active:
                self._finish(
                    "error",
                    expected_login_id=login_id,
                    error="上一次 OAuth 回调监听器已经停止，请重新开始。",
                )
            self._stop_callback_server(
                expected_login_id=self.callback_server_login_id,
            )
            self.login_id = None
            self.callback_port = None
            self._clear_secrets_locked()

    def start(self, payload: dict) -> dict:
        with self.lock:
            self._prepare_for_start()
            reauth_account = None
            if payload.get("reauthAccountId"):
                import oauth_reauthentication
                reauth_account = oauth_reauthentication.target(str(payload["reauthAccountId"]))
                payload = {**payload, "label":reauth_account.get("label"), "groupId":reauth_account.get("groupId") or "official", "proxyEnabled":reauth_account.get("proxyEnabled", False)}
            group_id = str(payload.get("groupId") or "official")
            core._account_group(core.load_settings(), group_id)
            login_id = secrets.token_hex(8)
            verifier, challenge = self._pkce_pair()
            expected_state = secrets.token_urlsafe(32)
            server, port = self._bind_callback_server(login_id)
            redirect_uri = f"http://localhost:{port}/auth/callback"
            authorization_url = self._authorization_url(redirect_uri, challenge, expected_state)
            if reauth_account:
                authorization_url += "&" + urllib.parse.urlencode({"prompt":"select_account", "login_hint":reauth_account.get("email") or ""})
            started_at = datetime.now(timezone.utc)
            self.login_id = login_id
            self.callback_port = port
            self.callback_server = server
            self.callback_server_login_id = login_id
            self.code_verifier = verifier
            self.expected_state = expected_state
            self.authorization_code = None
            self.exchange_started = False
            self.deadline_monotonic = time.monotonic() + self.timeout_seconds
            self.account_payload = {
                "label": str(payload.get("label") or ""),
                "groupId": group_id,
                "proxyEnabled": bool(payload.get("proxyEnabled")),
                "sourceType": "codex_auth",
                # Make the account visible immediately. Quota/model discovery
                # is queued after the encrypted credential has been committed.
                "deferRefresh": True,
                **({"reauthAccountId":reauth_account["id"]} if reauth_account else {}),
            }
            self.data = self._new_state("starting")
            self.data.update(
                {
                    "loginId": login_id,
                    "startedAt": started_at.isoformat(),
                    "expiresAt": (started_at + timedelta(seconds=self.timeout_seconds)).isoformat(),
                    "url": authorization_url,
                    "callbackUrl": redirect_uri,
                    "port": port,
                    **({"reauthAccountId":reauth_account["id"]} if reauth_account else {}),
                }
            )
            callback_thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.1},
                name=f"codex-agent-manager-oauth-listener-{login_id}",
                daemon=True,
            )
            timeout_thread = threading.Thread(
                target=self._watch_timeout,
                args=(login_id,),
                name=f"codex-agent-manager-oauth-timeout-{login_id}",
                daemon=True,
            )
            self.callback_thread = callback_thread
            self.timeout_thread = timeout_thread
            try:
                callback_thread.start()
                timeout_thread.start()
            except Exception:
                self._finish(
                    "error",
                    expected_login_id=login_id,
                    error="无法启动 OAuth 回调任务。",
                )
                raise
            if self.data.get("status") == "starting":
                self.data["status"] = "waiting"
        try:
            opened = bool(open_browser_window(authorization_url))
            browser_error = None
        except Exception as exc:
            opened = False
            browser_error = core._redact_sensitive_text(exc, limit=500)
        with self.lock:
            if self.login_id == login_id and self.data.get("status") in self.ACTIVE_STATUSES:
                self.data["browserOpened"] = opened
                self.data["browserError"] = browser_error
        return self.state()

    def open_browser(self, expected_login_id: str | None = None) -> dict:
        self._expire_if_due()
        with self.lock:
            if expected_login_id and self.login_id != expected_login_id:
                raise core.ManagerError("OAuth 登录会话不匹配，请重新开始。")
            if self.data.get("status") not in {"starting", "waiting"}:
                raise core.ManagerError("当前没有可重新打开的 OAuth 登录。")
            authorization_url = str(self.data.get("url") or "")
            opening_login_id = self.login_id
        if not authorization_url:
            raise core.ManagerError("OAuth 授权链接尚未就绪。")
        try:
            opened = bool(open_browser_window(authorization_url))
            browser_error = None
        except Exception as exc:
            opened = False
            browser_error = core._redact_sensitive_text(exc, limit=500)
        with self.lock:
            if self.login_id == opening_login_id:
                self.data["browserOpened"] = opened
                self.data["browserError"] = browser_error
        if not opened and browser_error:
            raise core.ManagerError(f"无法打开浏览器：{browser_error}")
        return self.state()

    def cancel(self, expected_login_id: str | None = None) -> dict:
        with self.lock:
            if expected_login_id and self.login_id != expected_login_id:
                raise core.ManagerError("OAuth 登录会话不匹配，请重新开始。")
            login_id = self.login_id
            self._finish("cancelled", expected_login_id=login_id)
            return json.loads(json.dumps(self.data))

    def close(self) -> dict:
        with self.lock:
            active = self.data.get("status") in self.ACTIVE_STATUSES
            if active:
                return self.cancel()
            self._stop_callback_server(
                expected_login_id=self.callback_server_login_id,
            )
            return json.loads(json.dumps(self.data))

def _totp_api_item(item: dict) -> dict:
    remaining = max(1, int(item.get("seconds_remaining") or 1))
    return {
        "id": item.get("id"),
        "code": str(item.get("code") or ""),
        "label": str(item.get("label") or ""),
        "issuer": str(item.get("issuer") or ""),
        "algorithm": str(item.get("algorithm") or "SHA1"),
        "digits": int(item.get("digits") or 6),
        "period": int(item.get("period") or 30),
        "secondsRemaining": remaining,
        "validUntil": (datetime.now(timezone.utc) + timedelta(seconds=remaining)).isoformat(),
        "createdAt": item.get("createdAt"),
    }


def _mail_account_api_item(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "label": item.get("label"),
        "email": item.get("email"),
        "provider": item.get("provider"),
        "imapHost": item.get("imap_host"),
        "imapPort": item.get("imap_port"),
        "security": item.get("security"),
        "authMode": item.get("auth_method"),
        "mailbox": item.get("mailbox"),
        "credentialStatus": item.get("credential_status"),
    }


def _mail_message_api_item(item: dict) -> dict:
    senders = item.get("from") if isinstance(item.get("from"), list) else []
    recipients = item.get("to") if isinstance(item.get("to"), list) else []
    text = str(item.get("text") or "")
    return {
        "id": str(item.get("uid") or ""),
        "subject": str(item.get("subject") or ""),
        "from": ", ".join(str(value) for value in senders),
        "to": ", ".join(str(value) for value in recipients),
        "receivedAt": item.get("date"),
        "unread": bool(item.get("unread")),
        "codes": item.get("verification_codes") if isinstance(item.get("verification_codes"), list) else [],
        "preview": text[:2_000],
        "truncated": bool(item.get("truncated")) or len(text) > 2_000,
    }


def _radar_api_section(envelope: dict | None) -> dict:
    """Flatten the cache envelope while keeping provenance visible to the UI."""
    wrapped = envelope if isinstance(envelope, dict) else {}
    data = wrapped.get("data")
    section = dict(data) if isinstance(data, dict) else {}
    source = wrapped.get("source") if isinstance(wrapped.get("source"), dict) else {}
    section["stale"] = bool(wrapped.get("stale"))
    section["meta"] = {
        "sourceLabel": "Codex Radar 社区公开数据",
        "sourceUrl": source.get("url"),
        "sourceFormat": source.get("format"),
        "fetchedAt": wrapped.get("fetchedAt"),
        "checkedAt": wrapped.get("lastAttemptAt"),
        "nextAllowedAt": wrapped.get("nextAllowedAt"),
        "stale": bool(wrapped.get("stale")),
        "cached": bool(wrapped.get("cached")),
        "refreshSuppressed": bool(wrapped.get("refreshSuppressed")),
        "error": wrapped.get("error"),
    }
    return section


class ToolboxRuntime:
    """Manual-only mailbox access with single-flight and a short anti-repeat cache."""

    CACHE_SECONDS = 15.0

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.active_mailboxes: set[str] = set()
        self.mail_cache: dict[tuple[str, int, bool], tuple[float, list[dict]]] = {}
        self.temporary_mail_cache: dict[tuple[str, int, bool], tuple[float, list[dict]]] = {}
        self.mail_generation = 0
        self.mail_revisions: dict[str, int] = {}

    def state(self) -> dict:
        return {
            "totpItems": [_totp_api_item(item) for item in toolbox.list_totp_items()],
            "mailAccounts": [_mail_account_api_item(item) for item in toolbox.list_mail_accounts()],
        }

    def fetch_mail(self, account_id: str, *, limit: int, unread_only: bool) -> list[dict]:
        key = (account_id, limit, unread_only)
        now = time.monotonic()
        with self.lock:
            cached = self.mail_cache.get(key)
            if cached and now - cached[0] < self.CACHE_SECONDS:
                return json.loads(json.dumps(cached[1]))
            if account_id in self.active_mailboxes:
                raise core.ManagerError("这个邮箱正在读取中，请勿重复请求。")
            self.active_mailboxes.add(account_id)
            generation = (self.mail_generation, self.mail_revisions.get(account_id, 0))
        try:
            messages = toolbox.fetch_mail_messages(
                account_id,
                limit=limit,
                unread_only=unread_only,
            )
            public_messages = [_mail_message_api_item(item) for item in messages]
            with self.lock:
                if generation != (self.mail_generation, self.mail_revisions.get(account_id, 0)):
                    raise core.ManagerError("邮箱凭据已修改或恢复，请重新读取邮件。")
                self.mail_cache[key] = (time.monotonic(), public_messages)
            return json.loads(json.dumps(public_messages))
        finally:
            with self.lock:
                self.active_mailboxes.discard(account_id)

    def forget_mail(self, account_id: str) -> None:
        with self.lock:
            self.mail_revisions[account_id] = self.mail_revisions.get(account_id, 0) + 1
            for key in [key for key in self.mail_cache if key[0] == account_id]:
                self.mail_cache.pop(key, None)

    def forget_all_mail(self) -> None:
        with self.lock:
            self.mail_generation += 1
            self.mail_revisions.clear()
            self.mail_cache.clear()
            self.temporary_mail_cache.clear()

    def fetch_temporary_mail(self, source: str, *, limit: int, unread_only: bool) -> list[dict]:
        """Read one unsaved mailbox bundle without retaining its credentials."""
        raw = str(source or "")
        accounts = toolbox.parse_email_accounts(raw, max_accounts=1)
        if len(accounts) != 1:
            raise toolbox.ValidationError("请输入一个邮箱账号。")
        digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
        mailbox_key = f"temporary:{digest}"
        key = (digest, limit, unread_only)
        now = time.monotonic()
        with self.lock:
            cached = self.temporary_mail_cache.get(key)
            if cached and now - cached[0] < self.CACHE_SECONDS:
                return json.loads(json.dumps(cached[1]))
            if mailbox_key in self.active_mailboxes:
                raise core.ManagerError("这个邮箱正在读取中，请稍候。")
            self.active_mailboxes.add(mailbox_key)
            generation = self.mail_generation
        try:
            messages = toolbox.fetch_email_messages(
                accounts[0],
                limit=limit,
                unread_only=unread_only,
            )
            public_messages = [_mail_message_api_item(item) for item in messages]
            with self.lock:
                if generation != self.mail_generation:
                    raise core.ManagerError("邮箱缓存已清理，请重新读取邮件。")
                self.temporary_mail_cache[key] = (time.monotonic(), public_messages)
            return json.loads(json.dumps(public_messages))
        finally:
            # `accounts` and `raw` fall out of scope here; no credential is
            # written to the mailbox store or returned by the API.
            with self.lock:
                self.active_mailboxes.discard(mailbox_key)


class ManagerRuntime:
    def __init__(
        self,
        quick_restart: bool = False,
        defer_configuration: bool = False,
        restart_handoff: dict | None = None,
    ) -> None:
        self.lock = threading.RLock()
        self.last_error: str | None = None
        self.switch_operation_lock = threading.RLock()
        self.switch_operations: dict[str, dict] = {}
        self._closed = False
        self.app_updates = None
        self._close_result = None
        self.quick_restart = bool(quick_restart)
        self.restart_handoff = dict(restart_handoff or {})
        self.defer_configuration = bool(defer_configuration)
        self._restart_prepared = False
        self._restart_prepare_result: dict | None = None
        self.account_refresh_lock = threading.RLock()
        self.account_refresh_stop = threading.Event()
        self.account_refresh_threads: list[threading.Thread] = []
        self.account_refresh_pending: list[str] = []
        self.account_refresh_active: set[str] = set()
        self.account_auto_refresh_stop = threading.Event()
        self.account_auto_refresh_wake = threading.Event()
        self.account_auto_refresh_thread: threading.Thread | None = None
        self.mail_health_stop = threading.Event()
        self.mail_health_wake = threading.Event()
        self.mail_health_thread: threading.Thread | None = None
        self.update_check_stop = threading.Event()
        self.update_check_wake = threading.Event()
        self.update_check_thread: threading.Thread | None = None
        self.radar_monitor_stop = threading.Event()
        self.radar_monitor_wake = threading.Event()
        self.radar_monitor_thread: threading.Thread | None = None
        self.radar_alert_notifier = None
        self.radar_monitor_status = {
            "status": "waiting",
            "lastCheckedAt": None,
            "nextCheckAt": None,
            "lastResult": "尚未检查",
            "lastError": None,
        }
        self.update_check_status = {
            "status": "waiting",
            "lastCheckedAt": None,
            "nextCheckAt": None,
            "lastError": None,
        }
        self.account_auto_refresh_status = {
            "enabled": True,
            "intervalMinutes": 10,
            "lastCheckedAt": None,
            "lastQueuedAt": None,
            "queued": 0,
            "lastError": None,
        }
        self.mail_health_status = {
            "enabled": True,
            "intervalHours": 24,
            "status": "waiting",
            "lastCheckedAt": None,
            "checked": 0,
            "healthy": 0,
            "error": 0,
            "lastError": None,
        }
        self.account_refresh_status = {
            "status": "idle",
            "total": 0,
            "completed": 0,
            "ready": 0,
            "partial": 0,
            "error": 0,
            "currentAccountId": None,
            "activeAccountIds": [],
            "startedAt": None,
            "finishedAt": None,
        }
        self.validation = {
            "valid": None,
            "errors": [],
            "warnings": [],
            "doctor": "尚未检查",
            "checkedAt": None,
            "agentCount": 0,
        }
        self.toolbox = ToolboxRuntime()
        self.web2api = Web2APIManager()
        from quota_calibration_runtime import QuotaCalibrationSampler
        self.quota_estimates = QuotaCalibrationSampler(self)
        self.radar = radar.RadarService()
        self.oauth = OAuthDeviceLogin(on_account_saved=self.schedule_account_refresh)
        self.relay_portal = relay_portal.RelayPortalService()
        self.configuration_session = {
            "status": "waiting" if defer_configuration else "starting",
            "active": False,
            "message": (
                "管理器窗口就绪后将应用临时 Codex 配置。"
                if defer_configuration
                else "正在应用临时 Codex 配置。"
            ),
            "applied": None,
            "restart": None,
            "restore": None,
            "error": None,
            "recoveryOnly": False,
            "recoverable": False,
            "externalSelection": None,
            "externalSelectionPreserved": False,
        }
        self._configuration_activation_started = False
        if not defer_configuration:
            self.activate_configuration_session()

    def enter_startup_recovery(self, error: object) -> dict:
        """Keep the local repair surface alive after state bootstrap fails.

        A damaged or newer settings document can fail before the normal
        configuration activation step.  A visible launch must still expose
        the authenticated local UI so Emergency Repair can inspect it.  Mark
        activation as attempted to prevent the window-ready callback from
        immediately retrying the same failing bootstrap path.
        """
        detail = core._redact_sensitive_text(error, limit=500)
        with self.lock:
            self._configuration_activation_started = True
            self.configuration_session.update(
                {
                    "status": "error",
                    "active": False,
                    "message": (
                        "本机状态初始化失败；Agent Manager 已进入安全修复模式。"
                        "窗口与紧急修复功能保持可用，Codex 配置和进程不会被改动。"
                    ),
                    "applied": None,
                    "restore": None,
                    "restart": None,
                    "error": detail,
                    "recoveryOnly": True,
                    "recoverable": True,
                }
            )
            self.last_error = detail
            return json.loads(json.dumps(self.configuration_session))

    def _recovery_public_state(self, error: object) -> dict:
        """Build a credential-safe first-paint payload for recovery mode."""
        detail = core._redact_sensitive_text(error, limit=500)
        try:
            settings = core.load_settings()
            public_settings = core._public_settings_projection(settings)
        except Exception:
            # Never return the raw failing document: imported records may
            # contain legacy inline credentials.  Defaults contain no user
            # data and keep the React shell structurally usable while the
            # dedicated repair endpoints work on the real files.
            defaults = core._initial_settings()
            try:
                public_settings = core._public_settings_projection(defaults)
            except Exception:
                public_settings = json.loads(json.dumps(defaults))
        workspace = public_settings.get("modelWorkspace")
        if not isinstance(workspace, dict):
            workspace = {}
        return {
            "settings": public_settings,
            "agents": [],
            "localModels": [],
            "modelSources": [],
            "selectedModelKeys": [],
            "effectiveSubagentRouting": {},
            "subagentRuntime": {},
            "modelError": detail,
            "difficultyMeta": core.DIFFICULTY_META,
            "efforts": list(core.VALID_EFFORTS),
            "sandboxes": list(core.VALID_SANDBOXES),
            "codexVersion": "暂不可用（安全修复模式）",
            "codexHome": str(core.CODEX_HOME),
            "configPath": str(core.CONFIG_FILE),
            "status": {
                "mainActive": False,
                "strategyActive": False,
                "fullyApplied": False,
                "mode": str(workspace.get("mode") or "independent"),
                "activeSourceId": str(workspace.get("activeSourceId") or ""),
                "modelCount": 0,
                "modelCountScope": "current_account",
                "recoveryOnly": True,
                "error": detail,
            },
            "auth": {
                "signedIn": False,
                "authMode": None,
                "email": "",
                "name": "",
                "plan": "",
                "display": "安全修复模式下暂未读取",
                "activeAccountId": None,
                "declaredAuthMode": "",
                "authContractValid": False,
                "requiresReapply": False,
                "credentialStore": "unknown",
                "snapshotSupported": False,
                "error": detail,
                "runningProcesses": [],
                "processScanKnown": False,
                "processScanError": detail,
            },
            "historySummary": {},
        }

    def activate_configuration_session(self, *, retry: bool = False) -> dict:
        """Apply the temporary Codex overlay once the manager UI is visible.

        Startup used to close Codex before WebView creation.  Any later UI or
        settings error therefore left the user with neither application.  The
        native entry point now calls this method from pywebview's post-start
        callback, and the guard makes the transition safe to retry/idempotent.
        """
        with self.lock:
            if self._closed:
                return json.loads(json.dumps(self.configuration_session))
            if self._configuration_activation_started and not (
                retry and self.configuration_session.get("status") == "error"
            ):
                return json.loads(json.dumps(self.configuration_session))
            self._configuration_activation_started = True
            self.configuration_session.update(
                {
                    "status": "starting",
                    "message": "正在应用临时 Codex 配置。",
                    "error": None,
                    "recoveryOnly": False,
                    "recoverable": False,
                    "externalSelection": None,
                    "externalSelectionPreserved": False,
                }
            )
            self.last_error = None
        overlay_started = False
        activation_started = time.monotonic()
        try:
            external_selection = None
            preserve_external_selection = bool(self.quick_restart and self.restart_handoff.get("preserveExternalSelection"))
            if (
                self.quick_restart
                and "preserveExternalSelection" not in self.restart_handoff
                and self.restart_handoff.get("sourceOverlayCurrent") is False
            ):
                # A 9.4 parent cannot send the new decision, but it already
                # reports whether external changes invalidated its overlay.
                preserve_external_selection = live_selection.preserve_live_configuration_on_restart()
            if preserve_external_selection:
                external_selection = {"preserveCurrent": True, "message": "管理器已重启，继续保留外部工具选择的 Codex 账号与配置。"}
            if not self.quick_restart:
                external_selection = live_selection.reconcile_startup_selection()
                preserve_external_selection = bool(external_selection.get("preserveCurrent"))
            adopt_existing_overlay = bool(
                preserve_external_selection and core._runtime_overlay_read()
            )
            overlay = (
                core.adopt_runtime_configuration_overlay()
                if self.quick_restart or adopt_existing_overlay
                else core.begin_runtime_configuration_overlay()
            )
            overlay_started = True
            fast_adopted = bool(
                self.quick_restart
                and _quick_restart_can_adopt_without_apply(self.restart_handoff)
            )
            applied = (
                {
                    "changed": False,
                    "gatewayRequired": bool(
                        core.load_settings().get("web2api", {}).get("enabled")
                    ),
                    "fastRestartAdopted": True,
                }
                if fast_adopted
                else {
                    "changed": False,
                    "gatewayRequired": False,
                    "externalSelectionPreserved": True,
                }
                if preserve_external_selection
                else core.apply_configuration(False)
            )
            settings = core.load_settings()
            gateway_required = bool(applied.get("gatewayRequired"))
            service_enabled = bool(settings.get("web2api", {}).get("enabled"))
            if (gateway_required or service_enabled) and core.service_secret_configured("web2api"):
                try:
                    self.web2api.start()
                except core.ManagerError as exc:
                    self.web2api.last_error = str(exc)
                    if gateway_required:
                        raise
            restart = None
            codex_was_running = False
            if not self.quick_restart:
                # Starting Agent Manager is a passive action. Never terminate
                # an existing Codex task just to make newly written settings
                # take effect; explicit account/configuration actions already
                # offer a reviewed restart path when one is actually needed.
                codex_was_running = bool(core.running_codex_processes())
            self.configuration_session.update(
                {
                    "status": "active",
                    "active": True,
                    "message": (
                        "管理器已快速重启；Codex 进程与临时配置均保持不变。"
                        if self.quick_restart
                        else str(
                            external_selection.get("message")
                            or "检测到外部 Codex 选择，已保留当前账号与配置。"
                        ).strip()
                        if preserve_external_selection
                        else "临时配置已应用；检测到 Codex 正在运行，现有任务不会被自动关闭。"
                        "新配置将在下次重启 Codex 后完整生效。"
                        if codex_was_running
                        else "临时配置已应用；请选择账号后打开 Codex。彻底退出时自动还原默认配置。"
                    ),
                    "overlay": overlay,
                    "applied": applied,
                    "restart": restart,
                    "closedOnStartup": None,
                    "codexWasRunningOnStartup": codex_was_running,
                    "recoveryOnly": False,
                    "recoverable": False,
                    "externalSelection": external_selection,
                    "externalSelectionPreserved": preserve_external_selection,
                    "restartTiming": {
                        "activationMs": round((time.monotonic() - activation_started) * 1000, 1),
                        "configurationReapplied": not fast_adopted and not preserve_external_selection,
                        "fastOverlayAdopted": fast_adopted,
                    },
                    "configurationStateFingerprint": _restart_configuration_fingerprint(),
                }
            )
        except Exception as exc:
            try:
                if self.web2api.status().get("running"):
                    self.web2api.stop(disable=False)
            except Exception:
                pass
            restored = None
            if overlay_started and not self.quick_restart:
                try:
                    restored = core.restore_runtime_configuration_overlay(force=True)
                except Exception as restore_exc:
                    restored = {"restored": False, "warnings": [core._redact_sensitive_text(restore_exc, limit=300)]}
            self.configuration_session.update(
                {
                    "status": "error",
                    "active": False,
                    "message": (
                        "临时配置未启用；Agent Manager 已进入安全修复模式。"
                        "窗口与紧急修复功能保持可用，Codex 配置不会继续改动。"
                    ),
                    "restore": restored,
                    "restart": None,
                    "error": core._redact_sensitive_text(exc, limit=500),
                    "recoveryOnly": True,
                    "recoverable": True,
                }
            )
            self.last_error = self.configuration_session["error"]
            # A visible desktop/browser window must remain available so the
            # user can run Emergency Repair.  Headless construction has no
            # recovery UI, so preserve its fail-fast behavior.
            if not self.defer_configuration:
                raise
        if self.configuration_session.get("active"):
            self._start_account_auto_refresh()
            self._start_mail_health_checks()
            self._start_update_checks()
            if core.load_settings().get("appBehavior", {}).get("radarMonitoring") is True:
                self._start_radar_monitor()
        return json.loads(json.dumps(self.configuration_session))

    def _account_auto_refresh_interval_minutes(self) -> int:
        settings = core.load_settings()
        raw_value = settings.get("appBehavior", {}).get("quotaRefreshMinutes", 10)
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = 10
        return value if value in core.VALID_QUOTA_REFRESH_MINUTES else 10

    def _account_auto_refresh_tick(self) -> dict:
        interval_minutes = self._account_auto_refresh_interval_minutes()
        checked_at = core.now_iso()
        with self.account_refresh_lock:
            self.account_auto_refresh_status.update(
                {
                    "enabled": interval_minutes > 0,
                    "intervalMinutes": interval_minutes,
                    "lastCheckedAt": checked_at,
                    "queued": 0,
                    "lastError": None,
                }
            )
        if interval_minutes <= 0 or self._closed:
            with self.account_refresh_lock:
                return json.loads(json.dumps(self.account_auto_refresh_status))

        account_ids = core.stale_codex_account_ids(interval_minutes * 60)
        if account_ids:
            self.schedule_account_refresh(account_ids)
            with self.account_refresh_lock:
                self.account_auto_refresh_status.update(
                    {"lastQueuedAt": checked_at, "queued": len(account_ids)}
                )
        with self.account_refresh_lock:
            return json.loads(json.dumps(self.account_auto_refresh_status))

    def _run_account_auto_refresh(self) -> None:
        refresh_on_start = True
        while not self.account_auto_refresh_stop.is_set():
            if refresh_on_start:
                try:
                    self._account_auto_refresh_tick()
                except Exception as exc:
                    with self.account_refresh_lock:
                        self.account_auto_refresh_status.update(
                            {"lastCheckedAt": core.now_iso(), "lastError": core._redact_sensitive_text(exc, limit=300)}
                        )
                refresh_on_start = False
            interval_minutes = self._account_auto_refresh_interval_minutes()
            with self.account_refresh_lock:
                self.account_auto_refresh_status.update(
                    {"enabled": interval_minutes > 0, "intervalMinutes": interval_minutes}
                )
            wait_seconds = interval_minutes * 60 if interval_minutes > 0 else None
            woke = self.account_auto_refresh_wake.wait(wait_seconds)
            self.account_auto_refresh_wake.clear()
            if self.account_auto_refresh_stop.is_set():
                return
            if woke:
                continue
            try:
                self._account_auto_refresh_tick()
            except Exception as exc:
                with self.account_refresh_lock:
                    self.account_auto_refresh_status.update(
                        {"lastCheckedAt": core.now_iso(), "lastError": core._redact_sensitive_text(exc, limit=300)}
                    )

    def _start_account_auto_refresh(self) -> None:
        if self.account_auto_refresh_thread and self.account_auto_refresh_thread.is_alive():
            return
        self.account_auto_refresh_stop.clear()
        self.account_auto_refresh_thread = threading.Thread(
            target=self._run_account_auto_refresh,
            name="codex-agent-manager-quota-refresh",
            daemon=True,
        )
        self.account_auto_refresh_thread.start()

    def _mail_health_interval_hours(self) -> int:
        settings = core.load_settings()
        raw_value = settings.get("appBehavior", {}).get("mailHealthCheckHours", 24)
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = 24
        return value if value in core.VALID_MAIL_HEALTH_CHECK_HOURS else 24

    def _mail_health_tick(self) -> dict:
        interval_hours = self._mail_health_interval_hours()
        checked_at = core.now_iso()
        status = {
            "enabled": interval_hours > 0,
            "intervalHours": interval_hours,
            "status": "checking" if interval_hours > 0 else "disabled",
            "lastCheckedAt": checked_at,
            "checked": 0,
            "healthy": 0,
            "error": 0,
            "lastError": None,
        }
        self.mail_health_status.update(status)
        if interval_hours <= 0 or self._closed:
            return json.loads(json.dumps(self.mail_health_status))
        try:
            account_ids = toolbox.stale_mail_account_ids(interval_hours * 60 * 60)
        except toolbox.ToolboxError as exc:
            self.mail_health_status.update(
                {"status": "error", "lastError": core._redact_sensitive_text(exc, limit=240)}
            )
            return json.loads(json.dumps(self.mail_health_status))

        for account_id in account_ids:
            if self.mail_health_stop.is_set() or self._closed:
                break
            try:
                result = toolbox.check_saved_email_health(
                    account_id,
                    timeout=12,
                    commit_guard=lambda: not self.mail_health_stop.is_set() and not self._closed,
                )
                if result.get("status") == "cancelled":
                    break
                self.mail_health_status["checked"] += 1
                bucket = "healthy" if result.get("healthy") else "error"
                self.mail_health_status[bucket] += 1
            except toolbox.ToolboxError:
                # A safe per-account state is already persisted when possible;
                # keep the scheduler alive without exposing credential details.
                self.mail_health_status["checked"] += 1
                self.mail_health_status["error"] += 1
            # Serialize checks and leave a cancellation point between accounts.
            if self.mail_health_stop.wait(0.2):
                break
        self.mail_health_status["status"] = (
            "cancelled" if self.mail_health_stop.is_set() or self._closed else "completed"
        )
        return json.loads(json.dumps(self.mail_health_status))

    def _run_mail_health_checks(self) -> None:
        check_on_start = True
        while not self.mail_health_stop.is_set():
            if check_on_start:
                self._mail_health_tick()
                check_on_start = False
            interval_hours = self._mail_health_interval_hours()
            self.mail_health_status.update(
                {"enabled": interval_hours > 0, "intervalHours": interval_hours}
            )
            wait_seconds = interval_hours * 60 * 60 if interval_hours > 0 else None
            woke = self.mail_health_wake.wait(wait_seconds)
            self.mail_health_wake.clear()
            if self.mail_health_stop.is_set():
                return
            if woke:
                continue
            self._mail_health_tick()

    def _start_mail_health_checks(self) -> None:
        if self.mail_health_thread and self.mail_health_thread.is_alive():
            return
        self.mail_health_stop.clear()
        self.mail_health_thread = threading.Thread(
            target=self._run_mail_health_checks,
            name="agent-manager-mail-health",
            daemon=True,
        )
        self.mail_health_thread.start()

    def request_mail_health_recalculate(self) -> None:
        self.mail_health_wake.set()

    def _stop_mail_health_checks(self) -> None:
        self.mail_health_stop.set()
        self.mail_health_wake.set()
        thread = self.mail_health_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def request_account_auto_refresh_check(self) -> None:
        self.account_auto_refresh_wake.set()

    def _stop_account_auto_refresh(self) -> None:
        self.account_auto_refresh_stop.set()
        self.account_auto_refresh_wake.set()
        thread = self.account_auto_refresh_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _run_update_checks(self) -> None:
        try:
            status = maintenance.update_center_status(force=False)
            self.update_check_status.update(
                {
                    "status": "ready" if status.get("checkedAt") else "waiting",
                    "lastCheckedAt": status.get("checkedAt"),
                    "nextCheckAt": None,
                    "lastError": status.get("lastError"),
                    "cached": True,
                    "needsManualCheck": bool(status.get("needsManualCheck")),
                }
            )
        except Exception as exc:
            self.update_check_status.update(
                {"status": "error", "lastCheckedAt": None, "nextCheckAt": None, "lastError": core._redact_sensitive_text(exc, limit=300)}
            )

    def _start_update_checks(self) -> None:
        self._run_update_checks()

    def get_app_updates(self):
        with self.lock:
            if self._closed:
                raise core.ManagerError("管理器正在退出。")
            if self.app_updates is None:
                if getattr(sys, "frozen", False):
                    version = ".".join(str(part) for part in _executable_version(Path(sys.executable))[:3])
                else:
                    version = str(core.read_json(RESOURCE_ROOT / "gui/package.json", {}).get("version") or "0.0.0")
                self.app_updates = app_updates.AppUpdateService(
                    version, core.STATE_DIR / "app-update-config.json",
                    core.user_downloads_directory() / "AgentManagerUpdates",
                    bundled_source_path=RESOURCE_ROOT / "app-update-source.json",
                    cache_path=core.STATE_DIR / "app-update-cache.json",
                    install_supported=bool(getattr(sys, "frozen", False) and os.name == "nt"),
                )
            return self.app_updates

    def request_update_check(self) -> None:
        self._run_update_checks()

    def _stop_update_checks(self) -> None:
        self.update_check_stop.set()

    def set_radar_alert_notifier(self, notifier) -> None:
        self.radar_alert_notifier = notifier if callable(notifier) else None

    def _run_radar_monitor(self, stop=None, wake=None) -> None:
        stop = stop if stop is not None else self.radar_monitor_stop
        wake = wake if wake is not None else self.radar_monitor_wake
        while not stop.is_set():
            try:
                current = self.radar.monitor_status()
                self.radar_monitor_status.update({
                    "status": "waiting",
                    "lastCheckedAt": current.get("lastSuccessAt"),
                    "nextCheckAt": current.get("nextCheckAt"),
                    "lastResult": current.get("lastResult") or "尚未检查",
                    "lastError": current.get("lastError"),
                })
                wait_seconds = max(0.1, self.radar.seconds_until_next_monitor())
            except Exception as exc:
                self.radar_monitor_status.update({"status": "error", "lastError": core._redact_sensitive_text(exc, limit=300)})
                wait_seconds = 300.0
            woke = wake.wait(wait_seconds)
            wake.clear()
            if stop.is_set():
                return
            if woke:
                continue
            try:
                self.radar_monitor_status["status"] = "checking"
                result = self.radar.run_monitor()
                if stop.is_set():
                    return
                state = result.get("state") if isinstance(result.get("state"), dict) else {}
                self.radar_monitor_status.update({
                    "status": "waiting" if result.get("success") else "error",
                    "lastCheckedAt": state.get("lastSuccessAt") or state.get("lastRunAt"),
                    "nextCheckAt": state.get("nextCheckAt"),
                    "lastResult": state.get("lastResult") or "无预警",
                    "lastError": state.get("lastError"),
                })
                if result.get("newAlert") and isinstance(result.get("alert"), dict) and callable(self.radar_alert_notifier):
                    try:
                        self.radar_alert_notifier(result["alert"])
                    except Exception:
                        pass
            except Exception as exc:
                self.radar_monitor_status.update({"status": "error", "lastError": core._redact_sensitive_text(exc, limit=300)})

    def _start_radar_monitor(self) -> None:
        """Run the public-source hourly monitor only after explicit opt-in."""
        enabled = core.load_settings().get("appBehavior", {}).get("radarMonitoring") is True
        if not enabled or self._closed or self._restart_prepared:
            self._stop_radar_monitor()
            self.radar_monitor_status.update(enabled=False, status="manual", nextCheckAt=None)
            return
        self.radar_monitor_status["enabled"] = True
        if self.radar_monitor_thread and self.radar_monitor_thread.is_alive() and not self.radar_monitor_stop.is_set():
            return
        self.radar_monitor_stop = threading.Event()
        self.radar_monitor_wake = threading.Event()
        self.radar_monitor_thread = threading.Thread(target=self._run_radar_monitor, args=(self.radar_monitor_stop, self.radar_monitor_wake), name="radar-hourly-monitor", daemon=True)
        self.radar_monitor_thread.start()

    def check_radar_now(self) -> dict:
        result = self.radar.run_monitor(force=True)
        state = result.get("state") if isinstance(result.get("state"), dict) else {}
        self.radar_monitor_status.update(
            lastCheckedAt=state.get("lastSuccessAt") or state.get("lastRunAt"),
            lastResult=state.get("lastResult") or "尚未完成检查", lastError=state.get("lastError"),
        )
        if result.get("newAlert") and isinstance(result.get("alert"), dict) and callable(self.radar_alert_notifier):
            try:
                self.radar_alert_notifier(result["alert"])
            except Exception:
                pass
        return result

    def _stop_radar_monitor(self) -> None:
        stop = getattr(self, "radar_monitor_stop", None)
        wake = getattr(self, "radar_monitor_wake", None)
        if stop is None or wake is None:
            return
        stop.set()
        wake.set()
        thread = getattr(self, "radar_monitor_thread", None)
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def state(self) -> dict:
        try:
            state = core.public_state()
        except Exception as exc:
            if not self.configuration_session.get("recoveryOnly"):
                raise
            state = self._recovery_public_state(exc)
        try:
            diagnostics = maintenance.run_emergency_checks(force=False)
        except Exception as exc:
            detail = core._redact_sensitive_text(exc, limit=300)
            diagnostics = {
                "schemaVersion": 2,
                "checkedAt": None,
                "checks": [
                    {
                        "id": "startup_recovery",
                        "title": "启动状态读取",
                        "status": "error",
                        "detail": detail,
                        "autoFixable": False,
                    }
                ],
                "summary": {"ok": 0, "warning": 0, "error": 1},
                "healthy": False,
                "repairableCount": 0,
                "enableRepair": False,
                "configurationStatus": None,
                "cached": False,
                "needsManualCheck": True,
            }
        state["diagnostics"] = diagnostics
        state["status"] = maintenance.merge_configuration_diagnostics(state.get("status"), diagnostics)
        state["validation"] = self.validation
        state["web2apiStatus"] = self.web2api.status()
        state["oauthStatus"] = self.oauth.state()
        state["relayLoginStatus"] = self.relay_portal.public_state()
        state["configurationSession"] = json.loads(json.dumps(self.configuration_session))
        with self.account_refresh_lock:
            state["accountRefreshStatus"] = json.loads(json.dumps(self.account_refresh_status))
            state["accountAutoRefreshStatus"] = json.loads(json.dumps(self.account_auto_refresh_status))
        state["mailHealthStatus"] = json.loads(json.dumps(self.mail_health_status))
        state["updateCheckStatus"] = json.loads(json.dumps(self.update_check_status))
        state["radarMonitorStatus"] = json.loads(json.dumps(self.radar_monitor_status))
        if getattr(self, "quota_estimates", None):
            self.quota_estimates.decorate(state.get("settings", {}).get("accounts", []))
        return state

    def account_snapshot(self) -> dict:
        snapshot = core.public_account_snapshot()
        if getattr(self, "quota_estimates", None):
            self.quota_estimates.decorate(snapshot.get("accounts", []))
        with self.account_refresh_lock:
            snapshot["accountRefreshStatus"] = json.loads(json.dumps(self.account_refresh_status))
            snapshot["accountAutoRefreshStatus"] = json.loads(json.dumps(self.account_auto_refresh_status))
        return snapshot

    def configuration_snapshot(self) -> dict:
        """Return the small startup state without rebuilding the full dashboard."""
        with self.lock:
            return json.loads(json.dumps(self.configuration_session))

    def _prune_switch_operations_locked(self) -> None:
        cutoff = time.monotonic() - 15 * 60
        retained = [
            (operation_id, record)
            for operation_id, record in self.switch_operations.items()
            if float(record.get("_updatedMonotonic") or 0) >= cutoff
        ]
        retained.sort(key=lambda item: float(item[1].get("_updatedMonotonic") or 0), reverse=True)
        self.switch_operations = dict(retained[:32])

    def begin_switch_operation(
        self,
        operation_id: str,
        *,
        target_id: str,
        target_name: str,
        target_kind: str,
    ) -> dict:
        operation_id = str(operation_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", operation_id):
            raise core.ManagerError("切换操作标识无效，请刷新界面后重试。")
        now = time.monotonic()
        with self.switch_operation_lock:
            self._prune_switch_operations_locked()
            existing = self.switch_operations.get(operation_id)
            if existing and existing.get("status") == "running":
                raise core.ManagerError("该切换操作已经在运行，请勿重复提交。")
            record = {
                "operationId": operation_id,
                "targetId": str(target_id or ""),
                "targetName": str(target_name or target_id or "Codex"),
                "targetKind": str(target_kind or "account"),
                "status": "running",
                "phase": "queued",
                "progress": 1,
                "message": "已收到切换请求，正在准备",
                "elapsedMs": 0,
                "timings": {},
                "startedAt": core.now_iso(),
                "updatedAt": core.now_iso(),
                "error": None,
                "_startedMonotonic": now,
                "_updatedMonotonic": now,
            }
            self.switch_operations[operation_id] = record
            return self.switch_operation_status(operation_id)

    def update_switch_operation(self, operation_id: str, payload: dict) -> dict:
        now = time.monotonic()
        with self.switch_operation_lock:
            record = self.switch_operations.get(str(operation_id or ""))
            if not record:
                raise core.ManagerError("切换进度已经过期，请重新操作。")
            for key in (
                "phase",
                "progress",
                "message",
                "status",
                "elapsedMs",
                "timings",
                "error",
                "failedPhase",
                "recoveryState",
                "canRetry",
            ):
                if key in payload:
                    record[key] = payload[key]
            record["updatedAt"] = core.now_iso()
            record["_updatedMonotonic"] = now
            return self.switch_operation_status(operation_id)

    def finish_switch_operation(
        self,
        operation_id: str,
        *,
        result: dict | None = None,
        error: BaseException | str | None = None,
    ) -> dict:
        with self.switch_operation_lock:
            record = self.switch_operations.get(str(operation_id or ""))
            if not record:
                raise core.ManagerError("切换进度已经过期，请重新操作。")
            now = time.monotonic()
            if error is not None:
                record.setdefault("failedPhase", record.get("phase"))
            reporter_already_failed = record.get("phase") == "failed"
            record["status"] = "error" if error is not None else "completed"
            record["phase"] = "failed" if error is not None else "completed"
            record["progress"] = 100
            if error is not None:
                # A reporter may have already distinguished "no state was
                # written" from "snapshot restored".  Preserve that more
                # accurate result instead of replacing it with a generic claim.
                if not (reporter_already_failed and str(record.get("message") or "").strip()):
                    if record.get("failedPhase") in {"queued", "preflight", "credentials", "prepared"}:
                        record["message"] = "预检未通过，原账号与配置未改动"
                        record["recoveryState"] = "unchanged"
                    else:
                        record["message"] = "切换未完成，请查看错误详情"
                record.setdefault("canRetry", True)
            else:
                record["message"] = "Codex 已启动并完成账号/模型回验"
                visibility = (result.get("sessionSync") or {}).get("visibility") if isinstance(result, dict) else None
                if isinstance(visibility, dict):
                    visibility = {**visibility, **(visibility.get("completion") or {})}
                    record["sessionVisibility"] = {
                        key: visibility[key] for key in ("changed", "status", "partial", "deferredSessions", "catalogMissing", "catalogBlocked", "remainingActions", "message", "reason") if key in visibility
                    }
            record["error"] = core._redact_sensitive_text(error, limit=360) if error is not None else None
            if isinstance(result, dict) and isinstance(result.get("performance"), dict):
                performance = result["performance"]
                record["elapsedMs"] = int(performance.get("totalMs") or 0)
                record["timings"] = performance.get("stages") or record.get("timings") or {}
            else:
                record["elapsedMs"] = max(
                    0,
                    round((now - float(record.get("_startedMonotonic") or now)) * 1000),
                )
            record["updatedAt"] = core.now_iso()
            record["_updatedMonotonic"] = now
            return self.switch_operation_status(operation_id)

    def switch_operation_status(self, operation_id: str) -> dict:
        with self.switch_operation_lock:
            self._prune_switch_operations_locked()
            record = self.switch_operations.get(str(operation_id or ""))
            if not record:
                raise core.ManagerError("未找到该切换进度，可能已经过期。")
            public = {key: value for key, value in record.items() if not key.startswith("_")}
            if public.get("status") == "running":
                public["elapsedMs"] = max(
                    int(public.get("elapsedMs") or 0),
                    round((time.monotonic() - float(record.get("_startedMonotonic") or time.monotonic())) * 1000),
                )
            return json.loads(json.dumps(public))

    def schedule_account_refresh(self, account_ids: list[str]) -> dict:
        requested = list(dict.fromkeys(str(item) for item in account_ids if str(item).strip()))
        if not requested:
            with self.account_refresh_lock:
                return json.loads(json.dumps(self.account_refresh_status))
        with self.account_refresh_lock:
            if self._closed or self.account_refresh_stop.is_set():
                return json.loads(json.dumps(self.account_refresh_status))
            queued = set(self.account_refresh_pending)
            added = [item for item in requested if item not in self.account_refresh_active and item not in queued]
            self.account_refresh_pending.extend(added)
            if self.account_refresh_status.get("status") not in {"running", "queued"}:
                self.account_refresh_status.update(
                    {
                        "status": "queued",
                        "total": 0,
                        "completed": 0,
                        "ready": 0,
                        "partial": 0,
                        "error": 0,
                        "currentAccountId": None,
                        "activeAccountIds": [],
                        "startedAt": core.now_iso(),
                        "finishedAt": None,
                    }
                )
            self.account_refresh_status["total"] += len(added)
            self.account_refresh_threads = [thread for thread in self.account_refresh_threads if thread.is_alive()]
            worker_target = min(2, len(self.account_refresh_pending) + len(self.account_refresh_active))
            while len(self.account_refresh_threads) < worker_target:
                worker = threading.Thread(
                    target=self._run_account_refresh,
                    name=f"codex-agent-manager-import-refresh-{len(self.account_refresh_threads) + 1}",
                    daemon=True,
                )
                self.account_refresh_threads.append(worker)
                worker.start()
            return json.loads(json.dumps(self.account_refresh_status))

    def _run_account_refresh(self) -> None:
        worker = threading.current_thread()
        while not self.account_refresh_stop.is_set():
            with self.account_refresh_lock:
                if not self.account_refresh_pending:
                    self.account_refresh_threads = [item for item in self.account_refresh_threads if item is not worker]
                    if not self.account_refresh_threads and not self.account_refresh_active:
                        self.account_refresh_status.update(
                            {
                                "status": "completed",
                                "currentAccountId": None,
                                "activeAccountIds": [],
                                "finishedAt": core.now_iso(),
                            }
                        )
                    return
                account_id = self.account_refresh_pending.pop(0)
                self.account_refresh_active.add(account_id)
                self.account_refresh_status.update(
                    {
                        "status": "running",
                        "currentAccountId": account_id,
                        "activeAccountIds": sorted(self.account_refresh_active),
                    }
                )
            try:
                result = core.refresh_codex_accounts(
                    [account_id],
                    max_workers=1,
                    parallel_operations=False,
                    commit_guard=lambda: not self.account_refresh_stop.is_set() and not self._closed,
                )
                if getattr(self, "quota_estimates", None):
                    self.quota_estimates.schedule()
                if result.get("cancelled"):
                    state = "cancelled"
                else:
                    state = "ready" if result.get("ready") else "partial" if result.get("partial") else "error"
            except Exception:
                state = "error"
            with self.account_refresh_lock:
                self.account_refresh_active.discard(account_id)
                if state != "cancelled":
                    self.account_refresh_status["completed"] += 1
                    self.account_refresh_status[state] += 1
                active_ids = sorted(self.account_refresh_active)
                self.account_refresh_status["activeAccountIds"] = active_ids
                self.account_refresh_status["currentAccountId"] = active_ids[0] if active_ids else None
        with self.account_refresh_lock:
            self.account_refresh_pending.clear()
            self.account_refresh_threads = [item for item in self.account_refresh_threads if item is not worker]
            if not self.account_refresh_threads:
                self.account_refresh_status.update(
                    {
                        "status": "cancelled",
                        "currentAccountId": None,
                        "activeAccountIds": [],
                        "finishedAt": core.now_iso(),
                    }
                )

    def _stop_account_refresh_workers(self) -> None:
        import web2api_service
        web2api_service.stop_codex_session_usage_backfill(timeout_seconds=1.0)
        if getattr(self, "quota_estimates", None):
            self.quota_estimates.close()
        self.account_refresh_stop.set()
        with self.account_refresh_lock:
            self.account_refresh_pending.clear()
            workers = [
                thread
                for thread in self.account_refresh_threads
                if thread.is_alive() and thread is not threading.current_thread()
            ]
        deadline = time.monotonic() + 2.0
        for thread in workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

    def validate(self, *, run_doctor: bool = True) -> dict:
        with self.lock:
            self.validation = core.validate_configuration(run_doctor=run_doctor)
            return self.validation

    def prepare_for_restart(self, timeout_seconds: float = 2.0) -> dict:
        """Cancel manager background work under one shared deadline.

        The replacement process can unpack while these workers drain.  Keeping
        the HTTP and gateway servers alive during this phase makes the visible
        outage start only after the replacement has reached the mutex handoff.
        """
        with self.lock:
            if self._closed:
                return self._restart_prepare_result or {
                    "prepared": True,
                    "alreadyClosed": True,
                    "elapsedMs": 0.0,
                    "lingeringWorkers": [],
                }
            if self._restart_prepared:
                return self._restart_prepare_result or {
                    "prepared": True,
                    "elapsedMs": 0.0,
                    "lingeringWorkers": [],
                }
            self._restart_prepared = True
            started = time.monotonic()
            if getattr(self, "app_updates", None):
                self.app_updates.cancel_download()
            import web2api_service
            web2api_service.stop_codex_session_usage_backfill(timeout_seconds=0)
            self.update_check_stop.set()
            self.radar_monitor_stop.set()
            self.radar_monitor_wake.set()
            self.account_auto_refresh_stop.set()
            self.account_auto_refresh_wake.set()
            self.mail_health_stop.set()
            self.mail_health_wake.set()
            self.account_refresh_stop.set()
            with self.account_refresh_lock:
                self.account_refresh_pending.clear()
                refresh_workers = list(self.account_refresh_threads)
            candidates = [
                self.radar_monitor_thread,
                self.account_auto_refresh_thread,
                self.mail_health_thread,
                *refresh_workers,
            ]
            workers = list(
                dict.fromkeys(
                    thread
                    for thread in candidates
                    if thread is not None
                    and thread is not threading.current_thread()
                    and thread.is_alive()
                )
            )
        deadline = time.monotonic() + max(0.0, min(float(timeout_seconds), 5.0))
        for thread in workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        lingering = [thread.name for thread in workers if thread.is_alive()]
        if not web2api_service.stop_codex_session_usage_backfill(timeout_seconds=max(0.0, deadline - time.monotonic())):
            lingering.append("codex-usage-history-backfill")
        result = {
            "prepared": True,
            "elapsedMs": round((time.monotonic() - started) * 1000, 1),
            "lingeringWorkers": lingering,
        }
        with self.lock:
            self._restart_prepare_result = result
        return json.loads(json.dumps(result))

    def resume_after_failed_restart(self) -> None:
        """Resume periodic workers when preflight failed before shutdown."""
        with self.lock:
            if self._closed or not self._restart_prepared:
                return
            self._restart_prepared = False
            self._restart_prepare_result = None
            self.update_check_stop.clear()
            self.account_refresh_stop.clear()
            self.radar_monitor_stop.clear()
            self.account_auto_refresh_stop.clear()
            self.account_auto_refresh_wake.set()
            self.mail_health_stop.clear()
            self.mail_health_wake.set()
            active = bool(self.configuration_session.get("active"))
        if active:
            import web2api_service
            web2api_service.enable_codex_session_usage_backfill()
            self._start_account_auto_refresh()
            self._start_mail_health_checks()
            self._start_update_checks()
            if core.load_settings().get("appBehavior", {}).get("radarMonitoring") is True:
                self._start_radar_monitor()

    def close_for_restart(self, *, exit_only: bool = False) -> dict:
        """Preserve Codex only for a handoff or the explicit manager-only exit."""
        prepared = self.prepare_for_restart()
        with self.lock:
            if self._closed:
                return self._close_result or {"preserved": True, "alreadyClosed": True}
            session_was_active = bool(self.configuration_session.get("active"))
            self._closed = True
            errors = []
            if getattr(self, "app_updates", None):
                try:
                    self.app_updates.close(timeout=2.0)
                except Exception as exc:
                    errors.append(f"停止更新服务：{str(exc)[:300]}")
            try:
                web2api_running = bool(self.web2api.status().get("running"))
            except Exception as exc:
                web2api_running = bool(getattr(self.web2api, "server", None))
                errors.append(f"读取本地服务状态：{str(exc)[:300]}")
            if web2api_running:
                try:
                    self.web2api.stop(disable=False)
                except Exception as exc:
                    errors.append(f"停止本地服务：{str(exc)[:300]}")
            try:
                self.oauth.close()
            except Exception as exc:
                errors.append(f"取消 OAuth：{str(exc)[:300]}")
            try:
                self.relay_portal.close(silent=True)
            except Exception as exc:
                errors.append(f"关闭中转站登录窗口：{str(exc)[:300]}")
            self.configuration_session.update(
                {
                    "status": "exited" if exit_only else "restarting",
                    "active": session_was_active,
                    "message": (
                        "Agent Manager 已退出；保留当前 Codex 进程和临时配置。"
                        if exit_only
                        else "正在快速重启管理器；不会关闭、重开或修改 Codex 进程。"
                    ),
                    "error": "；".join(errors) or None,
                }
            )
            self._close_result = {
                "completed": True,
                "restorationComplete": False,
                "preserved": True,
                "restored": False,
                "closedCodex": None,
                "launch": None,
                "errors": errors,
                "restartPreparation": prepared,
            }
            return self._close_result

    def close(self) -> dict:
        # Worker callbacks may need self.lock to finish. Drain them before
        # acquiring it for the restore transaction.
        prepared = self.prepare_for_restart()
        with self.lock:
            if self._closed:
                return self._close_result or {"restored": False, "alreadyClosed": True}
            closed = None
            session_was_active = bool(
                self.configuration_session.get("active")
                or self.configuration_session.get("restorePending")
            )
            restored = None
            restore_attempted = False
            try:
                if prepared.get("lingeringWorkers"):
                    raise core.ManagerError("后台任务尚未停止，已取消配置恢复；请稍后重试退出。")
                if session_was_active:
                    if core._require_codex_process_scan_known():
                        closed = _close_codex_processes_safely()
                    # A successful termination request is not evidence that
                    # Codex is gone. Also reject unknown/partial process scans.
                    if core._require_codex_process_scan_known():
                        raise core.ManagerError("Codex 仍在运行，未恢复配置或停止本地服务。")
                restore_attempted = True
                restored = core.restore_runtime_configuration_overlay()
                if self.configuration_session.get("restorePending") and not core.RUNTIME_OVERLAY_FILE.exists() and not restored.get("restored"):
                    previous = self.configuration_session.get("restore") or {}
                    session_id = previous.get("sessionId")
                    evidence = core.read_json(core.RUNTIME_RESTORE_STATUS_FILE, {})
                    if (
                        session_id and isinstance(evidence, dict)
                        and evidence.get("sessionId") == session_id
                        and evidence.get("restored") is True
                        and not evidence.get("partial")
                        and not evidence.get("conflicts")
                        and not evidence.get("remainingFiles")
                        and not evidence.get("remainingEnvironment")
                    ):
                        restored = evidence
                pending = bool(
                    core.RUNTIME_OVERLAY_FILE.exists()
                    or restored.get("partial")
                    or restored.get("conflicts")
                    or restored.get("remainingFiles")
                    or restored.get("remainingEnvironment")
                    or (session_was_active and not restored.get("restored"))
                    or (not restored.get("restored") and restored.get("warnings"))
                )
                if pending:
                    remaining = [
                        *restored.get("conflicts", []),
                        *restored.get("remainingFiles", []),
                        *restored.get("remainingEnvironment", []),
                        *restored.get("warnings", []),
                    ]
                    detail = "、".join(str(item) for item in dict.fromkeys(remaining))[:500]
                    raise core.ManagerError(
                        "临时 Codex 配置尚未完全恢复；管理器与恢复记录已保留，请处理后重试退出。"
                        + (f" {detail}" if detail else "")
                    )
            except Exception as exc:
                error = core._redact_sensitive_text(exc, limit=700)
                self.configuration_session.update(
                    {
                        "status": "error",
                        "active": session_was_active if not restore_attempted else False,
                        "restorePending": session_was_active or restore_attempted,
                        "recoveryOnly": restore_attempted,
                        "recoverable": True,
                        "message": "退出未完成；管理器和本地服务保持运行，请检查错误后重试。",
                        "restore": restored,
                        "error": error,
                    }
                )
                if not restore_attempted:
                    self.resume_after_failed_restart()
                return {
                    "completed": False,
                    "restorationComplete": False,
                    "restored": False,
                    "restore": restored,
                    "closedCodex": closed,
                    "errors": [error],
                    "blocked": "restoration" if restore_attempted else "codex-or-workers",
                }
            # Do not mark this runtime closed or dismantle the gateway until
            # restoration is verified. Failed attempts remain retryable.
            self._closed = True
            errors = []
            for label, stop in (
                ("停止更新检查", self._stop_update_checks),
                ("停止雷达监控", self._stop_radar_monitor),
                ("停止账号自动刷新", self._stop_account_auto_refresh),
                ("停止邮箱检查", self._stop_mail_health_checks),
                ("停止账号刷新任务", self._stop_account_refresh_workers),
            ):
                try:
                    stop()
                except Exception as exc:
                    errors.append(f"{label}：{str(exc)[:300]}")
            if getattr(self, "app_updates", None):
                try:
                    self.app_updates.close(timeout=2.0)
                except Exception as exc:
                    errors.append(f"停止更新服务：{str(exc)[:300]}")
            try:
                web2api_running = bool(self.web2api.status().get("running"))
            except Exception as exc:
                web2api_running = bool(getattr(self.web2api, "server", None))
                errors.append(f"读取本地服务状态：{str(exc)[:300]}")
            if web2api_running:
                try:
                    self.web2api.stop(disable=False)
                except Exception as exc:
                    errors.append(f"停止本地服务：{str(exc)[:300]}")
            try:
                self.oauth.close()
            except Exception as exc:
                errors.append(f"取消 OAuth：{str(exc)[:300]}")
            try:
                self.relay_portal.close(silent=True)
            except Exception as exc:
                errors.append(f"关闭中转站登录窗口：{str(exc)[:300]}")
            self.configuration_session.update(
                {
                    "status": "restored" if restored.get("restored") else "closed",
                    "active": False,
                    "restorePending": False,
                    "message": (
                        "已关闭 Codex 并还原默认配置；需要使用时请手动启动 Codex。"
                        if restored.get("restored") and closed is not None
                        else "已还原默认 Codex 配置；需要使用时请手动启动 Codex。"
                        if restored.get("restored")
                        else "临时配置未处于活动状态；Codex 不会自动启动。"
                    ),
                    "restore": restored,
                    "restart": None,
                    "error": "；".join(errors) or None,
                }
            )
            self._close_result = {
                "completed": True,
                "restorationComplete": True,
                "restored": bool(restored.get("restored")),
                "restore": restored,
                "closedCodex": closed,
                "launch": None,
                "errors": errors,
            }
            return self._close_result


def activate_web2api_for_codex(runtime: ManagerRuntime, preferred_account_id: str | None = None) -> dict:
    target = preferred_account_id or "pool"
    with core._exclusive_switch_operation("web2api-activate", target):
        return _activate_web2api_for_codex(runtime, preferred_account_id)


def _activate_web2api_for_codex(runtime: ManagerRuntime, preferred_account_id: str | None = None) -> dict:
    settings_before = core.load_settings()
    transaction_snapshot = core._capture_switch_transaction_snapshot(settings_before)
    config_before = json.loads(json.dumps(settings_before.get("web2api", core._default_web2api_settings())))
    launch_plan = None
    generated_key = None
    was_running = runtime.web2api.status()["running"]
    service_started = False
    closed = None
    launch_attempted = False
    session_visibility = None
    try:
        if preferred_account_id:
            preferred = next(
                (
                    item
                    for item in settings_before.get("accounts", [])
                    if str(item.get("id")) == preferred_account_id
                ),
                None,
            )
            if not preferred:
                raise core.ManagerError("账号不存在。")
            if preferred.get("sourceType") != "web_session":
                raise core.ManagerError("只有 Web Session 账号需要通过本地转换模式启动。")
            if not core._account_codex_compatible(preferred) or core.account_invalid_reason(preferred):
                raise core.ManagerError("该 Web Session 尚未通过 Codex 推理授权检测，不能用于对话。")
            if not preferred.get("models"):
                raise core.ManagerError("该 Web Session 尚未同步到可用模型，请刷新账号后重试。")
        core.validate_web2api_codex_pool(core.load_settings(), preferred_account_id)
        launch_plan = core.resolve_codex_launch_plan()
        if not core.service_secret_configured("web2api"):
            generated_key = core.rotate_web2api_key()
        runtime.web2api.start()
        service_started = True
        closed = _close_codex_processes_safely()
        core.set_web2api_codex_active(True, preferred_account_id)
        applied = core.apply_configuration(False)
        session_sync = core.auto_sync_sessions_after_switch(core.AGGREGATE_PROVIDER_ID)
        session_visibility = core._repair_switch_session_visibility()
        session_sync["visibility"] = session_visibility
        launch_attempted = True
        launch = core.launch_codex_app(launch_plan=launch_plan)
        expected_model = str(core.read_toml(core.CONFIG_FILE).get("model") or "")
        launch["readiness"] = core.wait_for_codex_runtime_ready(
            expected_model=expected_model or None, launch_plan=launch_plan,
        )
        launch["retryCount"] = 0
        status = runtime.web2api.status()
    except Exception as exc:
        recovery_errors = []
        if launch_attempted:
            try:
                if core.running_codex_processes():
                    _close_codex_processes_safely(timeout_seconds=8)
            except Exception as cleanup_exc:
                recovery_errors.append(f"关闭未通过验证的 Codex：{str(cleanup_exc)[:300]}")
        if service_started and not was_running:
            try:
                runtime.web2api.stop(disable=not bool(config_before.get("enabled")))
            except Exception as cleanup_exc:
                recovery_errors.append(f"停止本地反代：{str(cleanup_exc)[:300]}")
        recovery_errors.extend(core._restore_switch_transaction_snapshot(transaction_snapshot))
        try:
            core._restore_switch_session_visibility(session_visibility)
        except Exception as recovery_exc:
            recovery_errors.append(f"恢复会话标记：{str(recovery_exc)[:300]}")
        try:
            if closed is not None and launch_plan and not launch_attempted and not recovery_errors:
                core.launch_codex_app(launch_plan=launch_plan)
        except Exception as recovery_exc:
            recovery_errors.append(str(recovery_exc)[:300])
        detail = f"；恢复原状态也失败：{'；'.join(recovery_errors)}" if recovery_errors else "；已原样回滚"
        if launch_attempted:
            detail += "；为避免循环重启，未再次启动 Codex"
        if isinstance(exc, core.ManagerError):
            raise core.ManagerError(f"启用本地反代并切换 Codex 失败：{exc}{detail}") from exc
        raise core.ManagerError(f"启用本地反代并切换 Codex 失败：{exc}{detail}") from exc
    return {
        "status": status,
        "generatedKey": generated_key,
        "closed": closed,
        "applied": applied,
        "sessionSync": session_sync,
        "launch": launch,
        "mode": "web_session_local_conversion" if preferred_account_id else "web2api_pool",
        "preferredAccountId": preferred_account_id,
        "verified": True,
    }


def deactivate_web2api_for_codex(runtime: ManagerRuntime) -> dict:
    with core._exclusive_switch_operation("web2api-deactivate", "openai"):
        return _deactivate_web2api_for_codex(runtime)


def _deactivate_web2api_for_codex(runtime: ManagerRuntime) -> dict:
    settings_before = core.load_settings()
    transaction_snapshot = core._capture_switch_transaction_snapshot(settings_before)
    config_before = json.loads(json.dumps(settings_before.get("web2api", core._default_web2api_settings())))
    active = bool(config_before.get("activeForCodex"))
    if not active:
        applied = core.apply_configuration(False)
        if applied.get("gatewayRequired"):
            status = runtime.web2api.status() if runtime.web2api.status()["running"] else runtime.web2api.start()
            return {
                "status": status,
                "closed": None,
                "applied": applied,
                "launch": None,
                "keptForSubagents": True,
            }
        return {
            "status": runtime.web2api.stop(),
            "closed": None,
            "applied": applied,
            "launch": None,
            "keptForSubagents": False,
        }
    launch_plan = core.resolve_codex_launch_plan()
    closed = _close_codex_processes_safely()
    launch_attempted = False
    session_visibility = None
    try:
        core.set_web2api_codex_active(False)
        applied = core.apply_configuration(False)
        session_sync = core.auto_sync_sessions_after_switch("openai")
        session_visibility = core._repair_switch_session_visibility()
        session_sync["visibility"] = session_visibility
        if applied.get("gatewayRequired"):
            status = runtime.web2api.status() if runtime.web2api.status()["running"] else runtime.web2api.start()
        else:
            status = runtime.web2api.stop()
        launch_attempted = True
        launch = core.launch_codex_app(launch_plan=launch_plan)
        expected_model = str(core.read_toml(core.CONFIG_FILE).get("model") or "")
        launch["readiness"] = core.wait_for_codex_runtime_ready(
            expected_model=expected_model or None, launch_plan=launch_plan,
        )
        launch["retryCount"] = 0
    except Exception as exc:
        recovery_errors = []
        if launch_attempted:
            try:
                if core.running_codex_processes():
                    _close_codex_processes_safely(timeout_seconds=8)
            except Exception as cleanup_exc:
                recovery_errors.append(f"关闭未通过验证的 Codex：{str(cleanup_exc)[:300]}")
        recovery_errors.extend(core._restore_switch_transaction_snapshot(transaction_snapshot))
        try:
            core._restore_switch_session_visibility(session_visibility)
        except Exception as recovery_exc:
            recovery_errors.append(f"恢复会话标记：{str(recovery_exc)[:300]}")
        try:
            if not runtime.web2api.status()["running"]:
                runtime.web2api.start()
            if not launch_attempted and not recovery_errors:
                core.launch_codex_app(launch_plan=launch_plan)
        except Exception as recovery_exc:
            recovery_errors.append(str(recovery_exc)[:300])
        detail = f"；恢复反代状态也失败：{'；'.join(recovery_errors)}" if recovery_errors else "；已原样回滚"
        if launch_attempted:
            detail += "；为避免循环重启，未再次启动 Codex"
        if isinstance(exc, core.ManagerError):
            raise core.ManagerError(f"退出反代模式失败：{exc}{detail}") from exc
        raise core.ManagerError(f"退出反代模式失败：{exc}{detail}") from exc
    return {
        "status": status,
        "closed": closed,
        "applied": applied,
        "sessionSync": session_sync,
        "launch": launch,
        "keptForSubagents": bool(applied.get("gatewayRequired")),
        "verified": True,
    }


def rotate_web2api_key_for_runtime(runtime: ManagerRuntime) -> dict:
    # HTTP authentication reads the stored key for every request. Codex uses a
    # distinct private key, so public rotation never requires terminating it.
    api_key = core.rotate_web2api_key()
    return {
        "apiKey": api_key,
        "status": runtime.web2api.status(),
        "closed": None,
        "applied": None,
        "launch": None,
    }


def _ensure_runtime_gateway(runtime: ManagerRuntime):
    if runtime.web2api.status().get("running"):
        return
    try:
        return runtime.web2api.start()
    except Exception as exc:
        try:
            runtime.web2api.stop(disable=False)
        except Exception as cleanup_exc:
            raise core.ManagerError(
                f"本地路由启动失败，清理未完成：{core._redact_sensitive_text(cleanup_exc, limit=180)}"
            ) from exc
        raise


def save_orchestration_for_runtime(runtime: ManagerRuntime, payload: dict) -> dict:
    return core.save_orchestration_and_apply(payload, ensure_gateway=lambda: _ensure_runtime_gateway(runtime))


class ManagerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], runtime: ManagerRuntime):
        super().__init__(address, handler)
        self.runtime = runtime
        self.api_token = secrets.token_urlsafe(32)
        # A second launch only needs permission to reveal the existing native
        # window.  Keep that capability separate from the full local API token
        # so app-runtime.json never grants access to account/configuration APIs.
        self.activation_token = secrets.token_urlsafe(32)
        # Local maintenance tools need one safe operation without receiving the
        # full browser API token. This token can only request a manager-only
        # quick restart; it cannot read accounts, secrets, or modify settings.
        self.control_token = secrets.token_urlsafe(32)
        self.runtime_nonce = secrets.token_urlsafe(24)
        self.started_at = core.now_iso()
        self.static_dir = STATIC_DIR.resolve()
        self.last_request_at = time.time()
        self.native_window = False
        self.native_window_object = None
        # HTTP readiness is not UI readiness.  Quick restart must keep the old
        # manager alive until the replacement has a visible window and has
        # successfully activated the preserved configuration session.
        self.ui_ready = threading.Event()
        # A second launch can arrive after the local server is healthy but
        # before WebView2 has created the HWND. Treat that as a queued reveal,
        # not as a failed launch followed by an apparently crashing process.
        self.window_activation_requested = threading.Event()
        self.tray = None
        self.tray_thread = None
        self.tray_error = None
        self.tray_lock = threading.RLock()
        self.force_exit = False
        self.quick_restart_requested = False
        self.quick_restart_token = None
        self.quick_restart_launch: dict | None = None
        self.quick_restart_handoff: dict | None = None
        self.quick_restart_timing: dict = {}
        self.exit_only_requested = False
        self.shutdown_lock = threading.RLock()
        self.shutdown_started = threading.Event()
        self.shutdown_finished = threading.Event()
        self.shutdown_aborted = threading.Event()
        self.allow_forced_process_exit = False
        self.mutation_condition = threading.Condition(threading.RLock())
        self.mutations_open = True
        self.inflight_mutations = 0
        self._thread_slots = threading.BoundedSemaphore(MAX_MANAGEMENT_THREADS)
        self.ui_bootstrap_lock = threading.RLock()
        self.ui_bootstrap_tokens: dict[str, tuple[float, int]] = {}

    def server_bind(self) -> None:
        _bind_exclusive_loopback(self)

    def ui_url(self) -> str:
        port = int(self.server_address[1])
        now = time.monotonic()
        token = secrets.token_urlsafe(32)
        with self.ui_bootstrap_lock:
            self.ui_bootstrap_tokens = {
                candidate: record
                for candidate, record in self.ui_bootstrap_tokens.items()
                if record[0] > now and record[1] < UI_BOOTSTRAP_MAX_EXCHANGES
            }
            self.ui_bootstrap_tokens[token] = (now + UI_BOOTSTRAP_TOKEN_TTL_SECONDS, 0)
        return f"http://127.0.0.1:{port}/#bootstrap={urllib.parse.quote(token, safe='')}"

    def consume_ui_bootstrap(self, token: str) -> bool:
        now = time.monotonic()
        supplied = str(token or "")
        with self.ui_bootstrap_lock:
            record = self.ui_bootstrap_tokens.get(supplied)
            self.ui_bootstrap_tokens = {
                candidate: candidate_record
                for candidate, candidate_record in self.ui_bootstrap_tokens.items()
                if candidate_record[0] > now
                and candidate_record[1] < UI_BOOTSTRAP_MAX_EXCHANGES
            }
            record = self.ui_bootstrap_tokens.get(supplied)
            if record is None:
                return False
            self.ui_bootstrap_tokens[supplied] = (record[0], record[1] + 1)
            return True

    def process_request(self, request: object, client_address: object) -> None:
        """Bound local management concurrency so a faulty peer cannot exhaust threads."""
        if not self._thread_slots.acquire(blocking=False):
            try:
                if hasattr(request, "settimeout"):
                    request.settimeout(0.2)
                # Consume only the already-arriving HTTP headers. Closing a
                # Windows socket with unread request bytes can turn the intended
                # 429 into WSAECONNABORTED at the client. This loop is strictly
                # time- and size-bounded, so overload handling cannot become a
                # new slow-client denial of service.
                received = b""
                while len(received) < 16_384 and b"\r\n\r\n" not in received:
                    chunk = request.recv(min(4096, 16_384 - len(received)))
                    if not chunk:
                        break
                    received += chunk
                body = json.dumps(
                    {"ok": False, "error": "Agent Manager 当前请求较多，请稍后重试。"},
                    ensure_ascii=False,
                ).encode("utf-8")
                headers = (
                    b"HTTP/1.1 429 Too Many Requests\r\n"
                    b"Content-Type: application/json; charset=utf-8\r\n"
                    b"Cache-Control: no-store\r\nRetry-After: 1\r\nConnection: close\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                )
                request.sendall(headers + body)
            except (AttributeError, OSError, TimeoutError):
                pass
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
                request.settimeout(MANAGEMENT_CLIENT_TIMEOUT_SECONDS)
            super().process_request_thread(request, client_address)
        finally:
            self._thread_slots.release()

    def begin_mutating_request(self) -> bool:
        with self.mutation_condition:
            if not self.mutations_open or self.shutdown_started.is_set():
                return False
            self.inflight_mutations += 1
            return True

    def finish_mutating_request(self) -> None:
        with self.mutation_condition:
            if self.inflight_mutations > 0:
                self.inflight_mutations -= 1
            if self.inflight_mutations == 0:
                self.mutation_condition.notify_all()

    def stop_accepting_mutations(self) -> None:
        with self.mutation_condition:
            self.mutations_open = False

    def reopen_mutations(self) -> None:
        with self.mutation_condition:
            self.mutations_open = True
            self.mutation_condition.notify_all()

    def wait_for_mutations(self, timeout_seconds: float = MUTATION_DRAIN_TIMEOUT_SECONDS) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        with self.mutation_condition:
            while self.inflight_mutations:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.mutation_condition.wait(remaining)
            return True

    def show_native_window(self) -> bool:
        """Reveal and restore the primary window from any local UI thread."""
        window = self.native_window_object
        if not self.native_window or window is None or self.shutdown_started.is_set():
            return False
        shown = False
        for attempt in range(20 if os.name == "nt" else 1):
            try:
                window.show()
                shown = True
            except Exception:
                pass
            try:
                window.restore()
                shown = True
            except Exception:
                pass
            # A successful pywebview method call only means the command was
            # queued; it does not prove WebView2 created a visible native HWND.
            # On Windows, require a real process window before Codex is closed.
            if os.name != "nt" or focus_process_window(os.getpid()):
                return shown
            if attempt < 19:
                time.sleep(0.1)
        return False

    def tray_status(self) -> dict:
        return {
            "available": bool(self.native_window),
            "ready": bool(self.tray is not None and not self.tray_error and self.tray.visible),
            "error": self.tray_error,
        }

    def notify_radar_alert(self, alert: dict) -> bool:
        """Keep community predictions distinct from source-confirmed alerts."""
        if not isinstance(alert, dict) or alert.get("level") not in {"A", "B", "P"}:
            return False
        if self.tray is None:
            try:
                behavior = core.load_settings().get("appBehavior", {})
                if not behavior.get("closeToTray") and not behavior.get("radarMonitoring"):
                    return False
            except Exception:
                return False
            if not self.ensure_tray():
                return False
        lines = [
            "【社区预测预警，非官方确认】" if alert.get("level") == "P" else f"【{alert.get('level')}级】",
            f"证据：{str(alert.get('evidence') or '')[:160]}",
            f"窗口：{str(alert.get('window') or '')[:80]}",
            f"建议：{str(alert.get('advice') or '继续观察')[:40]}",
        ]
        try:
            self.tray.notify("\n".join(lines), "Agent Manager · Codex 重置预警")
            return True
        except Exception as exc:
            self.tray_error = str(exc)[:300]
            return False

    def ensure_tray(self) -> bool:
        if not self.native_window:
            return False
        with self.tray_lock:
            if self.shutdown_started.is_set():
                return False
            if self.tray is not None:
                return bool(not self.tray_error and getattr(self.tray, "visible", False))
            try:
                import pystray
                from PIL import Image

                icon_path = RESOURCE_ROOT / "assets" / "app-icon.png"
                image = Image.open(icon_path).convert("RGBA")

                def show_window(_icon=None, _item=None) -> None:
                    self.show_native_window()

                def exit_application(icon=None, _item=None) -> None:
                    self.force_exit = True
                    if icon is not None:
                        try:
                            icon.stop()
                        except Exception:
                            pass
                    request_application_shutdown(self)

                menu = pystray.Menu(
                    pystray.MenuItem("显示 Agent Manager", show_window, default=True),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("彻底退出", exit_application),
                )
                tray = pystray.Icon("codex-agent-manager", image, core.APP_NAME, menu)
                ready = threading.Event()

                def setup_tray(icon) -> None:
                    try:
                        icon.visible = True
                        ready.set()
                    except Exception as exc:
                        self.tray_error = str(exc)[:300]
                        ready.set()

                def run_tray() -> None:
                    try:
                        tray.run(setup=setup_tray)
                    except Exception as exc:
                        self.tray_error = str(exc)[:300]
                    finally:
                        ready.set()
                        with self.tray_lock:
                            lost = self.tray is tray
                            if lost:
                                self.tray = None
                                self.tray_thread = None
                                self.tray_error = self.tray_error or "托盘图标已意外停止。"
                        if lost and not self.shutdown_started.is_set():
                            self.show_native_window()

                # pystray.run_detached() creates an untracked non-daemon thread on
                # Windows.  If that message loop misses WM_STOP it keeps the frozen
                # executable alive after both the window and tray icon disappear.
                # Own the thread instead: it is daemonised, tracked and joined on
                # shutdown, so a stale tray loop can never pin the process.
                tray_thread = threading.Thread(
                    target=run_tray,
                    name="codex-agent-manager-tray",
                    daemon=True,
                )
                self.tray = tray
                self.tray_thread = tray_thread
                self.tray_error = None
                tray_thread.start()
            except Exception as exc:
                self.tray = None
                self.tray_thread = None
                self.tray_error = str(exc)
                return False
        # The native message loop must have installed the icon before the
        # close handler hides the only window. Creating a Thread is not enough.
        ready.wait(3.0)
        with self.tray_lock:
            success = bool(self.tray is tray and not self.tray_error and tray.visible)
        if not success:
            error = self.tray_error or "托盘图标未能及时就绪；窗口保持打开。"
            self.stop_tray()
            self.tray_error = error
        return success

    def stop_tray(self) -> None:
        with self.tray_lock:
            tray = self.tray
            tray_thread = self.tray_thread
            self.tray = None
            self.tray_thread = None
            self.tray_error = None
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
        if tray_thread is not None and tray_thread is not threading.current_thread():
            tray_thread.join(timeout=2)

    def claim_shutdown(self) -> bool:
        with self.shutdown_lock:
            if self.shutdown_started.is_set():
                return False
            self.shutdown_finished.clear()
            self.shutdown_aborted.clear()
            self.stop_accepting_mutations()
            self.shutdown_started.set()
            return True


class RequestHandler(BaseHTTPRequestHandler):
    server: ManagerServer

    def handle_one_request(self) -> None:
        self._mutation_registered = False
        self._request_body_handled = False
        try:
            super().handle_one_request()
        finally:
            # Several no-payload DELETE routes and early rejection branches do
            # not otherwise consume urllib's small `{}` body.  On Windows, a
            # close with unread inbound bytes can replace a valid response with
            # WSAECONNABORTED.  Drain only a bounded body after the response has
            # been flushed; parsers mark their body handled to avoid a second
            # read.
            if not self._request_body_handled:
                self._discard_small_request_body()
            if self._mutation_registered:
                self._mutation_registered = False
                self.server.finish_mutating_request()

    def log_message(self, format: str, *args: object) -> None:
        return

    def _headers(self, content_type: str, length: int, status: int = 200, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; font-src 'self'; frame-ancestors 'none'",
        )
        self.end_headers()

    def _json(self, payload: object, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(data) > MAX_RESPONSE_BYTES:
            status = 413
            data = json.dumps(
                {"ok": False, "error": "响应内容过大，请缩小查询范围后重试。"},
                ensure_ascii=False,
            ).encode("utf-8")
        self._headers("application/json; charset=utf-8", len(data), status)
        self.wfile.write(data)

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"ok": False, "error": message}, status)

    def _discard_small_request_body(self, limit: int = 65_536) -> None:
        """Drain a small pending body before an early close on Windows.

        Closing a TCP socket while the peer's POST body is still unread can
        make Winsock replace an already-written HTTP response with
        WSAECONNABORTED.  The shutdown gate rejects requests before their route
        parser runs, so consume only a strictly bounded body first.  Oversized
        or malformed requests remain subject to the normal connection timeout
        and are never buffered in memory here.
        """
        if getattr(self, "_request_body_handled", False):
            return
        self._request_body_handled = True
        try:
            headers = getattr(self, "headers", None)
            values = headers.get_all("Content-Length") if headers is not None else None
            if values and len(values) != 1:
                self.close_connection = True
                return
            raw_length = str(values[0]).strip() if values else "0"
            if not re.fullmatch(r"[0-9]+", raw_length):
                self.close_connection = True
                return
            length = int(raw_length, 10)
        except (AttributeError, TypeError, ValueError):
            self.close_connection = True
            return
        if length <= 0:
            return
        if length > max(0, int(limit)):
            self.close_connection = True
            return
        remaining = length
        previous_timeout = None
        try:
            connection = getattr(self, "connection", None)
            if connection is not None and hasattr(connection, "gettimeout"):
                previous_timeout = connection.gettimeout()
                connection.settimeout(min(0.25, previous_timeout or 0.25))
            while remaining:
                chunk = self.rfile.read(min(65_536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, TimeoutError):
            self.close_connection = True
        finally:
            try:
                if connection is not None and previous_timeout is not None:
                    connection.settimeout(previous_timeout)
            except (AttributeError, OSError):
                self.close_connection = True

    def _internal_error(self, exc: Exception) -> None:
        # The desktop UI does not need Python paths, OS details or upstream
        # payload fragments. Keep unexpected exceptions out of HTTP responses;
        # expected user-facing failures already use the typed error branches.
        try:
            self.server.runtime.last_error = core._redact_sensitive_text(exc, limit=500)
        except Exception:
            pass
        self._error("Agent Manager 处理请求时发生内部错误，请重试或运行诊断。", 500)

    def _authorized(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Agent-Manager-Token", ""), self.server.api_token)

    def _request_host_is_valid(self) -> bool:
        supplied = str(self.headers.get("Host") or "").strip()
        if not supplied or any(character in supplied for character in "\r\n/\\"):
            return False
        try:
            parsed = urllib.parse.urlsplit(f"//{supplied}")
            hostname = str(parsed.hostname or "").rstrip(".").casefold()
            port = parsed.port
        except ValueError:
            return False
        return hostname in {"127.0.0.1", "localhost", "::1"} and port == int(self.server.server_address[1])

    def _content_length_values(self) -> list[str]:
        """Return raw Content-Length header values without collapsing duplicates."""

        try:
            values = self.headers.get_all("Content-Length") or []
        except (AttributeError, TypeError):
            values = []
        return [str(value).strip() for value in values if str(value).strip()]

    def _request_framing_is_valid(self) -> bool:
        """Reject ambiguous HTTP framing before any route can consume a body.

        ``BaseHTTPRequestHandler`` deliberately does not implement chunked
        request decoding.  Accepting a second Content-Length or a
        Transfer-Encoding header anyway would leave different intermediaries
        disagreeing about where the next request starts.
        """

        transfer_encoding = []
        try:
            transfer_encoding = self.headers.get_all("Transfer-Encoding") or []
        except (AttributeError, TypeError):
            pass
        if any(str(value).strip() for value in transfer_encoding):
            self.close_connection = True
            self._error("不支持 Transfer-Encoding 请求，请使用带长度的 JSON 请求。", 400)
            return False
        content_lengths = self._content_length_values()
        if len(content_lengths) > 1:
            self.close_connection = True
            self._error("请求包含重复的 Content-Length，已拒绝。", 400)
            return False
        if content_lengths and not re.fullmatch(r"[0-9]+", content_lengths[0]):
            self.close_connection = True
            self._error("请求长度无效。", 400)
            return False
        return True

    def _write_origin_is_valid(self) -> bool:
        fetch_site = str(self.headers.get("Sec-Fetch-Site") or "").strip().casefold()
        if fetch_site == "cross-site":
            return False
        supplied = str(self.headers.get("Origin") or "").strip()
        if not supplied:
            return True
        try:
            parsed = urllib.parse.urlsplit(supplied)
            hostname = str(parsed.hostname or "").rstrip(".").casefold()
            port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
        except ValueError:
            return False
        return bool(
            parsed.scheme.casefold() == "http"
            and hostname in {"127.0.0.1", "localhost", "::1"}
            and port == int(self.server.server_address[1])
            and not parsed.username
            and not parsed.password
        )

    def _validate_local_request(self, write: bool = False) -> bool:
        if not self._request_host_is_valid():
            self._error("本地请求 Host 与 Agent Manager 监听地址不匹配。", 421)
            return False
        if not self._request_framing_is_valid():
            return False
        if write and not self._write_origin_is_valid():
            self._error("已拒绝来自其他网页或端口的跨站修改请求。", 403)
            return False
        return True

    def _request_body_limit(self) -> int:
        path = urlparse(self.path).path
        if path in {
            "/api/accounts/import",
            "/api/accounts/import-preview",
            "/api/accounts/import-batch",
        }:
            return min(MAX_BODY_BYTES, MAX_IMPORT_BODY_BYTES)
        if path == "/api/codex-config":
            return min(MAX_BODY_BYTES, MAX_CONFIG_BODY_BYTES)
        return min(MAX_BODY_BYTES, DEFAULT_JSON_BODY_BYTES)

    def _read_json(self, optional: bool = False) -> dict:
        if not self._request_framing_is_valid():
            self._request_body_handled = True
            raise core.ManagerError("请求 HTTP framing 无效。")
        content_lengths = self._content_length_values()
        raw_length = content_lengths[0] if content_lengths else "0"
        try:
            length = int(raw_length, 10)
        except (TypeError, ValueError) as exc:
            self._request_body_handled = True
            raise core.ManagerError("请求长度无效。") from exc
        if optional and length == 0:
            self._request_body_handled = True
            return {}
        if length <= 0 or length > self._request_body_limit():
            self._request_body_handled = True
            if length > self._request_body_limit():
                self.close_connection = True
            raise core.ManagerError("请求内容为空或超过当前接口的安全限制。")
        content_type = str(self.headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
        if content_type != "application/json" and not content_type.endswith("+json"):
            self._request_body_handled = True
            raise core.ManagerError("JSON 请求必须使用 Content-Type: application/json。")
        try:
            raw = self.rfile.read(length)
            self._request_body_handled = True
            if len(raw) != length:
                self.close_connection = True
                raise core.ManagerError("请求内容未完整接收。")
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise core.ManagerError("请求 JSON 无效。") from exc
        except core.ManagerError:
            raise
        except (OSError, TimeoutError):
            self._request_body_handled = True
            self.close_connection = True
            raise
        if not isinstance(payload, dict):
            raise core.ManagerError("请求必须是 JSON 对象。")
        return payload

    def _serve_static(self, request_path: str) -> None:
        if not self.server.static_dir.exists():
            self._error("GUI 尚未构建，请先运行 npm run build。", 503)
            return
        relative = "index.html" if request_path in {"", "/"} else unquote(request_path.lstrip("/"))
        candidate = (self.server.static_dir / relative).resolve()
        try:
            candidate.relative_to(self.server.static_dir)
        except ValueError:
            self._error("无效的静态文件路径。", 403)
            return
        if not candidate.is_file():
            if "." not in Path(relative).name:
                candidate = self.server.static_dir / "index.html"
            else:
                self._error("文件不存在。", 404)
                return
        data = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        cache = "public, max-age=31536000, immutable" if candidate.name != "index.html" else "no-store"
        self._headers(content_type, len(data), 200, cache)
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.server.last_request_at = time.time()
        if not self._validate_local_request():
            return
        parsed_request = urlparse(self.path)
        path = parsed_request.path
        query = urllib.parse.parse_qs(parsed_request.query, keep_blank_values=True)
        if path == "/api/health":
            from frozen_dll_isolation import runtime_status as dll_runtime_status
            self._json(
                {
                    "ok": True,
                    "app": core.APP_NAME,
                    "appId": RUNTIME_APP_ID,
                    "runtimePid": os.getpid(),
                    "runtimeNonce": self.server.runtime_nonce,
                    "activationTokenHash": _activation_token_hash(self.server.activation_token),
                    "controlTokenHash": _activation_token_hash(self.server.control_token),
                    "uiReady": self.server.ui_ready.is_set(),
                    "independentLifecycle": _manager_lifecycle_is_independent(),
                    "trayStatus": self.server.tray_status(),
                    "windowAppearance": getattr(self.server, "window_appearance_status", None),
                    "dllIsolation": dll_runtime_status(),
                }
            )
            return
        if path.startswith("/api/"):
            if not self._authorized():
                self._error("未授权的本地请求。", 403)
                return
            try:
                if path == "/api/state":
                    self._json({"ok": True, **self.server.runtime.state(), "trayStatus": self.server.tray_status()})
                    return
                if path == "/api/app-lifecycle":
                    self._json(
                        {
                            "ok": True,
                            "pid": os.getpid(),
                            "uiReady": self.server.ui_ready.is_set(),
                            "independentLifecycle": _manager_lifecycle_is_independent(),
                            "configurationSession": self.server.runtime.configuration_snapshot(),
                            "trayStatus": self.server.tray_status(),
                            "shutdown": _read_shutdown_status(),
                        }
                    )
                    return
                if path == "/api/accounts/snapshot":
                    self._json({"ok": True, **self.server.runtime.account_snapshot()})
                    return
                if path == "/api/connections":
                    connections = core.public_connections_state()
                    sampler = getattr(self.server.runtime, "quota_estimates", None)
                    if sampler:
                        sampler.decorate(connections.get("settings", {}).get("accounts", []))
                    self._json({"ok": True, **connections})
                    return
                if path == "/api/recovery":
                    self._json({"ok": True, **recovery.list_points()})
                    return
                if path == "/api/history/recoveries":
                    self._json({"ok": True, **history_sync.list_recoveries()})
                    return
                if path == "/api/sessions/visibility":
                    import session_visibility_service
                    mode = str((query.get("mode") or ["quick"])[0])
                    self._json({"ok": True, "inspection": session_visibility_service.inspect(mode=mode), **session_visibility_service.list_backups()})
                    return
                if path == "/api/switch-operation":
                    operation_id = str((query.get("operationId") or [""])[0])
                    self._json(
                        {
                            "ok": True,
                            "operation": self.server.runtime.switch_operation_status(operation_id),
                        }
                    )
                    return
                if path == "/api/codex-runtime":
                    self._json({"ok": True, "runtime": core.codex_runtime_status(force=True)})
                    return
                if path == "/api/codex-config":
                    self._json({"ok": True, "document": core.codex_config_document()})
                    return
                if path == "/api/codex-config/recovery":
                    import codex_config_recovery
                    self._json({"ok": True, "inspection": codex_config_recovery.inspect_recovery()})
                    return
                if path == "/api/preview":
                    self._json({"ok": True, **core.preview_apply()})
                    return
                if path == "/api/export":
                    self._json({"ok": True, "bundle": core.export_bundle()})
                    return
                if path == "/api/history":
                    self._json({"ok": True, **core.history_state(include_threads=True)})
                    return
                if path == "/api/web2api/status":
                    self._json({"ok": True, "status": self.server.runtime.web2api.status()})
                    return
                if path == "/api/oauth/status":
                    self._json({"ok": True, "status": self.server.runtime.oauth.state()})
                    return
                if path == "/api/relay-login/status":
                    self._json(
                        {
                            "ok": True,
                            "status": self.server.runtime.relay_portal.public_state(),
                        }
                    )
                    return
                if path == "/api/toolbox/state":
                    self._json({"ok": True, **self.server.runtime.toolbox.state()})
                    return
                if path == "/api/skills":
                    self._json(
                        {
                            "ok": True,
                            **maintenance.list_skills(
                                cwd=(query.get("cwd") or [None])[0],
                                force=str((query.get("force") or [""])[0]).casefold() in {"1", "true", "yes"},
                            ),
                        }
                    )
                    return
                if path == "/api/skills/catalog":
                    self._json(
                        {
                            "ok": True,
                            **maintenance.public_skill_catalog(
                                force=str((query.get("force") or [""])[0]).casefold() in {"1", "true", "yes"}
                            ),
                        }
                    )
                    return
                if path == "/api/updates":
                    self._json(
                        {
                            "ok": True,
                            "status": maintenance.update_center_status(
                                force=str((query.get("force") or [""])[0]).casefold() in {"1", "true", "yes"}
                            ),
                        }
                    )
                    return
                if path == "/api/emergency/checks":
                    self._json(
                        {
                            "ok": True,
                            **maintenance.run_emergency_checks(
                                force=str((query.get("force") or [""])[0]).casefold() in {"1", "true", "yes"}
                            ),
                        }
                    )
                    return
                if path == "/api/claude/profiles":
                    self._json({"ok": True, **claude.list_profiles()})
                    return
                if path == "/api/claude/import-preview":
                    self._json({"ok": True, "preview": claude.preview_claude_code_import()})
                    return
                if path == "/api/usage":
                    usage = self.server.runtime.web2api.usage_snapshot()
                    backfill = ((usage.get("codexSessions") or {}).get("coverage") or {}).get("backfill") or {}
                    if backfill.get("pendingFiles") and not getattr(self.server.runtime, "_closed", False) and not getattr(self.server.runtime, "_restart_prepared", False):
                        import web2api_service
                        web2api_service.request_codex_session_usage_backfill()
                    self._json({"ok": True, "usage": usage})
                    return
                if path == "/api/app-update":
                    self._json({"ok": True, "status": self.server.runtime.get_app_updates().status()})
                    return
                if path == "/api/radar/reset-status":
                    accounts = core.load_settings().get("accounts", [])
                    refreshed = self.server.runtime.radar.get_reset_radar(accounts, refresh=False)
                    self._json({"ok": True, "reset": _radar_api_section(refreshed)})
                    return
                if path == "/api/web2api/key":
                    key = core.load_service_secret("web2api", required=True)
                    if key == core.load_service_secret("gateway_internal"):
                        raise core.ManagerError("当前公开 Key 需要先轮换，不能复制内部路由凭据。")
                    self._json({"ok": True, "apiKey": key})
                    return
                if path == "/api/radar":
                    accounts = core.load_settings().get("accounts", [])
                    snapshot = self.server.runtime.radar.get_snapshot(
                        accounts if isinstance(accounts, list) else [],
                        startup=True,
                    )
                    self._json(
                        {
                            "ok": True,
                            "schemaVersion": snapshot.get("schemaVersion", 1),
                            "intelligence": _radar_api_section(snapshot.get("intelligence")),
                            "quota": _radar_api_section(snapshot.get("quota")),
                            "reset": _radar_api_section(snapshot.get("reset")),
                            "sources": snapshot.get("sources", []),
                        }
                    )
                    return
                account_export = re.fullmatch(r"/api/accounts/([^/]+)/export", path)
                if account_export:
                    self._json({"ok": True, "export": core.export_codex_account(unquote(account_export.group(1)))})
                    return
                provider_export = re.fullmatch(r"/api/providers/([^/]+)/export", path)
                if provider_export:
                    self._json({"ok": True, "export": core.export_api_provider(unquote(provider_export.group(1)))})
                    return
                relay_export = re.fullmatch(r"/api/relay-accounts/([^/]+)/export", path)
                if relay_export:
                    self._json(
                        {
                            "ok": True,
                            "export": core.export_relay_account(
                                unquote(relay_export.group(1))
                            ),
                        }
                    )
                    return
                self._error("API 路径不存在。", 404)
            except (core.ManagerError, radar.RadarError, toolbox.ToolboxError, app_updates.UpdateError) as exc:
                self._error(str(exc), 400)
            except Exception as exc:
                self._internal_error(exc)
            return
        self._serve_static(path)

    def do_POST(self) -> None:
        self.server.last_request_at = time.time()
        if not self._validate_local_request(write=True):
            return
        path = urlparse(self.path).path
        if path == "/api/window/show":
            supplied = self.headers.get("X-Agent-Manager-Activation", "")
            if not secrets.compare_digest(supplied, self.server.activation_token):
                self._error("窗口激活请求无效。", 403)
                return
            queued = bool(
                self.server.native_window
                and self.server.native_window_object is None
                and not self.server.shutdown_started.is_set()
            )
            if queued:
                self.server.window_activation_requested.set()
            shown = False if queued else self.server.show_native_window()
            opened = False
            if not shown and not queued and not self.server.shutdown_started.is_set():
                # Keep the full API token inside the live instance. A second
                # launch may ask us to open the UI, but never receives that
                # token through app-runtime.json or the activation response.
                opened = open_browser_window(self.server.ui_url())
            self._json({"ok": True, "shown": shown, "opened": opened, "queued": queued})
            return
        if path == CONTROL_QUICK_RESTART_PATH:
            supplied = self.headers.get(CONTROL_HEADER_NAME, "")
            if not secrets.compare_digest(supplied, self.server.control_token):
                self._error("本机控制请求无效。", 403)
                return
            try:
                self._read_json(optional=True)
                if not request_application_restart(self.server):
                    raise core.ManagerError("Agent Manager 已在退出或重启中。")
            except core.ManagerError as exc:
                self._error(str(exc), 400)
                return
            self._json(
                {
                    "ok": True,
                    "message": "管理器正在快速重启；Codex 将保持运行。",
                    "sourcePid": os.getpid(),
                    "requestedAt": core.now_iso(),
                }
            )
            return
        if path == "/api/session/bootstrap":
            bootstrap = self.headers.get("X-Agent-Manager-Bootstrap", "")
            if not self.server.consume_ui_bootstrap(bootstrap):
                self._error("管理界面启动凭据无效或已过期，请重新打开 Agent Manager。", 403)
                return
            self._json({"ok": True, "token": self.server.api_token})
            return
        if not self._authorized():
            self._error("未授权的本地请求。", 403)
            return
        if not self.server.begin_mutating_request():
            self._discard_small_request_body()
            self._error("Agent Manager 正在退出，已拒绝新的修改请求。", 503)
            return
        self._mutation_registered = True
        try:
            if path.startswith("/api/app-update/"):
                service = self.server.runtime.get_app_updates()
                payload = self._read_json(optional=True)
                if path == "/api/app-update/source":
                    if "source" not in payload:
                        raise core.ManagerError("请提供更新源，或使用 null 恢复内置更新源。")
                    status = service.configure(payload["source"])
                elif path == "/api/app-update/check":
                    status = service.check()
                elif path == "/api/app-update/download":
                    auto_install = bool(payload.get("installAfterDownload") and service.install_supported)
                    status = service.start_download(payload.get("releaseToken"),
                        on_ready=(lambda: prepare_application_update(self.server)) if auto_install else None)
                elif path == "/api/app-update/install":
                    result = prepare_application_update(self.server)
                    self._json({"ok": True, "result": result, "message": "更新已校验；正在关闭 Codex、恢复配置并重启管理器。"})
                    return
                elif path == "/api/app-update/cancel":
                    status = service.cancel_download()
                elif path == "/api/app-update/open-folder":
                    downloaded = service.verified_download_path()
                    os.startfile(str(downloaded.parent))
                    status = service.status()
                else:
                    raise core.ManagerError("更新操作不存在。")
                self._json({"ok": True, "status": status})
                return
            if path == "/api/window/appearance":
                from native_window_theme import sync_server_appearance
                try:
                    result = sync_server_appearance(self.server, self._read_json())
                except ValueError as exc:
                    raise core.ManagerError(str(exc)) from exc
                self._json({"ok": True, **result})
                return
            if path == "/api/apply":
                payload = self._read_json(optional=True)
                result = core.apply_configuration(bool(payload.get("syncSecrets")))
                if result.get("gatewayRequired") and not self.server.runtime.web2api.status()["running"]:
                    self.server.runtime.web2api.start()
                diagnostics = maintenance.run_emergency_checks(force=False)
                self._json(
                    {
                        "ok": True,
                        "result": result,
                        "status": maintenance.merge_configuration_diagnostics(
                            core.configuration_status(), diagnostics
                        ),
                    }
                )
                return
            if path == "/api/apply-and-launch":
                payload = self._read_json(optional=True)
                result = core.apply_configuration(bool(payload.get("syncSecrets")))
                if result.get("gatewayRequired") and not self.server.runtime.web2api.status()["running"]:
                    self.server.runtime.web2api.start()
                env_overrides = {}
                if payload.get("syncSecrets"):
                    for provider in core.load_settings()["providers"]:
                        if provider.get("kind") == "custom" and core.provider_key_configured(provider["id"]):
                            env_overrides[provider["envKey"]] = core.load_provider_key(provider["id"]) or ""
                launch_plan = core.resolve_codex_launch_plan()
                closed = None
                try:
                    closed = _close_codex_processes_safely()
                    launch = core.launch_codex_app(launch_plan=launch_plan, env_overrides=env_overrides)
                except Exception as exc:
                    recovery_error = None
                    if closed is not None and not core.running_codex_processes():
                        try:
                            core.launch_codex_app(launch_plan=launch_plan)
                        except Exception as recovery_exc:
                            recovery_error = str(recovery_exc)[:300]
                    detail = f"；自动恢复也失败：{recovery_error}" if recovery_error else ""
                    raise core.ManagerError(f"应用配置后重新打开 Codex 失败：{exc}{detail}") from exc
                self._json({"ok": True, "result": result, "closed": closed, "launch": launch})
                return
            if path == "/api/codex/restart":
                self._read_json(optional=True)
                # Resolve a valid, dynamic Desktop/CLI launch target before
                # closing anything. This preserves a working Codex instance if
                # the installed Store package moved or the runtime is missing.
                launch_plan = core.resolve_codex_launch_plan()
                closed = None
                try:
                    closed = _close_codex_processes_safely()
                    launch = core.launch_codex_app(launch_plan=launch_plan)
                except Exception as exc:
                    recovery_error = None
                    if closed is not None and not core.running_codex_processes():
                        try:
                            core.launch_codex_app(launch_plan=launch_plan)
                        except Exception as recovery_exc:
                            recovery_error = str(recovery_exc)[:300]
                    detail = f"；恢复启动也失败：{recovery_error}" if recovery_error else ""
                    raise core.ManagerError(f"重启 Codex 失败：{exc}{detail}") from exc
                self._json({"ok": True, "closed": closed, "launch": launch})
                return
            if path == "/api/validate":
                payload = self._read_json(optional=True)
                self._json(
                    {
                        "ok": True,
                        "validation": self.server.runtime.validate(
                            run_doctor=bool(payload.get("runDoctor", True))
                        ),
                    }
                )
                return
            if path == "/api/providers":
                payload = self._read_json()
                key = str(payload.get("key", "")).strip()
                record = core.save_provider(
                    payload,
                    payload.get("originalId"),
                    api_key=key or None,
                )
                self._json({"ok": True, "provider": record})
                return
            if path == "/api/accounts/capture":
                self._json({"ok": True, "account": core.save_codex_account(self._read_json())})
                return
            if path == "/api/accounts/import":
                self._json({"ok": True, "account": core.import_codex_account(self._read_json())})
                return
            if path == "/api/accounts/import-preview":
                payload = self._read_json()
                payload.setdefault("validateRemote", True)
                self._json({"ok": True, "preview": core.preview_codex_accounts_batch(payload)})
                return
            if path == "/api/accounts/import-batch":
                payload = self._read_json()
                payload["deferRefresh"] = True
                result = core.import_codex_accounts_batch(payload)
                refresh_status = self.server.runtime.schedule_account_refresh(
                    result.get("refreshAccountIds") if isinstance(result.get("refreshAccountIds"), list) else []
                )
                self._json({"ok": True, "result": result, "refreshStatus": refresh_status})
                return
            if path == "/api/model-sources/delete-batch":
                payload = self._read_json()
                result, point = recovery_coordinator.before_account_delete(self.server.runtime, "批量删除账号前", lambda: core.remove_model_sources_batch(
                    payload.get("accountIds") if isinstance(payload.get("accountIds"), list) else [],
                    payload.get("providerIds") if isinstance(payload.get("providerIds"), list) else [],
                ))
                result["backup"] = point
                if result.get("deleted"):
                    try:
                        applied = core.apply_configuration(False)
                        if applied.get("gatewayRequired") and not self.server.runtime.web2api.status()["running"]:
                            self.server.runtime.web2api.start()
                        result["configurationApplied"] = True
                        result["applied"] = applied
                    except Exception as exc:
                        result["configurationApplied"] = False
                        result["configurationWarning"] = f"账号已删除，但子代理配置刷新失败：{str(exc)[:300]}"
                self._json({"ok": True, "result": result})
                return
            if path == "/api/account-groups":
                self._json({"ok": True, "group": core.save_account_group(self._read_json())})
                return
            assign_group = re.fullmatch(r"/api/account-groups/([^/]+)/assign", path)
            if assign_group:
                payload = self._read_json()
                account_ids = payload.get("accountIds", [])
                provider_ids = payload.get("providerIds", [])
                if not isinstance(account_ids, list) or not isinstance(provider_ids, list):
                    raise core.ManagerError("accountIds 和 providerIds 必须是数组。")
                result = core.assign_sources_to_group(
                    unquote(assign_group.group(1)), account_ids, provider_ids
                )
                self._json({"ok": True, "result": result})
                return
            if path == "/api/accounts/proxy-batch":
                payload = self._read_json()
                result = core.set_accounts_proxy_enabled_batch(
                    payload.get("accountIds") if isinstance(payload.get("accountIds"), list) else [],
                    bool(payload.get("enabled")),
                )
                if not payload.get("enabled"):
                    self.server.runtime.web2api.clear_account_runtime_state(payload.get("accountIds", []))
                self._json({"ok": True, "result": result})
                return
            if path == "/api/providers/proxy-batch":
                payload = self._read_json()
                result = core.set_providers_proxy_enabled_batch(
                    payload.get("providerIds") if isinstance(payload.get("providerIds"), list) else [],
                    bool(payload.get("enabled")),
                )
                self._json({"ok": True, "result": result})
                return
            account_proxy = re.fullmatch(r"/api/accounts/([^/]+)/proxy", path)
            if account_proxy:
                payload = self._read_json()
                account = core.set_account_proxy_enabled(
                    unquote(account_proxy.group(1)), bool(payload.get("enabled"))
                )
                if not payload.get("enabled"):
                    self.server.runtime.web2api.clear_account_runtime_state([unquote(account_proxy.group(1))])
                self._json({"ok": True, "account": account})
                return
            provider_proxy = re.fullmatch(r"/api/providers/([^/]+)/proxy", path)
            if provider_proxy:
                payload = self._read_json()
                provider = core.set_provider_proxy_enabled(
                    unquote(provider_proxy.group(1)), bool(payload.get("enabled"))
                )
                self._json({"ok": True, "provider": provider})
                return
            if path == "/api/web2api/config":
                payload = self._read_json()
                was_running = self.server.runtime.web2api.status()["running"]
                previous = json.loads(json.dumps(core.load_settings().get("web2api", core._default_web2api_settings())))
                config = core.save_web2api_settings(payload)
                restart_required = was_running and int(previous.get("port", 17860)) != int(config.get("port", 17860))
                if restart_required:
                    self.server.runtime.web2api.stop(disable=False)
                    try:
                        status = self.server.runtime.web2api.start()
                    except Exception:
                        settings = core.load_settings()
                        settings["web2api"] = previous
                        core.save_settings(settings)
                        try:
                            self.server.runtime.web2api.start()
                        except Exception:
                            pass
                        raise
                else:
                    status = self.server.runtime.web2api.status()
                removed_accounts = set(previous.get("accountIds", [])) - set(config.get("accountIds", []))
                if removed_accounts:
                    self.server.runtime.web2api.clear_account_runtime_state(removed_accounts)
                    status = self.server.runtime.web2api.status()
                self._json({"ok": True, "config": config, "status": status})
                return
            if path == "/api/web2api/clear-cooldowns":
                payload = self._read_json()
                selected = payload.get("accountIds")
                allowed = set(core.load_settings().get("web2api", {}).get("accountIds", []))
                if not isinstance(selected, list) or not selected or len(selected) > 4096 or any(not isinstance(value, str) or value not in allowed for value in selected):
                    raise core.ManagerError("请选择当前 API 号池中的账号。")
                result = self.server.runtime.web2api.clear_account_runtime_state(selected)
                self._json({"ok": True, "result": result, "status": self.server.runtime.web2api.status()})
                return
            if path == "/api/web2api/start":
                self._json({"ok": True, **activate_web2api_for_codex(self.server.runtime)})
                return
            if path == "/api/web2api/stop":
                self._json({"ok": True, **deactivate_web2api_for_codex(self.server.runtime)})
                return
            if path == "/api/web2api/service-start":
                self._read_json(optional=True)
                if not core.service_secret_configured("web2api"):
                    core.rotate_web2api_key()
                self._json({"ok": True, "status": self.server.runtime.web2api.start()})
                return
            if path == "/api/web2api/service-stop":
                self._read_json(optional=True)
                if core.load_settings().get("web2api", {}).get("activeForCodex"):
                    raise core.ManagerError("Codex 正在使用本地反代，请先选择“停止并恢复直连”。")
                self._json({"ok": True, "status": self.server.runtime.web2api.stop()})
                return
            if path == "/api/web2api/rotate-key":
                self._json({"ok": True, **rotate_web2api_key_for_runtime(self.server.runtime)})
                return
            if path == "/api/oauth/start":
                self._json({"ok": True, "status": self.server.runtime.oauth.start(self._read_json())})
                return
            reauth_account = re.fullmatch(r"/api/accounts/([^/]+)/reauth", path)
            if reauth_account:
                self._read_json(optional=True)
                self._json({"ok": True, "status": self.server.runtime.oauth.start({"reauthAccountId":unquote(reauth_account.group(1))})})
                return
            if path in {"/api/oauth/open", "/api/oauth/callback", "/api/oauth/cancel"}:
                oauth_payload = self._read_json(optional=True)
                expected_login = oauth_payload.get("loginId")
                if expected_login and expected_login != self.server.runtime.oauth.state().get("loginId"):
                    raise core.ManagerError("认证会话已改变，未操作其他登录会话。")
            if path == "/api/oauth/open":
                self._json({"ok": True, "status": self.server.runtime.oauth.open_browser(expected_login_id=expected_login) if expected_login else self.server.runtime.oauth.open_browser()})
                return
            if path == "/api/oauth/callback":
                payload = oauth_payload
                self._json(
                    {
                        "ok": True,
                        "status": self.server.runtime.oauth.submit_callback(
                            str(payload.get("callbackUrl") or ""),
                            **({"expected_login_id":expected_login} if expected_login else {}),
                        ),
                    }
                )
                return
            if path == "/api/oauth/cancel":
                self._json({"ok": True, "status": self.server.runtime.oauth.cancel(expected_login_id=expected_login) if expected_login else self.server.runtime.oauth.cancel()})
                return
            if path == "/api/relay-login/start":
                payload = self._read_json()
                account_id = payload.get("accountId")
                if account_id is not None and not isinstance(account_id, str):
                    raise core.ManagerError("accountId 必须是字符串。")
                status = self.server.runtime.relay_portal.start(payload.get("url"), account_id=account_id) if account_id else self.server.runtime.relay_portal.start(payload.get("url"))
                self._json(
                    {
                        "ok": True,
                        "status": status,
                    }
                )
                return
            if path == "/api/relay-login/check":
                payload = self._read_json()
                force = payload.get("force", False)
                if not isinstance(force, bool):
                    raise core.ManagerError("force 必须是布尔值。")
                self._json({"ok": True, "status": self.server.runtime.relay_portal.check_auto_auth(payload.get("sessionId"), force=force)})
                return
            if path == "/api/relay-login/scan":
                payload = self._read_json()
                self._json(
                    {
                        "ok": True,
                        "status": self.server.runtime.relay_portal.scan(payload.get("sessionId")),
                    }
                )
                return
            if path == "/api/relay-login/import":
                payload = self._read_json()
                self._json(
                    {
                        "ok": True,
                        "result": self.server.runtime.relay_portal.import_keys(
                            payload.get("sessionId"), payload
                        ),
                    }
                )
                return
            if path == "/api/relay-login/create":
                payload = self._read_json()
                self._json(
                    {
                        "ok": True,
                        "result": self.server.runtime.relay_portal.create_key(
                            payload.get("sessionId"), payload
                        ),
                    }
                )
                return
            if path == "/api/relay-login/cancel":
                self._read_json(optional=True)
                self._json(
                    {
                        "ok": True,
                        "status": self.server.runtime.relay_portal.close(),
                    }
                )
                return
            relay_selection = re.fullmatch(r"/api/relay-accounts/([^/]+)/selection", path)
            if relay_selection:
                result = core.update_relay_account_selection(
                    unquote(relay_selection.group(1)),
                    self._read_json(),
                )
                self._json({"ok": True, "result": result})
                return
            relay_group = re.fullmatch(r"/api/relay-accounts/([^/]+)/group", path)
            if relay_group:
                payload = self._read_json()
                result = core.move_relay_account_group(
                    unquote(relay_group.group(1)),
                    payload.get("groupId"),
                )
                self._json({"ok": True, "result": result})
                return
            relay_refresh = re.fullmatch(r"/api/relay-accounts/([^/]+)/refresh", path)
            if relay_refresh:
                payload = self._read_json(optional=True)
                full = payload.get("full", True)
                if not isinstance(full, bool):
                    raise core.ManagerError("完整同步选项必须为布尔值。")
                result = self.server.runtime.relay_portal.refresh_account(
                    unquote(relay_refresh.group(1)), full=full
                )
                self._json({"ok": True, "result": result})
                return
            relay_metadata = re.fullmatch(r"/api/relay-accounts/([^/]+)/metadata", path)
            if relay_metadata:
                result = core.update_relay_account_metadata(
                    unquote(relay_metadata.group(1)),
                    self._read_json(),
                )
                self._json({"ok": True, "result": result})
                return
            relay_create_key = re.fullmatch(r"/api/relay-accounts/([^/]+)/keys", path)
            if relay_create_key:
                result = self.server.runtime.relay_portal.create_saved_key(
                    unquote(relay_create_key.group(1)),
                    self._read_json(),
                )
                self._json({"ok": True, "result": result})
                return
            relay_key_group = re.fullmatch(
                r"/api/relay-accounts/([^/]+)/keys/([^/]+)/group",
                path,
            )
            if relay_key_group:
                payload = self._read_json()
                result = self.server.runtime.relay_portal.update_saved_key_group(
                    unquote(relay_key_group.group(1)),
                    unquote(relay_key_group.group(2)),
                    payload.get("groupId"),
                )
                self._json({"ok": True, "result": result})
                return
            if path == "/api/toolbox/totp/generate":
                payload = self._read_json()
                source = str(payload.get("input") or "")
                label = str(payload.get("label") or "")
                if payload.get("save"):
                    generated = toolbox.save_totp_item(source, label=label)
                else:
                    generated = toolbox.generate_totp(source)
                    if label.strip():
                        generated["label"] = label.strip()
                self._json({"ok": True, "result": _totp_api_item(generated)})
                return
            if path == "/api/toolbox/mail/preview":
                payload = self._read_json()
                preview = toolbox.preview_mail_import_items(str(payload.get("text") or ""))
                preview["items"] = [
                    {
                        **item,
                        "authMode": item.get("auth_method"),
                        "imapHost": item.get("imap_host"),
                        "imapPort": item.get("imap_port"),
                    }
                    for item in preview.get("items", [])
                ]
                self._json({"ok": True, "preview": preview})
                return
            if path == "/api/toolbox/mail/import":
                payload = self._read_json()
                selected_indices = payload.get("selectedIndices")
                if not isinstance(selected_indices, list):
                    raise toolbox.ValidationError("selectedIndices 必须是数组。")
                imported = toolbox.save_mail_import_selection(
                    str(payload.get("text") or ""),
                    selected_indices,
                )
                self._json(
                    {
                        "ok": True,
                        "imported": len(imported),
                        "mailAccounts": [_mail_account_api_item(item) for item in imported],
                    }
                )
                return
            if path == "/api/toolbox/mail/temporary/messages":
                payload = self._read_json()
                try:
                    limit = int(payload.get("limit") or 10)
                except (TypeError, ValueError) as exc:
                    raise toolbox.ValidationError("邮件数量必须是整数。") from exc
                messages = self.server.runtime.toolbox.fetch_temporary_mail(
                    str(payload.get("text") or ""),
                    limit=limit,
                    unread_only=bool(payload.get("unreadOnly")),
                )
                self._json({"ok": True, "messages": messages})
                return
            mailbox_messages = re.fullmatch(r"/api/toolbox/mail/([^/]+)/messages", path)
            if mailbox_messages:
                payload = self._read_json(optional=True)
                try:
                    limit = int(payload.get("limit") or 20)
                except (TypeError, ValueError) as exc:
                    raise toolbox.ValidationError("邮件数量必须是整数。") from exc
                messages = self.server.runtime.toolbox.fetch_mail(
                    unquote(mailbox_messages.group(1)),
                    limit=limit,
                    unread_only=bool(payload.get("unreadOnly")),
                )
                self._json({"ok": True, "messages": messages})
                return
            if path == "/api/accounts/refresh":
                payload = self._read_json(optional=True)
                self._json(
                    {
                        "ok": True,
                        "result": core.refresh_all_codex_accounts(stale_only=bool(payload.get("staleOnly"))),
                    }
                )
                return
            if path == "/api/accounts/delete-invalid":
                payload = self._read_json(optional=True)
                result, point = recovery_coordinator.before_account_delete(self.server.runtime, "清理失效账号前", lambda: core.delete_invalid_accounts(str(payload.get("groupId") or "all")))
                result["backup"] = point
                if result.get("deleted"):
                    try:
                        applied = core.apply_configuration(False)
                        if applied.get("gatewayRequired") and not self.server.runtime.web2api.status()["running"]:
                            self.server.runtime.web2api.start()
                        result["configurationApplied"] = True
                        result["applied"] = applied
                    except Exception as exc:
                        result["configurationApplied"] = False
                        result["configurationWarning"] = (
                            f"失效账号已删除，但子代理配置刷新失败：{str(exc)[:300]}"
                        )
                self._json({"ok": True, "result": result})
                return
            account_export_download = re.fullmatch(r"/api/accounts/([^/]+)/export-download", path)
            if account_export_download:
                self._read_json(optional=True)
                result = core.export_codex_account_to_downloads(unquote(account_export_download.group(1)))
                self._json({"ok": True, "result": result})
                return
            provider_export_download = re.fullmatch(r"/api/providers/([^/]+)/export-download", path)
            if provider_export_download:
                self._read_json(optional=True)
                result = core.export_api_provider_to_downloads(unquote(provider_export_download.group(1)))
                self._json({"ok": True, "result": result})
                return
            relay_export_download = re.fullmatch(
                r"/api/relay-accounts/([^/]+)/export-download",
                path,
            )
            if relay_export_download:
                self._read_json(optional=True)
                result = core.export_relay_account_to_downloads(
                    unquote(relay_export_download.group(1))
                )
                self._json({"ok": True, "result": result})
                return
            refresh_account = re.fullmatch(r"/api/accounts/([^/]+)/refresh", path)
            if refresh_account:
                # A user-initiated refresh must bypass the metadata TTL.  New
                # Codex models can roll out between background refreshes and
                # otherwise remain hidden even though the account already has
                # access to them.
                account = core.refresh_codex_account(
                    unquote(refresh_account.group(1)),
                    force_metadata=True,
                )
                sampler = getattr(self.server.runtime, "quota_estimates", None)
                if sampler:
                    sampler.decorate([account])
                self._json({"ok": True, "account": account})
                return
            reset_details = re.fullmatch(r"/api/accounts/([^/]+)/reset-credit/details", path)
            if reset_details:
                payload = self._read_json(optional=True)
                account = core.refresh_account_reset_credit_details(
                    unquote(reset_details.group(1)),
                    force=bool(payload.get("force")),
                )
                self._json({"ok": True, "account": account})
                return
            consume_reset = re.fullmatch(r"/api/accounts/([^/]+)/reset-credit/consume", path)
            if consume_reset:
                result = core.consume_account_reset_credit(unquote(consume_reset.group(1)))
                self._json({"ok": True, "result": result})
                return
            switch_account = re.fullmatch(r"/api/accounts/([^/]+)/switch", path)
            if switch_account:
                payload = self._read_json(optional=True)
                account_id = unquote(switch_account.group(1))
                settings = core.load_settings()
                account = next(
                    (item for item in settings.get("accounts", []) if str(item.get("id")) == account_id),
                    None,
                )
                if not account:
                    raise core.ManagerError("账号不存在。")
                operation_id = str(payload.get("operationId") or secrets.token_urlsafe(12))
                self.server.runtime.begin_switch_operation(
                    operation_id,
                    target_id=account_id,
                    target_name=str(account.get("label") or account.get("email") or account_id),
                    target_kind="account",
                )
                try:
                    if account.get("sourceType") == "web_session":
                        self.server.runtime.update_switch_operation(
                            operation_id,
                            {
                                "phase": "configuring",
                                "progress": 38,
                                "message": "正在准备单账号本地转换与 Codex 配置",
                            },
                        )
                        result = activate_web2api_for_codex(self.server.runtime, account_id)
                    else:
                        result = core.switch_codex_account_and_launch(
                            account_id,
                            progress_callback=lambda update: self.server.runtime.update_switch_operation(
                                operation_id,
                                update,
                            ),
                            close_processes_callback=_close_codex_processes_safely,
                            ensure_gateway=lambda: _ensure_runtime_gateway(self.server.runtime),
                            force_reapply=payload.get("forceReapply") is True,
                        )
                except Exception as exc:
                    self.server.runtime.finish_switch_operation(operation_id, error=exc)
                    raise
                self.server.runtime.finish_switch_operation(operation_id, result=result)
                self._json({"ok": True, "result": result})
                return
            account_metadata = re.fullmatch(r"/api/accounts/([^/]+)/metadata", path)
            if account_metadata:
                account = core.update_codex_account_metadata(
                    unquote(account_metadata.group(1)),
                    self._read_json(),
                )
                self._json({"ok": True, "account": account})
                return
            switch_provider = re.fullmatch(r"/api/providers/([^/]+)/switch", path)
            if switch_provider:
                payload = self._read_json(optional=True)
                provider_id = unquote(switch_provider.group(1))
                provider = core.provider_by_id(provider_id)
                operation_id = str(payload.get("operationId") or secrets.token_urlsafe(12))
                self.server.runtime.begin_switch_operation(
                    operation_id,
                    target_id=provider_id,
                    target_name=str(provider.get("name") or provider_id),
                    target_kind="provider",
                )
                try:
                    result = core.switch_api_provider_and_launch(
                        provider_id,
                        progress_callback=lambda update: self.server.runtime.update_switch_operation(
                            operation_id,
                            update,
                        ),
                        close_processes_callback=_close_codex_processes_safely,
                        ensure_gateway=lambda: _ensure_runtime_gateway(self.server.runtime),
                        force_reapply=payload.get("forceReapply") is True,
                    )
                except Exception as exc:
                    self.server.runtime.finish_switch_operation(operation_id, error=exc)
                    raise
                self.server.runtime.finish_switch_operation(operation_id, result=result)
                self._json({"ok": True, "result": result})
                return
            if path == "/api/api-accounts/probe":
                self._json({"ok": True, "probe": core.probe_api_account(self._read_json())})
                return
            if path == "/api/api-accounts/import":
                self._json({"ok": True, "result": core.import_api_account(self._read_json())})
                return
            if path == "/api/model-source/select":
                payload = self._read_json()
                result = core.select_model_source(str(payload.get("sourceId") or ""))
                self._json({"ok": True, "result": result})
                return
            if path == "/api/dashboard/move":
                from dashboard_order import move_dashboard_card
                result = move_dashboard_card(core, self._read_json())
                self._json({"ok": True, "result": result})
                return
            if path == "/api/model-workspace":
                workspace = core.save_model_workspace(self._read_json())
                self._json({"ok": True, "workspace": workspace})
                return
            if path == "/api/orchestration/save":
                self._json({"ok": True, **save_orchestration_for_runtime(self.server.runtime, self._read_json())})
                return
            if path == "/api/runtime-tuning":
                tuning = core.save_runtime_tuning(self._read_json())
                self._json({"ok": True, "runtimeTuning": tuning})
                return
            if path == "/api/codex-config":
                import codex_config_recovery
                codex_config_recovery.claim_orphaned_overlay()
                result = core.save_codex_config_document(self._read_json())
                self._json({"ok": True, "result": result, **result})
                return
            if path in {"/api/codex-config/backups", "/api/codex-config/backups/delete"}:
                import codex_config_recovery
                payload = self._read_json()
                result = (codex_config_recovery.create_manual_backup(expected_fingerprint=str(payload.get("expectedFingerprint") or ""))
                          if path.endswith("/backups") else codex_config_recovery.delete_backup(backup_id=str(payload.get("backupId") or "")))
                self._json({"ok": True, "result": result, "inspection": codex_config_recovery.inspect_recovery()})
                return
            if path == "/api/codex-config/recovery":
                import codex_config_recovery
                payload = self._read_json()
                codex_config_recovery.claim_orphaned_overlay()
                result = codex_config_recovery.repair_config(
                    expected_fingerprint=str(payload.get("expectedFingerprint") or ""),
                    backup_id=str(payload.get("backupId") or "") or None,
                    reset=payload.get("reset") is True,
                )
                self._json({"ok": True, "result": result, "document": core.codex_config_document()})
                return
            if path == "/api/sessions/repair":
                import session_repair_service
                self._read_json(optional=True)
                result = session_repair_service.repair_sessions(close_codex=_close_codex_processes_safely)
                self._json({"ok": True, "result": result})
                return
            if path == "/api/sessions/visibility/repair":
                import session_visibility_service
                payload = self._read_json()
                result = session_visibility_service.repair(payload.get("expectedToken"), mode=payload.get("mode", "quick"))
                self._json({"ok": True, "result": result})
                return
            if path == "/api/sessions/visibility/restore":
                import session_visibility_service
                result = session_visibility_service.restore(self._read_json().get("backupId"))
                self._json({"ok": True, "result": result})
                return
            if path == "/api/subagent-routing":
                routing = core.save_subagent_routing(self._read_json())
                self._json({"ok": True, "routing": routing})
                return
            if path == "/api/orchestration/restore-defaults":
                restored = core.restore_orchestration_defaults()
                applied = core.apply_configuration(False)
                if applied.get("gatewayRequired") and not self.server.runtime.web2api.status()["running"]:
                    self.server.runtime.web2api.start()
                self._json({"ok": True, "restored": restored, "applied": applied})
                return
            if path == "/api/app-behavior":
                payload = self._read_json()
                behavior = core.save_app_behavior(payload)
                if "quotaRefreshMinutes" in payload:
                    self.server.runtime.request_account_auto_refresh_check()
                if "mailHealthCheckHours" in payload:
                    self.server.runtime.request_mail_health_recalculate()
                if "radarMonitoring" in payload:
                    self.server.runtime._start_radar_monitor()
                    if not behavior.get("radarMonitoring") and not behavior.get("closeToTray"):
                        self.server.stop_tray()
                if "closeToTray" in payload:
                    if behavior.get("closeToTray"):
                        self.server.ensure_tray()
                    elif not behavior.get("radarMonitoring"):
                        self.server.stop_tray()
                self._json({"ok": True, "behavior": behavior, "trayStatus": self.server.tray_status()})
                return
            if path == "/api/history/settings":
                self._json({"ok": True, "sync": core.save_history_sync_settings(self._read_json())})
                return
            history_recovery_action = re.fullmatch(r"/api/history/recoveries/([^/]+)/(preview|restore)", path)
            if history_recovery_action:
                payload = self._read_json(optional=True)
                recovery_id, action = history_recovery_action.groups()
                result = (
                    history_sync.preview_recovery(recovery_id)
                    if action == "preview"
                    else history_sync.restore_recovery(recovery_id, str(payload.get("expectedFingerprint") or ""))
                )
                self._json({"ok": True, "result": result})
                return
            if path == "/api/history/preview":
                self._json({"ok": True, "preview": core.preview_history_sync(self._read_json())})
                return
            if path == "/api/history/sync":
                self._json({"ok": True, "result": core.perform_history_sync(self._read_json())})
                return
            if path == "/api/history/reindex":
                self._json({"ok": True, "result": core.refresh_codex_history_index()})
                return
            if path == "/api/sessions/action":
                payload = self._read_json()
                result = core.manage_codex_threads(
                    str(payload.get("action") or ""),
                    payload.get("threadIds") if isinstance(payload.get("threadIds"), list) else [],
                )
                self._json({"ok": True, "result": result})
                return
            if path == "/api/sessions/rename":
                payload = self._read_json()
                result = core.rename_codex_thread(
                    str(payload.get("threadId") or ""),
                    str(payload.get("name") or ""),
                )
                self._json({"ok": True, "result": result})
                return
            provider_key = re.fullmatch(r"/api/providers/([^/]+)/key", path)
            if provider_key:
                payload = self._read_json()
                core.store_provider_key(unquote(provider_key.group(1)), str(payload.get("key", "")))
                self._json({"ok": True})
                return
            provider_portal = re.fullmatch(r"/api/providers/([^/]+)/portal", path)
            if provider_portal:
                self._read_json(optional=True)
                url = core.provider_portal_url(unquote(provider_portal.group(1)))
                if not open_external_browser(url):
                    raise core.ManagerError("系统浏览器未能打开中转站官网，请稍后重试。")
                self._json({"ok": True, "url": url})
                return
            provider_models = re.fullmatch(r"/api/providers/([^/]+)/models", path)
            if provider_models:
                discovery = core.refresh_provider_models(unquote(provider_models.group(1)))
                self._json(
                    {
                        "ok": True,
                        "models": discovery["models"],
                        "discovery": discovery,
                    }
                )
                return
            model_reasoning = re.fullmatch(r"/api/providers/([^/]+)/model-reasoning", path)
            if model_reasoning:
                import model_preferences
                payload = self._read_json()
                result = model_preferences.save_reasoning(
                    unquote(model_reasoning.group(1)), str(payload.get("modelId") or ""), payload,
                )
                self._json({"ok": True, "result": result})
                return
            provider_balance = re.fullmatch(r"/api/providers/([^/]+)/balance", path)
            if provider_balance:
                balance = core.fetch_provider_balance(unquote(provider_balance.group(1)))
                self._json({"ok": True, "balance": balance})
                return
            if path == "/api/main-profiles":
                self._json({"ok": True, "profile": core.save_main_profile(self._read_json())})
                return
            activate_main = re.fullmatch(r"/api/main-profiles/([^/]+)/activate", path)
            if activate_main:
                core.set_active_main(unquote(activate_main.group(1)))
                self._json({"ok": True})
                return
            if path == "/api/strategies":
                self._json({"ok": True, "strategy": core.save_strategy(self._read_json())})
                return
            activate_strategy = re.fullmatch(r"/api/strategies/([^/]+)/activate", path)
            if activate_strategy:
                core.set_active_strategy(unquote(activate_strategy.group(1)))
                self._json({"ok": True})
                return
            if path == "/api/routes":
                core.save_routes(self._read_json().get("routes", {}))
                self._json({"ok": True})
                return
            if path == "/api/agents":
                payload = self._read_json()
                saved = core.write_agent(payload, bool(payload.get("force")))
                self._json({"ok": True, "path": str(saved)})
                return
            if path == "/api/import":
                payload = self._read_json()
                result = core.import_bundle(payload.get("bundle"), str(payload.get("mode") or "merge"))
                self._json({"ok": True, "result": result})
                return
            if path == "/api/shutdown":
                self._json({"ok": True})
                request_application_shutdown(self.server)
                return
            if path == "/api/exit-only":
                self._json({"ok": True})
                request_application_exit_only(self.server)
                return
            if path == "/api/configuration-session/retry":
                self._read_json(optional=True)
                result = self.server.runtime.activate_configuration_session(retry=True)
                self._json({"ok": True, "configurationSession": result})
                return
            if path == "/api/quick-restart":
                if not request_application_restart(self.server):
                    raise core.ManagerError("Agent Manager 已在退出或重启中。")
                self._json(
                    {
                        "ok": True,
                        "message": "管理器正在快速重启；Codex 将保持运行。",
                        "sourcePid": os.getpid(),
                        "requestedAt": core.now_iso(),
                    }
                )
                return
            if path == "/api/codex-runtime/deploy":
                result = core.deploy_codex_runtime()
                self._json({"ok": True, **result})
                return
            if path == "/api/skills/toggle":
                payload = self._read_json()
                if not isinstance(payload.get("enabled"), bool):
                    raise core.ManagerError("enabled 必须是布尔值。")
                result = maintenance.set_skill_enabled(
                    str(payload.get("id") or ""),
                    payload["enabled"],
                    cwd=payload.get("cwd"),
                )
                self._json({"ok": True, **result})
                return
            if path == "/api/skills/install":
                payload = self._read_json()
                result = maintenance.install_public_plugin(
                    str(payload.get("id") or ""),
                    force_refresh=payload.get("force", False),
                )
                self._json({"ok": True, "result": result})
                return
            if path == "/api/updates/check":
                self._read_json(optional=True)
                status = maintenance.update_center_status(force=True)
                self.server.runtime.update_check_status.update(
                    {
                        "status": "ready",
                        "lastCheckedAt": status.get("checkedAt"),
                        "nextCheckAt": None,
                        "lastError": status.get("lastError"),
                        "cached": False,
                        "needsManualCheck": False,
                    }
                )
                self._json({"ok": True, "status": status})
                return
            if path == "/api/updates/cli":
                self._read_json(optional=True)
                self._json({"ok": True, "result": maintenance.update_cli()})
                return
            if path == "/api/updates/desktop":
                self._read_json(optional=True)
                self._json({"ok": True, "result": maintenance.open_desktop_update()})
                return
            if path == "/api/emergency/repair":
                payload = self._read_json()
                check_ids = payload.get("checkIds")
                if not isinstance(check_ids, list):
                    raise core.ManagerError("checkIds 必须是数组。")
                self._json({"ok": True, "result": maintenance.apply_emergency_repairs(check_ids)})
                return
            if path == "/api/claude/profiles":
                self._json({"ok": True, "profile": claude.save_profile(self._read_json())})
                return
            if path == "/api/claude/apply":
                payload = self._read_json()
                self._json({"ok": True, "result": claude.apply_profile(str(payload.get("id") or ""))})
                return
            if path == "/api/claude/restore":
                self._read_json(optional=True)
                self._json({"ok": True, "result": claude.restore_official()})
                return
            if path == "/api/usage/reset":
                self._read_json(optional=True)
                usage, point = recovery_coordinator.reset_usage(self.server.runtime)
                self._json({"ok": True, "usage": usage, "backup": point})
                return
            if path == "/api/usage/export-download":
                from usage_export_service import export_usage
                result = export_usage(self.server.runtime, self._read_json())
                self._json({"ok": True, "result": result})
                return
            if path == "/api/recovery":
                payload = self._read_json(optional=True)
                scope = str(payload.get("scope") or "configuration")
                point = recovery_coordinator.create(self.server.runtime, scope, str(payload.get("name") or ""))
                self._json({"ok": True, "point": point})
                return
            recovery_action = re.fullmatch(r"/api/recovery/([^/]+)/(preview|restore|delete)", path)
            if recovery_action:
                payload = self._read_json(optional=True)
                point_id, action = recovery_action.groups()
                if action == "preview":
                    result = recovery.preview(point_id)
                elif action == "delete":
                    result = recovery.delete(point_id)
                else:
                    result = recovery_coordinator.restore(self.server.runtime, point_id, str(payload.get("expectedFingerprint") or ""))
                self._json({"ok": True, "result": result})
                return
            if path == "/api/radar/refresh":
                payload = self._read_json(optional=True)
                section = str(payload.get("section") or "").strip().casefold()
                if section == "intelligence":
                    refreshed = self.server.runtime.radar.get_intelligence(refresh=True)
                elif section == "quota":
                    # The weekly cadence governs automatic background refreshes.
                    # A deliberate click in the UI must always perform a request.
                    refreshed = self.server.runtime.radar.get_quota(refresh=True, force=True)
                elif section == "reset":
                    accounts = core.load_settings().get("accounts", [])
                    refreshed = self.server.runtime.radar.get_reset_radar(
                        accounts if isinstance(accounts, list) else [],
                        refresh=True,
                    )
                    if refreshed.get("newAlert") and isinstance(refreshed.get("alert"), dict):
                        self.server.notify_radar_alert(refreshed["alert"])
                else:
                    raise core.ManagerError("雷达刷新类型无效；请选择智力、额度或重置雷达。")
                self._json({"ok": True, section: _radar_api_section(refreshed)})
                return
            if path == "/api/radar/check":
                self._read_json(optional=True)
                result = self.server.runtime.check_radar_now()
                accounts = core.load_settings().get("accounts", [])
                refreshed = self.server.runtime.radar.get_reset_radar(accounts, refresh=False)
                self._json({"ok": True, "result": result, "reset": _radar_api_section(refreshed)})
                return
            self._error("API 路径不存在。", 404)
        except (core.ManagerError, radar.RadarError, toolbox.ToolboxError, app_updates.UpdateError) as exc:
            self._error(str(exc), 400)
        except Exception as exc:
            self._internal_error(exc)

    def do_DELETE(self) -> None:
        self.server.last_request_at = time.time()
        if not self._validate_local_request(write=True):
            return
        path = urlparse(self.path).path
        if not self._authorized():
            self._error("未授权的本地请求。", 403)
            return
        if not self.server.begin_mutating_request():
            self._discard_small_request_body()
            self._error("Agent Manager 正在退出，已拒绝新的修改请求。", 503)
            return
        self._mutation_registered = True
        try:
            totp_item = re.fullmatch(r"/api/toolbox/totp/([^/]+)", path)
            if totp_item:
                removed = toolbox.delete_totp_item(unquote(totp_item.group(1)))
                if not removed:
                    raise toolbox.ValidationError("未找到验证码项目。")
                self._json({"ok": True})
                return
            mail_account = re.fullmatch(r"/api/toolbox/mail/([^/]+)", path)
            if mail_account:
                account_id = unquote(mail_account.group(1))
                removed = toolbox.delete_mail_account(account_id)
                if not removed:
                    raise toolbox.ValidationError("未找到邮箱账户。")
                self.server.runtime.toolbox.forget_mail(account_id)
                self._json({"ok": True})
                return
            provider_key = re.fullmatch(r"/api/providers/([^/]+)/key", path)
            if provider_key:
                _result, point = recovery_coordinator.before_account_delete(self.server.runtime, "移除本地 API Key 前", lambda: core.delete_provider_key(unquote(provider_key.group(1))))
                self._json({"ok": True, "backup": point})
                return
            relay_account_key = re.fullmatch(
                r"/api/relay-accounts/([^/]+)/keys/([^/]+)",
                path,
            )
            if relay_account_key:
                payload = self._read_json(optional=True)
                scope = payload.get("scope", "local")
                if scope not in ("local", "website"):
                    raise core.ManagerError("删除范围必须为 local 或 website。")
                def operation():
                    return self.server.runtime.relay_portal.delete_saved_key(
                        unquote(relay_account_key.group(1)), unquote(relay_account_key.group(2)),
                        delete_remote=scope == "website",
                    )
                if scope == "website":
                    # Snapshot locally before the remote mutation. Do not hold
                    # the gateway's global lock during a website request.
                    point = recovery_coordinator.create(self.server.runtime, "configuration", "删除网站 Key 前")
                    result = operation()
                else:
                    result, point = recovery_coordinator.before_account_delete(self.server.runtime, "移除中转站本地 Key 前", operation)
                self._json({"ok": True, "result": result, "backup": point})
                return
            relay_account = re.fullmatch(r"/api/relay-accounts/([^/]+)", path)
            if relay_account:
                _result, point = recovery_coordinator.before_account_delete(self.server.runtime, "删除中转站账号前", lambda: core.remove_relay_account(unquote(relay_account.group(1))))
                self._json({"ok": True, "backup": point})
                return
            provider = re.fullmatch(r"/api/providers/([^/]+)", path)
            if provider:
                _result, point = recovery_coordinator.before_account_delete(self.server.runtime, "删除 API Provider 前", lambda: core.remove_provider(unquote(provider.group(1))))
                warning = None
                applied = None
                try:
                    applied = core.apply_configuration(False)
                    if applied.get("gatewayRequired") and not self.server.runtime.web2api.status()["running"]:
                        self.server.runtime.web2api.start()
                except Exception as exc:
                    warning = f"中转站已删除，但子代理配置刷新失败：{str(exc)[:300]}"
                self._json({"ok": True, "applied": applied, "configurationWarning": warning, "backup": point})
                return
            profile = re.fullmatch(r"/api/main-profiles/([^/]+)", path)
            if profile:
                core.remove_main_profile(unquote(profile.group(1)))
                self._json({"ok": True})
                return
            strategy = re.fullmatch(r"/api/strategies/([^/]+)", path)
            if strategy:
                core.remove_strategy(unquote(strategy.group(1)))
                self._json({"ok": True})
                return
            agent = re.fullmatch(r"/api/agents/([^/]+)", path)
            if agent:
                destination = core.archive_agent(unquote(agent.group(1)))
                self._json({"ok": True, "backupPath": str(destination)})
                return
            account = re.fullmatch(r"/api/accounts/([^/]+)", path)
            if account:
                _result, point = recovery_coordinator.before_account_delete(self.server.runtime, "删除 Codex 账号前", lambda: core.remove_codex_account(unquote(account.group(1))))
                warning = None
                applied = None
                try:
                    applied = core.apply_configuration(False)
                    if applied.get("gatewayRequired") and not self.server.runtime.web2api.status()["running"]:
                        self.server.runtime.web2api.start()
                except Exception as exc:
                    warning = f"账号已删除，但子代理配置刷新失败：{str(exc)[:300]}"
                self._json({"ok": True, "applied": applied, "configurationWarning": warning, "backup": point})
                return
            account_group = re.fullmatch(r"/api/account-groups/([^/]+)", path)
            if account_group:
                result = core.remove_account_group(unquote(account_group.group(1)))
                self._json({"ok": True, "result": result})
                return
            skill = re.fullmatch(r"/api/skills/([^/]+)", path)
            if skill:
                payload = self._read_json(optional=True)
                result = maintenance.delete_skill(
                    unquote(skill.group(1)),
                    str(payload.get("expectedFingerprint") or ""),
                    cwd=payload.get("cwd"),
                )
                self._json({"ok": True, "result": result})
                return
            claude_profile = re.fullmatch(r"/api/claude/profiles/([^/]+)", path)
            if claude_profile:
                self._json({"ok": True, "result": claude.delete_profile(unquote(claude_profile.group(1)))})
                return
            self._error("API 路径不存在。", 404)
        except (core.ManagerError, toolbox.ToolboxError) as exc:
            self._error(str(exc), 400)
        except Exception as exc:
            self._internal_error(exc)


def find_edge() -> Path | None:
    candidates = []
    for key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        root = os.environ.get(key)
        if root:
            candidates.append(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    return next((path for path in candidates if path.is_file()), None)


def open_browser_window(url: str) -> bool:
    edge = find_edge()
    if edge:
        flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        subprocess.Popen(
            [str(edge), f"--app={url}", "--window-size=1320,860"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags if os.name == "nt" else 0,
        )
        return True
    return bool(webbrowser.open(url))


def open_external_browser(url: str) -> bool:
    """Open trusted third-party pages in the user's normal browser profile.

    Unlike ``open_browser_window`` this intentionally avoids Edge app mode:
    provider dashboards should reuse the user's existing browser login state,
    while Agent Manager never reads or stores those browser cookies.
    """

    return bool(webbrowser.open_new_tab(url))


def _report_startup_error(message: str) -> None:
    detail = str(message or "Agent Manager 启动失败。")[:1200]
    try:
        core.atomic_write_json(
            SHUTDOWN_STATUS_FILE,
            {"phase": "startup-error", "at": core.now_iso(), "errors": [detail], "native": True},
        )
    except Exception:
        pass
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, detail, f"{core.APP_NAME} 启动失败", 0x10 | 0x00040000)
            return
        except Exception:
            pass
    if sys.stderr is not None:
        print(f"{core.APP_NAME} 启动失败：{detail}", file=sys.stderr, flush=True)


def _activation_token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8", errors="replace")).hexdigest()


def _pid_is_running(process_id: int) -> bool:
    if process_id <= 0:
        return False
    if process_id == os.getpid():
        return True
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, process_id)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(process_id, 0)
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
        runtime.get("appId") != RUNTIME_APP_ID
        or runtime_pid <= 0
        or not 0 < port <= 65535
        or len(runtime_nonce) < 16
        or len(activation_token) < 16
        or (control_token and len(control_token) < 16)
        or not _pid_is_running(runtime_pid)
    ):
        return False
    try:
        health_pid = int(health.get("runtimePid") or 0)
    except (TypeError, ValueError):
        return False
    identity_matches = bool(
        health.get("ok") is True
        and health.get("appId") == RUNTIME_APP_ID
        and health_pid == runtime_pid
        and secrets.compare_digest(str(health.get("runtimeNonce") or ""), runtime_nonce)
        and secrets.compare_digest(
            str(health.get("activationTokenHash") or ""),
            _activation_token_hash(activation_token),
        )
        and (
            not control_token
            or secrets.compare_digest(
                str(health.get("controlTokenHash") or ""),
                _activation_token_hash(control_token),
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
            runtime.get("appId") != RUNTIME_APP_ID
            or not 0 < port <= 65535
            or runtime_pid <= 0
            or len(str(runtime.get("runtimeNonce") or "")) < 16
            or len(str(runtime.get("activationToken") or "")) < 16
            or not _pid_is_running(runtime_pid)
        ):
            return False
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health",
            timeout=max(0.1, float(timeout_seconds)),
        ) as response:
            if response.status != 200:
                return False
            raw = response.read(65_537)
            if len(raw) > 65_536:
                return False
            health = json.loads(raw.decode("utf-8"))
    except Exception:
        return False
    return _runtime_identity_is_valid(
        runtime,
        health,
        require_ui_ready=require_ui_ready,
        require_independent=require_independent,
    )


def focus_process_window(process_id: int) -> bool:
    if os.name != "nt":
        return False
    found: list[tuple[int, int]] = []
    user32 = ctypes.windll.user32

    class Rect(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    def callback(hwnd: int, _lparam: int) -> bool:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == process_id and user32.GetWindowTextLengthW(hwnd) > 0:
            title_length = int(user32.GetWindowTextLengthW(hwnd))
            title_buffer = ctypes.create_unicode_buffer(title_length + 1)
            user32.GetWindowTextW(hwnd, title_buffer, title_length + 1)
            rect = Rect()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            width = max(0, int(rect.right - rect.left))
            height = max(0, int(rect.bottom - rect.top))
            score = width * height
            if title_buffer.value.strip().casefold() == core.APP_NAME.casefold():
                score += 100_000_000
            if user32.IsWindowVisible(hwnd):
                score += 10_000_000
            if not user32.GetWindow(hwnd, 4):  # GW_OWNER
                score += 1_000_000
            found.append((score, hwnd))
        return True

    enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(callback)
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
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/window/show",
            data=b"{}",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Agent-Manager-Activation": token,
            },
        )
        with urllib.request.urlopen(request, timeout=1.2) as response:
            raw = response.read(MAX_EXISTING_INSTANCE_RESPONSE_BYTES + 1)
            status = response.status
        if len(raw) > MAX_EXISTING_INSTANCE_RESPONSE_BYTES:
            return False
        payload = json.loads(raw.decode("utf-8"))
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

    record = _read_runtime_file() if runtime is None else dict(runtime)
    token = str(record.get("controlToken") or "")
    if len(token) < 16:
        raise core.ManagerError("当前 Agent Manager 版本尚未提供本机快速重启接口。")
    if not _probe_runtime_identity(
        record,
        require_ui_ready=True,
        require_independent=True,
    ):
        raise core.ManagerError("无法验证正在运行的 Agent Manager，未执行快速重启。")
    try:
        port = int(record.get("port") or 0)
    except (TypeError, ValueError) as exc:
        raise core.ManagerError("Agent Manager 本机控制端口无效。") from exc
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{CONTROL_QUICK_RESTART_PATH}",
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            CONTROL_HEADER_NAME: token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=RESTART_STAGE_TIMEOUT_SECONDS + 8) as response:
            raw = response.read(MAX_EXISTING_INSTANCE_RESPONSE_BYTES + 1)
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read(MAX_EXISTING_INSTANCE_RESPONSE_BYTES).decode("utf-8"))
        except Exception:
            detail = {}
        finally:
            exc.close()
        raise core.ManagerError(str(detail.get("error") or f"快速重启请求失败：HTTP {exc.code}")) from exc
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise core.ManagerError("无法连接正在运行的 Agent Manager 快速重启接口。") from exc
    if len(raw) > MAX_EXISTING_INSTANCE_RESPONSE_BYTES:
        raise core.ManagerError("快速重启接口返回的数据异常过大。")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise core.ManagerError("快速重启接口返回了无效数据。") from exc
    if status != 200 or not isinstance(payload, dict) or payload.get("ok") is not True:
        raise core.ManagerError(str((payload or {}).get("error") or "快速重启请求未被接受。"))
    return payload


def _read_runtime_file() -> dict:
    """Read the small single-instance handoff file without unbounded I/O."""
    with RUNTIME_FILE.open("rb") as stream:
        raw = stream.read(MAX_RUNTIME_FILE_BYTES + 1)
    if len(raw) > MAX_RUNTIME_FILE_BYTES:
        raise core.ManagerError("Agent Manager 运行状态文件异常过大，已拒绝读取。")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise core.ManagerError("Agent Manager 运行状态文件格式无效。") from exc
    if not isinstance(payload, dict):
        raise core.ManagerError("Agent Manager 运行状态文件结构无效。")
    return payload


def _read_shutdown_status() -> dict:
    """Return one bounded, UI-safe lifecycle record for restart diagnostics."""
    if not SHUTDOWN_STATUS_FILE.is_file():
        return {}
    try:
        with SHUTDOWN_STATUS_FILE.open("rb") as stream:
            raw = stream.read(MAX_RUNTIME_FILE_BYTES + 1)
        if len(raw) > MAX_RUNTIME_FILE_BYTES:
            return {}
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
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
        "errors": [core._redact_sensitive_text(item, limit=300) for item in errors[:8]],
        "native": bool(payload.get("native")),
        "restartTiming": restart_timing,
    }


def open_existing_runtime() -> bool:
    if not RUNTIME_FILE.exists():
        return False
    try:
        runtime = _read_runtime_file()
        if _probe_runtime_identity(runtime):
            opened = request_existing_window(runtime)
            if not opened and bool(runtime.get("native")):
                opened = focus_process_window(int(runtime.get("pid", 0)))
            return opened
    except Exception:
        return False
    return False


def idle_watchdog(server: ManagerServer, timeout_seconds: int = 14_400) -> None:
    while True:
        time.sleep(30)
        if time.time() - server.last_request_at > timeout_seconds:
            request_application_shutdown(server)
            return


def _cleanup_runtime() -> None:
    try:
        runtime = _read_runtime_file()
        if runtime.get("pid") == os.getpid():
            RUNTIME_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    _release_instance_mutex()


def _write_runtime_discovery(server: ManagerServer) -> None:
    control_token = str(getattr(server, "control_token", "") or "")
    session = getattr(getattr(server, "runtime", None), "configuration_session", {})
    timing = session.get("restartTiming", {}) if isinstance(session, dict) else {}
    core.atomic_write_json(
        RUNTIME_FILE,
        {
            "pid": os.getpid(),
            "port": int(server.server_address[1]),
            "appId": RUNTIME_APP_ID,
            "runtimeNonce": server.runtime_nonce,
            "startedAt": str(getattr(server, "started_at", "") or core.now_iso()),
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
            "independentLifecycle": _manager_lifecycle_is_independent(),
            "lifecycleIsolation": _WINDOWS_JOB_ISOLATION_STATUS,
            "activationToken": server.activation_token,
            **(
                {
                    "controlToken": control_token,
                    "controlApi": {
                        "quickRestartPath": CONTROL_QUICK_RESTART_PATH,
                        "header": CONTROL_HEADER_NAME,
                    },
                }
                if control_token
                else {}
            ),
        },
    )


def _mark_ui_ready(server: ManagerServer) -> None:
    """Publish readiness only after the visible UI and config are active."""
    server.ui_ready.set()
    try:
        _write_runtime_discovery(server)
    except Exception:
        server.ui_ready.clear()
        raise


def _write_restart_handoff(token: str, metadata: dict | None = None) -> None:
    details = dict(metadata or {})
    details.pop("token", None)
    details.pop("parentPid", None)
    details.pop("requestedAtEpoch", None)
    details.pop("requestedAt", None)
    core.atomic_write_json(
        RESTART_HANDOFF_FILE,
        {
            "protocolVersion": 2,
            **details,
            "token": token,
            "parentPid": os.getpid(),
            "requestedAtEpoch": time.time(),
            "requestedAt": core.now_iso(),
        },
    )


def _restart_handoff_payload(token: str | None) -> dict | None:
    token = str(token or "").strip()
    if not token:
        return None
    try:
        payload = json.loads(RESTART_HANDOFF_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    expected = str(payload.get("token") or "")
    if not expected or not secrets.compare_digest(expected, token):
        return None
    return payload


def _restart_handoff_is_valid(token: str | None) -> bool:
    payload = _restart_handoff_payload(token)
    if payload is None:
        return False
    try:
        requested_at = float(payload.get("requestedAtEpoch") or 0)
    except (TypeError, ValueError):
        return False
    return 0 <= time.time() - requested_at <= RESTART_HANDOFF_TIMEOUT_SECONDS


def _consume_restart_handoff(token: str | None, *, delete: bool = True) -> bool:
    payload = _restart_handoff_payload(token)
    if payload is None:
        return False
    try:
        requested_at = float(payload.get("requestedAtEpoch") or 0)
    except (TypeError, ValueError):
        return False
    valid = 0 <= time.time() - requested_at <= RESTART_HANDOFF_TIMEOUT_SECONDS
    if delete:
        RESTART_HANDOFF_FILE.unlink(missing_ok=True)
    return valid


def _mark_restart_child_staged(token: str) -> dict | None:
    """Publish that extraction/imports completed and the child is at the mutex."""
    payload = _restart_handoff_payload(token)
    if payload is None or not _restart_handoff_is_valid(token):
        return None
    payload.update(
        {
            "childPid": os.getpid(),
            "childStagedAtEpoch": time.time(),
            "childBuild": _current_manager_build_fingerprint(),
        }
    )
    core.atomic_write_json(RESTART_HANDOFF_FILE, payload)
    return payload


def _arm_restart_readiness_deadline(token: str) -> dict:
    payload = _restart_handoff_payload(token)
    if payload is None or not payload.get("childStagedAtEpoch"):
        raise core.ManagerError("新管理器未完成启动预热，快速重启已取消。")
    payload["readyDeadlineEpoch"] = time.time() + RESTART_REPLACEMENT_READY_SECONDS
    core.atomic_write_json(RESTART_HANDOFF_FILE, payload)
    return payload


def _take_restart_handoff(token: str | None) -> dict | None:
    payload = _restart_handoff_payload(token)
    if payload is None or not _restart_handoff_is_valid(token):
        return None
    RESTART_HANDOFF_FILE.unlink(missing_ok=True)
    return payload


def _executable_version(path: Path) -> tuple[int, int, int, int]:
    match = re.fullmatch(
        r"(?:AgentManager|CodexAgentManager)-(\d+)\.(\d+)\.(\d+)(?:[.-](\d+))?\.exe",
        path.name,
        re.IGNORECASE,
    )
    filename_version = tuple(int(value or 0) for value in match.groups()) if match else (0, 0, 0, 0)
    if os.name != "nt" or not path.is_file():
        return filename_version
    try:
        version = ctypes.windll.version
        size = int(version.GetFileVersionInfoSizeW(str(path), None))
        if size <= 0:
            return filename_version
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return filename_version
        value = ctypes.c_void_p()
        length = wintypes.UINT()
        if not version.VerQueryValueW(buffer, "\\", ctypes.byref(value), ctypes.byref(length)):
            return filename_version

        class FixedFileInfo(ctypes.Structure):
            _fields_ = [
                ("signature", wintypes.DWORD),
                ("struct_version", wintypes.DWORD),
                ("file_version_ms", wintypes.DWORD),
                ("file_version_ls", wintypes.DWORD),
            ]

        fixed = ctypes.cast(value, ctypes.POINTER(FixedFileInfo)).contents
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


def _manager_build_fingerprint(path: Path | None = None) -> str:
    """Identify configuration-generating code without trusting a filename."""
    digest = hashlib.sha256()
    digest.update(f"quick-restart-v2\0schema={getattr(core, 'SCHEMA_VERSION', 0)}\0".encode("ascii"))
    if getattr(sys, "frozen", False):
        candidates = [Path(path or sys.executable).resolve()]
    else:
        candidates = [Path(__file__).resolve(), Path(core.__file__).resolve()]
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
    return _manager_build_fingerprint(
        Path(sys.executable).resolve() if getattr(sys, "frozen", False) else None
    )


def _runtime_overlay_matches_last_apply() -> bool:
    """Verify every journaled managed value still has its last applied hash."""
    try:
        with core.RUNTIME_OVERLAY_LOCK:
            payload = core._runtime_overlay_read()
            if not payload or int(payload.get("ownerPid") or 0) != os.getpid():
                return False
            files = payload.get("files")
            environment = payload.get("environment")
            if not isinstance(files, dict) or not files:
                return False
            if not isinstance(environment, dict):
                return False
            root = core.CODEX_HOME.resolve()
            for relative, record in files.items():
                if not isinstance(record, dict) or not str(record.get("appliedHash") or ""):
                    return False
                candidate = (root / str(relative)).resolve()
                candidate.relative_to(root)
                current = candidate.read_bytes() if candidate.is_file() else None
                if not secrets.compare_digest(
                    str(record["appliedHash"]),
                    str(core._overlay_value_hash(current)),
                ):
                    return False
            for name, record in environment.items():
                if not isinstance(record, dict) or not str(record.get("appliedHash") or ""):
                    return False
                current = core._read_user_environment(str(name))
                if not secrets.compare_digest(
                    str(record["appliedHash"]),
                    str(core._overlay_value_hash(current)),
                ):
                    return False
    except Exception:
        return False
    return True


def _restart_configuration_fingerprint() -> str:
    """Hash saved settings and model capabilities, excluding cache freshness."""
    digest = hashlib.sha256()
    try:
        for index, path in enumerate((core.SETTINGS_FILE, core.MODELS_CACHE_FILE)):
            digest.update(str(index).encode("ascii"))
            if not path.is_file():
                digest.update(b"<absent>")
                continue
            content = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(content, dict):
                return ""
            if index == 1:
                # Codex routinely rewrites these even when its model records
                # are identical. Neither field affects generated configuration.
                content.pop("fetched_at", None)
                content.pop("etag", None)
            digest.update(json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    except (OSError, ValueError):
        return ""
    return digest.hexdigest()


def _quick_restart_can_adopt_without_apply(handoff: dict | None) -> bool:
    """Use the fast path only for an unchanged build and unchanged overlay."""
    if not isinstance(handoff, dict) or handoff.get("protocolVersion") != 2:
        return False
    source_build = str(handoff.get("sourceBuild") or "")
    target_build = str(handoff.get("targetBuild") or "")
    current_build = _current_manager_build_fingerprint()
    return bool(
        handoff.get("sourceOverlayCurrent") is True
        and source_build
        and secrets.compare_digest(source_build, target_build)
        and secrets.compare_digest(target_build, current_build)
        and bool(handoff.get("sourceStateFingerprint"))
        and secrets.compare_digest(str(handoff["sourceStateFingerprint"]), _restart_configuration_fingerprint())
        and _runtime_overlay_matches_last_apply()
    )


def _manager_restart_command(token: str) -> tuple[list[str], Path]:
    if getattr(sys, "frozen", False):
        current = Path(sys.executable).resolve()
        candidates = [current]
        for pattern in ("AgentManager*.exe", "CodexAgentManager*.exe"):
            for candidate in current.parent.glob(pattern):
                if re.fullmatch(
                    r"(?:AgentManager|CodexAgentManager)(?:-[0-9][A-Za-z0-9_.-]*)?\.exe",
                    candidate.name,
                    re.IGNORECASE,
                ):
                    candidates.append(candidate.resolve())
        target = max(
            dict.fromkeys(candidates),
            key=lambda path: (
                _executable_version(path),
                path.stat().st_mtime_ns if path.is_file() else 0,
                path.name.casefold() == "agentmanager.exe",
            ),
        )
        # Use the ``--option=value`` form so a URL-safe token that happens to
        # begin with '-' can never be parsed as another command-line option.
        return [str(target), f"--quick-restart-token={token}"], target.parent
    script = Path(__file__).resolve()
    return [sys.executable, str(script), f"--quick-restart-token={token}"], script.parent


def _start_restarted_manager(
    token: str,
    *,
    command: list[str] | None = None,
    workdir: Path | None = None,
    source_pid: int | None = None,
    source_nonce: str | None = None,
) -> dict:
    if command is None or workdir is None:
        command, workdir = _manager_restart_command(token)
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    launch_environment = os.environ.copy()
    # A PyInstaller one-file child inherits bootloader state from its parent.
    # Without this reset, relaunching the same EXE can reuse the old temporary
    # extraction directory; when the old parent exits, the new GUI assets are
    # deleted underneath the restarted manager.  Force a fully independent
    # bootloader parent for every update/restart handoff.
    launch_environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    if (
        os.name == "nt"
        and getattr(sys, "frozen", False)
        and _manager_lifecycle_is_independent()
    ):
        launch_environment[TRUSTED_RESTART_ENV] = "1"
    process = subprocess.Popen(
        command,
        cwd=str(workdir),
        close_fds=True,
        creationflags=flags,
        env=launch_environment,
    )
    return {
        "process": process,
        "command": list(command),
        "workdir": Path(workdir),
        "token": token,
        "sourcePid": int(source_pid or os.getpid()),
        "sourceNonce": str(source_nonce or ""),
        "startedMonotonic": time.monotonic(),
    }


def _wait_for_restart_child_staged(
    launch: dict,
    timeout_seconds: float = RESTART_STAGE_TIMEOUT_SECONDS,
) -> dict:
    process = launch["process"]
    token = str(launch.get("token") or "")
    deadline = time.monotonic() + max(0.5, min(float(timeout_seconds), 45.0))
    launcher_exit_code: int | None = None
    last_error = "新管理器仍在解包和加载模块"
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            launcher_exit_code = int(exit_code)
            if launcher_exit_code != 0:
                raise core.ManagerError(f"新管理器启动器提前退出，代码 {launcher_exit_code}。")
        payload = _restart_handoff_payload(token)
        if payload is not None and payload.get("childStagedAtEpoch"):
            child_build = str(payload.get("childBuild") or "")
            target_build = str(payload.get("targetBuild") or "")
            if target_build and not secrets.compare_digest(child_build, target_build):
                raise core.ManagerError("新管理器预热进程与选定程序不一致，快速重启已取消。")
            return {
                "staged": True,
                "childPid": int(payload.get("childPid") or 0),
                "launcherExitCode": launcher_exit_code,
                "stageMs": round(
                    (time.monotonic() - float(launch["startedMonotonic"])) * 1000,
                    1,
                ),
            }
        if payload is None:
            last_error = "快速重启交接凭据已失效"
            break
        time.sleep(RESTART_POLL_INTERVAL_SECONDS)
    raise core.ManagerError(f"新管理器未能在旧管理器离线前完成预热：{last_error}。")


def _wait_for_restarted_manager(
    launch: dict,
    ready_timeout_seconds: float = RESTART_REPLACEMENT_READY_SECONDS
    + RESTART_REPLACEMENT_EXIT_GRACE_SECONDS,
) -> dict:
    process = launch["process"]
    command = list(launch["command"])
    deadline = time.monotonic() + max(3.0, min(float(ready_timeout_seconds), 45.0))
    last_error = "新进程尚未写入运行状态"
    launcher_exit_code: int | None = None
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            launcher_exit_code = int(exit_code)
            # A packaged manager may perform one CREATE_BREAKAWAY_FROM_JOB or
            # ShellExecute relay before the real runtime starts. That relay
            # exits with code 0 by design. Readiness belongs to the verified
            # runtime-file + health handshake, not to the short-lived launcher
            # PID returned by Popen. Non-zero still fails immediately.
            if launcher_exit_code != 0:
                raise core.ManagerError(f"新管理器启动器提前退出，代码 {launcher_exit_code}。")
        try:
            runtime = _read_runtime_file()
            port = int(runtime.get("port") or 0)
            runtime_pid = int(runtime.get("pid") or 0)
            runtime_nonce = str(runtime.get("runtimeNonce") or "")
            if runtime_pid == int(launch.get("sourcePid") or 0):
                last_error = "旧管理器仍在释放运行状态"
                time.sleep(RESTART_POLL_INTERVAL_SECONDS)
                continue
            if launch.get("sourceNonce") and secrets.compare_digest(
                runtime_nonce,
                str(launch["sourceNonce"]),
            ):
                last_error = "运行状态仍属于旧管理器"
                time.sleep(RESTART_POLL_INTERVAL_SECONDS)
                continue
            if not bool(runtime.get("uiReady")):
                last_error = "新管理器窗口尚未就绪"
            elif not bool(runtime.get("independentLifecycle")):
                last_error = "新管理器尚未完成与 Codex 的生命周期隔离"
            if port > 0 and runtime_pid > 0 and _probe_runtime_identity(
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
                        (time.monotonic() - float(launch["startedMonotonic"])) * 1000,
                        1,
                    ),
                    "offlineMs": round(
                        (time.monotonic() - float(launch.get("offlineMonotonic") or launch["startedMonotonic"]))
                        * 1000,
                        1,
                    ),
                }
        except Exception as exc:
            last_error = str(exc)[:240]
        time.sleep(RESTART_POLL_INTERVAL_SECONDS)
    relay_detail = "；启动中继已正常退出" if launcher_exit_code == 0 else ""
    raise core.ManagerError(f"新管理器在限定时间内未就绪{relay_detail}：{last_error}")


def _launch_restarted_manager(token: str, ready_timeout_seconds: float = 20.0) -> dict:
    """Compatibility wrapper for callers that do not use prewarmed handoff."""
    launch = _start_restarted_manager(token)
    return _wait_for_restarted_manager(launch, ready_timeout_seconds)


def _write_shutdown_status(server: ManagerServer, phase: str, errors: list[str] | None = None) -> None:
    try:
        core.atomic_write_json(
            SHUTDOWN_STATUS_FILE,
            {
                "pid": os.getpid(),
                "phase": phase,
                "runtimeNonce": str(getattr(server, "runtime_nonce", "")),
                "at": core.now_iso(),
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


def _forced_exit_watchdog(server: ManagerServer) -> None:
    finished = server.shutdown_finished.wait(FORCED_EXIT_TIMEOUT_SECONDS)
    if server.shutdown_aborted.is_set():
        return
    if not finished:
        # A timeout is not permission to leave the overlay or gateway orphaned.
        # The existing cleanup worker still owns recovery and the instance lock.
        _write_shutdown_status(server, "shutdown-timeout", ["退出清理尚未完成，管理器继续等待安全恢复。"])
        return
    if finished:
        # Give WebView2 and the Python interpreter a brief opportunity to tear
        # down normally.  If any third-party non-daemon thread remains, force a
        # clean process boundary after all application state has been restored.
        time.sleep(1.5)
    if server.shutdown_aborted.is_set():
        return
    result = getattr(server, "shutdown_result", {})
    if not (result.get("completed") and (result.get("restorationComplete") or result.get("preserved"))):
        return
    # Keep the completed/completed-with-errors status and restore evidence for
    # external maintenance helpers; do not replace it with an ambiguous phase.
    _cleanup_runtime()
    os._exit(1 if getattr(server, "shutdown_errors", []) else 0)


def _restart_readiness_watchdog(server: ManagerServer, handoff: dict | None) -> None:
    """Gracefully retire a replacement that never becomes UI-ready."""
    if not isinstance(handoff, dict):
        return
    try:
        deadline_epoch = float(handoff.get("readyDeadlineEpoch") or 0)
    except (TypeError, ValueError):
        return
    remaining = deadline_epoch - time.time()
    if deadline_epoch <= 0 or remaining > RESTART_HANDOFF_TIMEOUT_SECONDS:
        return
    if server.ui_ready.wait(max(0.0, remaining)):
        return
    if server.shutdown_started.is_set():
        return
    server.allow_forced_process_exit = False
    _write_shutdown_status(
        server,
        "quick-restart-replacement-not-ready",
        ["新管理器未在交接期限内完成窗口激活，已安全退出并交回旧管理器。"],
    )
    request_application_exit_only(server)


def _schedule_restart_readiness_watchdog(
    server: ManagerServer,
    handoff: dict | None,
) -> threading.Thread | None:
    if not isinstance(handoff, dict) or not handoff.get("readyDeadlineEpoch"):
        return None
    worker = threading.Thread(
        target=_restart_readiness_watchdog,
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
    return True if not callable(callback) else bool(callback(MUTATION_DRAIN_TIMEOUT_SECONDS))


def prepare_application_update(server):
    """Explicit one-click update continues even after navigating away from Settings."""
    service = server.runtime.get_app_updates()
    if not service._operation.acquire(blocking=False):
        raise core.ManagerError("更新正在处理，请勿重复操作。")
    try:
        if not service.status().get("canInstall"):
            raise core.ManagerError("请先成功检查、下载并校验更新，并使用 Windows EXE 版本安装。")
        from app_update_installer import prepare_install, launch_install
        prepared = prepare_install(service, target=sys.executable, state_dir=core.STATE_DIR,
            shutdown_status=SHUTDOWN_STATUS_FILE, source_pid=os.getpid(),
            source_nonce=server.runtime_nonce, runtime_file=RUNTIME_FILE)
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
    timer = threading.Timer(0.25, request_application_shutdown, args=(server,))
    timer.daemon = True
    timer.start()
    return result


def request_application_shutdown(server: ManagerServer) -> bool:
    """Start exactly one non-daemon cleanup worker for every exit source."""
    if not server.claim_shutdown():
        return False
    server.exit_only_requested = False
    worker = threading.Thread(
        target=shutdown_application,
        args=(server, True),
        name="codex-agent-manager-shutdown",
        daemon=False,
    )
    worker.start()
    return True


def request_application_restart(server: ManagerServer) -> bool:
    """Restart only the manager process and preserve the running Codex session."""
    if not bool(server.runtime.configuration_session.get("active")):
        raise core.ManagerError("当前处于安全修复模式；请先完成紧急修复，再执行快速重启。")
    if not server.claim_shutdown():
        return False
    token = secrets.token_urlsafe(32)
    launch = None
    try:
        command, workdir = _manager_restart_command(token)
        source_build = _current_manager_build_fingerprint()
        target_build = (
            _manager_build_fingerprint(Path(command[0]).resolve())
            if getattr(sys, "frozen", False)
            else source_build
        )
        if not source_build or not target_build:
            raise core.ManagerError("无法校验管理器程序版本，快速重启已取消。")
        _write_restart_handoff(
            token,
            {
                "sourceBuild": source_build,
                "targetBuild": target_build,
                "sourceOverlayCurrent": _runtime_overlay_matches_last_apply(),
                "preserveExternalSelection": live_selection.preserve_live_configuration_on_restart(),
                "sourceRuntimeNonce": str(getattr(server, "runtime_nonce", "") or ""),
                "sourceStateFingerprint": str(server.runtime.configuration_session.get("configurationStateFingerprint") or ""),
            },
        )
        launch = _start_restarted_manager(
            token,
            command=command,
            workdir=workdir,
            source_pid=os.getpid(),
            source_nonce=str(getattr(server, "runtime_nonce", "") or ""),
        )
        prepare = getattr(server.runtime, "prepare_for_restart", None)
        prepared = prepare() if callable(prepare) else {}
        staged = _wait_for_restart_child_staged(launch)
        handoff = _restart_handoff_payload(token)
    except Exception as exc:
        payload = _restart_handoff_payload(token)
        if payload is not None:
            RESTART_HANDOFF_FILE.unlink(missing_ok=True)
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
        _reopen_server_mutations(server)
        _write_shutdown_status(server, "quick-restart-preflight-failed", [str(exc)[:300]])
        if isinstance(exc, core.ManagerError):
            raise
        raise core.ManagerError(f"新管理器预热失败：{str(exc)[:300]}") from exc
    server.quick_restart_requested = True
    server.quick_restart_token = token
    server.quick_restart_launch = launch
    server.quick_restart_handoff = handoff
    server.quick_restart_timing = {
        **({"prepareMs": prepared.get("elapsedMs")} if isinstance(prepared, dict) else {}),
        **staged,
        "buildChanged": not secrets.compare_digest(source_build, target_build),
    }
    worker = threading.Thread(
        target=shutdown_application,
        args=(server, True, True),
        name="codex-agent-manager-quick-restart",
        daemon=False,
    )
    worker.start()
    return True


def _recover_unexpected_native_window_exit(server: ManagerServer, error: object | None = None) -> bool:
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
    if isinstance(shutdown_started, threading.Event) and shutdown_started.is_set():
        if isinstance(shutdown_finished, threading.Event):
            shutdown_finished.wait(NATIVE_WINDOW_RECOVERY_WAIT_SECONDS)
        if not isinstance(shutdown_aborted, threading.Event) or not shutdown_aborted.is_set():
            # The claimed worker owns cleanup even if the bounded wait elapsed.
            # Running another finalizer concurrently is the dangerous case.
            return True

    configuration_session = getattr(getattr(server, "runtime", None), "configuration_session", {})
    if not isinstance(configuration_session, dict) or not configuration_session.get("active"):
        return False

    detail = str(error or "WebView2 事件循环意外结束")[:500]
    recovery_errors = [f"桌面窗口异常退出：{detail}"]
    if os.environ.get(NATIVE_WINDOW_RECOVERY_ENV) != "1":
        os.environ[NATIVE_WINDOW_RECOVERY_ENV] = "1"
        _write_shutdown_status(server, "native-window-restart", recovery_errors)
        try:
            started = request_application_restart(server)
            if started:
                if isinstance(shutdown_finished, threading.Event):
                    completed = shutdown_finished.wait(NATIVE_WINDOW_RECOVERY_WAIT_SECONDS)
                else:
                    completed = True
                aborted = isinstance(shutdown_aborted, threading.Event) and shutdown_aborted.is_set()
                if completed and not aborted:
                    return True
                if isinstance(shutdown_started, threading.Event) and shutdown_started.is_set() and not aborted:
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
    _write_shutdown_status(server, "native-window-recovery-required", recovery_errors)
    _report_startup_error(
        "Agent Manager 桌面窗口异常退出且自动重启未完成；"
        "将尝试关闭 Codex 并恢复原始配置。恢复失败时保留管理服务与恢复记录。"
    )
    return False


def request_application_exit_only(server: ManagerServer) -> bool:
    """Exit the manager without closing Codex or restoring its active overlay."""
    if not server.claim_shutdown():
        return False
    server.exit_only_requested = True
    threading.Thread(
        target=shutdown_application,
        args=(server,),
        kwargs={"claimed": True, "exit_only": True},
        name="agent-manager-exit-only",
        daemon=False,
    ).start()
    return True


def _handle_startup_activation_failure(server: ManagerServer, phase: str, error: object) -> None:
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
        _write_shutdown_status(server, phase, [message])
        _report_startup_error(message)
        return
    message = f"{surface}初始化失败，已开始回滚 Codex 配置：{str(error)[:700]}"
    _write_shutdown_status(server, phase, [message])
    _report_startup_error(message)
    server.force_exit = True
    request_application_shutdown(server)


def handle_native_window_closing(server: ManagerServer, window: object) -> bool | None:
    if server.force_exit:
        started = getattr(server, "shutdown_started", None)
        if (
            isinstance(started, threading.Event) and started.is_set()
            and not getattr(getattr(server, "runtime", None), "_closed", False)
        ):
            # Repeated title-bar clicks while cleanup is running must not
            # destroy the repair window before restoration has been verified.
            return False
        return None
    try:
        behavior = core.load_settings().get("appBehavior", {})
    except Exception as exc:
        _write_shutdown_status(server, "close-settings-unavailable", [str(exc)[:300]])
        behavior = {}
    if behavior.get("closeToTray") and not server.force_exit:
        if server.ensure_tray():
            window.hide()
        return False
    if not server.force_exit:
        # Keep the window until guarded cleanup succeeds; a blocked restore
        # must leave a usable repair surface rather than a hidden process.
        request_application_shutdown(server)
        return False
    return None


def shutdown_application(
    server: ManagerServer,
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
    _write_shutdown_status(server, "quick-restart-starting" if quick_restart else "starting")
    if server.allow_forced_process_exit:
        threading.Thread(
            target=_forced_exit_watchdog,
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
        if not _wait_for_server_mutations(server):
            errors.append(
                f"仍有修改请求在 {MUTATION_DRAIN_TIMEOUT_SECONDS:g} 秒内未结束；"
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
                if _restart_handoff_payload(token) is not None:
                    RESTART_HANDOFF_FILE.unlink(missing_ok=True)
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
            _reopen_server_mutations(server)
            threading.Thread(
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
                    _write_runtime_discovery(server)
                    if not open_browser_window(server.ui_url()):
                        errors.append("无法自动打开修复页面；请重新打开 Agent Manager 访问保留的服务。")
                except Exception as exc:
                    errors.append(f"打开修复页面：{str(exc)[:300]}")
            if server.native_window_object is not None:
                try:
                    server.native_window_object.show()
                except Exception:
                    pass
            server.ensure_tray()
            _write_shutdown_status(server, blocked_phase, errors)
            server.shutdown_finished.set()
            recovered = True
        elif quick_restart:
            launch = getattr(server, "quick_restart_launch", None)
            if isinstance(launch, dict):
                launch["offlineMonotonic"] = time.monotonic()
            try:
                if isinstance(launch, dict):
                    server.quick_restart_handoff = _arm_restart_readiness_deadline(str(server.quick_restart_token or ""))
                _cleanup_runtime()
                replacement = (
                    _wait_for_restarted_manager(launch)
                    if isinstance(launch, dict)
                    else _launch_restarted_manager(str(server.quick_restart_token or ""))
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
                RESTART_HANDOFF_FILE.unlink(missing_ok=True)
                errors.append(f"重新启动管理器：{str(exc)[:300]}")
                try:
                    _cleanup_runtime()
                    acquired = _acquire_instance_mutex()
                    retry_deadline = time.monotonic() + RESTART_REPLACEMENT_EXIT_GRACE_SECONDS
                    while not acquired and time.monotonic() < retry_deadline:
                        time.sleep(RESTART_POLL_INTERVAL_SECONDS)
                        acquired = _acquire_instance_mutex()
                    if not acquired:
                        raise core.ManagerError("单实例锁已被其他管理器占用。")
                    server.runtime = ManagerRuntime(
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
                    _reopen_server_mutations(server)
                    threading.Thread(
                        target=server.serve_forever,
                        kwargs={"poll_interval": 0.25},
                        name="codex-agent-manager-restart-recovery",
                        daemon=False,
                    ).start()
                    try:
                        _write_runtime_discovery(server)
                    except Exception as runtime_exc:
                        errors.append(f"恢复运行状态文件：{str(runtime_exc)[:300]}")
                    if server.native_window_object is not None:
                        try:
                            server.native_window_object.show()
                        except Exception:
                            pass
                    server.ensure_tray()
                    _write_shutdown_status(server, "quick-restart-rolled-back", errors)
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
            _cleanup_runtime()
        if not recovered:
            _write_shutdown_status(server, "completed-with-errors" if errors else "completed", errors)
            server.shutdown_finished.set()


def _finalize_application_server(
    server: ManagerServer,
    server_thread: threading.Thread | None = None,
) -> list[str]:
    """Use the same guarded cleanup when a native/browser server loop returns."""
    started = getattr(server, "shutdown_started", None)
    aborted = getattr(server, "shutdown_aborted", None)
    if isinstance(started, threading.Event) and started.is_set():
        # Browser serve_forever returns as soon as the worker stops HTTP. It
        # must not concurrently restore files or release that worker's mutex.
        server.shutdown_finished.wait(NATIVE_WINDOW_RECOVERY_WAIT_SECONDS)
        return list(getattr(server, "shutdown_errors", []))
    if isinstance(aborted, threading.Event) and aborted.is_set():
        return list(getattr(server, "shutdown_errors", []))
    shutdown_application(
        server,
        quick_restart=bool(getattr(server, "quick_restart_requested", False)),
        exit_only=bool(getattr(server, "exit_only_requested", False)),
    )
    if server_thread is not None and server_thread is not threading.current_thread():
        server_thread.join(timeout=3)
    return list(getattr(server, "shutdown_errors", []))


def _create_startup_runtime(
    *,
    open_window: bool,
    quick_restart: bool,
    restart_handoff: dict | None,
) -> ManagerRuntime:
    """Create a runtime while preserving a visible repair path on bootstrap errors."""
    bootstrap_error: Exception | None = None
    try:
        core.ensure_state()
        import codex_config_recovery
        codex_config_recovery.normalize_encoding_if_needed()
    except Exception as exc:
        # A quick-restart child must fail before publishing readiness so the
        # still-live source manager can resume ownership of its overlay.
        if not open_window or quick_restart:
            raise
        bootstrap_error = exc
    runtime = ManagerRuntime(
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
    quick_restart = _consume_restart_handoff(quick_restart_token, delete=False)
    restart_handoff = _restart_handoff_payload(quick_restart_token) if quick_restart else None
    if restart_handoff is not None:
        try:
            restart_handoff = _mark_restart_child_staged(str(quick_restart_token or ""))
        except Exception:
            return 1
    acquired = _acquire_instance_mutex()
    if quick_restart and not acquired:
        # A prewarmed replacement deliberately reaches this point while the old
        # manager is still serving.  It waits for the parent to drain mutations
        # and release the mutex, and exits on cancellation without touching the
        # overlay or attempting to terminate either process.
        deadline = time.monotonic() + RESTART_MUTEX_WAIT_SECONDS
        while time.monotonic() < deadline and not acquired:
            if restart_handoff is not None and not _restart_handoff_is_valid(quick_restart_token):
                break
            time.sleep(RESTART_POLL_INTERVAL_SECONDS)
            acquired = _acquire_instance_mutex()
    if not acquired:
        if quick_restart:
            return 1
        if open_window and not quick_restart:
            for _ in range(EXISTING_INSTANCE_WAKE_ATTEMPTS):
                if open_existing_runtime():
                    return 0
                time.sleep(EXISTING_INSTANCE_WAKE_INTERVAL_SECONDS)
        message = (
            "检测到另一个 Agent Manager 进程占用了单实例锁，但无法验证或唤醒它。"
            "请稍候重试；若持续出现，请在任务管理器结束残留的 AgentManager 进程后重新打开。"
        )
        if open_window:
            _report_startup_error(message)
        return 1
    if restart_handoff is not None:
        restart_handoff = _take_restart_handoff(quick_restart_token)
        if restart_handoff is None:
            _release_instance_mutex()
            return 1
    runtime = None
    server = None
    try:
        # When a user-facing window is requested, do not touch Codex until the
        # window loop has actually started.  This keeps startup failures
        # recoverable and prevents a headless half-start from closing Codex.
        runtime = _create_startup_runtime(
            open_window=open_window,
            quick_restart=quick_restart,
            restart_handoff=restart_handoff,
        )
        server = ManagerServer(("127.0.0.1", port), RequestHandler, runtime)
        if hasattr(runtime, "set_radar_alert_notifier"):
            runtime.set_radar_alert_notifier(server.notify_radar_alert)
        server.allow_forced_process_exit = True
        server.native_window = bool(open_window and native_window)
        _write_runtime_discovery(server)
        if quick_restart:
            _schedule_restart_readiness_watchdog(server, restart_handoff)
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
        _cleanup_runtime()
        if open_window:
            _report_startup_error(f"管理服务初始化失败；已尝试安全清理，未完成的配置恢复记录将保留：{str(exc)[:700]}")
        raise
    ui_url = server.ui_url()

    if not open_window:
        if sys.stdout is not None:
            print(ui_url, flush=True)
        threading.Thread(target=idle_watchdog, args=(server,), daemon=True).start()
        _schedule_release_maintenance()
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            _finalize_application_server(server)
        return 1 if getattr(server, "shutdown_errors", []) else 0

    if native_window:
        server_thread = None
        try:
            import webview

            server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True)
            server_thread.start()
            _schedule_release_maintenance()
            window = webview.create_window(
                core.APP_NAME,
                ui_url,
                width=1320,
                height=860,
                min_size=(940, 640),
                text_select=True,
                background_color="#111816",
            )
            server.native_window_object = window

            def handle_closing() -> bool | None:
                return handle_native_window_closing(server, window)

            window.events.closing += handle_closing
            activation_attempted = threading.Event()

            def activate_after_window_ready() -> None:
                # pywebview runs this callback only after the GUI event loop is
                # active.  Explicitly restore the window before touching Codex;
                # a persisted minimized WebView2 state must never produce a
                # headless manager that has already closed the user's Codex.
                activation_attempted.set()
                try:
                    from native_window_theme import sync_server_appearance
                    sync_server_appearance(server)
                    if not server.show_native_window():
                        raise core.ManagerError("Agent Manager 未检测到可见窗口，已取消应用运行配置。")
                    server.window_activation_requested.clear()
                    server.runtime.activate_configuration_session()
                    _mark_ui_ready(server)
                except Exception as exc:
                    _handle_startup_activation_failure(server, "native-window-activation-error", exc)
                    return
                try:
                    if core.load_settings().get("appBehavior", {}).get("closeToTray"):
                        server.ensure_tray()
                except Exception:
                    # The window remains usable and can surface configuration
                    # errors through /api/state even if tray setup is unavailable.
                    pass

            webview.start(activate_after_window_ready, gui="edgechromium", private_mode=False)
            if not activation_attempted.is_set():
                message = "WebView2 事件循环在窗口就绪前退出，将执行安全清理与配置恢复。"
                _write_shutdown_status(server, "native-window-not-ready", [message])
                _report_startup_error(message)
                _finalize_application_server(server, server_thread)
                return 1
            if _recover_unexpected_native_window_exit(server):
                return 0
            _finalize_application_server(server, server_thread)
            return 1 if getattr(server, "shutdown_errors", []) else 0
        except Exception as exc:
            # Once the native path has started its own server loop, falling
            # through to the browser path would start serve_forever a second
            # time.  That was another way to leave a headless process behind.
            if server_thread is not None:
                if _recover_unexpected_native_window_exit(server, exc):
                    return 0
                _write_shutdown_status(server, "native-window-error", [str(exc)[:500]])
                if not bool(getattr(server, "exit_only_requested", False)):
                    _report_startup_error(f"桌面窗口启动失败，将尝试安全清理与配置恢复：{str(exc)[:700]}")
                _finalize_application_server(server, server_thread)
                return 1

    def open_browser_and_activate() -> None:
        opened = False
        try:
            opened = open_browser_window(ui_url)
            if not opened:
                raise core.ManagerError("系统未能打开 Agent Manager 浏览器窗口。")
            # Browser mode has no window-ready event.  Only activate when the
            # operating system accepted the browser launch request.
            server.runtime.activate_configuration_session()
            _mark_ui_ready(server)
        except Exception as exc:
            _handle_startup_activation_failure(server, "browser-window-activation-error", exc)

    browser_timer = threading.Timer(0.25, open_browser_and_activate)
    browser_timer.daemon = True
    browser_timer.start()
    threading.Thread(target=idle_watchdog, args=(server,), daemon=True).start()
    _schedule_release_maintenance()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        _finalize_application_server(server)
    return 1 if getattr(server, "shutdown_errors", []) else 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    raw_argv, shell_handoff = _consume_shell_job_handoff(raw_argv)
    # A normal second click only needs to reveal the already independent live
    # instance. Do that before another PyInstaller Job handoff; otherwise a
    # harmless window activation incurs a second one-file extraction and can
    # look like a failed or frozen launch. Quick restarts and explicit CLI
    # modes still take the full lifecycle path.
    if not shell_handoff and not raw_argv and open_existing_runtime():
        return 0
    try:
        if not shell_handoff and _relaunch_frozen_manager_outside_parent_job(raw_argv):
            return 0
    except Exception as exc:
        _report_startup_error(str(exc))
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--browser", action="store_true", help="Use Edge/default browser instead of WebView2.")
    parser.add_argument("--quick-restart-token", help=argparse.SUPPRESS)
    args = parser.parse_args(raw_argv)
    return run_server(
        args.port,
        not args.no_open,
        not args.browser,
        args.quick_restart_token,
    )


if __name__ == "__main__":
    raise SystemExit(main())
