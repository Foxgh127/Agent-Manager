from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_manager_core as core
import relay_portal_service as relay


def api_response(data, *, status=200, success=True):
    return {
        "status": status,
        "json": True,
        "body": {"success": success, "data": data},
    }


class RelayCatalogCompletenessV93Tests(unittest.TestCase):
    def test_reported_total_must_be_covered_before_catalog_is_authoritative(self):
        rows = [{"id": index + 1} for index in range(200)]
        complete, total = relay._key_catalog_status(
            [api_response({"items": rows, "total": 201})],
            page_size=200,
        )
        self.assertFalse(complete)
        self.assertEqual(total, 201)

    def test_full_page_without_total_is_ambiguous_even_with_empty_index_alias(self):
        rows = [{"id": index + 1} for index in range(100)]
        complete, total = relay._key_catalog_status(
            [api_response({"items": rows}), api_response({"items": []})],
            page_size=100,
        )
        self.assertFalse(complete)
        self.assertIsNone(total)

    def test_short_page_and_empty_catalog_are_complete(self):
        self.assertEqual(
            relay._key_catalog_status(
                [api_response({"items": [{"id": 1}]})],
                page_size=100,
            ),
            (True, 1),
        )
        self.assertEqual(
            relay._key_catalog_status(
                [api_response({"items": []})],
                page_size=100,
            ),
            (True, 0),
        )

    def test_normalizer_cannot_promote_truncated_pages_with_explicit_true_flag(self):
        rows = [{"id": index + 1, "name": f"key-{index + 1}"} for index in range(100)]
        preview, _secrets = relay.normalize_probe_result(
            {
                "adapter": "new-api",
                "status": api_response({"server_address": "https://relay.example.test"}),
                "user": api_response({"id": 42, "quota": 1_000_000}),
                "keys": [api_response({"items": rows, "total": 101})],
                "models": [],
                "keysAuthoritative": True,
            },
            portal_url="https://relay.example.test/dashboard",
        )
        self.assertFalse(preview["keysAuthoritative"])
        self.assertFalse(preview["keysCatalogComplete"])
        self.assertEqual(preview["keysCatalogTotal"], 101)


