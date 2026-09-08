"""Subscription services."""
from __future__ import annotations
from agent_manager import core as _core


def _json_scalar_text(value: _core.Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""



def _account_check_parts(record: dict) -> tuple[dict, dict]:
    account = record.get("account") if isinstance(record.get("account"), dict) else record
    entitlement = record.get("entitlement") if isinstance(record.get("entitlement"), dict) else {}
    return account, entitlement



def _account_check_value(record: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = _core._json_scalar_text(record.get(key))
        if value:
            return value
    return ""



def _parse_chatgpt_subscription_account_check(
    payload: dict,
    preferred_account_id: str = "",
    preferred_organization_id: str = "",
) -> dict:
    root = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    accounts = root.get("accounts") if isinstance(root, dict) else None
    records: list[tuple[str, dict]] = []
    if isinstance(accounts, dict):
        records.extend(
            (str(key).strip(), value)
            for key, value in accounts.items()
            if isinstance(value, dict)
        )
    elif isinstance(accounts, list):
        records.extend(("", value) for value in accounts if isinstance(value, dict))
    if not records:
        raise _core.ManagerError("accounts/check 未返回可用账号。")

    def account_id(item: tuple[str, dict]) -> str:
        account, _ = _core._account_check_parts(item[1])
        return _core._account_check_value(account, ("account_id", "id", "chatgpt_account_id", "workspace_id"))

    def plan(item: tuple[str, dict]) -> str:
        account, entitlement = _core._account_check_parts(item[1])
        return _core._account_check_value(entitlement, ("subscription_plan",)) or _core._account_check_value(
            account,
            ("plan_type", "planType"),
        )

    preferred_organization_id = str(preferred_organization_id or "").strip()
    preferred_account_id = str(preferred_account_id or "").strip()
    selected = next(
        (item for item in records if preferred_organization_id and item[0] == preferred_organization_id
         and (not preferred_account_id or account_id(item) == preferred_account_id)),
        None,
    )
    if selected is None:
        selected = next(
            (item for item in records if preferred_account_id and account_id(item) == preferred_account_id),
            None,
        )
    if selected is None:
        selected = next(
            (
                item
                for item in records
                if _core._account_check_parts(item[1])[0].get("is_default") is True
            ),
            None,
        )
    if selected is None:
        selected = next(
            (item for item in records if plan(item).casefold() not in {"", "free"}),
            None,
        )
    selected = selected or records[0]
    account, entitlement = _core._account_check_parts(selected[1])
    plan_raw = _core._account_check_value(entitlement, ("subscription_plan",)) or _core._account_check_value(
        account,
        ("plan_type", "planType"),
    )
    expiry = _core._session_expiry(
        _core._account_check_value(entitlement, ("expires_at",))
        or _core._account_check_value(account, ("expires_at",))
    )
    checked_at = _core.now_iso()
    metadata = _core.extract_plan_metadata(selected[1], source="entitlement", observed_at=checked_at)
    return {
        "accountId": _core._account_check_value(
            account,
            ("account_id", "id", "chatgpt_account_id", "workspace_id"),
        )
        or preferred_account_id,
        "plan": plan_raw,
        "planLabel": _core._normalize_plan_label(plan_raw),
        **metadata,
        "subscriptionExpiresAt": expiry,
        "subscriptionLastCheckedAt": checked_at,
        "subscriptionMetadataSource": "entitlement",
        "subscriptionStatus": "unknown"
        if not expiry
        else "expired"
        if _core._subscription_missing_or_expired(expiry)
        else "active",
    }



def _parse_chatgpt_subscription(payload: dict, fallback_account_id: str) -> dict:
    root = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    subscription = root.get("subscription") if isinstance(root.get("subscription"), dict) else root
    plan_raw = _core._account_check_value(subscription, ("subscription_plan", "plan_type", "planType"))
    expiry = _core._session_expiry(
        _core._account_check_value(
            subscription,
            ("active_until", "expires_at", "subscription_expires_at", "current_period_end"),
        )
    )
    checked_at = _core.now_iso()
    metadata = _core.extract_plan_metadata(root, source="subscription", observed_at=checked_at)
    return {
        "accountId": str(fallback_account_id or "").strip(),
        "plan": plan_raw,
        "planLabel": _core._normalize_plan_label(plan_raw),
        **metadata,
        "subscriptionExpiresAt": expiry,
        "subscriptionLastCheckedAt": checked_at,
        "subscriptionMetadataSource": "entitlement",
        "subscriptionStatus": "unknown"
        if not expiry
        else "expired"
        if _core._subscription_missing_or_expired(expiry)
        else "active",
    }



def _subscription_request_headers(target_path: str) -> dict[str, str]:
    return {
        "Referer": "https://chatgpt.com/",
        "User-Agent": _core.CHATGPT_WEB_USER_AGENT,
        "x-openai-target-path": target_path,
        "x-openai-target-route": target_path,
    }



def _chatgpt_timezone_offset_minutes() -> int:
    offset = _core.datetime.now().astimezone().utcoffset()
    return -int(offset.total_seconds() // 60) if offset else 0



def _fetch_chatgpt_subscription_status(access_token: str, account_id: str) -> dict:
    target_path = "/backend-api/accounts/check/v4-2023-04-27"
    check_payload = _core._fetch_chatgpt_json(
        _core.CHATGPT_ACCOUNTS_CHECK_URL,
        access_token,
        account_id,
        {"timezone_offset_min": _core._chatgpt_timezone_offset_minutes()},
        include_account_id=False,
        extra_headers=_core._subscription_request_headers(target_path),
    )
    snapshot = _core._parse_chatgpt_subscription_account_check(
        check_payload,
        preferred_account_id=account_id,
        preferred_organization_id=_core._chatgpt_organization_id(access_token),
    )
    if not _core._subscription_missing_or_expired(snapshot.get("subscriptionExpiresAt")):
        return snapshot

    resolved_account_id = str(snapshot.get("accountId") or account_id).strip()
    if not resolved_account_id:
        raise _core.ManagerError("未获取到 Account ID，无法继续同步订阅有效期。")
    target_path = "/backend-api/subscriptions"
    fallback_payload = _core._fetch_chatgpt_json(
        _core.CHATGPT_SUBSCRIPTIONS_URL,
        access_token,
        resolved_account_id,
        {"account_id": resolved_account_id},
        include_account_id=False,
        extra_headers=_core._subscription_request_headers(target_path),
    )
    fallback = _core._parse_chatgpt_subscription(fallback_payload, resolved_account_id)
    snapshot.update(_core.select_plan_metadata(
        _core._plan_snapshot_metadata(snapshot), _core._plan_snapshot_metadata(fallback),
    ))
    snapshot["subscriptionExpiresAt"] = fallback.get("subscriptionExpiresAt")
    snapshot["subscriptionLastCheckedAt"] = fallback["subscriptionLastCheckedAt"]
    snapshot["subscriptionStatus"] = fallback["subscriptionStatus"]
    return snapshot

