import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_manager.core as core


class BatchProviderCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.patches = [
            patch.object(core, "STATE_DIR", root / "state"),
            patch.object(core, "SETTINGS_FILE", root / "state" / "settings.json"),
            patch.object(core, "SECRETS_FILE", root / "state" / "secrets.json"),
            patch.object(core, "BACKUPS_DIR", root / "state" / "backups"),
            patch.object(core, "CODEX_HOME", root / ".codex"),
            patch.object(core, "CONFIG_FILE", root / ".codex" / "config.toml"),
            patch.object(core, "AGENTS_DIR", root / ".codex" / "agents"),
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    def test_batch_provider_respects_sync_to_api_choice(self):
        exported = {
            "id": "codex_apikey_deadbeef",
            "email": "Imported Relay",
            "auth_mode": "apikey",
            "openai_api_key": "sk-cockpit-relay-secret",
            "api_base_url": "https://relay.example.test/v1",
            "api_provider_mode": "custom",
            "api_provider_id": "cockpit_relay",
            "api_provider_name": "Cockpit Relay",
            "api_model_catalog": ["relay-model-a"],
        }
        with patch.object(core, "fetch_provider_models", side_effect=AssertionError("must use catalog")):
            result = core.import_codex_accounts_batch(
                {
                    "groupId": "relay",
                    "proxyEnabled": True,
                    "selectedIndices": [0],
                    "items": [{"authJson": json.dumps(exported)}],
                }
            )
        self.assertEqual(len(result["importedProviders"]), 1)
        self.assertTrue(result["importedProviders"][0]["proxyEnabled"])
        settings = core.load_settings()
        self.assertIn("cockpit_relay", settings["web2api"]["providerIds"])
        self.assertIn("provider:cockpit_relay", settings["web2api"]["sourceOrder"])

    def test_same_host_provider_accounts_do_not_overwrite_each_other(self):
        records = [
            {
                "auth_mode": "apikey",
                "openai_api_key": f"sk-shared-relay-{index}-secret",
                "api_base_url": "https://shared-relay.example.test/v1",
                "api_provider_mode": "custom",
                "api_provider_name": f"共享中转账号 {index}",
                "api_model_catalog": [f"relay-model-{index}"],
            }
            for index in range(3)
        ]
        preview = core.preview_codex_accounts_batch(
            {"groupId": "relay", "items": [{"authJson": records}]}
        )
        self.assertEqual(preview["valid"], 3)
        self.assertEqual(preview["unique"], 3)
        self.assertEqual(preview["duplicatesInBatch"], 0)

        with patch.object(core, "fetch_provider_models", side_effect=AssertionError("must use catalog")):
            result = core.import_codex_accounts_batch(
                {"groupId": "relay", "items": [{"authJson": records}]}
            )

        self.assertEqual(len(result["importedProviders"]), 3)
        self.assertFalse(result["skippedDuplicates"])
        provider_ids = [item["provider"]["id"] for item in result["importedProviders"]]
        self.assertEqual(len(set(provider_ids)), 3)
        custom_ids = {
            item["id"]
            for item in core.load_settings()["providers"]
            if item.get("kind") == "custom"
        }
        self.assertTrue(set(provider_ids).issubset(custom_ids))
        self.assertEqual(len(set(provider_ids)), 3)
        self.assertEqual(
            {core.load_provider_key(provider_id) for provider_id in provider_ids},
            {f"sk-shared-relay-{index}-secret" for index in range(3)},
        )

    def test_exact_same_provider_key_is_duplicate_but_other_key_on_host_is_not(self):
        shared = {
            "auth_mode": "apikey",
            "openai_api_key": "sk-exact-duplicate-secret",
            "api_base_url": "https://shared-relay.example.test/v1",
            "api_provider_mode": "custom",
            "api_provider_name": "共享中转账号",
            "api_model_catalog": ["relay-model"],
        }
        other = {**shared, "openai_api_key": "sk-independent-key-secret"}
        preview = core.preview_codex_accounts_batch(
            {"groupId": "relay", "items": [{"authJson": [shared, dict(shared), other]}]}
        )
        self.assertEqual(preview["valid"], 3)
        self.assertEqual(preview["unique"], 2)
        self.assertEqual(preview["duplicatesInBatch"], 1)
        self.assertTrue(preview["items"][1]["duplicateInBatch"])
        self.assertFalse(preview["items"][2]["duplicateInBatch"])

        with patch.object(core, "fetch_provider_models", side_effect=AssertionError("must use catalog")):
            result = core.import_codex_accounts_batch(
                {"groupId": "relay", "items": [{"authJson": [shared, dict(shared), other]}]}
            )
        self.assertEqual(len(result["importedProviders"]), 2)
        self.assertEqual(result["skippedDuplicates"], [{"index": 1, "reason": "本批次中 API 账号重复"}])

    def test_provider_preview_error_never_echoes_api_key(self):
        secret = "sk-provider-preview-must-not-leak"
        document = {
            "auth_mode": "apikey",
            "openai_api_key": secret,
            "api_base_url": "https://relay.example.test/v1",
            "api_provider_mode": "custom",
            "api_provider_name": "Relay",
            "api_model_catalog": ["relay-model"],
        }
        with patch.object(
            core,
            "_validated_provider_url",
            side_effect=core.ManagerError(f"api_key={secret}"),
        ):
            preview = core.preview_codex_accounts_batch(
                {"groupId": "relay", "items": [{"authJson": document}]}
            )
        self.assertNotIn(secret, preview["items"][0]["error"])
        self.assertIn("已隐藏", preview["items"][0]["error"])


if __name__ == "__main__":
    unittest.main()
