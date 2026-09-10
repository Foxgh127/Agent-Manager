"""Lifecycle speed/semantics fixtures; never touch live Codex or user state."""

import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import agent_manager.application as app
import agent_manager.core as core
from tests.config.test_shutdown_recovery import runtime_stub, server_stub


class LifecycleShutdownTests(unittest.TestCase):
    def service_runtime(self, callback):
        runtime = runtime_stub()
        for name in (
            "_stop_update_checks", "_stop_radar_monitor", "_stop_account_auto_refresh",
            "_stop_mail_health_checks", "_stop_account_refresh_workers",
        ):
            setattr(runtime, name, callback)
        runtime.app_updates = SimpleNamespace(close=callback)
        runtime.web2api = SimpleNamespace(server=object(), stop=callback, status=Mock())
        runtime.oauth = SimpleNamespace(close=callback)
        runtime.relay_portal = SimpleNamespace(close=callback)
        return runtime

    def test_independent_service_stops_overlap_and_do_not_hold_runtime_lock(self):
        # All nine callbacks must enter before any one can complete. A serial
        # shutdown or a join while holding runtime.lock breaks this barrier.
        barrier = threading.Barrier(9, timeout=2)
        unlocked = []

        def stop(**_kwargs):
            acquired = runtime.lock.acquire(blocking=False)
            unlocked.append(acquired)
            if acquired:
                runtime.lock.release()
            barrier.wait()

        runtime = self.service_runtime(stop)
        result = runtime._close_services()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["lingeringWorkers"], [])
        self.assertEqual(unlocked, [True] * 9)
        self.assertEqual(len(result["timings"]), 9)
        runtime.web2api.status.assert_not_called()

    def test_optional_worker_joins_share_one_deadline(self):
        release = threading.Event()
        runtime = self.service_runtime(lambda **_kwargs: release.wait(2))
        runtime.web2api.stop = Mock()
        runtime.oauth.close = Mock()
        runtime.relay_portal.close = Mock()
        started = time.monotonic()
        try:
            result = runtime._close_services(timeout_seconds=0.05)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.3)
            self.assertEqual(len(result["lingeringWorkers"]), 6)
        finally:
            release.set()

    def test_required_gateway_shutdown_is_retained_for_bounded_retry(self):
        release, entered = (threading.Event() for _ in range(2))
        runtime = self.service_runtime(lambda **_kwargs: None)

        def stop_gateway(**_kwargs):
            entered.set()
            release.wait(2)

        runtime.web2api.stop = Mock(side_effect=stop_gateway)
        try:
            result = runtime._close_services(0.01)
            self.assertTrue(entered.wait(1))
            self.assertEqual(result["pendingRequired"], ["停止本地服务"])
            again = runtime._close_services(0.01)
            self.assertEqual(again["pendingRequired"], ["停止本地服务"])
            runtime.web2api.stop.assert_called_once()
        finally:
            release.set()
        completed = runtime._close_services(1)
        self.assertEqual(completed["pendingRequired"], [])
        runtime.web2api.stop.assert_called_once()

    def test_pending_required_cleanup_never_completes_or_repeats_restoration(self):
        runtime = runtime_stub()
        pending = {"pendingRequired": ["取消 OAuth"], "errors": [], "timings": {}}
        ready = {"pendingRequired": [], "errors": [], "timings": {}}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(core, "RUNTIME_OVERLAY_FILE", Path(directory) / "absent.json"),
            patch.object(core, "_require_codex_process_scan_known", return_value=[]),
            patch.object(core, "restore_runtime_configuration_overlay", return_value={"restored": True}) as restore,
            patch.object(runtime, "_close_services", side_effect=[pending, ready]),
        ):
            first = runtime.close()
            self.assertFalse(first["completed"])
            self.assertTrue(first["restorationComplete"])
            self.assertEqual(first["blocked"], "services")
            self.assertTrue(runtime.configuration_session["recoveryOnly"])
            second = runtime.close()
            self.assertTrue(second["completed"])
            self.assertNotIn("blocked", second)
            self.assertFalse(runtime.configuration_session["recoveryOnly"])
            restore.assert_called_once()

    def test_pending_services_keep_repair_window_even_after_runtime_is_retired(self):
        runtime = runtime_stub()
        runtime._closed = True
        runtime._close_result = {"completed": False, "blocked": "services"}
        server = server_stub(runtime)
        server.force_exit = True
        server.shutdown_started.set()
        self.assertFalse(app.handle_native_window_closing(server, Mock()))

    def test_pending_preserving_exit_can_be_retried_as_full_exit(self):
        runtime = runtime_stub()
        pending = {"pendingRequired": ["取消 OAuth"], "errors": [], "timings": {}}
        ready = {"pendingRequired": [], "errors": [], "timings": {}}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(core, "RUNTIME_OVERLAY_FILE", Path(directory) / "absent.json"),
            patch.object(core, "_require_codex_process_scan_known", return_value=[]),
            patch.object(core, "restore_runtime_configuration_overlay", return_value={"restored": True}) as restore,
            patch.object(runtime, "_close_services", side_effect=[pending, ready, ready]),
        ):
            self.assertFalse(runtime.close_for_restart(exit_only=True)["completed"])
            restore.assert_not_called()
            result = runtime.close()
        self.assertTrue(result["completed"])
        self.assertTrue(result["restorationComplete"])
        self.assertFalse(result.get("preserved"))
        restore.assert_called_once()

    def test_failed_required_callback_is_not_complete_and_only_failure_is_retried(self):
        for preserving in (False, True):
            with self.subTest(preserving=preserving), tempfile.TemporaryDirectory() as directory:
                runtime = runtime_stub()
                runtime.oauth.close.side_effect = [RuntimeError("callback close failed"), None]
                with (
                    patch.object(core, "RUNTIME_OVERLAY_FILE", Path(directory) / "absent.json"),
                    patch.object(core, "_require_codex_process_scan_known", return_value=[]),
                    patch.object(core, "restore_runtime_configuration_overlay", return_value={"restored": True}) as restore,
                ):
                    close = runtime.close_for_restart if preserving else runtime.close
                    first = close()
                    self.assertFalse(first["completed"])
                    self.assertEqual(first["blocked"], "services")
                    self.assertIn("取消 OAuth", first["serviceCleanup"]["requiredErrors"])
                    second = close()
                    self.assertTrue(second["completed"])
                    self.assertEqual(second["serviceCleanup"]["requiredErrors"], {})
                    self.assertEqual(restore.call_count, 0 if preserving else 1)
                self.assertEqual(runtime.oauth.close.call_count, 2)
                runtime.web2api.stop.assert_called_once()
                runtime.relay_portal.close.assert_called_once()

    def test_failed_gateway_retry_retains_original_server_after_references_clear(self):
        runtime = self.service_runtime(lambda **_kwargs: None)
        target = object()
        runtime.web2api.server = target
        runtime.web2api.thread = object()
        runtime.web2api.lock = threading.RLock()
        attempts = []

        def stop(**_kwargs):
            attempts.append(runtime.web2api.server)
            runtime.web2api.server = None
            runtime.web2api.thread = None
            if len(attempts) == 1:
                raise RuntimeError("shutdown failed")

        runtime.web2api.stop = stop
        first = runtime._close_services()
        self.assertIn("停止本地服务", first["requiredErrors"])
        second = runtime._close_services()
        self.assertEqual(second["requiredErrors"], {})
        self.assertEqual(attempts, [target, target])

    def test_prepare_cancels_quota_sampler_before_shared_worker_join(self):
        runtime = app.ManagerRuntime(defer_configuration=True)
        sampler_entered = threading.Event()
        sampler = runtime.quota_estimates
        sampler.thread = threading.Thread(
            target=lambda: (sampler_entered.set(), sampler.stop.wait(2)), daemon=True,
        )
        sampler.thread.start()
        self.assertTrue(sampler_entered.wait(1))
        with patch("agent_manager.gateway.service.stop_codex_session_usage_backfill", return_value=True):
            result = runtime.prepare_for_restart(0.25)
        self.assertTrue(sampler.stop.is_set())
        self.assertTrue(runtime.update_check_wake.is_set())
        self.assertFalse(sampler.thread.is_alive())
        self.assertEqual(result["lingeringWorkers"], [])
        self.assertLess(result["elapsedMs"], 200)

    def test_exit_only_requires_explicit_api_dependency_acknowledgment(self):
        runtime = runtime_stub()
        server = server_stub(runtime)
        with patch.object(app.threading, "Thread") as worker:
            with self.assertRaisesRegex(core.ManagerError, "本地 API"):
                app.request_application_exit_only(server)
            self.assertFalse(server.shutdown_started.is_set())
            worker.assert_not_called()
            self.assertTrue(app.request_application_exit_only(server, confirmed=True))
            self.assertTrue(server.exit_only_requested)
            worker.return_value.start.assert_called_once()
        runtime.web2api.stop.assert_not_called()

    def test_exit_only_without_gateway_needs_no_dependency_confirmation(self):
        runtime = runtime_stub()
        runtime.web2api.status.return_value = {"running": False}
        self.assertFalse(runtime.exit_only_preflight()["requiresConfirmation"])
        with patch.object(app.threading, "Thread"):
            self.assertTrue(app.request_application_exit_only(server_stub(runtime)))

    def test_exit_only_rechecks_gateway_after_active_mutations_drain(self):
        runtime = runtime_stub()
        runtime.close_for_restart = Mock()
        server = server_stub(runtime)
        server.exit_only_confirmed = False
        with patch.object(app, "_write_shutdown_status"), patch.object(app, "_cleanup_runtime"):
            app.shutdown_application(server, exit_only=True)
        runtime.close_for_restart.assert_not_called()
        self.assertTrue(server.shutdown_aborted.is_set())
        self.assertFalse(server.shutdown_started.is_set())

    def test_shutdown_status_roundtrips_numeric_timings_and_restoration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shutdown.json"
            runtime = SimpleNamespace(close=Mock(return_value={
                "completed": True, "restorationComplete": True, "errors": [],
                "restartPreparation": {"elapsedMs": 10},
                "serviceCleanup": {"elapsedMs": 20, "timings": {"停止本地服务": 15}},
                "shutdownTiming": {"codexCloseMs": 30, "restoreMs": 5},
            }))
            server = server_stub(runtime)
            with patch.object(app, "SHUTDOWN_STATUS_FILE", path), patch.object(app, "_cleanup_runtime"):
                app.shutdown_application(server)
                record = app._read_shutdown_status()
            self.assertEqual(record["phase"], "completed")
            self.assertTrue(record["restorationComplete"])
            self.assertEqual(record["shutdownTiming"]["service:停止本地服务"], 15)
            self.assertEqual(record["shutdownTiming"]["codexCloseMs"], 30)
            self.assertIn("elapsedMs", record["shutdownTiming"])
            self.assertNotIn("runtimeNonce", record)
            self.assertIsInstance(json.loads(path.read_text(encoding="utf-8"))["shutdownTiming"], dict)


