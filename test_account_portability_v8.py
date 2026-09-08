import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_manager_core as core


class AccountPortabilityV8Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        paths = {
            "STATE_DIR": root / "state",
            "SETTINGS_FILE": root / "state" / "settings.json",
            "SECRETS_FILE": root / "state" / "secrets.json",
            "BACKUPS_DIR": root / "state" / "backups",
            "CODEX_HOME": root / ".codex",
            "CONFIG_FILE": root / ".codex" / "config.toml",
            "AGENTS_FILE": root / ".codex" / "AGENTS.md",
            "AGENTS_DIR": root / ".codex" / "agents",
        }
        self.patches = [patch.object(core, name, value) for name, value in paths.items()]
        self.patches.extend(
            [
                patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
                patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            ]
        )
        for item in self.patches:
            item.start()
        core.ensure_state()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def jwt(payload):
        def encode(value):
            return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

        return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(payload)}.signature"

    @staticmethod
    def agent_private_key(seed=17):
        der = bytes.fromhex("302e020100300506032b657004220420") + bytes([seed]) * 32
        return base64.b64encode(der).decode("ascii")

    def native_oauth_with_agent_cache(self):
        account_id = "native-portable-workspace"
        return {
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": self.jwt(
                    {
                        "aud": [core.CODEX_OAUTH_CLIENT_ID],
                        "sub": "native-portable-user",
                        "email": "native-portable@example.test",
                        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
                    }
                ),
                "access_token": self.jwt(
                    {
                        "client_id": core.CODEX_OAUTH_CLIENT_ID,
                        "sub": "native-portable-user",
                        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
                    }
                ),
                "refresh_token": "native-portable-refresh",
                "account_id": account_id,
            },
            "agent_identity": {
                "agent_runtime_id": "native-portable-runtime",
                "agent_private_key": self.agent_private_key(),
                "account_id": account_id,
                "chatgpt_user_id": "native-portable-user",
                "email": "native-portable@example.test",
                "plan_type": "plus",
            },
            "last_refresh": "2026-09-01T00:00:00Z",
        }

    def test_native_oauth_agent_cache_is_classified_as_chatgpt_identity(self):
        encoded = json.dumps(self.native_oauth_with_agent_cache()).encode("utf-8")

        identity = core._identity_from_auth_bytes(encoded)

        self.assertEqual(identity["authMode"], "chatgpt")
        self.assertEqual(identity["accountId"], "native-portable-workspace")
        self.assertEqual(identity["principalId"], "native-portable-user")
        self.assertTrue(identity["refreshCapable"])

    def test_oauth_claims_are_authoritative_over_auxiliary_agent_cache_metadata(self):
        document = self.native_oauth_with_agent_cache()
        document["agent_identity"].update(
            account_id="cached-other-workspace",
            chatgpt_user_id="cached-other-user",
            email="cached-other@example.test",
        )

        identity = core._identity_from_auth_bytes(json.dumps(document).encode("utf-8"))

        self.assertEqual(identity["accountId"], "native-portable-workspace")
        self.assertEqual(identity["principalId"], "native-portable-user")
        self.assertEqual(identity["email"], "native-portable@example.test")

    def test_native_oauth_agent_cache_survives_import_normalization(self):
        original = self.native_oauth_with_agent_cache()

        encoded, source_type = core._normalize_import_auth_payload(original)
        normalized = json.loads(encoded)

        self.assertEqual(source_type, "codex_auth")
        self.assertEqual(normalized["tokens"], original["tokens"])
        self.assertEqual(normalized["agent_identity"], original["agent_identity"])

    def test_pre_fix_agent_cache_snapshot_fingerprint_remains_loadable(self):
        auth_bytes = json.dumps(self.native_oauth_with_agent_cache()).encode("utf-8")
        old_fingerprint = hashlib.sha256(
            b"agent_identity:native-portable-runtime"
        ).hexdigest()
        snapshot = {
            "version": 1,
            "fingerprint": old_fingerprint,
            "files": [
                {
                    "name": "auth.json",
                    "present": True,
                    "content": base64.b64encode(auth_bytes).decode("ascii"),
                },
                {"name": "cap_sid", "present": False, "content": ""},
            ],
        }

        decoded = core._decode_snapshot_files(snapshot)

        self.assertEqual(decoded["auth.json"], auth_bytes)

    def test_original_id_cannot_overwrite_a_different_existing_identity(self):
        first = core.import_codex_account(
            {
                "authJson": {"OPENAI_API_KEY": "sk-portability-first-secret"},
                "groupId": "official",
                "label": "First",
            }
        )
        second = core.import_codex_account(
            {
                "authJson": {"OPENAI_API_KEY": "sk-portability-second-secret"},
                "groupId": "official",
                "label": "Second",
            }
        )
        settings_before = core.SETTINGS_FILE.read_bytes()
        secrets_before = core.SECRETS_FILE.read_bytes()

        with self.assertRaisesRegex(core.ManagerError, "身份|账号|重复"):
            core.import_codex_account(
                {
                    "authJson": {"OPENAI_API_KEY": "sk-portability-second-secret"},
                    "groupId": "official",
                    "label": "Confused",
                    "originalId": first["id"],
                }
            )

        self.assertEqual(core.SETTINGS_FILE.read_bytes(), settings_before)
        self.assertEqual(core.SECRETS_FILE.read_bytes(), secrets_before)
        accounts = core.load_settings()["accounts"]
        self.assertEqual({item["id"] for item in accounts}, {first["id"], second["id"]})
        self.assertEqual(len({item["fingerprint"] for item in accounts}), 2)

    def test_account_export_round_trip_preserves_secret_models_and_exported_group(self):
        account = core.import_codex_account(
            {
                "authJson": {"OPENAI_API_KEY": "sk-account-round-trip-secret"},
                "groupId": "free",
                "label": "Portable account",
                "models": ["gpt-portable-a", "gpt-portable-b"],
            }
        )
        exported = core.export_codex_account(account["id"])
        core.remove_codex_account(account["id"])

        result = core.import_codex_accounts_batch(
            {"items": [{"authJson": exported}], "deferRefresh": True}
        )

        self.assertFalse(result["failed"])
        self.assertEqual(len(result["imported"]), 1)
        restored = result["imported"][0]
        restored_export = core.export_codex_account(restored["id"])
        self.assertEqual(restored["label"], "Portable account")
        self.assertEqual(restored["models"], ["gpt-portable-a", "gpt-portable-b"])
        self.assertEqual(
            restored_export["authJson"]["OPENAI_API_KEY"],
            "sk-account-round-trip-secret",
        )
        self.assertEqual(restored["groupId"], "free")

    def test_provider_export_round_trip_preserves_key_catalog_endpoint_and_exported_group(self):
        provider = core.save_provider(
            {
                "id": "portable_provider_v8",
                "name": "Portable provider",
                "baseUrl": "https://portable-provider.example.test/v1",
                "resolvedBaseUrl": "https://portable-provider.example.test/v1",
                "envKey": "PORTABLE_PROVIDER_V8_KEY",
                "groupId": "free",
                "models": ["provider-model-a", "provider-model-b"],
            },
            api_key="sk-provider-round-trip-secret",
        )
        exported = core.export_api_provider(provider["id"])
        core.remove_provider(provider["id"])

        with (
            patch.object(core, "fetch_provider_models", side_effect=AssertionError("must use exported catalog")),
            patch.object(core, "fetch_provider_balance", return_value=None),
        ):
            result = core.import_codex_accounts_batch({"items": [{"authJson": exported}]})

        self.assertFalse(result["failed"])
        self.assertEqual(len(result["importedProviders"]), 1)
        restored = result["importedProviders"][0]["provider"]
        self.assertEqual(core.load_provider_key(restored["id"]), "sk-provider-round-trip-secret")
        self.assertEqual(restored["baseUrl"], "https://portable-provider.example.test/v1")
        self.assertEqual(restored["resolvedBaseUrl"], "https://portable-provider.example.test/v1")
        self.assertEqual(restored["models"], ["provider-model-a", "provider-model-b"])
        self.assertEqual(restored["groupId"], "free")

    def test_provider_edit_preserves_unrecognized_persisted_metadata(self):
        provider = core.save_provider(
            {
                "id": "provider_edit_metadata_v8",
                "name": "Before edit",
                "baseUrl": "https://provider-edit.example.test/v1",
                "envKey": "PROVIDER_EDIT_METADATA_V8_KEY",
                "models": ["edit-model"],
            }
        )
        settings = core.load_settings()
        stored = next(item for item in settings["providers"] if item["id"] == provider["id"])
        stored["futureMetadata"] = {"schema": 99, "opaque": ["keep", "me"]}
        stored["_idWasExplicit"] = True
        stored["key"] = "must-not-survive-edit"
        core.save_settings(settings)

        core.save_provider(
            {
                "id": provider["id"],
                "name": "After edit",
                "baseUrl": provider["baseUrl"],
                "envKey": provider["envKey"],
            },
            provider["id"],
        )

        restored = core.provider_by_id(provider["id"])
        self.assertEqual(restored["name"], "After edit")
        self.assertEqual(restored["futureMetadata"], {"schema": 99, "opaque": ["keep", "me"]})
        self.assertNotIn("_idWasExplicit", restored)
        self.assertNotIn("key", restored)

    def test_public_mutation_entrypoints_reject_non_object_payloads_as_manager_errors(self):
        calls = {
            "single account import": lambda: core.import_codex_account(None),
            "batch preview": lambda: core.preview_codex_accounts_batch(None),
            "batch import": lambda: core.import_codex_accounts_batch(None),
            "provider save": lambda: core.save_provider(None),
        }
        for label, call in calls.items():
            with self.subTest(label=label):
                with self.assertRaises(core.ManagerError):
                    call()

    def test_non_string_api_key_field_is_rejected_instead_of_stringified(self):
        malformed = {"OPENAI_API_KEY": {"nested": "not-a-key"}}

        with self.assertRaisesRegex(core.ManagerError, "API Key|字符串|格式"):
            core._normalize_import_auth_payload(malformed)

    def test_api_key_import_uses_same_whitespace_and_size_rules_as_provider_keys(self):
        malformed_values = [
            "sk-key-with embedded-space",
            "sk-key-with\r\ncontrol",
            "sk-" + "x" * 4_097,
        ]
        for value in malformed_values:
            with self.subTest(length=len(value)):
                with self.assertRaisesRegex(core.ManagerError, "API Key|格式"):
                    core._normalize_import_auth_payload({"OPENAI_API_KEY": value})

    def test_declared_auth_mode_cannot_be_silently_changed_to_api_key(self):
        for declared_mode in ("chatgpt", "personalAccessToken", "agentIdentity"):
            with self.subTest(auth_mode=declared_mode):
                with self.assertRaisesRegex(core.ManagerError, "auth_mode|凭据|API Key"):
                    core._normalize_import_auth_payload(
                        {
                            "auth_mode": declared_mode,
                            "OPENAI_API_KEY": "sk-mode-confusion-secret",
                        }
                    )

    def test_oauth_agent_cache_does_not_allow_other_primary_credential_families(self):
        base = self.native_oauth_with_agent_cache()
        variants = (
            {**base, "OPENAI_API_KEY": "sk-mixed-primary-secret"},
            {**base, "personal_access_token": "at-mixed-primary-token"},
            {**base, "sessionToken": "mixed-browser-session"},
        )
        for value in variants:
            with self.subTest(fields=sorted(value)), self.assertRaisesRegex(
                core.ManagerError,
                "同时包含|凭据|混合",
            ):
                core._normalize_import_auth_payload(value)

    def test_all_primary_credential_fields_require_strings(self):
        malformed = (
            {"personal_access_token": {"token": "not-a-string"}},
            {"tokens": {"access_token": ["not-a-string"]}},
            {"tokens": {"id_token": {"jwt": "not-a-string"}}},
            {"tokens": {"refresh_token": 12345}},
            {"sessionToken": ["not-a-string"], "accessToken": "header.payload.signature"},
        )
        for value in malformed:
            with self.subTest(value=value), self.assertRaisesRegex(core.ManagerError, "字符串"):
                core._normalize_import_auth_payload(value)

    def test_explicit_batch_group_overrides_export_and_missing_export_group_is_clear(self):
        account = core.import_codex_account(
            {
                "authJson": {"OPENAI_API_KEY": "sk-group-override-secret"},
                "groupId": "free",
                "label": "Group override",
            }
        )
        exported = core.export_codex_account(account["id"])
        core.remove_codex_account(account["id"])
        overridden = core.import_codex_accounts_batch(
            {"groupId": "official", "items": [{"authJson": exported}], "deferRefresh": True}
        )
        self.assertEqual(overridden["imported"][0]["groupId"], "official")
        core.remove_codex_account(overridden["imported"][0]["id"])

        exported["group"] = {"id": "missing-group", "name": "Missing"}
        missing = core.import_codex_accounts_batch(
            {"items": [{"authJson": exported}], "deferRefresh": True}
        )
        self.assertEqual(missing["imported"], [])
        self.assertEqual(len(missing["failed"]), 1)
        self.assertRegex(missing["failed"][0]["error"], "分组不存在")

    def test_batch_selection_and_provider_secret_boundaries_raise_manager_errors(self):
        with self.assertRaisesRegex(core.ManagerError, "selectedIndices"):
            core.import_codex_accounts_batch(
                {
                    "items": [{"authJson": {"OPENAI_API_KEY": "sk-selection-boundary-secret"}}],
                    "selectedIndices": [0.5],
                }
            )
        with self.assertRaisesRegex(core.ManagerError, "groupId"):
            core.preview_codex_accounts_batch(
                {
                    "groupId": {"id": "official"},
                    "items": [{"authJson": {"OPENAI_API_KEY": "sk-group-boundary-secret"}}],
                }
            )
        with self.assertRaisesRegex(core.ManagerError, "API Key.*字符串"):
            core.save_provider(
                {
                    "id": "strict_provider_key",
                    "name": "Strict key",
                    "baseUrl": "https://strict-provider.example.test/v1",
                    "envKey": "STRICT_PROVIDER_KEY",
                },
                api_key={"secret": "not-a-string"},
            )


if __name__ == "__main__":
    unittest.main()
