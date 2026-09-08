import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_manager.core as core
import agent_manager.integrations.claude as claude


class ClaudeManagerServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.local = self.root / "Local"
        self.state = self.root / "state"
        self.state.mkdir(parents=True)
        self.patchers = [
            patch.object(claude, "REGISTRY_FILE", self.state / "profiles.json"),
            patch.object(claude, "SECRETS_FILE", self.state / "secrets.json"),
            patch.object(claude, "TRANSACTION_FILE", self.state / "transaction.json"),
            patch.object(claude, "_local_app_data", return_value=self.local),
            patch.object(claude, "_running_claude_desktop_processes", return_value=[]),
            patch.object(claude, "_encrypt", side_effect=lambda value: "enc:" + value),
            patch.object(claude, "_decrypt", side_effect=lambda value: value.removeprefix("enc:")),
        ]
        for item in self.patchers:
            item.start()
        paths = claude.configuration_paths()
        core.atomic_write_json(paths["normalConfig"], {"deploymentMode": "1p", "keep": "normal"})
        core.atomic_write_json(paths["threepConfig"], {"deploymentMode": "1p", "keep": "threep"})
        core.atomic_write_json(paths["meta"], {"entries": [{"id": "other", "name": "Other"}], "appliedId": "other"})

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    def save_demo(self):
        return claude.save_profile(
            {
                "id": "work_api",
                "name": "Work API",
                "baseUrl": "https://api.example.test",
                "apiKey": "secret-value",
                "models": [
                    {"name": "claude-sonnet-5", "supports1m": True},
                    "claude-haiku-4-5",
                ],
            }
        )

    def test_apply_and_restore_preserve_unrelated_claude_settings(self):
        self.save_demo()
        result = claude.apply_profile("work_api")
        self.assertTrue(result["applied"])
        paths = claude.configuration_paths()
        normal = json.loads(paths["normalConfig"].read_text(encoding="utf-8"))
        threep = json.loads(paths["threepConfig"].read_text(encoding="utf-8"))
        profile = json.loads(paths["profile"].read_text(encoding="utf-8"))
        meta = json.loads(paths["meta"].read_text(encoding="utf-8"))
        self.assertEqual(normal, {"deploymentMode": "3p", "keep": "normal"})
        self.assertEqual(threep, {"deploymentMode": "3p", "keep": "threep"})
        self.assertEqual(profile["inferenceGatewayApiKey"], "secret-value")
        self.assertEqual(profile["coworkEgressAllowedHosts"], ["api.example.test"])
        self.assertEqual(meta["appliedId"], claude.PROFILE_ID)
        self.assertIn("other", {item["id"] for item in meta["entries"]})

        restored = claude.restore_official()
        self.assertEqual(restored["profileId"], "official")
        self.assertFalse(paths["profile"].exists())
        normal = json.loads(paths["normalConfig"].read_text(encoding="utf-8"))
        meta = json.loads(paths["meta"].read_text(encoding="utf-8"))
        self.assertEqual(normal["deploymentMode"], "1p")
        self.assertEqual(normal["keep"], "normal")
        self.assertEqual(meta["appliedId"], "other")

    def test_apply_refuses_to_modify_files_while_desktop_is_running(self):
        self.save_demo()
        before = claude.configuration_paths()["normalConfig"].read_bytes()
        with patch.object(claude, "_running_claude_desktop_processes", return_value=[{"pid": 1}]):
            with self.assertRaisesRegex(core.ManagerError, "完全退出 Claude Desktop"):
                claude.apply_profile("work_api")
        self.assertEqual(claude.configuration_paths()["normalConfig"].read_bytes(), before)

    def test_partial_apply_rolls_back_all_four_files(self):
        self.save_demo()
        paths = claude.configuration_paths()
        before = {key: (path.read_bytes() if path.exists() else None) for key, path in paths.items() if key != "library"}
        with patch.object(claude, "_write_meta", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                claude.apply_profile("work_api")
        after = {key: (path.read_bytes() if path.exists() else None) for key, path in paths.items() if key != "library"}
        self.assertEqual(after, before)
        self.assertFalse(claude.TRANSACTION_FILE.exists())

    def test_save_profile_restores_registry_and_secrets_when_registry_write_fails(self):
        self.save_demo()
        before = {
            path: path.read_bytes() if path.exists() else None
            for path in (claude.REGISTRY_FILE, claude.SECRETS_FILE)
        }
        atomic_write_json = core.atomic_write_json

        def fail_registry(path, payload):
            if Path(path) == claude.REGISTRY_FILE:
                raise OSError("registry unavailable")
            return atomic_write_json(path, payload)

        with patch.object(core, "atomic_write_json", side_effect=fail_registry):
            with self.assertRaisesRegex(OSError, "registry unavailable"):
                claude.save_profile(
                    {
                        "id": "backup_api",
                        "name": "Backup API",
                        "baseUrl": "https://backup.example.test",
                        "apiKey": "backup-secret",
                        "models": ["claude-haiku-4-5"],
                    }
                )

        after = {
            path: path.read_bytes() if path.exists() else None
            for path in (claude.REGISTRY_FILE, claude.SECRETS_FILE)
        }
        self.assertEqual(after, before)

    def test_apply_profile_restores_configs_and_registry_when_registry_commit_fails(self):
        self.save_demo()
        paths = claude.configuration_paths()
        transaction_paths = claude._transaction_paths(paths)
        before = {
            key: path.read_bytes() if path.exists() else None
            for key, path in transaction_paths.items()
        }
        atomic_write_json = core.atomic_write_json

        def fail_registry(path, payload):
            if Path(path) == claude.REGISTRY_FILE:
                raise OSError("registry unavailable")
            return atomic_write_json(path, payload)

        with patch.object(core, "atomic_write_json", side_effect=fail_registry):
            with self.assertRaisesRegex(OSError, "registry unavailable"):
                claude.apply_profile("work_api")

        after = {
            key: path.read_bytes() if path.exists() else None
            for key, path in transaction_paths.items()
        }
        self.assertEqual(after, before)
        self.assertFalse(claude.TRANSACTION_FILE.exists())

    def test_delete_profile_restores_registry_and_secrets_when_registry_write_fails(self):
        self.save_demo()
        before = {
            path: path.read_bytes() if path.exists() else None
            for path in (claude.REGISTRY_FILE, claude.SECRETS_FILE)
        }
        atomic_write_json = core.atomic_write_json

        def fail_registry(path, payload):
            if Path(path) == claude.REGISTRY_FILE:
                raise OSError("registry unavailable")
            return atomic_write_json(path, payload)

        with patch.object(core, "atomic_write_json", side_effect=fail_registry):
            with self.assertRaisesRegex(OSError, "registry unavailable"):
                claude.delete_profile("work_api")

        after = {
            path: path.read_bytes() if path.exists() else None
            for path in (claude.REGISTRY_FILE, claude.SECRETS_FILE)
        }
        self.assertEqual(after, before)

    def test_corrupt_transaction_journal_remains_and_blocks_mutation(self):
        journal = claude._snapshot(claude.configuration_paths())
        journal["files"] = [item for item in journal["files"] if item["key"] != "registry"]
        core.atomic_write_json(claude.TRANSACTION_FILE, journal)
        before = claude.TRANSACTION_FILE.read_bytes()

        with self.assertRaisesRegex(core.ManagerError, "事务日志损坏"):
            self.save_demo()

        self.assertEqual(claude.TRANSACTION_FILE.read_bytes(), before)
        self.assertFalse(claude.REGISTRY_FILE.exists())
        self.assertFalse(claude.SECRETS_FILE.exists())

    def test_rejects_plain_http_remote_and_non_claude_direct_model(self):
        with self.assertRaisesRegex(core.ManagerError, "HTTPS"):
            claude.save_profile(
                {"name": "Bad", "baseUrl": "http://example.test", "apiKey": "x", "models": ["claude-sonnet-5"]}
            )
        with self.assertRaisesRegex(core.ManagerError, "不安全"):
            claude.save_profile(
                {"name": "Bad model", "baseUrl": "https://example.test", "apiKey": "x", "models": ["deepseek-v3"]}
            )

    def test_transaction_health_is_read_only_and_marks_valid_journal_recoverable(self):
        journal = claude._snapshot(claude.configuration_paths())
        core.atomic_write_json(claude.TRANSACTION_FILE, journal)
        before = claude.TRANSACTION_FILE.read_bytes()
        with patch.object(claude, "_decrypt") as decrypt:
            result = claude.transaction_health()
        self.assertEqual(result["status"], "warning")
        self.assertTrue(result["recoverable"])
        self.assertEqual(claude.TRANSACTION_FILE.read_bytes(), before)
        decrypt.assert_not_called()

    def test_configuration_health_detects_orphan_secret_without_decryption(self):
        core.atomic_write_json(
            claude.REGISTRY_FILE,
            {"schemaVersion": 1, "activeProfileId": "official", "profiles": []},
        )
        core.atomic_write_json(
            claude.SECRETS_FILE,
            {"scheme": "windows-dpapi-current-user-v1", "profiles": {"orphan": "opaque"}},
        )
        with patch.object(claude, "_decrypt") as decrypt:
            result = claude.configuration_health()
        self.assertFalse(result["healthy"])
        self.assertIn("没有对应", result["issues"][0])
        decrypt.assert_not_called()

    def test_corrupt_registry_blocks_profile_mutation_instead_of_being_overwritten(self):
        claude.REGISTRY_FILE.write_text("{not-json", encoding="utf-8")
        before = claude.REGISTRY_FILE.read_bytes()
        with self.assertRaisesRegex(core.ManagerError, "状态文件.*损坏"):
            self.save_demo()
        self.assertEqual(claude.REGISTRY_FILE.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
