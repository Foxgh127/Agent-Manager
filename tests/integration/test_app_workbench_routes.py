import json
import http.client
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock
from types import SimpleNamespace

import agent_manager.application as app
import agent_manager.usage.export


class _FakeWeb2API:
    def status(self):
        return {"running": False}

    def usage_snapshot(self):
        return {"days": [{"date": "2026-08-11"}]}

    def reset_usage_stats(self):
        return {"days": []}


class _FakeRadar:
    def __init__(self):
        self.calls = []

    @staticmethod
    def _section(name):
        return {
            "data": {"name": name, "items": [{"model": "gpt-test"}]},
            "source": {"url": "https://codex-reset-radar.pages.dev/", "format": "json"},
            "fetchedAt": "2026-08-12T00:00:00Z",
            "lastAttemptAt": "2026-08-12T00:00:00Z",
            "nextAllowedAt": None,
            "stale": False,
            "error": None,
            "cached": True,
        }

    def get_snapshot(self, accounts, *, startup=False):
        self.calls.append(("snapshot", len(list(accounts)), startup))
        return {
            "schemaVersion": 1,
            "intelligence": self._section("intelligence"),
            "quota": self._section("quota"),
            "reset": self._section("reset"),
            "sources": [{"url": "https://codex-reset-radar.pages.dev/"}],
        }

    def get_intelligence(self, *, refresh=False):
        self.calls.append(("intelligence", refresh))
        return self._section("intelligence-refreshed")

    def get_quota(self, *, refresh=False, force=False):
        self.calls.append(("quota", refresh, force))
        return self._section("quota-refreshed")

    def get_reset_radar(self, accounts, *, refresh=False):
        self.calls.append(("reset", len(list(accounts)), refresh))
        return self._section("reset-refreshed")


class _FakeToolbox:
    def __init__(self):
        self.fetches = []
        self.forgotten = []

    def state(self):
        return {
            "totpItems": [{"id": "totp_0123456789abcdef", "code": "123456", "secondsRemaining": 30}],
            "mailAccounts": [{"id": "primary", "email": "a@gmail.com"}],
        }

    def fetch_mail(self, account_id, *, limit, unread_only):
        self.fetches.append((account_id, limit, unread_only))
        return [{"id": "1", "subject": "Code"}]

    def fetch_temporary_mail(self, source, *, limit, unread_only):
        return []

    def forget_mail(self, account_id):
        self.forgotten.append(account_id)


class _FakeRuntime:
    def __init__(self):
        self.web2api = _FakeWeb2API()
        self.radar = _FakeRadar()
        self.toolbox = _FakeToolbox()
        self.update_check_status = {}

    @staticmethod
    def account_snapshot():
        return {
            "accounts": [{"id": "official", "usage": {"weekly": {"remainingPercent": 88}}}],
            "auth": {"activeAccountId": "official"},
            "revision": 7,
        }


