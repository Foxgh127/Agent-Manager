from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_manager.core as core


class GatewaySecretTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, value in {"STATE_DIR": root, "SECRETS_FILE": root / "secrets.json", "SETTINGS_FILE": root / "settings.json"}.items():
            mock = patch.object(core, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        for mock in (patch.object(core, "dpapi_protect", side_effect=lambda text: text.encode()),
                     patch.object(core, "dpapi_unprotect", side_effect=lambda raw: raw.decode())):
            mock.start()
            self.addCleanup(mock.stop)

    def test_public_rotation_preserves_distinct_internal_key(self):
        public = core.rotate_web2api_key()
        internal = core.ensure_internal_gateway_secret()
        self.assertNotEqual(public, internal)
        self.assertEqual(core.ensure_internal_gateway_secret(), internal)
        self.assertNotEqual(core.rotate_web2api_key(), public)
        self.assertEqual(core.load_service_secret("gateway_internal"), internal)
        self.assertEqual(core._managed_environment_values({})[core.AGGREGATE_ENV_KEY], internal)

    def test_legacy_collision_is_replaced_without_rotating_public_key(self):
        core.store_service_secret("web2api", "legacy")
        core.store_service_secret("gateway_internal", "legacy")
        self.assertNotEqual(core.ensure_internal_gateway_secret(), "legacy")
        self.assertEqual(core.load_service_secret("web2api"), "legacy")

    def test_reimport_of_codex_config_excludes_managers_internal_provider(self):
        config = {"model_providers": {
            core.AGGREGATE_PROVIDER_ID: {"base_url": "http://127.0.0.1:17860/v1", "env_key": core.AGGREGATE_ENV_KEY},
            "custom": {"base_url": "https://api.example.test/v1", "env_key": "CUSTOM_API_KEY"},
        }}
        with patch.object(core, "read_toml", return_value={}), patch.object(core, "read_json", return_value={}), \
                patch.object(core, "LEGACY_PROFILE_FILE", Path(self.temp.name) / "missing.toml"):
            providers = core._initial_providers(config)
        ids = {item["id"] for item in providers}
        self.assertNotIn(core.AGGREGATE_PROVIDER_ID, ids)
        self.assertIn("custom", ids)


if __name__ == "__main__":
    unittest.main()
