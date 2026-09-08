"""Isolated regressions for v9.7 workbench service integrations."""

import copy
import io
import json
import unittest
import urllib.error
from contextlib import nullcontext
from unittest.mock import Mock, patch

import agent_manager_app as app
import model_preferences
import session_visibility_service as visibility
import test_app_workbench_routes as routes
import test_model_compatibility_v96 as compatibility


class WorkbenchIntegrationV97Tests(unittest.TestCase):
    setUp = routes.WorkbenchRouteTests.setUp
    tearDown = routes.WorkbenchRouteTests.tearDown
    request = routes.WorkbenchRouteTests.request

    def assert_api_error(self, path, payload, status=400):
        with self.assertRaises(urllib.error.HTTPError) as failed:
            self.request(path, method="POST", payload=payload)
        with failed.exception as response:
            self.assertEqual(response.code, status)
            return json.loads(response.read())

    def test_visibility_inspection_and_backup_list_are_returned(self):
        with (
            patch.object(visibility, "inspect", return_value={"token": "review"}) as inspect,
            patch.object(visibility, "list_backups", return_value={"backups": [{"id": "b"}]}) as backups,
        ):
            result = self.request("/api/sessions/visibility?mode=full")
            self.assertEqual(result["inspection"], {"token": "review"})
            self.assertEqual(result["backups"], [{"id": "b"}])
            inspect.assert_called_once_with(mode="full")
            backups.assert_called_once_with()
            inspect.reset_mock()
            self.request("/api/sessions/visibility")
            inspect.assert_called_once_with(mode="quick")

    def test_visibility_mutations_forward_review_token_mode_and_backup(self):
        with (
            patch.object(visibility, "repair", return_value={"changed": True}) as repair,
            patch.object(visibility, "restore", return_value={"restored": True}) as restore,
        ):
            self.assertTrue(self.request("/api/sessions/visibility/repair", method="POST", payload={"expectedToken": "review", "mode": "full"})["result"]["changed"])
            repair.assert_called_once_with("review", mode="full")
            repair.reset_mock()
            self.request("/api/sessions/visibility/repair", method="POST", payload={"expectedToken": "quick-review"})
            repair.assert_called_once_with("quick-review", mode="quick")
            self.assertTrue(self.request("/api/sessions/visibility/restore", method="POST", payload={"backupId": "backup-1"})["result"]["restored"])
            restore.assert_called_once_with("backup-1")

    def test_visibility_validation_errors_and_unexpected_errors_are_safe(self):
        for operation in ("repair", "restore"):
            path = "/api/sessions/visibility/" + operation
            with self.subTest(operation=operation), patch.object(visibility, operation, side_effect=app.core.ManagerError("Review is stale")):
                self.assertEqual(self.assert_api_error(path, {})["error"], "Review is stale")
            with patch.object(visibility, operation, side_effect=RuntimeError("secret credentials.json")):
                result = self.assert_api_error(path, {}, status=500)
                self.assertNotIn("credentials.json", result["error"])
                self.assertIn("内部错误", result["error"])

    def test_cooldown_clear_accepts_only_nonempty_known_pool_selection(self):
        clear = self.runtime.web2api.clear_account_runtime_state = Mock(return_value={"cleared": 1})
        with patch.object(app.core, "load_settings", return_value={"web2api": {"accountIds": ["a", "b"]}}):
            result = self.request("/api/web2api/clear-cooldowns", method="POST", payload={"accountIds": ["b"]})
            self.assertEqual(result["result"], {"cleared": 1})
            clear.assert_called_once_with(["b"])
            clear.reset_mock()
            for selected in (None, [], "a", ["unknown"], ["a", "unknown"], [1], [{}], ["a"] * 4097):
                with self.subTest(selected_type=type(selected).__name__):
                    self.assert_api_error("/api/web2api/clear-cooldowns", {"accountIds": selected})
            clear.assert_not_called()

    def test_pool_config_clears_removed_accounts_only_after_successful_save(self):
        clear = self.runtime.web2api.clear_account_runtime_state = Mock(return_value={})
        previous = {"accountIds": ["a", "b"], "port": 17860}
        updated = {"accountIds": ["b", "c"], "port": 17860}
        with (
            patch.object(app.core, "load_settings", return_value={"web2api": previous}),
            patch.object(app.core, "save_web2api_settings", return_value=updated) as save,
        ):
            self.request("/api/web2api/config", method="POST", payload=updated)
            save.assert_called_once_with(updated)
            self.assertEqual(set(clear.call_args.args[0]), {"a"})
            self.assertEqual(clear.call_count, 1)
            clear.reset_mock()
            save.return_value = previous
            self.request("/api/web2api/config", method="POST", payload=previous)
            clear.assert_not_called()
            save.side_effect = app.core.ManagerError("save failed")
            self.assert_api_error("/api/web2api/config", updated)
            clear.assert_not_called()

    def test_account_proxy_disable_clears_selected_ids_and_enable_preserves_state(self):
        clear = self.runtime.web2api.clear_account_runtime_state = Mock(return_value={})
        for path, method, args, payload in (
            ("/api/accounts/a/proxy", "set_account_proxy_enabled", ("a", False), {}),
            ("/api/accounts/proxy-batch", "set_accounts_proxy_enabled_batch", (["a", "c"], False), {"accountIds": ["a", "c"]}),
        ):
            with self.subTest(path=path), patch.object(app.core, method, return_value={}) as save:
                clear.reset_mock()
                self.request(path, method="POST", payload={**payload, "enabled": False})
                save.assert_called_once_with(*args)
                clear.assert_called_once_with(["a"] if isinstance(args[0], str) else args[0])
                clear.reset_mock()
                self.request(path, method="POST", payload={**payload, "enabled": True})
                clear.assert_not_called()
                save.side_effect = app.core.ManagerError("unknown account")
                self.assert_api_error(path, {**payload, "enabled": False})
                clear.assert_not_called()


