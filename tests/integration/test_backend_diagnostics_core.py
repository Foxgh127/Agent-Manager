from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_manager.core as core


class BackendDiagnosticsCoreTests(unittest.TestCase):
    def test_independent_selection_counts_only_active_source(self):
        settings = {
            "modelWorkspace": {
                "mode": "independent",
                "activeSourceId": "account:stale",
                "selectAll": True,
                "selectedModels": [],
            }
        }
        sources = [
            {
                "id": "account:current",
                "kind": "account",
                "recordId": "current",
                "name": "Current",
                "active": True,
                "available": True,
                "models": [{"key": "account:current/gpt-a", "id": "gpt-a"}],
            },
            {
                "id": "account:other",
                "kind": "account",
                "recordId": "other",
                "name": "Other",
                "active": False,
                "available": True,
                "models": [{"key": "account:other/gpt-b", "id": "gpt-b"}],
            },
        ]
        with patch.object(core, "model_sources", return_value=sources):
            independent = core.selected_model_records(settings)
            settings["modelWorkspace"]["mode"] = "aggregate"
            aggregate = core.selected_model_records(settings)
        self.assertEqual([item["key"] for item in independent], ["account:current/gpt-a"])
        self.assertEqual({item["key"] for item in aggregate}, {"account:current/gpt-a", "account:other/gpt-b"})

    def test_configuration_status_exposes_mode_scoped_model_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_file = root / "config.toml"
            agents_file = root / "AGENTS.md"
            config_file.write_text('model = "gpt-a"\n', encoding="utf-8")
            agents_file.write_text(
                f"{core.MANAGED_BLOCK_START}\nstrategy (`adaptive`)\n{core.MANAGED_BLOCK_END}\n",
                encoding="utf-8",
            )
            settings = {
                "activeStrategyId": "adaptive",
                "modelWorkspace": {"mode": "independent", "activeSourceId": "account:current"},
            }
            with (
                patch.object(core, "CONFIG_FILE", config_file),
                patch.object(core, "AGENTS_FILE", agents_file),
                patch.object(core, "read_toml", return_value={"model": "gpt-a"}),
                patch.object(core, "build_codex_config", return_value='model = "gpt-a"\n'),
                patch.object(core, "build_agents_file", return_value=agents_file.read_text(encoding="utf-8")),
                patch.object(
                    core,
                    "selected_model_records",
                    return_value=[{"sourceId": "account:current", "key": "account:current/gpt-a"}],
                ),
            ):
                status = core.configuration_status(settings)
        self.assertEqual(status["modelCount"], 1)
        self.assertEqual(status["modelCountScope"], "current_account")
        self.assertEqual(status["activeSourceId"], "account:current")


if __name__ == "__main__":
    unittest.main()
