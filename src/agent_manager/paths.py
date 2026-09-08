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


def codex_home(environ=None, home=None) -> Path:
    values = os.environ if environ is None else environ
    base = Path.home() if home is None else Path(home)
    override = str(values.get("CODEX_HOME") or "").strip()
    path = Path(override).expanduser() if override else base / ".codex"
    # A relative environment override must not depend on a launcher's working directory.
    if not path.is_absolute():
        path = base / path
    return path.resolve()
