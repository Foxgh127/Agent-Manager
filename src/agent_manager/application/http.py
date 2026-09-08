"""Http services."""
from __future__ import annotations
from agent_manager import application as _app


class RequestHandler(_app.BaseHTTPRequestHandler):
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
        data = _app.json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(data) > _app.MAX_RESPONSE_BYTES:
            status = 413
            data = _app.json.dumps(
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
            if not _app.re.fullmatch(r"[0-9]+", raw_length):
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
            self.server.runtime.last_error = _app.core._redact_sensitive_text(exc, limit=500)
        except Exception:
            pass
        self._error("Agent Manager 处理请求时发生内部错误，请重试或运行诊断。", 500)

    def _authorized(self) -> bool:
        return _app.secrets.compare_digest(self.headers.get("X-Agent-Manager-Token", ""), self.server.api_token)

    def _request_host_is_valid(self) -> bool:
        supplied = str(self.headers.get("Host") or "").strip()
        if not supplied or any(character in supplied for character in "\r\n/\\"):
            return False
        try:
            parsed = _app.urllib.parse.urlsplit(f"//{supplied}")
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
        if content_lengths and not _app.re.fullmatch(r"[0-9]+", content_lengths[0]):
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
            parsed = _app.urllib.parse.urlsplit(supplied)
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
        path = _app.urlparse(self.path).path
        if path in {
            "/api/accounts/import",
            "/api/accounts/import-preview",
            "/api/accounts/import-batch",
        }:
            return min(_app.MAX_BODY_BYTES, _app.MAX_IMPORT_BODY_BYTES)
        if path == "/api/codex-config":
            return min(_app.MAX_BODY_BYTES, _app.MAX_CONFIG_BODY_BYTES)
        return min(_app.MAX_BODY_BYTES, _app.DEFAULT_JSON_BODY_BYTES)

    def _read_json(self, optional: bool = False) -> dict:
        if not self._request_framing_is_valid():
            self._request_body_handled = True
            raise _app.core.ManagerError("请求 HTTP framing 无效。")
        content_lengths = self._content_length_values()
        raw_length = content_lengths[0] if content_lengths else "0"
        try:
            length = int(raw_length, 10)
        except (TypeError, ValueError) as exc:
            self._request_body_handled = True
            raise _app.core.ManagerError("请求长度无效。") from exc
        if optional and length == 0:
            self._request_body_handled = True
            return {}
        if length <= 0 or length > self._request_body_limit():
            self._request_body_handled = True
            if length > self._request_body_limit():
                self.close_connection = True
            raise _app.core.ManagerError("请求内容为空或超过当前接口的安全限制。")
        content_type = str(self.headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
        if content_type != "application/json" and not content_type.endswith("+json"):
            self._request_body_handled = True
            raise _app.core.ManagerError("JSON 请求必须使用 Content-Type: application/json。")
        try:
            raw = self.rfile.read(length)
            self._request_body_handled = True
            if len(raw) != length:
                self.close_connection = True
                raise _app.core.ManagerError("请求内容未完整接收。")
            payload = _app.json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, _app.json.JSONDecodeError) as exc:
            raise _app.core.ManagerError("请求 JSON 无效。") from exc
        except _app.core.ManagerError:
            raise
        except (OSError, TimeoutError):
            self._request_body_handled = True
            self.close_connection = True
            raise
        if not isinstance(payload, dict):
            raise _app.core.ManagerError("请求必须是 JSON 对象。")
        return payload

    def _serve_static(self, request_path: str) -> None:
        if not self.server.static_dir.exists():
            self._error("GUI 尚未构建，请先运行 npm run build。", 503)
            return
        relative = "index.html" if request_path in {"", "/"} else _app.unquote(request_path.lstrip("/"))
        candidate = (self.server.static_dir / relative).resolve()
        try:
            candidate.relative_to(self.server.static_dir)
        except ValueError:
            self._error("无效的静态文件路径。", 403)
            return
        if not candidate.is_file():
            if "." not in _app.Path(relative).name:
                candidate = self.server.static_dir / "index.html"
            else:
                self._error("文件不存在。", 404)
                return
        data = candidate.read_bytes()
        content_type = _app.mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        cache = "public, max-age=31536000, immutable" if candidate.name != "index.html" else "no-store"
        self._headers(content_type, len(data), 200, cache)
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.server.last_request_at = _app.time.time()
        if not self._validate_local_request():
            return
        parsed_request = _app.urlparse(self.path)
        path = parsed_request.path
        query = _app.urllib.parse.parse_qs(parsed_request.query, keep_blank_values=True)
        if path == "/api/health":
            from agent_manager.platform.dlls import runtime_status as dll_runtime_status
            self._json(
                {
                    "ok": True,
                    "app": _app.core.APP_NAME,
                    "appId": _app.RUNTIME_APP_ID,
                    "runtimePid": _app.os.getpid(),
                    "runtimeNonce": self.server.runtime_nonce,
                    "activationTokenHash": _app._activation_token_hash(self.server.activation_token),
                    "controlTokenHash": _app._activation_token_hash(self.server.control_token),
                    "uiReady": self.server.ui_ready.is_set(),
                    "independentLifecycle": _app._manager_lifecycle_is_independent(),
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
                            "pid": _app.os.getpid(),
                            "uiReady": self.server.ui_ready.is_set(),
                            "independentLifecycle": _app._manager_lifecycle_is_independent(),
                            "configurationSession": self.server.runtime.configuration_snapshot(),
                            "trayStatus": self.server.tray_status(),
                            "shutdown": _app._read_shutdown_status(),
                        }
                    )
                    return
                if path == "/api/accounts/snapshot":
                    self._json({"ok": True, **self.server.runtime.account_snapshot()})
                    return
                if path == "/api/connections":
                    connections = _app.core.public_connections_state()
                    sampler = getattr(self.server.runtime, "quota_estimates", None)
                    if sampler:
                        sampler.decorate(connections.get("settings", {}).get("accounts", []))
                    self._json({"ok": True, **connections})
                    return
                if path == "/api/recovery":
                    self._json({"ok": True, **_app.recovery.list_points()})
                    return
                if path == "/api/history/recoveries":
                    self._json({"ok": True, **_app.history_sync.list_recoveries()})
                    return
                if path == "/api/sessions/visibility":
                    import agent_manager.sessions.visibility
                    mode = str((query.get("mode") or ["quick"])[0])
                    self._json({"ok": True, "inspection": agent_manager.sessions.visibility.inspect(mode=mode), **agent_manager.sessions.visibility.list_backups()})
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
                    self._json({"ok": True, "runtime": _app.core.codex_runtime_status(force=True)})
                    return
                if path == "/api/codex-config":
                    self._json({"ok": True, "document": _app.core.codex_config_document()})
                    return
                if path == "/api/codex-config/recovery":
                    import agent_manager.config.recovery
                    self._json({"ok": True, "inspection": agent_manager.config.recovery.inspect_recovery()})
                    return
                if path == "/api/preview":
                    self._json({"ok": True, **_app.core.preview_apply()})
                    return
                if path == "/api/export":
                    self._json({"ok": True, "bundle": _app.core.export_bundle()})
                    return
                if path == "/api/history":
                    self._json({"ok": True, **_app.core.history_state(include_threads=True)})
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
                            **_app.maintenance.list_skills(
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
                            **_app.maintenance.public_skill_catalog(
                                force=str((query.get("force") or [""])[0]).casefold() in {"1", "true", "yes"}
                            ),
                        }
                    )
                    return
                if path == "/api/updates":
                    self._json(
                        {
                            "ok": True,
                            "status": _app.maintenance.update_center_status(
                                force=str((query.get("force") or [""])[0]).casefold() in {"1", "true", "yes"}
                            ),
                        }
                    )
                    return
                if path == "/api/emergency/checks":
                    self._json(
                        {
                            "ok": True,
                            **_app.maintenance.run_emergency_checks(
                                force=str((query.get("force") or [""])[0]).casefold() in {"1", "true", "yes"}
                            ),
                        }
                    )
                    return
                if path == "/api/claude/profiles":
                    self._json({"ok": True, **_app.claude.list_profiles()})
                    return
                if path == "/api/claude/import-preview":
                    self._json({"ok": True, "preview": _app.claude.preview_claude_code_import()})
                    return
                if path == "/api/usage":
                    usage = self.server.runtime.web2api.usage_snapshot()
                    backfill = ((usage.get("codexSessions") or {}).get("coverage") or {}).get("backfill") or {}
                    if backfill.get("pendingFiles") and not getattr(self.server.runtime, "_closed", False) and not getattr(self.server.runtime, "_restart_prepared", False):
                        import agent_manager.gateway.service
                        agent_manager.gateway.service.request_codex_session_usage_backfill()
                    self._json({"ok": True, "usage": usage})
                    return
                if path == "/api/app-update":
                    self._json({"ok": True, "status": self.server.runtime.get_app_updates().status()})
                    return
                if path == "/api/radar/reset-status":
                    accounts = _app.core.load_settings().get("accounts", [])
                    refreshed = self.server.runtime.radar.get_reset_radar(accounts, refresh=False)
                    self._json({"ok": True, "reset": _app._radar_api_section(refreshed)})
                    return
                if path == "/api/web2api/key":
                    key = _app.core.load_service_secret("web2api", required=True)
                    if key == _app.core.load_service_secret("gateway_internal"):
                        raise _app.core.ManagerError("当前公开 Key 需要先轮换，不能复制内部路由凭据。")
                    self._json({"ok": True, "apiKey": key})
                    return
                if path == "/api/radar":
                    accounts = _app.core.load_settings().get("accounts", [])
                    snapshot = self.server.runtime.radar.get_snapshot(
                        accounts if isinstance(accounts, list) else [],
                        startup=True,
                    )
                    self._json(
                        {
                            "ok": True,
                            "schemaVersion": snapshot.get("schemaVersion", 1),
                            "intelligence": _app._radar_api_section(snapshot.get("intelligence")),
                            "quota": _app._radar_api_section(snapshot.get("quota")),
                            "reset": _app._radar_api_section(snapshot.get("reset")),
                            "sources": snapshot.get("sources", []),
                        }
                    )
                    return
                account_export = _app.re.fullmatch(r"/api/accounts/([^/]+)/export", path)
                if account_export:
                    self._json({"ok": True, "export": _app.core.export_codex_account(_app.unquote(account_export.group(1)))})
                    return
                provider_export = _app.re.fullmatch(r"/api/providers/([^/]+)/export", path)
                if provider_export:
                    self._json({"ok": True, "export": _app.core.export_api_provider(_app.unquote(provider_export.group(1)))})
                    return
                relay_export = _app.re.fullmatch(r"/api/relay-accounts/([^/]+)/export", path)
                if relay_export:
                    self._json(
                        {
                            "ok": True,
                            "export": _app.core.export_relay_account(
                                _app.unquote(relay_export.group(1))
                            ),
                        }
                    )
                    return
                self._error("API 路径不存在。", 404)
            except (_app.core.ManagerError, _app.radar.RadarError, _app.toolbox.ToolboxError, _app.app_updates.UpdateError) as exc:
                self._error(str(exc), 400)
            except Exception as exc:
                self._internal_error(exc)
            return
        self._serve_static(path)

    def do_POST(self) -> None:
        self.server.last_request_at = _app.time.time()
        if not self._validate_local_request(write=True):
            return
        path = _app.urlparse(self.path).path
        if path == "/api/window/show":
            supplied = self.headers.get("X-Agent-Manager-Activation", "")
            if not _app.secrets.compare_digest(supplied, self.server.activation_token):
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
                opened = _app.open_browser_window(self.server.ui_url())
            self._json({"ok": True, "shown": shown, "opened": opened, "queued": queued})
            return
        if path == _app.CONTROL_QUICK_RESTART_PATH:
            supplied = self.headers.get(_app.CONTROL_HEADER_NAME, "")
            if not _app.secrets.compare_digest(supplied, self.server.control_token):
                self._error("本机控制请求无效。", 403)
                return
            try:
                self._read_json(optional=True)
                if not _app.request_application_restart(self.server):
                    raise _app.core.ManagerError("Agent Manager 已在退出或重启中。")
            except _app.core.ManagerError as exc:
                self._error(str(exc), 400)
                return
            self._json(
                {
                    "ok": True,
                    "message": "管理器正在快速重启；Codex 将保持运行。",
                    "sourcePid": _app.os.getpid(),
                    "requestedAt": _app.core.now_iso(),
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
                        raise _app.core.ManagerError("请提供更新源，或使用 null 恢复内置更新源。")
                    status = service.configure(payload["source"])
                elif path == "/api/app-update/check":
                    status = service.check()
                elif path == "/api/app-update/download":
                    auto_install = bool(payload.get("installAfterDownload") and service.install_supported)
                    status = service.start_download(payload.get("releaseToken"),
                        on_ready=(lambda: _app.prepare_application_update(self.server)) if auto_install else None)
                elif path == "/api/app-update/install":
                    result = _app.prepare_application_update(self.server)
                    self._json({"ok": True, "result": result, "message": "更新已校验；正在关闭 Codex、恢复配置并重启管理器。"})
                    return
                elif path == "/api/app-update/cancel":
                    status = service.cancel_download()
                elif path == "/api/app-update/open-folder":
                    downloaded = service.verified_download_path()
                    _app.os.startfile(str(downloaded.parent))
                    status = service.status()
                else:
                    raise _app.core.ManagerError("更新操作不存在。")
                self._json({"ok": True, "status": status})
                return
            if path == "/api/window/appearance":
                from agent_manager.platform.window_theme import sync_server_appearance
                try:
                    result = sync_server_appearance(self.server, self._read_json())
                except ValueError as exc:
                    raise _app.core.ManagerError(str(exc)) from exc
                self._json({"ok": True, **result})
                return
            if path == "/api/apply":
                payload = self._read_json(optional=True)
                result = _app.core.apply_configuration(bool(payload.get("syncSecrets")))
                if result.get("gatewayRequired") and not self.server.runtime.web2api.status()["running"]:
                    self.server.runtime.web2api.start()
                diagnostics = _app.maintenance.run_emergency_checks(force=False)
                self._json(
                    {
                        "ok": True,
                        "result": result,
                        "status": _app.maintenance.merge_configuration_diagnostics(
                            _app.core.configuration_status(), diagnostics
                        ),
                    }
                )
                return
            if path == "/api/apply-and-launch":
                payload = self._read_json(optional=True)
                result = _app.core.apply_configuration(bool(payload.get("syncSecrets")))
                if result.get("gatewayRequired") and not self.server.runtime.web2api.status()["running"]:
                    self.server.runtime.web2api.start()
                env_overrides = {}
                if payload.get("syncSecrets"):
                    for provider in _app.core.load_settings()["providers"]:
                        if provider.get("kind") == "custom" and _app.core.provider_key_configured(provider["id"]):
                            env_overrides[provider["envKey"]] = _app.core.load_provider_key(provider["id"]) or ""
                launch_plan = _app.core.resolve_codex_launch_plan()
                closed = None
                try:
                    closed = _app._close_codex_processes_safely()
                    launch = _app.core.launch_codex_app(launch_plan=launch_plan, env_overrides=env_overrides)
                except Exception as exc:
                    recovery_error = None
                    if closed is not None and not _app.core.running_codex_processes():
                        try:
                            _app.core.launch_codex_app(launch_plan=launch_plan)
                        except Exception as recovery_exc:
                            recovery_error = str(recovery_exc)[:300]
                    detail = f"；自动恢复也失败：{recovery_error}" if recovery_error else ""
                    raise _app.core.ManagerError(f"应用配置后重新打开 Codex 失败：{exc}{detail}") from exc
                self._json({"ok": True, "result": result, "closed": closed, "launch": launch})
                return
            if path == "/api/codex/restart":
                self._read_json(optional=True)
                # Resolve a valid, dynamic Desktop/CLI launch target before
                # closing anything. This preserves a working Codex instance if
                # the installed Store package moved or the runtime is missing.
                launch_plan = _app.core.resolve_codex_launch_plan()
                closed = None
                try:
                    closed = _app._close_codex_processes_safely()
                    launch = _app.core.launch_codex_app(launch_plan=launch_plan)
                except Exception as exc:
                    recovery_error = None
                    if closed is not None and not _app.core.running_codex_processes():
                        try:
                            _app.core.launch_codex_app(launch_plan=launch_plan)
                        except Exception as recovery_exc:
                            recovery_error = str(recovery_exc)[:300]
                    detail = f"；恢复启动也失败：{recovery_error}" if recovery_error else ""
                    raise _app.core.ManagerError(f"重启 Codex 失败：{exc}{detail}") from exc
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
                record = _app.core.save_provider(
                    payload,
                    payload.get("originalId"),
                    api_key=key or None,
                )
                self._json({"ok": True, "provider": record})
                return
            if path == "/api/accounts/capture":
                self._json({"ok": True, "account": _app.core.save_codex_account(self._read_json())})
                return
            if path == "/api/accounts/import":
                self._json({"ok": True, "account": _app.core.import_codex_account(self._read_json())})
                return
            if path == "/api/accounts/import-preview":
                payload = self._read_json()
                payload.setdefault("validateRemote", True)
                self._json({"ok": True, "preview": _app.core.preview_codex_accounts_batch(payload)})
                return
            if path == "/api/accounts/import-batch":
                payload = self._read_json()
                payload["deferRefresh"] = True
                result = _app.core.import_codex_accounts_batch(payload)
                refresh_status = self.server.runtime.schedule_account_refresh(
                    result.get("refreshAccountIds") if isinstance(result.get("refreshAccountIds"), list) else []
                )
                self._json({"ok": True, "result": result, "refreshStatus": refresh_status})
                return
            if path == "/api/model-sources/delete-batch":
                payload = self._read_json()
                result, point = _app.agent_manager.storage.coordinator.before_account_delete(self.server.runtime, "批量删除账号前", lambda: _app.core.remove_model_sources_batch(
                    payload.get("accountIds") if isinstance(payload.get("accountIds"), list) else [],
                    payload.get("providerIds") if isinstance(payload.get("providerIds"), list) else [],
                ))
                result["backup"] = point
                if result.get("deleted"):
                    try:
                        applied = _app.core.apply_configuration(False)
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
                self._json({"ok": True, "group": _app.core.save_account_group(self._read_json())})
                return
            assign_group = _app.re.fullmatch(r"/api/account-groups/([^/]+)/assign", path)
            if assign_group:
                payload = self._read_json()
                account_ids = payload.get("accountIds", [])
                provider_ids = payload.get("providerIds", [])
                if not isinstance(account_ids, list) or not isinstance(provider_ids, list):
                    raise _app.core.ManagerError("accountIds 和 providerIds 必须是数组。")
                result = _app.core.assign_sources_to_group(
                    _app.unquote(assign_group.group(1)), account_ids, provider_ids
                )
                self._json({"ok": True, "result": result})
                return
            if path == "/api/accounts/proxy-batch":
                payload = self._read_json()
                result = _app.core.set_accounts_proxy_enabled_batch(
                    payload.get("accountIds") if isinstance(payload.get("accountIds"), list) else [],
                    bool(payload.get("enabled")),
                )
                if not payload.get("enabled"):
                    self.server.runtime.web2api.clear_account_runtime_state(payload.get("accountIds", []))
                self._json({"ok": True, "result": result})
                return
            if path == "/api/providers/proxy-batch":
                payload = self._read_json()
                result = _app.core.set_providers_proxy_enabled_batch(
                    payload.get("providerIds") if isinstance(payload.get("providerIds"), list) else [],
                    bool(payload.get("enabled")),
                )
                self._json({"ok": True, "result": result})
                return
            account_proxy = _app.re.fullmatch(r"/api/accounts/([^/]+)/proxy", path)
            if account_proxy:
                payload = self._read_json()
                account = _app.core.set_account_proxy_enabled(
                    _app.unquote(account_proxy.group(1)), bool(payload.get("enabled"))
                )
                if not payload.get("enabled"):
                    self.server.runtime.web2api.clear_account_runtime_state([_app.unquote(account_proxy.group(1))])
                self._json({"ok": True, "account": account})
                return
            provider_proxy = _app.re.fullmatch(r"/api/providers/([^/]+)/proxy", path)
            if provider_proxy:
                payload = self._read_json()
                provider = _app.core.set_provider_proxy_enabled(
                    _app.unquote(provider_proxy.group(1)), bool(payload.get("enabled"))
                )
                self._json({"ok": True, "provider": provider})
                return
            if path == "/api/web2api/config":
                payload = self._read_json()
                was_running = self.server.runtime.web2api.status()["running"]
                previous = _app.json.loads(_app.json.dumps(_app.core.load_settings().get("web2api", _app.core._default_web2api_settings())))
                config = _app.core.save_web2api_settings(payload)
                restart_required = was_running and int(previous.get("port", 17860)) != int(config.get("port", 17860))
                if restart_required:
                    self.server.runtime.web2api.stop(disable=False)
                    try:
                        status = self.server.runtime.web2api.start()
                    except Exception:
                        settings = _app.core.load_settings()
                        settings["web2api"] = previous
                        _app.core.save_settings(settings)
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
                allowed = set(_app.core.load_settings().get("web2api", {}).get("accountIds", []))
                if not isinstance(selected, list) or not selected or len(selected) > 4096 or any(not isinstance(value, str) or value not in allowed for value in selected):
                    raise _app.core.ManagerError("请选择当前 API 号池中的账号。")
                result = self.server.runtime.web2api.clear_account_runtime_state(selected)
                self._json({"ok": True, "result": result, "status": self.server.runtime.web2api.status()})
                return
            if path == "/api/web2api/start":
                self._json({"ok": True, **_app.activate_web2api_for_codex(self.server.runtime)})
                return
            if path == "/api/web2api/stop":
                self._json({"ok": True, **_app.deactivate_web2api_for_codex(self.server.runtime)})
                return
            if path == "/api/web2api/service-start":
                self._read_json(optional=True)
                if not _app.core.service_secret_configured("web2api"):
                    _app.core.rotate_web2api_key()
                self._json({"ok": True, "status": self.server.runtime.web2api.start()})
                return
            if path == "/api/web2api/service-stop":
                self._read_json(optional=True)
                if _app.core.load_settings().get("web2api", {}).get("activeForCodex"):
                    raise _app.core.ManagerError("Codex 正在使用本地反代，请先选择“停止并恢复直连”。")
                self._json({"ok": True, "status": self.server.runtime.web2api.stop()})
                return
            if path == "/api/web2api/rotate-key":
                self._json({"ok": True, **_app.rotate_web2api_key_for_runtime(self.server.runtime)})
                return
            if path == "/api/oauth/start":
                self._json({"ok": True, "status": self.server.runtime.oauth.start(self._read_json())})
                return
            reauth_account = _app.re.fullmatch(r"/api/accounts/([^/]+)/reauth", path)
            if reauth_account:
                self._read_json(optional=True)
                self._json({"ok": True, "status": self.server.runtime.oauth.start({"reauthAccountId":_app.unquote(reauth_account.group(1))})})
                return
            if path in {"/api/oauth/open", "/api/oauth/callback", "/api/oauth/cancel"}:
                oauth_payload = self._read_json(optional=True)
                expected_login = oauth_payload.get("loginId")
                if expected_login and expected_login != self.server.runtime.oauth.state().get("loginId"):
                    raise _app.core.ManagerError("认证会话已改变，未操作其他登录会话。")
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
                    raise _app.core.ManagerError("accountId 必须是字符串。")
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
                    raise _app.core.ManagerError("force 必须是布尔值。")
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
            relay_selection = _app.re.fullmatch(r"/api/relay-accounts/([^/]+)/selection", path)
            if relay_selection:
                result = _app.core.update_relay_account_selection(
                    _app.unquote(relay_selection.group(1)),
                    self._read_json(),
                )
                self._json({"ok": True, "result": result})
                return
            relay_group = _app.re.fullmatch(r"/api/relay-accounts/([^/]+)/group", path)
            if relay_group:
                payload = self._read_json()
                result = _app.core.move_relay_account_group(
                    _app.unquote(relay_group.group(1)),
                    payload.get("groupId"),
                )
                self._json({"ok": True, "result": result})
                return
            relay_refresh = _app.re.fullmatch(r"/api/relay-accounts/([^/]+)/refresh", path)
            if relay_refresh:
                payload = self._read_json(optional=True)
                full = payload.get("full", True)
                if not isinstance(full, bool):
                    raise _app.core.ManagerError("完整同步选项必须为布尔值。")
                result = self.server.runtime.relay_portal.refresh_account(
                    _app.unquote(relay_refresh.group(1)), full=full
                )
                self._json({"ok": True, "result": result})
                return
            relay_metadata = _app.re.fullmatch(r"/api/relay-accounts/([^/]+)/metadata", path)
            if relay_metadata:
                result = _app.core.update_relay_account_metadata(
                    _app.unquote(relay_metadata.group(1)),
                    self._read_json(),
                )
                self._json({"ok": True, "result": result})
                return
            relay_create_key = _app.re.fullmatch(r"/api/relay-accounts/([^/]+)/keys", path)
            if relay_create_key:
                result = self.server.runtime.relay_portal.create_saved_key(
                    _app.unquote(relay_create_key.group(1)),
                    self._read_json(),
                )
                self._json({"ok": True, "result": result})
                return
            relay_key_group = _app.re.fullmatch(
                r"/api/relay-accounts/([^/]+)/keys/([^/]+)/group",
                path,
            )
            if relay_key_group:
                payload = self._read_json()
                result = self.server.runtime.relay_portal.update_saved_key_group(
                    _app.unquote(relay_key_group.group(1)),
                    _app.unquote(relay_key_group.group(2)),
                    payload.get("groupId"),
                )
                self._json({"ok": True, "result": result})
                return
            if path == "/api/toolbox/totp/generate":
                payload = self._read_json()
                source = str(payload.get("input") or "")
                label = str(payload.get("label") or "")
                if payload.get("save"):
                    generated = _app.toolbox.save_totp_item(source, label=label)
                else:
                    generated = _app.toolbox.generate_totp(source)
                    if label.strip():
                        generated["label"] = label.strip()
                self._json({"ok": True, "result": _app._totp_api_item(generated)})
                return
            if path == "/api/toolbox/mail/preview":
                payload = self._read_json()
                preview = _app.toolbox.preview_mail_import_items(str(payload.get("text") or ""))
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
                    raise _app.toolbox.ValidationError("selectedIndices 必须是数组。")
                imported = _app.toolbox.save_mail_import_selection(
                    str(payload.get("text") or ""),
                    selected_indices,
                )
                self._json(
                    {
                        "ok": True,
                        "imported": len(imported),
                        "mailAccounts": [_app._mail_account_api_item(item) for item in imported],
                    }
                )
                return
            if path == "/api/toolbox/mail/temporary/messages":
                payload = self._read_json()
                try:
                    limit = int(payload.get("limit") or 10)
                except (TypeError, ValueError) as exc:
                    raise _app.toolbox.ValidationError("邮件数量必须是整数。") from exc
                messages = self.server.runtime.toolbox.fetch_temporary_mail(
                    str(payload.get("text") or ""),
                    limit=limit,
                    unread_only=bool(payload.get("unreadOnly")),
                )
                self._json({"ok": True, "messages": messages})
                return
            mailbox_messages = _app.re.fullmatch(r"/api/toolbox/mail/([^/]+)/messages", path)
            if mailbox_messages:
                payload = self._read_json(optional=True)
                try:
                    limit = int(payload.get("limit") or 20)
                except (TypeError, ValueError) as exc:
                    raise _app.toolbox.ValidationError("邮件数量必须是整数。") from exc
                messages = self.server.runtime.toolbox.fetch_mail(
                    _app.unquote(mailbox_messages.group(1)),
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
                        "result": _app.core.refresh_all_codex_accounts(stale_only=bool(payload.get("staleOnly"))),
                    }
                )
                return
            if path == "/api/accounts/delete-invalid":
                payload = self._read_json(optional=True)
                result, point = _app.agent_manager.storage.coordinator.before_account_delete(self.server.runtime, "清理失效账号前", lambda: _app.core.delete_invalid_accounts(str(payload.get("groupId") or "all")))
                result["backup"] = point
                if result.get("deleted"):
                    try:
                        applied = _app.core.apply_configuration(False)
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
            account_export_download = _app.re.fullmatch(r"/api/accounts/([^/]+)/export-download", path)
            if account_export_download:
                self._read_json(optional=True)
                result = _app.core.export_codex_account_to_downloads(_app.unquote(account_export_download.group(1)))
                self._json({"ok": True, "result": result})
                return
            provider_export_download = _app.re.fullmatch(r"/api/providers/([^/]+)/export-download", path)
            if provider_export_download:
                self._read_json(optional=True)
                result = _app.core.export_api_provider_to_downloads(_app.unquote(provider_export_download.group(1)))
                self._json({"ok": True, "result": result})
                return
            relay_export_download = _app.re.fullmatch(
                r"/api/relay-accounts/([^/]+)/export-download",
                path,
            )
            if relay_export_download:
                self._read_json(optional=True)
                result = _app.core.export_relay_account_to_downloads(
                    _app.unquote(relay_export_download.group(1))
                )
                self._json({"ok": True, "result": result})
                return
            refresh_account = _app.re.fullmatch(r"/api/accounts/([^/]+)/refresh", path)
            if refresh_account:
                # A user-initiated refresh must bypass the metadata TTL.  New
                # Codex models can roll out between background refreshes and
                # otherwise remain hidden even though the account already has
                # access to them.
                account = _app.core.refresh_codex_account(
                    _app.unquote(refresh_account.group(1)),
                    force_metadata=True,
                )
                sampler = getattr(self.server.runtime, "quota_estimates", None)
                if sampler:
                    sampler.decorate([account])
                self._json({"ok": True, "account": account})
                return
            reset_details = _app.re.fullmatch(r"/api/accounts/([^/]+)/reset-credit/details", path)
            if reset_details:
                payload = self._read_json(optional=True)
                account = _app.core.refresh_account_reset_credit_details(
                    _app.unquote(reset_details.group(1)),
                    force=bool(payload.get("force")),
                )
                self._json({"ok": True, "account": account})
                return
            consume_reset = _app.re.fullmatch(r"/api/accounts/([^/]+)/reset-credit/consume", path)
            if consume_reset:
                result = _app.core.consume_account_reset_credit(_app.unquote(consume_reset.group(1)))
                self._json({"ok": True, "result": result})
                return
            switch_account = _app.re.fullmatch(r"/api/accounts/([^/]+)/switch", path)
            if switch_account:
                payload = self._read_json(optional=True)
                account_id = _app.unquote(switch_account.group(1))
                settings = _app.core.load_settings()
                account = next(
                    (item for item in settings.get("accounts", []) if str(item.get("id")) == account_id),
                    None,
                )
                if not account:
                    raise _app.core.ManagerError("账号不存在。")
                operation_id = str(payload.get("operationId") or _app.secrets.token_urlsafe(12))
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
                        result = _app.activate_web2api_for_codex(self.server.runtime, account_id)
                    else:
                        result = _app.core.switch_codex_account_and_launch(
                            account_id,
                            progress_callback=lambda update: self.server.runtime.update_switch_operation(
                                operation_id,
                                update,
                            ),
                            close_processes_callback=_app._close_codex_processes_safely,
                            ensure_gateway=lambda: _app._ensure_runtime_gateway(self.server.runtime),
                            force_reapply=payload.get("forceReapply") is True,
                        )
                except Exception as exc:
                    self.server.runtime.finish_switch_operation(operation_id, error=exc)
                    raise
                self.server.runtime.finish_switch_operation(operation_id, result=result)
                self._json({"ok": True, "result": result})
                return
            account_metadata = _app.re.fullmatch(r"/api/accounts/([^/]+)/metadata", path)
            if account_metadata:
                account = _app.core.update_codex_account_metadata(
                    _app.unquote(account_metadata.group(1)),
                    self._read_json(),
                )
                self._json({"ok": True, "account": account})
                return
            switch_provider = _app.re.fullmatch(r"/api/providers/([^/]+)/switch", path)
            if switch_provider:
                payload = self._read_json(optional=True)
                provider_id = _app.unquote(switch_provider.group(1))
                provider = _app.core.provider_by_id(provider_id)
                operation_id = str(payload.get("operationId") or _app.secrets.token_urlsafe(12))
                self.server.runtime.begin_switch_operation(
                    operation_id,
                    target_id=provider_id,
                    target_name=str(provider.get("name") or provider_id),
                    target_kind="provider",
                )
                try:
                    result = _app.core.switch_api_provider_and_launch(
                        provider_id,
                        progress_callback=lambda update: self.server.runtime.update_switch_operation(
                            operation_id,
                            update,
                        ),
                        close_processes_callback=_app._close_codex_processes_safely,
                        ensure_gateway=lambda: _app._ensure_runtime_gateway(self.server.runtime),
                        force_reapply=payload.get("forceReapply") is True,
                    )
                except Exception as exc:
                    self.server.runtime.finish_switch_operation(operation_id, error=exc)
                    raise
                self.server.runtime.finish_switch_operation(operation_id, result=result)
                self._json({"ok": True, "result": result})
                return
            if path == "/api/api-accounts/probe":
                self._json({"ok": True, "probe": _app.core.probe_api_account(self._read_json())})
                return
            if path == "/api/api-accounts/import":
                self._json({"ok": True, "result": _app.core.import_api_account(self._read_json())})
                return
            if path == "/api/model-source/select":
                payload = self._read_json()
                result = _app.core.select_model_source(str(payload.get("sourceId") or ""))
                self._json({"ok": True, "result": result})
                return
            if path == "/api/dashboard/move":
                from agent_manager.models.ordering import move_dashboard_card
                result = move_dashboard_card(_app.core, self._read_json())
                self._json({"ok": True, "result": result})
                return
            if path == "/api/model-workspace":
                workspace = _app.core.save_model_workspace(self._read_json())
                self._json({"ok": True, "workspace": workspace})
                return
            if path == "/api/orchestration/save":
                self._json({"ok": True, **_app.save_orchestration_for_runtime(self.server.runtime, self._read_json())})
                return
            if path == "/api/runtime-tuning":
                tuning = _app.core.save_runtime_tuning(self._read_json())
                self._json({"ok": True, "runtimeTuning": tuning})
                return
            if path == "/api/codex-config":
                import agent_manager.config.recovery
                agent_manager.config.recovery.claim_orphaned_overlay()
                result = _app.core.save_codex_config_document(self._read_json())
                self._json({"ok": True, "result": result, **result})
                return
            if path in {"/api/codex-config/backups", "/api/codex-config/backups/delete"}:
                import agent_manager.config.recovery
                payload = self._read_json()
                result = (agent_manager.config.recovery.create_manual_backup(expected_fingerprint=str(payload.get("expectedFingerprint") or ""))
                          if path.endswith("/backups") else agent_manager.config.recovery.delete_backup(backup_id=str(payload.get("backupId") or "")))
                self._json({"ok": True, "result": result, "inspection": agent_manager.config.recovery.inspect_recovery()})
                return
            if path == "/api/codex-config/recovery":
                import agent_manager.config.recovery
                payload = self._read_json()
                agent_manager.config.recovery.claim_orphaned_overlay()
                result = agent_manager.config.recovery.repair_config(
                    expected_fingerprint=str(payload.get("expectedFingerprint") or ""),
                    backup_id=str(payload.get("backupId") or "") or None,
                    reset=payload.get("reset") is True,
                )
                self._json({"ok": True, "result": result, "document": _app.core.codex_config_document()})
                return
            if path == "/api/sessions/repair":
                import agent_manager.sessions.repair
                self._read_json(optional=True)
                result = agent_manager.sessions.repair.repair_sessions(close_codex=_app._close_codex_processes_safely)
                self._json({"ok": True, "result": result})
                return
            if path == "/api/sessions/visibility/repair":
                import agent_manager.sessions.visibility
                payload = self._read_json()
                result = agent_manager.sessions.visibility.repair(payload.get("expectedToken"), mode=payload.get("mode", "quick"))
                self._json({"ok": True, "result": result})
                return
            if path == "/api/sessions/visibility/restore":
                import agent_manager.sessions.visibility
                result = agent_manager.sessions.visibility.restore(self._read_json().get("backupId"))
                self._json({"ok": True, "result": result})
                return
            if path == "/api/subagent-routing":
                routing = _app.core.save_subagent_routing(self._read_json())
                self._json({"ok": True, "routing": routing})
                return
            if path == "/api/orchestration/restore-defaults":
                restored = _app.core.restore_orchestration_defaults()
                applied = _app.core.apply_configuration(False)
                if applied.get("gatewayRequired") and not self.server.runtime.web2api.status()["running"]:
                    self.server.runtime.web2api.start()
                self._json({"ok": True, "restored": restored, "applied": applied})
                return
            if path == "/api/app-behavior":
                payload = self._read_json()
                behavior = _app.core.save_app_behavior(payload)
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
                self._json({"ok": True, "sync": _app.core.save_history_sync_settings(self._read_json())})
                return
            history_recovery_action = _app.re.fullmatch(r"/api/history/recoveries/([^/]+)/(preview|restore)", path)
            if history_recovery_action:
                payload = self._read_json(optional=True)
                recovery_id, action = history_recovery_action.groups()
                result = (
                    _app.history_sync.preview_recovery(recovery_id)
                    if action == "preview"
                    else _app.history_sync.restore_recovery(recovery_id, str(payload.get("expectedFingerprint") or ""))
                )
                self._json({"ok": True, "result": result})
                return
            if path == "/api/history/preview":
                self._json({"ok": True, "preview": _app.core.preview_history_sync(self._read_json())})
                return
            if path == "/api/history/sync":
                self._json({"ok": True, "result": _app.core.perform_history_sync(self._read_json())})
                return
            if path == "/api/history/reindex":
                self._json({"ok": True, "result": _app.core.refresh_codex_history_index()})
                return
            if path == "/api/sessions/action":
                payload = self._read_json()
                result = _app.core.manage_codex_threads(
                    str(payload.get("action") or ""),
                    payload.get("threadIds") if isinstance(payload.get("threadIds"), list) else [],
                )
                self._json({"ok": True, "result": result})
                return
            if path == "/api/sessions/rename":
                payload = self._read_json()
                result = _app.core.rename_codex_thread(
                    str(payload.get("threadId") or ""),
                    str(payload.get("name") or ""),
                )
                self._json({"ok": True, "result": result})
                return
            provider_key = _app.re.fullmatch(r"/api/providers/([^/]+)/key", path)
            if provider_key:
                payload = self._read_json()
                _app.core.store_provider_key(_app.unquote(provider_key.group(1)), str(payload.get("key", "")))
                self._json({"ok": True})
                return
            provider_portal = _app.re.fullmatch(r"/api/providers/([^/]+)/portal", path)
            if provider_portal:
                self._read_json(optional=True)
                url = _app.core.provider_portal_url(_app.unquote(provider_portal.group(1)))
                if not _app.open_external_browser(url):
                    raise _app.core.ManagerError("系统浏览器未能打开中转站官网，请稍后重试。")
                self._json({"ok": True, "url": url})
                return
            provider_models = _app.re.fullmatch(r"/api/providers/([^/]+)/models", path)
            if provider_models:
                discovery = _app.core.refresh_provider_models(_app.unquote(provider_models.group(1)))
                self._json(
                    {
                        "ok": True,
                        "models": discovery["models"],
                        "discovery": discovery,
                    }
                )
                return
            model_reasoning = _app.re.fullmatch(r"/api/providers/([^/]+)/model-reasoning", path)
            if model_reasoning:
                import agent_manager.models.preferences
                payload = self._read_json()
                result = agent_manager.models.preferences.save_reasoning(
                    _app.unquote(model_reasoning.group(1)), str(payload.get("modelId") or ""), payload,
                )
                self._json({"ok": True, "result": result})
                return
            provider_balance = _app.re.fullmatch(r"/api/providers/([^/]+)/balance", path)
            if provider_balance:
                balance = _app.core.fetch_provider_balance(_app.unquote(provider_balance.group(1)))
                self._json({"ok": True, "balance": balance})
                return
            if path == "/api/main-profiles":
                self._json({"ok": True, "profile": _app.core.save_main_profile(self._read_json())})
                return
            activate_main = _app.re.fullmatch(r"/api/main-profiles/([^/]+)/activate", path)
            if activate_main:
                _app.core.set_active_main(_app.unquote(activate_main.group(1)))
                self._json({"ok": True})
                return
            if path == "/api/strategies":
                self._json({"ok": True, "strategy": _app.core.save_strategy(self._read_json())})
                return
            activate_strategy = _app.re.fullmatch(r"/api/strategies/([^/]+)/activate", path)
            if activate_strategy:
                _app.core.set_active_strategy(_app.unquote(activate_strategy.group(1)))
                self._json({"ok": True})
                return
            if path == "/api/routes":
                _app.core.save_routes(self._read_json().get("routes", {}))
                self._json({"ok": True})
                return
            if path == "/api/agents":
                payload = self._read_json()
                saved = _app.core.write_agent(payload, bool(payload.get("force")))
                self._json({"ok": True, "path": str(saved)})
                return
            if path == "/api/import":
                payload = self._read_json()
                result = _app.core.import_bundle(payload.get("bundle"), str(payload.get("mode") or "merge"))
                self._json({"ok": True, "result": result})
                return
            if path == "/api/shutdown":
                self._json({"ok": True})
                _app.request_application_shutdown(self.server)
                return
            if path == "/api/exit-only":
                self._json({"ok": True})
                _app.request_application_exit_only(self.server)
                return
            if path == "/api/configuration-session/retry":
                self._read_json(optional=True)
                result = self.server.runtime.activate_configuration_session(retry=True)
                self._json({"ok": True, "configurationSession": result})
                return
            if path == "/api/quick-restart":
                if not _app.request_application_restart(self.server):
                    raise _app.core.ManagerError("Agent Manager 已在退出或重启中。")
                self._json(
                    {
                        "ok": True,
                        "message": "管理器正在快速重启；Codex 将保持运行。",
                        "sourcePid": _app.os.getpid(),
                        "requestedAt": _app.core.now_iso(),
                    }
                )
                return
            if path == "/api/codex-runtime/deploy":
                result = _app.core.deploy_codex_runtime()
                self._json({"ok": True, **result})
                return
            if path == "/api/skills/toggle":
                payload = self._read_json()
                if not isinstance(payload.get("enabled"), bool):
                    raise _app.core.ManagerError("enabled 必须是布尔值。")
                result = _app.maintenance.set_skill_enabled(
                    str(payload.get("id") or ""),
                    payload["enabled"],
                    cwd=payload.get("cwd"),
                )
                self._json({"ok": True, **result})
                return
            if path == "/api/skills/install":
                payload = self._read_json()
                result = _app.maintenance.install_public_plugin(
                    str(payload.get("id") or ""),
                    force_refresh=payload.get("force", False),
                )
                self._json({"ok": True, "result": result})
                return
            if path == "/api/updates/check":
                self._read_json(optional=True)
                status = _app.maintenance.update_center_status(force=True)
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
                self._json({"ok": True, "result": _app.maintenance.update_cli()})
                return
            if path == "/api/updates/desktop":
                self._read_json(optional=True)
                self._json({"ok": True, "result": _app.maintenance.open_desktop_update()})
                return
            if path == "/api/emergency/repair":
                payload = self._read_json()
                check_ids = payload.get("checkIds")
                if not isinstance(check_ids, list):
                    raise _app.core.ManagerError("checkIds 必须是数组。")
                self._json({"ok": True, "result": _app.maintenance.apply_emergency_repairs(check_ids)})
                return
            if path == "/api/claude/profiles":
                self._json({"ok": True, "profile": _app.claude.save_profile(self._read_json())})
                return
            if path == "/api/claude/apply":
                payload = self._read_json()
                self._json({"ok": True, "result": _app.claude.apply_profile(str(payload.get("id") or ""))})
                return
            if path == "/api/claude/restore":
                self._read_json(optional=True)
                self._json({"ok": True, "result": _app.claude.restore_official()})
                return
            if path == "/api/usage/reset":
                self._read_json(optional=True)
                usage, point = _app.agent_manager.storage.coordinator.reset_usage(self.server.runtime)
                self._json({"ok": True, "usage": usage, "backup": point})
                return
            if path == "/api/usage/export-download":
                from agent_manager.usage.export import export_usage
                result = export_usage(self.server.runtime, self._read_json())
                self._json({"ok": True, "result": result})
                return
            if path == "/api/recovery":
                payload = self._read_json(optional=True)
                scope = str(payload.get("scope") or "configuration")
                point = _app.agent_manager.storage.coordinator.create(self.server.runtime, scope, str(payload.get("name") or ""))
                self._json({"ok": True, "point": point})
                return
            recovery_action = _app.re.fullmatch(r"/api/recovery/([^/]+)/(preview|restore|delete)", path)
            if recovery_action:
                payload = self._read_json(optional=True)
                point_id, action = recovery_action.groups()
                if action == "preview":
                    result = _app.recovery.preview(point_id)
                elif action == "delete":
                    result = _app.recovery.delete(point_id)
                else:
                    result = _app.agent_manager.storage.coordinator.restore(self.server.runtime, point_id, str(payload.get("expectedFingerprint") or ""))
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
                    accounts = _app.core.load_settings().get("accounts", [])
                    refreshed = self.server.runtime.radar.get_reset_radar(
                        accounts if isinstance(accounts, list) else [],
                        refresh=True,
                    )
                    if refreshed.get("newAlert") and isinstance(refreshed.get("alert"), dict):
                        self.server.notify_radar_alert(refreshed["alert"])
                else:
                    raise _app.core.ManagerError("雷达刷新类型无效；请选择智力、额度或重置雷达。")
                self._json({"ok": True, section: _app._radar_api_section(refreshed)})
                return
            if path == "/api/radar/check":
                self._read_json(optional=True)
                result = self.server.runtime.check_radar_now()
                accounts = _app.core.load_settings().get("accounts", [])
                refreshed = self.server.runtime.radar.get_reset_radar(accounts, refresh=False)
                self._json({"ok": True, "result": result, "reset": _app._radar_api_section(refreshed)})
                return
            self._error("API 路径不存在。", 404)
        except (_app.core.ManagerError, _app.radar.RadarError, _app.toolbox.ToolboxError, _app.app_updates.UpdateError) as exc:
            self._error(str(exc), 400)
        except Exception as exc:
            self._internal_error(exc)

    def do_DELETE(self) -> None:
        self.server.last_request_at = _app.time.time()
        if not self._validate_local_request(write=True):
            return
        path = _app.urlparse(self.path).path
        if not self._authorized():
            self._error("未授权的本地请求。", 403)
            return
        if not self.server.begin_mutating_request():
            self._discard_small_request_body()
            self._error("Agent Manager 正在退出，已拒绝新的修改请求。", 503)
            return
        self._mutation_registered = True
        try:
            totp_item = _app.re.fullmatch(r"/api/toolbox/totp/([^/]+)", path)
            if totp_item:
                removed = _app.toolbox.delete_totp_item(_app.unquote(totp_item.group(1)))
                if not removed:
                    raise _app.toolbox.ValidationError("未找到验证码项目。")
                self._json({"ok": True})
                return
            mail_account = _app.re.fullmatch(r"/api/toolbox/mail/([^/]+)", path)
            if mail_account:
                account_id = _app.unquote(mail_account.group(1))
                removed = _app.toolbox.delete_mail_account(account_id)
                if not removed:
                    raise _app.toolbox.ValidationError("未找到邮箱账户。")
                self.server.runtime.toolbox.forget_mail(account_id)
                self._json({"ok": True})
                return
            provider_key = _app.re.fullmatch(r"/api/providers/([^/]+)/key", path)
            if provider_key:
                _result, point = _app.agent_manager.storage.coordinator.before_account_delete(self.server.runtime, "移除本地 API Key 前", lambda: _app.core.delete_provider_key(_app.unquote(provider_key.group(1))))
                self._json({"ok": True, "backup": point})
                return
            relay_account_key = _app.re.fullmatch(
                r"/api/relay-accounts/([^/]+)/keys/([^/]+)",
                path,
            )
            if relay_account_key:
                payload = self._read_json(optional=True)
                scope = payload.get("scope", "local")
                if scope not in ("local", "website"):
                    raise _app.core.ManagerError("删除范围必须为 local 或 website。")
                def operation():
                    return self.server.runtime.relay_portal.delete_saved_key(
                        _app.unquote(relay_account_key.group(1)), _app.unquote(relay_account_key.group(2)),
                        delete_remote=scope == "website",
                    )
                if scope == "website":
                    # Snapshot locally before the remote mutation. Do not hold
                    # the gateway's global lock during a website request.
                    point = _app.agent_manager.storage.coordinator.create(self.server.runtime, "configuration", "删除网站 Key 前")
                    result = operation()
                else:
                    result, point = _app.agent_manager.storage.coordinator.before_account_delete(self.server.runtime, "移除中转站本地 Key 前", operation)
                self._json({"ok": True, "result": result, "backup": point})
                return
            relay_account = _app.re.fullmatch(r"/api/relay-accounts/([^/]+)", path)
            if relay_account:
                _result, point = _app.agent_manager.storage.coordinator.before_account_delete(self.server.runtime, "删除中转站账号前", lambda: _app.core.remove_relay_account(_app.unquote(relay_account.group(1))))
                self._json({"ok": True, "backup": point})
                return
            provider = _app.re.fullmatch(r"/api/providers/([^/]+)", path)
            if provider:
                _result, point = _app.agent_manager.storage.coordinator.before_account_delete(self.server.runtime, "删除 API Provider 前", lambda: _app.core.remove_provider(_app.unquote(provider.group(1))))
                warning = None
                applied = None
                try:
                    applied = _app.core.apply_configuration(False)
                    if applied.get("gatewayRequired") and not self.server.runtime.web2api.status()["running"]:
                        self.server.runtime.web2api.start()
                except Exception as exc:
                    warning = f"中转站已删除，但子代理配置刷新失败：{str(exc)[:300]}"
                self._json({"ok": True, "applied": applied, "configurationWarning": warning, "backup": point})
                return
            profile = _app.re.fullmatch(r"/api/main-profiles/([^/]+)", path)
            if profile:
                _app.core.remove_main_profile(_app.unquote(profile.group(1)))
                self._json({"ok": True})
                return
            strategy = _app.re.fullmatch(r"/api/strategies/([^/]+)", path)
            if strategy:
                _app.core.remove_strategy(_app.unquote(strategy.group(1)))
                self._json({"ok": True})
                return
            agent = _app.re.fullmatch(r"/api/agents/([^/]+)", path)
            if agent:
                destination = _app.core.archive_agent(_app.unquote(agent.group(1)))
                self._json({"ok": True, "backupPath": str(destination)})
                return
            account = _app.re.fullmatch(r"/api/accounts/([^/]+)", path)
            if account:
                _result, point = _app.agent_manager.storage.coordinator.before_account_delete(self.server.runtime, "删除 Codex 账号前", lambda: _app.core.remove_codex_account(_app.unquote(account.group(1))))
                warning = None
                applied = None
                try:
                    applied = _app.core.apply_configuration(False)
                    if applied.get("gatewayRequired") and not self.server.runtime.web2api.status()["running"]:
                        self.server.runtime.web2api.start()
                except Exception as exc:
                    warning = f"账号已删除，但子代理配置刷新失败：{str(exc)[:300]}"
                self._json({"ok": True, "applied": applied, "configurationWarning": warning, "backup": point})
                return
            account_group = _app.re.fullmatch(r"/api/account-groups/([^/]+)", path)
            if account_group:
                result = _app.core.remove_account_group(_app.unquote(account_group.group(1)))
                self._json({"ok": True, "result": result})
                return
            skill = _app.re.fullmatch(r"/api/skills/([^/]+)", path)
            if skill:
                payload = self._read_json(optional=True)
                result = _app.maintenance.delete_skill(
                    _app.unquote(skill.group(1)),
                    str(payload.get("expectedFingerprint") or ""),
                    cwd=payload.get("cwd"),
                )
                self._json({"ok": True, "result": result})
                return
            claude_profile = _app.re.fullmatch(r"/api/claude/profiles/([^/]+)", path)
            if claude_profile:
                self._json({"ok": True, "result": _app.claude.delete_profile(_app.unquote(claude_profile.group(1)))})
                return
            self._error("API 路径不存在。", 404)
        except (_app.core.ManagerError, _app.toolbox.ToolboxError) as exc:
            self._error(str(exc), 400)
        except Exception as exc:
            self._internal_error(exc)

