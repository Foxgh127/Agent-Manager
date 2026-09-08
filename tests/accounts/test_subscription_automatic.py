import unittest
from datetime import datetime,timezone
from unittest.mock import patch
import agent_manager.core as core
from agent_manager.accounts.subscription import select_plan_metadata, extract_plan_metadata


class AutomaticSubscriptionTests(unittest.TestCase):
    def test_new_official_generic_pro_replaces_stale_cached_five_x(self):
        before={"plan":"pro5x","planLabel":"Pro 5x","planEvidence":{"source":"entitlement","observedAt":"2026-09-01T00:00:00Z"}}
        current=extract_plan_metadata({"plan_type":"pro"},source="usage",observed_at="2026-09-08T00:00:00Z")
        result=select_plan_metadata(before,current,now=datetime(2026,9,8,tzinfo=timezone.utc))
        self.assertEqual(result["planRaw"],"pro")
        self.assertEqual(result["planLabel"],"Pro")

    def test_explicit_current_five_x_beats_family_label_in_same_batch(self):
        result=extract_plan_metadata({"account":{"plan_type":"pro"},"entitlement":{"subscription_plan":"pro5x"}},source="entitlement")
        self.assertEqual(result["planLabel"],"Pro 5x")

    def test_retired_manual_metadata_cannot_change_plan(self):
        account={"id":"a","label":"A","plan":"pro","groupId":"official"}
        with patch.object(core,"load_settings",return_value={"accounts":[account]}),patch.object(core,"save_settings"),patch.object(core,"_account_group"):
            result=core.update_codex_account_metadata("a",{"planVariantOverride":"pro5x"})
        self.assertEqual(result["plan"],"pro")
        self.assertNotIn("planVariantOverride",result)


if __name__=="__main__":
    unittest.main()
