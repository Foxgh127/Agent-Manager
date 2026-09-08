from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_manager_core as core
import codex_config_recovery as recovery
import config_backup_service as backups


class ConfigBackupServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config.toml"
        self.state = self.root / "state"
        self.backup_root = self.state / "backups"
        self.config.write_bytes(b'model = "original"\n')
        for name, value in {
            "CODEX_HOME": self.root, "CONFIG_FILE": self.config, "STATE_DIR": self.state,
            "BACKUPS_DIR": self.backup_root, "RUNTIME_OVERLAY_FILE": self.state / "runtime-overlay.json",
            "SETTINGS_FILE": self.state / "settings.json", "AGENTS_FILE": self.root / "AGENTS.md",
            "MODEL_CATALOG_FILE": self.state / "catalog.json", "AGENTS_DIR": self.root / "agents",
        }.items():
            context = patch.object(core, name, value)
            context.start()
            self.addCleanup(context.stop)

    def manual(self):
        return recovery.create_manual_backup(expected_fingerprint=backups.fingerprint(self.config.read_bytes()))

    def auto(self, number):
        self.config.write_bytes(f'model = "v{number}"\n'.encode())
        return core.backup_file(self.config)

    def test_automatic_content_dedup_and_only_three_distinct_versions(self):
        first = self.auto(0)
        for _ in range(7):
            self.assertEqual(core.backup_file(self.config), first)
        self.assertEqual(len(backups.list_backups()), 1)
        for number in range(1, 7):
            self.auto(number)
        rows = backups.list_backups()
        self.assertEqual(len(rows), 3)
        self.assertEqual({Path(row["path"]).read_bytes() for row in rows},
                         {f'model = "v{number}"\n'.encode() for number in (4, 5, 6)})

    def test_manual_snapshots_are_independent_and_survive_automatic_and_general_pruning(self):
        manual = [self.manual() for _ in range(4)]
        for number in range(6):
            self.auto(number)
        with patch.object(core, "BACKUP_MAX_FILES", 0):
            core._prune_file_backups()
        for row in manual:
            self.assertEqual(Path(row["path"]).read_bytes(), b'model = "original"\n')
        self.assertEqual(len([row for row in backups.list_backups() if row["kind"] == "auto"]), 3)
        self.assertEqual(len({row["id"] for row in manual}), 4)
        before = self.config.read_bytes()
        recovery.delete_backup(backup_id=manual[0]["id"])
        self.assertFalse(Path(manual[0]["path"]).exists())
        self.assertEqual(self.config.read_bytes(), before)

    def test_revisited_version_is_counted_at_its_latest_transition(self):
        for index in (0, 1, 2, 0, 3):
            self.auto(index)
        rows = backups.list_backups()
        self.assertEqual(len(rows), 3)
        self.assertEqual({Path(row["path"]).read_bytes() for row in rows},
                         {f'model = "v{number}"\n'.encode() for number in (2, 0, 3)})

    def test_legacy_auto_adoption_is_in_place_and_unknown_external_files_are_retained(self):
        self.backup_root.mkdir(parents=True)
        legacy = []
        for index, value in enumerate((0, 1, 1, 2, 3, 4)):
            path = self.backup_root / f"config.toml.20260909-120000-{index:06d}.bak"
            path.write_bytes(f'model = "{value}"\n'.encode())
            os.utime(path, (index + 1, index + 1))
            legacy.append(path)
        external = self.backup_root / "config.toml.my-personal-copy.bak"
        external.write_bytes(b'model = "external"\n')
        external_row = next(row for row in backups.list_backups() if row["kind"] == "external")
        self.assertFalse(external_row["canDelete"])
        with self.assertRaises(core.ManagerError):
            recovery.delete_backup(backup_id=external_row["id"])
        report = backups.prune_automatic()
        self.assertEqual(report["removed"], 3)
        self.assertEqual({path for path in legacy if path.exists()}, set(legacy[-3:]))
        self.assertTrue(external.exists())

    def test_active_overlay_and_rollback_references_override_retention(self):
        first = self.auto(0)
        core.RUNTIME_OVERLAY_FILE.write_text(json.dumps({"files": {"config.toml": {"backupPath": str(first)}}}))
        for index in range(1, 6):
            self.auto(index)
        self.assertTrue(first.exists())
        rows = backups.list_backups()
        protected = next(row for row in rows if row["path"] == str(first))
        self.assertEqual(len(rows), 4)
        self.assertFalse(protected["canDelete"])
        with self.assertRaises(core.ManagerError):
            recovery.delete_backup(backup_id=protected["id"])
        core.RUNTIME_OVERLAY_FILE.unlink()
        journal = self.state / "rollback-journal.json"
        journal.write_text(json.dumps({"backups": [str(first)]}))
        backups.prune_automatic()
        self.assertTrue(first.exists())
        journal.unlink()
        backups.prune_automatic()
        self.assertFalse(first.exists())

    def test_unreadable_control_journal_fails_closed(self):
        first = self.auto(0)
        core.RUNTIME_OVERLAY_FILE.write_bytes(b"{invalid")
        for index in range(1, 5):
            self.auto(index)
        self.assertTrue(first.exists())
        self.assertTrue(all(not row["canDelete"] for row in backups.list_backups()))

    def test_reference_by_id_relative_path_or_backup_directory_is_protected(self):
        row = self.manual()
        path = Path(row["path"])
        for value in ({"backupId": row["id"]}, {"backupPath": str(path.relative_to(self.state))},
                      {"backupPath": str(path.parent)}):
            with self.subTest(value=value):
                core.RUNTIME_OVERLAY_FILE.write_text(json.dumps(value))
                with self.assertRaises(core.ManagerError):
                    recovery.delete_backup(backup_id=row["id"])
                self.assertTrue(path.exists())

    def test_stale_ids_reject_changed_or_replaced_backup_and_path_traversal(self):
        row = self.manual()
        path = Path(row["path"])
        content = path.read_bytes()
        path.unlink()
        path.write_bytes(content)
        with self.assertRaises(core.ManagerError):
            recovery.delete_backup(backup_id=row["id"])
        for value in ("../config.toml", str(path), "x" * 64):
            with self.assertRaises(core.ManagerError):
                recovery.delete_backup(backup_id=value)
        self.assertEqual(path.read_bytes(), content)

    def test_file_replaced_between_listing_and_deletion_is_preserved(self):
        row = self.manual()
        path = Path(row["path"])
        real_delete = backups._delete_checked
        def replace_then_delete(selected):
            path.write_bytes(b'model = "external-replacement"\n')
            return real_delete(selected)
        with patch.object(backups, "_delete_checked", side_effect=replace_then_delete):
            with self.assertRaises(core.ManagerError):
                recovery.delete_backup(backup_id=row["id"])
        self.assertEqual(path.read_bytes(), b'model = "external-replacement"\n')

    def test_hardlink_and_junction_are_not_deleted(self):
        row = self.manual()
        path = Path(row["path"])
        outside = self.root / "outside"
        outside.mkdir()
        os.link(path, outside / "hardlink")
        with self.assertRaises(core.ManagerError):
            recovery.delete_backup(backup_id=row["id"])
        (outside / "hardlink").unlink()
        moved = self.backup_root / "parked-manual"
        path.parent.rename(moved)
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(outside), str(path.parent))
        else:
            path.parent.symlink_to(outside, target_is_directory=True)
        target = outside / path.name
        target.write_bytes(b'model = "external"\n')
        with self.assertRaises(core.ManagerError):
            recovery.delete_backup(backup_id=row["id"])
        self.assertEqual(target.read_bytes(), b'model = "external"\n')

    @unittest.skipUnless(os.name == "nt", "Windows file-handle safety")
    def test_open_delete_handle_blocks_late_replacement(self):
        row = self.manual()
        path = Path(row["path"])
        real_refs = backups._references
        calls = 0
        def try_swap():
            nonlocal calls
            calls += 1
            if calls == 2:  # Deletion re-checks references with the handle held.
                with self.assertRaises(OSError):
                    path.unlink()
                with self.assertRaises(OSError):
                    path.write_bytes(b"wrong")
            return real_refs()
        with patch.object(backups, "_references", side_effect=try_swap):
            recovery.delete_backup(backup_id=row["id"])
        self.assertEqual(calls, 2)
        self.assertFalse(path.exists())

    def test_invalid_manual_and_recovery_originals_are_visible_but_not_restorable(self):
        self.config.write_bytes(b"broken=[\n")
        manual = self.manual()
        inspection = recovery.inspect_recovery()
        self.assertEqual(inspection["backups"], [])
        self.assertFalse(inspection["backupFiles"][0]["canRestore"])
        result = recovery.repair_config(expected_fingerprint=inspection["fingerprint"], reset=True)
        for index in range(5):
            self.auto(index)
        self.assertEqual(Path(result["originalBackup"]).read_bytes(), b"broken=[\n")
        self.assertEqual(Path(manual["path"]).read_bytes(), b"broken=[\n")

    def test_restore_retains_its_selected_backup_and_exact_original(self):
        selected = self.manual()
        for number in range(6):
            self.auto(number)
        original = self.config.read_bytes()
        result = recovery.repair_config(expected_fingerprint=backups.fingerprint(original), backup_id=selected["id"])
        self.assertEqual(self.config.read_bytes(), b'model = "original"\n')
        self.assertTrue(Path(selected["path"]).exists())
        self.assertEqual(Path(result["originalBackup"]).read_bytes(), original)

    def test_manual_backup_rejects_stale_current_fingerprint(self):
        with self.assertRaises(core.ManagerError):
            recovery.create_manual_backup(expected_fingerprint="0" * 64)
        self.assertFalse(self.backup_root.exists())

    def test_repeated_apply_creates_no_new_config_backup(self):
        settings = core._initial_settings()
        with ExitStack() as stack:
            for name, value in {"load_settings": settings, "_configuration_model_records": [],
                                "_managed_subagent_specs": [], "_runtime_overlay_targets": [],
                                "_clear_inactive_provider_environment_overrides": [],
                                "local_model_catalog": [], "save_settings": None}.items():
                stack.enter_context(patch.object(core, name, return_value=value))
            stack.enter_context(patch.object(core, "_sync_user_environment", side_effect=AssertionError("real environment write")))
            stack.enter_context(patch.object(core, "_remove_user_environment", side_effect=AssertionError("real environment write")))
            first = core.apply_configuration()
            contents = self.config.read_bytes()
            count = len(backups.list_backups())
            for _ in range(5):
                result = core.apply_configuration()
                self.assertFalse(result["changed"])
                self.assertEqual(self.config.read_bytes(), contents)
                self.assertEqual(len(backups.list_backups()), count)
            self.assertTrue(first["changed"])


if __name__ == "__main__":
    unittest.main()
