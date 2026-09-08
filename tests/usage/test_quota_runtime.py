import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace
import agent_manager.usage.estimation as estimator
import agent_manager.core as core
from agent_manager.usage.calibration import combined_observation, QuotaCalibrationSampler


class CalibrationRuntimeTests(unittest.TestCase):
    def snapshot(self, missing=2):
        return {"counterGeneration":"gateway-generation","coverage":{"counterGenerationValid":True},
                "byAccount":[{"accountId":"a","totalTokens":3000,"usageMissingCount":missing},{"accountId":"other","totalTokens":900000}],
                "codexSessions":{"coverage":{"partialFiles":34},"liveCoverage":{"scope":"native_direct_main","epoch":"live-generation","complete":True,"accounts":{"a":500},"observedAt":"2026-09-08T00:00:00Z"}}}

    def test_live_samples_can_progress_despite_partial_old_history(self):
        result=combined_observation({"id":"a"},self.snapshot())
        self.assertEqual(result["cumulativeTokens"],3500)
        self.assertTrue(result["coverageComplete"])
        self.assertEqual(result["usageMissingCount"],0)
        self.assertEqual(result["scope"],"native_direct_main_plus_gateway_reported")

    def test_new_missing_report_invalidates_epoch_but_old_missing_does_not_block(self):
        before=combined_observation({"id":"a"},self.snapshot())
        self.assertEqual(before["coverageEpoch"],combined_observation({"id":"a"},self.snapshot())["coverageEpoch"])
        self.assertNotEqual(before["coverageEpoch"],combined_observation({"id":"a"},self.snapshot(3))["coverageEpoch"])

    def test_unknown_generation_cannot_claim_complete(self):
        snapshot=self.snapshot();snapshot["coverage"]={}
        self.assertFalse(combined_observation({"id":"a"},snapshot)["coverageComplete"])

    def test_card_snapshot_never_fails_if_estimate_store_unavailable(self):
        sampler=QuotaCalibrationSampler(SimpleNamespace(_closed=True))
        accounts=[{"id":"a","authMode":"chatgpt","sourceType":"codex_auth"}]
        with patch.object(core,"read_json",return_value={"accounts":accounts}), patch.object(estimator,"read_summaries",side_effect=OSError("locked")):
            sampler.decorate(accounts)
        self.assertEqual(accounts[0]["quotaEstimate"]["reason"],"calibration_store_unavailable")

    def test_batch_card_read_loads_store_only_once(self):
        with patch.object(estimator,"_load",return_value={}) as load:
            result=estimator.read_summaries([{"id":"a"},{"id":"b"}])
        load.assert_called_once()
        self.assertEqual(set(result),{"a","b"})

    def test_public_card_and_private_sampler_use_same_account_identity(self):
        sampler=QuotaCalibrationSampler(SimpleNamespace(_closed=True))
        private={"id":"a","fingerprint":"private-hash","email":"a@example.test","authMode":"chatgpt","sourceType":"codex_auth"}
        public={key:value for key,value in private.items() if key!="fingerprint"}
        with patch.object(core,"read_json",return_value={"accounts":[private]}), patch.object(estimator,"read_summaries",return_value={"a":{"status":"calibrated"}}) as read:
            sampler.decorate([public])
        read.assert_called_once_with([private])
        self.assertEqual(public["quotaEstimate"]["status"],"calibrated")
        self.assertNotIn("fingerprint",public)


if __name__=="__main__":
    unittest.main()
