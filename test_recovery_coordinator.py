import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import agent_manager_app as app
import agent_manager_core as core
import recovery_coordinator as coordinator


class RecoveryCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.store = SimpleNamespace(io_lock=threading.RLock(), lock=threading.RLock(),
                                     pending={}, pending_recent=[], flush=Mock())
        self.manager = SimpleNamespace(lock=threading.RLock(), usage_stats=self.store,
                                       status=Mock(return_value={"running": False}),
                                       reset_usage_stats=Mock(return_value={"cleared": True}))
        self.runtime = SimpleNamespace(web2api=self.manager, toolbox=app.ToolboxRuntime())

    def test_pending_usage_prevents_backup_and_destructive_reset(self):
        self.store.pending = {"unwritten": {"requestCount": 1}}
        with patch.object(coordinator.recovery, "create") as create:
            with self.assertRaisesRegex(core.ManagerError, "尚未完整写入"):
                coordinator.reset_usage(self.runtime)
        create.assert_not_called()
        self.manager.reset_usage_stats.assert_not_called()
        self.assertIn("unwritten", self.store.pending)

    def test_backup_failure_preserves_usage(self):
        with patch.object(coordinator.recovery, "create", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                coordinator.reset_usage(self.runtime)
        self.manager.reset_usage_stats.assert_not_called()

    def test_backup_and_reset_keep_delta_lock_across_both_operations(self):
        attempts = []
        def create(*args, **kwargs):
            def competing_writer():
                acquired = self.store.lock.acquire(timeout=0.05)
                attempts.append(acquired)
                if acquired:
                    self.store.lock.release()
            worker = threading.Thread(target=competing_writer)
            worker.start()
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
            return {"id": "point"}
        with patch.object(coordinator.recovery, "create", side_effect=create):
            usage, point = coordinator.reset_usage(self.runtime)
        self.assertEqual(attempts, [False])
        self.assertEqual(point["id"], "point")
        self.assertTrue(usage["cleared"])

    def test_configuration_backup_waits_for_vault_transaction(self):
        started = threading.Event()
        finished = threading.Event()
        def capture():
            started.set()
            coordinator.create(self.runtime)
            finished.set()
        with patch.object(coordinator.recovery, "create", return_value={}) as create:
            with coordinator.claude._LOCK:
                worker = threading.Thread(target=capture)
                worker.start()
                self.assertTrue(started.wait(1))
                self.assertFalse(finished.wait(0.05))
                create.assert_not_called()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            create.assert_called_once()

    def test_restore_refuses_inflight_request_and_invalidates_caches_on_success(self):
        self.manager.status.return_value = {"running": False, "activeRequestCount": 1}
        with patch.object(coordinator.recovery, "restore") as restore:
            with self.assertRaisesRegex(core.ManagerError, "现有请求结束"):
                coordinator.restore(self.runtime, "point", "fingerprint")
            restore.assert_not_called()
        self.manager.status.return_value = {"running": False}
        self.runtime.toolbox.mail_cache[("mail", 10, False)] = (1, [])
        with patch.object(coordinator.recovery, "restore", return_value={"restored": True, "scope": "configuration"}), \
                patch.object(coordinator.toolbox, "_clear_oauth_state") as clear:
            coordinator.restore(self.runtime, "point", "fingerprint")
        clear.assert_called_once()
        self.assertEqual(self.runtime.toolbox.mail_cache, {})

    def test_mail_edit_during_fetch_drops_obsolete_response(self):
        runtime = self.runtime.toolbox
        def fetch(*args, **kwargs):
            runtime.forget_mail("mail")
            return []
        with patch.object(app.toolbox, "fetch_mail_messages", side_effect=fetch):
            with self.assertRaisesRegex(core.ManagerError, "重新读取"):
                runtime.fetch_mail("mail", limit=10, unread_only=False)
        self.assertEqual(runtime.mail_cache, {})
        self.assertEqual(runtime.active_mailboxes, set())

    def test_account_delete_waits_for_a_successful_recovery_point(self):
        delete = Mock()
        with patch.object(coordinator.recovery, "create", side_effect=OSError("backup disk full")):
            with self.assertRaises(OSError):
                coordinator.before_account_delete(self.runtime, "before delete", delete)
        delete.assert_not_called()
        events = []
        with patch.object(coordinator.recovery, "create", side_effect=lambda *args, **kwargs: events.append("backup") or {"id": "safe-point"}):
            result, point = coordinator.before_account_delete(self.runtime, "before delete", lambda: events.append("delete") or {"deleted": True})
        self.assertEqual(events, ["backup", "delete"])
        self.assertTrue(result["deleted"])
        self.assertEqual(point["id"], "safe-point")

    def test_account_delete_error_retains_and_identifies_recovery_point(self):
        with patch.object(coordinator.recovery, "create", return_value={"id": "safe-point"}):
            with self.assertRaisesRegex(core.ManagerError, "safe-point"):
                coordinator.before_account_delete(self.runtime, "before delete", Mock(side_effect=core.ManagerError("active account")))


if __name__ == "__main__":
    unittest.main()
