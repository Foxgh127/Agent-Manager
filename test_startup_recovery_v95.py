"""Isolated startup/recovery regressions for Agent Manager 9.5."""

from types import SimpleNamespace
import json
import unittest
import threading
from unittest.mock import Mock, patch

import agent_manager_app as app
import agent_manager_core as core


def _empty_diagnostics() -> dict:
    return {
        "schemaVersion": 2,
        "checkedAt": None,
        "checks": [],
        "summary": {"ok": 0, "warning": 0, "error": 0},
        "healthy": None,
        "repairableCount": 0,
        "enableRepair": False,
        "configurationStatus": None,
        "cached": True,
        "needsManualCheck": True,
    }


class _FakeWeb2API:
    last_error = None

    def status(self) -> dict:
        return {"running": False}

    def stop(self, disable: bool = False) -> dict:
        return {"stopped": True, "disable": disable}


class _FakeOAuth:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def state(self) -> dict:
        return {"status": "idle"}


class _FakeRelayPortal:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def public_state(self) -> dict:
        return {"status": "idle"}


class StartupRecoveryV95Tests(unittest.TestCase):
    def test_preflight_failure_does_not_claim_that_a_rollback_occurred(self):
        runtime = app.ManagerRuntime.__new__(app.ManagerRuntime)
        runtime.switch_operation_lock = threading.RLock()
        runtime.switch_operations = {}
        runtime.begin_switch_operation(
            "preflight-test",
            target_id="relay1",
            target_name="Relay",
            target_kind="provider",
        )
        runtime.update_switch_operation(
            "preflight-test", {"phase": "preflight", "progress": 4}
        )
        result = runtime.finish_switch_operation(
            "preflight-test", error=core.ManagerError("Cannot locate launcher")
        )
        self.assertEqual(result["recoveryState"], "unchanged")
        self.assertIn("未改动", result["message"])
        self.assertNotIn("已安全恢复", result["message"])

    def test_normal_startup_preserves_recognized_external_selection_before_overlay(
        self,
    ):
        defaults = core._initial_settings()
        calls = []
        selection = {
            "preserveCurrent": True,
            "sourceId": "provider:cockpit-imported",
            "recognized": True,
            "message": "已识别 Cockpit 当前来源并保持使用。",
        }
        with (
            patch.object(app, "Web2APIManager", _FakeWeb2API),
            patch.object(app, "OAuthDeviceLogin", _FakeOAuth),
            patch.object(app.relay_portal, "RelayPortalService", _FakeRelayPortal),
            patch.object(app.radar, "RadarService", return_value=object()),
            patch.object(
                app.live_selection,
                "reconcile_startup_selection",
                side_effect=lambda: calls.append("reconcile") or selection,
            ),
            patch.object(
                core,
                "_runtime_overlay_read",
                side_effect=lambda: calls.append("overlay-read") or {"active": True},
            ),
            patch.object(
                core,
                "adopt_runtime_configuration_overlay",
                side_effect=lambda: calls.append("adopt") or {"active": True},
            ),
            patch.object(
                core,
                "begin_runtime_configuration_overlay",
                side_effect=lambda: calls.append("begin") or {"active": True},
            ) as begin,
            patch.object(
                core,
                "apply_configuration",
                side_effect=lambda *_args: calls.append("apply") or {},
            ) as apply_configuration,
            patch.object(core, "load_settings", return_value=defaults),
            patch.object(core, "running_codex_processes", return_value=[{"pid": "77"}]),
            patch.object(
                app, "_restart_configuration_fingerprint", return_value="fingerprint"
            ),
            patch.object(app.ManagerRuntime, "_start_account_auto_refresh"),
            patch.object(app.ManagerRuntime, "_start_mail_health_checks"),
            patch.object(app.ManagerRuntime, "_start_update_checks"),
        ):
            runtime = app.ManagerRuntime(defer_configuration=True)
            session = runtime.activate_configuration_session()

        self.assertEqual(calls, ["reconcile", "overlay-read", "adopt"])
        begin.assert_not_called()
        apply_configuration.assert_not_called()
        self.assertTrue(session["active"])
        self.assertTrue(session["externalSelectionPreserved"])
        self.assertEqual(session["externalSelection"], selection)
        self.assertEqual(session["message"], selection["message"])
        self.assertFalse(session["restartTiming"]["configurationReapplied"])

    def test_quick_restart_does_not_reconcile_external_selection(self):
        defaults = core._initial_settings()
        with (
            patch.object(app, "Web2APIManager", _FakeWeb2API),
            patch.object(app, "OAuthDeviceLogin", _FakeOAuth),
            patch.object(app.relay_portal, "RelayPortalService", _FakeRelayPortal),
            patch.object(app.radar, "RadarService", return_value=object()),
            patch.object(
                app.live_selection, "reconcile_startup_selection"
            ) as reconcile,
            patch.object(
                core,
                "adopt_runtime_configuration_overlay",
                return_value={"active": True},
            ) as adopt,
            patch.object(core, "_runtime_overlay_read") as overlay_read,
            patch.object(core, "apply_configuration") as apply_configuration,
            patch.object(core, "load_settings", return_value=defaults),
            patch.object(
                app, "_quick_restart_can_adopt_without_apply", return_value=True
            ),
            patch.object(
                app, "_restart_configuration_fingerprint", return_value="fingerprint"
            ),
            patch.object(app.ManagerRuntime, "_start_account_auto_refresh"),
            patch.object(app.ManagerRuntime, "_start_mail_health_checks"),
            patch.object(app.ManagerRuntime, "_start_update_checks"),
        ):
            runtime = app.ManagerRuntime(
                quick_restart=True,
                defer_configuration=True,
                restart_handoff={"protocolVersion": 2},
            )
            session = runtime.activate_configuration_session()

        reconcile.assert_not_called()
        overlay_read.assert_not_called()
        adopt.assert_called_once_with()
        apply_configuration.assert_not_called()
        self.assertTrue(session["active"])
        self.assertFalse(session["externalSelectionPreserved"])

    def test_restart_preserves_external_selection_even_when_build_requires_apply(self):
        defaults = core._initial_settings()
        for handoff in (
            {"protocolVersion": 2, "preserveExternalSelection": True},
            {"protocolVersion": 2, "sourceOverlayCurrent": False},
        ):
            with (
                patch.object(
                    app.live_selection,
                    "preserve_live_configuration_on_restart",
                    return_value=True,
                ),
                patch.object(app, "Web2APIManager", _FakeWeb2API),
                patch.object(app, "OAuthDeviceLogin", _FakeOAuth),
                patch.object(app.relay_portal, "RelayPortalService", _FakeRelayPortal),
                patch.object(app.radar, "RadarService", return_value=object()),
                patch.object(
                    app.live_selection, "reconcile_startup_selection"
                ) as reconcile,
                patch.object(
                    core,
                    "adopt_runtime_configuration_overlay",
                    return_value={"active": True},
                ),
                patch.object(
                    core, "_runtime_overlay_read", return_value={"active": True}
                ),
                patch.object(core, "apply_configuration") as apply,
                patch.object(core, "load_settings", return_value=defaults),
                patch.object(
                    app, "_quick_restart_can_adopt_without_apply", return_value=False
                ),
                patch.object(
                    app, "_restart_configuration_fingerprint", return_value="test"
                ),
                patch.object(app.ManagerRuntime, "_start_account_auto_refresh"),
                patch.object(app.ManagerRuntime, "_start_mail_health_checks"),
                patch.object(app.ManagerRuntime, "_start_update_checks"),
            ):
                runtime = app.ManagerRuntime(
                    quick_restart=True,
                    defer_configuration=True,
                    restart_handoff=handoff,
                )
                session = runtime.activate_configuration_session()
            reconcile.assert_not_called()
            apply.assert_not_called()
            self.assertTrue(session["active"])
            self.assertTrue(session["externalSelectionPreserved"])
            self.assertFalse(session["restartTiming"]["configurationReapplied"])

    def test_recursive_process_preflight_still_returns_repair_first_paint(self):
        defaults = core._initial_settings()
        public_defaults = json.loads(json.dumps(defaults))
        with (
            patch.object(app, "Web2APIManager", _FakeWeb2API),
            patch.object(app, "OAuthDeviceLogin", _FakeOAuth),
            patch.object(app.relay_portal, "RelayPortalService", _FakeRelayPortal),
            patch.object(app.radar, "RadarService", return_value=object()),
            patch.object(
                app.live_selection,
                "reconcile_startup_selection",
                return_value={
                    "preserveCurrent": False,
                    "sourceId": "",
                    "recognized": False,
                    "message": "",
                },
            ),
            patch.object(
                core,
                "begin_runtime_configuration_overlay",
                return_value={"active": True},
            ),
            patch.object(
                core, "apply_configuration", return_value={"gatewayRequired": False}
            ),
            patch.object(core, "load_settings", return_value=defaults),
            patch.object(
                core, "_public_settings_projection", return_value=public_defaults
            ),
            patch.object(
                core,
                "running_codex_processes",
                side_effect=RecursionError("maximum recursion depth exceeded"),
            ) as process_scan,
            patch.object(
                core,
                "restore_runtime_configuration_overlay",
                return_value={"restored": True},
            ) as restore,
            patch.object(
                core,
                "public_state",
                side_effect=RecursionError("maximum recursion depth exceeded"),
            ),
            patch.object(
                app.maintenance,
                "run_emergency_checks",
                return_value=_empty_diagnostics(),
            ),
            patch.object(core, "close_codex_processes") as close_codex,
        ):
            runtime = app.ManagerRuntime(defer_configuration=True)
            session = runtime.activate_configuration_session()
            state = runtime.state()

        self.assertEqual(process_scan.call_count, 1)
        self.assertEqual(session["status"], "error")
        self.assertTrue(session["recoveryOnly"])
        self.assertTrue(session["recoverable"])
        self.assertEqual(session["restore"], {"restored": True})
        restore.assert_called_once_with(force=True)
        close_codex.assert_not_called()
        self.assertTrue(state["configurationSession"]["recoveryOnly"])
        self.assertTrue(state["status"]["recoveryOnly"])
        self.assertEqual(state["settings"]["accounts"], [])
        self.assertFalse(state["auth"]["processScanKnown"])
        self.assertIn("maximum recursion depth", state["modelError"])

    def test_visible_bootstrap_failure_constructs_recovery_runtime(self):
        runtime = Mock()
        with (
            patch.object(
                core, "ensure_state", side_effect=core.ManagerError("broken settings")
            ),
            patch.object(
                app, "ManagerRuntime", return_value=runtime
            ) as runtime_factory,
        ):
            result = app._create_startup_runtime(
                open_window=True,
                quick_restart=False,
                restart_handoff=None,
            )

        self.assertIs(result, runtime)
        runtime_factory.assert_called_once_with(
            quick_restart=False,
            defer_configuration=True,
            restart_handoff=None,
        )
        runtime.enter_startup_recovery.assert_called_once()
        self.assertIn(
            "broken settings", str(runtime.enter_startup_recovery.call_args.args[0])
        )

    def test_headless_bootstrap_failure_remains_fail_fast(self):
        with (
            patch.object(
                core, "ensure_state", side_effect=core.ManagerError("broken settings")
            ),
            patch.object(app, "ManagerRuntime") as runtime_factory,
        ):
            with self.assertRaisesRegex(core.ManagerError, "broken settings"):
                app._create_startup_runtime(
                    open_window=False,
                    quick_restart=False,
                    restart_handoff=None,
                )
        runtime_factory.assert_not_called()

    def test_quick_restart_bootstrap_failure_leaves_source_manager_in_charge(self):
        with (
            patch.object(
                core, "ensure_state", side_effect=core.ManagerError("broken settings")
            ),
            patch.object(app, "ManagerRuntime") as runtime_factory,
        ):
            with self.assertRaisesRegex(core.ManagerError, "broken settings"):
                app._create_startup_runtime(
                    open_window=True,
                    quick_restart=True,
                    restart_handoff={"protocolVersion": 2},
                )
        runtime_factory.assert_not_called()

    def test_ready_publication_failure_does_not_shutdown_active_codex_session(self):
        server = SimpleNamespace(
            runtime=SimpleNamespace(configuration_session={"active": True}),
            force_exit=False,
        )
        with (
            patch.object(app, "_write_shutdown_status") as status,
            patch.object(app, "_report_startup_error") as report,
            patch.object(app, "request_application_shutdown") as shutdown,
            patch.object(app, "request_application_exit_only") as exit_only,
        ):
            app._handle_startup_activation_failure(
                server,
                "native-window-activation-error",
                OSError("runtime discovery write failed"),
            )

        self.assertFalse(server.force_exit)
        status.assert_called_once()
        self.assertIn("当前 Codex 会话保持可用", report.call_args.args[0])
        shutdown.assert_not_called()
        exit_only.assert_not_called()

    def test_non_recovery_state_errors_are_not_hidden(self):
        runtime = app.ManagerRuntime.__new__(app.ManagerRuntime)
        runtime.configuration_session = {"status": "active", "recoveryOnly": False}
        with patch.object(core, "public_state", side_effect=RuntimeError("unexpected")):
            with self.assertRaisesRegex(RuntimeError, "unexpected"):
                runtime.state()


if __name__ == "__main__":
    unittest.main()
