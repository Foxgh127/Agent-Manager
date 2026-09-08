"""Oauth services."""
from __future__ import annotations
from agent_manager import application as _app


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
        timeout_seconds: float = _app.OAUTH_LOGIN_TIMEOUT_SECONDS,
        on_account_saved: object | None = None,
    ) -> None:
        self.lock = _app.threading.RLock()
        self.timeout_seconds = max(10.0, float(timeout_seconds))
        self.deadline_monotonic: float | None = None
        self.callback_server: ThreadingHTTPServer | None = None
        self.callback_thread: _app.threading.Thread | None = None
        self.callback_server_login_id: str | None = None
        self.timeout_thread: _app.threading.Thread | None = None
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
            return _app.json.loads(_app.json.dumps(self.data))

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
            if thread and thread.is_alive() and thread is not _app.threading.current_thread():
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
                    "finishedAt": _app.core.now_iso(),
                    "error": _app.core._redact_sensitive_text(error, limit=500) if error else None,
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
                and _app.time.monotonic() >= self.deadline_monotonic
            )
            if due:
                self._finish(
                    "expired",
                    expected_login_id=login_id,
                    error="OAuth 登录已超时，请重新开始。",
                )

    @staticmethod
    def _pkce_pair() -> tuple[str, str]:
        verifier = _app.base64.urlsafe_b64encode(_app.secrets.token_bytes(64)).decode("ascii").rstrip("=")
        digest = _app.hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = _app.base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return verifier, challenge

    @classmethod
    def _authorization_url(cls, redirect_uri: str, challenge: str, state: str) -> str:
        query = _app.urllib.parse.urlencode(
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

        class CallbackHandler(_app.BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_GET(self) -> None:
                try:
                    result = owner.submit_callback(self.path, expected_login_id=login_id)
                except (_app.core.ManagerError, ValueError) as exc:
                    body = owner._success_html(False, _app.core._redact_sensitive_text(exc, limit=360))
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
                server = _app.OAuthCallbackServer(("127.0.0.1", port), CallbackHandler)
                return server, port
            except OSError as exc:
                last_error = exc
        raise _app.core.ManagerError(
            "无法启动 OAuth 回调监听器：本机 1455 端口已被占用。"
            "请先取消其他 Codex / VS Code 登录任务，再重新开始。"
        ) from last_error

    def _parse_callback_locked(self, callback_value: str) -> tuple[str, str | None]:
        raw = str(callback_value or "").strip()
        if not raw:
            raise _app.core.ManagerError("请粘贴完整回调地址。")
        try:
            raw_size = len(raw.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise _app.core.ManagerError("OAuth 回调地址包含无效 Unicode 字符。") from exc
        if raw_size > self.MAX_CALLBACK_INPUT_BYTES:
            raise _app.core.ManagerError("OAuth 回调地址过长，已拒绝处理。")
        if any(ord(character) < 32 or ord(character) == 127 for character in raw):
            raise _app.core.ManagerError("OAuth 回调地址包含无效控制字符。")
        port = self.callback_port
        expected_state = self.expected_state
        status = str(self.data.get("status") or "")
        if status not in {"waiting", "exchanging"} or not port or not expected_state:
            raise _app.core.ManagerError("当前没有可接收回调的 OAuth 登录。")
        if raw.startswith("/"):
            raw = f"http://localhost:{port}{raw}"
        elif "://" not in raw:
            raw = f"http://localhost:{port}/auth/callback?{raw.lstrip('?')}"
        try:
            parsed = _app.urlparse(raw)
            parsed_port = parsed.port
        except ValueError as exc:
            raise _app.core.ManagerError("OAuth 回调地址格式无效。") from exc
        if (
            parsed.scheme != "http"
            or str(parsed.hostname or "").casefold() not in {"localhost", "127.0.0.1"}
            or parsed_port != port
            or parsed.path != "/auth/callback"
            or parsed.params
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise _app.core.ManagerError(
                f"回调地址必须是 http://localhost:{port}/auth/callback?code=...&state=..."
            )
        if "#" in raw:
            raise _app.core.ManagerError("OAuth 回调地址不能包含 fragment。")
        if _app.re.search(r"%(?![0-9A-Fa-f]{2})", parsed.query):
            raise _app.core.ManagerError("OAuth 回调查询参数包含无效百分号编码。")
        try:
            pairs = _app.urllib.parse.parse_qsl(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=20,
            )
        except ValueError as exc:
            raise _app.core.ManagerError("OAuth 回调查询参数格式无效。") from exc
        params: dict[str, list[str]] = {}
        for key, value in pairs:
            params.setdefault(key, []).append(value)
        for key in ("code", "state", "error", "error_description", "error_uri"):
            if len(params.get(key, [])) > 1:
                raise _app.core.ManagerError(f"OAuth 回调包含重复的 {key} 参数。")
        state = str((params.get("state") or [""])[0])
        if not state or not _app.secrets.compare_digest(state, expected_state):
            raise _app.core.ManagerError("回调 state 校验失败，请粘贴当前这次授权产生的地址。")
        code = str((params.get("code") or [""])[0])
        oauth_error = str((params.get("error") or [""])[0])
        if code and oauth_error:
            raise _app.core.ManagerError("OAuth 回调不能同时包含 code 和 error 参数。")
        if oauth_error:
            if not _app.re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", oauth_error):
                raise _app.core.ManagerError("OAuth 回调 error 参数格式无效。")
            for key in ("error_description", "error_uri"):
                value = str((params.get(key) or [""])[0])
                if len(value.encode("utf-8")) > self.MAX_CALLBACK_FIELD_BYTES:
                    raise _app.core.ManagerError(f"OAuth 回调 {key} 参数过长，已拒绝处理。")
            return "", oauth_error
        if not code:
            raise _app.core.ManagerError("回调地址缺少 code 参数。")
        if len(code.encode("utf-8")) > self.MAX_CALLBACK_CODE_BYTES:
            raise _app.core.ManagerError("OAuth 回调 code 参数过长，已拒绝处理。")
        if any(ord(character) < 32 or ord(character) == 127 for character in code):
            raise _app.core.ManagerError("OAuth 回调 code 参数包含无效控制字符。")
        return code, None

    def _parse_callback(self, callback_value: str) -> str:
        with self.lock:
            code, oauth_error = self._parse_callback_locked(callback_value)
            if oauth_error:
                raise _app.core.ManagerError(f"OAuth 授权未完成：{oauth_error}。")
            return code

    def submit_callback(self, callback_value: str, expected_login_id: str | None = None) -> dict:
        with self.lock:
            if expected_login_id and self.login_id != expected_login_id:
                raise _app.core.ManagerError("OAuth 登录会话不匹配，请重新开始。")
            self._expire_if_due()
            if expected_login_id and self.login_id != expected_login_id:
                raise _app.core.ManagerError("OAuth 登录会话不匹配，请重新开始。")
            code, oauth_error = self._parse_callback_locked(callback_value)
            login_id = self.login_id
            if not login_id:
                raise _app.core.ManagerError("OAuth 登录会话已经结束。")
            if oauth_error:
                if self.data.get("status") != "waiting" or self.exchange_started:
                    raise _app.core.ManagerError("OAuth Token 交换已经开始，不能再提交授权错误。")
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
                result = _app.json.loads(_app.json.dumps(self.data))
                if oauth_error == "access_denied":
                    result["error"] = message
                return result
            if self.exchange_started:
                return _app.json.loads(_app.json.dumps(self.data))
            self.authorization_code = code
            self.exchange_started = True
            self.data["status"] = "exchanging"
            exchange_thread = _app.threading.Thread(
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
                raise _app.core.ManagerError("无法启动 OAuth Token 交换任务。") from exc
            return _app.json.loads(_app.json.dumps(self.data))

    def _exchange_code(self, code: str, verifier: str, port: int) -> dict:
        redirect_uri = f"http://localhost:{port}/auth/callback"
        request = _app.urllib.request.Request(
            self.TOKEN_ENDPOINT,
            data=_app.urllib.parse.urlencode(
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
            with _app.core._open_same_origin_request(request, timeout=30) as response:
                raw = response.read(self.MAX_TOKEN_RESPONSE_BYTES + 1)
        except _app.urllib.error.HTTPError as exc:
            status_code = exc.code
            try:
                exc.close()
            finally:
                raise _app.core.ManagerError(f"OAuth Token 交换失败：HTTP {status_code}。") from exc
        except (_app.urllib.error.URLError, TimeoutError, OSError) as exc:
            raise _app.core.ManagerError("OAuth Token 交换失败，请检查网络或代理后重试。") from exc
        if len(raw) > self.MAX_TOKEN_RESPONSE_BYTES:
            raise _app.core.ManagerError("OAuth Token 响应过大，已拒绝处理。")
        try:
            payload = _app.json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, _app.json.JSONDecodeError) as exc:
            raise _app.core.ManagerError("OAuth Token 响应不是有效 JSON。") from exc
        if not isinstance(payload, dict):
            raise _app.core.ManagerError("OAuth Token 响应格式无效。")
        tokens = {}
        for key in ("id_token", "access_token", "refresh_token"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                if len(value) > 100_000:
                    raise _app.core.ManagerError("OAuth Token 字段过大，已拒绝处理。")
                tokens[key] = value
        if not tokens.get("id_token") or not tokens.get("access_token"):
            raise _app.core.ManagerError("OAuth Token 响应缺少必要凭据。")
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
                raise _app.core.ManagerError("OAuth 登录状态不完整，请重新开始。")
            exchange = exchange_code if callable(exchange_code) else self._exchange_code
            tokens = exchange(code, verifier, port)
            with self.lock:
                if self.login_id != login_id or self.data.get("status") != "exchanging":
                    return
                if (
                    self.deadline_monotonic is None
                    or _app.time.monotonic() >= self.deadline_monotonic
                ):
                    self._finish(
                        "expired",
                        expected_login_id=login_id,
                        error="OAuth 登录已超时，请重新开始。",
                    )
                    return
                auth_bytes = _app.json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "OPENAI_API_KEY": None,
                        "tokens": {
                            "id_token": tokens["id_token"],
                            "access_token": tokens["access_token"],
                            "refresh_token": tokens.get("refresh_token"),
                        },
                        "last_refresh": _app.core.now_iso(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                # Saving is the local commit point. Holding the lifecycle lock
                # prevents cancel/start from changing generations midway through
                # an otherwise valid credential write.
                if account_payload.get("reauthAccountId"):
                    import agent_manager.accounts.reauthentication
                    account = agent_manager.accounts.reauthentication.reauthenticate(account_payload["reauthAccountId"], auth_bytes)
                else:
                    account = _app.core.save_codex_account(account_payload, auth_bytes=auth_bytes)
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
                error=_app.core._redact_sensitive_text(exc, limit=500),
            )

    def _watch_timeout(self, login_id: str) -> None:
        while True:
            with self.lock:
                if self.login_id != login_id or self.data.get("status") not in self.ACTIVE_STATUSES:
                    return
                deadline = self.deadline_monotonic
            remaining = (deadline or 0.0) - _app.time.monotonic()
            if remaining <= 0:
                self._finish(
                    "expired",
                    expected_login_id=login_id,
                    error="OAuth 登录已超时，请重新开始。",
                )
                return
            _app.time.sleep(min(remaining, 0.25))

    def _prepare_for_start(self) -> None:
        with self.lock:
            self._expire_if_due()
            active = self.data.get("status") in self.ACTIVE_STATUSES
            listener_alive = bool(self.callback_thread and self.callback_thread.is_alive())
            login_id = self.login_id
            if active and listener_alive:
                raise _app.core.ManagerError("已有 OAuth 登录正在进行；可以继续、手动提交回调或先取消。")
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
                import agent_manager.accounts.reauthentication
                reauth_account = agent_manager.accounts.reauthentication.target(str(payload["reauthAccountId"]))
                payload = {**payload, "label":reauth_account.get("label"), "groupId":reauth_account.get("groupId") or "official", "proxyEnabled":reauth_account.get("proxyEnabled", False)}
            group_id = str(payload.get("groupId") or "official")
            _app.core._account_group(_app.core.load_settings(), group_id)
            login_id = _app.secrets.token_hex(8)
            verifier, challenge = self._pkce_pair()
            expected_state = _app.secrets.token_urlsafe(32)
            server, port = self._bind_callback_server(login_id)
            redirect_uri = f"http://localhost:{port}/auth/callback"
            authorization_url = self._authorization_url(redirect_uri, challenge, expected_state)
            if reauth_account:
                authorization_url += "&" + _app.urllib.parse.urlencode({"prompt":"select_account", "login_hint":reauth_account.get("email") or ""})
            started_at = _app.datetime.now(_app.timezone.utc)
            self.login_id = login_id
            self.callback_port = port
            self.callback_server = server
            self.callback_server_login_id = login_id
            self.code_verifier = verifier
            self.expected_state = expected_state
            self.authorization_code = None
            self.exchange_started = False
            self.deadline_monotonic = _app.time.monotonic() + self.timeout_seconds
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
                    "expiresAt": (started_at + _app.timedelta(seconds=self.timeout_seconds)).isoformat(),
                    "url": authorization_url,
                    "callbackUrl": redirect_uri,
                    "port": port,
                    **({"reauthAccountId":reauth_account["id"]} if reauth_account else {}),
                }
            )
            callback_thread = _app.threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.1},
                name=f"codex-agent-manager-oauth-listener-{login_id}",
                daemon=True,
            )
            timeout_thread = _app.threading.Thread(
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
            opened = bool(_app.open_browser_window(authorization_url))
            browser_error = None
        except Exception as exc:
            opened = False
            browser_error = _app.core._redact_sensitive_text(exc, limit=500)
        with self.lock:
            if self.login_id == login_id and self.data.get("status") in self.ACTIVE_STATUSES:
                self.data["browserOpened"] = opened
                self.data["browserError"] = browser_error
        return self.state()

    def open_browser(self, expected_login_id: str | None = None) -> dict:
        self._expire_if_due()
        with self.lock:
            if expected_login_id and self.login_id != expected_login_id:
                raise _app.core.ManagerError("OAuth 登录会话不匹配，请重新开始。")
            if self.data.get("status") not in {"starting", "waiting"}:
                raise _app.core.ManagerError("当前没有可重新打开的 OAuth 登录。")
            authorization_url = str(self.data.get("url") or "")
            opening_login_id = self.login_id
        if not authorization_url:
            raise _app.core.ManagerError("OAuth 授权链接尚未就绪。")
        try:
            opened = bool(_app.open_browser_window(authorization_url))
            browser_error = None
        except Exception as exc:
            opened = False
            browser_error = _app.core._redact_sensitive_text(exc, limit=500)
        with self.lock:
            if self.login_id == opening_login_id:
                self.data["browserOpened"] = opened
                self.data["browserError"] = browser_error
        if not opened and browser_error:
            raise _app.core.ManagerError(f"无法打开浏览器：{browser_error}")
        return self.state()

    def cancel(self, expected_login_id: str | None = None) -> dict:
        with self.lock:
            if expected_login_id and self.login_id != expected_login_id:
                raise _app.core.ManagerError("OAuth 登录会话不匹配，请重新开始。")
            login_id = self.login_id
            self._finish("cancelled", expected_login_id=login_id)
            return _app.json.loads(_app.json.dumps(self.data))

    def close(self) -> dict:
        with self.lock:
            active = self.data.get("status") in self.ACTIVE_STATUSES
            if active:
                return self.cancel()
            self._stop_callback_server(
                expected_login_id=self.callback_server_login_id,
            )
            return _app.json.loads(_app.json.dumps(self.data))

