"""Portable EXE relocation and owned Desktop shortcuts, without moving user data.

The independent helper waits for the existing guarded shutdown, copies across
volumes, and removes only the verified old EXE after the new UI/service is ready.
An unverified restart always leaves the old executable available for recovery.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time

from agent_manager._version import VERSION
from agent_manager.updates import installer
from agent_manager.updates.service import UpdateError, _atomic_json, _read_json, _regular_path


def _current_executable():
    path = Path(sys.executable).absolute()
    if os.name != "nt" or not getattr(sys, "frozen", False) or path.suffix.lower() != ".exe":
        raise UpdateError("更改程序位置和创建快捷方式需要使用 Windows 单文件 EXE 版本。", "unsupported_location")
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle and Path(bundle).absolute().is_relative_to(path.parent) and not Path(bundle).name.startswith("_MEI"):
        raise UpdateError("当前是多文件程序，请使用单文件 EXE 版本更改位置。", "unsupported_location")
    return _regular_path(path, missing=False).resolve(strict=True)


def _directory(value, *, state_dir=None):
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise UpdateError("请选择程序保存目录。", "invalid_location")
    raw = str(value)
    path = Path(raw)
    if (not path.is_absolute() or raw.startswith(("\\\\", "//"))
            or any(part.endswith((".", " ")) or any(ord(char) < 32 or char in '<>"|?*:' for char in part)
                   for part in path.parts[1:])):
        raise UpdateError("请选择有效的本机绝对目录。", "unsafe_location")
    path = _regular_path(path, missing=False).resolve(strict=True)
    if not path.is_dir() or path == Path(path.anchor):
        raise UpdateError("请选择已有的普通文件夹，不能使用磁盘根目录。", "unsafe_location")
    protected = [os.environ.get(key) for key in ("SystemRoot", "WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData")]
    protected.extend([state_dir, getattr(sys, "_MEIPASS", None)])
    if any(value and path.is_relative_to(Path(value).absolute()) for value in protected):
        raise UpdateError("请在系统目录、程序数据目录之外选择保存位置。", "unsafe_location")
    return path


def _stage_root(state_dir):
    return _regular_path(Path(state_dir).absolute() / "app-location")


def _load_latest(state_dir):
    root = _stage_root(state_dir)
    latest = _read_json(root / "latest.json", 65536)
    if not isinstance(latest, dict) or not re.fullmatch(r"[a-f0-9]{48}", str(latest.get("moveId") or "")):
        return None
    stage = _regular_path(root / latest["moveId"], missing=False)
    spec = _read_json(stage / "location.json", 65536)
    if not isinstance(spec, dict) or spec.get("moveId") != latest["moveId"] or spec.get("schemaVersion") != 1:
        raise UpdateError("程序迁移记录无效。", "invalid_location_state")
    return {"spec": spec, "directory": stage, "script": stage / "location.ps1", "statusFile": stage / "result.json"}


def _helper_running(result):
    try:
        pid = result.get("helperPid")
        return (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
                and installer._process_creation_filetime(pid) == int(result["helperStartFileTime"]))
    except (UpdateError, OSError, KeyError, TypeError, ValueError):
        return False


def _location_script():
    digest = hashlib.sha256(installer.INSTALL_SCRIPT.encode("utf-8-sig")).hexdigest()
    return LOCATION_SCRIPT.replace("__COMMON_SHA256__", digest)


def prepare_location_move(*, destination, state_dir, shutdown_status, source_pid,
                          source_nonce, runtime_file=None, source=None):
    """Prepare a reviewed handoff only; no copy, process stop, or old-file delete."""
    current = _current_executable()
    if source is not None and Path(source).absolute() != current:
        raise UpdateError("只能迁移当前正在运行的管理器。", "invalid_location_source")
    if (isinstance(source_pid, bool) or not isinstance(source_pid, int) or source_pid <= 0
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", str(source_nonce))):
        raise UpdateError("无法确认当前管理器实例。", "invalid_location_identity")
    directory = _directory(destination, state_dir=state_dir)
    if directory == current.parent:
        raise UpdateError("程序已在所选目录中。", "same_location")
    target = _regular_path(directory / "AgentManager.exe")
    if target.exists():
        raise UpdateError("目标目录已有 AgentManager.exe，请选择其他目录。", "location_collision")
    latest = _load_latest(state_dir)
    if latest:
        result = _read_json(latest["statusFile"], 65536) or {}
        if _helper_running(result) or result.get("state", latest["spec"].get("state")) not in {"complete", "failed", "prepared"}:
            raise UpdateError("上一次迁移尚未结束，请先确认迁移状态。", "location_busy")
    created = installer._process_creation_filetime(source_pid)
    digest, size = installer._sha256(current), current.stat().st_size
    if size <= 0:
        raise UpdateError("当前程序文件无效。", "invalid_location_source")
    probe = directory / (".agent-manager-location-" + secrets.token_hex(12) + ".tmp")
    probe_created = False
    try:
        with probe.open("xb") as stream:
            probe_created = True
            stream.write(b"Agent Manager location preflight")
    except OSError as exc:
        raise UpdateError("无法写入所选目录，请选择有写入权限的文件夹。", "location_not_writable") from exc
    finally:
        if probe_created:
            probe.unlink(missing_ok=True)
    move_id = secrets.token_hex(24)
    stage = _regular_path(_stage_root(state_dir) / move_id)
    stage.mkdir(parents=True, exist_ok=False)
    shutdown = _regular_path(Path(shutdown_status).absolute())
    runtime = _regular_path(Path(runtime_file or shutdown.parent / "app-runtime.json").absolute())
    spec = {"schemaVersion": 1, "moveId": move_id, "installId": move_id,
            "version": VERSION, "state": "prepared", "preparedAt": datetime.now(timezone.utc).isoformat(),
            "source": str(current), "target": str(target), "sha256": digest, "size": size,
            "sourcePid": source_pid, "sourceStartFileTime": str(created), "sourceNonce": source_nonce,
            "sourceParentPid": os.getppid() if source_pid == os.getpid() else 0,
            "shutdownStatus": str(shutdown), "runtimeFile": str(runtime),
            "stateDirectory": str(Path(state_dir).absolute())}
    _atomic_json(stage / "location.json", spec)
    (stage / "common.ps1").write_text(installer.INSTALL_SCRIPT, encoding="utf-8-sig", newline="")
    script = stage / "location.ps1"
    script.write_text(_location_script(), encoding="utf-8-sig", newline="")
    _atomic_json(_stage_root(state_dir) / "latest.json", {"moveId": move_id})
    return {"spec": spec, "directory": stage, "script": script, "statusFile": stage / "result.json"}


def _launch_helper(prepared, *, recover=False):
    if os.name != "nt":
        raise UpdateError("程序迁移仅支持 Windows。", "unsupported_location")
    for path, expected in ((prepared["script"], _location_script()), (Path(prepared["directory"]) / "common.ps1", installer.INSTALL_SCRIPT)):
        if _regular_path(path, missing=False).read_text(encoding="utf-8-sig") != expected:
            raise UpdateError("迁移助手内容已变化，已取消操作。", "helper_changed")
    saved = _read_json(Path(prepared["directory"]) / "location.json", 65536)
    if saved != prepared["spec"]:
        raise UpdateError("迁移记录已变化，已取消操作。", "helper_changed")
    powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(prepared["script"])]
    if recover:
        command.append("-RecoverOnly")
    try:
        return subprocess.Popen(command, cwd=str(prepared["directory"]), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                                               | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                                               | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)))
    except OSError as exc:
        raise UpdateError("迁移助手无法独立启动，管理器保持运行。", "helper_launch_failed") from exc


def launch_location_move(prepared, *, ready_timeout=15.0):
    process = _launch_helper(prepared)
    deadline = time.monotonic() + max(0.0, min(float(ready_timeout), 30.0))
    while True:
        try:
            result = _read_json(prepared["statusFile"], 65536) or {}
        except (UpdateError, OSError):
            result = {}
        same = result.get("moveId") == prepared["spec"]["moveId"] and result.get("helperPid") == process.pid
        if same and result.get("state") == "waiting_for_exit" and process.poll() is None:
            return {"started": True, "pid": process.pid, "statusFile": str(prepared["statusFile"]),
                    "destination": prepared["spec"]["target"]}
        failed = same and result.get("state") == "failed"
        if failed or process.poll() is not None or time.monotonic() >= deadline:
            # Only the not-yet-authorized helper can be cancelled here.
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            message = str(result.get("message") or "迁移助手未确认就绪，管理器保持运行。")[:500] if failed else "迁移助手未确认就绪，管理器保持运行。"
            _atomic_json(prepared["statusFile"], {"moveId": prepared["spec"]["moveId"], "state": "failed", "message": message,
                                               "code": "helper_not_ready", "helperPid": process.pid})
            raise UpdateError(message, "helper_preflight_failed" if failed else "helper_not_ready")
        time.sleep(0.05)


def read_location_status(*, state_dir, runtime_file=None):
    """Report state; a proven late startup may resume exact-file cleanup only."""
    current = Path(sys.executable).absolute()
    try:
        current = _current_executable()
        supported, message = True, ""
    except (UpdateError, OSError) as exc:
        supported, message = False, str(exc)
    status = {"supported": supported, "currentExecutable": str(current), "currentDirectory": str(current.parent),
              "message": message, "move": None}
    try:
        prepared = _load_latest(state_dir)
        if not prepared:
            return status
        spec = prepared["spec"]
        result = _read_json(prepared["statusFile"], 65536) or {"state": "prepared", "message": "迁移尚未开始。"}
        if result.get("moveId", spec["moveId"]) != spec["moveId"]:
            raise UpdateError("迁移结果与当前记录不一致。", "invalid_location_state")
        running = _helper_running(result)
        if (supported and not running and result.get("state") in {"copying", "verifying_startup", "cleanup_pending"}
                and current == Path(spec["target"]).absolute()):
            proof = installer.verify_current_installation(spec, current_version=VERSION,
                        runtime_file=runtime_file or spec["runtimeFile"])
            # Limit recovery after a persistent file lock to one attempt per
            # running instance. A future startup can safely try again.
            attempt = _read_json(Path(prepared["directory"]) / "recovery.json", 65536) or {}
            if proof and attempt.get("runtimeNonce") != proof["runtimeNonce"]:
                process = _launch_helper(prepared, recover=True)
                _atomic_json(Path(prepared["directory"]) / "recovery.json", {"runtimeNonce": proof["runtimeNonce"], "pid": process.pid})
                running = True
        status["move"] = {key: result[key] for key in ("state", "message", "code", "detail", "restart", "at", "shortcuts") if key in result}
        status["move"].update(moveId=spec["moveId"], source=spec["source"], target=spec["target"], helperRunning=running,
                              oldCopyRetained=Path(spec["source"]).exists())
    except (UpdateError, OSError, KeyError, TypeError, ValueError) as exc:
        status["move"] = {"state": "failed", "message": "无法读取或恢复程序迁移状态。", "detail": str(exc)[:500], "code": "location_status_failed"}
    return status


def create_desktop_shortcut(*, target=None, state_dir=None):
    current = _current_executable()
    if target is not None and Path(target).absolute() != current:
        raise UpdateError("快捷方式只能指向当前管理器。", "invalid_shortcut_target")
    parent = _regular_path(Path(state_dir).absolute()) if state_dir else None
    if parent:
        parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agent-manager-shortcut-", dir=parent) as temporary:
        root = Path(temporary)
        _atomic_json(root / "shortcut.json", {"target": str(current)})
        script = root / "shortcut.ps1"
        script.write_text(SHORTCUT_SCRIPT + SHORTCUT_ENTRY, encoding="utf-8-sig", newline="")
        powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        try:
            completed = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
            result = _read_json(root / "shortcut-result.json", 65536)
            if completed.returncode != 0 or not isinstance(result, dict) or result.get("ok") is not True:
                raise UpdateError("无法创建桌面快捷方式，请确认桌面目录可写。", "shortcut_failed")
            return {key: result[key] for key in ("created", "path", "target")}
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateError("创建桌面快捷方式超时或系统组件不可用。", "shortcut_failed") from exc


SHORTCUT_SCRIPT = r'''$ErrorActionPreference = 'Stop'
$script:shortcutOwner = 'openai-agent-manager:desktop-shortcut:v1'
function Get-LocationDesktop {
    if (-not ('AgentManagerLocation.KnownFolders' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace AgentManagerLocation {
    public static class KnownFolders {
        [DllImport("shell32.dll")] private static extern int SHGetKnownFolderPath(ref Guid id, uint flags, IntPtr token, out IntPtr path);
        public static string Desktop() {
            Guid id = new Guid("B4BFCC3A-DB2C-424C-B029-7FE99A87C641");
            IntPtr path; int result = SHGetKnownFolderPath(ref id, 0, IntPtr.Zero, out path);
            if (result != 0) Marshal.ThrowExceptionForHR(result);
            try { return Marshal.PtrToStringUni(path); } finally { Marshal.FreeCoTaskMem(path); }
        }
    }
}
'@
    }
    $path = [AgentManagerLocation.KnownFolders]::Desktop()
    if (-not [IO.Directory]::Exists($path)) { throw 'Windows Desktop known folder is unavailable.' }
    return $path
}
function Initialize-NativeShortcuts {
    if ('AgentManagerLocation.NativeShortcuts' -as [type]) { return }
    # WScript.Shell may convert shortcut paths through the system ANSI code
    # page. Explicit IShellLinkW/IPersistFile preserve every UTF-16 path.
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Text;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
namespace AgentManagerLocation {
    [ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IShellLinkW {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int count, IntPtr findData, uint flags);
        void GetIDList(out IntPtr idList);
        void SetIDList(IntPtr idList);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int count);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string value);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int count);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string value);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int count);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string value);
        void GetHotkey(out short hotkey);
        void SetHotkey(short hotkey);
        void GetShowCmd(out int command);
        void SetShowCmd(int command);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int count, out int index);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
        void Resolve(IntPtr window, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
    }
    public sealed class ShortcutData {
        public string TargetPath { get; set; }
        public string WorkingDirectory { get; set; }
        public string Description { get; set; }
        public string Arguments { get; set; }
        public string IconPath { get; set; }
        public int IconIndex { get; set; }
        public int WindowStyle { get; set; }
        public string IconLocation { get { return IconPath + "," + IconIndex; } }
    }
    public static class NativeShortcuts {
        static object Create() {
            return Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("00021401-0000-0000-C000-000000000046"), true));
        }
        public static void Save(string path, string target, string description) {
            object instance = Create();
            try {
                IShellLinkW link = (IShellLinkW)instance;
                link.SetPath(target);
                link.SetWorkingDirectory(Path.GetDirectoryName(target));
                link.SetIconLocation(target, 0);
                link.SetDescription(description);
                link.SetArguments("");
                link.SetShowCmd(1);
                ((IPersistFile)instance).Save(path, true);
            } finally { Marshal.FinalReleaseComObject(instance); }
        }
        public static ShortcutData Read(string path) {
            object instance = Create();
            try {
                ((IPersistFile)instance).Load(path, 0);
                IShellLinkW link = (IShellLinkW)instance;
                StringBuilder buffer = new StringBuilder(32768);
                ShortcutData data = new ShortcutData();
                // SLGP_RAWPATH: read the stored target without Resolve, UI,
                // search/tracking or network access to a missing target.
                link.GetPath(buffer, buffer.Capacity, IntPtr.Zero, 4);
                data.TargetPath = buffer.ToString(); buffer.Length = 0;
                link.GetWorkingDirectory(buffer, buffer.Capacity);
                data.WorkingDirectory = buffer.ToString(); buffer.Length = 0;
                link.GetDescription(buffer, buffer.Capacity);
                data.Description = buffer.ToString(); buffer.Length = 0;
                link.GetArguments(buffer, buffer.Capacity);
                data.Arguments = buffer.ToString(); buffer.Length = 0;
                int index, show;
                link.GetIconLocation(buffer, buffer.Capacity, out index);
                data.IconPath = buffer.ToString(); data.IconIndex = index;
                link.GetShowCmd(out show); data.WindowStyle = show;
                return data;
            } finally { Marshal.FinalReleaseComObject(instance); }
        }
    }
}
'@
}
function Test-OwnedShortcut([string]$path, [string]$target) {
    if (-not [IO.File]::Exists($path)) { return $false }
    $item = Get-Item -LiteralPath $path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { return $false }
    try {
        Initialize-NativeShortcuts
        $link = [AgentManagerLocation.NativeShortcuts]::Read($path)
        return ($link.Description -ceq $script:shortcutOwner -and $link.TargetPath -ieq $target -and [string]::IsNullOrEmpty($link.Arguments))
    } catch { return $false }
}
function Save-OwnedShortcut([string]$path, [string]$target, [string]$previous = '') {
    $existed = [IO.File]::Exists($path)
    if ($existed -and -not (Test-OwnedShortcut $path $previous)) { throw 'Existing shortcut belongs to another application.' }
    $temporary = Join-Path ([IO.Path]::GetDirectoryName($path)) ('.agent-manager-' + [Guid]::NewGuid().ToString('N') + '.lnk')
    try {
        Initialize-NativeShortcuts
        [AgentManagerLocation.NativeShortcuts]::Save($temporary, $target, $script:shortcutOwner)
        if (-not (Test-OwnedShortcut $temporary $target)) { throw 'Shortcut verification failed.' }
        $link = [AgentManagerLocation.NativeShortcuts]::Read($temporary)
        if ($link.WorkingDirectory -ine [IO.Path]::GetDirectoryName($target) -or $link.IconPath -ine $target -or
            $link.IconIndex -ne 0 -or $link.WindowStyle -ne 1) { throw 'Shortcut fields changed while saving.' }
        if ($existed) {
            if (-not (Test-OwnedShortcut $path $previous)) { throw 'Existing shortcut changed during creation.' }
            [IO.File]::Replace($temporary, $path, [NullString]::Value)
        } else { [IO.File]::Move($temporary, $path) }
        return @{created=(-not $existed); path=$path; target=$target}
    } finally { if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) } }
}
function New-OwnedDesktopShortcut([string]$target) {
    $desktop = Get-LocationDesktop
    foreach ($item in @(Get-ChildItem -LiteralPath $desktop -Filter '*.lnk' -File -Force)) {
        if (Test-OwnedShortcut $item.FullName $target) { return (Save-OwnedShortcut $item.FullName $target $target) }
    }
    foreach ($name in @('Agent Manager.lnk', 'Agent Manager (便携版).lnk') + @(2..20 | ForEach-Object { 'Agent Manager (' + $_ + ').lnk' })) {
        $path = Join-Path $desktop $name
        if (-not (Test-Path -LiteralPath $path)) { return (Save-OwnedShortcut $path $target) }
    }
    throw 'No available desktop shortcut name.'
}
function Update-OwnedDesktopShortcuts([string]$source, [string]$target) {
    $desktop = Get-LocationDesktop
    $updated = 0
    foreach ($item in @(Get-ChildItem -LiteralPath $desktop -Filter '*.lnk' -File -Force)) {
        if (Test-OwnedShortcut $item.FullName $source) {
            [void](Save-OwnedShortcut $item.FullName $target $source)
            $updated += 1
        }
    }
    return @{updated=$updated}
}
'''

SHORTCUT_ENTRY = r'''
try {
    $metadata = ([IO.File]::ReadAllText((Join-Path $PSScriptRoot 'shortcut.json'), [Text.Encoding]::UTF8) | ConvertFrom-Json)
    $result = New-OwnedDesktopShortcut ([string]$metadata.target)
    $result.ok = $true
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot 'shortcut-result.json'), ($result | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
} catch { exit 1 }
'''


LOCATION_SCRIPT = r'''param([switch]$RecoverOnly)
$ErrorActionPreference = 'Stop'
$common = Join-Path $PSScriptRoot 'common.ps1'
$commonStream = [IO.File]::Open($common, 'Open', 'Read', 'Read')
$commonHash = [Security.Cryptography.SHA256]::Create()
try {
    $commonDigest = ([BitConverter]::ToString($commonHash.ComputeHash($commonStream))).Replace('-', '').ToLowerInvariant()
    if ($commonDigest -cne '__COMMON_SHA256__') { throw 'Location helper library changed.' }
} finally { $commonHash.Dispose(); $commonStream.Dispose() }
. $common
''' + SHORTCUT_SCRIPT + r'''
$script:originalManagerStartInfo = ${function:New-ManagerStartInfo}
function New-ManagerStartInfo {
    $info = & $script:originalManagerStartInfo
    # CODEX_HOME must keep pointing to the existing data across a cwd change,
    # including when the original process was launched with a relative value.
    $info.EnvironmentVariables['CODEX_HOME'] = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath([string]$script:installSpec.stateDirectory))
    return $info
}
function Save-LocationResult([string]$state, [string]$message, [string]$code = '', $restart = $null, [string]$detail = '') {
    $result = @{schemaVersion=1; moveId=$script:installSpec.moveId; state=$state; message=$message; code=$code; detail=$detail;
        restart=$restart; helperPid=$PID; helperStartFileTime=[string](Get-Process -Id $PID).StartTime.ToUniversalTime().ToFileTimeUtc();
        at=[DateTime]::UtcNow.ToString('o'); shortcuts=$script:shortcutResult; startedAt=$script:restartStartedAt}
    $temporary = Join-Path $script:helperRoot ('.result-' + [Guid]::NewGuid().ToString('N') + '.json')
    [IO.File]::WriteAllText($temporary, ($result | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))
    if ([IO.File]::Exists($script:resultFile)) { [IO.File]::Replace($temporary, $script:resultFile, [NullString]::Value) }
    else { [IO.File]::Move($temporary, $script:resultFile) }
}
function Get-SourceProcess {
    $process = Get-Process -Id ([int]$script:installSpec.sourcePid) -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    if ($process.StartTime.ToUniversalTime().ToFileTimeUtc() -ne [long]$script:installSpec.sourceStartFileTime -or
        $process.MainModule.FileName -ine $script:installSpec.source) { throw 'Source manager identity changed.' }
    return $process
}
function Assert-LocationTarget {
    $target = [IO.Path]::GetFullPath([string]$script:installSpec.target)
    $source = [IO.Path]::GetFullPath([string]$script:installSpec.source)
    $directory = [IO.Path]::GetDirectoryName($target)
    if ($target -cne [string]$script:installSpec.target -or $source -cne [string]$script:installSpec.source -or
        $target.StartsWith('\\') -or $source.StartsWith('\\') -or [IO.Path]::GetFileName($target) -cne 'AgentManager.exe' -or
        [IO.Path]::GetExtension($source) -ine '.exe' -or $directory -ieq [IO.Path]::GetDirectoryName($source) -or
        $directory.TrimEnd('\') -ieq [IO.Path]::GetPathRoot($directory).TrimEnd('\')) { throw 'Unsafe relocation path.' }
    Assert-Regular $directory
    foreach ($blocked in @($env:SystemRoot, $env:WINDIR, $env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:ProgramW6432, $env:ProgramData, $script:installSpec.stateDirectory)) {
        if ($blocked -and ($directory -ieq $blocked -or $directory.StartsWith(([IO.Path]::GetFullPath($blocked).TrimEnd('\') + '\'), [StringComparison]::OrdinalIgnoreCase))) {
            throw 'Relocation into a protected directory is not allowed.'
        }
    }
}
function Initialize-ExactDelete {
    if ('AgentManagerLocation.ExactFile' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Text;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using Microsoft.Win32.SafeHandles;
namespace AgentManagerLocation {
    public static class ExactFile {
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        static extern SafeFileHandle CreateFile(string path, uint access, uint share, IntPtr security, uint mode, uint flags, IntPtr template);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        static extern uint GetFinalPathNameByHandle(SafeFileHandle handle, StringBuilder buffer, uint length, uint flags);
        [DllImport("kernel32.dll", SetLastError=true)]
        static extern bool SetFileInformationByHandle(SafeFileHandle handle, int type, ref byte info, uint size);
        public static void DeleteVerified(string path, string digest, long size) {
            using (SafeFileHandle handle = CreateFile(path, 0x80010000, 1, IntPtr.Zero, 3, 0x00200000, IntPtr.Zero)) {
                if (handle.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
                StringBuilder actual = new StringBuilder(32768);
                uint length = GetFinalPathNameByHandle(handle, actual, (uint)actual.Capacity, 0);
                if (length == 0 || length >= actual.Capacity || !String.Equals(actual.ToString(), @"\\?\" + Path.GetFullPath(path), StringComparison.OrdinalIgnoreCase))
                    throw new IOException("Old executable resolved to an unexpected path.");
                using (FileStream stream = new FileStream(handle, FileAccess.Read))
                using (SHA256 hash = SHA256.Create()) {
                    if (stream.Length != size || BitConverter.ToString(hash.ComputeHash(stream)).Replace("-", "").ToLowerInvariant() != digest)
                        throw new IOException("Old executable bytes changed; file retained.");
                    byte delete = 1;
                    if (!SetFileInformationByHandle(handle, 4, ref delete, 1)) throw new Win32Exception(Marshal.GetLastWin32Error());
                }
            }
        }
    }
}
'@
}
function Get-OldLocationProcesses {
    foreach ($candidate in @(Get-Process -Name ([IO.Path]::GetFileNameWithoutExtension($script:installSpec.source)) -ErrorAction SilentlyContinue)) {
        $path = $candidate.MainModule.FileName
        if (-not $path) { throw 'Cannot verify an executable process path.' }
        if ($path -ieq $script:installSpec.source) { $candidate }
    }
}
function Remove-VerifiedOldLocation([int]$timeoutSeconds = 15) {
    if (-not [IO.File]::Exists($script:installSpec.source)) { return }
    Initialize-ExactDelete
    $deadline = [DateTime]::UtcNow.AddSeconds($timeoutSeconds)
    do {
        Assert-Regular $script:installSpec.source
        $parentPresent = $false
        foreach ($process in @(Get-OldLocationProcesses)) {
            if ($process.Id -eq [int]$script:installSpec.sourceParentPid -and
                $process.StartTime.ToUniversalTime().ToFileTimeUtc() -le [long]$script:installSpec.sourceStartFileTime) { $parentPresent = $true }
            else { throw 'Another manager is still running at the old location; original retained.' }
        }
        if (-not $parentPresent) {
            try {
                [AgentManagerLocation.ExactFile]::DeleteVerified($script:installSpec.source, $script:installSpec.sha256, $script:installSpec.size)
                return
            } catch {
                $cause = $_.Exception
                while ($cause.InnerException) { $cause = $cause.InnerException }
                if ($cause -isnot [ComponentModel.Win32Exception] -or $cause.NativeErrorCode -notin @(5, 32, 33)) { throw }
            }
        }
        if ([DateTime]::UtcNow -ge $deadline) { throw 'Old executable is still locked; original retained.' }
        Start-Sleep -Milliseconds 200
    } while ($true)
}
function Confirm-LocationReady([DateTime]$started) {
    Assert-Hash $script:installTarget $script:installSpec.sha256 $script:installSpec.size
    $ready = Test-ManagerReady $started
    if ($null -eq $ready) { throw 'New manager window and service identity are not verified.' }
    return $ready
}
function Invoke-LocationMove([switch]$Recovery) {
    $script:resultFile = Join-Path $script:helperRoot 'result.json'
    $script:installSpec = $null; $script:shortcutResult = $null; $script:restartStartedAt = $null
    $script:launchedProcess = $null; $script:readinessReason = $null; $script:readinessTimedOut = $false
    $sibling = $null; $copied = $false; $cleanShutdownConfirmed = $false; $ready = $null; $mutex = $null; $locked = $false
    try {
        $script:installSpec = Read-Json (Join-Path $script:helperRoot 'location.json')
        $spec = $script:installSpec
        if ($spec.schemaVersion -ne 1 -or [string]$spec.moveId -notmatch '^[0-9a-f]{48}$' -or $spec.installId -cne $spec.moveId -or
            [string]$spec.sourceNonce -notmatch '^[A-Za-z0-9_-]{16,128}$' -or [int]$spec.sourcePid -le 0 -or
            [long]$spec.sourceStartFileTime -le 0 -or [string]$spec.sha256 -notmatch '^[0-9a-f]{64}$' -or [long]$spec.size -le 0) { throw 'Invalid relocation metadata.' }
        $mutex = New-Object Threading.Mutex($false, ('Local\AgentManagerLocation-' + $spec.moveId))
        try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
        if (-not $locked) { return }
        Assert-LocationTarget
        $script:installTarget = [string]$spec.target
        if ($Recovery) {
            $previous = Read-Json $script:resultFile
            if ($previous.state -notin @('copying','verifying_startup','cleanup_pending')) { return }
            $started = [DateTimeOffset]::Parse([string]$spec.preparedAt).UtcDateTime
            $copied = $true
            Assert-CleanShutdown
            if ($null -ne (Get-SourceProcess)) { throw 'Old manager is running; cleanup cancelled.' }
            $ready = Confirm-LocationReady $started
        } else {
            if (Test-Path -LiteralPath $script:installTarget) { throw 'Destination AgentManager.exe already exists.' }
            Assert-Hash $spec.source $spec.sha256 $spec.size
            if ($null -eq (Get-SourceProcess)) { throw 'Old manager already exited before relocation readiness.' }
            $runtime = Read-Json ([string]$spec.runtimeFile)
            if ($runtime.pid -ne $spec.sourcePid -or $runtime.runtimeNonce -cne $spec.sourceNonce) { throw 'Source runtime identity does not match.' }
            Save-LocationResult 'waiting_for_exit' '正在等待配置恢复和管理器退出。' 'location_waiting'
            Wait-ForSourceExit
            Assert-CleanShutdown
            $cleanShutdownConfirmed = $true
            Assert-LocationTarget
            Save-LocationResult 'copying' '正在复制程序到新目录。' 'location_copying'
            $sibling = Join-Path ([IO.Path]::GetDirectoryName($script:installTarget)) ('.agent-manager-move-' + $spec.moveId + '.exe')
            Assert-Hash $spec.source $spec.sha256 $spec.size
            $sourceLock = [IO.File]::Open($spec.source, 'Open', 'Read', 'Read')
            try {
                Assert-Hash $spec.source $spec.sha256 $spec.size
                $destination = [IO.File]::Open($sibling, 'CreateNew', 'Write', 'None')
                try { $sourceLock.CopyTo($destination); $destination.Flush($true) } finally { $destination.Dispose() }
            } finally { $sourceLock.Dispose() }
            Assert-Hash $sibling $spec.sha256 $spec.size
            Assert-LocationTarget
            [IO.File]::Move($sibling, $script:installTarget)
            $sibling = $null; $copied = $true
            Save-LocationResult 'verifying_startup' '程序已复制，正在确认新窗口和服务就绪。' 'verifying_startup'
            $ready = Start-VerifiedManager
            $ready = Confirm-LocationReady ([DateTimeOffset]::Parse($script:restartStartedAt).UtcDateTime)
        }
        Save-LocationResult 'cleanup_pending' '新程序已就绪，正在清理旧程序。' 'cleanup_pending' $ready
        try { $script:shortcutResult = Update-OwnedDesktopShortcuts $spec.source $script:installTarget }
        catch { $script:shortcutResult = @{updated=0; warning='桌面快捷方式未能自动更新，可在设置中重新创建。'} }
        [void](Confirm-LocationReady ([DateTimeOffset]::Parse([string]$spec.preparedAt).UtcDateTime))
        Remove-VerifiedOldLocation
        Save-LocationResult 'complete' '程序位置已更改，旧 EXE 已清理。' 'location_complete' $ready
    } catch {
        if (-not $locked) { throw }
        $failure = $_.Exception.Message
        if ($cleanShutdownConfirmed -and -not $Recovery -and $null -eq $ready) {
            try {
                # Copying can fail after the old window has already closed.
                # Restart only after proven restoration, even when no new EXE
                # was committed. Never duplicate a live or unknown instance.
                if (@(Get-TargetProcesses).Count -eq 0 -and @(Get-OldLocationProcesses).Count -eq 0 -and
                    ($null -eq $script:launchedProcess -or $script:launchedProcess.HasExited)) {
                    Assert-CleanShutdown
                    Assert-Hash $spec.source $spec.sha256 $spec.size
                    $script:installTarget = [string]$spec.source
                    $restored = Start-VerifiedManager
                    Assert-Hash $spec.source $spec.sha256 $spec.size
                    $rollbackMessage = '程序迁移未完成，已重新打开原位置程序。'
                    if ($copied) { $rollbackMessage = '新位置启动失败，已重新打开原位置程序；新目录副本已保留。' }
                    Save-LocationResult 'failed' $rollbackMessage 'location_rollback_ready' $restored $failure
                    return
                }
            } catch { $failure += ' Original restart: ' + $_.Exception.Message }
            finally { $script:installTarget = [string]$spec.target }
        }
        if ($copied) {
            Save-LocationResult 'cleanup_pending' '新位置已保留，旧 EXE 暂未清理；确认启动后会继续处理。' $(if ($ready) { 'cleanup_pending' } else { 'startup_unverified' }) $ready $failure
        } else { Save-LocationResult 'failed' '程序迁移未完成，旧 EXE 已保留。' 'location_failed' $null $failure }
    } finally {
        if ($sibling -and [IO.File]::Exists($sibling)) {
            try { Assert-Regular $sibling; Initialize-ExactDelete; [AgentManagerLocation.ExactFile]::DeleteVerified($sibling, $script:installSpec.sha256, $script:installSpec.size) } catch { }
        }
        if ($locked) { $mutex.ReleaseMutex() }
        if ($mutex) { $mutex.Dispose() }
    }
}
if ($MyInvocation.InvocationName -ne '.') { Invoke-LocationMove -Recovery:$RecoverOnly }
'''
