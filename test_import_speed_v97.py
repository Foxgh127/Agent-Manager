"""Offline import regression tests: temporary stores and synthetic credentials only."""
import base64
import copy
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

import agent_manager_core as core


class ImportSpeedV97Tests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        paths = {
            "STATE_DIR": root / "state", "SETTINGS_FILE": root / "state/settings.json",
            "SECRETS_FILE": root / "state/secrets.json", "BACKUPS_DIR": root / "state/backups",
            "CODEX_HOME": root / "codex", "CONFIG_FILE": root / "codex/config.toml",
            "AGENTS_FILE": root / "codex/AGENTS.md", "AGENTS_DIR": root / "codex/agents",
        }
        patches = [patch.object(core, name, value) for name, value in paths.items()]
        patches += [
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode()),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode()),
            patch.object(socket.socket, "connect", side_effect=AssertionError("real network forbidden")),
            patch.object(core.subprocess, "run", side_effect=AssertionError("runtime probe forbidden")),
            patch.object(core, "_fetch_chatgpt_json", side_effect=AssertionError("remote preview forbidden")),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        core.ensure_state()

    @staticmethod
    def jwt(payload):
        encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
        return f"{encode({'alg': 'none'})}.{encode(payload)}.testsignature"

    def official(self, index=0, models=None):
        claims = {
            "exp": 2_100_000_000, "sub": f"speed-user-{index}", "email": f"speed-{index}@example.test",
            "client_id": core.CODEX_OAUTH_CLIENT_ID,
            "https://api.openai.com/auth": {"chatgpt_account_id": f"speed-account-{index}", "chatgpt_plan_type": "pro"},
        }
        token = self.jwt(claims)
        return {
            "format": "codex-agent-manager-account", "version": 1, "label": "Pro20x export",
            "sourceType": "codex_auth", "group": {"id": "official"},
            "subscriptionExpiresAt": "2030-01-01T00:00:00Z",
            "models": models if models is not None else ["gpt-test"],
            "authJson": {"OPENAI_API_KEY": None, "tokens": {
                "access_token": token, "id_token": token, "refresh_token": f"fake-refresh-{index}",
                "account_id": f"speed-account-{index}",
            }},
        }

    @staticmethod
    def relay():
        return {
            "format": "codex-agent-manager-relay-account", "version": 1,
            "relayAccount": {
                "id": "speed_relay", "groupId": "relay", "selectedKeyId": "key-1",
                "selectedEndpointId": "default", "keySecrets": {"key-1": "sk-fake-speed-secret"},
                "preview": {
                    "siteName": "Offline relay", "origin": "https://relay.example.test",
                    "portalUrl": "https://relay.example.test/dashboard", "adapter": "sub2api",
                    "integrationKind": "sub2api", "user": {"id": "user-1"},
                    "models": ["gpt-test"], "groups": [],
                    "keys": [{"id": "key-1", "name": "Codex", "active": True, "models": ["gpt-test"]}],
                    "apiEndpoints": [{"id": "default", "baseUrl": "https://relay.example.test/v1"}],
                },
                "dashboardSession": {
                    "origin": "https://relay.example.test", "adapter": "sub2api",
                    "accessToken": "fake-dashboard-access", "refreshToken": "fake-dashboard-refresh",
                },
            },
        }

    def test_official_export_keeps_metadata_without_network_or_runtime(self):
        original = self.official()
        before = copy.deepcopy(original)
        docs = core._batch_documents({"items": [original]})
        self.assertEqual(docs[0]["models"], ["gpt-test"])
        self.assertEqual(docs[0]["subscriptionExpiresAt"], original["subscriptionExpiresAt"])
        self.assertEqual(docs[0]["authJson"]["format"], original["format"])
        result = core.preview_codex_accounts_batch({"items": [original]})
        self.assertEqual(result["valid"], 1)
        self.assertEqual(result["items"][0]["remoteValidation"], "unverified")
        self.assertEqual(result["remoteChecked"], 0)
        self.assertEqual(original, before)

    def test_legacy_manual_plan_display_is_ignored_without_losing_credentials(self):
        original = self.official()
        original["planDisplay"] = {"variant":"pro20x", "confirmedAt":core.now_iso(), "expiresAt":original["subscriptionExpiresAt"]}
        imported = core.import_codex_accounts_batch({"items":[original], "deferRefresh":True})["imported"][0]
        stored = next(item for item in core.load_settings()["accounts"] if item["id"] == imported["id"])
        self.assertNotIn("planVariantOverride", stored)
        self.assertNotIn("planDisplay", core.export_codex_account(imported["id"])["codex_agent_manager"])

    def test_duplicate_text_and_credentials_parsed_once_per_request(self):
        raw = json.dumps(self.official(), indent=2)
        payload = {"items": [{"authJson": raw}] * 40}
        with patch.object(core, "_decode_many_json_documents", wraps=core._decode_many_json_documents) as decode, patch.object(core, "_normalize_import_auth_payload", wraps=core._normalize_import_auth_payload) as normalize:
            started = time.perf_counter()
            preview = core.preview_codex_accounts_batch(payload)
            elapsed = time.perf_counter() - started
            self.assertEqual(decode.call_count, 1)
            self.assertEqual(normalize.call_count, 1)
            self.assertEqual(preview["unique"], 1)
            self.assertEqual(preview["duplicatesInBatch"], 39)
            core.preview_codex_accounts_batch(payload)
            self.assertEqual(decode.call_count, 2)
            self.assertEqual(normalize.call_count, 2)
        print(f"v9.7 repeated official 40: {elapsed:.4f}s; decode=1 normalize=1 network=0 runtime=0")

    def test_large_catalog_avoids_generic_line_scan_and_auth_catalog_copy(self):
        document = self.official(models=[f"gpt-model-{index}" for index in range(30_000)])
        raw = json.dumps(document, indent=2)
        with patch.object(core.re, "match", side_effect=AssertionError("generic line scanner used")):
            started = time.perf_counter()
            result = core.preview_codex_accounts_batch({"items": [{"authJson": raw}]})
            elapsed = time.perf_counter() - started
        self.assertEqual(result["valid"], 1)
        docs = core._batch_documents({"items": [document]})
        self.assertNotIn("models", docs[0]["authJson"])
        self.assertEqual(len(docs[0]["models"]), 2_000)
        print(f"v9.7 official 30,000 catalog rows: {elapsed:.4f}s; network=0 runtime=0")

    def test_relay_nested_batch_and_session_import_without_online_login_claim(self):
        exported = self.relay()
        payload = {"items": [{"authJson": json.dumps({"accounts": [exported] * 20})}]}
        with patch.object(core, "_normalize_relay_account_snapshot", wraps=core._normalize_relay_account_snapshot) as normalize:
            started = time.perf_counter()
            preview = core.preview_codex_accounts_batch(payload)
            elapsed = time.perf_counter() - started
            self.assertEqual(normalize.call_count, 1)
        self.assertEqual(preview["valid"], 20)
        self.assertEqual(preview["unique"], 1)
        self.assertEqual(preview["items"][0]["remoteValidation"], "unverified")
        result = core.import_codex_accounts_batch(payload)
        self.assertFalse(result["failed"])
        self.assertEqual(len(result["importedRelayAccounts"]), 1)
        self.assertEqual(len(result["skippedDuplicates"]), 19)
        self.assertTrue(result["importedRelayAccounts"][0]["dashboardSessionImported"])
        self.assertEqual(core.load_relay_account_dashboard_session("speed_relay")["refreshToken"], "fake-dashboard-refresh")
        print(f"v9.7 repeated website export 20: {elapsed:.4f}s; relay normalize=1 network=0 runtime=0")

    def test_repeat_import_preserves_identity_and_returns_background_refresh_ids(self):
        payload = {"items": [self.official()] * 12, "deferRefresh": True}
        with patch.object(core, "_normalize_import_auth_payload", wraps=core._normalize_import_auth_payload) as normalize, patch.object(core, "_snapshot_from_bytes", wraps=core._snapshot_from_bytes) as snapshot, patch.object(core, "refresh_codex_accounts", side_effect=AssertionError("synchronous refresh")):
            first = core.import_codex_accounts_batch(payload)
            self.assertEqual(normalize.call_count, 1)
            self.assertEqual(snapshot.call_count, 1)
            second = core.import_codex_accounts_batch(payload)
        self.assertFalse(first["failed"])
        self.assertEqual(len(first["skippedDuplicates"]), 11)
        self.assertEqual(first["refreshAccountIds"], second["refreshAccountIds"])
        self.assertEqual(len(core.load_settings()["accounts"]), 1)
        self.assertEqual(core.load_settings()["accounts"][0]["models"], ["gpt-test"])
        exported = core.export_codex_account(first["imported"][0]["id"])
        self.assertEqual(core.preview_codex_accounts_batch({"items": [exported]})["valid"], 1)

    def test_explicit_remote_validation_still_rejects_revoked_credentials(self):
        with patch.object(core, "_fetch_chatgpt_json", side_effect=core.ManagerError("HTTP 401: token invalidated")) as fetch:
            result = core.preview_codex_accounts_batch({"items": [self.official()] * 2, "validateRemote": True})
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["remoteInvalid"], 2)
        self.assertIn("401", result["items"][0]["error"])

    def test_changed_token_same_identity_is_never_cached_as_old_token(self):
        old, fresh = self.official(), self.official()
        fresh["authJson"]["tokens"]["access_token"] += "changed"
        with patch.object(core, "_normalize_import_auth_payload", wraps=core._normalize_import_auth_payload) as normalize, patch.object(core, "_fetch_chatgpt_json", return_value={}) as fetch:
            core.preview_codex_accounts_batch({"items": [old, fresh], "validateRemote": True})
        self.assertEqual(normalize.call_count, 2)
        self.assertEqual(fetch.call_count, 2)

    def test_malformed_exports_and_foreign_dashboard_origin_fail_closed(self):
        bad = self.official()
        bad["authJson"]["tokens"]["access_token"] = {"malformed": True}
        result = core.preview_codex_accounts_batch({"items": [bad]})
        self.assertEqual(result["invalid"], 1)
        self.assertIn("access_token", result["items"][0]["error"])
        bad = self.official()
        bad["version"] = 99
        with self.assertRaisesRegex(core.ManagerError, "版本不受支持"):
            core.preview_codex_accounts_batch({"items": [bad]})
        relay = self.relay()
        relay["relayAccount"]["dashboardSession"]["origin"] = "https://foreign.example.test"
        with self.assertRaisesRegex(core.ManagerError, "同一站点"):
            core.preview_codex_accounts_batch({"items": [relay]})


if __name__ == "__main__":
    unittest.main()
