"""Lossless encoding normalization and fingerprinted, explicit config recovery."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import time
import tomllib
import uuid

import agent_manager.core as core
import agent_manager.config.backups as backups


def decode_toml(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return raw.decode("utf-32")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def read_text(path: Path) -> str:
    try:
        return decode_toml(path.read_bytes())
    except FileNotFoundError:
        return ""


def _fingerprint(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_bounded(path: Path) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(core.CODEX_CONFIG_MAX_BYTES + 1)
    if len(raw) > core.CODEX_CONFIG_MAX_BYTES:
        raise core.ManagerError("配置文件过大，已停止自动处理。")
    return raw


def _current_config() -> tuple[bytes, bool]:
    try:
        return _read_bounded(core.CONFIG_FILE), True
    except FileNotFoundError:
        return b"", False


def _valid_text(raw: bytes) -> str | None:
    try:
        text = decode_toml(raw)
        tomllib.loads(text)
        return text
    except (UnicodeError, tomllib.TOMLDecodeError):
        return None


def _directory_is_direct(path: Path) -> bool:
    """Do not discover or create backups through a junction/symlink ancestor."""
    return backups.directory_is_direct(path)


def claim_orphaned_overlay() -> None:
    """Authenticated app recovery may resume a journal whose owner has exited."""
    with core.SWITCH_OPERATION_LOCK, core.RUNTIME_OVERLAY_LOCK:
        overlay = core._runtime_overlay_read()
        owner = int((overlay or {}).get("ownerPid") or 0)
        if not overlay or owner == os.getpid():
            return
        alive = False
        if owner > 0:
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes

                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.OpenProcess.argtypes = [
                    wintypes.DWORD,
                    wintypes.BOOL,
                    wintypes.DWORD,
                ]
                kernel.OpenProcess.restype = wintypes.HANDLE
                handle = kernel.OpenProcess(0x1000, False, owner)
                if handle:
                    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                    kernel.CloseHandle(handle)
                    alive = True
                else:
                    alive = ctypes.get_last_error() != 87
            else:
                try:
                    os.kill(owner, 0)
                    alive = True
                except ProcessLookupError:
                    pass
                except PermissionError:
                    alive = True
        if alive:
            raise core.ManagerError(
                "另一个仍在运行的进程持有临时配置记录，未接管或覆盖配置。"
            )
        core.adopt_runtime_configuration_overlay()


def inspect_recovery() -> dict:
    raw, exists = _current_config()
    text = _valid_text(raw)
    status = "valid" if text is not None else "invalid"
    if text is not None and raw.startswith(
        (b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")
    ):
        status = "encoding"
    files = backups.list_backups()
    candidates = [item for item in files if item["canRestore"]]
    return {
        "status": status,
        "exists": exists,
        "path": str(core.CONFIG_FILE),
        "fingerprint": _fingerprint(raw),
        "size": len(raw),
        "backups": candidates,
        "backupFiles": files,
        "automaticLimit": backups.AUTO_LIMIT,
        "retentionSummary": {
            "automatic": sum(item["retention"] == "automatic" for item in files),
            "recoveryOriginals": sum(item["kind"] == "recovery" for item in files),
            "protectedAutomatic": sum(item["retention"] == "automatic" and not item["canDelete"] for item in files),
            "manual": sum(item["kind"] == "manual" for item in files),
            "external": sum(item["kind"] == "external" for item in files),
        },
        "canNormalize": status == "encoding",
        "message": "当前配置可正常解析。"
        if status == "valid"
        else "配置编码可无损转换为 UTF-8。"
        if status == "encoding"
        else "当前配置无法解析，可选择有效备份恢复，或保留原文件后重建空配置。",
    }


def create_manual_backup(*, expected_fingerprint: str) -> dict:
    with core.SWITCH_OPERATION_LOCK, core.CONFIG_FILE_LOCK, core.RUNTIME_OVERLAY_LOCK:
        original, exists = _current_config()
        if _fingerprint(original) != expected_fingerprint:
            raise core.ManagerError("配置已被其他程序修改，请重新检查后再备份。")
        if not exists:
            raise core.ManagerError("当前配置文件不存在，无法创建手动备份。")
        result = backups.create_backup(original, kind="manual")
        observed, observed_exists = _current_config()
        if observed != original or observed_exists != exists:
            result["warning"] = "备份已保存检查时的原始内容；当前配置随后被其他程序修改，请重新检查。"
        return result


def delete_backup(*, backup_id: str) -> dict:
    with core.SWITCH_OPERATION_LOCK, core.CONFIG_FILE_LOCK, core.RUNTIME_OVERLAY_LOCK:
        return backups.delete_backup(backup_id)


def repair_config(
    *, expected_fingerprint: str, backup_id: str | None = None, reset: bool = False
) -> dict:
    with core.SWITCH_OPERATION_LOCK, core.CONFIG_FILE_LOCK, core.RUNTIME_OVERLAY_LOCK:
        current = inspect_recovery()
        if current["fingerprint"] != expected_fingerprint:
            raise core.ManagerError("配置已被其他程序修改，请重新检查后再恢复。")
        original, original_exists = _current_config()
        if _fingerprint(original) != expected_fingerprint:
            raise core.ManagerError("配置在检查后再次变化，请重新检查后再恢复。")
        selected = (
            next((item for item in current["backups"] if item["id"] == backup_id), None)
            if backup_id
            else None
        )
        if backup_id and not selected:
            raise core.ManagerError("所选备份已变化或不可用，请重新检查。")
        if selected:
            value = backups.read_backup(Path(selected["path"]), expected_id=selected["id"])
            if _fingerprint(value) != selected["fingerprint"]:
                raise core.ManagerError("备份在检查后发生变化，未恢复。")
            text = _valid_text(value)
        elif reset:
            text = ""
        elif current["canNormalize"]:
            text = _valid_text(original)
        elif current["status"] == "valid":
            return {"changed": False, "action": "unchanged"}
        else:
            raise core.ManagerError(
                "请先选择有效备份，或明确选择保留原文件后重建空配置。"
            )
        if text is None:
            raise core.ManagerError("恢复内容不是有效 TOML，未覆盖原配置。")
        output = text.encode("utf-8")
        if output == original:
            return {"changed": False, "action": "unchanged"}
        # Copy, never discard, the exact original bytes before replacement.
        backup_root = core.BACKUPS_DIR / "config-recovery"
        if not _directory_is_direct(backup_root):
            raise core.ManagerError(
                "备份目录经过目录链接或连接点，已停止恢复以免写到指定范围之外。"
            )
        backup_root.mkdir(parents=True, exist_ok=True)
        saved = (
            backup_root
            / f"config.toml.{time.time_ns()}-{uuid.uuid4().hex[:8]}.original"
        )
        journal = backups.begin_recovery(saved, Path(selected["path"]) if selected else None, original=original)
        saved = Path(journal["originalBackup"])
        snapshot = None
        try:
            if not journal["originalReused"]:
                core.atomic_write_bytes(saved, original)
            if backups.read_backup(saved) != original:
                raise core.ManagerError("恢复前原件备份回验失败，已取消覆盖。")
            # Detect external editors racing the backup operation.
            observed, observed_exists = _current_config()
            if observed != original or observed_exists != original_exists:
                raise core.ManagerError("备份期间配置又被修改，已保留备份并取消覆盖。")
            captured = core._capture_file_bytes((core.CONFIG_FILE, core.RUNTIME_OVERLAY_FILE))
            if captured.get(core.CONFIG_FILE) != (original if original_exists else None):
                raise core.ManagerError("建立恢复快照时配置再次变化，已取消覆盖。")
            snapshot = captured
            core.atomic_write_bytes(core.CONFIG_FILE, output)
            if _read_bounded(core.CONFIG_FILE) != output:
                raise core.ManagerError("配置写入回验失败，正在还原原文件。")
            if selected or reset:
                core._runtime_overlay_rebase_user_file_checked(core.CONFIG_FILE, output)
        except Exception as exc:
            rollback = core._restore_file_bytes(snapshot) if snapshot is not None else []
            try:
                backups.finish_recovery(journal, succeeded=False)
            except Exception:
                pass  # The already-durable active journal still protects the evidence.
            detail = f"；还原异常：{'；'.join(rollback)}" if rollback else ""
            raise core.ManagerError(
                f"配置恢复未完成，原始文件已另存备份：{exc}{detail}"
            ) from exc
        warning = None
        try:
            backups.finish_recovery(journal, succeeded=True)
            # Only referenced, unfinished recovery files are exempt from expiry.
            retention = backups.prune_automatic()
        except Exception:
            retention = None
            warning = "配置已恢复；恢复记录清理未完成，相关备份暂时保留。"
        return {
            "changed": True,
            "action": "restore" if selected else "reset" if reset else "normalize",
            "originalBackup": str(saved),
            "restoredFrom": selected["name"] if selected else None,
            "fingerprint": _fingerprint(output),
            "retention": retention,
            "warning": warning,
        }


def normalize_encoding_if_needed() -> dict:
    """Startup converts only valid Unicode encoding; semantic recovery is explicit."""
    if not core.CONFIG_FILE.is_file():
        return {"changed": False}
    with core.CONFIG_FILE.open("rb") as stream:
        prefix = stream.read(4)
    if not prefix.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")):
        return {"changed": False}
    inspection = inspect_recovery()
    if inspection["canNormalize"]:
        return repair_config(expected_fingerprint=inspection["fingerprint"])
    return {"changed": False}
