import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_manager_app as app


class ReleasePromotionTests(unittest.TestCase):
    def test_build_script_fails_closed_before_packaging_stale_frontend(self):
        source = (Path(__file__).parent / "build_exe.ps1").read_text(encoding="utf-8")
        npm_ci = source.index("npm ci --ignore-scripts")
        dist_cleanup = source.index("Remove-Item -LiteralPath $distRoot -Recurse -Force")
        npm_build = source.index("npm run build")
        pyinstaller = source.index("python -m PyInstaller")
        self.assertLess(npm_ci, dist_cleanup)
        self.assertLess(dist_cleanup, npm_build)
        self.assertLess(npm_build, pyinstaller)
        self.assertIn("npm ci --ignore-scripts", source)
        self.assertIn('throw "npm ci failed with exit code $LASTEXITCODE."', source)
        self.assertIn('throw "Frontend build failed with exit code $LASTEXITCODE."', source)
        self.assertIn("Frontend build did not produce a complete", source)
        self.assertIn("Release directory still contains unmanifested executable", source)
        self.assertIn("Release cleanup could not remove unmanifested executable", source)
        self.assertIn("$lockedCanonicalHandoff", source)
        self.assertIn("safe running-update handoff", source)
        self.assertIn("quick restart to promote", source)
        self.assertIn("Test-PathInsideProject", source)
        self.assertIn("OrdinalIgnoreCase", source)

    def test_versioned_handoff_promotes_exact_bytes_then_canonical_cleans_fallbacks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canonical = root / "AgentManager.exe"
            versioned = root / "AgentManager-5.7.0.exe"
            legacy = root / "CodexAgentManager-5.6.0.exe"
            canonical.write_bytes(b"old")
            versioned.write_bytes(b"new-release-bytes")
            legacy.write_bytes(b"legacy")
            with (
                patch.object(app.sys, "frozen", True, create=True),
                patch.object(app.sys, "executable", str(versioned)),
            ):
                result = app._promote_and_cleanup_release_executable()
            self.assertTrue(result["promoted"])
            self.assertEqual(canonical.read_bytes(), versioned.read_bytes())
            self.assertTrue(result["manifestUpdated"])
            self.assertTrue((root / "SHA256.txt").read_text(encoding="utf-8").endswith("  AgentManager.exe\n"))
            self.assertTrue(versioned.exists(), "a running fallback must not try to delete itself")

            with (
                patch.object(app.sys, "frozen", True, create=True),
                patch.object(app.sys, "executable", str(canonical)),
            ):
                result = app._promote_and_cleanup_release_executable()
            self.assertFalse(result["promoted"])
            self.assertFalse(versioned.exists())
            self.assertFalse(legacy.exists())
            self.assertEqual(set(result["cleaned"]), {versioned.name, legacy.name})

    def test_unmanaged_executable_name_never_modifies_neighbors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "helper.exe"
            canonical = root / "AgentManager.exe"
            current.write_bytes(b"helper")
            canonical.write_bytes(b"canonical")
            with (
                patch.object(app.sys, "frozen", True, create=True),
                patch.object(app.sys, "executable", str(current)),
            ):
                result = app._promote_and_cleanup_release_executable()
            self.assertEqual(result["reason"], "unmanaged_name")
            self.assertEqual(canonical.read_bytes(), b"canonical")

    def test_versioned_handoff_retries_transient_canonical_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canonical = root / "AgentManager.exe"
            versioned = root / "AgentManager-5.7.0.exe"
            canonical.write_bytes(b"old")
            versioned.write_bytes(b"new-release-bytes")
            real_replace = app.os.replace
            attempts = 0

            def transient_replace(source, destination):
                nonlocal attempts
                if Path(destination) == canonical:
                    attempts += 1
                    if attempts == 1:
                        raise PermissionError("temporary executable lock")
                return real_replace(source, destination)

            with (
                patch.object(app.sys, "frozen", True, create=True),
                patch.object(app.sys, "executable", str(versioned)),
                patch.object(app.os, "replace", side_effect=transient_replace),
                patch.object(app.time, "sleep"),
            ):
                result = app._promote_and_cleanup_release_executable()
            self.assertTrue(result["promoted"])
            self.assertEqual(attempts, 2)
            self.assertEqual(canonical.read_bytes(), versioned.read_bytes())

    def test_restart_prefers_canonical_after_promoted_copy_matches_timestamp(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canonical = root / "AgentManager.exe"
            versioned = root / "AgentManager-5.7.0.exe"
            canonical.write_bytes(b"same-release")
            versioned.write_bytes(b"same-release")
            canonical.touch()
            versioned.touch()
            timestamp = max(canonical.stat().st_mtime_ns, versioned.stat().st_mtime_ns)
            import os

            os.utime(canonical, ns=(timestamp, timestamp))
            os.utime(versioned, ns=(timestamp, timestamp))
            with (
                patch.object(app.sys, "frozen", True, create=True),
                patch.object(app.sys, "executable", str(versioned)),
                patch.object(app, "_executable_version", return_value=(5, 7, 0, 0)),
            ):
                command, workdir = app._manager_restart_command("handoff")
            self.assertEqual(Path(command[0]), canonical)
            self.assertEqual(workdir, root)

    def test_handoff_can_wait_for_ui_readiness_beyond_eight_seconds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "AgentManager-8.0.0.exe"
            canonical = root / "AgentManager.exe"
            current.write_bytes(b"new-release")
            canonical.write_bytes(b"old-release")
            clock = {"now": 0.0}
            replace = app.os.replace
            def locked_until_ready(source, destination):
                if Path(destination) == canonical and clock["now"] < 20:
                    raise PermissionError("previous WebView is still completing handoff")
                return replace(source, destination)
            with patch.object(app.sys, "frozen", True, create=True), \
                    patch.object(app.sys, "executable", str(current)), \
                    patch.object(app.os, "replace", side_effect=locked_until_ready), \
                    patch.object(app.time, "monotonic", side_effect=lambda: clock["now"]), \
                    patch.object(app.time, "sleep", side_effect=lambda seconds: clock.__setitem__("now", clock["now"] + seconds)):
                result = app._promote_and_cleanup_release_executable(app.RESTART_HANDOFF_TIMEOUT_SECONDS + 15)
            self.assertTrue(result["promoted"])
            self.assertGreaterEqual(clock["now"], 20)
            self.assertEqual(canonical.read_bytes(), b"new-release")

    def test_release_maintenance_is_deferred_until_background_worker(self):
        started = []

        class ImmediateThread:
            def __init__(self, *, target, name, daemon):
                self.target = target
                self.name = name
                self.daemon = daemon

            def start(self):
                started.append(self.name)

        with (
            patch.object(app.sys, "frozen", True, create=True),
            patch.object(app.threading, "Thread", ImmediateThread),
            patch.object(app, "_promote_and_cleanup_release_executable") as promote,
        ):
            worker = app._schedule_release_maintenance()
        self.assertIsNotNone(worker)
        self.assertEqual(started, ["agent-manager-release-maintenance"])
        promote.assert_not_called()

    def test_canonical_cleanup_retries_transient_fallback_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canonical = root / "AgentManager.exe"
            fallback = root / "AgentManager-5.7.0.exe"
            canonical.write_bytes(b"canonical")
            fallback.write_bytes(b"fallback")
            real_unlink = Path.unlink
            attempts = 0

            def transient_unlink(path, *args, **kwargs):
                nonlocal attempts
                if Path(path) == fallback:
                    attempts += 1
                    if attempts == 1:
                        raise PermissionError("temporary executable lock")
                return real_unlink(path, *args, **kwargs)

            with (
                patch.object(app.sys, "frozen", True, create=True),
                patch.object(app.sys, "executable", str(canonical)),
                patch.object(Path, "unlink", transient_unlink),
                patch.object(app.time, "sleep"),
            ):
                result = app._promote_and_cleanup_release_executable()
            self.assertEqual(attempts, 2)
            self.assertFalse(fallback.exists())
            self.assertEqual(result["cleaned"], [fallback.name])


if __name__ == "__main__":
    unittest.main()
