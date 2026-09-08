"""Isolated relay-dashboard login and import support for Agent Manager.

The login window never reads Chrome/Edge state.  After an explicit successful
first import, or after a same-account reauthentication is verified twice, only
that isolated window's same-origin renewal credential is persisted in the
existing Windows DPAPI secret vault. Tokens and cookies never enter settings,
public API state, logs, or export bundles; the saved credential lets account
refresh and Key management continue after the WebView is closed.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import wraps
from http.cookies import SimpleCookie, CookieError
import json
import math
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

import agent_manager_core as core


SESSION_TTL_SECONDS = 15 * 60
SCAN_TIMEOUT_SECONDS = 35.0
AUTO_AUTH_CHECK_INTERVAL_SECONDS = 2.5
AUTO_AUTH_CHECK_TIMEOUT_SECONDS = 12.0
AUTO_AUTH_MAX_ATTEMPTS = 120
MAX_KEYS = 200
MAX_MODELS = 1_000
MAX_TEXT = 300
COOKIE_LOCK = threading.RLock()

class DashboardTemporarilyUnavailable(core.ManagerError):
    """The service failed to answer; saved login credentials remain unproven."""


class DashboardLoginRequired(core.ManagerError):
    """The dashboard conclusively rejected or lacks its saved login session."""


class DashboardIdentityChanged(core.ManagerError):
    """Renewal returned another identity and must not replace this account."""


def _serialized_dashboard(function):
    @wraps(function)
    def invoke(self, account_id, *args, **kwargs):
        account_key = core.slugify(str(account_id or ""), "中转站账号 ID")
        with core._account_refresh_lock_for("relay-dashboard:" + account_key):
            return function(self, account_id, *args, **kwargs)
    return invoke


def _text(value: object, limit: int = MAX_TEXT) -> str:
    return str(value or "").strip()[:limit]


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
        if match:
            try:
                number = float(match.group(0))
                return number if math.isfinite(number) else None
            except ValueError:
                pass
    return None


def _response_data(value: object) -> object:
    if not isinstance(value, dict):
        return value
    # The WebView probe marks parse failures explicitly.  A SPA fallback can
    # return HTML with HTTP 200 for an unknown API path; treating the wrapper
    # object as JSON is what previously misclassified Sub2API as New API.
    if value.get("json") is False:
        return None
    if "status" in value:
        try:
            status = int(value.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        if status and not 200 <= status < 300:
            return None
    body = value.get("body") if isinstance(value.get("body"), (dict, list)) else value
    if not isinstance(body, dict):
        return body
    if body.get("success") is False:
        return None
    if "code" in body and body.get("code") not in {0, 200, True, None}:
        return None
    return body.get("data", body)


def _response_ok(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    status = int(value.get("status") or 0)
    if status < 200 or status >= 300:
        return False
    if value.get("json") is False:
        return False
    body = value.get("body")
    if isinstance(body, dict):
        if body.get("success") is False:
            return False
        if "code" in body and body.get("code") not in {0, 200, True, None}:
            return False
    return isinstance(body, (dict, list))


def _items(value: object) -> list[dict]:
    data = _response_data(value)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("items", "list", "tokens", "keys", "results", "records"):
        rows = data.get(key)
        if isinstance(rows, list):
            return [item for item in rows if isinstance(item, dict)]
    return []


def _catalog_reported_total(value: object) -> int | None:
    """Return a trustworthy pagination total when the dashboard exposes one."""

    data = _response_data(value)
    containers: list[dict] = []
    body = value.get("body") if isinstance(value, dict) else None
    for candidate in (data, body):
        if isinstance(candidate, dict) and candidate not in containers:
            containers.append(candidate)
    for container in list(containers):
        for key in ("pagination", "meta", "page_info", "pageInfo"):
            nested = container.get(key)
            if isinstance(nested, dict):
                containers.append(nested)
    for container in containers:
        for key in ("total", "total_count", "totalCount", "total_items", "totalItems"):
            raw = container.get(key)
            if isinstance(raw, bool):
                continue
            try:
                total = int(raw)
            except (TypeError, ValueError, OverflowError):
                continue
            if total >= 0:
                return total
    return None


def _key_catalog_status(
    responses: object,
    *,
    page_size: int,
) -> tuple[bool, int | None]:
    """Prove that the fetched pages cover the whole remote Key catalog.

    Relays differ on whether their first page is zero- or one-based, which is
    why New API is queried both ways.  HTTP success alone is insufficient:
    when a full page has no usable total, another page may still exist and a
    missing local row must not be interpreted as a remote deletion.
    """

    pages = responses if isinstance(responses, list) else []
    successful = [item for item in pages if _response_ok(item)]
    if not successful:
        return False, None
    unique_rows: set[str] = set()
    page_lengths: list[int] = []
    totals: list[int] = []
    for response in successful:
        rows = _items(response)
        page_lengths.append(len(rows))
        for index, item in enumerate(rows):
            identity = _text(
                item.get("id") or item.get("token_id") or item.get("key_id"),
                120,
            )
            if not identity:
                identity = json.dumps(item, ensure_ascii=False, sort_keys=True)[:1000] or str(index)
            unique_rows.add(identity)
        total = _catalog_reported_total(response)
        if total is not None:
            totals.append(total)

    reported_total = max(totals) if totals else None
    if reported_total is not None:
        return len(unique_rows) >= reported_total, reported_total

    # A non-empty short page proves the end.  All-successful empty pages prove
    # an empty catalog.  A lone empty alternate page next to a full page is
    # ambiguous because the relay may reject that pagination convention by
    # returning an empty JSON result with HTTP 200.
    if unique_rows and any(0 < length < page_size for length in page_lengths):
        return True, len(unique_rows)
    if not unique_rows and all(length == 0 for length in page_lengths):
        return True, 0
    return False, reported_total


def _full_api_key(value: object, *, assume_one_api: bool = False) -> str:
    text = _text(value, 600)
    if not text or any(marker in text for marker in ("*", "…", "...")):
        return ""
    if re.search(r"\s", text) or len(text) < 12:
        return ""
    if assume_one_api and not text.casefold().startswith("sk-"):
        text = f"sk-{text}"
    if len(text) > 512 or not re.fullmatch(r"[A-Za-z0-9._~+\-/=]+", text):
        return ""
    return text


def _mask_api_key(value: object) -> str:
    text = _text(value, 600)
    if not text:
        return "未返回"
    if any(marker in text for marker in ("*", "…", "...")):
        return text[:80]
    if len(text) <= 12:
        return "已识别"
    return f"{text[:7]}••••{text[-4:]}"


def _normalize_key_record(item: dict, adapter: str) -> tuple[dict, str]:
    key_id = _text(item.get("id") or item.get("token_id") or item.get("key_id"), 80)
    name = _text(item.get("name") or item.get("label") or f"API Key {key_id or ''}", 120)
    raw_key = item.get("key") or item.get("raw_key") or item.get("token") or ""
    secret = _full_api_key(raw_key, assume_one_api=adapter == "new-api")
    status = item.get("status")
    active = (
        status in {1, "1", "active", "enabled", True}
        if status is not None
        else True
    )
    unlimited = (
        bool(item.get("unlimited_quota"))
        if adapter == "new-api"
        else bool(item.get("unlimited_quota")) or _number(item.get("quota")) == 0
    )
    quota = _number(item.get("quota") if "quota" in item else item.get("remain_quota"))
    used = _number(item.get("quota_used") if "quota_used" in item else item.get("used_quota"))
    models_value = item.get("model_limits") if item.get("model_limits_enabled") else item.get("models")
    if isinstance(models_value, str):
        models = [part.strip() for part in models_value.split(",") if part.strip()]
    elif isinstance(models_value, list):
        models = [_text(part, 160) for part in models_value if _text(part, 160)]
    else:
        models = []
    group_value = item.get("group")
    group_id = item.get("group_id")
    group_platform = ""
    group_rate = None
    if isinstance(group_value, dict):
        group_id = group_id if group_id is not None else group_value.get("id")
        group_platform = _text(group_value.get("platform"), 60)
        group_rate = _number(group_value.get("rate_multiplier"))
        group = group_value.get("name") or group_value.get("id")
    else:
        group = group_value
    if not group and isinstance(item.get("group_info"), dict):
        group = item["group_info"].get("name")
    record = {
        "id": key_id,
        "name": name or "未命名 Key",
        "maskedKey": _mask_api_key(raw_key),
        "canImport": bool(secret) or bool(key_id),
        "requiresReveal": not bool(secret),
        "active": bool(active),
        "status": _text(status, 40) or ("active" if active else "inactive"),
        "group": _text(group, 100),
        "groupId": _text(group_id, 80),
        "groupPlatform": group_platform,
        "groupRateMultiplier": group_rate,
        "quota": quota,
        "used": used,
        "unlimited": unlimited,
        "expiresAt": item.get("expires_at") or item.get("expired_time"),
        "lastUsedAt": item.get("last_used_at") or item.get("accessed_time"),
        "currentConcurrency": _number(item.get("current_concurrency")),
        "models": list(dict.fromkeys(models))[:MAX_MODELS],
    }
    return record, secret


def _collect_models(*values: object) -> list[str]:
    output: list[str] = []

    def add(value: object) -> None:
        normalized = _response_data(value)
        if normalized is not value:
            add(normalized)
            return
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",")]
        if isinstance(value, list):
            for item in value:
                add(item)
        elif isinstance(value, dict):
            nested = False
            for key in ("items", "models", "data", "list", "available_models", "model_ids"):
                if key in value:
                    nested = True
                    add(value[key])
            if not nested:
                candidate = value.get("id") or value.get("model") or value.get("name")
                text = _text(candidate, 180)
                if text and text not in output:
                    output.append(text)

    for value in values:
        add(value)
    return output[:MAX_MODELS]


def _site_name(status_data: object, user_data: object, origin: str) -> str:
    for source in (status_data, user_data):
        if isinstance(source, dict):
            for key in ("system_name", "name", "site_name", "title"):
                if _text(source.get(key), 120):
                    return _text(source.get(key), 120)
    return urllib.parse.urlsplit(origin).hostname or "中转站"


def _normalized_api_root(status_data: object, origin: str) -> str:
    candidate = ""
    if isinstance(status_data, dict):
        candidate = _text(
            status_data.get("server_address")
            or status_data.get("serverAddress")
            or status_data.get("api_base_url")
            or status_data.get("api_base")
            or status_data.get("apiBase"),
            500,
        )
    if not candidate:
        candidate = origin
    try:
        root = core._validated_provider_url(candidate, "中转站自动识别 API 地址")
    except core.ManagerError:
        root = origin
    if root.casefold().endswith("/v1"):
        return root[:-3]
    return root.rstrip("/")


def _endpoint_urls(value: object, *, origin: str) -> tuple[str, str, str, str] | None:
    candidate = _text(value, 500)
    if not candidate:
        return None
    candidate = urllib.parse.urljoin(origin.rstrip("/") + "/", candidate)
    try:
        validated = core._validated_provider_url(candidate, "中转站自动识别 API 地址")
    except core.ManagerError:
        return None
    if validated.casefold().endswith("/v1"):
        api_root = validated[:-3].rstrip("/")
        base_url = validated
    else:
        api_root = validated.rstrip("/")
        base_url = f"{api_root}/v1"
    return api_root, base_url, f"{base_url}/models", f"{base_url}/usage"


def _normalize_api_endpoints(status_data: object, *, origin: str) -> list[dict]:
    status = status_data if isinstance(status_data, dict) else {}
    candidates: list[dict] = []
    primary = (
        status.get("api_base_url")
        or status.get("server_address")
        or status.get("serverAddress")
        or status.get("api_base")
        or status.get("apiBase")
    )
    if primary:
        candidates.append(
            {
                "name": "默认 API",
                "endpoint": primary,
                "description": "站点公布的默认 API 端点",
                "isDefault": True,
            }
        )
    custom = status.get("custom_endpoints")
    if isinstance(custom, list):
        for item in custom[:20]:
            if not isinstance(item, dict):
                continue
            candidates.append(
                {
                    "name": _text(item.get("name"), 80) or "自定义端点",
                    "endpoint": item.get("endpoint") or item.get("url"),
                    "description": _text(item.get("description"), 180),
                    "isDefault": False,
                }
            )
    if not candidates:
        candidates.append(
            {
                "name": "默认 API",
                "endpoint": _normalized_api_root(status, origin),
                "description": "根据登录站点自动推导",
                "isDefault": True,
            }
        )

    endpoints: list[dict] = []
    by_base: dict[str, dict] = {}
    for candidate in candidates:
        urls = _endpoint_urls(candidate.get("endpoint"), origin=origin)
        if not urls:
            continue
        api_root, base_url, models_endpoint, balance_endpoint = urls
        existing = by_base.get(base_url.casefold())
        if existing:
            alias = _text(candidate.get("name"), 80)
            if alias and alias != existing["name"] and alias not in existing["aliases"]:
                existing["aliases"].append(alias)
            description = _text(candidate.get("description"), 180)
            if description and description not in existing["descriptions"]:
                existing["descriptions"].append(description)
            existing["isDefault"] = bool(existing["isDefault"] or candidate.get("isDefault"))
            continue
        record = {
            "id": "default" if not endpoints else f"endpoint-{len(endpoints) + 1}",
            "name": _text(candidate.get("name"), 80) or "API 端点",
            "apiRoot": api_root,
            "baseUrl": base_url,
            "modelsEndpoint": models_endpoint,
            "balanceEndpoint": balance_endpoint,
            "description": _text(candidate.get("description"), 180),
            "descriptions": [
                _text(candidate.get("description"), 180)
            ] if _text(candidate.get("description"), 180) else [],
            "aliases": [],
            "isDefault": bool(candidate.get("isDefault") or not endpoints),
        }
        endpoints.append(record)
        by_base[base_url.casefold()] = record
    if not endpoints:
        fallback = _endpoint_urls(origin, origin=origin)
        if fallback:
            api_root, base_url, models_endpoint, balance_endpoint = fallback
            endpoints.append(
                {
                    "id": "default",
                    "name": "默认 API",
                    "apiRoot": api_root,
                    "baseUrl": base_url,
                    "modelsEndpoint": models_endpoint,
                    "balanceEndpoint": balance_endpoint,
                    "description": "根据登录站点自动推导",
                    "descriptions": ["根据登录站点自动推导"],
                    "aliases": [],
                    "isDefault": True,
                }
            )
    endpoints.sort(key=lambda item: not item.get("isDefault"))
    for index, endpoint in enumerate(endpoints):
        endpoint["id"] = "default" if index == 0 else f"endpoint-{index + 1}"
    return endpoints


def _normalize_groups(groups_response: object, rates_response: object) -> list[dict]:
    groups_data = _response_data(groups_response)
    if isinstance(groups_data, dict):
        groups_data = groups_data.get("items") or groups_data.get("groups") or []
    if not isinstance(groups_data, list):
        groups_data = []
    rates_data = _response_data(rates_response)
    if not isinstance(rates_data, dict):
        rates_data = {}
    groups: list[dict] = []
    for item in groups_data[:MAX_KEYS]:
        if not isinstance(item, dict):
            continue
        group_id_value = item.get("id")
        group_id = _text(group_id_value, 80)
        if not group_id:
            continue
        custom_rate = rates_data.get(group_id)
        if custom_rate is None and group_id_value in rates_data:
            custom_rate = rates_data[group_id_value]
        base_rate = _number(item.get("rate_multiplier"))
        effective_rate = _number(custom_rate)
        if effective_rate is None:
            effective_rate = base_rate
        groups.append(
            {
                "id": group_id,
                "name": _text(item.get("name"), 100) or f"分组 {group_id}",
                "description": _text(item.get("description"), 240),
                "platform": _text(item.get("platform"), 60),
                "status": _text(item.get("status"), 40) or "active",
                "active": item.get("status") in {None, "", "active", 1, "1", True},
                "rateMultiplier": effective_rate,
                "baseRateMultiplier": base_rate,
                "customRateMultiplier": _number(custom_rate),
                "subscriptionType": _text(item.get("subscription_type"), 60),
                "dailyLimitUsd": _number(item.get("daily_limit_usd")),
                "weeklyLimitUsd": _number(item.get("weekly_limit_usd")),
                "monthlyLimitUsd": _number(item.get("monthly_limit_usd")),
            }
        )
    return groups


def _key_usage_map(value: object) -> dict[str, dict]:
    data = _response_data(value)
    if not isinstance(data, dict):
        return {}
    stats = data.get("stats") if isinstance(data.get("stats"), dict) else data
    return {
        _text(key, 80): item
        for key, item in stats.items()
        if _text(key, 80) and isinstance(item, dict)
    }


def normalize_probe_result(raw: dict, *, portal_url: str) -> tuple[dict, dict[str, str]]:
    """Normalize the fixed same-origin probe result and separate any full keys."""

    if not isinstance(raw, dict):
        raise core.ManagerError("中转站识别窗口返回了无效结果，请刷新登录页后重试。")
    if raw.get("error"):
        raise core.ManagerError(_text(raw.get("error"), 500))
    adapter = _text(raw.get("adapter"), 40)
    if adapter not in {"new-api", "sub2api"}:
        details = _text(raw.get("message"), 400)
        raise core.ManagerError(
            details
            or "尚未识别到已登录的 New API、One API 或 Sub2API 会话；请在弹窗完成登录后重试。"
        )
    origin = urllib.parse.urlunsplit((*urllib.parse.urlsplit(portal_url)[:2], "", "", ""))
    status_data = _response_data(raw.get("status"))
    user_data = _response_data(raw.get("user"))
    stats_data = _response_data(raw.get("stats"))
    if not isinstance(status_data, dict):
        status_data = {}
    if not isinstance(user_data, dict):
        user_data = {}
    if not isinstance(stats_data, dict):
        stats_data = {}

    key_responses = raw.get("keys", []) if isinstance(raw.get("keys"), list) else []
    catalog_complete, catalog_total = _key_catalog_status(
        key_responses,
        page_size=200 if adapter == "sub2api" else 100,
    )
    # A producer may explicitly downgrade an otherwise complete catalog, but
    # it may never promote a response whose pagination cannot be proven
    # complete.  This is the deletion safety boundary used by snapshot sync.
    keys_authoritative = bool(
        catalog_complete
        and (
            "keysAuthoritative" not in raw
            or raw.get("keysAuthoritative") is True
        )
    )
    groups_authoritative = (
        bool(raw.get("groupsAuthoritative"))
        if "groupsAuthoritative" in raw
        else _response_ok(raw.get("groups"))
    )
    key_rows: list[dict] = []
    secrets_by_id: dict[str, str] = {}
    seen_ids: set[str] = set()
    for response in key_responses:
        for item in _items(response):
            record, secret = _normalize_key_record(item, adapter)
            identity = record["id"] or f"row-{len(key_rows) + 1}"
            if identity in seen_ids:
                continue
            seen_ids.add(identity)
            record["id"] = identity
            key_rows.append(record)
            if secret:
                secrets_by_id[identity] = secret
            if len(key_rows) >= MAX_KEYS:
                break
        if len(key_rows) >= MAX_KEYS:
            break

    groups = _normalize_groups(raw.get("groups"), raw.get("groupRates"))
    groups_by_id = {str(item["id"]): item for item in groups}
    usage_by_id = _key_usage_map(raw.get("keyUsage"))
    for record in key_rows:
        group = groups_by_id.get(str(record.get("groupId") or ""))
        if group:
            record["group"] = record.get("group") or group["name"]
            record["groupPlatform"] = record.get("groupPlatform") or group["platform"]
            record["groupRateMultiplier"] = (
                group.get("rateMultiplier")
                if group.get("rateMultiplier") is not None
                else record.get("groupRateMultiplier")
            )
        usage = usage_by_id.get(str(record.get("id") or ""), {})
        record["todayUsed"] = _number(
            usage.get("today_actual_cost")
            if "today_actual_cost" in usage
            else usage.get("today_cost")
        )
        record["totalUsed"] = _number(
            usage.get("total_actual_cost")
            if "total_actual_cost" in usage
            else usage.get("total_cost")
        )
        record["thirtyDayUsed"] = _number(
            next(
                (
                    usage[key]
                    for key in (
                        "last_30d_actual_cost",
                        "last_30_days_actual_cost",
                        "recent_30d_actual_cost",
                        "month_actual_cost",
                    )
                    if key in usage
                ),
                None,
            )
        )

    models = _collect_models(raw.get("models"), *(row.get("models") for row in key_rows))
    endpoints = _normalize_api_endpoints(status_data, origin=origin)
    if not endpoints:
        raise core.ManagerError("中转站没有提供可用的 API 端点，请改用自定义登录填写 Base URL。")
    if adapter == "new-api":
        for endpoint in endpoints:
            endpoint["balanceEndpoint"] = f"{endpoint['apiRoot']}/api/usage/token"
    selected_endpoint = endpoints[0]
    root = selected_endpoint["apiRoot"]
    base_url = selected_endpoint["baseUrl"]
    models_endpoint = selected_endpoint["modelsEndpoint"]
    balance_endpoint = (
        f"{root}/api/usage/token"
        if adapter == "new-api"
        else selected_endpoint["balanceEndpoint"]
    )

    reported_quota_per_unit = _number(status_data.get("quota_per_unit"))
    if reported_quota_per_unit is not None and reported_quota_per_unit <= 0:
        reported_quota_per_unit = None
    quota_per_unit = reported_quota_per_unit
    if adapter == "new-api":
        for record in key_rows:
            if record.get("quota") is not None and quota_per_unit is not None:
                record["quota"] = round(float(record["quota"]) / quota_per_unit, 6)
            elif record.get("quota") is not None:
                record["quota"] = None
            if record.get("used") is not None and quota_per_unit is not None:
                record["used"] = round(float(record["used"]) / quota_per_unit, 6)
            elif record.get("used") is not None:
                record["used"] = None
            record["quotaCurrency"] = "USD"
        remaining_raw = _number(user_data.get("quota"))
        used_raw = _number(user_data.get("used_quota"))
        remaining = (
            remaining_raw / quota_per_unit
            if remaining_raw is not None and quota_per_unit is not None
            else 0.0 if remaining_raw == 0
            else _number(user_data.get("balance"))
        )
        used = (
            used_raw / quota_per_unit
            if used_raw is not None and quota_per_unit is not None
            else 0.0 if used_raw == 0
            else None
        )
        currency = "USD"
    else:
        for record in key_rows:
            record["quotaCurrency"] = "USD"
        remaining = _number(user_data.get("balance"))
        used = _number(
            stats_data.get("total_actual_cost")
            if "total_actual_cost" in stats_data
            else stats_data.get("total_cost")
        )
        currency = _text(user_data.get("currency") or stats_data.get("currency"), 12).upper() or "USD"

    user_group = user_data.get("group") or user_data.get("plan")
    if isinstance(user_group, dict):
        user_group = user_group.get("name") or user_group.get("id")
    preview = {
        "adapter": adapter,
        "adapterLabel": "New API / One API" if adapter == "new-api" else "Sub2API",
        "siteName": _site_name(status_data, user_data, origin),
        "portalUrl": portal_url,
        "origin": origin,
        "baseUrl": base_url,
        "modelsEndpoint": models_endpoint,
        "balanceEndpoint": balance_endpoint,
        "apiEndpoints": endpoints,
        "defaultEndpointId": selected_endpoint["id"],
        "integrationKind": "sub2api" if adapter == "sub2api" else "",
        "user": {
            "id": _text(user_data.get("id") or user_data.get("user_id"), 80),
            "name": _text(
                user_data.get("display_name")
                or user_data.get("username")
                or user_data.get("email"),
                160,
            ),
            "email": _text(user_data.get("email"), 180),
            "group": _text(user_group, 100),
        },
        "balance": {
            "remaining": round(remaining, 6) if remaining is not None else None,
            "used": round(used, 6) if used is not None else None,
            "currency": currency,
        },
        "models": models,
        "groups": groups,
        "keys": key_rows,
        # These flags distinguish a complete successful list from an empty
        # fallback caused by a missing/failed endpoint.  Snapshot sync may
        # delete stale local rows only when the corresponding flag is true.
        "keysAuthoritative": keys_authoritative,
        "keysCatalogComplete": catalog_complete,
        "keysCatalogTotal": catalog_total,
        "groupsAuthoritative": groups_authoritative,
        "supportsCreate": adapter == "new-api" or bool(groups),
        # Persist only an explicitly reported divisor. Unknown raw quota units
        # are left unrendered rather than inventing a monetary conversion.
        "quotaPerUnit": reported_quota_per_unit if adapter == "new-api" else None,
        "detectedAt": core.now_iso(),
    }
    return preview, secrets_by_id


def _codex_key_record(record: object) -> bool:
    return bool(
        isinstance(record, dict)
        and core._relay_is_codex_compatible(
            record.get("groupPlatform"),
            f"{record.get('group') or ''} {record.get('name') or ''}",
            record.get("models"),
        )
    )


def _codex_group_record(record: object) -> bool:
    return bool(
        isinstance(record, dict)
        and core._relay_is_codex_compatible(record.get("platform"), record.get("name"))
    )


def _codex_only_preview(preview: dict, *, key_ids: set[str] | None = None) -> dict:
    """Return the subset usable by this Codex console.

    Generic OpenAI-compatible rows are retained when a relay omits platform
    metadata. Explicit Claude and media-generation lanes are excluded on the
    server as well as in the UI so crafted requests cannot import them.
    """

    result = json.loads(json.dumps(preview))
    all_keys: list[dict] = []
    if isinstance(preview.get("keys"), list):
        for item in preview["keys"]:
            if not _codex_key_record(item):
                continue
            normalized = json.loads(json.dumps(item))
            if isinstance(normalized.get("models"), list):
                normalized["models"] = [
                    model
                    for model in normalized["models"]
                    if core._relay_is_codex_compatible("", model)
                ]
            all_keys.append(normalized)
    result["remoteKeyCount"] = len(all_keys)
    # Counts below describe the filtered Codex-compatible catalog handed to
    # core.  Preserve the producer's completeness verdict, but do not carry an
    # all-platform total that would make a complete filtered catalog appear
    # truncated.
    result["keysCatalogTotal"] = (
        len(all_keys) if result.get("keysAuthoritative") is True else None
    )
    result["keys"] = [
        item
        for item in all_keys
        if key_ids is None or str(item.get("id") or "") in key_ids
    ]
    all_groups = [
        item
        for item in preview.get("groups", [])
        if _codex_group_record(item)
    ] if isinstance(preview.get("groups"), list) else []
    result["remoteGroupCount"] = len(all_groups)
    result["groups"] = all_groups
    result["apiEndpoints"] = [
        item
        for item in preview.get("apiEndpoints", [])
        if isinstance(item, dict) and core._relay_endpoint_is_codex_compatible(item)
    ] if isinstance(preview.get("apiEndpoints"), list) else []
    result["models"] = [
        model
        for model in preview.get("models", [])
        if core._relay_is_codex_compatible("", "", [model])
    ] if isinstance(preview.get("models"), list) else []
    valid_endpoint_ids = {
        str(item.get("id") or "")
        for item in result["apiEndpoints"]
        if str(item.get("id") or "")
    }
    if str(result.get("defaultEndpointId") or "") not in valid_endpoint_ids:
        result["defaultEndpointId"] = next(
            (
                str(item.get("id") or "")
                for item in result["apiEndpoints"]
                if item.get("isDefault")
            ),
            (
                str(result["apiEndpoints"][0].get("id") or "default")
                if result["apiEndpoints"]
                else "default"
            ),
        )
    return result


def _auth_probe_script(expected_origin: str, expected_adapter: str) -> str:
    """Return a small same-origin login probe used by UI polling.

    Unlike ``_probe_script``, this probe never reads Key, model, balance, usage,
    or group endpoints.  It only proves the current dashboard user and captures
    the renewable credentials needed by the one full scan that follows.
    """

    expected = json.dumps(expected_origin)
    adapter = json.dumps(expected_adapter)
    return f"""
