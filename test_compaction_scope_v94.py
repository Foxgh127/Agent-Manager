import tomllib
import unittest

import tomlkit

import agent_manager_core as core


class CompactionScopeV94Tests(unittest.TestCase):
    def test_automatic_threshold_can_persist_an_explicit_scope(self):
        settings = {"runtimeTuning": core._default_runtime_tuning()}
        tuning = core._normalize_runtime_tuning_update(
            settings,
            {
                "autoCompactTokenLimit": 0,
                "autoCompactScope": "body_after_prefix",
            },
        )
        document = tomlkit.document()

        core._apply_root_runtime_tuning(document, tuning)

        parsed = tomllib.loads(tomlkit.dumps(document))
        self.assertEqual(
            tuning["managedFields"],
            ["autoCompactTokenLimit", "autoCompactScope"],
        )
        self.assertNotIn("model_auto_compact_token_limit", parsed)
        self.assertEqual(
            parsed["model_auto_compact_token_limit_scope"],
            "body_after_prefix",
        )

    def test_resetting_threshold_does_not_erase_explicitly_owned_scope(self):
        document = tomlkit.parse(
            "model_auto_compact_token_limit = 240000\n"
            'model_auto_compact_token_limit_scope = "body_after_prefix"\n'
        )
        settings = {
            "runtimeTuning": {
                "managedFields": ["autoCompactTokenLimit", "autoCompactScope"],
                "autoCompactTokenLimit": 240000,
                "autoCompactScope": "body_after_prefix",
            }
        }
        tuning = core._normalize_runtime_tuning_update(
            settings,
            {"autoCompactTokenLimit": 0},
        )

        core._apply_root_runtime_tuning(document, tuning)

        parsed = tomllib.loads(tomlkit.dumps(document))
        self.assertNotIn("model_auto_compact_token_limit", parsed)
        self.assertEqual(
            parsed["model_auto_compact_token_limit_scope"],
            "body_after_prefix",
        )

    def test_threshold_reset_leaves_user_owned_scope_untouched(self):
        document = tomlkit.parse(
            "# scope remains owned by the raw config editor\n"
            "model_auto_compact_token_limit = 240000\n"
            'model_auto_compact_token_limit_scope = "body_after_prefix"\n'
        )
        settings = {
            "runtimeTuning": {
                "managedFields": ["autoCompactTokenLimit"],
                "autoCompactTokenLimit": 240000,
                "autoCompactScope": "total",
            }
        }
        tuning = core._normalize_runtime_tuning_update(
            settings,
            {"autoCompactTokenLimit": 0},
        )

        core._apply_root_runtime_tuning(document, tuning)

        rendered = tomlkit.dumps(document)
        parsed = tomllib.loads(rendered)
        self.assertNotIn("model_auto_compact_token_limit", parsed)
        self.assertEqual(
            parsed["model_auto_compact_token_limit_scope"],
            "body_after_prefix",
        )
        self.assertIn("# scope remains owned by the raw config editor", rendered)

    def test_common_values_read_scope_when_threshold_is_unset(self):
        common = core._codex_config_common_values(
            {"model_auto_compact_token_limit_scope": "body_after_prefix"}
        )

        self.assertEqual(common["autoCompactTokenLimit"], 0)
        self.assertEqual(common["autoCompactScope"], "body_after_prefix")


if __name__ == "__main__":
    unittest.main()
