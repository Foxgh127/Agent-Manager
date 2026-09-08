import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_manager_core as core
import history_sync_service as history


class HistorySyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.local = self.root / "codex"
        self.remote = self.root / "backup" / core.HISTORY_SYNC_FOLDER
        self.local.mkdir()
        self.remote.mkdir(parents=True)
        self.backups = self.local / "agent-manager/backups"
        for name, value in {"CODEX_HOME": self.local, "BACKUPS_DIR": self.backups}.items():
            p = patch.object(core, name, value); p.start(); self.addCleanup(p.stop)
        for p in [patch.object(core, "load_settings", return_value={}), patch.object(core, "save_settings"),
                  patch.object(core, "running_codex_processes", return_value=[]), patch.object(core, "refresh_codex_history_index", return_value={"ok": True})]:
            p.start(); self.addCleanup(p.stop)
        self.payload = {"target": str(self.remote), "direction": "push", "recentDays": 0}

    def write(self, root, name, content):
        path = root / "sessions" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_same_size_and_timestamp_do_not_hide_divergence(self):
        local = self.write(self.local, "a.jsonl", b'{"a":1}\n')
        remote = self.write(self.remote, "a.jsonl", b'{"a":2}\n')
        os.utime(remote, ns=(local.stat().st_atime_ns, local.stat().st_mtime_ns))
        preview = history.preview(self.payload)
        self.assertEqual(preview["counts"], {"conflict": 1})
        self.assertEqual(history.perform(self.payload)["conflicts"], 1)
        self.assertEqual(remote.read_bytes(), b'{"a":2}\n')

    def test_changed_source_requires_a_new_preview_even_when_mtime_is_preserved(self):
        path = self.write(self.local, "a.jsonl", b"old\n")
        stamp = path.stat().st_mtime_ns
        preview = history.preview(self.payload)
        path.write_bytes(b"new\n")
        os.utime(path, ns=(stamp, stamp))
        with self.assertRaisesRegex(core.ManagerError, "预览后发生变化"):
            history.perform({**self.payload, "expectedFingerprint": preview["fingerprint"]})
        self.assertFalse((self.remote / "sessions/a.jsonl").exists())

    def test_failed_second_copy_rolls_back_first_created_file(self):
        self.write(self.local, "a.jsonl", b"first\n")
        self.write(self.local, "b.jsonl", b"second\n")
        copy = history._copy_snapshot
        def fail_second(source, destination, *args):
            if destination == self.remote / "sessions/b.jsonl":
                raise OSError("injected disk failure")
            return copy(source, destination, *args)
        with patch.object(history, "_copy_snapshot", side_effect=fail_second):
            with self.assertRaisesRegex(core.ManagerError, "已恢复"):
                history.perform(self.payload)
        self.assertFalse((self.remote / "sessions/a.jsonl").exists())
        journal = json.loads(next(self.backups.glob("history-sync-*/manifest.json")).read_text(encoding="utf-8"))
        self.assertEqual(journal["status"], "rolled_back")

    def test_rollback_preserves_concurrent_external_changes(self):
        self.write(self.local, "a.jsonl", b"first\n")
        self.write(self.local, "b.jsonl", b"second\n")
        destination = self.remote / "sessions/a.jsonl"
        copy = history._copy_snapshot
        def fail_second(source, target, *args):
            if target == self.remote / "sessions/b.jsonl":
                destination.write_bytes(b"external content\n")
                raise OSError("disk failure")
            return copy(source, target, *args)
        with patch.object(history, "_copy_snapshot", side_effect=fail_second):
            with self.assertRaisesRegex(core.ManagerError, "外部修改"):
                history.perform(self.payload)
        self.assertEqual(destination.read_bytes(), b"external content\n")

    def test_prefix_replacement_retains_validated_backup(self):
        self.write(self.local, "a.jsonl", b"old\nnew\n")
        self.write(self.remote, "a.jsonl", b"old\n")
        result = history.perform(self.payload)
        self.assertEqual(result["pushed"], 1)
        backup = Path(result["backupPath"]) / "remote/sessions/a.jsonl"
        self.assertEqual(backup.read_bytes(), b"old\n")
        journal = json.loads((Path(result["backupPath"]) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(journal["status"], "complete")
        self.assertEqual(journal["operations"][0]["afterHash"], core._hash_file(self.remote / "sessions/a.jsonl"))

    def test_pull_refuses_to_modify_running_codex_history(self):
        self.write(self.remote, "a.jsonl", b"remote\n")
        with patch.object(core, "running_codex_processes", return_value=[{"pid": 1}]):
            with self.assertRaisesRegex(core.ManagerError, "关闭 Codex"):
                history.perform({**self.payload, "direction": "pull"})
        self.assertFalse((self.local / "sessions/a.jsonl").exists())

    def test_symlink_destination_cannot_escape_selected_directory(self):
        self.write(self.local, "a.jsonl", b"source\n")
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (self.remote / "sessions").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Windows symlink creation is not enabled")
        with self.assertRaises(core.ManagerError):
            history.preview(self.payload)
        self.assertFalse((outside / "a.jsonl").exists())

    def test_post_copy_settings_failure_reports_completed_files_with_warning(self):
        self.write(self.local, "a.jsonl", b"source\n")
        with patch.object(core, "save_settings", side_effect=OSError("settings unavailable")):
            result = history.perform(self.payload)
        self.assertEqual(result["pushed"], 1)
        self.assertIn("设置记录未能保存", result["warning"])
        self.assertEqual((self.remote / "sessions/a.jsonl").read_bytes(), b"source\n")

    def test_invalid_days_and_concurrent_sync_have_actionable_errors(self):
        with self.assertRaises(core.ManagerError):
            history.preview({**self.payload, "recentDays": "bad"})
        with history.SYNC_LOCK:
            with self.assertRaisesRegex(core.ManagerError, "另一个会话同步"):
                history.perform(self.payload)

    def test_append_after_preview_backs_up_the_reviewed_prefix(self):
        path = self.write(self.local, "a.jsonl", b"first\n")
        preview = history.preview(self.payload)
        path.write_bytes(b"first\nnew-message\n")
        result = history.perform({**self.payload, "expectedFingerprint": preview["fingerprint"]})
        self.assertEqual(result["pushed"], 1)
        self.assertEqual((self.remote / "sessions/a.jsonl").read_bytes(), b"first\n")
        self.assertEqual(path.read_bytes(), b"first\nnew-message\n")


if __name__ == "__main__":
    unittest.main()
