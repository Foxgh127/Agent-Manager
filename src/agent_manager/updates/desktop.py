"""Check/update the installed OpenAI desktop package through Microsoft Store CLI.

Uses an installed package family, never a fuzzy name or the Codex CLI package.
Unknown/localized output remains unknown; a zero exit status alone is not proof.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone

_LOCK = threading.Lock()
_FAMILY = re.compile(r"^(OpenAI\.(?:Codex|ChatGPT))_[a-z0-9]{13}$", re.I)


def package_family(desktop: dict) -> str | None:
    family = str(desktop.get("appUserModelId") or "").split("!", 1)[0]
    return family if _FAMILY.fullmatch(family) else None


def parse_store_check(output: str) -> dict:
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output).strip()
    # Store CLI can ask to apply even with --apply false, then report EOF on
    # redirected stdin. That final prompt failure does not invalidate its check.
    # Ignore only this exact message; other failures must remain unknown.
    checked_text = re.sub(r"Failed to read input in non-interactive mode\.?", "", text, flags=re.I)
    if re.search(r"\berror\b|could not|cannot |failed|错误|失败|无法", checked_text, re.I):
        return {"updateAvailable": None, "updateState": "check_failed", "message": "检查未成功，请重试。"}
    if re.search(r"already up.to.date|no updates? (?:are )?available|已是最新|没有可用更新", text, re.I):
        return {"updateAvailable": False, "updateState": "current", "message": "Microsoft Store 确认已是最新版"}
    if re.search(r"updates? (?:is |are )?available|updates? found|new version|有可用更新|可更新到", checked_text, re.I):
        versions = re.findall(r"\b\d+\.\d+\.\d+(?:\.\d+)?\b", text)
        return {"updateAvailable": True, "updateState": "available", "availableVersion": versions[-1] if versions else None,
                "message": "Microsoft Store 发现可用更新"}
    return {"updateAvailable": None, "updateState": "unknown", "message": "未能确认更新结果，请重新检查。"}


def _run(command: list[str], timeout: float):
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def check(desktop: dict | None, *, runner=None, executable=None) -> dict:
    desktop = desktop or {}
    family = package_family(desktop)
    store = executable or (shutil.which("store.exe") if os.name == "nt" else None)
    result = {"kind": "desktop", "installed": bool(desktop), "installedVersion": desktop.get("version"),
              "availableVersion": None, "updateAvailable": None, "checkedAt": datetime.now(timezone.utc).isoformat(),
              "manualInApp": False, "canAutoUpdate": bool(family and store), "supported": bool(family and store),
              "source": "microsoft_store_cli" if family and store else "microsoft_store", "packageFamily": family}
    if not desktop:
        return {**result, "updateState": "not_installed", "message": "尚未安装 Codex Desktop"}
    if not family or not store:
        return {**result, "updateState": "external_unavailable", "message": "当前系统没有可用的 Store CLI，可打开 Microsoft Store 更新"}
    try:
        completed = (runner or _run)([store, "update", family, "--apply", "false"], 50)
        output = (completed.stdout or "") + "\n" + (completed.stderr or "")
        parsed = parse_store_check(output)
        expected_prompt_abort = (parsed.get("updateAvailable") is True and
            "Failed to read input in non-interactive mode" in output)
        if completed.returncode and not expected_prompt_abort:
            parsed = {"updateAvailable": None, "updateState": "check_failed", "message": "Microsoft Store 检查失败，请重试。"}
        return {**result, **parsed}
    except (OSError, subprocess.TimeoutExpired):
        return {**result, "updateState": "check_failed", "message": "Microsoft Store 检查超时或不可用，请重试。"}


def apply(desktop: dict, *, detector, runner=None, executable=None) -> dict:
    if not _LOCK.acquire(blocking=False):
        raise RuntimeError("Codex Desktop 更新正在进行，请稍候。")
    try:
        store = executable or (shutil.which("store.exe") if os.name == "nt" else None)
        before = check(desktop, runner=runner, executable=store)
        if before.get("updateAvailable") is False:
            return {"updated": False, "component": before, "message": "Codex Desktop 已是最新版。"}
        if before.get("updateAvailable") is not True or not before.get("canAutoUpdate"):
            raise RuntimeError(before.get("message") or "请先成功检查 Desktop 更新。")
        completed = (runner or _run)([store, "update", before["packageFamily"], "--apply"], 900)
        output = (completed.stdout or "") + "\n" + (completed.stderr or "")
        if completed.returncode or re.search(r"\berror\b|\bfailed\b|错误|失败", output, re.I):
            raise RuntimeError("Microsoft Store 未完成更新；若提示应用正在使用，请结束 Codex 任务并关闭桌面端后重试。")
        installed = detector(force=True) or {}
        after = check(installed, runner=runner, executable=store)
        changed = (package_family(installed) == before["packageFamily"] and
                   installed.get("version") and installed["version"] != desktop.get("version"))
        if not changed or after.get("updateAvailable") is not False:
            return {"updated": False, "pendingVerification": True, "component": after,
                    "message": "更新请求已提交；尚未确认安装完成，请稍后刷新状态。Codex 正在使用时可能需要先关闭它。"}
        return {"updated": True, "component": after, "message": f"Codex Desktop 已更新到 {installed['version']}。"}
    finally:
        _LOCK.release()
