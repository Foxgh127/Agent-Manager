from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import agent_manager.core as core


class GroupTransactionsV8Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, value in {
            "CODEX_HOME": root, "CONFIG_FILE": root / "config.toml", "AGENTS_DIR": root / "agents",
            "STATE_DIR": root / "state", "SETTINGS_FILE": root / "state/settings.json",
            "SECRETS_FILE": root / "state/secrets.json", "BACKUPS_DIR": root / "backups",
            "LEGACY_STATE_DIR": root / "legacy", "LEGACY_PROFILE_FILE": root / "legacy/config.toml",
        }.items():
            mocked = patch.object(core, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        core.ensure_state()
        self.group = core.save_account_group({"id": "custom", "name": "Before"})
        settings = core.load_settings()
        settings["accounts"] = [{"id": "a", "groupId": "custom", "models": []}]
        settings["providers"].append({"id": "p", "kind": "custom", "name": "Relay", "baseUrl": "https://relay.example.test/v1", "envKey": "RELAY_GROUP_KEY", "groupId": "custom", "models": []})
        settings["relayAccounts"] = [{"id": "r", "providerId": "p", "groupId": "custom", "origin": "https://relay.example.test", "keys": []}]
        core.save_settings(settings)
        core.load_settings()  # Normalize the deliberately minimal fixture once.

    def groups(self):
        settings = core.load_settings()
        return [next(item for item in settings[key] if item["id"] == identity)["groupId"]
                for key, identity in [("accounts", "a"), ("providers", "p"), ("relayAccounts", "r")]]

    def test_ui_name_edit_keeps_group_id_and_all_members(self):
        result = core.save_account_group({"originalId": "custom", "name": "After", "color": "cyan"})
        self.assertEqual(result["id"], "custom")
        self.assertEqual(self.groups(), ["custom"] * 3)

    def test_explicit_id_rename_migrates_all_source_references(self):
        result = core.save_account_group({"originalId": "custom", "id": "renamed", "name": "After"})
        self.assertEqual(result["id"], "renamed")
        self.assertEqual(self.groups(), ["renamed"] * 3)

    def test_move_updates_relay_with_only_primary_provider_reference(self):
        core.assign_sources_to_group("free", [], ["p"])
        self.assertEqual(self.groups(), ["custom", "free", "free"])

    def test_delete_group_preserves_all_sources_and_moves_them_to_defaults(self):
        before = core.load_settings()
        result = core.remove_account_group("custom")
        after = core.load_settings()
        self.assertEqual(self.groups(), ["official", "relay", "relay"])
        self.assertNotIn("custom", [item["id"] for item in after["accountGroups"]])
        for collection in ("accounts", "providers", "relayAccounts"):
            self.assertEqual([item["id"] for item in after[collection]], [item["id"] for item in before[collection]])
        self.assertEqual(after["modelWorkspace"], before["modelWorkspace"])
        self.assertEqual(after["web2api"], before["web2api"])
        self.assertEqual(result, {"movedAccounts": 1, "movedProviders": 1})

    def test_builtin_group_delete_is_rejected_without_changes(self):
        before = core.SETTINGS_FILE.read_bytes()
        with self.assertRaisesRegex(core.ManagerError, "内置分组不能删除"):
            core.remove_account_group("official")
        self.assertEqual(core.SETTINGS_FILE.read_bytes(), before)

    def test_stale_group_edit_is_rejected_without_creating_another_group(self):
        before = core.SETTINGS_FILE.read_bytes()
        with self.assertRaisesRegex(core.ManagerError, "不存在"):
            core.save_account_group({"originalId": "missing", "name": "After"})
        self.assertEqual(core.SETTINGS_FILE.read_bytes(), before)

    def test_assignment_validates_again_after_concurrent_rename(self):
        saving = threading.Event()
        release = threading.Event()
        assigned = threading.Event()
        errors = []
        save = core.save_settings
        def pause_save(settings):
            if threading.current_thread().name == "rename-group-test":
                saving.set()
                if not release.wait(2):
                    raise RuntimeError("test release timeout")
            save(settings)
        def rename():
            try:
                core.save_account_group({"originalId": "custom", "id": "renamed", "name": "After"})
            except Exception as exc:
                errors.append(exc)
        def assign():
            try:
                core.assign_sources_to_group("custom", ["a"], [])
            except core.ManagerError as exc:
                errors.append(exc)
            finally:
                assigned.set()
        with patch.object(core, "save_settings", side_effect=pause_save):
            first = threading.Thread(target=rename, name="rename-group-test")
            second = threading.Thread(target=assign)
            first.start()
            try:
                self.assertTrue(saving.wait(1))
                second.start()
                self.assertFalse(assigned.wait(0.05))
            finally:
                release.set()
                first.join(timeout=2)
                if second.ident:
                    second.join(timeout=2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], core.ManagerError)
        self.assertEqual(self.groups(), ["renamed"] * 3)


if __name__ == "__main__":
    unittest.main()
