import copy
from datetime import datetime, timedelta, timezone
import json
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import agent_manager_core as core


class ModelRefreshRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.account = {
            "id": "test-account", "authMode": "chatgpt", "sourceType": "codex_auth",
            "email": "test-account@example.invalid",
            "models": ["old-model"], "modelsLastCheckedAt": "2020-01-01T00:00:00Z",
            "subscriptionExpiresAt": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
            "plan": "plus", "refreshState": "ready",
        }
        self.settings = core._initial_settings()
        self.settings["accounts"] = [self.account]
        for name, value in {
            "CODEX_HOME": self.root, "CONFIG_FILE": self.root / "config.toml",
            "MODELS_CACHE_FILE": self.root / "models_cache.json",
            "MODEL_CACHE": {"at": 0.0, "models": None, "raw": None},
            "CODEX_VERSION_CACHE": {"at": 0.0, "value": None},
        }.items():
            p = patch.object(core, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_official_visibility_and_live_capabilities_are_not_invented(self):
        result = core._parse_official_model_catalog({"models": [
            {"slug": "gpt-6-astra", "visibility": "list", "supported_reasoning_levels": [
                {"effort": "high"}, {"effort": "ultra"}, None, {"effort": "bogus"}],
             "default_reasoning_level": "high"},
            {"slug": "reserve", "visibility": "hide"},
            {"id": "review", "hidden": True},
            {"id": "future-model", "supportedReasoningEfforts": [{"reasoningEffort": "low"}]},
        ]})
        self.assertEqual(result["models"], ["future-model", "gpt-6-astra"])
        self.assertEqual(result["modelCapabilities"]["gpt-6-astra"]["efforts"], ["high", "ultra"])
        self.assertEqual(core._parse_model_ids({"data": [{"id": "relay-private", "hidden": True}]}), ["relay-private"])

    def test_probe_persists_account_capabilities_and_filters_hidden_models(self):
        def fetch(url, *_args, **_kwargs):
            if url == core.CHATGPT_MODELS_URL:
                return {"models": [
                    {"slug": "gpt-6-astra", "supported_reasoning_levels": [{"effort": "ultra"}]},
                    {"slug": "codex-auto-review", "visibility": "hide"},
                ]}
            return {"plan_type": "plus", "rateLimitResetCredits": {"availableCount": 0}}
        with patch.object(core, "load_settings", return_value=self.settings), \
                patch.object(core, "_account_chatgpt_credentials", return_value={"accessToken": "fake", "accountId": "fake"}), \
                patch.object(core, "_fetch_chatgpt_json", side_effect=fetch), \
                patch.object(core, "_live_official_account_matches", return_value=False), \
                patch.object(core, "_codex_client_version", return_value="0.153.4"):
            result = core._probe_codex_account("test-account", force_metadata=True)
        self.assertEqual(result["models"], ["gpt-6-astra"])
        self.assertEqual(result["modelCapabilities"]["gpt-6-astra"]["efforts"], ["ultra"])

    def test_full_manual_refresh_forces_metadata_and_background_does_not(self):
        with patch.object(core, "load_settings", return_value=self.settings), \
                patch.object(core, "stale_codex_account_ids", return_value=["test-account"]), \
                patch.object(core, "refresh_codex_accounts", return_value={}) as refresh:
            core.refresh_all_codex_accounts()
            refresh.assert_called_with(["test-account"], force_metadata=True)
            core.refresh_all_codex_accounts(stale_only=True)
            refresh.assert_called_with(["test-account"], force_metadata=False)

    def perform(self, **kwargs):
        with patch.object(core, "load_settings", return_value=copy.deepcopy(self.settings)), \
                patch.object(core, "save_settings"), \
                patch.object(core, "_probe_codex_account", return_value={"refreshState": "ready"}) as probe:
            result = core._perform_account_refresh("test-account", **kwargs)
        return result, probe.call_count

    def test_manual_metadata_refresh_is_not_coalesced_with_quota_only_refresh(self):
        self.account["lastRefreshedAt"] = core.now_iso()
        _, count = self.perform(force_metadata=True)
        self.assertEqual(count, 1)

    def test_manual_retry_bypasses_network_backoff_but_respects_429(self):
        self.account["nextRefreshAt"] = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        self.account["refreshErrors"] = {"usage": "连接 ChatGPT 超时"}
        _, count = self.perform(force_metadata=True)
        self.assertEqual(count, 1)
        self.account["refreshErrors"] = {"usage": "HTTP 429"}
        result, count = self.perform(force_metadata=True)
        self.assertEqual(count, 0)
        self.assertEqual(result["skipReason"], "backoff")

    def test_background_refresh_keeps_network_backoff(self):
        self.account["nextRefreshAt"] = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        _, count = self.perform()
        self.assertEqual(count, 0)

    def test_native_templates_do_not_read_managers_generated_catalog(self):
        bundled = {"models": [{"slug": "gpt-6-astra", "visibility": "list"}]}
        with patch.object(core, "run_codex_capture", return_value=subprocess.CompletedProcess([], 0, json.dumps(bundled), "")) as run:
            self.assertEqual(core._raw_local_model_catalog(), bundled)
        run.assert_called_once_with(["debug", "models", "--bundled"], timeout=30)

    def test_native_file_change_invalidates_in_memory_cache_and_keeps_visibility(self):
        def write(models):
            core.MODELS_CACHE_FILE.write_text(json.dumps({
                "models": models, "client_version": "0.153.4", "fetched_at": core.now_iso(),
            }), encoding="utf-8")
        with patch.object(core, "_codex_client_version", return_value="0.153.4"), \
                patch.object(core, "run_codex_capture") as run:
            write([{"slug": "old-model", "visibility": "list"}])
            self.assertEqual(core.local_model_catalog()[0]["id"], "old-model")
            write([{"slug": "gpt-6-astra", "visibility": "list"}, {"slug": "reserve", "hidden": True}])
            self.assertEqual([m["id"] for m in core.local_model_catalog()], ["gpt-6-astra"])
        run.assert_not_called()

    def test_model_list_cannot_mislabel_custom_catalog_as_official_discovery(self):
        core.CONFIG_FILE.write_text('model_catalog_json = "manager-generated.json"', encoding="utf-8")
        with patch.object(core, "_live_official_account_matches", return_value=True), \
                patch.object(core, "codex_app_server_requests") as request:
            with self.assertRaisesRegex(core.ManagerError, "自定义模型目录"):
                core._active_codex_model_catalog(self.account)
        request.assert_not_called()

    def test_model_pagination_does_not_commit_truncated_catalog(self):
        with patch.object(core, "_live_official_account_matches", return_value=True), \
                patch.object(core, "codex_app_server_requests", return_value=[
                    {"account": {"type": "chatgpt", "email": self.account["email"]}},
                    {"data": [{"id": "first"}], "nextCursor": "more"},
                ]):
            with self.assertRaisesRegex(core.ManagerError, "分页上限"):
                core._active_codex_model_catalog(self.account, max_pages=1)

    def test_app_server_early_exit_reports_manager_error_instead_of_deque_slice_crash(self):
        process = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO("server unavailable\n"),
                                  poll=lambda: 1, wait=lambda **kwargs: 1, kill=lambda: None)
        with patch.object(core, "codex_prefix", return_value=["fake-codex"]), \
                patch.object(core.subprocess, "Popen", return_value=process):
            with self.assertRaises(core.ManagerError):
                core.codex_app_server_request("model/list", {}, timeout=1)

    def test_remote_account_efforts_override_fallback_but_not_another_account(self):
        self.account.update({"models": ["gpt-6-astra"], "codexCompatible": True,
                             "modelCapabilities": {"gpt-6-astra": {"efforts": ["ultra"], "defaultEffort": "ultra"}}})
        with patch.object(core, "current_auth_state", return_value={}), patch.object(core, "account_invalid_reason", return_value=None), \
                patch.object(core, "codex_version", return_value="0.153.4"):
            source = core.model_sources(self.settings, [])[0]
        self.assertEqual(source["models"][0]["efforts"], ["ultra"])
        self.assertIn("ultra", core.KNOWN_REMOTE_REASONING_CAPABILITIES["gpt-6-astra"]["efforts"])


if __name__ == "__main__":
    unittest.main()
