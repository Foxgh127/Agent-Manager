import copy
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
import agent_manager_core as core
import agent_manager_app as app
import oauth_reauthentication as service
from test_oauth_lifecycle import FakeCallbackServer


class ReauthenticationTests(unittest.TestCase):
    def setUp(self):
        self.account = {"id":"kept", "email":"target@example.test", "label":"My account", "sourceType":"codex_auth", "authMode":"chatgpt", "groupId":"custom", "models":["model"], "importedAt":"2026-01-01", "plan":"pro", "proxyEnabled":True, "usage":{"weekly":{"remainingPercent":0}}, "refreshErrors":{"usage":"401"}}

    def test_same_account_renewal_preserves_record_and_queues_fresh_metadata(self):
        settings = {"accounts":[self.account]}
        with (
            patch.object(service,"target",return_value=self.account),
            patch.object(core,"_snapshot_from_bytes",return_value=({},{"email":"target@example.test"})),
            patch.object(core,"_account_matches_identity",return_value=True),
            patch.object(core,"_persist_account_oauth_auth") as persist,
            patch.object(core,"load_settings",return_value=settings),
            patch.object(core,"save_settings") as save,
            patch.object(core,"_settings_file_lock") as lock,
        ):
            result = service.reauthenticate("kept",b"synthetic")
        self.assertEqual(result["id"],"kept")
        self.assertEqual(result["groupId"],"custom")
        self.assertEqual(result["importedAt"],"2026-01-01")
        self.assertEqual(result["models"],["model"])
        self.assertTrue(result["proxyEnabled"])
        self.assertTrue(result["usage"]["weekly"]["stale"])
        self.assertEqual(result["refreshState"],"pending")
        self.assertEqual(result["refreshErrors"],{})
        self.assertTrue(persist.call_args.kwargs["replace_live_if_active"])
        save.assert_called_once()

    def test_other_user_cannot_overwrite_target_even_when_workspace_matches(self):
        before=copy.deepcopy(self.account)
        with (
            patch.object(service,"target",return_value=self.account),
            patch.object(core,"_snapshot_from_bytes",return_value=({},{"email":"other@example.test"})),
            patch.object(core,"_account_matches_identity",return_value=True),
            patch.object(core,"_persist_account_oauth_auth") as persist,
            self.assertRaises(core.ManagerError),
        ):
            service.reauthenticate("kept",b"synthetic")
        persist.assert_not_called()
        self.assertEqual(self.account,before)

    def test_reauthentication_login_is_bound_to_target_and_does_not_import_new_record(self):
        oauth=app.OAuthDeviceLogin(on_account_saved=Mock())
        self.addCleanup(oauth.close)
        server=FakeCallbackServer()
        with (
            patch.object(service,"target",return_value=self.account),
            patch.object(core,"load_settings",return_value={}),
            patch.object(core,"_account_group"),
            patch.object(oauth,"_bind_callback_server",return_value=(server,1455)),
            patch.object(app,"open_browser_window",return_value=True),
        ):
            state=oauth.start({"reauthAccountId":"kept","groupId":"wrong","proxyEnabled":False})
        query=parse_qs(urlsplit(state["url"]).query)
        self.assertEqual(query["login_hint"],["target@example.test"])
        self.assertEqual(query["prompt"],["select_account"])
        self.assertEqual(oauth.account_payload["groupId"],"custom")
        self.assertEqual(state["reauthAccountId"],"kept")
        with oauth.lock:
            oauth.authorization_code="fake-code"
            oauth.data["status"]="exchanging"
        with (
            patch.object(service,"reauthenticate",return_value=self.account) as renew,
            patch.object(core,"save_codex_account") as create,
        ):
            oauth._exchange_and_save(state["loginId"],lambda *args:{"access_token":"access","id_token":"id","refresh_token":"refresh"})
        renew.assert_called_once()
        self.assertEqual(renew.call_args.args[0],"kept")
        create.assert_not_called()
        self.assertEqual(oauth.state()["status"],"completed")
        oauth.on_account_saved.assert_called_once_with(["kept"])

    def test_stale_modal_cannot_cancel_or_open_another_login(self):
        oauth=app.OAuthDeviceLogin()
        oauth.login_id="current"
        oauth.data["status"]="waiting"
        with patch.object(oauth,"_expire_if_due"), patch.object(app,"open_browser_window") as browser:
            for operation in (lambda:oauth.cancel("old"), lambda:oauth.open_browser("old"), lambda:oauth.submit_callback("bad","old")):
                with self.assertRaises(core.ManagerError): operation()
        browser.assert_not_called()
        self.assertEqual(oauth.data["status"],"waiting")


if __name__=="__main__":
    unittest.main()
