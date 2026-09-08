"""Refresh one stored official identity without deleting or replacing its account."""
from __future__ import annotations

import json
import agent_manager_core as core


def target(account_id: str) -> dict:
    account = next((item for item in core.load_settings().get("accounts", []) if item.get("id") == account_id), None)
    if not account:
        raise core.ManagerError("账号已不存在，请刷新列表。")
    if account.get("sourceType") != "codex_auth" or account.get("authMode") != "chatgpt":
        raise core.ManagerError("此入口仅用于官方 ChatGPT OAuth 账号。")
    return account


def reauthenticate(account_id: str, auth_bytes: bytes) -> dict:
    with core._account_refresh_lock_for(account_id):
        account = target(account_id)
        _snapshot, identity = core._snapshot_from_bytes(auth_bytes, None)
        previous_email = str(account.get("email") or "").strip().casefold()
        incoming_email = str(identity.get("email") or "").strip().casefold()
        if not core._account_matches_identity(account, identity) or (
            previous_email and incoming_email and previous_email != incoming_email
        ):
            raise core.ManagerError("登录的账号与正在重新认证的账号不一致，原账号未被覆盖；请重新选择正确账号。")
        # A newly authorized bundle is the authority. Preserve the local account
        # ID, grouping, catalog, routes and import timestamp; never transplant a
        # different computer's cap_sid into this machine.
        core._persist_account_oauth_auth(
            account_id, account, auth_bytes, None, replace_live_if_active=True,
        )
        with core.SETTINGS_LOCK, core._settings_file_lock():
            settings = core.load_settings()
            latest = next((item for item in settings.get("accounts", []) if item.get("id") == account_id), None)
            if not latest or not core._account_matches_identity(latest, identity):
                raise core.ManagerError("账号在认证期间被删除或修改，请刷新列表。")
            latest.update(
                refreshState="pending", refreshErrors={}, refreshWarnings={},
                nextRefreshAt=None, reauthenticatedAt=core.now_iso(), updatedAt=core.now_iso(),
            )
            if isinstance(latest.get("usage"), dict):
                latest["usage"] = json.loads(json.dumps(latest["usage"]))
                if isinstance(latest["usage"].get("weekly"), dict):
                    latest["usage"]["weekly"]["stale"] = True
            core.save_settings(settings)
            return latest