class WorkbenchRouteTests(unittest.TestCase):
    def setUp(self):
        self.recovery_create = patch.object(app.recovery, "create", return_value={"id": "test-backup", "scope": "usage"})
        self.recovery_create_mock = self.recovery_create.start()
        self.addCleanup(self.recovery_create.stop)
        self.runtime = _FakeRuntime()
        self.server = app.ManagerServer(("127.0.0.1", 0), app.RequestHandler, self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_connection_projection_route_skips_full_state(self):
        with patch.object(app.core, "public_connections_state", return_value={"modelSources": []}) as light, patch.object(app.core, "public_state") as full:
            self.assertEqual(self.request("/api/connections"), {"ok": True, "modelSources": []})
        light.assert_called_once()
        full.assert_not_called()

    def test_relay_refresh_syncs_catalog_unless_quick_is_explicit(self):
        refresh = Mock(return_value={"account": {"id": "saved"}})
        self.runtime.relay_portal = SimpleNamespace(refresh_account=refresh)
        self.request("/api/relay-accounts/saved/refresh", method="POST")
        refresh.assert_called_once_with("saved", full=True)
        refresh.reset_mock()
        self.request("/api/relay-accounts/saved/refresh", method="POST", payload={"full": False})
        refresh.assert_called_once_with("saved", full=False)
        with self.assertRaises(urllib.error.HTTPError) as failed:
            self.request("/api/relay-accounts/saved/refresh", method="POST", payload={"full": "false"})
        self.assertEqual(failed.exception.code, 400)
        failed.exception.close()

    def test_configuration_retry_uses_explicit_runtime_recovery(self):
        self.runtime.activate_configuration_session = Mock(return_value={"active": True})
        result = self.request("/api/configuration-session/retry", method="POST")
        self.assertTrue(result["configurationSession"]["active"])
        self.runtime.activate_configuration_session.assert_called_once_with(retry=True)

    def test_relay_key_deletion_defaults_local_and_requires_explicit_website_scope(self):
        delete = Mock(return_value={"localRemoved":True,"remoteDeleted":False})
        self.runtime.relay_portal = SimpleNamespace(delete_saved_key=delete)
        events = []
        def guarded(_runtime, _name, operation):
            events.append("local-backup")
            return operation(), {"id":"local-point"}
        with patch.object(app.agent_manager.storage.coordinator,"before_account_delete",side_effect=guarded), patch.object(app.agent_manager.storage.coordinator,"create",side_effect=lambda *_args: events.append("remote-backup") or {"id":"remote-point"}):
            result=self.request("/api/relay-accounts/saved/keys/7",method="DELETE")
            self.assertTrue(result["result"]["localRemoved"])
            delete.assert_called_once_with("saved","7",delete_remote=False)
            delete.reset_mock()
            self.request("/api/relay-accounts/saved/keys/7",method="DELETE",payload={"scope":"website"})
            delete.assert_called_once_with("saved","7",delete_remote=True)
            self.assertEqual(events,["local-backup","remote-backup"])
            delete.reset_mock()
            with self.assertRaises(urllib.error.HTTPError) as failed:
                self.request("/api/relay-accounts/saved/keys/7",method="DELETE",payload={"scope":True})
            failed.exception.close()
            self.assertEqual(failed.exception.code,400)
            delete.assert_not_called()

    def test_failed_backup_blocks_remote_key_deletion(self):
        delete=Mock()
        self.runtime.relay_portal=SimpleNamespace(delete_saved_key=delete)
        with patch.object(app.agent_manager.storage.coordinator,"create",side_effect=app.core.ManagerError("backup failed")):
            with self.assertRaises(urllib.error.HTTPError) as failed:
                self.request("/api/relay-accounts/saved/keys/7",method="DELETE",payload={"scope":"website"})
            failed.exception.close()
        delete.assert_not_called()

    def test_relay_reauthentication_routes_preserve_account_identity(self):
        start = Mock(return_value={"mode": "reauth", "accountId": "saved", "sessionId": "login"})
        check = Mock(return_value={"status": "reauthenticated", "completion": {"accountId": "saved"}})
        self.runtime.relay_portal = SimpleNamespace(start=start, check_auto_auth=check)
        self.request("/api/relay-login/start", method="POST", payload={"url": "https://relay.example.test", "accountId": "saved"})
        start.assert_called_once_with("https://relay.example.test", account_id="saved")
        result = self.request("/api/relay-login/check", method="POST", payload={"sessionId": "login"})
        self.assertEqual(result["status"]["status"], "reauthenticated")
        check.assert_called_once_with("login", force=False)
        with self.assertRaises(urllib.error.HTTPError) as failed:
            self.request("/api/relay-login/check", method="POST", payload={"sessionId": "login", "force": "false"})
        self.assertEqual(failed.exception.code, 400)
        failed.exception.close()

    def test_recovery_routes_preserve_preview_and_restore_fingerprint(self):
        with patch.object(app.recovery, "list_points", return_value={"points": [], "invalidCount": 0}), \
                patch.object(app.recovery, "preview", return_value={"fingerprint": "expected", "changed": 1}), \
                patch.object(app.recovery, "restore", return_value={"restored": True}) as restore:
            self.assertEqual(self.request("/api/recovery")["points"], [])
            self.assertEqual(self.request("/api/recovery/point-test/preview", method="POST")["result"]["fingerprint"], "expected")
            self.request("/api/recovery/point-test/restore", method="POST", payload={"expectedFingerprint": "expected"})
            restore.assert_called_once_with("point-test", "expected")

    def test_usage_clear_creates_restore_point_before_reset(self):
        response = self.request("/api/usage/reset", method="POST")
        self.assertEqual(response["backup"]["id"], "test-backup")
        self.recovery_create_mock.assert_called_once_with("usage", "清空统计前", reason="before_clear")

    def test_orchestration_save_forwards_one_transaction_and_returns_document(self):
        payload = {"modelWorkspace": {"mode": "independent"}, "subagentRouting": {}, "runtimeTuning": {}}
        result = {"result": {"changed": True}, "runtimeTuning": {}, "document": {"valid": True}}
        with patch.object(app.core, "save_orchestration_and_apply", return_value=result) as save:
            response = self.request("/api/orchestration/save", method="POST", payload=payload)
        self.assertEqual(response["document"], {"valid": True})
        self.assertEqual(save.call_args.args, (payload,))
        self.assertTrue(callable(save.call_args.kwargs["ensure_gateway"]))

    def test_history_recovery_endpoints_preserve_review_fingerprint(self):
        with patch.object(app.history_sync, "list_recoveries", return_value={"recoveries": []}), \
                patch.object(app.history_sync, "preview_recovery", return_value={"fingerprint": "review"}) as preview, \
                patch.object(app.history_sync, "restore_recovery", return_value={"restored": True}) as restore:
            self.assertEqual(self.request("/api/history/recoveries")["recoveries"], [])
            self.assertEqual(self.request("/api/history/recoveries/history-sync-test/preview", method="POST")["result"]["fingerprint"], "review")
            response = self.request("/api/history/recoveries/history-sync-test/restore", method="POST", payload={"expectedFingerprint": "review"})
        self.assertTrue(response["result"]["restored"])
        preview.assert_called_once_with("history-sync-test")
        restore.assert_called_once_with("history-sync-test", "review")

    def test_history_sync_preview_uses_the_documented_preview_envelope(self):
        with patch.object(app.core, "preview_history_sync", return_value={"fingerprint": "planned"}):
            response = self.request("/api/history/preview", method="POST", payload={"target": "test"})
        self.assertEqual(response["preview"]["fingerprint"], "planned")

    def test_skill_toggle_does_not_coerce_string_false_into_enabled(self):
        with patch.object(app.maintenance, "set_skill_enabled") as toggle:
            for value in ["false", 0, None]:
                with self.subTest(value=value), self.assertRaises(urllib.error.HTTPError) as failed:
                    self.request("/api/skills/toggle", method="POST", payload={"id": "skill", "enabled": value})
                self.assertEqual(failed.exception.code, 400)
                failed.exception.close()
            toggle.assert_not_called()

    def test_usage_export_route_passes_server_filtered_document_to_download_writer(self):
        with patch.object(agent_manager.usage.export.core, "save_json_export_to_downloads", return_value={"path": "test-export.json"}) as save:
            response = self.request("/api/usage/export-download", method="POST", payload={"source": "gateway", "sourceFilter": "all", "model": "all", "date": "all"})
        self.assertEqual(response["result"]["path"], "test-export.json")
        document, name = save.call_args.args
        self.assertEqual(name, "usage-gateway")
        self.assertFalse(document["containsRawConversations"])
        self.assertEqual(document["recordCount"], 0)

    def request(self, path, *, method="GET", payload=None):
        data = None if payload is None and method == "GET" else json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-Agent-Manager-Token": self.server.api_token,
            },
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_read_only_workbench_routes_return_service_results(self):
        with (
            patch.object(app.maintenance, "list_skills", return_value={"skills": [], "count": 0}) as skills,
            patch.object(app.maintenance, "public_skill_catalog", return_value={"items": []}),
            patch.object(app.maintenance, "update_center_status", return_value={"components": {}}),
            patch.object(app.maintenance, "run_emergency_checks", return_value={"checks": []}),
            patch.object(app.claude, "list_profiles", return_value={"profiles": []}),
            patch.object(app.claude, "preview_claude_code_import", return_value={"available": False}),
            patch.object(app.core, "load_settings", return_value={"accounts": []}),
        ):
            self.assertEqual(self.request("/api/skills?force=1&cwd=C%3A%5Cwork")["count"], 0)
            self.assertEqual(self.request("/api/skills/catalog")["items"], [])
            self.assertEqual(self.request("/api/updates")["status"]["components"], {})
            self.assertEqual(self.request("/api/emergency/checks")["checks"], [])
            self.assertEqual(
                self.request("/api/accounts/snapshot")["accounts"][0]["usage"]["weekly"]["remainingPercent"],
                88,
            )
            self.assertEqual(self.request("/api/claude/profiles")["profiles"], [])
            self.assertFalse(self.request("/api/claude/import-preview")["preview"]["available"])
            self.assertEqual(self.request("/api/usage")["usage"]["days"][0]["date"], "2026-08-11")
            radar = self.request("/api/radar")
            self.assertEqual(radar["intelligence"]["name"], "intelligence")
            self.assertEqual(
                radar["intelligence"]["meta"]["sourceUrl"],
                "https://codex-reset-radar.pages.dev/",
            )
            self.assertEqual(self.runtime.radar.calls, [("snapshot", 0, True)])
            skills.assert_called_once_with(cwd="C:\\work", force=True)

    def test_codex_config_routes_read_and_save_documents(self):
        import agent_manager.config.recovery
        claim = patch.object(agent_manager.config.recovery, "claim_orphaned_overlay", return_value=False)
        claim.start()
        self.addCleanup(claim.stop)
        document = {
            "path": "C:\\Users\\test\\.codex\\config.toml",
            "valid": True,
            "content": 'model = "gpt-test"\n',
            "entries": [{"path": ["model"], "key": "model", "value": "gpt-test"}],
            "common": {"modelContextWindow": 0},
        }
        saved = {
            "changed": True,
            "backupPath": "backup.toml",
            "restartRequired": True,
            "document": document,
        }
        with (
            patch.object(app.core, "codex_config_document", return_value=document) as read_config,
            patch.object(app.core, "save_codex_config_document", return_value=saved) as save_config,
        ):
            self.assertEqual(self.request("/api/codex-config")["document"]["path"], document["path"])
            response = self.request(
                "/api/codex-config",
                method="POST",
                payload={"content": document["content"]},
            )

        self.assertTrue(response["changed"])
        self.assertTrue(response["restartRequired"])
        read_config.assert_called_once_with()
        save_config.assert_called_once_with({"content": document["content"]})

    def test_manual_account_refresh_bypasses_the_model_metadata_ttl(self):
        refreshed = {"id": "official", "models": ["gpt-6-astra"]}
        with patch.object(
            app.core,
            "refresh_codex_account",
            return_value=refreshed,
        ) as refresh:
            response = self.request(
                "/api/accounts/official/refresh",
                method="POST",
                payload={},
            )

        self.assertEqual(response["account"], refreshed)
        refresh.assert_called_once_with("official", force_metadata=True)

    def test_unexpected_api_exception_is_redacted(self):
        with patch.object(
            app.maintenance,
            "list_skills",
            side_effect=RuntimeError(r"secret at C:\\private\\credentials.json"),
        ):
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                self.request("/api/skills")
        self.assertEqual(rejected.exception.code, 500)
        payload = json.loads(rejected.exception.read().decode("utf-8"))
        rejected.exception.close()
        self.assertNotIn("credentials.json", payload["error"])
        self.assertNotIn("C:\\private", payload["error"])
        self.assertIn("内部错误", payload["error"])
        self.assertIn("credentials.json", self.runtime.last_error)

    def test_mutating_workbench_routes_are_wired_through_shutdown_gate(self):
        with (
            patch.object(app.maintenance, "set_skill_enabled", return_value={"skill": {"id": "s"}}) as toggle,
            patch.object(app.maintenance, "install_public_plugin", return_value={"installed": True}),
            patch.object(app.maintenance, "update_center_status", return_value={"components": {}}),
            patch.object(app.maintenance, "apply_emergency_repairs", return_value={"rechecked": True}),
            patch.object(app.claude, "save_profile", return_value={"id": "c"}),
            patch.object(app.claude, "apply_profile", return_value={"applied": True}),
            patch.object(app.claude, "restore_official", return_value={"applied": True}),
        ):
            self.assertEqual(
                self.request("/api/skills/toggle", method="POST", payload={"id": "s", "enabled": False})["skill"]["id"],
                "s",
            )
            self.assertTrue(self.request("/api/skills/install", method="POST", payload={"id": "p"})["result"]["installed"])
            self.assertEqual(self.request("/api/updates/check", method="POST")["status"], {"components": {}})
            self.assertTrue(
                self.request("/api/emergency/repair", method="POST", payload={"checkIds": ["runtime"]})["result"]["rechecked"]
            )
            self.assertEqual(self.request("/api/claude/profiles", method="POST", payload={"name": "C"})["profile"]["id"], "c")
            self.assertTrue(self.request("/api/claude/apply", method="POST", payload={"id": "c"})["result"]["applied"])
            self.assertTrue(self.request("/api/claude/restore", method="POST")["result"]["applied"])
            self.assertEqual(self.request("/api/usage/reset", method="POST")["usage"]["days"], [])
            refreshed = self.request(
                "/api/radar/refresh",
                method="POST",
                payload={"section": "quota"},
            )
            self.assertEqual(refreshed["quota"]["name"], "quota-refreshed")
            self.assertIn(("quota", True, True), self.runtime.radar.calls)
            toggle.assert_called_once_with("s", False, cwd=None)

    def test_toolbox_routes_support_edit_import_check_and_delete_flows(self):
        self.assertEqual(self.request("/api/toolbox/state")["totpItems"][0]["code"], "123456")
        edit_source = json.dumps({"_toolboxEdit": True, "id": "totp_0123456789abcdef"})
        with (
            patch.object(app.toolbox, "save_totp_item", return_value={"id": "totp_0123456789abcdef", "code": "654321", "seconds_remaining": 30}) as save_totp,
            patch.object(app.toolbox, "save_mail_import_selection", return_value=[{"id": "primary", "email": "a@gmail.com"}]) as save_mail,
            patch.object(app.toolbox, "delete_totp_item", return_value=True) as delete_totp,
            patch.object(app.toolbox, "delete_mail_account", return_value=True) as delete_mail,
        ):
            generated = self.request(
                "/api/toolbox/totp/generate",
                method="POST",
                payload={"input": edit_source, "label": "新备注", "save": True},
            )
            self.assertEqual(generated["result"]["code"], "654321")
            save_totp.assert_called_once_with(edit_source, label="新备注")
            imported = self.request(
                "/api/toolbox/mail/import",
                method="POST",
                payload={"text": '{"id":"primary","label":"工作邮箱"}', "selectedIndices": [0]},
            )
            self.assertEqual(imported["imported"], 1)
            save_mail.assert_called_once_with('{"id":"primary","label":"工作邮箱"}', [0])
            messages = self.request(
                "/api/toolbox/mail/primary/messages",
                method="POST",
                payload={"limit": 30},
            )
            self.assertEqual(messages["messages"][0]["subject"], "Code")
            self.assertEqual(self.runtime.toolbox.fetches, [("primary", 30, False)])
            self.assertTrue(self.request("/api/toolbox/totp/totp_0123456789abcdef", method="DELETE")["ok"])
            self.assertTrue(self.request("/api/toolbox/mail/primary", method="DELETE")["ok"])
            delete_totp.assert_called_once_with("totp_0123456789abcdef")
            delete_mail.assert_called_once_with("primary")
            self.assertEqual(self.runtime.toolbox.forgotten, ["primary"])

    def test_radar_refresh_rejects_unknown_sections(self):
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            self.request(
                "/api/radar/refresh",
                method="POST",
                payload={"section": "everything"},
            )
        self.assertEqual(rejected.exception.code, 400)
        rejected.exception.close()

    def test_delete_routes_forward_fingerprint_and_profile_id(self):
        with (
            patch.object(app.maintenance, "delete_skill", return_value={"deleted": True}) as remove_skill,
            patch.object(app.claude, "delete_profile", return_value={"deleted": True}) as remove_profile,
        ):
            self.assertTrue(
                self.request(
                    "/api/skills/skill-id",
                    method="DELETE",
                    payload={"expectedFingerprint": "fingerprint", "cwd": "C:\\work"},
                )["result"]["deleted"]
            )
            self.assertTrue(self.request("/api/claude/profiles/claude-id", method="DELETE")["result"]["deleted"])
            remove_skill.assert_called_once_with("skill-id", "fingerprint", cwd="C:\\work")
            remove_profile.assert_called_once_with("claude-id")

    def test_shutdown_gate_rejects_new_workbench_mutations(self):
        self.server.stop_accepting_mutations()
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            self.request(
                "/api/usage/reset",
                method="POST",
                payload={"pendingBody": "x" * 4096},
            )
        self.assertEqual(rejected.exception.code, 503)
        rejected.exception.close()

    def test_json_routes_reject_wrong_content_type(self):
        request = urllib.request.Request(
            self.base + "/api/usage/reset",
            data=b"{}",
            method="POST",
            headers={
                "Content-Type": "text/plain",
                "X-Agent-Manager-Token": self.server.api_token,
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(rejected.exception.code, 400)
        payload = json.loads(rejected.exception.read().decode("utf-8"))
        rejected.exception.close()
        self.assertIn("Content-Type", payload["error"])

    def test_request_framing_rejects_duplicate_content_length(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        try:
            connection.putrequest("POST", "/api/usage/reset")
            connection.putheader("Host", f"127.0.0.1:{self.server.server_address[1]}")
            connection.putheader("X-Agent-Manager-Token", self.server.api_token)
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", "2")
            connection.putheader("Content-Length", "2")
            connection.endheaders()
            connection.send(b"{}")
            response = connection.getresponse()
            body = json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()
        self.assertEqual(response.status, 400)
        self.assertIn("重复", body["error"])

    def test_root_ui_uses_lightweight_account_sync_without_whole_page_refresh(self):
        source = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "App.jsx").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("setInterval(() => reload", source)
        self.assertNotIn('setInterval(async () => {\n      try {\n        const result = await api("/api/oauth/status")', source)
        self.assertIn('api("/api/oauth/status", { signal: controller.signal })', source)
        self.assertIn("refreshStartupResourcesOnce().catch", source)
        self.assertIn('view === "routing"', source)
        self.assertIn('api("/api/accounts/snapshot")', source)
        self.assertIn('addEventListener("visibilitychange"', source)
        self.assertNotIn("setTimeout(\n      () => reload({ preserveWorkspace: true })", source)

    def test_relay_group_switch_is_optimistic_and_never_reloads_the_whole_app(self):
        source = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "App.jsx").read_text(
            encoding="utf-8"
        )
        start = source.index("const moveRelayAccountGroup")
        end = source.index("const exportRelayAccount", start)
        handler = source[start:end]
        self.assertIn("patchRelayAccountGroup(account, source, nextGroupId);", handler)
        self.assertIn("patchRelayAccountGroup(account, source, previousGroupId);", handler)
        self.assertNotIn("await reload()", handler)


if __name__ == "__main__":
    unittest.main()
