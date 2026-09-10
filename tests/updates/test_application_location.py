"""Relocation uses real PowerShell 5 filesystem/COM with fake EXEs only.

Process/launch boundaries are mocked; no installed executable or real Desktop
shortcut is moved, started, deleted or modified by these tests.
"""
from datetime import datetime, timedelta, timezone
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

from agent_manager.updates import location
from agent_manager.updates.service import UpdateError


def _write_native_shortcut_fixture(path, target, description):
    """Independent fixture writer: call Unicode COM vtables directly in Python.

    This uses neither the product's PowerShell/C# wrapper nor WScript.Shell, so
    unrelated-shortcut preservation is checked against independently made data.
    """
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    ole32.CoInitializeEx.restype = ctypes.c_long
    initialized = ole32.CoInitializeEx(None, 2)
    if initialized not in (0, 1, -2147417850):  # RPC_E_CHANGED_MODE: already initialized.
        raise OSError("Unable to initialize fixture COM", initialized)
    ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    ole32.CoCreateInstance.restype = ctypes.c_long
    def guid(value):
        return (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le)
    def call(pointer, index, types, *values):
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        method = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *types)(table[index])
        result = method(pointer, *values)
        if result < 0:
            raise OSError("Unicode shortcut fixture COM call failed", result)
    shell = ctypes.c_void_p()
    persist = ctypes.c_void_p()
    try:
        result = ole32.CoCreateInstance(guid("00021401-0000-0000-C000-000000000046"), None, 1,
                                       guid("000214F9-0000-0000-C000-000000000046"), ctypes.byref(shell))
        if result < 0:
            raise OSError("Unable to create fixture ShellLinkW", result)
        call(shell, 20, [ctypes.c_wchar_p], str(target))  # IShellLinkW.SetPath
        call(shell, 7, [ctypes.c_wchar_p], description)  # SetDescription
        call(shell, 9, [ctypes.c_wchar_p], str(Path(target).parent))
        call(shell, 17, [ctypes.c_wchar_p, ctypes.c_int], str(target), 0)
        call(shell, 0, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
             guid("0000010B-0000-0000-C000-000000000046"), ctypes.byref(persist))
        call(persist, 6, [ctypes.c_wchar_p, ctypes.c_int], str(path), 1)  # IPersistFile.Save
    finally:
        if persist.value:
            call(persist, 2, [])
        if shell.value:
            call(shell, 2, [])
        if initialized in (0, 1):
            ole32.CoUninitialize()


class ApplicationLocationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="位置-'引号'-[literal]-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "下载 '程序' [旧]" / "AgentManager-v1.exe"
        self.source.parent.mkdir()
        self.original = b"fake portable EXE -- never execute\x00\x01"
        self.source.write_bytes(self.original)
        self.destination = self.root / "保存 中文 ' [新]"
        self.destination.mkdir()
        self.state = self.root / "状态文件"
        self.shutdown = self.root / "退出状态.json"
        self.runtime = self.root / "运行实例.json"
        self.arguments = dict(destination=self.destination, state_dir=self.state, shutdown_status=self.shutdown,
                              source_pid=12345, source_nonce="source_instance_123456", runtime_file=self.runtime)

    def prepare(self):
        with patch.object(location, "_current_executable", return_value=self.source), patch.object(location.installer, "_process_creation_filetime", return_value=133333333333333333):
            self.prepared = location.prepare_location_move(**self.arguments)
        self.shutdown_data = {"pid": 12345, "runtimeNonce": self.arguments["source_nonce"], "phase": "completed",
                              "restorationComplete": True, "preserved": False, "errors": [], "at": datetime.now(timezone.utc).isoformat()}
        self.shutdown.write_text(json.dumps(self.shutdown_data, ensure_ascii=False), encoding="utf-8")
        self.runtime.write_text(json.dumps({"pid": 12345, "runtimeNonce": self.arguments["source_nonce"]}), encoding="utf-8")
        return self.prepared

    def powershell(self, overrides="", *, recover=False, invoke=True):
        powershell = shutil.which("powershell.exe")
        if not powershell:
            self.skipTest("Windows PowerShell 5 is unavailable")
        runner = self.prepared["directory"] / "test-runner.ps1"
        runner.write_text(
            ". (Join-Path $PSScriptRoot 'location.ps1')\n"
            "function Get-SourceProcess { return @{present=$true} }\n"
            "function Wait-ForSourceExit { }\n"
            "function Get-OldLocationProcesses { }\n"
            "function Get-TargetProcesses { return @{Id=999; Simulated=$true} }\n"
            "function Start-ManagerProcess($info) { return @{HasExited=$false; Simulated=$true} }\n"
            "function Test-ManagerReady([DateTime]$started) { return @{ready=$true; runtimeNonce='new_instance_1234567'; pid=999} }\n"
            "function Update-OwnedDesktopShortcuts { return @{updated=0; simulated=$true} }\n"
            + overrides + ("\nInvoke-LocationMove" + (" -Recovery" if recover else "") + "\n" if invoke else "\n"),
            encoding="utf-8-sig", newline="")
        completed = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(runner)],
                                   capture_output=True, timeout=25, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        result = self.prepared["statusFile"]
        return json.loads(result.read_text(encoding="utf-8-sig")) if result.exists() else None

    def test_prepare_does_not_copy_and_preserves_identity_unicode_and_digests(self):
        prepared = self.prepare()
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(list(self.destination.iterdir()), [])
        spec = prepared["spec"]
        self.assertEqual(spec["target"], str(self.destination / "AgentManager.exe"))
        self.assertEqual(spec["sha256"], hashlib.sha256(self.original).hexdigest())
        self.assertEqual(spec["sourceStartFileTime"], "133333333333333333")
        self.assertEqual(spec["runtimeFile"], str(self.runtime))
        self.assertIn("中文", (prepared["directory"] / "location.json").read_text(encoding="utf-8"))
        self.assertTrue(prepared["script"].read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_prepare_rejects_same_directory_and_existing_target_without_touching_files(self):
        for directory, code in ((self.source.parent, "same_location"), (self.destination, "location_collision")):
            with self.subTest(code=code):
                if directory == self.destination:
                    (directory / "AgentManager.exe").write_bytes(b"unrelated program")
                with patch.object(location, "_current_executable", return_value=self.source):
                    with self.assertRaises(UpdateError) as caught:
                        location.prepare_location_move(**{**self.arguments, "destination": directory})
                self.assertEqual(caught.exception.code, code)
        self.assertEqual((self.destination / "AgentManager.exe").read_bytes(), b"unrelated program")

    def test_prepare_rejects_roots_protected_relative_missing_and_reparse_directories(self):
        for directory in (self.destination.anchor, self.state, ".", self.root / "missing", os.environ.get("SystemRoot", self.root)):
            with self.subTest(directory=directory), patch.object(location, "_current_executable", return_value=self.source):
                with self.assertRaises(UpdateError):
                    location.prepare_location_move(**{**self.arguments, "destination": directory})
        if os.name == "nt":
            junction = self.root / "联接"
            completed = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junction), str(self.destination)], capture_output=True)
            self.assertEqual(completed.returncode, 0)
            try:
                with patch.object(location, "_current_executable", return_value=self.source), self.assertRaises(UpdateError):
                    location.prepare_location_move(**{**self.arguments, "destination": junction})
            finally:
                junction.rmdir()

    def test_prepare_rejects_invalid_nonce_before_process_reads(self):
        with patch.object(location, "_current_executable", return_value=self.source), patch.object(location.installer, "_process_creation_filetime") as process:
            with self.assertRaises(UpdateError):
                location.prepare_location_move(**{**self.arguments, "source_nonce": ""})
        process.assert_not_called()

    def test_helper_is_hidden_independent_and_requires_matching_readiness(self):
        prepared = self.prepare()
        prepared["statusFile"].write_text(json.dumps({"state": "waiting_for_exit", "helperPid": 555, "moveId": prepared["spec"]["moveId"]}), encoding="utf-8")
        process = Mock(pid=555)
        process.poll.return_value = None
        with patch.object(location.subprocess, "Popen", return_value=process) as popen:
            result = location.launch_location_move(prepared)
        self.assertTrue(result["started"])
        self.assertTrue(popen.call_args.kwargs["creationflags"] & 0x01000000)
        self.assertTrue(popen.call_args.kwargs["creationflags"] & 0x08000000)
        self.assertFalse(popen.call_args.kwargs.get("shell", False))

    def test_stale_helper_result_blocks_shutdown_authorization_and_only_stops_its_helper(self):
        prepared = self.prepare()
        prepared["statusFile"].write_text(json.dumps({"state": "waiting_for_exit", "helperPid": 555, "moveId": "other"}), encoding="utf-8")
        process = Mock(pid=555)
        process.poll.return_value = None
        with patch.object(location.subprocess, "Popen", return_value=process), self.assertRaises(UpdateError):
            location.launch_location_move(prepared, ready_timeout=0)
        process.terminate.assert_called_once()
        self.assertTrue(self.source.exists())

    def test_tampered_script_library_and_metadata_are_rejected_before_launch(self):
        for name in ("location.ps1", "common.ps1", "location.json"):
            with self.subTest(name=name):
                prepared = self.prepare()
                path = prepared["directory"] / name
                if name.endswith("json"):
                    path.write_text(json.dumps({**prepared["spec"], "source": "changed.exe"}), encoding="utf-8")
                else:
                    path.write_text("# modified", encoding="utf-8-sig")
                with patch.object(location.subprocess, "Popen") as popen, self.assertRaises(UpdateError):
                    location.launch_location_move(prepared)
                popen.assert_not_called()
                # The next iteration starts from a separately prepared record.
                (self.state / "app-location" / "latest.json").unlink()

    def test_real_helper_copies_then_deletes_only_old_exe_after_verification(self):
        self.prepare()
        unrelated = self.source.parent / "用户文件.json"
        unrelated.write_text("keep", encoding="utf-8")
        sibling = self.destination / "another.exe"
        sibling.write_bytes(b"unrelated")
        result = self.powershell()
        self.assertEqual(result["state"], "complete", result)
        self.assertFalse(self.source.exists())
        self.assertTrue(self.source.parent.is_dir())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")
        self.assertEqual(sibling.read_bytes(), b"unrelated")
        self.assertEqual((self.destination / "AgentManager.exe").read_bytes(), self.original)
        self.assertEqual(list(self.destination.glob(".agent-manager-move-*")), [])

    def test_failed_shutdown_identity_freshness_or_restoration_retains_old_without_copy(self):
        changes = [{"runtimeNonce": "wrong_instance_123456"}, {"pid": 10}, {"restorationComplete": False}, {"restorationComplete": "true"},
                   {"preserved": True}, {"errors": ["failed"]}, {"at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}]
        for change in changes:
            with self.subTest(change=change):
                self.prepare()
                self.shutdown.write_text(json.dumps({**self.shutdown_data, **change}), encoding="utf-8")
                result = self.powershell()
                self.assertEqual(result["state"], "failed", result)
                self.assertEqual(self.source.read_bytes(), self.original)
                self.assertFalse((self.destination / "AgentManager.exe").exists())

    def test_target_collision_after_preflight_is_not_overwritten(self):
        self.prepare()
        result = self.powershell(r'''
function Wait-ForSourceExit { [IO.File]::WriteAllText($script:installSpec.target, 'unrelated') }
function Get-TargetProcesses { }
function Start-ManagerProcess($info) {
    if ($info.FileName -ine $script:installSpec.source) { throw 'must restart only original executable' }
    return @{HasExited=$false; Simulated=$true}
}
''')
        self.assertEqual(result["state"], "failed", result)
        self.assertEqual(result["code"], "location_rollback_ready", result)
        self.assertTrue(result["restart"]["ready"])
        self.assertEqual((self.destination / "AgentManager.exe").read_text(), "unrelated")
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_post_exit_copy_create_failure_restarts_original_and_preserves_unrelated_staging_file(self):
        prepared = self.prepare()
        result = self.powershell(r'''
function Wait-ForSourceExit {
    $blockingFile = Join-Path ([IO.Path]::GetDirectoryName($script:installSpec.target)) ('.agent-manager-move-' + $script:installSpec.moveId + '.exe')
    [IO.File]::WriteAllText($blockingFile, 'unrelated staging collision')
}
function Get-TargetProcesses { }
function Start-ManagerProcess($info) {
    if ($info.FileName -ine $script:installSpec.source) { throw 'must restart only original executable' }
    return @{HasExited=$false; Simulated=$true}
}
''')
        self.assertEqual(result["code"], "location_rollback_ready", result)
        self.assertTrue(result["restart"]["ready"])
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse((self.destination / "AgentManager.exe").exists())
        blocking_file = self.destination / (".agent-manager-move-" + prepared["spec"]["moveId"] + ".exe")
        self.assertEqual(blocking_file.read_text(), "unrelated staging collision")

    def test_preflight_or_unconfirmed_restoration_never_restarts_original(self):
        for failure in ("preflight", "restoration"):
            with self.subTest(failure=failure):
                self.prepare()
                if failure == "restoration":
                    self.shutdown.write_text(json.dumps({**self.shutdown_data, "restorationComplete": False}), encoding="utf-8")
                else:
                    self.runtime.write_text(json.dumps({"pid": 0, "runtimeNonce": "wrong_source_123456789"}), encoding="utf-8")
                result = self.powershell(r'''
function Get-TargetProcesses { }
function Start-ManagerProcess {
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot 'unexpected-restart.txt'), 'must not run')
    return @{HasExited=$false; Simulated=$true}
}
''')
                self.assertEqual(result["state"], "failed", result)
                self.assertEqual(result["code"], "location_failed", result)
                self.assertIsNone(result["restart"])
                self.assertFalse((self.prepared["directory"] / "unexpected-restart.txt").exists())
                self.assertEqual(self.source.read_bytes(), self.original)
                self.assertFalse((self.destination / "AgentManager.exe").exists())

    def test_source_changed_after_exit_is_never_copied_or_deleted(self):
        self.prepare()
        result = self.powershell("function Wait-ForSourceExit { [IO.File]::WriteAllText($script:installSpec.source, 'changed') }")
        self.assertEqual(result["state"], "failed", result)
        self.assertEqual(self.source.read_text(), "changed")
        self.assertFalse((self.destination / "AgentManager.exe").exists())

    def test_unverified_startup_keeps_both_copies_and_late_success_finishes_cleanup(self):
        self.prepare()
        result = self.powershell("function Start-VerifiedManager { throw 'simulated startup timeout' }")
        self.assertEqual(result["state"], "cleanup_pending", result)
        self.assertEqual(result["code"], "startup_unverified")
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual((self.destination / "AgentManager.exe").read_bytes(), self.original)
        result = self.powershell("function Get-SourceProcess { return $null }", recover=True)
        self.assertEqual(result["state"], "complete", result)
        self.assertFalse(self.source.exists())

    def test_changed_destination_or_changed_old_bytes_block_old_deletion(self):
        self.prepare()
        result = self.powershell("function Test-ManagerReady { [IO.File]::WriteAllText($script:installSpec.source, 'replaced old'); return @{ready=$true} }")
        self.assertEqual(result["state"], "cleanup_pending", result)
        self.assertEqual(self.source.read_text(), "replaced old")

    def test_failed_destination_launch_safely_restarts_original_with_absolute_data_home(self):
        self.prepare()
        result = self.powershell(r'''
function Get-TargetProcesses { }
function Start-ManagerProcess($info) {
    if ($info.FileName -ieq $script:installSpec.target) { throw 'simulated new launch failure' }
    if ($info.FileName -ine $script:installSpec.source) { throw 'unexpected restart path' }
    if ($info.EnvironmentVariables['CODEX_HOME'] -ine [IO.Path]::GetDirectoryName($script:installSpec.stateDirectory)) { throw 'wrong data home' }
    if ($info.WorkingDirectory -ine [IO.Path]::GetDirectoryName($script:installSpec.source)) { throw 'wrong original cwd' }
    if ($info.EnvironmentVariables['PYINSTALLER_RESET_ENVIRONMENT'] -cne '1') { throw 'bootloader environment not reset' }
    return @{HasExited=$false; Simulated=$true}
}
''')
        self.assertEqual(result["state"], "failed", result)
        self.assertEqual(result["code"], "location_rollback_ready", result)
        self.assertTrue(result["restart"]["ready"])
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual((self.destination / "AgentManager.exe").read_bytes(), self.original)

    def test_proven_dead_destination_allows_original_restart(self):
        self.prepare()
        result = self.powershell(r'''
function Get-TargetProcesses { }
function Start-VerifiedManager {
    if ($script:installTarget -ieq $script:installSpec.target) {
        $script:launchedProcess = @{HasExited=$true}; throw 'simulated dead destination'
    }
    return @{ready=$true; restored=$true}
}
''')
        self.assertEqual(result["code"], "location_rollback_ready", result)
        self.assertTrue(self.source.exists())

    def test_uncertain_destination_process_scan_retains_both_without_original_restart(self):
        self.prepare()
        result = self.powershell(r'''
function Get-TargetProcesses { throw 'cannot read process image path' }
function Start-VerifiedManager {
    if ($script:installTarget -ieq $script:installSpec.source) { throw 'must not restart source under uncertainty' }
    $script:launchedProcess = @{HasExited=$true}; throw 'simulated dead destination'
}
''')
        self.assertEqual(result["state"], "cleanup_pending", result)
        self.assertNotIn("must not restart", result["detail"])
        self.assertTrue(self.source.exists())

    def test_mutated_destination_after_startup_proof_retains_old_exe(self):
        self.prepare()
        result = self.powershell(r'''
function Update-OwnedDesktopShortcuts {
    [IO.File]::WriteAllText($script:installSpec.target, 'destination changed after ready')
    return @{updated=0}
}
''')
        self.assertEqual(result["state"], "cleanup_pending", result)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_real_read_handle_lock_is_bounded_and_retains_old_exe(self):
        self.prepare()
        result = self.powershell(r'''
function Update-OwnedDesktopShortcuts {
    $script:testLock = [IO.File]::Open($script:installSpec.source, 'Open', 'Read', 'Read')
    return @{updated=0}
}
$boundedRemove = ${function:Remove-VerifiedOldLocation}
function Remove-VerifiedOldLocation {
    try { & $boundedRemove -timeoutSeconds 0 } finally { $script:testLock.Dispose() }
}
''')
        self.assertEqual(result["state"], "cleanup_pending", result)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_source_runtime_nonce_mismatch_blocks_helper_before_copy(self):
        self.prepare()
        self.runtime.write_text(json.dumps({"pid": 12345, "runtimeNonce": "wrong_instance_12345"}), encoding="utf-8")
        result = self.powershell()
        self.assertEqual(result["state"], "failed", result)
        self.assertFalse((self.destination / "AgentManager.exe").exists())
        self.assertTrue(self.source.exists())

    def test_windows_desktop_known_folder_resolves_without_modifying_desktop(self):
        self.prepare()
        self.powershell(r'''
$actualDesktop = Get-LocationDesktop
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'known-desktop.json'), (@{path=$actualDesktop; exists=[IO.Directory]::Exists($actualDesktop)} | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
''', invoke=False)
        result = json.loads((self.prepared["directory"] / "known-desktop.json").read_text(encoding="utf-8"))
        self.assertTrue(result["exists"])
        self.assertTrue(Path(result["path"]).is_absolute())

    def test_cross_drive_copy_uses_destination_sibling_then_cleans_old_file(self):
        artifact_root = Path(__file__).resolve().parents[2] / "artifacts"
        if artifact_root.anchor.lower() == self.root.anchor.lower():
            self.skipTest("A second test volume is unavailable")
        artifact_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="location-cross-drive-", dir=artifact_root) as temporary:
            self.destination = Path(temporary).resolve()
            self.arguments["destination"] = self.destination
            self.prepare()
            result = self.powershell()
            self.assertEqual(result["state"], "complete", result)
            self.assertFalse(self.source.exists())
            self.assertEqual((self.destination / "AgentManager.exe").read_bytes(), self.original)

    def test_old_parent_process_has_bounded_wait_without_killing(self):
        self.prepare()
        result = self.powershell("""
function Get-OldLocationProcesses {
    return [PSCustomObject]@{Id=0; StartTime=[DateTime]::FromFileTimeUtc(133333333333333332)}
}
$boundedRemove = ${function:Remove-VerifiedOldLocation}
function Remove-VerifiedOldLocation { & $boundedRemove -timeoutSeconds 0 }
""")
        self.assertEqual(result["state"], "cleanup_pending", result)
        self.assertTrue(self.source.exists())
        self.assertNotIn("Stop-Process", location.LOCATION_SCRIPT)
        self.assertNotIn(".Kill(", location.LOCATION_SCRIPT)

    def test_python_status_resumes_late_cleanup_only_with_ready_proof_and_once_per_instance(self):
        prepared = self.prepare()
        prepared["statusFile"].write_text(json.dumps({"moveId": prepared["spec"]["moveId"], "state": "cleanup_pending", "code": "startup_unverified"}), encoding="utf-8")
        target = Path(prepared["spec"]["target"])
        with patch.object(location, "_current_executable", return_value=target), patch.object(location.installer, "verify_current_installation", return_value=None), patch.object(location, "_launch_helper") as launch:
            status = location.read_location_status(state_dir=self.state, runtime_file=self.runtime)
            self.assertEqual(status["move"]["state"], "cleanup_pending")
            launch.assert_not_called()
        with patch.object(location, "_current_executable", return_value=target), patch.object(location.installer, "verify_current_installation", return_value={"runtimeNonce": "fresh_instance_123456"}), patch.object(location, "_launch_helper", return_value=Mock(pid=777)) as launch:
            status = location.read_location_status(state_dir=self.state, runtime_file=self.runtime)
            self.assertTrue(status["move"]["helperRunning"])
            location.read_location_status(state_dir=self.state, runtime_file=self.runtime)
            launch.assert_called_once()

    def test_pending_previous_move_blocks_new_prepare(self):
        prepared = self.prepare()
        prepared["statusFile"].write_text(json.dumps({"moveId": prepared["spec"]["moveId"], "state": "cleanup_pending"}), encoding="utf-8")
        with patch.object(location, "_current_executable", return_value=self.source), self.assertRaises(UpdateError) as caught:
            location.prepare_location_move(**self.arguments)
        self.assertEqual(caught.exception.code, "location_busy")

    def test_create_entry_verifies_final_desktop_file_and_updates_existing_hidden_owned_link(self):
        if os.name != "nt":
            self.skipTest("Windows only")
        desktop = self.root / "用户桌面🚀"
        desktop.mkdir()
        script = location.SHORTCUT_SCRIPT + "\nfunction Get-LocationDesktop { return $env:AGENT_MANAGER_TEST_DESKTOP }\n"
        with patch.object(location, "_current_executable", return_value=self.source), patch.object(location, "SHORTCUT_SCRIPT", script), patch.dict(os.environ, {"AGENT_MANAGER_TEST_DESKTOP": str(desktop)}):
            first = location.create_desktop_shortcut(state_dir=self.state)
            self.assertTrue(first["created"])
            self.assertTrue(first["verified"])
            self.assertTrue(first["exists"])
            self.assertTrue(first["shellNotified"])
            self.assertEqual(Path(first["path"]).parent, desktop)
            set_attributes = ctypes.WinDLL("kernel32", use_last_error=True).SetFileAttributesW
            set_attributes.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
            set_attributes.restype = ctypes.c_int
            self.assertTrue(set_attributes(first["path"], 2))
            second = location.create_desktop_shortcut(state_dir=self.state)
        self.assertFalse(second["created"])
        self.assertEqual(first["path"], second["path"])
        self.assertFalse(Path(second["path"]).stat().st_file_attributes & (2 | 4))
        self.assertEqual(len(list(desktop.glob("*.lnk"))), 1)

    def test_helper_exit_zero_with_claimed_success_but_missing_file_is_rejected(self):
        def fake_run(command, **kwargs):
            root = Path(command[-1]).parent
            (root / "shortcut-result.json").write_text(json.dumps({"ok": True, "created": True, "verified": True, "exists": True,
                "target": str(self.source), "path": str(self.destination / "missing.lnk"), "desktop": str(self.destination), "shellNotified": True}), encoding="utf-8")
            return Mock(returncode=0)
        with patch.object(location, "_current_executable", return_value=self.source), patch.object(location.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(UpdateError) as caught:
                location.create_desktop_shortcut(state_dir=self.state)
        self.assertEqual(caught.exception.code, "shortcut_verification_failed")

    def test_helper_failure_feedback_preserves_code_and_diagnostic(self):
        def fake_run(command, **kwargs):
            root = Path(command[-1]).parent
            (root / "shortcut-result.json").write_text(json.dumps({"ok": False, "code": "shortcut_not_found", "message": "当前桌面没有管理器快捷方式。", "detail": "native verification detail"}), encoding="utf-8")
            return Mock(returncode=1)
        with patch.object(location, "_current_executable", return_value=self.source), patch.object(location.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(UpdateError) as caught:
                location.reveal_desktop_shortcut(state_dir=self.state)
        self.assertEqual(caught.exception.code, "shortcut_not_found")
        self.assertEqual(caught.exception.detail, "native verification detail")
        self.assertEqual(str(caught.exception), "当前桌面没有管理器快捷方式。")

    def test_reveal_uses_only_verified_current_owned_shortcut_path(self):
        result = {"path": str(self.destination / "管理器🚀.lnk"), "verified": True, "exists": True}
        with patch.object(location, "_current_executable", return_value=self.source), patch.object(location, "_run_shortcut_helper", return_value=result) as helper, patch.object(location.subprocess, "Popen") as launch:
            returned = location.reveal_desktop_shortcut(state_dir=self.state)
        helper.assert_called_once_with(self.source, self.state, operation="find")
        self.assertEqual(launch.call_args.args[0][1:], ["/select,", result["path"]])
        self.assertTrue(returned["revealed"])

    def test_known_folder_uses_current_user_token_despite_inherited_profile_overrides(self):
        self.prepare()
        probe = r'''
$desktop = Get-LocationDesktop
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'desktop-env.json'), (@{path=$desktop} | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
'''
        self.powershell(probe, invoke=False)
        actual = json.loads((self.prepared["directory"] / "desktop-env.json").read_text(encoding="utf-8"))["path"]
        fake_profile = self.root / "另一个用户🚀"
        (fake_profile / "Desktop").mkdir(parents=True)
        with patch.dict(os.environ, {"USERPROFILE": str(fake_profile), "OneDrive": str(fake_profile), "HOMEPATH": str(fake_profile)[2:]}):
            self.powershell(probe, invoke=False)
        with_override = json.loads((self.prepared["directory"] / "desktop-env.json").read_text(encoding="utf-8"))["path"]
        self.assertEqual(with_override, actual)
        self.assertNotEqual(Path(with_override), fake_profile / "Desktop")

    def test_shortcut_com_roundtrip_redirected_unicode_desktop_idempotent_and_preserves_other_link(self):
        powershell = shutil.which("powershell.exe")
        if not powershell:
            self.skipTest("Windows PowerShell 5 is unavailable")
        desktop = self.root / "OneDrive 企业🚀" / "重定向桌面 中文 𐐀 ' [one]"
        desktop.mkdir(parents=True)
        unicode_source = self.source.parent / "来源🚀𐐀" / "管理器🚀.exe"
        unicode_source.parent.mkdir()
        unicode_source.write_bytes(self.original)
        unicode_target = self.destination / "目标🛰️𐐀" / "AgentManager.exe"
        unicode_target.parent.mkdir()
        unicode_target.write_bytes(self.original)
        other_target = self.root / "另一程序🚀.exe"
        other_target.write_bytes(b"unrelated fixture executable")
        other_path = desktop / "Agent Manager.lnk"
        _write_native_shortcut_fixture(other_path, other_target, "unrelated 另一程序🚀")
        metadata = self.root / "shortcut-test.json"
        metadata.write_text(json.dumps({"desktop": str(desktop), "target": str(unicode_source), "newTarget": str(unicode_target)}, ensure_ascii=False), encoding="utf-8")
        runner = self.root / "shortcut-test.ps1"
        runner.write_text(location.SHORTCUT_SCRIPT + r'''
$testData = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'shortcut-test.json'), [Text.Encoding]::UTF8) | ConvertFrom-Json
function Get-LocationDesktop { return [string]$testData.desktop }
Initialize-NativeShortcuts
function Get-TestHash([string]$path) {
    $stream = [IO.File]::OpenRead($path); $hash = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($hash.ComputeHash($stream)) } finally { $stream.Dispose(); $hash.Dispose() }
}
$otherPath = Join-Path $testData.desktop 'Agent Manager.lnk'
$other = [AgentManagerLocation.NativeShortcuts]::Read($otherPath)
$before = Get-TestHash $otherPath
$first = New-OwnedDesktopShortcut $testData.target
$second = New-OwnedDesktopShortcut $testData.target
$unicodePath = Join-Path $testData.desktop "管理器快捷方式🚀𐐀.lnk"
[IO.File]::Move($first.path, $unicodePath)
$third = New-OwnedDesktopShortcut $testData.target
$updated = Update-OwnedDesktopShortcuts $testData.target $testData.newTarget
$link = [AgentManagerLocation.NativeShortcuts]::Read($third.path)
$result = @{first=$first; second=$second; third=$third; updated=$updated; target=$link.TargetPath; working=$link.WorkingDirectory;
    icon=$link.IconLocation; description=$link.Description; arguments=$link.Arguments; windowStyle=$link.WindowStyle;
    otherTarget=$other.TargetPath; otherDescription=$other.Description; otherUnchanged=((Get-TestHash $otherPath) -ceq $before)}
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'shortcut-check.json'), ($result | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))
''', encoding="utf-8-sig", newline="")
        completed = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(runner)], capture_output=True, timeout=25, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        result = json.loads((self.root / "shortcut-check.json").read_text(encoding="utf-8"))
        self.assertTrue(result["first"]["created"])
        self.assertFalse(result["second"]["created"])
        self.assertEqual(result["first"]["path"], result["second"]["path"])
        self.assertNotEqual(Path(result["first"]["path"]).name, "Agent Manager.lnk")
        self.assertFalse(result["third"]["created"])
        self.assertEqual(result["third"]["path"], str(desktop / "管理器快捷方式🚀𐐀.lnk"))
        self.assertTrue(result["otherUnchanged"])
        self.assertEqual(result["otherDescription"], "unrelated 另一程序🚀")
        self.assertEqual(result["otherTarget"], str(other_target))
        self.assertEqual(result["updated"]["updated"], 1)
        self.assertEqual(result["target"], str(unicode_target))
        self.assertEqual(result["working"], str(unicode_target.parent))
        self.assertEqual(result["icon"], str(unicode_target) + ",0")
        self.assertEqual(result["description"], "openai-agent-manager:desktop-shortcut:v1")
        self.assertEqual(result["arguments"], "")
        self.assertEqual(result["windowStyle"], 1)


if __name__ == "__main__":
    unittest.main()
