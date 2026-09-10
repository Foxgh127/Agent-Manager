"""Receipt-scoped update garbage collection; never discover EXEs by glob.

The local HMAC binds prepared paths/hashes and successful readiness evidence.
It detects edits/corruption; it is not a boundary against an attacker who can
read the same user's state directory and its key. Unknown legacy files remain.
"""
from __future__ import annotations

import base64
from contextlib import ExitStack, contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets

from agent_manager.updates.service import UpdateError, _atomic_json, _read_json, _regular_path

KEY_NAME = ".cleanup-key"
RECEIPT_NAME = "cleanup-receipt.json"
KEEP_RECEIPTS = 5


def _key(root, *, create=False):
    path = _regular_path(Path(root) / KEY_NAME)
    if create:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(secrets.token_bytes(32))
                stream.flush()
                os.fsync(stream.fileno())
    with path.open("rb") as stream:
        value = stream.read(33)
    if len(value) != 32:
        raise ValueError("Invalid cleanup authentication key")
    return value


def _seal(value, key):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {"payload": base64.b64encode(raw).decode("ascii"),
            "signature": hmac.digest(key, raw, "sha256").hex()}


def _unseal(path, key):
    value = _read_json(path, 65536)
    if not isinstance(value, dict):
        raise ValueError("Missing authenticated cleanup receipt")
    raw = base64.b64decode(value["payload"], validate=True)
    if not hmac.compare_digest(hmac.digest(key, raw, "sha256").hex(), value["signature"]):
        raise ValueError("Changed cleanup receipt")
    return json.loads(raw.decode("utf-8"))


def prepare_cleanup(stage, spec, download):
    """Seal only files created/verified by this install's preparation."""
    stage = _regular_path(stage, missing=False)
    backup = Path(spec["target"]).parent / f".agent-manager-old-{spec['installId']}.exe"
    spec["backup"] = str(backup)
    value = {"schemaVersion": 1, "installId": spec["installId"], "target": spec["target"],
             "sha256": spec["sha256"], "size": spec["size"], "state": "prepared",
             "downloadDirectory": str(Path(download).parent),
             "files": [{"role": "backup", "path": str(backup), "sha256": spec["originalSha256"], "size": spec["originalSize"]},
                       {"role": "staged", "path": spec["source"], "sha256": spec["sha256"], "size": spec["size"]},
                       {"role": "download", "path": str(download), "sha256": spec["sha256"], "size": spec["size"]}]}
    _atomic_json(stage / "cleanup-authority.json", _seal(value, _key(stage.parent, create=True)))


def _validated(value, stage, *, complete):
    if (not isinstance(value, dict) or value.get("schemaVersion") != 1
            or value.get("installId") != stage.name or not re.fullmatch(r"[a-f0-9]{48}", stage.name)
            or value.get("state") != ("complete" if complete else "prepared")):
        raise ValueError("Invalid cleanup identity")
    if complete and (value.get("ready") is not True or value.get("verifiedSha256") != value.get("sha256")):
        raise ValueError("Cleanup requires readiness and target digest")
    if complete:
        result_path = _regular_path(stage / "result.json", missing=False)
        with result_path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != value.get("resultSha256"):
            raise ValueError("Installation result changed after verified success")
    target = _regular_path(value["target"])
    download_dir = _regular_path(value["downloadDirectory"])
    if target.suffix.lower() != ".exe" or len(value["files"]) != 3:
        raise ValueError("Invalid cleanup file list")
    roles = set()
    for item in value["files"]:
        path = _regular_path(item["path"])
        role = item["role"]
        if (role in roles or path == target or not Path(item["path"]).is_absolute()
                or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
                or isinstance(item["size"], bool) or not isinstance(item["size"], int) or item["size"] <= 0):
            raise ValueError("Invalid cleanup file identity")
        roles.add(role)
        if role == "backup":
            expected = target.parent / f".agent-manager-old-{stage.name}.exe"
        elif role == "staged":
            expected = stage / "AgentManager.exe"
        elif role == "download" and path.parent == download_dir and path.suffix.lower() == ".exe":
            expected = path
        else:
            raise ValueError("Invalid cleanup role")
        if path != expected:
            raise ValueError("Cleanup path escaped its recorded owner")
    return value


def confirm_cleanup(stage, spec, proof):
    """Persist an authenticated late-ready proof without changing helper history."""
    if (not isinstance(proof, dict) or proof.get("ready") is not True
            or proof.get("sha256") != spec.get("sha256")):
        return False
    try:
        stage = _regular_path(stage, missing=False)
        key = _key(stage.parent)
        value = _validated(_unseal(stage / "cleanup-authority.json", key), stage, complete=False)
        if any(value[name] != spec.get(name) for name in ("installId", "target", "sha256", "size")):
            return False
        try:
            existing = _validated(_unseal(stage / RECEIPT_NAME, key), stage, complete=True)
            if all(existing[name] == value[name] for name in ("installId", "target", "sha256", "size", "files")):
                return True
        except (UpdateError, OSError, ValueError, TypeError, KeyError):
            pass
        _atomic_json(stage / RECEIPT_NAME, _seal({**value, "state": "complete", "ready": True,
                     "verifiedSha256": proof["sha256"], "verifiedAt": proof["verifiedAt"],
                     "resultSha256": hashlib.sha256(_regular_path(stage / "result.json", missing=False).read_bytes()).hexdigest()}, key))
        return True
    except (UpdateError, OSError, ValueError, TypeError, KeyError):
        return False