class TrayLifecycleTests(unittest.TestCase):
    def setUp(self):
        class Menu:
            SEPARATOR = object()

            def __init__(self, *items):
                self.items = items

        class Item:
            def __init__(self, text, action, **kwargs):
                self.text, self.action = text, action
                self.default = kwargs.get("default", False)

        class Icon:
            def __init__(self, _name, _image, _title, menu):
                self.menu = menu
                self.stopped = threading.Event()

            def run(self, setup):
                setup(self)
                self.stopped.wait(3)

            def stop(self):
                self.stopped.set()

        for target, replacement in (
            ("pystray.Menu", Menu), ("pystray.MenuItem", Item), ("pystray.Icon", Icon),
            ("PIL.Image.open", Mock(return_value=SimpleNamespace(convert=lambda _mode: None))),
        ):
            patcher = patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = app.ManagerServer(("127.0.0.1", 0), app.RequestHandler, runtime_stub())
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.stop_tray)
        self.server.native_window = True
        self.server.native_window_object = SimpleNamespace(create_confirmation_dialog=Mock(return_value=True))
        self.server.show_native_window = Mock(return_value=True)
        self.assertTrue(self.server.ensure_tray())
        self.actions = {item.text: item for item in self.server.tray.menu.items if isinstance(item, Item)}

    def test_exact_menu_labels_and_common_full_exit_without_inline_icon_stop(self):
        self.assertEqual(list(self.actions), ["显示", "仅退出软件", "彻底退出"])
        self.assertTrue(self.actions["显示"].default)
        self.actions["显示"].action()
        self.server.show_native_window.assert_called_once()
        icon = Mock()
        with patch.object(app, "request_application_shutdown") as shutdown:
            self.actions["彻底退出"].action(icon)
        shutdown.assert_called_once_with(self.server)
        icon.stop.assert_not_called()

    def test_exit_only_confirmation_is_async_and_invokes_common_gate(self):
        dialog_entered, release, called = (threading.Event() for _ in range(3))

        def confirm(*_args):
            dialog_entered.set()
            release.wait(2)
            return True

        self.server.native_window_object.create_confirmation_dialog.side_effect = confirm
        with patch.object(app, "request_application_exit_only", side_effect=lambda *_a, **_kw: called.set()) as exit_only:
            self.actions["仅退出软件"].action()
            try:
                self.assertTrue(dialog_entered.wait(1))
                self.assertFalse(called.is_set())
            finally:
                release.set()
            self.assertTrue(called.wait(1))
            exit_only.assert_called_once_with(self.server, confirmed=True)

    def test_cancelling_exit_only_keeps_manager_and_gateway(self):
        self.server.native_window_object.create_confirmation_dialog.return_value = False
        with patch.object(app, "request_application_exit_only") as exit_only:
            self.actions["仅退出软件"].action()
            deadline = time.monotonic() + 1
            while not self.server.native_window_object.create_confirmation_dialog.called and time.monotonic() < deadline:
                time.sleep(0.005)
            with self.server.tray_action_lock:
                exit_only.assert_not_called()
        self.assertFalse(self.server.shutdown_started.is_set())
        self.server.runtime.web2api.stop.assert_not_called()

    def test_no_gateway_skips_dialog_without_forging_acknowledgment(self):
        self.server.runtime.web2api.status.return_value = {"running": False}
        called = threading.Event()
        with patch.object(app, "request_application_exit_only", side_effect=lambda *_a, **_kw: called.set()) as exit_only:
            self.actions["仅退出软件"].action()
            self.assertTrue(called.wait(1))
            exit_only.assert_called_once_with(self.server, confirmed=False)
        self.server.native_window_object.create_confirmation_dialog.assert_not_called()


if __name__ == "__main__":
    unittest.main()
