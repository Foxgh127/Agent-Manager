"""Startup health regressions; all filesystem and CLI access is isolated."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import agent_manager.application as app
import agent_manager.core as core
import agent_manager.codex.maintenance as maintenance


class StartupHealthConsistencyTests(unittest.TestCase):
    def test_native_policy_without_marker_is_applied_and_drift_remains_visible(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agents = root / "AGENTS.md"
            agents.write_text("User instructions\n", encoding="utf-8")
            settings = {"modelWorkspace": {}, "activeStrategyId": "adaptive"}
            with (
                patch.object(core, "AGENTS_FILE", agents),
                patch.object(core, "read_toml", return_value={"model": "local"}),
                patch.object(core, "build_codex_config", return_value='model = "local"'),
                patch.object(core, "build_agents_file", return_value="User instructions\n") as render,
                patch.object(core, "selected_model_records", return_value=[]),
            ):
                self.assertTrue(core.configuration_status(settings)["fullyApplied"])
                render.return_value = "Changed routing policy\n"
                self.assertFalse(core.configuration_status(settings)["fullyApplied"])

    def test_cached_configuration_warning_is_rechecked_but_other_errors_survive(self):
        cached = {"checks": [
            {"id": "generated_configuration", "status": "warning", "autoFixable": True,
             "action": "reapply_configuration", "data": {"fullyApplied": False}},
            {"id": "state_directory", "status": "error", "autoFixable": False},
        ]}
        with (
            patch.object(maintenance, "_runtime_overlay_health", return_value={"status": "ok", "detail": "live"}),
            patch.object(core, "configuration_status", return_value={"fullyApplied": True}) as check,
        ):
            result = maintenance._normalize_cached_diagnostics(cached)
            self.assertEqual(result["checks"][0]["status"], "ok")
            self.assertFalse(result["enableRepair"])
            self.assertFalse(result["healthy"])
            self.assertTrue(result["stale"])
            self.assertEqual(result["summary"]["error"], 1)
            check.return_value = {"fullyApplied": False}
            self.assertTrue(maintenance._normalize_cached_diagnostics(cached)["enableRepair"])
            check.side_effect = core.ManagerError("invalid TOML")
            failed = maintenance._normalize_cached_diagnostics(cached)
            self.assertEqual(failed["checks"][0]["status"], "error")
            self.assertFalse(failed["checks"][0]["autoFixable"])

    def test_activation_finishing_during_state_read_keeps_polling_required(self):
        runtime = app.ManagerRuntime.__new__(app.ManagerRuntime)
        runtime.configuration_session = {"status": "starting", "active": False}
        runtime.account_refresh_lock = threading.RLock()
        for name in ("validation", "account_refresh_status", "account_auto_refresh_status",
                     "mail_health_status", "update_check_status", "radar_monitor_status"):
            setattr(runtime, name, {})
        runtime.web2api = SimpleNamespace(status=lambda: {})
        runtime.oauth = SimpleNamespace(state=lambda: {})
        runtime.relay_portal = SimpleNamespace(public_state=lambda: {})
        def read():
            runtime.configuration_session.update({"status": "active", "active": True})
            return {"status": {"fullyApplied": False}}
        with (
            patch.object(core, "public_state", side_effect=read),
            patch.object(maintenance, "run_emergency_checks", return_value={}),
        ):
            state = runtime.state()
        self.assertEqual(runtime.configuration_session["status"], "active")
        self.assertEqual(state["configurationSession"]["status"], "starting")

    def test_configuration_read_waits_for_apply_transaction(self):
        entered = threading.Event()
        done = threading.Event()
        with patch("agent_manager.core.configuration._configuration_status_locked",
                   side_effect=lambda settings: entered.set() or {}) as read:
            with core.CONFIG_FILE_LOCK:
                thread = threading.Thread(target=lambda: (core.configuration_status({}), done.set()))
                thread.start()
                self.assertFalse(entered.wait(0.05))
            self.assertTrue(done.wait(1))
            thread.join(1)
            read.assert_called_once()

    def test_public_state_does_not_scan_session_history(self):
        with (
            patch.object(core, "load_settings", return_value={}),
            patch.object(core, "_public_settings_projection", return_value={}),
            patch.object(core, "discover_agents", return_value=[]),
            patch.object(core, "local_model_catalog", return_value=[]),
            patch.object(core, "_public_connections_state", return_value={
                "modelSources": [], "selectedModelKeys": [], "effectiveSubagentRouting": {}, "auth": {}}),
            patch.object(core, "subagent_runtime_summary", return_value={}),
            patch.object(core, "codex_version", return_value="test"),
            patch.object(core, "configuration_status", return_value={}),
            patch.object(core, "history_inventory", side_effect=AssertionError("full scan")) as scan,
        ):
            self.assertTrue(core.public_state()["historySummary"]["deferred"])
            scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
