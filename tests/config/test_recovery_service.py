import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_manager.core as core
import agent_manager.storage.recovery as recovery


class RecoveryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.codex = self.root / "codex"
        self.state = self.codex / "agent-manager"
        self.state.mkdir(parents=True)
        for name, value in {"CODEX_HOME": self.codex, "STATE_DIR": self.state,
                            "CONFIG_FILE": self.codex / "config.toml", "AGENTS_FILE": self.codex / "AGENTS.md",
                            "AGENTS_DIR": self.codex / "agents", "SETTINGS_FILE": self.state / "settings.json",
                            "RUNTIME_OVERLAY_FILE": self.state / "runtime-configuration-overlay.json",
                            "SECRETS_FILE": self.state / "provider-secrets.json"}.items():
            p = patch.object(core, name, value); p.start(); self.addCleanup(p.stop)
        for p in [patch.object(core, "dpapi_protect", side_effect=lambda text: b"protected:" + text.encode()),
                  patch.object(core, "dpapi_unprotect", side_effect=lambda raw: raw.removeprefix(b"protected:").decode()),
                  patch.object(core, "running_codex_processes", return_value=[]), patch.object(core, "invalidate_codex_version_cache")]:
            p.start(); self.addCleanup(p.stop)
        core.CONFIG_FILE.write_text('model = "before"\n', encoding="utf-8")
        core.SETTINGS_FILE.write_text(json.dumps({"schemaVersion": core.SCHEMA_VERSION, "accounts": []}), encoding="utf-8")
        core.SECRETS_FILE.write_text('{"accounts": {"test": "encrypted-value"}}', encoding="utf-8")

    def test_point_preview_and_restore_round_trip_with_safety_backup(self):
        point = recovery.create(name="Before edit")
        core.CONFIG_FILE.write_text('model = "after"\n', encoding="utf-8")
        preview = recovery.preview(point["id"])
        self.assertEqual(preview["changed"], 1)
        result = recovery.restore(point["id"], preview["fingerprint"])
        self.assertTrue(result["restored"])
        self.assertEqual(core.CONFIG_FILE.read_text(encoding="utf-8"), 'model = "before"\n')
        undo = recovery.preview(result["safetyBackupId"])
        recovery.restore(result["safetyBackupId"], undo["fingerprint"])
        self.assertEqual(core.CONFIG_FILE.read_text(encoding="utf-8"), 'model = "after"\n')

    def test_list_authenticates_metadata_without_exposing_payloads(self):
        recovery.create()
        calls = []
        def unprotect(raw):
            calls.append(raw)
            return raw.removeprefix(b"protected:").decode()
        with patch.object(core, "dpapi_unprotect", side_effect=unprotect):
            result = recovery.list_points()
        self.assertEqual(len(result["points"]), 1)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("encrypted-value", json.dumps(result))

    def test_modified_plaintext_manifest_is_rejected_by_authenticated_payload(self):
        point = recovery.create()
        path = recovery._point_path(point["id"])
        header, payload = path.read_bytes().split(b"\n", 1)
        metadata = json.loads(header)
        metadata["manifest"]["files"][0]["sha256"] = "0" * 64
        path.write_bytes(json.dumps(metadata).encode() + b"\n" + payload)
        listed = recovery.list_points()
        self.assertEqual(listed["points"], [])
        self.assertEqual(listed["invalidCount"], 1)
        with self.assertRaisesRegex(core.ManagerError, "校验或解密失败"):
            recovery.preview(point["id"])

    def test_changed_current_configuration_requires_new_preview(self):
        point = recovery.create()
        preview = recovery.preview(point["id"])
        core.CONFIG_FILE.write_text('model = "external-edit"', encoding="utf-8")
        with self.assertRaisesRegex(core.ManagerError, "预览后已变化"):
            recovery.restore(point["id"], preview["fingerprint"])

    def test_failed_restore_rolls_back_files_already_written(self):
        point = recovery.create()
        core.SETTINGS_FILE.write_text('{"changed": true}', encoding="utf-8")
        core.CONFIG_FILE.write_text('model = "after"', encoding="utf-8")
        preview = recovery.preview(point["id"])
        write = core.atomic_write_bytes
        def fail_config(path, content):
            if path == core.CONFIG_FILE and content.replace(b"\r\n", b"\n") == b'model = "before"\n':
                raise OSError("injected failure")
            return write(path, content)
        with patch.object(core, "atomic_write_bytes", side_effect=fail_config):
            with self.assertRaisesRegex(core.ManagerError, "已回滚"):
                recovery.restore(point["id"], preview["fingerprint"])
        self.assertEqual(core.SETTINGS_FILE.read_text(encoding="utf-8"), '{"changed": true}')

    def test_invalid_config_backup_can_be_kept_but_not_restored(self):
        core.CONFIG_FILE.write_text('invalid = [', encoding="utf-8")
        point = recovery.create()
        core.CONFIG_FILE.write_text('model = "valid"', encoding="utf-8")
        preview = recovery.preview(point["id"])
        with self.assertRaisesRegex(core.ManagerError, "配置格式无效"):
            recovery.restore(point["id"], preview["fingerprint"])
        self.assertEqual(core.CONFIG_FILE.read_text(encoding="utf-8"), 'model = "valid"')

    def test_running_codex_is_not_terminated_by_restore(self):
        point = recovery.create()
        core.CONFIG_FILE.write_text('model = "changed"', encoding="utf-8")
        preview = recovery.preview(point["id"])
        with patch.object(core, "running_codex_processes", return_value=[{"pid": 1}]):
            with self.assertRaisesRegex(core.ManagerError, "先关闭 Codex"):
                recovery.restore(point["id"], preview["fingerprint"])

    def test_restored_user_preferences_become_overlay_restore_baseline(self):
        core.CONFIG_FILE.write_text('personality = "pragmatic"\n', encoding="utf-8")
        point = recovery.create()
        core.CONFIG_FILE.write_text('personality = "friendly"\n', encoding="utf-8")
        original = core.CONFIG_FILE.read_bytes()
        core.atomic_write_json(core.RUNTIME_OVERLAY_FILE, {
            "schemaVersion": 1, "codexHome": str(core.CODEX_HOME), "ownerPid": os.getpid(), "environment": {},
            "files": {"config.toml": {"kind": "config", "baseline": core._overlay_encrypt_bytes(original),
                                      "baselineHash": core._overlay_value_hash(original), "appliedHash": core._overlay_value_hash(original)}},
        })
        recovery.restore(point["id"], recovery.preview(point["id"])["fingerprint"])
        overlay = core._runtime_overlay_read()
        baseline = core._overlay_decrypt_bytes(overlay["files"]["config.toml"]["baseline"])
        self.assertIn(b'personality = "pragmatic"', baseline)

    def test_path_traversal_unknown_scope_and_oversized_file_are_rejected(self):
        with self.assertRaises(core.ManagerError): recovery.preview("../../config")
        with self.assertRaises(core.ManagerError): recovery.create(scope="everything-on-disk")
        with patch.object(recovery, "MAX_FILE_BYTES", 8):
            with self.assertRaisesRegex(core.ManagerError, "超过"):
                recovery.create()


if __name__ == "__main__":
    unittest.main()
