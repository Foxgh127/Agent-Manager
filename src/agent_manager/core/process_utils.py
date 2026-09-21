"""Cross-platform process discovery and lifecycle helpers.

The original fix plan used PowerShell for every operation.  This module keeps
that behaviour on Windows, where the application primarily targets Codex
Desktop, and uses the host process APIs on POSIX systems.  Commands are built
from fixed process-name constants and validated integer PIDs are passed as
individual subprocess arguments; no shell interpolation is used for a caller
provided PID.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import errno
import json
import math
import ntpath
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Any, Iterable, List, Optional, Set


class ProcessCheckMode(Enum):
    FAST = "fast"
    THOROUGH = "thorough"
    VALIDATE = "validate"


@dataclass
class ProcessInfo:
    pid: int
    name: str
    exe_path: str
    command_line: str
    parent_pid: Optional[int] = None


# Include extensionless names for POSIX ``ps`` output.  Matching is exact on
# the executable basename so a process such as ``codex-helper`` is ignored.
_CODEX_PROCESS_NAMES = frozenset(
    {
        "codex",
        "codex.exe",
        "codex-cli",
        "codex-cli.exe",
        "codex-desktop",
        "codex-desktop.exe",
    }
)
_CODEX_PROCESS_STEMS = frozenset(
    name[:-4] if name.endswith(".exe") else name for name in _CODEX_PROCESS_NAMES
)
_MAX_PID = (1 << 63) - 1


def validate_pid(pid: int) -> int:
    """Return ``pid`` as an int after rejecting unsafe values.

    PIDs are intentionally strict integers.  Accepting arbitrary strings
    would make it too easy for a future shell based caller to turn a process
    operation into command injection.  ``bool`` is rejected even though it is
    an ``int`` subclass.
    """

    if isinstance(pid, bool) or not isinstance(pid, int):
        raise ValueError("pid must be a positive integer")
    if pid < 1 or pid > _MAX_PID:
        raise ValueError("pid must be a positive integer")
    return pid


def _validated_timeout(value: int | float, *, allow_zero: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout must be a finite non-negative number")
    value = float(value)
    minimum = 0.0 if allow_zero else 0.000001
    if not math.isfinite(value) or value < minimum:
        if allow_zero:
            raise ValueError("timeout must be a finite non-negative number")
        raise ValueError("timeout must be a positive finite number")
    return value


def _validated_retry_count(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("retry_count must be a non-negative integer")
    return value


def _validated_delay(value: int | float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("retry_delay must be a finite non-negative number")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("retry_delay must be a finite non-negative number")
    return value


def _coerce_mode(mode: ProcessCheckMode | str) -> ProcessCheckMode:
    if isinstance(mode, ProcessCheckMode):
        return mode
    if isinstance(mode, str):
        try:
            return ProcessCheckMode(mode.lower())
        except ValueError as exc:
            raise ValueError(f"unknown process check mode: {mode!r}") from exc
    raise ValueError("mode must be a ProcessCheckMode or mode string")


def _run_powershell_command(command: str, timeout: int | float = 10) -> str:
    """Run an internal PowerShell query on Windows.

    The helper is kept as a separate function so callers and tests can stub a
    platform query without creating or killing real processes.  It is a no-op
    on POSIX hosts.
    """

    if os.name != "nt":
        return ""
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string")
    timeout_value = _validated_timeout(timeout, allow_zero=False)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=timeout_value,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if isinstance(result.stdout, str) else ""


def _run_posix_process_query(timeout: int | float = 10) -> str:
    """Return a stable, parseable process listing on POSIX hosts."""

    timeout_value = _validated_timeout(timeout, allow_zero=False)
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,comm=,args="],
            capture_output=True,
            text=True,
            timeout=timeout_value,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if isinstance(result.stdout, str) else ""


def _powershell_process_query(mode: ProcessCheckMode) -> str:
    names = ",".join(
        "'" + name.replace("'", "''") + "'"
        for name in sorted(name for name in _CODEX_PROCESS_NAMES if name.endswith(".exe"))
    )
    if mode is ProcessCheckMode.FAST:
        return (
            "$names=@(" + names + "); "
            "Get-Process | Where-Object { $_.ProcessName -in "
            "@('codex','codex-cli','codex-desktop') } | "
            "Select-Object Id,ProcessName,Path | ConvertTo-Json -Compress"
        )
    return (
        "$names=@(" + names + "); "
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -in $names } | "
        "Select-Object ProcessId,Name,ExecutablePath,CommandLine,ParentProcessId | "
        "ConvertTo-Json -Compress"
    )


def _safe_int(value: Any, *, minimum: int = 1) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        try:
            parsed = int(value.strip(), 10)
        except ValueError:
            return None
    else:
        return None
    return parsed if minimum <= parsed <= _MAX_PID else None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _basename(path: str) -> str:
    # ``ntpath`` also handles a Windows path when tests run on POSIX.
    return ntpath.basename(path.replace("/", "\\"))


def _is_codex_name(name: str) -> bool:
    candidate = _basename(name).lower()
    return candidate in _CODEX_PROCESS_NAMES or candidate.removesuffix(".exe") in _CODEX_PROCESS_STEMS


def _exe_path_for_pid(pid: int, fallback: str = "") -> str:
    if os.name == "posix":
        proc_link = Path(f"/proc/{pid}/exe")
        try:
            if proc_link.exists():
                return os.readlink(proc_link)
        except (OSError, ValueError):
            pass
    return fallback


def _validated_process_info(
    *,
    pid: Any,
    name: Any,
    exe_path: Any = "",
    command_line: Any = "",
    parent_pid: Any = None,
    require_codex: bool = True,
) -> ProcessInfo | None:
    process_id = _safe_int(pid)
    if process_id is None:
        return None
    process_name = _text(name)
    executable = _text(exe_path)
    command = _text(command_line)
    parent = _safe_int(parent_pid) if parent_pid not in (None, "", 0) else None
    if require_codex and not _is_codex_name(process_name or executable):
        return None
    if not executable:
        executable = _exe_path_for_pid(process_id, process_name)
    return ProcessInfo(
        pid=process_id,
        name=process_name,
        exe_path=executable,
        command_line=command,
        parent_pid=parent,
    )


def _parse_windows_processes(output: str, mode: ProcessCheckMode) -> list[ProcessInfo]:
    if not output or not output.strip():
        return []
    try:
        data = json.loads(output)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if data is None:
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []

    processes: list[ProcessInfo] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if mode is ProcessCheckMode.FAST:
            process = _validated_process_info(
                pid=item.get("Id"),
                name=item.get("ProcessName"),
                exe_path=item.get("Path"),
                require_codex=True,
            )
        else:
            process = _validated_process_info(
                pid=item.get("ProcessId"),
                name=item.get("Name"),
                exe_path=item.get("ExecutablePath"),
                command_line=item.get("CommandLine"),
                parent_pid=item.get("ParentProcessId"),
                require_codex=True,
            )
        if process is None:
            continue
        if mode is ProcessCheckMode.VALIDATE:
            evidence = process.exe_path or process.name
            if not _is_codex_name(evidence):
                continue
        processes.append(process)
    return processes


def _parse_posix_processes(output: str, mode: ProcessCheckMode) -> list[ProcessInfo]:
    processes: list[ProcessInfo] = []
    for line in output.splitlines():
        # The query order is pid, ppid, comm, args; command lines may contain
        # spaces, so split only the first three boundaries.
        fields = line.strip().split(None, 3)
        if len(fields) < 3:
            continue
        pid, parent_pid, name = fields[:3]
        command_line = fields[3] if len(fields) == 4 else ""
        if not _is_codex_name(name):
            continue
        process = _validated_process_info(
            pid=pid,
            name=name,
            exe_path="",
            command_line=command_line if mode is not ProcessCheckMode.FAST else "",
            parent_pid=parent_pid,
            require_codex=True,
        )
        if process is None:
            continue
        if mode is ProcessCheckMode.VALIDATE:
            evidence = process.exe_path or process.name
            if not _is_codex_name(evidence):
                continue
        processes.append(process)
    return processes


def _detect_processes_internal(
    process_names: Set[str], mode: ProcessCheckMode
) -> List[ProcessInfo]:
    """Detect processes using the current platform's native query."""

    del process_names  # The query is intentionally fixed to trusted names.
    if os.name == "nt":
        output = _run_powershell_command(_powershell_process_query(mode))
        return _parse_windows_processes(output, mode)
    # Keep the query helper patchable for callers that run the Windows query
    # in a compatibility environment (and for tests), while the normal POSIX
    # path remains shell-free and uses ``ps`` below.
    powershell_output = _run_powershell_command(_powershell_process_query(mode))
    if powershell_output and powershell_output.strip():
        return _parse_windows_processes(powershell_output, mode)
    output = _run_posix_process_query()
    return _parse_posix_processes(output, mode)


