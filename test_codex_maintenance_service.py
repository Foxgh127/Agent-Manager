from pathlib import Path
import os
import tempfile
import time
import tomllib
import unittest
from unittest.mock import patch

import agent_manager_core as core
import codex_maintenance_service as maintenance


class CodexMaintenanceServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / ".codex"
        self.root.mkdir(parents=True)
        self.patchers = [
            patch.object(core, "CODEX_HOME", self.root),
            patch.object(core, "CONFIG_FILE", self.root / "config.toml"),
            patch.object(core, "STATE_DIR", self.root / "agent-manager"),
            patch.object(core, "RUNTIME_OVERLAY_FILE", self.root / "agent-manager" / "runtime-configuration-overlay.json"),
            patch.object(maintenance, "UPDATE_CACHE_FILE", self.root / "agent-manager" / "update-cache.json"),
            patch.object(maintenance, "SKILLS_CACHE_FILE", self.root / "agent-manager" / "skills-cache.json"),
            patch.object(maintenance, "SKILL_CATALOG_CACHE_FILE", self.root / "agent-manager" / "skill-catalog.json"),
            patch.object(maintenance, "DIAGNOSTICS_CACHE_FILE", self.root / "agent-manager" / "diagnostics-cache.json"),
            patch.object(maintenance, "SKILL_TRASH_DIR", self.root / "agent-manager" / "skills-trash"),
        ]
        for item in self.patchers:
            item.start()
        core.STATE_DIR.mkdir(parents=True)
        core.CONFIG_FILE.write_text('model = "gpt-test"\n', encoding="utf-8")

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    def create_skill(self, name="demo"):
        directory = self.root / "skills" / name
        directory.mkdir(parents=True)
        skill = directory / "SKILL.md"
        skill.write_text(
            f'---\nname: {name}\ndescription: Test {name}\n---\n\n# {name}\n',
            encoding="utf-8",
        )
        return skill

    def test_live_manager_overlay_is_not_reported_as_repairable_orphan(self):
        core.atomic_write_json(core.RUNTIME_OVERLAY_FILE, {})
        payload = {
            "version": 1,
            "ownerPid": 4321,
            "files": {"config.toml": {}},
            "environment": {},
        }
        with (
            patch.object(core, "_runtime_overlay_read", return_value=payload),
            patch.object(maintenance, "_runtime_owner_is_live_manager", return_value=True),
        ):
            health = maintenance._runtime_overlay_health()
        self.assertEqual(health["status"], "ok")
        self.assertFalse(health["recoverable"])
        self.assertIn("无需修复", health["detail"])

    def test_lists_toggles_and_quarantines_mutable_skill(self):
        skill = self.create_skill()
        with patch.object(core, "codex_app_server_request", side_effect=core.ManagerError("offline")):
            inventory = maintenance.list_skills(cwd=self.root, force=True)
            self.assertEqual(inventory["count"], 1)
            record = inventory["skills"][0]
            self.assertTrue(record["mutable"])
            toggled = maintenance.set_skill_enabled(record["id"], False, cwd=self.root)
            self.assertFalse(toggled["skill"]["enabled"])
            parsed = tomllib.loads(core.CONFIG_FILE.read_text(encoding="utf-8"))
            self.assertEqual(parsed["skills"]["config"][0]["path"], str(skill.resolve()))
            self.assertFalse(parsed["skills"]["config"][0]["enabled"])
            deleted = maintenance.delete_skill(record["id"], record["fingerprint"], cwd=self.root)
        self.assertTrue(deleted["recoverable"])
        self.assertFalse(skill.parent.exists())
        self.assertTrue(Path(deleted["quarantinePath"]).is_dir())
        parsed = tomllib.loads(core.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertFalse(parsed.get("skills", {}).get("config"))

    def test_delete_rejects_changed_skill(self):
        skill = self.create_skill()
        with patch.object(core, "codex_app_server_request", side_effect=core.ManagerError("offline")):
            record = maintenance.list_skills(cwd=self.root, force=True)["skills"][0]
            skill.write_text(skill.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
            with self.assertRaisesRegex(core.ManagerError, "发生了变化"):
                maintenance.delete_skill(record["id"], record["fingerprint"], cwd=self.root)

    def test_delete_rechecks_cached_fingerprint_inside_mutation_lock(self):
        skill = self.create_skill()
        with patch.object(core, "codex_app_server_request", side_effect=core.ManagerError("offline")):
            record = maintenance.list_skills(cwd=self.root, force=True)["skills"][0]
            skill.write_text(skill.read_text(encoding="utf-8") + "changed after preview\n", encoding="utf-8")
            # _find_skill may return the cached preview record.  Deletion must
            # still fingerprint the live directory immediately before moving it.
            with patch.object(maintenance, "_find_skill", return_value=record):
                with self.assertRaisesRegex(core.ManagerError, "发生了变化"):
                    maintenance.delete_skill(record["id"], record["fingerprint"], cwd=self.root)
        self.assertTrue(skill.is_file())

    def test_delete_rejects_nested_symlink_before_quarantine(self):
        skill = self.create_skill()
        link = skill.parent / "linked"
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("当前 Windows 策略不允许创建测试符号链接")
        with patch.object(core, "codex_app_server_request", side_effect=core.ManagerError("offline")):
            record = maintenance.list_skills(cwd=self.root, force=True)["skills"][0]
            with self.assertRaisesRegex(core.ManagerError, "符号链接"):
                maintenance.delete_skill(record["id"], record["fingerprint"], cwd=self.root)
        self.assertTrue(skill.is_file())

    def test_inventory_bounds_abnormal_skill_tree_without_freezing_other_skills(self):
        abnormal = self.create_skill("large")
        normal = self.create_skill("normal")
        (abnormal.parent / "extra.txt").write_text("extra", encoding="utf-8")
        real_bounded_files = maintenance._bounded_skill_files

        def bounds_only_large(directory):
            if Path(directory).name == "large":
                with patch.object(maintenance, "MAX_SKILL_TREE_FILES", 1):
                    return real_bounded_files(directory)
            return real_bounded_files(directory)

        with (
            patch.object(core, "codex_app_server_request", side_effect=core.ManagerError("offline")),
            patch.object(maintenance, "_bounded_skill_files", side_effect=bounds_only_large),
        ):
            inventory = maintenance.list_skills(cwd=self.root, force=True)
        by_name = {item["name"]: item for item in inventory["skills"]}
        self.assertEqual({"large", "normal"}, set(by_name))
        self.assertFalse(by_name["large"]["mutable"])
        self.assertTrue(by_name["normal"]["mutable"])
        self.assertTrue(any("规模异常" in error for error in by_name["large"]["errors"]))
        self.assertTrue(abnormal.is_file())
        self.assertTrue(normal.is_file())

        # A bounded safety scan is allowed to withhold automatic deletion, but
        # enable/disable only writes the SKILL.md path and must remain fast and
        # available.  In particular it must not recursively rescan the tree.
        with (
            patch.object(core, "codex_app_server_request", side_effect=core.ManagerError("offline")),
            patch.object(maintenance, "_bounded_skill_files", side_effect=AssertionError("unexpected rescan")),
        ):
            toggled = maintenance.set_skill_enabled(by_name["large"]["id"], False, cwd=self.root)
        self.assertFalse(toggled["skill"]["enabled"])

    def test_asset_heavy_skill_budget_covers_large_template_bundles(self):
        # Regression for paper-framework-figure-studio-pro (4,163 files,
        # predominantly assets).  Entry and byte ceilings still bound scans.
        self.assertGreaterEqual(maintenance.MAX_SKILL_TREE_FILES, 4_163)
        self.assertLessEqual(maintenance.MAX_SKILL_TREE_FILES, maintenance.MAX_SKILL_TREE_ENTRIES)

    def test_skill_fingerprint_uses_metadata_instead_of_reading_asset_payloads(self):
        skill = self.create_skill("asset-fingerprint")
        assets = skill.parent / "assets"
        assets.mkdir()
        asset = assets / "template.bin"
        asset.write_bytes(b"asset payload")
        original_read_bytes = Path.read_bytes
        reads = []

        def traced_read_bytes(path):
            reads.append(Path(path))
            return original_read_bytes(path)

        with patch.object(Path, "read_bytes", traced_read_bytes):
            fingerprint = maintenance._skill_fingerprint(skill)
        self.assertTrue(fingerprint)
        self.assertIn(skill, reads)
        self.assertNotIn(asset, reads)

    def test_delete_reports_quarantine_path_when_config_and_restore_both_fail(self):
        skill = self.create_skill("rollback")
        with patch.object(core, "codex_app_server_request", side_effect=core.ManagerError("offline")):
            record = maintenance.list_skills(cwd=self.root, force=True)["skills"][0]
        original_replace = maintenance.os.replace

        def fail_restore(source, destination):
            if Path(source).parent == maintenance.SKILL_TRASH_DIR:
                raise OSError("restore blocked")
            return original_replace(source, destination)

        with (
            patch.object(maintenance, "_write_skill_config", side_effect=core.ManagerError("config blocked")),
            patch.object(maintenance.os, "replace", side_effect=fail_restore),
        ):
            with self.assertRaisesRegex(core.ManagerError, "可恢复位置") as failure:
                maintenance.delete_skill(record["id"], record["fingerprint"], cwd=self.root)
        self.assertIn(str(maintenance.SKILL_TRASH_DIR), str(failure.exception))
        self.assertFalse(skill.parent.exists())

    def test_catalog_uses_verified_app_server_install_identity(self):
        plugin_list = {
            "marketplaces": [
                {
                    "name": "openai-curated",
                    "path": None,
                    "interface": {"displayName": "ChatGPT Official"},
                    "plugins": [
                        {
                            "id": "calendar@openai-curated",
                            "name": "calendar",
                            "installed": False,
                            "enabled": False,
                            "installPolicy": "AVAILABLE",
                            "availability": "AVAILABLE",
                            "authPolicy": "ON_USE",
                            "interface": {"displayName": "Calendar", "category": "Productivity"},
                        }
                    ],
                }
            ],
            "marketplaceLoadErrors": [],
            "featuredPluginIds": ["calendar@openai-curated"],
        }
        with (
            patch.object(
                core,
                "codex_app_server_request",
                side_effect=[plugin_list, {"authPolicy": "ON_USE", "appsNeedingAuth": []}],
            ) as request,
            patch.object(maintenance, "_official_catalog_records", return_value=("openai-curated", [])) as public_catalog,
        ):
            catalog = maintenance.public_skill_catalog(force=True)
            result = maintenance.install_public_plugin("calendar@openai-curated")
        self.assertEqual(catalog["count"], 1)
        public_catalog.assert_called_once()
        self.assertTrue(catalog["items"][0]["installable"])
        self.assertTrue(result["installed"])
        method, params = request.call_args_list[-1].args[:2]
        self.assertEqual(method, "plugin/install")
        self.assertEqual(params["remoteMarketplaceName"], "openai-curated")
        self.assertEqual(params["pluginName"], "calendar")

    def test_catalog_supplements_short_app_server_with_public_directory_without_duplicates(self):
        plugin_list = {
            "marketplaces": [
                {
                    "name": "openai-api-curated",
                    "plugins": [
                        {
                            "id": "calendar@openai-api-curated",
                            "name": "calendar",
                            "installPolicy": "AVAILABLE",
                            "availability": "AVAILABLE",
                            "interface": {"displayName": "Calendar", "category": "Productivity"},
                        }
                    ],
                }
            ],
            "marketplaceLoadErrors": [],
            "featuredPluginIds": [],
        }
        public_items = [
            {"id": "calendar", "name": "calendar", "pluginName": "calendar", "category": "Productivity", "official": True, "installable": False},
            {"id": "linear", "name": "linear", "pluginName": "linear", "category": "Productivity", "official": True, "installable": False},
        ]
        with (
            patch.object(core, "codex_app_server_request", return_value=plugin_list),
            patch.object(maintenance, "_official_catalog_records", return_value=("openai-curated", public_items)),
        ):
            catalog = maintenance.public_skill_catalog(force=True)
        self.assertEqual(catalog["count"], 2)
        self.assertEqual([item["pluginName"] for item in catalog["items"]], ["calendar", "linear"])
        self.assertTrue(catalog["items"][0]["installable"])
        self.assertFalse(catalog["items"][1]["installable"])
        self.assertTrue(catalog["items"][0]["official"])

    def test_update_status_reuses_six_hour_cache(self):
        runtime = {"available": True, "source": "desktop", "desktop": {"version": "26.1.0"}, "command": ["codex.exe"]}
        npm = {"installed": True, "version": "0.146.0", "npmCommand": ["npm"], "packageRoot": "x"}
        with (
            patch.object(core, "codex_runtime_status", return_value=runtime) as runtime_status,
            patch.object(maintenance, "_npm_installation", return_value=npm),
            patch.object(core, "_registry_json", return_value={"version": "0.147.0"}) as registry,
        ):
            first = maintenance.update_center_status(force=True)
            second = maintenance.update_center_status(force=False)
        self.assertTrue(first["components"]["cli"]["updateAvailable"])
        self.assertTrue(second["cached"])
        self.assertEqual(runtime_status.call_count, 1)
        self.assertEqual(registry.call_count, 1)

    def test_remote_metadata_rejects_downgrade_private_and_unknown_redirects(self):
        handler = maintenance._SafeMetadataRedirectHandler()
        for target in (
            "http://raw.githubusercontent.com/openai/plugins/main/catalog.json",
            "https://127.0.0.1/catalog.json",
            "https://attacker.example/catalog.json",
        ):
            with self.subTest(target=target):
                with self.assertRaises(core.ManagerError):
                    handler.redirect_request(None, None, 302, "redirect", {}, target)

    def test_non_forced_loads_do_not_scan_or_use_network(self):
        with (
            patch.object(core, "codex_app_server_request") as app_server,
            patch.object(core, "codex_runtime_status") as runtime_status,
            patch.object(core, "_registry_json") as registry,
            patch.object(maintenance, "_https_json") as http_json,
        ):
            skills = maintenance.list_skills(cwd=self.root, force=False)
            catalog = maintenance.public_skill_catalog(force=False)
            updates = maintenance.update_center_status(force=False)
            diagnostics = maintenance.run_emergency_checks(force=False)
        self.assertTrue(skills["needsManualRefresh"])
        self.assertTrue(catalog["needsManualRefresh"])
        self.assertTrue(updates["needsManualCheck"])
        self.assertTrue(diagnostics["needsManualCheck"])
        app_server.assert_not_called()
        runtime_status.assert_not_called()
        registry.assert_not_called()
        http_json.assert_not_called()

    def test_current_cli_is_not_reported_or_reinstalled_as_update(self):
        runtime = {
            "available": True,
            "source": "npm_javascript",
            "desktop": {"version": "26.803.1"},
            "command": ["codex.exe"],
        }
        npm = {"installed": True, "version": "0.147.0", "npmCommand": ["npm"], "packageRoot": "x"}
        with (
            patch.object(core, "codex_runtime_status", return_value=runtime),
            patch.object(maintenance, "_npm_installation", return_value=npm),
            patch.object(core, "_registry_json", return_value={"version": "0.147.0"}),
            patch.object(maintenance.subprocess, "run") as run,
        ):
            status = maintenance.update_center_status(force=True)
            result = maintenance.update_cli()
        self.assertFalse(status["components"]["cli"]["updateAvailable"])
        self.assertEqual(status["components"]["cli"]["updateState"], "current")
        self.assertTrue(result["noop"])
        run.assert_not_called()

    def test_cli_update_invalidates_version_cache_before_refreshing_status(self):
        before = {
            "components": {
                "cli": {
                    "updateAvailable": True,
                    "canAutoUpdate": True,
                    "availableVersion": "0.151.0",
                    "installedVersion": "0.150.1",
                }
            }
        }
        after = {
            "components": {
                "cli": {
                    "updateAvailable": False,
                    "canAutoUpdate": True,
                    "availableVersion": "0.151.0",
                    "installedVersion": "0.151.0",
                }
            }
        }
        completed = maintenance.subprocess.CompletedProcess(
            args=[], returncode=0, stdout="updated", stderr=""
        )
        with (
            patch.object(maintenance, "update_center_status", side_effect=[before, after]),
            patch.object(maintenance, "_npm_installation", return_value={"npmCommand": ["npm"]}),
            patch.object(maintenance.subprocess, "run", return_value=completed) as run,
            patch.object(core, "invalidate_codex_version_cache") as invalidate,
        ):
            result = maintenance.update_cli()

        self.assertTrue(result["updated"])
        invalidate.assert_called_once_with()
        self.assertIn("@openai/codex@0.151.0", run.call_args.args[0])

    def test_npm_inventory_probe_is_bounded_and_degrades_cleanly(self):
        runtime = {
            "node": "node.exe",
            "npm": "npm.cmd",
            "npmCommand": ["npm.cmd"],
        }
        with (
            patch.object(core, "_discover_node_npm_runtime", return_value=runtime),
            patch.object(core, "_npm_global_prefix", return_value=None),
            patch.object(
                maintenance.subprocess,
                "run",
                side_effect=maintenance.subprocess.TimeoutExpired(["npm.cmd"], 5),
            ) as run,
        ):
            result = maintenance._npm_installation()
        self.assertFalse(result["installed"])
        self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_desktop_update_uses_store_without_launching_codex(self):
        component = {"kind":"desktop", "installed":True, "installedVersion":"26.901.6511.0",
                     "updateAvailable":False, "updateState":"current", "source":"microsoft_store_cli"}
        runtime = {"available": True, "desktop": {"version":"26.901.6511.0"}}
        with (
            patch.object(core, "codex_runtime_status", return_value=runtime),
            patch.object(maintenance, "_npm_installation", return_value={"installed":False}),
            patch.object(core, "_registry_json", return_value={"version":"0.153.4"}),
            patch.object(core, "_detect_codex_windows_app", return_value=runtime["desktop"]),
            patch.object(maintenance.desktop_updates, "check", return_value=component),
            patch.object(maintenance.desktop_updates, "apply", return_value={"component":component,"updated":False}) as apply,
            patch.object(core, "launch_codex_app") as launch,
        ):
            status = maintenance.update_center_status(force=True)
            result = maintenance.open_desktop_update()
        self.assertFalse(status["components"]["desktop"]["updateAvailable"])
        self.assertFalse(result["updated"])
        apply.assert_called_once()
        launch.assert_not_called()

    def test_legacy_update_cache_is_normalized_without_network(self):
        core.atomic_write_json(
            maintenance.UPDATE_CACHE_FILE,
            {
                "schemaVersion": 1,
                "checkedAt": "2026-08-10T00:00:00+00:00",
                "components": {
                    "desktop": {
                        "installed": True,
                        "installedVersion": "26.803.1",
                        "updateState": "store_managed",
                        "source": "microsoft_store",
                    },
                    "cli": {
                        "installed": True,
                        "installedVersion": "0.147.0",
                        "availableVersion": "0.147.0",
                        "updateAvailable": True,
                    },
                },
            },
        )
        with (
            patch.object(core, "codex_runtime_status") as runtime_status,
            patch.object(core, "_registry_json") as registry,
        ):
            status = maintenance.update_center_status(force=False)
        self.assertEqual(status["components"]["desktop"]["source"], "microsoft_store")
        self.assertEqual(status["components"]["desktop"]["updateState"], "not_checked")
        self.assertFalse(status["components"]["cli"]["updateAvailable"])
        self.assertEqual(status["components"]["cli"]["updateState"], "current")
        runtime_status.assert_not_called()
        registry.assert_not_called()

    def test_emergency_configuration_check_uses_fully_applied_status(self):
        with (
            patch.object(core, "codex_runtime_status", return_value={"available": True, "source": "desktop"}),
            patch.object(core, "configuration_status", return_value={"fullyApplied": True}),
            patch.object(maintenance, "list_skills", return_value={"skills": [], "count": 0}),
        ):
            result = maintenance.run_emergency_checks(force=True)
        statuses = {item["id"]: item["status"] for item in result["checks"]}
        self.assertEqual(statuses["generated_configuration"], "ok")
        self.assertEqual(result["repairableCount"], 0)
        self.assertFalse(result["enableRepair"])

    def test_healthy_repair_request_is_a_noop(self):
        healthy = {
            "checkedAt": "2026-08-11T00:00:00+00:00",
            "checks": [{"id": "generated_configuration", "status": "ok", "autoFixable": False}],
            "summary": {"ok": 1, "warning": 0, "error": 0},
            "healthy": True,
            "repairableCount": 0,
            "enableRepair": False,
            "cached": True,
            "needsManualCheck": False,
        }
        core.atomic_write_json(maintenance.DIAGNOSTICS_CACHE_FILE, healthy)
        with patch.object(maintenance, "_repair_generated_configuration") as repair:
            result = maintenance.apply_emergency_repairs(["generated_configuration"])
        self.assertTrue(result["noop"])
        self.assertFalse(result["rechecked"])
        repair.assert_not_called()

    def test_legacy_diagnostics_cannot_reenable_unsafe_environment_repair(self):
        core.atomic_write_json(
            maintenance.DIAGNOSTICS_CACHE_FILE,
            {
                "schemaVersion": 1,
                "checkedAt": "2026-08-10T00:00:00+00:00",
                "checks": [
                    {
                        "id": "unsafe_cli_override",
                        "status": "error",
                        "autoFixable": True,
                        "action": "clear_unsafe_cli_override",
                    }
                ],
            },
        )
        diagnostics = maintenance.run_emergency_checks(force=False)
        self.assertEqual(diagnostics["repairableCount"], 0)
        self.assertFalse(diagnostics["enableRepair"])
        self.assertFalse(diagnostics["checks"][0]["autoFixable"])

    def test_failed_repair_restores_preflight_backup(self):
        original = 'model = "original"\n'
        core.CONFIG_FILE.write_text(original, encoding="utf-8")
        cached = {
            "checkedAt": "2026-08-11T00:00:00+00:00",
            "checks": [
                {
                    "id": "generated_configuration",
                    "status": "warning",
                    "autoFixable": True,
                    "data": {"fullyApplied": False},
                }
            ],
            "summary": {"ok": 0, "warning": 1, "error": 0},
            "healthy": False,
            "repairableCount": 1,
            "enableRepair": True,
            "cached": True,
            "needsManualCheck": False,
        }
        preflight = {**cached, "cached": False}
        rolled_back = {**cached, "checkedAt": "2026-08-11T00:01:00+00:00", "cached": False}

        def fail_after_write(_files=None):
            core.CONFIG_FILE.write_text('model = "broken"\n', encoding="utf-8")
            raise core.ManagerError("synthetic failure")

        with (
            patch.object(maintenance, "run_emergency_checks", side_effect=[cached, preflight, rolled_back]),
            patch.object(maintenance, "_generated_configuration_files", return_value={core.CONFIG_FILE: 'model = "fixed"\n'}),
            patch.object(maintenance, "_repair_generated_configuration", side_effect=fail_after_write),
            patch.object(core, "backup_file", return_value=None),
        ):
            result = maintenance.apply_emergency_repairs(["generated_configuration"])
        self.assertTrue(result["rolledBack"])
        self.assertEqual(core.CONFIG_FILE.read_text(encoding="utf-8"), original)

    def test_emergency_repair_rejects_concurrent_transaction_without_waiting(self):
        self.assertTrue(maintenance._REPAIR_LOCK.acquire(blocking=False))
        try:
            started = time.monotonic()
            with self.assertRaisesRegex(core.ManagerError, "正在执行"):
                maintenance.apply_emergency_repairs(["generated_configuration"])
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            maintenance._REPAIR_LOCK.release()

    def test_restore_repair_backup_continues_after_one_file_fails(self):
        first = self.root / "first.toml"
        second = self.root / "second.toml"
        first.write_text("broken-first", encoding="utf-8")
        second.write_text("broken-second", encoding="utf-8")
        snapshots = [
            {"path": first, "existed": True, "content": b"original-first"},
            {"path": second, "existed": True, "content": b"original-second"},
        ]
        original_replace = os.replace

        def selective_replace(source, destination):
            if Path(destination) == first:
                raise OSError("first restore failed")
            return original_replace(source, destination)

        with patch.object(maintenance.os, "replace", side_effect=selective_replace):
            errors = maintenance._restore_repair_backup(snapshots)

        self.assertEqual(first.read_text(encoding="utf-8"), "broken-first")
        self.assertEqual(second.read_text(encoding="utf-8"), "original-second")
        self.assertEqual(len(errors), 1)
        self.assertIn("first restore failed", errors[0])

    def test_failed_rollback_returns_structured_status_instead_of_escaping(self):
        cached = {
            "checkedAt": "2026-08-11T00:00:00+00:00",
            "checks": [
                {
                    "id": "generated_configuration",
                    "status": "warning",
                    "autoFixable": True,
                }
            ],
            "summary": {"ok": 0, "warning": 1, "error": 0},
            "healthy": False,
            "repairableCount": 1,
            "enableRepair": True,
            "cached": True,
            "needsManualCheck": False,
        }
        preflight = {**cached, "cached": False}

        with (
            patch.object(maintenance, "run_emergency_checks", side_effect=[cached, preflight, RuntimeError("recheck failed")]),
            patch.object(maintenance, "_generated_configuration_files", return_value={core.CONFIG_FILE: 'model = "fixed"\n'}),
            patch.object(maintenance, "_capture_repair_backup", return_value=[]),
            patch.object(maintenance, "_repair_generated_configuration", side_effect=core.ManagerError("repair failed")),
            patch.object(maintenance, "_restore_repair_backup", return_value=["config.toml：restore failed"]),
        ):
            result = maintenance.apply_emergency_repairs(["generated_configuration"])

        self.assertTrue(result["rollbackAttempted"])
        self.assertFalse(result["rolledBack"])
        self.assertFalse(result["rechecked"])
        self.assertEqual(result["results"][-1]["status"], "rollback_failed")
        self.assertIn("restore failed", result["rollbackErrors"][0])
        self.assertIn("recheck failed", result["rollbackErrors"][1])

    def test_generated_configuration_repair_refreshes_runtime_overlay_hashes(self):
        expected = 'model = "fixed"\n'
        cached = {
            "checkedAt": "2026-08-11T00:00:00+00:00",
            "checks": [
                {
                    "id": "generated_configuration",
                    "status": "warning",
                    "autoFixable": True,
                }
            ],
            "summary": {"ok": 0, "warning": 1, "error": 0},
            "healthy": False,
            "repairableCount": 1,
            "enableRepair": True,
            "cached": True,
            "needsManualCheck": False,
        }
        preflight = {**cached, "cached": False}
        after = {
            **preflight,
            "checks": [{"id": "generated_configuration", "status": "ok", "autoFixable": False}],
            "healthy": True,
            "repairableCount": 0,
            "enableRepair": False,
        }
        with (
            patch.object(maintenance, "run_emergency_checks", side_effect=[cached, preflight, after]),
            patch.object(maintenance, "_generated_configuration_files", return_value={core.CONFIG_FILE: expected}),
            patch.object(core, "backup_file", return_value=None),
            patch.object(core, "_runtime_overlay_record_applied") as record_applied,
        ):
            result = maintenance.apply_emergency_repairs(["generated_configuration"])

        self.assertFalse(result["rolledBack"])
        self.assertEqual(result["results"][0]["status"], "fixed")
        self.assertEqual(core.CONFIG_FILE.read_text(encoding="utf-8"), expected)
        record_applied.assert_called_once_with(paths=[core.CONFIG_FILE])

    def test_emergency_check_surfaces_cross_module_health_without_reading_secrets(self):
        healthy_refs = {
            "healthy": True,
            "issues": [],
            "issueCount": 0,
            "repairableCount": 0,
            "nonRepairableCount": 0,
        }
        with (
            patch.object(core, "codex_runtime_status", return_value={"available": True, "source": "desktop"}),
            patch.object(core, "configuration_status", return_value={"fullyApplied": True}),
            patch.object(core, "settings_reference_integrity", return_value=healthy_refs),
            patch.object(core, "session_storage_health", return_value={"status": "ok", "threadCount": 3, "rolloutCount": 2, "issues": []}),
            patch.object(maintenance, "list_skills", return_value={"skills": [], "count": 0}),
            patch.object(maintenance, "_runtime_overlay_health", return_value={"status": "ok", "recoverable": False, "detail": "ok"}),
        ):
            result = maintenance.run_emergency_checks(force=True)
        statuses = {item["id"]: item["status"] for item in result["checks"]}
        for check_id in (
            "settings_references",
            "runtime_overlay",
            "session_storage",
            "claude_transaction",
            "toolbox_storage",
            "radar_cache",
            "disk_space",
        ):
            self.assertIn(check_id, statuses)

    def test_cached_diagnostics_only_reenable_allowlisted_reversible_repairs(self):
        core.atomic_write_json(
            maintenance.DIAGNOSTICS_CACHE_FILE,
            {
                "schemaVersion": 2,
                "checkedAt": "2026-08-13T00:00:00+00:00",
                "checks": [
                    {
                        "id": "settings_references",
                        "status": "error",
                        "action": "repair_settings_references",
                        "autoFixable": True,
                        "data": {"recoverable": True},
                    },
                    {
                        "id": "toolbox_storage",
                        "status": "error",
                        "action": "delete_toolbox",
                        "autoFixable": True,
                        "data": {"recoverable": True},
                    },
                ],
            },
        )
        result = maintenance.run_emergency_checks(force=False)
        checks = {item["id"]: item for item in result["checks"]}
        self.assertTrue(checks["settings_references"]["autoFixable"])
        self.assertFalse(checks["toolbox_storage"]["autoFixable"])

    def test_cached_overlay_warning_is_rechecked_against_the_live_owner(self):
        core.atomic_write_json(
            maintenance.DIAGNOSTICS_CACHE_FILE,
            {
                "schemaVersion": 2,
                "checkedAt": "2026-08-13T00:00:00+00:00",
                "checks": [
                    {
                        "id": "runtime_overlay",
                        "label": "临时配置恢复记录",
                        "status": "warning",
                        "detail": "旧进程留下的恢复记录。",
                        "action": "restore_runtime_overlay",
                        "autoFixable": True,
                        "data": {"recoverable": True},
                    }
                ],
            },
        )
        with patch.object(
            maintenance,
            "_runtime_overlay_health",
            return_value={
                "status": "ok",
                "active": True,
                "recoverable": False,
                "detail": "恢复记录属于当前正在运行的 Agent Manager。",
            },
        ):
            result = maintenance.run_emergency_checks(force=False)

        overlay = next(item for item in result["checks"] if item["id"] == "runtime_overlay")
        self.assertEqual(overlay["status"], "ok")
        self.assertFalse(overlay["autoFixable"])
        self.assertTrue(result["healthy"])
        self.assertFalse(result["enableRepair"])

    def test_repair_dispatches_settings_reference_cleanup_and_rechecks(self):
        cached = {
            "checkedAt": "2026-08-13T00:00:00+00:00",
            "checks": [
                {
                    "id": "settings_references",
                    "status": "error",
                    "autoFixable": True,
                    "action": "repair_settings_references",
                    "data": {"recoverable": True},
                }
            ],
            "summary": {"ok": 0, "warning": 0, "error": 1},
            "healthy": False,
            "repairableCount": 1,
            "enableRepair": True,
            "cached": True,
            "needsManualCheck": False,
        }
        after = {
            **cached,
            "checks": [{"id": "settings_references", "status": "ok", "autoFixable": False}],
            "healthy": True,
            "repairableCount": 0,
            "enableRepair": False,
            "cached": False,
        }
        with (
            patch.object(maintenance, "run_emergency_checks", side_effect=[cached, {**cached, "cached": False}, after]),
            patch.object(core, "repair_settings_references", return_value={"changed": True, "removed": 4}) as repair,
            patch.object(maintenance, "_capture_repair_backup", return_value=[]),
        ):
            result = maintenance.apply_emergency_repairs(["settings_references"])
        repair.assert_called_once_with()
        self.assertEqual(result["results"][0]["status"], "fixed")
        self.assertFalse(result["rolledBack"])


if __name__ == "__main__":
    unittest.main()
