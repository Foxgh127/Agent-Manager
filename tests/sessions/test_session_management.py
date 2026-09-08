from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import agent_manager.core as core
import agent_manager.sessions.preferences


class SessionManagementV8Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, value in {
            "CODEX_HOME": root, "CONFIG_FILE": root / "config.toml", "STATE_DIR": root / "state",
            "SETTINGS_FILE": root / "state/settings.json", "SECRETS_FILE": root / "state/secrets.json",
            "BACKUPS_DIR": root / "backups", "LEGACY_STATE_DIR": root / "legacy",
            "LEGACY_PROFILE_FILE": root / "legacy/config.toml",
        }.items():
            mocked = patch.object(core, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        core.ensure_state()

    def test_manager_pins_persist_without_unsupported_git_metadata_calls(self):
        with patch.object(core, "codex_app_server_requests", side_effect=AssertionError("pin is not a Codex Git metadata field")):
            result = core.manage_codex_threads("pin", ["thread-one", "thread-two"])
            self.assertEqual(result["changed"], 2)
            self.assertEqual(core.manage_codex_threads("pin", ["thread-one"])["changed"], 0)
            core.manage_codex_threads("unpin", ["thread-two"])
        self.assertEqual(agent_manager.sessions.preferences.pinned_ids(core.load_settings()), {"thread-one"})
        rows = agent_manager.sessions.preferences.apply_to_threads([{"id": "thread-one"}, {"id": "thread-two", "pinned": True}])
        self.assertEqual([row["pinned"] for row in rows], [True, False])

    def test_malformed_saved_pin_preferences_are_not_overwritten(self):
        settings = core.load_settings()
        settings["sessionPreferences"] = {"pinnedThreadIds": "corrupt"}
        core.save_settings(settings)
        before = core.SETTINGS_FILE.read_bytes()
        with self.assertRaisesRegex(core.ManagerError, "置顶偏好"):
            core.manage_codex_threads("pin", ["thread-one"])
        self.assertEqual(core.SETTINGS_FILE.read_bytes(), before)

    @staticmethod
    def server_script(last_response):
        return (
            "import json,sys\n"
            "for line in sys.stdin:\n"
            " request=json.loads(line)\n"
            " ident=request.get('id')\n"
            " if ident==1: print(json.dumps({'id':1,'result':{}}),flush=True)\n"
            " elif ident==3:\n"
            "  print(json.dumps({'id':2,'result':{}}),flush=True)\n"
            f"  response={last_response!r}\n"
            "  if response is not None: print(json.dumps(response),flush=True)\n"
            "  break\n"
        )

    def test_batch_archive_reports_each_confirmed_failure(self):
        script = self.server_script({"id": 3, "error": {"code": -32000, "message": "archive denied"}})
        with patch.object(core, "codex_prefix", return_value=[sys.executable, "-u", "-c", script]):
            result = core.manage_codex_threads("archive", ["one", "two"])
        self.assertEqual(result["threadIds"], ["one"])
        self.assertEqual(result["changed"], 1)
        self.assertEqual(result["failed"][0]["threadId"], "two")
        self.assertEqual(result["unconfirmed"], [])

    def test_missing_batch_response_is_unconfirmed_not_reported_as_failed_or_successful(self):
        script = self.server_script(None)
        with patch.object(core, "codex_prefix", return_value=[sys.executable, "-u", "-c", script]):
            result = core.manage_codex_threads("restore", ["one", "two"])
        self.assertEqual(result["threadIds"], ["one"])
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["unconfirmed"][0]["threadId"], "two")


if __name__ == "__main__":
    unittest.main()
