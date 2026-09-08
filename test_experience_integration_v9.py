import threading
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import agent_manager_app as app
import agent_manager_core as core


class ExperienceIntegrationTests(unittest.TestCase):
    def test_appearance_is_persisted_independently_of_browser_origin(self):
        settings = {"appBehavior": {"closeToTray": True, "quotaRefreshMinutes": 30}}
        with patch.object(core, "load_settings", return_value=settings), patch.object(core, "save_settings") as save, patch.object(core, "_settings_file_lock", return_value=nullcontext()):
            result = core.save_app_behavior({"appearance": "light"})
            self.assertEqual(result["appearance"], "light")
            self.assertTrue(result["closeToTray"])
            self.assertEqual(result["quotaRefreshMinutes"], 30)
            save.assert_called_once_with(settings)

    def test_invalid_appearance_is_rejected_before_save(self):
        with patch.object(core, "load_settings", return_value={}), patch.object(core, "save_settings") as save, patch.object(core, "_settings_file_lock", return_value=nullcontext()):
            with self.assertRaises(core.ManagerError):
                core.save_app_behavior({"appearance": "neon"})
            save.assert_not_called()

    def test_removed_answer_preferences_remain_owned_by_codex(self):
        settings = {"runtimeTuning": core._default_runtime_tuning()}
        doc = {"personality": "friendly", "model_verbosity": "high", "custom": True}
        tuning = core._normalize_runtime_tuning_update(settings, {"webSearch": "live"})
        core._apply_root_runtime_tuning(doc, tuning)
        self.assertEqual(doc["personality"], "friendly")
        self.assertEqual(doc["model_verbosity"], "high")
        tuning = core._normalize_runtime_tuning_update(settings, {"personality": "pragmatic"})
        core._apply_root_runtime_tuning(doc, tuning)
        self.assertEqual(doc["personality"], "friendly")
        self.assertEqual(doc["model_verbosity"], "high")
        tuning = core._normalize_runtime_tuning_update(settings, {"verbosity": ""})
        core._apply_root_runtime_tuning(doc, tuning)
        self.assertEqual(doc["model_verbosity"], "high")
        self.assertTrue(doc["custom"])
        legacy = core._normalize_runtime_tuning({"managedFields": ["personality", "verbosity"], "personality": "pragmatic", "verbosity": "low"})
        self.assertEqual(legacy["managedFields"], [])
        core._apply_root_runtime_tuning(doc, legacy)
        self.assertEqual(doc["personality"], "friendly")
        self.assertEqual(doc["model_verbosity"], "high")

    def test_old_broad_owner_does_not_adopt_new_preferences(self):
        tuning = core._normalize_runtime_tuning({"configManaged": True, "personality": "pragmatic", "verbosity": "high", "webSearch": "live"})
        self.assertEqual(tuning["managedFields"], ["webSearch"])

    def test_invalid_answer_option_rejected(self):
        with self.assertRaises(core.ManagerError):
            core._normalize_runtime_tuning_update({}, {"verbosity": "extreme"})

    def test_overlay_can_recover_after_lock_error_and_does_not_restart_codex(self):
        runtime = object.__new__(app.ManagerRuntime)
        runtime.lock = threading.RLock()
        runtime._closed = False
        runtime._configuration_activation_started = True
        runtime.configuration_session = {"status": "error", "active": False, "error": "locked"}
        runtime.last_error = "locked"
        runtime.quick_restart = False
        runtime.defer_configuration = True
        runtime.web2api = SimpleNamespace(status=lambda: {"running": False})
        runtime._start_account_auto_refresh = Mock()
        runtime._start_mail_health_checks = Mock()
        runtime._start_update_checks = Mock()
        with (
            patch.object(app.live_selection, "reconcile_startup_selection", return_value={"preserveCurrent": False}),
            patch.object(core, "begin_runtime_configuration_overlay", return_value={"active": True}) as begin,
            patch.object(core, "apply_configuration", return_value={}) as apply,
            patch.object(core, "load_settings", return_value={}),
            patch.object(core, "running_codex_processes", return_value=[{"pid": "test"}]),
            patch.object(core, "close_codex_processes") as close,
        ):
            self.assertFalse(runtime.activate_configuration_session()["active"])
            begin.assert_not_called()
            self.assertTrue(runtime.activate_configuration_session(retry=True)["active"])
            runtime.activate_configuration_session(retry=True)
            begin.assert_called_once()
            apply.assert_called_once_with(False)
            close.assert_not_called()
        self.assertIsNone(runtime.last_error)


if __name__ == "__main__":
    unittest.main()
