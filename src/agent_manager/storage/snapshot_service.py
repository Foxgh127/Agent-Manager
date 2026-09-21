"""Durable snapshot and rollback primitives.

The manager has several workflows that need a point-in-time copy before a
mutation. This module keeps the representation small and transport-neutral so
configuration, account, and storage code can share the same atomic persistence
rules without importing each other.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol


_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(slots=True)
class Snapshot:
    content_hash: str
    timestamp: float
    data: Any
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Snapshot":
        if not isinstance(value, dict):
            raise ValueError("Snapshot must be an object")
        content_hash = str(value.get("content_hash") or "").strip()
        # Keep compatibility with older callers that used a short logical
        # fingerprint (for example ``"switch-1"``) while bounding the value
        # persisted to disk. Snapshots created by this service still use a
        # SHA-256 digest.
        if not content_hash or len(content_hash) > 256:
            raise ValueError("Snapshot content_hash is invalid")
        try:
            timestamp = float(value.get("timestamp"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Snapshot timestamp is invalid") from exc
        metadata = value.get("metadata")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise ValueError("Snapshot metadata must be an object")
        return cls(content_hash, timestamp, copy.deepcopy(value.get("data")), copy.deepcopy(metadata))


class SnapshotProvider(Protocol):
    def capture(self) -> Any: ...
    def restore(self, data: Any) -> None: ...


class SnapshotService:
    """Create, persist, restore, and expire named snapshots safely."""

    def __init__(self, storage_dir: str | Path):
        self.storage_dir = Path(storage_dir).expanduser()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @staticmethod
    def _snapshot_name(name: str) -> str:
        candidate = str(name or "").strip()
        if not _NAME_PATTERN.fullmatch(candidate) or candidate in {".", ".."}:
            raise ValueError("Snapshot name must be a simple file name")
        return candidate

    def _path(self, name: str) -> Path:
        return self.storage_dir / f"{self._snapshot_name(name)}.json"

    def create_snapshot(
        self,
        provider: SnapshotProvider,
        metadata: dict[str, Any] | None = None,
    ) -> Snapshot:
        data = copy.deepcopy(provider.capture())
        content = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return Snapshot(
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            timestamp=time.time(),
            data=data,
            metadata=copy.deepcopy(metadata) if isinstance(metadata, dict) else {},
        )

    def save_snapshot(self, snapshot: Snapshot, name: str) -> Path:
        if not isinstance(snapshot, Snapshot):
            raise TypeError("snapshot must be a Snapshot")
        target = self._path(name)
        payload = json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=False) + "\n"
        with self._lock:
            fd, temp_name = tempfile.mkstemp(prefix=f".{target.stem}.", suffix=".tmp", dir=str(self.storage_dir))
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, target)
            finally:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
        return target

    def load_snapshot(self, name: str) -> Snapshot | None:
        target = self._path(name)
        try:
            with self._lock, target.open("r", encoding="utf-8") as stream:
                return Snapshot.from_dict(json.load(stream))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Unable to load snapshot {name!r}: {exc}") from exc

    def restore_snapshot(self, snapshot: Snapshot, provider: SnapshotProvider) -> None:
        if not isinstance(snapshot, Snapshot):
            raise TypeError("snapshot must be a Snapshot")
        provider.restore(copy.deepcopy(snapshot.data))

    def list_snapshots(self) -> list[str]:
        with self._lock:
            return sorted(path.stem for path in self.storage_dir.glob("*.json") if path.is_file())

    def delete_snapshot(self, name: str) -> bool:
        target = self._path(name)
        try:
            target.unlink()
        except FileNotFoundError:
            return False
        return True

    def cleanup_old_snapshots(self, keep_count: int = 3) -> list[str]:
        keep = max(0, int(keep_count))
        entries: list[tuple[float, Path]] = []
        for name in self.list_snapshots():
            path = self._path(name)
            try:
                with path.open("r", encoding="utf-8") as stream:
                    timestamp = float(json.load(stream).get("timestamp", 0))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                timestamp = 0.0
            entries.append((timestamp, path))
        entries.sort(key=lambda item: (item[0], item[1].name), reverse=True)
        removed: list[str] = []
        for _, path in entries[keep:]:
            try:
                path.unlink()
                removed.append(path.stem)
            except FileNotFoundError:
                pass
        return removed


class ConfigSnapshotProvider:
    """Snapshot provider for a TOML configuration file."""

    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path)

    def capture(self) -> dict[str, Any]:
        import tomllib

        with self.config_path.open("rb") as stream:
            return tomllib.load(stream)

    def restore(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise ValueError("Configuration snapshot must be an object")
        import tomlkit

        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        document = tomlkit.dumps(data)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.config_path.name}.", suffix=".tmp", dir=str(self.config_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(document)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.config_path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


class AccountSnapshotProvider:
    """Snapshot provider backed by a mutable account mapping."""

    def __init__(self, accounts_data: dict[str, Any]):
        self.accounts_data = accounts_data

    def capture(self) -> dict[str, Any]:
        return copy.deepcopy(self.accounts_data)

    def restore(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise ValueError("Account snapshot must be an object")
        self.accounts_data.clear()
        self.accounts_data.update(copy.deepcopy(data))
