"""Runtime services."""
from __future__ import annotations
from agent_manager import application as _app


class ManagerRuntime:
    def __init__(
        self,
        quick_restart: bool = False,
        defer_configuration: bool = False,
        restart_handoff: dict | None = None,
    ) -> None:
        self.lock = _app.threading.RLock()
        self.last_error: str | None = None
        self.switch_operation_lock = _app.threading.RLock()
        self.switch_operations: dict[str, dict] = {}
        self._closed = False
        self.app_updates = None
        self._close_result = None
        self.quick_restart = bool(quick_restart)
        self.restart_handoff = dict(restart_handoff or {})
        self.defer_configuration = bool(defer_configuration)
        self._restart_prepared = False
        self._restart_prepare_result: dict | None = None
        self.account_refresh_lock = _app.threading.RLock()
        self.account_refresh_stop = _app.threading.Event()
        self.account_refresh_threads: list[_app.threading.Thread] = []
        self.account_refresh_pending: list[str] = []
        self.account_refresh_active: set[str] = set()
        self.account_auto_refresh_stop = _app.threading.Event()
        self.account_auto_refresh_wake = _app.threading.Event()
        self.account_auto_refresh_thread: _app.threading.Thread | None = None
        self.mail_health_stop = _app.threading.Event()
        self.mail_health_wake = _app.threading.Event()
        self.mail_health_thread: _app.threading.Thread | None = None
        self.update_check_stop = _app.threading.Event()
        self.update_check_wake = _app.threading.Event()
        self.update_check_thread: _app.threading.Thread | None = None
        self.radar_monitor_stop = _app.threading.Event()
        self.radar_monitor_wake = _app.threading.Event()
        self.radar_monitor_thread: _app.threading.Thread | None = None
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
        self.toolbox = _app.ToolboxRuntime()
        self.web2api = _app.Web2APIManager()
        from agent_manager.usage.calibration import QuotaCalibrationSampler
        self.quota_estimates = QuotaCalibrationSampler(self)
        self.radar = _app.radar.RadarService()
        self.oauth = _app.OAuthDeviceLogin(on_account_saved=self.schedule_account_refresh)
        self.relay_portal = _app.relay_portal.RelayPortalService()
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
        detail = _app.core._redact_sensitive_text(error, limit=500)
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
            return _app.json.loads(_app.json.dumps(self.configuration_session))

    def _recovery_public_state(self, error: object) -> dict:
        """Build a credential-safe first-paint payload for recovery mode."""
        detail = _app.core._redact_sensitive_text(error, limit=500)
        try:
            settings = _app.core.load_settings()
            public_settings = _app.core._public_settings_projection(settings)
        except Exception:
            # Never return the raw failing document: imported records may
            # contain legacy inline credentials.  Defaults contain no user
            # data and keep the React shell structurally usable while the
            # dedicated repair endpoints work on the real files.
            defaults = _app.core._initial_settings()
            try:
                public_settings = _app.core._public_settings_projection(defaults)
            except Exception:
                public_settings = _app.json.loads(_app.json.dumps(defaults))
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
            "difficultyMeta": _app.core.DIFFICULTY_META,
            "efforts": list(_app.core.VALID_EFFORTS),
            "sandboxes": list(_app.core.VALID_SANDBOXES),
            "codexVersion": "暂不可用（安全修复模式）",
            "codexHome": str(_app.core.CODEX_HOME),
            "configPath": str(_app.core.CONFIG_FILE),
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
                return _app.json.loads(_app.json.dumps(self.configuration_session))
            if self._configuration_activation_started and not (
                retry and self.configuration_session.get("status") == "error"
            ):
                return _app.json.loads(_app.json.dumps(self.configuration_session))
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
        activation_started = _app.time.monotonic()
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
                preserve_external_selection = _app.live_selection.preserve_live_configuration_on_restart()
            if preserve_external_selection:
                external_selection = {"preserveCurrent": True, "message": "管理器已重启，继续保留外部工具选择的 Codex 账号与配置。"}
            if not self.quick_restart:
                external_selection = _app.live_selection.reconcile_startup_selection()
                preserve_external_selection = bool(external_selection.get("preserveCurrent"))
            adopt_existing_overlay = bool(
                preserve_external_selection and _app.core._runtime_overlay_read()
            )
            overlay = (
                _app.core.adopt_runtime_configuration_overlay()
                if self.quick_restart or adopt_existing_overlay
                else _app.core.begin_runtime_configuration_overlay()
            )
            overlay_started = True
            fast_adopted = bool(
                self.quick_restart
                and _app._quick_restart_can_adopt_without_apply(self.restart_handoff)
            )
            applied = (
                {
                    "changed": False,
                    "gatewayRequired": bool(
                        _app.core.load_settings().get("web2api", {}).get("enabled")
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
                else _app.core.apply_configuration(False)
            )
            settings = _app.core.load_settings()
            gateway_required = bool(applied.get("gatewayRequired"))
            service_enabled = bool(settings.get("web2api", {}).get("enabled"))
            if (gateway_required or service_enabled) and _app.core.service_secret_configured("web2api"):
                try:
                    self.web2api.start()
                except _app.core.ManagerError as exc:
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
                codex_was_running = bool(_app.core.running_codex_processes())
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
                        "activationMs": round((_app.time.monotonic() - activation_started) * 1000, 1),
                        "configurationReapplied": not fast_adopted and not preserve_external_selection,
                        "fastOverlayAdopted": fast_adopted,
                    },
                    "configurationStateFingerprint": _app._restart_configuration_fingerprint(),
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
                    restored = _app.core.restore_runtime_configuration_overlay(force=True)
                except Exception as restore_exc:
                    restored = {"restored": False, "warnings": [_app.core._redact_sensitive_text(restore_exc, limit=300)]}
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
                    "error": _app.core._redact_sensitive_text(exc, limit=500),
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
            if _app.core.load_settings().get("appBehavior", {}).get("radarMonitoring") is True:
                self._start_radar_monitor()
        return _app.json.loads(_app.json.dumps(self.configuration_session))

    def _account_auto_refresh_interval_minutes(self) -> int:
        settings = _app.core.load_settings()
        raw_value = settings.get("appBehavior", {}).get("quotaRefreshMinutes", 10)
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = 10
        return value if value in _app.core.VALID_QUOTA_REFRESH_MINUTES else 10

    def _account_auto_refresh_tick(self) -> dict:
        interval_minutes = self._account_auto_refresh_interval_minutes()
        checked_at = _app.core.now_iso()
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
                return _app.json.loads(_app.json.dumps(self.account_auto_refresh_status))

        account_ids = _app.core.stale_codex_account_ids(interval_minutes * 60)
        if account_ids:
            self.schedule_account_refresh(account_ids)
            with self.account_refresh_lock:
                self.account_auto_refresh_status.update(
                    {"lastQueuedAt": checked_at, "queued": len(account_ids)}
                )
        with self.account_refresh_lock:
            return _app.json.loads(_app.json.dumps(self.account_auto_refresh_status))

    def _run_account_auto_refresh(self) -> None:
        refresh_on_start = True
        while not self.account_auto_refresh_stop.is_set():
            if refresh_on_start:
                try:
                    self._account_auto_refresh_tick()
                except Exception as exc:
                    with self.account_refresh_lock:
                        self.account_auto_refresh_status.update(
                            {"lastCheckedAt": _app.core.now_iso(), "lastError": _app.core._redact_sensitive_text(exc, limit=300)}
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
                        {"lastCheckedAt": _app.core.now_iso(), "lastError": _app.core._redact_sensitive_text(exc, limit=300)}
                    )

    def _start_account_auto_refresh(self) -> None:
        if self.account_auto_refresh_thread and self.account_auto_refresh_thread.is_alive():
            return
        self.account_auto_refresh_stop.clear()
        self.account_auto_refresh_thread = _app.threading.Thread(
            target=self._run_account_auto_refresh,
            name="codex-agent-manager-quota-refresh",
            daemon=True,
        )
        self.account_auto_refresh_thread.start()

    def _mail_health_interval_hours(self) -> int:
        settings = _app.core.load_settings()
        raw_value = settings.get("appBehavior", {}).get("mailHealthCheckHours", 24)
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = 24
        return value if value in _app.core.VALID_MAIL_HEALTH_CHECK_HOURS else 24

    def _mail_health_tick(self) -> dict:
        interval_hours = self._mail_health_interval_hours()
        checked_at = _app.core.now_iso()
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
            return _app.json.loads(_app.json.dumps(self.mail_health_status))
        try:
            account_ids = _app.toolbox.stale_mail_account_ids(interval_hours * 60 * 60)
        except _app.toolbox.ToolboxError as exc:
            self.mail_health_status.update(
                {"status": "error", "lastError": _app.core._redact_sensitive_text(exc, limit=240)}
            )
            return _app.json.loads(_app.json.dumps(self.mail_health_status))

        for account_id in account_ids:
            if self.mail_health_stop.is_set() or self._closed:
                break
            try:
                result = _app.toolbox.check_saved_email_health(
                    account_id,
                    timeout=12,
                    commit_guard=lambda: not self.mail_health_stop.is_set() and not self._closed,
                )
                if result.get("status") == "cancelled":
                    break
                self.mail_health_status["checked"] += 1
                bucket = "healthy" if result.get("healthy") else "error"
                self.mail_health_status[bucket] += 1
            except _app.toolbox.ToolboxError:
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
        return _app.json.loads(_app.json.dumps(self.mail_health_status))

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
        self.mail_health_thread = _app.threading.Thread(
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
        if thread and thread.is_alive() and thread is not _app.threading.current_thread():
            thread.join(timeout=2.0)

    def request_account_auto_refresh_check(self) -> None:
        self.account_auto_refresh_wake.set()

    def _stop_account_auto_refresh(self) -> None:
        self.account_auto_refresh_stop.set()
        self.account_auto_refresh_wake.set()
        thread = self.account_auto_refresh_thread
        if thread and thread.is_alive() and thread is not _app.threading.current_thread():
            thread.join(timeout=2.0)

    def _run_update_checks(self) -> None:
        try:
            status = _app.maintenance.update_center_status(force=False)
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
                {"status": "error", "lastCheckedAt": None, "nextCheckAt": None, "lastError": _app.core._redact_sensitive_text(exc, limit=300)}
            )

    def _start_update_checks(self) -> None:
        self._run_update_checks()

    def get_app_updates(self):
        with self.lock:
            if self._closed:
                raise _app.core.ManagerError("管理器正在退出。")
            if self.app_updates is None:
                if getattr(_app.sys, "frozen", False):
                    version = ".".join(str(part) for part in _app._executable_version(_app.Path(_app.sys.executable))[:3])
                else:
                    version = _app.VERSION
                self.app_updates = _app.app_updates.AppUpdateService(
                    version, _app.core.STATE_DIR / "app-update-config.json",
                    _app.core.user_downloads_directory() / "AgentManagerUpdates",
                    bundled_source_path=_app.RESOURCE_ROOT / "app-update-source.json",
                    cache_path=_app.core.STATE_DIR / "app-update-cache.json",
                    install_supported=bool(getattr(_app.sys, "frozen", False) and _app.os.name == "nt"),
                    release_epoch=_app.RELEASE_EPOCH,
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
                self.radar_monitor_status.update({"status": "error", "lastError": _app.core._redact_sensitive_text(exc, limit=300)})
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
                self.radar_monitor_status.update({"status": "error", "lastError": _app.core._redact_sensitive_text(exc, limit=300)})

    def _start_radar_monitor(self) -> None:
        """Run the public-source hourly monitor only after explicit opt-in."""
        enabled = _app.core.load_settings().get("appBehavior", {}).get("radarMonitoring") is True
        if not enabled or self._closed or self._restart_prepared:
            self._stop_radar_monitor()
            self.radar_monitor_status.update(enabled=False, status="manual", nextCheckAt=None)
            return
        self.radar_monitor_status["enabled"] = True
        if self.radar_monitor_thread and self.radar_monitor_thread.is_alive() and not self.radar_monitor_stop.is_set():
            return
        self.radar_monitor_stop = _app.threading.Event()
        self.radar_monitor_wake = _app.threading.Event()
        self.radar_monitor_thread = _app.threading.Thread(target=self._run_radar_monitor, args=(self.radar_monitor_stop, self.radar_monitor_wake), name="radar-hourly-monitor", daemon=True)
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
        if thread and thread.is_alive() and thread is not _app.threading.current_thread():
            thread.join(timeout=2.0)

    def state(self) -> dict:
        # Pair first-paint status with the lifecycle observed before its reads.
        # If activation finishes mid-request, the client will still poll and
        # replace this pre-activation snapshot once, instead of keeping it.
        session = _app.json.loads(_app.json.dumps(self.configuration_session))
        try:
            state = _app.core.public_state()
        except Exception as exc:
            if not self.configuration_session.get("recoveryOnly"):
                raise
            state = self._recovery_public_state(exc)
        try:
            diagnostics = _app.maintenance.run_emergency_checks(force=False)
        except Exception as exc:
            detail = _app.core._redact_sensitive_text(exc, limit=300)
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
        state["status"] = _app.maintenance.merge_configuration_diagnostics(state.get("status"), diagnostics)
        state["validation"] = self.validation
        state["web2apiStatus"] = self.web2api.status()
        state["oauthStatus"] = self.oauth.state()
        state["relayLoginStatus"] = self.relay_portal.public_state()
        state["configurationSession"] = session
        with self.account_refresh_lock:
            state["accountRefreshStatus"] = _app.json.loads(_app.json.dumps(self.account_refresh_status))
            state["accountAutoRefreshStatus"] = _app.json.loads(_app.json.dumps(self.account_auto_refresh_status))
        state["mailHealthStatus"] = _app.json.loads(_app.json.dumps(self.mail_health_status))
        state["updateCheckStatus"] = _app.json.loads(_app.json.dumps(self.update_check_status))
        state["radarMonitorStatus"] = _app.json.loads(_app.json.dumps(self.radar_monitor_status))
        if getattr(self, "quota_estimates", None):
            self.quota_estimates.decorate(state.get("settings", {}).get("accounts", []))
        return state

    def account_snapshot(self) -> dict:
        snapshot = _app.core.public_account_snapshot()
        if getattr(self, "quota_estimates", None):
            self.quota_estimates.decorate(snapshot.get("accounts", []))
        with self.account_refresh_lock:
            snapshot["accountRefreshStatus"] = _app.json.loads(_app.json.dumps(self.account_refresh_status))
            snapshot["accountAutoRefreshStatus"] = _app.json.loads(_app.json.dumps(self.account_auto_refresh_status))
        return snapshot

    def configuration_snapshot(self) -> dict:
        """Return the small startup state without rebuilding the full dashboard."""
        with self.lock:
            return _app.json.loads(_app.json.dumps(self.configuration_session))

    def _prune_switch_operations_locked(self) -> None:
        cutoff = _app.time.monotonic() - 15 * 60
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
        if not _app.re.fullmatch(r"[A-Za-z0-9_-]{8,80}", operation_id):
            raise _app.core.ManagerError("切换操作标识无效，请刷新界面后重试。")
        now = _app.time.monotonic()
        with self.switch_operation_lock:
            self._prune_switch_operations_locked()
            existing = self.switch_operations.get(operation_id)
            if existing and existing.get("status") == "running":
                raise _app.core.ManagerError("该切换操作已经在运行，请勿重复提交。")
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
                "startedAt": _app.core.now_iso(),
                "updatedAt": _app.core.now_iso(),
                "error": None,
                "_startedMonotonic": now,
                "_updatedMonotonic": now,
            }
            self.switch_operations[operation_id] = record
            return self.switch_operation_status(operation_id)

    def update_switch_operation(self, operation_id: str, payload: dict) -> dict:
        now = _app.time.monotonic()
        with self.switch_operation_lock:
            record = self.switch_operations.get(str(operation_id or ""))
            if not record:
                raise _app.core.ManagerError("切换进度已经过期，请重新操作。")
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
            record["updatedAt"] = _app.core.now_iso()
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
                raise _app.core.ManagerError("切换进度已经过期，请重新操作。")
            now = _app.time.monotonic()
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
                # A successfully changed route ends the previous dashboard lease.
                # Identity is also checked by the relay service on every read.
                relay = getattr(self, "relay_portal", None)
                if relay is not None:
                    try:
                        relay.close(silent=True)
                    except Exception:
                        pass
                visibility = (result.get("sessionSync") or {}).get("visibility") if isinstance(result, dict) else None
                if isinstance(visibility, dict):
                    visibility = {**visibility, **(visibility.get("completion") or {})}
                    record["sessionVisibility"] = {
                        key: visibility[key] for key in ("changed", "status", "partial", "deferredSessions", "catalogMissing", "catalogBlocked", "remainingActions", "message", "reason") if key in visibility
                    }
            record["error"] = _app.core._redact_sensitive_text(error, limit=360) if error is not None else None
            if isinstance(result, dict) and isinstance(result.get("performance"), dict):
                performance = result["performance"]
                record["elapsedMs"] = int(performance.get("totalMs") or 0)
                record["timings"] = performance.get("stages") or record.get("timings") or {}
            else:
                record["elapsedMs"] = max(
                    0,
                    round((now - float(record.get("_startedMonotonic") or now)) * 1000),
                )
            record["updatedAt"] = _app.core.now_iso()
            record["_updatedMonotonic"] = now
            return self.switch_operation_status(operation_id)

    def switch_operation_status(self, operation_id: str) -> dict:
        with self.switch_operation_lock:
            self._prune_switch_operations_locked()
            record = self.switch_operations.get(str(operation_id or ""))
            if not record:
                raise _app.core.ManagerError("未找到该切换进度，可能已经过期。")
            public = {key: value for key, value in record.items() if not key.startswith("_")}
            if public.get("status") == "running":
                public["elapsedMs"] = max(
                    int(public.get("elapsedMs") or 0),
                    round((_app.time.monotonic() - float(record.get("_startedMonotonic") or _app.time.monotonic())) * 1000),
                )
            return _app.json.loads(_app.json.dumps(public))

    def schedule_account_refresh(self, account_ids: list[str]) -> dict:
        requested = list(dict.fromkeys(str(item) for item in account_ids if str(item).strip()))
        if not requested:
            with self.account_refresh_lock:
                return _app.json.loads(_app.json.dumps(self.account_refresh_status))
        with self.account_refresh_lock:
            if self._closed or self.account_refresh_stop.is_set():
                return _app.json.loads(_app.json.dumps(self.account_refresh_status))
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
                        "startedAt": _app.core.now_iso(),
                        "finishedAt": None,
                    }
                )
            self.account_refresh_status["total"] += len(added)
            self.account_refresh_threads = [thread for thread in self.account_refresh_threads if thread.is_alive()]
            worker_target = min(2, len(self.account_refresh_pending) + len(self.account_refresh_active))
            while len(self.account_refresh_threads) < worker_target:
                worker = _app.threading.Thread(
                    target=self._run_account_refresh,
                    name=f"codex-agent-manager-import-refresh-{len(self.account_refresh_threads) + 1}",
                    daemon=True,
                )
                self.account_refresh_threads.append(worker)
                worker.start()
            return _app.json.loads(_app.json.dumps(self.account_refresh_status))

    def _run_account_refresh(self) -> None:
        worker = _app.threading.current_thread()
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
                                "finishedAt": _app.core.now_iso(),
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
                result = _app.core.refresh_codex_accounts(
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
                        "finishedAt": _app.core.now_iso(),
                    }
                )

    def _stop_account_refresh_workers(self) -> None:
        import agent_manager.gateway.service
        agent_manager.gateway.service.stop_codex_session_usage_backfill(timeout_seconds=1.0)
        if getattr(self, "quota_estimates", None):
            self.quota_estimates.close()
        self.account_refresh_stop.set()
        with self.account_refresh_lock:
            self.account_refresh_pending.clear()
            workers = [
                thread
                for thread in self.account_refresh_threads
                if thread.is_alive() and thread is not _app.threading.current_thread()
            ]
        deadline = _app.time.monotonic() + 2.0
        for thread in workers:
            remaining = deadline - _app.time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

    def validate(self, *, run_doctor: bool = True) -> dict:
        with self.lock:
            self.validation = _app.core.validate_configuration(run_doctor=run_doctor)
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
            started = _app.time.monotonic()
            if getattr(self, "app_updates", None):
                self.app_updates.cancel_download()
            import agent_manager.gateway.service
            agent_manager.gateway.service.stop_codex_session_usage_backfill(timeout_seconds=0)
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
                    and thread is not _app.threading.current_thread()
                    and thread.is_alive()
                )
            )
        deadline = _app.time.monotonic() + max(0.0, min(float(timeout_seconds), 5.0))
        for thread in workers:
            remaining = deadline - _app.time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        lingering = [thread.name for thread in workers if thread.is_alive()]
        if not agent_manager.gateway.service.stop_codex_session_usage_backfill(timeout_seconds=max(0.0, deadline - _app.time.monotonic())):
            lingering.append("codex-usage-history-backfill")
        result = {
            "prepared": True,
            "elapsedMs": round((_app.time.monotonic() - started) * 1000, 1),
            "lingeringWorkers": lingering,
        }
        with self.lock:
            self._restart_prepare_result = result
        return _app.json.loads(_app.json.dumps(result))

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
            import agent_manager.gateway.service
            agent_manager.gateway.service.enable_codex_session_usage_backfill()
            self._start_account_auto_refresh()
            self._start_mail_health_checks()
            self._start_update_checks()
            if _app.core.load_settings().get("appBehavior", {}).get("radarMonitoring") is True:
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
                    raise _app.core.ManagerError("后台任务尚未停止，已取消配置恢复；请稍后重试退出。")
                if session_was_active:
                    if _app.core._require_codex_process_scan_known():
                        closed = _app._close_codex_processes_safely()
                    # A successful termination request is not evidence that
                    # Codex is gone. Also reject unknown/partial process scans.
                    if _app.core._require_codex_process_scan_known():
                        raise _app.core.ManagerError("Codex 仍在运行，未恢复配置或停止本地服务。")
                restore_attempted = True
                restored = _app.core.restore_runtime_configuration_overlay()
                if self.configuration_session.get("restorePending") and not _app.core.RUNTIME_OVERLAY_FILE.exists() and not restored.get("restored"):
                    previous = self.configuration_session.get("restore") or {}
                    session_id = previous.get("sessionId")
                    evidence = _app.core.read_json(_app.core.RUNTIME_RESTORE_STATUS_FILE, {})
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
                    _app.core.RUNTIME_OVERLAY_FILE.exists()
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
                    raise _app.core.ManagerError(
                        "临时 Codex 配置尚未完全恢复；管理器与恢复记录已保留，请处理后重试退出。"
                        + (f" {detail}" if detail else "")
                    )
            except Exception as exc:
                error = _app.core._redact_sensitive_text(exc, limit=700)
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