(async () => {{
  const EXPECTED = {expected};
  const ADAPTER = {adapter};
  if (location.origin !== EXPECTED) {{
    return {{ authenticated: false, error: '登录窗口仍在第三方授权页。' }};
  }}
  const state = window.__agentManagerRelayState || (window.__agentManagerRelayState = {{}});
  const parseStored = (value) => {{
    if (!value) return null;
    try {{ return JSON.parse(value); }} catch {{ return value; }}
  }};
  const tokenFrom = (value) => {{
    if (!value) return '';
    if (typeof value === 'string') return value;
    if (typeof value !== 'object') return '';
    return value.access_token || value.accessToken || value.token || value.auth_token || '';
  }};
  if (!state.managementToken) {{
    for (const store of [localStorage, sessionStorage]) {{
      for (const name of ['auth_token', 'access_token', 'token', 'user', 'auth_user']) {{
        try {{
          const stored = parseStored(store.getItem(name));
          const candidate = tokenFrom(stored);
          if (stored && typeof stored === 'object' && (stored.id || stored.user_id)) {{
            state.userId = stored.id || stored.user_id;
          }}
          if (candidate && typeof candidate === 'string') {{
            state.managementToken = candidate;
            break;
          }}
        }} catch {{}}
      }}
      if (state.managementToken) break;
    }}
  }}
  if (!state.refreshToken) {{
    try {{ state.refreshToken = localStorage.getItem('refresh_token') || ''; }} catch {{}}
  }}
  const request = async (path, options = {{}}) => {{
    const url = new URL(path, EXPECTED);
    if (url.origin !== EXPECTED) return {{ status: 0, body: null, json: false }};
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 1800);
    try {{
      const response = await fetch(url.href, {{
        method: options.method || 'GET',
        credentials: 'include',
        cache: 'no-store',
        headers: {{ Accept: 'application/json', ...(options.headers || {{}}) }},
        body: options.body,
        signal: controller.signal,
      }});
      const text = await response.text();
      let body = null;
      let json = true;
      try {{ body = text ? JSON.parse(text) : {{}}; }}
      catch {{ json = false; body = {{ message: text.slice(0, 240) }}; }}
      return {{ status: response.status, body, json }};
    }} catch (error) {{
      return {{ status: 0, body: null, json: false, error: String(error && error.message || error).slice(0, 240) }};
    }} finally {{ clearTimeout(timer); }}
  }};
  const dataOf = (response) => {{
    if (!response || response.json !== true || response.status < 200 || response.status >= 300) return null;
    const body = response.body;
    if (!body || typeof body !== 'object' || Array.isArray(body)) return null;
    if (body.success === false) return null;
    if ('code' in body && ![0, 200, true, null].includes(body.code)) return null;
    return 'data' in body ? body.data : body;
  }};
  const isUserData = (value) => Boolean(
    value && typeof value === 'object' && !Array.isArray(value)
    && ['id', 'user_id', 'username', 'email', 'quota', 'balance'].some((key) => key in value)
  );
  const finish = (adapterName, user, authMode, sessionId = '') => {{
    const userData = dataOf(user);
    if (!isUserData(userData)) {{
      const status = Number(user && user.status || 0);
      return {{
        authenticated: false,
        adapter: adapterName,
        message: status === 401 || status === 403 ? '等待网页登录完成。' : '尚未确认登录状态。',
      }};
    }}
    if (userData.id || userData.user_id) state.userId = userData.id || userData.user_id;
    return {{
      authenticated: true,
      adapter: adapterName,
      user,
      session: {{
        accessToken: state.managementToken || '',
        refreshToken: adapterName === 'sub2api' ? state.refreshToken || '' : '',
        sessionId: sessionId || '',
        authMode,
        userId: state.userId || '',
      }},
    }};
  }};

  if (ADAPTER === 'sub2api' || !ADAPTER) {{
    const headers = () => ({{
      ...(state.managementToken ? {{ Authorization: `Bearer ${{state.managementToken}}` }} : {{}}),
      'X-User-UI-Request': '1',
    }});
    let user = await request('/api/v1/auth/me', {{ headers: headers() }});
    if (!isUserData(dataOf(user)) && state.refreshToken && ADAPTER === 'sub2api') {{
      const previousRefresh = state.refreshToken;
      const refreshed = await request('/api/v1/auth/refresh', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json', 'X-User-UI-Request': '1' }},
        body: JSON.stringify({{ refresh_token: previousRefresh }}),
      }});
      const tokens = dataOf(refreshed);
      if (tokens && typeof tokens.access_token === 'string') {{
        state.managementToken = tokens.access_token;
        state.refreshToken = typeof tokens.refresh_token === 'string' && tokens.refresh_token
          ? tokens.refresh_token : previousRefresh;
        try {{
          localStorage.setItem('auth_token', state.managementToken);
          localStorage.setItem('refresh_token', state.refreshToken);
        }} catch {{}}
        user = await request('/api/v1/auth/me', {{ headers: headers() }});
      }}
    }}
    const result = finish('sub2api', user, 'bearer');
    if (result.authenticated || ADAPTER === 'sub2api') return result;
  }}

  if (ADAPTER === 'new-api' || !ADAPTER) {{
    const authHeaders = (mode = state.authMode || 'bearer') => {{
      if (!state.managementToken || mode === 'cookie') return {{}};
      return {{
        Authorization: mode === 'raw' ? state.managementToken : `Bearer ${{state.managementToken}}`,
        ...(state.userId ? {{ 'New-Api-User': String(state.userId) }} : {{}}),
      }};
    }};
    let user = state.managementToken
      ? await request('/api/user/self', {{ headers: authHeaders('bearer') }})
      : await request('/api/user/self');
    if (!isUserData(dataOf(user)) && state.managementToken) user = await request('/api/user/self');
    if (!isUserData(dataOf(user))) {{
      const refreshed = await request('/api/user/auth/refresh', {{ method: 'POST' }});
      const tokens = dataOf(refreshed);
      if (tokens && typeof tokens.access_token === 'string') {{
        state.managementToken = tokens.access_token;
        state.authMode = 'bearer';
        state.dashboardSessionId = typeof tokens.session?.sid === 'string' ? tokens.session.sid : '';
        user = await request('/api/user/self', {{ headers: authHeaders('bearer') }});
      }}
    }}
    if (!isUserData(dataOf(user)) && state.managementToken) {{
      user = await request('/api/user/self', {{ headers: authHeaders('raw') }});
      if (isUserData(dataOf(user))) state.authMode = 'raw';
    }} else if (isUserData(dataOf(user))) {{
      state.authMode = state.managementToken ? 'bearer' : 'cookie';
    }}
    return finish('new-api', user, state.authMode || 'cookie', state.dashboardSessionId || '');
  }}

  return {{ authenticated: false, error: '当前账号缺少可识别的中转站类型。' }};
}})()
""".strip()


def _probe_script(expected_origin: str) -> str:
    """Return a fixed, auditable same-origin probe. No user string becomes code."""

    expected = json.dumps(expected_origin)
    return f"""
