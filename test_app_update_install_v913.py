"""Updater verification using temporary fake executables and real PowerShell I/O.

PowerShell tests replace only process/launch functions. No fake EXE is executed.
"""
from datetime import datetime, timedelta, timezone
import ctypes
from ctypes import wintypes
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import app_update_installer as installer
from app_update_service import UpdateError


class InstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="installer-'quoted-[literal]-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = self.root / "install 'folder' [one]" / "AgentManager.exe"
        self.target.parent.mkdir()
        self.target.write_bytes(b"old fake executable - never executed")
        self.source = self.root / "download.exe"
        self.source.write_bytes(b"new fake executable - never executed")
        self.original = self.target.read_bytes()
        self.updated = self.source.read_bytes()
        self.service = Mock()
        self.service.verified_download_path.return_value = self.source
        self.service.status.return_value = {"download": {
            "totalBytes": len(self.updated), "sha256": hashlib.sha256(self.updated).hexdigest(), "version": "99.0.0",
        }}
        def verify(path, spec):
            data = Path(path).read_bytes()
            if len(data) != spec["size"] or hashlib.sha256(data).hexdigest() != spec["sha256"]:
                raise UpdateError("hash mismatch", "hash_mismatch")
            return path
        self.service._verify_file.side_effect = verify
        self.arguments = dict(target=self.target, state_dir=self.root / "state", shutdown_status=self.root / "shutdown.json",
                              source_pid=12345, source_nonce="source_nonce_123456789")

    def prepare(self):
        with patch.object(installer, "_process_creation_filetime", return_value=133333333333333333):
            self.prepared = installer.prepare_install(self.service, **self.arguments)
        self.shutdown = {
            "pid": self.arguments["source_pid"], "runtimeNonce": self.arguments["source_nonce"],
            "phase": "completed", "restorationComplete": True, "preserved": False, "errors": [],
            "at": datetime.now(timezone.utc).isoformat(),
        }
        return self.prepared

    def run_powershell(self, overrides="", *, shutdown_changes=None, invoke=True):
        powershell = shutil.which("powershell.exe")
        if not powershell:
            self.skipTest("Windows PowerShell is unavailable")
        self.shutdown.update(shutdown_changes or {})
        self.arguments["shutdown_status"].write_text(json.dumps(self.shutdown), encoding="utf-8")
        # Dot-source production functions, then override process boundaries.
        # All filesystem verification and replacement/rollback remains real.
        runner = self.prepared["directory"] / "test-runner.ps1"
        runner.write_text(
            ". (Join-Path $PSScriptRoot 'install.ps1')\n"
            "function Get-SourceProcess { return @{present=$true} }\n"
            "function Wait-ForSourceExit { }\n"
            "function Get-TargetProcesses { }\n"
            "function Start-VerifiedManager { return @{ready=$true; simulated=$true} }\n"
            + overrides + ("\nInvoke-Install\n" if invoke else "\n"), encoding="utf-8-sig",
        )
        completed = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(runner)],
                                   capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        path = self.prepared["statusFile"]
        return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else None

    def test_prepare_snapshots_identity_digests_and_does_not_replace(self):
        prepared = self.prepare()
        self.assertEqual(self.target.read_bytes(), self.original)
        spec = prepared["spec"]
        self.assertEqual(spec["originalSha256"], hashlib.sha256(self.original).hexdigest())
        self.assertEqual(spec["sourceNonce"], self.arguments["source_nonce"])
        self.assertEqual(spec["sourceStartFileTime"], "133333333333333333")
        self.assertEqual(spec["runtimeFile"], str(self.root / "app-runtime.json"))
        self.assertEqual(len(spec["installId"]), 48)
        self.assertEqual(list(self.target.parent.glob("*.tmp")), [])

    def test_missing_nonce_rejects_before_process_or_download_access(self):
        self.arguments["source_nonce"] = ""
        with patch.object(installer, "_process_creation_filetime") as process:
            with self.assertRaises(UpdateError):
                installer.prepare_install(self.service, **self.arguments)
        process.assert_not_called()
        self.service.verified_download_path.assert_not_called()

    def test_process_creation_identity_reads_current_process_without_mutation(self):
        if os.name != "nt":
            self.skipTest("Windows only")
        self.assertGreater(installer._process_creation_filetime(os.getpid()), 0)

    def test_launch_requires_breakaway_and_confirmed_helper_identity(self):
        prepared = self.prepare()
        process = Mock(pid=555)
        process.poll.return_value = None
        prepared["statusFile"].write_text(json.dumps({"state": "waiting_for_exit", "helperPid": 555,
                                                     "installId": prepared["spec"]["installId"]}), encoding="utf-8")
        with patch.object(installer.subprocess, "Popen", return_value=process) as popen:
            result = installer.launch_install(prepared)
        self.assertTrue(result["started"])
        self.assertTrue(popen.call_args.kwargs["creationflags"] & 0x01000000)
        self.assertTrue(popen.call_args.kwargs["creationflags"] & 0x08000000)
        self.assertFalse(popen.call_args.kwargs.get("shell", False))
        self.assertEqual(popen.call_args.args[0][-1], str(prepared["script"]))

    def test_launch_rejects_stale_helper_result_and_stops_only_its_helper(self):
        prepared = self.prepare()
        process = Mock(pid=555)
        process.poll.return_value = None
        prepared["statusFile"].write_text(json.dumps({"state": "waiting_for_exit", "helperPid": 555,
                                                     "installId": "other"}), encoding="utf-8")
        with patch.object(installer.subprocess, "Popen", return_value=process):
            with self.assertRaises(UpdateError):
                installer.launch_install(prepared, ready_timeout=0)
        process.terminate.assert_called_once()
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_failed_breakaway_has_no_attached_process_fallback(self):
        prepared = self.prepare()
        with patch.object(installer.subprocess, "Popen", side_effect=OSError("breakaway denied")) as popen:
            with self.assertRaises(UpdateError):
                installer.launch_install(prepared)
        self.assertEqual(popen.call_count, 1)

    def test_script_tampering_blocks_launch(self):
        prepared = self.prepare()
        prepared["script"].write_text("Write-Output 'changed'", encoding="utf-8-sig")
        with patch.object(installer.subprocess, "Popen") as popen:
            with self.assertRaises(UpdateError):
                installer.launch_install(prepared)
        popen.assert_not_called()

    def test_powershell_replaces_atomically_at_literal_paths_and_keeps_backup(self):
        self.prepare()
        result = self.run_powershell()
        self.assertEqual(result["state"], "complete", result)
        self.assertTrue(result["restart"]["simulated"])
        self.assertEqual(self.target.read_bytes(), self.updated)
        self.assertEqual(Path(result["backup"]).read_bytes(), self.original)
        self.assertEqual(Path(result["backup"]).parent, self.target.parent)
        self.assertEqual(list(self.target.parent.glob(".agent-manager-new-*.exe")), [])

    def test_powershell_rejects_shutdown_identity_freshness_and_restore_failures(self):
        changes = [
            {"runtimeNonce": "different_nonce_123456789"}, {"pid": 9},
            {"phase": "completed-with-errors"}, {"restorationComplete": False},
            {"restorationComplete": "true"}, {"preserved": True}, {"preserved": None},
            {"errors": ["restore failed"]},
            {"at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.prepare()
                result = self.run_powershell(shutdown_changes=change)
                self.assertEqual(result["state"], "failed", result)
                self.assertEqual(self.target.read_bytes(), self.original)

    def test_powershell_rechecks_staged_hash_after_parent_exit(self):
        self.prepare()
        result = self.run_powershell("function Wait-ForSourceExit { [IO.File]::WriteAllText($script:installSpec.source, 'corrupt') }")
        self.assertEqual(result["state"], "failed", result)
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_powershell_rejects_changed_original_target(self):
        self.prepare()
        self.target.write_bytes(b"another installation")
        result = self.run_powershell()
        self.assertEqual(result["state"], "failed", result)
        self.assertEqual(self.target.read_bytes(), b"another installation")

    def test_powershell_rollback_restores_original_and_verifies_old_restart(self):
        self.prepare()
        result = self.run_powershell("""
$script:launchCount = 0
function Start-VerifiedManager {
    $script:launchCount += 1
    if ($script:launchCount -eq 1) { throw 'simulated new version startup failure' }
    return @{ready=$true; simulated=$true; oldVersion=$true}
}
""")
        self.assertEqual(result["state"], "failed", result)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertTrue(result["restart"]["oldVersion"], result)
        self.assertIsNone(result["backup"])

    def test_powershell_never_rolls_back_while_new_process_is_still_running(self):
        self.prepare()
        result = self.run_powershell("""
function Start-VerifiedManager {
    $script:launchedProcess = @{HasExited=$false}
    throw 'readiness timeout with a live process'
}
""")
        self.assertEqual(result["state"], "failed", result)
        self.assertEqual(self.target.read_bytes(), self.updated)
        self.assertEqual(Path(result["backup"]).read_bytes(), self.original)
        self.assertFalse(result["restart"]["ready"])

    def test_powershell_parent_exit_timeout_never_replaces(self):
        self.prepare()
        # Use the actual waiting implementation with an always-present mock.
        result = self.run_powershell("""
. (Join-Path $PSScriptRoot 'install.ps1')
function Get-SourceProcess { return @{present=$true} }
$actualWait = ${function:Wait-ForSourceExit}
function Wait-ForSourceExit { & $actualWait -timeoutSeconds 0 }
function Get-TargetProcesses { }
function Start-VerifiedManager { throw 'must not execute' }
""")
        self.assertEqual(result["state"], "failed", result)
        self.assertIn("not safely exited", result["message"])
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_powershell_sharing_lock_retries_then_replaces(self):
        self.prepare()
        result = self.run_powershell("""
$script:replacementAttempts = 0
function Invoke-AtomicReplacement([string]$replacement, [string]$target, [string]$backup) {
    $script:replacementAttempts += 1
    if ($script:replacementAttempts -le 2) { throw (New-Object IO.IOException('sharing violation', -2147024864)) }
    [IO.File]::Replace($replacement, $target, $backup, $true)
}
""")
        self.assertEqual(result["state"], "complete", result)
        self.assertEqual(self.target.read_bytes(), self.updated)

    def test_powershell_lock_retry_rechecks_original_identity(self):
        self.prepare()
        result = self.run_powershell("""
function Invoke-AtomicReplacement([string]$replacement, [string]$target, [string]$backup) {
    [IO.File]::WriteAllText($target, 'concurrent target replacement')
    throw (New-Object IO.IOException('sharing violation', -2147024864))
}
""")
        self.assertEqual(result["state"], "failed", result)
        self.assertEqual(self.target.read_bytes(), b"concurrent target replacement")
        self.assertIn("verification failed", result["message"])

    def test_real_helper_rejects_wrong_source_executable_before_exit(self):
        if os.name != "nt":
            self.skipTest("Windows only")
        self.arguments["source_pid"] = os.getpid()
        prepared = installer.prepare_install(self.service, **self.arguments)
        with self.assertRaises(UpdateError) as rejected:
            installer.launch_install(prepared, ready_timeout=5)
        self.assertIn(rejected.exception.code, {"helper_preflight_failed", "helper_launch_failed"})
        if rejected.exception.code == "helper_preflight_failed":
            self.assertIn("Source process identity changed", str(rejected.exception))
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_powershell_restart_readiness_requires_live_matching_health_nonce(self):
        self.prepare()
        response_data = {"ok": True, "appId": "openai-agent-manager", "runtimePid": 98765,
                         "runtimeNonce": "new_runtime_nonce_123456789", "uiReady": True, "independentLifecycle": True}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler):
                body = json.dumps(response_data).encode()
                handler.send_response(200)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)
            def log_message(self, *_args):
                pass
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            runtime = {**response_data, "pid": 98765, "port": httpd.server_port}
            Path(self.prepared["spec"]["runtimeFile"]).write_text(json.dumps(runtime), encoding="utf-8")
            code = """
$script:installSpec = Read-Json (Join-Path $PSScriptRoot 'install.json')
$script:installTarget = $script:installSpec.target
$script:resultFile = Join-Path $PSScriptRoot 'result.json'
function Get-Process {
    return @{MainModule=@{FileName=$script:installTarget}; StartTime=[DateTime]::Now}
}
$ready = Test-ManagerReady ([DateTime]::UtcNow)
if ($null -ne $ready) { Save-Result 'ready' 'Verified local test service.' $ready }
else { Save-Result 'rejected' 'Identity did not match.' }
"""
            result = self.run_powershell(code, invoke=False)
            self.assertEqual(result["state"], "ready", result)
            response_data["runtimeNonce"] = "unexpected_nonce_123456789"
            result = self.run_powershell(code, invoke=False)
            self.assertEqual(result["state"], "rejected", result)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(3)

    def test_helper_survives_its_test_parent_kill_on_close_job(self):
        """Kill only this test's dummy parent; the helper executes no EXE."""
        if os.name != "nt":
            self.skipTest("Windows Jobs only")
        class Basic(ctypes.Structure):
            _fields_ = [("processTime", ctypes.c_longlong), ("jobTime", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("minWorking", ctypes.c_size_t), ("maxWorking", ctypes.c_size_t),
                        ("processLimit", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_ulonglong * 6),
                        ("memory", ctypes.c_size_t * 4)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        job = kernel.CreateJobObjectW(None, None)
        self.assertTrue(job)
        limits = Extended()
        limits.basic.flags = 0x2000 | 0x800  # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
        self.assertTrue(kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        stage = self.root / "job-test"
        stage.mkdir()
        harmless = r"""$ErrorActionPreference='Stop'
$spec = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'install.json') -Raw | ConvertFrom-Json
@{state='waiting_for_exit'; helperPid=$PID; installId=$spec.installId} | ConvertTo-Json |
    Set-Content -LiteralPath (Join-Path $PSScriptRoot 'result.json') -Encoding UTF8
$deadline=[DateTime]::UtcNow.AddSeconds(10)
while ((Get-Process -Id $spec.sourcePid -ErrorAction SilentlyContinue) -and [DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Milliseconds 100
}
if (-not (Get-Process -Id $spec.sourcePid -ErrorAction SilentlyContinue)) {
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot 'survived.txt'), 'survived')
}
"""
        (stage / "install.ps1").write_text(harmless, encoding="utf-8-sig")
        worker = stage / "parent.py"
        worker.write_text(
            "import json, os, sys, time\nfrom pathlib import Path\n"
            f"sys.path.insert(0, {str(Path(installer.__file__).parent)!r})\n"
            "import app_update_installer as installer\n"
            "stage=Path(__file__).parent\n"
            "while not (stage/'go').exists(): time.sleep(0.05)\n"
            "spec={'installId':'a'*48,'version':'test','sourcePid':os.getpid()}\n"
            "(stage/'install.json').write_text(json.dumps(spec))\n"
            "installer.INSTALL_SCRIPT=(stage/'install.ps1').read_text(encoding='utf-8-sig')\n"
            "prepared={'script':stage/'install.ps1','directory':stage,'statusFile':stage/'result.json','spec':spec}\n"
            "try:\n"
            " result=installer.launch_install(prepared,ready_timeout=5)\n"
            " (stage/'parent-ready.json').write_text(json.dumps(result))\n"
            " time.sleep(15)\n"
            "except Exception as exc:\n"
            " (stage/'parent-error.txt').write_text(str(exc))\n", encoding="utf-8",
        )
        parent = subprocess.Popen([sys.executable, str(worker)], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            if not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(int(parent._handle))):
                self.skipTest(f"host forbids isolated nested test job: {ctypes.get_last_error()}")
            (stage / "go").write_text("go")
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and not (stage / "parent-ready.json").exists():
                if (stage / "parent-error.txt").exists():
                    self.fail((stage / "parent-error.txt").read_text())
                time.sleep(0.05)
            self.assertTrue((stage / "parent-ready.json").exists())
            kernel.CloseHandle(job)
            job = None
            parent.wait(timeout=5)
            deadline = time.monotonic() + 4
            while not (stage / "survived.txt").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual((stage / "survived.txt").read_text(), "survived")
            # Wait for the harmless helper to release its current directory.
            time.sleep(0.5)
        finally:
            if job:
                kernel.CloseHandle(job)
            if parent.poll() is None:
                parent.terminate()
            parent.wait(timeout=5)

    def test_restart_reloads_restored_environment_and_discards_cached_overlay(self):
        self.prepare()
        result = self.run_powershell("""
$script:installSpec = Read-Json (Join-Path $PSScriptRoot 'install.json')
$script:installTarget = $script:installSpec.target
$script:resultFile = Join-Path $PSScriptRoot 'result.json'
$env:CODEX_AGENT_MANAGER_API_KEY = 'synthetic-old-overlay'
$env:CODEX_CLI_PATH = 'synthetic-old-wrapper'
$env:_PYI_APPLICATION_HOME_DIR = 'synthetic-old-unpack'
$env:SYNTHETIC_PROVIDER_KEY = 'synthetic-old-provider'
function Read-PersistentEnvironment([string]$scope) {
    if ($scope -eq 'Machine') { return @{Path='machine-path'} }
    return @{Path='user-path'; SYNTHETIC_PROVIDER_KEY='synthetic-restored-provider'; CODEX_CLI_PATH='obsolete-wrapper'}
}
$info = New-ManagerStartInfo
if ($info.EnvironmentVariables['CODEX_AGENT_MANAGER_API_KEY'] -or $info.EnvironmentVariables['CODEX_CLI_PATH'] -or
    $info.EnvironmentVariables['_PYI_APPLICATION_HOME_DIR'] -or
    $info.EnvironmentVariables['SYNTHETIC_PROVIDER_KEY'] -cne 'synthetic-restored-provider' -or
    $info.EnvironmentVariables['Path'] -cne 'machine-path;user-path' -or
    $info.EnvironmentVariables['PYINSTALLER_RESET_ENVIRONMENT'] -cne '1') { throw 'Environment was not rebuilt safely.' }
Save-Result 'safe-environment' 'Synthetic environment assertions passed.'
""", invoke=False)
        self.assertEqual(result["state"], "safe-environment")


if __name__ == "__main__":
    unittest.main()