class RelaySavedKeyDeletionV93Tests(unittest.TestCase):
    def account(self, adapter="new-api"):
        return {
            "id": "relay-test",
            "origin": "https://relay.example.test",
            "portalUrl": "https://relay.example.test/dashboard",
            "adapter": adapter,
            "user": {"id": "42", "email": "alice@example.test"},
            "keys": [{"id": "17", "name": "Codex"}],
        }

    def session(self, adapter="new-api"):
        return {
            "origin": "https://relay.example.test",
            "portalUrl": "https://relay.example.test/dashboard",
            "adapter": adapter,
            "authMode": "bearer",
            "userId": "42",
            "accessToken": "saved-access",
            "cookies": [],
        }

    def preview(self, adapter="new-api", *, keys=None):
        return {
            "origin": "https://relay.example.test",
            "portalUrl": "https://relay.example.test/dashboard",
            "adapter": adapter,
            "user": {"id": "42", "email": "alice@example.test"},
            "keys": [{"id": "17", "name": "Codex"}] if keys is None else keys,
            "keysAuthoritative": True,
            "keysCatalogComplete": True,
            "keysCatalogTotal": 1 if keys is None else len(keys),
        }

    def test_local_scope_never_reads_or_mutates_website(self):
        service = relay.RelayPortalService()
        account = self.account()
        with (
            patch.object(service, "_saved_account", return_value=(account, {})),
            patch.object(relay.core, "relay_account_key_configured", return_value=True),
            patch.object(service, "_scan_saved_account") as scanned,
            patch.object(relay, "_saved_json_request") as requested,
            patch.object(
                relay.core,
                "remove_relay_account_key",
                return_value={
                    "account": {**account, "keys": []},
                    "removedKeyId": "17",
                    "requiresReapply": True,
                    "warnings": ["provider disabled"],
                },
            ) as removed,
        ):
            result = service.delete_saved_key("relay-test", "17")

        scanned.assert_not_called()
        requested.assert_not_called()
        removed.assert_called_once_with("relay-test", "17")
        self.assertEqual(result["scope"], "local")
        self.assertTrue(result["localRemoved"])
        self.assertFalse(result["remoteDeleted"])
        self.assertTrue(result["requiresReapply"])

    def test_website_scope_uses_only_verified_adapter_delete_routes(self):
        cases = (
            ("new-api", "/api/token/17", {"Authorization": "Bearer saved-access", "New-Api-User": "42"}),
            ("sub2api", "/api/v1/keys/17", {"Authorization": "Bearer saved-access", "X-User-UI-Request": "1"}),
        )
        for adapter, expected_path, expected_headers in cases:
            with self.subTest(adapter=adapter):
                service = relay.RelayPortalService()
                account = self.account(adapter)
                session = self.session(adapter)
                preview = self.preview(adapter)
                with (
                    patch.object(service, "_saved_account", return_value=(account, {})),
                    patch.object(relay.core, "relay_account_key_configured", return_value=True),
                    patch.object(
                        service,
                        "_scan_saved_account",
                        return_value=(preview, {}, session),
                    ) as scanned,
                    patch.object(
                        relay,
                        "_saved_json_request",
                        return_value=api_response({"message": "deleted"}),
                    ) as requested,
                    patch.object(
                        relay.core,
                        "remove_relay_account_key",
                        return_value={
                            "account": {**account, "keys": []},
                            "removedKeyId": "17",
                            "requiresReapply": False,
                            "warnings": [],
                        },
                    ) as removed,
                ):
                    result = service.delete_saved_key(
                        "relay-test",
                        "17",
                        delete_remote=True,
                    )

                scanned.assert_called_once_with(account, full=True)
                requested.assert_called_once_with(
                    session,
                    expected_path,
                    method="DELETE",
                    headers=expected_headers,
                )
                removed.assert_called_once_with("relay-test", "17")
                self.assertTrue(result["remoteDeleted"])
                self.assertTrue(result["localRemoved"])
                self.assertEqual(result["remoteOutcome"], "deleted")

    def test_incomplete_catalog_and_remote_rejection_preserve_local_key(self):
        service = relay.RelayPortalService()
        account = self.account()
        incomplete = {
            **self.preview(),
            "keysAuthoritative": False,
            "keysCatalogComplete": False,
        }
        with (
            patch.object(service, "_saved_account", return_value=(account, {})),
            patch.object(relay.core, "relay_account_key_configured", return_value=True),
            patch.object(
                service,
                "_scan_saved_account",
                return_value=(incomplete, {}, self.session()),
            ),
            patch.object(relay, "_saved_json_request") as requested,
            patch.object(relay.core, "remove_relay_account_key") as removed,
        ):
            with self.assertRaisesRegex(core.ManagerError, "未完整返回"):
                service.delete_saved_key("relay-test", "17", delete_remote=True)
        requested.assert_not_called()
        removed.assert_not_called()

        with (
            patch.object(service, "_saved_account", return_value=(account, {})),
            patch.object(relay.core, "relay_account_key_configured", return_value=True),
            patch.object(
                service,
                "_scan_saved_account",
                return_value=(self.preview(), {}, self.session()),
            ),
            patch.object(
                relay,
                "_saved_json_request",
                return_value={
                    "status": 409,
                    "json": True,
                    "body": {"success": False, "message": "cannot delete"},
                },
            ),
            patch.object(relay.core, "remove_relay_account_key") as removed,
        ):
            with self.assertRaisesRegex(core.ManagerError, "cannot delete"):
                service.delete_saved_key("relay-test", "17", delete_remote=True)
        removed.assert_not_called()

    def test_login_failure_is_actionable_and_keeps_both_copies(self):
        service = relay.RelayPortalService()
        account = self.account()
        with (
            patch.object(service, "_saved_account", return_value=(account, {})),
            patch.object(relay.core, "relay_account_key_configured", return_value=True),
            patch.object(
                service,
                "_scan_saved_account",
                side_effect=relay.DashboardLoginRequired("请重新登录"),
            ),
            patch.object(relay, "_saved_json_request") as requested,
            patch.object(relay.core, "remove_relay_account_key") as removed,
        ):
            result = service.delete_saved_key("relay-test", "17", delete_remote=True)

        self.assertTrue(result["requiresLogin"])
        self.assertFalse(result["remoteDeleted"])
        self.assertFalse(result["localRemoved"])
        requested.assert_not_called()
        removed.assert_not_called()

    def test_remote_success_local_failure_is_reported_as_partial_success(self):
        service = relay.RelayPortalService()
        account = self.account()
        with (
            patch.object(service, "_saved_account", return_value=(account, {})),
            patch.object(relay.core, "relay_account_key_configured", return_value=True),
            patch.object(
                service,
                "_scan_saved_account",
                return_value=(self.preview(), {}, self.session()),
            ),
            patch.object(relay, "_saved_json_request", return_value=api_response({})),
            patch.object(
                relay.core,
                "remove_relay_account_key",
                side_effect=core.ManagerError("disk busy"),
            ),
        ):
            result = service.delete_saved_key("relay-test", "17", delete_remote=True)

        self.assertTrue(result["remoteDeleted"])
        self.assertFalse(result["localRemoved"])
        self.assertIn("disk busy", result["localError"])
        self.assertIn("网站上的 API Key 已不可用", result["warnings"][-1])

    def test_remote_404_or_permission_rejection_does_not_prove_key_absence(self):
        for status in (403, 404):
            with self.subTest(status=status):
                service = relay.RelayPortalService()
                with (
                    patch.object(service, "_saved_account", return_value=(self.account(), {})),
                    patch.object(relay.core, "relay_account_key_configured", return_value=True),
                    patch.object(service, "_scan_saved_account", return_value=(self.preview(), {}, self.session())),
                    patch.object(relay, "_saved_json_request", return_value=api_response({}, status=status, success=False)),
                    patch.object(relay.core, "remove_relay_account_key") as removed,
                ):
                    with self.assertRaises(core.ManagerError):
                        service.delete_saved_key("relay-test", "17", delete_remote=True)
                    removed.assert_not_called()

    def test_non_numeric_remote_id_never_reaches_adapter_endpoint(self):
        service = relay.RelayPortalService()
        account = {**self.account(), "keys": [{"id": "../../17"}]}
        preview = {**self.preview(), "keys": [{"id": "../../17"}]}
        with (
            patch.object(service, "_saved_account", return_value=(account, {})),
            patch.object(relay.core, "relay_account_key_configured", return_value=True),
            patch.object(
                service,
                "_scan_saved_account",
                return_value=(preview, {}, self.session()),
            ),
            patch.object(relay, "_saved_json_request") as requested,
            patch.object(relay.core, "remove_relay_account_key") as removed,
        ):
            with self.assertRaisesRegex(core.ManagerError, "正整数"):
                service.delete_saved_key("relay-test", "../../17", delete_remote=True)
        requested.assert_not_called()
        removed.assert_not_called()


