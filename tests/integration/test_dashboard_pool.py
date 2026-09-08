import copy
import unittest
from unittest.mock import patch

import agent_manager.core as core
from agent_manager.models.ordering import move_dashboard_card
import tests.integration.test_dashboard_order as ordering_tests


class DashboardPoolDropTests(unittest.TestCase):
    setUp = ordering_tests.DashboardOrderTests.setUp

    def prepare(self):
        for account in self.saved["accounts"]:
            account.update(codexCompatible=True, models=["fixture-model"])
        for provider in self.saved["providers"]:
            provider.update(kind="custom", models=["fixture-model"])
        self.saved["relayAccounts"][0].update(selectedKeyId="key-1", keys=[{"id":"key-1","active":True}])
        self.saved["providers"][0].update(sourceType="relay_account",relayAccountId="r",relayKeyId="key-1")
        self.key_patcher = patch.multiple(core, load_provider_key=lambda *a, **k:"fixture-key", load_relay_account_key=lambda *a, **k:"fixture-key", provider_key_configured=lambda *_:True,
            _load_account_snapshot=lambda account_id:{"account":account_id}, _decode_snapshot_files=lambda snapshot:{"auth.json":b"fixture-auth"}, _auth_bytes_support_codex=lambda _:True)
        self.key_patcher.start(); self.addCleanup(self.key_patcher.stop)

    def join(self, source):
        return move_dashboard_card(core,{"sourceId":source,"dropTarget":"apiPool"})

    def test_account_is_appended_once_without_switch_or_dashboard_reorder(self):
        self.prepare(); before=copy.deepcopy(self.saved)
        result=self.join("account:a")
        self.assertEqual(result["poolSourceId"],"account:a")
        self.assertEqual(self.saved["web2api"]["sourceOrder"],before["web2api"]["sourceOrder"]+["account:a"])
        self.assertTrue(self.join("account:a")["alreadyMember"])
        self.assertEqual(self.saved["web2api"]["accountIds"],["b","a"])
        self.assertEqual(self.saved["dashboardOrder"],before["dashboardOrder"])
        self.assertEqual(self.saved["unrelated"],before["unrelated"])

    def test_relay_adds_only_selected_provider_not_all_site_keys(self):
        self.prepare()
        result=self.join("relay:r")
        self.assertEqual(result["poolSourceId"],"provider:p")
        self.assertEqual(self.saved["web2api"]["providerIds"],["child","p"])
        self.assertEqual(self.saved["relayAccounts"][0]["groupId"],"relay")

    def test_provider_drop(self):
        self.prepare()
        self.assertEqual(self.join("provider:independent")["poolSourceId"],"provider:independent")

    def test_relay_key_mismatch_rolls_back_without_any_mutation(self):
        self.prepare(); before=copy.deepcopy(self.saved)
        with patch.object(core,"load_relay_account_key",return_value="another-key"), self.assertRaisesRegex(core.ManagerError,"不一致"):
            self.join("relay:r")
        self.assertEqual(self.saved,before)
        self.assertEqual(self.save_count,0)

    def test_missing_relay_selection_and_unsupported_account_are_rejected(self):
        self.prepare(); self.saved["relayAccounts"][0]["selectedKeyId"]="missing"
        self.saved["accounts"][0]["quotaOnly"]=True
        for source in ("relay:r","account:a"):
            with self.subTest(source=source), self.assertRaises(core.ManagerError): self.join(source)
        self.assertEqual(self.save_count,0)

    def test_write_failure_restores_pool(self):
        self.prepare(); before=copy.deepcopy(self.saved); self.fail_at=1
        with self.assertRaises(OSError): self.join("account:a")
        self.assertEqual(self.saved,before)

    def test_mixed_pool_and_group_drop_is_rejected(self):
        self.prepare()
        with self.assertRaises(core.ManagerError):
            move_dashboard_card(core,{"sourceId":"account:a","dropTarget":"apiPool","groupId":"work"})
        self.assertEqual(self.save_count,0)


class LinkedKeyAvailabilityTests(unittest.TestCase):
    def test_inherited_env_does_not_mark_missing_selected_relay_key_available(self):
        provider={"kind":"custom","sourceType":"relay_account","relayAccountId":"r","relayKeyId":"gone","envKey":"FIXTURE_OLD_KEY"}
        with patch.object(core,"provider_by_id",return_value=provider), patch.object(core,"relay_account_key_configured",return_value=False) as configured, patch.dict(core.os.environ,{"FIXTURE_OLD_KEY":"unrelated"}):
            self.assertFalse(core.provider_key_configured("p"))
        configured.assert_called_once_with("r","gone")
