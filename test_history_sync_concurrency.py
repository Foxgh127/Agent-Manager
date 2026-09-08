import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import agent_manager_core as core
import history_sync_service as history


class HistorySyncConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.local = self.root / "codex"
        self.remote = self.root / "backup" / core.HISTORY_SYNC_FOLDER
        self.local.mkdir()
        self.remote.mkdir(parents=True)
        self.backups = self.root / "journals"
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

    def test_destination_change_during_snapshot_copy_is_preserved(self):
        self.write(self.local, "a.jsonl", b"old\nnew\n")
        destination = self.write(self.remote, "a.jsonl", b"old\n")
        reviewed = history.preview(self.push)
        original = history._copy_snapshot

        def race(*args):
            destination.write_bytes(b"external writer\n")
            return original(*args)

        with patch.object(history, "_copy_snapshot", side_effect=race):
            with self.assertRaisesRegex(core.ManagerError, "外部修改"):
                history.perform({**self.push, "expectedFingerprint": reviewed["fingerprint"]})
        self.assertEqual(destination.read_bytes(), b"external writer\n")
        backup = next(self.backups.glob("history-sync-*/remote/sessions/a.jsonl"))
        self.assertEqual(backup.read_bytes(), b"old\n")

    def test_interrupt_after_atomic_publish_uses_prepared_record_for_rollback(self):
        self.write(self.local, "a.jsonl", b"old\nnew\n")
        destination = self.write(self.remote, "a.jsonl", b"old\n")
        original = history._copy_snapshot
        observed = {}

        def interrupt_after_publish(*args):
            manifest_path = next(self.backups.glob("history-sync-*/manifest.json"))
            observed.update(json.loads(manifest_path.read_text(encoding="utf-8")))
            original(*args)
            raise KeyboardInterrupt()

        with patch.object(history, "_copy_snapshot", side_effect=interrupt_after_publish):
            with self.assertRaises(KeyboardInterrupt):
                history.perform(self.push)
        self.assertEqual(observed["operations"][0]["state"], "prepared")
        self.assertEqual(destination.read_bytes(), b"old\n")
        journal = json.loads(next(self.backups.glob("history-sync-*/manifest.json")).read_text(encoding="utf-8"))
        self.assertEqual(journal["status"], "rolled_back")

    def test_mismatched_request_does_not_consume_preview_but_success_does(self):
        self.write(self.local, "a.jsonl", b"source\n")
        reviewed = history.preview(self.push)
        expected = reviewed["fingerprint"]
        with self.assertRaisesRegex(core.ManagerError, "参数与预览不同"):
            history.perform({**self.push, "direction": "pull", "expectedFingerprint": expected})
        result = history.perform({**self.push, "expectedFingerprint": expected})
        self.assertEqual(result["pushed"], 1)
        with self.assertRaisesRegex(core.ManagerError, "已过期或已执行"):
            history.perform({**self.push, "expectedFingerprint": expected})

    def test_codex_process_precondition_does_not_consume_pull_preview(self):
        self.write(self.remote, "a.jsonl", b"source\n")
        pull = {**self.push, "direction": "pull"}
        reviewed = history.preview(pull)
        request = {**pull, "expectedFingerprint": reviewed["fingerprint"]}
        with patch.object(core, "running_codex_processes", return_value=[{"pid": 7}]):
            with self.assertRaisesRegex(core.ManagerError, "关闭 Codex"):
                history.perform(request)
        result = history.perform(request)
        self.assertEqual(result["pulled"], 1)

    def test_expired_preview_is_removed_and_cannot_execute(self):
        self.write(self.local, "a.jsonl", b"source\n")
        reviewed = history.preview(self.push)
        expected = reviewed["fingerprint"]
        with history.PLAN_LOCK:
            _timestamp, plan = history.PLAN_CACHE[expected]
            history.PLAN_CACHE[expected] = (time.monotonic() - history.PLAN_TTL_SECONDS - 1, plan)
        with self.assertRaisesRegex(core.ManagerError, "已过期或已执行"):
            history.perform({**self.push, "expectedFingerprint": expected})
        with history.PLAN_LOCK:
            self.assertNotIn(expected, history.PLAN_CACHE)
        self.assertFalse((self.remote / "sessions/a.jsonl").exists())

    def test_appended_source_keeps_reviewed_prefix_mtime(self):
        source = self.write(self.local, "a.jsonl", b"first\n")
        os.utime(source, ns=(1_700_000_000_123_456_700,) * 2)
        reviewed_mtime = source.stat().st_mtime_ns
        reviewed = history.preview(self.push)
        source.write_bytes(b"first\nappended\n")
        os.utime(source, ns=(reviewed_mtime + 10_000_000_000,) * 2)
        history.perform({**self.push, "expectedFingerprint": reviewed["fingerprint"]})
        destination = self.remote / "sessions/a.jsonl"
        self.assertEqual(destination.read_bytes(), b"first\n")
        self.assertEqual(destination.stat().st_mtime_ns, reviewed_mtime)

    def test_mtime_change_creates_distinct_cached_preview(self):
        source = self.write(self.local, "a.jsonl", b"source\n")
        os.utime(source, ns=(1_700_000_000_123_456_700,) * 2)
        first_mtime = source.stat().st_mtime_ns
        first = history.preview(self.push)
        os.utime(source, ns=(first_mtime + 10_000_000_000,) * 2)
        second = history.preview(self.push)
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        history.perform({**self.push, "expectedFingerprint": first["fingerprint"]})
        self.assertEqual((self.remote / "sessions/a.jsonl").stat().st_mtime_ns, first_mtime)

    def test_selected_root_junction_is_rejected_before_planning(self):
        if not hasattr(Path, "is_junction"):
            self.skipTest("Path.is_junction is unavailable")
        candidate = Path(os.path.abspath(self.remote))

        def is_selected_root(path):
            return Path(os.path.abspath(path)) == candidate

        with patch.object(Path, "is_junction", autospec=True, side_effect=is_selected_root):
            with self.assertRaisesRegex(core.ManagerError, "目录联接"):
                history.preview(self.push)

    def test_forged_plan_destination_outside_selected_root_is_rejected(self):
        source = self.write(self.local, "a.jsonl", b"source\n")
        outside = self.root / "outside.jsonl"
        operation = {
            "action": "copy_to_remote",
            "relative": "sessions/a.jsonl",
            "source": source,
            "destination": outside,
            "bytes": source.stat().st_size,
            "reason": "forged",
            "sourceHash": core._hash_file(source),
            "sourceMtimeNs": source.stat().st_mtime_ns,
            "destinationHash": None,
        }
        with patch.object(core, "_history_plan", return_value=(self.remote, [operation])):
            with self.assertRaisesRegex(core.ManagerError, "超出所选目录"):
                history.preview(self.push)
        self.assertFalse(outside.exists())

    def test_rollback_restores_existing_destination_mtime(self):
        self.write(self.local, "a.jsonl", b"old-a\nnew-a\n")
        self.write(self.local, "b.jsonl", b"old-b\nnew-b\n")
        destination = self.write(self.remote, "a.jsonl", b"old-a\n")
        self.write(self.remote, "b.jsonl", b"old-b\n")
        os.utime(destination, ns=(1_690_000_000_987_654_300,) * 2)
        original_mtime = destination.stat().st_mtime_ns
        original = history._copy_snapshot

        def fail_second(*args):
            if args[1] == self.remote / "sessions/b.jsonl":
                raise OSError("injected copy failure")
            return original(*args)

        with patch.object(history, "_copy_snapshot", side_effect=fail_second):
            with self.assertRaisesRegex(core.ManagerError, "已恢复"):
                history.perform(self.push)
        self.assertEqual(destination.read_bytes(), b"old-a\n")
        self.assertEqual(destination.stat().st_mtime_ns, original_mtime)


if __name__ == "__main__":
    unittest.main()