class RelaySnapshotReconciliationV93Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / ".codex"
        self.originals = {}
        paths = {
            "CODEX_HOME": self.root,
            "CONFIG_FILE": self.root / "config.toml",
            "AGENTS_FILE": self.root / "AGENTS.md",
            "AGENTS_DIR": self.root / "agents",
            "STATE_DIR": self.root / "agent-manager",
            "SETTINGS_FILE": self.root / "agent-manager" / "settings.json",
            "MODEL_CATALOG_FILE": self.root / "agent-manager" / "model-catalog.json",
            "MODELS_CACHE_FILE": self.root / "models_cache.json",
            "SECRETS_FILE": self.root / "agent-manager" / "provider-secrets.json",
            "BACKUPS_DIR": self.root / "agent-manager" / "backups",
            "RUNTIME_OVERLAY_FILE": self.root / "agent-manager" / "runtime-overlay.json",
            "ACCOUNT_ACTIVATION_HISTORY_FILE": self.root / "agent-manager" / "activation.json",
            "MANAGED_CODEX_RUNTIME_DIR": self.root / "agent-manager" / "runtime" / "codex",
            "LEGACY_STATE_DIR": self.root / "legacy-switchboard",
            "LEGACY_PROFILE_FILE": self.root / "legacy-gateway.config.toml",
        }
        for name, value in paths.items():
            self.originals[name] = getattr(core, name)
            setattr(core, name, value)
        self.root.mkdir(parents=True)
        core.CONFIG_FILE.write_text("", encoding="utf-8")
        core.AGENTS_FILE.write_text("", encoding="utf-8")
        self.protect = patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8"))
        self.unprotect = patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8"))
        self.protect.start()
        self.unprotect.start()

    def tearDown(self):
        self.unprotect.stop()
        self.protect.stop()
        for name, value in self.originals.items():
            setattr(core, name, value)
        self.temp.cleanup()

    def preview(self):
        return {
            "adapter": "sub2api",
            "adapterLabel": "Sub2API",
            "siteName": "Relay",
            "portalUrl": "https://relay.example.test/dashboard",
            "origin": "https://relay.example.test",
            "integrationKind": "sub2api",
            "user": {"id": "42", "email": "alice@example.test"},
            "balance": {"remaining": 8, "used": 2, "currency": "USD"},
            "models": ["gpt-test"],
            "groups": [
                {"id": "1", "name": "Standard", "platform": "openai", "active": True, "rateMultiplier": 0.1},
                {"id": "2", "name": "Economy", "platform": "openai", "active": True, "rateMultiplier": 0.2},
            ],
            "keys": [
                {"id": "17", "name": "Primary", "active": True, "group": "Standard", "groupId": "1", "groupPlatform": "openai", "groupRateMultiplier": 0.1},
                {"id": "18", "name": "Backup", "active": True, "group": "Economy", "groupId": "2", "groupPlatform": "openai", "groupRateMultiplier": 0.2},
            ],
            "apiEndpoints": [
                {"id": "default", "name": "Default", "baseUrl": "https://relay.example.test/v1", "modelsEndpoint": "https://relay.example.test/v1/models", "balanceEndpoint": "https://relay.example.test/v1/usage", "isDefault": True}
            ],
            "defaultEndpointId": "default",
            "keysAuthoritative": True,
            "keysCatalogComplete": True,
            "keysCatalogTotal": 2,
            "groupsAuthoritative": True,
        }

    def import_account(self):
        imported = core.import_relay_account(
            self.preview(),
            {"17": "sk-primary-secret", "18": "sk-backup-secret"},
            selected_key_id="17",
        )
        account = imported["account"]
        settings = core.load_settings()
        settings["modelWorkspace"]["activeSourceId"] = f"provider:{account['providerId']}"
        provider = next(
            item for item in settings["providers"] if item.get("id") == account["providerId"]
        )
        provider["appliedRuntimeRevision"] = provider["runtimeRevision"]
        core.save_settings(settings)
        return account

    def test_selected_remote_deletion_rotates_provider_and_syncs_group_metadata(self):
        account = self.import_account()
        refreshed = self.preview()
        refreshed["groups"][1].update({"name": "Website Edited", "rateMultiplier": 0.35})
        refreshed["keys"] = [
            {
                **refreshed["keys"][1],
                "group": "Website Edited",
                "groupRateMultiplier": 0.35,
            }
        ]
        refreshed["keysCatalogTotal"] = 1

        result = core.sync_relay_account_snapshot(account["id"], refreshed)

        self.assertEqual(result["removedKeyIds"], ["17"])
        self.assertTrue(result["selectedKeyChanged"])
        self.assertTrue(result["requiresReapply"])
        self.assertEqual(result["account"]["selectedKeyId"], "18")
        self.assertEqual(result["account"]["groupId"], "relay")
        self.assertEqual(result["account"]["groups"][1]["name"], "Website Edited")
        provider = core.provider_by_id(account["providerId"])
        self.assertEqual(provider["relayKeyId"], "18")
        self.assertEqual(provider["relayGroupName"], "Website Edited")
        self.assertEqual(provider["relayRateMultiplier"], 0.35)
        self.assertEqual(core.load_provider_key(account["providerId"]), "sk-backup-secret")
        self.assertFalse(core.relay_account_key_configured(account["id"], "17"))

    def test_local_only_removal_is_not_reimported_by_later_full_refresh(self):
        account = self.import_account()

        removed = core.remove_relay_account_key(account["id"], "18")
        self.assertFalse(removed["needsKey"])
        self.assertEqual([item["id"] for item in removed["account"]["keys"]], ["17"])

        refreshed = core.sync_relay_account_snapshot(account["id"], self.preview())
        self.assertEqual([item["id"] for item in refreshed["account"]["keys"]], ["17"])
        self.assertFalse(core.relay_account_key_configured(account["id"], "18"))

    def test_incomplete_snapshot_never_deletes_then_complete_empty_disables_provider(self):
        account = self.import_account()
        partial = self.preview()
        partial["keys"] = []
        partial["keysAuthoritative"] = True
        partial["keysCatalogComplete"] = False
        partial["keysCatalogTotal"] = 3
        partial["groups"] = [{"id": "2", "name": "Partial", "platform": "openai"}]
        partial["groupsAuthoritative"] = False

        preserved = core.sync_relay_account_snapshot(account["id"], partial)

        self.assertFalse(preserved["keysAuthoritative"])
        self.assertEqual(preserved["removedKeyCount"], 0)
        self.assertEqual([item["id"] for item in preserved["account"]["keys"]], ["17", "18"])
        self.assertEqual([item["name"] for item in preserved["account"]["groups"]], ["Standard", "Economy"])
        self.assertTrue(core.relay_account_key_configured(account["id"], "17"))

        empty = self.preview()
        empty["keys"] = []
        empty["keysCatalogTotal"] = 0
        emptied = core.sync_relay_account_snapshot(account["id"], empty)

        self.assertEqual(emptied["removedKeyCount"], 2)
        self.assertTrue(emptied["providerDisabled"])
        self.assertTrue(emptied["requiresReapply"])
        self.assertEqual(emptied["account"]["keys"], [])
        self.assertEqual(emptied["account"]["selectedKeyId"], "")
        provider = core.provider_by_id(account["providerId"])
        self.assertEqual(provider["modelDiscoveryState"], "disabled")
        self.assertEqual(provider["models"], [])
        self.assertFalse(core.provider_key_configured(account["providerId"]))
        source = next(
            item
            for item in core.model_sources(local_models=[])
            if item["recordId"] == account["providerId"]
        )
        self.assertFalse(source["available"])
        self.assertNotIn(account["providerId"], core.load_settings()["web2api"]["providerIds"])


if __name__ == "__main__":
    unittest.main()
