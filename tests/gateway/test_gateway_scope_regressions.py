import unittest
from unittest.mock import patch

import agent_manager.core as core
import agent_manager.gateway.service as web2api


class GatewayScopeRegressionTests(unittest.TestCase):
    """Define the boundary between the public API pool and private agent routes."""

    def setUp(self):
        self.settings = core._initial_settings()
        self.settings["accounts"] = [
            {
                "id": "public-pool",
                "label": "Public pool",
                "authMode": "chatgpt",
                "codexCompatible": True,
                "models": ["public-model"],
                "refreshState": "ready",
            },
            {
                "id": "private-import",
                "label": "Private imported account",
                "authMode": "chatgpt",
                "codexCompatible": True,
                "models": ["hidden-only"],
                "refreshState": "ready",
            },
        ]
        self.settings["modelWorkspace"].update(
            {
                "mode": "independent",
                "activeSourceId": "account:public-pool",
                "selectAll": True,
                "selectedModels": [],
            }
        )
        self.settings["web2api"].update(
            {
                "enabled": True,
                "activeForCodex": False,
                "activeAccountId": None,
                "accountIds": ["public-pool"],
                "providerIds": [],
                "sourceOrder": ["account:public-pool"],
                "routing": "ordered",
            }
        )
        routes = {
            level: {"models": [f"missing:{level}"], "efforts": ["low"]}
            for level in core.DIFFICULTIES
        }
        routes["simple"] = {
            "models": ["account:private-import::hidden-only"],
            "efforts": ["low"],
        }
        self.settings["subagentRouting"].update(
            {"strategyId": "adaptive", "routes": routes}
        )
        self.patchers = [
            patch.object(core, "load_settings", return_value=self.settings),
            patch.object(
                core,
                "current_auth_state",
                return_value={"activeAccountId": "public-pool", "requiresReapply": False},
            ),
            patch.object(core, "_reasoning_capabilities", return_value={}),
        ]
        for mocked in self.patchers:
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_internal_gateway_and_public_pool_have_distinct_source_sets(self):
        gateway = core.gateway_model_records(self.settings)
        pool = core.web2api_pool_model_records(self.settings)
        managed = core.managed_subagent_model_records(self.settings)

        self.assertEqual(
            {item["sourceRecordId"] for item in gateway},
            {"public-pool", "private-import"},
        )
        self.assertEqual(
            {item["sourceRecordId"] for item in pool},
            {"public-pool"},
        )
        self.assertEqual(len(managed), 1)
        self.assertEqual(managed[0]["sourceRecordId"], "private-import")
        self.assertTrue(managed[0]["subagentAlias"])

    def test_public_route_does_not_fall_through_to_non_pool_raw_model(self):
        # A model absent from the public pool must not be resolved by the
        # aggregate/private fallback merely because it is unique.
        self.assertIsNone(
            core.resolve_model_route(
                "hidden-only",
                self.settings,
                access_scope="public",
            )
        )

    def test_public_account_filter_cannot_expand_beyond_configured_pool(self):
        # ``allowed_ids`` is derived from an alias. It must narrow the public
        # pool, never turn an imported but disabled account into a pool member.
        manager = web2api.Web2APIManager()
        with self.assertRaises(web2api.GatewayError):
            manager._accounts({"private-import"}, "hidden-only")
        self.assertEqual(
            [item["id"] for item in manager._accounts(
                {"private-import"},
                "hidden-only",
                access_scope="internal",
            )],
            ["private-import"],
        )

    def test_public_models_do_not_publish_private_managed_aliases(self):
        manager = web2api.Web2APIManager()
        internal_aliases = {
            item["slug"] for item in core.managed_subagent_model_records(self.settings)
        }
        public_ids = {item["id"] for item in manager.models()["data"]}

        self.assertEqual(public_ids, {"public-model"})
        self.assertTrue(internal_aliases.isdisjoint(public_ids))

    def test_public_models_ignore_nonmember_active_codex_session(self):
        self.settings["web2api"]["activeForCodex"] = True
        self.settings["web2api"]["activeAccountId"] = "private-import"
        manager = web2api.Web2APIManager()

        public_ids = {item["id"] for item in manager.models()["data"]}
        internal_ids = {
            item["id"] for item in manager.models(access_scope="internal")["data"]
        }
        self.assertEqual(public_ids, {"public-model"})
        self.assertIn("hidden-only", internal_ids)

    def test_execute_and_stream_reject_non_pool_model_before_loading_credentials(self):
        for method_name in ("execute", "stream"):
            with self.subTest(method=method_name):
                manager = web2api.Web2APIManager()
                with (
                    patch.object(manager, "_record_usage"),
                    patch.object(
                        core,
                        "_load_account_snapshot",
                        side_effect=core.ManagerError("credential load sentinel"),
                    ) as load_snapshot,
                ):
                    with self.assertRaises(web2api.GatewayError):
                        getattr(manager, method_name)(
                            "/v1/responses",
                            {"model": "hidden-only", "input": "hello", "stream": method_name == "stream"},
                        )
                load_snapshot.assert_not_called()

    def test_pool_raw_model_does_not_override_private_managed_alias(self):
        self.settings["accounts"][0]["models"] = ["shared-model"]
        self.settings["accounts"][1]["models"] = ["shared-model"]
        self.settings["subagentRouting"]["routes"]["simple"] = {
            "models": ["account:private-import::shared-model"],
            "efforts": ["low"],
        }
        managed = core.managed_subagent_model_records(self.settings)
        self.assertEqual(len(managed), 1)

        private_route = core.resolve_model_route(
            managed[0]["slug"],
            self.settings,
            access_scope="internal",
        )
        self.assertEqual(private_route["sourceRecordId"], "private-import")
        self.assertTrue(private_route["subagentAlias"])
        # The raw pool ID retains pool semantics instead of pinning the private
        # account with the same upstream model name.
        self.assertIsNone(
            core.resolve_model_route(
                "shared-model",
                self.settings,
                access_scope="internal",
            )
        )


if __name__ == "__main__":
    unittest.main()
