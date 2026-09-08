"""File-free regression tests; real membership helpers, in-memory persistence."""
import copy
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import agent_manager_core as core
from dashboard_order import move_dashboard_card, normalized_dashboard_order, reorder_visible


class DashboardOrderTests(unittest.TestCase):
    def setUp(self):
        self.saved = {
            "accountGroups": [{"id": "official"}, {"id": "relay"}, {"id": "work"}],
            "accounts": [{"id": name, "groupId": "official", "authMode": "chatgpt"} for name in ("a", "b", "c", "hidden")],
            "providers": [
                {"id": "p", "groupId": "relay"},
                {"id": "child", "groupId": "relay", "relayAccountId": "r"},
                {"id": "independent", "groupId": "relay"},
            ],
            "relayAccounts": [{"id": "r", "providerId": "p", "groupId": "relay"}],
            "web2api": {"sourceOrder": ["provider:child", "account:b"], "providerIds": ["child"], "accountIds": ["b"]},
            "dashboardOrder": ["account:a", "account:hidden", "account:b", "account:c", "relay:r", "provider:p", "provider:child", "provider:independent"],
            "unrelated": {"tokenReference": "do-not-touch", "currentAccountId": "b"},
        }
        self.original = copy.deepcopy(self.saved)
        self.save_count = 0
        self.fail_at = None
        self.lock_entries = []

        @contextmanager
        def file_lock():
            self.lock_entries.append("file")
            yield

        def save(settings):
            self.save_count += 1
            # Simulate an error even AFTER an OS commit: rollback must restore it.
            self.saved = copy.deepcopy(settings)
            if self.save_count == self.fail_at:
                raise OSError("simulated persistence failure")

        def restore(snapshot):
            self.saved = copy.deepcopy(snapshot["settings"])
            return []

        self.patcher = patch.multiple(core,
            SWITCH_OPERATION_LOCK=threading.RLock(), SETTINGS_LOCK=threading.RLock(),
            _settings_file_lock=file_lock, load_settings=lambda: copy.deepcopy(self.saved),
            save_settings=save, _capture_file_bytes=lambda paths: {"settings": copy.deepcopy(self.saved)},
            _restore_file_bytes=restore)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def move(self, **kwargs):
        return move_dashboard_card(core, kwargs)

    def assert_routing_unchanged(self):
        self.assertEqual(self.saved["web2api"], self.original["web2api"])
        self.assertEqual(self.saved["unrelated"], self.original["unrelated"])

    def test_visible_subset_reorders_without_moving_hidden_slots(self):
        result = self.move(sourceId="account:c", visibleIds=["account:a", "account:b", "account:c"], beforeId="account:a")
        self.assertEqual(result["dashboardOrder"][:4], ["account:c", "account:hidden", "account:a", "account:b"])
        self.assertEqual(self.saved["accounts"], self.original["accounts"])
        self.assert_routing_unchanged()

    def test_after_anchor_and_noop_anchor(self):
        self.assertEqual(reorder_visible(["a", "hidden", "b", "c"], ["a", "b", "c"], "a", after_id="c"), ["b", "hidden", "c", "a"])
        self.assertEqual(reorder_visible(["a", "b"], ["a", "b"], "a", before_id="a"), ["a", "b"])

    def test_first_move_preserves_rendered_order(self):
        self.saved.pop("dashboardOrder")
        result = self.move(sourceId="account:a", visibleIds=["account:c", "account:a", "account:b"], afterId="account:b")
        self.assertEqual([item for item in result["dashboardOrder"] if item in {"account:a", "account:b", "account:c"}],
                         ["account:c", "account:b", "account:a"])

    def test_saved_order_sanitizes_stale_duplicates_and_bad_types(self):
        self.saved["dashboardOrder"] = ["account:b", "missing:a", "account:b", None, {}, "relay:r"]
        order = normalized_dashboard_order(self.saved)
        self.assertEqual(order[:2], ["account:b", "relay:r"])
        self.assertEqual(len(order), 8)
        self.assertEqual(len(order), len(set(order)))

    def test_first_group_drop_keeps_existing_visible_order(self):
        self.saved.pop("dashboardOrder")
        visible = ["account:c", "account:a", "account:b"]
        result = self.move(sourceId="account:a", groupId="work", visibleIds=visible)
        self.assertEqual([item for item in result["dashboardOrder"] if item in visible], visible)

    def test_account_group_and_order_are_one_transaction(self):
        self.move(sourceId="account:c", visibleIds=["account:a", "account:b", "account:c"], beforeId="account:a", groupId="work")
        self.assertEqual(next(item for item in self.saved["accounts"] if item["id"] == "c")["groupId"], "work")
        self.assertEqual(self.saved["dashboardOrder"][0], "account:c")
        self.assert_routing_unchanged()

    def test_relay_group_moves_primary_and_all_child_providers(self):
        result = self.move(sourceId="relay:r", groupId="work")
        self.assertEqual(result["groupAssignments"], {"accounts": {}, "providers": {"p": "work", "child": "work"}, "relayAccounts": {"r": "work"}})
        self.assertEqual(self.saved["relayAccounts"][0]["groupId"], "work")
        self.assertEqual([item["groupId"] for item in self.saved["providers"]], ["work", "work", "relay"])
        self.assert_routing_unchanged()

    def test_child_provider_group_moves_legacy_primary_without_backref(self):
        self.move(sourceId="provider:child", groupId="work")
        self.assertEqual(self.saved["relayAccounts"][0]["groupId"], "work")
        self.assertEqual([item["groupId"] for item in self.saved["providers"]], ["work", "work", "relay"])
        self.assert_routing_unchanged()

    def test_relay_without_provider_can_move(self):
        self.saved["relayAccounts"].append({"id": "empty", "groupId": "relay"})
        self.move(sourceId="relay:empty", groupId="work")
        self.assertEqual(self.saved["relayAccounts"][-1]["groupId"], "work")

    def test_failed_final_save_restores_group_order_and_unrelated_settings(self):
        self.fail_at = 3  # relay save, providers save, dashboard save
        with self.assertRaisesRegex(OSError, "persistence"):
            self.move(sourceId="relay:r", groupId="work", visibleIds=["relay:r", "account:a"], beforeId="account:a")
        self.assertEqual(self.saved, self.original)

    def test_failed_first_save_restores_settings(self):
        self.fail_at = 1
        with self.assertRaises(OSError):
            self.move(sourceId="account:c", visibleIds=["account:a", "account:c"], beforeId="account:a")
        self.assertEqual(self.saved, self.original)

    def test_rejects_invalid_or_stale_payloads_without_changes(self):
        invalid = [None, {}, {"sourceId": "account:a"},
                   {"sourceId": "unknown:a", "groupId": "work"},
                   {"sourceId": "account:a", "groupId": "all"},
                   {"sourceId": "account:a", "groupId": "deleted"},
                   {"sourceId": "account:a", "beforeId": "account:b", "afterId": "account:c"},
                   {"sourceId": "account:a", "beforeId": "account:b", "visibleIds": ["account:a"]},
                   {"sourceId": "account:a", "beforeId": "account:b", "visibleIds": ["account:a", "account:b", "account:b"]},
                   {"sourceId": "account:a", "beforeId": "account:b", "visibleIds": ["account:a", "account:b", {}]}]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(core.ManagerError):
                move_dashboard_card(core, payload)
            self.assertEqual(self.saved, self.original)
        self.assertEqual(self.save_count, 0)


if __name__ == "__main__":
    unittest.main()
