"""Path utilities and registry handling"""
from __future__ import annotations
from agent_manager import core as _core


def _split_runtime_path(value: str | None) -> list[str]:
    return [item.strip().strip('"') for item in str(value or "").split(_core.os.pathsep) if item.strip().strip('"')]



def _dedupe_runtime_paths(paths: list[str | _core.Path]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        value = _core.os.path.expandvars(str(raw).strip().strip('"'))
        if not value:
            continue
        key = _core.os.path.normcase(_core.os.path.normpath(value))
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result



def _windows_registry_path_values() -> list[str]:
    if _core.os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    locations = (
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    )
    values: list[str] = []
    for root, subkey in locations:
        try:
            with winreg.OpenKey(root, subkey, 0, winreg.KEY_QUERY_VALUE) as key:
                value, _kind = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        values.append(str(value or ""))
    return values



def _refresh_windows_process_path(extra_directories: list[str | _core.Path] | None = None) -> list[str]:
    if _core.os.name != "nt":
        return _core._split_runtime_path(_core.os.environ.get("PATH"))
    registry_entries: list[str] = []
    for value in _core._windows_registry_path_values():
        registry_entries.extend(_core._split_runtime_path(value))
    merged = _core._dedupe_runtime_paths(
        [*(extra_directories or []), *registry_entries, *_core._split_runtime_path(_core.os.environ.get("PATH"))]
    )
    _core.os.environ["PATH"] = _core.os.pathsep.join(merged)
    return merged
