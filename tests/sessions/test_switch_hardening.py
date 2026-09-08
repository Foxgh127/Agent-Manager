import base64
from contextlib import nullcontext
import unittest
from unittest.mock import patch
import agent_manager.core as core


class SwitchHardeningTests(unittest.TestCase):
    def test_stored_provider_key_wins_over_inherited_key(self):
        with (
            patch.object(core,"provider_by_id",return_value={"id":"relay","envKey":"RELAY_KEY"}),
            patch.object(core,"_secret_store",return_value={"providers":{"relay":base64.b64encode(b"stored-new").decode()}}),
            patch.object(core,"dpapi_unprotect",side_effect=lambda value:value.decode()),
            patch.dict(core.os.environ,{"RELAY_KEY":"inherited-old"}),
        ):
            self.assertEqual(core.load_provider_key("relay"),"stored-new")

    def test_corrupt_saved_key_does_not_silently_select_environment_account(self):
        with (
            patch.object(core,"provider_by_id",return_value={"id":"relay","envKey":"RELAY_KEY"}),
            patch.object(core,"_secret_store",return_value={"providers":{"relay":"bad-base64!"}}),
            patch.dict(core.os.environ,{"RELAY_KEY":"other-account"}),
            self.assertRaises(core.ManagerError),
        ):
            core.load_provider_key("relay")

    def test_environment_only_provider_remains_supported(self):
        with (
            patch.object(core,"provider_by_id",return_value={"id":"relay","envKey":"RELAY_KEY"}),
            patch.object(core,"_secret_store",return_value={"providers":{}}),
            patch.dict(core.os.environ,{"RELAY_KEY":"configured-env-only"}),
        ):
            self.assertEqual(core.load_provider_key("relay"),"configured-env-only")

    def test_missing_selected_website_key_never_falls_back_to_another_stored_key(self):
        with (
            patch.object(core,"provider_by_id",return_value={"id":"relay","sourceType":"relay_account","relayAccountId":"site","relayKeyId":"selected","envKey":"RELAY_KEY"}),
            patch.object(core,"load_relay_account_key",return_value=None),
            patch.object(core,"_secret_store",side_effect=AssertionError("must not read a different key")),
            patch.dict(core.os.environ,{"RELAY_KEY":"unrelated-old-key"}),
        ):
            self.assertIsNone(core.load_provider_key("relay"))

    def test_api_launch_scrubs_global_authority_then_binds_selected_key(self):
        with (
            patch.dict(core.os.environ,{"CODEX_ACCESS_TOKEN":"old-login","OPENAI_API_KEY":"old-api","OPENAI_BASE_URL":"https://old.example.test","CODEX_HOME":"old-home"}),
            patch.object(core,"provider_by_id",return_value={"id":"relay","envKey":"OPENAI_API_KEY"}),
            patch.object(core,"load_provider_key",return_value="selected-api"),
        ):
            env=core._codex_source_environment({"apiProviderId":"relay"})
        self.assertNotIn("CODEX_ACCESS_TOKEN",env)
        self.assertNotIn("OPENAI_BASE_URL",env)
        self.assertEqual(env["OPENAI_API_KEY"],"selected-api")
        self.assertEqual(env["CODEX_HOME"],str(core.CODEX_HOME))

    def test_old_provider_auth_headers_cannot_override_selected_environment_key(self):
        table={"env_key":"NEW_KEY","experimental_bearer_token":"old-secret","requires_openai_auth":True,
               "http_headers":{"Authorization":"Bearer other-account","User-Agent":"relay-client","OpenAI-Project":"old-project"},
               "env_http_headers":{"X_API_Key":"OLD_ENV","X-Custom":"CUSTOM_ENV"}}
        self.assertTrue(core._provider_auth_overrides(table))
        core._clear_provider_auth_overrides(table)
        self.assertFalse(core._provider_auth_overrides(table))
        self.assertEqual(table["env_key"],"NEW_KEY")
        self.assertEqual(table["http_headers"],{"User-Agent":"relay-client"})
        self.assertEqual(table["env_http_headers"],{"X-Custom":"CUSTOM_ENV"})
        self.assertFalse(table["requires_openai_auth"])

    def test_disabled_native_auth_is_not_an_official_fast_path(self):
        self.assertTrue(core._official_route_has_overrides({"model_providers":{"openai":{"requires_openai_auth":False}}}))

    def test_chatgpt_host_override_isolated_but_native_default_is_accepted(self):
        self.assertTrue(core._official_route_has_overrides({"chatgpt_base_url":"https://other.example.test/backend-api/"}))
        self.assertFalse(core._official_route_has_overrides({"chatgpt_base_url":"https://chatgpt.com/backend-api/"}))
        with patch.dict(core.os.environ,{"CHATGPT_BASE_URL":"https://other.example.test","CODEX_CHATGPT_BASE_URL":"https://another.example.test"}):
            env=core._codex_runtime_environment(official=True)
        self.assertNotIn("CHATGPT_BASE_URL",env)
        self.assertNotIn("CODEX_CHATGPT_BASE_URL",env)

    def test_invalid_auth_header_table_is_repaired_without_a_type_error(self):
        table={"http_headers":True}
        self.assertTrue(core._provider_auth_overrides(table))
        core._clear_provider_auth_overrides(table)
        self.assertNotIn("http_headers",table)

    def test_explicit_reapply_relaunches_even_when_saved_account_is_active(self):
        events=[]
        account={"id":"a","email":"a@example.test","sourceType":"codex_auth","authMode":"chatgpt"}
        with (
            patch.object(core,"_credential_store_mode",return_value="file"),
            patch.object(core,"load_settings",return_value={"accounts":[account]}),
            patch.object(core,"_prepare_account_switch_target",return_value=(account,{})),
            patch.object(core,"_account_model_source",return_value={"id":"account:a","models":[]}),
            patch.object(core,"_official_account_target_is_active",return_value=True),
            patch.object(core,"_account_chatgpt_credentials",return_value={}) as renew,
            patch.object(core,"resolve_codex_launch_plan",return_value={}),
            patch.object(core,"_exclusive_switch_operation",return_value=nullcontext()),
            patch.object(core,"_capture_switch_transaction_snapshot",return_value={}),
            patch.object(core,"switch_codex_account",side_effect=lambda *a,**kw:events.append("write") or {"sessionSync":{}}),
            patch.object(core,"_apply_official_account_configuration",return_value={"applied":{},"workspace":{},"model":"model"}),
            patch.object(core,"_repair_switch_session_visibility",return_value={}),
            patch.object(core,"launch_codex_app",side_effect=lambda **kw:events.append("launch") or {}),
            patch.object(core,"wait_for_codex_runtime_ready",return_value={"ready":True}),
        ):
            result=core.switch_codex_account_and_launch("a",force_reapply=True,close_processes_callback=lambda:events.append("close") or {})
        self.assertEqual(events,["close","write","launch"])
        renew.assert_called_once()
        self.assertTrue(result["verified"])


if __name__=="__main__":
    unittest.main()
