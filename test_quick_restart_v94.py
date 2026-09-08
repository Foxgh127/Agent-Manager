"""Restart protocol regressions; all process and runtime mutations are mocked."""

from pathlib import Path
from types import SimpleNamespace
import tempfile
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch

import agent_manager_app as app
import agent_manager_core as core


class QuickRestartProtocolTests(unittest.TestCase):
    def setUp(self):
        probe = patch.object(
            app.live_selection,
            "preserve_live_configuration_on_restart",
            return_value=False,
        )
        probe.start()
        self.addCleanup(probe.stop)

    def test_fast_adoption_requires_same_build_saved_inputs_and_owned_overlay(self):
        handoff = dict(
            protocolVersion=2,
            sourceBuild="same",
            targetBuild="same",
            sourceOverlayCurrent=True,
            sourceStateFingerprint="inputs",
        )
        with (
            patch.object(
                app, "_current_manager_build_fingerprint", return_value="same"
            ),
            patch.object(
                app, "_restart_configuration_fingerprint", return_value="inputs"
            ),
            patch.object(
                app, "_runtime_overlay_matches_last_apply", return_value=True
            ) as overlay,
        ):
            self.assertTrue(app._quick_restart_can_adopt_without_apply(handoff))
            for change in (
                {"protocolVersion": 1},
                {"sourceBuild": "old"},
                {"targetBuild": "new"},
                {"sourceOverlayCurrent": False},
                {"sourceStateFingerprint": ""},
                {"sourceStateFingerprint": "changed"},
            ):
                self.assertFalse(
                    app._quick_restart_can_adopt_without_apply({**handoff, **change})
                )
            overlay.return_value = False
            self.assertFalse(app._quick_restart_can_adopt_without_apply(handoff))

    def test_saved_input_fingerprint_detects_settings_and_catalog_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(core, "SETTINGS_FILE", root / "settings.json"),
                patch.object(core, "MODELS_CACHE_FILE", root / "models.json"),
            ):
                original = app._restart_configuration_fingerprint()
                core.SETTINGS_FILE.write_text("{}", encoding="utf-8")
                configured = app._restart_configuration_fingerprint()
                core.MODELS_CACHE_FILE.write_text('{"models":[]}', encoding="utf-8")
                self.assertNotEqual(original, configured)
                self.assertNotEqual(
                    configured, app._restart_configuration_fingerprint()
                )

    def test_catalog_freshness_alone_does_not_require_configuration_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(core, "SETTINGS_FILE", root / "settings.json"),
                patch.object(core, "MODELS_CACHE_FILE", root / "models.json"),
            ):
                core.SETTINGS_FILE.write_text("{}", encoding="utf-8")
                catalog = {
                    "client_version": "0.153.4",
                    "fetched_at": "old",
                    "etag": "old",
                    "models": [{"id": "test", "context_window": 1000}],
                }
                core.MODELS_CACHE_FILE.write_text(json.dumps(catalog), encoding="utf-8")
                original = app._restart_configuration_fingerprint()
                catalog.update(fetched_at="new", etag="new")
                core.MODELS_CACHE_FILE.write_text(
                    json.dumps(catalog, indent=2), encoding="utf-8"
                )
                self.assertEqual(original, app._restart_configuration_fingerprint())
                catalog["models"][0]["context_window"] = 2000
                core.MODELS_CACHE_FILE.write_text(json.dumps(catalog), encoding="utf-8")
                self.assertNotEqual(original, app._restart_configuration_fingerprint())

    def fake_server(self):
        return SimpleNamespace(
            runtime=SimpleNamespace(
                configuration_session={
                    "active": True,
                    "configurationStateFingerprint": "inputs",
                },
                prepare_for_restart=Mock(return_value={"elapsedMs": 1}),
                resume_after_failed_restart=Mock(),
            ),
            claim_shutdown=Mock(return_value=True),
            runtime_nonce="old",
            shutdown_lock=threading.Lock(),
            shutdown_started=threading.Event(),
            reopen_mutations=Mock(),
            force_exit=False,
        )

    def test_replacement_prewarms_before_shutdown_and_deadline_starts_later(self):
        server = self.fake_server()
        events = []
        server.runtime.prepare_for_restart.side_effect = lambda: (
            events.append("prepare") or {}
        )
        with (
            patch.object(
                app,
                "_manager_restart_command",
                return_value=(["python", "app.py"], Path.cwd()),
            ),
            patch.object(
                app, "_current_manager_build_fingerprint", return_value="build"
            ),
            patch.object(app, "_runtime_overlay_matches_last_apply", return_value=True),
            patch.object(app, "_write_restart_handoff") as write,
            patch.object(
                app,
                "_start_restarted_manager",
                side_effect=lambda *a, **kw: events.append("spawn") or {},
            ),
            patch.object(
                app,
                "_wait_for_restart_child_staged",
                side_effect=lambda *a: events.append("stage") or {},
            ),
            patch.object(app, "_restart_handoff_payload", return_value={}),
            patch.object(app, "_arm_restart_readiness_deadline") as arm,
            patch.object(app.threading, "Thread") as worker,
        ):
            self.assertTrue(app.request_application_restart(server))
            self.assertEqual(events, ["spawn", "prepare", "stage"])
            self.assertEqual(
                write.call_args.args[1]["sourceStateFingerprint"], "inputs"
            )
            arm.assert_not_called()
            worker.return_value.start.assert_called_once()
            server.runtime.resume_after_failed_restart.assert_not_called()

    def test_preflight_failure_keeps_old_runtime_available(self):
        server = self.fake_server()
        server.shutdown_started.set()
        with tempfile.TemporaryDirectory() as directory:
            handoff = Path(directory) / "handoff.json"
            handoff.write_text("{}", encoding="utf-8")
            with (
                patch.object(app, "RESTART_HANDOFF_FILE", handoff),
                patch.object(
                    app,
                    "_manager_restart_command",
                    return_value=(["python"], Path.cwd()),
                ),
                patch.object(
                    app, "_current_manager_build_fingerprint", return_value="build"
                ),
                patch.object(
                    app, "_runtime_overlay_matches_last_apply", return_value=True
                ),
                patch.object(app, "_write_restart_handoff"),
                patch.object(app, "_start_restarted_manager", return_value={}),
                patch.object(
                    app,
                    "_wait_for_restart_child_staged",
                    side_effect=core.ManagerError("not ready"),
                ),
                patch.object(app, "_restart_handoff_payload", return_value={}),
                patch.object(app, "_write_shutdown_status"),
                patch.object(app.threading, "Thread") as worker,
            ):
                with self.assertRaisesRegex(core.ManagerError, "not ready"):
                    app.request_application_restart(server)
                self.assertFalse(handoff.exists())
                self.assertFalse(server.shutdown_started.is_set())
                self.assertFalse(server.force_exit)
                server.runtime.resume_after_failed_restart.assert_called_once()
                server.reopen_mutations.assert_called_once()
                worker.assert_not_called()

    def test_expired_replacement_deadline_exits_manager_only(self):
        server = SimpleNamespace(
            ui_ready=threading.Event(),
            shutdown_started=threading.Event(),
            allow_forced_process_exit=True,
        )
        with (
            patch.object(app, "request_application_exit_only") as exit_only,
            patch.object(app, "_write_shutdown_status"),
        ):
            app._restart_readiness_watchdog(
                server, {"readyDeadlineEpoch": time.time() - 1}
            )
            exit_only.assert_called_once_with(server)
            self.assertFalse(server.allow_forced_process_exit)

    def test_cancelled_child_never_migrates_state_or_constructs_runtime(self):
        with (
            patch.object(app, "_consume_restart_handoff", return_value=True),
            patch.object(
                app, "_restart_handoff_payload", return_value={"protocolVersion": 2}
            ),
            patch.object(app, "_mark_restart_child_staged", return_value={}),
            patch.object(app, "_acquire_instance_mutex", return_value=False),
            patch.object(app, "_restart_handoff_is_valid", return_value=False),
            patch.object(core, "ensure_state") as ensure,
            patch.object(app, "ManagerRuntime") as runtime,
        ):
            self.assertEqual(app.run_server(0, True, True, "cancelled"), 1)
            ensure.assert_not_called()
            runtime.assert_not_called()

    def test_failed_preflight_resumes_stop_flags(self):
        runtime = app.ManagerRuntime.__new__(app.ManagerRuntime)
        runtime.lock = threading.RLock()
        runtime._closed = False
        runtime._restart_prepared = True
        runtime.configuration_session = {"active": True}
        for key in (
            "update_check_stop",
            "account_refresh_stop",
            "radar_monitor_stop",
            "account_auto_refresh_stop",
            "account_auto_refresh_wake",
            "mail_health_stop",
            "mail_health_wake",
        ):
            setattr(runtime, key, threading.Event())
            getattr(runtime, key).set()
        with (
            patch.object(runtime, "_start_account_auto_refresh"),
            patch.object(runtime, "_start_mail_health_checks"),
            patch.object(runtime, "_start_update_checks"),
        ):
            runtime.resume_after_failed_restart()
        for key in (
            "update_check_stop",
            "account_refresh_stop",
            "radar_monitor_stop",
            "account_auto_refresh_stop",
            "mail_health_stop",
        ):
            self.assertFalse(getattr(runtime, key).is_set(), key)


if __name__ == "__main__":
    unittest.main()
