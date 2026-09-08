import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_manager_core as core


class FakeResponse(io.BytesIO):
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class ProviderCapabilitiesV9Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / ".codex"
        self.root.mkdir(parents=True)
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
        }
        self.originals = {name: getattr(core, name) for name in paths}
        for name, value in paths.items():
            setattr(core, name, value)
        core.CONFIG_FILE.write_text('model = "baseline"\n', encoding="utf-8")
        core.AGENTS_FILE.write_text("# test\n", encoding="utf-8")
        self.dpapi = patch.multiple(
            core,
            dpapi_protect=lambda value: value.encode("utf-8"),
            dpapi_unprotect=lambda value: value.decode("utf-8"),
        )
        self.dpapi.start()

    def tearDown(self):
        self.dpapi.stop()
        for name, value in self.originals.items():
            setattr(core, name, value)
        self.temp.cleanup()

    @staticmethod
    def provider_record(models, capabilities=None):
        return {
            "id": "capability_relay",
            "kind": "custom",
            "name": "Capability Relay",
            "baseUrl": "https://capability.example.test/v1",
            "envKey": "CAPABILITY_RELAY_KEY",
            "models": list(models),
            "modelCapabilities": capabilities or {},
            "modelDiscoveryState": "ready",
            "lastCheckStatus": "ok",
            "runtimeRevision": 1,
            "appliedRuntimeRevision": 1,
            "groupId": "relay",
        }

    def test_provider_catalog_preserves_source_metadata_and_hidden_models(self):
        payload = {
            "data": [
                {
                    "id": "hidden-capable",
                    "hidden": True,
                    "supportedReasoningEfforts": ["low", {"effort": "high"}, "invalid"],
                    "defaultReasoningEffort": "high",
                    "contextWindow": 131072,
                    "max_context_window": 262144,
                    "effectiveContextWindowPercent": 90,
                    "supportsPersonality": True,
                    "support_verbosity": True,
                    "defaultVerbosity": "high",
                },
                {
                    "id": "explicit-no-reasoning",
                    "capabilities": {"reasoning": False},
                    "context_window": 32768,
                    "supports_verbosity": False,
                },
                {"id": "unknown-new-model"},
            ]
        }

        catalog = core._parse_provider_model_catalog(payload)

        self.assertEqual(
            catalog["models"],
            ["explicit-no-reasoning", "hidden-capable", "unknown-new-model"],
        )
        capable = catalog["modelCapabilities"]["hidden-capable"]
        self.assertEqual(capable["efforts"], ["low", "high"])
        self.assertEqual(capable["defaultEffort"], "high")
        self.assertTrue(capable["reasoningSupported"])
        self.assertEqual(capable["contextWindow"], 131072)
        self.assertEqual(capable["maxContextWindow"], 262144)
        self.assertEqual(capable["effectiveContextWindowPercent"], 90)
        self.assertTrue(capable["supportsPersonality"])
        self.assertTrue(capable["supportsVerbosity"])
        self.assertEqual(capable["defaultVerbosity"], "high")
        no_reasoning = catalog["modelCapabilities"]["explicit-no-reasoning"]
        self.assertTrue(no_reasoning["reasoningKnown"])
        self.assertFalse(no_reasoning["reasoningSupported"])
        self.assertEqual(no_reasoning["efforts"], [])
        self.assertFalse(no_reasoning["supportsVerbosity"])
        self.assertNotIn("unknown-new-model", catalog["modelCapabilities"])

    def test_api_document_import_keeps_embedded_model_capabilities(self):
        imported = core._provider_from_import_candidate(
            {
                "api_provider_id": "embedded_capabilities",
                "api_provider_name": "Embedded Capabilities",
                "api_base_url": "https://embedded.example.test/v1",
                "openai_api_key": "sk-embedded-capability",
                "api_model_catalog": {
                    "models": {
                        "embedded-model": {
                            "supports_reasoning": True,
                            "reasoning_efforts": ["medium", "high"],
                            "default_reasoning_effort": "medium",
                            "context_window": 65536,
                        }
                    }
                },
            }
        )

        self.assertIsNotNone(imported)
        self.assertEqual(imported["models"], ["embedded-model"])
        self.assertEqual(
            imported["modelCapabilities"]["embedded-model"]["efforts"],
            ["medium", "high"],
        )
        self.assertEqual(
            imported["modelCapabilities"]["embedded-model"]["contextWindow"],
            65536,
        )

    def test_provider_sources_use_native_effort_compatibility_only_when_unspecified(self):
        settings = core._initial_settings()
        settings["providers"].append(
            self.provider_record(
                ["gpt-5.4", "explicit-no", "provider-capable"],
                {
                    "explicit-no": {
                        "reasoningKnown": True,
                        "reasoningSupported": False,
                        "efforts": [],
                        "defaultEffort": "",
                    },
                    "provider-capable": {
                        "reasoningKnown": True,
                        "reasoningSupported": True,
                        "efforts": ["medium", "high"],
                        "defaultEffort": "medium",
                        "contextWindow": 65536,
                    },
                },
            )
        )
        official = [
            {
                "id": "gpt-5.4",
                "efforts": ["low", "medium", "high", "xhigh"],
                "defaultEffort": "medium",
            }
        ]

        with (
            patch.object(core, "provider_key_configured", return_value=True),
            patch.object(core, "current_auth_state", return_value={}),
            patch.object(core, "_provider_runtime_requires_reapply", return_value=False),
        ):
            source = next(
                item
                for item in core.model_sources(settings, local_models=official)
                if item["recordId"] == "capability_relay"
            )

        models = {item["id"]: item for item in source["models"]}
        self.assertTrue(models["gpt-5.4"]["reasoningKnown"])
        self.assertTrue(models["gpt-5.4"]["reasoningSupported"])
        self.assertEqual(models["gpt-5.4"]["efforts"], ["low", "medium", "high", "xhigh"])
        self.assertEqual(models["gpt-5.4"]["capabilitySource"], "codex_compatibility")
        self.assertNotIn("contextWindow", models["gpt-5.4"])
        self.assertTrue(models["explicit-no"]["reasoningKnown"])
        self.assertFalse(models["explicit-no"]["reasoningSupported"])
        self.assertEqual(models["provider-capable"]["efforts"], ["medium", "high"])
        self.assertEqual(models["provider-capable"]["contextWindow"], 65536)

    def test_fetch_saves_capabilities_and_403_preserves_cache(self):
        core.ensure_state()
        core.save_provider(
            {
                "id": "capability_relay",
                "name": "Capability Relay",
                "baseUrl": "https://capability.example.test/v1",
                "envKey": "CAPABILITY_RELAY_KEY",
                "models": ["same-model"],
                "modelCapabilities": {
                    "same-model": {
                        "reasoningKnown": True,
                        "reasoningSupported": True,
                        "efforts": ["low"],
                        "defaultEffort": "low",
                    }
                },
            }
        )
        core.store_provider_key("capability_relay", "sk-capability-test")
        before_revision = core.provider_by_id("capability_relay")["runtimeRevision"]
        response = FakeResponse(
            json.dumps(
                {
                    "data": [
                        {
                            "id": "same-model",
                            "hidden": True,
                            "supported_reasoning_levels": [{"effort": "high"}],
                            "default_reasoning_level": "high",
                            "context_window": 123456,
                        }
                    ]
                }
            ).encode("utf-8")
        )

        with patch.object(core, "_open_same_origin_request", return_value=response):
            self.assertEqual(core.fetch_provider_models("capability_relay"), ["same-model"])

        refreshed = core.provider_by_id("capability_relay")
        self.assertGreater(refreshed["runtimeRevision"], before_revision)
        self.assertEqual(refreshed["modelCapabilities"]["same-model"]["efforts"], ["high"])
        self.assertEqual(refreshed["modelCapabilities"]["same-model"]["contextWindow"], 123456)
        cached_capabilities = json.loads(json.dumps(refreshed["modelCapabilities"]))

        def forbidden(request, timeout=0):
            raise core.urllib.error.HTTPError(
                request.full_url,
                403,
                "forbidden",
                {},
                io.BytesIO(),
            )

        with patch.object(core, "_open_same_origin_request", side_effect=forbidden):
            stale = core.refresh_provider_models("capability_relay")

        self.assertEqual(stale["status"], "stale")
        self.assertFalse(stale["needsModel"])
        self.assertEqual(stale["modelCapabilities"], cached_capabilities)
        preserved = core.provider_by_id("capability_relay")
        self.assertEqual(preserved["models"], ["same-model"])
        self.assertEqual(preserved["modelCapabilities"], cached_capabilities)

    def test_capability_change_invalidates_an_inflight_probe_generation(self):
        settings = core._initial_settings()
        provider = self.provider_record(["model-a"])
        settings["providers"].append(provider)
        token = core._provider_probe_token(provider)
        provider["modelCapabilities"] = {
            "model-a": {
                "reasoningKnown": True,
                "reasoningSupported": False,
                "efforts": [],
                "defaultEffort": "",
            }
        }

        self.assertFalse(core._provider_probe_is_current(settings, provider["id"], token))

    def test_generated_catalog_uses_provider_metadata_and_strips_template_claims(self):
        settings = core._initial_settings()
        records = [
            {
                "key": "provider:p::capable",
                "id": "capable",
                "slug": "capable",
                "displayName": "capable",
                "sourceName": "Provider",
                "sourceKind": "provider",
                "reasoningKnown": True,
                "reasoningSupported": True,
                "efforts": ["high"],
                "defaultEffort": "high",
                "contextWindow": 131072,
                "maxContextWindow": 262144,
                "effectiveContextWindowPercent": 90,
                "supportsPersonality": False,
                "supportsVerbosity": True,
                "defaultVerbosity": "high",
            },
            {
                "key": "provider:p::unknown",
                "id": "unknown",
                "slug": "unknown",
                "displayName": "unknown",
                "sourceName": "Provider",
                "sourceKind": "provider",
                "reasoningKnown": False,
                "reasoningSupported": None,
                "efforts": [],
                "defaultEffort": "",
            },
            {
                "key": "provider:p::plain",
                "id": "plain",
                "slug": "plain",
                "displayName": "plain",
                "sourceName": "Provider",
                "sourceKind": "provider",
                "reasoningKnown": True,
                "reasoningSupported": False,
                "efforts": [],
                "defaultEffort": "",
            },
        ]
        template = {
            "slug": "gpt-5.4",
            "display_name": "Template",
            "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}],
            "default_reasoning_level": "low",
            "context_window": 272000,
            "max_context_window": 872000,
            "effective_context_window_percent": 95,
            "supports_personality": True,
            "support_verbosity": True,
            "default_verbosity": "low",
            "apply_patch_tool_type": "freeform",
            "supports_search_tool": True,
            "input_modalities": ["text", "image"],
            "service_tiers": [{"name": "fast"}],
            "tool_mode": "shell",
        }

        with (
            patch.object(core, "_configuration_model_records", return_value=records),
            patch.object(core, "managed_subagent_model_records", return_value=[]),
            patch.object(core, "_subagents_require_shared_gateway", return_value=False),
            patch.object(core, "_raw_local_model_catalog", return_value={"models": [template]}),
        ):
            catalog, _records = core.build_synced_model_catalog(settings)

        models = {item["slug"]: item for item in catalog["models"]}
        capable = models["capable"]
        self.assertEqual(capable["context_window"], 131072)
        self.assertEqual(capable["max_context_window"], 262144)
        self.assertEqual(capable["effective_context_window_percent"], 90)
        self.assertFalse(capable["supports_personality"])
        self.assertTrue(capable["support_verbosity"])
        self.assertEqual(capable["default_verbosity"], "high")
        self.assertEqual(
            [item["effort"] for item in capable["supported_reasoning_levels"]],
            ["high"],
        )
        unknown = models["unknown"]
        for key in (
            "default_reasoning_level",
            "context_window",
            "max_context_window",
            "supports_personality",
            "default_verbosity",
            "apply_patch_tool_type",
            "supports_search_tool",
            "input_modalities",
            "service_tiers",
            "tool_mode",
        ):
            self.assertNotIn(key, unknown)
        self.assertEqual(unknown["supported_reasoning_levels"], [])
        self.assertEqual(unknown["shell_type"], "default")
        self.assertFalse(unknown["support_verbosity"])
        self.assertEqual(unknown["truncation_policy"], {"mode": "bytes", "limit": 10_000})
        self.assertEqual(unknown["experimental_supported_tools"], [])
        self.assertFalse(unknown["supports_reasoning_summary_parameter"])
        self.assertFalse(unknown["supports_reasoning_summaries"])
        self.assertFalse(unknown["supports_parallel_tool_calls"])
        self.assertEqual(models["plain"]["supported_reasoning_levels"], [])
        self.assertNotIn("default_reasoning_level", models["plain"])

    def test_unknown_default_route_uses_model_auto_but_explicit_choice_is_preserved(self):
        settings = core._initial_settings()
        key = "provider:p::future-model"
        unknown = {
            "key": key,
            "id": "future-model",
            "name": "future-model",
            "sourceId": "provider:p",
            "sourceName": "Provider",
            "sourceKind": "provider",
            "reasoningKnown": False,
            "reasoningSupported": None,
            "efforts": [],
            "defaultEffort": "",
        }
        source = {"models": [unknown]}

        with (
            patch.object(core, "gateway_model_records", return_value=[unknown]),
            patch.object(core, "selected_model_records", return_value=[unknown]),
            patch.object(
                core,
                "_reasoning_capabilities",
                return_value={"future-model": {"efforts": list(core.VALID_EFFORTS)}},
            ),
        ):
            specs = core._managed_subagent_specs(settings)
        self.assertTrue(specs)
        self.assertTrue(all(item["effort"] is None for item in specs))

        effective = core._effective_subagent_routing(settings, [source], unknown)
        self.assertTrue(
            all(effective["routes"][level]["efforts"] == [""] for level in core.DIFFICULTIES)
        )

        settings["subagentRouting"]["routes"]["hard"] = {
            "models": [key],
            "efforts": ["max"],
        }
        explicit = core._effective_subagent_routing(settings, [source], unknown)
        self.assertEqual(explicit["routes"]["hard"]["efforts"], ["max"])
        with (
            patch.object(core, "gateway_model_records", return_value=[unknown]),
            patch.object(core, "selected_model_records", return_value=[unknown]),
            patch.object(core, "_reasoning_capabilities", return_value={}),
        ):
            hard = [
                item
                for item in core._managed_subagent_specs(settings)
                if item["level"] == "hard"
            ]
        self.assertEqual(hard[0]["effort"], "max")

    def test_explicit_no_reasoning_removes_an_incompatible_saved_effort(self):
        settings = core._initial_settings()
        key = "provider:p::plain-model"
        record = {
            "key": key,
            "id": "plain-model",
            "name": "plain-model",
            "sourceId": "provider:p",
            "sourceName": "Provider",
            "sourceKind": "provider",
            "reasoningKnown": True,
            "reasoningSupported": False,
            "efforts": [],
            "defaultEffort": "",
        }
        settings["subagentRouting"]["routes"]["hard"] = {
            "models": [key],
            "efforts": ["high"],
        }

        effective = core._effective_subagent_routing(
            settings,
            [{"models": [record]}],
            record,
        )
        self.assertEqual(effective["routes"]["hard"]["efforts"], [""])
        with (
            patch.object(core, "gateway_model_records", return_value=[record]),
            patch.object(core, "selected_model_records", return_value=[record]),
            patch.object(core, "_reasoning_capabilities", return_value={}),
        ):
            hard = [
                item
                for item in core._managed_subagent_specs(settings)
                if item["level"] == "hard"
            ]
        self.assertIsNone(hard[0]["effort"])

    def test_probe_reports_needs_model_without_claiming_authentication_success(self):
        with (
            patch.object(
                core,
                "_probe_provider_models_with_key",
                side_effect=core.ManagerError("模型目录返回 HTTP 403"),
            ),
            patch.object(
                core,
                "_probe_provider_balance_with_key",
                return_value=(None, "", []),
            ),
        ):
            result = core.probe_api_account(
                {
                    "baseUrl": "https://manual.example.test/v1",
                    "key": "sk-manual-test",
                }
            )

        self.assertTrue(result["needsModel"])
        self.assertEqual(result["models"], [])
        self.assertEqual(result["status"], "partial")
        self.assertNotEqual(result["status"], "ready")
        self.assertTrue(any("HTTP 403" in warning for warning in result["warnings"]))

    def test_public_connections_state_skips_agents_history_and_configuration_status(self):
        settings = core._initial_settings()
        provider = self.provider_record(["provider-model"])
        settings["providers"].append(provider)
        model = {
            "key": "provider:capability_relay::provider-model",
            "id": "provider-model",
            "name": "provider-model",
            "reasoningKnown": False,
            "efforts": [],
        }
        sources = [
            {
                "id": "provider:capability_relay",
                "kind": "provider",
                "recordId": "capability_relay",
                "name": "Capability Relay",
                "active": True,
                "available": True,
                "models": [model],
            }
        ]
        settings["modelWorkspace"].update(
            {
                "mode": "independent",
                "activeSourceId": "provider:capability_relay",
                "selectAll": True,
                "defaultModelKey": model["key"],
            }
        )

        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "local_model_catalog", return_value=[]),
            patch.object(core, "model_sources", return_value=sources),
            patch.object(core, "_configuration_model_records", return_value=[model]),
            patch.object(core, "public_account_records", return_value=[{"id": "public-account"}]),
            patch.object(core, "_secret_store", return_value={"relayAccounts": {}}),
            patch.object(core, "service_secret_configured", return_value=True),
            patch.object(core, "provider_key_configured", return_value=True),
            patch.object(core, "current_auth_state", return_value={"activeAccountId": None}),
            patch.object(core, "discover_agents", side_effect=AssertionError("must stay lightweight")),
            patch.object(core, "history_inventory", side_effect=AssertionError("must stay lightweight")),
            patch.object(core, "configuration_status", side_effect=AssertionError("must stay lightweight")),
        ):
            state = core.public_connections_state()

        self.assertEqual(
            set(state),
            {
                "settings",
                "modelSources",
                "selectedModelKeys",
                "effectiveSubagentRouting",
                "auth",
            },
        )
        self.assertEqual(
            set(state["settings"]),
            {
                "accounts",
                "providers",
                "relayAccounts",
                "accountGroups",
                "web2api",
                "modelWorkspace",
            },
        )
        self.assertEqual(state["settings"]["accounts"], [{"id": "public-account"}])
        self.assertTrue(state["settings"]["providers"][-1]["keyConfigured"])
        self.assertTrue(state["settings"]["web2api"]["keyConfigured"])
        self.assertEqual(state["selectedModelKeys"], [model["key"]])
        self.assertEqual(state["auth"], {"activeAccountId": None})


if __name__ == "__main__":
    unittest.main()
