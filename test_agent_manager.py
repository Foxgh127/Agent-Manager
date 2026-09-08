import base64
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

import agent_manager_core as core
import agent_manager_app as app
import web2api_service as web2api


class AgentManagerTests(unittest.TestCase):
    def setUp(self):
        # Configuration tests exercise the same registry synchronization used
        # by the packaged application.  Always restore the reserved gateway
        # variable so a successful test run can never poison the developer's
        # real Codex environment or manufacture a later overlay conflict.
        self.original_aggregate_environment = core._read_user_environment(core.AGGREGATE_ENV_KEY)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / ".codex"
        self.originals = {}
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
            "RUNTIME_OVERLAY_FILE": self.root / "agent-manager" / "runtime-configuration-overlay.json",
            "RUNTIME_RESTORE_STATUS_FILE": self.root / "agent-manager" / "last-runtime-restore.json",
            "ACCOUNT_ACTIVATION_HISTORY_FILE": self.root / "agent-manager" / "account-activation-history.json",
            "MANAGED_CODEX_RUNTIME_DIR": self.root / "agent-manager" / "runtime" / "codex",
            "LEGACY_STATE_DIR": self.root / "legacy-switchboard",
            "LEGACY_PROFILE_FILE": self.root / "legacy-gateway.config.toml",
        }
        for name, value in paths.items():
            self.originals[name] = getattr(core, name)
            setattr(core, name, value)
        self.root.mkdir(parents=True)
        core.CONFIG_FILE.write_text(
            'service_tier = "default"\nmodel = "gpt-test-old"\nmodel_reasoning_effort = "medium"\n\n[desktop]\nkeep_me = true\n',
            encoding="utf-8",
        )
        core.AGENTS_FILE.write_text("# Personal rules\n\nKeep this line.\n", encoding="utf-8")
        self.model_patch = patch.object(
            core,
            "local_model_catalog",
            return_value=[{"id": "gpt-test-old", "name": "Test", "description": "", "efforts": ["medium"], "defaultEffort": "medium", "priority": 1}],
        )
        self.model_patch.start()

    def tearDown(self):
        self.model_patch.stop()
        for name, value in self.originals.items():
            setattr(core, name, value)
        current_aggregate_environment = core._read_user_environment(core.AGGREGATE_ENV_KEY)
        if current_aggregate_environment != self.original_aggregate_environment:
            if self.original_aggregate_environment is None:
                core._remove_user_environment(core.AGGREGATE_ENV_KEY)
            else:
                core._sync_user_environment(core.AGGREGATE_ENV_KEY, self.original_aggregate_environment)
        self.temp.cleanup()

    @staticmethod
    def jwt(payload):
        def encode(value):
            return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

        return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(payload)}.signature"

    @staticmethod
    def agent_private_key(seed: int = 1):
        der = bytes.fromhex("302e020100300506032b657004220420") + bytes([seed]) * 32
        return base64.b64encode(der).decode("ascii")

    def test_apply_preserves_unrelated_config_and_builds_routes(self):
        core.ensure_state()
        core.save_provider(
            {
                "id": "test_gateway",
                "name": "Test Gateway",
                "baseUrl": "https://example.invalid/v1",
                "envKey": "TEST_GATEWAY_KEY",
            }
        )
        core.write_agent(
            {
                "name": "quick_worker",
                "description": "Handles small bounded changes.",
                "provider": "openai",
                "model": "gpt-test-old",
                "effort": "medium",
                "sandbox": "read-only",
                "instructions": "Do only the delegated task and report checks.",
            }
        )
        routes = {
            level: {
                "enabled": level == "simple",
                "agents": ["quick_worker"] if level == "simple" else [],
                "description": core.DIFFICULTY_META[level]["description"],
            }
            for level in core.DIFFICULTIES
        }
        core.save_routes(routes)
        preview = core.preview_apply()
        self.assertTrue(preview["changed"])
        self.assertIn("model_providers.test_gateway", preview["diff"])
        self.assertIn("quick_worker", preview["diff"])

        result = core.apply_configuration(sync_secrets=False)
        self.assertTrue(result["changed"])
        parsed = tomllib.loads(core.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertTrue(parsed["desktop"]["keep_me"])
        self.assertEqual(parsed["model"], "gpt-test-old")
        self.assertEqual(parsed["model_providers"]["test_gateway"]["wire_api"], "responses")
        self.assertNotIn("max_concurrent_threads_per_session", parsed.get("agents", {}))
        self.assertNotIn("max_depth", parsed.get("agents", {}))
        agents_text = core.AGENTS_FILE.read_text(encoding="utf-8")
        self.assertIn("Keep this line.", agents_text)
        self.assertIn(core.MANAGED_BLOCK_START, agents_text)
        self.assertIn("`quick_worker`", agents_text)
        self.assertTrue(any(core.BACKUPS_DIR.iterdir()))

    def test_read_json_rejects_oversized_state_without_unbounded_read(self):
        target = core.STATE_DIR / "oversized.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as stream:
            stream.truncate(core.STATE_JSON_MAX_BYTES + 1)

        with self.assertRaisesRegex(core.ManagerError, "JSON 文件过大"):
            core.read_json(target, {})

    def test_request_existing_window_rejects_oversized_response(self):
        class FakeResponse:
            status = 200

            def read(self, limit=-1):
                self.limit = limit
                return b" " * limit

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        response = FakeResponse()
        with patch.object(app.urllib.request, "urlopen", return_value=response):
            opened = app.request_existing_window(
                {"port": 12345, "activationToken": "activation-token-1234567890"}
            )

        self.assertFalse(opened)
        self.assertEqual(response.limit, app.MAX_EXISTING_INSTANCE_RESPONSE_BYTES + 1)

    def test_windows_system_proxy_fills_missing_environment_proxy_schemes(self):
        with (
            patch.object(core.os, "name", "nt"),
            patch.object(
                core.urllib.request,
                "getproxies",
                return_value={"no": "localhost", "https": "http://env-proxy:8443"},
            ),
            patch.object(
                core.urllib.request,
                "getproxies_registry",
                return_value={
                    "http": "http://127.0.0.1:7899",
                    "https": "http://127.0.0.1:7899",
                },
                create=True,
            ),
        ):
            proxies = core._effective_url_proxies()

        self.assertEqual(proxies["http"], "http://127.0.0.1:7899")
        self.assertEqual(proxies["https"], "http://env-proxy:8443")
        self.assertEqual(proxies["no"], "localhost")

    def test_existing_quick_restart_uses_narrow_control_capability(self):
        captured = {}

        class FakeResponse:
            status = 200

            def read(self, _limit=-1):
                return json.dumps({"ok": True, "message": "restarting"}).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def open_request(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["headers"] = {
                key.casefold(): value for key, value in request.header_items()
            }
            return FakeResponse()

        runtime = {
            "port": 34567,
            "controlToken": "control-token-1234567890",
        }
        with (
            patch.object(app, "_probe_runtime_identity", return_value=True) as probe,
            patch.object(app.urllib.request, "urlopen", side_effect=open_request),
        ):
            result = app.request_existing_quick_restart(runtime)

        self.assertTrue(result["ok"])
        self.assertEqual(
            captured["url"],
            f"http://127.0.0.1:34567{app.CONTROL_QUICK_RESTART_PATH}",
        )
        self.assertEqual(
            captured["headers"][app.CONTROL_HEADER_NAME.casefold()],
            runtime["controlToken"],
        )
        probe.assert_called_once_with(
            runtime,
            require_ui_ready=True,
            require_independent=True,
        )

    def test_apply_marks_restart_when_only_required_environment_changes(self):
        settings = core._initial_settings()
        settings["modelWorkspace"]["mode"] = "aggregate"
        settings["modelWorkspace"]["syncToCodex"] = False
        core.save_settings(settings)
        core.CONFIG_FILE.write_text(core.build_codex_config(settings), encoding="utf-8")
        core.AGENTS_FILE.write_text(core.build_agents_file(settings), encoding="utf-8")
        with (
            patch.object(core, "service_secret_configured", return_value=True),
            patch.object(core, "load_service_secret", return_value="runtime-secret"),
            patch.object(core, "ensure_internal_gateway_secret", return_value="runtime-secret"),
            patch.object(core, "_read_user_environment", return_value=None),
            patch.object(core, "_sync_user_environment") as sync,
        ):
            result = core.apply_configuration(False)
        self.assertTrue(result["changed"])
        self.assertTrue(result["restartRequired"])
        sync.assert_called_once_with(core.AGGREGATE_ENV_KEY, "runtime-secret")

    def test_stale_settings_snapshots_merge_unrelated_edits(self):
        first = core.load_settings()
        second = core.load_settings()
        first["appBehavior"]["closeToTray"] = False
        second["accountGroups"].append(
            {"id": "friends", "name": "朋友", "color": "#60a5fa", "builtin": False}
        )

        core.save_settings(first)
        core.save_settings(second)

        merged = core.load_settings()
        self.assertFalse(merged["appBehavior"]["closeToTray"])
        self.assertIn("friends", {item["id"] for item in merged["accountGroups"]})

    def test_stale_settings_snapshots_merge_distinct_account_changes_and_additions(self):
        seeded = core.load_settings()
        seeded["accounts"] = [
            {"id": "one", "label": "One", "groupId": "official"},
            {"id": "two", "label": "Two", "groupId": "official"},
        ]
        core.save_settings(seeded)
        first = core.load_settings()
        second = core.load_settings()
        first["accounts"][0]["label"] = "One refreshed"
        second["accounts"][1]["groupId"] = "relay"
        second["accounts"].append({"id": "three", "label": "Three", "groupId": "daily"})

        core.save_settings(first)
        core.save_settings(second)

        accounts = {item["id"]: item for item in core.load_settings()["accounts"]}
        self.assertEqual(accounts["one"]["label"], "One refreshed")
        self.assertEqual(accounts["two"]["groupId"], "relay")
        self.assertEqual(accounts["three"]["label"], "Three")

    def test_provider_json_reader_rejects_declared_and_streamed_oversize_responses(self):
        class FakeResponse:
            def __init__(self, body, declared=None):
                self.body = body
                self.headers = {"Content-Length": str(declared if declared is not None else len(body))}

            def read(self, size=-1):
                return self.body if size < 0 else self.body[:size]

        with self.assertRaisesRegex(core.ManagerError, "安全限制"):
            core._read_limited_json_response(FakeResponse(b"{}", declared=100), 10, "测试")
        with self.assertRaisesRegex(core.ManagerError, "安全限制"):
            core._read_limited_json_response(FakeResponse(b"{" + b"x" * 30, declared=0), 10, "测试")
        self.assertEqual(core._read_limited_json_response(FakeResponse(b'{"ok":true}'), 100, "测试"), {"ok": True})

    def test_provider_url_rejects_link_local_and_unspecified_literal_targets(self):
        for value in (
            "https://169.254.169.254/v1",
            "https://[fe80::1]/v1",
            "https://0.0.0.0/v1",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(
                core.ManagerError,
                "不安全的保留或链路本地地址",
            ):
                core._validated_provider_url(value, "中转站 Base URL")

        self.assertEqual(
            core._validated_provider_url("http://127.0.0.1:8080/v1", "中转站 Base URL"),
            "http://127.0.0.1:8080/v1",
        )

    def test_provider_secret_rejects_header_injection_and_unbounded_values(self):
        for value in ("sk-good\r\nX-Evil: yes", "x" * 4_097):
            with self.subTest(value=value[:20]), self.assertRaisesRegex(
                core.ManagerError,
                "API Key 格式无效",
            ):
                core._validated_provider_secret(value)
        self.assertEqual(core._validated_provider_secret("  sk-good  "), "sk-good")

    def test_credential_request_redirects_must_remain_same_origin(self):
        handler = core._SameOriginRedirectHandler("https://relay.example.test/v1/models")
        request = core.urllib.request.Request(
            "https://relay.example.test/v1/models",
            headers={"Authorization": "Bearer secret"},
        )
        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://relay.example.test/v1/catalog",
        )
        self.assertEqual(redirected.full_url, "https://relay.example.test/v1/catalog")
        self.assertIn("Authorization", redirected.headers)

        with self.assertRaisesRegex(core.ManagerError, "其他来源"):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://collector.example.test/capture",
            )

        with self.assertRaisesRegex(core.ManagerError, "其他来源"):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "http://relay.example.test/v1/catalog",
            )

    def test_chatgpt_metadata_read_retries_one_transient_tls_eof_then_succeeds(self):
        class FakeResponse(io.BytesIO):
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        failure = urllib.error.URLError(
            ssl.SSLEOFError(8, "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")
        )
        response = FakeResponse(b'{"ok":true}')
        with (
            patch.object(core, "_throttle_chatgpt_request"),
            patch.object(core.time, "sleep"),
            patch.object(core, "_open_same_origin_request", side_effect=[failure, response]) as opened,
        ):
            payload = core._fetch_chatgpt_json("https://chatgpt.example.test/data", "token", "account")

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(opened.call_count, 2)

    def test_chatgpt_tls_eof_exhaustion_is_sanitized_and_bounded(self):
        failure = urllib.error.URLError(
            ssl.SSLEOFError(8, "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")
        )
        with (
            patch.object(core, "_throttle_chatgpt_request"),
            patch.object(core.time, "sleep"),
            patch.object(core, "_open_same_origin_request", side_effect=[failure, failure, failure]) as opened,
        ):
            with self.assertRaises(core.ManagerError) as raised:
                core._fetch_chatgpt_json("https://chatgpt.example.test/data", "token", "account")

        self.assertIn("临时中断了安全连接", str(raised.exception))
        self.assertNotIn("_ssl.c", str(raised.exception))
        self.assertEqual(opened.call_count, 3)

    def test_remote_error_message_redacts_credentials_before_display(self):
        secrets = (
            "sk-sensitive-error-value-123456789",
            "eyJhbGciOiJub25lIn0.eyJzdWIiOiJzZWNyZXQifQ.signature",
            "refresh-sensitive-error-value-123456789",
        )
        body = json.dumps(
            {
                "detail": (
                    f"Authorization: Bearer {secrets[0]} "
                    f"access_token={secrets[1]} refresh_token={secrets[2]}"
                )
            }
        ).encode("utf-8")
        error = urllib.error.HTTPError(
            "https://example.test/v1/models",
            401,
            "Unauthorized",
            {},
            io.BytesIO(body),
        )

        try:
            message = core._remote_error_message(error)
        finally:
            error.close()

        self.assertIn("HTTP 401", message)
        self.assertIn("已隐藏", message)
        for secret in secrets:
            self.assertNotIn(secret, message)

    def test_chatgpt_mutating_post_never_retries_transient_tls_eof(self):
        failure = urllib.error.URLError(
            ssl.SSLEOFError(8, "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")
        )
        with (
            patch.object(core, "_throttle_chatgpt_request"),
            patch.object(core.time, "sleep"),
            patch.object(core, "_open_same_origin_request", side_effect=failure) as opened,
        ):
            with self.assertRaises(core.ManagerError):
                core._post_chatgpt_json(
                    "https://chatgpt.example.test/mutate",
                    "token",
                    "account",
                    {"value": 1},
                )

        self.assertEqual(opened.call_count, 1)

    def test_automatic_file_backups_are_pruned_without_touching_directories(self):
        source = self.root / "bounded.txt"
        source.write_text("0", encoding="utf-8")
        protected_directory = core.BACKUPS_DIR / "history-sync-keep"
        protected_directory.mkdir(parents=True)
        (protected_directory / "session.jsonl").write_text("keep", encoding="utf-8")
        with patch.object(core, "BACKUP_MAX_FILES", 3), patch.object(core, "BACKUP_MAX_BYTES", 1_000_000):
            for index in range(6):
                source.write_text(str(index), encoding="utf-8")
                core.backup_file(source)
        self.assertLessEqual(len(list(core.BACKUPS_DIR.glob("*.bak"))), 3)
        self.assertTrue((protected_directory / "session.jsonl").is_file())

    @unittest.skipUnless(core.os.name == "nt", "runtime overlay uses Windows DPAPI")
    def test_runtime_overlay_restores_codex_defaults_and_preserves_unrelated_edits(self):
        core.ensure_state()
        original_agents = core.AGENTS_FILE.read_bytes()
        environment = {}

        def set_environment(name, value):
            environment[name] = value

        def remove_environment(name):
            environment.pop(name, None)

        managed_agent = core.AGENTS_DIR / "cam-simple-1.toml"
        with (
            patch.object(core, "_read_user_environment", side_effect=lambda name: environment.get(name)),
            patch.object(core, "_sync_user_environment", side_effect=set_environment),
            patch.object(core, "_remove_user_environment", side_effect=remove_environment),
        ):
            overlay = core.begin_runtime_configuration_overlay()
            self.assertTrue(overlay["active"])
            core.atomic_write_text(
                core.CONFIG_FILE,
                'model = "cam-gpt-test"\n'
                'model_provider = "cam_aggregate"\n'
                'model_reasoning_effort = "high"\n'
                f'model_catalog_json = "{core.MODEL_CATALOG_FILE.as_posix()}"\n'
                '\n'
                '[desktop]\nkeep_me = true\n\n'
                '[model_providers.cam_aggregate]\n'
                'name = "Temporary"\nbase_url = "http://127.0.0.1:17860/v1"\n'
                'env_key = "CODEX_AGENT_MANAGER_API_KEY"\nwire_api = "responses"\n\n'
                '[agents.cam_simple_1]\n'
                f'config_file = "{managed_agent.as_posix()}"\n'
                'description = "temporary"\n',
            )
            core.atomic_write_text(
                core.AGENTS_FILE,
                original_agents.decode("utf-8")
                + "\n"
                + core.MANAGED_BLOCK_START
                + "\nmanaged\n"
                + core.MANAGED_BLOCK_END
                + "\n",
            )
            core.atomic_write_text(
                managed_agent,
                core.render_agent_toml(
                    "cam_simple_1",
                    "temporary",
                    "cam-gpt-test",
                    "low",
                    "You are the simple difficulty worker in an ordered fallback route.",
                    core.AGGREGATE_PROVIDER_ID,
                    None,
                ),
            )
            core.atomic_write_text(core.MODEL_CATALOG_FILE, '{"models": []}\n')
            environment[core.AGGREGATE_ENV_KEY] = "runtime-key"
            core._runtime_overlay_record_applied(
                paths=[core.CONFIG_FILE, core.AGENTS_FILE, managed_agent, core.MODEL_CATALOG_FILE],
                environment=[core.AGGREGATE_ENV_KEY],
            )
            core.atomic_write_text(
                core.CONFIG_FILE,
                'user_runtime_note = "keep"\n' + core.CONFIG_FILE.read_text(encoding="utf-8"),
            )
            result = core.restore_runtime_configuration_overlay()

        restored = tomllib.loads(core.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertTrue(result["restored"])
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(restored["model"], "gpt-test-old")
        self.assertNotIn("model_provider", restored)
        self.assertEqual(restored["user_runtime_note"], "keep")
        self.assertTrue(restored["desktop"]["keep_me"])
        self.assertNotIn(core.AGGREGATE_PROVIDER_ID, restored.get("model_providers", {}))
        self.assertNotIn("cam_simple_1", restored.get("agents", {}))
        self.assertEqual(core.AGENTS_FILE.read_bytes(), original_agents)
        self.assertFalse(managed_agent.exists())
        self.assertFalse(core.MODEL_CATALOG_FILE.exists())
        self.assertNotIn(core.AGGREGATE_ENV_KEY, environment)
        self.assertFalse(core.RUNTIME_OVERLAY_FILE.exists())

    @unittest.skipUnless(core.os.name == "nt", "runtime overlay uses Windows DPAPI")
    def test_codex_config_save_rebases_active_runtime_overlay(self):
        core.ensure_state()
        environment = {}

        with (
            patch.object(core, "_read_user_environment", side_effect=lambda name: environment.get(name)),
            patch.object(core, "_sync_user_environment", side_effect=lambda name, value: environment.__setitem__(name, value)),
            patch.object(core, "_remove_user_environment", side_effect=lambda name: environment.pop(name, None)),
        ):
            core.begin_runtime_configuration_overlay()
            saved = core.save_codex_config_document(
                {
                    "content": (
                        'model = "temporary-manager-model"\n'
                        'model_provider = "cam_aggregate"\n'
                        'user_saved_note = "keep after exit"\n'
                        '\n[model_providers.cam_aggregate]\n'
                        'name = "Temporary"\n'
                        'base_url = "http://127.0.0.1:17860/v1"\n'
                        'env_key = "CODEX_AGENT_MANAGER_API_KEY"\n'
                    )
                }
            )
            self.assertTrue(saved["changed"])
            restored = core.restore_runtime_configuration_overlay()

        parsed = tomllib.loads(core.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertTrue(restored["restored"])
        self.assertEqual(parsed["user_saved_note"], "keep after exit")
        self.assertNotIn("model_provider", parsed)
        self.assertNotIn("cam_aggregate", parsed.get("model_providers", {}))
        self.assertFalse(core.RUNTIME_OVERLAY_FILE.exists())

    @unittest.skipUnless(core.os.name == "nt", "runtime overlay uses Windows DPAPI")
    def test_runtime_overlay_keeps_journal_when_a_restore_write_fails(self):
        core.ensure_state()
        original = core.CONFIG_FILE.read_bytes()
        overlay = core.begin_runtime_configuration_overlay()
        self.assertTrue(overlay["active"])
        core.atomic_write_text(core.CONFIG_FILE, 'model = "temporary-manager-model"\n')
        core._runtime_overlay_record_applied(paths=[core.CONFIG_FILE])
        real_atomic_write_bytes = core.atomic_write_bytes

        def fail_config_write(path, content):
            if Path(path) == core.CONFIG_FILE:
                raise PermissionError("simulated locked config")
            return real_atomic_write_bytes(path, content)

        with patch.object(core, "atomic_write_bytes", side_effect=fail_config_write):
            result = core.restore_runtime_configuration_overlay()

        self.assertFalse(result["restored"])
        self.assertIn("config.toml", result["remainingFiles"])
        self.assertTrue(core.RUNTIME_OVERLAY_FILE.exists())
        self.assertEqual(core.CONFIG_FILE.read_text(encoding="utf-8"), 'model = "temporary-manager-model"\n')

        retried = core.restore_runtime_configuration_overlay()
        self.assertTrue(retried["restored"])
        self.assertEqual(core.CONFIG_FILE.read_bytes(), original)
        self.assertFalse(core.RUNTIME_OVERLAY_FILE.exists())

    @unittest.skipUnless(core.os.name == "nt", "runtime overlay uses Windows DPAPI")
    def test_runtime_overlay_discards_rotated_manager_owned_gateway_key(self):
        core.ensure_state()
        environment = {}

        def set_environment(name, value):
            environment[name] = value

        def remove_environment(name):
            environment.pop(name, None)

        with (
            patch.object(core, "_read_user_environment", side_effect=lambda name: environment.get(name)),
            patch.object(core, "_sync_user_environment", side_effect=set_environment),
            patch.object(core, "_remove_user_environment", side_effect=remove_environment),
        ):
            core.begin_runtime_configuration_overlay()
            environment[core.AGGREGATE_ENV_KEY] = "old-manager-key"
            core._runtime_overlay_record_applied(environment=[core.AGGREGATE_ENV_KEY])
            # Simulate an older build rotating its private gateway key without
            # updating the journal's applied hash.
            environment[core.AGGREGATE_ENV_KEY] = "new-manager-key"
            result = core.restore_runtime_configuration_overlay()

        self.assertTrue(result["restored"])
        self.assertEqual(result["conflicts"], [])
        self.assertNotIn(core.AGGREGATE_ENV_KEY, environment)
        self.assertTrue(any(core.AGGREGATE_ENV_KEY in item for item in result["warnings"]))
        self.assertFalse(core.RUNTIME_OVERLAY_FILE.exists())

    @unittest.skipUnless(core.os.name == "nt", "runtime overlay uses Windows DPAPI")
    def test_runtime_overlay_preserves_real_user_environment_conflict(self):
        core.ensure_state()
        environment = {core.AGGREGATE_ENV_KEY: "user-baseline"}

        def set_environment(name, value):
            environment[name] = value

        def remove_environment(name):
            environment.pop(name, None)

        with (
            patch.object(core, "_read_user_environment", side_effect=lambda name: environment.get(name)),
            patch.object(core, "_sync_user_environment", side_effect=set_environment),
            patch.object(core, "_remove_user_environment", side_effect=remove_environment),
        ):
            core.begin_runtime_configuration_overlay()
            environment[core.AGGREGATE_ENV_KEY] = "manager-key"
            core._runtime_overlay_record_applied(environment=[core.AGGREGATE_ENV_KEY])
            environment[core.AGGREGATE_ENV_KEY] = "external-edit"
            result = core.restore_runtime_configuration_overlay()

        self.assertFalse(result["restored"])
        self.assertIn(f"环境变量 {core.AGGREGATE_ENV_KEY}", result["conflicts"])
        self.assertEqual(environment[core.AGGREGATE_ENV_KEY], "external-edit")
        self.assertTrue(core.RUNTIME_OVERLAY_FILE.exists())

    def test_remote_provider_requires_https_but_loopback_http_is_allowed(self):
        core.ensure_state()
        with self.assertRaisesRegex(core.ManagerError, "必须使用 HTTPS"):
            core.save_provider(
                {
                    "id": "insecure_remote",
                    "name": "Insecure remote",
                    "baseUrl": "http://api.example.test/v1",
                    "envKey": "INSECURE_REMOTE_KEY",
                }
            )
        local = core.save_provider(
            {
                "id": "local_loopback",
                "name": "Local loopback",
                "baseUrl": "http://127.0.0.1:8080/v1",
                "envKey": "LOCAL_LOOPBACK_KEY",
            }
        )
        self.assertEqual(local["baseUrl"], "http://127.0.0.1:8080/v1")

    def test_provider_portal_presets_match_exact_hosts_and_reject_lookalikes(self):
        presets = {item["id"]: item for item in core.PROVIDER_PORTAL_PRESETS}
        self.assertEqual(presets["hajimi"]["baseUrl"], "https://api.hajimi.chat/v1")
        self.assertEqual(presets["hajimi"]["balanceEndpoint"], "https://api.hajimi.chat/v1/usage")
        self.assertEqual(presets["fastaitoken"]["modelsEndpoint"], "https://www.fastaitoken.com/v1/models")
        self.assertFalse(hasattr(core, "public_provider_portal_presets"))
        self.assertEqual(
            core.detect_provider_portal_preset("https://www.fastaitoken.com/v1")["id"],
            "fastaitoken",
        )
        self.assertEqual(
            core.detect_provider_portal_preset("https://api.hajimi.chat/v1")["id"],
            "hajimi",
        )
        self.assertIsNone(core.detect_provider_portal_preset("https://www.fastaitoken.com.evil.test/v1"))
        self.assertIsNone(core.detect_provider_portal_preset("https://evil-fastaitoken.com/v1"))
        with patch.object(app.webbrowser, "open_new_tab", return_value=True) as opened:
            self.assertTrue(app.open_external_browser("https://hajimi.chat/dashboard"))
        opened.assert_called_once_with("https://hajimi.chat/dashboard")

    def test_legacy_hajimi_provider_migrates_from_spa_host_to_published_api_host(self):
        settings = core._initial_settings()
        settings["providers"].append(
            {
                "id": "legacy_hajimi",
                "name": "Legacy Hajimi",
                "kind": "custom",
                "baseUrl": "https://hajimi.chat",
                "presetId": "hajimi",
                "portalUrl": "https://hajimi.chat/dashboard",
                "modelsEndpoint": "https://hajimi.chat/v1/models",
                "balanceEndpoint": "https://hajimi.chat/v1/usage",
                "envKey": "LEGACY_HAJIMI_API_KEY",
                "models": ["gpt-test"],
                "groupId": "relay",
            }
        )

        migrated, _changed = core._migrate_settings(settings)
        provider = next(item for item in migrated["providers"] if item["id"] == "legacy_hajimi")

        self.assertEqual(provider["baseUrl"], "https://api.hajimi.chat/v1")
        self.assertEqual(provider["modelsEndpoint"], "https://api.hajimi.chat/v1/models")
        self.assertEqual(provider["balanceEndpoint"], "https://api.hajimi.chat/v1/usage")
        self.assertEqual(provider["lastCheckStatus"], "pending")

    def test_manual_provider_skips_hidden_quick_fill_and_legacy_site_id_is_host_bound(self):
        core.ensure_state()
        provider = core.save_provider(
            {
                "id": "hajimi_relay",
                "name": "Hajimi Relay",
                "baseUrl": "https://hajimi.chat",
                "envKey": "HAJIMI_RELAY_KEY",
            }
        )
        self.assertEqual(provider["presetId"], "")
        self.assertEqual(provider["baseUrl"], "https://hajimi.chat")
        self.assertEqual(provider["portalUrl"], "")
        self.assertEqual(provider["integrationKind"], "")
        with self.assertRaisesRegex(core.ManagerError, "尚未配置"):
            core.provider_portal_url(provider["id"])

        legacy = core.save_provider(
            {
                "id": provider["id"],
                "name": provider["name"],
                "baseUrl": provider["baseUrl"],
                "envKey": provider["envKey"],
                "presetId": "hajimi",
                "portalUrl": "https://hajimi.chat/dashboard",
            },
            original_id=provider["id"],
        )
        self.assertEqual(legacy["presetId"], "hajimi")
        self.assertEqual(legacy["baseUrl"], "https://api.hajimi.chat/v1")
        self.assertEqual(core.provider_portal_url(legacy["id"]), "https://hajimi.chat/dashboard")
        with self.assertRaisesRegex(core.ManagerError, "可疑站点"):
            core.save_provider(
                {
                    "id": legacy["id"],
                    "name": legacy["name"],
                    "baseUrl": legacy["baseUrl"],
                    "envKey": legacy["envKey"],
                    "presetId": "hajimi",
                    "portalUrl": "https://hajimi.chat.evil.test/dashboard",
                },
                original_id=legacy["id"],
            )

    def test_sub2api_balance_parser_and_probe_use_key_scoped_usage_endpoint(self):
        class FakeResponse(io.BytesIO):
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        parsed = core._parse_provider_balance(
            {
                "mode": "quota_limited",
                "isValid": True,
                "quota": {"limit": 20, "used": 7.5, "remaining": 12.5, "unit": "USD"},
            }
        )
        self.assertEqual(parsed["amount"], 12.5)
        self.assertEqual(parsed["currency"], "USD")
        self.assertEqual(parsed["integrationKind"], "sub2api")
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            core.save_provider(
                {
                    "id": "generic_sub2api",
                    "name": "Generic Sub2API",
                    "baseUrl": "https://relay.example.test",
                    "envKey": "GENERIC_SUB2API_KEY",
                }
            )
            core.store_provider_key("generic_sub2api", "sk-sub2api")
            payload = {
                "mode": "unrestricted",
                "isValid": True,
                "planName": "Codex Plan",
                "remaining": 9.25,
                "unit": "USD",
            }
            with patch.object(
                core,
                "_open_same_origin_request",
                return_value=FakeResponse(json.dumps(payload).encode()),
            ) as opened:
                balance = core.fetch_provider_balance("generic_sub2api")
        self.assertEqual(opened.call_args.args[0].full_url, "https://relay.example.test/v1/usage")
        self.assertEqual(balance["planName"], "Codex Plan")
        saved = core.provider_by_id("generic_sub2api")
        self.assertEqual(saved["balanceEndpoint"], "https://relay.example.test/v1/usage")
        self.assertEqual(saved["integrationKind"], "sub2api")

    def test_new_api_balance_probe_combines_subscription_usage_and_key_quota(self):
        class FakeResponse(io.BytesIO):
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        responses = [
            FakeResponse(json.dumps({"hard_limit_usd": 25}).encode()),
            FakeResponse(json.dumps({"total_usage": 725}).encode()),
            FakeResponse(
                json.dumps(
                    {
                        "data": {
                            "unlimited_quota": False,
                            "total_granted": 25,
                            "total_available": 17.75,
                            "model_limits_enabled": True,
                        }
                    }
                ).encode()
            ),
        ]
        with patch.object(
            core,
            "_open_same_origin_request",
            side_effect=responses,
        ) as opened:
            balance, endpoint, failures = core._probe_provider_balance_with_key(
                "https://new-api.example.test/v1",
                "sk-new-api",
                integration_kind="new_api",
            )

        self.assertFalse(failures)
        self.assertEqual(endpoint, "https://new-api.example.test/dashboard/billing/subscription")
        self.assertEqual(balance["integrationKind"], "new_api")
        self.assertEqual(balance["limit"], 25)
        self.assertEqual(balance["used"], 7.25)
        self.assertEqual(balance["amount"], 17.75)
        self.assertTrue(balance["modelLimitsEnabled"])
        self.assertEqual(
            [call.args[0].full_url for call in opened.call_args_list],
            [
                "https://new-api.example.test/dashboard/billing/subscription",
                "https://new-api.example.test/dashboard/billing/usage",
                "https://new-api.example.test/api/usage/token/",
            ],
        )

    def test_api_account_probe_needs_only_base_url_and_key_and_never_returns_secret(self):
        with (
            patch.object(
                core,
                "_probe_provider_models_with_key",
                return_value={
                    "models": ["gpt-probe"],
                    "modelsEndpoint": "https://api.example.test/v1/models",
                    "resolvedBaseUrl": "https://api.example.test/v1",
                    "latencyMs": 42,
                },
            ),
            patch.object(
                core,
                "_probe_provider_balance_with_key",
                return_value=(
                    {
                        "amount": 9.5,
                        "currency": "USD",
                        "integrationKind": "sub2api",
                    },
                    "https://api.example.test/v1/usage",
                    [],
                ),
            ),
        ):
            probe = core.probe_api_account(
                {"baseUrl": "https://api.example.test/v1", "key": "sk-never-return"}
            )

        self.assertEqual(probe["status"], "ready")
        self.assertEqual(probe["name"], "example.test")
        self.assertEqual(probe["models"], ["gpt-probe"])
        self.assertEqual(probe["integrationKind"], "sub2api")
        self.assertTrue(probe["id"].startswith("api_api_example_test_"))
        self.assertNotIn("key", probe)
        self.assertNotIn("sk-never-return", json.dumps(probe))

    def test_api_account_import_needs_only_base_url_and_key(self):
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "fetch_provider_models", return_value=["gpt-auto"]),
            patch.object(
                core,
                "fetch_provider_balance",
                return_value={"amount": 8.5, "currency": "USD"},
            ),
        ):
            result = core.import_api_account(
                {
                    "baseUrl": "https://relay.example.test/v1",
                    "key": "sk-two-fields-only",
                    "activate": False,
                }
            )

        provider = result["provider"]
        self.assertEqual(provider["name"], "relay.example.test")
        self.assertTrue(provider["id"].startswith("api_relay_example_test_"))
        self.assertEqual(provider["models"], ["gpt-auto"])
        self.assertEqual(result["profile"]["model"], "gpt-auto")
        self.assertNotIn("sk-two-fields-only", json.dumps(result))

    def test_provider_url_rejects_embedded_credentials_and_bad_ports(self):
        core.ensure_state()
        for value, message in (
            ("https://user:password@example.test/v1", "用户名或密码"),
            ("https://example.test:70000/v1", "端口"),
        ):
            with self.subTest(value=value), self.assertRaisesRegex(core.ManagerError, message):
                core.save_provider(
                    {
                        "id": "bad_provider",
                        "name": "Bad provider",
                        "baseUrl": value,
                        "envKey": "BAD_PROVIDER_KEY",
                    }
                )

    def test_provider_rejects_query_bases_and_cross_origin_credential_endpoints(self):
        core.ensure_state()
        cases = (
            ({"baseUrl": "https://relay.example.test/v1?target=other"}, "查询参数"),
            (
                {
                    "baseUrl": "https://relay.example.test/v1",
                    "balanceEndpoint": "https://collector.example.test/balance",
                },
                "同一来源",
            ),
            (
                {
                    "baseUrl": "https://relay.example.test/v1",
                    "resolvedBaseUrl": "https://collector.example.test/v1",
                },
                "同一来源",
            ),
        )
        for index, (extra, message) in enumerate(cases):
            with self.subTest(extra=extra), self.assertRaisesRegex(core.ManagerError, message):
                core.save_provider(
                    {
                        "id": f"unsafe_provider_{index}",
                        "name": "Unsafe provider",
                        "envKey": f"UNSAFE_PROVIDER_{index}_KEY",
                        **extra,
                    }
                )

    def test_provider_accepts_same_origin_balance_query_and_can_clear_it(self):
        core.ensure_state()
        created = core.save_provider(
            {
                "id": "safe_provider",
                "name": "Safe provider",
                "baseUrl": "https://relay.example.test/v1",
                "envKey": "SAFE_PROVIDER_KEY",
                "balanceEndpoint": "https://relay.example.test/billing?currency=usd",
            }
        )
        self.assertEqual(created["balanceEndpoint"], "https://relay.example.test/billing?currency=usd")
        updated = core.save_provider(
            {
                "id": "safe_provider",
                "name": "Safe provider",
                "baseUrl": "https://relay.example.test/v1",
                "envKey": "SAFE_PROVIDER_KEY",
                "balanceEndpoint": "",
            },
            original_id="safe_provider",
        )
        self.assertEqual(updated["balanceEndpoint"], "")

    def test_legacy_cross_origin_resolved_provider_fails_closed_before_config_write(self):
        core.ensure_state()
        settings = core.load_settings()
        settings["providers"].append(
            {
                "id": "legacy_unsafe",
                "name": "Legacy unsafe",
                "kind": "custom",
                "baseUrl": "https://relay.example.test/v1",
                "resolvedBaseUrl": "https://collector.example.test/v1",
                "envKey": "LEGACY_UNSAFE_KEY",
                "wireApi": "responses",
                "models": ["gpt-test"],
            }
        )
        settings["managedProviderIds"].append("legacy_unsafe")
        with self.assertRaisesRegex(core.ManagerError, "同一来源"):
            core.build_codex_config(settings)

    def test_imported_provider_cannot_repurpose_protected_environment_variables(self):
        core.ensure_state()
        for index, env_key in enumerate(("PATH", core.AGGREGATE_ENV_KEY)):
            with self.subTest(env_key=env_key):
                bundle = core.export_bundle()
                provider_id = f"malicious_import_{index}"
                bundle["settings"]["providers"].append(
                    {
                        "id": provider_id,
                        "name": "Malicious import",
                        "kind": "custom",
                        "baseUrl": "https://relay.example.test/v1",
                        "envKey": env_key,
                        "models": ["gpt-test"],
                    }
                )
                bundle["settings"]["managedProviderIds"].append(provider_id)
                with self.assertRaisesRegex(core.ManagerError, "保留项"):
                    core.import_bundle(bundle, mode="merge")
                self.assertFalse(
                    any(item.get("id") == provider_id for item in core.load_settings()["providers"])
                )

    def test_provider_environment_keys_must_be_unique(self):
        core.ensure_state()
        core.save_provider(
            {
                "id": "relay_one",
                "name": "Relay one",
                "baseUrl": "https://one.example.test/v1",
                "envKey": "SHARED_RELAY_KEY",
            }
        )
        with self.assertRaisesRegex(core.ManagerError, "已由中转站"):
            core.save_provider(
                {
                    "id": "relay_two",
                    "name": "Relay two",
                    "baseUrl": "https://two.example.test/v1",
                    "envKey": "shared_relay_key",
                }
            )

    def test_process_identity_filter_rejects_same_name_unrelated_programs(self):
        desktop = Path("C:/Program Files/WindowsApps/OpenAI.Codex_1/app/ChatGPT.exe")
        with (
            patch.object(core, "_is_manager_downloaded_codex_path", return_value=False),
            patch.object(core, "_is_desktop_managed_codex_path", return_value=False),
        ):
            self.assertTrue(core._trusted_codex_process_path("ChatGPT.exe", str(desktop), desktop))
            self.assertTrue(
                core._trusted_codex_process_path(
                    "codex.exe",
                    str(desktop.parent / "resources" / "codex.exe"),
                    desktop,
                )
            )
            self.assertTrue(
                core._trusted_codex_process_path(
                    "codex.exe",
                    "C:/Users/test/AppData/Roaming/npm/node_modules/@openai/codex/bin/codex.exe",
                    desktop,
                )
            )
            self.assertFalse(
                core._trusted_codex_process_path(
                    "codex.exe",
                    "C:/UnrelatedVendor/codex.exe",
                    desktop,
                )
            )
            self.assertFalse(
                core._trusted_codex_process_path(
                    "ChatGPT.exe",
                    "C:/UnrelatedVendor/ChatGPT.exe",
                    desktop,
                )
            )
            self.assertFalse(core._trusted_codex_process_path("codex.exe", None, desktop))

    @unittest.skipUnless(core.os.name == "nt", "DPAPI is Windows-only")
    def test_export_never_contains_provider_secret(self):
        core.ensure_state()
        core.save_provider(
            {
                "id": "private_gateway",
                "name": "Private Gateway",
                "baseUrl": "https://private.invalid",
                "envKey": "PRIVATE_GATEWAY_KEY",
            }
        )
        secret = "test-secret-" + "x" * 24
        core.store_provider_key("private_gateway", secret)
        bundle = core.export_bundle()
        encoded = json.dumps(bundle)
        self.assertFalse(bundle["containsSecrets"])
        self.assertNotIn(secret, encoded)
        self.assertNotIn("ciphertext", encoded)
        self.assertEqual(core.load_provider_key("private_gateway"), secret)

    def test_main_profiles_agents_and_import_round_trip(self):
        core.ensure_state()
        core.save_main_profile(
            {"id": "deep", "name": "Deep", "provider": "openai", "model": "gpt-deep", "effort": "max"}
        )
        core.set_active_main("deep")
        core.write_agent(
            {
                "name": "deep_worker",
                "description": "Deep independent implementation.",
                "provider": "openai",
                "model": "gpt-deep-worker",
                "effort": "max",
                "sandbox": "workspace-write",
                "instructions": "Stay inside the delegated scope.",
            }
        )
        content = (core.AGENTS_DIR / "deep-worker.toml").read_text(encoding="utf-8")
        parsed_agent = tomllib.loads(content)
        self.assertEqual(parsed_agent["model"], "gpt-deep-worker")
        self.assertEqual(parsed_agent["sandbox_mode"], "workspace-write")

        bundle = core.export_bundle()
        core.remove_main_profile("current")
        result = core.import_bundle(bundle, mode="merge")
        self.assertEqual(result["agentsImported"], 1)
        settings = core.load_settings()
        self.assertTrue(any(item["id"] == "deep" for item in settings["mainProfiles"]))

    def test_import_bundle_migrates_previous_settings_schema(self):
        bundle = core.export_bundle()
        bundle["settings"]["schemaVersion"] = 10
        bundle["settings"].pop("subagentRouting", None)
        result = core.import_bundle(bundle, mode="merge")
        self.assertEqual(result["migratedToSchema"], core.SCHEMA_VERSION)
        self.assertEqual(core.load_settings()["schemaVersion"], core.SCHEMA_VERSION)

    def test_import_bundle_rolls_back_all_agents_when_later_write_fails(self):
        core.ensure_state()
        first = core.AGENTS_DIR / "first.toml"
        first.write_text('name = "original"\ndescription = "old"\nmodel = "gpt-test"\n', encoding="utf-8")
        bundle = core.export_bundle()
        bundle["agents"] = [
            {"fileName": "first.toml", "content": 'name = "first"\ndescription = "new"\nmodel = "gpt-test"\n'},
            {"fileName": "second.toml", "content": 'name = "second"\ndescription = "new"\nmodel = "gpt-test"\n'},
        ]
        original_write = core.atomic_write_text
        calls = 0

        def fail_second(path, content):
            nonlocal calls
            if Path(path).parent == core.AGENTS_DIR:
                calls += 1
                if calls == 2:
                    raise OSError("simulated disk failure")
            return original_write(path, content)

        with patch.object(core, "atomic_write_text", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "simulated disk failure"):
                core.import_bundle(bundle, mode="merge")
        self.assertIn('name = "original"', first.read_text(encoding="utf-8"))
        self.assertFalse((core.AGENTS_DIR / "second.toml").exists())

    def test_aggregate_workspace_registers_four_levels_with_three_fallbacks(self):
        core.ensure_state()
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            provider = core.save_provider(
                {
                    "id": "deepseek_test",
                    "name": "DeepSeek Test",
                    "baseUrl": "https://example.invalid/v1",
                    "envKey": "DEEPSEEK_TEST_KEY",
                    "groupId": "relay",
                }
            )
            core.store_provider_key(provider["id"], "sk-deepseek-test")
            settings = core.load_settings()
            target = next(item for item in settings["providers"] if item["id"] == provider["id"])
            target["models"] = ["deepseek-chat", "deepseek-reasoner", "deepseek-fast"]
            core.save_settings(settings)
            source_id = f"provider:{provider['id']}"
            keys = [f"{source_id}::{model}" for model in target["models"]]
            core.save_model_workspace(
                {
                    "mode": "aggregate",
                    "activeSourceId": source_id,
                    "selectAll": True,
                    "selectedModels": keys,
                    "defaultModelKey": keys[0],
                    "syncToCodex": True,
                }
            )
            core.save_subagent_routing(
                {
                    "advanced": True,
                    "strategyId": "adaptive",
                    "prompt": "Delegate bounded work and actually invoke the configured difficulty Agent.",
                    "routes": {
                        level: {"models": keys, "efforts": ["", "high", "ultra"]}
                        for level in core.DIFFICULTIES
                    },
                }
            )
            settings = core.load_settings()
            config = tomllib.loads(core.build_codex_config(settings))
            block = core.build_routing_block(settings)
            simple_specs = [
                item for item in core._managed_subagent_specs(settings) if item["level"] == "simple"
            ]
        self.assertEqual(config["model_provider"], core.AGGREGATE_PROVIDER_ID)
        self.assertEqual(len([name for name in config["agents"] if name.startswith("cam_")]), 12)
        self.assertEqual([item["effort"] for item in simple_specs], [None, "high", "ultra"])
        self.assertTrue(all(item["model"].startswith("cam-agent-simple-") for item in simple_specs))
        resolved_child = core.resolve_model_route(simple_specs[0]["model"], settings)
        self.assertTrue(resolved_child["subagentAlias"])
        self.assertEqual(resolved_child["subagentLevel"], "simple")
        self.assertEqual(resolved_child["sourceRecordId"], provider["id"])
        self.assertNotIn("model_reasoning_effort", tomllib.loads(core._render_managed_agent(simple_specs[0])))
        self.assertEqual(
            tomllib.loads(core._render_managed_agent(simple_specs[1]))["model_reasoning_effort"],
            "high",
        )
        self.assertIn("cam_simple_1", block)
        self.assertIn("cam_simple_2", block)
        self.assertIn("cam_simple_3", block)
        self.assertIn("actually invoke", block)
        self.assertIn("Subagent lifecycle safety", block)
        self.assertIn("summarize, claim completion, or return a final", block)
        self.assertIn('fork_turns="none"', block)
        managed_instructions = tomllib.loads(core._render_managed_agent(simple_specs[0]))[
            "developer_instructions"
        ]
        self.assertIn("Begin working immediately", managed_instructions)
        self.assertIn("report blocked immediately", managed_instructions)

    def test_subagent_specs_keep_effort_paired_when_an_earlier_fallback_is_unavailable(self):
        settings = core._initial_settings()
        settings["subagentRouting"]["routes"]["hard"] = {
            "models": ["provider:offline::model-a", "provider:online::model-b"],
            "efforts": ["low", "xhigh"],
        }
        available = {
            "key": "provider:online::model-b",
            "id": "model-b",
            "sourceName": "Online",
        }
        with (
            patch.object(core, "gateway_model_records", return_value=[available]),
            patch.object(
                core,
                "_reasoning_capabilities",
                return_value={"model-b": {"efforts": ["xhigh"], "defaultEffort": "xhigh"}},
            ),
        ):
            hard_specs = [
                item for item in core._managed_subagent_specs(settings) if item["level"] == "hard"
            ]

        self.assertEqual(len(hard_specs), 1)
        self.assertEqual(hard_specs[0]["modelKey"], "provider:online::model-b")
        self.assertEqual(hard_specs[0]["effort"], "xhigh")

    def test_subagent_specs_do_not_rebind_an_unavailable_configured_route(self):
        settings = core._initial_settings()
        settings["subagentRouting"]["routes"]["expert"] = {
            "models": ["provider:offline::model-a"],
            "efforts": ["max"],
        }
        available = {
            "key": "provider:other::model-b",
            "id": "model-b",
            "sourceName": "Other",
        }
        with patch.object(core, "gateway_model_records", return_value=[available]):
            expert_specs = [
                item for item in core._managed_subagent_specs(settings) if item["level"] == "expert"
            ]

        self.assertEqual(expert_specs, [])

    def test_subagent_safety_preserves_user_runtime_concurrency_settings(self):
        core.CONFIG_FILE.write_text(
            '[agents]\n'
            'interrupt_message = false\n'
            'max_threads = 12\n'
            'max_depth = 4\n'
            '\n[features.multi_agent_v2]\n'
            'enabled = true\n'
            'max_concurrent_threads_per_session = 30\n',
            encoding="utf-8",
        )
        parsed = tomllib.loads(core.build_codex_config(core._initial_settings()))
        self.assertFalse(parsed["agents"]["interrupt_message"])
        self.assertEqual(parsed["agents"]["max_threads"], 12)
        self.assertEqual(parsed["agents"]["max_depth"], 4)
        self.assertNotIn("max_concurrent_threads_per_session", parsed["agents"])
        self.assertTrue(parsed["features"]["multi_agent_v2"]["enabled"])
        self.assertEqual(
            parsed["features"]["multi_agent_v2"]["max_concurrent_threads_per_session"],
            30,
        )

    def test_runtime_tuning_leaves_existing_user_config_untouched_until_managed(self):
        core.CONFIG_FILE.write_text(
            'model_context_window = 123456\n'
            '[model_providers.relay]\n'
            'name = "Relay"\n'
            'base_url = "https://relay.example/v1"\n'
            'env_key = "RELAY_API_KEY"\n'
            'request_max_retries = 9\n',
            encoding="utf-8",
        )

        parsed = tomllib.loads(core.build_codex_config(core._initial_settings()))

        self.assertEqual(parsed["model_context_window"], 123456)
        self.assertEqual(parsed["model_providers"]["relay"]["request_max_retries"], 9)

    def test_runtime_tuning_writes_only_beginner_owned_official_keys(self):
        core.CONFIG_FILE.write_text(
            '[tools.web_search]\n'
            'allowed_domains = ["openai.com"]\n'
            '\n[model_providers.relay]\n'
            'name = "Relay"\n'
            'base_url = "https://relay.example/v1"\n'
            'env_key = "RELAY_API_KEY"\n',
            encoding="utf-8",
        )
        settings = core._initial_settings()
        settings["runtimeTuning"].update(
            {
                "configManaged": True,
                "managedFields": [
                    "modelContextWindow",
                    "autoCompactTokenLimit",
                    "autoCompactScope",
                    "mcpOptionalStartupGraceMs",
                    "webSearch",
                    "webSearchContextSize",
                    "serviceTier",
                    "preventIdleSleep",
                ],
                "enabled": True,
                "modelContextWindow": 400000,
                "autoCompactTokenLimit": 300000,
                "autoCompactScope": "body_after_prefix",
                "mcpOptionalStartupGraceMs": 2000,
                "reasoningSummary": "concise",
                "verbosity": "high",
                "webSearch": "indexed",
                "webSearchContextSize": "high",
                "personality": "pragmatic",
                "serviceTier": "fast",
                "checkForUpdates": False,
                "tuiAnimations": False,
                "requestMaxRetries": 3,
                "streamMaxRetries": 2,
                "streamIdleTimeoutMs": 600000,
                "supportsWebsockets": True,
                "preventIdleSleep": True,
            }
        )

        parsed = tomllib.loads(core.build_codex_config(settings))
        relay = parsed["model_providers"]["relay"]

        self.assertEqual(parsed["model_context_window"], 400000)
        self.assertEqual(parsed["model_auto_compact_token_limit"], 300000)
        self.assertEqual(parsed["model_auto_compact_token_limit_scope"], "body_after_prefix")
        self.assertEqual(parsed["mcp_optional_startup_grace_ms"], 2000)
        self.assertEqual(parsed["web_search"], "indexed")
        self.assertEqual(parsed["tools"]["web_search"]["context_size"], "high")
        self.assertEqual(parsed["tools"]["web_search"]["allowed_domains"], ["openai.com"])
        self.assertEqual(parsed["service_tier"], "fast")
        self.assertTrue(parsed["features"]["prevent_idle_sleep"])
        self.assertNotIn("model_reasoning_summary", parsed)
        self.assertNotIn("model_verbosity", parsed)
        self.assertNotIn("personality", parsed)
        self.assertNotIn("check_for_update_on_startup", parsed)
        self.assertNotIn("tui", parsed)
        self.assertNotIn("request_max_retries", relay)
        self.assertNotIn("stream_max_retries", relay)
        self.assertNotIn("stream_idle_timeout_ms", relay)
        self.assertNotIn("supports_websockets", relay)

    def test_runtime_tuning_does_not_override_websocket_without_visible_repair(self):
        settings = core._initial_settings()
        settings["runtimeTuning"].update(
            {
                "configManaged": True,
                "enabled": True,
                "supportsWebsockets": False,
                "vpnCompatibility": False,
            }
        )
        record = {
            "key": "account:test::gpt-test",
            "sourceId": "account:test",
            "id": "gpt-test",
            "slug": "gpt-test",
            "displayName": "GPT Test",
            "sourceName": "Official",
        }
        template = {
            "slug": "gpt-5.4",
            "display_name": "GPT 5.4",
            "description": "template",
            "prefer_websockets": True,
        }

        with (
            patch.object(core, "selected_model_records", return_value=[record]),
            patch.object(core, "_raw_local_model_catalog", return_value={"models": [template]}),
        ):
            catalog, _records = core.build_synced_model_catalog(settings)

        self.assertTrue(catalog["models"][0]["prefer_websockets"])

    def test_runtime_tuning_vpn_repair_disables_websocket_preference(self):
        settings = core._initial_settings()
        settings["runtimeTuning"].update(
            {
                "configManaged": True,
                "managedFields": ["vpnCompatibility"],
                "enabled": True,
                "vpnCompatibility": True,
            }
        )
        record = {
            "key": "account:test::gpt-test",
            "sourceId": "account:test",
            "id": "gpt-test",
            "slug": "gpt-test",
            "displayName": "GPT Test",
            "sourceName": "Official",
        }
        template = {
            "slug": "gpt-5.4",
            "display_name": "GPT 5.4",
            "description": "template",
            "prefer_websockets": True,
        }

        with (
            patch.object(core, "selected_model_records", return_value=[record]),
            patch.object(core, "_raw_local_model_catalog", return_value={"models": [template]}),
        ):
            catalog, _records = core.build_synced_model_catalog(settings)

        self.assertFalse(catalog["models"][0]["prefer_websockets"])

    def test_synced_astra_catalog_does_not_inherit_unsupported_ultra_effort(self):
        settings = core._initial_settings()
        record = {
            "key": "account:test::gpt-6-astra",
            "sourceId": "account:test",
            "id": "gpt-6-astra",
            "slug": "gpt-6-astra",
            "displayName": "gpt-6-astra",
            "sourceName": "Official",
            "reasoningKnown": True,
            "efforts": ["low", "medium", "high", "xhigh", "max"],
            "defaultEffort": "",
        }
        template = {
            "slug": "gpt-5.4",
            "display_name": "GPT 5.4",
            "description": "template",
            "default_reasoning_level": "low",
            "supported_reasoning_levels": [
                {"effort": effort, "description": effort}
                for effort in ["low", "medium", "high", "xhigh", "max", "ultra"]
            ],
        }

        with (
            patch.object(core, "selected_model_records", return_value=[record]),
            patch.object(core, "_raw_local_model_catalog", return_value={"models": [template]}),
        ):
            catalog, _records = core.build_synced_model_catalog(settings)

        self.assertEqual(
            [item["effort"] for item in catalog["models"][0]["supported_reasoning_levels"]],
            ["low", "medium", "high", "xhigh", "max"],
        )

    def test_runtime_tuning_rejects_compaction_at_or_beyond_context_window(self):
        with self.assertRaisesRegex(core.ManagerError, "自动压缩阈值必须小于模型上下文长度"):
            core.save_runtime_tuning(
                {
                    "modelContextWindow": 272000,
                    "autoCompactTokenLimit": 272000,
                }
            )

    def test_runtime_tuning_tracks_field_scoped_ownership(self):
        first = core.save_runtime_tuning({"webSearch": "live"})
        second = core.save_runtime_tuning({"preventIdleSleep": True})

        self.assertEqual(first["managedFields"], ["webSearch"])
        self.assertEqual(second["managedFields"], ["webSearch", "preventIdleSleep"])
        self.assertTrue(second["configManaged"])
        self.assertEqual(second["personality"], "")
        self.assertEqual(second["verbosity"], "")

    def test_runtime_tuning_migrates_old_broad_owner_without_answer_style(self):
        migrated = core._normalize_runtime_tuning(
            {
                "configManaged": True,
                "enabled": True,
                "modelContextWindow": 400000,
                "personality": "friendly",
                "verbosity": "high",
                "checkForUpdates": False,
            }
        )

        self.assertEqual(migrated["managedFields"], ["modelContextWindow"])
        self.assertTrue(migrated["configManaged"])
        self.assertEqual(migrated["personality"], "friendly")
        self.assertEqual(migrated["verbosity"], "high")

    def test_runtime_tuning_gates_optional_mcp_grace_by_codex_version(self):
        with (
            patch.object(core, "_codex_supports_mcp_optional_startup_grace", return_value=False),
            self.assertRaisesRegex(core.ManagerError, "Codex CLI 0.151.0"),
        ):
            core.save_runtime_tuning({"mcpOptionalStartupGraceMs": 1000})

        with patch.object(core, "_codex_supports_mcp_optional_startup_grace", return_value=False):
            saved = core.save_runtime_tuning({"mcpOptionalStartupGraceMs": -1})
        self.assertEqual(saved["mcpOptionalStartupGraceMs"], -1)

    def test_codex_config_document_reads_full_file_and_common_values(self):
        core.MODELS_CACHE_FILE.write_text(
            json.dumps(
                {
                    "models": [
                        {
                            "slug": "gpt-test-old",
                            "context_window": 272000,
                            "max_context_window": 872000,
                            "effective_context_window_percent": 95,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        core.CONFIG_FILE.write_text(
            '# personal comment\n'
            'model = "gpt-test-old"\n'
            'model_provider = "relay"\n'
            'model_context_window = 400000\n'
            'model_auto_compact_token_limit = 300000\n'
            'model_auto_compact_token_limit_scope = "body_after_prefix"\n'
            'mcp_optional_startup_grace_ms = 1500\n'
            'web_search = "live"\n'
            'personality = "friendly"\n'
            'service_tier = "fast"\n'
            'check_for_update_on_startup = false\n'
            '\n[desktop]\n'
            'keep_me = true\n'
            '\n[tui]\n'
            'animations = false\n'
            '\n[tools.web_search]\n'
            'context_size = "medium"\n'
            '\n[features]\n'
            'prevent_idle_sleep = true\n'
            '\n[model_providers.relay]\n'
            'name = "Relay"\n'
            'base_url = "https://relay.example/v1"\n'
            'request_max_retries = 7\n'
            'stream_max_retries = 0\n'
            'supports_websockets = false\n',
            encoding="utf-8",
        )

        document = core.codex_config_document()

        self.assertTrue(document["valid"])
        self.assertIn("# personal comment", document["content"])
        self.assertIn("desktop.keep_me", {item["key"] for item in document["entries"]})
        self.assertIn(
            "model_providers.relay.request_max_retries",
            {item["key"] for item in document["entries"]},
        )
        self.assertEqual(document["common"]["modelContextWindow"], 400000)
        self.assertEqual(document["common"]["modelId"], "gpt-test-old")
        # The selected source is a custom Provider with no context metadata;
        # an official same-named cache entry must not supply invented limits.
        self.assertEqual(document["common"]["modelContextDefault"], 0)
        self.assertEqual(document["common"]["modelContextMax"], 0)
        self.assertEqual(document["common"]["modelContextEffectivePercent"], 0)
        self.assertEqual(document["common"]["autoCompactTokenLimit"], 300000)
        self.assertEqual(document["common"]["autoCompactScope"], "body_after_prefix")
        self.assertEqual(document["common"]["mcpOptionalStartupGraceMs"], 1500)
        self.assertEqual(document["common"]["requestMaxRetries"], 7)
        self.assertEqual(document["common"]["webSearch"], "live")
        self.assertEqual(document["common"]["webSearchContextSize"], "medium")
        self.assertEqual(document["common"]["personality"], "friendly")
        self.assertEqual(document["common"]["serviceTier"], "fast")
        self.assertFalse(document["common"]["checkForUpdates"])
        self.assertFalse(document["common"]["tuiAnimations"])
        self.assertTrue(document["common"]["vpnCompatibility"])
        self.assertTrue(document["common"]["preventIdleSleep"])
        self.assertEqual(document["common"]["providerId"], "relay")
        self.assertRegex(document["fingerprint"], r"^[0-9a-f]{64}$")

    def test_codex_config_visual_save_preserves_comments_and_unknown_fields(self):
        core.CONFIG_FILE.write_text(
            '# keep this comment\n'
            'model_context_window = 128000\n'
            '\n[desktop]\n'
            'keep_me = true\n',
            encoding="utf-8",
        )

        fingerprint = core.codex_config_document()["fingerprint"]
        result = core.save_codex_config_document(
            {
                "expectedFingerprint": fingerprint,
                "updates": [
                    {"path": ["model_context_window"], "value": 256000},
                    {"path": ["desktop", "keep_me"], "value": False},
                ]
            }
        )

        rendered = core.CONFIG_FILE.read_text(encoding="utf-8")
        parsed = tomllib.loads(rendered)
        self.assertTrue(result["changed"])
        self.assertTrue(Path(result["backupPath"]).is_file())
        self.assertIn("# keep this comment", rendered)
        self.assertEqual(parsed["model_context_window"], 256000)
        self.assertFalse(parsed["desktop"]["keep_me"])

    def test_codex_config_save_rejects_stale_editor_fingerprint(self):
        document = core.codex_config_document()
        external = 'model = "external-edit"\n'
        core.CONFIG_FILE.write_text(external, encoding="utf-8")

        with self.assertRaisesRegex(core.ManagerError, "已被其他程序或窗口修改"):
            core.save_codex_config_document(
                {
                    "expectedFingerprint": document["fingerprint"],
                    "content": 'model = "stale-editor"\n',
                }
            )

        self.assertEqual(core.CONFIG_FILE.read_text(encoding="utf-8"), external)

    def test_codex_config_visual_save_supports_complex_arrays_and_dates(self):
        core.CONFIG_FILE.write_text(
            'cutoff = 2026-08-20T10:00:00Z\n'
            'skill_overrides = [{ path = "one", enabled = true }]\n',
            encoding="utf-8",
        )
        document = core.codex_config_document()
        entries = {item["key"]: item for item in document["entries"]}
        self.assertTrue(entries["cutoff"]["editable"])
        self.assertTrue(entries["skill_overrides"]["editable"])

        core.save_codex_config_document(
            {
                "updates": [
                    {"path": ["cutoff"], "value": "2026-08-21T11:30:00+00:00"},
                    {
                        "path": ["skill_overrides"],
                        "value": [
                            {"path": "one", "enabled": False},
                            {"path": "two", "enabled": True},
                        ],
                    },
                ]
            }
        )
        parsed = tomllib.loads(core.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(parsed["cutoff"].isoformat(), "2026-08-21T11:30:00+00:00")
        self.assertEqual(parsed["skill_overrides"][1]["path"], "two")
        self.assertFalse(parsed["skill_overrides"][0]["enabled"])

    def test_codex_config_rejects_invalid_raw_toml_without_writing(self):
        before = core.CONFIG_FILE.read_bytes()

        with self.assertRaisesRegex(core.ManagerError, "TOML 格式无效"):
            core.save_codex_config_document({"content": 'model = "unterminated\n'})

        self.assertEqual(core.CONFIG_FILE.read_bytes(), before)

    def test_planning_preset_uses_managed_agents_instructions_not_unknown_config_key(self):
        settings = core._initial_settings()
        settings["runtimeTuning"].update(
            {"planningManaged": True, "planningMode": "clarify"}
        )

        block = core.build_routing_block(settings)
        config = core.build_codex_config(settings)

        self.assertIn("### Planning interaction", block)
        self.assertIn("Ask the user to choose", block)
        self.assertNotIn("collaboration_modes", config)

    def test_subagent_safety_removes_legacy_manager_fixed_limits(self):
        managed_agent = core.AGENTS_DIR / "cam-simple-1.toml"
        managed_agent.parent.mkdir(parents=True, exist_ok=True)
        managed_agent.write_text(
            core.render_agent_toml(
                "cam_simple_1",
                "managed",
                "cam-agent-simple-1-old-deadbeef",
                "low",
                "You are the simple difficulty worker in an ordered fallback route.",
                core.AGGREGATE_PROVIDER_ID,
                None,
            ),
            encoding="utf-8",
        )
        core.CONFIG_FILE.write_text(
            '[agents]\n'
            'max_concurrent_threads_per_session = 3\n'
            'max_depth = 1\n'
            '\n[agents.cam_simple_1]\n'
            f'config_file = "{managed_agent.as_posix()}"\n'
            'description = "managed"\n'
            '\n[features.multi_agent_v2]\n'
            'enabled = true\n'
            'max_concurrent_threads_per_session = 4\n',
            encoding="utf-8",
        )
        parsed = tomllib.loads(core.build_codex_config(core._initial_settings()))
        self.assertNotIn("max_concurrent_threads_per_session", parsed.get("agents", {}))
        self.assertNotIn("max_depth", parsed.get("agents", {}))
        self.assertTrue(parsed["features"]["multi_agent_v2"]["enabled"])
        self.assertNotIn(
            "max_concurrent_threads_per_session",
            parsed["features"]["multi_agent_v2"],
        )

    def test_subagent_safety_preserves_user_limits_that_match_legacy_numbers(self):
        core.CONFIG_FILE.write_text(
            '[agents]\n'
            'max_concurrent_threads_per_session = 3\n'
            'max_depth = 1\n'
            '\n[features.multi_agent_v2]\n'
            'enabled = true\n'
            'max_concurrent_threads_per_session = 4\n',
            encoding="utf-8",
        )

        parsed = tomllib.loads(core.build_codex_config(core._initial_settings()))

        self.assertEqual(parsed["agents"]["max_concurrent_threads_per_session"], 3)
        self.assertEqual(parsed["agents"]["max_depth"], 1)
        self.assertEqual(
            parsed["features"]["multi_agent_v2"]["max_concurrent_threads_per_session"],
            4,
        )

    def test_apply_configuration_removes_only_obsolete_manager_agent_files(self):
        core.ensure_state()
        stale = core.AGENTS_DIR / "cam-simple-3.toml"
        user_owned = core.AGENTS_DIR / "cam-normal-3.toml"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(
            'name = "cam_simple_3"\nmodel = "cam-agent-simple-3-old-deadbeef"\n'
            'model_provider = "cam_aggregate"\n',
            encoding="utf-8",
        )
        user_owned.write_text('name = "my_agent"\nmodel = "gpt-user"\n', encoding="utf-8")
        settings = core.load_settings()
        settings["subagentRouting"]["strategyId"] = "verification_first"
        core.save_settings(settings)

        with patch.object(core, "service_secret_configured", return_value=True):
            result = core.apply_configuration(False)

        self.assertFalse(stale.exists())
        self.assertTrue(user_owned.exists())
        self.assertIn(str(stale), result["removedManagedAgents"])
        self.assertNotIn(str(user_owned), result["removedManagedAgents"])

    def test_subagent_route_rejects_effort_not_supported_by_selected_model(self):
        core.ensure_state()
        provider = core.save_provider(
            {
                "id": "reasoning_guard",
                "name": "Reasoning Guard",
                "baseUrl": "https://example.invalid/v1",
                "envKey": "REASONING_GUARD_KEY",
            }
        )
        settings = core.load_settings()
        target = next(item for item in settings["providers"] if item["id"] == provider["id"])
        target["models"] = ["gpt-test-old"]
        target["modelCapabilities"] = {"gpt-test-old": {
            "reasoningKnown": True, "reasoningSupported": True,
            "efforts": ["low", "medium"], "defaultEffort": "medium",
        }}
        core.save_settings(settings)
        key = f"provider:{provider['id']}::gpt-test-old"
        routes = {level: {"models": [], "efforts": []} for level in core.DIFFICULTIES}
        routes["simple"] = {"models": [key], "efforts": ["high"]}
        with self.assertRaisesRegex(core.ManagerError, "不支持思考程度"):
            core.save_subagent_routing(
                {
                    "advanced": True,
                    "strategyId": "adaptive",
                    "prompt": "Actually invoke the configured route.",
                    "routes": routes,
                }
            )

        routes["simple"]["efforts"] = ["medium"]
        saved = core.save_subagent_routing(
            {
                "advanced": True,
                "strategyId": "adaptive",
                "prompt": "Actually invoke the configured route.",
                "routes": routes,
            }
        )
        self.assertEqual(saved["routes"]["simple"]["efforts"], ["medium"])

    def test_validation_reports_structural_success(self):
        core.ensure_state()
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Codex Doctor: 12 ok | 0 warn | 0 fail ok\n", stderr=""
        )
        with patch.object(core, "run_codex_capture", return_value=completed):
            result = core.validate_configuration(run_doctor=True)
        self.assertTrue(result["valid"])
        self.assertIn("0 fail", result["doctor"])

    def test_run_codex_capture_drops_inherited_dumb_terminal_marker(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured.update(kwargs)
            return subprocess.CompletedProcess(command, 0, "ok", "")

        with (
            patch.object(core, "codex_prefix", return_value=["codex.exe"]),
            patch.object(core.subprocess, "run", side_effect=fake_run),
            patch.dict(core.os.environ, {"TERM": "dumb", "KEEP_ME": "yes"}, clear=True),
        ):
            result = core.run_codex_capture(["doctor"])
            self.assertEqual(core.os.environ["TERM"], "dumb")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(captured["command"], ["codex.exe", "doctor"])
        self.assertNotIn("TERM", captured["env"])
        self.assertEqual(captured["env"]["KEEP_ME"], "yes")

    def test_validation_reports_the_failed_doctor_check(self):
        core.ensure_state()
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=json.dumps(
                {
                    "checks": {
                        "terminal.env": {
                            "id": "terminal.env",
                            "category": "terminal",
                            "status": "fail",
                            "summary": "TERM=dumb - colors are disabled",
                        }
                    }
                }
            ),
            stderr="",
        )
        with patch.object(core, "run_codex_capture", return_value=completed):
            result = core.validate_configuration(run_doctor=True)

        self.assertTrue(result["valid"])
        self.assertEqual(result["errors"], [])
        self.assertTrue(any("TERM=dumb" in item for item in result["warnings"]))

    def test_validation_keeps_blocking_doctor_categories_as_errors(self):
        core.ensure_state()
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=json.dumps(
                {
                    "checks": {
                        "config.load": {
                            "id": "config.load",
                            "category": "config",
                            "status": "fail",
                            "summary": "config could not be loaded",
                        }
                    }
                }
            ),
            stderr="",
        )
        with patch.object(core, "run_codex_capture", return_value=completed):
            result = core.validate_configuration(run_doctor=True)

        self.assertFalse(result["valid"])
        self.assertTrue(any("config.load" in item for item in result["errors"]))

    def test_maintenance_ui_uses_fresh_diagnostics_without_double_counting(self):
        source = (Path(__file__).resolve().parent / "gui" / "src" / "App.jsx").read_text(
            encoding="utf-8"
        )
        self.assertIn("generatedConfigurationCheck", source)
        self.assertIn("发现 ${issues.length} 个配置或运行问题", source)
        self.assertNotIn("issues.length + (configurationHealthy", source)
        self.assertIn("需按提示处理", source)
        self.assertIn("expectedFingerprint", source)
        self.assertIn("长任务与扩展启动", source)

    def test_missing_synced_key_fails_before_writing_config(self):
        core.ensure_state()
        core.save_provider(
            {
                "id": "missing_key",
                "name": "Missing Key",
                "baseUrl": "https://missing.invalid/v1",
                "envKey": "MISSING_KEY_API_KEY",
            }
        )
        core.save_main_profile(
            {"id": "remote", "name": "Remote", "provider": "missing_key", "model": "remote-model", "effort": "high"}
        )
        core.set_active_main("remote")
        before = core.CONFIG_FILE.read_text(encoding="utf-8")
        with self.assertRaises(core.ManagerError):
            core.apply_configuration(sync_secrets=True)
        self.assertEqual(core.CONFIG_FILE.read_text(encoding="utf-8"), before)

    def test_schema_two_migrates_without_losing_existing_settings(self):
        settings = core._initial_settings()
        settings["schemaVersion"] = 2
        settings.pop("accounts")
        settings.pop("historySync")
        adaptive = next(item for item in settings["strategies"] if item["id"] == "adaptive")
        adaptive["instructions"] = core.LEGACY_ADAPTIVE_INSTRUCTIONS
        core.atomic_write_json(core.SETTINGS_FILE, settings)

        migrated = core.load_settings()

        self.assertEqual(migrated["schemaVersion"], core.SCHEMA_VERSION)
        self.assertEqual(migrated["accounts"], [])
        self.assertEqual(migrated["historySync"]["recentDays"], 30)
        self.assertEqual([item["id"] for item in migrated["accountGroups"][:3]], ["official", "free", "relay"])
        self.assertEqual(migrated["accountGroups"][1]["name"], "日抛账号")
        self.assertFalse(migrated["web2api"]["enabled"])
        self.assertFalse(migrated["web2api"]["activeForCodex"])
        self.assertTrue(migrated["sessionSync"]["enabled"])
        self.assertEqual(migrated["appBehavior"]["quotaRefreshMinutes"], 10)
        self.assertEqual(
            next(item for item in migrated["strategies"] if item["id"] == "adaptive")["instructions"],
            core.OPTIMAL_ADAPTIVE_INSTRUCTIONS,
        )
        self.assertTrue(any(core.BACKUPS_DIR.glob("settings.json.*.bak")))

    def test_schema_one_and_numeric_string_versions_migrate_without_startup_failure(self):
        settings = core._initial_settings()
        settings["schemaVersion"] = "1"
        settings.pop("runtimeTuning", None)
        core.atomic_write_json(core.SETTINGS_FILE, settings)

        migrated = core.load_settings()

        self.assertEqual(migrated["schemaVersion"], core.SCHEMA_VERSION)
        self.assertIn("runtimeTuning", migrated)
        self.assertTrue(any(core.BACKUPS_DIR.glob("settings.json.*.bak")))

    def test_missing_schema_on_empty_settings_is_recovered_as_legacy_document(self):
        core.atomic_write_json(core.SETTINGS_FILE, {})

        migrated = core.load_settings()

        self.assertEqual(migrated["schemaVersion"], core.SCHEMA_VERSION)
        self.assertEqual(migrated["accounts"], [])

    def test_legacy_builtin_free_group_is_renamed_without_touching_custom_groups(self):
        settings = core._initial_settings()
        next(item for item in settings["accountGroups"] if item["id"] == "free")["name"] = "白嫖账号"
        settings["accountGroups"].append(
            {"id": "custom-free", "name": "白嫖账号", "color": "violet", "sortOrder": 9, "system": False}
        )
        core.atomic_write_json(core.SETTINGS_FILE, settings)

        migrated = core.load_settings()

        self.assertEqual(next(item for item in migrated["accountGroups"] if item["id"] == "free")["name"], "日抛账号")
        self.assertEqual(next(item for item in migrated["accountGroups"] if item["id"] == "custom-free")["name"], "白嫖账号")

    def test_schema_six_migrates_primary_window_to_weekly_and_marks_metadata_stale(self):
        settings = core._initial_settings()
        settings["schemaVersion"] = 6
        settings["accounts"] = [
            {
                "id": "legacy-weekly",
                "label": "Legacy",
                "authMode": "chatgpt",
                "sourceType": "codex_auth",
                "groupId": "official",
                "createdAt": "2026-08-01T08:30:00+08:00",
                "lastRefreshedAt": "2026-08-09T08:30:00+08:00",
                "refreshState": "ready",
                "usage": {
                    "hourly": {"remainingPercent": 72, "windowMinutes": 10_080},
                    "weekly": None,
                    "hourlyUnlimited": False,
                },
            }
        ]
        core.atomic_write_json(core.SETTINGS_FILE, settings)

        migrated = core.load_settings()
        account = migrated["accounts"][0]
        self.assertEqual(migrated["schemaVersion"], core.SCHEMA_VERSION)
        self.assertEqual(account["usage"]["weekly"]["windowMinutes"], 10_080)
        self.assertNotIn("hourly", account["usage"])
        self.assertNotIn("hourlyUnlimited", account["usage"])
        self.assertIsNone(account["usage"]["resetCredits"])
        self.assertEqual(account["importedAt"], account["createdAt"])
        self.assertIsNone(account["lastRefreshedAt"])
        self.assertEqual(account["refreshState"], "pending")

    def test_schema_eight_migrates_legacy_routes_with_difficulty_efforts(self):
        settings = core._initial_settings()
        settings["schemaVersion"] = 8
        for level in core.DIFFICULTIES:
            settings["subagentRouting"]["routes"][level] = {
                "models": [f"provider:test::{level}-a", f"provider:test::{level}-b"]
            }
        core.atomic_write_json(core.SETTINGS_FILE, settings)

        migrated = core.load_settings()

        self.assertEqual(migrated["schemaVersion"], core.SCHEMA_VERSION)
        for level in core.DIFFICULTIES:
            self.assertEqual(
                migrated["subagentRouting"]["routes"][level]["efforts"],
                [core.DEFAULT_EFFORT_BY_DIFFICULTY[level]] * 2,
            )

    def test_app_behavior_saves_quota_interval_without_overwriting_tray_setting(self):
        core.ensure_state()
        settings = core.load_settings()
        settings["appBehavior"] = {"closeToTray": True, "quotaRefreshMinutes": 5}
        core.save_settings(settings)

        behavior = core.save_app_behavior({"quotaRefreshMinutes": 10})

        self.assertTrue(behavior["closeToTray"])
        self.assertEqual(behavior["quotaRefreshMinutes"], 10)
        with self.assertRaises(core.ManagerError):
            core.save_app_behavior({"quotaRefreshMinutes": 3})

    def test_quota_refresh_backs_error_accounts_off_for_ten_minutes(self):
        refreshed_at = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()

        self.assertFalse(
            core._refresh_is_stale(
                {"lastRefreshedAt": refreshed_at, "refreshState": "ready"},
                300,
            )
        )
        self.assertFalse(
            core._refresh_is_stale(
                {"lastRefreshedAt": refreshed_at, "refreshState": "error"},
                300,
            )
        )
        self.assertTrue(
            core._refresh_is_stale(
                {
                    "lastRefreshedAt": (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(),
                    "refreshState": "error",
                },
                300,
            )
        )

    def test_account_switch_is_encrypted_transactional_and_not_exported(self):
        auth_a = json.dumps({"OPENAI_API_KEY": "account-key-a"}).encode()
        auth_b = json.dumps({"OPENAI_API_KEY": "account-key-b"}).encode()
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "running_codex_processes", return_value=[]),
            patch.object(core.time, "sleep", return_value=None),
        ):
            (self.root / "auth.json").write_bytes(auth_a)
            account_a = core.save_codex_account({"label": "Account A"})
            (self.root / "auth.json").write_bytes(auth_b)
            account_b = core.save_codex_account({"label": "Account B"})

            switched = core.switch_codex_account(account_a["id"])
            projected_a = core._codex_auth_projection_bytes(auth_a)
            self.assertTrue(switched["changed"])
            self.assertEqual((self.root / "auth.json").read_bytes(), projected_a)
            self.assertTrue(Path(switched["backupPath"]).is_dir())
            self.assertNotEqual(account_a["id"], account_b["id"])

            with patch.object(core, "_live_auth_files_match", return_value=False):
                with self.assertRaisesRegex(core.ManagerError, "已自动回滚"):
                    core.switch_codex_account(account_b["id"])
            self.assertEqual((self.root / "auth.json").read_bytes(), projected_a)

            bundle = core.export_bundle()
            encoded = json.dumps(bundle)
            self.assertEqual(bundle["settings"]["accounts"], [])
            self.assertNotIn("account-key-a", encoded)
            self.assertNotIn("Account A", encoded)

    def test_switch_rewrites_legacy_cockpit_oauth_shape_to_official_codex_schema(self):
        access_token = self.jwt(
            {
                "client_id": core.CODEX_OAUTH_CLIENT_ID,
                "exp": 2_000_000_000,
                "https://api.openai.com/auth": {"chatgpt_account_id": "cockpit-workspace"},
            }
        )
        id_token = self.jwt(
            {
                "aud": [core.CODEX_OAUTH_CLIENT_ID],
                "exp": 2_000_000_000,
                "email": "cockpit@example.test",
                "https://api.openai.com/auth": {"chatgpt_account_id": "cockpit-workspace"},
            }
        )
        legacy = json.dumps(
            {
                "auth_mode": "chatgptAuthTokens",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": id_token,
                    "access_token": access_token,
                    "refresh_token": "refresh-token",
                    "account_id": "cockpit-workspace",
                },
                "session_meta": {"importFormat": "CPA / Cockpit"},
                "last_refresh": "2026-08-01T00:00:00Z",
            }
        ).encode()
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "running_codex_processes", return_value=[]),
            patch.object(core, "local_model_catalog", return_value=[{"id": "gpt-test"}]),
        ):
            (self.root / "auth.json").write_bytes(legacy)
            # This test verifies auth projection, not whether the synthetic
            # test token is accepted by the live ChatGPT endpoints.
            account = core.save_codex_account({"label": "Cockpit", "deferRefresh": True})
            legacy_state = core.current_auth_state(force=True)
            legacy_source = next(
                item
                for item in core.model_sources()
                if item["recordId"] == account["id"]
            )
            switched = core.switch_codex_account(account["id"])
            canonical_state = core.current_auth_state(force=True)

        self.assertTrue(switched["changed"])
        self.assertTrue(legacy_state["requiresReapply"])
        self.assertFalse(legacy_state["authContractValid"])
        self.assertTrue(legacy_source["activationRequired"])
        self.assertFalse(canonical_state["requiresReapply"])
        self.assertTrue(canonical_state["authContractValid"])
        projected = json.loads((self.root / "auth.json").read_text(encoding="utf-8"))
        self.assertNotIn("auth_mode", projected)
        self.assertNotIn("session_meta", projected)
        self.assertEqual(projected["tokens"]["access_token"], access_token)
        self.assertEqual(projected["tokens"]["id_token"], id_token)

    def test_missing_auth_mode_is_valid_when_chatgpt_tokens_are_unambiguous(self):
        access_token = self.jwt(
            {
                "client_id": core.CODEX_OAUTH_CLIENT_ID,
                "exp": 2_000_000_000,
                "https://api.openai.com/auth": {"chatgpt_account_id": "workspace-no-mode"},
            }
        )
        id_token = self.jwt(
            {
                "aud": [core.CODEX_OAUTH_CLIENT_ID],
                "exp": 2_000_000_000,
                "email": "no-mode@example.test",
                "https://api.openai.com/auth": {"chatgpt_account_id": "workspace-no-mode"},
            }
        )
        identity = core._identity_from_auth_bytes(
            json.dumps(
                {
                    "OPENAI_API_KEY": None,
                    "tokens": {
                        "id_token": id_token,
                        "access_token": access_token,
                        "refresh_token": "refresh-token",
                        "account_id": "workspace-no-mode",
                    },
                }
            ).encode()
        )
        self.assertEqual(identity["authMode"], "chatgpt")
        self.assertEqual(identity["declaredAuthMode"], "")
        self.assertTrue(identity["authContractValid"])

    def test_official_oauth_refresh_rotates_active_snapshot_and_live_auth(self):
        now = datetime.now(timezone.utc)
        account_id = "oauth-active-workspace"
        old_access = self.jwt(
            {
                "client_id": core.CODEX_OAUTH_CLIENT_ID,
                "exp": int((now + timedelta(seconds=30)).timestamp()),
                "sub": "oauth-active-user",
                "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
            }
        )
        new_access = self.jwt(
            {
                "client_id": core.CODEX_OAUTH_CLIENT_ID,
                "exp": int((now + timedelta(hours=2)).timestamp()),
                "sub": "oauth-active-user",
                "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
            }
        )
        id_token = self.jwt(
            {
                "aud": [core.CODEX_OAUTH_CLIENT_ID],
                "exp": int((now + timedelta(hours=2)).timestamp()),
                "sub": "oauth-active-user",
                "email": "active-oauth@example.test",
                "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
            }
        )
        old_refresh = "active-old-refresh"
        rotated_refresh = "active-rotated-refresh"
        auth = json.dumps(
            {
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": id_token,
                    "access_token": old_access,
                    "refresh_token": old_refresh,
                    "account_id": account_id,
                },
                "last_refresh": (now - timedelta(days=1)).isoformat(),
            }
        ).encode()
        requests = []

        class Response:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps(
                    {"access_token": new_access, "refresh_token": rotated_refresh}
                ).encode()

        def open_request(request, *, timeout):
            requests.append((request, timeout))
            return Response()

        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "_codex_client_version", return_value="0.147.0"),
            patch.object(core, "_open_same_origin_request", side_effect=open_request),
        ):
            account = core.save_codex_account(
                {"label": "Active OAuth", "sourceType": "codex_auth", "deferRefresh": True},
                auth_bytes=auth,
            )
            (self.root / "auth.json").write_bytes(core._codex_auth_projection_bytes(auth))
            credentials = core._account_chatgpt_credentials(account["id"])
            stored = json.loads(
                (core._decode_snapshot_files(core._load_account_snapshot(account["id"]))["auth.json"] or b"{}").decode()
            )

        self.assertEqual(credentials["accessToken"], new_access)
        self.assertEqual(len(requests), 1)
        request, timeout = requests[0]
        self.assertEqual(request.full_url, core.CODEX_OAUTH_TOKEN_URL)
        self.assertEqual(timeout, core.CHATGPT_REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(
            json.loads(request.data.decode()),
            {
                "client_id": core.CODEX_OAUTH_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": old_refresh,
            },
        )
        self.assertEqual(stored["tokens"]["access_token"], new_access)
        self.assertEqual(stored["tokens"]["refresh_token"], rotated_refresh)
        live = json.loads((self.root / "auth.json").read_text(encoding="utf-8"))
        self.assertEqual(live["tokens"]["access_token"], new_access)
        self.assertEqual(live["tokens"]["refresh_token"], rotated_refresh)
        self.assertTrue(stored.get("last_refresh"))

    def test_active_codex_rotation_is_synced_without_redeeming_old_refresh_token(self):
        now = datetime.now(timezone.utc)
        workspace_id = "oauth-live-rotation-workspace"
        old_access = self.jwt(
            {
                "client_id": core.CODEX_OAUTH_CLIENT_ID,
                "exp": int((now - timedelta(minutes=1)).timestamp()),
                "sub": "oauth-live-rotation-user",
                "https://api.openai.com/auth": {"chatgpt_account_id": workspace_id},
            }
        )
        live_access = self.jwt(
            {
                "client_id": core.CODEX_OAUTH_CLIENT_ID,
                "exp": int((now + timedelta(hours=2)).timestamp()),
                "sub": "oauth-live-rotation-user",
                "https://api.openai.com/auth": {"chatgpt_account_id": workspace_id},
            }
        )
        id_token = self.jwt(
            {
                "aud": [core.CODEX_OAUTH_CLIENT_ID],
                "sub": "oauth-live-rotation-user",
                "email": "live-rotation@example.test",
                "https://api.openai.com/auth": {"chatgpt_account_id": workspace_id},
            }
        )
        auth = json.dumps(
            {
                "tokens": {
                    "id_token": id_token,
                    "access_token": old_access,
                    "refresh_token": "spent-refresh-token",
                    "account_id": workspace_id,
                },
                "last_refresh": (now - timedelta(days=1)).isoformat(),
            }
        ).encode()
        live_auth = json.dumps(
            {
                "tokens": {
                    "id_token": id_token,
                    "access_token": live_access,
                    "refresh_token": "codex-rotated-refresh",
                    "account_id": workspace_id,
                },
                "last_refresh": now.isoformat(),
            }
        ).encode()
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "_request_codex_oauth_refresh", side_effect=AssertionError("must use live rotation")),
        ):
            account = core.save_codex_account(
                {"label": "Live Rotation", "sourceType": "codex_auth", "deferRefresh": True},
                auth_bytes=auth,
            )
            (self.root / "auth.json").write_bytes(core._codex_auth_projection_bytes(live_auth))
            credentials = core._account_chatgpt_credentials(account["id"])
            stored = json.loads(
                (core._decode_snapshot_files(core._load_account_snapshot(account["id"]))["auth.json"] or b"{}").decode()
            )

        self.assertEqual(credentials["accessToken"], live_access)
        self.assertEqual(stored["tokens"]["refresh_token"], "codex-rotated-refresh")
        self.assertEqual(stored["tokens"]["access_token"], live_access)

    def test_official_oauth_refresh_keeps_old_refresh_token_when_not_rotated(self):
        now = datetime.now(timezone.utc)
        workspace_id = "oauth-retain-workspace"
        old_access = self.jwt(
            {
                "client_id": core.CODEX_OAUTH_CLIENT_ID,
                "exp": int((now - timedelta(minutes=1)).timestamp()),
                "sub": "oauth-retain-user",
                "https://api.openai.com/auth": {"chatgpt_account_id": workspace_id},
            }
        )
        new_access = self.jwt(
            {
                "client_id": core.CODEX_OAUTH_CLIENT_ID,
                "exp": int((now + timedelta(hours=1)).timestamp()),
                "sub": "oauth-retain-user",
                "https://api.openai.com/auth": {"chatgpt_account_id": workspace_id},
            }
        )
        id_token = self.jwt(
            {
                "aud": [core.CODEX_OAUTH_CLIENT_ID],
                "sub": "oauth-retain-user",
                "email": "retain-oauth@example.test",
                "https://api.openai.com/auth": {"chatgpt_account_id": workspace_id},
            }
        )
        old_refresh = "retain-old-refresh"
        auth = json.dumps(
            {
                "tokens": {
                    "id_token": id_token,
                    "access_token": old_access,
                    "refresh_token": old_refresh,
                    "account_id": workspace_id,
                },
                "last_refresh": (now - timedelta(days=9)).isoformat(),
            }
        ).encode()

        class Response:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps({"access_token": new_access}).encode()

        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "_codex_client_version", return_value="0.147.0"),
            patch.object(core, "_open_same_origin_request", return_value=Response()),
        ):
            account = core.save_codex_account(
                {"label": "Retain OAuth", "sourceType": "codex_auth", "deferRefresh": True},
                auth_bytes=auth,
            )
            core._account_chatgpt_credentials(account["id"])
            stored = json.loads(
                (core._decode_snapshot_files(core._load_account_snapshot(account["id"]))["auth.json"] or b"{}").decode()
            )

        self.assertEqual(stored["tokens"]["access_token"], new_access)
        self.assertEqual(stored["tokens"]["refresh_token"], old_refresh)

    def test_official_oauth_refresh_rolls_back_live_auth_when_snapshot_write_fails(self):
        now = datetime.now(timezone.utc)
        workspace_id = "oauth-rollback-workspace"
        old_access = self.jwt(
            {
                "client_id": core.CODEX_OAUTH_CLIENT_ID,
                "exp": int((now - timedelta(minutes=1)).timestamp()),
                "sub": "oauth-rollback-user",
                "https://api.openai.com/auth": {"chatgpt_account_id": workspace_id},
            }
        )
        new_access = self.jwt(
            {
                "client_id": core.CODEX_OAUTH_CLIENT_ID,
                "exp": int((now + timedelta(hours=1)).timestamp()),
                "sub": "oauth-rollback-user",
                "https://api.openai.com/auth": {"chatgpt_account_id": workspace_id},
            }
        )
        id_token = self.jwt(
            {
                "aud": [core.CODEX_OAUTH_CLIENT_ID],
                "sub": "oauth-rollback-user",
                "email": "rollback-oauth@example.test",
                "https://api.openai.com/auth": {"chatgpt_account_id": workspace_id},
            }
        )
        auth = json.dumps(
            {
                "tokens": {
                    "id_token": id_token,
                    "access_token": old_access,
                    "refresh_token": "rollback-old-refresh",
                    "account_id": workspace_id,
                }
            }
        ).encode()

        class Response:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps(
                    {"access_token": new_access, "refresh_token": "rollback-new-refresh"}
                ).encode()

        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "_codex_client_version", return_value="0.147.0"),
            patch.object(core, "_open_same_origin_request", return_value=Response()),
        ):
            account = core.save_codex_account(
                {"label": "Rollback OAuth", "sourceType": "codex_auth", "deferRefresh": True},
                auth_bytes=auth,
            )
            live_before = core._codex_auth_projection_bytes(auth)
            (self.root / "auth.json").write_bytes(live_before)
            secrets_before = core.SECRETS_FILE.read_bytes()
            real_atomic_write_json = core.atomic_write_json

            def fail_secret_write(path, payload):
                if Path(path) == core.SECRETS_FILE:
                    raise OSError("simulated snapshot write failure")
                return real_atomic_write_json(path, payload)

            with patch.object(core, "atomic_write_json", side_effect=fail_secret_write):
                with self.assertRaisesRegex(OSError, "simulated snapshot write failure"):
                    core._account_chatgpt_credentials(account["id"])

        self.assertEqual((self.root / "auth.json").read_bytes(), live_before)
        self.assertEqual(core.SECRETS_FILE.read_bytes(), secrets_before)

    def test_official_oauth_refresh_is_single_flight_per_account(self):
        now = datetime.now(timezone.utc)
        workspace_id = "oauth-single-flight-workspace"
        old_access = self.jwt(
            {
                "client_id": core.CODEX_OAUTH_CLIENT_ID,
                "exp": int((now - timedelta(minutes=1)).timestamp()),
                "sub": "oauth-single-flight-user",
                "https://api.openai.com/auth": {"chatgpt_account_id": workspace_id},
            }
        )
        new_access = self.jwt(
            {
                "client_id": core.CODEX_OAUTH_CLIENT_ID,
                "exp": int((now + timedelta(hours=1)).timestamp()),
                "sub": "oauth-single-flight-user",
                "https://api.openai.com/auth": {"chatgpt_account_id": workspace_id},
            }
        )
        id_token = self.jwt(
            {
                "aud": [core.CODEX_OAUTH_CLIENT_ID],
                "sub": "oauth-single-flight-user",
                "email": "single-flight@example.test",
                "https://api.openai.com/auth": {"chatgpt_account_id": workspace_id},
            }
        )
        auth = json.dumps(
            {
                "tokens": {
                    "id_token": id_token,
                    "access_token": old_access,
                    "refresh_token": "single-flight-refresh",
                    "account_id": workspace_id,
                }
            }
        ).encode()
        calls = []

        class Response:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps({"access_token": new_access}).encode()

        def open_request(*_args, **_kwargs):
            calls.append(time.monotonic())
            time.sleep(0.05)
            return Response()

        results = []
        errors = []
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "_codex_client_version", return_value="0.147.0"),
            patch.object(core, "_open_same_origin_request", side_effect=open_request),
        ):
            account = core.save_codex_account(
                {"label": "Single Flight", "sourceType": "codex_auth", "deferRefresh": True},
                auth_bytes=auth,
            )

            def refresh():
                try:
                    results.append(core._account_chatgpt_credentials(account["id"])["accessToken"])
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=refresh) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

        self.assertFalse(errors)
        self.assertEqual(results, [new_access, new_access])
        self.assertEqual(len(calls), 1)

    def test_codex_auth_projection_rejects_ambiguous_unknown_and_web_session_credentials(self):
        access_token = self.jwt(
            {
                "client_id": core.CODEX_OAUTH_CLIENT_ID,
                "https://api.openai.com/auth": {"chatgpt_account_id": "projection-account"},
            }
        )
        id_token = self.jwt(
            {
                "aud": [core.CODEX_OAUTH_CLIENT_ID],
                "https://api.openai.com/auth": {"chatgpt_account_id": "projection-account"},
            }
        )
        tokens = {
            "id_token": id_token,
            "access_token": access_token,
            "refresh_token": "projection-refresh",
            "account_id": "projection-account",
        }
        with self.assertRaisesRegex(core.ManagerError, "混合了多种凭据"):
            core._codex_auth_projection_bytes(
                json.dumps({"OPENAI_API_KEY": "sk-mixed", "tokens": tokens}).encode()
            )
        with self.assertRaisesRegex(core.ManagerError, "auth_mode"):
            core._codex_auth_projection_bytes(
                json.dumps({"auth_mode": "future-magic", "tokens": tokens}).encode()
            )
        synthetic = core._synthetic_web_session_id_token(
            "web@example.test", "web-account", "free", "web-user", None
        )
        with self.assertRaisesRegex(core.ManagerError, "不是可持久使用"):
            core._codex_auth_projection_bytes(
                json.dumps(
                    {
                        "tokens": {
                            **tokens,
                            "id_token": synthetic,
                            "refresh_token": "",
                        }
                    }
                ).encode()
            )

    def test_native_oauth_projection_preserves_optional_mode_and_agent_identity_cache(self):
        account_id = "native-oauth-account"
        access_token = self.jwt(
            {
                "client_id": core.CODEX_OAUTH_CLIENT_ID,
                "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
            }
        )
        id_token = self.jwt(
            {
                "aud": [core.CODEX_OAUTH_CLIENT_ID],
                "email": "native@example.test",
                "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
            }
        )
        agent_identity = {
            "agent_runtime_id": "runtime-native-cache",
            "agent_private_key": self.agent_private_key(8),
            "account_id": account_id,
            "chatgpt_user_id": "native-user",
            "email": "native@example.test",
            "plan_type": "plus",
        }
        encoded = json.dumps(
            {
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": id_token,
                    "access_token": access_token,
                    "refresh_token": "native-refresh-token",
                    "account_id": account_id,
                },
                "last_refresh": "2026-09-01T00:00:00Z",
                "agent_identity": agent_identity,
                "manager_metadata": {"must": "not leak"},
            }
        ).encode()

        projected = json.loads(core._codex_auth_projection_bytes(encoded))

        self.assertTrue(core._auth_bytes_support_codex(encoded))
        self.assertNotIn("auth_mode", projected)
        self.assertNotIn("manager_metadata", projected)
        self.assertEqual(projected["agent_identity"]["agent_runtime_id"], "runtime-native-cache")
        self.assertEqual(projected["tokens"]["account_id"], account_id)

        legacy_base = dict(json.loads(encoded), auth_mode="chatgpt")
        legacy_base.pop("agent_identity")
        merged = json.loads(
            core._merge_codex_oauth_auth_bytes(
                json.dumps(legacy_base).encode(),
                newer_auth_bytes=encoded,
            )
        )
        self.assertNotIn("auth_mode", merged)
        self.assertEqual(merged["agent_identity"]["agent_runtime_id"], "runtime-native-cache")

    def test_cockpit_personal_access_token_import_keeps_native_pat_schema(self):
        pat = self.jwt(
            {
                "exp": 2_000_000_000,
                "email": "pat@example.test",
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "pat-account",
                    "chatgpt_plan_type": "free",
                },
            }
        )
        encoded, source_type = core._normalize_import_auth_payload(
            {
                "type": "codex",
                "auth_mode": "personal_access_token",
                "access_token": pat,
                "account_id": "pat-account",
                "email": "pat@example.test",
                "tokens": {},
            }
        )
        canonical = json.loads(encoded)
        identity = core._identity_from_auth_bytes(encoded)
        credentials = core._chatgpt_credentials_from_auth_bytes(encoded)
        projected = json.loads(core._codex_auth_projection_bytes(encoded))

        self.assertEqual(source_type, "codex_auth")
        self.assertEqual(canonical["personal_access_token"], pat)
        self.assertNotIn("tokens", canonical)
        self.assertEqual(identity["accountId"], "pat-account")
        self.assertEqual(credentials["accessToken"], pat)
        self.assertEqual(
            projected,
            {"OPENAI_API_KEY": None, "personal_access_token": pat},
        )

        key_value = "\n".join(
            [
                "auth_mode=personal_access_token",
                f"personal_access_token={pat}",
                "account_id=pat-account",
                "email=pat@example.test",
            ]
        )
        documents = core._decode_many_json_documents(key_value, 1)
        self.assertEqual(len(documents), 1)
        parsed, parsed_source = core._normalize_import_auth_payload(documents[0])
        self.assertEqual(parsed_source, "codex_auth")
        self.assertEqual(json.loads(parsed)["personal_access_token"], pat)

    def test_opaque_personal_access_tokens_support_raw_noisy_and_key_value_batch_imports(self):
        first = "at-test_personal_access_token_00000001"
        second = "at-test.personal-access-token-00000002"

        encoded, source_type = core._normalize_import_auth_payload(first)
        canonical = json.loads(encoded)
        identity = core._identity_from_auth_bytes(encoded)
        self.assertEqual(source_type, "codex_auth")
        self.assertEqual(canonical["personal_access_token"], first)
        self.assertEqual(identity["authMode"], "personal_access_token")
        self.assertTrue(core._auth_bytes_support_codex(encoded))
        self.assertEqual(
            json.loads(core._codex_auth_projection_bytes(encoded)),
            {"OPENAI_API_KEY": None, "personal_access_token": first},
        )

        documents = core._decode_many_json_documents(
            f"以下两项仅供测试：\n{first}\n--- 任意说明 ---\n{second}\n完成",
            1,
        )
        self.assertEqual(len(documents), 2)
        self.assertEqual(
            [json.loads(core._normalize_import_auth_payload(item)[0])["personal_access_token"] for item in documents],
            [first, second],
        )

        key_value = core._decode_many_json_documents(f"token={first}", 1)
        self.assertEqual(len(key_value), 1)
        parsed, parsed_source = core._normalize_import_auth_payload(key_value[0])
        self.assertEqual(parsed_source, "codex_auth")
        self.assertEqual(json.loads(parsed)["personal_access_token"], first)

    def test_bare_agent_identity_jwt_is_distinguished_from_browser_web_session_jwt(self):
        agent_token = self.jwt(
            {
                "agent_runtime_id": "runtime-from-jwt",
                "agent_private_key": self.agent_private_key(2),
                "account_id": "agent-account-from-jwt",
                "chatgpt_user_id": "agent-user-from-jwt",
                "plan_type": "free",
                "email": "agent-jwt@example.test",
            }
        )
        encoded, source_type = core._normalize_import_auth_payload(agent_token)
        identity = core._identity_from_auth_bytes(encoded)
        self.assertEqual(source_type, "codex_auth")
        self.assertEqual(identity["authMode"], "agent_identity")
        self.assertEqual(identity["accountId"], "agent-account-from-jwt")

        browser_token = self.jwt(
            {
                "email": "browser@example.test",
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "browser-account",
                    "chatgpt_plan_type": "plus",
                },
            }
        )
        encoded, source_type = core._normalize_import_auth_payload(browser_token)
        identity = core._identity_from_auth_bytes(encoded)
        self.assertEqual(source_type, "web_session")
        self.assertEqual(identity["authMode"], "chatgpt")
        self.assertNotIn("agent_identity", json.loads(encoded))

    def test_cockpit_agent_identity_record_imports_and_projects_to_official_schema(self):
        record = {
            "agent_runtime_id": "runtime-agent-1",
            "agent_private_key": self.agent_private_key(3),
            "account_id": "agent-account",
            "chatgpt_user_id": "agent-user",
            "email": "agent@example.test",
            "plan_type": "free",
            "chatgpt_account_is_fedramp": False,
            "task_id": "task-1",
        }

        encoded, source_type = core._normalize_import_auth_payload(
            {
                "type": "codex",
                "auth_mode": "agentIdentity",
                "agent_identity": record,
            }
        )
        canonical = json.loads(encoded)
        identity = core._identity_from_auth_bytes(encoded)
        projected = json.loads(core._codex_auth_projection_bytes(encoded))

        self.assertEqual(source_type, "codex_auth")
        self.assertTrue(core._auth_bytes_support_codex(encoded))
        self.assertEqual(identity["authMode"], "agent_identity")
        self.assertEqual(identity["accountId"], "agent-account")
        self.assertEqual(identity["email"], "agent@example.test")
        self.assertEqual(canonical["agent_identity"], record)
        self.assertEqual(
            projected,
            {
                "auth_mode": "agentIdentity",
                "OPENAI_API_KEY": None,
                "agent_identity": record,
            },
        )

        flat_encoded, flat_source = core._normalize_import_auth_payload(
            {"auth_mode": "agentIdentity", "credentials": record}
        )
        self.assertEqual(flat_source, "codex_auth")
        self.assertEqual(json.loads(flat_encoded)["agent_identity"], record)

    def test_agent_identity_import_rejects_incomplete_or_mixed_credentials(self):
        with self.assertRaisesRegex(core.ManagerError, "缺少必需字段"):
            core._normalize_import_auth_payload(
                {
                    "auth_mode": "agentIdentity",
                    "agent_identity": {"account_id": "agent-only"},
                }
            )
        with self.assertRaisesRegex(core.ManagerError, "Base64 PKCS#8"):
            core._normalize_import_auth_payload(
                {
                    "auth_mode": "agentIdentity",
                    "agent_identity": {
                        "agent_runtime_id": "runtime-agent-1",
                        "agent_private_key": "not-base64-key",
                        "account_id": "agent-account",
                        "chatgpt_user_id": "agent-user",
                        "plan_type": "free",
                    },
                }
            )
        with self.assertRaisesRegex(core.ManagerError, "其他凭据"):
            core._normalize_import_auth_payload(
                {
                    "auth_mode": "agentIdentity",
                    "agent_identity": {
                        "agent_runtime_id": "runtime-agent-1",
                        "agent_private_key": self.agent_private_key(4),
                        "account_id": "agent-account",
                        "chatgpt_user_id": "agent-user",
                        "plan_type": "free",
                    },
                    "access_token": self.jwt({"sub": "mixed"}),
                }
            )

    def test_switch_preserves_machine_cap_sid_when_portable_import_omits_it(self):
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "running_codex_processes", return_value=[]),
        ):
            (self.root / "auth.json").write_text(
                json.dumps({"OPENAI_API_KEY": "portable-key"}), encoding="utf-8"
            )
            account = core.save_codex_account({"label": "Portable"})
            (self.root / "auth.json").write_text(
                json.dumps({"OPENAI_API_KEY": "current-key"}), encoding="utf-8"
            )
            (self.root / "cap_sid").write_bytes(b"machine-local-cap")

            switched = core.switch_codex_account(account["id"])

        self.assertTrue(switched["changed"])
        self.assertEqual((self.root / "cap_sid").read_bytes(), b"machine-local-cap")

    def test_account_activation_timeline_uses_bounded_non_sensitive_markers(self):
        core.ensure_state()
        settings = core.load_settings()
        settings["accounts"] = [{"id": "account-main", "label": "Main"}]
        core.save_settings(settings)
        event = core.record_direct_account_activation(
            "account-main",
            "2026-08-11T01:02:03+00:00",
        )
        self.assertEqual(event["accountId"], "account-main")
        stored = json.loads(core.ACCOUNT_ACTIVATION_HISTORY_FILE.read_text(encoding="utf-8"))
        self.assertEqual(set(stored["events"][0]), {"accountId", "timestamp", "source"})
        timeline = core.account_activation_timeline(core.load_settings())
        self.assertEqual(timeline[0]["accountId"], "account-main")

    def test_account_metadata_edit_preserves_encrypted_credentials(self):
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            account = core.import_codex_account(
                {"authJson": {"OPENAI_API_KEY": "sk-edit-secret"}, "groupId": "official"}
            )
            secret_before = core.SECRETS_FILE.read_bytes()
            updated = core.update_codex_account_metadata(
                account["id"],
                {"label": "新的本地名称", "groupId": "free"},
            )
        self.assertEqual(updated["label"], "新的本地名称")
        self.assertEqual(updated["groupId"], "free")
        self.assertEqual(core.SECRETS_FILE.read_bytes(), secret_before)

    def test_switch_and_launch_closes_before_switching_and_reopening(self):
        settings = core._initial_settings()
        settings["accounts"] = [{"id": "target-account", "label": "Target"}]
        calls = []
        progress = []
        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "_load_account_snapshot", return_value={"version": 1}),
            patch.object(
                core,
                "_decode_snapshot_files",
                return_value={"auth.json": b'{"OPENAI_API_KEY":"target-key"}', "cap_sid": None},
            ),
            patch.object(core, "codex_prefix", return_value=["codex.exe"]),
            patch.object(
                core,
                "resolve_codex_launch_plan",
                return_value={
                    "strategy": "windows_app",
                    "appUserModelId": "OpenAI.Codex_2p2nqsd0c76g0!App",
                },
            ),
            patch.object(core, "close_codex_processes", side_effect=AssertionError("unsafe closer must not run")),
            patch.object(core, "switch_codex_account", side_effect=lambda *_args, **_kwargs: calls.append("switch") or {"changed": True}),
            patch.object(core, "launch_codex_app", side_effect=lambda **_kwargs: calls.append("launch") or {"started": True}),
            patch.object(
                core,
                "wait_for_codex_runtime_ready",
                side_effect=lambda *_args, **_kwargs: calls.append("ready") or {"ready": True},
            ),
        ):
            result = core.switch_codex_account_and_launch(
                "target-account",
                progress_callback=progress.append,
                close_processes_callback=lambda **_kwargs: calls.append("close") or {"closed": ["7"]},
            )
        self.assertEqual(calls, ["close", "switch", "launch", "ready"])
        self.assertTrue(result["launch"]["started"])
        self.assertTrue(result["launch"]["readiness"]["ready"])
        self.assertEqual(progress[0]["phase"], "preflight")
        self.assertEqual(progress[-1]["phase"], "completed")
        self.assertEqual(progress[-1]["status"], "completed")
        self.assertIn("totalMs", result["performance"])

    def test_switch_preflights_oauth_before_closing_codex(self):
        settings = core._initial_settings()
        settings["accounts"] = [
            {
                "id": "target-account",
                "label": "Target",
                "authMode": "chatgpt",
                "sourceType": "codex_auth",
            }
        ]
        calls = []
        account = settings["accounts"][0]
        source = {"id": "account:target-account", "models": [{"id": "gpt-test", "key": "target"}]}
        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(
                core,
                "_prepare_account_switch_target",
                return_value=(account, {"auth.json": b"{}", "cap_sid": None}),
            ),
            patch.object(core, "_account_model_source", return_value=source),
            patch.object(core, "_official_account_target_is_active", return_value=False),
            patch.object(
                core,
                "_account_chatgpt_credentials",
                side_effect=lambda *_args, **_kwargs: calls.append("credentials") or {},
            ),
            patch.object(core, "resolve_codex_launch_plan", return_value={"strategy": "windows_app"}),
            patch.object(core, "_capture_switch_transaction_snapshot", return_value={"files": {}, "environment": {}}),
            patch.object(core, "close_codex_processes", side_effect=lambda: calls.append("close") or {}),
            patch.object(
                core,
                "switch_codex_account",
                side_effect=lambda *_args, **_kwargs: calls.append("switch") or {"sessionSync": {}},
            ),
            patch.object(
                core,
                "_apply_official_account_configuration",
                return_value={"workspace": {}, "applied": {}, "model": "gpt-test"},
            ),
            patch.object(core, "launch_codex_app", return_value={"started": True}),
            patch.object(core, "wait_for_codex_runtime_ready", return_value={"ready": True}),
        ):
            core.switch_codex_account_and_launch("target-account")

        self.assertLess(calls.index("credentials"), calls.index("close"))
        self.assertEqual(calls.count("credentials"), 1)

    def test_switch_progress_registry_reports_real_phase_and_preserves_failure_stage(self):
        runtime = object.__new__(app.ManagerRuntime)
        runtime.switch_operation_lock = threading.RLock()
        runtime.switch_operations = {}

        queued = runtime.begin_switch_operation(
            "switch_progress_01",
            target_id="account-1",
            target_name="official@example.test",
            target_kind="account",
        )
        self.assertEqual(queued["phase"], "queued")
        running = runtime.update_switch_operation(
            "switch_progress_01",
            {
                "phase": "verifying",
                "progress": 90,
                "message": "正在核对官方账号",
                "elapsedMs": 1250,
            },
        )
        self.assertEqual(running["phase"], "verifying")
        self.assertGreaterEqual(running["elapsedMs"], 1250)

        failed = runtime.finish_switch_operation(
            "switch_progress_01",
            error=core.ManagerError("runtime check failed"),
        )
        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["phase"], "failed")
        self.assertEqual(failed["failedPhase"], "verifying")
        self.assertNotIn("_startedMonotonic", failed)

    def test_switch_progress_preserves_precise_failure_phase_and_recovery_result(self):
        runtime = object.__new__(app.ManagerRuntime)
        runtime.switch_operation_lock = threading.RLock()
        runtime.switch_operations = {}
        operation_id = "switch_progress_precise_01"
        runtime.begin_switch_operation(
            operation_id,
            target_id="account-1",
            target_name="official@example.test",
            target_kind="account",
        )
        reporter = core._SwitchProgressReporter(
            lambda payload: runtime.update_switch_operation(operation_id, payload)
        )
        reporter.emit("closing", 30, "正在安全关闭旧 Codex")
        failed_phase = reporter.phase
        reporter.emit("recovering", 96, "正在核对原状态")
        reporter.failed(
            core.ManagerError("transient process scan"),
            failed_phase=failed_phase,
            message="切换未完成，原账号与配置未改变",
            recovery_state="unchanged",
        )
        failed = runtime.finish_switch_operation(
            operation_id,
            error=core.ManagerError("transient process scan"),
        )
        self.assertEqual(failed["failedPhase"], "closing")
        self.assertEqual(failed["recoveryState"], "unchanged")
        self.assertEqual(failed["message"], "切换未完成，原账号与配置未改变")
        self.assertTrue(failed["canRetry"])

    def test_failed_switch_before_first_write_does_not_claim_snapshot_restore(self):
        with patch.object(core, "_restore_switch_transaction_snapshot") as restore:
            detail = core._rollback_failed_switch(
                {"files": {}, "environment": {}},
                {"strategy": "windows_app"},
                closed=None,
                launch_attempted=False,
                state_mutated=False,
            )
        restore.assert_not_called()
        self.assertIn("目标配置尚未写入", detail)
        self.assertIn("原账号与配置未改变", detail)

    def test_switch_and_launch_readiness_failure_does_not_restart_loop(self):
        settings = core._initial_settings()
        settings["accounts"] = [{"id": "target-account", "label": "target@example.test"}]
        calls = []

        def close(**_kwargs):
            calls.append("close")
            return {"closed": ["7"]}

        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "_load_account_snapshot", return_value={"version": 1}),
            patch.object(
                core,
                "_decode_snapshot_files",
                return_value={"auth.json": b'{"OPENAI_API_KEY":"target-key"}', "cap_sid": None},
            ),
            patch.object(core, "resolve_codex_launch_plan", return_value={"strategy": "windows_app"}),
            patch.object(core, "close_codex_processes", side_effect=close),
            patch.object(core, "switch_codex_account", return_value={"changed": True}),
            patch.object(
                core,
                "launch_codex_app",
                side_effect=lambda **_kwargs: calls.append("launch") or {"started": True},
            ),
            patch.object(core, "_detect_codex_windows_app", return_value=None),
            patch.object(core, "running_codex_processes", return_value=[{"pid": "7"}]),
            patch.object(
                core,
                "wait_for_codex_runtime_ready",
                side_effect=core.ManagerError("startup timeout"),
            ),
        ):
            with self.assertRaisesRegex(core.ManagerError, "避免循环重启"):
                core.switch_codex_account_and_launch("target-account")
        self.assertEqual(calls, ["close", "launch", "close"])

    def test_failed_switch_cleanup_uses_injected_lifecycle_safe_closer(self):
        safe_close_calls = []

        def safe_close(**kwargs):
            safe_close_calls.append(kwargs)
            return {"closed": ["7"]}

        with (
            patch.object(core, "running_codex_processes", return_value=[{"pid": "7"}]),
            patch.object(core, "close_codex_processes", side_effect=AssertionError("unsafe closer must not run")),
            patch.object(core, "_restore_switch_transaction_snapshot", return_value=[]),
        ):
            detail = core._rollback_failed_switch(
                {"files": {}, "environment": {}},
                {"strategy": "windows_app"},
                closed=None,
                launch_attempted=True,
                close_processes_callback=safe_close,
            )
        self.assertEqual(safe_close_calls, [{"timeout_seconds": 8}])
        self.assertIn("关闭未通过验证的 Codex", detail)

    def test_switch_and_launch_restores_original_credentials_when_both_readiness_checks_fail(self):
        settings = core._initial_settings()
        settings["accounts"] = [{"id": "target-account", "label": "target@example.test"}]
        settings["modelWorkspace"]["activeSourceId"] = "account:original-account"
        settings["web2api"]["activeForCodex"] = True
        original_auth = json.dumps({"OPENAI_API_KEY": "original-key"}).encode()
        (self.root / "auth.json").write_bytes(original_auth)
        calls = []

        def switch(*_args, **_kwargs):
            calls.append("switch")
            (self.root / "auth.json").write_text(
                json.dumps({"OPENAI_API_KEY": "target-key"}), encoding="utf-8"
            )
            return {"changed": True}

        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "save_settings", side_effect=lambda _settings: calls.append("save")),
            patch.object(core, "_load_account_snapshot", return_value={"version": 1}),
            patch.object(
                core,
                "_decode_snapshot_files",
                return_value={"auth.json": b'{"OPENAI_API_KEY":"target-key"}', "cap_sid": None},
            ),
            patch.object(core, "resolve_codex_launch_plan", return_value={"strategy": "windows_app"}),
            patch.object(
                core,
                "close_codex_processes",
                side_effect=lambda **_kwargs: calls.append("close") or {"closed": ["7"]},
            ),
            patch.object(core, "switch_codex_account", side_effect=switch),
            patch.object(
                core,
                "launch_codex_app",
                side_effect=lambda **_kwargs: calls.append("launch") or {"started": True},
            ),
            patch.object(core, "_detect_codex_windows_app", return_value=None),
            patch.object(core, "running_codex_processes", return_value=[{"pid": "7"}]),
            patch.object(
                core,
                "wait_for_codex_runtime_ready",
                side_effect=core.ManagerError("startup timeout"),
            ),
            patch.object(
                core,
                "apply_configuration",
                side_effect=lambda _sync=False: calls.append("apply") or {"changed": True},
            ),
        ):
            with self.assertRaisesRegex(core.ManagerError, "已原样回滚"):
                core.switch_codex_account_and_launch("target-account")

        self.assertEqual((self.root / "auth.json").read_bytes(), original_auth)
        self.assertEqual(settings["modelWorkspace"]["activeSourceId"], "account:original-account")
        self.assertTrue(settings["web2api"]["activeForCodex"])
        self.assertEqual(calls.count("launch"), 1)
        self.assertEqual(calls.count("close"), 2)
        self.assertEqual(calls.count("apply"), 1)

    def test_codex_launch_plan_never_uses_an_implicit_current_directory(self):
        windows_app = {
            "appUserModelId": "OpenAI.Codex_2p2nqsd0c76g0!App",
            "executable": "C:/Program Files/WindowsApps/OpenAI.Codex/app/ChatGPT.exe",
            "package": "OpenAI.Codex_test",
            "version": "1.0.0.0",
        }
        with patch.object(core, "_detect_codex_windows_app", return_value=windows_app):
            windows_plan = core.resolve_codex_launch_plan(["codex.exe"])
        self.assertEqual(windows_plan["strategy"], "windows_app")
        self.assertEqual(windows_plan["appUserModelId"], "OpenAI.Codex_2p2nqsd0c76g0!App")

        with (
            patch.object(core, "_detect_codex_windows_app", return_value=None),
            patch.object(core, "_recent_codex_workspace", return_value=self.root),
        ):
            cli_plan = core.resolve_codex_launch_plan(["codex.exe"])
        self.assertEqual(cli_plan["command"], ["codex.exe", "app", str(self.root)])
        self.assertNotEqual(cli_plan["command"], ["codex.exe", "app"])

    def test_close_codex_processes_uses_graceful_hidden_taskkill_first(self):
        process = {"name": "Codex.exe", "pid": "4321"}
        with (
            patch.object(core, "running_codex_processes", side_effect=[[process], []]),
            patch.object(core.subprocess, "run") as run,
        ):
            result = core.close_codex_processes()
        self.assertEqual(result["closed"], ["4321"])
        command = run.call_args.args[0]
        self.assertEqual(command, ["taskkill.exe", "/PID", "4321"])
        self.assertNotIn("/F", command)
        self.assertNotIn("/T", command)

    def test_close_codex_processes_retries_transient_unknown_scan_after_close_started(self):
        process = {"name": "ChatGPT.exe", "pid": "4321", "parentPid": "9"}
        unknown = core.CodexProcessScan(
            [],
            known=False,
            error="renderer executable path pending",
        )
        observations = iter([[process], unknown, []])
        with (
            patch.object(core, "running_codex_processes", side_effect=lambda: next(observations)),
            patch.object(core.subprocess, "run") as run,
            patch.object(core.time, "sleep", return_value=None),
        ):
            result = core.close_codex_processes()
        self.assertEqual(result["closed"], ["4321"])
        self.assertEqual(result["processScanRetries"], 1)
        self.assertEqual(run.call_args.args[0], ["taskkill.exe", "/PID", "4321"])

    def test_close_codex_processes_persistent_unknown_scan_stays_fail_closed(self):
        process = {"name": "ChatGPT.exe", "pid": "4321", "parentPid": "9"}
        unknown = core.CodexProcessScan(
            [],
            known=False,
            error="protected same-name process",
        )
        calls = 0

        def scan():
            nonlocal calls
            calls += 1
            return [process] if calls == 1 else unknown

        with (
            patch.object(core, "running_codex_processes", side_effect=scan),
            patch.object(core.subprocess, "run"),
            patch.object(core.time, "sleep", return_value=None),
        ):
            with self.assertRaisesRegex(core.CodexProcessScanError, "自动复查 12 次"):
                core.close_codex_processes()
        self.assertEqual(calls, 13)

    def test_process_scan_failure_is_explicit_and_close_fails_closed(self):
        unknown = core.CodexProcessScan(
            [],
            known=False,
            error="Toolhelp unavailable",
        )
        with self.assertRaisesRegex(core.CodexProcessScanError, "Toolhelp unavailable"):
            core._require_known_codex_processes(unknown)
        with (
            patch.object(core.os, "name", "nt"),
            patch.object(core, "running_codex_processes", return_value=unknown),
        ):
            with self.assertRaisesRegex(core.CodexProcessScanError, "已停止本次修改操作"):
                core.close_codex_processes()

    def test_launch_wait_accepts_verified_process_during_transient_partial_scan(self):
        process = {"name": "ChatGPT.exe", "pid": "200", "parentPid": "9"}
        observations = iter(
            [
                core.CodexProcessScan([], known=False, error="renderer path pending"),
                core.CodexProcessScan([process], known=False, error="renderer path pending"),
            ]
        )
        completed = subprocess.CompletedProcess([], 0, "", "")
        plan = {
            "strategy": "windows_app",
            "appUserModelId": "OpenAI.Codex_2p2nqsd0c76g0!App",
            "package": "OpenAI.Codex_test",
        }
        with (
            patch.object(core.subprocess, "run", return_value=completed),
            patch.object(core, "running_codex_processes", side_effect=lambda: next(observations)),
            patch.object(core.time, "sleep", return_value=None),
        ):
            result = core.launch_codex_app(launch_plan=plan)
        self.assertTrue(result["started"])
        self.assertEqual(result["launchMethod"], "app_user_model_id")

    def test_windows_process_scan_does_not_treat_tasklist_as_stopped(self):
        tasklist = type("Tasklist", (), {"stdout": '"codex.exe","1234"', "returncode": 0})()
        with (
            patch.object(core.os, "name", "nt"),
            patch.object(core, "_detect_codex_windows_app", return_value=None),
            patch.object(core.ctypes, "windll", SimpleNamespace(kernel32=SimpleNamespace())),
            patch.object(core.subprocess, "run", return_value=tasklist),
        ):
            result = core.running_codex_processes()
        self.assertFalse(result.known)
        self.assertIn("无法验证可执行路径", result.error)

    def test_read_only_health_defers_when_process_scan_is_unknown(self):
        unknown = core.CodexProcessScan([], known=False, error="Toolhelp unavailable")
        with patch.object(core, "running_codex_processes", return_value=unknown):
            health = core.stale_subagent_health()
        self.assertEqual(health["status"], "warning")
        self.assertFalse(health["checked"])
        self.assertFalse(health["recoverable"])
        self.assertIn("无法可靠确认", health["detail"])

    def test_close_codex_processes_terminates_only_root_process_trees(self):
        root = {"name": "ChatGPT.exe", "pid": "100", "parentPid": "9"}
        renderer = {"name": "ChatGPT.exe", "pid": "101", "parentPid": "100"}
        app_server = {"name": "codex.exe", "pid": "102", "parentPid": "100"}
        with (
            patch.object(core, "running_codex_processes", side_effect=[[root, renderer, app_server], []]),
            patch.object(core.subprocess, "run") as run,
        ):
            result = core.close_codex_processes()
        self.assertEqual(result["rootPids"], ["100"])
        self.assertEqual(run.call_count, 3)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands, [
            ["taskkill.exe", "/PID", "100"],
            ["taskkill.exe", "/PID", "101"],
            ["taskkill.exe", "/PID", "102"],
        ])
        self.assertTrue(all("/T" not in command for command in commands))
        self.assertEqual(set(result["closed"]), {"100", "101", "102"})

    def test_windows_app_resolver_uses_current_appx_manifest_not_legacy_shortcuts(self):
        executable = self.root / "ChatGPT.exe"
        executable.write_bytes(b"test")
        payload = {
            "appUserModelId": "OpenAI.Codex_2p2nqsd0c76g0!App",
            "executable": str(executable),
            "exists": True,
            "package": "OpenAI.Codex_5.7.0.0_x64__2p2nqsd0c76g0",
            "version": "5.7.0.0",
            "applicationId": "App",
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        core.CODEX_WINDOWS_APP_CACHE.update({"at": 0.0, "value": None})
        with patch.object(core.subprocess, "run", return_value=completed) as run:
            detected = core._detect_codex_windows_app(force=True)
        self.assertEqual(detected["appUserModelId"], "OpenAI.Codex_2p2nqsd0c76g0!App")
        self.assertEqual(detected["executable"], str(executable))
        self.assertIn("Get-AppxPackage | Where-Object", run.call_args.args[0][-1])
        self.assertNotIn("Get-StartApps", run.call_args.args[0][-1])

    def test_windows_app_resolver_reuses_persisted_app_id_when_codex_is_closed(self):
        discovery_file = core.STATE_DIR / "codex-windows-app.json"
        discovery_file.parent.mkdir(parents=True, exist_ok=True)
        discovery_file.write_text(
            json.dumps(
                {
                    "appUserModelId": "OpenAI.Codex_2p2nqsd0c76g0!App",
                    "executable": str(self.root / "removed-version" / "ChatGPT.exe"),
                    "package": "OpenAI.Codex_old",
                    "version": "1.0.0.0",
                }
            ),
            encoding="utf-8",
        )
        core.CODEX_WINDOWS_APP_CACHE.update({"at": 0.0, "value": None})
        failed_scan = subprocess.CompletedProcess([], 1, "", "transient failure")
        with (
            patch.object(core.subprocess, "run", return_value=failed_scan),
            patch.object(core, "_running_windows_codex_candidates", return_value=[]),
        ):
            detected = core._detect_codex_windows_app(force=True)
        self.assertEqual(detected["appUserModelId"], "OpenAI.Codex_2p2nqsd0c76g0!App")
        self.assertEqual(detected["package"], "OpenAI.Codex_old")

    @unittest.skipUnless(core.os.name == "nt", "desktop bundled runtime is Windows-specific")
    def test_codex_prefix_uses_desktop_bundled_runtime_when_standalone_cli_is_missing(self):
        app_dir = self.root / "desktop" / "app"
        desktop_executable = app_dir / "ChatGPT.exe"
        bundled_runtime = app_dir / "resources" / "codex.exe"
        bundled_runtime.parent.mkdir(parents=True)
        desktop_executable.write_bytes(b"desktop")
        bundled_runtime.write_bytes(b"bundled-runtime")
        completed = subprocess.CompletedProcess([], 0, "codex-cli 0.147.0", "")
        with (
            patch.dict(core.os.environ, {"CODEX_CLI_PATH": ""}, clear=False),
            patch.object(core.shutil, "which", return_value=None),
            patch.object(
                core,
                "_detect_codex_windows_app",
                return_value={"executable": str(desktop_executable)},
            ),
            patch.object(core, "_desktop_managed_codex_candidates", return_value=[]),
            patch.object(core, "running_codex_processes", return_value=[]),
            patch.object(core.subprocess, "run", return_value=completed) as run,
        ):
            prefix = core.codex_prefix()
        self.assertEqual(prefix, [str(bundled_runtime)])
        self.assertEqual(run.call_args.args[0], [str(bundled_runtime), "--version"])

    @unittest.skipUnless(core.os.name == "nt", "desktop managed runtime is Windows-specific")
    def test_codex_prefix_finds_versioned_runtime_downloaded_by_desktop(self):
        local_app_data = self.root / "LocalAppData"
        managed_runtime = local_app_data / "OpenAI" / "Codex" / "bin" / "build-hash" / "codex.exe"
        managed_runtime.parent.mkdir(parents=True)
        managed_runtime.write_bytes(b"managed-runtime")
        completed = subprocess.CompletedProcess([], 0, "codex-cli 0.148.0", "")
        with (
            patch.dict(
                core.os.environ,
                {"CODEX_CLI_PATH": "", "LOCALAPPDATA": str(local_app_data)},
                clear=False,
            ),
            patch.object(core.shutil, "which", return_value=None),
            patch.object(core, "_discover_node_npm_runtime", return_value=None),
            patch.object(core, "_detect_codex_windows_app", return_value=None),
            patch.object(core, "running_codex_processes", return_value=[]),
            patch.object(core.subprocess, "run", return_value=completed) as run,
        ):
            prefix = core.codex_prefix()
        self.assertEqual(prefix, [str(managed_runtime.resolve())])
        self.assertEqual(run.call_args.args[0], [str(managed_runtime.resolve()), "--version"])

    @unittest.skipUnless(core.os.name == "nt", "manager runtime discovery is Windows-specific")
    def test_codex_prefix_finds_manager_downloaded_native_runtime(self):
        runtime = core.MANAGED_CODEX_RUNTIME_DIR / "0.147.0-win32-x64" / "bin" / "codex.exe"
        runtime.parent.mkdir(parents=True)
        runtime.write_bytes(b"runtime")
        completed = subprocess.CompletedProcess([], 0, "codex-cli 0.147.0", "")
        with (
            patch.dict(core.os.environ, {"CODEX_CLI_PATH": ""}, clear=False),
            patch.object(core.shutil, "which", return_value=None),
            patch.object(core, "_discover_node_npm_runtime", return_value=None),
            patch.object(core, "_detect_codex_windows_app", return_value=None),
            patch.object(core, "running_codex_processes", return_value=[]),
            patch.object(core, "_desktop_managed_codex_candidates", return_value=[]),
            patch.object(core.subprocess, "run", return_value=completed) as run,
        ):
            prefix = core.codex_prefix()
        self.assertEqual(prefix, [str(runtime.resolve())])
        self.assertEqual(run.call_args.args[0], [str(runtime.resolve()), "--version"])

    @unittest.skipUnless(core.os.name == "nt", "native runtime installer is Windows-specific")
    def test_official_native_runtime_download_verifies_integrity_and_extracts_only_vendor(self):
        archive_bytes = io.BytesIO()
        files = {
            "package/vendor/x86_64-pc-windows-msvc/bin/codex.exe": b"native-codex",
            "package/vendor/x86_64-pc-windows-msvc/codex-resources/helper.exe": b"helper",
            "package/package.json": b"must-not-be-extracted",
        }
        with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
            for name, content in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        payload = archive_bytes.getvalue()
        digest = hashlib.sha512(payload).digest()
        latest = {"version": "0.147.0"}
        platform = {
            "version": "0.147.0-win32-x64",
            "dist": {
                "tarball": "https://registry.npmjs.org/@openai/codex/-/codex-test.tgz",
                "integrity": "sha512-" + base64.b64encode(digest).decode("ascii"),
            },
        }
        with (
            patch.dict(core.os.environ, {"PROCESSOR_ARCHITECTURE": "AMD64"}, clear=False),
            patch.object(core, "_registry_json", side_effect=[latest, platform]),
            patch.object(core, "_open_same_origin_request", return_value=io.BytesIO(payload)),
            patch.object(core, "_verify_codex_cli", return_value="codex-cli 0.147.0"),
        ):
            result = core._download_official_codex_runtime()
        installed = Path(result["path"])
        self.assertTrue(installed.is_file())
        self.assertEqual(installed.read_bytes(), b"native-codex")
        self.assertTrue((installed.parent.parent / "codex-resources" / "helper.exe").is_file())
        self.assertFalse((installed.parent.parent / "package.json").exists())
        self.assertTrue(result["downloaded"])

    def test_runtime_archive_windows_path_validation_rejects_escape_and_devices(self):
        staging = self.root / "runtime-staging"
        staging.mkdir()
        safe = core._safe_runtime_archive_destination(
            staging,
            core.PurePosixPath("bin", "codex.exe"),
        )
        self.assertEqual(safe, staging / "bin" / "codex.exe")
        for unsafe in (
            core.PurePosixPath(r"bin\..\..\escape.exe"),
            core.PurePosixPath("bin", "payload.exe:stream"),
            core.PurePosixPath("bin", "CON.txt"),
            core.PurePosixPath("bin", "trailing."),
            core.PurePosixPath("..", "escape.exe"),
        ):
            with self.subTest(path=str(unsafe)), self.assertRaises(core.ManagerError):
                core._safe_runtime_archive_destination(staging, unsafe)

    @unittest.skipUnless(core.os.name == "nt", "native runtime installer is Windows-specific")
    def test_official_native_runtime_replaces_corrupt_existing_release_directory(self):
        archive_bytes = io.BytesIO()
        content = b"native-codex"
        with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
            info = tarfile.TarInfo("package/vendor/x86_64-pc-windows-msvc/bin/codex.exe")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        payload = archive_bytes.getvalue()
        digest = hashlib.sha512(payload).digest()
        release_id = f"0.147.0-win32-x64-{digest.hex()[:12]}"
        corrupt_release = core.MANAGED_CODEX_RUNTIME_DIR / release_id
        corrupt_release.mkdir(parents=True)
        (corrupt_release / "corrupt.txt").write_text("partial install", encoding="utf-8")
        latest = {"version": "0.147.0"}
        platform = {
            "version": "0.147.0-win32-x64",
            "dist": {
                "tarball": "https://registry.npmjs.org/@openai/codex/-/codex-test.tgz",
                "integrity": "sha512-" + base64.b64encode(digest).decode("ascii"),
            },
        }

        with (
            patch.dict(core.os.environ, {"PROCESSOR_ARCHITECTURE": "AMD64"}, clear=False),
            patch.object(core, "_registry_json", side_effect=[latest, platform]),
            patch.object(core, "_open_same_origin_request", return_value=io.BytesIO(payload)),
            patch.object(core, "_verify_codex_cli", return_value="codex-cli 0.147.0"),
        ):
            result = core._download_official_codex_runtime()

        installed = Path(result["path"])
        self.assertEqual(installed.read_bytes(), content)
        self.assertFalse((corrupt_release / "corrupt.txt").exists())
        self.assertTrue(result["downloaded"])

    def test_codex_prefix_bounds_and_ignores_failed_npm_global_root_probe(self):
        runtime = {"node": "node.exe", "npmCommand": ["npm.cmd"]}
        with (
            patch.dict(core.os.environ, {"CODEX_CLI_PATH": ""}, clear=False),
            patch.object(core.shutil, "which", return_value=None),
            patch.object(core, "_discover_node_npm_runtime", return_value=runtime),
            patch.object(
                core.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["npm.cmd", "root", "-g"], 5),
            ) as run,
            patch.object(core, "running_codex_processes", return_value=[]),
            patch.object(core, "_detect_codex_windows_app", return_value=None),
            patch.object(core, "_manager_downloaded_codex_candidates", return_value=[]),
            patch.object(core, "_desktop_managed_codex_candidates", return_value=[]),
        ):
            with self.assertRaisesRegex(core.ManagerError, "未找到可用"):
                core.codex_prefix()

        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["timeout"], 5)

    @unittest.skipUnless(core.os.name == "nt", "running desktop discovery is Windows-specific")
    def test_codex_prefix_uses_runtime_of_current_desktop_process_when_appx_query_fails(self):
        bundled_runtime = self.root / "custom-install" / "resources" / "codex.exe"
        bundled_runtime.parent.mkdir(parents=True)
        bundled_runtime.write_bytes(b"runtime")
        completed = subprocess.CompletedProcess([], 0, "codex-cli 0.147.0", "")
        with (
            patch.dict(core.os.environ, {"CODEX_CLI_PATH": ""}, clear=False),
            patch.object(core.shutil, "which", return_value=None),
            patch.object(
                core,
                "running_codex_processes",
                return_value=[
                    {
                        "name": "codex.exe",
                        "pid": "42",
                        "parentPid": "10",
                        "executable": str(bundled_runtime),
                    }
                ],
            ),
            patch.object(core, "_detect_codex_windows_app", return_value=None),
            patch.object(core.subprocess, "run", return_value=completed) as run,
        ):
            prefix = core.codex_prefix()
        self.assertEqual(prefix, [str(bundled_runtime.resolve())])
        self.assertEqual(run.call_args.args[0], [str(bundled_runtime.resolve()), "--version"])

    @unittest.skipUnless(core.os.name == "nt", "running desktop discovery is Windows-specific")
    def test_windows_app_resolver_falls_back_to_running_desktop_executable(self):
        desktop_executable = self.root / "portable" / "ChatGPT.exe"
        desktop_executable.parent.mkdir(parents=True)
        desktop_executable.write_bytes(b"desktop")
        failed_query = subprocess.CompletedProcess([], 1, "", "query failed")
        core.CODEX_WINDOWS_APP_CACHE.update({"at": 0.0, "value": None})
        with (
            patch.object(core.subprocess, "run", return_value=failed_query),
            patch.object(
                core,
                "_running_windows_codex_candidates",
                return_value=[
                    {
                        "name": "ChatGPT.exe",
                        "pid": "100",
                        "parentPid": "9",
                        "executable": str(desktop_executable),
                    }
                ],
            ),
        ):
            detected = core._detect_codex_windows_app(force=True)
            plan = core.resolve_codex_launch_plan()
        self.assertEqual(detected["source"], "running_process")
        self.assertEqual(detected["executable"], str(desktop_executable))
        self.assertEqual(plan["strategy"], "desktop_executable")

    def test_runtime_deploy_reuses_existing_runtime_before_any_install(self):
        existing = self.root / "codex.exe"
        existing.write_bytes(b"runtime")
        status = {
            "available": True,
            "source": "desktop_bundled",
            "command": [str(existing)],
        }
        with (
            patch.object(core, "_clear_unsafe_codex_cli_override", return_value=False),
            patch.object(core, "codex_runtime_status", return_value=status),
            patch.object(core, "_sync_user_environment") as sync_environment,
            patch.object(core.subprocess, "run") as run,
        ):
            result = core.deploy_codex_runtime()
        self.assertFalse(result["installed"])
        self.assertTrue(result["repaired"])
        self.assertFalse(result["removedUnsafeCliOverride"])
        sync_environment.assert_not_called()
        run.assert_not_called()

    def test_runtime_path_refresh_merges_machine_user_and_process_paths(self):
        previous_path = os.environ.get("PATH")
        try:
            os.environ["PATH"] = os.pathsep.join([r"C:\Process", r"C:\Shared"])
            with patch.object(
                core,
                "_windows_registry_path_values",
                return_value=[
                    os.pathsep.join([r"C:\Machine", r"C:\Shared"]),
                    r"C:\User",
                ],
            ):
                paths = core._refresh_windows_process_path([r"C:\Node"])
            self.assertEqual(paths[:4], [r"C:\Node", r"C:\Machine", r"C:\Shared", r"C:\User"])
            self.assertEqual(os.environ["PATH"], os.pathsep.join(paths))
        finally:
            if previous_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = previous_path

    def test_node_runtime_discovery_uses_npm_cli_js_when_wrapper_is_missing(self):
        node_dir = self.root / "nodejs"
        npm_cli = node_dir / "node_modules" / "npm" / "bin" / "npm-cli.js"
        npm_cli.parent.mkdir(parents=True)
        node = node_dir / "node.exe"
        node.write_bytes(b"node")
        npm_cli.write_text("npm", encoding="utf-8")
        with (
            patch.object(core.shutil, "which", return_value=None),
            patch.object(core, "_node_runtime_search_directories", return_value=[node_dir]),
            patch.object(core, "_refresh_windows_process_path", return_value=[]),
        ):
            runtime = core._discover_node_npm_runtime(refresh_registry=True, update_process_path=True)
        self.assertEqual(runtime["node"], str(node))
        self.assertEqual(runtime["npm"], str(npm_cli))
        self.assertEqual(runtime["npmCommand"], [str(node), str(npm_cli)])

    def test_runtime_deploy_repairs_node_when_winget_install_still_has_no_npm(self):
        node = self.root / "nodejs" / "node.exe"
        npm = self.root / "nodejs" / "npm.cmd"
        codex = self.root / "npm" / "codex.cmd"
        runtime = {"node": str(node), "npm": str(npm), "npmCommand": [str(npm)]}
        missing_status = {"available": False, "source": "missing", "command": []}
        ready_status = {"available": True, "source": "configured", "command": [str(codex)]}
        completed = subprocess.CompletedProcess([], 0, "ok", "")

        def which(name):
            return "winget.exe" if name in {"winget.exe", "winget"} else None

        with (
            patch.object(core, "_clear_unsafe_codex_cli_override", return_value=False),
            patch.object(core, "codex_runtime_status", side_effect=[missing_status, ready_status]),
            patch.object(core, "_discover_node_npm_runtime", side_effect=[None, None, runtime]) as discover,
            patch.object(
                core,
                "_download_official_codex_runtime",
                side_effect=core.ManagerError("simulated direct download failure"),
            ),
            patch.object(core.shutil, "which", side_effect=which),
            patch.object(core, "_configure_windows_runtime_path"),
            patch.object(core, "_locate_npm_codex_cli", side_effect=[None, codex]),
            patch.object(core, "_verify_codex_cli", return_value="codex-cli 1.2.3") as verify,
            patch.object(core.subprocess, "run", return_value=completed) as run,
        ):
            result = core.deploy_codex_runtime()

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][1], "install")
        self.assertEqual(commands[1][1], "repair")
        self.assertEqual(commands[2], [str(npm), "install", "--global", "@openai/codex@latest"])
        self.assertEqual(discover.call_count, 3)
        verify.assert_called_once_with(codex)
        self.assertTrue(result["installed"])
        self.assertTrue(result["nodeInstalled"])
        self.assertEqual(result["version"], "codex-cli 1.2.3")

    def test_runtime_deploy_reuses_existing_npm_cli_without_reinstalling(self):
        node = self.root / "nodejs" / "node.exe"
        npm = self.root / "nodejs" / "npm.cmd"
        codex = self.root / "npm" / "codex.cmd"
        runtime = {"node": str(node), "npm": str(npm), "npmCommand": [str(npm)]}
        missing_status = {"available": False, "source": "missing", "command": []}
        ready_status = {"available": True, "source": "configured", "command": [str(codex)]}
        with (
            patch.object(core, "_clear_unsafe_codex_cli_override", return_value=False),
            patch.object(core, "codex_runtime_status", side_effect=[missing_status, ready_status]),
            patch.object(core, "_discover_node_npm_runtime", return_value=runtime),
            patch.object(core, "_configure_windows_runtime_path"),
            patch.object(core, "_locate_npm_codex_cli", return_value=codex),
            patch.object(core, "_verify_codex_cli", return_value="codex-cli 1.2.3"),
            patch.object(core, "_sync_user_environment") as sync_environment,
            patch.object(core.subprocess, "run") as run,
        ):
            result = core.deploy_codex_runtime()
        run.assert_not_called()
        sync_environment.assert_not_called()
        self.assertFalse(result["installed"])
        self.assertEqual(result["version"], "codex-cli 1.2.3")

    def test_runtime_deploy_uses_official_native_package_when_node_and_npm_are_missing(self):
        cli = self.root / "agent-manager" / "runtime" / "codex" / "release" / "bin" / "codex.exe"
        cli.parent.mkdir(parents=True)
        cli.write_bytes(b"runtime")
        missing = {"available": False, "source": "missing", "command": []}
        ready = {"available": True, "source": "manager_managed", "command": [str(cli)]}
        with (
            patch.object(core, "_clear_unsafe_codex_cli_override", return_value=False),
            patch.object(core, "codex_runtime_status", side_effect=[missing, ready]),
            patch.object(core, "_discover_node_npm_runtime", return_value=None),
            patch.object(
                core,
                "_download_official_codex_runtime",
                return_value={
                    "path": str(cli),
                    "version": "codex-cli 1.2.3",
                    "downloaded": True,
                },
            ) as download,
            patch.object(core, "_sync_user_environment") as sync_environment,
            patch.object(core.shutil, "which") as which,
        ):
            result = core.deploy_codex_runtime()
        download.assert_called_once_with()
        sync_environment.assert_not_called()
        which.assert_not_called()
        self.assertTrue(result["installed"])
        self.assertEqual(result["method"], "official_native_package")
        self.assertEqual(result["runtime"], ready)

    def test_runtime_deploy_promotes_javascript_runtime_for_desktop_reuse(self):
        node = self.root / "nodejs" / "node.exe"
        npm = self.root / "nodejs" / "npm.cmd"
        cli_js = self.root / "npm-root" / "@openai" / "codex" / "bin" / "codex.js"
        codex = self.root / "npm" / "codex.cmd"
        runtime = {"node": str(node), "npm": str(npm), "npmCommand": [str(npm)]}
        javascript_status = {"available": True, "source": "npm_javascript", "command": [str(node), str(cli_js)]}
        ready_status = {"available": True, "source": "configured", "command": [str(codex)]}
        with (
            patch.object(core, "_clear_unsafe_codex_cli_override", return_value=False),
            patch.object(core, "codex_runtime_status", side_effect=[javascript_status, ready_status]),
            patch.object(core, "_discover_node_npm_runtime", return_value=runtime),
            patch.object(core, "_locate_npm_codex_cli", return_value=codex),
            patch.object(core, "_verify_codex_cli", return_value="codex-cli 1.2.3"),
            patch.object(core, "_configure_windows_runtime_path") as configure_runtime,
            patch.object(core, "_sync_user_environment") as sync_environment,
            patch.object(core.subprocess, "run") as run,
        ):
            result = core.deploy_codex_runtime()
        run.assert_not_called()
        configure_runtime.assert_called_once_with([node.parent])
        sync_environment.assert_not_called()
        self.assertFalse(result["installed"])
        self.assertEqual(result["runtime"], ready_status)

    @unittest.skipUnless(core.os.name == "nt", "Desktop wrapper regression is Windows-specific")
    def test_codex_prefix_ignores_cmd_override_and_uses_native_desktop_runtime(self):
        wrapper = self.root / "npm" / "codex.cmd"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text("@echo off", encoding="utf-8")
        desktop = self.root / "desktop" / "ChatGPT.exe"
        bundled = desktop.parent / "resources" / "codex.exe"
        bundled.parent.mkdir(parents=True)
        desktop.write_bytes(b"desktop")
        bundled.write_bytes(b"native")
        completed = subprocess.CompletedProcess([], 0, "codex-cli 0.147.0", "")
        with (
            patch.dict(core.os.environ, {"CODEX_CLI_PATH": str(wrapper)}, clear=False),
            patch.object(core.shutil, "which", return_value=None),
            patch.object(core, "_discover_node_npm_runtime", return_value=None),
            patch.object(core, "running_codex_processes", return_value=[]),
            patch.object(core, "_manager_downloaded_codex_candidates", return_value=[]),
            patch.object(core, "_desktop_managed_codex_candidates", return_value=[]),
            patch.object(core, "_detect_codex_windows_app", return_value={"executable": str(desktop)}),
            patch.object(core.subprocess, "run", return_value=completed),
        ):
            prefix = core.codex_prefix()
        self.assertEqual(prefix, [str(bundled.resolve())])

    def test_explicit_repair_only_removes_unsafe_cli_override_and_never_sets_one(self):
        wrapper = self.root / "npm" / "codex.cmd"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text("@echo off", encoding="utf-8")
        removed = []
        with (
            patch.dict(core.os.environ, {"CODEX_CLI_PATH": str(wrapper)}, clear=False),
            patch.object(core, "_user_environment_value", return_value=str(wrapper)),
            patch.object(core, "_remove_user_environment", side_effect=lambda name: removed.append(name)),
            patch.object(core, "_sync_user_environment") as sync_environment,
        ):
            self.assertTrue(core._clear_unsafe_codex_cli_override())
        self.assertEqual(removed, ["CODEX_CLI_PATH"])
        sync_environment.assert_not_called()

    def test_runtime_source_contains_no_codex_cli_path_assignment(self):
        source = Path(core.__file__).read_text(encoding="utf-8")
        self.assertNotIn('_sync_user_environment("CODEX_CLI_PATH"', source)
        self.assertNotIn('os.environ["CODEX_CLI_PATH"]', source)

    def test_environment_writer_rejects_codex_cli_path_even_through_dynamic_key(self):
        with self.assertRaisesRegex(core.ManagerError, "禁止写入 CODEX_CLI_PATH"):
            core._sync_user_environment("codex_cli_path", r"C:\npm\codex.cmd")

    def test_provider_cannot_use_codex_cli_path_as_its_api_key_environment(self):
        with self.assertRaisesRegex(core.ManagerError, "Codex 保留项"):
            core._validate_provider_env_key("CODEX_CLI_PATH")
        self.assertEqual(
            core._safe_imported_provider_env_key("CODEX_CLI_PATH", "RELAY_API_KEY"),
            "RELAY_API_KEY",
        )

    def test_launch_preflight_refuses_unsafe_cli_override_before_closing_desktop(self):
        wrapper = self.root / "npm" / "codex.cmd"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text("@echo off", encoding="utf-8")
        with (
            patch.dict(core.os.environ, {"CODEX_CLI_PATH": str(wrapper)}, clear=False),
            patch.object(core, "_user_environment_value", return_value=""),
            patch.object(core, "_detect_codex_windows_app") as detect_desktop,
        ):
            with self.assertRaisesRegex(core.ManagerError, "spawn EINVAL"):
                core.resolve_codex_launch_plan()
        detect_desktop.assert_not_called()

    def test_runtime_deploy_rejects_concurrent_install_attempt(self):
        self.assertTrue(core.CODEX_RUNTIME_DEPLOY_LOCK.acquire(blocking=False))
        try:
            with self.assertRaisesRegex(core.ManagerError, "正在扫描或部署"):
                core.deploy_codex_runtime()
        finally:
            core.CODEX_RUNTIME_DEPLOY_LOCK.release()

    def test_windows_app_launch_uses_valid_appx_id_and_verifies_process(self):
        process = {"name": "ChatGPT.exe", "pid": "200", "parentPid": "9"}
        observations = [[], process]

        def running():
            value = observations.pop(0) if observations else process
            return value if isinstance(value, list) else [value]

        completed = subprocess.CompletedProcess([], 0, "", "")
        plan = {
            "strategy": "windows_app",
            "appUserModelId": "OpenAI.Codex_2p2nqsd0c76g0!App",
            "executable": str(self.root / "ChatGPT.exe"),
            "package": "OpenAI.Codex_test",
        }
        with (
            patch.object(core.subprocess, "run", return_value=completed) as run,
            patch.object(core, "running_codex_processes", side_effect=running),
            patch.object(core.time, "sleep", return_value=None),
        ):
            result = core.launch_codex_app(launch_plan=plan)
        command = run.call_args.args[0]
        self.assertEqual(Path(command[0]).name.casefold(), "explorer.exe")
        self.assertEqual(command[1], "shell:AppsFolder\\OpenAI.Codex_2p2nqsd0c76g0!App")
        self.assertNotIn("com.openai.codex", " ".join(command).casefold())
        self.assertEqual(result["launchMethod"], "app_user_model_id")

    def test_valid_app_id_skips_slow_store_rescan_before_launch(self):
        process = {"name": "ChatGPT.exe", "pid": "200", "parentPid": "9"}
        completed = subprocess.CompletedProcess([], 0, "", "")
        plan = {
            "strategy": "windows_app",
            "appUserModelId": "OpenAI.Codex_2p2nqsd0c76g0!App",
            "executable": str(self.root / "removed-version" / "ChatGPT.exe"),
            "refreshBeforeLaunch": True,
        }
        with (
            patch.object(core.subprocess, "run", return_value=completed),
            patch.object(core, "_detect_codex_windows_app") as detect,
            patch.object(core, "running_codex_processes", return_value=[process]),
        ):
            result = core.launch_codex_app(launch_plan=plan)

        detect.assert_not_called()
        self.assertEqual(result["launchMethod"], "app_user_model_id")

    @unittest.skipUnless(core.os.name == "nt", "Windows App cache is Windows-specific")
    def test_windows_app_resolver_reuses_durable_record_without_store_scan(self):
        executable = self.root / "ChatGPT.exe"
        executable.write_bytes(b"test")
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        core.atomic_write_json(
            core.STATE_DIR / "codex-windows-app.json",
            {
                "appUserModelId": "OpenAI.Codex_2p2nqsd0c76g0!App",
                "executable": str(executable),
                "package": "OpenAI.Codex_test",
                "version": "1.0.0.0",
            },
        )
        with (
            patch.dict(core.CODEX_WINDOWS_APP_CACHE, {"at": 0.0, "value": None}, clear=True),
            patch.object(core.subprocess, "run", side_effect=AssertionError("must use durable cache")),
        ):
            record = core._detect_codex_windows_app()

        self.assertEqual(record["appUserModelId"], "OpenAI.Codex_2p2nqsd0c76g0!App")

    @unittest.skipUnless(core.os.name == "nt", "Desktop refresh is Windows-specific")
    def test_desktop_launch_refreshes_a_stale_versioned_path_before_starting(self):
        refreshed_executable = self.root / "current" / "ChatGPT.exe"
        refreshed_executable.parent.mkdir(parents=True)
        refreshed_executable.write_bytes(b"current")
        process = {"name": "ChatGPT.exe", "pid": "200", "parentPid": "9"}
        fake_child = type("Child", (), {"pid": 200})()
        plan = {
            "strategy": "desktop_executable",
            "executable": str(self.root / "removed-version" / "ChatGPT.exe"),
            "refreshBeforeLaunch": True,
        }
        with (
            patch.object(
                core,
                "_detect_codex_windows_app",
                return_value={"appUserModelId": "", "executable": str(refreshed_executable)},
            ) as detect,
            patch.object(core.subprocess, "Popen", return_value=fake_child) as popen,
            patch.object(core, "running_codex_processes", return_value=[process]),
        ):
            result = core.launch_codex_app(launch_plan=plan)
        detect.assert_called_once_with(force=True)
        self.assertEqual(popen.call_args.args[0], [str(refreshed_executable)])
        self.assertEqual(result["executable"], str(refreshed_executable))

    def test_windows_app_launch_rejects_legacy_shortcut_identifier(self):
        with self.assertRaisesRegex(core.ManagerError, "Windows App ID 无效"):
            core.launch_codex_app(
                launch_plan={"strategy": "windows_app", "appUserModelId": "com.openai.codex"}
            )

    def test_windows_direct_gui_fallback_never_uses_create_no_window(self):
        executable = self.root / "ChatGPT.exe"
        executable.write_bytes(b"gui")
        process = {"name": "ChatGPT.exe", "pid": "200", "parentPid": "9"}
        completed = subprocess.CompletedProcess([], 0, "", "")
        fake_child = type("Child", (), {"pid": 200})()
        plan = {
            "strategy": "windows_app",
            "appUserModelId": "OpenAI.Codex_2p2nqsd0c76g0!App",
            "executable": str(executable),
            "package": "OpenAI.Codex_test",
        }
        with (
            patch.object(core, "CODEX_WINDOWS_APP_START_TIMEOUT_SECONDS", 0),
            patch.object(core.subprocess, "run", return_value=completed),
            patch.object(core.subprocess, "Popen", return_value=fake_child) as popen,
            patch.object(core, "_detect_codex_windows_app", return_value=None),
            patch.object(core, "running_codex_processes", side_effect=[[], [process], [process]]),
        ):
            result = core.launch_codex_app(launch_plan=plan)
        flags = popen.call_args.kwargs["creationflags"]
        self.assertEqual(flags & getattr(subprocess, "CREATE_NO_WINDOW", 0), 0)
        self.assertEqual(result["launchMethod"], "current_appx_executable")

    def test_windows_app_fallbacks_share_one_start_deadline(self):
        executable = self.root / "ChatGPT.exe"
        executable.write_bytes(b"gui")
        completed = subprocess.CompletedProcess([], 0, "", "")
        clock = [0.0]

        class FakeChild:
            pid = 200

            @staticmethod
            def wait(timeout):
                return 0

        def sleep(seconds):
            clock[0] += seconds

        plan = {
            "strategy": "windows_app",
            "appUserModelId": "OpenAI.Codex_2p2nqsd0c76g0!App",
            "executable": str(executable),
            "package": "OpenAI.Codex_test",
        }
        with (
            patch.object(core, "CODEX_WINDOWS_APP_START_TIMEOUT_SECONDS", 15),
            patch.object(core.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(core.time, "sleep", side_effect=sleep),
            patch.object(core.subprocess, "run", return_value=completed),
            patch.object(core.subprocess, "Popen", return_value=FakeChild()) as popen,
            patch.object(core, "_detect_codex_windows_app", return_value=None),
            patch.object(core, "_recent_codex_workspace", return_value=self.root),
            patch.object(core, "running_codex_processes", return_value=[]),
        ):
            with self.assertRaisesRegex(core.ManagerError, "Windows App 启动失败"):
                core.launch_codex_app(command_prefix=["codex.exe"], launch_plan=plan)
        self.assertLessEqual(clock[0], 15.01)
        self.assertEqual(popen.call_count, 2)

    def test_runtime_readiness_rejects_a_different_chatgpt_account(self):
        with patch.object(
            core,
            "codex_app_server_requests",
            return_value=[
                {"account": {"type": "chatgpt", "email": "other@example.test"}},
                {"data": [{"id": "gpt-test"}]},
            ],
        ):
            with self.assertRaisesRegex(core.ManagerError, "并非刚切换"):
                core.wait_for_codex_runtime_ready("target@example.test", timeout_seconds=1)

    def test_runtime_readiness_retries_while_account_store_is_initializing(self):
        initializing = [
            [{"account": None}, {"data": [{"id": "gpt-test"}]}],
            [
                {"account": {"type": "chatgpt", "email": "target@example.test"}},
                {"data": [{"id": "gpt-test"}]},
            ],
        ]
        with (
            patch.object(core, "codex_app_server_requests", side_effect=initializing) as requests,
            patch.object(core.time, "sleep", return_value=None),
        ):
            result = core.wait_for_codex_runtime_ready(
                "target@example.test",
                "gpt-test",
                timeout_seconds=1,
            )
        self.assertTrue(result["ready"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(requests.call_count, 2)

    def test_runtime_readiness_checks_the_full_first_model_page(self):
        with patch.object(
            core,
            "codex_app_server_requests",
            return_value=[
                {"account": {"type": "chatgpt", "email": "target@example.test"}},
                {"data": [{"id": "gpt-first"}, {"id": "gpt-target"}]},
            ],
        ) as requests:
            result = core.wait_for_codex_runtime_ready(
                "target@example.test",
                "gpt-target",
                timeout_seconds=1,
            )
        self.assertTrue(result["ready"])
        self.assertEqual(requests.call_args.args[0][1][1]["limit"], 100)

    def test_runtime_readiness_follows_model_catalog_cursors_without_unbounded_scans(self):
        with (
            patch.object(
                core,
                "codex_app_server_requests",
                return_value=[
                    {"account": {"type": "chatgpt", "email": "target@example.test"}},
                    {"data": [{"id": "gpt-first"}], "nextCursor": "page-2"},
                ],
            ),
            patch.object(
                core,
                "codex_app_server_request",
                return_value={"data": [{"id": "gpt-target"}], "nextCursor": None},
            ) as page_request,
        ):
            result = core.wait_for_codex_runtime_ready(
                "target@example.test",
                "gpt-target",
                timeout_seconds=1,
            )
        self.assertTrue(result["ready"])
        self.assertEqual(result["modelsVisible"], 2)
        page_request.assert_called_once_with(
            "model/list",
            {"cursor": "page-2", "limit": 100},
            timeout=2,
        )

    def test_runtime_model_health_prefers_catalog_default_and_never_autofixes_while_codex_runs(self):
        settings = core._initial_settings()
        settings["accounts"] = [
            {
                "id": "model-account",
                "label": "Model",
                "groupId": "official",
                "models": ["gpt-old", "gpt-new"],
                "authMode": "chatgpt",
                "sourceType": "codex_auth",
                "codexCompatible": True,
            }
        ]
        settings["modelWorkspace"].update(
            {
                "activeSourceId": "account:model-account",
                "defaultModelKey": "account:model-account::gpt-old",
            }
        )
        self.root.mkdir(parents=True, exist_ok=True)
        core.CONFIG_FILE.write_text('model = "gpt-old"\n', encoding="utf-8")
        with (
            patch.object(core, "current_auth_state", return_value={"activeAccountId": "model-account"}),
            patch.object(core, "local_model_catalog", return_value=[]),
            patch.object(core, "running_codex_processes", return_value=[]),
            patch.object(
                core,
                "codex_app_server_request",
                return_value={"data": [{"id": "gpt-new", "isDefault": True}]},
            ),
        ):
            health = core.runtime_model_health(settings)
        self.assertEqual(health["candidateModel"], "gpt-new")
        self.assertEqual(health["candidateKey"], "account:model-account::gpt-new")
        self.assertTrue(health["recoverable"])

        with (
            patch.object(core, "current_auth_state", return_value={"activeAccountId": "model-account"}),
            patch.object(core, "local_model_catalog", return_value=[]),
            patch.object(core, "running_codex_processes", return_value=[{"pid": "7"}]),
            patch.object(
                core,
                "codex_app_server_request",
                return_value={"data": [{"id": "gpt-new", "isDefault": True}]},
            ),
        ):
            running_health = core.runtime_model_health(settings)
        self.assertFalse(running_health["recoverable"])
        self.assertIn("关闭 Codex", running_health["detail"])

    def test_runtime_model_health_never_repairs_from_cache_when_runtime_catalog_is_unavailable(self):
        settings = core._initial_settings()
        settings["accounts"] = [
            {
                "id": "model-account",
                "label": "Model",
                "groupId": "official",
                "models": ["gpt-cached"],
                "authMode": "chatgpt",
                "sourceType": "codex_auth",
                "codexCompatible": True,
            }
        ]
        settings["modelWorkspace"].update(
            {
                "activeSourceId": "account:model-account",
                "defaultModelKey": "account:model-account::gpt-cached",
            }
        )
        core.CONFIG_FILE.write_text('model = "gpt-old"\n', encoding="utf-8")
        with (
            patch.object(core, "current_auth_state", return_value={"activeAccountId": "model-account"}),
            patch.object(core, "running_codex_processes", return_value=[]),
            patch.object(core, "codex_app_server_request", side_effect=core.ManagerError("offline")),
        ):
            health = core.runtime_model_health(settings)
        self.assertTrue(health["healthy"])
        self.assertFalse(health["checked"])
        self.assertFalse(health["recoverable"])

    def test_runtime_model_repair_is_a_two_file_transaction_and_rolls_back_on_write_failure(self):
        settings = core._initial_settings()
        settings["accounts"] = [
            {
                "id": "model-account",
                "label": "Model",
                "groupId": "official",
                "models": ["gpt-old", "gpt-new"],
                "authMode": "chatgpt",
                "sourceType": "codex_auth",
                "codexCompatible": True,
            }
        ]
        settings["modelWorkspace"].update(
            {
                "activeSourceId": "account:model-account",
                "defaultModelKey": "account:model-account::gpt-old",
            }
        )
        core.save_settings(settings)
        core.CONFIG_FILE.write_text('model = "gpt-old"\n', encoding="utf-8")
        core.MODEL_CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        core.MODEL_CATALOG_FILE.write_text('{"keep": true}\n', encoding="utf-8")
        before_settings = core.SETTINGS_FILE.read_bytes()
        before_config = core.CONFIG_FILE.read_bytes()
        before_agents = core.AGENTS_FILE.read_bytes()
        before_catalog = core.MODEL_CATALOG_FILE.read_bytes()
        original_atomic_write_text = core.atomic_write_text

        def fail_config_write(path, content):
            if Path(path) == core.CONFIG_FILE:
                raise OSError("simulated config write failure")
            return original_atomic_write_text(path, content)

        with (
            patch.object(core, "current_auth_state", return_value={"activeAccountId": "model-account"}),
            patch.object(core, "atomic_write_text", side_effect=fail_config_write),
        ):
            with self.assertRaisesRegex(core.ManagerError, "已原样回滚"):
                core.repair_runtime_model_selection("account:model-account::gpt-new")
        self.assertEqual(core.SETTINGS_FILE.read_bytes(), before_settings)
        self.assertEqual(core.CONFIG_FILE.read_bytes(), before_config)
        self.assertEqual(core.AGENTS_FILE.read_bytes(), before_agents)
        self.assertEqual(core.MODEL_CATALOG_FILE.read_bytes(), before_catalog)

    def test_reopening_active_account_rolls_back_and_closes_unverified_runtime(self):
        settings = core._initial_settings()
        account = {"id": "target-account", "label": "target@example.test"}
        settings["accounts"] = [account]
        source = {
            "id": "account:target-account",
            "models": [{"id": "gpt-target", "key": "account:target-account::gpt-target"}],
        }
        self.root.mkdir(parents=True, exist_ok=True)
        core.CONFIG_FILE.write_text('model = "gpt-target"\n', encoding="utf-8")
        calls = []
        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "_prepare_account_switch_target", return_value=(account, {})),
            patch.object(core, "_account_model_source", return_value=source),
            patch.object(core, "_official_account_target_is_active", return_value=True),
            patch.object(core, "resolve_codex_launch_plan", return_value={"strategy": "windows_app"}),
            patch.object(core, "running_codex_processes", side_effect=lambda: [{"pid": "7"}] if "launch" in calls else []),
            patch.object(core, "launch_codex_app", side_effect=lambda **_kwargs: calls.append("launch") or {"started": True}),
            patch.object(core, "close_codex_processes", side_effect=lambda **_kwargs: calls.append("close") or {"closed": ["7"]}),
            patch.object(core, "wait_for_codex_runtime_ready", side_effect=core.ManagerError("model unavailable")),
        ):
            with self.assertRaisesRegex(core.ManagerError, "未再次启动 Codex"):
                core.switch_codex_account_and_launch("target-account")
        self.assertEqual(calls, ["launch", "close"])

    def test_switch_rollback_never_launches_codex_when_it_was_already_stopped(self):
        snapshot = {"files": {}, "environment": {}}
        with (
            patch.object(core, "_restore_switch_transaction_snapshot", return_value=[]),
            patch.object(core, "launch_codex_app") as launch,
        ):
            detail = core._rollback_failed_switch(
                snapshot,
                {"strategy": "windows_app"},
                closed={"requested": [], "closed": [], "alreadyStopped": True},
                launch_attempted=False,
            )
        launch.assert_not_called()
        self.assertEqual(detail, "；已原样回滚")

    def test_switch_rollback_relaunches_original_once_after_failed_runtime_crashes(self):
        snapshot = {"files": {}, "environment": {}}
        calls = []
        with (
            patch.object(core, "_restore_switch_transaction_snapshot", return_value=[]),
            patch.object(core, "running_codex_processes", return_value=[]),
            patch.object(core, "launch_codex_app", side_effect=lambda **_kwargs: calls.append("launch") or {"started": True}),
        ):
            detail = core._rollback_failed_switch(
                snapshot,
                {"strategy": "windows_app"},
                closed={"requested": ["7"], "closed": ["7"], "alreadyStopped": False},
                launch_attempted=True,
            )
        self.assertEqual(calls, ["launch"])
        self.assertEqual(detail, "；已原样回滚，并仅启动一次原 Codex")

    def test_chatgpt_import_auto_discovers_models_quota_and_expiry(self):
        access_token = self.jwt(
            {
                "exp": 2_000_000_000,
                "https://api.openai.com/auth": {"chatgpt_account_id": "workspace-test"},
            }
        )
        id_token = self.jwt(
            {
                "email": "person@example.com",
                "chatgpt_plan_type": "plus",
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "workspace-test",
                    "chatgpt_subscription_active_start": 1_990_000_000,
                    "chatgpt_subscription_active_until": 2_010_000_000,
                    "chatgpt_subscription_last_checked": 1_999_000_000,
                },
            }
        )
        auth = json.dumps(
            {
                "tokens": {
                    "access_token": access_token,
                    "id_token": id_token,
                    "refresh_token": "refresh-secret",
                    "account_id": "workspace-test",
                }
            }
        ).encode()

        def fetch(url, *_args, **_kwargs):
            if url == core.CHATGPT_USAGE_URL:
                return {
                    "plan_type": "pro",
                    "rate_limit": {
                        "allowed": True,
                        "limit_reached": False,
                        "primary_window": {
                            "used_percent": 23,
                            "limit_window_seconds": 18_000,
                            "reset_at": 2_000_000_100,
                        },
                        "secondary_window": {
                            "used_percent": 61,
                            "limit_window_seconds": 604_800,
                            "reset_after_seconds": 3600,
                        },
                    },
                }
            return {"models": [{"slug": "gpt-z"}, {"id": "gpt-a"}, "gpt-a"]}

        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "_fetch_chatgpt_json", side_effect=fetch),
        ):
            account = core.save_codex_account({"label": "Pro Account"}, auth_bytes=auth)
            self.assertEqual(account["plan"], "pro")
            self.assertEqual(account["models"], ["gpt-a", "gpt-z"])
            self.assertNotIn("hourly", account["usage"])
            self.assertEqual(account["usage"]["weekly"]["remainingPercent"], 39)
            self.assertEqual(account["usage"]["weekly"]["windowMinutes"], 10_080)
            self.assertEqual(account["refreshState"], "ready")
            self.assertTrue(account["tokenExpiresAt"])
            self.assertTrue(account["subscriptionExpiresAt"])
            self.assertTrue(account["subscriptionStartedAt"])
            self.assertTrue(account["importedAt"])
            public = json.dumps(core.public_state())
            self.assertNotIn(access_token, public)
            self.assertNotIn("refresh-secret", public)

    def test_public_account_records_keep_quota_but_hide_fingerprint(self):
        settings = core._initial_settings()
        settings["accounts"] = [
            {
                "id": "visible-quota",
                "label": "Official",
                "authMode": "chatgpt",
                "sourceType": "codex_auth",
                "fingerprint": "private-identity-fingerprint",
                "usage": {"weekly": {"remainingPercent": 84}},
            }
        ]

        records = core.public_account_records(settings)

        self.assertEqual(records[0]["usage"]["weekly"]["remainingPercent"], 84)
        self.assertNotIn("fingerprint", records[0])

    def test_expired_token_subscription_is_replaced_by_current_entitlement(self):
        past = (core.datetime.now(core.timezone.utc) - core.timedelta(days=1)).isoformat()
        current_entitlement = (core.datetime.now(core.timezone.utc) + core.timedelta(days=31)).replace(
            microsecond=0
        ).isoformat()
        access_token = self.jwt(
            {
                "exp": 2_000_000_000,
                "https://api.openai.com/auth": {"chatgpt_account_id": "workspace-current"},
            }
        )
        id_token = self.jwt(
            {
                "email": "current@example.com",
                "chatgpt_plan_type": "prolite",
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "workspace-current",
                    "chatgpt_subscription_active_until": past,
                },
            }
        )
        auth = json.dumps(
            {
                "tokens": {
                    "access_token": access_token,
                    "id_token": id_token,
                    "account_id": "workspace-current",
                }
            }
        ).encode()
        calls = []

        def fetch(url, *_args, **kwargs):
            calls.append((url, kwargs))
            if url == core.CHATGPT_USAGE_URL:
                return {"plan_type": "chatgptprolite", "rate_limit": {"allowed": True}}
            if url == core.CHATGPT_RESET_CREDITS_URL:
                return {"available_count": 0, "credits": []}
            if url == core.CHATGPT_MODELS_URL:
                return {"models": [{"slug": "gpt-5.6-sol"}]}
            if url == core.CHATGPT_ACCOUNTS_CHECK_URL:
                return {
                    "accounts": {
                        "org-current": {
                            "account": {
                                "account_id": "workspace-current",
                                "plan_type": "chatgptprolite",
                                "is_default": True,
                            },
                            "entitlement": {
                                "subscription_plan": "chatgptprolite",
                                "expires_at": past,
                            },
                        }
                    }
                }
            if url == core.CHATGPT_SUBSCRIPTIONS_URL:
                return {
                    "subscription_plan": "chatgptprolite",
                    "active_until": current_entitlement,
                }
            raise AssertionError(f"unexpected ChatGPT endpoint: {url}")

        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "_fetch_chatgpt_json", side_effect=fetch),
        ):
            account = core.save_codex_account({"label": "Current entitlement"}, auth_bytes=auth)

        expected = core._session_expiry(current_entitlement)
        self.assertEqual(account["subscriptionExpiresAt"], expected)
        self.assertEqual(account["usage"]["subscriptionExpiresAt"], expected)
        self.assertEqual(account["subscriptionMetadataSource"], "entitlement")
        self.assertEqual(account["subscriptionStatus"], "active")
        self.assertEqual(account["planLabel"], "Pro 5x")
        self.assertEqual(account["refreshState"], "partial")
        self.assertIn("usageQuota", account["refreshErrors"])
        self.assertIn(core.CHATGPT_ACCOUNTS_CHECK_URL, [url for url, _ in calls])
        self.assertIn(core.CHATGPT_SUBSCRIPTIONS_URL, [url for url, _ in calls])
        account_check_kwargs = next(kwargs for url, kwargs in calls if url == core.CHATGPT_ACCOUNTS_CHECK_URL)
        self.assertFalse(account_check_kwargs["include_account_id"])
        self.assertIn("x-openai-target-path", account_check_kwargs["extra_headers"])

    def test_chatgpt_probe_failure_keeps_account_and_reports_recovery_path(self):
        access_token = self.jwt(
            {"https://api.openai.com/auth": {"chatgpt_account_id": "workspace-test"}}
        )
        auth = json.dumps(
            {"tokens": {"access_token": access_token, "id_token": self.jwt({"email": "a@b.test"})}}
        ).encode()
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "_fetch_chatgpt_json", side_effect=core.ManagerError("网络不可用")),
        ):
            account = core.save_codex_account({"label": "Offline"}, auth_bytes=auth)
            self.assertEqual(account["refreshState"], "error")
            self.assertIn("usage", account["refreshErrors"])
            self.assertIn("models", account["refreshErrors"])
            self.assertEqual(len(core.load_settings()["accounts"]), 1)

    def test_models_probe_uses_installed_codex_semantic_version(self):
        with patch.object(core, "codex_version", return_value="codex-cli 0.147.0-alpha.1.2"):
            self.assertEqual(core._codex_client_version(), "0.147.0-alpha.1.2")
        with patch.object(core, "codex_version", return_value="Codex version unknown"):
            self.assertEqual(core._codex_client_version(), "0.1.0")

    def test_model_catalog_parser_accepts_nested_compatibility_envelopes_and_bounds_ids(self):
        payload = {
            "result": {
                "items": [
                    {"slug": "gpt-nested"},
                    {"id": "gpt-nested"},
                    {"name": "gpt-named"},
                    {"id": "bad\nmodel"},
                    {"id": "x" * (core.MAX_MODEL_ID_BYTES + 1)},
                ]
            }
        }

        self.assertEqual(core._parse_model_ids(payload), ["gpt-named", "gpt-nested"])
        self.assertEqual(
            core._provider_models_from_import({"models": {"mapped-model": {}}}),
            ["mapped-model"],
        )

    def test_history_sync_appends_safely_and_reports_divergence(self):
        local = self.root / "sessions" / "2026" / "task.jsonl"
        local.parent.mkdir(parents=True)
        local.write_bytes(b'{"step":1}\n')
        target = Path(self.temp.name) / "shared-history"
        payload = {"target": str(target), "direction": "two_way", "recentDays": 0}

        preview = core.preview_history_sync(payload)
        self.assertEqual(preview["counts"]["copy_to_remote"], 1)
        result = core.perform_history_sync(payload)
        remote = target / core.HISTORY_SYNC_FOLDER / "sessions" / "2026" / "task.jsonl"
        self.assertEqual(result["completed"], 1)
        self.assertEqual(remote.read_bytes(), local.read_bytes())

        local.write_bytes(local.read_bytes() + b'{"step":2}\n')
        preview = core.preview_history_sync(payload)
        self.assertEqual(preview["counts"]["replace_remote"], 1)
        core.perform_history_sync(payload)
        self.assertEqual(remote.read_bytes(), local.read_bytes())

        local.write_bytes(b'{"fork":"local"}\n')
        remote.write_bytes(b'{"fork":"remote"}\n')
        core.os.utime(local, (2, 2))
        core.os.utime(remote, (1, 1))
        conflict = core.preview_history_sync(payload)
        self.assertEqual(conflict["counts"]["conflict"], 1)
        before_local = local.read_bytes()
        before_remote = remote.read_bytes()
        result = core.perform_history_sync(payload)
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual(local.read_bytes(), before_local)
        self.assertEqual(remote.read_bytes(), before_remote)

        remote_new = target / core.HISTORY_SYNC_FOLDER / "sessions" / "2026" / "remote-only.jsonl"
        remote_new.write_bytes(b'{"remote":true}\n')
        local_new = self.root / "sessions" / "2026" / "remote-only.jsonl"
        with patch.object(core, "running_codex_processes", return_value=[{"name": "codex.exe", "pid": "42"}]):
            with self.assertRaisesRegex(core.ManagerError, "改用仅推送"):
                core.perform_history_sync({**payload, "direction": "pull"})
        self.assertFalse(local_new.exists())

    def test_session_actions_share_one_app_server_batch_and_rename(self):
        captured = []
        with patch.object(core, "codex_app_server_requests", side_effect=lambda requests, timeout=30, **kwargs: captured.append(requests) or [{"ok": True, "result": {}} for _ in requests]):
            result = core.manage_codex_threads("archive", ["thread-1", "thread-2"])
        self.assertEqual(result["changed"], 2)
        self.assertEqual([item[0] for item in captured[0]], ["thread/archive", "thread/archive"])
        with patch.object(core, "codex_app_server_request", return_value={}) as request:
            renamed = core.rename_codex_thread("thread-1", "  New   name  ")
        self.assertEqual(renamed["name"], "New name")
        request.assert_called_once_with(
            "thread/name/set",
            {"threadId": "thread-1", "name": "New name"},
            timeout=30,
        )

    def test_thread_rows_accept_current_string_status_enum(self):
        rows = core._codex_thread_rows(
            {
                "data": [
                    {
                        "id": "child-current-status",
                        "name": "Current protocol child",
                        "source": {"type": "subAgent"},
                        "status": "systemError",
                        "updatedAt": 1_788_000_000,
                    }
                ]
            },
            False,
        )
        self.assertEqual(rows[0]["status"]["type"], "systemError")

    def test_app_server_batch_reaps_reader_threads_and_pipe_handles(self):
        script = (
            "import json,sys\n"
            "for line in sys.stdin:\n"
            " request=json.loads(line)\n"
            " if 'id' not in request: continue\n"
            " result={} if request.get('method')=='initialize' else {'echo':request.get('method')}\n"
            " print(json.dumps({'id':request['id'],'result':result}),flush=True)\n"
        )
        real_popen = subprocess.Popen
        children = []

        def capture_process(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        with (
            patch.object(core, "codex_prefix", return_value=[sys.executable, "-u", "-c", script]),
            patch.object(core.subprocess, "Popen", side_effect=capture_process),
        ):
            result = core.codex_app_server_requests([("skills/list", {})], timeout=5)

        self.assertEqual(result, [{"echo": "skills/list"}])
        self.assertEqual(len(children), 1)
        child = children[0]
        self.assertIsNotNone(child.poll())
        self.assertTrue(child.stdin.closed)
        self.assertTrue(child.stdout.closed)
        self.assertTrue(child.stderr.closed)

    def test_app_server_batch_accepts_out_of_order_responses_and_preserves_result_order(self):
        script = (
            "import json,sys\n"
            "pending=[]\n"
            "for line in sys.stdin:\n"
            " request=json.loads(line)\n"
            " if request.get('method')=='initialize':\n"
            "  print(json.dumps({'id':request['id'],'result':{}}),flush=True)\n"
            " elif 'id' in request:\n"
            "  pending.append(request)\n"
            "  if len(pending)==2:\n"
            "   for item in reversed(pending):\n"
            "    print(json.dumps({'id':item['id'],'result':{'echo':item['method']}}),flush=True)\n"
            "   pending.clear()\n"
        )
        with patch.object(core, "codex_prefix", return_value=[sys.executable, "-u", "-c", script]):
            result = core.codex_app_server_requests(
                [("thread/read", {"threadId": "one"}), ("thread/read", {"threadId": "two"})],
                timeout=3,
            )
        self.assertEqual(result, [{"echo": "thread/read"}, {"echo": "thread/read"}])

    def test_session_listing_follows_app_server_cursors(self):
        first_active = {
            "data": [{"id": f"active-{index}", "name": f"A {index}"} for index in range(100)],
            "nextCursor": "active-next",
        }
        first_archived = {"data": [], "nextCursor": None}
        second_active = {"data": [{"id": "active-100", "name": "A 100"}], "nextCursor": None}
        calls = []

        def pages(requests, timeout=30):
            calls.append(requests)
            return [first_active, first_archived] if len(calls) == 1 else [second_active]

        with patch.object(core, "codex_app_server_requests", side_effect=pages):
            rows = core.list_codex_thread_groups(active_limit=500, archived_limit=500)
        self.assertEqual(len(rows), 101)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0][1]["cursor"], "active-next")

    def test_session_listing_stops_when_app_server_repeats_cursor(self):
        calls = []

        def pages(requests, timeout=30):
            calls.append(requests)
            if len(calls) == 1:
                return [
                    {"data": [{"id": "active-1"}], "nextCursor": "loop-cursor"},
                    {"data": [], "nextCursor": None},
                ]
            return [{"data": [{"id": "active-2"}], "nextCursor": "loop-cursor"}]

        with patch.object(core, "codex_app_server_requests", side_effect=pages):
            rows = core.list_codex_thread_groups(active_limit=500, archived_limit=500)

        self.assertEqual([item["id"] for item in rows], ["active-1", "active-2"])
        self.assertEqual(len(calls), 2)

    def test_web_session_import_group_proxy_and_switch_guard(self):
        access_token = self.jwt(
            {
                "exp": 2_000_000_000,
                "https://api.openai.com/auth": {"chatgpt_account_id": "web-session-workspace"},
            }
        )
        session = {
            "accessToken": access_token,
            "account": {"id": "web-session-workspace", "plan": "free"},
            "user": {"email": "web@example.test", "name": "Web User"},
            "authProvider": "openai",
            "subscriptionExpiresAt": 2_010_000_000,
        }

        def fetch(url, *_args, **_kwargs):
            if url == core.CHATGPT_USAGE_URL:
                return {"plan_type": "free", "rate_limit": {"allowed": True}}
            return {"models": [{"slug": "gpt-web"}]}

        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "_fetch_chatgpt_json", side_effect=fetch),
            patch.object(core, "running_codex_processes", return_value=[]),
        ):
            account = core.import_codex_account(
                {"authJson": json.dumps(session), "groupId": "free", "proxyEnabled": True}
            )
            self.assertEqual(account["sourceType"], "web_session")
            self.assertEqual(account["groupId"], "free")
            self.assertFalse(account["proxyEnabled"])
            self.assertFalse(account["codexCompatible"])
            self.assertTrue(account["quotaOnly"])
            self.assertEqual(account["email"], "web@example.test")
            self.assertIsNone(account["subscriptionExpiresAt"])
            self.assertEqual(account["subscriptionStatus"], "not_applicable")
            self.assertTrue(account["importedAt"])
            self.assertNotIn(account["id"], core.load_settings()["web2api"]["accountIds"])
            with self.assertRaisesRegex(core.ManagerError, "只能查询额度"):
                core.switch_codex_account(account["id"])

    def test_sub2api_web_session_is_locally_converted_and_capability_probed(self):
        access_token = self.jwt(
            {
                "exp": 2_000_000_000,
                "client_id": "browser-session-client",
                "https://api.openai.com/auth": {"chatgpt_account_id": "sub2api-workspace"},
            }
        )
        bundle = {
            "exported_at": core.now_iso(),
            "accounts": [
                {
                    "name": "Converted Plus",
                    "platform": "openai",
                    "type": "oauth",
                    "credentials": {
                        "access_token": access_token,
                        "chatgpt_account_id": "sub2api-workspace",
                        "email": "converted@example.test",
                        "plan_type": "plus",
                    },
                }
            ],
        }
        preview = core.preview_codex_accounts_batch(
            {"groupId": "official", "items": [{"authJson": bundle}]}
        )
        self.assertEqual(preview["valid"], 1)
        self.assertEqual(preview["items"][0]["sourceType"], "web_session")
        self.assertTrue(preview["items"][0]["compatibilityPending"])
        self.assertNotIn(access_token, json.dumps(preview))

        def fetch(url, *_args, **_kwargs):
            if url == core.CHATGPT_USAGE_URL:
                return {"plan_type": "plus", "rate_limit": {"allowed": True}}
            if url == core.CHATGPT_MODELS_URL:
                return {"models": [{"slug": "gpt-test"}]}
            return {"available_count": 0, "credits": []}

        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "_fetch_chatgpt_json", side_effect=fetch),
            patch.object(
                core,
                "_fetch_chatgpt_subscription_status",
                return_value={"subscriptionStatus": "unknown", "subscriptionLastCheckedAt": core.now_iso()},
            ),
            patch.object(
                core,
                "_probe_chatgpt_codex_access",
                return_value={"compatible": True, "status": 400, "checkedAt": core.now_iso(), "method": "test"},
            ),
        ):
            imported = core.import_codex_accounts_batch(
                {"groupId": "official", "proxyEnabled": True, "items": [{"authJson": bundle}]}
            )["imported"][0]
        self.assertTrue(imported["codexCompatible"])
        self.assertFalse(imported["quotaOnly"])
        self.assertTrue(imported["proxyEnabled"])
        self.assertEqual(imported["credentialCapability"], "codex_short_lived")

    def test_web_session_conversion_requires_local_gateway_not_native_auth_projection(self):
        access_token = self.jwt(
            {
                "exp": 2_000_000_000,
                "https://api.openai.com/auth": {"chatgpt_account_id": "cli-web-session"},
            }
        )
        session = {
            "accessToken": access_token,
            "account": {"id": "cli-web-session", "plan": "free"},
            "user": {"email": "cli-web@example.test"},
        }
        auth_bytes, source_type = core._normalize_import_auth_payload(session)
        self.assertEqual(source_type, "web_session")
        auth_payload = json.loads(auth_bytes)
        self.assertNotIn("auth_mode", auth_payload)
        self.assertTrue(auth_payload["tokens"]["id_token"].endswith(".synthetic"))
        with self.assertRaisesRegex(core.ManagerError, "不是可持久使用"):
            core._codex_auth_projection_bytes(auth_bytes)

    @unittest.skipUnless(shutil.which("codex"), "Codex CLI is not installed")
    def test_agent_identity_record_projection_is_parseable_by_real_codex_cli(self):
        record = {
            "agent_runtime_id": "runtime-cli-test",
            "agent_private_key": self.agent_private_key(9),
            "account_id": "agent-cli-account",
            "chatgpt_user_id": "agent-cli-user",
            "email": "agent-cli@example.test",
            "plan_type": "free",
            "chatgpt_account_is_fedramp": False,
            "task_id": "task-cli-test",
        }
        encoded, _ = core._normalize_import_auth_payload(
            {"auth_mode": "agentIdentity", "agent_identity": record}
        )
        (self.root / "auth.json").write_bytes(core._codex_auth_projection_bytes(encoded))
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(self.root)
        result = subprocess.run(
            [shutil.which("codex") or "codex", "login", "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("logged in", f"{result.stdout}\n{result.stderr}".casefold())

    def test_batch_import_accepts_accounts_array_and_custom_group(self):
        core.ensure_state()
        group = core.save_account_group({"name": "测试组", "color": "violet"})
        sessions = []
        for index in range(2):
            sessions.append(
                {
                    "accessToken": self.jwt(
                        {
                            "exp": 2_000_000_000,
                            "https://api.openai.com/auth": {"chatgpt_account_id": f"batch-{index}"},
                        }
                    ),
                    "account": {"id": f"batch-{index}"},
                    "user": {"email": f"batch-{index}@example.test"},
                }
            )
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "_fetch_chatgpt_json", side_effect=core.ManagerError("offline")),
            patch.object(
                core,
                "_probe_chatgpt_codex_access",
                return_value={"compatible": True, "status": 400, "checkedAt": core.now_iso(), "method": "test"},
            ),
        ):
            result = core.import_codex_accounts_batch(
                {"items": [{"authJson": {"accounts": sessions}}], "groupId": group["id"]}
            )
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["imported"]), 2)
        self.assertFalse(result["failed"])
        self.assertTrue(all(item["groupId"] == group["id"] for item in result["imported"]))
        self.assertEqual(
            {item["label"] for item in result["imported"]},
            {"batch-0@example.test", "batch-1@example.test"},
        )

    def test_batch_import_splits_concatenated_json_and_web_sessions(self):
        sessions = []
        for index in range(2):
            sessions.append(
                {
                    "accessToken": self.jwt(
                        {
                            "exp": 2_000_000_000,
                            "https://api.openai.com/auth": {"chatgpt_account_id": f"concat-{index}"},
                        }
                    ),
                    "account": {"id": f"concat-{index}"},
                    "user": {"email": f"concat-{index}@example.test"},
                }
            )
        pasted = "\n".join(json.dumps(item) for item in sessions)
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "_fetch_chatgpt_json", side_effect=core.ManagerError("offline")),
        ):
            result = core.import_codex_accounts_batch({"groupId": "free", "items": [{"authJson": pasted}]})
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["imported"]), 2)
        self.assertTrue(all(item["sourceType"] == "web_session" for item in result["imported"]))

    def test_import_parser_recognizes_common_converter_schemas(self):
        def credentials(index, *, placeholder=False):
            access = self.jwt(
                {
                    "exp": 2_000_000_000,
                    "client_id": core.CODEX_OAUTH_CLIENT_ID,
                    "email": f"format-{index}@example.test",
                    "https://api.openai.com/auth": {"chatgpt_account_id": f"format-{index}"},
                }
            )
            identity = self.jwt(
                {
                    "exp": 2_000_000_000,
                    "client_id": core.CODEX_OAUTH_CLIENT_ID,
                    "email": f"format-{index}@example.test",
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": f"format-{index}",
                        "chatgpt_plan_type": "plus",
                    },
                }
            )
            return access, identity, "placeholder" if placeholder else f"refresh-{index}"

        cpa = credentials(0)
        sub2api = credentials(1)
        router = credentials(2)
        manager = credentials(3)
        axon = credentials(4, placeholder=True)
        payload = {
            "backup": {
                "records": [
                    {
                        "type": "codex",
                        "access_token": cpa[0],
                        "id_token": cpa[1],
                        "refresh_token": cpa[2],
                        "account_id": "format-0",
                        "email": "format-0@example.test",
                    },
                    {
                        "credentials": {
                            "access_token": sub2api[0],
                            "id_token": sub2api[1],
                            "refresh_token": sub2api[2],
                            "chatgpt_account_id": "format-1",
                        },
                        "extra": {"email": "format-1@example.test", "plan": "plus"},
                    },
                    {
                        "provider": "codex",
                        "accessToken": router[0],
                        "idToken": router[1],
                        "refreshToken": router[2],
                        "providerSpecificData": {
                            "chatgpt_account_id": "format-2",
                            "email": "format-2@example.test",
                        },
                    },
                    {
                        "tokens": {
                            "accessToken": manager[0],
                            "idToken": manager[1],
                            "refreshToken": manager[2],
                            "accountId": "format-3",
                        },
                        "meta": {"email": "format-3@example.test", "plan": "plus"},
                    },
                    {
                        "tokens": {
                            "access_token": axon[0],
                            "id_token": axon[1],
                            "refresh_token": axon[2],
                            "account_id": "format-4",
                        }
                    },
                ]
            }
        }
        preview = core.preview_codex_accounts_batch(
            {"groupId": "official", "items": [{"authJson": payload}]}
        )
        self.assertEqual(preview["total"], 5)
        self.assertEqual(preview["valid"], 5)
        self.assertEqual(
            [item["importFormat"] for item in preview["items"]],
            [
                "CPA / Cockpit",
                "Sub2API / credentials",
                "9Router",
                "Codex Manager",
                "AxonHub / auth.json",
            ],
        )
        self.assertTrue(all(item["codexCompatible"] for item in preview["items"][:4]))
        self.assertEqual(preview["items"][4]["sourceType"], "web_session")
        self.assertNotIn(cpa[0], json.dumps(preview))

    def test_import_parser_tolerates_noise_markdown_damage_and_double_encoding(self):
        sessions = []
        for index in range(2):
            sessions.append(
                {
                    "accessToken": self.jwt(
                        {
                            "exp": 2_000_000_000,
                            "https://api.openai.com/auth": {"chatgpt_account_id": f"noise-{index}"},
                        }
                    ),
                    "account": {"id": f"noise-{index}", "plan": "free"},
                    "user": {"email": f"noise-{index}@example.test"},
                }
            )
        noisy = (
            "以下内容仅供导入，不是账号的一部分：\n```json\n"
            + json.dumps(sessions[0])
            + "\n```\n{ this fragment is broken }\n请继续读取下一段：\n"
            + json.dumps(sessions[1])
            + "\n--- end ---"
        )
        preview = core.preview_codex_accounts_batch(
            {"groupId": "free", "items": [{"authJson": noisy}]}
        )
        self.assertEqual(preview["valid"], 2)
        self.assertEqual({item["email"] for item in preview["items"]}, {
            "noise-0@example.test",
            "noise-1@example.test",
        })

        double_encoded = json.dumps(json.dumps({"payload": {"accounts": sessions}}))
        decoded_preview = core.preview_codex_accounts_batch(
            {"groupId": "free", "items": [{"authJson": double_encoded}]}
        )
        self.assertEqual(decoded_preview["valid"], 2)

        key_value = "\n".join(
            [
                f"access_token={sessions[0]['accessToken']}",
                "account_id=noise-0",
                "email=noise-0@example.test",
                "plan=free",
            ]
        )
        key_value_preview = core.preview_codex_accounts_batch(
            {"groupId": "free", "items": [{"authJson": key_value}]}
        )
        self.assertEqual(key_value_preview["valid"], 1)
        self.assertEqual(key_value_preview["items"][0]["email"], "noise-0@example.test")

    def test_session_cookie_is_never_promoted_to_oauth_refresh_token(self):
        with self.assertRaisesRegex(core.ManagerError, "sessionToken.*不能伪装"):
            core._normalize_import_auth_payload(
                {
                    "sessionToken": "browser-cookie-only",
                    "user": {"email": "cookie@example.test"},
                }
            )

    def test_large_batch_commits_settings_and_secrets_once_and_skips_duplicates(self):
        core.ensure_state()
        core.load_settings()
        sessions = []
        for index in range(250):
            sessions.append(
                {
                    "accessToken": self.jwt(
                        {
                            "exp": 2_000_000_000,
                            "https://api.openai.com/auth": {"chatgpt_account_id": f"bulk-{index}"},
                        }
                    ),
                    "account": {"id": f"bulk-{index}", "plan": "free"},
                    "user": {"email": f"bulk-{index}@example.test"},
                }
            )
        sessions.append(json.loads(json.dumps(sessions[0])))
        writes = []
        original_atomic_write_json = core.atomic_write_json

        def tracked_write(path, payload):
            if path in {core.SETTINGS_FILE, core.SECRETS_FILE}:
                writes.append(path)
            return original_atomic_write_json(path, payload)

        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "atomic_write_json", side_effect=tracked_write),
        ):
            result = core.import_codex_accounts_batch(
                {
                    "groupId": "free",
                    "deferRefresh": True,
                    "items": [{"authJson": {"data": {"accounts": sessions}}}],
                }
            )
        self.assertEqual(len(result["imported"]), 250)
        self.assertEqual(len(result["skippedDuplicates"]), 1)
        self.assertFalse(result["failed"])
        self.assertEqual(writes.count(core.SECRETS_FILE), 1)
        self.assertEqual(writes.count(core.SETTINGS_FILE), 1)
        self.assertEqual(len(core.load_settings()["accounts"]), 250)

    def test_team_members_with_shared_workspace_id_are_not_batch_duplicates(self):
        core.ensure_state()

        def team_auth(email, subject, suffix):
            claims = {
                "sub": subject,
                "email": email,
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "shared-team-workspace",
                    "chatgpt_plan_type": "team",
                },
            }
            return {
                "tokens": {
                    "access_token": self.jwt({**claims, "exp": 2_000_000_000}),
                    "id_token": self.jwt(claims),
                    "refresh_token": f"refresh-{suffix}",
                    "account_id": "shared-team-workspace",
                }
            }

        accounts = [
            team_auth("first@example.test", "user-first", "first"),
            team_auth("second@example.test", "user-second", "second"),
            team_auth("third@example.test", "user-third", "third"),
        ]
        preview = core.preview_codex_accounts_batch(
            {"groupId": "free", "items": [{"authJson": value} for value in accounts]}
        )
        self.assertEqual(preview["unique"], 3)
        self.assertEqual(preview["duplicatesInBatch"], 0)
        self.assertTrue(all(not item["duplicateInBatch"] for item in preview["items"]))

        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            result = core.import_codex_accounts_batch(
                {
                    "groupId": "free",
                    "deferRefresh": True,
                    "items": [{"authJson": value} for value in accounts],
                }
            )
        self.assertEqual(len(result["imported"]), 3)
        self.assertFalse(result["skippedDuplicates"])
        self.assertEqual(len({item["fingerprint"] for item in result["imported"]}), 3)
        self.assertEqual(
            {item["email"] for item in core.load_settings()["accounts"]},
            {"first@example.test", "second@example.test", "third@example.test"},
        )

    def test_wrapped_team_members_keep_outer_email_for_batch_identity(self):
        core.ensure_state()

        def wrapped_team_auth(email, suffix):
            shared_claims = {
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "shared-wrapper-workspace",
                    "chatgpt_plan_type": "team",
                }
            }
            return {
                "email": email,
                "authJson": {
                    "tokens": {
                        "access_token": self.jwt({**shared_claims, "exp": 2_000_000_000}),
                        "id_token": self.jwt(shared_claims),
                        "refresh_token": f"refresh-{suffix}",
                        "account_id": "shared-wrapper-workspace",
                    }
                },
            }

        accounts = [
            wrapped_team_auth("outer-first@example.test", "first"),
            wrapped_team_auth("outer-second@example.test", "second"),
            wrapped_team_auth("outer-third@example.test", "third"),
        ]
        preview = core.preview_codex_accounts_batch(
            {"groupId": "free", "items": [{"authJson": accounts}]}
        )
        self.assertEqual(preview["unique"], 3)
        self.assertEqual(preview["duplicatesInBatch"], 0)
        self.assertEqual(
            {item["email"] for item in preview["items"]},
            {
                "outer-first@example.test",
                "outer-second@example.test",
                "outer-third@example.test",
            },
        )

        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            result = core.import_codex_accounts_batch(
                {
                    "groupId": "free",
                    "deferRefresh": True,
                    "items": [{"authJson": accounts}],
                }
            )
        self.assertEqual(len(result["imported"]), 3)
        self.assertFalse(result["skippedDuplicates"])

    def test_legacy_team_snapshot_fingerprint_remains_readable_for_same_email(self):
        auth = {
            "tokens": {
                "access_token": self.jwt(
                    {
                        "sub": "legacy-user",
                        "email": "legacy@example.test",
                        "exp": 2_000_000_000,
                        "https://api.openai.com/auth": {
                            "chatgpt_account_id": "legacy-shared-team"
                        },
                    }
                ),
                "id_token": self.jwt(
                    {
                        "sub": "legacy-user",
                        "email": "legacy@example.test",
                        "https://api.openai.com/auth": {
                            "chatgpt_account_id": "legacy-shared-team"
                        },
                    }
                ),
                "refresh_token": "legacy-refresh",
                "account_id": "legacy-shared-team",
            }
        }
        auth_bytes = json.dumps(auth).encode("utf-8")
        identity = core._identity_from_auth_bytes(auth_bytes)
        self.assertEqual(len(identity["legacyFingerprints"]), 1)
        snapshot = {
            "version": 1,
            "files": [
                {
                    "name": "auth.json",
                    "present": True,
                    "content": base64.b64encode(auth_bytes).decode("ascii"),
                },
                {"name": "cap_sid", "present": False, "content": ""},
            ],
            "fingerprint": identity["legacyFingerprints"][0],
        }
        decoded = core._decode_snapshot_files(snapshot)
        self.assertEqual(decoded["auth.json"], auth_bytes)
        self.assertTrue(
            core._account_matches_identity(
                {
                    "fingerprint": identity["legacyFingerprints"][0],
                    "email": "legacy@example.test",
                },
                identity,
            )
        )
        self.assertFalse(
            core._account_matches_identity(
                {
                    "fingerprint": identity["legacyFingerprints"][0],
                    "email": "someone-else@example.test",
                },
                identity,
            )
        )

    def test_bulk_import_rolls_back_secret_store_when_settings_commit_fails(self):
        core.ensure_state()
        core.load_settings()
        settings_before = core.SETTINGS_FILE.read_bytes()
        secret_existed = core.SECRETS_FILE.exists()
        secret_before = core.SECRETS_FILE.read_bytes() if secret_existed else None
        session = {
            "accessToken": self.jwt(
                {
                    "exp": 2_000_000_000,
                    "https://api.openai.com/auth": {"chatgpt_account_id": "rollback-batch"},
                }
            ),
            "account": {"id": "rollback-batch", "plan": "free"},
            "user": {"email": "rollback@example.test"},
        }
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "save_settings", side_effect=core.ManagerError("simulated settings failure")),
        ):
            with self.assertRaisesRegex(core.ManagerError, "simulated settings failure"):
                core.import_codex_accounts_batch(
                    {
                        "groupId": "free",
                        "deferRefresh": True,
                        "items": [{"authJson": session}],
                    }
                )
        self.assertEqual(core.SETTINGS_FILE.read_bytes(), settings_before)
        self.assertEqual(core.SECRETS_FILE.exists(), secret_existed)
        if secret_before is not None:
            self.assertEqual(core.SECRETS_FILE.read_bytes(), secret_before)

    def test_import_parse_errors_never_echo_credentials(self):
        secret = "sk-sensitive-import-value-123456789"
        with self.assertRaises(core.ManagerError) as captured:
            core.preview_codex_accounts_batch(
                {
                    "groupId": "official",
                    "items": [{"authJson": f"{{ broken: '{secret}' }}"}],
                }
            )
        self.assertNotIn(secret, str(captured.exception))

    def test_single_account_export_is_directly_reimportable(self):
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            account = core.import_codex_account(
                {"authJson": {"OPENAI_API_KEY": "sk-test-export"}, "groupId": "official", "label": "可移植账号"}
            )
            exported = core.export_codex_account(account["id"])
            imported = core.import_codex_accounts_batch(
                {"groupId": "free", "items": [{"authJson": json.dumps(exported)}]}
            )
        self.assertTrue(exported["containsSecrets"])
        self.assertEqual(exported["format"], "codex-agent-manager-account")
        self.assertEqual(len(imported["imported"]), 1)
        self.assertEqual(imported["imported"][0]["groupId"], "free")

    def test_cockpit_account_import_preserves_optional_model_catalog(self):
        cockpit = {
            "type": "codex",
            "email": "portable@example.test",
            "models": ["gpt-portable-a", {"id": "gpt-portable-b"}],
            "tokens": {
                "id_token": self.jwt(
                    {
                        "email": "portable@example.test",
                        "https://api.openai.com/auth": {"chatgpt_account_id": "portable-account"},
                    }
                ),
                "access_token": self.jwt(
                    {"https://api.openai.com/auth": {"chatgpt_account_id": "portable-account"}}
                ),
                "refresh_token": "refresh-portable",
                "account_id": "portable-account",
            },
        }
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            imported = core.import_codex_accounts_batch(
                {
                    "groupId": "official",
                    "deferRefresh": True,
                    "items": [{"authJson": cockpit}],
                }
            )
            exported = core.export_codex_account(imported["imported"][0]["id"])
        self.assertEqual(
            imported["imported"][0]["models"],
            ["gpt-portable-a", "gpt-portable-b"],
        )
        self.assertEqual(exported["codex_agent_manager"]["models"], ["gpt-portable-a", "gpt-portable-b"])

    def test_account_export_saves_to_downloads_without_overwriting(self):
        downloads = self.root / "Downloads"
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "_windows_downloads_directory", return_value=downloads),
        ):
            account = core.import_codex_account(
                {
                    "authJson": {"OPENAI_API_KEY": "sk-download-export"},
                    "groupId": "official",
                    "label": "测试/账号:*?",
                }
            )
            first = core.export_codex_account_to_downloads(account["id"])
            second = core.export_codex_account_to_downloads(account["id"])
        first_path = Path(first["path"])
        second_path = Path(second["path"])
        self.assertEqual(first_path.parent, downloads)
        self.assertEqual(second_path.parent, downloads)
        self.assertNotEqual(first_path, second_path)
        self.assertNotIn("/", first_path.name)
        self.assertEqual(json.loads(first_path.read_text(encoding="utf-8"))["format"], "codex-agent-manager-account")

    def test_import_preview_hides_secrets_and_imports_only_selected_accounts(self):
        sessions = []
        for index in range(2):
            sessions.append(
                {
                    "accessToken": self.jwt(
                        {
                            "exp": 2_000_000_000,
                            "https://api.openai.com/auth": {"chatgpt_account_id": f"preview-{index}"},
                        }
                    ),
                    "account": {"id": f"preview-{index}", "plan": "plus"},
                    "user": {"email": f"preview-{index}@example.test"},
                }
            )
        pasted = "\n".join(json.dumps(item) for item in sessions)
        preview = core.preview_codex_accounts_batch(
            {"groupId": "official", "items": [{"authJson": pasted}]}
        )
        self.assertEqual(preview["total"], 2)
        self.assertEqual(preview["valid"], 2)
        encoded_preview = json.dumps(preview)
        self.assertNotIn(sessions[0]["accessToken"], encoded_preview)
        self.assertNotIn(sessions[1]["accessToken"], encoded_preview)
        invalid_cap = core.preview_codex_accounts_batch(
            {
                "groupId": "official",
                "items": [{"authJson": {"OPENAI_API_KEY": "sk-preview"}, "capSidBase64": "not-base64"}],
            }
        )
        self.assertFalse(invalid_cap["items"][0]["valid"])
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "_fetch_chatgpt_json", side_effect=core.ManagerError("offline")),
            patch.object(
                core,
                "_probe_chatgpt_codex_access",
                return_value={"compatible": True, "status": 400, "checkedAt": core.now_iso(), "method": "test"},
            ),
        ):
            result = core.import_codex_accounts_batch(
                {
                    "groupId": "official",
                    "proxyEnabled": True,
                    "selectedIndices": [1],
                    "items": [{"authJson": pasted}],
                }
            )
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["imported"][0]["email"], "preview-1@example.test")
        self.assertTrue(result["imported"][0]["proxyEnabled"])
        self.assertEqual(result["imported"][0]["credentialCapability"], "codex_short_lived")

    def test_import_preview_excludes_only_definitive_remote_credential_failures(self):
        sessions = []
        for suffix in ("ok", "401", "402", "429"):
            sessions.append(
                {
                    "accessToken": self.jwt(
                        {
                            "exp": 2_000_000_000,
                            "sub": f"preview-user-{suffix}",
                            "https://api.openai.com/auth": {
                                "chatgpt_account_id": f"preview-remote-{suffix}"
                            },
                        }
                    ),
                    "account": {"id": f"preview-remote-{suffix}", "plan": "plus"},
                    "user": {"email": f"preview-{suffix}@example.test"},
                }
            )

        def fetch_usage(_url, _token, account_id, *_args, **_kwargs):
            if account_id.endswith("-401"):
                raise core.ManagerError("远端接口返回 HTTP 401：token invalidated")
            if account_id.endswith("-402"):
                raise core.ManagerError("远端接口返回 HTTP 402：deactivated_workspace")
            if account_id.endswith("-429"):
                raise core.ManagerError("远端接口返回 HTTP 429：rate limited")
            return {"plan_type": "plus"}

        with patch.object(core, "_fetch_chatgpt_json", side_effect=fetch_usage):
            preview = core.preview_codex_accounts_batch(
                {
                    "groupId": "official",
                    "validateRemote": True,
                    "items": [{"authJson": "\n".join(json.dumps(item) for item in sessions)}],
                }
            )

        by_email = {item["email"]: item for item in preview["items"]}
        self.assertTrue(by_email["preview-ok@example.test"]["valid"])
        self.assertEqual(by_email["preview-ok@example.test"]["remoteValidation"], "valid")
        for suffix in ("401", "402"):
            item = by_email[f"preview-{suffix}@example.test"]
            self.assertFalse(item["valid"])
            self.assertTrue(item["credentialInvalid"])
            self.assertIn(f"HTTP {suffix}", item["error"])
        transient = by_email["preview-429@example.test"]
        self.assertTrue(transient["valid"])
        self.assertEqual(transient["remoteValidation"], "unverified")
        self.assertIn("未自动排除", transient["warning"])
        self.assertEqual(preview["remoteInvalid"], 2)
        self.assertEqual(preview["valid"], 2)

    def test_import_preview_checks_a_duplicate_credential_only_once(self):
        session = {
            "accessToken": self.jwt(
                {
                    "exp": 2_000_000_000,
                    "sub": "preview-duplicate-user",
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": "preview-duplicate-workspace"
                    },
                }
            ),
            "account": {"id": "preview-duplicate-workspace", "plan": "plus"},
            "user": {"email": "preview-duplicate@example.test"},
        }
        with patch.object(core, "_fetch_chatgpt_json", return_value={}) as fetch_usage:
            preview = core.preview_codex_accounts_batch(
                {
                    "groupId": "official",
                    "validateRemote": True,
                    "items": [{"authJson": json.dumps([session, session])}],
                }
            )
        self.assertEqual(fetch_usage.call_count, 1)
        self.assertEqual(preview["total"], 2)
        self.assertFalse(preview["items"][0]["duplicateInBatch"])
        self.assertTrue(preview["items"][1]["duplicateInBatch"])
        self.assertEqual(preview["items"][0]["remoteValidation"], "valid")
        self.assertEqual(preview["items"][1]["remoteValidation"], "valid")

    def test_import_preview_allows_fresh_replacement_after_stale_token_for_same_account(self):
        def session(version):
            return {
                "accessToken": self.jwt(
                    {
                        "exp": 2_000_000_000,
                        "jti": version,
                        "sub": "preview-replacement-user",
                        "https://api.openai.com/auth": {
                            "chatgpt_account_id": "preview-replacement-workspace"
                        },
                    }
                ),
                "account": {"id": "preview-replacement-workspace", "plan": "plus"},
                "user": {"email": "preview-replacement@example.test"},
            }

        def fetch_usage(_url, token, _account_id, *_args, **_kwargs):
            if core._jwt_payload(token).get("jti") == "stale":
                raise core.ManagerError("远端接口返回 HTTP 401：token invalidated")
            return {}

        with patch.object(core, "_fetch_chatgpt_json", side_effect=fetch_usage) as fetch:
            preview = core.preview_codex_accounts_batch(
                {
                    "groupId": "official",
                    "validateRemote": True,
                    "items": [
                        {
                            "authJson": json.dumps(
                                [session("stale"), session("fresh")]
                            )
                        }
                    ],
                }
            )
        self.assertEqual(fetch.call_count, 2)
        self.assertFalse(preview["items"][0]["valid"])
        self.assertTrue(preview["items"][1]["valid"])
        self.assertFalse(preview["items"][1]["duplicateInBatch"])
        self.assertEqual(preview["duplicatesInBatch"], 0)

    def test_import_preview_stops_scheduling_after_remote_outage(self):
        sessions = []
        for index in range(5):
            sessions.append(
                {
                    "accessToken": self.jwt(
                        {
                            "exp": 2_000_000_000,
                            "sub": f"preview-outage-user-{index}",
                            "https://api.openai.com/auth": {
                                "chatgpt_account_id": f"preview-outage-{index}"
                            },
                        }
                    ),
                    "account": {"id": f"preview-outage-{index}", "plan": "plus"},
                    "user": {"email": f"preview-outage-{index}@example.test"},
                }
            )
        with patch.object(
            core,
            "_fetch_chatgpt_json",
            side_effect=core.ManagerError("远端接口返回 HTTP 429：rate limited"),
        ) as fetch:
            preview = core.preview_codex_accounts_batch(
                {
                    "groupId": "official",
                    "validateRemote": True,
                    "items": [{"authJson": json.dumps(sessions)}],
                }
            )
        self.assertLessEqual(fetch.call_count, 2)
        self.assertEqual(preview["valid"], 5)
        self.assertEqual(preview["remoteInvalid"], 0)
        self.assertTrue(
            any("停止继续检查" in item.get("warning", "") for item in preview["items"])
        )

    def test_provider_export_uses_unified_preview_and_batch_import(self):
        secret = "sk-provider-preview-secret"
        exported = {
            "format": "codex-agent-manager-api-account",
            "version": 1,
            "provider": {
                "id": "relay_preview",
                "name": "Relay Preview",
                "baseUrl": "https://relay.invalid/v1",
                "envKey": "RELAY_PREVIEW_KEY",
                "models": ["relay-model"],
                "key": secret,
            },
        }
        preview = core.preview_codex_accounts_batch(
            {"groupId": "relay", "items": [{"authJson": json.dumps(exported)}]}
        )
        self.assertEqual(preview["items"][0]["kind"], "provider")
        self.assertNotIn(secret, json.dumps(preview))
        invalid_export = json.loads(json.dumps(exported))
        invalid_export["provider"]["baseUrl"] = "not-a-url"
        invalid_preview = core.preview_codex_accounts_batch(
            {"groupId": "relay", "items": [{"authJson": invalid_export}]}
        )
        self.assertFalse(invalid_preview["items"][0]["valid"])
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "fetch_provider_models", return_value=["relay-model"]),
        ):
            result = core.import_codex_accounts_batch(
                {"groupId": "relay", "selectedIndices": [0], "items": [{"authJson": json.dumps(exported)}]}
            )
        self.assertEqual(len(result["importedProviders"]), 1)
        self.assertEqual(result["importedProviders"][0]["provider"]["id"], "relay_preview")

    def test_provider_exports_bulk_reimport_with_resolved_url_and_pool_membership(self):
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            for provider_id in ("portable_one", "portable_two"):
                core.save_provider(
                    {
                        "id": provider_id,
                        "name": provider_id,
                        "baseUrl": f"https://{provider_id}.example.test",
                        "resolvedBaseUrl": f"https://{provider_id}.example.test/v1",
                        "envKey": f"{provider_id.upper()}_KEY",
                        "models": [f"{provider_id}-model"],
                    }
                )
                core.store_provider_key(provider_id, f"sk-{provider_id}")
                core.set_provider_proxy_enabled(provider_id, True)
            exports = [core.export_api_provider(provider_id) for provider_id in ("portable_one", "portable_two")]
            for provider_id in ("portable_one", "portable_two"):
                core.remove_provider(provider_id)
            with patch.object(core, "fetch_provider_models", side_effect=AssertionError("catalog import must not query")):
                imported = core.import_codex_accounts_batch({"items": [{"authJson": exports}]})

        self.assertEqual(len(imported["importedProviders"]), 2)
        settings = core.load_settings()
        self.assertEqual(settings["web2api"]["providerIds"], ["portable_one", "portable_two"])
        self.assertEqual(
            settings["web2api"]["sourceOrder"],
            ["provider:portable_one", "provider:portable_two"],
        )
        self.assertEqual(
            core.provider_by_id("portable_one")["resolvedBaseUrl"],
            "https://portable_one.example.test/v1",
        )

    def test_remove_provider_does_not_mistake_user_cam_prefixed_agent_for_managed_agent(self):
        core.ensure_state()
        core.save_provider(
            {
                "id": "user_agent_relay",
                "name": "User Agent Relay",
                "baseUrl": "https://user-agent.example.test/v1",
                "envKey": "USER_AGENT_RELAY_KEY",
                "models": ["relay-model"],
            }
        )
        core.write_agent(
            {
                "name": "cam_custom_worker",
                "description": "User-owned agent whose name happens to start with cam_.",
                "provider": "user_agent_relay",
                "model": "relay-model",
                "effort": "high",
                "instructions": "Do the user-defined task.",
            }
        )

        with self.assertRaisesRegex(core.ManagerError, "正被 Agent `cam_custom_worker` 使用"):
            core.remove_provider("user_agent_relay")

        self.assertEqual(core.provider_by_id("user_agent_relay")["id"], "user_agent_relay")

    def test_common_api_provider_shape_imports_without_becoming_official_auth(self):
        document = {
            "provider": {
                "providerId": "common_api",
                "providerName": "Common API",
                "baseUrl": "https://common.example.test/v1",
                "models": {"data": [{"id": "common-model"}]},
            },
            "credentials": {"apiKey": "sk-common-api"},
        }
        preview = core.preview_codex_accounts_batch({"items": [{"authJson": document}]})
        self.assertEqual(preview["items"][0]["kind"], "provider")
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "fetch_provider_models", side_effect=AssertionError("catalog import must not query")),
        ):
            imported = core.import_codex_accounts_batch({"items": [{"authJson": document}]})
        self.assertEqual(imported["imported"], [])
        self.assertEqual(imported["importedProviders"][0]["provider"]["id"], "common_api")

    def test_cockpit_api_key_export_imports_as_provider_without_network_discovery(self):
        cockpit = {
            "id": "codex_apikey_deadbeef",
            "email": "Imported Relay",
            "auth_mode": "apikey",
            "openai_api_key": "sk-cockpit-relay-secret",
            "api_base_url": "https://relay.example.test/v1",
            "api_provider_mode": "custom",
            "api_provider_id": "cockpit_relay",
            "api_provider_name": "Cockpit Relay",
            "api_model_catalog": ["relay-model-a", "relay-model-b"],
            "api_wire_api": "responses",
            "tokens": {"id_token": "", "access_token": "", "refresh_token": None},
        }
        preview = core.preview_codex_accounts_batch(
            {"groupId": "relay", "items": [{"authJson": json.dumps([cockpit])}]}
        )
        self.assertEqual(preview["total"], 1)
        self.assertEqual(preview["items"][0]["kind"], "provider")
        self.assertEqual(preview["items"][0]["modelsCount"], 2)
        self.assertNotIn(cockpit["openai_api_key"], json.dumps(preview))
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "fetch_provider_models", side_effect=AssertionError("catalog import must not query")),
        ):
            result = core.import_codex_accounts_batch(
                {"groupId": "relay", "selectedIndices": [0], "items": [{"authJson": cockpit}]}
            )
            provider = core.provider_by_id("cockpit_relay")
            self.assertTrue(core.provider_key_configured("cockpit_relay"))
        self.assertEqual(len(result["importedProviders"]), 1)
        self.assertEqual(provider["baseUrl"], "https://relay.example.test/v1")
        self.assertEqual(provider["models"], ["relay-model-a", "relay-model-b"])

    def test_provider_model_probe_remembers_the_api_base_that_answered(self):
        class FakeResponse(io.BytesIO):
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            core.save_provider(
                {
                    "id": "root_relay",
                    "name": "Root Relay",
                    "baseUrl": "https://relay.example.test",
                    "envKey": "ROOT_RELAY_KEY",
                }
            )
            core.store_provider_key("root_relay", "sk-root")

            def open_endpoint(request, timeout=0):
                if request.full_url.endswith("/models") and not request.full_url.endswith("/v1/models"):
                    raise core.urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
                return FakeResponse(json.dumps({"data": [{"id": "relay-model"}]}).encode())

            with patch.object(core, "_open_same_origin_request", side_effect=open_endpoint):
                self.assertEqual(core.fetch_provider_models("root_relay"), ["relay-model"])
        provider = core.provider_by_id("root_relay")
        self.assertEqual(provider["resolvedBaseUrl"], "https://relay.example.test/v1")
        config = tomllib.loads(core.build_codex_config(core.load_settings()))
        self.assertEqual(
            config["model_providers"]["root_relay"]["base_url"],
            "https://relay.example.test/v1",
        )

    def test_provider_model_probe_retries_v1_after_root_forbidden(self):
        class FakeResponse(io.BytesIO):
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        calls = []

        def open_endpoint(request, timeout=0):
            calls.append(request.full_url)
            if len(calls) == 1:
                raise core.urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, io.BytesIO())
            return FakeResponse(json.dumps({"data": [{"id": "forbidden-root-model"}]}).encode())

        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            core.save_provider(
                {
                    "id": "forbidden_root_relay",
                    "name": "Forbidden Root Relay",
                    "baseUrl": "https://forbidden-root.example.test",
                    "envKey": "FORBIDDEN_ROOT_RELAY_KEY",
                }
            )
            core.store_provider_key("forbidden_root_relay", "sk-forbidden-root")
            with patch.object(core, "_open_same_origin_request", side_effect=open_endpoint):
                self.assertEqual(
                    core.fetch_provider_models("forbidden_root_relay"),
                    ["forbidden-root-model"],
                )

        self.assertEqual(
            calls,
            [
                "https://forbidden-root.example.test/models",
                "https://forbidden-root.example.test/v1/models",
            ],
        )
        self.assertEqual(
            core.provider_by_id("forbidden_root_relay")["resolvedBaseUrl"],
            "https://forbidden-root.example.test/v1",
        )

    def test_provider_custom_model_endpoint_supports_nonstandard_catalogs(self):
        class FakeResponse(io.BytesIO):
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        endpoint = "https://custom-catalog.example.test/api/catalog?scope=codex"
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            provider = core.save_provider(
                {
                    "id": "custom_catalog_relay",
                    "name": "Custom Catalog Relay",
                    "baseUrl": "https://custom-catalog.example.test/v1",
                    "modelsEndpoint": endpoint,
                    "envKey": "CUSTOM_CATALOG_RELAY_KEY",
                }
            )
            core.store_provider_key(provider["id"], "sk-custom-catalog")
            with patch.object(
                core,
                "_open_same_origin_request",
                return_value=FakeResponse(json.dumps({"result": {"items": [{"id": "gpt-custom"}]}}).encode()),
            ) as opened:
                models = core.fetch_provider_models(provider["id"])

        self.assertEqual(models, ["gpt-custom"])
        self.assertEqual(opened.call_args.args[0].full_url, endpoint)
        saved = core.provider_by_id(provider["id"])
        self.assertEqual(saved["modelsEndpoint"], endpoint)
        self.assertEqual(saved["resolvedBaseUrl"], "")

    def test_provider_custom_model_endpoint_must_share_base_origin(self):
        with self.assertRaisesRegex(core.ManagerError, "同一来源"):
            core.save_provider(
                {
                    "id": "cross_origin_catalog",
                    "name": "Cross Origin Catalog",
                    "baseUrl": "https://relay.example.test/v1",
                    "modelsEndpoint": "https://attacker.example.test/v1/models",
                    "envKey": "CROSS_ORIGIN_CATALOG_KEY",
                }
            )

    def test_provider_refresh_discards_response_after_key_rotation(self):
        class FakeResponse(io.BytesIO):
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            core.save_provider(
                {
                    "id": "rotating_probe",
                    "name": "Rotating Probe",
                    "baseUrl": "https://rotating-probe.example.test/v1",
                    "envKey": "ROTATING_PROBE_KEY",
                    "models": ["cached-model"],
                }
            )
            core.store_provider_key("rotating_probe", "sk-before")
            revision_before = core.provider_by_id("rotating_probe")["runtimeRevision"]

            def rotate_during_request(_request, timeout=0):
                core.store_provider_key("rotating_probe", "sk-after")
                return FakeResponse(json.dumps({"data": [{"id": "stale-model"}]}).encode())

            with patch.object(core, "_open_same_origin_request", side_effect=rotate_during_request):
                with self.assertRaisesRegex(core.ManagerError, "丢弃旧模型目录"):
                    core.fetch_provider_models("rotating_probe")

            saved = core.provider_by_id("rotating_probe")
            self.assertEqual(saved["models"], ["cached-model"])
            self.assertGreater(saved["runtimeRevision"], revision_before)
            self.assertEqual(core.load_provider_key("rotating_probe"), "sk-after")

    def test_provider_switch_uses_isolated_provider_auth_and_preserves_official_login(self):
        environment = {}
        original_auth = b'{"auth_mode":"chatgpt","tokens":{"access_token":"official-token"}}'
        (self.root / "auth.json").write_bytes(original_auth)
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            core.save_provider(
                {
                    "id": "switch_relay",
                    "name": "Switch Relay",
                    "baseUrl": "https://relay.example.test",
                    "envKey": "SWITCH_RELAY_KEY",
                    "models": ["relay-model-a", "relay-model-b"],
                }
            )
            core.store_provider_key("switch_relay", "sk-switch")
            settings = core.load_settings()
            settings["modelWorkspace"].update(
                {
                    "mode": "aggregate",
                    "activeSourceId": "account:old",
                    "selectAll": False,
                    "selectedModels": ["account:old::old-model"],
                }
            )
            core.save_settings(settings)
            rollout = self.root / "sessions" / "2026" / "08" / "rollout-provider-preserved.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text('{"type":"session_meta","payload":{"model_provider":"openai"}}\n', encoding="utf-8")
            database = self.root / "state_5.sqlite"
            connection = core.sqlite3.connect(database)
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, rollout_path TEXT)")
            connection.execute("INSERT INTO threads VALUES ('one', 'openai', ?)", (str(rollout),))
            connection.commit()
            connection.close()
            session_hashes = (
                hashlib.sha256(database.read_bytes()).digest(),
                hashlib.sha256(rollout.read_bytes()).digest(),
            )

            def read_environment(name):
                return environment.get(name)

            def write_environment(name, value):
                environment[name] = value

            with (
                patch.object(core, "resolve_codex_launch_plan", return_value={"strategy": "windows_app"}),
                patch.object(core, "close_codex_processes", return_value={"closed": ["7"]}),
                patch.object(core, "launch_codex_app", return_value={"started": True}),
                patch.object(core, "wait_for_codex_runtime_ready", return_value={"ready": True}),
                patch.object(core, "auto_sync_sessions_after_switch", return_value={"synced": 0}) as sync,
                patch.object(
                    core,
                    "fetch_provider_models",
                    side_effect=AssertionError("manual models must not require catalog discovery"),
                ),
                patch.object(core, "_read_user_environment", side_effect=read_environment),
                patch.object(core, "_sync_user_environment", side_effect=write_environment),
            ):
                result = core.switch_api_provider_and_launch("switch_relay")

        self.assertTrue(result["verified"])
        self.assertEqual(result["model"], "relay-model-a")
        updated = core.load_settings()
        self.assertEqual(updated["modelWorkspace"]["mode"], "independent")
        self.assertEqual(updated["modelWorkspace"]["activeSourceId"], "provider:switch_relay")
        self.assertIn(
            "provider:switch_relay::relay-model-a",
            updated["modelWorkspace"]["selectedModels"],
        )
        config = tomllib.loads(core.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(config["model_provider"], "switch_relay")
        self.assertNotIn("openai_base_url", config)
        self.assertEqual(
            config["model_providers"]["switch_relay"]["base_url"],
            "https://relay.example.test",
        )
        self.assertEqual(
            config["model_providers"]["switch_relay"]["env_key"],
            "SWITCH_RELAY_KEY",
        )
        self.assertEqual(config["model"], "relay-model-a")
        self.assertEqual(environment["SWITCH_RELAY_KEY"], "sk-switch")
        self.assertEqual((self.root / "auth.json").read_bytes(), original_auth)
        sync.assert_called_once_with("switch_relay")
        self.assertEqual(
            session_hashes,
            (hashlib.sha256(database.read_bytes()).digest(), hashlib.sha256(rollout.read_bytes()).digest()),
        )

    def test_active_provider_reopens_codex_when_runtime_is_stopped(self):
        environment = {"REPEAT_RELAY_KEY": "sk-repeat"}
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            core.save_provider(
                {
                    "id": "repeat_relay",
                    "name": "Repeat Relay",
                    "baseUrl": "https://repeat.example.test/v1",
                    "resolvedBaseUrl": "https://repeat.example.test/v1",
                    "envKey": "REPEAT_RELAY_KEY",
                    "models": ["repeat-model"],
                }
            )
            core.store_provider_key("repeat_relay", "sk-repeat")
            settings = core.load_settings()
            provider = core.provider_by_id("repeat_relay", settings)
            provider["appliedRuntimeRevision"] = provider.get("runtimeRevision")
            settings["modelWorkspace"].update(
                {
                    "mode": "independent",
                    "activeSourceId": "provider:repeat_relay",
                    "selectAll": True,
                    "selectedModels": ["provider:repeat_relay::repeat-model"],
                    "defaultModelKey": "provider:repeat_relay::repeat-model",
                }
            )
            core.save_settings(settings)
            core.CONFIG_FILE.write_text(core.build_codex_config(settings), encoding="utf-8")
            with (
                patch.object(core, "resolve_codex_launch_plan", return_value={"strategy": "windows_app"}),
                patch.object(core, "running_codex_processes", return_value=[]),
                patch.object(core, "close_codex_processes", side_effect=AssertionError("must not close")),
                patch.object(core, "launch_codex_app", return_value={"started": True}) as launch,
                patch.object(core, "wait_for_codex_runtime_ready", return_value={"ready": True}),
                patch.object(core, "_read_user_environment", side_effect=lambda name: environment.get(name)),
            ):
                result = core.switch_api_provider_and_launch("repeat_relay")

        self.assertFalse(result["changed"])
        self.assertTrue(result["launch"]["started"])
        launch.assert_called_once()

    def test_active_provider_reopen_failure_closes_unverified_runtime_and_preserves_files(self):
        calls = []
        environment = {"REPEAT_FAILURE_KEY": "sk-repeat-failure"}
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            core.save_provider(
                {
                    "id": "repeat_failure_relay",
                    "name": "Repeat Failure Relay",
                    "baseUrl": "https://repeat-failure.example.test/v1",
                    "resolvedBaseUrl": "https://repeat-failure.example.test/v1",
                    "envKey": "REPEAT_FAILURE_KEY",
                    "models": ["repeat-failure-model"],
                }
            )
            core.store_provider_key("repeat_failure_relay", "sk-repeat-failure")
            settings = core.load_settings()
            provider = core.provider_by_id("repeat_failure_relay", settings)
            provider["appliedRuntimeRevision"] = provider.get("runtimeRevision")
            settings["modelWorkspace"].update(
                {
                    "mode": "independent",
                    "activeSourceId": "provider:repeat_failure_relay",
                    "selectAll": True,
                    "selectedModels": ["provider:repeat_failure_relay::repeat-failure-model"],
                    "defaultModelKey": "provider:repeat_failure_relay::repeat-failure-model",
                }
            )
            core.save_settings(settings)
            core.CONFIG_FILE.write_text(core.build_codex_config(settings), encoding="utf-8")
            (self.root / "auth.json").write_bytes(b'{"auth_mode":"chatgpt","tokens":{}}')
            before = (core.SETTINGS_FILE.read_bytes(), core.CONFIG_FILE.read_bytes(), (self.root / "auth.json").read_bytes())
            with (
                patch.object(core, "resolve_codex_launch_plan", return_value={"strategy": "windows_app"}),
                patch.object(
                    core,
                    "running_codex_processes",
                    side_effect=lambda: [{"pid": "9"}] if "launch" in calls else [],
                ),
                patch.object(core, "launch_codex_app", side_effect=lambda **_kw: calls.append("launch") or {}),
                patch.object(core, "close_codex_processes", side_effect=lambda **_kw: calls.append("close") or {}),
                patch.object(core, "wait_for_codex_runtime_ready", side_effect=core.ManagerError("timeout")),
                patch.object(core, "_read_user_environment", side_effect=lambda name: environment.get(name)),
            ):
                with self.assertRaisesRegex(core.ManagerError, "避免循环重启"):
                    core.switch_api_provider_and_launch("repeat_failure_relay")

        self.assertEqual(calls, ["launch", "close"])
        self.assertEqual(
            before,
            (core.SETTINGS_FILE.read_bytes(), core.CONFIG_FILE.read_bytes(), (self.root / "auth.json").read_bytes()),
        )

    def test_provider_id_edit_migrates_pool_routes_models_and_key(self):
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "discover_agents", return_value=[]),
        ):
            core.save_provider(
                {
                    "id": "relay_before",
                    "name": "Relay Before",
                    "baseUrl": "https://rename.example.test/v1",
                    "envKey": "RELAY_RENAME_KEY",
                    "models": ["rename-model"],
                }
            )
            core.store_provider_key("relay_before", "sk-rename")
            core.set_provider_proxy_enabled("relay_before", True)
            settings = core.load_settings()
            settings["modelWorkspace"].update(
                {
                    "activeSourceId": "provider:relay_before",
                    "defaultModelKey": "provider:relay_before::rename-model",
                    "selectedModels": ["provider:relay_before::rename-model"],
                }
            )
            settings["subagentRouting"]["routes"]["hard"] = {
                "models": ["provider:relay_before::rename-model"],
                "efforts": ["high"],
            }
            core.save_settings(settings)
            core.save_provider(
                {
                    "id": "relay_after",
                    "name": "Relay After",
                    "baseUrl": "https://rename.example.test/v1",
                    "envKey": "RELAY_RENAME_KEY",
                    "models": ["rename-model"],
                },
                "relay_before",
            )

            updated = core.load_settings()
            self.assertEqual(updated["web2api"]["providerIds"], ["relay_after"])
            self.assertEqual(updated["web2api"]["sourceOrder"], ["provider:relay_after"])
            self.assertEqual(updated["modelWorkspace"]["activeSourceId"], "provider:relay_after")
            self.assertEqual(updated["modelWorkspace"]["defaultModelKey"], "provider:relay_after::rename-model")
            self.assertEqual(
                updated["subagentRouting"]["routes"]["hard"]["models"],
                ["provider:relay_after::rename-model"],
            )
            self.assertEqual(core.load_provider_key("relay_after", required=True), "sk-rename")
            self.assertNotIn("relay_before", core._secret_store()["providers"])

    def test_provider_id_edit_rolls_back_settings_and_key_when_secret_write_fails(self):
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "discover_agents", return_value=[]),
        ):
            core.save_provider(
                {
                    "id": "edit_rollback_before",
                    "name": "Edit Rollback",
                    "baseUrl": "https://edit-rollback.example.test/v1",
                    "envKey": "EDIT_ROLLBACK_KEY",
                    "models": ["edit-rollback-model"],
                }
            )
            core.store_provider_key("edit_rollback_before", "sk-edit-rollback")
            core.set_provider_proxy_enabled("edit_rollback_before", True)
            before = (core.SETTINGS_FILE.read_bytes(), core.SECRETS_FILE.read_bytes())
            real_atomic_write_json = core.atomic_write_json

            def fail_secret_write(path, payload):
                if path == core.SECRETS_FILE:
                    raise OSError("secret write failed")
                return real_atomic_write_json(path, payload)

            with patch.object(core, "atomic_write_json", side_effect=fail_secret_write):
                with self.assertRaisesRegex(OSError, "secret write failed"):
                    core.save_provider(
                        {
                            "id": "edit_rollback_after",
                            "name": "Edit Rollback",
                            "baseUrl": "https://edit-rollback.example.test/v1",
                            "envKey": "EDIT_ROLLBACK_KEY",
                            "models": ["edit-rollback-model"],
                        },
                        "edit_rollback_before",
                    )

        self.assertEqual(before, (core.SETTINGS_FILE.read_bytes(), core.SECRETS_FILE.read_bytes()))

    def test_provider_create_with_key_is_one_transaction(self):
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            provider = core.save_provider(
                {
                    "id": "atomic_relay",
                    "name": "Atomic Relay",
                    "baseUrl": "https://atomic.example.test/v1",
                    "envKey": "ATOMIC_RELAY_KEY",
                    "models": ["atomic-model"],
                },
                api_key="sk-atomic",
            )
            self.assertEqual(provider["id"], "atomic_relay")
            self.assertEqual(core.load_provider_key("atomic_relay", required=True), "sk-atomic")

            before = (core.SETTINGS_FILE.read_bytes(), core.SECRETS_FILE.read_bytes())
            real_atomic_write_json = core.atomic_write_json

            def fail_secret_write(path, payload):
                if path == core.SECRETS_FILE:
                    raise OSError("secret write failed")
                return real_atomic_write_json(path, payload)

            with patch.object(core, "atomic_write_json", side_effect=fail_secret_write):
                with self.assertRaisesRegex(OSError, "secret write failed"):
                    core.save_provider(
                        {
                            "id": "atomic_failed_relay",
                            "name": "Atomic Failed Relay",
                            "baseUrl": "https://atomic-failed.example.test/v1",
                            "envKey": "ATOMIC_FAILED_RELAY_KEY",
                            "models": ["atomic-model"],
                        },
                        api_key="sk-must-not-leak",
                    )
            self.assertEqual(before, (core.SETTINGS_FILE.read_bytes(), core.SECRETS_FILE.read_bytes()))
            self.assertFalse(any(item.get("id") == "atomic_failed_relay" for item in core.load_settings()["providers"]))

    def test_deleting_active_provider_is_rejected_without_breaking_route(self):
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "discover_agents", return_value=[]),
        ):
            core.save_provider(
                {
                    "id": "active_delete_relay",
                    "name": "Active Delete Relay",
                    "baseUrl": "https://active-delete.example.test/v1",
                    "envKey": "ACTIVE_DELETE_KEY",
                    "models": ["active-delete-model"],
                }
            )
            core.store_provider_key("active_delete_relay", "sk-active-delete")
            settings = core.load_settings()
            settings["modelWorkspace"]["activeSourceId"] = "provider:active_delete_relay"
            core.save_settings(settings)
            core.CONFIG_FILE.write_text(
                'model = "active-delete-model"\nopenai_base_url = "https://active-delete.example.test/v1"\n',
                encoding="utf-8",
            )
            before = (core.SETTINGS_FILE.read_bytes(), core.CONFIG_FILE.read_bytes(), core.SECRETS_FILE.read_bytes())
            with self.assertRaisesRegex(core.ManagerError, "当前正用于 Codex"):
                core.remove_provider("active_delete_relay")

        self.assertEqual(
            before,
            (core.SETTINGS_FILE.read_bytes(), core.CONFIG_FILE.read_bytes(), core.SECRETS_FILE.read_bytes()),
        )

    def test_provider_to_official_clears_overrides_preserves_sessions_and_is_idempotent(self):
        environment = {}
        calls = []
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode()),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode()),
        ):
            core.save_provider(
                {
                    "id": "relay_before_official",
                    "name": "Relay Before Official",
                    "baseUrl": "https://relay.example.test/v1",
                    "resolvedBaseUrl": "https://relay.example.test/v1",
                    "envKey": "RELAY_BEFORE_OFFICIAL_KEY",
                    "models": ["relay-model"],
                }
            )
            core.store_provider_key("relay_before_official", "relay-secret")
            (self.root / "auth.json").write_text('{"OPENAI_API_KEY":"official-token"}', encoding="utf-8")
            account = core.save_codex_account({"label": "Official Target"})
            settings = core.load_settings()
            next(item for item in settings["accounts"] if item["id"] == account["id"])["models"] = [
                "gpt-official-a",
                "gpt-official-b",
            ]
            settings["modelWorkspace"].update(
                {
                    "mode": "independent",
                    "activeSourceId": "provider:relay_before_official",
                    "selectAll": False,
                    "selectedModels": ["provider:relay_before_official::relay-model"],
                }
            )
            core._active_main(settings).update(
                {"provider": "relay_before_official", "model": "relay-model"}
            )
            core.save_settings(settings)

            def read_env(name):
                return environment.get(name)

            def write_env(name, value):
                environment[name] = value

            def remove_env(name):
                environment.pop(name, None)

            with (
                patch.object(core, "_read_user_environment", side_effect=read_env),
                patch.object(core, "_sync_user_environment", side_effect=write_env),
                patch.object(core, "_remove_user_environment", side_effect=remove_env),
            ):
                core.apply_configuration(False)
                rollout = self.root / "sessions" / "2026" / "08" / "rollout-preserved.jsonl"
                rollout.parent.mkdir(parents=True)
                rollout.write_text(
                    json.dumps(
                        {"type": "session_meta", "payload": {"model_provider": "relay_before_official"}}
                    )
                    + "\n",
                    encoding="utf-8",
                )
                database = self.root / "state_5.sqlite"
                connection = core.sqlite3.connect(database)
                connection.execute(
                    "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, rollout_path TEXT)"
                )
                connection.execute("INSERT INTO threads VALUES ('one', 'relay_before_official', ?)", (str(rollout),))
                connection.commit()
                connection.close()
                before = (hashlib.sha256(database.read_bytes()).digest(), hashlib.sha256(rollout.read_bytes()).digest())
                with (
                    patch.object(core, "resolve_codex_launch_plan", return_value={"strategy": "windows_app"}),
                    patch.object(core, "close_codex_processes", side_effect=lambda **_kw: calls.append("close") or {}),
                    patch.object(core, "launch_codex_app", side_effect=lambda **_kw: calls.append("launch") or {}),
                    patch.object(core, "wait_for_codex_runtime_ready", return_value={"ready": True}),
                    patch.object(core, "running_codex_processes", return_value=[]),
                ):
                    result = core.switch_codex_account_and_launch(account["id"])
                    repeated = core.switch_codex_account_and_launch(account["id"])

        config = tomllib.loads(core.CONFIG_FILE.read_text(encoding="utf-8"))
        workspace = core.load_settings()["modelWorkspace"]
        expected = {f"account:{account['id']}::gpt-official-a", f"account:{account['id']}::gpt-official-b"}
        self.assertTrue(result["verified"])
        self.assertNotIn("model_provider", config)
        self.assertNotIn("openai_base_url", config)
        self.assertIn(config["model"], {"gpt-official-a", "gpt-official-b"})
        self.assertEqual(workspace["activeSourceId"], f"account:{account['id']}")
        self.assertTrue(expected.issubset(set(workspace["selectedModels"])))
        self.assertNotIn("RELAY_BEFORE_OFFICIAL_KEY", environment)
        self.assertEqual(before, (hashlib.sha256(database.read_bytes()).digest(), hashlib.sha256(rollout.read_bytes()).digest()))
        self.assertEqual(calls, ["close", "launch", "launch"])
        self.assertFalse(repeated["accountSwitch"]["changed"])
        self.assertIsNotNone(repeated["launch"])

    def test_provider_config_route_is_restored_with_runtime_overlay(self):
        environment = {}
        original_auth = b'{"auth_mode":"apikey","OPENAI_API_KEY":"official-before"}'
        (self.root / "auth.json").write_bytes(original_auth)
        original_config = core.CONFIG_FILE.read_bytes()
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode()),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode()),
        ):
            core.save_provider(
                {
                    "id": "overlay_relay",
                    "name": "Overlay Relay",
                    "baseUrl": "https://relay.example.test/v1",
                    "resolvedBaseUrl": "https://relay.example.test/v1",
                    "envKey": "OVERLAY_RELAY_KEY",
                    "models": ["relay-model"],
                }
            )
            core.store_provider_key("overlay_relay", "relay-secret")

            def read_env(name):
                return environment.get(name)

            def write_env(name, value):
                environment[name] = value

            def remove_env(name):
                environment.pop(name, None)

            with (
                patch.object(core, "_read_user_environment", side_effect=read_env),
                patch.object(core, "_sync_user_environment", side_effect=write_env),
                patch.object(core, "_remove_user_environment", side_effect=remove_env),
                patch.object(core, "resolve_codex_launch_plan", return_value={"strategy": "windows_app"}),
                patch.object(core, "close_codex_processes", return_value={}),
                patch.object(core, "launch_codex_app", return_value={"started": True}),
                patch.object(core, "wait_for_codex_runtime_ready", return_value={"ready": True}),
                patch.object(core, "auto_sync_sessions_after_switch", return_value={"synced": 0}),
            ):
                core.begin_runtime_configuration_overlay()
                core.switch_api_provider_and_launch("overlay_relay")
                self.assertEqual((self.root / "auth.json").read_bytes(), original_auth)
                self.assertEqual(environment["OVERLAY_RELAY_KEY"], "relay-secret")
                restored = core.restore_runtime_configuration_overlay()

        self.assertTrue(restored["restored"])
        self.assertEqual((self.root / "auth.json").read_bytes(), original_auth)
        self.assertEqual(core.CONFIG_FILE.read_bytes(), original_config)
        self.assertNotIn("OVERLAY_RELAY_KEY", environment)

    def test_provider_failure_restores_exact_snapshot_and_closes_failed_runtime_once(self):
        environment = {}
        calls = []
        (self.root / "auth.json").write_text('{"OPENAI_API_KEY":"official-before-provider"}', encoding="utf-8")
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode()),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode()),
        ):
            core.save_provider(
                {
                    "id": "rollback_provider",
                    "name": "Rollback Provider",
                    "baseUrl": "https://relay.example.test/v1",
                    "resolvedBaseUrl": "https://relay.example.test/v1",
                    "envKey": "ROLLBACK_PROVIDER_KEY",
                    "models": ["rollback-model"],
                }
            )
            core.store_provider_key("rollback_provider", "rollback-secret")
            before = {
                "config": core.CONFIG_FILE.read_bytes(),
                "settings": core.SETTINGS_FILE.read_bytes(),
                "auth": (self.root / "auth.json").read_bytes(),
            }

            def read_env(name):
                return environment.get(name)

            def write_env(name, value):
                environment[name] = value

            def remove_env(name):
                environment.pop(name, None)

            with (
                patch.object(core, "_read_user_environment", side_effect=read_env),
                patch.object(core, "_sync_user_environment", side_effect=write_env),
                patch.object(core, "_remove_user_environment", side_effect=remove_env),
                patch.object(core, "resolve_codex_launch_plan", return_value={"strategy": "windows_app"}),
                patch.object(core, "close_codex_processes", side_effect=lambda **_kw: calls.append("close") or {}),
                patch.object(core, "launch_codex_app", side_effect=lambda **_kw: calls.append("launch") or {}),
                patch.object(core, "running_codex_processes", return_value=[{"pid": "7"}]),
                patch.object(core, "wait_for_codex_runtime_ready", side_effect=core.ManagerError("timeout")),
            ):
                with self.assertRaisesRegex(core.ManagerError, "避免循环重启"):
                    core.switch_api_provider_and_launch("rollback_provider")

        self.assertEqual(core.CONFIG_FILE.read_bytes(), before["config"])
        self.assertEqual(core.SETTINGS_FILE.read_bytes(), before["settings"])
        self.assertEqual((self.root / "auth.json").read_bytes(), before["auth"])
        self.assertEqual(environment, {})
        self.assertEqual(calls, ["close", "launch", "close"])

    def test_concurrent_switch_is_rejected_before_side_effects(self):
        entered = threading.Event()
        release = threading.Event()

        def hold_switch_lock():
            with core._exclusive_switch_operation("holder", "one"):
                entered.set()
                release.wait(5)

        holder = threading.Thread(target=hold_switch_lock)
        holder.start()
        self.assertTrue(entered.wait(2))
        try:
            with self.assertRaisesRegex(core.ManagerError, "切换正在进行"):
                core.switch_api_provider_and_launch("must-not-be-read")
        finally:
            release.set()
            holder.join(5)
        self.assertFalse(holder.is_alive())

    def test_independent_provider_remains_active_while_official_auth_exists(self):
        core.save_provider(
            {
                "id": "active_relay",
                "name": "Active Relay",
                "baseUrl": "https://relay.example.test/v1",
                "envKey": "ACTIVE_RELAY_KEY",
                "models": ["relay-model"],
            }
        )
        settings = core.load_settings()
        settings["modelWorkspace"].update(
            {"mode": "independent", "activeSourceId": "provider:active_relay"}
        )
        with patch.object(core, "current_auth_state", return_value={"activeAccountId": "official-account"}):
            sources = core.model_sources(settings, local_models=[])
        active = [item["id"] for item in sources if item["active"]]
        self.assertEqual(active, ["provider:active_relay"])

    def test_api_account_fallback_model_is_kept_in_provider_catalog(self):
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "fetch_provider_models", side_effect=core.ManagerError("offline")),
        ):
            result = core.import_api_account(
                {
                    "id": "fallback_relay",
                    "name": "Fallback Relay",
                    "baseUrl": "https://fallback.invalid/v1",
                    "envKey": "FALLBACK_RELAY_KEY",
                    "key": "sk-fallback",
                    "model": "fallback-model",
                    "activate": False,
                }
            )
        self.assertIn("fallback-model", result["models"])
        self.assertEqual(result["discoveryStatus"], "stale")
        self.assertIn("已保留手动填写的模型", result["discoveryWarning"])
        self.assertIsNone(result["discoveryError"])
        provider = core.provider_by_id("fallback_relay")
        self.assertIn("fallback-model", provider["models"])
        self.assertEqual(provider["lastCheckStatus"], "stale")

    def test_relay_dashboard_balance_snapshot_is_seeded_before_key_scoped_refresh(self):
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "fetch_provider_models", return_value=["gpt-test"]),
            patch.object(core, "fetch_provider_balance", return_value=None),
        ):
            core.import_api_account(
                {
                    "id": "dashboard_snapshot_relay",
                    "name": "Dashboard Snapshot Relay",
                    "baseUrl": "https://relay.example.test/v1",
                    "envKey": "DASHBOARD_SNAPSHOT_RELAY_KEY",
                    "key": "sk-dashboard-snapshot",
                    "model": "gpt-test",
                    "integrationKind": "sub2api",
                    "balanceEndpoint": "https://relay.example.test/v1/usage",
                    "balanceSnapshot": {
                        "remaining": 13.22,
                        "used": 29.4803,
                        "currency": "USD",
                    },
                    "activate": False,
                }
            )

        provider = core.provider_by_id("dashboard_snapshot_relay")
        self.assertEqual(provider["balance"]["amount"], 13.22)
        self.assertEqual(provider["balance"]["used"], 29.4803)
        self.assertEqual(provider["balance"]["source"], "relay-dashboard")
        self.assertIsNone(provider["balanceError"])

    def test_relay_login_import_persists_one_account_and_rotates_encrypted_keys(self):
        preview = {
            "adapter": "sub2api",
            "adapterLabel": "Sub2API",
            "siteName": "Relay Account",
            "portalUrl": "https://relay-account.example.test/keys",
            "origin": "https://relay-account.example.test",
            "integrationKind": "sub2api",
            "user": {"id": "user-7", "name": "alice", "email": "alice@example.test"},
            "balance": {"remaining": 13.22, "used": 29.48, "currency": "USD"},
            "models": ["gpt-test"],
            "groups": [
                {"id": "7", "name": "Codex 特惠", "platform": "openai", "active": True, "rateMultiplier": 0.06},
                {"id": "9", "name": "Codex 公益", "platform": "openai", "active": True, "rateMultiplier": 0.03},
                {"id": "8", "name": "Claude 低缓", "platform": "anthropic", "active": True, "rateMultiplier": 0.065},
            ],
            "keys": [
                {"id": "k-codex", "name": "codex", "maskedKey": "sk-codex••••0001", "active": True, "group": "Codex 特惠", "groupId": "7", "groupPlatform": "openai", "groupRateMultiplier": 0.06, "models": ["gpt-test"]},
                {"id": "k-codex-2", "name": "codex-2", "maskedKey": "sk-codex••••0003", "active": True, "group": "Codex 公益", "groupId": "9", "groupPlatform": "openai", "groupRateMultiplier": 0.03, "models": ["gpt-test", "claude-test"]},
                {"id": "k-claude", "name": "claude", "maskedKey": "sk-claude••••0002", "active": True, "group": "Claude 低缓", "groupId": "8", "groupPlatform": "anthropic", "groupRateMultiplier": 0.065, "models": ["gpt-test"]},
            ],
            "apiEndpoints": [
                {"id": "default", "name": "默认 API", "baseUrl": "https://relay-account.example.test/v1", "modelsEndpoint": "https://relay-account.example.test/v1/models", "balanceEndpoint": "https://relay-account.example.test/v1/usage", "isDefault": True},
                {"id": "fast", "name": "快速线路", "baseUrl": "https://fast.relay-account.example.test/v1", "modelsEndpoint": "https://fast.relay-account.example.test/v1/models", "balanceEndpoint": "https://fast.relay-account.example.test/v1/usage", "isDefault": False},
            ],
            "defaultEndpointId": "default",
            "supportsCreate": True,
            "detectedAt": "2026-09-01T12:00:00+08:00",
        }
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            imported = core.import_relay_account(
                preview,
                {
                    "k-codex": "sk-codex-secret",
                    "k-codex-2": "sk-codex-secret-2",
                    "k-claude": "sk-claude-secret",
                },
                selected_key_id="k-codex",
                endpoint_id="default",
            )
            account = imported["account"]
            provider_id = account["providerId"]
            self.assertEqual(imported["configuredKeyCount"], 2)
            self.assertEqual(len(core.load_settings()["relayAccounts"]), 1)
            self.assertEqual(account["groups"][0]["platformKind"], "codex")
            self.assertEqual(account["groups"][1]["platformKind"], "codex")
            self.assertNotIn("k-claude", {item["id"] for item in account["keys"]})
            self.assertEqual(account["keys"][0]["groupRateMultiplier"], 0.06)
            self.assertEqual(account["keys"][1]["models"], ["gpt-test"])
            self.assertNotIn("sk-codex-secret", core.SETTINGS_FILE.read_text(encoding="utf-8"))
            provider = core.provider_by_id(provider_id)
            self.assertEqual(provider["sourceType"], "relay_account")
            self.assertEqual(provider["relayAccountId"], account["id"])
            self.assertEqual(provider["relayKeyId"], "k-codex")
            self.assertEqual(core.load_provider_key(provider_id), "sk-codex-secret")

            core.store_relay_account_dashboard_session(
                account["id"],
                {
                    "origin": "https://relay-account.example.test",
                    "portalUrl": "https://relay-account.example.test/keys",
                    "adapter": "sub2api",
                    "authMode": "bearer",
                    "userId": "user-7",
                    "accessToken": "dashboard-access-secret",
                    "refreshToken": "dashboard-refresh-secret",
                    "cookies": [
                        {
                            "name": "session",
                            "value": "dashboard-cookie-secret",
                            "domain": "relay-account.example.test",
                            "path": "/",
                            "secure": True,
                            "httpOnly": True,
                        }
                    ],
                },
            )
            saved_session = core.load_relay_account_dashboard_session(account["id"], required=True)
            self.assertEqual(saved_session["refreshToken"], "dashboard-refresh-secret")
            self.assertNotIn("dashboard-access-secret", core.SETTINGS_FILE.read_text(encoding="utf-8"))
            public = core.public_state()["settings"]["relayAccounts"][0]
            self.assertTrue(public["dashboardSessionStored"])
            self.assertNotIn("dashboard-refresh-secret", json.dumps(public))

            # Updating the imported account must preserve its separately
            # encrypted dashboard renewal credential.
            core.import_relay_account(
                preview,
                {"k-codex": "sk-codex-secret"},
                selected_key_id="k-codex",
                endpoint_id="default",
                relay_account_id=account["id"],
            )
            self.assertEqual(
                core.load_relay_account_dashboard_session(account["id"], required=True)["accessToken"],
                "dashboard-access-secret",
            )

            updated_metadata = core.update_relay_account_metadata(
                account["id"],
                {"portalUrl": "https://relay-account.example.test/dashboard"},
            )
            self.assertEqual(
                updated_metadata["account"]["portalUrl"],
                "https://relay-account.example.test/dashboard",
            )
            self.assertEqual(
                core.provider_by_id(provider_id)["portalUrl"],
                "https://relay-account.example.test/dashboard",
            )
            with self.assertRaisesRegex(core.ManagerError, "只能修改同一域名"):
                core.update_relay_account_metadata(
                    account["id"],
                    {"portalUrl": "https://other.example.test/dashboard"},
                )

            active_settings = core.load_settings()
            active_settings["modelWorkspace"]["activeSourceId"] = f"provider:{provider_id}"
            active_provider = next(
                item for item in active_settings["providers"] if item.get("id") == provider_id
            )
            active_provider["appliedRuntimeRevision"] = active_provider["runtimeRevision"]
            core.save_settings(active_settings)

            # A dashboard scan may know the Key and its group without exposing
            # any model catalog.  That sparse snapshot must not erase the last
            # successful /models discovery when the user rotates the route.
            catalogless = core.load_settings()
            relay_record = next(
                item for item in catalogless["relayAccounts"] if item.get("id") == account["id"]
            )
            relay_record["models"] = []
            for relay_key in relay_record["keys"]:
                relay_key["models"] = []
            core.save_settings(catalogless)

            rotated = core.update_relay_account_selection(
                account["id"],
                {"keyId": "k-codex-2", "endpointId": "fast"},
            )
            self.assertTrue(rotated["requiresReapply"])
            provider = core.provider_by_id(provider_id)
            self.assertEqual(provider["relayKeyId"], "k-codex-2")
            self.assertEqual(provider["relayPlatform"], "openai")
            self.assertEqual(provider["relayRateMultiplier"], 0.03)
            self.assertEqual(provider["baseUrl"], "https://fast.relay-account.example.test/v1")
            self.assertEqual(provider["models"], ["gpt-test"])
            self.assertEqual(core.load_provider_key(provider_id), "sk-codex-secret-2")
            self.assertEqual(len(core.load_settings()["providers"]), 2)  # builtin + one relay account

            applied_settings = core.load_settings()
            applied_provider = next(
                item for item in applied_settings["providers"] if item.get("id") == provider_id
            )
            applied_provider["appliedRuntimeRevision"] = applied_provider["runtimeRevision"]
            applied_revision = applied_provider["runtimeRevision"]
            core.save_settings(applied_settings)

            regrouped = core.update_relay_account_key_group(
                account["id"],
                "k-codex-2",
                "7",
            )
            self.assertFalse(regrouped["requiresReapply"])
            provider = core.provider_by_id(provider_id)
            self.assertEqual(provider["relayGroupId"], "7")
            self.assertEqual(provider["relayRateMultiplier"], 0.06)
            self.assertEqual(provider["runtimeRevision"], applied_revision)
            source = next(
                item
                for item in core.model_sources()
                if item.get("recordId") == provider_id
            )
            self.assertFalse(source["activationRequired"])

            refreshed_selection = core.update_relay_account_selection(
                account["id"],
                {"keyId": "k-codex-2", "endpointId": "fast"},
            )
            self.assertFalse(refreshed_selection["requiresReapply"])
            self.assertEqual(
                core.provider_by_id(provider_id)["runtimeRevision"],
                applied_revision,
            )

            with (
                patch.object(core, "fetch_provider_balance", return_value=None) as balance_refresh,
                patch.object(
                    core,
                    "refresh_provider_models",
                    return_value={
                        "models": ["gpt-test", "gpt-new"],
                        "status": "ready",
                        "warning": None,
                    },
                ) as model_refresh,
            ):
                refreshed_metadata = core.refresh_relay_account(account["id"])
            balance_refresh.assert_called_once_with(provider_id)
            model_refresh.assert_called_once_with(provider_id)
            self.assertEqual(refreshed_metadata["counts"]["models"], 2)
            self.assertEqual(
                refreshed_metadata["account"]["models"],
                ["gpt-test", "gpt-new"],
            )

            removed = core.remove_relay_account_key(account["id"], "k-codex")
            self.assertEqual(removed["removedKeyId"], "k-codex")
            self.assertFalse(core.relay_account_key_configured(account["id"], "k-codex"))
            emptied = core.remove_relay_account_key(account["id"], "k-codex-2")
            self.assertTrue(emptied["providerDisabled"])
            self.assertTrue(emptied["requiresReapply"])
            self.assertEqual(emptied["account"]["keys"], [])
            self.assertEqual(emptied["account"]["selectedKeyId"], "")
            self.assertFalse(core.provider_key_configured(provider_id))
            self.assertEqual(core.provider_by_id(provider_id)["modelDiscoveryState"], "disabled")

    def test_relay_refresh_prunes_remote_deletions_and_export_round_trips_as_account(self):
        preview = {
            "adapter": "sub2api",
            "adapterLabel": "Sub2API",
            "siteName": "Portable Relay",
            "portalUrl": "https://portable-relay.example.test/dashboard",
            "origin": "https://portable-relay.example.test",
            "integrationKind": "sub2api",
            "user": {"id": "user-9", "name": "portable", "email": "portable@example.test"},
            "balance": {"remaining": 8.5, "used": 1.5, "currency": "USD"},
            "models": ["gpt-test"],
            "groups": [
                {"id": "cheap", "name": "公益", "platform": "openai", "active": True, "rateMultiplier": 0.01},
                {"id": "standard", "name": "标准", "platform": "openai", "active": True, "rateMultiplier": 0.06},
            ],
            "keys": [
                {"id": "key-1", "name": "one", "maskedKey": "sk-one••••0001", "active": True, "group": "标准", "groupId": "standard", "groupPlatform": "openai", "groupRateMultiplier": 0.06, "models": ["gpt-test"]},
                {"id": "key-2", "name": "two", "maskedKey": "sk-two••••0002", "active": True, "group": "公益", "groupId": "cheap", "groupPlatform": "openai", "groupRateMultiplier": 0.01, "models": ["gpt-test"]},
            ],
            "apiEndpoints": [
                {"id": "default", "name": "默认 API", "baseUrl": "https://portable-relay.example.test/v1", "modelsEndpoint": "https://portable-relay.example.test/v1/models", "balanceEndpoint": "https://portable-relay.example.test/v1/usage", "isDefault": True},
            ],
            "defaultEndpointId": "default",
            "supportsCreate": True,
            "keysAuthoritative": True,
            "groupsAuthoritative": True,
            "detectedAt": "2026-09-01T12:00:00+08:00",
        }
        session = {
            "origin": "https://portable-relay.example.test",
            "portalUrl": "https://portable-relay.example.test/dashboard",
            "adapter": "sub2api",
            "authMode": "bearer",
            "userId": "user-9",
            "accessToken": "portable-access-token",
            "refreshToken": "portable-refresh-token",
            "cookies": [],
        }
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            imported = core.import_relay_account(
                preview,
                {"key-1": "sk-portable-one", "key-2": "sk-portable-two"},
                selected_key_id="key-1",
            )
            account_id = imported["account"]["id"]
            provider_id = imported["account"]["providerId"]
            core.store_relay_account_dashboard_session(account_id, session)

            exported = core.export_relay_account(account_id)
            self.assertEqual(exported["format"], "codex-agent-manager-relay-account")
            self.assertEqual(set(exported["relayAccount"]["keySecrets"]), {"key-1", "key-2"})
            self.assertEqual(
                exported["relayAccount"]["dashboardSession"]["refreshToken"],
                "portable-refresh-token",
            )
            preview_result = core.preview_codex_accounts_batch(
                {"groupId": "relay", "items": [{"authJson": exported}]}
            )
            self.assertEqual(preview_result["items"][0]["kind"], "relay")
            self.assertTrue(preview_result["items"][0]["valid"])
            round_trip = core.import_codex_accounts_batch(
                {
                    "groupId": "official",
                    "proxyEnabled": True,
                    "selectedIndices": [0],
                    "items": [{"authJson": exported}],
                }
            )
            self.assertEqual(len(round_trip["importedRelayAccounts"]), 1)
            self.assertTrue(round_trip["importedRelayAccounts"][0]["dashboardSessionImported"])
            self.assertEqual(
                core.load_settings()["relayAccounts"][0]["groupId"],
                "official",
            )
            self.assertIn(provider_id, core.load_settings()["web2api"]["providerIds"])

            moved = core.move_relay_account_group(account_id, "relay")
            self.assertEqual(moved["account"]["groupId"], "relay")
            self.assertEqual(moved["provider"]["groupId"], "relay")

            one_remote_key = json.loads(json.dumps(preview))
            one_remote_key["keys"] = [one_remote_key["keys"][1]]
            one_remote_key["remoteKeyCount"] = 1
            synced = core.sync_relay_account_snapshot(account_id, one_remote_key)
            self.assertEqual(synced["removedKeyCount"], 1)
            current = core.load_settings()["relayAccounts"][0]
            self.assertEqual([item["id"] for item in current["keys"]], ["key-2"])
            self.assertEqual(current["selectedKeyId"], "key-2")
            self.assertFalse(core.relay_account_key_configured(account_id, "key-1"))
            self.assertEqual(core.load_provider_key(provider_id), "sk-portable-two")

            partial = json.loads(json.dumps(one_remote_key))
            partial["keys"] = []
            partial["keysAuthoritative"] = False
            core.sync_relay_account_snapshot(account_id, partial)
            self.assertEqual(
                [item["id"] for item in core.load_settings()["relayAccounts"][0]["keys"]],
                ["key-2"],
            )

            empty_remote = json.loads(json.dumps(one_remote_key))
            empty_remote["keys"] = []
            empty_remote["remoteKeyCount"] = 0
            empty_remote["keysAuthoritative"] = True
            emptied = core.sync_relay_account_snapshot(account_id, empty_remote)
            self.assertEqual(emptied["removedKeyCount"], 1)
            self.assertEqual(core.load_settings()["relayAccounts"][0]["keys"], [])
            self.assertFalse(core.provider_key_configured(provider_id))
            self.assertNotIn(provider_id, core.load_settings()["web2api"]["providerIds"])

    def test_api_account_manual_model_and_models_403_is_stale_but_usable(self):
        def forbidden(request, timeout=0):
            raise core.urllib.error.HTTPError(
                request.full_url,
                403,
                "forbidden",
                {},
                io.BytesIO(b"forbidden"),
            )

        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "_open_same_origin_request", side_effect=forbidden),
        ):
            result = core.import_api_account(
                {
                    "id": "manual_403_relay",
                    "name": "Manual 403 Relay",
                    "baseUrl": "https://manual-403.example.test/v1",
                    "envKey": "MANUAL_403_RELAY_KEY",
                    "key": "sk-manual-403",
                    "model": "manual-model",
                    "activate": False,
                }
            )

        provider = core.provider_by_id("manual_403_relay")
        self.assertEqual(result["discoveryStatus"], "stale")
        self.assertIsNone(result["discoveryError"])
        self.assertIn("已保留手动填写的模型", result["discoveryWarning"])
        self.assertEqual(provider["lastCheckStatus"], "stale")
        self.assertIn("HTTP 403", provider["lastCheckError"])
        self.assertIn("manual-model", provider["models"])
        source = next(item for item in core.model_sources(core.load_settings(), local_models=[]) if item["recordId"] == "manual_403_relay")
        self.assertTrue(source["available"])

    def test_provider_model_refresh_returns_cached_models_when_catalog_is_forbidden(self):
        def forbidden(request, timeout=0):
            raise core.urllib.error.HTTPError(
                request.full_url,
                403,
                "forbidden",
                {},
                io.BytesIO(),
            )

        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            core.save_provider(
                {
                    "id": "refresh_403_relay",
                    "name": "Refresh 403 Relay",
                    "baseUrl": "https://refresh-403.example.test",
                    "envKey": "REFRESH_403_RELAY_KEY",
                    "models": ["manual-model"],
                }
            )
            core.store_provider_key("refresh_403_relay", "sk-refresh-403")
            with patch.object(core, "_open_same_origin_request", side_effect=forbidden):
                result = core.refresh_provider_models("refresh_403_relay")

        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["models"], ["manual-model"])
        self.assertIn("不影响按已配置模型使用", result["warning"])
        provider = core.provider_by_id("refresh_403_relay")
        self.assertEqual(provider["lastCheckStatus"], "stale")
        self.assertEqual(provider["modelDiscoveryState"], "stale")

    def test_provider_model_refresh_reuses_cached_models_for_same_second_forbidden_probes(self):
        def forbidden(request, timeout=0):
            raise core.urllib.error.HTTPError(
                request.full_url,
                403,
                "forbidden",
                {},
                io.BytesIO(),
            )

        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            core.save_provider(
                {
                    "id": "same_second_403_relay",
                    "name": "Same Second 403 Relay",
                    "baseUrl": "https://same-second-403.example.test/v1",
                    "envKey": "SAME_SECOND_403_RELAY_KEY",
                    "models": ["cached-model"],
                }
            )
            core.store_provider_key("same_second_403_relay", "sk-same-second-403")
            with (
                patch.object(core, "_open_same_origin_request", side_effect=forbidden),
                patch.object(core, "now_iso", return_value="2026-08-19T12:34:56+08:00"),
            ):
                first = core.refresh_provider_models("same_second_403_relay")
                second = core.refresh_provider_models("same_second_403_relay")

        self.assertEqual(first["status"], "stale")
        self.assertEqual(second["status"], "stale")
        self.assertEqual(first["models"], ["cached-model"])
        self.assertEqual(second["models"], ["cached-model"])
        self.assertEqual(
            core.provider_by_id("same_second_403_relay")["lastCheckedAt"],
            "2026-08-19T12:34:56+08:00",
        )

    def test_provider_model_refresh_does_not_hide_401_with_cached_models(self):
        def unauthorized(request, timeout=0):
            raise core.urllib.error.HTTPError(
                request.full_url,
                401,
                "unauthorized",
                {},
                io.BytesIO(),
            )

        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            core.save_provider(
                {
                    "id": "refresh_401_relay",
                    "name": "Refresh 401 Relay",
                    "baseUrl": "https://refresh-401.example.test",
                    "envKey": "REFRESH_401_RELAY_KEY",
                    "models": ["manual-model"],
                }
            )
            core.store_provider_key("refresh_401_relay", "sk-refresh-401")
            with (
                patch.object(core, "_open_same_origin_request", side_effect=unauthorized),
                self.assertRaisesRegex(core.ManagerError, "HTTP 401"),
            ):
                core.refresh_provider_models("refresh_401_relay")

        provider = core.provider_by_id("refresh_401_relay")
        self.assertEqual(provider["lastCheckStatus"], "error")
        self.assertEqual(provider["modelDiscoveryState"], "error")
        source = next(
            item
            for item in core.model_sources(core.load_settings(), local_models=[])
            if item["recordId"] == "refresh_401_relay"
        )
        self.assertFalse(source["available"])
        self.assertIn("401/402", source["invalidReason"])

    def test_settings_migration_recovers_legacy_catalog_403_with_manual_models(self):
        settings = core._initial_settings()
        settings["providers"].append(
            {
                "id": "legacy_403_relay",
                "kind": "custom",
                "name": "Legacy 403 Relay",
                "baseUrl": "https://legacy-403.example.test/v1",
                "envKey": "LEGACY_403_RELAY_KEY",
                "models": ["manual-model"],
                "lastCheckStatus": "error",
                "lastCheckError": "https://legacy-403.example.test/v1/models: HTTP 403",
                "modelDiscoveryState": "error",
                "modelDiscoveryError": "HTTP 403",
            }
        )

        migrated, changed = core._migrate_settings(settings)
        provider = next(item for item in migrated["providers"] if item["id"] == "legacy_403_relay")

        self.assertTrue(changed)
        self.assertEqual(provider["lastCheckStatus"], "stale")
        self.assertEqual(provider["modelDiscoveryState"], "stale")
        self.assertIn("HTTP 403", provider["modelDiscoveryError"])

    def test_settings_migration_keeps_legacy_catalog_401_invalid(self):
        settings = core._initial_settings()
        settings["providers"].append(
            {
                "id": "legacy_401_relay",
                "kind": "custom",
                "name": "Legacy 401 Relay",
                "baseUrl": "https://legacy-401.example.test/v1",
                "envKey": "LEGACY_401_RELAY_KEY",
                "models": ["manual-model"],
                "lastCheckStatus": "error",
                "lastCheckError": "HTTP 401: unauthorized",
                "modelDiscoveryState": "error",
                "modelDiscoveryError": "HTTP 401: unauthorized",
            }
        )

        migrated, _changed = core._migrate_settings(settings)
        provider = next(item for item in migrated["providers"] if item["id"] == "legacy_401_relay")

        self.assertEqual(provider["lastCheckStatus"], "error")
        self.assertEqual(provider["modelDiscoveryState"], "error")

    def test_api_account_manual_model_rejects_401_and_rolls_back(self):
        def unauthorized(request, timeout=0):
            raise core.urllib.error.HTTPError(
                request.full_url,
                401,
                "unauthorized",
                {},
                io.BytesIO(),
            )

        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "_open_same_origin_request", side_effect=unauthorized),
            self.assertRaisesRegex(core.ManagerError, "401/402"),
        ):
            core.import_api_account(
                {
                    "id": "import_401_relay",
                    "name": "Import 401 Relay",
                    "baseUrl": "https://import-401.example.test",
                    "envKey": "IMPORT_401_RELAY_KEY",
                    "key": "sk-import-401",
                    "model": "manual-model",
                    "activate": False,
                }
            )

        self.assertFalse(
            any(item.get("id") == "import_401_relay" for item in core.load_settings()["providers"])
        )

    def test_provider_model_probe_retry_clears_stale_state_and_preserves_pinned_base(self):
        class FakeResponse(io.BytesIO):
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        calls = []

        def probe(request, timeout=0):
            calls.append(request.full_url)
            if len(calls) == 1:
                raise core.urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, io.BytesIO())
            return FakeResponse(json.dumps({"data": [{"id": "fresh-model"}]}).encode())

        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            core.save_provider(
                {
                    "id": "retry_pinned_relay",
                    "name": "Retry Pinned Relay",
                    "baseUrl": "https://retry-pinned.example.test",
                    "resolvedBaseUrl": "https://retry-pinned.example.test/v1",
                    "envKey": "RETRY_PINNED_RELAY_KEY",
                    "models": ["cached-model"],
                }
            )
            core.store_provider_key("retry_pinned_relay", "sk-retry-pinned")
            with patch.object(core, "_open_same_origin_request", side_effect=probe):
                with self.assertRaisesRegex(core.ManagerError, "HTTP 403"):
                    core.fetch_provider_models("retry_pinned_relay")
                stale = core.provider_by_id("retry_pinned_relay")
                self.assertEqual(stale["lastCheckStatus"], "stale")
                self.assertEqual(core.fetch_provider_models("retry_pinned_relay"), ["fresh-model"])

        provider = core.provider_by_id("retry_pinned_relay")
        self.assertEqual(calls, [
            "https://retry-pinned.example.test/v1/models",
            "https://retry-pinned.example.test/v1/models",
        ])
        self.assertEqual(provider["resolvedBaseUrl"], "https://retry-pinned.example.test/v1")
        self.assertEqual(provider["lastCheckStatus"], "ok")
        self.assertIsNone(provider["lastCheckError"])

    def test_api_account_manual_model_403_rolls_back_settings_and_secret_on_late_failure(self):
        core.ensure_state()
        before_settings = core.SETTINGS_FILE.read_bytes()
        before_secrets = core.SECRETS_FILE.read_bytes() if core.SECRETS_FILE.exists() else None

        def forbidden(request, timeout=0):
            raise core.urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, io.BytesIO())

        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "_open_same_origin_request", side_effect=forbidden),
            self.assertRaisesRegex(core.ManagerError, "主模型推理强度无效"),
        ):
            core.import_api_account(
                {
                    "id": "rollback_manual_403",
                    "name": "Rollback Manual 403",
                    "baseUrl": "https://rollback-manual-403.example.test/v1",
                    "envKey": "ROLLBACK_MANUAL_403_KEY",
                    "key": "sk-rollback-manual-403",
                    "model": "rollback-model",
                    "effort": "invalid-effort",
                    "activate": False,
                }
            )

        self.assertEqual(core.SETTINGS_FILE.read_bytes(), before_settings)
        self.assertEqual(
            core.SECRETS_FILE.read_bytes() if core.SECRETS_FILE.exists() else None,
            before_secrets,
        )
        with self.assertRaises(core.ManagerError):
            core.provider_by_id("rollback_manual_403")

    def test_api_account_can_join_local_reverse_pool_on_import(self):
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(core, "fetch_provider_models", return_value=["relay-model"]),
        ):
            result = core.import_api_account(
                {
                    "id": "pooled_relay",
                    "name": "Pooled Relay",
                    "baseUrl": "https://relay.invalid/v1",
                    "envKey": "POOLED_RELAY_KEY",
                    "key": "sk-pooled",
                    "proxyEnabled": True,
                    "activate": False,
                }
            )
            settings = core.load_settings()
            self.assertTrue(result["proxyEnabled"])
            self.assertIn("pooled_relay", settings["web2api"]["providerIds"])
            self.assertIn("provider:pooled_relay", settings["web2api"]["sourceOrder"])
            records = core.web2api_pool_model_records(settings)
            self.assertEqual([item["id"] for item in records], ["relay-model"])
            self.assertEqual(core.resolve_model_route("relay-model", settings)["sourceKind"], "provider")

    def test_api_account_import_rolls_back_on_late_validation_failure(self):
        core.ensure_state()
        before_settings = core.SETTINGS_FILE.read_bytes()
        with self.assertRaises(core.ManagerError):
            core.import_api_account(
                {
                    "id": "atomic_import_relay",
                    "name": "Atomic Import Relay",
                    "baseUrl": "https://atomic-import.example.test/v1",
                    "envKey": "ATOMIC_IMPORT_KEY",
                    "models": ["atomic-model"],
                    "fetchModels": False,
                    "effort": "invalid-effort",
                }
            )
        self.assertEqual(core.SETTINGS_FILE.read_bytes(), before_settings)

    def test_disabling_subagents_writes_no_managed_agent_specs(self):
        settings = core._initial_settings()
        settings["subagentRouting"]["strategyId"] = "verification_first"
        block = core.build_routing_block(settings)
        self.assertEqual(core._managed_subagent_specs(settings), [])
        self.assertEqual(block, "")
        self.assertNotIn("Ordered difficulty routes", block)

    def test_plan_label_only_reports_detected_multiplier(self):
        pro = core._parse_chatgpt_usage({"plan_type": "pro"})
        pro_lite = core._parse_chatgpt_usage({"plan_type": "chatgptprolite"})
        pro_max = core._parse_chatgpt_usage({"plan_type": "chatgptpromax"})
        pro_twenty = core._parse_chatgpt_usage(
            {"subscription": {"subscription_plan": "ChatGPT Pro 20x"}}
        )
        self.assertEqual(pro["planLabel"], "Pro")
        self.assertEqual(pro_lite["planLabel"], "Pro 5x")
        self.assertEqual(pro_max["planLabel"], "Pro 20x")
        self.assertEqual(pro_twenty["planLabel"], "Pro 20x")

    def test_single_official_window_is_weekly_and_parses_reset_credit_details(self):
        usage = core._parse_chatgpt_usage(
            {
                "plan_type": "pro_5x",
                "rate_limit": {
                    "secondary_window": {
                        "used_percent": 41,
                        "limit_window_seconds": 604_800,
                        "reset_at": 2_000_000_000,
                    }
                },
                "rate_limit_reset_credits": {"available_count": 1},
            },
            {
                "available_count": 1,
                "credits": [
                    {
                        "id": "reset-1",
                        "status": "available",
                        "granted_at": 1_999_000_000,
                        "expires_at": 2_001_000_000,
                        "title": "Full reset",
                    }
                ],
            },
        )
        self.assertNotIn("hourly", usage)
        self.assertNotIn("hourlyUnlimited", usage)
        self.assertEqual(usage["weekly"]["remainingPercent"], 59)
        self.assertEqual(usage["resetCredits"]["availableCount"], 1)
        self.assertEqual(usage["resetCredits"]["credits"][0]["id"], "reset-1")
        self.assertTrue(usage["resetCredits"]["credits"][0]["expiresAt"])

    def test_reset_credit_count_and_rows_merge_across_cockpit_and_app_server_shapes(self):
        reset = core._parse_reset_credits(
            {
                "available_count": 3,
                "credits": [{"id": "card-1", "status": "available"}],
            },
            {"rateLimitResetCredits": {"availableCount": 3}},
        )

        self.assertEqual(reset["availableCount"], 3)
        self.assertTrue(reset["detailsAvailable"])
        self.assertEqual(reset["credits"][0]["id"], "card-1")

    def test_app_server_rate_limit_shape_parses_weekly_quota_and_reset_cards(self):
        usage = core._parse_chatgpt_usage(
            {
                "planType": "pro_5x",
                "rateLimits": {
                    "primary": {
                        "usedPercent": 10,
                        "windowDurationMins": 300,
                        "resetsAt": 2_000_000_000,
                    },
                    "secondary": {
                        "usedPercent": 45,
                        "windowDurationMins": 10_080,
                        "resetsAt": 2_000_100_000,
                    },
                },
                "rateLimitResetCredits": {"availableCount": 3},
            }
        )

        self.assertEqual(usage["planLabel"], "Pro 5x")
        self.assertEqual(usage["weekly"]["remainingPercent"], 55)
        self.assertEqual(usage["weekly"]["windowMinutes"], 10_080)
        self.assertEqual(usage["resetCredits"]["availableCount"], 3)

    def test_sparse_usage_keeps_last_known_weekly_quota_marked_stale(self):
        previous = {
            "remainingPercent": 72,
            "usedPercent": 28,
            "resetAt": "2030-01-01T00:00:00+00:00",
            "windowMinutes": 10_080,
        }

        preserved = core._merge_quota_window_snapshot(previous, None)
        current = core._merge_quota_window_snapshot(
            previous,
            {"remainingPercent": 61, "usedPercent": 39},
        )

        self.assertEqual(preserved["remainingPercent"], 72)
        self.assertTrue(preserved["stale"])
        self.assertEqual(current["remainingPercent"], 61)
        self.assertFalse(current["stale"])

    def test_account_model_refresh_extends_a_fully_selected_source_only(self):
        settings = core._initial_settings()
        settings["accounts"] = [
            {
                "id": "new-model-account",
                "authMode": "chatgpt",
                "sourceType": "codex_auth",
                "codexCompatible": True,
                "models": ["gpt-old-a", "gpt-old-b"],
            }
        ]
        workspace = settings["modelWorkspace"]
        workspace.update(
            {
                "selectAll": False,
                "selectedModels": [
                    "account:new-model-account::gpt-old-a",
                    "account:new-model-account::gpt-old-b",
                ],
            }
        )

        core._merge_account_updates(
            settings,
            "new-model-account",
            {"models": ["gpt-old-a", "gpt-6-astra"]},
        )

        self.assertIn(
            "account:new-model-account::gpt-6-astra",
            workspace["selectedModels"],
        )
        self.assertNotIn(
            "account:new-model-account::gpt-old-b",
            workspace["selectedModels"],
        )

        workspace["selectedModels"] = ["account:new-model-account::gpt-old-a"]
        core._merge_account_updates(
            settings,
            "new-model-account",
            {"models": ["gpt-old-a", "gpt-6-astra", "gpt-later"]},
        )
        self.assertNotIn("account:new-model-account::gpt-later", workspace["selectedModels"])

    def test_full_official_selection_uses_native_catalog_but_curated_and_vpn_modes_do_not(self):
        settings = core._initial_settings()
        account_id = "native-catalog-account"
        models = ["gpt-official-a", "gpt-official-b"]
        settings["accounts"] = [
            {
                "id": account_id,
                "label": "Native Catalog",
                "authMode": "chatgpt",
                "sourceType": "codex_auth",
                "codexCompatible": True,
                "models": models,
            }
        ]
        settings["modelWorkspace"].update(
            {
                "mode": "independent",
                "activeSourceId": f"account:{account_id}",
                "selectAll": False,
                "selectedModels": [f"account:{account_id}::{model}" for model in models],
                "defaultModelKey": f"account:{account_id}::{models[0]}",
                "syncToCodex": True,
            }
        )

        with patch.object(core, "current_auth_state", return_value={"activeAccountId": account_id}):
            native_config = tomllib.loads(core.build_codex_config(settings))
            native_records = core.selected_model_records(settings)
        self.assertTrue(core._use_native_official_model_catalog(settings, native_records))
        self.assertNotIn("model_catalog_json", native_config)

        curated = json.loads(json.dumps(settings))
        curated["modelWorkspace"]["selectedModels"] = [f"account:{account_id}::{models[0]}"]
        with patch.object(core, "current_auth_state", return_value={"activeAccountId": account_id}):
            curated_config = tomllib.loads(core.build_codex_config(curated))
        self.assertEqual(curated_config["model_catalog_json"], str(core.MODEL_CATALOG_FILE))

        vpn = json.loads(json.dumps(settings))
        vpn["runtimeTuning"].update(
            {
                "configManaged": True,
                "managedFields": ["vpnCompatibility"],
                "vpnCompatibility": True,
            }
        )
        with patch.object(core, "current_auth_state", return_value={"activeAccountId": account_id}):
            vpn_config = tomllib.loads(core.build_codex_config(vpn))
        self.assertEqual(vpn_config["model_catalog_json"], str(core.MODEL_CATALOG_FILE))

    def test_astra_reasoning_metadata_is_known_before_local_catalog_refresh(self):
        metadata = core._model_reasoning_metadata(
            "gpt-6-astra",
            core._reasoning_capabilities([]),
        )

        self.assertTrue(metadata["reasoningKnown"])
        self.assertEqual(
            metadata["efforts"],
            ["low", "medium", "high", "xhigh", "max", "ultra"],
        )
        self.assertIn("ultra", metadata["efforts"])
        self.assertLess(
            core._model_sort_key("gpt-6-astra"),
            core._model_sort_key("gpt-5.6-sol"),
        )

    def test_sparse_successful_usage_probe_preserves_visible_quota(self):
        account = {
            "id": "sparse-usage",
            "authMode": "chatgpt",
            "sourceType": "codex_auth",
            "plan": "pro",
            "planLabel": "Pro",
            "models": ["gpt-cached"],
            "modelsLastCheckedAt": core.now_iso(),
            "subscriptionExpiresAt": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
            "usage": {
                "plan": "pro",
                "planLabel": "Pro",
                "weekly": {
                    "remainingPercent": 72,
                    "usedPercent": 28,
                    "windowMinutes": 10_080,
                },
            },
        }
        with (
            patch.object(core, "load_settings", return_value={"accounts": [account]}),
            patch.object(core, "_load_account_snapshot", return_value={}),
            patch.object(core, "_decode_snapshot_files", return_value={"auth.json": b"{}"}),
            patch.object(
                core,
                "_chatgpt_credentials_from_auth_bytes",
                return_value={
                    "accessToken": "token",
                    "accountId": "workspace",
                    "subscriptionExpiresAt": account["subscriptionExpiresAt"],
                },
            ),
            patch.object(core, "_fetch_chatgpt_json", return_value={"plan_type": "pro"}),
        ):
            updates = core._probe_codex_account("sparse-usage", parallel_operations=False)

        self.assertEqual(updates["usage"]["weekly"]["remainingPercent"], 72)
        self.assertTrue(updates["usage"]["weekly"]["stale"])
        self.assertEqual(updates["refreshState"], "partial")
        self.assertIn("usageQuota", updates["refreshErrors"])

    def test_sparse_reset_credit_responses_preserve_unexpired_card(self):
        previous = {
            "availableCount": 1,
            "detailsAvailable": True,
            "credits": [
                {
                    "id": "still-valid",
                    "expiresAt": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
                }
            ],
        }
        self.assertIsNone(core._parse_reset_credits({"plan_type": "pro"}))
        preserved_unknown = core._merge_reset_credit_snapshot(previous, None)
        self.assertEqual(preserved_unknown["availableCount"], 1)
        preserved_zero = core._merge_reset_credit_snapshot(
            previous,
            {"availableCount": 0, "credits": [], "detailsAvailable": True},
        )
        self.assertEqual(preserved_zero["availableCount"], 1)
        self.assertTrue(preserved_zero["stale"])
        cleared = core._merge_reset_credit_snapshot(
            previous,
            {"availableCount": 0, "credits": [], "detailsAvailable": True},
            allow_clear=True,
        )
        self.assertEqual(cleared["availableCount"], 0)

    def test_reset_detail_transport_error_does_not_hide_or_degrade_cached_card_rows(self):
        account = {
            "id": "cached-reset-detail",
            "authMode": "chatgpt",
            "sourceType": "codex_auth",
            "codexCompatible": True,
            "plan": "pro",
            "planLabel": "Pro",
            "models": ["gpt-cached"],
            "modelsLastCheckedAt": core.now_iso(),
            "subscriptionExpiresAt": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
            "usage": {
                "weekly": {"remainingPercent": 60, "usedPercent": 40, "windowMinutes": 10_080},
                "resetCredits": {
                    "availableCount": 1,
                    "detailsAvailable": True,
                    "credits": [{"id": "cached-card"}],
                },
            },
        }

        def fetch(url, *_args, **_kwargs):
            if url == core.CHATGPT_USAGE_URL:
                return {
                    "plan_type": "pro",
                    "rate_limit": {
                        "secondary_window": {
                            "used_percent": 40,
                            "limit_window_seconds": 604_800,
                        }
                    },
                    "rate_limit_reset_credits": {"available_count": 1},
                }
            raise core.ManagerError("temporary reset detail failure")

        with (
            patch.object(core, "load_settings", return_value={"accounts": [account]}),
            patch.object(core, "_account_chatgpt_credentials", return_value={
                "accessToken": "token",
                "accountId": "workspace",
                "subscriptionExpiresAt": account["subscriptionExpiresAt"],
            }),
            patch.object(core, "_fetch_chatgpt_json", side_effect=fetch),
            patch.object(core, "_live_official_account_matches", return_value=False),
        ):
            updates = core._probe_codex_account(
                "cached-reset-detail",
                include_reset_details=True,
            )

        self.assertEqual(updates["refreshState"], "ready")
        self.assertNotIn("resetCredits", updates["refreshErrors"])
        self.assertEqual(updates["usage"]["resetCredits"]["credits"][0]["id"], "cached-card")

    def test_pending_reset_redemption_is_idempotent_and_serialized(self):
        core.ensure_state()
        settings = core.load_settings()
        settings["accounts"] = [
            {
                "id": "reset-account",
                "authMode": "chatgpt",
                "groupId": "official",
                "usage": {
                    "resetCredits": {
                        "availableCount": 1,
                        "detailsAvailable": True,
                        "credits": [{"id": "reset-1"}],
                    }
                },
            }
        ]
        core.save_settings(settings)

        observed_request_ids = []

        def uncertain_then_success(_url, _token, _account, payload):
            observed_request_ids.append(payload["redeem_request_id"])
            if len(observed_request_ids) == 1:
                raise core.ManagerError("ChatGPT 临时中断了安全连接；结果尚不确定。")
            return {"outcome": "already_redeemed"}

        with (
            patch.object(core, "_load_account_snapshot", return_value={}),
            patch.object(core, "_decode_snapshot_files", return_value={"auth.json": b"{}"}),
            patch.object(
                core,
                "_chatgpt_credentials_from_auth_bytes",
                return_value={"accessToken": "token", "accountId": "workspace"},
            ),
            patch.object(core, "_post_chatgpt_json", side_effect=uncertain_then_success),
            patch.object(core, "refresh_codex_account", side_effect=lambda account_id, **_kwargs: next(
                item for item in core.load_settings()["accounts"] if item["id"] == account_id
            )),
        ):
            with self.assertRaisesRegex(core.ManagerError, "尚不确定"):
                core.consume_account_reset_credit("reset-account")
            pending = core.load_settings()["accounts"][0]["usage"]["resetCredits"]["pendingRedeem"]
            result = core.consume_account_reset_credit("reset-account")

        self.assertEqual(observed_request_ids, [pending["requestId"], pending["requestId"]])
        self.assertEqual(result["outcome"], "alreadyRedeemed")
        final_reset = core.load_settings()["accounts"][0]["usage"]["resetCredits"]
        self.assertEqual(final_reset["availableCount"], 0)
        self.assertNotIn("pendingRedeem", final_reset)

    def test_usage_confirmation_clears_pending_reset_even_with_future_credit_expiry(self):
        previous = {
            "availableCount": 1,
            "detailsAvailable": True,
            "pendingRedeem": {
                "requestId": "same-request",
                "previousCount": 1,
                "startedAt": core.now_iso(),
            },
            "credits": [
                {
                    "id": "spent-card",
                    "expiresAt": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
                }
            ],
        }

        merged = core._merge_reset_credit_snapshot(
            previous,
            {"availableCount": 0, "credits": [], "detailsAvailable": True},
        )

        self.assertEqual(merged["availableCount"], 0)
        self.assertNotIn("pendingRedeem", merged)

    def test_schema_nine_recovers_only_unexpired_reset_details_from_backup(self):
        settings = core._initial_settings()
        settings["schemaVersion"] = 9
        settings["accounts"] = [
            {
                "id": "recover-card",
                "authMode": "chatgpt",
                "groupId": "official",
                "usage": {
                    "resetCredits": {
                        "availableCount": 0,
                        "credits": [],
                        "detailsAvailable": False,
                    }
                },
            }
        ]
        backup = json.loads(json.dumps(settings))
        backup["accounts"][0]["usage"]["resetCredits"] = {
            "availableCount": 1,
            "detailsAvailable": True,
            "credits": [
                {
                    "id": "recovered-card",
                    "expiresAt": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                }
            ],
        }
        core.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        core.atomic_write_json(core.BACKUPS_DIR / "settings.json.20260810-100000-000001.bak", backup)
        core.atomic_write_json(core.SETTINGS_FILE, settings)

        migrated = core.load_settings()

        reset = migrated["accounts"][0]["usage"]["resetCredits"]
        self.assertEqual(migrated["schemaVersion"], core.SCHEMA_VERSION)
        self.assertEqual(reset["availableCount"], 1)
        self.assertEqual(reset["credits"][0]["id"], "recovered-card")
        self.assertTrue(reset["stale"])

    def test_schema_ten_migrates_to_eleven_without_blocking_startup(self):
        settings = core._initial_settings()
        settings["schemaVersion"] = 10
        settings["web2api"].pop("providerIds", None)
        settings["web2api"].pop("sourceOrder", None)
        settings["providers"].append(
            {
                "id": "relay-provider",
                "name": "Relay",
                "kind": "custom",
                "baseUrl": "https://relay.invalid/v1",
                "groupId": "relay",
            }
        )
        core.atomic_write_json(core.SETTINGS_FILE, settings)

        migrated = core.load_settings()

        self.assertEqual(migrated["schemaVersion"], core.SCHEMA_VERSION)
        self.assertEqual(migrated["web2api"]["providerIds"], [])
        self.assertEqual(migrated["web2api"]["sourceOrder"], [])

    def test_active_official_probe_falls_back_to_app_server_for_models_and_reset_count(self):
        account = {
            "id": "active-fallback",
            "authMode": "chatgpt",
            "sourceType": "codex_auth",
            "codexCompatible": True,
            "fingerprint": "active-fingerprint",
            "plan": "pro_5x",
            "planLabel": "Pro 5x",
            "models": ["gpt-cached"],
            "modelsLastCheckedAt": "2020-01-01T00:00:00+00:00",
            "subscriptionExpiresAt": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
            "usage": {
                "weekly": {"remainingPercent": 70, "usedPercent": 30, "windowMinutes": 10_080}
            },
        }
        rate_limits = {
            "rateLimits": {
                "secondary": {
                    "usedPercent": 44,
                    "windowDurationMins": 10_080,
                    "resetsAt": 2_000_000_000,
                }
            },
            "rateLimitResetCredits": {"availableCount": 3},
        }
        with (
            patch.object(core, "load_settings", return_value={"accounts": [account]}),
            patch.object(core, "_account_chatgpt_credentials", return_value={
                "accessToken": "token",
                "accountId": "workspace",
                "subscriptionExpiresAt": account["subscriptionExpiresAt"],
            }),
            patch.object(core, "_fetch_chatgpt_json", side_effect=core.ManagerError("direct offline")),
            patch.object(core, "_live_official_account_matches", return_value=True),
            patch.object(
                core,
                "_active_codex_model_catalog",
                return_value={"data": [{"id": "gpt-6-astra"}, {"id": "gpt-cached"}]},
            ),
            patch.object(core, "_active_codex_rate_limits", return_value=rate_limits),
        ):
            updates = core._probe_codex_account(
                "active-fallback",
                parallel_operations=False,
                force_metadata=True,
            )

        self.assertEqual(updates["models"], ["gpt-6-astra", "gpt-cached"])
        self.assertEqual(updates["modelsSource"], "codex_app_server")
        self.assertEqual(updates["usage"]["weekly"]["remainingPercent"], 56)
        self.assertEqual(updates["usage"]["resetCredits"]["availableCount"], 3)
        self.assertNotEqual(updates["refreshState"], "error")

    def test_background_account_probe_uses_cached_metadata_and_requests_usage_only(self):
        account = {
            "id": "cached-metadata",
            "authMode": "chatgpt",
            "sourceType": "codex_auth",
            "plan": "plus",
            "planLabel": "Plus",
            "models": ["gpt-cached"],
            "modelsLastCheckedAt": core.now_iso(),
            "subscriptionExpiresAt": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
            "usage": {
                "resetCredits": {
                    "availableCount": 0,
                    "credits": [],
                    "detailsAvailable": True,
                    "detailsCheckedAt": core.now_iso(),
                }
            },
        }
        calls = []

        def fetch(url, *_args, **_kwargs):
            calls.append(url)
            return {
                "plan_type": "plus",
                "rate_limit": {
                    "secondary_window": {
                        "used_percent": 10,
                        "limit_window_seconds": 604_800,
                    }
                },
            }

        with (
            patch.object(core, "load_settings", return_value={"accounts": [account]}),
            patch.object(core, "_load_account_snapshot", return_value={}),
            patch.object(core, "_decode_snapshot_files", return_value={"auth.json": b"{}"}),
            patch.object(
                core,
                "_chatgpt_credentials_from_auth_bytes",
                return_value={
                    "accessToken": "token",
                    "accountId": "workspace",
                    "subscriptionExpiresAt": account["subscriptionExpiresAt"],
                },
            ),
            patch.object(core, "_fetch_chatgpt_json", side_effect=fetch),
        ):
            updates = core._probe_codex_account("cached-metadata", parallel_operations=False)

        self.assertEqual(calls, [core.CHATGPT_USAGE_URL])
        self.assertEqual(updates["refreshState"], "ready")
        self.assertEqual(updates["usage"]["resetCredits"]["availableCount"], 0)

    def test_partial_tls_failure_preserves_metadata_and_uses_short_backoff(self):
        expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        account = {
            "id": "tls-partial",
            "authMode": "chatgpt",
            "sourceType": "codex_auth",
            "plan": "pro",
            "planLabel": "Pro 5x",
            "models": ["gpt-cached"],
            "modelsLastCheckedAt": "2020-01-01T00:00:00+00:00",
            "subscriptionExpiresAt": expired,
            "usage": {
                "resetCredits": {
                    "availableCount": 1,
                    "detailsAvailable": True,
                    "detailsCheckedAt": core.now_iso(),
                    "credits": [
                        {
                            "id": "cached-reset",
                            "expiresAt": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                        }
                    ],
                }
            },
        }

        def fetch(url, *_args, **_kwargs):
            if url == core.CHATGPT_USAGE_URL:
                return {
                    "plan_type": "pro",
                    "rate_limit": {
                        "secondary_window": {
                            "used_percent": 25,
                            "limit_window_seconds": 604_800,
                        }
                    },
                }
            if url == core.CHATGPT_MODELS_URL:
                raise core.ManagerError(
                    "ChatGPT 临时中断了安全连接；已自动重试仍未成功，请稍后再刷新。"
                )
            raise AssertionError(f"unexpected endpoint: {url}")

        with (
            patch.object(core, "load_settings", return_value={"accounts": [account]}),
            patch.object(core, "_load_account_snapshot", return_value={}),
            patch.object(core, "_decode_snapshot_files", return_value={"auth.json": b"{}"}),
            patch.object(
                core,
                "_chatgpt_credentials_from_auth_bytes",
                return_value={
                    "accessToken": "token",
                    "accountId": "workspace",
                    "subscriptionExpiresAt": expired,
                },
            ),
            patch.object(core, "_fetch_chatgpt_json", side_effect=fetch),
            patch.object(
                core,
                "_fetch_chatgpt_subscription_status",
                side_effect=core.ManagerError(
                    "ChatGPT 临时中断了安全连接；已自动重试仍未成功，请稍后再刷新。"
                ),
            ),
        ):
            updates = core._probe_codex_account(
                "tls-partial",
                parallel_operations=False,
                force_metadata=True,
            )

        self.assertEqual(updates["refreshState"], "partial")
        self.assertNotIn("modelsLastCheckedAt", updates)
        self.assertEqual(updates["usage"]["resetCredits"]["availableCount"], 1)
        next_refresh = datetime.fromisoformat(updates["nextRefreshAt"])
        subscription_retry = datetime.fromisoformat(updates["subscriptionNextRetryAt"])
        now = datetime.now(timezone.utc)
        self.assertLessEqual(next_refresh, now + timedelta(seconds=core.ACCOUNT_REFRESH_ERROR_RETRY_SECONDS + 5))
        self.assertLessEqual(subscription_retry, now + timedelta(seconds=core.ACCOUNT_REFRESH_ERROR_RETRY_SECONDS + 5))

    def test_web_session_preview_rejects_unusable_or_expired_codex_credentials(self):
        missing_account = {
            "accessToken": self.jwt({"exp": 2_000_000_000}),
            "user": {"email": "missing@example.test"},
        }
        expired = {
            "accessToken": self.jwt(
                {
                    "exp": 1,
                    "https://api.openai.com/auth": {"chatgpt_account_id": "expired-workspace"},
                }
            ),
            "account": {"id": "expired-workspace"},
            "user": {"email": "expired@example.test"},
        }
        opaque_expired = {
            "accessToken": "opaque-session-token",
            "expires": "2020-01-01T00:00:00Z",
            "account": {"id": "opaque-expired-workspace"},
            "user": {"email": "opaque-expired@example.test"},
        }
        preview = core.preview_codex_accounts_batch(
            {
                "groupId": "official",
                "items": [
                    {"authJson": json.dumps(missing_account)},
                    {"authJson": json.dumps(expired)},
                    {"authJson": json.dumps(opaque_expired)},
                ],
            }
        )
        self.assertEqual(preview["valid"], 0)
        self.assertTrue(all(not item["codexCompatible"] for item in preview["items"]))
        self.assertIn("Account ID", preview["items"][0]["error"])
        self.assertIn("已过期", preview["items"][1]["error"])
        self.assertIn("已过期", preview["items"][2]["error"])

    def test_oauth_login_uses_official_browser_flow_supports_reopen_cancel_and_restart(self):
        class FakeServer:
            def __init__(self):
                self.closed = threading.Event()

            def serve_forever(self, poll_interval=0.1):
                self.closed.wait(5)

            def shutdown(self):
                self.closed.set()

            def server_close(self):
                self.closed.set()

        first_server = FakeServer()
        second_server = FakeServer()
        scheduled = []
        oauth = app.OAuthDeviceLogin(
            timeout_seconds=30,
            on_account_saved=lambda account_ids: scheduled.extend(account_ids),
        )
        with (
            patch.object(core, "load_settings", return_value={}),
            patch.object(core, "_account_group", return_value={"id": "official"}),
            patch.object(
                oauth,
                "_bind_callback_server",
                side_effect=[(first_server, 1455), (second_server, 1455)],
            ) as bind,
            patch.object(app, "open_browser_window", return_value=True) as open_browser,
            patch.object(
                oauth,
                "_exchange_code",
                return_value={
                    "id_token": "id-token",
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                },
            ),
            patch.object(
                core,
                "save_codex_account",
                return_value={"id": "saved", "label": "Browser Login"},
            ) as save,
        ):
            first = oauth.start({"groupId": "official", "label": "Browser Login"})
            self.assertEqual(first["status"], "waiting")
            self.assertTrue(first["browserOpened"])
            self.assertIsNotNone(first["startedAt"])
            self.assertIsNotNone(first["expiresAt"])
            self.assertIn("https://auth.openai.com/oauth/authorize?", first["url"])
            self.assertEqual(first["callbackUrl"], "http://localhost:1455/auth/callback")
            self.assertEqual(bind.call_count, 1)

            reopened = oauth.open_browser()
            self.assertEqual(reopened["status"], "waiting")
            self.assertEqual(open_browser.call_count, 2)
            self.assertTrue(all(item.args == (first["url"],) for item in open_browser.call_args_list))

            callback = (
                f"http://localhost:1455/auth/callback?code=authorization-code"
                f"&state={oauth.expected_state}"
            )
            submitted = oauth.submit_callback(callback)
            self.assertIn(submitted["status"], {"exchanging", "completed"})
            completed = oauth.state()
            deadline = time.time() + 2
            while completed["status"] not in {"completed", "error"} and time.time() < deadline:
                time.sleep(0.01)
                completed = oauth.state()
            self.assertEqual(completed["status"], "completed")
            auth = json.loads(save.call_args.kwargs["auth_bytes"].decode("utf-8"))
            self.assertEqual(auth["tokens"]["access_token"], "access-token")
            self.assertEqual(auth["auth_mode"], "chatgpt")
            self.assertTrue(save.call_args.args[0]["deferRefresh"])
            self.assertEqual(scheduled, ["saved"])
            self.assertTrue(first_server.closed.is_set())

            restarted = oauth.start({"groupId": "official"})
            self.assertEqual(restarted["status"], "waiting")
            self.assertEqual(bind.call_count, 2)
            cancelled = oauth.cancel()
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertTrue(second_server.closed.is_set())

    def test_toolbox_mailbox_uses_short_cache_instead_of_repeating_imap_reads(self):
        runtime = app.ToolboxRuntime()
        upstream = [
            {
                "uid": "42",
                "subject": "Your code is 123456",
                "from": ["security@example.com"],
                "to": ["user@example.com"],
                "date": "2026-08-10T12:00:00+00:00",
                "unread": True,
                "text": "Use 123456 to continue.",
                "verification_codes": ["123456"],
                "truncated": False,
            }
        ]
        with patch.object(app.toolbox, "fetch_mail_messages", return_value=upstream) as fetch:
            first = runtime.fetch_mail("mail_example", limit=20, unread_only=False)
            second = runtime.fetch_mail("mail_example", limit=20, unread_only=False)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(first, second)
        self.assertEqual(first[0]["codes"], ["123456"])
        self.assertEqual(first[0]["from"], "security@example.com")

    def test_temporary_mailbox_bundle_is_not_saved_and_uses_short_cache(self):
        runtime = app.ToolboxRuntime()
        source = "user@outlook.com----mail-password----access-token"
        upstream = [
            {
                "uid": "43",
                "subject": "OpenAI code 654321",
                "from": ["security@openai.com"],
                "to": ["user@outlook.com"],
                "date": "2026-08-11T01:00:00+00:00",
                "unread": True,
                "text": "Your code is 654321.",
                "verification_codes": ["654321"],
                "truncated": False,
            }
        ]
        with patch.object(app.toolbox, "fetch_email_messages", return_value=upstream) as fetch:
            first = runtime.fetch_temporary_mail(source, limit=10, unread_only=False)
            second = runtime.fetch_temporary_mail(source, limit=10, unread_only=False)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(first, second)
        self.assertEqual(first[0]["codes"], ["654321"])
        self.assertEqual(fetch.call_args.args[0]["access_token"], "access-token")
        self.assertNotIn(
            source,
            json.dumps(list(runtime.temporary_mail_cache.values()), ensure_ascii=False),
        )

    def test_oauth_expiry_cleans_process_and_allows_fresh_start(self):
        class FakeServer:
            def __init__(self):
                self.closed = threading.Event()

            def serve_forever(self, poll_interval=0.1):
                self.closed.wait(5)

            def shutdown(self):
                self.closed.set()

            def server_close(self):
                self.closed.set()

        first_server = FakeServer()
        second_server = FakeServer()
        oauth = app.OAuthDeviceLogin(timeout_seconds=30)
        with (
            patch.object(core, "load_settings", return_value={}),
            patch.object(core, "_account_group", return_value={"id": "official"}),
            patch.object(
                oauth,
                "_bind_callback_server",
                side_effect=[(first_server, 1455), (second_server, 1455)],
            ) as bind,
            patch.object(app, "open_browser_window", return_value=True),
        ):
            oauth.start({"groupId": "official"})
            with oauth.lock:
                oauth.deadline_monotonic = time.monotonic() - 1
            expired = oauth.state()
            self.assertEqual(expired["status"], "expired")
            self.assertIn("超时", expired["error"])
            self.assertTrue(first_server.closed.is_set())

            restarted = oauth.start({"groupId": "official"})
            self.assertEqual(restarted["status"], "waiting")
            self.assertEqual(bind.call_count, 2)
            oauth.cancel()
            self.assertTrue(second_server.closed.is_set())

    def test_oauth_completion_and_failure_always_release_task_directory(self):
        class FakeServer:
            def __init__(self):
                self.closed = threading.Event()

            def serve_forever(self, poll_interval=0.1):
                self.closed.wait(5)

            def shutdown(self):
                self.closed.set()

            def server_close(self):
                self.closed.set()

        servers = [FakeServer(), FakeServer()]
        oauth = app.OAuthDeviceLogin(timeout_seconds=30)
        with (
            patch.object(core, "load_settings", return_value={}),
            patch.object(core, "_account_group", return_value={"id": "official"}),
            patch.object(
                oauth,
                "_bind_callback_server",
                side_effect=[(servers[0], 1455), (servers[1], 1455)],
            ),
            patch.object(app, "open_browser_window", return_value=True),
            patch.object(core, "save_codex_account", return_value={"id": "saved", "label": "Saved"}) as save,
        ):
            oauth.start({"groupId": "official", "label": "Saved"})
            with patch.object(
                oauth,
                "_exchange_code",
                return_value={"id_token": "id", "access_token": "access", "refresh_token": "refresh"},
            ):
                oauth.submit_callback(
                    f"code=success&state={urllib.parse.quote(oauth.expected_state or '')}"
                )
            completed = oauth.state()
            deadline = time.time() + 2
            while completed["status"] not in {"completed", "error"} and time.time() < deadline:
                time.sleep(0.01)
                completed = oauth.state()
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["account"], {"id": "saved", "label": "Saved"})
            save.assert_called_once()
            self.assertTrue(servers[0].closed.is_set())

            oauth.start({"groupId": "official"})
            with patch.object(
                oauth,
                "_exchange_code",
                side_effect=core.ManagerError("OAuth Token 交换失败：HTTP 401。"),
            ):
                oauth.submit_callback(
                    f"/auth/callback?code=failure&state={urllib.parse.quote(oauth.expected_state or '')}"
                )
            failed = oauth.state()
            deadline = time.time() + 2
            while failed["status"] not in {"completed", "error"} and time.time() < deadline:
                time.sleep(0.01)
                failed = oauth.state()
            self.assertEqual(failed["status"], "error")
            self.assertIn("HTTP 401", failed["error"])
            self.assertTrue(servers[1].closed.is_set())

    def test_oauth_open_endpoint_reuses_captured_authorization_url(self):
        class FakeOAuth:
            def __init__(self):
                self.calls = 0

            def open_browser(self):
                self.calls += 1
                return {"status": "waiting", "url": "https://auth.openai.com/oauth/authorize?client_id=test"}

        class FakeRuntime:
            def __init__(self):
                self.oauth = FakeOAuth()

        runtime = FakeRuntime()
        server = app.ManagerServer(("127.0.0.1", 0), app.RequestHandler, runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = app.urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/oauth/open",
                data=b"",
                method="POST",
                headers={"X-Agent-Manager-Token": server.api_token},
            )
            with app.urllib.request.urlopen(request, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"]["status"], "waiting")
            self.assertEqual(runtime.oauth.calls, 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_oauth_callback_endpoint_forwards_manual_url_and_rejects_wrong_state(self):
        class FakeOAuth:
            def __init__(self):
                self.callback = None

            def submit_callback(self, callback):
                self.callback = callback
                return {"status": "exchanging"}

        class FakeRuntime:
            def __init__(self):
                self.oauth = FakeOAuth()

        runtime = FakeRuntime()
        server = app.ManagerServer(("127.0.0.1", 0), app.RequestHandler, runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        callback = "http://localhost:1455/auth/callback?code=test&state=state"
        try:
            request = app.urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/oauth/callback",
                data=json.dumps({"callbackUrl": callback}).encode("utf-8"),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Agent-Manager-Token": server.api_token,
                },
            )
            with app.urllib.request.urlopen(request, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"]["status"], "exchanging")
            self.assertEqual(runtime.oauth.callback, callback)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        oauth = app.OAuthDeviceLogin(timeout_seconds=30)
        with oauth.lock:
            oauth.callback_port = 1455
            oauth.expected_state = "expected"
            oauth.login_id = "login"
            oauth.data = oauth._new_state("waiting")
        with self.assertRaisesRegex(core.ManagerError, "state 校验失败"):
            oauth.submit_callback(
                "http://localhost:1455/auth/callback?code=test&state=wrong"
            )

    def test_api_pool_batch_membership_and_ordered_routing_preserve_user_order(self):
        core.ensure_state()
        settings = core.load_settings()
        settings["accounts"] = [
            {"id": "account-a", "label": "A", "authMode": "chatgpt", "models": ["gpt-x"], "proxyEnabled": False},
            {"id": "account-b", "label": "B", "authMode": "chatgpt", "models": ["gpt-x"], "proxyEnabled": False},
        ]
        core.save_settings(settings)
        first = core.set_accounts_proxy_enabled_batch(["account-b", "account-a"], True)
        self.assertEqual(first["pool"], ["account-b", "account-a"])
        core.save_web2api_settings({"routing": "ordered", "accountIds": ["account-a", "account-b"]})
        manager = web2api.Web2APIManager()
        self.assertEqual([item["id"] for item in manager._accounts()], ["account-a", "account-b"])
        removed = core.set_accounts_proxy_enabled_batch(["account-a"], False)
        self.assertEqual(removed["pool"], ["account-b"])

    def test_active_web2api_mode_writes_shared_pool_provider_and_model(self):
        core.ensure_state()
        settings = core.load_settings()
        settings["accounts"] = [
            {
                "id": "account-pool",
                "label": "Pool",
                "authMode": "chatgpt",
                "models": ["gpt-pool"],
                "groupId": "official",
                "refreshState": "ready",
                "proxyEnabled": True,
            }
        ]
        settings["web2api"]["accountIds"] = ["account-pool"]
        settings["web2api"]["activeForCodex"] = True
        core.save_settings(settings)
        with patch.object(core, "current_auth_state", return_value={"activeAccountId": None}):
            config = tomllib.loads(core.build_codex_config(core.load_settings()))
        self.assertEqual(config["model_provider"], core.AGGREGATE_PROVIDER_ID)
        self.assertEqual(config["model"], "gpt-pool")
        self.assertEqual(
            config["model_providers"][core.AGGREGATE_PROVIDER_ID]["base_url"],
            "http://127.0.0.1:17860/v1",
        )
        with patch.object(core, "current_auth_state", return_value={"activeAccountId": None}):
            self.assertIsNone(core.resolve_model_route("gpt-pool", core.load_settings()))
            models = web2api.Web2APIManager().models(access_scope="internal")
        model_ids = [item["id"] for item in models["data"]]
        self.assertEqual(model_ids[0], "gpt-pool")
        self.assertEqual(len([item for item in model_ids if item.startswith("cam-agent-")]), 4)

    def test_activate_web2api_starts_service_before_closing_and_relaunching_codex(self):
        settings = core._initial_settings()
        settings["web2api"]["accountIds"] = ["pool-account"]
        calls = []

        class FakeWeb2API:
            running = False

            def status(self):
                return {"running": self.running, "activeForCodex": settings["web2api"]["activeForCodex"]}

            def start(self):
                calls.append("service-start")
                self.running = True
                return self.status()

            def stop(self, disable=True):
                calls.append("service-stop")
                self.running = False
                return self.status()

        runtime = type("Runtime", (), {"web2api": FakeWeb2API()})()

        def activate(value, preferred_account_id=None):
            calls.append("activate" if value else "deactivate")
            settings["web2api"]["activeForCodex"] = value
            settings["web2api"]["activeAccountId"] = preferred_account_id if value else None
            return settings["web2api"]

        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "validate_web2api_codex_pool", return_value={"accounts": ["pool-account"], "models": ["gpt-test"]}),
            patch.object(core, "codex_prefix", return_value=["codex.exe"]),
            patch.object(core, "service_secret_configured", return_value=True),
            patch.object(core, "close_codex_processes", side_effect=lambda: calls.append("close") or {}),
            patch.object(core, "set_web2api_codex_active", side_effect=activate),
            patch.object(core, "apply_configuration", side_effect=lambda *_args: calls.append("apply") or {}),
            patch.object(core, "auto_sync_sessions_after_switch", side_effect=lambda *_args: calls.append("sync") or {}),
            patch.object(core, "launch_codex_app", side_effect=lambda **_kwargs: calls.append("launch") or {"started": True}),
            patch.object(core, "read_toml", return_value={"model": "gpt-test"}),
            patch.object(
                core,
                "wait_for_codex_runtime_ready",
                side_effect=lambda **_kwargs: calls.append("ready") or {"ready": True},
            ),
        ):
            result = app.activate_web2api_for_codex(runtime)
        self.assertEqual(calls, ["service-start", "close", "activate", "apply", "sync", "launch", "ready"])
        self.assertTrue(result["status"]["activeForCodex"])

    def test_web_session_switch_uses_isolated_local_conversion_without_mutating_pool(self):
        settings = core._initial_settings()
        settings["web2api"]["accountIds"] = ["other-account"]
        settings["web2api"]["routing"] = "quota_first"
        settings["accounts"] = [
            {
                "id": "web-account",
                "label": "Converted Web Session",
                "authMode": "chatgpt",
                "sourceType": "web_session",
                "codexCompatible": True,
                "quotaOnly": False,
                "credentialCapability": "codex_short_lived",
                "models": ["gpt-test"],
                "proxyEnabled": False,
                "proxyRequested": False,
            }
        ]
        calls = []

        class FakeWeb2API:
            running = False

            def status(self):
                return {
                    "running": self.running,
                    "activeForCodex": settings["web2api"]["activeForCodex"],
                }

            def start(self):
                self.running = True
                calls.append("service-start")
                return self.status()

            def stop(self, disable=True):
                self.running = False
                calls.append("service-stop")
                return self.status()

        runtime = type("Runtime", (), {"web2api": FakeWeb2API()})()

        def activate(value, preferred_account_id=None):
            settings["web2api"]["activeForCodex"] = value
            settings["web2api"]["activeAccountId"] = preferred_account_id if value else None
            calls.append("activate")
            return settings["web2api"]

        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "save_settings"),
            patch.object(core, "account_invalid_reason", return_value=None),
            patch.object(
                core,
                "validate_web2api_codex_pool",
                return_value={"accounts": ["web-account"], "models": ["gpt-test"]},
            ),
            patch.object(core, "resolve_codex_launch_plan", return_value={"kind": "app"}),
            patch.object(core, "service_secret_configured", return_value=True),
            patch.object(core, "close_codex_processes", side_effect=lambda: calls.append("close") or {}),
            patch.object(core, "set_web2api_codex_active", side_effect=activate),
            patch.object(core, "apply_configuration", side_effect=lambda *_args: calls.append("apply") or {}),
            patch.object(core, "auto_sync_sessions_after_switch", return_value={}),
            patch.object(core, "launch_codex_app", return_value={"started": True}),
            patch.object(core, "read_toml", return_value={"model": "gpt-test"}),
            patch.object(core, "wait_for_codex_runtime_ready", return_value={"ready": True}),
        ):
            result = app.activate_web2api_for_codex(runtime, "web-account")

        self.assertEqual(result["mode"], "web_session_local_conversion")
        self.assertEqual(settings["web2api"]["accountIds"], ["other-account"])
        self.assertEqual(settings["web2api"]["routing"], "quota_first")
        self.assertEqual(settings["web2api"]["activeAccountId"], "web-account")
        self.assertFalse(settings["accounts"][0]["proxyEnabled"])
        self.assertEqual(calls[:2], ["service-start", "close"])

    def test_web2api_readiness_failure_rolls_back_without_relaunch_loop(self):
        settings = core._initial_settings()
        settings["web2api"]["accountIds"] = ["pool-account"]
        calls = []

        class FakeWeb2API:
            running = False

            def status(self):
                return {"running": self.running}

            def start(self):
                self.running = True
                calls.append("service-start")
                return self.status()

            def stop(self, disable=True):
                self.running = False
                calls.append("service-stop")
                return self.status()

        runtime = type("Runtime", (), {"web2api": FakeWeb2API()})()
        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "_capture_switch_transaction_snapshot", return_value={"files": {}, "environment": {}}),
            patch.object(core, "_restore_switch_transaction_snapshot", side_effect=lambda _snapshot: calls.append("restore") or []),
            patch.object(core, "validate_web2api_codex_pool", return_value={}),
            patch.object(core, "resolve_codex_launch_plan", return_value={"strategy": "windows_app"}),
            patch.object(core, "service_secret_configured", return_value=True),
            patch.object(core, "close_codex_processes", side_effect=lambda **_kwargs: calls.append("close") or {}),
            patch.object(core, "set_web2api_codex_active", return_value={}),
            patch.object(core, "apply_configuration", return_value={}),
            patch.object(core, "auto_sync_sessions_after_switch", return_value={}),
            patch.object(core, "launch_codex_app", side_effect=lambda **_kwargs: calls.append("launch") or {}),
            patch.object(core, "read_toml", return_value={"model": "gpt-pool"}),
            patch.object(core, "wait_for_codex_runtime_ready", side_effect=core.ManagerError("timeout")),
            patch.object(core, "running_codex_processes", return_value=[{"pid": "7"}]),
        ):
            with self.assertRaisesRegex(core.ManagerError, "避免循环重启"):
                app.activate_web2api_for_codex(runtime)

        self.assertEqual(calls.count("launch"), 1)
        self.assertEqual(calls.count("close"), 2)
        self.assertIn("restore", calls)
        self.assertIn("service-stop", calls)

    def test_web2api_activation_stops_runtime_before_restoring_snapshot(self):
        settings = core._initial_settings()
        settings["web2api"]["accountIds"] = ["pool-account"]
        calls = []

        class FakeWeb2API:
            running = False

            def status(self):
                return {"running": self.running}

            def start(self):
                self.running = True
                calls.append("service-start")
                return self.status()

            def stop(self, disable=True):
                self.running = False
                calls.append(f"service-stop:{disable}")
                return self.status()

        runtime = type("Runtime", (), {"web2api": FakeWeb2API()})()
        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "_capture_switch_transaction_snapshot", return_value={"files": {}, "environment": {}}),
            patch.object(core, "_restore_switch_transaction_snapshot", side_effect=lambda _snapshot: calls.append("restore") or []),
            patch.object(core, "validate_web2api_codex_pool", return_value={}),
            patch.object(core, "resolve_codex_launch_plan", return_value={"strategy": "windows_app"}),
            patch.object(core, "service_secret_configured", return_value=True),
            patch.object(core, "close_codex_processes", side_effect=core.ManagerError("close failed")),
        ):
            with self.assertRaisesRegex(core.ManagerError, "close failed"):
                app.activate_web2api_for_codex(runtime)

        self.assertEqual(calls, ["service-start", "service-stop:True", "restore"])

    def test_active_web_session_never_falls_back_to_another_pool_identity(self):
        core.ensure_state()
        settings = core.load_settings()
        settings["accounts"] = [
            {
                "id": "selected-web",
                "label": "Selected Web Session",
                "authMode": "chatgpt",
                "sourceType": "web_session",
                "codexCompatible": True,
                "models": ["gpt-selected"],
            },
            {
                "id": "pool-fallback",
                "label": "Pool Fallback",
                "authMode": "chatgpt",
                "codexCompatible": True,
                "models": ["gpt-selected", "gpt-other"],
            },
        ]
        settings["web2api"].update(
            {
                "accountIds": ["pool-fallback"],
                "routing": "ordered",
                "activeForCodex": True,
                "activeAccountId": "selected-web",
            }
        )
        core.save_settings(settings)

        manager = web2api.Web2APIManager()
        self.assertEqual([item["id"] for item in manager._accounts(access_scope="internal")], ["selected-web"])
        self.assertEqual(
            [item["id"] for item in manager._accounts({"pool-fallback"})],
            ["pool-fallback"],
        )
        self.assertEqual(
            [item["id"] for item in core.web2api_pool_model_records(core.load_settings())],
            ["gpt-selected"],
        )
        model_ids = [item["id"] for item in manager.models(access_scope="internal")["data"]]
        self.assertEqual(model_ids[0], "gpt-selected")
        self.assertEqual(
            [item for item in model_ids if not item.startswith("cam-agent-")],
            ["gpt-selected"],
        )

    def test_pool_activation_clears_previous_single_account_selection(self):
        settings = core._initial_settings()
        settings["web2api"]["activeAccountId"] = "stale-web"
        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "save_settings") as save,
            patch.object(core, "validate_web2api_codex_pool", return_value={}),
        ):
            result = core.set_web2api_codex_active(True)
        self.assertTrue(result["activeForCodex"])
        self.assertIsNone(result["activeAccountId"])
        save.assert_called_once()

    def test_deactivate_main_proxy_keeps_gateway_running_for_subagents(self):
        calls = []
        settings = {"web2api": {"activeForCodex": True}}

        class FakeWeb2API:
            def status(self):
                return {"running": True, "activeForCodex": settings["web2api"]["activeForCodex"]}

            def start(self):
                calls.append("service-start")
                return self.status()

            def stop(self, disable=True):
                calls.append("service-stop")
                return {"running": False, "activeForCodex": False}

        def activate(value, preferred_account_id=None):
            settings["web2api"]["activeForCodex"] = value
            settings["web2api"]["activeAccountId"] = preferred_account_id if value else None
            calls.append(f"active:{value}")
            return settings["web2api"]

        runtime = type("Runtime", (), {"web2api": FakeWeb2API()})()
        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "resolve_codex_launch_plan", return_value={"strategy": "windows_app"}),
            patch.object(core, "close_codex_processes", side_effect=lambda: calls.append("close") or {}),
            patch.object(core, "set_web2api_codex_active", side_effect=activate),
            patch.object(
                core,
                "apply_configuration",
                side_effect=lambda *_args: calls.append("apply") or {"gatewayRequired": True},
            ),
            patch.object(core, "auto_sync_sessions_after_switch", return_value={}),
            patch.object(core, "launch_codex_app", side_effect=lambda **_kwargs: calls.append("launch") or {}),
            patch.object(core, "read_toml", return_value={"model": "gpt-official"}),
            patch.object(
                core,
                "wait_for_codex_runtime_ready",
                side_effect=lambda **_kwargs: calls.append("ready") or {"ready": True},
            ),
        ):
            result = app.deactivate_web2api_for_codex(runtime)

        self.assertTrue(result["keptForSubagents"])
        self.assertNotIn("service-stop", calls)
        self.assertEqual(calls, ["close", "active:False", "apply", "launch", "ready"])

    def test_manager_runtime_applies_on_open_and_restores_on_final_exit(self):
        calls = []

        class FakeWeb2API:
            def __init__(self):
                self.running = False
                self.last_error = None

            def status(self):
                return {"running": self.running}

            def start(self):
                calls.append("service-start")
                self.running = True
                return self.status()

            def stop(self, disable=True):
                calls.append("service-stop")
                self.running = False
                return self.status()

        with (
            patch.object(app, "Web2APIManager", FakeWeb2API),
            patch.object(
                core,
                "begin_runtime_configuration_overlay",
                side_effect=lambda: calls.append("overlay-begin") or {"active": True},
            ),
            patch.object(
                core,
                "apply_configuration",
                side_effect=lambda _sync=False: calls.append("apply")
                or {"changed": True, "gatewayRequired": True},
            ),
            patch.object(core, "load_settings", return_value={"web2api": {"enabled": True}}),
            patch.object(core, "service_secret_configured", return_value=True),
            patch.object(core, "_require_codex_process_scan_known", side_effect=lambda: [] if "codex-close" in calls else [{"pid":"1"}]),
            patch.object(core, "running_codex_processes", return_value=[{"name": "Codex.exe", "pid": "1"}]),
            patch.object(
                core,
                "resolve_codex_launch_plan",
                side_effect=lambda: calls.append("plan")
                or {
                    "strategy": "windows_app",
                    "appUserModelId": "OpenAI.Codex_2p2nqsd0c76g0!App",
                },
            ),
            patch.object(
                core,
                "close_codex_processes",
                side_effect=lambda: calls.append("codex-close") or {"closed": ["1"]},
            ),
            patch.object(
                core,
                "launch_codex_app",
                side_effect=lambda **_kwargs: calls.append("codex-launch") or {"launched": True},
            ) as launch_codex,
            patch.object(
                core,
                "restore_runtime_configuration_overlay",
                side_effect=lambda force=False: calls.append(f"restore:{force}") or {"restored": True},
            ),
        ):
            runtime = app.ManagerRuntime()
            self.assertTrue(runtime.configuration_session["active"])
            startup_message = runtime.configuration_session["message"]
            first_close = runtime.close()
            second_close = runtime.close()

        self.assertTrue(first_close["restored"])
        self.assertEqual(second_close, first_close)
        self.assertEqual(calls.count("overlay-begin"), 1)
        self.assertEqual(calls.count("apply"), 1)
        self.assertEqual(calls.count("service-start"), 1)
        self.assertEqual(calls.count("service-stop"), 1)
        self.assertEqual(calls.count("codex-close"), 1)
        self.assertEqual(calls.count("codex-launch"), 0)
        launch_codex.assert_not_called()
        self.assertEqual(calls.count("restore:False"), 1)
        self.assertIn("现有任务不会被自动关闭", startup_message)
        self.assertIsNone(first_close["launch"])

    def test_manager_runtime_does_not_start_radar_monitor_automatically(self):
        calls = []

        class FakeWeb2API:
            last_error = None

            def status(self):
                return {"running": False}

        with (
            patch.object(app, "Web2APIManager", FakeWeb2API),
            patch.object(core, "begin_runtime_configuration_overlay", return_value={"active": True}),
            patch.object(core, "apply_configuration", return_value={"gatewayRequired": False}),
            patch.object(core, "load_settings", return_value={"web2api": {"enabled": False}}),
            patch.object(core, "running_codex_processes", return_value=[]),
            patch.object(app.ManagerRuntime, "_start_account_auto_refresh", side_effect=lambda: calls.append("account")),
            patch.object(app.ManagerRuntime, "_start_update_checks", side_effect=lambda: calls.append("updates")),
            patch.object(app.ManagerRuntime, "_start_radar_monitor", side_effect=lambda: calls.append("radar")) as start_radar,
            patch.object(core, "restore_runtime_configuration_overlay", return_value={"restored": True}),
        ):
            runtime = app.ManagerRuntime()
            runtime.close()

        self.assertEqual(calls, ["account", "updates"])
        start_radar.assert_not_called()

    def test_radar_monitor_compatibility_hook_never_polls_in_background(self):
        runtime = object.__new__(app.ManagerRuntime)
        runtime.radar_monitor_stop = threading.Event()
        runtime.radar_monitor_wake = threading.Event()
        runtime.radar_monitor_thread = None
        runtime.radar_monitor_status = {
            "status": "waiting",
            "lastCheckedAt": "startup-cache",
            "nextCheckAt": None,
            "lastResult": "cached",
            "lastError": None,
        }
        calls = []
        runtime.radar = type("FakeRadar", (), {"run_monitor": lambda _self: calls.append("network")})()

        runtime._start_radar_monitor()
        time.sleep(0.15)

        self.assertIsNone(runtime.radar_monitor_thread)
        self.assertEqual(calls, [])
        self.assertEqual(runtime.radar_monitor_status["lastCheckedAt"], "startup-cache")

    def test_configuration_activation_failure_keeps_visible_repair_mode_available(self):
        class FakeWeb2API:
            last_error = None

            def status(self):
                return {"running": False}

        with (
            patch.object(app, "Web2APIManager", FakeWeb2API),
            patch.object(core, "begin_runtime_configuration_overlay", return_value={"active": True}),
            patch.object(core, "apply_configuration", side_effect=core.ManagerError("synthetic apply failure")),
            patch.object(
                core,
                "restore_runtime_configuration_overlay",
                return_value={"restored": True},
            ) as restore,
        ):
            runtime = app.ManagerRuntime(defer_configuration=True)
            session = runtime.activate_configuration_session()

        self.assertEqual(runtime.configuration_session["status"], "error")
        self.assertFalse(runtime.configuration_session["active"])
        self.assertTrue(runtime.configuration_session["recoveryOnly"])
        self.assertTrue(runtime.configuration_session["recoverable"])
        self.assertIn("安全修复模式", session["message"])
        self.assertEqual(runtime.configuration_session["restore"], {"restored": True})
        self.assertIsNone(runtime.configuration_session["restart"])
        restore.assert_called_once_with(force=True)

    def test_configuration_activation_failure_never_reopens_codex(self):
        class FakeWeb2API:
            last_error = None

            def status(self):
                return {"running": False}

        with (
            patch.object(app, "Web2APIManager", FakeWeb2API),
            patch.object(core, "begin_runtime_configuration_overlay", return_value={"active": True}),
            patch.object(core, "resolve_codex_launch_plan", return_value={"strategy": "windows_app"}),
            patch.object(core, "running_codex_processes", side_effect=[[{"pid": "7"}], []]),
            patch.object(core, "close_codex_processes", return_value={"closed": ["7"]}),
            patch.object(core, "apply_configuration", side_effect=core.ManagerError("synthetic apply failure")),
            patch.object(core, "restore_runtime_configuration_overlay", return_value={"restored": True}),
            patch.object(core, "launch_codex_app") as launch,
        ):
            runtime = app.ManagerRuntime(defer_configuration=True)
            runtime.activate_configuration_session()

        launch.assert_not_called()
        self.assertIn("配置不会继续改动", runtime.configuration_session["message"])

    def test_deferred_runtime_does_not_touch_codex_before_ui_activation(self):
        class FakeWeb2API:
            def __init__(self):
                self.server = None
                self.last_error = None

            def status(self):
                return {"running": False}

        with (
            patch.object(app, "Web2APIManager", FakeWeb2API),
            patch.object(core, "begin_runtime_configuration_overlay") as begin_overlay,
            patch.object(core, "apply_configuration") as apply_configuration,
            patch.object(core, "running_codex_processes", return_value=[{"pid": "1"}]) as running_codex,
            patch.object(core, "close_codex_processes") as close_codex,
            patch.object(core, "restore_runtime_configuration_overlay", return_value={"restored": False}),
        ):
            runtime = app.ManagerRuntime(defer_configuration=True)
            self.assertEqual(runtime.configuration_session["status"], "waiting")
            runtime.close()

        begin_overlay.assert_not_called()
        apply_configuration.assert_not_called()
        running_codex.assert_not_called()
        close_codex.assert_not_called()

    def test_manager_runtime_quick_restart_adopts_overlay_without_touching_codex(self):
        calls = []

        class FakeWeb2API:
            def __init__(self):
                self.running = False
                self.last_error = None

            def status(self):
                return {"running": self.running}

            def start(self):
                calls.append("service-start")
                self.running = True
                return self.status()

            def stop(self, disable=True):
                calls.append(f"service-stop:{disable}")
                self.running = False
                return self.status()

        with (
            patch.object(app, "Web2APIManager", FakeWeb2API),
            patch.object(
                core,
                "adopt_runtime_configuration_overlay",
                side_effect=lambda: calls.append("overlay-adopt") or {"active": True, "adopted": True},
            ),
            patch.object(core, "begin_runtime_configuration_overlay") as begin_overlay,
            patch.object(
                core,
                "apply_configuration",
                side_effect=lambda _sync=False: calls.append("apply")
                or {"changed": False, "gatewayRequired": True},
            ),
            patch.object(core, "load_settings", return_value={"web2api": {"enabled": True}}),
            patch.object(core, "service_secret_configured", return_value=True),
            patch.object(core, "running_codex_processes") as running_codex,
            patch.object(core, "close_codex_processes") as close_codex,
            patch.object(core, "restore_runtime_configuration_overlay") as restore_overlay,
        ):
            runtime = app.ManagerRuntime(quick_restart=True)
            result = runtime.close_for_restart()
            repeated = runtime.close()

        self.assertEqual(calls, ["overlay-adopt", "apply", "service-start", "service-stop:False"])
        self.assertTrue(result["preserved"])
        self.assertEqual(repeated, result)
        self.assertIn("快速重启", runtime.configuration_session["message"])
        begin_overlay.assert_not_called()
        running_codex.assert_not_called()
        close_codex.assert_not_called()
        restore_overlay.assert_not_called()

    def test_runtime_overlay_handoff_changes_only_owner(self):
        core.ensure_state()
        payload = {
            "schemaVersion": 1,
            "sessionId": "handoff-session",
            "ownerPid": 1234,
            "codexHome": str(core.CODEX_HOME),
            "createdAt": core.now_iso(),
            "updatedAt": core.now_iso(),
            "files": {"config.toml": {"baseline": None}},
            "environment": {},
        }
        core.atomic_write_json(core.RUNTIME_OVERLAY_FILE, payload)

        adopted = core.adopt_runtime_configuration_overlay()
        stored = json.loads(core.RUNTIME_OVERLAY_FILE.read_text(encoding="utf-8"))

        self.assertTrue(adopted["adopted"])
        self.assertEqual(adopted["previousOwnerPid"], 1234)
        self.assertEqual(stored["ownerPid"], os.getpid())
        self.assertEqual(stored["sessionId"], "handoff-session")
        self.assertEqual(stored["files"], payload["files"])

    def test_quick_restart_handoff_is_single_use_and_token_bound(self):
        handoff = self.root / "agent-manager" / "restart-handoff.json"
        handoff.parent.mkdir(parents=True, exist_ok=True)
        with patch.object(app, "RESTART_HANDOFF_FILE", handoff):
            app._write_restart_handoff("expected-token")
            self.assertFalse(app._consume_restart_handoff("wrong-token"))
            self.assertTrue(handoff.exists())
            self.assertTrue(app._consume_restart_handoff("expected-token"))
            self.assertFalse(handoff.exists())
            self.assertFalse(app._consume_restart_handoff("expected-token"))

    def test_quick_restart_prefers_newer_packaged_executable(self):
        release = Path(self.temp.name) / "release"
        release.mkdir()
        current = release / "CodexAgentManager.exe"
        update = release / "CodexAgentManager-5.7.0.exe"
        current.write_bytes(b"current")
        update.write_bytes(b"update")
        os.utime(current, (100, 100))
        os.utime(update, (200, 200))

        with (
            patch.object(app.sys, "frozen", True, create=True),
            patch.object(app.sys, "executable", str(current)),
        ):
            command, workdir = app._manager_restart_command("handoff-token")

        self.assertEqual(Path(command[0]), update.resolve())
        self.assertEqual(command[1:], ["--quick-restart-token=handoff-token"])
        self.assertEqual(workdir, release.resolve())

        with (
            patch.object(app.sys, "frozen", True, create=True),
            patch.object(app.sys, "executable", str(current)),
        ):
            leading_dash, _ = app._manager_restart_command("-leading-dash-token")
        self.assertEqual(leading_dash[1:], ["--quick-restart-token=-leading-dash-token"])

    def test_packaged_manager_breaks_away_from_parent_windows_job_before_startup(self):
        executable = self.root / "release" / "AgentManager.exe"
        captured = {}

        class FakeProcess:
            pid = 123

        def fake_popen(command, **kwargs):
            captured["command"] = command
            captured.update(kwargs)
            return FakeProcess()

        with (
            patch.object(app.os, "name", "nt"),
            patch.object(app.sys, "frozen", True, create=True),
            patch.object(app.sys, "executable", str(executable)),
            patch.object(app, "_current_process_is_in_windows_job", return_value=True),
            patch.object(app, "_WINDOWS_JOB_ISOLATION_STATUS", "unknown"),
            patch.object(app.subprocess, "Popen", side_effect=fake_popen),
            patch.dict(app.os.environ, {app.JOB_BREAKAWAY_ENV: ""}, clear=False),
        ):
            relaunched = app._relaunch_frozen_manager_outside_parent_job(["--browser"])

        self.assertTrue(relaunched)
        self.assertEqual(captured["command"], [str(executable), "--browser"])
        self.assertEqual(captured["env"][app.JOB_BREAKAWAY_ENV], "1")
        self.assertEqual(captured["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertTrue(captured["creationflags"] & 0x01000000)
        self.assertTrue(captured["close_fds"])

    def test_main_fast_wakes_existing_instance_before_job_handoff(self):
        with (
            patch.object(app, "_consume_shell_job_handoff", return_value=([], False)),
            patch.object(app, "open_existing_runtime", return_value=True) as wake,
            patch.object(app, "_relaunch_frozen_manager_outside_parent_job") as relaunch,
            patch.object(app, "run_server") as run_server,
        ):
            result = app.main([])

        self.assertEqual(result, 0)
        wake.assert_called_once_with()
        relaunch.assert_not_called()
        run_server.assert_not_called()

    def test_main_quick_restart_skips_existing_instance_fast_wake(self):
        argument = "--quick-restart-token=handoff-token"
        with (
            patch.object(app, "_consume_shell_job_handoff", return_value=([argument], False)),
            patch.object(app, "open_existing_runtime") as wake,
            patch.object(app, "_relaunch_frozen_manager_outside_parent_job", return_value=True) as relaunch,
            patch.object(app, "run_server") as run_server,
        ):
            result = app.main([argument])

        self.assertEqual(result, 0)
        wake.assert_not_called()
        relaunch.assert_called_once_with([argument])
        run_server.assert_not_called()

    def test_existing_instance_fast_wake_rejects_oversized_runtime_file(self):
        runtime_file = self.root / "agent-manager" / "app-runtime.json"
        runtime_file.parent.mkdir(parents=True, exist_ok=True)
        with runtime_file.open("wb") as stream:
            stream.truncate(app.MAX_RUNTIME_FILE_BYTES + 1)

        with (
            patch.object(app, "RUNTIME_FILE", runtime_file),
            patch.object(app, "_probe_runtime_identity") as probe,
        ):
            self.assertFalse(app.open_existing_runtime())
        probe.assert_not_called()

    def test_packaged_manager_accepts_successful_breakaway_even_if_windows_assigns_a_new_job(self):
        with (
            patch.object(app.os, "name", "nt"),
            patch.object(app.sys, "frozen", True, create=True),
            patch.object(app, "_current_process_is_in_windows_job", return_value=True),
            patch.object(app, "_WINDOWS_JOB_ISOLATION_STATUS", "unknown"),
            patch.dict(app.os.environ, {app.JOB_BREAKAWAY_ENV: "1"}, clear=False),
        ):
            self.assertFalse(app._relaunch_frozen_manager_outside_parent_job([]))
            self.assertNotIn(app.JOB_BREAKAWAY_ENV, app.os.environ)
            self.assertEqual(app._WINDOWS_JOB_ISOLATION_STATUS, "breakaway_completed")

    def test_quick_restart_inherits_only_a_token_bound_isolated_lifecycle(self):
        handoff = self.root / "agent-manager" / "restart-handoff.json"
        handoff.parent.mkdir(parents=True, exist_ok=True)
        with (
            patch.object(app, "RESTART_HANDOFF_FILE", handoff),
            patch.object(app.os, "name", "nt"),
            patch.object(app.sys, "frozen", True, create=True),
            patch.object(app, "_WINDOWS_JOB_ISOLATION_STATUS", "unknown"),
            patch.object(app, "_current_process_is_in_windows_job") as in_job,
            patch.dict(app.os.environ, {app.TRUSTED_RESTART_ENV: "1"}, clear=False),
        ):
            app._write_restart_handoff("expected-token")
            self.assertFalse(
                app._relaunch_frozen_manager_outside_parent_job(
                    ["--quick-restart-token=expected-token"]
                )
            )
            self.assertNotIn(app.TRUSTED_RESTART_ENV, app.os.environ)
            self.assertEqual(app._WINDOWS_JOB_ISOLATION_STATUS, "restart_inherited")
        in_job.assert_not_called()

    def test_untrusted_restart_marker_cannot_bypass_job_isolation(self):
        with (
            patch.object(app.os, "name", "nt"),
            patch.object(app.sys, "frozen", True, create=True),
            patch.object(app, "_current_process_is_in_windows_job", return_value=False) as in_job,
            patch.object(app, "_WINDOWS_JOB_ISOLATION_STATUS", "unknown"),
            patch.dict(app.os.environ, {app.TRUSTED_RESTART_ENV: "1"}, clear=False),
        ):
            self.assertFalse(
                app._relaunch_frozen_manager_outside_parent_job(
                    ["--quick-restart-token=wrong-token"]
                )
            )
            self.assertEqual(app._WINDOWS_JOB_ISOLATION_STATUS, "already_independent")
        in_job.assert_called_once_with()

    def test_packaged_manager_falls_back_to_windows_shell_when_job_forbids_breakaway(self):
        executable = self.root / "release" / "AgentManager.exe"
        captured = {}

        class FakeShell32:
            @staticmethod
            def ShellExecuteW(_owner, _verb, target, parameters, workdir, _show):
                captured.update(
                    target=target,
                    parameters=parameters,
                    workdir=workdir,
                    reset_environment=app.os.environ.get("PYINSTALLER_RESET_ENVIRONMENT"),
                )
                return 42

        with (
            patch.object(app.os, "name", "nt"),
            patch.object(app.sys, "frozen", True, create=True),
            patch.object(app.sys, "executable", str(executable)),
            patch.object(app, "_current_process_is_in_windows_job", return_value=True),
            patch.object(app, "_WINDOWS_JOB_ISOLATION_STATUS", "unknown"),
            patch.object(app.subprocess, "Popen", side_effect=OSError("breakaway denied")),
            patch.object(app.ctypes, "windll", type("Windll", (), {"shell32": FakeShell32()})()),
            patch.dict(
                app.os.environ,
                {
                    app.JOB_BREAKAWAY_ENV: "",
                    "PYINSTALLER_RESET_ENVIRONMENT": "previous-value",
                },
                clear=False,
            ),
        ):
            relaunched = app._relaunch_frozen_manager_outside_parent_job(["--browser"])
            self.assertEqual(app.os.environ["PYINSTALLER_RESET_ENVIRONMENT"], "previous-value")

        self.assertTrue(relaunched)
        self.assertEqual(Path(captured["target"]), executable.resolve())
        self.assertIn("--browser", captured["parameters"])
        self.assertIn(app.JOB_SHELL_HANDOFF_ARG, captured["parameters"])
        self.assertEqual(captured["reset_environment"], "1")

    def test_shell_job_handoff_marker_is_one_shot_and_must_verify_independence(self):
        with (
            patch.object(app, "_current_process_is_in_windows_job", return_value=False),
            patch.object(app, "_WINDOWS_JOB_ISOLATION_STATUS", "unknown"),
            patch.dict(app.os.environ, {app.JOB_BREAKAWAY_ENV: "1"}, clear=False),
        ):
            argv, consumed = app._consume_shell_job_handoff(
                ["--browser", app.JOB_SHELL_HANDOFF_ARG]
            )
            self.assertTrue(consumed)
            self.assertEqual(argv, ["--browser"])
            self.assertNotIn(app.JOB_BREAKAWAY_ENV, app.os.environ)
            self.assertEqual(app._WINDOWS_JOB_ISOLATION_STATUS, "shell_handoff_completed")

    def test_independent_packaged_manager_clears_breakaway_marker(self):
        with (
            patch.object(app.os, "name", "nt"),
            patch.object(app.sys, "frozen", True, create=True),
            patch.object(app, "_current_process_is_in_windows_job", return_value=False),
            patch.object(app, "_WINDOWS_JOB_ISOLATION_STATUS", "unknown"),
            patch.dict(app.os.environ, {app.JOB_BREAKAWAY_ENV: "1"}, clear=False),
        ):
            self.assertFalse(app._relaunch_frozen_manager_outside_parent_job([]))
            self.assertNotIn(app.JOB_BREAKAWAY_ENV, app.os.environ)

    def test_packaged_manager_refuses_to_close_codex_without_lifecycle_isolation(self):
        with (
            patch.object(app.os, "name", "nt"),
            patch.object(app.sys, "frozen", True, create=True),
            patch.object(app, "_manager_lifecycle_is_independent", return_value=False),
            patch.object(app.core, "close_codex_processes") as close_codex,
        ):
            with self.assertRaisesRegex(core.ManagerError, "本次操作已取消"):
                app._close_codex_processes_safely()
        close_codex.assert_not_called()

    def test_lifecycle_isolated_manager_closes_codex_with_requested_timeout(self):
        expected = {"closed": [123]}
        with (
            patch.object(app.os, "name", "nt"),
            patch.object(app.sys, "frozen", True, create=True),
            patch.object(app, "_manager_lifecycle_is_independent", return_value=True),
            patch.object(app.core, "close_codex_processes", return_value=expected) as close_codex,
        ):
            self.assertEqual(app._close_codex_processes_safely(8), expected)
        close_codex.assert_called_once_with(timeout_seconds=8)

    def test_quick_restart_resets_pyinstaller_environment(self):
        runtime_file = self.root / "agent-manager" / "app-runtime.json"
        runtime_file.parent.mkdir(parents=True, exist_ok=True)
        runtime_file.write_text(
            json.dumps(
                {
                    "pid": 202,
                    "port": 17860,
                    "appId": app.RUNTIME_APP_ID,
                    "runtimeNonce": "restart-runtime-nonce-123",
                    "activationToken": "restart-activation-token-123",
                    "startedAt": core.now_iso(),
                    "uiReady": True,
                    "independentLifecycle": True,
                }
            ),
            encoding="utf-8",
        )
        popen_arguments = {}

        class FakeProcess:
            pid = 101
            returncode = None

            def poll(self):
                return None

        class FakeResponse:
            status = 200

            def read(self, _limit=-1):
                return json.dumps(
                    {
                        "ok": True,
                        "appId": app.RUNTIME_APP_ID,
                        "runtimePid": 202,
                        "runtimeNonce": "restart-runtime-nonce-123",
                        "activationTokenHash": app._activation_token_hash("restart-activation-token-123"),
                        "uiReady": True,
                        "independentLifecycle": True,
                    }
                ).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def fake_popen(*args, **kwargs):
            popen_arguments.update(kwargs)
            return FakeProcess()

        with (
            patch.object(app, "RUNTIME_FILE", runtime_file),
            patch.object(app.os, "name", "nt"),
            patch.object(app.sys, "frozen", True, create=True),
            patch.object(app, "_manager_lifecycle_is_independent", return_value=True),
            patch.object(app, "_manager_restart_command", return_value=(["manager.exe"], self.root)),
            patch.object(app.subprocess, "Popen", side_effect=fake_popen),
            patch.object(app, "_pid_is_running", return_value=True),
            patch.object(app.urllib.request, "urlopen", return_value=FakeResponse()),
        ):
            result = app._launch_restarted_manager("handoff-token", ready_timeout_seconds=3)

        self.assertTrue(result["ready"])
        self.assertEqual(popen_arguments["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertEqual(popen_arguments["env"][app.TRUSTED_RESTART_ENV], "1")

    def test_quick_restart_accepts_successful_zero_exit_job_relay(self):
        runtime = {
            "pid": 202,
            "port": 17860,
            "appId": app.RUNTIME_APP_ID,
            "runtimeNonce": "restart-runtime-nonce-123",
            "activationToken": "restart-activation-token-123",
            "uiReady": True,
            "independentLifecycle": True,
        }

        class RelayedProcess:
            pid = 101
            returncode = 0

            def poll(self):
                return 0

        with (
            patch.object(app, "_manager_restart_command", return_value=(["manager.exe"], self.root)),
            patch.object(app.subprocess, "Popen", return_value=RelayedProcess()),
            patch.object(app, "_read_runtime_file", return_value=runtime),
            patch.object(app, "_probe_runtime_identity", return_value=True) as probe,
        ):
            result = app._launch_restarted_manager("handoff-token", ready_timeout_seconds=3)

        self.assertTrue(result["ready"])
        self.assertEqual(result["runtimePid"], 202)
        self.assertEqual(result["launcherExitCode"], 0)
        probe.assert_called_once_with(
            runtime,
            require_ui_ready=True,
            require_independent=True,
        )

    def test_quick_restart_rejects_nonzero_launcher_exit(self):
        class FailedProcess:
            pid = 101
            returncode = 7

            def poll(self):
                return 7

        with (
            patch.object(app, "_manager_restart_command", return_value=(["manager.exe"], self.root)),
            patch.object(app.subprocess, "Popen", return_value=FailedProcess()),
        ):
            with self.assertRaisesRegex(core.ManagerError, "代码 7"):
                app._launch_restarted_manager("handoff-token", ready_timeout_seconds=3)

    def test_quick_restart_shutdown_uses_preserving_close_and_relaunches_manager(self):
        calls = []

        class FakeRuntime:
            def close_for_restart(self):
                calls.append("runtime-preserve")
                return {"errors": []}

            def close(self):
                calls.append("runtime-full-close")
                return {"errors": []}

        class FakeServer:
            def __init__(self):
                self.force_exit = False
                self.allow_forced_process_exit = False
                self.native_window = False
                self.quick_restart_token = "restart-token"
                self.runtime = FakeRuntime()
                self.shutdown_finished = threading.Event()

            def stop_tray(self):
                calls.append("tray-stop")

            def shutdown(self):
                calls.append("server-shutdown")

            def server_close(self):
                calls.append("server-close")

        server = FakeServer()
        with (
            patch.object(app, "_write_shutdown_status"),
            patch.object(app, "_cleanup_runtime", side_effect=lambda: calls.append("runtime-file-clean")),
            patch.object(
                app,
                "_launch_restarted_manager",
                side_effect=lambda token: calls.append(f"manager-launch:{token}") or {"pid": 2},
            ),
        ):
            app.shutdown_application(server, claimed=True, quick_restart=True)

        self.assertNotIn("runtime-full-close", calls)
        self.assertEqual(
            calls,
            [
                "tray-stop",
                "server-shutdown",
                "runtime-preserve",
                "runtime-file-clean",
                "manager-launch:restart-token",
                "server-close",
            ],
        )
        self.assertTrue(server.shutdown_finished.is_set())

    def test_exit_only_preserves_codex_overlay_without_relaunching_manager(self):
        calls = []

        class FakeRuntime:
            def close_for_restart(self, *, exit_only=False):
                calls.append(f"runtime-preserve:{exit_only}")
                return {"errors": []}

            def close(self):
                calls.append("runtime-full-close")
                return {"errors": []}

        class FakeServer:
            def __init__(self):
                self.force_exit = False
                self.allow_forced_process_exit = False
                self.native_window = False
                self.runtime = FakeRuntime()
                self.shutdown_finished = threading.Event()

            def stop_tray(self):
                calls.append("tray-stop")

            def shutdown(self):
                calls.append("server-shutdown")

            def server_close(self):
                calls.append("server-close")

        server = FakeServer()
        with (
            patch.object(app, "_write_shutdown_status"),
            patch.object(app, "_cleanup_runtime", side_effect=lambda: calls.append("runtime-file-clean")),
        ):
            app.shutdown_application(server, claimed=True, exit_only=True)

        self.assertNotIn("runtime-full-close", calls)
        self.assertEqual(
            calls,
            [
                "tray-stop",
                "server-shutdown",
                "runtime-preserve:True",
                "server-close",
                "runtime-file-clean",
            ],
        )
        self.assertTrue(server.shutdown_finished.is_set())

    def test_quick_restart_failure_reopens_old_manager_instead_of_stranding_overlay(self):
        calls = []
        replacement_runtime = object()

        class FakeRuntime:
            def close_for_restart(self):
                calls.append("runtime-preserve")
                return {"errors": []}

        class FakeServer:
            def __init__(self):
                self.force_exit = False
                self.allow_forced_process_exit = False
                self.native_window = False
                self.native_window_object = None
                self.quick_restart_requested = True
                self.quick_restart_token = "restart-token"
                self.runtime = FakeRuntime()
                self.shutdown_lock = threading.RLock()
                self.shutdown_started = threading.Event()
                self.shutdown_started.set()
                self.shutdown_finished = threading.Event()
                self.shutdown_aborted = threading.Event()

            def stop_tray(self):
                calls.append("tray-stop")

            def shutdown(self):
                calls.append("server-shutdown")

            def server_close(self):
                calls.append("server-close")

            def serve_forever(self, poll_interval=0.25):
                calls.append("server-recovered")

            def ensure_tray(self):
                calls.append("tray-recovered")
                return True

        server = FakeServer()
        with (
            patch.object(app, "_write_shutdown_status"),
            patch.object(app, "_cleanup_runtime", side_effect=lambda: calls.append("runtime-file-clean")),
            patch.object(app, "_launch_restarted_manager", side_effect=core.ManagerError("new app failed")),
            patch.object(app, "_acquire_instance_mutex", return_value=True),
            patch.object(app, "_write_runtime_discovery", side_effect=lambda _server: calls.append("runtime-file-restored")),
            patch.object(app, "ManagerRuntime", return_value=replacement_runtime),
            patch.object(app, "RESTART_HANDOFF_FILE", self.root / "agent-manager" / "restart-handoff.json"),
        ):
            app.shutdown_application(server, claimed=True, quick_restart=True)

        self.assertIs(server.runtime, replacement_runtime)
        self.assertFalse(server.force_exit)
        self.assertFalse(server.quick_restart_requested)
        self.assertTrue(server.shutdown_aborted.is_set())
        self.assertFalse(server.shutdown_started.is_set())
        self.assertNotIn("server-close", calls)
        self.assertIn("server-recovered", calls)
        self.assertIn("runtime-file-restored", calls)
        self.assertIn("tray-recovered", calls)

    def test_quick_restart_retries_single_instance_mutex_during_handoff(self):
        mutex_results = iter([False, False, True])
        with (
            patch.object(core, "ensure_state"),
            patch.object(app, "_consume_restart_handoff", return_value=True),
            patch.object(app, "_acquire_instance_mutex", side_effect=lambda: next(mutex_results)) as mutex,
            patch.object(app.time, "sleep"),
            patch.object(app, "_release_instance_mutex"),
            patch.object(
                app,
                "ManagerRuntime",
                side_effect=RuntimeError("stop after mutex"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after mutex"):
                app.run_server(0, open_window=False, native_window=False, quick_restart_token="token")
        self.assertEqual(mutex.call_count, 3)

    def test_run_server_requires_single_instance_mutex_before_creating_overlay(self):
        calls = []
        with (
            patch.object(core, "ensure_state"),
            patch.object(app, "_consume_restart_handoff", return_value=False),
            patch.object(app, "_acquire_instance_mutex", side_effect=lambda: calls.append("mutex") or False),
            patch.object(app, "open_existing_runtime", side_effect=lambda: calls.append("probe") or True),
            patch.object(app, "ManagerRuntime") as runtime,
        ):
            result = app.run_server(0, open_window=True, native_window=False)
        self.assertEqual(result, 0)
        self.assertEqual(calls, ["mutex", "probe"])
        runtime.assert_not_called()

    def test_run_server_waits_for_existing_instance_window_to_become_ready(self):
        attempts = iter([False, False, True])
        with (
            patch.object(core, "ensure_state"),
            patch.object(app, "_consume_restart_handoff", return_value=False),
            patch.object(app, "_acquire_instance_mutex", return_value=False),
            patch.object(app, "open_existing_runtime", side_effect=lambda: next(attempts)) as probe,
            patch.object(app.time, "sleep"),
            patch.object(app, "ManagerRuntime") as runtime,
        ):
            result = app.run_server(0, open_window=True, native_window=True)
        self.assertEqual(result, 0)
        self.assertEqual(probe.call_count, 3)
        runtime.assert_not_called()

    def test_run_server_never_probes_stale_runtime_after_acquiring_mutex(self):
        calls = []
        with (
            patch.object(core, "ensure_state"),
            patch.object(app, "_consume_restart_handoff", return_value=False),
            patch.object(app, "_acquire_instance_mutex", side_effect=lambda: calls.append("mutex") or True),
            patch.object(app, "_release_instance_mutex"),
            patch.object(app, "open_existing_runtime") as probe,
            patch.object(
                app,
                "ManagerRuntime",
                side_effect=lambda **_kwargs: calls.append("runtime") or (_ for _ in ()).throw(RuntimeError("stop")),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                app.run_server(0, open_window=False, native_window=False)
        self.assertEqual(calls, ["mutex", "runtime"])
        probe.assert_not_called()

    def test_runtime_discovery_write_failure_closes_server_runtime_and_mutex(self):
        calls = []

        class FakeRuntime:
            def close(self):
                calls.append("runtime-close")
                return {"errors": []}

        class FakeServer:
            def __init__(self, *_args):
                self.server_address = ("127.0.0.1", 17860)
                self.runtime_nonce = "runtime-nonce"
                self.activation_token = "activation-token"

            def server_close(self):
                calls.append("server-close")

        with (
            patch.object(core, "ensure_state"),
            patch.object(app, "_consume_restart_handoff", return_value=False),
            patch.object(app, "_acquire_instance_mutex", return_value=True),
            patch.object(app, "ManagerRuntime", return_value=FakeRuntime()),
            patch.object(app, "ManagerServer", FakeServer),
            patch.object(app, "_write_runtime_discovery", side_effect=OSError("disk full")),
            patch.object(app, "_cleanup_runtime", side_effect=lambda: calls.append("cleanup-runtime")),
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                app.run_server(0, open_window=False, native_window=False)

        self.assertEqual(calls, ["runtime-close", "server-close", "cleanup-runtime"])

    def test_existing_native_runtime_requests_server_to_restore_hidden_window(self):
        runtime_file = self.root / "agent-manager" / "app-runtime.json"
        runtime_file.parent.mkdir(parents=True, exist_ok=True)
        runtime_file.write_text(
            json.dumps(
                {
                    "pid": 4242,
                    "port": 17860,
                    "appId": app.RUNTIME_APP_ID,
                    "runtimeNonce": "runtime-nonce-1234567890",
                    "native": True,
                    "activationToken": "show-only-token-1234567890",
                }
            ),
            encoding="utf-8",
        )

        class FakeResponse:
            status = 200

            def read(self, _limit=-1):
                return json.dumps(
                    {
                        "ok": True,
                        "appId": app.RUNTIME_APP_ID,
                        "runtimePid": 4242,
                        "runtimeNonce": "runtime-nonce-1234567890",
                        "activationTokenHash": app._activation_token_hash("show-only-token-1234567890"),
                    }
                ).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with (
            patch.object(app, "RUNTIME_FILE", runtime_file),
            patch.object(app, "_pid_is_running", return_value=True),
            patch.object(app.urllib.request, "urlopen", return_value=FakeResponse()),
            patch.object(app, "request_existing_window", return_value=True) as request_show,
            patch.object(app, "focus_process_window") as focus,
            patch.object(app, "open_browser_window") as browser,
        ):
            self.assertTrue(app.open_existing_runtime())

        request_show.assert_called_once()
        focus.assert_not_called()
        browser.assert_not_called()

    def test_management_root_never_discloses_full_api_token(self):
        server = app.ManagerServer(("127.0.0.1", 0), app.RequestHandler, object())
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with app.urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_address[1]}/",
                timeout=2,
            ) as response:
                body = response.read().decode("utf-8")
            self.assertNotIn(server.api_token, body)
            self.assertNotIn("__AGENT_MANAGER_TOKEN__", body)
            ui_url = server.ui_url()
            self.assertIn("#bootstrap=", ui_url)
            self.assertNotIn(server.api_token, urllib.parse.unquote(ui_url))
            bootstrap = urllib.parse.parse_qs(
                urllib.parse.urlsplit(ui_url).fragment
            )["bootstrap"][0]
            for _index in range(app.UI_BOOTSTRAP_MAX_EXCHANGES):
                self.assertTrue(server.consume_ui_bootstrap(bootstrap))
            self.assertFalse(server.consume_ui_bootstrap(bootstrap))
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(3)

    def test_loopback_listener_requests_windows_exclusive_address_use(self):
        class FakeSocket:
            def __init__(self):
                self.calls = []

            def setsockopt(self, *args):
                self.calls.append(args)

        fake_socket = FakeSocket()
        fake_server = SimpleNamespace(socket=fake_socket)
        with (
            patch.object(app.os, "name", "nt"),
            patch.object(app.socket, "SO_EXCLUSIVEADDRUSE", -5, create=True),
            patch.object(app.ThreadingHTTPServer, "server_bind") as inherited_bind,
        ):
            app._bind_exclusive_loopback(fake_server)
        self.assertEqual(fake_socket.calls, [(app.socket.SOL_SOCKET, -5, 1)])
        inherited_bind.assert_called_once_with(fake_server)

    def test_management_bootstrap_retry_window_is_bounded_without_exposing_api_token(self):
        server = app.ManagerServer(("127.0.0.1", 0), app.RequestHandler, object())
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            ui_url = server.ui_url()
            bootstrap = urllib.parse.parse_qs(
                urllib.parse.urlsplit(ui_url).fragment
            )["bootstrap"][0]
            request = app.urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/session/bootstrap",
                data=b"{}",
                method="POST",
                headers={"X-Agent-Manager-Bootstrap": bootstrap},
            )

            def exchange():
                last_error = None
                for _attempt in range(3):
                    try:
                        with app.urllib.request.urlopen(request, timeout=2) as response:
                            return json.loads(response.read().decode("utf-8"))
                    except (ConnectionAbortedError, ConnectionResetError, app.urllib.error.URLError) as exc:
                        last_error = exc
                        time.sleep(0.02)
                raise last_error

            payload = exchange()
            self.assertEqual(payload["token"], server.api_token)
            for _index in range(app.UI_BOOTSTRAP_MAX_EXCHANGES - 1):
                repeated = exchange()
                self.assertEqual(repeated["token"], server.api_token)
            with self.assertRaises(app.urllib.error.HTTPError) as exhausted:
                app.urllib.request.urlopen(request, timeout=2)
            self.assertEqual(exhausted.exception.code, 403)
            exhausted.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(3)

    def test_management_json_response_is_size_bounded(self):
        handler = object.__new__(app.RequestHandler)
        captured = []
        handler._headers = lambda content_type, length, status=200, cache="no-store": captured.append(
            (content_type, length, status, cache)
        )
        handler.wfile = io.BytesIO()
        with patch.object(app, "MAX_RESPONSE_BYTES", 32):
            handler._json({"ok": True, "payload": "x" * 200})
        body = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(captured[0][2], 413)
        self.assertFalse(body["ok"])
        self.assertNotIn("x" * 20, handler.wfile.getvalue().decode("utf-8"))

    def test_oauth_callback_worker_applies_timeout_and_releases_slot(self):
        class FakeRequest:
            timeout = None

            def settimeout(self, value):
                self.timeout = value

        request = FakeRequest()
        server = app.OAuthCallbackServer.__new__(app.OAuthCallbackServer)
        server._thread_slots = threading.BoundedSemaphore(1)
        self.assertTrue(server._thread_slots.acquire(blocking=False))
        with patch.object(app.ThreadingHTTPServer, "process_request_thread") as inherited:
            server.process_request_thread(request, ("127.0.0.1", 12345))
        self.assertEqual(request.timeout, app.OAUTH_CALLBACK_CLIENT_TIMEOUT_SECONDS)
        inherited.assert_called_once_with(request, ("127.0.0.1", 12345))
        self.assertTrue(server._thread_slots.acquire(blocking=False))

    def test_existing_runtime_rejects_stale_pid_before_touching_reused_port(self):
        runtime_file = self.root / "agent-manager" / "app-runtime.json"
        runtime_file.parent.mkdir(parents=True, exist_ok=True)
        runtime_file.write_text(
            json.dumps(
                {
                    "pid": 987654,
                    "port": 17860,
                    "appId": app.RUNTIME_APP_ID,
                    "runtimeNonce": "stale-runtime-nonce-12345",
                    "activationToken": "stale-activation-token-12345",
                }
            ),
            encoding="utf-8",
        )
        with (
            patch.object(app, "RUNTIME_FILE", runtime_file),
            patch.object(app, "_pid_is_running", return_value=False),
            patch.object(app.urllib.request, "urlopen") as urlopen,
        ):
            self.assertFalse(app.open_existing_runtime())
        urlopen.assert_not_called()

    def test_existing_runtime_rejects_fake_200_and_port_reuse_nonce_mismatch(self):
        runtime = {
            "pid": 4242,
            "port": 17860,
            "appId": app.RUNTIME_APP_ID,
            "runtimeNonce": "expected-runtime-nonce-123",
            "activationToken": "expected-activation-token-123",
        }

        class FakeResponse:
            status = 200

            def __init__(self, payload):
                self.payload = payload

            def read(self, _limit=-1):
                return json.dumps(self.payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        fake_ok = FakeResponse({"ok": True})
        wrong_nonce = FakeResponse(
            {
                "ok": True,
                "appId": app.RUNTIME_APP_ID,
                "runtimePid": 4242,
                "runtimeNonce": "different-runtime-nonce-123",
                "activationTokenHash": app._activation_token_hash(runtime["activationToken"]),
            }
        )
        with patch.object(app, "_pid_is_running", return_value=True):
            with patch.object(app.urllib.request, "urlopen", return_value=fake_ok):
                self.assertFalse(app._probe_runtime_identity(runtime))
            with patch.object(app.urllib.request, "urlopen", return_value=wrong_nonce):
                self.assertFalse(app._probe_runtime_identity(runtime))

    def test_restart_readiness_requires_ui_and_lifecycle_on_both_channels(self):
        runtime = {
            "pid": 4242,
            "port": 17860,
            "appId": app.RUNTIME_APP_ID,
            "runtimeNonce": "expected-runtime-nonce-123",
            "activationToken": "expected-activation-token-123",
            "uiReady": False,
            "independentLifecycle": True,
        }
        health = {
            "ok": True,
            "appId": app.RUNTIME_APP_ID,
            "runtimePid": 4242,
            "runtimeNonce": "expected-runtime-nonce-123",
            "activationTokenHash": app._activation_token_hash(runtime["activationToken"]),
            "uiReady": False,
            "independentLifecycle": True,
        }
        with patch.object(app, "_pid_is_running", return_value=True):
            self.assertTrue(app._runtime_identity_is_valid(runtime, health))
            self.assertFalse(
                app._runtime_identity_is_valid(
                    runtime,
                    health,
                    require_ui_ready=True,
                    require_independent=True,
                )
            )
            runtime["uiReady"] = True
            health["uiReady"] = True
            health["independentLifecycle"] = False
            self.assertFalse(
                app._runtime_identity_is_valid(
                    runtime,
                    health,
                    require_ui_ready=True,
                    require_independent=True,
                )
            )
            health["independentLifecycle"] = True
            self.assertTrue(
                app._runtime_identity_is_valid(
                    runtime,
                    health,
                    require_ui_ready=True,
                    require_independent=True,
                )
            )

    def test_ui_ready_is_published_atomically_and_cleared_on_write_failure(self):
        server = type("FakeServer", (), {"ui_ready": threading.Event()})()
        observed = []
        with patch.object(
            app,
            "_write_runtime_discovery",
            side_effect=lambda current: observed.append(current.ui_ready.is_set()),
        ):
            app._mark_ui_ready(server)
        self.assertEqual(observed, [True])
        self.assertTrue(server.ui_ready.is_set())

        server.ui_ready.clear()
        with patch.object(app, "_write_runtime_discovery", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                app._mark_ui_ready(server)
        self.assertFalse(server.ui_ready.is_set())

    def test_native_window_show_restores_before_focusing(self):
        calls = []

        class FakeWindow:
            def show(self):
                calls.append("show")

            def restore(self):
                calls.append("restore")

        server = app.ManagerServer.__new__(app.ManagerServer)
        server.native_window = True
        server.native_window_object = FakeWindow()
        server.shutdown_started = threading.Event()
        with patch.object(app, "focus_process_window", side_effect=lambda _pid: calls.append("focus") or True):
            self.assertTrue(server.show_native_window())

        self.assertEqual(calls, ["show", "restore", "focus"])

    def test_native_window_show_does_not_accept_queued_webview_calls_without_real_window(self):
        calls = []

        class FakeWindow:
            def show(self):
                calls.append("show")

            def restore(self):
                calls.append("restore")

        server = app.ManagerServer.__new__(app.ManagerServer)
        server.native_window = True
        server.native_window_object = FakeWindow()
        server.shutdown_started = threading.Event()
        with (
            patch.object(app, "focus_process_window", return_value=False) as focus,
            patch.object(app.time, "sleep"),
        ):
            self.assertFalse(server.show_native_window())

        self.assertEqual(focus.call_count, 20)
        self.assertEqual(calls.count("show"), 20)
        self.assertEqual(calls.count("restore"), 20)

    def test_startup_activation_failure_is_reported_and_routes_through_safe_shutdown(self):
        server = type("FakeServer", (), {"force_exit": False})()
        with (
            patch.object(app, "_write_shutdown_status") as status,
            patch.object(app, "_report_startup_error") as report,
            patch.object(app, "request_application_shutdown", return_value=True) as shutdown,
        ):
            app._handle_startup_activation_failure(
                server,
                "native-window-activation-error",
                core.ManagerError("WebView2 没有创建可见窗口"),
            )

        self.assertTrue(server.force_exit)
        self.assertIn("回滚 Codex 配置", report.call_args.args[0])
        status.assert_called_once()
        shutdown.assert_called_once_with(server)

    def test_native_webview_exit_before_ready_rolls_back_and_reports(self):
        calls = []

        class FakeRuntime:
            def activate_configuration_session(self):
                calls.append("activate")

            def close(self):
                calls.append("runtime-close")
                return {"errors": [], "completed": True, "restorationComplete": True}

            def close_for_restart(self):
                return self.close()

        class FakeHook:
            def __iadd__(self, _callback):
                return self

        class FakeWindow:
            events = type("Events", (), {"closing": FakeHook()})()
            def destroy(self):
                calls.append("window-destroy")

        runtime = FakeRuntime()

        class FakeServer:
            def __init__(self):
                self.runtime = runtime
                self.server_address = ("127.0.0.1", 17860)
                self.activation_token = "activation-token-1234567890"
                self.runtime_nonce = "runtime-nonce-1234567890"
                self.native_window = False
                self.native_window_object = None
                self.allow_forced_process_exit = False
                self.quick_restart_requested = False
                self.force_exit = False
                self.shutdown_started = threading.Event()
                self.shutdown_finished = threading.Event()
                self.shutdown_aborted = threading.Event()

            def claim_shutdown(self):
                if self.shutdown_started.is_set():
                    return False
                self.stop_accepting_mutations()
                self.shutdown_started.set()
                return True

            def ui_url(self):
                return "http://127.0.0.1:17860/#token=test-token"

            def serve_forever(self, poll_interval=0.25):
                calls.append("serve")

            def stop_accepting_mutations(self):
                calls.append("stop-writes")

            def wait_for_mutations(self, _timeout):
                return True

            def stop_tray(self):
                calls.append("stop-tray")

            def shutdown(self):
                calls.append("shutdown")

            def server_close(self):
                calls.append("server-close")

        server = FakeServer()

        class FakeWebview:
            @staticmethod
            def create_window(*_args, **_kwargs):
                return FakeWindow()

            @staticmethod
            def start(_callback, **_kwargs):
                calls.append("webview-returned-before-ready")

        with (
            patch.dict(app.sys.modules, {"webview": FakeWebview}),
            patch.object(core, "ensure_state"),
            patch.object(core, "atomic_write_json"),
            patch.object(app, "_consume_restart_handoff", return_value=False),
            patch.object(app, "_acquire_instance_mutex", return_value=True),
            patch.object(app, "_cleanup_runtime", side_effect=lambda: calls.append("cleanup-runtime")),
            patch.object(app, "_write_shutdown_status"),
            patch.object(app, "_forced_exit_watchdog"),
            patch.object(app, "_release_instance_mutex"),
            patch.object(app, "_report_startup_error") as report,
            patch.object(app, "ManagerRuntime", return_value=runtime),
            patch.object(app, "ManagerServer", return_value=server),
        ):
            result = app.run_server(0, open_window=True, native_window=True)

        self.assertEqual(result, 1)
        self.assertNotIn("activate", calls)
        self.assertIn("runtime-close", calls)
        self.assertIn("server-close", calls)
        self.assertIn("cleanup-runtime", calls)
        self.assertIn("窗口就绪前退出", report.call_args.args[0])

    def test_unexpected_native_window_exit_quick_restarts_without_closing_codex(self):
        server = type(
            "FakeServer",
            (),
            {
                "runtime": type("Runtime", (), {"configuration_session": {"active": True}})(),
                "shutdown_started": threading.Event(),
                "shutdown_finished": threading.Event(),
                "shutdown_aborted": threading.Event(),
                "exit_only_requested": False,
            },
        )()

        def restart(_server):
            _server.shutdown_started.set()
            _server.shutdown_finished.set()
            return True

        with (
            patch.dict(app.os.environ, {app.NATIVE_WINDOW_RECOVERY_ENV: ""}),
            patch.object(app, "_write_shutdown_status"),
            patch.object(app, "request_application_restart", side_effect=restart) as request_restart,
            patch.object(app, "_report_startup_error") as report,
        ):
            recovered = app._recover_unexpected_native_window_exit(server, "simulated WebView2 loss")

        self.assertTrue(recovered)
        self.assertFalse(server.exit_only_requested)
        request_restart.assert_called_once_with(server)
        report.assert_not_called()

    def test_repeated_native_window_exit_restores_configuration_and_stops_restart_loop(self):
        server = type(
            "FakeServer",
            (),
            {
                "runtime": type("Runtime", (), {"configuration_session": {"active": True}})(),
                "shutdown_started": threading.Event(),
                "shutdown_finished": threading.Event(),
                "shutdown_aborted": threading.Event(),
                "exit_only_requested": False,
            },
        )()

        with (
            patch.dict(app.os.environ, {app.NATIVE_WINDOW_RECOVERY_ENV: "1"}),
            patch.object(app, "_write_shutdown_status") as status,
            patch.object(app, "request_application_restart") as request_restart,
            patch.object(app, "_report_startup_error") as report,
        ):
            recovered = app._recover_unexpected_native_window_exit(server)

        self.assertFalse(recovered)
        self.assertFalse(server.exit_only_requested)
        request_restart.assert_not_called()
        self.assertEqual(status.call_args.args[1], "native-window-recovery-required")
        self.assertIn("恢复原始配置", report.call_args.args[0])

    def test_shutdown_waits_for_blocking_mutator_before_restoring_runtime(self):
        entered = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        events = []
        request_errors = []

        class FakeWeb2API:
            def status(self):
                return {"running": False}

            def start(self):
                raise AssertionError("gateway should not start")

        class FakeRuntime:
            web2api = FakeWeb2API()

            def close(self):
                events.append("runtime-close")
                closed.set()
                return {"errors": []}

            def close_for_restart(self):
                return self.close()

        def blocking_apply(_sync_secrets=False):
            events.append("apply-start")
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test mutator timed out")
            events.append("apply-finish")
            return {"gatewayRequired": False}

        server = app.ManagerServer(("127.0.0.1", 0), app.RequestHandler, FakeRuntime())
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        def post_apply():
            try:
                request = app.urllib.request.Request(
                    f"http://127.0.0.1:{server.server_address[1]}/api/apply",
                    data=b"{}",
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Agent-Manager-Token": server.api_token,
                    },
                )
                with app.urllib.request.urlopen(request, timeout=5) as response:
                    response.read()
            except Exception as exc:
                request_errors.append(exc)

        apply_thread = threading.Thread(target=post_apply)
        try:
            with (
                patch.object(core, "apply_configuration", side_effect=blocking_apply),
                patch.object(core, "configuration_status", return_value={}),
                patch.object(app, "_cleanup_runtime"),
                patch.object(app, "_write_shutdown_status"),
            ):
                apply_thread.start()
                self.assertTrue(entered.wait(2))
                self.assertTrue(app.request_application_shutdown(server))
                self.assertFalse(closed.wait(0.2))
                release.set()
                apply_thread.join(3)
                self.assertTrue(server.shutdown_finished.wait(3))
        finally:
            release.set()
            apply_thread.join(3)
            server_thread.join(3)
            try:
                server.server_close()
            except Exception:
                pass

        self.assertFalse(request_errors)
        self.assertEqual(events, ["apply-start", "apply-finish", "runtime-close"])

    def test_explicit_codex_restart_resolves_launch_before_closing(self):
        server = app.ManagerServer(("127.0.0.1", 0), app.RequestHandler, object())
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        calls = []
        try:
            with (
                patch.object(
                    core,
                    "resolve_codex_launch_plan",
                    side_effect=lambda: calls.append("plan") or {"strategy": "windows_app"},
                ),
                patch.object(
                    core,
                    "close_codex_processes",
                    side_effect=lambda: calls.append("close") or {"closed": ["1"]},
                ),
                patch.object(
                    core,
                    "launch_codex_app",
                    side_effect=lambda **_kwargs: calls.append("launch") or {"started": True},
                ),
            ):
                request = app.urllib.request.Request(
                    f"http://127.0.0.1:{server.server_address[1]}/api/codex/restart",
                    data=b"{}",
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Agent-Manager-Token": server.api_token,
                    },
                )
                with app.urllib.request.urlopen(request, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual(calls, ["plan", "close", "launch"])
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(3)

    def test_shutdown_gate_rejects_writes_while_health_and_window_show_remain_nonblocking(self):
        server = app.ManagerServer(("127.0.0.1", 0), app.RequestHandler, object())
        server.native_window = False
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            self.assertTrue(server.claim_shutdown())
            with app.urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_address[1]}/api/health",
                timeout=2,
            ) as response:
                health = json.loads(response.read().decode("utf-8"))
            self.assertEqual(health["appId"], app.RUNTIME_APP_ID)

            write_request = app.urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/validate",
                data=b"{}",
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Agent-Manager-Token": server.api_token,
                },
            )
            with self.assertRaises(app.urllib.error.HTTPError) as rejected:
                app.urllib.request.urlopen(write_request, timeout=2)
            self.assertEqual(rejected.exception.code, 503)
            rejected.exception.close()

            show_request = app.urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/window/show",
                data=b"{}",
                method="POST",
                headers={"X-Agent-Manager-Activation": server.activation_token},
            )
            with app.urllib.request.urlopen(show_request, timeout=2) as response:
                shown = json.loads(response.read().decode("utf-8"))
            self.assertFalse(shown["shown"])
        finally:
            server.shutdown_started.clear()
            server.reopen_mutations()
            server.shutdown()
            server.server_close()
            server_thread.join(3)

    def test_second_launch_queues_window_activation_while_webview_is_starting(self):
        server = app.ManagerServer(("127.0.0.1", 0), app.RequestHandler, object())
        server.native_window = True
        server.native_window_object = None
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            request = app.urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/window/show",
                data=b"{}",
                method="POST",
                headers={"X-Agent-Manager-Activation": server.activation_token},
            )
            with patch.object(app, "open_browser_window") as browser:
                with app.urllib.request.urlopen(request, timeout=2) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            self.assertTrue(payload["queued"])
            self.assertFalse(payload["shown"])
            self.assertFalse(payload["opened"])
            self.assertTrue(server.window_activation_requested.is_set())
            browser.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(3)

    def test_quick_restart_control_endpoint_rejects_full_api_token(self):
        server = app.ManagerServer(("127.0.0.1", 0), app.RequestHandler, object())
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        port = server.server_address[1]
        try:
            full_api_request = app.urllib.request.Request(
                f"http://127.0.0.1:{port}{app.CONTROL_QUICK_RESTART_PATH}",
                data=b"{}",
                method="POST",
                headers={"X-Agent-Manager-Token": server.api_token},
            )
            with self.assertRaises(app.urllib.error.HTTPError) as rejected:
                app.urllib.request.urlopen(full_api_request, timeout=2)
            self.assertEqual(rejected.exception.code, 403)
            rejected.exception.close()

            control_request = app.urllib.request.Request(
                f"http://127.0.0.1:{port}{app.CONTROL_QUICK_RESTART_PATH}",
                data=b"{}",
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    app.CONTROL_HEADER_NAME: server.control_token,
                },
            )
            with patch.object(app, "request_application_restart", return_value=True) as restart:
                with app.urllib.request.urlopen(control_request, timeout=2) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            self.assertTrue(payload["ok"])
            restart.assert_called_once_with(server)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(3)

    def test_management_api_rejects_reused_host_and_cross_site_write_origins(self):
        server = app.ManagerServer(("127.0.0.1", 0), app.RequestHandler, object())
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        port = server.server_address[1]
        try:
            bad_host = app.urllib.request.Request(f"http://127.0.0.1:{port}/api/health")
            bad_host.add_header("Host", f"example.invalid:{port}")
            with self.assertRaises(app.urllib.error.HTTPError) as rejected_host:
                app.urllib.request.urlopen(bad_host, timeout=2)
            self.assertEqual(rejected_host.exception.code, 421)
            rejected_host.exception.close()

            cross_site = app.urllib.request.Request(
                f"http://127.0.0.1:{port}/api/not-real",
                data=b"{}",
                method="POST",
                headers={
                    "X-Agent-Manager-Token": server.api_token,
                    "Origin": "http://evil.invalid",
                    "Sec-Fetch-Site": "cross-site",
                },
            )
            with self.assertRaises(app.urllib.error.HTTPError) as rejected_origin:
                app.urllib.request.urlopen(cross_site, timeout=2)
            self.assertEqual(rejected_origin.exception.code, 403)
            rejected_origin.exception.close()

            same_origin = app.urllib.request.Request(
                f"http://127.0.0.1:{port}/api/not-real",
                data=b"{}",
                method="POST",
                headers={
                    "X-Agent-Manager-Token": server.api_token,
                    "Origin": f"http://127.0.0.1:{port}",
                    "Sec-Fetch-Site": "same-origin",
                },
            )
            with self.assertRaises(app.urllib.error.HTTPError) as accepted_origin:
                app.urllib.request.urlopen(same_origin, timeout=2)
            self.assertEqual(accepted_origin.exception.code, 404)
            accepted_origin.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(3)

    def test_delete_invalid_accounts_api_reapplies_managed_agent_configuration(self):
        calls = []

        class FakeWeb2API:
            def status(self):
                calls.append("status")
                return {"running": False}

            def start(self):
                calls.append("start")
                return {"running": True}

        runtime = type("Runtime", (), {"web2api": FakeWeb2API()})()
        server = app.ManagerServer(("127.0.0.1", 0), app.RequestHandler, runtime)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        request = app.urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/api/accounts/delete-invalid",
            data=json.dumps({"groupId": "official"}).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Agent-Manager-Token": server.api_token,
            },
        )
        try:
            with (
                patch.object(
                    core,
                    "delete_invalid_accounts",
                    side_effect=lambda group_id: calls.append(f"delete:{group_id}") or {"deleted": 2},
                ),
                patch.object(
                    core,
                    "apply_configuration",
                    side_effect=lambda sync: calls.append(f"apply:{sync}") or {"gatewayRequired": True},
                ),
            ):
                with app.urllib.request.urlopen(request, timeout=3) as response:
                    payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(3)

        self.assertEqual(calls, ["delete:official", "apply:False", "status", "start"])
        self.assertTrue(payload["result"]["configurationApplied"])
        self.assertEqual(payload["result"]["applied"], {"gatewayRequired": True})

    def test_management_api_bounds_concurrency_and_rejects_excess_clients(self):
        server = app.ManagerServer(("127.0.0.1", 0), app.RequestHandler, object())
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        acquired = 0
        try:
            for _ in range(app.MAX_MANAGEMENT_THREADS):
                self.assertTrue(server._thread_slots.acquire(blocking=False))
                acquired += 1
            with self.assertRaises(app.urllib.error.HTTPError) as rejected:
                app.urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_address[1]}/api/health",
                    timeout=2,
                )
            self.assertEqual(rejected.exception.code, 429)
            self.assertEqual(rejected.exception.headers.get("Retry-After"), "1")
            rejected.exception.close()
        finally:
            for _ in range(acquired):
                server._thread_slots.release()
            server.shutdown()
            server.server_close()
            server_thread.join(3)

    def test_tray_runs_in_tracked_daemon_thread_and_stops_cleanly(self):
        class FakeImage:
            def convert(self, _mode):
                return self

        class FakeMenu:
            SEPARATOR = object()

            def __init__(self, *_items):
                pass

        class FakeMenuItem:
            def __init__(self, *_args, **_kwargs):
                pass

        class FakeIcon:
            def __init__(self, *_args):
                self.started = threading.Event()
                self.stopped = threading.Event()
                self.ran_as_daemon = None

            def run(self, setup=None):
                self.ran_as_daemon = threading.current_thread().daemon
                self.visible = True
                if setup:
                    setup(self)
                self.started.set()
                self.stopped.wait(5)

            def stop(self):
                self.stopped.set()

        server = app.ManagerServer(("127.0.0.1", 0), app.RequestHandler, object())
        server.native_window = True
        try:
            with (
                patch("pystray.Icon", FakeIcon),
                patch("pystray.Menu", FakeMenu),
                patch("pystray.MenuItem", FakeMenuItem),
                patch("PIL.Image.open", return_value=FakeImage()),
            ):
                self.assertTrue(server.ensure_tray())
                icon = server.tray
                tray_thread = server.tray_thread
                self.assertIsNotNone(icon)
                self.assertIsNotNone(tray_thread)
                self.assertTrue(icon.started.wait(2))
                self.assertTrue(tray_thread.daemon)
                self.assertTrue(icon.ran_as_daemon)
                server.stop_tray()
                self.assertFalse(tray_thread.is_alive())
                self.assertIsNone(server.tray)
                self.assertIsNone(server.tray_thread)
        finally:
            server.server_close()

    def test_import_refresh_pool_is_bounded_daemon_and_completes(self):
        runtime = object.__new__(app.ManagerRuntime)
        runtime._closed = False
        runtime.account_refresh_lock = threading.RLock()
        runtime.account_refresh_stop = threading.Event()
        runtime.account_refresh_threads = []
        runtime.account_refresh_pending = []
        runtime.account_refresh_active = set()
        runtime.account_refresh_status = {
            "status": "idle",
            "total": 0,
            "completed": 0,
            "ready": 0,
            "partial": 0,
            "error": 0,
            "currentAccountId": None,
            "activeAccountIds": [],
            "startedAt": None,
            "finishedAt": None,
        }
        entered = threading.Event()
        release = threading.Event()
        calls = []
        calls_lock = threading.Lock()

        def refresh(account_ids, *, max_workers, parallel_operations, commit_guard):
            self.assertEqual(max_workers, 1)
            self.assertFalse(parallel_operations)
            self.assertTrue(commit_guard())
            self.assertTrue(threading.current_thread().daemon)
            with calls_lock:
                calls.extend(account_ids)
                if len(calls) >= 2:
                    entered.set()
            self.assertTrue(release.wait(2))
            return {"refreshed": 1, "ready": 1, "partial": 0, "error": 0}

        with patch.object(core, "refresh_codex_accounts", side_effect=refresh):
            status = runtime.schedule_account_refresh([f"account-{index}" for index in range(8)])
            self.assertEqual(status["total"], 8)
            self.assertTrue(entered.wait(2))
            with runtime.account_refresh_lock:
                workers = list(runtime.account_refresh_threads)
            self.assertEqual(len(workers), 2)
            self.assertTrue(all(worker.daemon for worker in workers))
            release.set()
            for worker in workers:
                worker.join(3)

        with runtime.account_refresh_lock:
            final = dict(runtime.account_refresh_status)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["completed"], 8)
        self.assertEqual(final["ready"], 8)
        self.assertEqual(set(calls), {f"account-{index}" for index in range(8)})

    def test_cancelled_background_refresh_never_commits_after_manager_shutdown(self):
        settings = core._initial_settings()
        settings["accounts"] = [{"id": "account-1", "authMode": "chatgpt"}]
        updates = {
            "lastRefreshedAt": core.now_iso(),
            "refreshState": "ready",
            "usage": {"weekly": {"remainingPercent": 50}},
        }
        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "_probe_codex_account", return_value=updates),
            patch.object(core, "save_settings") as save,
        ):
            result = core.refresh_codex_accounts(
                ["account-1"],
                max_workers=1,
                parallel_operations=False,
                commit_guard=lambda: False,
            )
        self.assertTrue(result["cancelled"])
        save.assert_not_called()
        self.assertNotIn("usage", settings["accounts"][0])

    def test_quota_auto_refresh_tick_queues_only_due_accounts_and_can_be_disabled(self):
        runtime = object.__new__(app.ManagerRuntime)
        runtime._closed = False
        runtime.account_refresh_lock = threading.RLock()
        runtime.account_auto_refresh_status = {
            "enabled": True,
            "intervalMinutes": 5,
            "lastCheckedAt": None,
            "lastQueuedAt": None,
            "queued": 0,
            "lastError": None,
        }
        queued = []
        runtime.schedule_account_refresh = lambda account_ids: queued.extend(account_ids) or {}

        with (
            patch.object(
                core,
                "load_settings",
                return_value={"appBehavior": {"quotaRefreshMinutes": 5}},
            ),
            patch.object(core, "stale_codex_account_ids", return_value=["official-a", "official-b"]) as stale,
        ):
            status = runtime._account_auto_refresh_tick()

        stale.assert_called_once_with(300)
        self.assertEqual(queued, ["official-a", "official-b"])
        self.assertEqual(status["queued"], 2)
        self.assertTrue(status["enabled"])

        queued.clear()
        with (
            patch.object(
                core,
                "load_settings",
                return_value={"appBehavior": {"quotaRefreshMinutes": 0}},
            ),
            patch.object(core, "stale_codex_account_ids") as stale,
        ):
            status = runtime._account_auto_refresh_tick()

        stale.assert_not_called()
        self.assertEqual(queued, [])
        self.assertFalse(status["enabled"])

    def test_mail_health_tick_checks_only_due_accounts_and_can_be_disabled(self):
        runtime = object.__new__(app.ManagerRuntime)
        runtime._closed = False
        runtime.mail_health_stop = threading.Event()
        runtime.mail_health_status = {
            "enabled": True,
            "intervalHours": 24,
            "status": "waiting",
            "lastCheckedAt": None,
            "checked": 0,
            "healthy": 0,
            "error": 0,
            "lastError": None,
        }
        with (
            patch.object(core, "load_settings", return_value={"appBehavior": {"mailHealthCheckHours": 24}}),
            patch.object(app.toolbox, "stale_mail_account_ids", return_value=["mail-a", "mail-b"]) as stale,
            patch.object(
                app.toolbox,
                "check_saved_email_health",
                side_effect=[
                    {"status": "healthy", "healthy": True, "saved": True},
                    {"status": "auth_error", "healthy": False, "saved": True},
                ],
            ) as check,
        ):
            status = runtime._mail_health_tick()

        stale.assert_called_once_with(86_400)
        self.assertEqual(check.call_count, 2)
        self.assertEqual(status["status"], "completed")
        self.assertEqual((status["checked"], status["healthy"], status["error"]), (2, 1, 1))

        runtime.mail_health_status = dict(status)
        with (
            patch.object(core, "load_settings", return_value={"appBehavior": {"mailHealthCheckHours": 0}}),
            patch.object(app.toolbox, "stale_mail_account_ids") as stale,
        ):
            disabled = runtime._mail_health_tick()
        stale.assert_not_called()
        self.assertFalse(disabled["enabled"])
        self.assertEqual(disabled["status"], "disabled")

    def test_mail_health_recalculate_wake_does_not_trigger_an_immediate_second_check(self):
        runtime = object.__new__(app.ManagerRuntime)
        runtime._closed = False
        runtime.mail_health_stop = threading.Event()
        waits = []

        class FakeWake:
            def wait(self, seconds):
                waits.append(seconds)
                if len(waits) == 1:
                    return True
                runtime.mail_health_stop.set()
                return False

            def clear(self):
                pass

        runtime.mail_health_wake = FakeWake()
        runtime.mail_health_status = {
            "enabled": True,
            "intervalHours": 24,
            "status": "waiting",
            "lastCheckedAt": None,
            "checked": 0,
            "healthy": 0,
            "error": 0,
            "lastError": None,
        }
        with (
            patch.object(runtime, "_mail_health_tick") as tick,
            patch.object(runtime, "_mail_health_interval_hours", side_effect=[24, 48]),
        ):
            runtime._run_mail_health_checks()

        tick.assert_called_once_with()
        self.assertEqual(waits, [86_400, 172_800])

    def test_quota_auto_refresh_waits_full_interval_and_wake_only_recalculates(self):
        runtime = object.__new__(app.ManagerRuntime)
        runtime._closed = False
        runtime.account_refresh_lock = threading.RLock()
        runtime.account_auto_refresh_stop = threading.Event()
        waits = []

        class FakeWake:
            def wait(self, seconds):
                waits.append(seconds)
                if len(waits) == 1:
                    return True
                runtime.account_auto_refresh_stop.set()
                return False

            def clear(self):
                pass

        runtime.account_auto_refresh_wake = FakeWake()
        runtime.account_auto_refresh_status = {
            "enabled": True,
            "intervalMinutes": 5,
            "lastCheckedAt": None,
            "lastQueuedAt": None,
            "queued": 0,
            "lastError": None,
        }

        with (
            patch.object(runtime, "_account_auto_refresh_tick") as tick,
            patch.object(runtime, "_account_auto_refresh_interval_minutes", side_effect=[5, 10]),
        ):
            runtime._run_account_auto_refresh()

        tick.assert_called_once_with()
        self.assertEqual(waits, [300, 600])

    def test_quota_auto_refresh_idle_wait_does_not_repeat_refresh(self):
        runtime = object.__new__(app.ManagerRuntime)
        runtime._closed = False
        runtime.account_refresh_lock = threading.RLock()
        runtime.account_auto_refresh_stop = threading.Event()
        runtime.account_auto_refresh_wake = threading.Event()
        runtime.account_auto_refresh_thread = None
        runtime.account_auto_refresh_status = {
            "enabled": True,
            "intervalMinutes": 5,
            "lastCheckedAt": None,
            "lastQueuedAt": None,
            "queued": 0,
            "lastError": None,
        }
        checked = threading.Event()

        def tick():
            checked.set()
            return runtime.account_auto_refresh_status

        with (
            patch.object(runtime, "_account_auto_refresh_tick", side_effect=tick) as refresh,
            patch.object(runtime, "_account_auto_refresh_interval_minutes", return_value=5),
        ):
            runtime._start_account_auto_refresh()
            self.assertTrue(checked.wait(1))
            time.sleep(0.15)
            runtime._stop_account_auto_refresh()

        refresh.assert_called_once_with()

    def test_shutdown_recovers_manager_when_codex_cannot_be_closed_safely(self):
        calls = []

        class FakeRuntime:
            def close(self):
                calls.append("runtime-close")
                raise RuntimeError("simulated restore failure")

        class FakeServer:
            def __init__(self):
                self.force_exit = False
                self.native_window = False
                self.allow_forced_process_exit = False
                self.shutdown_lock = threading.RLock()
                self.shutdown_started = threading.Event()
                self.shutdown_finished = threading.Event()
                self.shutdown_aborted = threading.Event()
                self.native_window_object = None
                self.runtime = FakeRuntime()

            def claim_shutdown(self):
                with self.shutdown_lock:
                    if self.shutdown_started.is_set():
                        return False
                    self.shutdown_started.set()
                    return True

            def stop_tray(self):
                calls.append("tray-stop")

            def shutdown(self):
                calls.append("server-shutdown")

            def server_close(self):
                calls.append("server-close")

            def serve_forever(self, poll_interval=0.25):
                calls.append("server-recovered")

            def ensure_tray(self):
                calls.append("tray-recovered")
                return True

        server = FakeServer()
        with (
            patch.object(app, "_write_shutdown_status"),
            patch.object(app, "_cleanup_runtime", side_effect=lambda: calls.append("runtime-file-cleanup")),
        ):
            self.assertTrue(app.request_application_shutdown(server))
            self.assertTrue(server.shutdown_finished.wait(3))

        self.assertFalse(server.force_exit)
        self.assertTrue(server.shutdown_aborted.is_set())
        self.assertFalse(server.shutdown_started.is_set())
        self.assertEqual(calls.count("runtime-close"), 1)
        self.assertEqual(calls.count("server-shutdown"), 1)
        self.assertEqual(calls.count("server-close"), 0)
        self.assertNotIn("runtime-file-cleanup", calls)
        self.assertIn("server-recovered", calls)
        self.assertIn("tray-recovered", calls)

    def test_title_bar_close_restores_codex_unless_minimizing_to_tray(self):
        calls = []

        class FakeWindow:
            def hide(self):
                calls.append("hide")

        class FakeServer:
            force_exit = False

            def ensure_tray(self):
                calls.append("ensure-tray")
                return True

        server = FakeServer()
        window = FakeWindow()
        with (
            patch.object(core, "load_settings", return_value={"appBehavior": {"closeToTray": False}}),
            patch.object(
                app,
                "request_application_exit_only",
                side_effect=AssertionError("ordinary close must restore configuration"),
            ),
            patch.object(
                app,
                "request_application_shutdown",
                side_effect=lambda _server: calls.append("shutdown") or True,
            ),
        ):
            self.assertFalse(app.handle_native_window_closing(server, window))
        self.assertEqual(calls, ["shutdown"])

        calls.clear()
        with patch.object(core, "load_settings", return_value={"appBehavior": {"closeToTray": True}}):
            self.assertFalse(app.handle_native_window_closing(server, window))
        self.assertEqual(calls, ["ensure-tray", "hide"])

        calls.clear()
        server.force_exit = True
        with patch.object(core, "load_settings", return_value={"appBehavior": {"closeToTray": False}}):
            self.assertIsNone(app.handle_native_window_closing(server, window))
        self.assertEqual(calls, [])

    def test_rotate_public_web2api_key_preserves_running_codex(self):
        calls = []
        settings = {"web2api": {"activeForCodex": True}}

        class FakeWeb2API:
            def status(self):
                return {"running": True, "activeForCodex": True}

        runtime = type("Runtime", (), {"web2api": FakeWeb2API()})()
        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "codex_prefix", return_value=["codex.exe"]),
            patch.object(core, "load_service_secret", return_value="old-key"),
            patch.object(core, "close_codex_processes", side_effect=lambda: calls.append("close") or {}),
            patch.object(core, "rotate_web2api_key", side_effect=lambda: calls.append("rotate") or "new-key"),
            patch.object(core, "apply_configuration", side_effect=lambda *_args: calls.append("apply") or {}),
            patch.object(core, "launch_codex_app", side_effect=lambda **_kwargs: calls.append("launch") or {"started": True}),
        ):
            result = app.rotate_web2api_key_for_runtime(runtime)
        self.assertEqual(calls, ["rotate"])
        self.assertEqual(result["apiKey"], "new-key")
        self.assertIsNone(result["closed"])
        self.assertIsNone(result["applied"])
        self.assertIsNone(result["launch"])

    def test_restore_orchestration_defaults_keeps_active_source_and_clears_custom_routes(self):
        core.ensure_state()
        settings = core.load_settings()
        settings["accounts"] = [
            {
                "id": "restore-account",
                "label": "Restore",
                "authMode": "chatgpt",
                "models": ["gpt-restore"],
                "groupId": "official",
                "refreshState": "ready",
                "proxyEnabled": False,
            }
        ]
        settings["modelWorkspace"].update({"mode": "aggregate", "activeSourceId": "account:restore-account", "selectAll": False})
        settings["subagentRouting"]["routes"]["expert"]["models"] = ["account:restore-account::gpt-restore"]
        core.save_settings(settings)
        restored = core.restore_orchestration_defaults()
        self.assertEqual(restored["modelWorkspace"]["mode"], "independent")
        self.assertEqual(restored["modelWorkspace"]["activeSourceId"], "account:restore-account")
        self.assertTrue(restored["modelWorkspace"]["selectAll"])
        self.assertEqual(restored["subagentRouting"]["routes"]["expert"]["models"], [])

    def test_transient_refresh_failure_keeps_active_imported_account_visible_with_local_models(self):
        settings = core._initial_settings()
        settings["accounts"] = [
            {
                "id": "cockpit-account",
                "label": "Cockpit Account",
                "authMode": "chatgpt",
                "sourceType": "codex_auth",
                "codexCompatible": True,
                "quotaOnly": False,
                "models": [],
                "groupId": "official",
                "refreshState": "error",
                "refreshErrors": {"models": "SSL EOF while reading"},
            }
        ]
        settings["modelWorkspace"]["activeSourceId"] = "account:stale-account"

        with patch.object(core, "current_auth_state", return_value={"activeAccountId": "cockpit-account"}):
            source = core.model_sources(settings)[0]

        self.assertTrue(source["active"])
        self.assertTrue(source["available"])
        self.assertEqual(source["modelCatalogSource"], "local_runtime")
        self.assertEqual([item["id"] for item in source["models"]], ["gpt-test-old"])

    def test_authentication_failure_never_becomes_available_from_local_model_fallback(self):
        settings = core._initial_settings()
        settings["accounts"] = [
            {
                "id": "expired-account",
                "label": "Expired",
                "authMode": "chatgpt",
                "sourceType": "codex_auth",
                "codexCompatible": True,
                "quotaOnly": False,
                "models": [],
                "groupId": "official",
                "refreshState": "error",
                "refreshErrors": {"account": "HTTP 401 unauthorized"},
            }
        ]

        source = core.model_sources(settings)[0]

        self.assertFalse(source["available"])
        self.assertEqual(source["invalidReason"], "认证已失效")

    def test_restore_defaults_replaces_legacy_provider_profile_without_requiring_its_key(self):
        core.ensure_state()
        settings = core.load_settings()
        settings["providers"].append(
            {
                "id": "codex_local_access",
                "name": "Legacy local access",
                "kind": "custom",
                "baseUrl": "http://127.0.0.1:9/v1",
                "envKey": "CODEX_LOCAL_ACCESS_KEY",
                "wireApi": "responses",
                "models": [],
            }
        )
        settings["mainProfiles"] = [
            {
                "id": "current",
                "name": "Legacy current",
                "provider": "codex_local_access",
                "model": "cam-stale-model",
                "effort": "high",
            }
        ]
        settings["activeMainProfileId"] = "current"
        core.save_settings(settings)

        restored = core.restore_orchestration_defaults()
        with patch.object(core, "load_provider_key", side_effect=AssertionError("legacy key must not be read")):
            applied = core.apply_configuration(False)

        updated = core.load_settings()
        profile = next(item for item in updated["mainProfiles"] if item["id"] == updated["activeMainProfileId"])
        parsed = tomllib.loads(core.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(profile["provider"], "openai")
        self.assertFalse(updated["web2api"]["activeForCodex"])
        self.assertEqual(restored["modelWorkspace"]["mode"], "independent")
        self.assertNotEqual(parsed.get("model_provider"), "codex_local_access")
        self.assertIn("changed", applied)

    def test_unified_group_move_and_bulk_delete_accounts_and_providers(self):
        core.ensure_state()
        group = core.save_account_group({"name": "批量组", "color": "cyan"})
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            account = core.import_codex_account(
                {"authJson": {"OPENAI_API_KEY": "sk-bulk-account"}, "groupId": "official"}
            )
            provider = core.save_provider(
                {
                    "id": "bulk_relay",
                    "name": "Bulk Relay",
                    "baseUrl": "https://bulk.invalid/v1",
                    "envKey": "BULK_RELAY_KEY",
                }
            )
            moved = core.assign_sources_to_group(group["id"], [account["id"]], [provider["id"]])
            self.assertEqual(moved["changed"], 2)
            deleted = core.remove_model_sources_batch([account["id"]], [provider["id"]])
        self.assertEqual(deleted["deleted"], 2)
        self.assertFalse(deleted["failed"])

    def test_deleting_subagent_account_clears_routes_and_keeps_efforts_aligned(self):
        core.ensure_state()
        settings = core.load_settings()
        settings["accounts"] = [
            {
                "id": "delete-account",
                "label": "Delete",
                "authMode": "chatgpt",
                "models": ["gpt-delete-a", "gpt-delete-b"],
                "groupId": "official",
            },
            {
                "id": "keep-account",
                "label": "Keep",
                "authMode": "chatgpt",
                "models": ["gpt-keep"],
                "groupId": "official",
            },
        ]
        settings["modelWorkspace"].update(
            {
                "activeSourceId": "account:delete-account",
                "defaultModelKey": "account:delete-account::gpt-delete-a",
                "selectedModels": [
                    "account:delete-account::gpt-delete-a",
                    "account:keep-account::gpt-keep",
                ],
            }
        )
        settings["subagentRouting"]["routes"]["simple"] = {
            "models": ["account:delete-account::gpt-delete-a"],
            "efforts": ["low"],
        }
        settings["subagentRouting"]["routes"]["hard"] = {
            "models": [
                "account:delete-account::gpt-delete-a",
                "account:keep-account::gpt-keep",
                "account:delete-account::gpt-delete-b",
            ],
            "efforts": ["low", "high", "xhigh"],
        }
        settings["web2api"].update(
            {
                "activeForCodex": True,
                "activeAccountId": "delete-account",
                "accountIds": ["delete-account", "keep-account"],
            }
        )
        core.save_settings(settings)

        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            core.remove_codex_account("delete-account")

        updated = core.load_settings()
        self.assertEqual([item["id"] for item in updated["accounts"]], ["keep-account"])
        self.assertEqual(updated["modelWorkspace"]["activeSourceId"], "")
        self.assertEqual(updated["modelWorkspace"]["defaultModelKey"], "")
        self.assertEqual(
            updated["modelWorkspace"]["selectedModels"],
            ["account:keep-account::gpt-keep"],
        )
        self.assertEqual(updated["subagentRouting"]["routes"]["simple"], {"models": [], "efforts": []})
        self.assertEqual(
            updated["subagentRouting"]["routes"]["hard"],
            {"models": ["account:keep-account::gpt-keep"], "efforts": ["high"]},
        )
        self.assertFalse(updated["web2api"]["activeForCodex"])
        self.assertIsNone(updated["web2api"]["activeAccountId"])
        self.assertEqual(updated["web2api"]["accountIds"], ["keep-account"])

    def test_provider_balance_parser_supports_balance_and_grant_payloads(self):
        direct = core._parse_provider_balance({"remaining_balance": "12.50", "currency": "usd"})
        grants = core._parse_provider_balance({"data": {"total_granted": 20, "total_used": 7.25}})
        self.assertEqual(direct["amount"], 12.5)
        self.assertEqual(direct["currency"], "USD")
        self.assertEqual(grants["amount"], 12.75)

    def test_delete_invalid_accounts_is_limited_to_viewed_group(self):
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            official = core.import_codex_account(
                {"authJson": {"OPENAI_API_KEY": "sk-invalid-official"}, "groupId": "official"}
            )
            free = core.import_codex_account(
                {"authJson": {"OPENAI_API_KEY": "sk-invalid-free"}, "groupId": "free"}
            )
            settings = core.load_settings()
            for item in settings["accounts"]:
                if item["id"] in {official["id"], free["id"]}:
                    item["tokenExpiresAt"] = "2020-01-01T00:00:00+00:00"
            core.save_settings(settings)
            result = core.delete_invalid_accounts("free")
        remaining = {item["id"] for item in core.load_settings()["accounts"]}
        self.assertEqual(result["deleted"], 1)
        self.assertIn(official["id"], remaining)
        self.assertNotIn(free["id"], remaining)

    def test_deletions_restore_settings_when_secret_write_fails(self):
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            account = core.import_codex_account(
                {"authJson": {"OPENAI_API_KEY": "test-account-value"}, "groupId": "official"}
            )
            real_atomic_write_json = core.atomic_write_json

            def fail_secret_write(path, payload):
                if path == core.SECRETS_FILE:
                    raise OSError("secret write failed")
                return real_atomic_write_json(path, payload)

            before = (core.SETTINGS_FILE.read_bytes(), core.SECRETS_FILE.read_bytes())
            with patch.object(core, "atomic_write_json", side_effect=fail_secret_write):
                with self.assertRaisesRegex(OSError, "secret write failed"):
                    core.remove_codex_account(account["id"])
            self.assertEqual(before, (core.SETTINGS_FILE.read_bytes(), core.SECRETS_FILE.read_bytes()))

            settings = core.load_settings()
            target = next(item for item in settings["accounts"] if item["id"] == account["id"])
            target["tokenExpiresAt"] = "2020-01-01T00:00:00+00:00"
            core.save_settings(settings)
            before = (core.SETTINGS_FILE.read_bytes(), core.SECRETS_FILE.read_bytes())
            with patch.object(core, "atomic_write_json", side_effect=fail_secret_write):
                with self.assertRaisesRegex(OSError, "secret write failed"):
                    core.delete_invalid_accounts("official")
            self.assertEqual(before, (core.SETTINGS_FILE.read_bytes(), core.SECRETS_FILE.read_bytes()))

            core.save_provider(
                {
                    "id": "delete_rollback_relay",
                    "name": "Delete Rollback Relay",
                    "baseUrl": "https://delete-rollback.example.test/v1",
                    "envKey": "DELETE_ROLLBACK_KEY",
                    "models": ["delete-rollback-model"],
                }
            )
            core.store_provider_key("delete_rollback_relay", "test-provider-value")
            before = (core.SETTINGS_FILE.read_bytes(), core.SECRETS_FILE.read_bytes())
            with (
                patch.object(core, "discover_agents", return_value=[]),
                patch.object(core, "atomic_write_json", side_effect=fail_secret_write),
            ):
                with self.assertRaisesRegex(OSError, "secret write failed"):
                    core.remove_provider("delete_rollback_relay")
            self.assertEqual(before, (core.SETTINGS_FILE.read_bytes(), core.SECRETS_FILE.read_bytes()))

    def test_refresh_capable_401_and_402_accounts_are_invalid_but_transient_errors_are_not(self):
        base = {
            "authMode": "chatgpt",
            "refreshCapable": True,
            "refreshState": "error",
        }
        self.assertEqual(
            core.account_invalid_reason(
                {
                    **base,
                    "refreshErrors": {
                        "account": "HTTP 401: Your authentication token has been invalidated."
                    },
                }
            ),
            "认证已失效",
        )
        self.assertEqual(
            core.account_invalid_reason(
                {
                    **base,
                    "refreshErrors": {
                        "usage": "HTTP 402: deactivated_workspace"
                    },
                }
            ),
            "认证已失效",
        )
        for message in (
            "HTTP 429: Connector rate limit exceeded",
            "HTTP 500: upstream unavailable",
            "SSL: UNEXPECTED_EOF_WHILE_READING",
            "timed out while connecting",
        ):
            self.assertIsNone(
                core.account_invalid_reason(
                    {**base, "refreshErrors": {"account": message}}
                )
            )

    def test_removing_custom_group_rehomes_accounts_and_providers(self):
        core.ensure_state()
        group = core.save_account_group({"name": "临时组", "color": "cyan"})
        settings = core.load_settings()
        settings["accounts"].append(
            {
                "id": "account_group_test",
                "label": "Grouped account",
                "groupId": group["id"],
                "sourceType": "codex_auth",
                "proxyEnabled": False,
            }
        )
        core.save_settings(settings)
        core.save_provider(
            {
                "id": "grouped_provider",
                "name": "Grouped Provider",
                "baseUrl": "https://example.invalid/v1",
                "envKey": "GROUPED_PROVIDER_KEY",
                "groupId": group["id"],
            }
        )

        result = core.remove_account_group(group["id"])
        settings = core.load_settings()

        self.assertEqual(result, {"movedAccounts": 1, "movedProviders": 1})
        self.assertEqual(settings["accounts"][0]["groupId"], "official")
        provider = next(item for item in settings["providers"] if item["id"] == "grouped_provider")
        self.assertEqual(provider["groupId"], "relay")
        self.assertFalse(any(item["id"] == group["id"] for item in settings["accountGroups"]))

    def test_switch_session_sync_preserves_database_and_rollout_bytes(self):
        rollout = self.root / "sessions" / "2026" / "08" / "rollout-unchanged.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text(
            json.dumps({"type": "session_meta", "payload": {"model_provider": "old-provider"}}) + "\n",
            encoding="utf-8",
        )
        database = self.root / "state_5.sqlite"
        connection = core.sqlite3.connect(database)
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, has_user_event INTEGER, "
            "first_user_message TEXT, thread_source TEXT, rollout_path TEXT)"
        )
        connection.execute("INSERT INTO threads VALUES ('one', 'old-provider', 0, 'hello', '', ?)", (str(rollout),))
        connection.commit()
        connection.close()
        before = (hashlib.sha256(database.read_bytes()).digest(), hashlib.sha256(rollout.read_bytes()).digest())
        with (
            patch.object(core, "repair_codex_session_visibility", side_effect=AssertionError("must not repair")),
            patch.object(core, "perform_history_sync", side_effect=AssertionError("must not mirror")),
        ):
            result = core.auto_sync_sessions_after_switch("openai")
        after = (hashlib.sha256(database.read_bytes()).digest(), hashlib.sha256(rollout.read_bytes()).digest())
        self.assertTrue(result["preserved"])
        self.assertEqual(after, before)

    def test_session_visibility_repair_is_bounded_and_backed_up(self):
        rollout = self.root / "sessions" / "2026" / "08" / "rollout-test.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text(
            json.dumps({"type": "session_meta", "payload": {"model_provider": "old-provider", "cwd": "D:/work"}})
            + "\n"
            + json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "hello"}})
            + "\n",
            encoding="utf-8",
        )
        database = self.root / "state_5.sqlite"
        connection = core.sqlite3.connect(database)
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, has_user_event INTEGER, first_user_message TEXT, thread_source TEXT, rollout_path TEXT)"
        )
        connection.execute(
            "INSERT INTO threads VALUES ('one', 'old-provider', 0, 'hello', '', ?)",
            (str(rollout),),
        )
        connection.commit()
        connection.close()
        unrelated = self.root / "state_9.sqlite"
        other = core.sqlite3.connect(unrelated)
        other.execute("CREATE TABLE sentinel (value TEXT)")
        other.execute("INSERT INTO sentinel VALUES ('untouched')")
        other.commit()
        other.close()
        core.ensure_state()
        with patch.object(core, "refresh_codex_history_index", side_effect=AssertionError("must not rebuild index")):
            result = core.repair_codex_session_visibility()
        self.assertEqual(result["databases"], 1)
        self.assertGreaterEqual(result["rowsChanged"], 1)
        self.assertEqual(result["rolloutFilesChanged"], 1)
        self.assertTrue(Path(result["backups"][0]).is_file())
        self.assertTrue(Path(result["rolloutBackups"][0]).is_file())
        connection = core.sqlite3.connect(database)
        row = connection.execute(
            "SELECT model_provider, has_user_event, thread_source FROM threads WHERE id='one'"
        ).fetchone()
        connection.close()
        self.assertEqual(row, ("openai", 1, "user"))
        lines = rollout.read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[0])["payload"]["model_provider"], "openai")
        self.assertEqual(json.loads(lines[1])["payload"]["message"], "hello")
        other = core.sqlite3.connect(unrelated)
        self.assertEqual(other.execute("SELECT value FROM sentinel").fetchone()[0], "untouched")
        other.close()

    def test_web2api_chat_conversion_and_sse_completion(self):
        response = {
            "id": "resp_test",
            "model": "gpt-test",
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}],
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        }
        body = (
            "data: "
            + json.dumps({"type": "response.output_text.delta", "delta": "hello"})
            + "\n\n"
            + "data: "
            + json.dumps({"type": "response.completed", "response": response})
            + "\n\n"
        ).encode()
        manager = web2api.Web2APIManager()
        quota_headers = {
            "x-codex-primary-used-percent": "37",
            "x-codex-primary-window-minutes": "10080",
            "x-codex-primary-reset-at": "2000000000",
        }
        with patch.object(manager, "_upstream", return_value={"body": body, "headers": quota_headers}):
            result = manager.execute(
                "/v1/chat/completions",
                {"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}], "stream": False},
            )
            payload = json.loads(result["body"])
            self.assertEqual(payload["choices"][0]["message"]["content"], "hello")
            self.assertEqual(payload["usage"]["total_tokens"], 5)
            self.assertEqual(result["headers"], quota_headers)
            stream = manager.execute(
                "/v1/chat/completions",
                {"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            )
            self.assertIn(b"chat.completion.chunk", stream["body"])
            self.assertTrue(stream["body"].endswith(b"data: [DONE]\n\n"))

    def test_web2api_never_replays_ambiguous_transport_failure_on_another_account(self):
        manager = web2api.Web2APIManager()
        accounts = [
            {"id": "first", "label": "First"},
            {"id": "second", "label": "Second"},
        ]
        with (
            patch.object(manager, "_accounts", return_value=accounts),
            patch.object(
                core,
                "_account_chatgpt_credentials",
                return_value={"accessToken": "token", "accountId": "workspace"},
            ),
            patch.object(core, "_open_same_origin_request", side_effect=web2api.URLError("unexpected EOF")) as opened,
        ):
            with self.assertRaises(web2api.GatewayError) as error:
                manager._open_upstream({"model": "gpt-test", "input": "hello"})

        self.assertEqual(opened.call_count, 1)
        self.assertIn("避免重复请求", str(error.exception))

    def test_web2api_synthesizes_weekly_codex_quota_headers(self):
        account = {
            "plan": "free",
            "usage": {
                "weekly": {
                    "remainingPercent": 81,
                    "windowMinutes": 10_080,
                    "resetAt": "2033-05-18T03:33:20+00:00",
                }
            },
        }
        headers = web2api._codex_quota_headers({}, account)
        self.assertEqual(headers["x-codex-primary-used-percent"], "19")
        self.assertEqual(headers["x-codex-primary-window-minutes"], "10080")
        self.assertEqual(headers["x-codex-primary-reset-at"], "2000000000")
        self.assertEqual(headers["x-codex-plan-type"], "free")

        partial = web2api._codex_quota_headers(
            {"X-Codex-Primary-Used-Percent": "7"},
            account,
        )
        self.assertEqual(partial["x-codex-primary-used-percent"], "7")
        self.assertEqual(partial["x-codex-primary-window-minutes"], "10080")
        self.assertEqual(partial["x-codex-primary-reset-at"], "2000000000")
        self.assertEqual(partial["x-codex-plan-type"], "free")

    def test_web2api_loopback_server_requires_key_and_stops_cleanly(self):
        core.ensure_state()
        probe = core.socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        settings = core.load_settings()
        settings["web2api"]["port"] = port
        core.save_settings(settings)
        manager = web2api.Web2APIManager()
        with (
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
        ):
            api_key = core.rotate_web2api_key()
            try:
                status = manager.start()
                self.assertTrue(status["running"])
                with core.urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
                    health = json.loads(response.read())
                    self.assertEqual(health, {"ok": True, "service": "Agent Manager Web2API"})
                    self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
                unauthorized = web2api.Request(f"http://127.0.0.1:{port}/v1/models")
                with self.assertRaises(web2api.HTTPError) as error:
                    core.urllib.request.urlopen(unauthorized, timeout=3)
                self.assertEqual(error.exception.code, 401)
                error.exception.close()
                authorized = web2api.Request(
                    f"http://127.0.0.1:{port}/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                with core.urllib.request.urlopen(authorized, timeout=3) as response:
                    self.assertEqual(json.loads(response.read()), {"object": "list", "data": []})
                detailed = web2api.Request(
                    f"http://127.0.0.1:{port}/status",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                with core.urllib.request.urlopen(detailed, timeout=3) as response:
                    self.assertIn("maxConcurrentRequests", json.loads(response.read()))
            finally:
                manager.stop()
            self.assertFalse(manager.status()["running"])

    def test_web2api_upstream_concurrency_is_bounded_with_retry_after(self):
        manager = web2api.Web2APIManager()
        server = web2api.GatewayServer(("127.0.0.1", 0), manager)
        try:
            for _ in range(web2api.MAX_UPSTREAM_CONCURRENCY):
                self.assertTrue(server._upstream_slots.acquire(blocking=False))
            with self.assertRaises(web2api.GatewayError) as error:
                with server.upstream_slot():
                    pass
            self.assertEqual(error.exception.status, 429)
            self.assertEqual(error.exception.headers.get("Retry-After"), "2")
            self.assertEqual(manager.rejected_request_count, 1)
        finally:
            for _ in range(web2api.MAX_UPSTREAM_CONCURRENCY):
                server._upstream_slots.release()
            server.server_close()

    def test_settings_reference_integrity_repairs_only_dangling_sources(self):
        settings = core._initial_settings()
        settings["accounts"] = [
            {
                "id": "live-account",
                "label": "Live",
                "groupId": "official",
                "models": ["gpt-live"],
                "authMode": "chatgpt",
                "sourceType": "codex_auth",
                "codexCompatible": True,
            }
        ]
        settings["modelWorkspace"].update(
            {
                "activeSourceId": "account:missing",
                "defaultModelKey": "account:missing::gpt-old",
                "selectAll": False,
                "selectedModels": [
                    "account:live-account::gpt-live",
                    "account:missing::gpt-old",
                ],
            }
        )
        settings["subagentRouting"]["routes"]["simple"] = {
            "models": ["account:missing::gpt-old", "account:live-account::gpt-live"],
            "efforts": ["low", "medium", "high"],
        }
        settings["routes"]["simple"] = {
            "enabled": True,
            "agents": ["luna_worker"],
            "description": "legacy",
        }
        settings["web2api"].update(
            {
                "activeForCodex": True,
                "activeAccountId": "missing",
                "accountIds": ["live-account", "missing"],
                "sourceOrder": ["account:missing", "account:live-account"],
            }
        )
        core.ensure_state()
        core.save_settings(settings)

        before = core.settings_reference_integrity(core.load_settings())
        self.assertGreaterEqual(before["repairableCount"], 4)
        result = core.repair_settings_references()
        after = core.load_settings()

        self.assertTrue(result["changed"])
        self.assertTrue(result["after"]["healthy"])
        self.assertEqual(after["modelWorkspace"]["activeSourceId"], "")
        self.assertEqual(after["modelWorkspace"]["selectedModels"], ["account:live-account::gpt-live"])
        route = after["subagentRouting"]["routes"]["simple"]
        self.assertEqual(route["models"], ["account:live-account::gpt-live"])
        self.assertEqual(route["efforts"], ["medium"])
        self.assertEqual(after["routes"]["simple"]["agents"], [])
        self.assertFalse(after["routes"]["simple"]["enabled"])
        self.assertEqual(after["web2api"]["accountIds"], ["live-account"])
        self.assertEqual(after["web2api"]["sourceOrder"], ["account:live-account"])
        self.assertIsNone(after["web2api"]["activeAccountId"])

    def test_settings_reference_repair_refuses_ambiguous_duplicate_ids(self):
        settings = core._initial_settings()
        settings["accounts"] = [
            {"id": "same", "label": "One", "groupId": "official"},
            {"id": "same", "label": "Two", "groupId": "official"},
        ]
        core.ensure_state()
        core.save_settings(settings)
        with self.assertRaisesRegex(core.ManagerError, "不能自动推断"):
            core.repair_settings_references()

    def test_settings_reference_repair_removes_stale_models_from_existing_source(self):
        settings = core._initial_settings()
        settings["accounts"] = [
            {
                "id": "live-account",
                "label": "Live",
                "groupId": "official",
                "models": ["gpt-live"],
                "authMode": "chatgpt",
                "sourceType": "codex_auth",
                "codexCompatible": True,
            }
        ]
        settings["modelWorkspace"].update(
            {
                "activeSourceId": "account:live-account",
                "defaultModelKey": "account:live-account::gpt-removed",
                "selectAll": False,
                "selectedModels": [
                    "account:live-account::gpt-live",
                    "account:live-account::gpt-removed",
                ],
            }
        )
        settings["subagentRouting"]["routes"]["simple"] = {
            "models": [
                "account:live-account::gpt-removed",
                "account:live-account::gpt-live",
            ],
            "efforts": ["low", "medium"],
        }
        core.ensure_state()
        core.save_settings(settings)
        before = core.settings_reference_integrity(core.load_settings())
        self.assertEqual(
            {item["code"] for item in before["issues"]},
            {"stale_default_model", "stale_selected_model", "stale_subagent_model"},
        )
        result = core.repair_settings_references()
        repaired = core.load_settings()
        self.assertTrue(result["after"]["healthy"])
        self.assertEqual(repaired["modelWorkspace"]["defaultModelKey"], "")
        self.assertEqual(
            repaired["modelWorkspace"]["selectedModels"],
            ["account:live-account::gpt-live"],
        )
        self.assertEqual(
            repaired["subagentRouting"]["routes"]["simple"],
            {
                "models": ["account:live-account::gpt-live"],
                "efforts": ["medium"],
            },
        )

    def test_selecting_same_source_preserves_its_valid_default_model(self):
        settings = core._initial_settings()
        settings["modelWorkspace"]["defaultModelKey"] = "account:a::gpt-second"
        source = {
            "id": "account:a",
            "models": [
                {"id": "gpt-first", "key": "account:a::gpt-first"},
                {"id": "gpt-second", "key": "account:a::gpt-second"},
            ],
        }
        workspace = core._select_workspace_source(settings, source, independent=True)
        self.assertEqual(workspace["defaultModelKey"], "account:a::gpt-second")

    def test_session_storage_health_detects_invalid_rollout_without_writing(self):
        sessions = self.root / "sessions" / "2026" / "08"
        sessions.mkdir(parents=True)
        valid = sessions / "valid.jsonl"
        invalid = sessions / "invalid.jsonl"
        valid.write_text('{"type":"session_meta","payload":{"id":"ok"}}\n', encoding="utf-8")
        invalid.write_text('{"type":"response_item"}\n', encoding="utf-8")
        before = {path: path.read_bytes() for path in (valid, invalid)}

        result = core.session_storage_health()

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["rolloutCount"], 2)
        self.assertEqual(result["invalidRolloutCount"], 1)
        self.assertEqual(before, {path: path.read_bytes() for path in (valid, invalid)})

    def test_stale_subagent_health_never_inspects_live_codex(self):
        settings = core._initial_settings()
        settings["accounts"] = [
            {
                "id": "agent-account",
                "label": "Agent",
                "groupId": "official",
                "models": ["gpt-agent"],
                "authMode": "chatgpt",
                "sourceType": "codex_auth",
                "codexCompatible": True,
            }
        ]
        settings["subagentRouting"]["routes"]["simple"] = {
            "models": ["account:agent-account::gpt-agent"],
            "efforts": ["low"],
        }
        with (
            patch.object(core, "running_codex_processes", return_value=[{"pid": "7"}]),
            patch.object(core, "codex_app_server_request") as request,
        ):
            health = core.stale_subagent_health(settings)
        self.assertTrue(health["deferred"])
        self.assertFalse(health["recoverable"])
        request.assert_not_called()

    def test_stale_subagent_cleanup_archives_only_revalidated_orphans(self):
        settings = core._initial_settings()
        settings["accounts"] = [
            {
                "id": "agent-account",
                "label": "Agent",
                "groupId": "official",
                "models": ["gpt-agent"],
                "authMode": "chatgpt",
                "sourceType": "codex_auth",
                "codexCompatible": True,
            }
        ]
        settings["subagentRouting"]["routes"]["simple"] = {
            "models": ["account:agent-account::gpt-agent"],
            "efforts": ["low"],
        }
        old_epoch = time.time() - core.STUCK_SUBAGENT_MIN_AGE_SECONDS - 60
        listed = {
            "data": [
                {
                    "id": "child-stale",
                    "name": "stale",
                    "source": {"type": "subAgent"},
                    "updatedAt": old_epoch,
                    "status": {"type": "notLoaded"},
                },
                {
                    "id": "child-complete",
                    "name": "complete",
                    "source": {"type": "subAgent"},
                    "updatedAt": old_epoch,
                    "status": {"type": "idle"},
                },
            ]
        }
        reads = [
            {"thread": {"turns": [{"id": "turn-1", "status": "inProgress"}]}},
            {"thread": {"turns": [{"id": "turn-2", "status": "completed"}]}},
        ]
        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "running_codex_processes", return_value=[]),
            patch.object(core, "codex_app_server_request", return_value=listed),
            patch.object(core, "codex_app_server_requests", return_value=reads),
            patch.object(core, "manage_codex_threads", return_value={"changed": 1}) as manage,
        ):
            result = core.cleanup_stale_subagents()
        self.assertEqual(result["archived"], ["child-stale"])
        manage.assert_called_once_with("archive", ["child-stale"])

    def test_stale_subagent_health_detects_current_string_system_error_status(self):
        old_epoch = time.time() - core.STUCK_SUBAGENT_MIN_AGE_SECONDS - 60
        listed = {
            "data": [
                {
                    "id": "child-system-error",
                    "name": "failed child",
                    "source": {"type": "subAgent"},
                    "updatedAt": old_epoch,
                    "status": "systemError",
                }
            ]
        }
        reads = [
            {
                "thread": {
                    "status": "systemError",
                    "turns": [{"id": "turn-error", "status": "failed"}],
                }
            }
        ]
        with (
            patch.object(core, "running_codex_processes", return_value=[]),
            patch.object(core, "codex_app_server_request", return_value=listed),
            patch.object(core, "codex_app_server_requests", return_value=reads),
        ):
            health = core.stale_subagent_health(core._initial_settings())
        self.assertTrue(health["recoverable"])
        self.assertEqual(health["candidates"][0]["reason"], "system_error")

    def test_stale_subagent_cleanup_batches_archives_and_rolls_back_completed_batches(self):
        candidates = [{"threadId": f"child-{index:03d}"} for index in range(150)]
        calls = []

        def manage(action, thread_ids):
            calls.append((action, list(thread_ids)))
            if action == "archive" and thread_ids[0] == "child-100":
                raise core.ManagerError("simulated batch failure")
            return {"changed": len(thread_ids)}

        with (
            patch.object(core, "running_codex_processes", return_value=[]),
            patch.object(
                core,
                "stale_subagent_health",
                return_value={"candidates": candidates},
            ),
            patch.object(core, "manage_codex_threads", side_effect=manage),
        ):
            with self.assertRaisesRegex(core.ManagerError, "已恢复已归档任务"):
                core.cleanup_stale_subagents()
        self.assertEqual(calls[0], ("archive", [f"child-{index:03d}" for index in range(100)]))
        self.assertEqual(calls[1], ("archive", [f"child-{index:03d}" for index in range(100, 150)]))
        self.assertEqual(calls[2], ("restore", [f"child-{index:03d}" for index in range(100)]))

    def test_stale_subagent_cleanup_handles_unrecognized_legacy_agent_identity_when_codex_is_closed(self):
        settings = core._initial_settings()
        settings["accounts"] = [
            {
                "id": "agent-account",
                "label": "Agent",
                "groupId": "official",
                "models": ["gpt-agent"],
                "authMode": "chatgpt",
                "sourceType": "codex_auth",
                "codexCompatible": True,
            }
        ]
        settings["subagentRouting"]["routes"]["simple"] = {
            "models": ["account:agent-account::gpt-agent"],
            "efforts": ["low"],
        }
        old_epoch = time.time() - core.STUCK_SUBAGENT_MIN_AGE_SECONDS - 60
        with (
            patch.object(core, "running_codex_processes", return_value=[]),
            patch.object(
                core,
                "codex_app_server_request",
                return_value={
                    "data": [
                        {
                            "id": "legacy-child",
                            "name": "legacy",
                            "source": {"type": "subAgent"},
                            "updatedAt": old_epoch,
                        }
                    ]
                },
            ),
            patch.object(
                core,
                "codex_app_server_requests",
                return_value=[
                    {
                        "thread": {
                            "agentRole": "unrecognized-legacy-name",
                            "turns": [{"id": "turn-legacy", "status": "inProgress"}],
                        }
                    }
                ],
            ),
        ):
            health = core.stale_subagent_health(settings)
        self.assertEqual(
            [item["threadId"] for item in health["candidates"]],
            ["legacy-child"],
        )

    def test_stale_subagent_cleanup_still_finds_orphans_after_routing_is_disabled(self):
        settings = core._initial_settings()
        settings["subagentRouting"]["strategyId"] = "verification_first"
        old_epoch = time.time() - core.STUCK_SUBAGENT_MIN_AGE_SECONDS - 60
        listed = {
            "data": [
                {
                    "id": "disabled-route-child",
                    "source": {"type": "subAgent"},
                    "updatedAt": old_epoch,
                }
            ]
        }
        with (
            patch.object(core, "running_codex_processes", return_value=[]),
            patch.object(core, "codex_app_server_request", return_value=listed),
            patch.object(
                core,
                "codex_app_server_requests",
                return_value=[
                    {
                        "thread": {
                            "turns": [{"id": "turn-disabled", "status": "inProgress"}]
                        }
                    }
                ],
            ),
        ):
            health = core.stale_subagent_health(settings)
        self.assertEqual(
            [item["threadId"] for item in health["candidates"]],
            ["disabled-route-child"],
        )


if __name__ == "__main__":
    unittest.main()
