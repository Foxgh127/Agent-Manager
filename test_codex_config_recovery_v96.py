from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import agent_manager_core as core
import codex_config_recovery as recovery


class CodexConfigRecoveryV96Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.sandbox = Path(self.temporary.name)
        self.codex_home = self.sandbox / "codex-home"
        self.config = self.codex_home / "config.toml"
        self.backups = self.sandbox / "manager-state" / "backups"
        self.overlay = self.sandbox / "manager-state" / "runtime-overlay.json"
        self.settings = self.sandbox / "manager-state" / "settings.json"
        self.codex_home.mkdir(parents=True)
        for name, value in {
            "CODEX_HOME": self.codex_home,
            "CONFIG_FILE": self.config,
            "BACKUPS_DIR": self.backups,
            "RUNTIME_OVERLAY_FILE": self.overlay,
            "SETTINGS_FILE": self.settings,
            "STATE_DIR": self.sandbox / "manager-state",
            "MODELS_CACHE_FILE": self.codex_home / "models_cache.json",
        }.items():
            context = patch.object(core, name, value)
            context.start()
            self.addCleanup(context.stop)

    def _backup(self, name: str, raw: bytes) -> Path:
        self.backups.mkdir(parents=True, exist_ok=True)
        path = self.backups / name
        path.write_bytes(raw)
        return path

    def _invalid_utf16(self) -> bytes:
        return 'model = [\nunknown = "保留"\n'.encode("utf-16")

    def _directory_link(self, link: Path, target: Path) -> None:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)

    def test_valid_utf16_and_utf32_are_parsed_and_losslessly_normalized(self):
        source = (
            '# 用户注释\nmodel = "gpt-6-astra"\nunknown_root = "保留✓"\n\n'
            '[custom]\nvalue = 42 # 未知字段\n'
        )
        for codec in ("utf-16", "utf-32"):
            with self.subTest(codec=codec):
                raw = source.encode(codec)
                self.config.write_bytes(raw)

                self.assertEqual(core.read_toml(self.config)["unknown_root"], "保留✓")
                inspection = recovery.inspect_recovery()
                self.assertEqual(inspection["status"], "encoding")
                self.assertTrue(inspection["canNormalize"])

                result = recovery.normalize_encoding_if_needed()

                self.assertTrue(result["changed"])
                self.assertEqual(result["action"], "normalize")
                self.assertEqual(self.config.read_bytes(), source.encode("utf-8"))
                saved = Path(result["originalBackup"])
                self.assertEqual(saved.read_bytes(), raw)
                self.assertEqual(tomllib.loads(self.config.read_text(encoding="utf-8"))["custom"]["value"], 42)

    def test_inspection_lists_only_valid_direct_backups_without_touching_damage(self):
        damaged = b'not = [valid\n\xff'
        self.config.write_bytes(damaged)
        valid = b'model = "from-backup"\n[unknown]\nkeep = true\n'
        valid_path = self._backup("config.toml.20260907.bak", valid)
        self._backup("config.toml.invalid.backup", b"model = [\n")
        nested = self.backups / "nested"
        nested.mkdir()
        (nested / "config.toml.hidden.bak").write_bytes(b'model = "nested"\n')

        inspection = recovery.inspect_recovery()

        self.assertEqual(inspection["status"], "invalid")
        self.assertEqual(self.config.read_bytes(), damaged)
        self.assertEqual(len(inspection["backups"]), 1)
        self.assertEqual(inspection["backups"][0]["path"], str(valid_path.resolve()))
        self.assertEqual(
            inspection["backups"][0]["fingerprint"], hashlib.sha256(valid).hexdigest()
        )
        self.assertFalse((self.backups / "config-recovery").exists())

    def test_selected_backup_is_rejected_if_its_bytes_change_after_inspection(self):
        damaged = b"model = [\n"
        self.config.write_bytes(damaged)
        backup = self._backup("config.toml.race.bak", b'model = "before"\n')
        selected_id = recovery.inspect_recovery()["backups"][0]["id"]
        real_read = recovery._read_bounded
        backup_reads = 0

        def racing_read(path: Path) -> bytes:
            nonlocal backup_reads
            if Path(path) == backup:
                backup_reads += 1
                if backup_reads == 2:
                    backup.write_bytes(b'model = "after"\n')
            return real_read(path)

        with patch.object(recovery, "_read_bounded", side_effect=racing_read):
            with self.assertRaisesRegex(core.ManagerError, "备份在检查后发生变化"):
                recovery.repair_config(
                    expected_fingerprint=hashlib.sha256(damaged).hexdigest(),
                    backup_id=selected_id,
                )

        self.assertEqual(self.config.read_bytes(), damaged)
        self.assertFalse((self.backups / "config-recovery").exists())

    def test_invalid_utf16_is_not_reset_at_startup_or_without_explicit_choice(self):
        damaged = self._invalid_utf16()
        self.config.write_bytes(damaged)

        startup = recovery.normalize_encoding_if_needed()
        inspection = recovery.inspect_recovery()

        self.assertFalse(startup["changed"])
        self.assertEqual(inspection["status"], "invalid")
        with self.assertRaisesRegex(core.ManagerError, "选择有效备份|明确选择"):
            recovery.repair_config(expected_fingerprint=inspection["fingerprint"])
        self.assertEqual(self.config.read_bytes(), damaged)
        self.assertFalse((self.backups / "config-recovery").exists())

    def test_explicit_reset_keeps_exact_damaged_bytes_in_bounded_backup(self):
        damaged = self._invalid_utf16()
        self.config.write_bytes(damaged)
        fingerprint = recovery.inspect_recovery()["fingerprint"]

        result = recovery.repair_config(expected_fingerprint=fingerprint, reset=True)

        self.assertTrue(result["changed"])
        self.assertEqual(result["action"], "reset")
        self.assertEqual(self.config.read_bytes(), b"")
        saved = Path(result["originalBackup"])
        self.assertEqual(saved.read_bytes(), damaged)
        self.assertEqual(saved.resolve().parent, (self.backups / "config-recovery").resolve())
        self.assertRegex(saved.name, r"^config\.toml\.\d+-[0-9a-f]{8}\.original$")

    def test_external_config_change_during_backup_cancels_overwrite(self):
        damaged = b"model = [\n"
        external = b'model = "external-editor"\n'
        self.config.write_bytes(damaged)
        fingerprint = recovery.inspect_recovery()["fingerprint"]
        real_atomic_write = core.atomic_write_bytes

        def race_after_backup(path: Path, content: bytes) -> None:
            real_atomic_write(path, content)
            if Path(path).parent == self.backups / "config-recovery":
                self.config.write_bytes(external)

        with patch.object(core, "atomic_write_bytes", side_effect=race_after_backup):
            with self.assertRaisesRegex(core.ManagerError, "备份期间配置又被修改"):
                recovery.repair_config(
                    expected_fingerprint=fingerprint,
                    reset=True,
                )

        self.assertEqual(self.config.read_bytes(), external)
        saved = list((self.backups / "config-recovery").glob("*.original"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_bytes(), damaged)

    def test_external_change_before_transaction_snapshot_cancels_overwrite(self):
        damaged = b"model = [\n"
        external = b'model = "snapshot-race"\n'
        self.config.write_bytes(damaged)
        fingerprint = recovery.inspect_recovery()["fingerprint"]
        real_capture = core._capture_file_bytes

        def race_before_capture(paths):
            self.config.write_bytes(external)
            return real_capture(paths)

        with patch.object(core, "_capture_file_bytes", side_effect=race_before_capture):
            with self.assertRaisesRegex(core.ManagerError, "修改|变化"):
                recovery.repair_config(
                    expected_fingerprint=fingerprint,
                    reset=True,
                )

        self.assertEqual(self.config.read_bytes(), external)
        saved = list((self.backups / "config-recovery").glob("*.original"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_bytes(), damaged)

    def test_partial_config_write_failure_rolls_back_original_and_overlay(self):
        damaged = b"model = [\n"
        restored = b'model = "restored"\n'
        overlay_before = b'{"overlay":"before"}\n'
        self.config.write_bytes(damaged)
        self.overlay.parent.mkdir(parents=True, exist_ok=True)
        self.overlay.write_bytes(overlay_before)
        backup = self._backup("config.toml.write-failure.bak", restored)
        selected_id = next(
            item["id"]
            for item in recovery.inspect_recovery()["backups"]
            if item["path"] == str(backup.resolve())
        )
        real_atomic_write = core.atomic_write_bytes
        injected = False

        def fail_after_config_replace(path: Path, content: bytes) -> None:
            nonlocal injected
            real_atomic_write(path, content)
            if Path(path) == self.config and content == restored and not injected:
                injected = True
                raise OSError("synthetic post-replace failure")

        with patch.object(core, "atomic_write_bytes", side_effect=fail_after_config_replace):
            with self.assertRaisesRegex(core.ManagerError, "配置恢复未完成"):
                recovery.repair_config(
                    expected_fingerprint=hashlib.sha256(damaged).hexdigest(),
                    backup_id=selected_id,
                )

        self.assertTrue(injected)
        self.assertEqual(self.config.read_bytes(), damaged)
        self.assertEqual(self.overlay.read_bytes(), overlay_before)

    def test_config_write_readback_mismatch_rolls_back_original(self):
        damaged = b"model = [\n"
        restored = b'model = "restored"\n'
        wrong = b'model = "wrong-but-valid"\n'
        self.config.write_bytes(damaged)
        backup = self._backup("config.toml.readback-failure.bak", restored)
        selected_id = next(
            item["id"]
            for item in recovery.inspect_recovery()["backups"]
            if item["path"] == str(backup.resolve())
        )
        real_atomic_write = core.atomic_write_bytes
        corrupted_once = False

        def corrupt_config_write(path: Path, content: bytes) -> None:
            nonlocal corrupted_once
            if Path(path) == self.config and content == restored and not corrupted_once:
                corrupted_once = True
                real_atomic_write(path, wrong)
                return
            real_atomic_write(path, content)

        with patch.object(core, "atomic_write_bytes", side_effect=corrupt_config_write):
            with self.assertRaises(core.ManagerError):
                recovery.repair_config(
                    expected_fingerprint=hashlib.sha256(damaged).hexdigest(),
                    backup_id=selected_id,
                )

        self.assertTrue(corrupted_once)
        self.assertEqual(self.config.read_bytes(), damaged)

    def test_overlay_rebase_exception_rolls_back_config_and_overlay(self):
        damaged = b"model = [\n"
        restored = b'model = "restored"\n'
        self.config.write_bytes(damaged)
        self.overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay_before = json.dumps(
            {
                "schemaVersion": 1,
                "ownerPid": os.getpid(),
                "codexHome": str(self.codex_home),
                "files": {"config.toml": {"kind": "config"}},
                "environment": {},
            }
        ).encode("utf-8")
        self.overlay.write_bytes(overlay_before)
        backup = self._backup("config.toml.overlay-failure.bak", restored)
        selected_id = next(
            item["id"]
            for item in recovery.inspect_recovery()["backups"]
            if item["path"] == str(backup.resolve())
        )

        with patch.object(
            core,
            "_runtime_overlay_rebase_user_file",
            side_effect=core.ManagerError("synthetic rebase failure"),
        ):
            with self.assertRaisesRegex(core.ManagerError, "synthetic rebase failure"):
                recovery.repair_config(
                    expected_fingerprint=hashlib.sha256(damaged).hexdigest(),
                    backup_id=selected_id,
                )

        self.assertEqual(self.config.read_bytes(), damaged)
        self.assertEqual(self.overlay.read_bytes(), overlay_before)

    def test_tracked_foreign_overlay_rebase_decline_must_cancel_recovery(self):
        damaged = b"model = [\n"
        restored = b'model = "restored"\n'
        self.config.write_bytes(damaged)
        backup = self._backup("config.toml.foreign-overlay.bak", restored)
        self.overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay_before = {
            "schemaVersion": 1,
            "ownerPid": os.getpid() + 100000,
            "codexHome": str(self.codex_home),
            "files": {"config.toml": {"kind": "config"}},
            "environment": {},
        }
        self.overlay.write_text(json.dumps(overlay_before), encoding="utf-8")
        selected_id = next(
            item["id"]
            for item in recovery.inspect_recovery()["backups"]
            if item["path"] == str(backup.resolve())
        )

        with self.assertRaises(core.ManagerError):
            recovery.repair_config(
                expected_fingerprint=hashlib.sha256(damaged).hexdigest(),
                backup_id=selected_id,
            )

        self.assertEqual(self.config.read_bytes(), damaged)
        self.assertEqual(
            json.loads(self.overlay.read_text(encoding="utf-8")), overlay_before
        )

    def test_backup_file_symlink_and_nested_candidate_do_not_cross_boundary(self):
        self.config.write_bytes(b"model = [\n")
        outside = self.sandbox / "outside"
        outside.mkdir()
        target = outside / "config.toml.secret.bak"
        target.write_bytes(b'model = "outside"\n')
        self.backups.mkdir(parents=True)
        link = self.backups / "config.toml.link.bak"
        link.write_bytes(target.read_bytes())
        nested = self.backups / "nested"
        nested.mkdir()
        (nested / "config.toml.nested.bak").write_bytes(b'model = "nested"\n')

        path_type = type(link)
        real_is_symlink = path_type.is_symlink

        def simulated_link(candidate: Path) -> bool:
            return candidate == link or real_is_symlink(candidate)

        with patch.object(path_type, "is_symlink", simulated_link):
            inspection = recovery.inspect_recovery()

        self.assertEqual(inspection["backups"], [])

    def test_reparse_backup_root_is_not_trusted_for_discovery(self):
        self.config.write_bytes(b"model = [\n")
        outside = self.sandbox / "outside-backups"
        outside.mkdir()
        (outside / "config.toml.external.bak").write_bytes(b'model = "outside"\n')
        try:
            self.backups.parent.mkdir(parents=True, exist_ok=True)
            self._directory_link(self.backups, outside)
        except OSError as exc:
            self.skipTest(f"directory link creation unavailable: {exc}")

        inspection = recovery.inspect_recovery()

        self.assertEqual(inspection["backups"], [])

    def test_reparse_backup_root_is_not_used_to_store_reset_snapshot(self):
        damaged = b"model = [\n"
        self.config.write_bytes(damaged)
        outside = self.sandbox / "outside-backups"
        outside.mkdir()
        try:
            self.backups.parent.mkdir(parents=True, exist_ok=True)
            self._directory_link(self.backups, outside)
        except OSError as exc:
            self.skipTest(f"directory link creation unavailable: {exc}")

        with self.assertRaises(core.ManagerError):
            recovery.repair_config(
                expected_fingerprint=hashlib.sha256(damaged).hexdigest(),
                reset=True,
            )

        self.assertEqual(self.config.read_bytes(), damaged)
        self.assertEqual(list(outside.rglob("*.original")), [])

    def test_reparse_backup_ancestor_is_not_trusted(self):
        self.config.write_bytes(b"model = [\n")
        outside_state = self.sandbox / "outside-state"
        outside_backups = outside_state / "backups"
        outside_backups.mkdir(parents=True)
        (outside_backups / "config.toml.external.bak").write_bytes(
            b'model = "outside-ancestor"\n'
        )
        linked_state = self.sandbox / "linked-state"
        try:
            self._directory_link(linked_state, outside_state)
        except OSError as exc:
            self.skipTest(f"directory link creation unavailable: {exc}")

        with patch.object(core, "BACKUPS_DIR", linked_state / "backups"):
            inspection = recovery.inspect_recovery()

        self.assertEqual(inspection["backups"], [])

    def test_core_visual_edit_of_utf16_preserves_comments_and_unknown_fields(self):
        source = (
            '# 顶部说明\nmodel = "before" # 模型注释\nunknown_root = "保留✓"\n\n'
            '[custom]\nanswer = 42 # 行内说明\nopaque = ["a", "b"]\n'
        )
        original_raw = source.encode("utf-16")
        self.config.write_bytes(original_raw)
        document = core.codex_config_document()

        self.assertTrue(document["valid"])
        self.assertEqual(document["content"], source)
        self.assertIn("unknown_root", {entry["key"] for entry in document["entries"]})

        with (
            patch.object(core, "_settings_file_lock", return_value=nullcontext()),
            patch.object(core, "load_settings", return_value=core._initial_settings()),
        ):
            result = core.save_codex_config_document(
                {
                    "expectedFingerprint": document["fingerprint"],
                    "updates": [{"path": ["model"], "value": "after"}],
                }
            )

        rendered_raw = self.config.read_bytes()
        rendered = rendered_raw.decode("utf-8")
        parsed = tomllib.loads(rendered)
        self.assertTrue(result["changed"])
        self.assertFalse(rendered_raw.startswith((b"\xff\xfe", b"\xfe\xff")))
        self.assertEqual(parsed["model"], "after")
        self.assertEqual(parsed["unknown_root"], "保留✓")
        self.assertEqual(parsed["custom"], {"answer": 42, "opaque": ["a", "b"]})
        self.assertIn("# 顶部说明", rendered)
        self.assertIn("# 模型注释", rendered)
        self.assertIn("# 行内说明", rendered)
        self.assertEqual(Path(result["backupPath"]).read_bytes(), original_raw)

    def test_core_visual_save_rolls_back_if_tracked_overlay_cannot_rebase(self):
        original = b'model = "before"\nunknown_root = "keep"\n'
        self.config.write_bytes(original)
        document = core.codex_config_document()
        self.overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay_before = {
            "schemaVersion": 1,
            "ownerPid": os.getpid() + 100000,
            "codexHome": str(self.codex_home),
            "files": {"config.toml": {"kind": "config"}},
            "environment": {},
        }
        self.overlay.write_text(json.dumps(overlay_before), encoding="utf-8")

        with (
            patch.object(core, "_settings_file_lock", return_value=nullcontext()),
            patch.object(core, "load_settings", return_value=core._initial_settings()),
        ):
            with self.assertRaises(core.ManagerError):
                core.save_codex_config_document(
                    {
                        "expectedFingerprint": document["fingerprint"],
                        "updates": [{"path": ["model"], "value": "after"}],
                    }
                )

        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(
            json.loads(self.overlay.read_text(encoding="utf-8")), overlay_before
        )


if __name__ == "__main__":
    unittest.main()