@contextmanager
def _open_for_delete(path):
    """Hold read/delete access exclusively; ownership transfers to the stream."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(path), 0x80010000, 0, None, 3, 0x00200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except Exception:
            kernel.CloseHandle(handle)
            raise
        with os.fdopen(descriptor, "rb") as stream:
            yield stream
    else:
        with path.open("rb") as stream:
            yield stream


def _mark_delete(stream, path):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetFileInformationByHandle.restype = wintypes.BOOL
        delete = wintypes.BOOL(True)
        if not kernel.SetFileInformationByHandle(msvcrt.get_osfhandle(stream.fileno()), 4, ctypes.byref(delete), ctypes.sizeof(delete)):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        path.unlink()


def _delete_verified(path, item):
    """Hash and mark deletion on the SAME exclusively held Windows handle."""
    with _open_for_delete(path) as stream:
        if os.fstat(stream.fileno()).st_size != item["size"] or hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
            return False
        _mark_delete(stream, path)
    return True


def cleanup_verified_updates(root, *, protected_paths=(), active_install_id=None):
    """Retry authenticated successes; retain unresolved/unknown recovery data.

    Call while holding the service's operation lock, so a download cannot start
    or replace a cached EXE concurrently. Large files are removed first; only
    fully cleaned receipts beyond the newest five have metadata pruned.
    """
    summary = {"removed": 0, "deferred": 0, "pruned": 0}
    try:
        root = _regular_path(root, missing=False)
        key = _key(root)
        protected = {_regular_path(path) for path in protected_paths if path}
        stages = [entry for entry in root.iterdir() if re.fullmatch(r"[a-f0-9]{48}", entry.name)]
    except (UpdateError, OSError, ValueError):
        return summary
    verified = []
    # Every pending install protects its exact sources and recovery target.
    # No files from such receipts are selected for deletion.
    for stage in stages:
        try:
            _regular_path(stage, missing=False)
            value = _validated(_unseal(stage / RECEIPT_NAME, key), stage, complete=True)
            verified.append((stage, value))
        except (UpdateError, OSError, ValueError, TypeError, KeyError):
            try:
                spec = _read_json(stage / "install.json", 65536)
                spec = spec if isinstance(spec, dict) else {}
                for field in ("source", "target", "backup"):
                    if spec.get(field):
                        protected.add(_regular_path(spec[field]))
                authority = _validated(_unseal(stage / "cleanup-authority.json", key), stage, complete=False)
                protected.update(_regular_path(item["path"]) for item in authority["files"])
            except (UpdateError, OSError, ValueError, TypeError, KeyError):
                pass
    finished = []
    for stage, value in verified:
        if stage.name == active_install_id:
            continue
        complete = True
        for item in value["files"]:
            try:
                path = _regular_path(item["path"])
                if not path.exists():
                    continue
                if path in protected or not path.is_file() or not _delete_verified(path, item):
                    complete = False
                    summary["deferred"] += 1
                    continue
                summary["removed"] += 1
            except (UpdateError, OSError, ValueError):
                complete = False
                summary["deferred"] += 1
        if complete:
            finished.append((stage, value))
    finished.sort(key=lambda pair: str(pair[1].get("verifiedAt", "")), reverse=True)
    known_metadata = {"install.json", "install.ps1", "result.json", "cleanup-authority.json", RECEIPT_NAME}
    try:
        pointer = _read_json(root / "latest.json", 8192)
        latest = pointer.get("installId") if isinstance(pointer, dict) else None
    except (UpdateError, OSError, ValueError, TypeError):
        latest = None
    for stage, _value in finished[KEEP_RECEIPTS:]:
        if stage.name == latest:
            continue
        try:
            entries = list(stage.iterdir())
            if any(entry.name not in known_metadata or not _regular_path(entry).is_file() for entry in entries):
                continue
            # Acquire every handle before deleting anything. A locked receipt
            # must not strand a partially pruned directory without its result.
            with ExitStack() as stack:
                opened = [(entry, stack.enter_context(_open_for_delete(entry))) for entry in entries]
                if any(getattr(os.fstat(stream.fileno()), "st_file_attributes", 0) & 1 for _entry, stream in opened):
                    continue
                for entry, stream in opened:
                    _mark_delete(stream, entry)
            stage.rmdir()
            summary["pruned"] += 1
        except (UpdateError, OSError):
            continue
    return summary
