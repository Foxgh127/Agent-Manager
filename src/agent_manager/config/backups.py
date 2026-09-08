"""Content-addressed configuration history; only known Manager files are pruned.

Legacy timestamp backups are adopted in place, so journal references stay valid.
Manual backups never expire. Recovery originals share automatic retention;
unfinished recovery journals protect the exact files they still reference.
Inspection itself is read-only.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid

import agent_manager.core as core

AUTO_LIMIT = 3
LOCK = threading.RLock()
LEGACY_AUTO = re.compile(r"config\.toml\.\d{8}-\d{6}-\d{6}\.bak\Z")
MANAGED = re.compile(r"config\.toml\.\d{19,}-[0-9a-f]{8}\.bak\Z")
ORIGINAL = re.compile(r"config\.toml\.\d{19,}-[0-9a-f]{8}\.original\Z")
RECOVERY_JOURNAL_FORMAT = "agent-manager-config-recovery-v1"


def _recovery_journal_path() -> Path:
    return core.RUNTIME_OVERLAY_FILE.parent / "config-recovery-journal.json"


def _load_recovery_journal() -> dict | None:
    path = _recovery_journal_path()
    if not directory_is_direct(path.parent):
        raise ValueError("linked journal directory")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or info.st_size > 8 * 1024 * 1024):
        raise ValueError("unsafe recovery journal")
    record = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(record, dict) or record.get("format") != RECOVERY_JOURNAL_FORMAT
            or record.get("status") not in ("active", "failed", "completed")
            or not isinstance(record.get("operationId"), str) or not record["operationId"]
            or not isinstance(record.get("configPath"), str) or not Path(record["configPath"]).is_absolute()
            or not isinstance(record.get("backupPaths"), list)
            or any(not isinstance(item, str) or not Path(item).is_absolute() for item in record["backupPaths"])):
        raise ValueError("unknown recovery journal")
    if record["status"] == "completed":
        if record["backupPaths"]:
            raise ValueError("completed journal still has pending references")
    elif Path(record["configPath"]) != core.CONFIG_FILE.absolute():
        raise ValueError("unfinished recovery belongs to another config location")
    return record


def begin_recovery(saved: Path, selected: Path | None, *, original: bytes | None = None) -> dict:
    """Protect safety copies durably before creating or replacing any config file."""
    with LOCK:
        path = _recovery_journal_path()
        if not directory_is_direct(path.parent) or path.is_symlink():
            raise core.ManagerError("恢复记录路径经过链接，已停止恢复。")
        try:
            previous = _load_recovery_journal()
        except (OSError, ValueError) as exc:
            raise core.ManagerError("已有恢复记录无法验证，已保留记录并停止恢复。") from exc
        pending = previous["backupPaths"] if previous and previous["status"] != "completed" else []
        reused = False
        # Retrying a failed transaction needs one exact original, not another
        # permanently protected copy of the same bytes on every retry.
        if original is not None and pending:
            latest = next((row for row in list_backups() if row["retention"] == "automatic"), None)
            # An intervening version requires a fresh transition, otherwise a
            # successful retry could immediately expire its old safety copy.
            if latest and latest["kind"] == "recovery" and latest["path"] in pending:
                candidate = Path(latest["path"])
                try:
                    if read_backup(candidate, expected_id=latest["id"]) == original:
                        saved, reused = candidate, True
                except (OSError, core.ManagerError):
                    pass
        record = {"format": RECOVERY_JOURNAL_FORMAT, "operationId": uuid.uuid4().hex,
                  "configPath": str(core.CONFIG_FILE.absolute()), "status": "active",
                  "ownerPid": os.getpid(), "updatedAt": time.time(),
                  "originalBackup": str(saved.absolute()), "originalReused": reused,
                  "backupPaths": list(dict.fromkeys([*pending, str(saved.absolute()),
                                                     *([str(selected.absolute())] if selected else [])]))}
        core.atomic_write_json(path, record)
        return record


def finish_recovery(record: dict, *, succeeded: bool) -> None:
    """A verified recovery resolves previous failed attempts for this config."""
    with LOCK:
        current = _load_recovery_journal()
        if current != record:
            raise core.ManagerError("恢复记录已变化，已保留现有记录和备份。")
        updated = {**record, "status": "completed" if succeeded else "failed", "updatedAt": time.time()}
        if succeeded:
            updated["backupPaths"] = []
        core.atomic_write_json(_recovery_journal_path(), updated)


def fingerprint(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def directory_is_direct(path: Path) -> bool:
    for component in (path.absolute(), *path.absolute().parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            return False
    return True


def _kind(path: Path) -> str:
    parent = path.absolute().parent
    root = core.BACKUPS_DIR.absolute()
    if parent == root and LEGACY_AUTO.fullmatch(path.name):
        return "auto"
    if parent == root / "config-auto" and MANAGED.fullmatch(path.name):
        return "auto"
    if parent == root / "config-manual" and MANAGED.fullmatch(path.name):
        return "manual"
    if parent == root / "config-recovery" and ORIGINAL.fullmatch(path.name):
        return "recovery"
    return "external"


def _allowed_parent(path: Path) -> bool:
    return path.absolute().parent in {
        core.CONFIG_FILE.absolute().parent,
        core.BACKUPS_DIR.absolute(),
        *(core.BACKUPS_DIR.absolute() / name for name in ("config-auto", "config-manual", "config-recovery")),
    }


def _identity(info) -> tuple:
    # Python's Windows lstat and CRT fstat disagree on ctime after atomic rename.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _id(path: Path, raw: bytes, info) -> str:
    value = [os.path.normcase(str(path.absolute())), *_identity(info), fingerprint(raw)]
    return fingerprint(json.dumps(value, separators=(",", ":")).encode())


@contextmanager
def _open_checked(path: Path, *, deleting: bool = False):
    """Hold a stable file; Windows denies write/rename and deletes by this handle."""
    path = path.absolute()
    if not _allowed_parent(path) or not directory_is_direct(path.parent):
        raise core.ManagerError("备份目录经过链接或超出允许范围，已停止操作。")
    before = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(before.st_mode) or getattr(before, "st_file_attributes", 0) & 0x400:
        raise core.ManagerError("备份不是直接普通文件，已停止操作。")
    if deleting and before.st_nlink != 1:
        raise core.ManagerError("备份存在硬链接，已保留文件。")
    handle = None
    if os.name == "nt":
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        handle = kernel.CreateFileW(str(path), 0x80000000 | (0x10000 if deleting else 0),
                                    1, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "备份正在使用或不可访问")
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    else:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if _identity(info) != _identity(before) or not stat.S_ISREG(info.st_mode):
            raise core.ManagerError("备份在检查后发生变化，已停止操作。")
        if os.name == "nt":
            kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
            value = ctypes.create_unicode_buffer(32768)
            count = kernel.GetFinalPathNameByHandleW(handle, value, len(value), 0)
            resolved = value.value
            if resolved.startswith("\\\\?\\UNC\\"):
                resolved = "\\\\" + resolved[8:]
            elif resolved.startswith("\\\\?\\"):
                resolved = resolved[4:]
            if not count or count >= len(value) or os.path.normcase(resolved) != os.path.normcase(str(path)):
                raise core.ManagerError("备份路径在检查后发生变化，已停止操作。")
        raw = stream.read(core.CODEX_CONFIG_MAX_BYTES + 1)
        if len(raw) > core.CODEX_CONFIG_MAX_BYTES:
            raise core.ManagerError("配置文件过大，已停止自动处理。")
        if _identity(os.fstat(stream.fileno())) != _identity(info):
            raise core.ManagerError("备份在检查后发生变化，已停止操作。")
        yield stream, raw, info, handle


def read_backup(path: Path, *, expected_id: str | None = None) -> bytes:
    with _open_checked(path) as (_stream, raw, info, _handle):
        if expected_id and _id(path, raw, info) != expected_id:
            raise core.ManagerError("备份在检查后发生变化，未恢复。")
        return raw


def _references() -> tuple[set[str], set[str], bool]:
    """Embedded overlay bytes are independent, but preserve any explicit references."""
    paths, hashes = set(), set()
    uncertain = False
    sources = {core.RUNTIME_OVERLAY_FILE, _recovery_journal_path()}
    # Only live control journals, never archived auth/session databases or secrets.
    state = core.RUNTIME_OVERLAY_FILE.parent
    if directory_is_direct(state) and state.is_dir():
        for pattern in ("*journal*.json", "*recovery*.json", "*restore*.json"):
            sources.update(state.glob(pattern))

    def visit(value, key=""):
        if isinstance(value, dict):
            for name, item in value.items():
                visit(item, str(name))
        elif isinstance(value, list):
            for item in value:
                visit(item, key)
        elif isinstance(value, str):
            if "backup" in key.lower() or "originalpath" in key.lower():
                candidate = Path(value)
                if candidate.is_absolute():
                    paths.add(os.path.normcase(str(candidate.absolute())))
                elif candidate.suffix in {".bak", ".backup", ".original"}:
                    for root in (core.CODEX_HOME, state, core.BACKUPS_DIR):
                        paths.add(os.path.normcase(os.path.abspath(root / candidate)))
            if key in {"backupFingerprint", "backupHash", "backupId"}:
                hashes.add(value)

    for source in sources:
        try:
            if source == _recovery_journal_path():
                document = _load_recovery_journal()
                if document and document["status"] != "completed":
                    visit(document)
                continue
            if not source.exists():
                continue
            if not directory_is_direct(source.parent) or source.is_symlink() or source.stat().st_size > 8 * 1024 * 1024:
                uncertain = True
                continue
            document = json.loads(source.read_text(encoding="utf-8"))
            visit(document)
        except (OSError, ValueError):
            uncertain = True
    return paths, hashes, uncertain


def _is_referenced(path: Path, row_id: str, digest: str, references: set, hashes: set) -> bool:
    return row_id in hashes or digest in hashes or any(
        os.path.normcase(str(part)) in references for part in (path.absolute(), *path.absolute().parents)
    )


def list_backups() -> list[dict]:
    import agent_manager.config.recovery as recovery
    with LOCK:
        references, hashes, uncertain = _references()
        rows = []
        roots = {core.CONFIG_FILE.parent, core.BACKUPS_DIR,
                 *(core.BACKUPS_DIR / name for name in ("config-auto", "config-manual", "config-recovery"))}
        for directory in roots:
            if not directory_is_direct(directory) or not directory.is_dir():
                continue
            for path in directory.glob("config.toml*"):
                if path.suffix not in {".bak", ".backup", ".original"}:
                    continue
                try:
                    with _open_checked(path) as (_stream, raw, info, _handle):
                        kind = _kind(path)
                        digest = fingerprint(raw)
                        row_id = _id(path, raw, info)
                        reason = "外部备份由原工具管理" if kind == "external" else (
                            "备份存在硬链接" if info.st_nlink != 1 else
                            "恢复记录暂不可验证" if uncertain else
                            "活动恢复记录正在引用此备份" if _is_referenced(path, row_id, digest, references, hashes) else "")
                        rows.append({"id": row_id, "name": path.name,
                                     "path": str(path.absolute()), "size": len(raw),
                                     "modifiedAt": info.st_mtime, "fingerprint": digest,
                                     "kind": kind, "canRestore": recovery._valid_text(raw) is not None,
                                     "retention": "automatic" if kind in {"auto", "recovery"} else kind,
                                     "canDelete": not reason, "protectedReason": reason})
                except (OSError, core.ManagerError):
                    continue
        return sorted(rows, key=lambda item: (item["modifiedAt"], item["name"]), reverse=True)


def _delete_checked(row: dict) -> None:
    path = Path(row["path"])
    if _kind(path) == "external":
        raise core.ManagerError("外部备份由原工具管理，未删除。")
    with _open_checked(path, deleting=True) as (_stream, raw, info, handle):
        if _id(path, raw, info) != row["id"]:
            raise core.ManagerError("备份在检查后发生变化，未删除。")
        references, hashes, uncertain = _references()
        if uncertain or _is_referenced(path, row["id"], fingerprint(raw), references, hashes):
            raise core.ManagerError("恢复记录正在引用此备份或暂不可验证，未删除。")
        if os.name == "nt":
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            kernel.SetFileInformationByHandle.restype = wintypes.BOOL
            disposition = wintypes.BOOL(True)
            if not kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
                raise OSError(ctypes.get_last_error(), "删除备份失败")
        else:
            # Quarantine then revalidate before unlink: a swapped entry is never deleted.
            with _posix_parent(path.parent) as parent_fd:
                quarantine = ".config-delete-" + uuid.uuid4().hex
                os.rename(path.name, quarantine, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                try:
                    captured = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
                    if (captured.st_dev, captured.st_ino) != (info.st_dev, info.st_ino):
                        raise core.ManagerError("备份在检查后发生变化，未删除。")
                    os.unlink(quarantine, dir_fd=parent_fd)
                except Exception:
                    try:
                        os.link(quarantine, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
                        os.unlink(quarantine, dir_fd=parent_fd)
                    except OSError:
                        pass  # Preserve the quarantined file if another entry now owns the name.
                    raise


@contextmanager
def _posix_parent(path: Path):
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def prune_automatic(*, keep_paths=()) -> dict:
    """Adopt only exact legacy Manager names in place; unknown files never expire."""
    with LOCK:
        retained = {os.path.normcase(str(Path(item).absolute())) for item in keep_paths}
        rows = [row for row in list_backups() if row["retention"] == "automatic"]
        seen, keep_ids = set(), set()
        for row in rows:
            if row["fingerprint"] not in seen and len(seen) < AUTO_LIMIT:
                seen.add(row["fingerprint"])
                keep_ids.add(row["id"])
        removed, protected = 0, 0
        for row in rows:
            if row["id"] in keep_ids or os.path.normcase(row["path"]) in retained:
                continue
            if not row["canDelete"]:
                protected += 1
                continue
            try:
                _delete_checked(row)
                removed += 1
            except (OSError, core.ManagerError):
                protected += 1
        return {"removed": removed, "protected": protected, "limit": AUTO_LIMIT,
                "automaticRemaining": len(rows) - removed,
                "recoveryOriginalsBefore": sum(row["kind"] == "recovery" for row in rows)}


def create_backup(raw: bytes, *, kind: str) -> dict:
    if kind not in {"auto", "manual"}:
        raise ValueError("unsupported config backup kind")
    if len(raw) > core.CODEX_CONFIG_MAX_BYTES:
        raise core.ManagerError("配置文件过大，已停止自动处理。")
    with LOCK:
        if kind == "auto":
            digest = fingerprint(raw)
            automatic = [row for row in list_backups() if row["retention"] == "automatic"]
            # A revisited older version is a new transition. Replacing its older
            # duplicate keeps the three most recent distinct states in order.
            existing = automatic[0] if automatic and automatic[0]["fingerprint"] == digest else None
            if existing:
                prune_automatic(keep_paths=[existing["path"]])
                return {"created": False, "path": existing["path"], "id": existing["id"], "kind": kind}
        root = core.BACKUPS_DIR / ("config-auto" if kind == "auto" else "config-manual")
        if not directory_is_direct(root):
            raise core.ManagerError("备份目录经过目录链接或连接点，已停止创建。")
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"config.toml.{time.time_ns()}-{uuid.uuid4().hex[:8]}.bak"
        core.atomic_write_bytes(path, raw)
        if read_backup(path) != raw:
            raise core.ManagerError("配置备份写入回验失败。")
        row = next(row for row in list_backups() if row["path"] == str(path.absolute()))
        if kind == "auto":
            prune_automatic(keep_paths=[path])
        return {"created": True, "path": str(path), "id": row["id"], "kind": kind}


def backup_current() -> Path | None:
    import agent_manager.config.recovery as recovery
    try:
        raw = recovery._read_bounded(core.CONFIG_FILE)
    except FileNotFoundError:
        return None
    return Path(create_backup(raw, kind="auto")["path"])


def delete_backup(backup_id: str) -> dict:
    if not isinstance(backup_id, str) or not re.fullmatch(r"[0-9a-f]{64}", backup_id):
        raise core.ManagerError("备份标识无效，请重新检查。")
    with LOCK:
        row = next((row for row in list_backups() if row["id"] == backup_id), None)
        if row is None:
            raise core.ManagerError("所选备份已变化或不可用，请重新检查。")
        if not row["canDelete"]:
            raise core.ManagerError(row["protectedReason"] + "，未删除。")
        _delete_checked(row)
        return {"deleted": True, "id": backup_id, "name": row["name"]}
