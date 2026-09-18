"""Installation-independent resource and user-data paths."""
from __future__ import annotations
import os
from pathlib import Path
import sys


def resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "agent_manager" / "resources"
    return Path(__file__).resolve().parent / "resources"


def development_root() -> Path | None:
    candidate = Path(__file__).resolve().parents[2]
    return candidate if (candidate / "pyproject.toml").is_file() and (candidate / "frontend").is_dir() else None


def gui_directory() -> Path:
    bundled = resource_root() / "ui"
    if (bundled / "index.html").is_file():
        return bundled
    source = development_root()
    return source / "frontend" / "dist" if source else bundled


def _env_value(values, name: str) -> str:
    """Read an environment key case-insensitively across Windows/test mappings."""
    if hasattr(values, "get"):
        direct = values.get(name)
        if direct is not None and str(direct).strip():
            return str(direct).strip()
    if hasattr(values, "items"):
        wanted = name.casefold()
        for key, value in values.items():
            if str(key).casefold() == wanted and value is not None and str(value).strip():
                return str(value).strip()
    return ""


def user_home(environ=None) -> Path:
    """Resolve the interactive user's profile without following an elevated token."""
    values = os.environ if environ is None else environ
    raw_home = _env_value(values, "USERPROFILE") or _env_value(values, "HOME")
    if raw_home:
        try:
            return Path(raw_home).expanduser().resolve()
        except (OSError, RuntimeError):
            return Path(raw_home).expanduser().absolute()
    return Path.home()


def codex_home(environ=None, home=None) -> Path:
    values = os.environ if environ is None else environ
    if home is None:
        # ``Path.home()`` can follow the elevated token on Windows. Prefer the
        # profile explicitly inherited by the launcher so an administrator
        # launch does not silently switch to a different user's Codex data.
        base = user_home(values)
    else:
        base = Path(home)
    override = _env_value(values, "CODEX_HOME")
    # ``expandvars`` reads the process environment, which may differ from a
    # supplied mapping in tests, portable launchers, or an elevated child.
    for key, value in (values.items() if hasattr(values, "items") else ()):
        if isinstance(key, str) and value is not None:
            override = override.replace(f"%{key}%", str(value))
    override = os.path.expandvars(override)
    if override == "~" or override.startswith(("~/", "~\\")):
        suffix = override[1:].lstrip("/\\")
        path = base / suffix if suffix else base
    else:
        path = Path(override).expanduser() if override else base / ".codex"
    # A relative environment override must not depend on a launcher's working directory.
    if not path.is_absolute():
        path = base / path
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path.absolute()


def data_path_diagnostics(codex_path: Path, state_path: Path, environ=None) -> dict:
    """Return non-secret path/permission hints for cross-machine diagnosis."""
    values = os.environ if environ is None else environ
    raw_home = _env_value(values, "USERPROFILE") or _env_value(values, "HOME")
    home_source = "USERPROFILE" if _env_value(values, "USERPROFILE") else "HOME" if _env_value(values, "HOME") else "path_home"

    def inspect(path: Path) -> dict:
        candidate = Path(path)
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError):
            resolved = candidate.absolute()
        protected_parts = {part.casefold() for part in resolved.parts}
        protected = bool(protected_parts & {"windowsapps", "program files", "program files (x86)"})
        try:
            exists = resolved.exists()
            directory = resolved.is_dir()
            writable = bool(directory and os.access(str(resolved), os.W_OK))
        except OSError:
            exists = directory = writable = False
        nearest = resolved if directory else resolved.parent
        while nearest != nearest.parent and not nearest.exists():
            nearest = nearest.parent
        try:
            parent_writable = bool(nearest.is_dir() and os.access(str(nearest), os.W_OK))
        except OSError:
            parent_writable = False
        return {
            "path": str(resolved),
            "exists": exists,
            "directory": directory,
            "writable": writable,
            "parentWritable": parent_writable,
            "nearestExistingDirectory": str(nearest),
            "protectedLocation": protected,
        }

    return {
        "schemaVersion": 1,
        "homeSource": home_source,
        "profileExplicit": bool(raw_home),
        "codexHomeSource": "CODEX_HOME" if _env_value(values, "CODEX_HOME") else "default_user_profile",
        "codexHome": inspect(codex_path),
        "stateDirectory": inspect(state_path),
        "writeProbePerformed": False,
        "note": "权限仅依据目录存在性和当前用户可写标记；未写入探测文件。",
    }
