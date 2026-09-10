"""Verified portable replacement by an independently launched Windows helper."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time

from agent_manager.updates.service import UpdateError, Version, _regular_path, _atomic_json, _read_json


def _process_creation_filetime(pid):
    if os.name != "nt":
        raise UpdateError("一键安装只支持 Windows。", "unsupported_install")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise UpdateError("无法验证正在运行的管理器进程。", "process_identity_unknown")
    try:
        created, exited, kernel_time, user_time = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
            raise UpdateError("无法读取管理器进程创建时间。", "process_identity_unknown")
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def _sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def installation_is_historical(spec, *, current_version):
    """An attempt for another executable/version is history, never success."""
    if os.name != "nt" or not getattr(sys, "frozen", False) or not isinstance(spec, dict):
        return False
    try:
        target = Path(spec["target"])
        if not target.is_absolute() or target.suffix.lower() != ".exe":
            return False
        target = _regular_path(target)
        return (target.resolve() != Path(sys.executable).resolve()
                or Version.parse(spec["version"]) < Version.parse(current_version))
    except (UpdateError, OSError, ValueError, TypeError, KeyError):
        return False


def verify_current_installation(spec, *, current_version, runtime_file):
    """Read-only proof that this installed process became ready after a handoff.

    A version label or a live PID alone cannot clear an earlier install error.
    The current frozen executable, its creation time and bytes, the discovery
    nonce, and a direct loopback health response must all agree.
    """
    if os.name != "nt" or not getattr(sys, "frozen", False) or not isinstance(spec, dict):
        return None
    try:
        if spec.get("schemaVersion") != 1 or Version.parse(spec.get("version")) != Version.parse(current_version):
            return None
        if not re.fullmatch(r"[a-f0-9]{48}", str(spec.get("installId") or "")):
            return None
        target = _regular_path(Path(spec["target"]).absolute(), missing=False)
        runtime_path = _regular_path(Path(runtime_file).absolute(), missing=False)
        if target.resolve() != Path(sys.executable).resolve() or target.suffix.lower() != ".exe":
            return None
        if Path(spec["runtimeFile"]).resolve() != runtime_path.resolve():
            return None
        source_nonce = str(spec.get("sourceNonce") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", source_nonce):
            return None
        started = _process_creation_filetime(os.getpid())
        prepared = datetime.fromisoformat(spec["preparedAt"].replace("Z", "+00:00"))
        if prepared.tzinfo is None or started <= int(spec["sourceStartFileTime"]):
            return None
        prepared_filetime = int((prepared.timestamp() + 11644473600) * 10_000_000)
        if started < prepared_filetime or prepared > datetime.now(timezone.utc):
            return None
        runtime = _read_json(runtime_path, 65536)
        if not isinstance(runtime, dict):
            return None
        nonce = str(runtime.get("runtimeNonce") or "")
        if (runtime.get("appId") != "openai-agent-manager" or runtime.get("pid") != os.getpid()
                or runtime.get("uiReady") is not True or runtime.get("independentLifecycle") is not True
                or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", nonce) or secrets.compare_digest(nonce, source_nonce)):
            return None
        port = runtime.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
            return None
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.7)
        try:
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            if response.status != 200:
                return None
            raw = response.read(65537)
            if len(raw) > 65536:
                return None
            health = json.loads(raw)
        finally:
            connection.close()
        if (not isinstance(health, dict) or health.get("ok") is not True
                or health.get("appId") != "openai-agent-manager" or health.get("runtimePid") != os.getpid()
                or health.get("uiReady") is not True or health.get("independentLifecycle") is not True
                or not secrets.compare_digest(str(health.get("runtimeNonce") or ""), nonce)):
            return None
        digest, size = spec.get("sha256"), spec.get("size")
        if (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or isinstance(size, bool) or not isinstance(size, int) or size <= 0):
            return None
        with target.open("rb") as stream:
            if os.fstat(stream.fileno()).st_size != size or hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                return None
        # Recheck discovery after HTTP/file I/O, so a concurrent restart cannot
        # splice the health response from one instance into another's record.
        current_runtime = _read_json(runtime_path, 65536)
        if current_runtime != runtime or _process_creation_filetime(os.getpid()) != started:
            return None
        return {"ready": True, "pid": os.getpid(), "runtimeNonce": nonce,
                "startFileTime": str(started), "sha256": digest, "verifiedAt": datetime.now(timezone.utc).isoformat()}
    except (UpdateError, OSError, ValueError, TypeError, KeyError, http.client.HTTPException):
        return None


def prepare_install(service, *, target, state_dir, shutdown_status, source_pid, source_nonce="", runtime_file=None):
    """Prepare only; never replace installed files or stop a process here."""
    if isinstance(source_pid, bool) or int(source_pid) <= 0 or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", str(source_nonce)):
        raise UpdateError("更新缺少当前管理器的实例身份。", "invalid_install_identity")
    source_pid = int(source_pid)
    created = _process_creation_filetime(source_pid)
    source = service.verified_download_path()
    target = _regular_path(Path(target).absolute(), missing=False)
    if target.suffix.lower() != ".exe" or not target.is_file() or source.resolve() == target.resolve():
        raise UpdateError("当前程序不支持原位更新，请使用 Windows EXE 版本。", "unsupported_install")
    download = service.status()["download"]
    shutdown_status = _regular_path(Path(shutdown_status).absolute())
    runtime_file = _regular_path(Path(runtime_file or shutdown_status.parent / "app-runtime.json").absolute())
    install_id = secrets.token_hex(24)
    stage = _regular_path(Path(state_dir) / "app-update-install" / install_id)
    stage.mkdir(parents=True, exist_ok=False)
    staged = stage / "AgentManager.exe"
    shutil.copyfile(source, staged)
    service._verify_file(staged, {"size": download["totalBytes"], "sha256": download["sha256"]})
    probe = target.parent / f".agent-manager-update-{secrets.token_hex(8)}.tmp"
    try:
        with probe.open("xb") as stream:
            stream.write(b"update preflight")
    finally:
        probe.unlink(missing_ok=True)
    spec = {
        "schemaVersion": 1, "installId": install_id,
        "preparedAt": datetime.now(timezone.utc).isoformat(),
        "source": str(staged), "target": str(target),
        "sha256": download["sha256"], "size": download["totalBytes"], "version": download["version"],
        "originalSha256": _sha256(target), "originalSize": target.stat().st_size,
        "sourcePid": source_pid, "sourceStartFileTime": str(created), "sourceNonce": str(source_nonce),
        "sourceParentPid": os.getppid() if source_pid == os.getpid() else 0,
        "shutdownStatus": str(shutdown_status), "runtimeFile": str(runtime_file), "state": "prepared",
    }
    from agent_manager.updates.cleanup import prepare_cleanup
    prepare_cleanup(stage, spec, source)
    _atomic_json(stage / "install.json", spec)
    script = stage / "install.ps1"
    script.write_text(INSTALL_SCRIPT, encoding="utf-8-sig")
    return {"spec": spec, "directory": stage, "script": script, "statusFile": stage / "result.json"}


def launch_install(prepared, *, ready_timeout=15.0):
    """A successful result means the independent helper passed its preflight."""
    if os.name != "nt":
        raise UpdateError("一键安装只支持 Windows。", "unsupported_install")
    script = _regular_path(prepared["script"], missing=False)
    if script.read_text(encoding="utf-8-sig") != INSTALL_SCRIPT:
        raise UpdateError("更新助手内容已变化，已取消安装。", "helper_changed")
    powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        process = subprocess.Popen(
            [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            cwd=str(prepared["directory"]), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                           | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                           | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)),
            close_fds=True,
        )
    except OSError as exc:
        raise UpdateError("更新助手无法独立启动；管理器保持运行，尚未安装更新。", "helper_launch_failed") from exc
    deadline = time.monotonic() + max(0.0, min(float(ready_timeout), 30.0))
    while True:
        try:
            result_path = _regular_path(prepared["statusFile"], missing=False)
            if result_path.stat().st_size > 65536:
                raise ValueError("oversized helper status")
            result = json.loads(result_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UpdateError):
            result = {}
        same = result.get("installId") == prepared["spec"]["installId"] and result.get("helperPid") == process.pid
        if same and result.get("state") == "waiting_for_exit" and process.poll() is None:
            return {"started": True, "pid": process.pid, "version": prepared["spec"]["version"], "statusFile": str(prepared["statusFile"])}
        if same and result.get("state") == "failed":
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=3)
            raise UpdateError(str(result.get("message") or "更新助手预检失败。")[:700], "helper_preflight_failed")
        if process.poll() is not None or time.monotonic() >= deadline:
            # Only this not-yet-authorized helper is cancelled, never the manager.
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            raise UpdateError("更新助手未确认就绪；管理器保持运行，安装已取消。", "helper_not_ready")
        time.sleep(0.05)


INSTALL_SCRIPT = r'''$ErrorActionPreference = 'Stop'
$script:helperRoot = $PSScriptRoot
function Assert-Regular([string]$path) {
    $item = Get-Item -LiteralPath $path -Force
    while ($item) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Update path contains a reparse point.' }
        $parent = [IO.Directory]::GetParent($item.FullName)
        if ($null -eq $parent) { break }
        $item = Get-Item -LiteralPath $parent.FullName -Force
    }
}
function Read-Json([string]$path) {
    Assert-Regular $path
    if ((Get-Item -LiteralPath $path).Length -gt 65536) { throw 'Oversized update metadata.' }
    # _atomic_json emits BOMless UTF-8. Windows PowerShell 5 defaults to the
    # ANSI code page, which silently corrupts non-English profile/EXE paths.
    return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json)
}
function Save-Result([string]$state, [string]$message, $restart = $null, [string]$code = '', [string]$detail = '') {
    $result = @{state=$state; message=$message; version=$script:installSpec.version;
        installId=$script:installSpec.installId; helperPid=$PID; at=[DateTime]::UtcNow.ToString('o');
        backup=$script:backup; restart=$restart; code=$code; detail=$detail;
        verification=@{reason=$script:readinessReason; startedAt=$script:restartStartedAt}}
    $temporary = Join-Path $script:helperRoot ('.result-' + [Guid]::NewGuid().ToString('N') + '.json')
    [IO.File]::WriteAllText($temporary, ($result | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))
    if ([IO.File]::Exists($script:resultFile)) { [IO.File]::Replace($temporary, $script:resultFile, [NullString]::Value) }
    else { [IO.File]::Move($temporary, $script:resultFile) }
}
function Assert-Hash([string]$path, [string]$expected, [long]$size) {
    Assert-Regular $path
    $hashStream = [IO.File]::Open($path, 'Open', 'Read', 'Read')
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = ([BitConverter]::ToString($algorithm.ComputeHash($hashStream))).Replace('-', '').ToLowerInvariant()
        if ($hashStream.Length -ne $size -or $digest -cne $expected) { throw 'Update file verification failed.' }
    } finally { $algorithm.Dispose(); $hashStream.Dispose() }
}
function Get-SourceProcess {
    $process = Get-Process -Id ([int]$script:installSpec.sourcePid) -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    if ($process.StartTime.ToUniversalTime().ToFileTimeUtc() -ne [long]$script:installSpec.sourceStartFileTime -or
        $process.MainModule.FileName -ine $script:installTarget) { throw 'Source process identity changed; original executable retained.' }
    return $process
}
function Wait-ForSourceExit([int]$timeoutSeconds = 180) {
    $deadline = [DateTime]::UtcNow.AddSeconds($timeoutSeconds)
    while ($null -ne (Get-SourceProcess)) {
        if ([DateTime]::UtcNow -ge $deadline) { throw 'Application has not safely exited; original executable retained.' }
        Start-Sleep -Milliseconds 200
    }
}
function Assert-CleanShutdown {
    $shutdown = Read-Json ([string]$script:installSpec.shutdownStatus)
    $at = [DateTimeOffset]::Parse([string]$shutdown.at).UtcDateTime
    $preparedAt = [DateTimeOffset]::Parse([string]$script:installSpec.preparedAt).UtcDateTime
    if ($shutdown.pid -ne $script:installSpec.sourcePid -or $shutdown.phase -cne 'completed' -or
        $shutdown.restorationComplete -isnot [bool] -or $shutdown.restorationComplete -ne $true -or
        $shutdown.preserved -isnot [bool] -or $shutdown.preserved -ne $false -or
        $null -eq $shutdown.errors -or @($shutdown.errors).Count -gt 0 -or
        $shutdown.runtimeNonce -cne $script:installSpec.sourceNonce -or
        $at -lt $preparedAt -or $at -gt [DateTime]::UtcNow.AddSeconds(5)) {
        throw 'Fresh clean shutdown and original configuration restoration were not confirmed; update cancelled.'
    }
}
function Get-TargetProcesses {
    foreach ($candidate in (Get-Process -Name ([IO.Path]::GetFileNameWithoutExtension($script:installTarget)) -ErrorAction SilentlyContinue)) {
        try { $path = $candidate.MainModule.FileName }
        catch { throw 'Cannot safely verify whether the installed executable is still running.' }
        if (-not $path) { throw 'Cannot safely verify whether the installed executable is still running.' }
        if ($path -ieq $script:installTarget) { $candidate }
    }
}
function Invoke-AtomicReplacement([string]$replacement, [string]$target, [string]$backup) {
    [IO.File]::Replace($replacement, $target, $backup, $true)
}
function Replace-InstalledFile([string]$replacement, [int]$timeoutSeconds = 10) {
    $deadline = [DateTime]::UtcNow.AddSeconds($timeoutSeconds)
    do {
        Assert-Hash $script:installTarget $script:installSpec.originalSha256 $script:installSpec.originalSize
        $launcherPresent = $false
        foreach ($candidate in @(Get-TargetProcesses)) {
            if ($candidate.Id -eq [int]$script:installSpec.sourceParentPid -and
                $candidate.StartTime.ToUniversalTime().ToFileTimeUtc() -le [long]$script:installSpec.sourceStartFileTime) {
                $launcherPresent = $true
            } else { throw 'Another manager is running from the update target.' }
        }
        if (-not $launcherPresent) {
            try {
                Invoke-AtomicReplacement $replacement $script:installTarget $script:backup
                return
            } catch {
                $cause = $_.Exception
                if ($null -ne $cause.InnerException) { $cause = $cause.InnerException }
                $code = $cause.HResult -band 65535
                if ($code -notin @(5, 32, 33)) { throw }
            }
        }
        if ([DateTime]::UtcNow -ge $deadline) { throw 'Executable is still locked; original executable retained.' }
        Start-Sleep -Milliseconds 200
    } while ($true)
}
function Test-ManagerReady([DateTime]$started) {
    $script:readinessReason = 'runtime_metadata_unavailable'
    try {
        $runtime = Read-Json ([string]$script:installSpec.runtimeFile)
        $script:readinessReason = 'runtime_identity_mismatch'
        if ($runtime.appId -cne 'openai-agent-manager' -or $runtime.runtimeNonce -ceq $script:installSpec.sourceNonce -or
            [string]$runtime.runtimeNonce -notmatch '^[A-Za-z0-9_-]{16,128}$' -or
            [int]$runtime.pid -le 0 -or [int]$runtime.port -lt 1 -or [int]$runtime.port -gt 65535) { return $null }
        $script:readinessReason = 'ui_not_ready'
        if ($runtime.uiReady -isnot [bool] -or $runtime.uiReady -ne $true) { return $null }
        $script:readinessReason = 'lifecycle_not_independent'
        if ($runtime.independentLifecycle -isnot [bool] -or $runtime.independentLifecycle -ne $true) { return $null }
        $script:readinessReason = 'process_identity_mismatch'
        $process = Get-Process -Id ([int]$runtime.pid) -ErrorAction Stop
        if ($process.MainModule.FileName -ine $script:installTarget -or $process.StartTime.ToUniversalTime() -lt $started.AddSeconds(-1)) { return $null }
        $script:readinessReason = 'health_unavailable'
        $request = [Net.HttpWebRequest]::Create('http://127.0.0.1:' + [string]$runtime.port + '/api/health')
        $request.Proxy = $null; $request.Timeout = 1500; $request.ReadWriteTimeout = 1500; $request.AllowAutoRedirect = $false
        $response = $request.GetResponse()
        try {
            $reader = New-Object IO.StreamReader($response.GetResponseStream())
            try {
                $buffer = New-Object char[] 65537
                $count = $reader.ReadBlock($buffer, 0, $buffer.Length)
                if ($count -le 0 -or $count -ge $buffer.Length) { return $null }
                $health = (-join $buffer[0..($count - 1)]) | ConvertFrom-Json
            } finally { $reader.Dispose() }
        } finally { $response.Dispose() }
        $script:readinessReason = 'health_identity_mismatch'
        if ($health.ok -is [bool] -and $health.ok -eq $true -and $health.appId -ceq 'openai-agent-manager' -and
            $health.runtimePid -eq $runtime.pid -and $health.runtimeNonce -ceq $runtime.runtimeNonce -and
            $health.uiReady -is [bool] -and $health.uiReady -eq $true -and
            $health.independentLifecycle -is [bool] -and $health.independentLifecycle -eq $true) {
            $script:readinessReason = 'ready'
            return @{ready=$true; pid=$runtime.pid; runtimeNonce=$runtime.runtimeNonce}
        }
    } catch { }
    return $null
}
function Read-PersistentEnvironment([string]$scope) {
    return [Environment]::GetEnvironmentVariables([EnvironmentVariableTarget]$scope)
}
function New-ManagerStartInfo {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $script:installTarget
    $info.WorkingDirectory = [IO.Path]::GetDirectoryName($script:installTarget)
    $info.UseShellExecute = $false; $info.CreateNoWindow = $true
    $info.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    # The helper was launched BEFORE overlay recovery. Never pass its cached
    # credentials/runtime bootloader variables into the new manager baseline.
    $info.EnvironmentVariables.Clear()
    foreach ($scope in @('Machine', 'User')) {
        $persistent = Read-PersistentEnvironment $scope
        foreach ($name in $persistent.Keys) {
            if ($name -ieq 'Path' -and $scope -eq 'User' -and $info.EnvironmentVariables['Path']) {
                $info.EnvironmentVariables['Path'] += ';' + [string]$persistent[$name]
            } else { $info.EnvironmentVariables[[string]$name] = [string]$persistent[$name] }
        }
    }
    # Preserve only non-secret per-session locations absent from the registry.
    foreach ($name in @('CODEX_HOME','CODEX_WORKSPACE_PATH','USERPROFILE','APPDATA','LOCALAPPDATA','TEMP','TMP',
        'HOMEDRIVE','HOMEPATH','USERNAME','USERDOMAIN','SystemRoot','SystemDrive','COMSPEC','ProgramData',
        'ProgramFiles','ProgramFiles(x86)','ProgramW6432','PUBLIC','PROCESSOR_ARCHITECTURE','NUMBER_OF_PROCESSORS')) {
        if (-not $info.EnvironmentVariables.ContainsKey($name)) {
            $value = [Environment]::GetEnvironmentVariable($name, 'Process')
            if ($null -ne $value) { $info.EnvironmentVariables[$name] = $value }
        }
    }
    foreach ($name in @($info.EnvironmentVariables.Keys)) {
        if ($name -imatch '^(_PYI_|_MEIPASS|AGENT_MANAGER_)' -or
            $name -iin @('CODEX_CLI_PATH','CODEX_AGENT_MANAGER_API_KEY')) { $info.EnvironmentVariables.Remove($name) }
    }
    $info.EnvironmentVariables['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    return $info
}
function Start-ManagerProcess($info) {
    return [Diagnostics.Process]::Start($info)
}
function Start-VerifiedManager([int]$timeoutSeconds = 180) {
    $started = [DateTime]::UtcNow
    $script:restartStartedAt = $started.ToString('o')
    $script:readinessTimedOut = $false
    $info = New-ManagerStartInfo
    $script:launchedProcess = Start-ManagerProcess $info
    $deadline = $started.AddSeconds($timeoutSeconds)
    do {
        $ready = Test-ManagerReady $started
        if ($null -ne $ready) { return $ready }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    $script:readinessTimedOut = $true
    throw 'Started manager did not publish a verified ready window and service.'
}
function Write-CleanupReceipt {
    $authorityPath = Join-Path $script:helperRoot 'cleanup-authority.json'
    if (-not [IO.File]::Exists($authorityPath)) { return $null }
    $keyPath = Join-Path ([IO.Path]::GetDirectoryName($script:helperRoot)) '.cleanup-key'
    Assert-Regular $keyPath
    if ((Get-Item -LiteralPath $keyPath).Length -ne 32) { throw 'Invalid cleanup key.' }
    $key = [IO.File]::ReadAllBytes($keyPath)
    if ($key.Length -ne 32) { throw 'Invalid cleanup key.' }
    $envelope = Read-Json $authorityPath
    $bytes = [Convert]::FromBase64String([string]$envelope.payload)
    $hmac = New-Object Security.Cryptography.HMACSHA256
    try {
        $hmac.Key = $key
        $signature = ([BitConverter]::ToString($hmac.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
        if ($signature -cne [string]$envelope.signature) { throw 'Changed cleanup authority.' }
        $value = [Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
        if ($value.state -cne 'prepared' -or $value.schemaVersion -ne 1 -or
            $value.installId -cne $script:installSpec.installId -or
            $value.target -cne $script:installSpec.target -or
            $value.sha256 -cne $script:installSpec.sha256 -or $value.size -ne $script:installSpec.size) {
            throw 'Cleanup authority does not match this installation.'
        }
        Assert-Hash $script:installTarget $value.sha256 $value.size
        $value.state = 'complete'
        $value | Add-Member -NotePropertyName ready -NotePropertyValue $true
        $value | Add-Member -NotePropertyName verifiedSha256 -NotePropertyValue $value.sha256
        $value | Add-Member -NotePropertyName verifiedAt -NotePropertyValue ([DateTime]::UtcNow.ToString('o'))
        Assert-Regular $script:resultFile
        $resultAlgorithm = [Security.Cryptography.SHA256]::Create()
        try { $resultDigest = ([BitConverter]::ToString($resultAlgorithm.ComputeHash([IO.File]::ReadAllBytes($script:resultFile)))).Replace('-', '').ToLowerInvariant() }
        finally { $resultAlgorithm.Dispose() }
        $value | Add-Member -NotePropertyName resultSha256 -NotePropertyValue $resultDigest
        $bytes = [Text.Encoding]::UTF8.GetBytes(($value | ConvertTo-Json -Depth 8 -Compress))
        $signature = ([BitConverter]::ToString($hmac.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
        $signed = @{payload=[Convert]::ToBase64String($bytes); signature=$signature} | ConvertTo-Json -Compress
        $receipt = Join-Path $script:helperRoot 'cleanup-receipt.json'
        $temporary = Join-Path $script:helperRoot ('.cleanup-' + [Guid]::NewGuid().ToString('N') + '.tmp')
        [IO.File]::WriteAllText($temporary, $signed, (New-Object Text.UTF8Encoding($false)))
        if ([IO.File]::Exists($receipt)) { [IO.File]::Replace($temporary, $receipt, [NullString]::Value) }
        else { [IO.File]::Move($temporary, $receipt) }
        return $value
    } finally { $hmac.Dispose() }
}
function Remove-CompletedInstallFiles($receipt) {
    if ($null -eq $receipt) { return }
    # Hash and request deletion through the same exclusive handle; never
    # replace a path-based hash check with a later, racy Delete(path).
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using Microsoft.Win32.SafeHandles;
public static class AgentManagerCleanup {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern SafeFileHandle CreateFile(string path, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool SetFileInformationByHandle(SafeFileHandle handle, int kind, ref int value, uint length);
    public static bool DeleteVerified(string path, string expected, long size) {
        using (var handle = CreateFile(path, 0x80010000, 0, IntPtr.Zero, 3, 0x00200000, IntPtr.Zero)) {
            if (handle.IsInvalid) return false;
            using (var stream = new FileStream(handle, FileAccess.Read))
            using (var sha = SHA256.Create()) {
                if (stream.Length != size || BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant() != expected) return false;
                int delete = 1;
                return SetFileInformationByHandle(handle, 4, ref delete, 4);
            }
        }
    }
}
'@
    foreach ($file in $receipt.files) {
        # The download cache is collected by the new service under its
        # operation lock, preserving any current or in-flight download.
        if ($file.role -ceq 'backup') { $expectedPath = $script:backup }
        elseif ($file.role -ceq 'staged') { $expectedPath = $script:installSpec.source }
        else { continue }
        try {
            if ($file.path -cne $expectedPath -or $file.path -ieq $script:installTarget) { continue }
            Assert-Regular ([string]$file.path)
            $null = [AgentManagerCleanup]::DeleteVerified([string]$file.path, [string]$file.sha256, [long]$file.size)
        } catch { } # A lock or changed file is retried from the signed receipt.
    }
}
function Invoke-Install {
    $script:installSpec = $null; $script:backup = $null; $script:launchedProcess = $null
    $script:readinessTimedOut = $false; $script:readinessReason = $null; $script:restartStartedAt = $null
    $script:resultFile = Join-Path $script:helperRoot 'result.json'
    $sibling = $null; $replaced = $false; $restart = $null
    try {
        $script:installSpec = Read-Json (Join-Path $script:helperRoot 'install.json')
        $spec = $script:installSpec
        if ($spec.schemaVersion -ne 1 -or [string]$spec.installId -notmatch '^[0-9a-f]{48}$' -or
            [string]$spec.sourceNonce -notmatch '^[A-Za-z0-9_-]{16,128}$' -or [int]$spec.sourcePid -le 0 -or
            [long]$spec.sourceStartFileTime -le 0 -or [string]$spec.sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$spec.originalSha256 -notmatch '^[0-9a-f]{64}$') { throw 'Invalid installation identity or digest.' }
        $script:installTarget = [IO.Path]::GetFullPath([string]$spec.target)
        $installSource = [IO.Path]::GetFullPath([string]$spec.source)
        if ([IO.Path]::GetExtension($script:installTarget) -ine '.exe' -or $script:installTarget -ieq $installSource -or
            [IO.Path]::GetDirectoryName($installSource) -ine $script:helperRoot) { throw 'Invalid update target or staging directory.' }
        Assert-Hash $installSource $spec.sha256 $spec.size
        Assert-Hash $script:installTarget $spec.originalSha256 $spec.originalSize
        if ($null -eq (Get-SourceProcess)) { throw 'Source manager already exited before updater readiness.' }
        Save-Result 'waiting_for_exit' 'Waiting for configuration restoration and application exit.'
        Wait-ForSourceExit
        Assert-CleanShutdown
        Assert-Hash $script:installTarget $spec.originalSha256 $spec.originalSize
        $targetParent = [IO.Path]::GetDirectoryName($script:installTarget)
        $sibling = Join-Path $targetParent ('.agent-manager-new-' + [Guid]::NewGuid().ToString('N') + '.exe')
        $script:backup = Join-Path $targetParent ('.agent-manager-old-' + $spec.installId + '.exe')
        if ([IO.File]::Exists($script:backup)) { throw 'Update backup already exists; recovery file retained.' }
        Assert-Regular $installSource
        $sourceLock = [IO.File]::Open($installSource, 'Open', 'Read', 'Read')
        try {
            Assert-Hash $installSource $spec.sha256 $spec.size
            $destination = [IO.File]::Open($sibling, 'CreateNew', 'Write', 'None')
            try { $sourceLock.CopyTo($destination); $destination.Flush($true) }
            finally { $destination.Dispose() }
        } finally { $sourceLock.Dispose() }
        Assert-Hash $sibling $spec.sha256 $spec.size
        Assert-Regular $targetParent
        Replace-InstalledFile $sibling
        $sibling = $null; $replaced = $true
        Assert-Hash $script:installTarget $spec.sha256 $spec.size
        Save-Result 'installed' '更新已安装，正在确认窗口和服务就绪。' $null 'verifying_startup'
        $restart = Start-VerifiedManager
        Assert-Hash $script:installTarget $spec.sha256 $spec.size
        Save-Result 'complete' '更新已完成，窗口和服务已就绪。' $restart 'update_ready'
        # Cleanup cannot turn an already verified installation into a rollback.
        try { $receipt = Write-CleanupReceipt; Remove-CompletedInstallFiles $receipt } catch { }
    } catch {
        $failure = $_.Exception.Message
        if ($replaced) {
            try {
                if (($null -ne $script:launchedProcess -and -not $script:launchedProcess.HasExited) -or @(Get-TargetProcesses).Count -gt 0) {
                    if ($script:readinessTimedOut) {
                        Assert-Hash $script:installTarget $script:installSpec.sha256 $script:installSpec.size
                        Save-Result 'installed' '更新已安装，启动状态尚未确认。' @{ready=$false} 'startup_unverified' $failure
                        return
                    }
                    throw 'New manager is still running; backup retained for safe manual recovery.'
                }
                Assert-Hash $script:backup $script:installSpec.originalSha256 $script:installSpec.originalSize
                [IO.File]::Replace($script:backup, $script:installTarget, [NullString]::Value, $true)
                $script:backup = $null
                Assert-Hash $script:installTarget $script:installSpec.originalSha256 $script:installSpec.originalSize
                $restart = Start-VerifiedManager
                $failure += ' Original executable restored and its window/service verified.'
            } catch {
                $restart = @{ready=$false; error=$_.Exception.Message}
                $failure += ' Rollback/restart: ' + $_.Exception.Message
            }
        }
        Save-Result 'failed' '更新未完成，请查看安装详情。' $restart 'install_failed' $failure
    } finally {
        if ($sibling -and [IO.File]::Exists($sibling)) { [IO.File]::Delete($sibling) }
    }
}
if ($MyInvocation.InvocationName -ne '.') { Invoke-Install }
'''
