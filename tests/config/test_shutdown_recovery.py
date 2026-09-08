"""Shutdown regressions: temporary files only; no live process or registry writes."""

import base64
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import agent_manager.application as app
import agent_manager.core as core


def runtime_stub(active=True):
    runtime = app.ManagerRuntime.__new__(app.ManagerRuntime)
    runtime.lock = threading.RLock()
    runtime._closed = False
    runtime._close_result = None
    runtime.configuration_session = {"active": active}
    runtime.prepare_for_restart = Mock(return_value={"prepared": True, "lingeringWorkers": []})
    runtime.resume_after_failed_restart = Mock()
    runtime.app_updates = None
    for name in (
        "_stop_update_checks", "_stop_radar_monitor", "_stop_account_auto_refresh",
        "_stop_mail_health_checks", "_stop_account_refresh_workers",
    ):
        setattr(runtime, name, Mock())
    runtime.web2api = SimpleNamespace(status=Mock(return_value={"running": True}), stop=Mock())
    runtime.oauth = SimpleNamespace(close=Mock())
    runtime.relay_portal = SimpleNamespace(close=Mock())
    return runtime


def server_stub(runtime=None):
    server = app.ManagerServer.__new__(app.ManagerServer)
    server.runtime = runtime or SimpleNamespace(close=Mock(return_value={
        "completed": True, "restorationComplete": True, "restored": True, "errors": [],
    }))
    server.shutdown_lock = threading.RLock()
    server.shutdown_started = threading.Event()
    server.shutdown_finished = threading.Event()
    server.shutdown_aborted = threading.Event()
    server.allow_forced_process_exit = False
    server.force_exit = False
    server.exit_only_requested = False
    server.quick_restart_requested = False
    server.native_window = False
    server.native_window_object = None
    for name in (
        "stop_accepting_mutations", "reopen_mutations", "stop_tray", "shutdown",
        "server_close", "serve_forever", "ensure_tray", "show_native_window",
    ):
        setattr(server, name, Mock())
    server.wait_for_mutations = Mock(return_value=True)
    return server


class ShutdownRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, value in {
            "CODEX_HOME": self.root,
            "RUNTIME_OVERLAY_FILE": self.root / "overlay.json",
            "RUNTIME_RESTORE_STATUS_FILE": self.root / "restore.json",
        }.items():
            patcher = patch.object(core, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # These are process/registry boundaries, never exercised by these tests.
        for name in ("_read_user_environment", "_remove_user_environment", "_sync_user_environment"):
            patcher = patch.object(core, name, side_effect=AssertionError("live registry access"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_title_bar_full_exit_keeps_window_visible_until_cleanup(self):
        server, window = server_stub(), Mock()
        with (
            patch.object(core, "load_settings", return_value={"appBehavior": {"closeToTray": False}}),
            patch.object(app, "request_application_shutdown", return_value=True) as full_exit,
            patch.object(app, "request_application_exit_only") as exit_only,
        ):
            self.assertFalse(app.handle_native_window_closing(server, window))
        full_exit.assert_called_once_with(server)
        exit_only.assert_not_called()
        window.hide.assert_not_called()

    def test_title_bar_to_tray_hides_only_when_icon_is_ready(self):
        for ready in (True, False):
            with self.subTest(ready=ready):
                server, window = server_stub(), Mock()
                server.ensure_tray.return_value = ready
                with (
                    patch.object(core, "load_settings", return_value={"appBehavior": {"closeToTray": True}}),
                    patch.object(app, "request_application_shutdown") as full_exit,
                ):
                    self.assertFalse(app.handle_native_window_closing(server, window))
                self.assertEqual(window.hide.call_count, int(ready))
                full_exit.assert_not_called()

    def test_close_settings_error_does_not_bypass_guarded_shutdown(self):
        with (
            patch.object(core, "load_settings", side_effect=RuntimeError("broken settings")),
            patch.object(app, "_write_shutdown_status"),
            patch.object(app, "request_application_shutdown") as close,
        ):
            server = server_stub()
            self.assertFalse(app.handle_native_window_closing(server, Mock()))
        close.assert_called_once_with(server)

    def test_repeated_close_during_restore_cannot_destroy_repair_window(self):
        server = server_stub(runtime_stub())
        server.force_exit = True
        server.shutdown_started.set()
        self.assertFalse(app.handle_native_window_closing(server, Mock()))
        server.runtime._closed = True
        self.assertIsNone(app.handle_native_window_closing(server, Mock()))

    def test_restoration_occurs_after_verified_close_before_service_stop(self):
        runtime = runtime_stub()
        calls = []
        runtime.web2api.stop.side_effect = lambda **_kwargs: calls.append("stop-gateway")
        with (
            patch.object(core, "_require_codex_process_scan_known", side_effect=[[{"pid": 12}], []]),
            patch.object(app, "_close_codex_processes_safely", side_effect=lambda: calls.append("close-codex") or {"closed": [12]}),
            patch.object(core, "restore_runtime_configuration_overlay", side_effect=lambda: calls.append("restore") or {"restored": True}),
        ):
            result = runtime.close()
            self.assertIs(runtime.close(), result)
        self.assertEqual(calls, ["close-codex", "restore", "stop-gateway"])
        self.assertTrue(result["restorationComplete"])
        self.assertTrue(runtime._closed)

    def test_running_process_after_termination_blocks_restore_and_teardown(self):
        runtime = runtime_stub()
        with (
            patch.object(core, "_require_codex_process_scan_known", return_value=[{"pid": 12}]),
            patch.object(app, "_close_codex_processes_safely", return_value={"closed": []}),
            patch.object(core, "restore_runtime_configuration_overlay") as restore,
        ):
            result = runtime.close()
        self.assertFalse(result["completed"])
        self.assertFalse(runtime._closed)
        restore.assert_not_called()
        runtime.web2api.stop.assert_not_called()
        runtime.resume_after_failed_restart.assert_called_once()

    def test_unknown_process_scan_never_becomes_empty_safe_scan(self):
        runtime = runtime_stub()
        with (
            patch.object(core, "_require_codex_process_scan_known", side_effect=core.ManagerError("unknown scan")),
            patch.object(core, "restore_runtime_configuration_overlay") as restore,
            patch.object(app, "_close_codex_processes_safely") as close,
        ):
            result = runtime.close()
        self.assertFalse(result["completed"])
        close.assert_not_called()
        restore.assert_not_called()
        runtime.web2api.stop.assert_not_called()

    def test_lingering_workers_block_restore_and_allow_retry(self):
        runtime = runtime_stub()
        runtime.prepare_for_restart.return_value = {"lingeringWorkers": ["refresh"]}
        with patch.object(core, "restore_runtime_configuration_overlay") as restore:
            result = runtime.close()
        self.assertFalse(result["completed"])
        restore.assert_not_called()
        runtime.resume_after_failed_restart.assert_called_once()

    def test_conflicts_keep_runtime_open_for_repair_and_later_retry(self):
        runtime = runtime_stub()
        with (
            patch.object(core, "_require_codex_process_scan_known", return_value=[]),
            patch.object(core, "restore_runtime_configuration_overlay", side_effect=[
                {"restored": False, "partial": True, "conflicts": ["agents/custom.toml"]},
                {"restored": True},
            ]),
        ):
            blocked = runtime.close()
            self.assertFalse(runtime._closed)
            self.assertTrue(runtime.configuration_session["recoveryOnly"])
            self.assertTrue(runtime.configuration_session["restorePending"])
            runtime.web2api.stop.assert_not_called()
            completed = runtime.close()
        self.assertFalse(blocked["completed"])
        self.assertTrue(completed["completed"])
        self.assertFalse(runtime.configuration_session["restorePending"])

    def test_retained_journal_overrules_success_claim(self):
        core.RUNTIME_OVERLAY_FILE.write_text("{}", encoding="utf-8")
        runtime = runtime_stub()
        with (
            patch.object(core, "_require_codex_process_scan_known", return_value=[]),
            patch.object(core, "restore_runtime_configuration_overlay", return_value={"restored": True}),
        ):
            result = runtime.close()
        self.assertFalse(result["restorationComplete"])
        runtime.web2api.stop.assert_not_called()

    def test_external_repair_requires_success_evidence_for_same_overlay(self):
        for matching_session in (True, False):
            with self.subTest(matching_session=matching_session):
                runtime = runtime_stub(active=False)
                runtime.configuration_session.update({"restorePending": True, "restore": {"sessionId": "failed-session"}})
                core.RUNTIME_RESTORE_STATUS_FILE.write_text(json.dumps({
                    "restored": True, "sessionId": "failed-session" if matching_session else "unrelated-session",
                }), encoding="utf-8")
                with (
                    patch.object(core, "_require_codex_process_scan_known", return_value=[]),
                    patch.object(core, "restore_runtime_configuration_overlay", return_value={"restored": False}),
                ):
                    result = runtime.close()
                self.assertEqual(result["completed"], matching_session)
                self.assertEqual(runtime.web2api.stop.call_count, int(matching_session))

    def test_worker_cleanup_exception_does_not_erase_successful_restore_evidence(self):
        runtime = runtime_stub()
        runtime._stop_account_refresh_workers.side_effect = RuntimeError("worker cleanup failed")
        with (
            patch.object(core, "_require_codex_process_scan_known", return_value=[]),
            patch.object(core, "restore_runtime_configuration_overlay", return_value={"restored": True}),
        ):
            result = runtime.close()
        self.assertTrue(result["completed"])
        self.assertTrue(result["restorationComplete"])
        self.assertTrue(result["errors"])
        runtime.web2api.stop.assert_called_once()

    def test_restore_exception_leaves_recovery_mode_and_gateway(self):
        runtime = runtime_stub()
        with (
            patch.object(core, "_require_codex_process_scan_known", return_value=[]),
            patch.object(core, "restore_runtime_configuration_overlay", side_effect=OSError("disk unavailable")),
        ):
            result = runtime.close()
        self.assertFalse(result["completed"])
        self.assertTrue(runtime.configuration_session["recoveryOnly"])
        runtime.web2api.stop.assert_not_called()

    def test_deferred_runtime_without_overlay_does_not_close_codex(self):
        runtime = runtime_stub(active=False)
        with (
            patch.object(core, "_require_codex_process_scan_known") as scan,
            patch.object(core, "restore_runtime_configuration_overlay", return_value={"restored": False}),
        ):
            result = runtime.close()
        scan.assert_not_called()
        self.assertTrue(result["restorationComplete"])
        self.assertFalse(result["restored"])

    def test_explicit_manager_only_exit_and_restart_preserve_processes_and_overlay(self):
        for exit_only in (True, False):
            with self.subTest(exit_only=exit_only):
                runtime = runtime_stub()
                with (
                    patch.object(core, "restore_runtime_configuration_overlay") as restore,
                    patch.object(core, "running_codex_processes") as scan,
                    patch.object(app, "_close_codex_processes_safely") as close,
                ):
                    result = runtime.close_for_restart(exit_only=exit_only)
                restore.assert_not_called()
                scan.assert_not_called()
                close.assert_not_called()
                self.assertTrue(result["preserved"])
                self.assertFalse(result["restorationComplete"])
                self.assertEqual(runtime.configuration_session["status"], "exited" if exit_only else "restarting")

    def test_real_overlay_restores_config_agents_and_removes_generated_role(self):
        original_config = b'model_provider = "openai"\n[agents.original]\nconfig_file = "original.toml"\n'
        original_agents = b"# Personal instructions\r\nKeep my rules.\r\n"
        managed = {
            "config.toml": (original_config, b'model_provider = "manager"\n', "config"),
            "AGENTS.md": (original_agents, b"# Managed instructions\n", "agents"),
            "agents/cam_simple_1.toml": (None, b'model = "manager"\n', "managed_agent"),
        }
        payload = {"schemaVersion": 1, "sessionId": "isolated-exit", "codexHome": str(self.root), "files": {}, "environment": {}}
        for relative, (baseline, current, kind) in managed.items():
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(current)
            payload["files"][relative] = {
                "kind": kind,
                "baseline": None if baseline is None else base64.b64encode(baseline).decode(),
                "baselineHash": core._overlay_value_hash(baseline),
                "capturedHash": core._overlay_value_hash(baseline),
                "appliedHash": core._overlay_value_hash(current),
            }
        core.RUNTIME_OVERLAY_FILE.write_text(json.dumps(payload), encoding="utf-8")
        runtime = runtime_stub()
        with (
            patch.object(core, "_require_codex_process_scan_known", return_value=[]),
            patch.object(core, "_overlay_decrypt_bytes", side_effect=lambda value: None if value is None else base64.b64decode(value)),
            patch.object(core, "backup_file"),
        ):
            result = runtime.close()
        self.assertTrue(result["restorationComplete"], result)
        self.assertEqual((self.root / "config.toml").read_bytes(), original_config)
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), original_agents)
        self.assertFalse((self.root / "agents/cam_simple_1.toml").exists())
        self.assertFalse(core.RUNTIME_OVERLAY_FILE.exists())

    def test_shutdown_false_completion_keeps_server_and_mutex(self):
        server = server_stub(SimpleNamespace(close=Mock(return_value={
            "completed": False, "restorationComplete": False, "errors": ["conflict"], "blocked": "restoration",
        })))
        with (
            patch.object(app, "_write_shutdown_status") as status,
            patch.object(app, "_cleanup_runtime") as cleanup,
        ):
            app.shutdown_application(server)
        self.assertTrue(server.shutdown_aborted.is_set())
        self.assertFalse(server.shutdown_started.is_set())
        self.assertEqual(status.call_args.args[1], "blocked-restoration")
        server.server_close.assert_not_called()
        cleanup.assert_not_called()
        server.reopen_mutations.assert_called_once()

    def test_shutdown_mutation_timeout_preserves_runtime(self):
        server = server_stub()
        server.wait_for_mutations.return_value = False
        with patch.object(app, "_write_shutdown_status"), patch.object(app, "_cleanup_runtime") as cleanup:
            app.shutdown_application(server)
        server.runtime.close.assert_not_called()
        cleanup.assert_not_called()
        self.assertTrue(server.shutdown_aborted.is_set())

    def test_shutdown_cleanup_errors_do_not_write_success_phase(self):
        server = server_stub()
        server.server_close.side_effect = OSError("port cleanup failed")
        with patch.object(app, "_write_shutdown_status") as status, patch.object(app, "_cleanup_runtime"):
            app.shutdown_application(server)
        self.assertEqual(status.call_args.args[1], "completed-with-errors")
        self.assertTrue(server.shutdown_result["restorationComplete"])
        self.assertTrue(server.shutdown_errors)

    def test_synchronous_finalizer_does_not_race_claimed_worker(self):
        server = server_stub()
        server.shutdown_started.set()
        server.shutdown_finished.set()
        with patch.object(app, "shutdown_application") as shutdown:
            app._finalize_application_server(server)
        shutdown.assert_not_called()
        server.runtime.close.assert_not_called()

    def test_browser_finalizer_uses_guarded_shutdown(self):
        server = server_stub()
        with patch.object(app, "_write_shutdown_status"), patch.object(app, "_cleanup_runtime"):
            self.assertEqual(app._finalize_application_server(server), [])
        server.runtime.close.assert_called_once()
        self.assertTrue(server.shutdown_result["restorationComplete"])

    def test_window_crash_is_not_explicit_manager_only_exit(self):
        server = server_stub(runtime_stub())
        with (
            patch.dict(app.os.environ, {app.NATIVE_WINDOW_RECOVERY_ENV: "1"}),
            patch.object(app, "_write_shutdown_status") as status,
            patch.object(app, "_report_startup_error"),
            patch.object(app, "request_application_restart") as restart,
        ):
            self.assertFalse(app._recover_unexpected_native_window_exit(server))
        self.assertFalse(server.exit_only_requested)
        self.assertEqual(status.call_args.args[1], "native-window-recovery-required")
        restart.assert_not_called()

    def test_idle_browser_exit_uses_normal_shutdown(self):
        server = server_stub()
        server.last_request_at = 0
        with patch.object(app.time, "sleep"), patch.object(app, "request_application_shutdown") as shutdown:
            app.idle_watchdog(server, timeout_seconds=0)
        shutdown.assert_called_once_with(server)

    def test_watchdog_timeout_cannot_force_exit_or_release_mutex(self):
        server = server_stub()
        with (
            patch.object(app, "FORCED_EXIT_TIMEOUT_SECONDS", 0),
            patch.object(app, "_write_shutdown_status") as status,
            patch.object(app, "_cleanup_runtime") as cleanup,
            patch.object(app.os, "_exit") as exit_process,
        ):
            app._forced_exit_watchdog(server)
        exit_process.assert_not_called()
        cleanup.assert_not_called()
        self.assertEqual(status.call_args.args[1], "shutdown-timeout")

    def test_watchdog_preserves_completion_evidence_and_nonzero_cleanup_error(self):
        server = server_stub()
        server.shutdown_finished.set()
        server.shutdown_result = {"completed": True, "restorationComplete": True}
        server.shutdown_errors = ["cleanup failed"]
        with (
            patch.object(app.time, "sleep"),
            patch.object(app, "_write_shutdown_status") as status,
            patch.object(app, "_cleanup_runtime"),
            patch.object(app.os, "_exit") as exit_process,
        ):
            app._forced_exit_watchdog(server)
        status.assert_not_called()
        exit_process.assert_called_once_with(1)

    def test_shutdown_status_exports_verified_restoration(self):
        server = server_stub()
        server.runtime_nonce = "instance-proof"
        server.shutdown_result = {"restorationComplete": True, "preserved": False}
        with patch.object(core, "atomic_write_json") as write:
            app._write_shutdown_status(server, "completed")
        payload = write.call_args.args[1]
        self.assertTrue(payload["restorationComplete"])
        self.assertFalse(payload["preserved"])
        self.assertEqual(payload["runtimeNonce"], "instance-proof")

    def test_tray_ready_icon_is_owned_by_daemon_thread_and_stopped(self):
        class Icon:
            visible = False
            def __init__(self, *_args):
                self.stopped = threading.Event()
            def run(self, setup=None):
                setup(self)
                self.stopped.wait(3)
            def stop(self):
                self.stopped.set()

        server = server_stub()
        server.native_window = True
        server.tray = None
        server.tray_thread = None
        server.tray_error = None
        server.tray_lock = threading.RLock()
        server.ensure_tray = app.ManagerServer.ensure_tray.__get__(server)
        server.stop_tray = app.ManagerServer.stop_tray.__get__(server)
        menu = Mock()
        menu.SEPARATOR = object()
        with patch.dict("sys.modules", {
            "pystray": SimpleNamespace(Icon=Icon, Menu=menu, MenuItem=Mock()),
            "PIL": SimpleNamespace(Image=SimpleNamespace(open=Mock(return_value=Mock()))),
        }):
            try:
                self.assertTrue(server.ensure_tray())
                self.assertTrue(server.tray_status()["ready"])
                thread = server.tray_thread
                self.assertTrue(thread.daemon)
            finally:
                server.stop_tray()
        self.assertFalse(thread.is_alive())
        self.assertIsNone(server.tray)

    def test_tray_async_failure_does_not_claim_ready(self):
        class BrokenIcon:
            visible = False
            def __init__(self, *_args):
                pass
            def run(self, setup=None):
                raise RuntimeError("tray loop failed")
            def stop(self):
                pass

        server = server_stub()
        server.native_window = True
        server.tray = None
        server.tray_thread = None
        server.tray_error = None
        server.tray_lock = threading.RLock()
        server.ensure_tray = app.ManagerServer.ensure_tray.__get__(server)
        menu = Mock()
        menu.SEPARATOR = object()
        with patch.dict("sys.modules", {
            "pystray": SimpleNamespace(Icon=BrokenIcon, Menu=menu, MenuItem=Mock()),
            "PIL": SimpleNamespace(Image=SimpleNamespace(open=Mock(return_value=Mock()))),
        }):
            self.assertFalse(server.ensure_tray())
        self.assertFalse(server.tray_status()["ready"])
        self.assertIn("tray loop failed", server.tray_error)


if __name__ == "__main__":
    unittest.main()
