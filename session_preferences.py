"""Manager-local session preferences; independent of Codex's Git metadata API."""

import re

import agent_manager_core as core

MAX_PINNED_THREADS = 5000
THREAD_ID = re.compile(r"[A-Za-z0-9._:-]{1,160}")


def pinned_ids(settings=None, *, strict=False):
    settings = core.load_settings() if settings is None else settings
    preferences = settings.get("sessionPreferences", {})
    raw = preferences.get("pinnedThreadIds", []) if isinstance(preferences, dict) else None
    if not isinstance(raw, list) or len(raw) > MAX_PINNED_THREADS or any(
        not isinstance(item, str) or not THREAD_ID.fullmatch(item) for item in raw
    ):
        if strict:
            raise core.ManagerError("会话置顶偏好格式无效，已保留原设置，请先检查配置。")
        return set()
    return set(raw)


def update_pins(thread_ids, enabled):
    with core.SETTINGS_LOCK, core._settings_file_lock():
        settings = core.load_settings()
        pinned = pinned_ids(settings, strict=True)
        previous = set(pinned)
        if enabled:
            pinned.update(thread_ids)
        else:
            pinned.difference_update(thread_ids)
        if len(pinned) > MAX_PINNED_THREADS:
            raise core.ManagerError("管理器置顶会话已达上限，请先取消部分置顶。")
        settings.setdefault("sessionPreferences", {})["pinnedThreadIds"] = sorted(pinned)
        if pinned != previous:
            core.save_settings(settings)
        return {"action": "pin" if enabled else "unpin", "changed": len(previous.symmetric_difference(pinned)),
                "threadIds": thread_ids, "failed": [], "unconfirmed": [], "pinScope": "manager"}


def apply_to_threads(rows):
    pinned = pinned_ids()
    return [{**row, "pinned": row.get("id") in pinned, "pinScope": "manager"} for row in rows]
