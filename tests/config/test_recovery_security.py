import base64
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_manager.core as core
import agent_manager.storage.recovery as recovery


class RecoverySecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.codex = self.root / "codex"
        self.state = self.codex / "agent-manager"
        self.state.mkdir(parents=True)
        for name, value in {
            "CODEX_HOME": self.codex,
            "STATE_DIR": self.state,
            "CONFIG_FILE": self.codex / "config.toml",
            "AGENTS_FILE": self.codex / "AGENTS.md",
            "AGENTS_DIR": self.codex / "agents",
            "SETTINGS_FILE": self.state / "settings.json",
            "RUNTIME_OVERLAY_FILE": self.state / "runtime-configuration-overlay.json",
            "SECRETS_FILE": self.state / "provider-secrets.json",
        }.items():
            mocked = patch.object(core, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        for mocked in (
            patch.object(core, "dpapi_protect", side_effect=lambda text: b"protected:" + text.encode()),
            patch.object(core, "dpapi_unprotect", side_effect=lambda raw: raw.removeprefix(b"protected:").decode()),
            patch.object(core, "running_codex_processes", return_value=[]),
            patch.object(core, "invalidate_codex_version_cache"),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        core.CONFIG_FILE.write_text('model = "backup"\n', encoding="utf-8")
        core.SETTINGS_FILE.write_text(
            json.dumps({"schemaVersion": core.SCHEMA_VERSION, "accounts": []}), encoding="utf-8"
        )
        core.SECRETS_FILE.write_text('{"accounts": {"test": "encrypted-value"}}', encoding="utf-8")

    def _rewrite_valid_point(self, point_id, manifest, files):
        payload = {
            "format": recovery.FORMAT,
            "version": recovery.VERSION,
            "manifest": manifest,
            "files": {
                key: base64.b64encode(raw).decode("ascii") if raw is not None else None
                for key, raw in files.items()
            },
        }
        protected = base64.b64encode(
            core.dpapi_protect(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        ).decode("ascii")
        header = json.dumps(
            {"format": recovery.FORMAT, "version": recovery.VERSION, "manifest": manifest},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        core.atomic_write_bytes(recovery._point_path(point_id), header.encode() + b"\n" + protected.encode())

    def _write_overlay(self, records):
        core.atomic_write_json(
            core.RUNTIME_OVERLAY_FILE,
            {
                "schemaVersion": 1,
                "codexHome": str(core.CODEX_HOME),
                "ownerPid": os.getpid(),
                "environment": {},
                "files": records,
            },
        )

    @staticmethod
    def _overlay_record(kind, baseline, applied):
        return {
            "kind": kind,
            "baseline": core._overlay_encrypt_bytes(baseline),
            "baselineHash": core._overlay_value_hash(baseline),
            "capturedHash": core._overlay_value_hash(baseline),
            "appliedHash": core._overlay_value_hash(applied),
        }

    def test_authenticated_version_cannot_be_changed_in_plaintext_header(self):
        point = recovery.create()
        path = recovery._point_path(point["id"])
        header, protected = path.read_bytes().split(b"\n", 1)
        document = json.loads(header)
        document["version"] = recovery.VERSION + 1
        path.write_bytes(json.dumps(document).encode() + b"\n" + protected)
        with patch.object(recovery, "VERSION", recovery.VERSION + 1):
            with self.assertRaisesRegex(core.ManagerError, "校验或解密失败"):
                recovery.preview(point["id"])

    def test_restore_point_is_bound_to_its_data_directory(self):
        point = recovery.create()
        document = recovery._document(point["id"])
        other_state = self.codex / "other-manager-state"
        with patch.object(core, "STATE_DIR", other_state):
            with self.assertRaisesRegex(core.ManagerError, "不属于当前"):
                recovery._decode(document)

    def test_preview_fingerprint_rejects_authenticated_point_substitution(self):
        point = recovery.create()
        core.CONFIG_FILE.write_text('model = "live"\n', encoding="utf-8")
        expected = recovery.preview(point["id"])["fingerprint"]
        manifest, files = recovery._decode(recovery._document(point["id"]))
        manifest = copy.deepcopy(manifest)
        files = dict(files)
        replacement = b'model = "different-backup"\n'
        files["codex-config"] = replacement
        row = next(item for item in manifest["files"] if item["key"] == "codex-config")
        row.update({"present": True, "size": len(replacement), "sha256": recovery._hash(replacement)})
        self._rewrite_valid_point(point["id"], manifest, files)
        with self.assertRaisesRegex(core.ManagerError, "预览后已变化"):
            recovery.restore(point["id"], expected)
        self.assertEqual(core.CONFIG_FILE.read_text(encoding="utf-8"), 'model = "live"\n')

    def test_edit_between_restore_snapshot_and_write_is_preserved(self):
        point = recovery.create()
        core.CONFIG_FILE.write_text('model = "previewed"\n', encoding="utf-8")
        expected = recovery.preview(point["id"])["fingerprint"]
        real_read = recovery._read
        raced = False

        def racing_read(path):
            nonlocal raced
            raw = real_read(path)
            if path == core.CONFIG_FILE and not raced:
                raced = True
                core.CONFIG_FILE.write_text('model = "external-edit"\n', encoding="utf-8")
            return raw

        with patch.object(recovery, "_read", side_effect=racing_read):
            with self.assertRaisesRegex(core.ManagerError, "恢复期间"):
                recovery.restore(point["id"], expected)
        self.assertEqual(core.CONFIG_FILE.read_text(encoding="utf-8"), 'model = "external-edit"\n')

    def test_failure_after_atomic_replace_rolls_back_failed_file_and_prior_files(self):
        original_settings = core.SETTINGS_FILE.read_bytes()
        point = recovery.create()
        changed_settings = json.dumps({"schemaVersion": core.SCHEMA_VERSION, "changed": True}).encode()
        core.SETTINGS_FILE.write_bytes(changed_settings)
        live_config = b'model = "live"\n'
        core.CONFIG_FILE.write_bytes(live_config)
        expected = recovery.preview(point["id"])["fingerprint"]
        real_write = core.atomic_write_bytes
        failed = False

        def fail_after_replace(path, content):
            nonlocal failed
            real_write(path, content)
            normalized = content.replace(b"\r\n", b"\n")
            if path == core.CONFIG_FILE and normalized == b'model = "backup"\n' and not failed:
                failed = True
                raise OSError("injected post-replace failure")

        with patch.object(core, "atomic_write_bytes", side_effect=fail_after_replace):
            with self.assertRaisesRegex(core.ManagerError, "已回滚"):
                recovery.restore(point["id"], expected)
        self.assertNotEqual(original_settings, changed_settings)
        self.assertEqual(core.SETTINGS_FILE.read_bytes(), changed_settings)
        self.assertEqual(core.CONFIG_FILE.read_bytes(), live_config)

    def test_unreadable_safety_backup_aborts_before_target_mutation(self):
        point = recovery.create()
        original_point_path = recovery._point_path(point["id"])
        live_config = b'model = "live"\n'
        core.CONFIG_FILE.write_bytes(live_config)
        expected = recovery.preview(point["id"])["fingerprint"]
        real_write = core.atomic_write_bytes

        def silently_corrupt_new_point(path, content):
            real_write(path, content)
            if path.parent == recovery._directory() and path != original_point_path:
                path.write_bytes(b"corrupt")

        with patch.object(core, "atomic_write_bytes", side_effect=silently_corrupt_new_point):
            with self.assertRaises(core.ManagerError):
                recovery.restore(point["id"], expected)
        self.assertEqual(core.CONFIG_FILE.read_bytes(), live_config)
        self.assertEqual(list(recovery._directory().glob("*.cam-backup")), [original_point_path])

    def test_managed_agent_restore_rebases_runtime_overlay(self):
        core.AGENTS_DIR.mkdir()
        agent_path = core.AGENTS_DIR / "cam-simple-1.toml"
        backed_up = b'name = "cam_simple_1"\nmodel = "backup-model"\n'
        agent_path.write_bytes(backed_up)
        point = recovery.create()
        applied = b'name = "cam_simple_1"\nmodel = "runtime-model"\n'
        agent_path.write_bytes(applied)
        baseline = b'name = "cam_simple_1"\nmodel = "old-user-model"\n'
        self._write_overlay(
            {"agents/cam-simple-1.toml": self._overlay_record("managed_agent", baseline, applied)}
        )
        result = recovery.restore(point["id"], recovery.preview(point["id"])["fingerprint"])
        self.assertTrue(result["restored"])
        overlay = core._runtime_overlay_read()
        record = overlay["files"]["agents/cam-simple-1.toml"]
        self.assertEqual(agent_path.read_bytes(), backed_up)
        self.assertEqual(core._overlay_decrypt_bytes(record["baseline"]), backed_up)
        self.assertEqual(record["appliedHash"], core._overlay_value_hash(backed_up))

    def test_overlay_rebase_failure_rolls_back_overlay_and_all_restored_files(self):
        core.AGENTS_DIR.mkdir()
        agent_path = core.AGENTS_DIR / "cam-simple-1.toml"
        agent_backup = b'name = "cam_simple_1"\nmodel = "backup-model"\n'
        agent_path.write_bytes(agent_backup)
        point = recovery.create()
        live_config = b'model = "live"\n'
        live_agent = b'name = "cam_simple_1"\nmodel = "runtime-model"\n'
        core.CONFIG_FILE.write_bytes(live_config)
        agent_path.write_bytes(live_agent)
        self._write_overlay(
            {
                "config.toml": self._overlay_record("config", b'model = "user"\n', live_config),
                "agents/cam-simple-1.toml": self._overlay_record(
                    "managed_agent", b'name = "cam_simple_1"\nmodel = "user"\n', live_agent
                ),
            }
        )
        overlay_before = core.RUNTIME_OVERLAY_FILE.read_bytes()
        expected = recovery.preview(point["id"])["fingerprint"]
        real_rebase = core._runtime_overlay_rebase_user_file

        def fail_after_agent_rebase(path, content):
            result = real_rebase(path, content)
            if path == agent_path:
                raise OSError("injected overlay failure")
            return result

        with patch.object(core, "_runtime_overlay_rebase_user_file", side_effect=fail_after_agent_rebase):
            with self.assertRaisesRegex(core.ManagerError, "已回滚"):
                recovery.restore(point["id"], expected)
        self.assertEqual(core.CONFIG_FILE.read_bytes(), live_config)
        self.assertEqual(agent_path.read_bytes(), live_agent)
        self.assertEqual(core.RUNTIME_OVERLAY_FILE.read_bytes(), overlay_before)

    @unittest.skipUnless(os.name == "nt", "Windows path alias behavior")
    def test_case_aliases_cannot_target_one_agent_file_twice(self):
        point = recovery.create()
        document = recovery._document(point["id"])
        manifest = copy.deepcopy(document["manifest"])
        manifest["files"].extend(
            [
                {"key": "agent:Alias.toml", "label": "子代理 Alias", "present": False, "size": 0, "sha256": None},
                {"key": "agent:alias.toml", "label": "子代理 alias", "present": False, "size": 0, "sha256": None},
            ]
        )
        with self.assertRaisesRegex(core.ManagerError, "重复"):
            recovery._metadata({"format": recovery.FORMAT, "version": recovery.VERSION, "manifest": manifest})

    def test_symbolic_link_target_is_rejected_without_reading_external_file(self):
        outside = self.root / "outside.toml"
        outside.write_text('secret = "outside"\n', encoding="utf-8")
        core.CONFIG_FILE.unlink()
        try:
            os.symlink(outside, core.CONFIG_FILE)
        except OSError as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")
        real_open = Path.open
        def guarded_open(path, *args, **kwargs):
            if path.resolve() == outside.resolve():
                raise AssertionError("must not read the external target")
            return real_open(path, *args, **kwargs)
        with patch.object(Path, "open", guarded_open):
            with self.assertRaisesRegex(core.ManagerError, "符号链接|目录联接|超出 Codex"):
                recovery.create()
        self.assertEqual(outside.read_text(encoding="utf-8"), 'secret = "outside"\n')


if __name__ == "__main__":
    unittest.main()
