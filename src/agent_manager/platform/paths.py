"""Cross-platform path helpers used by runtime and process discovery."""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path


_WINDOWS_STORE_PACKAGE = re.compile(r"^microsoft\.windowsstore(?:_|$)", re.IGNORECASE)


def is_windows_store_path(path: str | Path) -> bool:
    """Return whether *path* belongs to a protected Windows Store package."""

    try:
        raw = str(path)
    except Exception:
        return False
    normalized = raw.replace("/", "\\")
    parts = [part for part in normalized.split("\\") if part]
    return any(part.casefold() == "windowsapps" or _WINDOWS_STORE_PACKAGE.match(part) for part in parts)


def normalize_path(path: str | Path) -> Path:
    """Expand a user path and resolve it without requiring it to exist."""

    return Path(path).expanduser().resolve(strict=False)


def ensure_writable_directory(path: str | Path) -> bool:
    """Create a directory if needed and verify that it accepts a private file."""

    directory = normalize_path(path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir():
            return False
        fd, name = tempfile.mkstemp(prefix=".agent-manager-write-test-", dir=str(directory))
        os.close(fd)
        try:
            Path(name).unlink(missing_ok=True)
        except OSError:
            return False
        return True
    except (OSError, ValueError):
        return False