class ModelPreferencePersistenceV97Tests(unittest.TestCase):
    setUp = compatibility.ModelCompatibilityTests.setUp

    def test_custom_preference_survives_provider_save_and_catalog_refresh(self):
        core = app.core
        settings = core._initial_settings()
        self.provider.update(baseUrl="https://relay.example.test/v1", envKey="TEST_RELAY_KEY")
        settings["providers"].append(self.provider)
        store = copy.deepcopy(settings)

        def save_settings(value):
            nonlocal store
            store = copy.deepcopy(value)

        with (
            patch.object(core, "load_settings", side_effect=lambda: copy.deepcopy(store)),
            patch.object(core, "save_settings", side_effect=save_settings),
            patch.object(core, "backup_file", return_value=None),
            patch.object(core, "_settings_file_lock", side_effect=nullcontext),
            patch.object(model_preferences.codex_live_selection, "inspect_live_selection", return_value={"sourceId": "account:other"}),
            patch.object(core, "_apply_configuration_locked") as apply,
            patch.object(core, "load_provider_key", return_value="fake-test-key"),
            patch.object(core, "_open_same_origin_request", return_value=io.BytesIO(json.dumps({"data": [{"id": "gpt-6-astra", "supported_reasoning_levels": [{"effort": "low"}]}]}).encode())),
        ):
            model_preferences.save_reasoning("relay", "gpt-6-astra", {"mode": "custom", "efforts": ["high", "ultra"], "defaultEffort": "high"})
            expected = copy.deepcopy(core.provider_by_id("relay")["modelReasoningOverrides"])
            core.save_provider({"id": "relay", "name": "Renamed Relay", "baseUrl": self.provider["baseUrl"], "envKey": self.provider["envKey"]})
            self.assertEqual(core.provider_by_id("relay")["modelReasoningOverrides"], expected)
            self.assertEqual(core.refresh_provider_models("relay")["models"], ["gpt-6-astra"])
            refreshed = core.provider_by_id("relay")
            self.assertEqual(refreshed["modelCapabilities"]["gpt-6-astra"]["efforts"], ["low"])
            self.assertEqual(refreshed["modelReasoningOverrides"], expected)
            effective = core._effective_provider_model_capabilities(refreshed, self.native)["gpt-6-astra"]
            self.assertEqual(effective["efforts"], ["high", "ultra"])
            apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
