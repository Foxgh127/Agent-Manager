import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest.mock import patch

import agent_manager.core as core
import agent_manager.sessions.history as history


class HistoryRecoveryV8Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.local = self.root / "codex"
        self.remote = self.root / "shared" / core.HISTORY_SYNC_FOLDER
        self.backups = self.local / "agent-manager" / "backups"
        self.local.mkdir(parents=True)
        self.remote.mkdir(parents=True)
        for name, value in {"CODEX_HOME": self.local, "BACKUPS_DIR": self.backups}.items():
            mocked = patch.object(core, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        for mocked in (
            patch.object(core, "load_settings", return_value={}),
            patch.object(core, "save_settings"),
            patch.object(core, "running_codex_processes", return_value=[]),
            patch.object(core, "refresh_codex_history_index", return_value={"ok": True}),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        with history.PLAN_LOCK:
            history.PLAN_CACHE.clear()
        self.addCleanup(self._clear_plan_cache)
        self.push = {"target": str(self.remote), "direction": "push", "recentDays": 0}

    def _clear_plan_cache(self):
        with history.PLAN_LOCK:
            history.PLAN_CACHE.clear()

    @staticmethod
    def write(root: Path, name: str, content: bytes) -> Path:
        path = root / "sessions" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def interrupted(self, files=None):
        files = files or {"a.jsonl": (b"old\nnew\n", b"old\n")}
        for name, (local, remote) in files.items():
            self.write(self.local, name, local)
            self.write(self.remote, name, remote)
        result = history.perform(self.push)
        journal_path = Path(result["backupPath"]) / "manifest.json"
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["status"] = "running"
        journal.pop("completedAt", None)
        journal.pop("summary", None)
        for row in journal["operations"]:
            row["state"] = "prepared"
        history._write_journal(journal_path, journal)
        return result["backupId"], journal_path

    def test_force_exit_leaves_a_previewable_and_restorable_transaction(self):
        self.write(self.local, "a.jsonl", b"old\nnew\n")
        destination = self.write(self.remote, "a.jsonl", b"old\n")
        script = textwrap.dedent(
            f"""
            import os
            from unittest.mock import patch
            import agent_manager.sessions.history as history

            original = history._copy_snapshot
            def terminate_after_publish(*args, **kwargs):
                original(*args, **kwargs)
                os._exit(73)

            with patch.object(history, "_copy_snapshot", side_effect=terminate_after_publish):
                history.perform({{"target": {str(self.remote)!r}, "direction": "push", "recentDays": 0}})
            """
        )
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(self.local)
        completed = subprocess.run(
            [sys.executable, "-c", script], cwd=Path(__file__).parent,
            env=environment, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(completed.returncode, 73, completed.stderr)
        self.assertEqual(destination.read_bytes(), b"old\nnew\n")
        listed = history.list_recoveries()
        self.assertEqual(len(listed["recoveries"]), 1)
        self.assertTrue(listed["recoveries"][0]["recoverable"])
        recovery_id = listed["recoveries"][0]["id"]
        reviewed = history.preview_recovery(recovery_id)
        self.assertEqual(reviewed["files"][0]["action"], "restore")
        result = history.restore_recovery(recovery_id, reviewed["fingerprint"])
        self.assertTrue(result["restored"])
        self.assertEqual(destination.read_bytes(), b"old\n")

    def test_restore_preserves_original_manifest_and_current_content_as_evidence(self):
        recovery_id, journal_path = self.interrupted()
        original_manifest = journal_path.read_bytes()
        synced = (self.remote / "sessions" / "a.jsonl").read_bytes()
        listed = history.list_recoveries()
        self.assertEqual(listed["recoveries"][0]["status"], "running")
        reviewed = history.preview_recovery(recovery_id)
        self.assertEqual(reviewed["changed"], 1)
        self.assertEqual(reviewed["conflicts"], 0)
        self.assertTrue(reviewed["requiresCodexClosed"])
        row = reviewed["files"][0]
        original_document = json.loads(original_manifest)
        self.assertEqual(row["currentHash"], row["syncedHash"])
        self.assertEqual(row["backupHash"], original_document["operations"][0]["beforeHash"])

        result = history.restore_recovery(recovery_id, reviewed["fingerprint"])

        self.assertTrue(result["restored"])
        self.assertEqual(result["changed"], 1)
        safety = Path(result["safetyPath"])
        self.assertEqual((safety / "source-manifest.json").read_bytes(), original_manifest)
        self.assertEqual((safety / "current/remote/sessions/a.jsonl").read_bytes(), synced)
        self.assertEqual((self.remote / "sessions/a.jsonl").read_bytes(), b"old\n")
        self.assertEqual(json.loads(journal_path.read_text(encoding="utf-8"))["status"], "recovered")
        self.assertEqual(history.list_recoveries()["recoveries"], [])

    def test_already_original_files_can_end_the_recovery(self):
        recovery_id, _journal_path = self.interrupted()
        (self.remote / "sessions/a.jsonl").write_bytes(b"old\n")
        reviewed = history.preview_recovery(recovery_id)
        self.assertEqual(reviewed["changed"], 0)
        self.assertEqual(reviewed["files"][0]["action"], "unchanged")
        result = history.restore_recovery(recovery_id, reviewed["fingerprint"])
        self.assertTrue(result["restored"])
        self.assertEqual(result["changed"], 0)
        self.assertEqual(history.list_recoveries()["recoveries"], [])

    def test_edit_after_preview_is_never_overwritten(self):
        recovery_id, _journal_path = self.interrupted()
        reviewed = history.preview_recovery(recovery_id)
        destination = self.remote / "sessions/a.jsonl"
        destination.write_bytes(b"external edit\n")
        with self.assertRaisesRegex(core.ManagerError, "预览后已变化"):
            history.restore_recovery(recovery_id, reviewed["fingerprint"])
        self.assertEqual(destination.read_bytes(), b"external edit\n")
        self.assertFalse(any(self.backups.glob("history-recovery-*")))

    def test_running_codex_blocks_even_a_remote_only_recovery(self):
        recovery_id, _journal_path = self.interrupted()
        reviewed = history.preview_recovery(recovery_id)
        with patch.object(core, "running_codex_processes", return_value=[{"pid": 7}]):
            with self.assertRaisesRegex(core.ManagerError, "关闭 Codex"):
                history.restore_recovery(recovery_id, reviewed["fingerprint"])
        self.assertEqual((self.remote / "sessions/a.jsonl").read_bytes(), b"old\nnew\n")

    def test_active_core_switch_gate_blocks_before_any_recovery_write(self):
        recovery_id, _journal_path = self.interrupted()
        reviewed = history.preview_recovery(recovery_id)
        busy = threading.Lock()
        busy.acquire()
        self.addCleanup(busy.release)
        with patch.object(core, "SWITCH_OPERATION_LOCK", busy):
            with self.assertRaisesRegex(core.ManagerError, "账号切换"):
                history.restore_recovery(recovery_id, reviewed["fingerprint"])
        self.assertEqual((self.remote / "sessions/a.jsonl").read_bytes(), b"old\nnew\n")
        self.assertFalse(any(self.backups.glob("history-recovery-*")))

    def test_tampered_absolute_destination_is_manual_only(self):
        recovery_id, journal_path = self.interrupted()
        outside = self.root / "outside.jsonl"
        outside.write_bytes(b"keep\n")
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["operations"][0]["destination"] = str(outside)
        core.atomic_write_json(journal_path, journal)

        listed = history.list_recoveries()

        self.assertEqual(listed["invalidCount"], 1)
        self.assertFalse(listed["recoveries"][0]["recoverable"])
        self.assertEqual(listed["recoveries"][0]["status"], "manual_review")
        with self.assertRaisesRegex(core.ManagerError, "篡改"):
            history.preview_recovery(recovery_id)
        self.assertEqual(outside.read_bytes(), b"keep\n")

    def test_even_authenticated_path_traversal_metadata_is_rejected(self):
        recovery_id, journal_path = self.interrupted()
        outside = self.root / "outside.jsonl"
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["operations"][0]["relative"] = "sessions/../../outside.jsonl"
        history._write_journal(journal_path, journal)
        with self.assertRaisesRegex(core.ManagerError, "路径穿越"):
            history.preview_recovery(recovery_id)
        self.assertFalse(outside.exists())

    def test_junction_backup_is_rejected_before_its_contents_are_read(self):
        if not hasattr(Path, "is_junction"):
            self.skipTest("Path.is_junction is unavailable")
        recovery_id, journal_path = self.interrupted()
        document = json.loads(journal_path.read_text(encoding="utf-8"))
        backup = Path(document["operations"][0]["backup"])
        normalized_backup = Path(os.path.abspath(backup))

        def is_backup(path):
            return Path(os.path.abspath(path)) == normalized_backup

        with patch.object(Path, "is_junction", autospec=True, side_effect=is_backup):
            with self.assertRaisesRegex(core.ManagerError, "符号链接|目录联接"):
                history.preview_recovery(recovery_id)

    def test_legacy_v1_journal_is_visible_but_never_trusted(self):
        recovery_id = "history-sync-20260905-010203-123456"
        root = self.backups / recovery_id
        root.mkdir(parents=True)
        core.atomic_write_json(root / "manifest.json", {
            "format": history.JOURNAL_FORMAT, "version": 1, "id": recovery_id,
            "createdAt": "2026-09-05T01:02:03Z", "status": "running",
            "target": str(self.root / "forged"), "operations": [],
        })

        listed = history.list_recoveries()

        self.assertEqual(listed["recoveries"][0]["status"], "manual_review")
        self.assertIn("旧版", listed["recoveries"][0]["reason"])
        with self.assertRaisesRegex(core.ManagerError, "旧版.*人工"):
            history.preview_recovery(recovery_id)
        self.assertFalse((self.root / "forged").exists())

    def test_restore_failure_rolls_back_and_keeps_safety_evidence(self):
        recovery_id, _journal_path = self.interrupted({
            "a.jsonl": (b"old-a\nnew-a\n", b"old-a\n"),
            "b.jsonl": (b"old-b\nnew-b\n", b"old-b\n"),
        })
        reviewed = history.preview_recovery(recovery_id)
        original = history._copy_snapshot
        failed = False

        def fail_second_after_publish(source, destination, *args, **kwargs):
            nonlocal failed
            result = original(source, destination, *args, **kwargs)
            if destination == self.remote / "sessions/b.jsonl" and not failed:
                failed = True
                raise OSError("injected recovery failure")
            return result

        with patch.object(history, "_copy_snapshot", side_effect=fail_second_after_publish):
            with self.assertRaisesRegex(core.ManagerError, "已回滚"):
                history.restore_recovery(recovery_id, reviewed["fingerprint"])
        self.assertEqual((self.remote / "sessions/a.jsonl").read_bytes(), b"old-a\nnew-a\n")
        self.assertEqual((self.remote / "sessions/b.jsonl").read_bytes(), b"old-b\nnew-b\n")
        safety_roots = list(self.backups.glob("history-recovery-*"))
        self.assertEqual(len(safety_roots), 1)
        safety = json.loads((safety_roots[0] / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(safety["status"], "rolled_back")

    def test_linked_transaction_directory_is_never_followed(self):
        recovery_id = "history-sync-20260905-020304-123456"
        outside = self.root / "outside-journal"
        outside.mkdir()
        (outside / "manifest.json").write_text("{}", encoding="utf-8")
        try:
            (self.backups / recovery_id).parent.mkdir(parents=True, exist_ok=True)
            (self.backups / recovery_id).symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")
        listed = history.list_recoveries()
        self.assertFalse(listed["recoveries"][0]["recoverable"])
        with self.assertRaisesRegex(core.ManagerError, "链接|联接"):
            history.preview_recovery(recovery_id)


if __name__ == "__main__":
    unittest.main()