(async () => {{
  const EXPECTED = {expected};
  if (location.origin !== EXPECTED) {{
    return {{ error: '登录窗口当前位于第三方授权页，请完成登录并返回中转站页面后再识别。' }};
  }}
  const state = window.__agentManagerRelayState || (window.__agentManagerRelayState = {{}});
  const parseStored = (value) => {{
    if (!value) return null;
    try {{ return JSON.parse(value); }} catch {{ return value; }}
  }};
  const tokenFrom = (value) => {{
    if (!value) return '';
    if (typeof value === 'string') return value;
    if (typeof value !== 'object') return '';
    return value.access_token || value.accessToken || value.token || value.auth_token || '';
  }};
  if (!state.managementToken) {{
    const stores = [localStorage, sessionStorage];
    const names = ['auth_token', 'access_token', 'token', 'user', 'auth_user'];
    for (const store of stores) {{
      for (const name of names) {{
        try {{
          const stored = parseStored(store.getItem(name));
          const candidate = tokenFrom(stored);
          if (stored && typeof stored === 'object' && (stored.id || stored.user_id)) {{
            state.userId = stored.id || stored.user_id;
          }}
          if (candidate && typeof candidate === 'string') {{ state.managementToken = candidate; break; }}
        }} catch {{}}
      }}
      if (state.managementToken) break;
    }}
  }}
  if (!state.refreshToken) {{
    try {{ state.refreshToken = localStorage.getItem('refresh_token') || ''; }} catch {{}}
  }}
  const request = async (path, options = {{}}) => {{
    const url = new URL(path, EXPECTED);
    if (url.origin !== EXPECTED) return {{ status: 0, body: null, json: false, error: 'cross-origin blocked' }};
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {{
      const response = await fetch(url.href, {{
        method: options.method || 'GET',
        credentials: 'include',
        cache: 'no-store',
        headers: {{ Accept: 'application/json', ...(options.headers || {{}}) }},
        body: options.body,
        signal: controller.signal,
      }});
      const text = await response.text();
      let body = null;
      let json = true;
      try {{ body = text ? JSON.parse(text) : {{}}; }} catch {{ json = false; body = {{ message: text.slice(0, 240) }}; }}
      return {{
        status: response.status,
        body,
        json,
        contentType: String(response.headers.get('content-type') || '').slice(0, 120),
      }};
    }} catch (error) {{
      return {{ status: 0, body: null, json: false, error: String(error && error.message || error).slice(0, 240) }};
    }} finally {{ clearTimeout(timer); }}
  }};
  const dataOf = (response) => {{
    if (!response || response.json !== true || response.status < 200 || response.status >= 300) return null;
    const body = response && response.body;
    if (!body || typeof body !== 'object') return null;
    if (body.success === false) return null;
    if ('code' in body && ![0, 200, true, null].includes(body.code)) return null;
    return 'data' in body ? body.data : body;
  }};
  const isUserData = (value) => Boolean(
    value
    && typeof value === 'object'
    && !Array.isArray(value)
    && ['id', 'user_id', 'username', 'email', 'quota', 'balance'].some((key) => key in value)
  );
  const authHeaders = (mode = state.authMode || 'bearer', token = state.managementToken) => {{
    if (!token || mode === 'cookie') return {{}};
    return {{
      Authorization: mode === 'raw' ? token : `Bearer ${{token}}`,
      ...(state.userId ? {{ 'New-Api-User': String(state.userId) }} : {{}}),
    }};
  }};

  // Sub2API is probed first.  Current releases use /api/v1 plus a user-UI
  // marker header; probing legacy /api/user/self first lets an SPA HTML
  // fallback masquerade as a successful New API response.
  const subHeaders = () => ({{
    ...(state.managementToken ? {{ Authorization: `Bearer ${{state.managementToken}}` }} : {{}}),
    'X-User-UI-Request': '1',
  }});
  const subSettings = await request('/api/v1/settings/public');
  let subUser = await request('/api/v1/auth/me', {{ headers: subHeaders() }});
  if (!isUserData(dataOf(subUser)) && state.refreshToken) {{
    const refreshToken = state.refreshToken;
    const refreshed = await request('/api/v1/auth/refresh', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json', 'X-User-UI-Request': '1' }},
      body: JSON.stringify({{ refresh_token: refreshToken }}),
    }});
    const tokens = dataOf(refreshed);
    if (tokens && typeof tokens.access_token === 'string') {{
      state.managementToken = tokens.access_token;
      state.refreshToken = typeof tokens.refresh_token === 'string' ? tokens.refresh_token : refreshToken;
      try {{
        localStorage.setItem('auth_token', state.managementToken);
        localStorage.setItem('refresh_token', state.refreshToken);
        if (Number(tokens.expires_in) > 0) {{
          localStorage.setItem('token_expires_at', String(Date.now() + Number(tokens.expires_in) * 1000));
        }}
      }} catch {{}}
      subUser = await request('/api/v1/auth/me', {{ headers: subHeaders() }});
    }}
  }}
  if (isUserData(dataOf(subUser))) {{
    const [keys, stats, groups, groupRates, models] = await Promise.all([
      request('/api/v1/keys?page=1&page_size=200&sort_by=created_at&sort_order=desc', {{ headers: subHeaders() }}),
      request('/api/v1/usage/dashboard/stats', {{ headers: subHeaders() }}),
      request('/api/v1/groups/available', {{ headers: subHeaders() }}),
      request('/api/v1/groups/rates', {{ headers: subHeaders() }}),
      request('/api/v1/usage/dashboard/models', {{ headers: subHeaders() }}),
    ]);
    const keyData = dataOf(keys);
    const keyRows = Array.isArray(keyData)
      ? keyData
      : keyData && Array.isArray(keyData.items)
        ? keyData.items
        : [];
    const keyIds = keyRows
      .map((item) => Number(item && item.id))
      .filter((id) => Number.isFinite(id) && id > 0)
      .slice(0, 200);
    const keyUsage = keyIds.length
      ? await request('/api/v1/usage/dashboard/api-keys-usage', {{
          method: 'POST',
          headers: {{ ...subHeaders(), 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ api_key_ids: keyIds }}),
        }})
      : {{ status: 200, json: true, body: {{ stats: {{}} }} }};
    return {{
      adapter: 'sub2api',
      status: subSettings,
      user: subUser,
      keys: [keys],
      stats,
      models: [models],
      groups,
      groupRates,
      keyUsage,
      session: {{
        accessToken: state.managementToken || '',
        refreshToken: state.refreshToken || '',
        authMode: 'bearer',
        userId: state.userId || '',
      }},
    }};
  }}

  // New API 2026 keeps the short-lived access token only in page memory and
  // the refresh credential in an HttpOnly cookie. A same-origin refresh lets
  // this isolated window obtain a temporary token without exporting cookies.
  const refresh = await request('/api/user/auth/refresh', {{ method: 'POST' }});
  const refreshed = dataOf(refresh);
  if (refreshed && typeof refreshed.access_token === 'string') {{
    state.managementToken = refreshed.access_token;
    state.authMode = 'bearer';
    state.dashboardSessionId = typeof refreshed.session?.sid === 'string' ? refreshed.session.sid : '';
  }}

  const status = await request('/api/status');
  let user = await request('/api/user/self', {{ headers: authHeaders('bearer') }});
  if (isUserData(dataOf(user))) state.authMode = state.managementToken ? 'bearer' : 'cookie';
  if (!isUserData(dataOf(user))) {{
    user = await request('/api/user/self');
    if (isUserData(dataOf(user))) state.authMode = 'cookie';
  }}
  if (!isUserData(dataOf(user)) && state.managementToken) {{
    user = await request('/api/user/self', {{ headers: authHeaders('raw') }});
    if (isUserData(dataOf(user))) state.authMode = 'raw';
  }}
  if (isUserData(dataOf(user))) {{
    const userData = dataOf(user);
    if (userData && (userData.id || userData.user_id)) state.userId = userData.id || userData.user_id;
    const managementHeaders = authHeaders();
    const keys = await Promise.all([
      request('/api/token/?p=1&size=100&page_size=100', {{ headers: managementHeaders }}),
      request('/api/token/?p=0&size=100&page_size=100', {{ headers: managementHeaders }}),
    ]);
    const models = await Promise.all([
      request('/api/user/models', {{ headers: managementHeaders }}),
      request('/api/user/available_models', {{ headers: managementHeaders }}),
      request('/api/models', {{ headers: managementHeaders }}),
    ]);
    return {{
      adapter: 'new-api', status, user, keys, models,
      session: {{
        accessToken: state.managementToken || '',
        refreshToken: '',
        sessionId: state.dashboardSessionId || '',
        authMode: state.authMode || 'cookie',
        userId: state.userId || '',
      }},
    }};
  }}

  const statusData = dataOf(subSettings) || dataOf(status);
  const systemName = statusData && (statusData.site_name || statusData.system_name || statusData.name);
  return {{
    adapter: '', status: dataOf(subSettings) ? subSettings : status, user, keys: [], models: [],
    message: systemName
      ? `已连接 ${{systemName}}，但尚未识别到登录会话；请确认弹窗中已经进入控制台。`
      : '没有识别到受支持的中转站模板或登录会话。',
  }};
}})()
""".strip()


def _reveal_script(expected_origin: str, adapter: str, key_id: str) -> str:
    expected = json.dumps(expected_origin)
    key_literal = json.dumps(key_id)
    adapter_literal = json.dumps(adapter)
    return f"""
(async () => {{
  const EXPECTED = {expected};
  const KEY_ID = {key_literal};
  const ADAPTER = {adapter_literal};
  if (location.origin !== EXPECTED) return {{ error: '登录窗口不在中转站页面。' }};
  const state = window.__agentManagerRelayState || {{}};
  const token = state.managementToken || (() => {{
    try {{ return localStorage.getItem('auth_token') || ''; }} catch {{ return ''; }}
  }})();
  const headers = {{
    Accept: 'application/json',
    ...(token ? {{ Authorization: `Bearer ${{token}}` }} : {{}}),
    ...(ADAPTER === 'sub2api' ? {{ 'X-User-UI-Request': '1' }} : {{}}),
  }};
  const request = async (path, options = {{}}) => {{
    const url = new URL(path, EXPECTED);
    if (url.origin !== EXPECTED) return {{ status: 0, body: null }};
    const response = await fetch(url.href, {{
      method: options.method || 'GET', credentials: 'include', cache: 'no-store',
      headers: {{ ...headers, ...(options.headers || {{}}) }},
    }});
    const text = await response.text();
    let body = null; try {{ body = text ? JSON.parse(text) : {{}}; }} catch {{ body = null; }}
    return {{ status: response.status, body }};
  }};
  if (ADAPTER === 'new-api') {{
    let result = await request(`/api/token/${{encodeURIComponent(KEY_ID)}}/key`, {{ method: 'POST' }});
    if (result.status >= 200 && result.status < 300) return result;
    result = await request(`/api/token/${{encodeURIComponent(KEY_ID)}}`);
    return result;
  }}
  const result = await request(`/api/v1/keys/${{encodeURIComponent(KEY_ID)}}`);
  return result;
}})()
""".strip()


def _create_script(
    expected_origin: str,
    adapter: str,
    name: str,
    relay_group_id: object = None,
) -> str:
    expected = json.dumps(expected_origin)
    adapter_literal = json.dumps(adapter)
    name_literal = json.dumps(name)
    relay_group_literal = json.dumps(relay_group_id)
    return f"""
(async () => {{
  const EXPECTED = {expected};
  const ADAPTER = {adapter_literal};
  const NAME = {name_literal};
  const RELAY_GROUP_ID = {relay_group_literal};
  if (location.origin !== EXPECTED) return {{ error: '登录窗口不在中转站页面。' }};
  const state = window.__agentManagerRelayState || {{}};
  const token = state.managementToken || (() => {{
    try {{ return localStorage.getItem('auth_token') || ''; }} catch {{ return ''; }}
  }})();
  const headers = {{
    Accept: 'application/json', 'Content-Type': 'application/json',
    ...(token ? {{ Authorization: `Bearer ${{token}}` }} : {{}}),
    ...(ADAPTER === 'sub2api' ? {{ 'X-User-UI-Request': '1' }} : {{}}),
  }};
  const path = ADAPTER === 'new-api' ? '/api/token/' : '/api/v1/keys';
  const payload = ADAPTER === 'new-api'
    ? {{ name: NAME, remain_quota: 0, expired_time: -1, unlimited_quota: true,
        model_limits_enabled: false, model_limits: '', allow_ips: '', group: '', auto_groups: [], cross_group_retry: false }}
    : {{ name: NAME, group_id: RELAY_GROUP_ID }};
  const response = await fetch(new URL(path, EXPECTED).href, {{
    method: 'POST', credentials: 'include', cache: 'no-store', headers,
    body: JSON.stringify(payload),
  }});
  const text = await response.text();
  let body = null; try {{ body = text ? JSON.parse(text) : {{}}; }} catch {{ body = {{ message: text.slice(0, 240) }}; }}
  return {{ status: response.status, body }};
}})()
""".strip()


def _window_cookie_records(window: object, expected_origin: str) -> list[dict]:
    """Copy only cookies owned by the isolated WebView's relay origin."""

    getter = getattr(window, "get_cookies", None)
    if not callable(getter):
        return []
    try:
        containers = getter() or []
    except Exception:
        return []
    host = str(urllib.parse.urlsplit(expected_origin).hostname or "").casefold().rstrip(".")
    result: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for container in containers if isinstance(containers, (list, tuple)) else [containers]:
        if isinstance(container, dict) and "name" in container:
            candidates = [container]
        elif hasattr(container, "values"):
            try:
                candidates = list(container.values())
            except Exception:
                candidates = []
        else:
            candidates = [container]
        for candidate in candidates:
            if hasattr(candidate, "key") and hasattr(candidate, "value"):
                name = _text(getattr(candidate, "key", ""), 256)
                value = str(getattr(candidate, "value", "") or "")
                try:
                    domain = _text(candidate["domain"], 253).casefold()
                    path = _text(candidate["path"] or "/", 2_048) or "/"
                    secure = bool(candidate["secure"])
                    http_only = bool(candidate["httponly"])
                    expires = _text(candidate["expires"], 160)
                except Exception:
                    domain, path, secure, http_only, expires = "", "/", False, False, ""
            elif isinstance(candidate, dict):
                name = _text(candidate.get("name"), 256)
                value = str(candidate.get("value") or "")
                domain = _text(candidate.get("domain"), 253).casefold()
                path = _text(candidate.get("path") or "/", 2_048) or "/"
                secure = bool(candidate.get("secure"))
                http_only = bool(candidate.get("httponly") or candidate.get("httpOnly"))
                expires = _text(candidate.get("expires"), 160)
            else:
                name, value, domain, path, secure, http_only, expires = "", "", "", "/", False, False, ""
            normalized_domain = domain.lstrip(".").rstrip(".") or host
            if not name or not value or not host:
                continue
            if normalized_domain != host and not host.endswith(f".{normalized_domain}"):
                continue
            if not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", name):
                continue
            if len(value) > 16_384 or any(char in value for char in "\r\n;"):
                continue
            identity = (name, normalized_domain, path)
            if identity in seen:
                continue
            seen.add(identity)
            result.append(
                {
                    "name": name,
                    "value": value,
                    "domain": normalized_domain,
                    "path": path,
                    "secure": secure,
                    "httpOnly": http_only,
                    "expires": expires,
                }
            )
            if len(result) >= 80:
                return result
    return result


def _dashboard_session_from_probe(
    raw: object,
    *,
    portal_url: str,
    window: object,
) -> dict | None:
    if not isinstance(raw, dict) or raw.get("adapter") not in {"new-api", "sub2api"}:
        return None
    auth = raw.get("session") if isinstance(raw.get("session"), dict) else {}
    origin = urllib.parse.urlunsplit((*urllib.parse.urlsplit(portal_url)[:2], "", "", ""))
    cookies = _window_cookie_records(window, origin)
    access_token = str(auth.get("accessToken") or "").strip()
    refresh_token = str(auth.get("refreshToken") or "").strip()
    if not access_token and not refresh_token and not cookies:
        return None
    return {
        "version": 1,
        "origin": origin,
        "portalUrl": portal_url,
        "adapter": str(raw.get("adapter") or ""),
        "authMode": _text(auth.get("authMode") or "bearer", 20),
        "userId": _text(auth.get("userId"), 120),
        "sessionId": _text(auth.get("sessionId"), 256),
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "cookies": cookies,
        "updatedAt": core.now_iso(),
    }


def _dashboard_user_identity(value: object) -> dict[str, str]:
    """Extract stable, non-secret identity fields from a dashboard response."""

    data = _response_data(value)
    if not isinstance(data, dict):
        return {"id": "", "email": "", "name": ""}
    return {
        "id": _text(data.get("id") or data.get("user_id"), 120),
        "email": _text(data.get("email"), 180).casefold(),
        "name": _text(
            data.get("display_name") or data.get("username") or data.get("name"),
            160,
        ),
    }


def _quick_dashboard_account_snapshot(
    value: object,
    adapter: str,
    *,
    quota_per_unit: object = None,
) -> dict:
    """Normalize only user and balance fields returned by the auth endpoint."""

    data = _response_data(value)
    if not isinstance(data, dict):
        return {
            "user": {},
            "balance": None,
            "quotaPerUnit": None,
            "balanceUnavailableReason": "balance_missing",
        }
    user_group = data.get("group") or data.get("plan")
    if isinstance(user_group, dict):
        user_group = user_group.get("name") or user_group.get("id")
    user = {
        "id": _text(data.get("id") or data.get("user_id"), 80),
        "name": _text(
            data.get("display_name") or data.get("username") or data.get("name"),
            160,
        ),
        "email": _text(data.get("email"), 180),
        "group": _text(user_group, 120),
    }
    if adapter == "new-api":
        reported_quota_per_unit = _number(data.get("quota_per_unit"))
        cached_quota_per_unit = _number(quota_per_unit)
        effective_quota_per_unit = (
            reported_quota_per_unit
            if reported_quota_per_unit is not None and reported_quota_per_unit > 0
            else cached_quota_per_unit
            if cached_quota_per_unit is not None and cached_quota_per_unit > 0
            else None
        )
        remaining_raw = _number(data.get("quota"))
        used_raw = _number(data.get("used_quota"))
        remaining = (
            remaining_raw / effective_quota_per_unit
            if remaining_raw is not None and effective_quota_per_unit is not None
            else 0.0 if remaining_raw == 0
            else _number(data.get("balance"))
        )
        used = (
            used_raw / effective_quota_per_unit
            if used_raw is not None and effective_quota_per_unit is not None
            else 0.0 if used_raw == 0
            else None
        )
        currency = "USD"
        balance_unavailable_reason = (
            "quota_per_unit_unknown"
            if effective_quota_per_unit is None
            and (remaining_raw is not None or used_raw is not None)
            and remaining is None
            else "balance_missing"
            if remaining is None and used is None
            else ""
        )
    else:
        effective_quota_per_unit = None
        remaining = _number(
            data.get("balance") if "balance" in data else data.get("quota")
        )
        used = _number(
            data.get("used") if "used" in data else data.get("used_balance")
        )
        currency = _text(data.get("currency"), 12).upper() or "USD"
        balance_unavailable_reason = (
            "balance_missing" if remaining is None and used is None else ""
        )
    balance = (
        {
            "remaining": round(remaining, 6) if remaining is not None else None,
            "used": round(used, 6) if used is not None else None,
            "currency": currency,
        }
        if remaining is not None or used is not None
        else None
    )
    return {
        "user": user,
        "balance": balance,
        "quotaPerUnit": effective_quota_per_unit,
        "balanceUnavailableReason": balance_unavailable_reason,
    }


def _assert_dashboard_identity(
    expected: dict,
    actual: dict,
    *,
    expected_adapter: str = "",
    actual_adapter: str = "",
) -> None:
    """Require a stable match before credentials can replace a saved session."""

    if expected_adapter and actual_adapter and expected_adapter != actual_adapter:
        raise DashboardIdentityChanged(
            "登录窗口识别到了不同类型的中转站账号，未覆盖原凭据。"
        )
    expected_id = _text(expected.get("id"), 120)
    actual_id = _text(actual.get("id"), 120)
    if expected_id:
        if not actual_id:
            raise core.ManagerError("登录已完成，但站点没有返回可核验的用户 ID，未保存凭据。")
        if actual_id != expected_id:
            raise DashboardIdentityChanged(
                "当前网页登录的是另一个中转站账号，未覆盖原账号凭据。"
            )
        return
    expected_email = _text(expected.get("email"), 180).casefold()
    actual_email = _text(actual.get("email"), 180).casefold()
    if expected_email:
        if not actual_email:
            raise core.ManagerError("登录已完成，但站点没有返回可核验的邮箱，未保存凭据。")
        if actual_email != expected_email:
            raise DashboardIdentityChanged(
                "当前网页登录的是另一个中转站账号，未覆盖原账号凭据。"
            )
        return
    raise core.ManagerError("现有中转站账号缺少可核验的用户 ID 或邮箱，无法安全自动续登。")


