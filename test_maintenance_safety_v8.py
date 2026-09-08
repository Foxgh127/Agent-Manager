import os
from pathlib import Path
import subprocess
import tempfile
import time
import tomllib
import unittest
from unittest.mock import patch

import agent_manager_core as core
import codex_maintenance_service as maintenance


class MaintenanceSafetyV8Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / ".codex"
        self.state = self.root / "agent-manager"
        self.root.mkdir(parents=True)
        replacements = {
            "CODEX_HOME": self.root,
            "CONFIG_FILE": self.root / "config.toml",
            "AGENTS_FILE": self.root / "AGENTS.md",
            "AGENTS_DIR": self.root / "agents",
            "MODEL_CATALOG_FILE": self.state / "model-catalog.json",
            "STATE_DIR": self.state,
            "SETTINGS_FILE": self.state / "settings.json",
            "SECRETS_FILE": self.state / "secrets.json",
            "RUNTIME_OVERLAY_FILE": self.state / "runtime-overlay.json",
            "RUNTIME_RESTORE_STATUS_FILE": self.state / "runtime-restore.json",
        }
        maintenance_replacements = {
            "UPDATE_CACHE_FILE": self.state / "update-cache.json",
            "SKILLS_CACHE_FILE": self.state / "skills-cache.json",
            "SKILL_CATALOG_CACHE_FILE": self.state / "skill-catalog.json",
            "DIAGNOSTICS_CACHE_FILE": self.state / "diagnostics.json",
            "SKILL_TRASH_DIR": self.state / "skills-trash",
        }
        self.patchers = [patch.object(core, name, value) for name, value in replacements.items()]
        self.patchers.extend(
            patch.object(maintenance, name, value)
            for name, value in maintenance_replacements.items()
        )
        self.patchers.extend(
            [
                patch.object(core, "dpapi_protect", side_effect=lambda value: b"enc:" + value.encode()),
                patch.object(
                    core,
                    "dpapi_unprotect",
                    side_effect=lambda value: value.removeprefix(b"enc:").decode(),
                ),
            ]
        )
        for item in self.patchers:
            item.start()
            self.addCleanup(item.stop)
        core.ensure_state()
        core.CONFIG_FILE.write_text('model = "gpt-test"\n', encoding="utf-8")
        with maintenance._SKILLS_LOCK:
            maintenance._VERIFIED_PLUGIN_INSTALLS.clear()

    def create_skill(self, name="demo"):
        directory = self.root / "skills" / name
        directory.mkdir(parents=True)
        skill = directory / "SKILL.md"
        skill.write_text(
            f"---\nname: {name}\ndescription: Test {name}\n---\n\n# {name}\n",
            encoding="utf-8",
        )
        return skill

    def skill_record(self, name="demo"):
        skill = self.create_skill(name)
        with patch.object(core, "codex_app_server_request", side_effect=core.ManagerError("offline")):
            record = maintenance.list_skills(cwd=self.root, force=True)["skills"][0]
        return skill, record

    def test_bad_existing_skill_config_shapes_are_never_overwritten(self):
        _skill, record = self.skill_record()
        cases = (
            'model = "gpt-test"\nskills = "opaque"\n',
            'model = "gpt-test"\n[skills]\nconfig = "opaque"\n',
            'model = "gpt-test"\n[skills]\nconfig = ["opaque"]\n',
        )
        for content in cases:
            with self.subTest(content=content):
                core.CONFIG_FILE.write_text(content, encoding="utf-8")
                before = core.CONFIG_FILE.read_bytes()
                with patch.object(core, "codex_app_server_request") as app_server:
                    with self.assertRaisesRegex(core.ManagerError, "未覆盖|未修改"):
                        maintenance.set_skill_enabled(record["id"], False, cwd=self.root)
                app_server.assert_not_called()
                self.assertEqual(core.CONFIG_FILE.read_bytes(), before)

    def test_skill_toggle_rebases_active_runtime_overlay(self):
        skill, record = self.skill_record("overlay")
        original = core.CONFIG_FILE.read_bytes()
        core.atomic_write_json(
            core.RUNTIME_OVERLAY_FILE,
            {
                "schemaVersion": 1,
                "codexHome": str(core.CODEX_HOME),
                "ownerPid": os.getpid(),
                "environment": {},
                "files": {
                    "config.toml": {
                        "kind": "config",
                        "baseline": core._overlay_encrypt_bytes(original),
                        "baselineHash": core._overlay_value_hash(original),
                        "capturedHash": core._overlay_value_hash(original),
                        "appliedHash": core._overlay_value_hash(original),
                    }
                },
            },
        )
        with patch.object(core, "codex_app_server_request", side_effect=core.ManagerError("offline")):
            maintenance.set_skill_enabled(record["id"], False, cwd=self.root)
        overlay = core._runtime_overlay_read()
        baseline = core._overlay_decrypt_bytes(overlay["files"]["config.toml"]["baseline"])
        parsed = tomllib.loads(baseline.decode("utf-8"))
        self.assertEqual(parsed["skills"]["config"][0]["path"], str(skill.resolve()))
        self.assertFalse(parsed["skills"]["config"][0]["enabled"])

    def test_quarantine_is_reverified_and_restored_if_content_changes_during_move(self):
        skill, record = self.skill_record("raced")
        original_replace = maintenance.os.replace
        moved = False

        def mutate_after_move(source, destination):
            nonlocal moved
            result = original_replace(source, destination)
            if Path(source) == skill.parent and not moved:
                moved = True
                (Path(destination) / "SKILL.md").write_text("changed in quarantine", encoding="utf-8")
            return result

        with patch.object(maintenance.os, "replace", side_effect=mutate_after_move):
            with self.assertRaisesRegex(core.ManagerError, "隔离后校验失败"):
                maintenance.delete_skill(
                    record["id"],
                    record["fingerprint"],
                    cwd=self.root,
                )
        self.assertTrue(skill.is_file())
        self.assertFalse(any(maintenance.SKILL_TRASH_DIR.glob("*")))

    def test_asset_fingerprint_detects_same_size_content_change_with_restored_mtime(self):
        skill = self.create_skill("asset-race")
        assets = skill.parent / "assets"
        assets.mkdir()
        asset = assets / "template.bin"
        asset.write_bytes(b"AAAA")
        first = maintenance._skill_fingerprint(skill)
        old_mtime = asset.stat().st_mtime_ns
        time.sleep(0.01)
        asset.write_bytes(b"BBBB")
        os.utime(asset, ns=(old_mtime, old_mtime))
        second = maintenance._skill_fingerprint(skill)
        self.assertNotEqual(first, second)

    def test_completed_toggle_survives_nonessential_cache_write_failure(self):
        _skill, record = self.skill_record("cache")
        with (
            patch.object(core, "codex_app_server_request", side_effect=core.ManagerError("offline")),
            patch.object(maintenance, "_update_cached_skill", side_effect=OSError("disk full")),
        ):
            result = maintenance.set_skill_enabled(record["id"], False, cwd=self.root)
        self.assertFalse(result["skill"]["enabled"])
        self.assertTrue(result["cacheWarning"])
        parsed = tomllib.loads(core.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertFalse(parsed["skills"]["config"][0]["enabled"])

    def test_tampered_catalog_cache_cannot_choose_install_source(self):
        plugin_id = "safe@openai-curated"
        core.atomic_write_json(
            maintenance.SKILL_CATALOG_CACHE_FILE,
            {
                "items": [
                    {
                        "id": plugin_id,
                        "pluginName": "evil-local-plugin",
                        "marketplacePath": str(self.root / "attacker-marketplace"),
                        "remoteMarketplaceName": None,
                        "installable": True,
                    }
                ]
            },
        )
        live_catalog = {
            "marketplaces": [
                {
                    "name": "openai-curated",
                    "path": None,
                    "plugins": [
                        {
                            "id": plugin_id,
                            "name": "safe",
                            "installPolicy": "AVAILABLE",
                            "availability": "AVAILABLE",
                            "interface": {},
                        }
                    ],
                }
            ],
            "marketplaceLoadErrors": [],
        }
        with (
            patch.object(
                core,
                "codex_app_server_request",
                side_effect=[live_catalog, {"authPolicy": "ON_USE", "appsNeedingAuth": []}],
            ) as request,
            patch.object(maintenance, "_official_catalog_records", return_value=("openai-curated", [])),
        ):
            result = maintenance.install_public_plugin(plugin_id)
        self.assertTrue(result["installed"])
        method, params = request.call_args_list[-1].args[:2]
        self.assertEqual(method, "plugin/install")
        self.assertEqual(params["pluginName"], "safe")
        self.assertEqual(params["remoteMarketplaceName"], "openai-curated")
        self.assertNotIn("marketplacePath", params)

    def test_plugin_install_requires_structured_app_server_confirmation(self):
        plugin_id = "safe@openai-curated"
        live_catalog = {
            "marketplaces": [
                {
                    "name": "openai-curated",
                    "plugins": [
                        {
                            "id": plugin_id,
                            "name": "safe",
                            "installPolicy": "AVAILABLE",
                            "availability": "AVAILABLE",
                            "interface": {},
                        }
                    ],
                }
            ],
            "marketplaceLoadErrors": [],
        }
        with (
            patch.object(core, "codex_app_server_request", side_effect=[live_catalog, None]),
            patch.object(maintenance, "_official_catalog_records", return_value=("openai-curated", [])),
            self.assertRaisesRegex(core.ManagerError, "未确认"),
        ):
            maintenance.install_public_plugin(plugin_id, force_refresh=True)

    def test_cli_update_never_uses_stale_check_and_pins_official_registry(self):
        stale = {
            "lastError": "registry unavailable",
            "stale": True,
            "components": {
                "cli": {
                    "updateAvailable": True,
                    "canAutoUpdate": True,
                    "availableVersion": "0.200.0",
                    "installedVersion": "0.199.0",
                }
            },
        }
        with (
            patch.object(maintenance, "update_center_status", return_value=stale),
            patch.object(maintenance.subprocess, "run") as run,
            self.assertRaisesRegex(core.ManagerError, "旧缓存"),
        ):
            maintenance.update_cli()
        run.assert_not_called()

        before = {**stale, "lastError": None, "stale": False}
        after = {
            "lastError": None,
            "stale": False,
            "components": {
                "cli": {
                    "installedVersion": "0.199.0",
                    "updateAvailable": True,
                }
            },
        }
        completed = subprocess.CompletedProcess([], 0, "ok", "")
        with (
            patch.object(maintenance, "update_center_status", side_effect=[before, after]),
            patch.object(
                maintenance,
                "_npm_installation",
                return_value={"installed": True, "version": "0.199.0", "npmCommand": ["npm"]},
            ),
            patch.object(maintenance.subprocess, "run", return_value=completed) as run,
            patch.object(core, "invalidate_codex_version_cache"),
            self.assertRaisesRegex(core.ManagerError, "未能确认"),
        ):
            maintenance.update_cli()
        command = run.call_args.args[0]
        self.assertIn("--registry=https://registry.npmjs.org", command)
        self.assertIn("@openai/codex@0.200.0", command)

    def test_combined_repairs_do_not_send_backup_placeholders_to_generator(self):
        warning_checks = [
            {
                "id": "generated_configuration",
                "status": "warning",
                "autoFixable": True,
                "action": "reapply_configuration",
                "data": {},
            },
            {
                "id": "settings_references",
                "status": "error",
                "autoFixable": True,
                "action": "repair_settings_references",
                "data": {},
            },
        ]
        before = {"checks": warning_checks, "healthy": False}
        after = {
            "checks": [
                {**item, "status": "ok", "autoFixable": False}
                for item in warning_checks
            ],
            "healthy": True,
        }
        generated = {core.CONFIG_FILE: 'model = "repaired"\n'}
        with (
            patch.object(maintenance, "run_emergency_checks", side_effect=[before, before, after]),
            patch.object(maintenance, "_generated_configuration_files", return_value=generated),
            patch.object(maintenance, "_capture_repair_backup", return_value=[]),
            patch.object(
                maintenance,
                "_repair_generated_configuration",
                return_value={"changed": True, "files": [str(core.CONFIG_FILE)]},
            ) as repair_generated,
            patch.object(core, "repair_settings_references", return_value={"changed": True, "removed": 1}),
            patch.object(core, "_runtime_overlay_record_applied"),
        ):
            result = maintenance.apply_emergency_repairs(
                ["generated_configuration", "settings_references"]
            )
        self.assertFalse(result["rolledBack"])
        repair_generated.assert_called_once_with(generated)

    def test_rollback_verifies_actual_bytes_and_boundary_errors_are_managed(self):
        target = self.state / "repair-target.json"
        target.write_bytes(b"mutated")
        snapshot = [{"path": target, "existed": True, "content": b"original", "backupPath": None}]

        def silently_write_wrong(path, _content):
            Path(path).write_bytes(b"wrong")

        with patch.object(core, "atomic_write_bytes", side_effect=silently_write_wrong):
            errors = maintenance._restore_repair_backup(snapshot)
        self.assertTrue(errors)
        self.assertIn("校验失败", errors[0])
        for value in (None, "runtime_overlay", [object()]):
            with self.subTest(value=value), self.assertRaises(core.ManagerError):
                maintenance.apply_emergency_repairs(value)
        with self.assertRaises(core.ManagerError):
            maintenance._validate_remote_metadata_url(
                "https://raw.githubusercontent.com:invalid/openai/plugins/main/catalog.json"
            )

    def test_runtime_overlay_environment_is_captured_and_verified_on_rollback(self):
        values = {"SAFE_REPAIR_KEY": "before"}

        def read_value(name):
            return values.get(name)

        def write_value(name, value):
            values[name] = value

        def remove_value(name):
            values.pop(name, None)

        with (
            patch.object(
                core,
                "_runtime_overlay_read",
                return_value={"environment": {"SAFE_REPAIR_KEY": {}}},
            ),
            patch.object(core, "_read_user_environment", side_effect=read_value),
            patch.object(core, "_sync_user_environment", side_effect=write_value),
            patch.object(core, "_remove_user_environment", side_effect=remove_value),
        ):
            snapshot = maintenance._capture_repair_environment(True)
            values["SAFE_REPAIR_KEY"] = "changed"
            errors = maintenance._restore_repair_environment(snapshot)
        self.assertEqual(errors, [])
        self.assertEqual(values["SAFE_REPAIR_KEY"], "before")


if __name__ == "__main__":
    unittest.main()