def detect_codex_processes(
    mode: ProcessCheckMode = ProcessCheckMode.THOROUGH,
    retry_count: int = 0,
    retry_delay: float = 0.5,
) -> List[ProcessInfo]:
    """Find running Codex processes, optionally retrying an empty result."""

    mode = _coerce_mode(mode)
    retry_count = _validated_retry_count(retry_count)
    retry_delay = _validated_delay(retry_delay)
    codex_names = set(_CODEX_PROCESS_NAMES)

    for attempt in range(retry_count + 1):
        if attempt > 0 and retry_delay:
            time.sleep(retry_delay)
        processes = _detect_processes_internal(codex_names, mode)
        if processes or attempt == retry_count:
            return processes
    return []


def _pid_exists(pid: int) -> bool:
    """Return whether a validated PID currently exists."""

    pid = validate_pid(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno in {errno.ESRCH, errno.ENOENT}:
            return False
        # EPERM and platform-specific access errors indicate a live process.
        if exc.errno in {errno.EPERM, errno.EACCES}:
            return True
        return False
    return True


def _linux_process_parents() -> dict[int, int]:
    """Read a best-effort PID -> parent map without invoking a shell."""

    parents: dict[int, int] = {}
    proc_root = Path("/proc")
    try:
        entries = proc_root.iterdir()
    except OSError:
        return parents
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
            # The comm field may contain ')' characters.  The fields after
            # the final ')' start with state, then ppid.
            suffix = stat.rsplit(")", 1)[-1].strip().split()
            pid = int(entry.name)
            parent = int(suffix[1])
        except (OSError, ValueError, IndexError):
            continue
        if pid > 0 and parent >= 0:
            parents[pid] = parent
    return parents


def _descendant_pids(pid: int) -> list[int]:
    parents = _linux_process_parents() if os.name == "posix" else {}
    descendants: list[int] = []
    frontier = [pid]
    while frontier:
        parent = frontier.pop()
        children = [child for child, candidate in parents.items() if candidate == parent]
        descendants.extend(children)
        frontier.extend(children)
    return descendants


def _send_signal(pid: int, signum: int) -> bool:
    try:
        os.kill(pid, signum)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    except OSError:
        return False


def _kill_posix_process_tree(pid: int, timeout: float) -> bool:
    if pid == os.getpid():
        # Never let a mistaken caller terminate the manager itself.
        return False
    if not _pid_exists(pid):
        return True
    targets = _descendant_pids(pid)
    # Terminate leaves first, then the requested parent.  A later process
    # snapshot catches children created during the first traversal.
    for child in reversed(targets):
        _send_signal(child, signal.SIGTERM)
    _send_signal(pid, signal.SIGTERM)

    deadline = time.monotonic() + timeout
    while _pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    if _pid_exists(pid):
        for child in reversed(_descendant_pids(pid)):
            _send_signal(child, signal.SIGKILL)
        _send_signal(pid, signal.SIGKILL)
    return not _pid_exists(pid)


def _kill_windows_process_tree(pid: int, timeout: float) -> bool:
    if pid == os.getpid():
        return False
    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=max(timeout, 0.001),
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode == 0:
        return True
    # A process that exited between validation and taskkill is already in the
    # desired state.  Do not report success for an arbitrary taskkill error.
    return not _pid_exists(pid)


def kill_process_tree(pid: int, timeout: int | float = 5) -> bool:
    """Terminate a validated process and its descendants.

    Windows uses ``taskkill /T /F`` with an argument list.  POSIX walks the
    Linux ``/proc`` parent map where available and sends signals directly.
    Invalid PIDs raise ``ValueError`` before any external command or signal is
    attempted; attempting to kill the current process returns ``False``.
    """

    pid = validate_pid(pid)
    timeout = _validated_timeout(timeout)
    if os.name == "nt":
        return _kill_windows_process_tree(pid, timeout)
    return _kill_posix_process_tree(pid, timeout)


def wait_for_process_exit(pid: int, timeout: int | float = 30) -> bool:
    """Wait until a validated PID exits, without shelling out."""

    pid = validate_pid(pid)
    timeout = _validated_timeout(timeout)
    deadline = time.monotonic() + timeout
    while _pid_exists(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    return True


__all__ = [
    "ProcessCheckMode",
    "ProcessInfo",
    "detect_codex_processes",
    "kill_process_tree",
    "validate_pid",
    "wait_for_process_exit",
]