def _cookie_header(session: dict, url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    host = str(parsed.hostname or "").casefold().rstrip(".")
    request_path = parsed.path or "/"
    secure_request = parsed.scheme.casefold() == "https"
    pairs: list[str] = []
    for item in session.get("cookies", []) if isinstance(session.get("cookies"), list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        domain = str(item.get("domain") or host).casefold().lstrip(".").rstrip(".")
        path = str(item.get("path") or "/")
        if not name or not value or (domain != host and not host.endswith(f".{domain}")):
            continue
        if item.get("secure") and not secure_request:
            continue
        if item.get("hostOnly") and domain != host:
            continue
        expiry = _cookie_expiry(item.get("expires"))
        if expiry is not None and expiry <= time.time():
            continue
        if path != "/" and not request_path.startswith(path.rstrip("/") + "/") and request_path != path:
            continue
        if not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", name):
            continue
        if any(char in value for char in "\r\n;"):
            continue
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def _cookie_expiry(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
    return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed).timestamp()


def _merge_response_cookies(session: dict, url: str, headers: object) -> None:
    values = headers.get_all("Set-Cookie") if hasattr(headers, "get_all") else [headers.get("Set-Cookie")] if hasattr(headers, "get") else []
    if not values:
        return
    parsed_url = urllib.parse.urlsplit(url)
    host = str(parsed_url.hostname or "").casefold()
    default_path = parsed_url.path.rpartition("/")[0] or "/"
    with COOKIE_LOCK:
        current = { (str(row.get("name")), str(row.get("domain") or host).lstrip(".").casefold(), str(row.get("path") or "/")): dict(row)
                    for row in session.get("cookies", []) if isinstance(row, dict) }
        for value in values[:80]:
            if not isinstance(value, str) or len(value) > 32768:
                continue
            jar = SimpleCookie()
            try:
                jar.load(value)
            except CookieError:
                continue
            for name, cookie in jar.items():
                domain = cookie["domain"].lstrip(".").rstrip(".").casefold() or host
                path = cookie["path"] if cookie["path"].startswith("/") else default_path
                if (domain != host and not host.endswith("." + domain)) or len(path) > 2048:
                    continue
                if len(cookie.value) > 16384 or any(char in cookie.value for char in "\r\n;"):
                    continue
                secure = bool(cookie["secure"])
                if name.startswith("__Host-") and (cookie["domain"] or path != "/" or not secure):
                    continue
                if name.startswith("__Secure-") and not secure:
                    continue
                expires = cookie["expires"]
                expiry = _cookie_expiry(expires)
                if cookie["max-age"]:
                    try:
                        age = int(cookie["max-age"])
                        expiry = time.time() + min(max(age, -1), 315360000)
                        expires = datetime.fromtimestamp(expiry, timezone.utc).isoformat()
                    except (ValueError, OverflowError):
                        continue
                key = (name, domain, path)
                if not cookie.value or (expiry is not None and expiry <= time.time()):
                    current.pop(key, None)
                else:
                    current.pop(key, None)
                    current[key] = {"name": name, "value": cookie.value, "domain": domain, "path": path,
                                    "secure": secure, "httpOnly": bool(cookie["httponly"]),
                                    "hostOnly": not bool(cookie["domain"]), "expires": expires}
        session["cookies"] = list(current.values())[-80:]


def _saved_auth_headers(session: dict, *, mode: str | None = None) -> dict[str, str]:
    adapter = str(session.get("adapter") or "")
    token = str(session.get("accessToken") or "").strip()
    selected_mode = str(mode or session.get("authMode") or "bearer")
    headers: dict[str, str] = {}
    if adapter == "sub2api":
        headers["X-User-UI-Request"] = "1"
    if token and selected_mode != "cookie":
        headers["Authorization"] = token if selected_mode == "raw" else f"Bearer {token}"
    if adapter == "new-api" and session.get("userId") and selected_mode != "cookie":
        headers["New-Api-User"] = str(session["userId"])
    return headers


def _saved_json_request(
    session: dict,
    path: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: object = None,
    timeout: float = 15.0,
) -> dict:
    origin = core._validated_provider_portal_url(session.get("origin"))
    target = urllib.parse.urljoin(origin.rstrip("/") + "/", str(path or "").lstrip("/"))
    if core._provider_url_origin(target) != core._provider_url_origin(origin):
        raise core.ManagerError("中转站管理接口尝试跨来源访问，已拒绝发送登录凭据。")
    request_headers = {
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "User-Agent": "Agent-Manager/7.1.8",
        **(headers or {}),
    }
    if method.upper() not in {"GET", "HEAD"}:
        parsed_origin = urllib.parse.urlsplit(origin)
        request_headers["Origin"] = urllib.parse.urlunsplit((parsed_origin.scheme, parsed_origin.netloc, "", "", ""))
        request_headers["Referer"] = request_headers["Origin"] + "/"
    if str(path).split("?")[0] == "/api/user/auth/refresh" and session.get("sessionId"):
        request_headers["X-Auth-Session"] = str(session["sessionId"])
    cookie = _cookie_header(session, target)
    if cookie:
        request_headers["Cookie"] = cookie
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(target, data=body, method=method, headers=request_headers)
    response_object: object
    try:
        response_object = core._open_same_origin_request(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response_object = exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DashboardTemporarilyUnavailable(
            f"无法连接中转站管理接口：{core._redact_sensitive_text(exc, limit=240)}"
        ) from exc
    with response_object:
        status = int(getattr(response_object, "status", 0) or getattr(response_object, "code", 0) or 0)
        # Capture rotation headers even if the response body is interrupted.
        _merge_response_cookies(session, target, getattr(response_object, "headers", {}))
        try:
            raw_body = response_object.read(4 * 1024 * 1024 + 1)
        except core._CHATGPT_TRANSPORT_EXCEPTIONS as exc:
            raise DashboardTemporarilyUnavailable("中转站响应暂时中断，已保留登录凭据。") from exc
        if len(raw_body) > 4 * 1024 * 1024:
            raise core.ManagerError("中转站管理接口返回内容过大，已停止读取。")
        content_type = str(getattr(response_object, "headers", {}).get("Content-Type", ""))[:120]
    try:
        parsed_body = json.loads(raw_body.decode("utf-8-sig")) if raw_body else {}
        is_json = isinstance(parsed_body, (dict, list))
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed_body = {"message": raw_body.decode("utf-8", errors="replace")[:240]}
        is_json = False
    return {
        "status": status,
        "body": parsed_body,
        "json": is_json,
        "contentType": content_type,
    }


def _saved_parallel_requests(
    session: dict,
    requests: dict[str, tuple[str, dict]],
) -> dict[str, dict]:
    """Run independent same-origin dashboard reads in one bounded wave.

    Relay dashboards commonly expose keys, groups, rates, models and usage as
    separate endpoints.  Waiting for each response serially made a healthy
    account refresh feel hung.  Authentication remains serialized, while this
    helper only overlaps independent read-only requests and preserves the same
    per-request validation performed by ``_saved_json_request``.
    """

    if not requests:
        return {}
    worker_count = max(1, min(6, len(requests)))
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="relay-refresh",
    ) as executor:
        pending = {
            name: executor.submit(_saved_json_request, session, path, **kwargs)
            for name, (path, kwargs) in requests.items()
        }
        return {name: future.result() for name, future in pending.items()}


def _is_user_response(value: object) -> bool:
    data = _response_data(value)
    return bool(
        isinstance(data, dict)
        and any(key in data for key in ("id", "user_id", "username", "email", "quota", "balance"))
    )


def _raise_dashboard_user_error(response: dict) -> None:
    status = int(response.get("status") or 0)
    if status in {401, 403}:
        raise DashboardLoginRequired("中转站登录凭据已失效，请重新登录一次以续期。")
    raise DashboardTemporarilyUnavailable(f"中转站用户接口暂不可用（HTTP {status}），已保留登录凭据。")


def _dashboard_response_is_transient(response: dict) -> bool:
    status = int(response.get("status") or 0)
    return status == 429 or status >= 500 or response.get("json") is False


def _check_dashboard_transient(response: dict) -> None:
    if _dashboard_response_is_transient(response):
        raise DashboardTemporarilyUnavailable(f"中转站接口暂不可用（HTTP {response.get('status') or 0}），已保留登录凭据。")


def _verify_dashboard_identity(current: dict, response: dict, original_user_id: str) -> None:
    data = _response_data(response)
    actual = str(data.get("id") or data.get("user_id") or "") if isinstance(data, dict) else ""
    if original_user_id and actual and actual != original_user_id:
        raise DashboardIdentityChanged("中转站续期返回了不同账号，未覆盖原凭据；请重新登录并确认账号。")
    if actual:
        current["userId"] = actual


def _probe_saved_dashboard_quick(
    session: dict,
    *,
    quota_per_unit: object = None,
) -> tuple[dict, dict]:
    """Validate one saved login without enumerating dashboard catalogs.

    Healthy sessions use one user request. Expired access credentials may add
    one refresh and one user retry (New API can add one bounded SID retry).
    A legacy New API account with raw quota but no cached divisor may add one
    status request. Key, group, usage, and dashboard-model catalogs are never
    read.
    """

    current = session
    request_count = 0

    def request(path: str, **kwargs) -> dict:
        nonlocal request_count
        request_count += 1
        return _saved_json_request(current, path, **kwargs)

    original_user_id = str(current.get("userId") or "")
    adapter = str(current.get("adapter") or "")
    if adapter == "sub2api":
        headers = _saved_auth_headers(current)
        user = request("/api/v1/auth/me", headers=headers)
        _check_dashboard_transient(user)
        refreshed: dict | None = None
        if not _is_user_response(user) and current.get("refreshToken"):
            refreshed = request(
                "/api/v1/auth/refresh",
                method="POST",
                headers={"X-User-UI-Request": "1"},
                payload={"refresh_token": current["refreshToken"]},
            )
            _check_dashboard_transient(refreshed)
            tokens = _response_data(refreshed)
            if isinstance(tokens, dict) and isinstance(tokens.get("access_token"), str):
                current["accessToken"] = tokens["access_token"]
                if isinstance(tokens.get("refresh_token"), str) and tokens["refresh_token"].strip():
                    current["refreshToken"] = tokens["refresh_token"]
                headers = _saved_auth_headers(current)
                user = request("/api/v1/auth/me", headers=headers)
        if not _is_user_response(user):
            if refreshed is not None and _dashboard_response_is_transient(refreshed):
                _check_dashboard_transient(refreshed)
            _raise_dashboard_user_error(user)
        _verify_dashboard_identity(current, user, original_user_id)
        current["authMode"] = "bearer"
        account_snapshot = _quick_dashboard_account_snapshot(user, "sub2api")
        return (
            {
                "adapter": "sub2api",
                "user": user,
                "_accountUser": account_snapshot["user"],
                "_balance": account_snapshot["balance"],
                "_balanceUnavailableReason": account_snapshot["balanceUnavailableReason"],
                "_quotaPerUnit": None,
                "_quotaPerUnitLearned": None,
                "_refreshScope": "account",
                "_requestCount": request_count,
            },
            current,
        )

    if adapter != "new-api":
        raise DashboardLoginRequired("中转站网页登录凭据类型不受支持，请重新登录。")

    # First try the current short-lived token/cookie. Refresh only after the
    # user endpoint has actually rejected it, avoiding a rotation on every
    # card refresh.
    mode = str(current.get("authMode") or "bearer")
    user_mode = mode
    user = request("/api/user/self", headers=_saved_auth_headers(current, mode=mode))
    _check_dashboard_transient(user)
    if not _is_user_response(user) and current.get("accessToken") and mode != "cookie":
        user = request(
            "/api/user/self",
            headers=_saved_auth_headers(current, mode="cookie"),
        )
        user_mode = "cookie"
        _check_dashboard_transient(user)

    refreshed: dict | None = None
    if not _is_user_response(user):
        refreshed = request("/api/user/auth/refresh", method="POST")
        if (
            refreshed.get("status") == 409
            and "AUTH_SESSION_MISMATCH"
            in json.dumps(refreshed.get("body"), ensure_ascii=False)
        ):
            current.pop("sessionId", None)
            refreshed = request("/api/user/auth/refresh", method="POST")
        refreshed_data = _response_data(refreshed)
        if isinstance(refreshed_data, dict) and isinstance(refreshed_data.get("access_token"), str):
            current["accessToken"] = refreshed_data["access_token"]
            current["authMode"] = "bearer"
            if isinstance(refreshed_data.get("session"), dict) and refreshed_data["session"].get("sid"):
                current["sessionId"] = str(refreshed_data["session"]["sid"])
            user = request(
                "/api/user/self",
                headers=_saved_auth_headers(current, mode="bearer"),
            )
            user_mode = "bearer"
            _check_dashboard_transient(user)

    if _is_user_response(user):
        current["authMode"] = user_mode
    elif current.get("accessToken"):
        user = request(
            "/api/user/self",
            headers=_saved_auth_headers(current, mode="raw"),
        )
        _check_dashboard_transient(user)
        if _is_user_response(user):
            current["authMode"] = "raw"
    if not _is_user_response(user):
        if refreshed is not None and _dashboard_response_is_transient(refreshed):
            _check_dashboard_transient(refreshed)
        _raise_dashboard_user_error(user)
    _verify_dashboard_identity(current, user, original_user_id)
    user_data = _response_data(user)
    reported_quota_per_unit = (
        _number(user_data.get("quota_per_unit"))
        if isinstance(user_data, dict)
        else None
    )
    cached_quota_per_unit = _number(quota_per_unit)
    effective_quota_per_unit = (
        reported_quota_per_unit
        if reported_quota_per_unit is not None and reported_quota_per_unit > 0
        else cached_quota_per_unit
        if cached_quota_per_unit is not None and cached_quota_per_unit > 0
        else None
    )
    quota_source = (
        "user"
        if reported_quota_per_unit is not None and reported_quota_per_unit > 0
        else "cache"
        if effective_quota_per_unit is not None
        else ""
    )
    has_raw_quota = bool(
        isinstance(user_data, dict)
        and ("quota" in user_data or "used_quota" in user_data)
    )
    has_direct_balance = bool(
        isinstance(user_data, dict) and _number(user_data.get("balance")) is not None
    )
    if has_raw_quota and not has_direct_balance and effective_quota_per_unit is None:
        # Older imported accounts may predate the persisted divisor. Spend at
        # most one extra bounded request to learn it; a failed status lookup
        # leaves the previous displayed balance untouched.
        status_response = request("/api/status", timeout=5.0)
        status_data = _response_data(status_response)
        status_quota_per_unit = (
            _number(status_data.get("quota_per_unit"))
            if isinstance(status_data, dict)
            else None
        )
        if status_quota_per_unit is not None and status_quota_per_unit > 0:
            effective_quota_per_unit = status_quota_per_unit
            quota_source = "status"
    account_snapshot = _quick_dashboard_account_snapshot(
        user,
        "new-api",
        quota_per_unit=effective_quota_per_unit,
    )
    return (
        {
            "adapter": "new-api",
            "user": user,
            "_accountUser": account_snapshot["user"],
            "_balance": account_snapshot["balance"],
            "_balanceUnavailableReason": account_snapshot["balanceUnavailableReason"],
            "_quotaPerUnit": account_snapshot["quotaPerUnit"],
            "_quotaPerUnitLearned": (
                account_snapshot["quotaPerUnit"]
                if quota_source in {"user", "status"}
                else None
            ),
            "_refreshScope": "account",
            "_requestCount": request_count,
        },
        current,
    )


def _probe_saved_dashboard(session: dict) -> tuple[dict, dict]:
    """Read one dashboard using only its DPAPI-restored same-origin session."""

    current = session
    original_user_id = str(current.get("userId") or "")
    adapter = str(current.get("adapter") or "")
    if adapter == "sub2api":
        sub_headers = _saved_auth_headers(current)
        initial = _saved_parallel_requests(
            current,
            {
                "status": ("/api/v1/settings/public", {}),
                "user": ("/api/v1/auth/me", {"headers": sub_headers}),
            },
        )
        status = initial["status"]
        user = initial["user"]
        _check_dashboard_transient(user)
        if not _is_user_response(user) and current.get("refreshToken"):
            refreshed = _saved_json_request(
                current,
                "/api/v1/auth/refresh",
                method="POST",
                headers={"X-User-UI-Request": "1"},
                payload={"refresh_token": current["refreshToken"]},
            )
            _check_dashboard_transient(refreshed)
            tokens = _response_data(refreshed)
            if isinstance(tokens, dict) and isinstance(tokens.get("access_token"), str):
                current["accessToken"] = tokens["access_token"]
                if isinstance(tokens.get("refresh_token"), str) and tokens["refresh_token"].strip():
                    current["refreshToken"] = tokens["refresh_token"]
                sub_headers = _saved_auth_headers(current)
                user = _saved_json_request(current, "/api/v1/auth/me", headers=sub_headers)
        if not _is_user_response(user):
            _raise_dashboard_user_error(user)
        _verify_dashboard_identity(current, user, original_user_id)
        dashboard = _saved_parallel_requests(
            current,
            {
                "keys": (
                    "/api/v1/keys?page=1&page_size=200&sort_by=created_at&sort_order=desc",
                    {"headers": sub_headers},
                ),
                "stats": ("/api/v1/usage/dashboard/stats", {"headers": sub_headers}),
                "groups": ("/api/v1/groups/available", {"headers": sub_headers}),
                "groupRates": ("/api/v1/groups/rates", {"headers": sub_headers}),
                "models": ("/api/v1/usage/dashboard/models", {"headers": sub_headers}),
            },
        )
        keys = dashboard["keys"]
        stats = dashboard["stats"]
        groups = dashboard["groups"]
        group_rates = dashboard["groupRates"]
        models = dashboard["models"]
        key_rows = _items(keys)
        numeric_ids = [
            int(item.get("id"))
            for item in key_rows
            if str(item.get("id") or "").isdigit() and int(item.get("id")) > 0
        ][:MAX_KEYS]
        key_usage = (
            _saved_json_request(
                current,
                "/api/v1/usage/dashboard/api-keys-usage",
                method="POST",
                headers=sub_headers,
                payload={"api_key_ids": numeric_ids},
            )
            if numeric_ids
            else {"status": 200, "json": True, "body": {"stats": {}}}
        )
        keys_complete, keys_total = _key_catalog_status([keys], page_size=200)
        current.update({"authMode": "bearer", "updatedAt": core.now_iso()})
        return (
            {
                "adapter": "sub2api",
                "status": status,
                "user": user,
                "keys": [keys],
                "stats": stats,
                "models": [models],
                "groups": groups,
                "groupRates": group_rates,
                "keyUsage": key_usage,
                "keysAuthoritative": keys_complete,
                "keysCatalogComplete": keys_complete,
                "keysCatalogTotal": keys_total,
                "groupsAuthoritative": _response_ok(groups),
            },
            current,
        )
    if adapter != "new-api":
        raise core.ManagerError("中转站网页登录凭据类型不受支持，请重新登录。")

    refreshed = _saved_json_request(current, "/api/user/auth/refresh", method="POST")
    if refreshed.get("status") == 409 and "AUTH_SESSION_MISMATCH" in json.dumps(refreshed.get("body"), ensure_ascii=False):
        current.pop("sessionId", None)
        refreshed = _saved_json_request(current, "/api/user/auth/refresh", method="POST")
    refreshed_data = _response_data(refreshed)
    if isinstance(refreshed_data, dict) and isinstance(refreshed_data.get("access_token"), str):
        current["accessToken"] = refreshed_data["access_token"]
        current["authMode"] = "bearer"
        if isinstance(refreshed_data.get("session"), dict) and refreshed_data["session"].get("sid"):
            current["sessionId"] = str(refreshed_data["session"]["sid"])
    initial = _saved_parallel_requests(
        current,
        {
            "status": ("/api/status", {}),
            "user": (
                "/api/user/self",
                {"headers": _saved_auth_headers(current, mode="bearer")},
            ),
        },
    )
    status = initial["status"]
    user = initial["user"]
    _check_dashboard_transient(user)
    if not _is_user_response(user) and _dashboard_response_is_transient(refreshed):
        _check_dashboard_transient(refreshed)
    if _is_user_response(user):
        current["authMode"] = "bearer" if current.get("accessToken") else "cookie"
    if not _is_user_response(user):
        user = _saved_json_request(current, "/api/user/self", headers=_saved_auth_headers(current, mode="cookie"))
        if _is_user_response(user):
            current["authMode"] = "cookie"
    if not _is_user_response(user) and current.get("accessToken"):
        user = _saved_json_request(current, "/api/user/self", headers=_saved_auth_headers(current, mode="raw"))
        if _is_user_response(user):
            current["authMode"] = "raw"
    if not _is_user_response(user):
        _raise_dashboard_user_error(user)
    _verify_dashboard_identity(current, user, original_user_id)
    user_data = _response_data(user)
    if isinstance(user_data, dict) and (user_data.get("id") or user_data.get("user_id")):
        current["userId"] = str(user_data.get("id") or user_data.get("user_id"))
    management_headers = _saved_auth_headers(current)
    dashboard = _saved_parallel_requests(
        current,
        {
            "keysPage1": (
                "/api/token/?p=1&size=100&page_size=100",
                {"headers": management_headers},
            ),
            "keysPage0": (
                "/api/token/?p=0&size=100&page_size=100",
                {"headers": management_headers},
            ),
            "userModels": ("/api/user/models", {"headers": management_headers}),
            "availableModels": (
                "/api/user/available_models",
                {"headers": management_headers},
            ),
            "models": ("/api/models", {"headers": management_headers}),
        },
    )
    keys = [dashboard["keysPage1"], dashboard["keysPage0"]]
    models = [dashboard["userModels"], dashboard["availableModels"], dashboard["models"]]
    keys_complete, keys_total = _key_catalog_status(keys, page_size=100)
    current["updatedAt"] = core.now_iso()
    return (
        {
            "adapter": "new-api",
            "status": status,
            "user": user,
            "keys": keys,
            "models": models,
            "keysAuthoritative": keys_complete,
            "keysCatalogComplete": keys_complete,
            "keysCatalogTotal": keys_total,
            "groupsAuthoritative": False,
        },
        current,
    )


@dataclass
class _Evaluation:
    event: threading.Event
    value: object = None


class RelayPortalService:
    """Own one isolated relay login window and its transient discovery state."""

    def __init__(self, *, window_factory: Callable[..., object] | None = None) -> None:
        self.lock = threading.RLock()
        self.window_factory = window_factory
        self.window: object | None = None
        self._preview: dict | None = None
        self._secrets: dict[str, str] = {}
        self._dashboard_session: dict | None = None
        self._session = self._empty_state()

    @staticmethod
    def _empty_state() -> dict:
        return {
            "sessionId": "",
            "status": "idle",
            "message": "输入中转站网址后，在独立窗口完成登录。",
            "mode": "new",
            "accountId": "",
            "portalUrl": "",
            "currentUrl": "",
            "adapter": "",
            "preview": None,
            "completion": None,
            "autoAuth": {
                "enabled": False,
                "state": "idle",
                "attempts": 0,
                "maxAttempts": AUTO_AUTH_MAX_ATTEMPTS,
                "nextCheckInMs": 0,
            },
            "error": None,
            "createdAt": None,
            "updatedAt": core.now_iso(),
        }

    def public_state(self) -> dict:
        with self.lock:
            state = {
                key: json.loads(json.dumps(value))
                for key, value in self._session.items()
                if not str(key).startswith("_")
            }
            if self._expired_locked():
                state["status"] = "expired"
                state["message"] = "中转站登录会话已超时，请重新打开登录窗口。"
                if isinstance(state.get("autoAuth"), dict):
                    state["autoAuth"].update({"state": "expired", "nextCheckInMs": 0})
            elif isinstance(state.get("autoAuth"), dict):
                auto_auth = state["autoAuth"]
                if auto_auth.get("enabled") and auto_auth.get("state") == "waiting":
                    last_check = float(self._session.get("_lastAutoCheckMonotonic") or 0)
                    remaining = max(
                        0.0,
                        AUTO_AUTH_CHECK_INTERVAL_SECONDS - (time.monotonic() - last_check),
                    )
                    auto_auth["nextCheckInMs"] = int(math.ceil(remaining * 1000))
            return state

    def _expired_locked(self) -> bool:
        created = float(self._session.get("_createdMonotonic") or 0)
        return bool(created and time.monotonic() - created > SESSION_TTL_SECONDS)

    def _require_current_session_locked(self, session_id: object) -> None:
        supplied = _text(session_id, 120)
        if not supplied or not secrets.compare_digest(supplied, str(self._session.get("sessionId") or "")):
            raise core.ManagerError("中转站登录会话已被替换，已丢弃旧请求结果。")
        if self._expired_locked():
            raise core.ManagerError("中转站登录会话已超时，请重新开始。")

    @staticmethod
    def _origin(url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    def _validate_session(self, session_id: object) -> tuple[object, str, str]:
        supplied = _text(session_id, 120)
        with self.lock:
            if not supplied or not secrets.compare_digest(supplied, str(self._session.get("sessionId") or "")):
                raise core.ManagerError("中转站登录会话不存在或已被替换，请重新开始。")
            if self._expired_locked():
                raise core.ManagerError("中转站登录会话已超时，请重新开始。")
            window = self.window
            portal_url = str(self._session.get("portalUrl") or "")
            expected_origin = self._origin(portal_url)
        if window is None:
            raise core.ManagerError("中转站登录窗口已经关闭，请重新打开。")
        try:
            current_url = str(window.get_current_url() or "")
        except Exception as exc:
            raise core.ManagerError("无法读取中转站登录窗口状态，请关闭后重试。") from exc
        if self._origin(current_url) != expected_origin:
            raise core.ManagerError("登录窗口当前位于第三方授权页，请完成授权并返回中转站后再继续。")
        with self.lock:
            self._require_current_session_locked(session_id)
            self._session["currentUrl"] = current_url
            self._session["updatedAt"] = core.now_iso()
        return window, portal_url, expected_origin

    def start(self, portal_url: object = None, account_id: object = None) -> dict:
        """Open an isolated login window for a new import or an existing account.

        Supplying ``account_id`` creates a reauthentication session.  Its
        origin, adapter and stable user identity are locked before the window
        opens so a later login cannot silently replace another saved account.
        """

        mode = "reauth" if _text(account_id, 120) else "new"
        account: dict | None = None
        expected_identity = {"id": "", "email": "", "name": ""}
        expected_adapter = ""
        normalized_account_id = ""
        if mode == "reauth":
            account, _settings = self._saved_account(account_id)
            account = json.loads(json.dumps(account))
            normalized_account_id = str(account.get("id") or "")
            account_url = account.get("portalUrl") or account.get("origin")
            url = core._validated_provider_portal_url(portal_url or account_url)
            account_origin = core._provider_url_origin(account.get("origin") or account_url)
            if core._provider_url_origin(url) != account_origin:
                raise core.ManagerError("续登网址与现有中转站账号来源不一致，已拒绝打开。")
            expected_adapter = _text(account.get("adapter"), 40)
            if expected_adapter not in {"new-api", "sub2api"}:
                raise core.ManagerError("现有中转站账号缺少受支持的站点类型，无法安全自动续登。")
            account_user = account.get("user") if isinstance(account.get("user"), dict) else {}
            saved_session: dict | None = None
            try:
                saved_session = core.load_relay_account_dashboard_session(
                    normalized_account_id,
                    required=False,
                )
            except core.ManagerError:
                # Reauthentication exists precisely to replace an unreadable
                # or expired session; account metadata can still lock identity.
                saved_session = None
            expected_identity = {
                "id": _text(
                    account_user.get("id")
                    or ((saved_session or {}).get("userId") if isinstance(saved_session, dict) else ""),
                    120,
                ),
                "email": _text(account_user.get("email"), 180).casefold(),
                "name": _text(account_user.get("name"), 160),
            }
            if not expected_identity["id"] and not expected_identity["email"]:
                raise core.ManagerError(
                    "现有中转站账号缺少可核验的用户 ID 或邮箱，无法安全自动续登。"
                )
        else:
            url = core._validated_provider_portal_url(portal_url)
        self.close(silent=True)
        try:
            import webview
        except Exception as exc:
            raise core.ManagerError("当前安装缺少桌面登录窗口组件，请改用自定义登录。") from exc
        if not getattr(webview, "windows", None) or getattr(webview.windows[0], "gui", None) is None:
            raise core.ManagerError("中转站登录仅在 Agent Manager 桌面窗口中可用。")
        factory = self.window_factory or webview.create_window
        session_id = secrets.token_urlsafe(18)
        host = urllib.parse.urlsplit(url).hostname or "中转站"
        with self.lock:
            self._preview = None
            self._secrets = {}
            self._dashboard_session = None
            self._session = {
                **self._empty_state(),
                "sessionId": session_id,
                "status": "waiting_login",
                "message": (
                    "请在弹出的隔离窗口重新登录；成功后会自动核验账号并继续刷新。"
                    if mode == "reauth"
                    else "请在弹出的隔离窗口完成登录；成功后会自动识别可导入的 API Key。"
                ),
                "mode": mode,
                "accountId": normalized_account_id,
                "portalUrl": url,
                "currentUrl": url,
                "autoAuth": {
                    "enabled": True,
                    "state": "waiting",
                    "attempts": 0,
                    "maxAttempts": AUTO_AUTH_MAX_ATTEMPTS,
                    "nextCheckInMs": 0,
                },
                "createdAt": core.now_iso(),
                "updatedAt": core.now_iso(),
                "_createdMonotonic": time.monotonic(),
                "_lastAutoCheckMonotonic": 0.0,
                "_autoAuthInFlight": False,
                "_fullScanStarted": False,
                "_cancelEvent": threading.Event(),
                "_expectedIdentity": expected_identity,
                "_expectedAdapter": expected_adapter,
            }
        try:
            window = factory(
                f"中转站登录 · {host}",
                url,
                width=1080,
                height=780,
                min_size=(760, 560),
                text_select=True,
                background_color="#111816",
            )
            if window is None:
                raise core.ManagerError("中转站登录窗口未能创建。")
            with self.lock:
                if self._session.get("sessionId") != session_id:
                    try:
                        window.destroy()
                    except Exception:
                        pass
                    raise core.ManagerError("中转站登录请求已被新的会话替换。")
                self.window = window

            def closed() -> None:
                with self.lock:
                    if self._session.get("sessionId") == session_id:
                        self.window = None
                        if self._session.get("status") not in {
                            "imported",
                            "cancelled",
                            "reauthenticated",
                            "identity_mismatch",
                            "reauth_timeout",
                            "reauth_error",
                        }:
                            self._session.update(
                                {
                                    "status": "closed",
                                    "message": "登录窗口已关闭；可以重新打开，不会影响已保存账号。",
                                    "updatedAt": core.now_iso(),
                                }
                            )

            window.events.closed += closed
            try:
                window.show()
            except Exception:
                pass
        except Exception as exc:
            with self.lock:
                if self._session.get("sessionId") == session_id:
                    self.window = None
                    self._session.update(
                    {
                        "status": "error",
                        "error": core._redact_sensitive_text(exc, limit=500),
                        "message": "中转站登录窗口打开失败。",
                        "updatedAt": core.now_iso(),
                    }
                )
            if isinstance(exc, core.ManagerError):
                raise
            raise core.ManagerError(f"中转站登录窗口打开失败：{str(exc)[:300]}") from exc
        return self.public_state()

    @staticmethod
    def _evaluate(window: object, script: str, timeout: float = SCAN_TIMEOUT_SECONDS) -> object:
        result = _Evaluation(threading.Event())

        def completed(value: object) -> None:
            result.value = value
            result.event.set()

        try:
            immediate = window.evaluate_js(script, callback=completed)
        except TypeError:
            # Unit-test fakes and older pywebview shims may expose only the
            # synchronous signature. Production pywebview 6 resolves Promises
            # through the callback path above.
            return window.evaluate_js(script)
        if immediate not in (None, True, "true") and not result.event.is_set():
            return immediate
        if not result.event.wait(timeout):
            raise core.ManagerError("中转站响应超时；登录窗口仍然保留，可以稍后重试识别。")
        return result.value

    def _auto_auth_waiting(
        self,
        session_id: str,
        cancel_event: threading.Event,
        message: str,
        *,
        error: str | None = None,
    ) -> dict:
        with self.lock:
            if (
                cancel_event.is_set()
                or str(self._session.get("sessionId") or "") != session_id
            ):
                return self.public_state()
            auto_auth = self._session.get("autoAuth")
            if not isinstance(auto_auth, dict):
                auto_auth = {}
                self._session["autoAuth"] = auto_auth
            attempts = int(auto_auth.get("attempts") or 0)
            exhausted = attempts >= AUTO_AUTH_MAX_ATTEMPTS
            mode = str(self._session.get("mode") or "new")
            auto_auth.update(
                {
                    "enabled": True,
                    "state": "exhausted" if exhausted else "waiting",
                    "attempts": attempts,
                    "maxAttempts": AUTO_AUTH_MAX_ATTEMPTS,
                    "nextCheckInMs": 0 if exhausted else int(AUTO_AUTH_CHECK_INTERVAL_SECONDS * 1000),
                }
            )
            self._session.update(
                {
                    "status": "reauth_timeout" if exhausted and mode == "reauth" else "waiting_login",
                    "message": (
                        "自动登录检测已停止；登录窗口仍保留，可手动重试识别。"
                        if exhausted
                        else message
                    ),
                    "error": error,
                    "updatedAt": core.now_iso(),
                    "_autoAuthInFlight": False,
                }
            )
        return self.public_state()

    def _auto_auth_error(
        self,
        session_id: str,
        cancel_event: threading.Event,
        exc: Exception,
        *,
        mode: str,
    ) -> dict:
        message = core._redact_sensitive_text(exc, limit=500)
        with self.lock:
            if (
                cancel_event.is_set()
                or str(self._session.get("sessionId") or "") != session_id
            ):
                return self.public_state()
            auto_auth = self._session.get("autoAuth")
            if not isinstance(auto_auth, dict):
                auto_auth = {}
                self._session["autoAuth"] = auto_auth
            if isinstance(exc, DashboardIdentityChanged):
                auto_state = "identity_mismatch"
                status = "identity_mismatch"
                status_message = "检测到登录的是另一个账号；未覆盖现有账号，请切回正确账号后重新打开续登。"
            else:
                auto_state = "error"
                status = "reauth_error" if mode == "reauth" else "waiting_login"
                status_message = (
                    "账号身份已确认，但自动刷新未完成；凭据未错误覆盖，可手动重试。"
                    if mode == "reauth"
                    else "已检测到登录，但自动识别未完成；可手动重试识别。"
                )
            auto_auth.update(
                {
                    "enabled": True,
                    "state": auto_state,
                    "maxAttempts": AUTO_AUTH_MAX_ATTEMPTS,
                    "nextCheckInMs": 0,
                }
            )
            self._session.update(
                {
                    "status": status,
                    "message": status_message,
                    "error": message,
                    "updatedAt": core.now_iso(),
                    "_autoAuthInFlight": False,
                }
            )
        return self.public_state()

    def check_auto_auth(self, session_id: object, *, force: bool = False) -> dict:
        """Advance one throttled automatic-login check.

        The UI may poll this method freely.  Server-side throttling ensures at
        most one small identity request wave every interval.  A full dashboard
        scan runs once after authentication. New sessions stop at ``ready`` so
        the user still chooses which Keys to import; reauthentication sessions
        update only the already-saved account and never call the import path.
        """

        supplied = _text(session_id, 120)
        with self.lock:
            self._require_current_session_locked(supplied)
            auto_auth = self._session.get("autoAuth")
            if not isinstance(auto_auth, dict) or not auto_auth.get("enabled"):
                return self.public_state()
            mode = str(self._session.get("mode") or "new")
            auto_state = str(auto_auth.get("state") or "waiting")
            if auto_state in {"completed", "cancelled", "expired"}:
                return self.public_state()
            if auto_state in {"identity_mismatch", "error", "exhausted"}:
                if not force:
                    return self.public_state()
                self._session["_fullScanStarted"] = False
                auto_auth["state"] = "waiting"
                self._session.update(
                    {
                        "status": "waiting_login",
                        "message": "正在按原账号身份重新检测网页登录状态…",
                        "error": None,
                        "updatedAt": core.now_iso(),
                    }
                )
            if self._session.get("_autoAuthInFlight"):
                return self.public_state()
            now = time.monotonic()
            last_check = float(self._session.get("_lastAutoCheckMonotonic") or 0)
            if not force and last_check and now - last_check < AUTO_AUTH_CHECK_INTERVAL_SECONDS:
                return self.public_state()
            attempts = int(auto_auth.get("attempts") or 0)
            if attempts >= AUTO_AUTH_MAX_ATTEMPTS and not force:
                cancel_event = self._session.get("_cancelEvent")
                if not isinstance(cancel_event, threading.Event):
                    cancel_event = threading.Event()
                return self._auto_auth_waiting(
                    supplied,
                    cancel_event,
                    "自动登录检测次数已用完。",
                )
            window = self.window
            if window is None:
                self._session.update(
                    {
                        "status": "closed",
                        "message": "登录窗口已关闭；重新打开后可继续。",
                        "updatedAt": core.now_iso(),
                    }
                )
                auto_auth.update({"state": "error", "nextCheckInMs": 0})
                return self.public_state()
            portal_url = str(self._session.get("portalUrl") or "")
            expected_origin = self._origin(portal_url)
            expected_adapter = str(self._session.get("_expectedAdapter") or "")
            expected_identity = dict(self._session.get("_expectedIdentity") or {})
            cancel_event = self._session.get("_cancelEvent")
            if not isinstance(cancel_event, threading.Event):
                cancel_event = threading.Event()
                self._session["_cancelEvent"] = cancel_event
            auto_auth.update(
                {
                    "state": "checking",
                    "attempts": min(attempts + 1, AUTO_AUTH_MAX_ATTEMPTS),
                    "maxAttempts": AUTO_AUTH_MAX_ATTEMPTS,
                    "nextCheckInMs": int(AUTO_AUTH_CHECK_INTERVAL_SECONDS * 1000),
                }
            )
            self._session.update(
                {
                    "status": "checking_login",
                    "message": "正在自动确认网页登录状态…",
                    "error": None,
                    "updatedAt": core.now_iso(),
                    "_lastAutoCheckMonotonic": now,
                    "_autoAuthInFlight": True,
                }
            )

        phase = "auth"
        try:
            try:
                current_url = str(window.get_current_url() or "")
            except Exception as exc:
                raise core.ManagerError("无法读取中转站登录窗口状态，请关闭后重试。") from exc
            if self._origin(current_url) != expected_origin:
                return self._auto_auth_waiting(
                    supplied,
                    cancel_event,
                    "等待第三方授权完成并返回中转站…",
                )
            with self.lock:
                if cancel_event.is_set():
                    return self.public_state()
                self._require_current_session_locked(supplied)
                self._session["currentUrl"] = current_url

            auth_raw = self._evaluate(
                window,
                _auth_probe_script(expected_origin, expected_adapter),
                timeout=AUTO_AUTH_CHECK_TIMEOUT_SECONDS,
            )
            if not isinstance(auth_raw, dict) or auth_raw.get("authenticated") is not True:
                error = _text(auth_raw.get("error"), 300) if isinstance(auth_raw, dict) else ""
                message = (
                    _text(auth_raw.get("message"), 240)
                    if isinstance(auth_raw, dict)
                    else ""
                ) or "尚未检测到已登录账号；会继续自动检查。"
                return self._auto_auth_waiting(
                    supplied,
                    cancel_event,
                    message,
                    error=error or None,
                )

            detected_adapter = _text(auth_raw.get("adapter"), 40)
            actual_identity = _dashboard_user_identity(auth_raw.get("user"))
            if mode == "reauth":
                _assert_dashboard_identity(
                    expected_identity,
                    actual_identity,
                    expected_adapter=expected_adapter,
                    actual_adapter=detected_adapter,
                )
            elif detected_adapter not in {"new-api", "sub2api"}:
                raise core.ManagerError("登录已完成，但无法确认中转站类型。")

            with self.lock:
                self._require_current_session_locked(supplied)
                if cancel_event.is_set():
                    return self.public_state()
                if self._session.get("_fullScanStarted"):
                    return self.public_state()
                self._session["_fullScanStarted"] = True
                auto_auth = self._session["autoAuth"]
                auto_auth.update(
                    {
                        "state": "syncing" if mode == "reauth" else "scanning",
                        "nextCheckInMs": 0,
                    }
                )
                self._session.update(
                    {
                        "status": "reauth_syncing" if mode == "reauth" else "scanning",
                        "message": (
                            "账号身份已确认，正在更新现有账号…"
                            if mode == "reauth"
                            else "登录已确认，正在识别账户、余额、模型与 API Key…"
                        ),
                        "updatedAt": core.now_iso(),
                    }
                )

            phase = "scan"
            full_raw = self._evaluate(window, _probe_script(expected_origin))
            preview, secrets_by_id = normalize_probe_result(full_raw, portal_url=portal_url)
            dashboard_session = _dashboard_session_from_probe(
                full_raw,
                portal_url=portal_url,
                window=window,
            )
            if preview.get("adapter") != detected_adapter:
                raise core.ManagerError("登录身份与完整识别结果不一致，已停止更新。")

            if mode == "new":
                with self.lock:
                    self._require_current_session_locked(supplied)
                    if cancel_event.is_set():
                        return self.public_state()
                    self._preview = preview
                    self._secrets = secrets_by_id
                    self._dashboard_session = dashboard_session
                    self._session["autoAuth"].update(
                        {"state": "completed", "nextCheckInMs": 0}
                    )
                    self._session.update(
                        {
                            "status": "ready",
                            "adapter": preview["adapter"],
                            "preview": preview,
                            "message": f"已自动识别 {preview['adapterLabel']}；找到 {len(preview['keys'])} 个 API Key，请选择后导入。",
                            "error": None,
                            "updatedAt": core.now_iso(),
                            "_autoAuthInFlight": False,
                        }
                    )
                return self.public_state()

            full_identity = {
                "id": _text((preview.get("user") or {}).get("id"), 120),
                "email": _text((preview.get("user") or {}).get("email"), 180).casefold(),
                "name": _text((preview.get("user") or {}).get("name"), 160),
            }
            _assert_dashboard_identity(
                expected_identity,
                full_identity,
                expected_adapter=expected_adapter,
                actual_adapter=str(preview.get("adapter") or ""),
            )
            if not dashboard_session:
                raise core.ManagerError("已确认登录账号，但没有取得可续期的网页登录凭据。")
            dashboard_session["userId"] = full_identity["id"] or expected_identity.get("id") or ""

            # Cancellation or replacement after either network probe discards
            # all late results before any credential or account mutation.
            with self.lock:
                self._require_current_session_locked(supplied)
                if cancel_event.is_set():
                    return self.public_state()
                account_id = str(self._session.get("accountId") or "")
            phase = "commit"
            core.store_relay_account_dashboard_session(account_id, dashboard_session)
            live = core.sync_relay_account_snapshot(
                account_id,
                _codex_only_preview(preview),
            )
            warnings = list(live.get("warnings", [])) if isinstance(live, dict) else []
            final_account = live.get("account") if isinstance(live, dict) else None
            model_refresh = None
            try:
                metadata = core.refresh_relay_account(
                    account_id,
                    refresh_balance=False,
                    fallback_notice=False,
                )
                if isinstance(metadata, dict):
                    final_account = metadata.get("account") or final_account
                    model_refresh = metadata.get("modelRefresh")
                    warnings.extend(metadata.get("warnings", []))
            except core.ManagerError as exc:
                warnings.append(
                    f"网页数据已同步，但模型目录刷新失败：{core._redact_sensitive_text(exc, limit=220)}"
                )
            if preview.get("quotaPerUnit") is not None:
                try:
                    final_account = self._update_quick_account_metadata(
                        account_id,
                        {},
                        None,
                        preview.get("quotaPerUnit"),
                    )
                except core.ManagerError as exc:
                    warnings.append(
                        "额度换算元数据未能缓存："
                        + core._redact_sensitive_text(exc, limit=220)
                    )
            completion = {
                "accountId": account_id,
                "liveDashboard": True,
                "requiresReapply": bool(
                    isinstance(live, dict) and live.get("requiresReapply")
                ),
                "warnings": list(dict.fromkeys(str(item) for item in warnings if str(item))),
                "modelRefresh": model_refresh,
                "counts": {
                    "keys": len((final_account or {}).get("keys", [])),
                    "groups": len((final_account or {}).get("groups", [])),
                    "models": len((final_account or {}).get("models", [])),
                },
            }
            with self.lock:
                self._require_current_session_locked(supplied)
                self._preview = None
                self._secrets = {}
                self._dashboard_session = None
                self._session["autoAuth"].update(
                    {"state": "completed", "nextCheckInMs": 0}
                )
                self._session.update(
                    {
                        "status": "reauthenticated",
                        "adapter": preview["adapter"],
                        "preview": None,
                        "completion": completion,
                        "message": "网页登录已续期，现有账号已自动刷新。",
                        "error": None,
                        "updatedAt": core.now_iso(),
                        "_autoAuthInFlight": False,
                    }
                )
            self._release_window(session_id=supplied)
            return self.public_state()
        except DashboardIdentityChanged as exc:
            return self._auto_auth_error(
                supplied,
                cancel_event,
                exc,
                mode=mode,
            )
        except core.ManagerError as exc:
            with self.lock:
                is_current = (
                    not cancel_event.is_set()
                    and str(self._session.get("sessionId") or "") == supplied
                )
            if not is_current:
                return self.public_state()
            if phase == "auth":
                return self._auto_auth_waiting(
                    supplied,
                    cancel_event,
                    "自动登录检测暂未完成；会按低频间隔继续检查。",
                    error=core._redact_sensitive_text(exc, limit=400),
                )
            return self._auto_auth_error(
                supplied,
                cancel_event,
                exc,
                mode=mode,
            )
        except Exception as exc:
            return self._auto_auth_error(
                supplied,
                cancel_event,
                core.ManagerError(f"中转站自动登录处理失败：{core._redact_sensitive_text(exc, limit=400)}"),
                mode=mode,
            )

    def scan(self, session_id: object) -> dict:
        with self.lock:
            reauth_mode = (
                str(self._session.get("mode") or "new") == "reauth"
                and str(self._session.get("sessionId") or "") == _text(session_id, 120)
            )
        if reauth_mode:
            return self.check_auto_auth(session_id, force=True)
        window, portal_url, expected_origin = self._validate_session(session_id)
        with self.lock:
            self._require_current_session_locked(session_id)
            self._session.update(
                {"status": "scanning", "message": "正在识别账户、余额、模型与 API Key…", "error": None}
            )
        try:
            raw = self._evaluate(window, _probe_script(expected_origin))
            preview, secrets_by_id = normalize_probe_result(raw, portal_url=portal_url)
            dashboard_session = _dashboard_session_from_probe(
                raw,
                portal_url=portal_url,
                window=window,
            )
        except Exception as exc:
            message = core._redact_sensitive_text(exc, limit=600)
            with self.lock:
                self._require_current_session_locked(session_id)
                self._session.update(
                    {
                        "status": "waiting_login",
                        "message": "识别尚未完成；确认已经登录并进入控制台后可再次尝试。",
                        "error": message,
                        "updatedAt": core.now_iso(),
                    }
                )
            if isinstance(exc, core.ManagerError):
                raise
            raise core.ManagerError(f"识别中转站失败：{message}") from exc
        with self.lock:
            self._require_current_session_locked(session_id)
            self._preview = preview
            self._secrets = secrets_by_id
            self._dashboard_session = dashboard_session
            self._session.update(
                {
                    "status": "ready",
                    "adapter": preview["adapter"],
                    "preview": preview,
                    "message": f"已识别 {preview['adapterLabel']}；找到 {len(preview['keys'])} 个 API Key。",
                    "error": None,
                    "updatedAt": core.now_iso(),
                }
            )
            auto_auth = self._session.get("autoAuth")
            if isinstance(auto_auth, dict):
                auto_auth.update({"state": "completed", "nextCheckInMs": 0})
            self._session["_autoAuthInFlight"] = False
            self._session["_fullScanStarted"] = True
        return self.public_state()

    def _preview_and_secret(self, session_id: object, key_id: object) -> tuple[dict, dict, str]:
        window, _portal_url, expected_origin = self._validate_session(session_id)
        normalized_id = _text(key_id, 80)
        with self.lock:
            self._require_current_session_locked(session_id)
            preview = json.loads(json.dumps(self._preview)) if self._preview else None
            secret = self._secrets.get(normalized_id, "")
        if not preview:
            raise core.ManagerError("请先识别中转站账户内容。")
        record = next((item for item in preview.get("keys", []) if item.get("id") == normalized_id), None)
        if not record:
            raise core.ManagerError("所选 API Key 已不在当前识别结果中，请重新识别。")
        if not secret:
            raw = self._evaluate(window, _reveal_script(expected_origin, preview["adapter"], normalized_id))
            if isinstance(raw, dict) and raw.get("error"):
                raise core.ManagerError(_text(raw.get("error"), 400))
            data = _response_data(raw)
            if isinstance(data, dict):
                secret = _full_api_key(
                    data.get("key") or data.get("raw_key") or data.get("token"),
                    assume_one_api=preview["adapter"] == "new-api",
                )
            if not secret:
                raise core.ManagerError("该站点没有返回完整 Key；请在中转站页面复制后改用“自定义登录”。")
            with self.lock:
                self._require_current_session_locked(session_id)
                self._secrets[normalized_id] = secret
        return preview, record, secret

    @staticmethod
    def _selected_endpoint(preview: dict, endpoint_id: object = None) -> dict:
        endpoints = preview.get("apiEndpoints") if isinstance(preview.get("apiEndpoints"), list) else []
        requested = _text(endpoint_id, 80)
        if endpoints:
            if requested:
                selected = next((item for item in endpoints if item.get("id") == requested), None)
                if not selected:
                    raise core.ManagerError("所选 API 端点已不在当前识别结果中，请重新识别。")
                return selected
            default_id = _text(preview.get("defaultEndpointId"), 80)
            return next(
                (item for item in endpoints if item.get("id") == default_id),
                endpoints[0],
            )
        # Backward-compatible shape for sessions created before endpoint cards
        # were introduced and for focused unit-test fixtures.
        return {
            "id": "default",
            "name": "默认 API",
            "baseUrl": preview["baseUrl"],
            "modelsEndpoint": preview["modelsEndpoint"],
            "balanceEndpoint": preview["balanceEndpoint"],
            "isDefault": True,
        }

    @staticmethod
    def _provider_id(preview: dict, record: dict, endpoint: dict) -> str:
        host = urllib.parse.urlsplit(endpoint["baseUrl"]).hostname or preview["siteName"]
        seed = (
            f"{host}_{endpoint.get('id') or 'default'}_"
            f"{record.get('name') or record.get('id')}_{record.get('id') or ''}"
        )
        normalized = re.sub(r"[^a-z0-9]+", "_", seed.casefold()).strip("_")
        if not normalized:
            normalized = f"relay_{secrets.token_hex(4)}"
        return normalized[:60].rstrip("_")

    def _import_record(
        self,
        preview: dict,
        record: dict,
        secret: str,
        *,
        group_id: object,
        proxy_enabled: bool,
        endpoint_id: object = None,
    ) -> dict:
        endpoint = self._selected_endpoint(preview, endpoint_id)
        provider_id = self._provider_id(preview, record, endpoint)
        models = list(dict.fromkeys([*record.get("models", []), *preview.get("models", [])]))[:MAX_MODELS]
        name_parts = [preview["siteName"], record.get("name") or "API Key"]
        if record.get("group"):
            name_parts.append(str(record["group"]))
        if not endpoint.get("isDefault"):
            name_parts.append(str(endpoint.get("name") or "自定义端点"))
        payload = {
            "id": provider_id,
            "originalId": provider_id,
            "name": " · ".join(name_parts)[:160],
            "baseUrl": endpoint["baseUrl"],
            "portalUrl": preview["portalUrl"],
            "integrationKind": preview["integrationKind"],
            "key": secret,
            "model": models[0] if models else "",
            "models": models,
            "envKey": f"{provider_id.upper()}_API_KEY",
            "modelsEndpoint": endpoint["modelsEndpoint"],
            "balanceEndpoint": endpoint["balanceEndpoint"],
            "balanceSnapshot": {
                "remaining": (preview.get("balance") or {}).get("remaining"),
                "used": (preview.get("balance") or {}).get("used"),
                "currency": (preview.get("balance") or {}).get("currency"),
            },
            "balanceSnapshotAt": preview.get("detectedAt"),
            "groupId": _text(group_id, 80) or "relay",
            "fetchModels": True,
            "activate": False,
            "proxyEnabled": bool(proxy_enabled),
        }
        return core.import_api_account(payload)

    def import_keys(self, session_id: object, payload: dict) -> dict:
        self._validate_session(session_id)
        with self.lock:
            self._require_current_session_locked(session_id)
            preview = json.loads(json.dumps(self._preview)) if self._preview else None
        if not preview:
            raise core.ManagerError("请先识别中转站账户内容。")
        requested = payload.get("keyIds") if isinstance(payload.get("keyIds"), list) else []
        preferred_ids = list(dict.fromkeys(_text(item, 80) for item in requested if _text(item, 80)))
        if not preferred_ids:
            raise core.ManagerError("请至少勾选一个要导入的 Codex API Key。")
        requested_ids = set(preferred_ids)
        records = [
            item
            for item in preview.get("keys", [])
            if isinstance(item, dict)
            and item.get("canImport")
            and _text(item.get("id"), 80) in requested_ids
            and _codex_key_record(item)
        ]
        record_by_id = {_text(item.get("id"), 80): item for item in records}
        ordered_records = [record_by_id[key_id] for key_id in preferred_ids if key_id in record_by_id]
        rejected_ids = [key_id for key_id in preferred_ids if key_id not in record_by_id]
        secrets_by_id: dict[str, str] = {}
        failed = [
            {"keyId": key_id, "error": "该 Key 不可导入或不属于 Codex / OpenAI 兼容分组。"}
            for key_id in rejected_ids
        ]
        for record in ordered_records:
            key_id = _text(record.get("id"), 80)
            try:
                _latest_preview, _latest_record, secret = self._preview_and_secret(session_id, key_id)
                secrets_by_id[key_id] = secret
            except Exception as exc:
                failed.append({"keyId": key_id, "error": core._redact_sensitive_text(exc, limit=400)})
        if not secrets_by_id:
            raise core.ManagerError("所选 Codex API Key 均无法读取，未写入任何账号数据。")
        selected_key_id = next(
            (key_id for key_id in preferred_ids if key_id in secrets_by_id),
            next(iter(secrets_by_id), ""),
        )
        selected_preview = _codex_only_preview(preview, key_ids=set(secrets_by_id))
        with self.lock:
            self._require_current_session_locked(session_id)
            result = core.import_relay_account(
                selected_preview,
                secrets_by_id,
                group_id=payload.get("groupId"),
                proxy_enabled=bool(payload.get("proxyEnabled")),
                endpoint_id=payload.get("endpointId"),
                selected_key_id=selected_key_id,
                relay_account_id=payload.get("relayAccountId"),
            )
            warnings: list[str] = []
            account_id = str((result.get("account") or {}).get("id") or "")
            if account_id and preview.get("quotaPerUnit") is not None:
                try:
                    result["account"] = self._update_quick_account_metadata(
                        account_id,
                        {},
                        None,
                        preview.get("quotaPerUnit"),
                    )
                except core.ManagerError as exc:
                    warnings.append(
                        "账号与 Key 已导入，但额度换算元数据未能缓存："
                        + core._redact_sensitive_text(exc, limit=220)
                    )
            with self.lock:
                dashboard_session = (
                    json.loads(json.dumps(self._dashboard_session))
                    if isinstance(self._dashboard_session, dict)
                    else None
                )
            if account_id and dashboard_session:
                try:
                    core.store_relay_account_dashboard_session(account_id, dashboard_session)
                except core.ManagerError as exc:
                    warnings.append(
                        "账号与 Key 已导入，但网页登录续期凭据未能保存："
                        + core._redact_sensitive_text(exc, limit=240)
                    )
            elif account_id:
                warnings.append("账号与 Key 已导入，但站点没有返回可续期的网页登录凭据。")
            with self.lock:
                self._session.update(
                    {
                        "status": "imported",
                        "message": (
                            f"已导入选中的 {len(secrets_by_id)} 个 Codex API Key；"
                            f"账号共保存 {result.get('configuredKeyCount', 0)} 个 Key。"
                        ),
                        "updatedAt": core.now_iso(),
                    }
                )
        self._release_window(session_id=session_id)
        return {
            **result,
            "imported": [result] if result.get("provider") else [],
            "failed": failed,
            "count": len(secrets_by_id),
            "warnings": warnings,
        }

    def create_key(self, session_id: object, payload: dict) -> dict:
        window, _portal_url, expected_origin = self._validate_session(session_id)
        with self.lock:
            self._require_current_session_locked(session_id)
            preview = json.loads(json.dumps(self._preview)) if self._preview else None
        if not preview:
            raise core.ManagerError("请先识别中转站账户内容。")
        name = _text(payload.get("name"), 80) or "Agent Manager"
        relay_group_id: object = None
        if preview.get("adapter") == "sub2api":
            groups = preview.get("groups") if isinstance(preview.get("groups"), list) else []
            requested_group_id = _text(payload.get("relayGroupId"), 80)
            selected_group = next(
                (
                    item
                    for item in groups
                    if str(item.get("id") or "") == requested_group_id
                    and _codex_group_record(item)
                ),
                None,
            )
            if selected_group is None and not requested_group_id:
                selected_group = next(
                    (item for item in groups if item.get("active") and _codex_group_record(item)),
                    None,
                )
            if selected_group is None:
                raise core.ManagerError("请选择中转站的 Key 分组后再创建；该项与本机账号分组不同。")
            raw_group_id = selected_group.get("id")
            relay_group_id = int(raw_group_id) if str(raw_group_id).isdigit() else raw_group_id
        previous_ids = {
            str(item.get("id") or "")
            for item in preview.get("keys", [])
            if str(item.get("id") or "")
        }
        raw = self._evaluate(
            window,
            _create_script(expected_origin, preview["adapter"], name, relay_group_id),
        )
        if isinstance(raw, dict) and raw.get("error"):
            raise core.ManagerError(_text(raw.get("error"), 500))
        if not _response_ok(raw):
            body = raw.get("body") if isinstance(raw, dict) else None
            message = _text(body.get("message"), 400) if isinstance(body, dict) else ""
            raise core.ManagerError(message or "中转站没有成功创建 API Key。")
        data = _response_data(raw)
        record: dict | None = None
        secret = ""
        if isinstance(data, dict) and any(
            key in data for key in ("id", "key", "raw_key", "token", "name")
        ):
            record, secret = _normalize_key_record(data, preview["adapter"])
        # Creating and importing are deliberately separate actions. Re-scan so
        # the new row appears above the unchanged import controls; the user can
        # then decide whether it should be selected and written locally.
        try:
            refreshed = self.scan(session_id)
            preview = refreshed.get("preview") or {}
        except Exception:
            refreshed = None
        candidates = [
            item
            for item in preview.get("keys", [])
            if str(item.get("id") or "") not in previous_ids
            and _text(item.get("name"), 80) == name
        ]
        if not candidates:
            candidates = [
                item
                for item in preview.get("keys", [])
                if str(item.get("id") or "") not in previous_ids
            ]
        if candidates:
            record = candidates[0]
        if not record:
            raise core.ManagerError("Key 已创建，但无法在列表中确认新记录；请重新识别后选择导入。")
        if secret:
            record["canImport"] = True
            record["requiresReveal"] = False
            with self.lock:
                self._require_current_session_locked(session_id)
                self._secrets[str(record.get("id") or "")] = secret
        with self.lock:
            self._require_current_session_locked(session_id)
            current_preview = json.loads(json.dumps(self._preview)) if self._preview else preview
            current_keys = current_preview.setdefault("keys", [])
            if not any(str(item.get("id") or "") == str(record.get("id") or "") for item in current_keys):
                current_keys.append(record)
            self._preview = current_preview
            self._session["preview"] = json.loads(json.dumps(current_preview))
            self._session["message"] = "新 API Key 已创建；请勾选需要的 Key 后再导入。"
            self._session["updatedAt"] = core.now_iso()
        return {
            "created": record,
            "status": self.public_state(),
        }

    @staticmethod
    def _saved_account(account_id: object) -> tuple[dict, dict]:
        normalized_id = core.slugify(str(account_id or ""), "中转站账号 ID")
        settings = core.load_settings()
        account = next(
            (
                item
                for item in settings.get("relayAccounts", [])
                if str(item.get("id") or "") == normalized_id
            ),
            None,
        )
        if not account:
            raise core.ManagerError("中转站账号不存在。")
        return account, settings

    @staticmethod
    def _update_quick_account_metadata(
        account_id: str,
        user: object,
        balance: object,
        quota_per_unit: object = None,
    ) -> dict:
        """Patch live user/balance fields without rebuilding cached catalogs."""

        supplied_id = str(account_id or "").strip()
        normalized_id = core.slugify(supplied_id, "中转站账号 ID")
        candidate_ids = {supplied_id, normalized_id}
        incoming_user = user if isinstance(user, dict) else {}
        incoming_balance = balance if isinstance(balance, dict) else None
        with core.SETTINGS_LOCK, core._settings_file_lock():
            settings = core.load_settings()
            account = next(
                (
                    item
                    for item in settings.get("relayAccounts", [])
                    if str(item.get("id") or "") in candidate_ids
                ),
                None,
            )
            if not account:
                raise core.ManagerError("中转站账号不存在。")
            current_user = account.get("user") if isinstance(account.get("user"), dict) else {}
            expected_user_id = _text(current_user.get("id"), 80)
            incoming_user_id = _text(incoming_user.get("id"), 80)
            if expected_user_id and incoming_user_id and expected_user_id != incoming_user_id:
                raise DashboardIdentityChanged("中转站快速刷新返回了不同账号，未更新本地数据。")
            merged_user = dict(current_user)
            for key, limit in (("id", 80), ("name", 160), ("email", 180), ("group", 120)):
                value = _text(incoming_user.get(key), limit)
                if value:
                    merged_user[key] = value
            account["user"] = merged_user

            normalized_quota_per_unit = _number(quota_per_unit)
            if normalized_quota_per_unit is not None and normalized_quota_per_unit > 0:
                dashboard_metadata = (
                    dict(account.get("dashboardMetadata"))
                    if isinstance(account.get("dashboardMetadata"), dict)
                    else {}
                )
                dashboard_metadata.update(
                    {
                        "quotaPerUnit": normalized_quota_per_unit,
                        "quotaPerUnitUpdatedAt": core.now_iso(),
                    }
                )
                account["dashboardMetadata"] = dashboard_metadata

            # A usage-only response cannot prove the current remaining balance.
            # Replace the snapshot as a whole so a missing field is not silently
            # combined with a value from an earlier request.
            if incoming_balance is not None and _number(incoming_balance.get("remaining")) is not None:
                current_balance = {"remaining": None, "used": None, "currency": "USD"}
                for key in ("remaining", "used"):
                    value = _number(incoming_balance.get(key))
                    if value is not None:
                        current_balance[key] = round(value, 6)
                currency = _text(incoming_balance.get("currency"), 12).upper()
                if currency:
                    current_balance["currency"] = currency
                account["balance"] = current_balance
                account.update({
                    "balanceFreshness": "fresh",
                    "balanceUpdatedAt": core.now_iso(),
                    "balanceCheckedAt": core.now_iso(),
                    "balanceError": None,
                    "balanceSource": "relay-dashboard",
                })

                provider_id = str(account.get("providerId") or "")
                provider = next(
                    (
                        item
                        for item in settings.get("providers", [])
                        if str(item.get("id") or "") == provider_id
                    ),
                    None,
                )
                if provider:
                    provider_balance = (
                        dict(provider.get("balance"))
                        if isinstance(provider.get("balance"), dict)
                        else {}
                    )
                    if current_balance.get("remaining") is not None:
                        provider_balance["amount"] = current_balance["remaining"]
                    provider_balance["used"] = current_balance.get("used")
                    provider_balance["currency"] = current_balance.get("currency") or "USD"
                    provider_balance["source"] = "relay-dashboard"
                    provider["balance"] = provider_balance
                    provider["balanceUpdatedAt"] = core.now_iso()
                    provider["balanceError"] = None
            account["updatedAt"] = core.now_iso()
            core.save_settings(settings)
            return json.loads(json.dumps(account))

    @staticmethod
    def _record_stale_balance(account_id: str, previous: dict, error: str) -> dict:
        """Keep the last account snapshot, never a selected Key's quota.

        updatedAt is deliberately not used as a balance timestamp: model and
        catalog refreshes also change it. Legacy snapshots have an unknown
        balance timestamp until an authenticated balance read succeeds.
        """
        with core.SETTINGS_LOCK, core._settings_file_lock():
            settings = core.load_settings()
            account = next((item for item in settings.get("relayAccounts", [])
                            if str(item.get("id") or "") == account_id), None)
            if account is None:
                raise core.ManagerError("中转站账号不存在。")
            balance = previous.get("balance")
            account["balance"] = json.loads(json.dumps(balance)) if isinstance(balance, dict) else None
            has_balance = isinstance(balance, dict) and _number(balance.get("remaining")) is not None
            account.update({
                "balanceFreshness": "stale" if has_balance else "unavailable",
                "balanceUpdatedAt": previous.get("balanceUpdatedAt"),
                "balanceCheckedAt": core.now_iso(),
                "balanceError": core._redact_sensitive_text(error, limit=240),
                "balanceSource": previous.get("balanceSource") or "relay-dashboard",
            })
            core.save_settings(settings)
            return json.loads(json.dumps(account))

    @staticmethod
    def _scan_saved_account(
        account: dict,
        *,
        full: bool = True,
    ) -> tuple[dict, dict[str, str], dict]:
        account_id = str(account.get("id") or "")
        with core._account_refresh_lock_for("relay-dashboard:" + account_id):
            if full:
                return RelayPortalService._scan_saved_account_locked(account)
            return RelayPortalService._scan_saved_account_quick_locked(account)

    @staticmethod
    def _scan_saved_account_quick_locked(
        account: dict,
    ) -> tuple[dict, dict[str, str], dict]:
        account_id = str(account.get("id") or "")
        try:
            session = core.load_relay_account_dashboard_session(account_id, required=True)
        except core.ManagerError as exc:
            raise DashboardLoginRequired(str(exc)) from exc
        if not isinstance(session, dict):
            raise DashboardLoginRequired("该中转站账号尚未保存网页登录凭据，请重新登录一次。")
        try:
            account_origin = core._validated_provider_portal_url(
                account.get("origin") or account.get("portalUrl")
            )
            if core._provider_url_origin(session.get("origin")) != core._provider_url_origin(account_origin):
                raise DashboardLoginRequired("已保存的网页登录凭据与当前中转站地址不匹配，请重新登录。")
            portal_url = core._validated_provider_portal_url(
                account.get("portalUrl") or session.get("portalUrl") or account_origin
            )
        except DashboardLoginRequired:
            raise
        except core.ManagerError as exc:
            raise DashboardLoginRequired(str(exc)) from exc
        session["portalUrl"] = portal_url
        original = json.loads(json.dumps(session))
        identity_changed = False
        try:
            dashboard_metadata = (
                account.get("dashboardMetadata")
                if isinstance(account.get("dashboardMetadata"), dict)
                else {}
            )
            raw, session = _probe_saved_dashboard_quick(
                session,
                quota_per_unit=dashboard_metadata.get("quotaPerUnit"),
            )
            account_user = account.get("user") if isinstance(account.get("user"), dict) else {}
            expected_identity = {
                "id": _text(account_user.get("id") or original.get("userId"), 120),
                "email": _text(account_user.get("email"), 180).casefold(),
            }
            try:
                _assert_dashboard_identity(
                    expected_identity,
                    _dashboard_user_identity(raw.get("user")),
                    expected_adapter=_text(account.get("adapter"), 40),
                    actual_adapter=_text(raw.get("adapter"), 40),
                )
            except core.ManagerError:
                identity_changed = True
                raise
            return raw, {}, session
        except DashboardIdentityChanged:
            identity_changed = True
            raise
        finally:
            original_credentials = {
                key: value for key, value in original.items() if key != "updatedAt"
            }
            current_credentials = {
                key: value for key, value in session.items() if key != "updatedAt"
            }
            if not identity_changed and current_credentials != original_credentials:
                session["updatedAt"] = core.now_iso()
                core.store_relay_account_dashboard_session(account_id, session)

    @staticmethod
    def _scan_saved_account_locked(account: dict) -> tuple[dict, dict[str, str], dict]:
        account_id = str(account.get("id") or "")
        session = core.load_relay_account_dashboard_session(account_id, required=True)
        if not isinstance(session, dict):
            raise core.ManagerError("该中转站账号尚未保存网页登录凭据，请重新登录一次。")
        account_origin = core._validated_provider_portal_url(
            account.get("origin") or account.get("portalUrl")
        )
        if core._provider_url_origin(session.get("origin")) != core._provider_url_origin(account_origin):
            raise core.ManagerError("已保存的网页登录凭据与当前中转站地址不匹配，请重新登录。")
        portal_url = core._validated_provider_portal_url(
            account.get("portalUrl") or session.get("portalUrl") or account_origin
        )
        session["portalUrl"] = portal_url
        original = json.loads(json.dumps(session))
        success = False
        identity_changed = False
        try:
            raw, session = _probe_saved_dashboard(session)
            preview, secrets_by_id = normalize_probe_result(raw, portal_url=portal_url)
            if preview.get("quotaPerUnit") is not None:
                try:
                    RelayPortalService._update_quick_account_metadata(
                        account_id,
                        {},
                        None,
                        preview.get("quotaPerUnit"),
                    )
                except core.ManagerError:
                    # The authoritative sync/import immediately following this
                    # scan gets another chance to persist the non-secret cache.
                    pass
            success = True
            return preview, secrets_by_id, session
        except DashboardIdentityChanged:
            identity_changed = True
            raise
        finally:
            if not identity_changed and (success or session != original):
                core.store_relay_account_dashboard_session(account_id, session)

    @staticmethod
    def _reveal_saved_key(session: dict, adapter: str, key_id: str) -> str:
        headers = _saved_auth_headers(session)
        if adapter == "new-api":
            raw = _saved_json_request(
                session,
                f"/api/token/{urllib.parse.quote(key_id, safe='')}/key",
                method="POST",
                headers=headers,
            )
            if not _response_ok(raw):
                raw = _saved_json_request(
                    session,
                    f"/api/token/{urllib.parse.quote(key_id, safe='')}",
                    headers=headers,
                )
        else:
            raw = _saved_json_request(
                session,
                f"/api/v1/keys/{urllib.parse.quote(key_id, safe='')}",
                headers=headers,
            )
        data = _response_data(raw)
        if not isinstance(data, dict):
            return ""
        return _full_api_key(
            data.get("key") or data.get("raw_key") or data.get("token"),
            assume_one_api=adapter == "new-api",
        )

    @_serialized_dashboard
    def create_saved_key(self, account_id: object, payload: dict) -> dict:
        """Create and import one Codex Key without keeping a WebView alive."""

        if not isinstance(payload, dict):
            raise core.ManagerError("创建中转站 API Key 的参数无效。")
        account, settings = self._saved_account(account_id)
        preview, _existing_secrets, session = self._scan_saved_account(account)
        name = _text(payload.get("name"), 80) or "Agent Manager"
        previous_ids = {
            str(item.get("id") or "")
            for item in preview.get("keys", [])
            if isinstance(item, dict) and str(item.get("id") or "")
        }
        relay_group_id: object = None
        if preview.get("adapter") == "sub2api":
            requested_group_id = _text(payload.get("relayGroupId"), 80)
            selected_group = next(
                (
                    item
                    for item in preview.get("groups", [])
                    if isinstance(item, dict)
                    and str(item.get("id") or "") == requested_group_id
                    and item.get("active", True)
                    and _codex_group_record(item)
                ),
                None,
            )
            if not selected_group:
                raise core.ManagerError("请选择一个可用的 Codex / OpenAI 中转站分组。")
            raw_group_id = selected_group.get("id")
            relay_group_id = int(raw_group_id) if str(raw_group_id).isdigit() else raw_group_id
        headers = {**_saved_auth_headers(session), "Content-Type": "application/json"}
        if preview.get("adapter") == "new-api":
            path = "/api/token/"
            create_payload = {
                "name": name,
                "remain_quota": 0,
                "expired_time": -1,
                "unlimited_quota": True,
                "model_limits_enabled": False,
                "model_limits": "",
                "allow_ips": "",
                "group": "",
                "auto_groups": [],
                "cross_group_retry": False,
            }
        else:
            path = "/api/v1/keys"
            create_payload = {"name": name, "group_id": relay_group_id}
        raw_created = _saved_json_request(
            session,
            path,
            method="POST",
            headers=headers,
            payload=create_payload,
        )
        if not _response_ok(raw_created):
            body = raw_created.get("body") if isinstance(raw_created, dict) else None
            message = _text(body.get("message"), 400) if isinstance(body, dict) else ""
            raise core.ManagerError(message or "中转站没有成功创建 API Key。")
        direct_record: dict | None = None
        direct_secret = ""
        created_data = _response_data(raw_created)
        if isinstance(created_data, dict):
            direct_record, direct_secret = _normalize_key_record(created_data, preview["adapter"])

        refreshed_preview, refreshed_secrets, refreshed_session = self._scan_saved_account(account)
        candidates = [
            item
            for item in refreshed_preview.get("keys", [])
            if isinstance(item, dict)
            and str(item.get("id") or "") not in previous_ids
            and _text(item.get("name"), 80) == name
            and _codex_key_record(item)
        ]
        if not candidates:
            candidates = [
                item
                for item in refreshed_preview.get("keys", [])
                if isinstance(item, dict)
                and str(item.get("id") or "") not in previous_ids
                and _codex_key_record(item)
            ]
        record = candidates[0] if candidates else direct_record
        if not record or not _codex_key_record(record):
            raise core.ManagerError("Key 已创建，但站点没有返回可确认的 Codex Key；请刷新账号后重试。")
        key_id = str(record.get("id") or "")
        secret = str(refreshed_secrets.get(key_id) or direct_secret or "")
        if not secret:
            secret = self._reveal_saved_key(refreshed_session, refreshed_preview["adapter"], key_id)
        if not secret:
            raise core.ManagerError("Key 已创建，但站点没有返回完整 Key；请在网站中查看后手动导入。")
        selected_preview = _codex_only_preview(refreshed_preview, key_ids={key_id})
        provider_id = str(account.get("providerId") or "")
        sync_to_api = provider_id in set((settings.get("web2api") or {}).get("providerIds", []))
        result = core.import_relay_account(
            selected_preview,
            {key_id: secret},
            group_id=account.get("groupId") or "relay",
            proxy_enabled=sync_to_api,
            endpoint_id=account.get("selectedEndpointId"),
            selected_key_id=account.get("selectedKeyId"),
            relay_account_id=account.get("id"),
        )
        if refreshed_preview.get("quotaPerUnit") is not None:
            try:
                result["account"] = self._update_quick_account_metadata(
                    str(account.get("id") or ""),
                    {},
                    None,
                    refreshed_preview.get("quotaPerUnit"),
                )
            except core.ManagerError:
                pass
        core.store_relay_account_dashboard_session(str(account.get("id") or ""), refreshed_session)
        return {
            **result,
            "created": next(
                (
                    item
                    for item in (result.get("account") or {}).get("keys", [])
                    if str(item.get("id") or "") == key_id
                ),
                record,
            ),
        }

    @_serialized_dashboard
    def update_saved_key_group(
        self,
        account_id: object,
        key_id: object,
        group_id: object,
    ) -> dict:
        """Change a Key's group on the relay site, then mirror it locally.

        The previous implementation changed only Agent Manager metadata, so
        the dropdown appeared successful while the relay kept routing through
        the old group.  Saved dashboard credentials are required because this
        is an authenticated remote mutation, not a Codex configuration edit.
        """

        account, _settings = self._saved_account(account_id)
        preview, _secrets, session = self._scan_saved_account(account)
        normalized_key_id = _text(key_id, 80)
        normalized_group_id = _text(group_id, 80)
        key = next(
            (
                item
                for item in preview.get("keys", [])
                if isinstance(item, dict)
                and str(item.get("id") or "") == normalized_key_id
                and _codex_key_record(item)
            ),
            None,
        )
        if not key:
            raise core.ManagerError("中转站网站上已找不到这个 Codex API Key，请先刷新账号。")
        group = next(
            (
                item
                for item in preview.get("groups", [])
                if isinstance(item, dict)
                and str(item.get("id") or "") == normalized_group_id
                and item.get("active", True)
                and _codex_group_record(item)
            ),
            None,
        )
        if not group:
            raise core.ManagerError("所选中转站分组已不可用，请刷新账号后重新选择。")

        normalized_account_id = str(account.get("id") or "")
        if str(key.get("groupId") or "") == normalized_group_id:
            local = core.update_relay_account_key_group(
                normalized_account_id,
                normalized_key_id,
                normalized_group_id,
            )
            return {
                **local,
                "remoteUpdated": False,
                "remoteAdapter": str(preview.get("adapter") or ""),
            }

        adapter = str(preview.get("adapter") or session.get("adapter") or "")
        headers = {**_saved_auth_headers(session), "Content-Type": "application/json"}
        if adapter == "sub2api":
            remote_group_id: object = (
                int(normalized_group_id)
                if normalized_group_id.isdigit()
                else normalized_group_id
            )
            updated = _saved_json_request(
                session,
                f"/api/v1/keys/{urllib.parse.quote(normalized_key_id, safe='')}",
                method="PUT",
                headers=headers,
                payload={"group_id": remote_group_id},
            )
        elif adapter == "new-api":
            details = _saved_json_request(
                session,
                f"/api/token/{urllib.parse.quote(normalized_key_id, safe='')}",
                headers=headers,
            )
            token = _response_data(details)
            if not isinstance(token, dict):
                raise core.ManagerError("中转站没有返回可编辑的 Token 详情。")
            token_payload = dict(token)
            token_payload["id"] = (
                int(normalized_key_id)
                if normalized_key_id.isdigit()
                else normalized_key_id
            )
            token_payload["group"] = normalized_group_id
            updated = _saved_json_request(
                session,
                "/api/token/",
                method="PUT",
                headers=headers,
                payload=token_payload,
            )
        else:
            raise core.ManagerError("该中转站类型尚不支持安全修改远端 Key 分组。")

        if not _response_ok(updated):
            body = updated.get("body") if isinstance(updated, dict) else None
            message = (
                _text(
                    body.get("message") or body.get("error") or body.get("detail"),
                    400,
                )
                if isinstance(body, dict)
                else ""
            )
            raise core.ManagerError(message or "中转站拒绝修改 API Key 分组。")

        local = core.update_relay_account_key_group(
            normalized_account_id,
            normalized_key_id,
            normalized_group_id,
        )
        return {
            **local,
            "remoteUpdated": True,
            "remoteAdapter": adapter,
        }

    @_serialized_dashboard
    def delete_saved_key(
        self,
        account_id: object,
        key_id: object,
        *,
        delete_remote: bool = False,
    ) -> dict:
        """Remove an imported Key locally and, when requested, from its site.

        Website deletion is guarded by a fresh, complete dashboard scan.  The
        scan proves same-origin account identity and ownership of the numeric
        adapter Key ID before the fixed adapter endpoint is allowed to receive
        a destructive request.  Local state is changed only after the website
        has reached the requested absent state.
        """

        if not isinstance(delete_remote, bool):
            raise core.ManagerError("删除范围参数无效。")
        account, _settings = self._saved_account(account_id)
        normalized_account_id = str(account.get("id") or "")
        normalized_key_id = core._relay_secret_key_id(key_id)
        local_key = next(
            (
                item
                for item in account.get("keys", [])
                if isinstance(item, dict)
                and str(item.get("id") or "") == normalized_key_id
            ),
            None,
        )
        if not local_key or not core.relay_account_key_configured(
            normalized_account_id,
            normalized_key_id,
        ):
            raise core.ManagerError("中转站 API Key 不存在或尚未导入。")

        scope = "website" if delete_remote else "local"
        remote_deleted = False
        remote_outcome = "not_requested"
        warnings: list[str] = []

        if delete_remote:
            try:
                preview, _secrets, session = self._scan_saved_account(
                    account,
                    full=True,
                )
            except (DashboardLoginRequired, DashboardIdentityChanged) as exc:
                return {
                    "scope": scope,
                    "removedKeyId": normalized_key_id,
                    "remoteDeleted": False,
                    "remoteOutcome": "login_required",
                    "localRemoved": False,
                    "needsKey": False,
                    "requiresLogin": True,
                    "requiresReapply": False,
                    "warnings": [core._redact_sensitive_text(exc, limit=240)],
                    "account": account,
                }
            except core.ManagerError as exc:
                message = core._redact_sensitive_text(exc, limit=240)
                if any(
                    marker in message
                    for marker in (
                        "尚未保存网页登录凭据",
                        "没有已保存的网页登录凭据",
                        "登录凭据已失效",
                        "无法解密",
                        "请重新登录",
                        "缺少可核验",
                    )
                ):
                    return {
                        "scope": scope,
                        "removedKeyId": normalized_key_id,
                        "remoteDeleted": False,
                        "remoteOutcome": "login_required",
                        "localRemoved": False,
                        "needsKey": False,
                        "requiresLogin": True,
                        "requiresReapply": False,
                        "warnings": [message],
                        "account": account,
                    }
                raise

            account_origin = core._provider_url_origin(
                core._validated_provider_portal_url(
                    account.get("origin") or account.get("portalUrl")
                )
            )
            preview_origin = core._provider_url_origin(
                core._validated_provider_portal_url(
                    preview.get("origin") or preview.get("portalUrl")
                )
            )
            session_origin = core._provider_url_origin(
                core._validated_provider_portal_url(session.get("origin"))
            )
            if not account_origin or {account_origin, preview_origin, session_origin} != {
                account_origin
            }:
                raise core.ManagerError("中转站账号、登录凭据与刷新结果来源不一致，已停止网站删除。")

            account_adapter = _text(account.get("adapter"), 40)
            preview_adapter = _text(preview.get("adapter"), 40)
            session_adapter = _text(session.get("adapter"), 40)
            if (
                account_adapter not in {"new-api", "sub2api"}
                or preview_adapter != account_adapter
                or session_adapter != account_adapter
            ):
                raise core.ManagerError("中转站类型无法安全确认，已停止网站删除。")
            _assert_dashboard_identity(
                account.get("user") if isinstance(account.get("user"), dict) else {},
                preview.get("user") if isinstance(preview.get("user"), dict) else {},
                expected_adapter=account_adapter,
                actual_adapter=preview_adapter,
            )
            if (
                preview.get("keysAuthoritative") is not True
                or preview.get("keysCatalogComplete") is not True
            ):
                raise core.ManagerError(
                    "中转站 API Key 列表未完整返回，无法核验 Key 所有权；未发送删除请求。"
                )

            remote_key = next(
                (
                    item
                    for item in preview.get("keys", [])
                    if isinstance(item, dict)
                    and str(item.get("id") or "") == normalized_key_id
                ),
                None,
            )
            if remote_key is None:
                remote_outcome = "already_absent"
                warnings.append("网站完整列表中已没有这个 API Key；本次只清理本机记录。")
            else:
                if (
                    not re.fullmatch(r"[1-9][0-9]{0,18}", normalized_key_id)
                    or int(normalized_key_id) > 9_223_372_036_854_775_807
                ):
                    raise core.ManagerError(
                        "该网站 Key ID 不是受支持的正整数，未发送删除请求。"
                    )
                path = (
                    f"/api/token/{normalized_key_id}"
                    if account_adapter == "new-api"
                    else f"/api/v1/keys/{normalized_key_id}"
                )
                deleted = _saved_json_request(
                    session,
                    path,
                    method="DELETE",
                    headers=_saved_auth_headers(session),
                )
                status = int(deleted.get("status") or 0)
                if status == 401:
                    return {
                        "scope": scope,
                        "removedKeyId": normalized_key_id,
                        "remoteDeleted": False,
                        "remoteOutcome": "login_required",
                        "localRemoved": False,
                        "needsKey": False,
                        "requiresLogin": True,
                        "requiresReapply": False,
                        "warnings": ["网站删除请求未通过身份验证，请重新登录后重试。"],
                        "account": account,
                    }
                if status == 404:
                    # A router/permission 404 does not prove Key absence.
                    # Only the authoritative catalog above can establish it.
                    raise core.ManagerError("网站删除接口未确认成功（404）；已保留本机 Key，请在网站确认后刷新。")
                elif status == 403:
                    raise core.ManagerError("网站拒绝删除此 Key；请确认删除权限，或在网站操作后刷新。本机记录保持不变。")
                elif not _response_ok(deleted):
                    body = deleted.get("body") if isinstance(deleted, dict) else None
                    message = (
                        _text(
                            body.get("message") or body.get("error") or body.get("detail"),
                            400,
                        )
                        if isinstance(body, dict)
                        else ""
                    )
                    raise core.ManagerError(message or "中转站拒绝删除 API Key；本机记录保持不变。")
                else:
                    remote_deleted = True
                    remote_outcome = "deleted"

        try:
            local = core.remove_relay_account_key(
                normalized_account_id,
                normalized_key_id,
            )
        except core.ManagerError as exc:
            if not delete_remote or remote_outcome not in {"deleted", "already_absent"}:
                raise
            local_error = core._redact_sensitive_text(exc, limit=300)
            try:
                latest_account, _latest_settings = self._saved_account(normalized_account_id)
                still_present = any(
                    isinstance(item, dict)
                    and str(item.get("id") or "") == normalized_key_id
                    for item in latest_account.get("keys", [])
                )
            except core.ManagerError:
                latest_account = account
                still_present = True
            if not still_present:
                return {
                    "scope": scope,
                    "removedKeyId": normalized_key_id,
                    "remoteDeleted": remote_deleted,
                    "remoteOutcome": remote_outcome,
                    "localRemoved": True,
                    "needsKey": not bool(latest_account.get("selectedKeyId")),
                    "requiresLogin": False,
                    "requiresReapply": False,
                    "warnings": [*warnings, "本机记录已由并发操作清理。"],
                    "account": latest_account,
                }
            return {
                "scope": scope,
                "removedKeyId": normalized_key_id,
                "remoteDeleted": remote_deleted,
                "remoteOutcome": remote_outcome,
                "localRemoved": False,
                "needsKey": not any(
                    isinstance(item, dict)
                    and str(item.get("id") or "") != normalized_key_id
                    for item in latest_account.get("keys", [])
                ),
                "requiresLogin": False,
                "requiresReapply": False,
                "warnings": [
                    *warnings,
                    "网站上的 API Key 已不可用，但本机记录清理失败：" + local_error,
                ],
                "account": latest_account,
                "localError": local_error,
            }

        return {
            **local,
            "scope": scope,
            "remoteDeleted": remote_deleted,
            "remoteOutcome": remote_outcome,
            "localRemoved": True,
            "needsKey": bool(local.get("needsKey")),
            "requiresLogin": False,
            "requiresReapply": bool(local.get("requiresReapply")),
            "warnings": [
                *warnings,
                *(
                    local.get("warnings", [])
                    if isinstance(local.get("warnings"), list)
                    else []
                ),
            ],
        }

    @_serialized_dashboard
    def refresh_account(self, account_id: object, *, full: bool = False) -> dict:
        """Refresh one relay card, using the lightweight path by default.

        Quick refresh validates/renews only the saved dashboard identity, then
        asks the selected API Key to refresh models and, only when the user
        response lacks balance, its balance endpoint. Key and group catalogs
        remain cached. ``full=True`` retains the authoritative management scan
        used by explicit synchronization flows.
        """

        account, _settings = self._saved_account(account_id)
        account = json.loads(json.dumps(account))
        normalized_id = str(account.get("id") or "")

        def finish_balance(result: dict, *, error: str = "", fresh: bool = False) -> dict:
            if not fresh:
                error = error or "网页登录未返回可验证的账号余额；保留上次余额。"
                if error not in result.setdefault("warnings", []):
                    result["warnings"].append(core._redact_sensitive_text(error, limit=240))
                try:
                    result["account"] = self._record_stale_balance(normalized_id, account, error)
                except core.ManagerError as exc:
                    # Return an honest state even when persistence itself fails.
                    current = dict(result.get("account") or account)
                    current.update({
                        "balance": account.get("balance"),
                        "balanceFreshness": "stale" if isinstance(account.get("balance"), dict)
                        and _number(account["balance"].get("remaining")) is not None else "unavailable",
                        "balanceUpdatedAt": account.get("balanceUpdatedAt"),
                        "balanceCheckedAt": core.now_iso(),
                        "balanceError": core._redact_sensitive_text(error, limit=240),
                        "balanceSource": account.get("balanceSource") or "relay-dashboard",
                    })
                    result["account"] = current
                    result.setdefault("warnings", []).append(
                        "余额状态未能保存：" + core._redact_sensitive_text(exc, limit=160)
                    )
            current = result.get("account") or {}
            result["balanceRefresh"] = {
                "freshness": current.get("balanceFreshness") or ("fresh" if fresh else "unavailable"),
                "updatedAt": current.get("balanceUpdatedAt"),
                "checkedAt": current.get("balanceCheckedAt"),
                "error": current.get("balanceError"),
                "source": current.get("balanceSource") or "relay-dashboard",
            }
            return result

        def with_key_catalog(live: dict) -> dict:
            live_warnings = (
                list(live.get("warnings", []))
                if isinstance(live.get("warnings"), list)
                else []
            )
            if live.get("providerDisabled") is True:
                return {
                    **live,
                    "modelRefresh": None,
                    "warnings": list(dict.fromkeys(item for item in live_warnings if item)),
                }
            try:
                metadata = core.refresh_relay_account(
                    normalized_id,
                    refresh_balance=False,
                    fallback_notice=False,
                )
            except core.ManagerError as exc:
                return {
                    **live,
                    "warnings": [
                        *live_warnings,
                        f"网页数据已同步，但模型目录刷新失败：{core._redact_sensitive_text(exc, limit=220)}"
                    ],
                }
            metadata_warnings = (
                list(metadata.get("warnings", []))
                if isinstance(metadata.get("warnings"), list)
                else []
            )
            return {
                **live,
                "account": metadata.get("account") or live.get("account"),
                "modelRefresh": metadata.get("modelRefresh"),
                "requiresReapply": bool(
                    live.get("requiresReapply") or metadata.get("requiresReapply")
                ),
                "warnings": list(
                    dict.fromkeys(
                        item for item in [*live_warnings, *metadata_warnings] if item
                    )
                ),
            }

        def full_key_fallback() -> dict:
            try:
                return core.refresh_relay_account(
                    normalized_id, refresh_balance=True, fallback_notice=False
                )
            except core.ManagerError as exc:
                return {"account": account, "modelRefresh": None, "warnings": [
                    "当前 Key 刷新失败：" + core._redact_sensitive_text(exc, limit=220)
                ]}

        def quick_key_refresh(
            *,
            dashboard_authenticated: bool,
            requires_login: bool,
            dashboard_requests: int | None,
            warning: str = "",
            quick_preview: dict | None = None,
        ) -> dict:
            dashboard_balance = (
                quick_preview.get("_balance")
                if isinstance(quick_preview, dict)
                and isinstance(quick_preview.get("_balance"), dict)
                else None
            )
            has_dashboard_balance = bool(
                dashboard_balance
                and _number(dashboard_balance.get("remaining")) is not None
            )
            balance_unavailable_reason = (
                str(quick_preview.get("_balanceUnavailableReason") or "")
                if isinstance(quick_preview, dict)
                else ""
            )
            preserve_unknown_quota_balance = (
                balance_unavailable_reason == "quota_per_unit_unknown"
            )
            # Models are always explicitly refreshed. Balance uses the same
            # authenticated user response when present; only missing balance
            # falls back to the selected Key's potentially multi-endpoint
            # balance probe.
            try:
                result = core.refresh_relay_account(
                    normalized_id,
                    refresh_balance=(
                        not has_dashboard_balance and not preserve_unknown_quota_balance
                    ),
                    fallback_notice=False,
                )
            except core.ManagerError as exc:
                result = {"account": account, "modelRefresh": None, "warnings": [
                    "当前 Key 刷新失败：" + core._redact_sensitive_text(exc, limit=220)
                ]}
            metadata_warning = ""
            if dashboard_authenticated and isinstance(quick_preview, dict):
                account_user = quick_preview.get("_accountUser")
                if isinstance(account_user, dict) or has_dashboard_balance:
                    try:
                        result["account"] = self._update_quick_account_metadata(
                            normalized_id,
                            account_user,
                            dashboard_balance,
                            quick_preview.get("_quotaPerUnitLearned"),
                        )
                    except core.ManagerError as exc:
                        metadata_warning = (
                            "当前 Key 已刷新，但网页登录余额写回失败："
                            + core._redact_sensitive_text(exc, limit=220)
                        )
            warnings = list(result.get("warnings", []))
            if preserve_unknown_quota_balance:
                warnings.insert(
                    0,
                    "站点未提供可验证的额度换算比例；已保留原余额，模型目录仍已刷新。",
                )
            if metadata_warning:
                warnings.insert(0, metadata_warning)
            if warning:
                warnings.insert(0, warning)
            result.update(
                {
                    "warnings": list(dict.fromkeys(item for item in warnings if item)),
                    "requiresLogin": requires_login,
                    "liveDashboard": dashboard_authenticated,
                    "refreshMode": "quick",
                    "catalogCache": {
                        "keys": "preserved",
                        "groups": "preserved",
                        "models": "requested_from_selected_key",
                        "balance": (
                            "updated_from_dashboard"
                            if has_dashboard_balance and not metadata_warning
                            else "preserved_quota_conversion_unknown"
                            if preserve_unknown_quota_balance
                            else "preserved_dashboard_balance"
                        ),
                    },
                    "requestBudget": {
                        "dashboardRequests": dashboard_requests,
                        "dashboardMaximum": 3
                        if str(account.get("adapter") or "") == "sub2api"
                        else 7,
                        "selectedKeyRefreshCalls": 1,
                        "selectedKeyOperations": (
                            1
                            if has_dashboard_balance or preserve_unknown_quota_balance
                            else 2
                        ),
                        "selectedKeyModelsRequested": True,
                        "selectedKeyBalanceRequested": (
                            not has_dashboard_balance and not preserve_unknown_quota_balance
                        ),
                        "selectedKeyOperationsParallel": (
                            not has_dashboard_balance and not preserve_unknown_quota_balance
                        ),
                    },
                }
            )
            return finish_balance(
                result,
                fresh=has_dashboard_balance and not metadata_warning,
                error=metadata_warning or warning or balance_unavailable_reason,
            )

        def login_error(value: Exception) -> bool:
            if isinstance(value, (DashboardLoginRequired, DashboardIdentityChanged)):
                return True
            # Compatibility for older saved-session loaders and test doubles
            # that predate DashboardLoginRequired but use the same messages.
            message = str(value)
            return any(
                marker in message
                for marker in (
                    "尚未保存网页登录凭据",
                    "没有已保存的网页登录凭据",
                    "登录凭据已失效",
                    "无法解密",
                    "请重新登录",
                    "缺少可核验",
                )
            )

        saved_error = ""
        try:
            preview, _secrets_by_id, _session = self._scan_saved_account(
                account,
                full=bool(full),
            )
            if preview.get("_refreshScope") == "account":
                return quick_key_refresh(
                    dashboard_authenticated=True,
                    requires_login=False,
                    dashboard_requests=int(preview.get("_requestCount") or 0),
                    quick_preview=preview,
                )
            live = core.sync_relay_account_snapshot(
                normalized_id,
                _codex_only_preview(preview),
            )
            full_result = {
                **with_key_catalog(live),
                "refreshMode": "full",
            }
            balance_written = False
            if isinstance(preview.get("balance"), dict) or preview.get("quotaPerUnit") is not None:
                try:
                    full_result["account"] = self._update_quick_account_metadata(
                        normalized_id,
                        {},
                        preview.get("balance"),
                        preview.get("quotaPerUnit"),
                    )
                    balance_written = isinstance(preview.get("balance"), dict) and _number(
                        preview["balance"].get("remaining")
                    ) is not None
                except core.ManagerError as exc:
                    full_result.setdefault("warnings", []).append(
                        "额度换算元数据未能缓存："
                        + core._redact_sensitive_text(exc, limit=220)
                    )
            return finish_balance(full_result, fresh=balance_written)
        except DashboardTemporarilyUnavailable as exc:
            return quick_key_refresh(
                dashboard_authenticated=False,
                requires_login=False,
                dashboard_requests=None,
                warning=core._redact_sensitive_text(exc, limit=240),
            )
        except core.ManagerError as exc:
            saved_error = core._redact_sensitive_text(exc, limit=240)
            if not full:
                return quick_key_refresh(
                    dashboard_authenticated=False,
                    requires_login=login_error(exc),
                    dashboard_requests=None,
                    warning=(
                        f"网页登录续期失败，账号余额未更新；当前 Key 刷新仍已继续：{saved_error}"
                        if login_error(exc)
                        else f"网页登录状态暂未确认，当前 Key 刷新仍已继续：{saved_error}"
                    ),
                )
        with self.lock:
            session_id = str(self._session.get("sessionId") or "")
            portal_url = str(self._session.get("portalUrl") or "")
            has_window = self.window is not None and not self._expired_locked()
        account_origin = core._provider_url_origin(account.get("origin") or account.get("portalUrl"))
        session_origin = core._provider_url_origin(portal_url) if portal_url else ""
        if has_window and session_id and session_origin == account_origin:
            try:
                scanned = self.scan(session_id)
                preview = scanned.get("preview") if isinstance(scanned, dict) else None
                if isinstance(preview, dict):
                    with self.lock:
                        dashboard_session = (
                            json.loads(json.dumps(self._dashboard_session))
                            if isinstance(self._dashboard_session, dict)
                            else None
                        )
                    if dashboard_session:
                        core.store_relay_account_dashboard_session(normalized_id, dashboard_session)
                    live = core.sync_relay_account_snapshot(
                        normalized_id,
                        _codex_only_preview(preview),
                    )
                    result = {
                        **with_key_catalog(live),
                        "status": scanned,
                        "refreshMode": "full",
                    }
                    result["account"] = self._update_quick_account_metadata(
                        normalized_id, {}, preview.get("balance"), preview.get("quotaPerUnit")
                    )
                    return finish_balance(result, fresh=isinstance(preview.get("balance"), dict)
                                          and _number(preview["balance"].get("remaining")) is not None)
            except core.ManagerError as exc:
                fallback = full_key_fallback()
                fallback["requiresLogin"] = True
                fallback["refreshMode"] = "full_fallback"
                fallback.setdefault("warnings", []).append(
                    f"站点登录会话刷新失败，已改用 Key 只读接口：{core._redact_sensitive_text(exc, limit=220)}"
                )
                return finish_balance(fallback, error=core._redact_sensitive_text(exc, limit=220))
        fallback = full_key_fallback()
        fallback["requiresLogin"] = True
        fallback["refreshMode"] = "full_fallback"
        if saved_error:
            fallback.setdefault("warnings", []).insert(
                0,
                f"网页登录续期失败，账号余额未更新；当前 Key 刷新仍已继续：{saved_error}",
            )
        return finish_balance(fallback, error=saved_error)

    def _release_window(self, *, session_id: object = None) -> None:
        with self.lock:
            if session_id is not None and str(self._session.get("sessionId") or "") != str(session_id):
                return
            window = self.window
            self.window = None
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass

    def close(self, *, silent: bool = False) -> dict:
        with self.lock:
            cancel_event = self._session.get("_cancelEvent")
            if isinstance(cancel_event, threading.Event):
                cancel_event.set()
            window = self.window
            self.window = None
            self._preview = None
            self._secrets = {}
            self._dashboard_session = None
            previous_id = self._session.get("sessionId")
            previous_mode = str(self._session.get("mode") or "new")
            previous_account_id = str(self._session.get("accountId") or "")
            self._session = self._empty_state()
            if previous_id and not silent:
                self._session.update(
                    {
                        "status": "cancelled",
                        "message": "中转站登录已取消。",
                        "mode": previous_mode,
                        "accountId": previous_account_id,
                        "autoAuth": {
                            "enabled": True,
                            "state": "cancelled",
                            "attempts": 0,
                            "maxAttempts": AUTO_AUTH_MAX_ATTEMPTS,
                            "nextCheckInMs": 0,
                        },
                        "updatedAt": core.now_iso(),
                    }
                )
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass
        return self.public_state()
