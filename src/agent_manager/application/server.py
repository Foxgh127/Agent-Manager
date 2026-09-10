"""Server services."""
from __future__ import annotations
from agent_manager import application as _app


class ManagerServer(_app.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], runtime: ManagerRuntime):
        super().__init__(address, handler)
        self.runtime = runtime
        self.api_token = _app.secrets.token_urlsafe(32)
        # A second launch only needs permission to reveal the existing native
        # window.  Keep that capability separate from the full local API token
        # so app-runtime.json never grants access to account/configuration APIs.
        self.activation_token = _app.secrets.token_urlsafe(32)
        # Local maintenance tools need one safe operation without receiving the
        # full browser API token. This token can only request a manager-only
        # quick restart; it cannot read accounts, secrets, or modify settings.
        self.control_token = _app.secrets.token_urlsafe(32)
        self.runtime_nonce = _app.secrets.token_urlsafe(24)
        self.started_at = _app.core.now_iso()
        self.static_dir = _app.STATIC_DIR.resolve()
        self.last_request_at = _app.time.time()
        self.native_window = False
        self.native_window_object = None
        # HTTP readiness is not UI readiness.  Quick restart must keep the old
        # manager alive until the replacement has a visible window and has
        # successfully activated the preserved configuration session.
        self.ui_ready = _app.threading.Event()
        # A second launch can arrive after the local server is healthy but
        # before WebView2 has created the HWND. Treat that as a queued reveal,
        # not as a failed launch followed by an apparently crashing process.
        self.window_activation_requested = _app.threading.Event()
        self.tray = None
        self.tray_thread = None
        self.tray_error = None
        self.tray_lock = _app.threading.RLock()
        self.force_exit = False
        self.quick_restart_requested = False
        self.quick_restart_token = None
        self.quick_restart_launch: dict | None = None
        self.quick_restart_handoff: dict | None = None
        self.quick_restart_timing: dict = {}
        self.exit_only_requested = False
        self.shutdown_lock = _app.threading.RLock()
        self.shutdown_started = _app.threading.Event()
        self.shutdown_finished = _app.threading.Event()
        self.shutdown_aborted = _app.threading.Event()
        self.allow_forced_process_exit = False
        self.mutation_condition = _app.threading.Condition(_app.threading.RLock())
        self.mutations_open = True
        self.inflight_mutations = 0
        self._thread_slots = _app.threading.BoundedSemaphore(_app.MAX_MANAGEMENT_THREADS)
        self.ui_bootstrap_lock = _app.threading.RLock()
        self.ui_bootstrap_tokens: dict[str, tuple[float, int]] = {}

    def server_bind(self) -> None:
        _app._bind_exclusive_loopback(self)

    def ui_url(self) -> str:
        port = int(self.server_address[1])
        now = _app.time.monotonic()
        token = _app.secrets.token_urlsafe(32)
        with self.ui_bootstrap_lock:
            self.ui_bootstrap_tokens = {
                candidate: record
                for candidate, record in self.ui_bootstrap_tokens.items()
                if record[0] > now and record[1] < _app.UI_BOOTSTRAP_MAX_EXCHANGES
            }
            self.ui_bootstrap_tokens[token] = (now + _app.UI_BOOTSTRAP_TOKEN_TTL_SECONDS, 0)
        return f"http://127.0.0.1:{port}/#bootstrap={_app.urllib.parse.quote(token, safe='')}"

    def consume_ui_bootstrap(self, token: str) -> bool:
        now = _app.time.monotonic()
        supplied = str(token or "")
        with self.ui_bootstrap_lock:
            record = self.ui_bootstrap_tokens.get(supplied)
            self.ui_bootstrap_tokens = {
                candidate: candidate_record
                for candidate, candidate_record in self.ui_bootstrap_tokens.items()
                if candidate_record[0] > now
                and candidate_record[1] < _app.UI_BOOTSTRAP_MAX_EXCHANGES
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
                body = _app.json.dumps(
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
                request.settimeout(_app.MANAGEMENT_CLIENT_TIMEOUT_SECONDS)
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

    def wait_for_mutations(self, timeout_seconds: float = _app.MUTATION_DRAIN_TIMEOUT_SECONDS) -> bool:
        deadline = _app.time.monotonic() + max(0.0, float(timeout_seconds))
        with self.mutation_condition:
            while self.inflight_mutations:
                remaining = deadline - _app.time.monotonic()
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
        for attempt in range(20 if _app.os.name == "nt" else 1):
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
            if _app.os.name != "nt" or _app.focus_process_window(_app.os.getpid()):
                return shown
            if attempt < 19:
                _app.time.sleep(0.1)
        return False

    def tray_status(self) -> dict:
        return {
            "available": bool(self.native_window),
            "ready": bool(self.tray is not None and not self.tray_error and self.tray.visible),
            "error": self.tray_error,
        }

    def notify_radar_alert(self, alert: dict) -> bool:
        """Deliver a system notification with or without a permanent tray."""
        from agent_manager.platform.notifications import radar_notification_text, notify_windows
        content = radar_notification_text(alert)
        if content is None or self.shutdown_started.is_set():
            return False
        title, message = content
        if self.tray is not None and getattr(self.tray, "visible", False) and not self.tray_error:
            try:
                self.tray.notify(message, title)
                return True
            except Exception as exc:
                self.tray_error = str(exc)[:300]
        return notify_windows(title, message)

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

                icon_path = _app.STATIC_DIR / "app-icon.png"
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
                    _app.request_application_shutdown(self)

                menu = pystray.Menu(
                    pystray.MenuItem("显示 Agent Manager", show_window, default=True),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("彻底退出", exit_application),
                )
                tray = pystray.Icon("codex-agent-manager", image, _app.core.APP_NAME, menu)
                ready = _app.threading.Event()

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
                tray_thread = _app.threading.Thread(
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
        if tray_thread is not None and tray_thread is not _app.threading.current_thread():
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

