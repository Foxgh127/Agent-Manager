"""Relay accounts services."""
from __future__ import annotations
from agent_manager import core as _core


def _relay_platform_kind(platform: object, name: object = "") -> str:
    value = f"{platform or ''} {name or ''}".casefold()
    if any(marker in value for marker in ("anthropic", "claude", "kiro")):
        return "claude"
    if any(marker in value for marker in ("openai", "codex", "gpt")):
        return "codex"
    return "other"



def _relay_is_codex_compatible(
    platform: object,
    name: object = "",
    models: object = None,
) -> bool:
    """Keep OpenAI-compatible/generic records, reject explicit non-Codex lanes.

    Several New API forks leave ``platform`` blank even though the Key serves
    an OpenAI-compatible `/v1` endpoint.  Blank metadata is therefore allowed;
    only an explicit Claude or media-generation marker is excluded.
    """

    identity = f"{platform or ''} {name or ''}".casefold()
    if any(marker in identity for marker in _core._RELAY_NON_CODEX_MARKERS):
        return False
    model_values = [
        str(item or "").strip().casefold()
        for item in (models if isinstance(models, list) else [])[:1000]
        if str(item or "").strip()
    ]
    if not model_values:
        return True
    # A generic Key may legitimately expose both GPT and Claude catalogs.
    # Keep the Key when at least one model is usable by this Codex console;
    # the individual non-Codex model rows are removed during normalization.
    return any(
        not any(marker in model for marker in _core._RELAY_NON_CODEX_MARKERS)
        for model in model_values
    )



def _relay_endpoint_is_codex_compatible(item: dict) -> bool:
    aliases = item.get("aliases") if isinstance(item.get("aliases"), list) else []
    value = " ".join(
        [
            str(item.get("name") or ""),
            str(item.get("baseUrl") or ""),
            str(item.get("description") or ""),
            *(str(alias or "") for alias in aliases[:20]),
        ]
    ).casefold()
    return not any(
        marker in value
        for marker in (
            "image.",
            "/images",
            "image generation",
            "image-gen",
            "midjourney",
            "dall-e",
            "生图",
            "绘图",
            "画图",
        )
    )



def _relay_account_identity(preview: dict, requested_id: object = None) -> tuple[str, str, str]:
    portal_url = _core._validated_provider_portal_url(preview.get("portalUrl"))
    parsed = _core.urllib.parse.urlsplit(portal_url)
    origin = _core.urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    supplied_origin = str(preview.get("origin") or "").strip()
    if supplied_origin:
        supplied_origin = _core._validated_provider_url(supplied_origin, "中转站来源地址")
        if _core._provider_url_origin(supplied_origin) != _core._provider_url_origin(origin):
            raise _core.ManagerError("中转站登录结果与登录网址来源不一致，已停止导入。")
    user = preview.get("user") if isinstance(preview.get("user"), dict) else {}
    user_identity = str(
        user.get("id") or user.get("email") or user.get("name") or parsed.hostname or "relay"
    ).strip().casefold()
    digest = _core.hashlib.sha256(
        f"{str(preview.get('adapter') or '').casefold()}|{origin.casefold()}|{user_identity}".encode("utf-8")
    ).hexdigest()
    generated_id = f"relay_{digest[:20]}"
    account_id = _core.slugify(str(requested_id or generated_id), "中转站账号 ID")
    return account_id, portal_url, origin



def _normalize_relay_account_snapshot(
    preview: dict,
    *,
    account_id: str,
    portal_url: str,
    origin: str,
    group_id: str,
    selected_key_id: str,
    selected_endpoint_id: str,
    provider_id: str,
    existing: dict | None,
    configured_key_ids: set[str],
) -> dict:
    def text(value: object, limit: int = 240) -> str:
        return str(value or "").strip()[:limit]

    raw_endpoints = preview.get("apiEndpoints") if isinstance(preview.get("apiEndpoints"), list) else []
    if not raw_endpoints and preview.get("baseUrl"):
        raw_endpoints = [
            {
                "id": "default",
                "name": "默认 API",
                "baseUrl": preview.get("baseUrl"),
                "modelsEndpoint": preview.get("modelsEndpoint"),
                "balanceEndpoint": preview.get("balanceEndpoint"),
                "isDefault": True,
            }
        ]
    endpoints: list[dict] = []
    for index, item in enumerate(raw_endpoints[:24]):
        if not isinstance(item, dict):
            continue
        if not _core._relay_endpoint_is_codex_compatible(item):
            continue
        base_url = _core._validated_provider_url(item.get("baseUrl"), "中转站 API 端点")
        endpoints.append(
            {
                "id": text(item.get("id") or ("default" if not endpoints else f"endpoint-{index + 1}"), 80),
                "name": text(item.get("name") or "API 端点", 120),
                "baseUrl": base_url,
                "modelsEndpoint": _core._validated_provider_related_url(
                    item.get("modelsEndpoint"), base_url, "模型目录接口", allow_empty=True, allow_query=True
                ),
                "balanceEndpoint": _core._validated_provider_related_url(
                    item.get("balanceEndpoint"), base_url, "余额接口", allow_empty=True, allow_query=True
                ),
                "description": text(item.get("description"), 240),
                "aliases": [text(value, 100) for value in item.get("aliases", [])[:12] if text(value, 100)]
                if isinstance(item.get("aliases"), list)
                else [],
                "isDefault": bool(item.get("isDefault") or not endpoints),
            }
        )
    if not endpoints:
        raise _core.ManagerError("中转站账号没有可用的 API 端点。")
    if selected_endpoint_id not in {item["id"] for item in endpoints}:
        selected_endpoint_id = next(
            (item["id"] for item in endpoints if item.get("isDefault")),
            endpoints[0]["id"],
        )

    groups: list[dict] = []
    for item in preview.get("groups", [])[:200] if isinstance(preview.get("groups"), list) else []:
        if not isinstance(item, dict) or not text(item.get("id"), 80):
            continue
        platform = text(item.get("platform"), 80)
        name = text(item.get("name") or f"分组 {item.get('id')}", 120)
        if not _core._relay_is_codex_compatible(platform, name):
            continue
        groups.append(
            {
                "id": text(item.get("id"), 80),
                "name": name,
                "description": text(item.get("description"), 240),
                "platform": platform,
                "platformKind": _core._relay_platform_kind(platform, name),
                "status": text(item.get("status") or "active", 40),
                "active": bool(item.get("active", True)),
                "rateMultiplier": _core._number_value(item.get("rateMultiplier")),
                "baseRateMultiplier": _core._number_value(item.get("baseRateMultiplier")),
                "customRateMultiplier": _core._number_value(item.get("customRateMultiplier")),
                "subscriptionType": text(item.get("subscriptionType"), 80),
                "dailyLimitUsd": _core._number_value(item.get("dailyLimitUsd")),
                "weeklyLimitUsd": _core._number_value(item.get("weeklyLimitUsd")),
                "monthlyLimitUsd": _core._number_value(item.get("monthlyLimitUsd")),
            }
        )

    keys: list[dict] = []
    for item in preview.get("keys", [])[:200] if isinstance(preview.get("keys"), list) else []:
        if not isinstance(item, dict) or not text(item.get("id"), 120):
            continue
        key_id = text(item.get("id"), 120)
        platform = text(item.get("groupPlatform"), 80)
        group_name = text(item.get("group"), 120)
        if not _core._relay_is_codex_compatible(platform, f"{group_name} {item.get('name') or ''}", item.get("models")):
            continue
        keys.append(
            {
                "id": key_id,
                "name": text(item.get("name") or "未命名 Key", 120),
                "maskedKey": text(item.get("maskedKey") or "已安全保存", 100),
                "secretConfigured": key_id in configured_key_ids,
                "active": bool(item.get("active", True)),
                "status": text(item.get("status") or "active", 40),
                "group": group_name,
                "groupId": text(item.get("groupId"), 80),
                "groupPlatform": platform,
                "platformKind": _core._relay_platform_kind(platform, group_name),
                "groupRateMultiplier": _core._number_value(item.get("groupRateMultiplier")),
                "quota": _core._number_value(item.get("quota")),
                "used": _core._number_value(item.get("used")),
                "unlimited": bool(item.get("unlimited")),
                "quotaCurrency": text(item.get("quotaCurrency") or "USD", 12),
                "expiresAt": item.get("expiresAt") if isinstance(item.get("expiresAt"), (str, int, float)) else None,
                "lastUsedAt": item.get("lastUsedAt") if isinstance(item.get("lastUsedAt"), (str, int, float)) else None,
                "todayUsed": _core._number_value(item.get("todayUsed")),
                "thirtyDayUsed": _core._number_value(item.get("thirtyDayUsed")),
                "totalUsed": _core._number_value(item.get("totalUsed")),
                "models": list(
                    dict.fromkeys(
                        text(model, 180)
                        for model in item.get("models", [])[:1000]
                        if text(model, 180) and _core._relay_is_codex_compatible("", text(model, 180))
                    )
                ) if isinstance(item.get("models"), list) else [],
            }
        )
    if selected_key_id not in {item["id"] for item in keys}:
        selected_key_id = next(
            (item["id"] for item in keys if item["secretConfigured"] and item["active"]),
            next((item["id"] for item in keys if item["secretConfigured"]), ""),
        )
    raw_remote_key_count = preview.get("remoteKeyCount")
    raw_remote_group_count = preview.get("remoteGroupCount")
    try:
        remote_key_count = max(0, int(raw_remote_key_count))
    except (TypeError, ValueError):
        remote_key_count = len(preview.get("keys", [])) if isinstance(preview.get("keys"), list) else 0
    try:
        remote_group_count = max(0, int(raw_remote_group_count))
    except (TypeError, ValueError):
        remote_group_count = len(preview.get("groups", [])) if isinstance(preview.get("groups"), list) else 0
    user = preview.get("user") if isinstance(preview.get("user"), dict) else {}
    balance = preview.get("balance") if isinstance(preview.get("balance"), dict) else {}
    previous = existing or {}
    remaining = _core._number_value(balance.get("remaining"))
    valid_balance = remaining is not None and _core.math.isfinite(remaining) and not isinstance(balance.get("remaining"), bool)
    checked_at = text(preview.get("detectedAt") or _core.now_iso(), 80)
    previous_balance = previous.get("balance") if isinstance(previous.get("balance"), dict) else None
    return {
        "id": account_id,
        "sourceType": "relay_account",
        "siteName": text(preview.get("siteName") or _core.urllib.parse.urlsplit(origin).hostname or "中转站", 160),
        "portalUrl": portal_url,
        "origin": origin,
        "adapter": text(preview.get("adapter"), 40),
        "adapterLabel": text(preview.get("adapterLabel") or "中转站账号", 80),
        "integrationKind": text(preview.get("integrationKind"), 40),
        "user": {
            "id": text(user.get("id"), 80),
            "name": text(user.get("name"), 160),
            "email": text(user.get("email"), 180),
            "group": text(user.get("group"), 120),
        },
        "balance": {"remaining": remaining, "used": _core._number_value(balance.get("used")),
                    "currency": text(balance.get("currency") or "USD", 12)} if valid_balance else previous_balance,
        "balanceFreshness": "fresh" if valid_balance else "stale" if previous_balance else "unavailable",
        "balanceUpdatedAt": checked_at if valid_balance else previous.get("balanceUpdatedAt"),
        "balanceCheckedAt": checked_at,
        "balanceError": None if valid_balance else "网站未返回可确认的账号余额，请刷新。",
        "balanceSource": "relay-dashboard",
        "dashboardMetadata": dict(previous.get("dashboardMetadata") or {}),
        "models": list(
            dict.fromkeys(
                text(model, 180)
                for model in preview.get("models", [])[:1000]
                if text(model, 180) and _core._relay_is_codex_compatible("", "", [model])
            )
        ) if isinstance(preview.get("models"), list) else [],
        "groups": groups,
        "keys": keys,
        "apiEndpoints": endpoints,
        "selectedKeyId": selected_key_id,
        "selectedEndpointId": selected_endpoint_id,
        "providerId": provider_id,
        "groupId": group_id,
        "supportsCreate": bool(preview.get("supportsCreate")),
        "remoteKeyCount": max(len(keys), remote_key_count),
        "remoteGroupCount": max(len(groups), remote_group_count),
        "detectedAt": text(preview.get("detectedAt") or _core.now_iso(), 80),
        "importedAt": str((existing or {}).get("importedAt") or _core.now_iso()),
        "updatedAt": _core.now_iso(),
    }



def _relay_selected_records(account: dict, key_id: str, endpoint_id: str) -> tuple[dict, dict]:
    key = next((item for item in account.get("keys", []) if str(item.get("id")) == key_id), None)
    if not key:
        raise _core.ManagerError("所选中转站 Key 已不存在，请重新登录同步账号。")
    endpoint = next(
        (item for item in account.get("apiEndpoints", []) if str(item.get("id")) == endpoint_id),
        None,
    )
    if not endpoint:
        raise _core.ManagerError("所选中转站 API 端点已不存在，请重新登录同步账号。")
    return key, endpoint



def _relay_provider_payload(account: dict, key: dict, endpoint: dict) -> dict:
    provider_id = str(account.get("providerId") or "")
    models = list(
        dict.fromkeys(
            [
                *[str(item) for item in key.get("models", []) if str(item).strip()],
                *[str(item) for item in account.get("models", []) if str(item).strip()],
            ]
        )
    )
    name_parts = [str(account.get("siteName") or "中转站账号")]
    if key.get("group"):
        name_parts.append(str(key["group"]))
    name_parts.append(str(key.get("name") or "API Key"))
    if not endpoint.get("isDefault") and endpoint.get("name"):
        name_parts.append(str(endpoint["name"]))
    return {
        "id": provider_id,
        "originalId": provider_id,
        "name": " · ".join(name_parts)[:160],
        "baseUrl": endpoint.get("baseUrl"),
        "portalUrl": account.get("portalUrl"),
        "integrationKind": account.get("integrationKind"),
        "envKey": f"{provider_id.upper()}_API_KEY",
        "modelsEndpoint": endpoint.get("modelsEndpoint") or "",
        "balanceEndpoint": endpoint.get("balanceEndpoint") or "",
        "models": models,
        "groupId": account.get("groupId") or "relay",
        "sourceType": "relay_account",
        "relayAccountId": account.get("id"),
        "relayKeyId": key.get("id"),
        "relayGroupId": key.get("groupId"),
        "relayGroupName": key.get("group"),
        "relayPlatform": key.get("groupPlatform"),
        "relayRateMultiplier": key.get("groupRateMultiplier"),
        "relayEndpointId": endpoint.get("id"),
        "relayEndpointName": endpoint.get("name"),
    }



def _relay_group_by_id(account: dict, group_id: object) -> dict:
    normalized = str(group_id or "").strip()
    group = next(
        (
            item
            for item in account.get("groups", [])
            if isinstance(item, dict) and str(item.get("id") or "") == normalized
        ),
        None,
    )
    if not group:
        raise _core.ManagerError("所选中转站分组不存在或不适用于 Codex。")
    if not bool(group.get("active", True)):
        raise _core.ManagerError("所选中转站分组已停用。")
    return group



def _apply_relay_group_to_key(key: dict, group: dict) -> None:
    key["groupId"] = str(group.get("id") or "")
    key["group"] = str(group.get("name") or "默认分组")
    key["groupPlatform"] = str(group.get("platform") or "")
    key["platformKind"] = str(group.get("platformKind") or "codex")
    key["groupRateMultiplier"] = _core._number_value(group.get("rateMultiplier"))



def import_relay_account(
    preview: dict,
    secrets_by_id: dict[str, str],
    *,
    group_id: object = "relay",
    proxy_enabled: bool = False,
    endpoint_id: object = None,
    selected_key_id: object = None,
    relay_account_id: object = None,
) -> dict:
    """Persist one dashboard login as one account with rotatable encrypted keys."""

    if not isinstance(preview, dict) or not isinstance(secrets_by_id, dict):
        raise _core.ManagerError("中转站账号导入内容无效。")
    with _core.SETTINGS_LOCK, _core.SECRETS_LOCK, _core._settings_file_lock():
        snapshot = _core._capture_file_bytes((_core.SETTINGS_FILE, _core.SECRETS_FILE))
        try:
            settings = _core.load_settings()
            local_group_id = str(group_id or "relay").strip()
            _core._account_group(settings, local_group_id)
            account_id, portal_url, origin = _core._relay_account_identity(preview, relay_account_id)
            existing = next(
                (item for item in settings.get("relayAccounts", []) if str(item.get("id")) == account_id),
                None,
            )
            if existing and _core._provider_url_origin(existing.get("origin") or existing.get("portalUrl")) != _core._provider_url_origin(origin):
                raise _core.ManagerError("要更新的中转站账号与当前登录站点不一致。")
            host = _core.urllib.parse.urlsplit(origin).hostname or "relay"
            provider_id = str((existing or {}).get("providerId") or "").strip()
            if not provider_id:
                provider_id = _core.slugify(
                    f"relay_{_core.re.sub(r'[^a-z0-9]+', '_', host.casefold()).strip('_')[:24]}_{account_id[-8:]}",
                    "Provider ID",
                )

            incoming_secret_ids = {
                _core._relay_secret_key_id(raw_key_id)
                for raw_key_id, secret in secrets_by_id.items()
                if str(secret or "").strip()
            }
            raw_preview_keys = preview.get("keys") if isinstance(preview.get("keys"), list) else []
            incoming_keys = [
                item
                for item in raw_preview_keys
                if isinstance(item, dict)
                and str(item.get("id") or "").strip() in incoming_secret_ids
            ]
            if not incoming_keys:
                raise _core.ManagerError("没有可写入的已选 Codex API Key，请重新勾选后导入。")
            merged_keys_by_id = {
                str(item.get("id") or "").strip(): dict(item)
                for item in (existing or {}).get("keys", [])
                if isinstance(item, dict) and str(item.get("id") or "").strip()
            }
            for item in incoming_keys:
                merged_keys_by_id[str(item.get("id") or "").strip()] = dict(item)
            preview_snapshot = _core.json.loads(_core.json.dumps(preview))
            preview_snapshot["keys"] = list(merged_keys_by_id.values())
            raw_keys = preview_snapshot["keys"]
            current_key_ids = {
                _core._relay_secret_key_id(item.get("id"))
                for item in raw_keys
                if isinstance(item, dict) and str(item.get("id") or "").strip()
            }
            secrets_payload = _core._secret_store()
            previous_encoded = _core._relay_account_secret_keys(secrets_payload, account_id)
            encoded_keys = {
                key_id: value
                for key_id, value in previous_encoded.items()
                if isinstance(value, str) and value
            }
            for raw_key_id, secret in secrets_by_id.items():
                key_id = _core._relay_secret_key_id(raw_key_id)
                normalized_secret = str(secret or "").strip()
                if key_id not in current_key_ids or not normalized_secret:
                    continue
                encoded_keys[key_id] = _core.base64.b64encode(_core.dpapi_protect(normalized_secret)).decode("ascii")
            configured_key_ids = set(encoded_keys)
            requested_key_id = str(selected_key_id or (existing or {}).get("selectedKeyId") or "").strip()
            requested_endpoint_id = str(
                endpoint_id
                or (existing or {}).get("selectedEndpointId")
                or preview.get("defaultEndpointId")
                or "default"
            ).strip()
            account = _core._normalize_relay_account_snapshot(
                preview_snapshot,
                account_id=account_id,
                portal_url=portal_url,
                origin=origin,
                group_id=local_group_id,
                selected_key_id=requested_key_id,
                selected_endpoint_id=requested_endpoint_id,
                provider_id=provider_id,
                existing=existing,
                configured_key_ids=configured_key_ids,
            )
            persisted_key_ids = {
                str(item.get("id") or "")
                for item in account.get("keys", [])
                if isinstance(item, dict) and str(item.get("id") or "")
            }
            encoded_keys = {
                key_id: value
                for key_id, value in encoded_keys.items()
                if key_id in persisted_key_ids
            }
            configured_key_ids = set(encoded_keys)
            provider_result = None
            if account.get("selectedKeyId"):
                key, endpoint = _core._relay_selected_records(
                    account,
                    str(account["selectedKeyId"]),
                    str(account["selectedEndpointId"]),
                )
                selected_secret = str(secrets_by_id.get(str(key["id"])) or "").strip()
                if not selected_secret:
                    encoded = encoded_keys.get(str(key["id"]))
                    if encoded:
                        selected_secret = _core.dpapi_unprotect(_core.base64.b64decode(encoded, validate=True))
                if not selected_secret:
                    raise _core.ManagerError("所选 Key 尚未安全导入，请重新登录后同步整个账号。")
                provider_payload = _core._relay_provider_payload(account, key, endpoint)
                provider_result = _core.import_api_account(
                    {
                        **provider_payload,
                        "key": selected_secret,
                        "model": provider_payload["models"][0] if provider_payload["models"] else "",
                        "balanceSnapshot": account.get("balance"),
                        "balanceSnapshotAt": account.get("detectedAt"),
                        "fetchModels": not bool(provider_payload["models"]),
                        "fetchBalance": False,
                        "activate": False,
                        "proxyEnabled": bool(proxy_enabled),
                    }
                )
            latest = _core.load_settings()
            relay_accounts = latest.setdefault("relayAccounts", [])
            latest_existing = next(
                (item for item in relay_accounts if str(item.get("id")) == account_id),
                None,
            )
            if latest_existing:
                relay_accounts[relay_accounts.index(latest_existing)] = account
            else:
                relay_accounts.append(account)
            _core.save_settings(latest)
            # Reload after Provider import so its encrypted key is not lost
            # when the relay-account key ring is added to the same file.
            secrets_payload = _core._secret_store()
            previous_account_secrets = secrets_payload.setdefault("relayAccounts", {}).get(account_id)
            if not isinstance(previous_account_secrets, dict):
                previous_account_secrets = {}
            secrets_payload["relayAccounts"][account_id] = {
                **previous_account_secrets,
                "keys": encoded_keys,
                "updatedAt": _core.now_iso(),
            }
            _core.atomic_write_json(_core.SECRETS_FILE, secrets_payload)
            return {
                "account": account,
                "provider": provider_result.get("provider") if provider_result else None,
                "profile": provider_result.get("profile") if provider_result else None,
                "keyCount": len(account.get("keys", [])),
                "configuredKeyCount": len(configured_key_ids),
                "selectedKeyId": account.get("selectedKeyId"),
            }
        except Exception as exc:
            rollback_errors = _core._restore_file_bytes(snapshot)
            detail = f"；回滚异常：{'；'.join(rollback_errors)}" if rollback_errors else ""
            if isinstance(exc, _core.ManagerError):
                raise _core.ManagerError(f"中转站账号导入失败：{exc}{detail}") from exc
            raise _core.ManagerError(f"中转站账号导入失败：{_core._redact_sensitive_text(exc, limit=320)}{detail}") from exc



def update_relay_account_selection(account_id: str, payload: dict) -> dict:
    """Select an encrypted relay key and optionally change its local group."""

    if not isinstance(payload, dict):
        raise _core.ManagerError("中转站账号选择内容无效。")
    account_id = _core.slugify(account_id, "中转站账号 ID")
    with _core.SETTINGS_LOCK, _core.SECRETS_LOCK, _core._settings_file_lock():
        snapshot = _core._capture_file_bytes((_core.SETTINGS_FILE, _core.SECRETS_FILE))
        try:
            settings = _core.load_settings()
            account = next(
                (item for item in settings.get("relayAccounts", []) if str(item.get("id")) == account_id),
                None,
            )
            if not account:
                raise _core.ManagerError("中转站账号不存在。")
            key_id = str(payload.get("keyId") or account.get("selectedKeyId") or "").strip()
            endpoint_id = str(
                payload.get("endpointId") or account.get("selectedEndpointId") or ""
            ).strip()
            key, endpoint = _core._relay_selected_records(account, key_id, endpoint_id)
            requested_group_id = str(payload.get("groupId") or "").strip()
            if requested_group_id:
                group = _core._relay_group_by_id(account, requested_group_id)
                _core._apply_relay_group_to_key(key, group)
            secret = _core.load_relay_account_key(account_id, key_id, required=True) or ""
            provider_payload = _core._relay_provider_payload(account, key, endpoint)
            # Dashboard adapters often cannot enumerate models for each Key.
            # Absence is unknown, not an authoritative empty catalog.  Omitting
            # the field lets the generated Provider retain its last successful
            # discovery while the explicit refresh path updates it in place.
            if not provider_payload.get("models"):
                provider_payload.pop("models", None)
            provider = _core._save_provider_locked(
                provider_payload,
                str(account.get("providerId") or provider_payload["id"]),
                api_key=secret,
            )
            latest = _core.load_settings()
            current = next(
                (item for item in latest.get("relayAccounts", []) if str(item.get("id")) == account_id),
                None,
            )
            if not current:
                raise _core.ManagerError("中转站账号在保存过程中被移除。")
            current["selectedKeyId"] = key_id
            current["selectedEndpointId"] = endpoint_id
            if requested_group_id:
                current_key = next(
                    (
                        item
                        for item in current.get("keys", [])
                        if isinstance(item, dict) and str(item.get("id") or "") == key_id
                    ),
                    None,
                )
                if not current_key:
                    raise _core.ManagerError("所选中转站 Key 在保存过程中被移除。")
                _core._apply_relay_group_to_key(
                    current_key,
                    _core._relay_group_by_id(current, requested_group_id),
                )
            current["updatedAt"] = _core.now_iso()
            _core.save_settings(latest)
            return {
                "account": current,
                "provider": provider,
                "requiresReapply": _core._provider_runtime_requires_reapply(latest, provider),
            }
        except Exception as exc:
            rollback_errors = _core._restore_file_bytes(snapshot)
            detail = f"；回滚异常：{'；'.join(rollback_errors)}" if rollback_errors else ""
            if isinstance(exc, _core.ManagerError):
                raise _core.ManagerError(f"中转站账号切换失败：{exc}{detail}") from exc
            raise _core.ManagerError(f"中转站账号切换失败：{_core._redact_sensitive_text(exc, limit=320)}{detail}") from exc



def update_relay_account_metadata(account_id: str, payload: dict) -> dict:
    """Update the visible portal URL while keeping credentials on one origin."""

    if not isinstance(payload, dict):
        raise _core.ManagerError("中转站账号设置内容无效。")
    account_id = _core.slugify(account_id, "中转站账号 ID")
    with _core.SETTINGS_LOCK, _core._settings_file_lock():
        settings = _core.load_settings()
        account = next(
            (item for item in settings.get("relayAccounts", []) if str(item.get("id") or "") == account_id),
            None,
        )
        if not account:
            raise _core.ManagerError("中转站账号不存在。")
        current_origin_url = _core._validated_provider_portal_url(
            account.get("origin") or account.get("portalUrl")
        )
        portal_url = _core._validated_provider_portal_url(
            payload.get("portalUrl") or account.get("portalUrl") or current_origin_url
        )
        if _core._provider_url_origin(portal_url) != _core._provider_url_origin(current_origin_url):
            raise _core.ManagerError("网站地址只能修改同一域名下的页面；切换到其他中转站请重新网页登录。")
        account["portalUrl"] = portal_url
        account["updatedAt"] = _core.now_iso()
        provider = next(
            (
                item
                for item in settings.get("providers", [])
                if str(item.get("id") or "") == str(account.get("providerId") or "")
            ),
            None,
        )
        if provider:
            provider["portalUrl"] = portal_url
            provider["updatedAt"] = _core.now_iso()
        _core.save_settings(settings)
        return {"account": account, "provider": provider}



def move_relay_account_group(account_id: str, group_id: object) -> dict:
    """Move the relay account and its generated Provider as one UI source."""

    account_id = _core.slugify(account_id, "中转站账号 ID")
    normalized_group_id = str(group_id or "").strip()
    with _core.SETTINGS_LOCK, _core._settings_file_lock():
        settings = _core.load_settings()
        _core._account_group(settings, normalized_group_id)
        account = next(
            (
                item
                for item in settings.get("relayAccounts", [])
                if str(item.get("id") or "") == account_id
            ),
            None,
        )
        if not account:
            raise _core.ManagerError("中转站账号不存在。")
        account["groupId"] = normalized_group_id
        account["updatedAt"] = _core.now_iso()
        provider = next(
            (
                item
                for item in settings.get("providers", [])
                if str(item.get("id") or "") == str(account.get("providerId") or "")
            ),
            None,
        )
        if provider:
            provider["groupId"] = normalized_group_id
            provider["updatedAt"] = _core.now_iso()
        _core.save_settings(settings)
        return {"account": account, "provider": provider}



def update_relay_account_key_group(account_id: str, key_id: str, group_id: object) -> dict:
    """Change one imported Key's local relay-group association.

    The website credential is never persisted, so this changes Agent Manager's
    routing metadata only; it does not mutate or delete anything on the relay
    website.  The selected Key is re-saved into its Provider immediately.
    """

    account_id = _core.slugify(account_id, "中转站账号 ID")
    normalized_key_id = _core._relay_secret_key_id(key_id)
    settings = _core.load_settings()
    account = next(
        (item for item in settings.get("relayAccounts", []) if str(item.get("id")) == account_id),
        None,
    )
    if not account:
        raise _core.ManagerError("中转站账号不存在。")
    if str(account.get("selectedKeyId") or "") == normalized_key_id:
        return _core.update_relay_account_selection(
            account_id,
            {
                "keyId": normalized_key_id,
                "endpointId": account.get("selectedEndpointId"),
                "groupId": group_id,
            },
        )
    with _core.SETTINGS_LOCK, _core._settings_file_lock():
        latest = _core.load_settings()
        current = next(
            (item for item in latest.get("relayAccounts", []) if str(item.get("id")) == account_id),
            None,
        )
        if not current:
            raise _core.ManagerError("中转站账号不存在。")
        key = next(
            (
                item
                for item in current.get("keys", [])
                if isinstance(item, dict) and str(item.get("id") or "") == normalized_key_id
            ),
            None,
        )
        if not key:
            raise _core.ManagerError("中转站 API Key 不存在。")
        group = _core._relay_group_by_id(current, group_id)
        _core._apply_relay_group_to_key(key, group)
        current["updatedAt"] = _core.now_iso()
        _core.save_settings(latest)
        return {"account": current, "requiresReapply": False}



def _disable_empty_relay_provider(
    settings: dict,
    secrets_payload: dict,
    account: dict,
) -> tuple[dict | None, bool]:
    """Disable the generated Provider while retaining its recoverable shell."""

    provider_id = str(account.get("providerId") or "")
    provider = next(
        (
            item
            for item in settings.get("providers", [])
            if isinstance(item, dict) and str(item.get("id") or "") == provider_id
        ),
        None,
    )
    provider_secrets = secrets_payload.setdefault("providers", {})
    had_provider_secret = bool(provider_secrets.pop(provider_id, None))
    if provider:
        previous_revision, previous_applied = _core._provider_runtime_revisions(provider)
        runtime_was_usable = bool(
            had_provider_secret or provider.get("relayKeyId") or provider.get("proxyEnabled")
        )
        provider.update(
            {
                "relayKeyId": "",
                "relayGroupId": "",
                "relayGroupName": "",
                "relayPlatform": "",
                "relayRateMultiplier": None,
                "proxyEnabled": False,
                # The process environment may still contain the last applied
                # secret until configuration is re-applied.  Clearing the
                # Provider catalog makes it unavailable immediately without
                # mutating that process-global environment inside this short
                # settings transaction.  The account keeps its model cache so
                # a later Key import can rebuild the Provider shell.
                "models": [],
                "modelCapabilities": {},
                "lastCheckStatus": "disabled",
                "lastCheckError": "此中转站账号当前没有已导入的 API Key。",
                "modelDiscoveryState": "disabled",
                "modelDiscoveryError": "此中转站账号当前没有已导入的 API Key。",
                "updatedAt": _core.now_iso(),
            }
        )
        provider["runtimeRevision"] = previous_revision + (1 if runtime_was_usable else 0)
        provider["appliedRuntimeRevision"] = previous_applied
    web2api = settings.setdefault("web2api", _core._default_web2api_settings())
    web2api["providerIds"] = [
        item for item in web2api.get("providerIds", []) if str(item) != provider_id
    ]
    web2api["sourceOrder"] = [
        item
        for item in web2api.get("sourceOrder", [])
        if str(item) != f"provider:{provider_id}"
    ]
    return (
        provider,
        bool(provider and _core._provider_runtime_requires_reapply(settings, provider)),
    )



def remove_relay_account_key(account_id: str, key_id: str) -> dict:
    """Remove one imported Key locally, retaining an empty recoverable account."""

    account_id = _core.slugify(account_id, "中转站账号 ID")
    normalized_key_id = _core._relay_secret_key_id(key_id)
    with _core._account_refresh_lock_for("relay-dashboard:" + account_id):
        with _core.SETTINGS_LOCK, _core.SECRETS_LOCK, _core._settings_file_lock():
            snapshot = _core._capture_file_bytes((_core.SETTINGS_FILE, _core.SECRETS_FILE))
            try:
                settings = _core.load_settings()
                account = next(
                    (
                        item
                        for item in settings.get("relayAccounts", [])
                        if str(item.get("id") or "") == account_id
                    ),
                    None,
                )
                if not account:
                    raise _core.ManagerError("中转站账号不存在。")
                secrets_payload = _core._secret_store()
                account_secrets = secrets_payload.setdefault("relayAccounts", {}).setdefault(
                    account_id,
                    {"keys": {}, "updatedAt": _core.now_iso()},
                )
                if not isinstance(account_secrets, dict):
                    account_secrets = {"keys": {}, "updatedAt": _core.now_iso()}
                    secrets_payload["relayAccounts"][account_id] = account_secrets
                key_store = account_secrets.get("keys")
                if not isinstance(key_store, dict):
                    key_store = {}
                    account_secrets["keys"] = key_store
                configured_keys = [
                    item
                    for item in account.get("keys", [])
                    if isinstance(item, dict)
                    and str(item.get("id") or "") in key_store
                    and isinstance(key_store.get(str(item.get("id") or "")), str)
                    and bool(key_store.get(str(item.get("id") or "")))
                ]
                if not any(
                    str(item.get("id") or "") == normalized_key_id
                    for item in configured_keys
                ):
                    raise _core.ManagerError("中转站 API Key 不存在或尚未导入。")

                remaining = [
                    item
                    for item in configured_keys
                    if str(item.get("id") or "") != normalized_key_id
                ]
                replacement_id = ""
                requires_reapply = False
                provider: dict | None = None
                if (
                    str(account.get("selectedKeyId") or "") == normalized_key_id
                    and remaining
                ):
                    replacement = next(
                        (item for item in remaining if bool(item.get("active", True))),
                        remaining[0],
                    )
                    replacement_id = str(replacement.get("id") or "")
                    selection = _core.update_relay_account_selection(
                        account_id,
                        {
                            "keyId": replacement_id,
                            "endpointId": account.get("selectedEndpointId"),
                        },
                    )
                    requires_reapply = bool(selection.get("requiresReapply"))
                    # The nested selection transaction wrote a newer Provider;
                    # continue from that state so the outer transaction cannot
                    # overwrite it with the stale pre-rotation snapshot.
                    settings = _core.load_settings()
                    account = next(
                        (
                            item
                            for item in settings.get("relayAccounts", [])
                            if str(item.get("id") or "") == account_id
                        ),
                        None,
                    )
                    if not account:
                        raise _core.ManagerError("中转站账号在切换备用 Key 后被移除。")
                    secrets_payload = _core._secret_store()
                    account_secrets = secrets_payload.setdefault("relayAccounts", {}).setdefault(
                        account_id,
                        {"keys": {}, "updatedAt": _core.now_iso()},
                    )
                    if not isinstance(account_secrets, dict):
                        account_secrets = {"keys": {}, "updatedAt": _core.now_iso()}
                        secrets_payload["relayAccounts"][account_id] = account_secrets
                    key_store = account_secrets.get("keys")
                    if not isinstance(key_store, dict):
                        key_store = {}
                        account_secrets["keys"] = key_store

                account["keys"] = [
                    item
                    for item in account.get("keys", [])
                    if str(item.get("id") or "") != normalized_key_id
                ]
                key_store.pop(normalized_key_id, None)
                account_secrets["updatedAt"] = _core.now_iso()

                if not remaining:
                    account["selectedKeyId"] = ""
                    provider, requires_reapply = _core._disable_empty_relay_provider(
                        settings,
                        secrets_payload,
                        account,
                    )
                elif replacement_id:
                    account["selectedKeyId"] = replacement_id
                    provider = next(
                        (
                            item
                            for item in settings.get("providers", [])
                            if str(item.get("id") or "")
                            == str(account.get("providerId") or "")
                        ),
                        None,
                    )
                account["updatedAt"] = _core.now_iso()
                _core.save_settings(settings)
                _core.atomic_write_json(_core.SECRETS_FILE, secrets_payload)
                warnings = (
                    [
                        "账号已没有本机 API Key；Provider 已停用。请重新导入 Key，或切换到其他账号。"
                    ]
                    if not remaining
                    else []
                )
                return {
                    "account": account,
                    "provider": provider,
                    "removedKeyId": normalized_key_id,
                    "remainingKeyCount": len(remaining),
                    "providerDisabled": not remaining,
                    "needsKey": not remaining,
                    "requiresReapply": requires_reapply,
                    "warnings": warnings,
                }
            except Exception as exc:
                rollback_errors = _core._restore_file_bytes(snapshot)
                detail = f"；回滚异常：{'；'.join(rollback_errors)}" if rollback_errors else ""
                if isinstance(exc, _core.ManagerError):
                    raise _core.ManagerError(f"删除中转站 API Key 失败：{exc}{detail}") from exc
                raise _core.ManagerError(
                    f"删除中转站 API Key 失败：{_core._redact_sensitive_text(exc, limit=320)}{detail}"
                ) from exc



def refresh_relay_account(
    account_id: str,
    *,
    refresh_balance: bool = True,
    fallback_notice: bool = True,
) -> dict:
    """Refresh model metadata and optionally balance through the selected Key."""

    account_id = _core.slugify(account_id, "中转站账号 ID")
    settings = _core.load_settings()
    account = next(
        (item for item in settings.get("relayAccounts", []) if str(item.get("id")) == account_id),
        None,
    )
    if not account:
        raise _core.ManagerError("中转站账号不存在。")
    provider_id = str(account.get("providerId") or "").strip()
    warnings: list[str] = []
    balance: dict | None = None
    model_refresh: dict | None = None
    if provider_id:
        operations = {"models": lambda: _core.refresh_provider_models(provider_id)}
        if refresh_balance:
            operations["balance"] = lambda: _core.fetch_provider_balance(provider_id)
        with _core.ThreadPoolExecutor(
            max_workers=len(operations),
            thread_name_prefix="relay-key-refresh",
        ) as executor:
            pending = {executor.submit(operation): name for name, operation in operations.items()}
            for future in _core.as_completed(pending):
                name = pending[future]
                try:
                    value = future.result()
                    if name == "balance":
                        balance = value
                    else:
                        model_refresh = value
                        warning = value.get("warning") if isinstance(value, dict) else None
                        if warning:
                            warnings.append(str(warning))
                except _core.ManagerError as exc:
                    warnings.append(_core._redact_sensitive_text(exc, limit=240))
    if fallback_notice:
        warnings.append(
            "额度与模型目录已通过当前 Key 刷新；网页登录续期不可用，因此 API 数量与分组沿用最近一次成功同步。"
        )
    with _core.SETTINGS_LOCK, _core._settings_file_lock():
        latest = _core.load_settings()
        current = next(
            (item for item in latest.get("relayAccounts", []) if str(item.get("id")) == account_id),
            None,
        )
        if not current:
            raise _core.ManagerError("中转站账号不存在。")
        provider = next(
            (item for item in latest.get("providers", []) if str(item.get("id")) == provider_id),
            {},
        )
        # Provider balance describes an API Key's allowance, not the website account.
        # Dashboard refresh owns account.balance and its freshness timestamps.
        refreshed_models = (
            model_refresh.get("models")
            if isinstance(model_refresh, dict) and isinstance(model_refresh.get("models"), list)
            else provider.get("models")
        )
        if isinstance(refreshed_models, list) and refreshed_models:
            current["models"] = list(
                dict.fromkeys(str(item).strip() for item in refreshed_models if str(item).strip())
            )
        current["updatedAt"] = _core.now_iso()
        _core.save_settings(latest)
        return {
            "account": current,
            "liveDashboard": False,
            "requiresReapply": bool(
                provider and _core._provider_runtime_requires_reapply(latest, provider)
            ),
            "warnings": list(dict.fromkeys(item for item in warnings if item)),
            "modelRefresh": model_refresh,
            "counts": {
                "keys": len(current.get("keys", [])),
                "groups": len(current.get("groups", [])),
                "models": len(current.get("models", [])),
            },
        }



def sync_relay_account_snapshot(account_id: str, preview: dict) -> dict:
    """Synchronize a complete dashboard scan without importing unknown secrets.

    A successful dashboard key list is authoritative for keys that were
    already imported: rows deleted or moved out of the Codex lane remotely are
    removed locally as well.  A failed/partial endpoint is explicitly marked
    non-authoritative and therefore keeps the last good local snapshot.
    """

    account_id = _core.slugify(account_id, "中转站账号 ID")
    if not isinstance(preview, dict):
        raise _core.ManagerError("中转站刷新内容无效。")
    keys_authoritative = bool(
        preview.get("keysAuthoritative") is True
        and preview.get("keysCatalogComplete", True) is not False
    )
    groups_authoritative = preview.get("groupsAuthoritative") is True
    incoming_rows = [
        item
        for item in preview.get("keys", [])
        if isinstance(item, dict) and str(item.get("id") or "")
    ]
    incoming_by_id = {str(item.get("id") or ""): item for item in incoming_rows}
    try:
        declared_total = int(preview.get("keysCatalogTotal"))
    except (TypeError, ValueError, OverflowError):
        declared_total = None
    if declared_total is not None and declared_total > len(incoming_by_id):
        keys_authoritative = False

    removed_key_ids: set[str] = set()
    warnings: list[str] = []
    if (
        preview.get("keysAuthoritative") is True
        or preview.get("keysCatalogComplete") is False
    ) and not keys_authoritative:
        warnings.append("网站 API Key 分页未完整返回；已保留未出现的本机 Key。")

    with _core._account_refresh_lock_for("relay-dashboard:" + account_id):
        with _core.SETTINGS_LOCK, _core.SECRETS_LOCK, _core._settings_file_lock():
            snapshot = _core._capture_file_bytes((_core.SETTINGS_FILE, _core.SECRETS_FILE))
            try:
                settings = _core.load_settings()
                existing = next(
                    (
                        item
                        for item in settings.get("relayAccounts", [])
                        if str(item.get("id") or "") == account_id
                    ),
                    None,
                )
                if not existing:
                    raise _core.ManagerError("中转站账号不存在。")
                existing_origin = _core._provider_url_origin(
                    existing.get("origin") or existing.get("portalUrl")
                )
                preview_origin = _core._provider_url_origin(
                    preview.get("origin") or preview.get("portalUrl")
                )
                if existing_origin != preview_origin:
                    raise _core.ManagerError("当前登录会话与要刷新的中转站账号不一致。")

                merged_keys = []
                existing_key_ids: set[str] = set()
                for current_key in existing.get("keys", []):
                    if not isinstance(current_key, dict) or not str(current_key.get("id") or ""):
                        continue
                    current_key_id = str(current_key.get("id") or "")
                    existing_key_ids.add(current_key_id)
                    fresh = incoming_by_id.get(current_key_id)
                    if fresh:
                        merged_keys.append({**current_key, **fresh})
                    elif not keys_authoritative:
                        merged_keys.append(dict(current_key))
                retained_key_ids = {
                    str(item.get("id") or "")
                    for item in merged_keys
                    if isinstance(item, dict) and str(item.get("id") or "")
                }
                if keys_authoritative:
                    removed_key_ids = existing_key_ids - retained_key_ids

                preview_snapshot = _core.json.loads(_core.json.dumps(preview))
                preview_snapshot["keys"] = merged_keys
                # A partial group response must not replace a previously
                # complete local group catalog.  Key-level group metadata can
                # still refresh independently from incoming Key rows.
                if not groups_authoritative:
                    preview_snapshot["groups"] = existing.get("groups", [])
                if not preview_snapshot.get("apiEndpoints"):
                    preview_snapshot["apiEndpoints"] = existing.get("apiEndpoints", [])

                secrets_payload = _core._secret_store()
                account_store = secrets_payload.setdefault("relayAccounts", {}).setdefault(
                    account_id,
                    {"keys": {}, "updatedAt": _core.now_iso()},
                )
                if not isinstance(account_store, dict):
                    account_store = {"keys": {}, "updatedAt": _core.now_iso()}
                    secrets_payload["relayAccounts"][account_id] = account_store
                encoded_keys = account_store.get("keys")
                if not isinstance(encoded_keys, dict):
                    encoded_keys = {}
                    account_store["keys"] = encoded_keys
                if keys_authoritative:
                    encoded_keys = {
                        stored_key_id: encoded
                        for stored_key_id, encoded in encoded_keys.items()
                        if stored_key_id in retained_key_ids
                        and isinstance(encoded, str)
                        and encoded
                    }
                    account_store["keys"] = encoded_keys
                    account_store["updatedAt"] = _core.now_iso()
                configured_ids = set(encoded_keys)
                previous_selected_key_id = str(existing.get("selectedKeyId") or "")
                account = _core._normalize_relay_account_snapshot(
                    preview_snapshot,
                    account_id=account_id,
                    portal_url=str(existing.get("portalUrl") or preview.get("portalUrl") or ""),
                    origin=str(existing.get("origin") or preview.get("origin") or ""),
                    group_id=str(existing.get("groupId") or "relay"),
                    selected_key_id=previous_selected_key_id,
                    selected_endpoint_id=str(existing.get("selectedEndpointId") or ""),
                    provider_id=str(existing.get("providerId") or ""),
                    existing=existing,
                    configured_key_ids=configured_ids,
                )
                relay_accounts = settings.setdefault("relayAccounts", [])
                relay_accounts[relay_accounts.index(existing)] = account

                provider_disabled = False
                requires_reapply = False
                if keys_authoritative and not account.get("selectedKeyId"):
                    _provider, requires_reapply = _core._disable_empty_relay_provider(
                        settings,
                        secrets_payload,
                        account,
                    )
                    provider_disabled = True
                    if removed_key_ids:
                        warnings.append(
                            "网站已没有已导入的 API Key；本机 Provider 已停用。请重新导入 Key，或切换到其他账号。"
                        )
                _core.save_settings(settings)
                _core.atomic_write_json(_core.SECRETS_FILE, secrets_payload)

                selection_result: dict | None = None
                if account.get("selectedKeyId"):
                    selection_result = _core.update_relay_account_selection(
                        account_id,
                        {
                            "keyId": account.get("selectedKeyId"),
                            "endpointId": account.get("selectedEndpointId"),
                        },
                    )
                    account = selection_result.get("account") or account
                    requires_reapply = bool(selection_result.get("requiresReapply"))

                return {
                    **(selection_result or {}),
                    "account": account,
                    "liveDashboard": True,
                    "requiresReapply": requires_reapply,
                    "keysAuthoritative": keys_authoritative,
                    "groupsAuthoritative": groups_authoritative,
                    "removedKeyCount": len(removed_key_ids),
                    "removedKeyIds": sorted(removed_key_ids),
                    "selectedKeyChanged": previous_selected_key_id
                    != str(account.get("selectedKeyId") or ""),
                    "providerDisabled": provider_disabled,
                    "needsKey": not bool(account.get("selectedKeyId")),
                    "warnings": list(dict.fromkeys(item for item in warnings if item)),
                    "counts": {
                        "keys": len(account.get("keys", [])),
                        "groups": len(account.get("groups", [])),
                        "models": len(account.get("models", [])),
                    },
                }
            except Exception as exc:
                rollback_errors = _core._restore_file_bytes(snapshot)
                detail = f"；回滚异常：{'；'.join(rollback_errors)}" if rollback_errors else ""
                if isinstance(exc, _core.ManagerError):
                    raise _core.ManagerError(f"中转站账号同步失败：{exc}{detail}") from exc
                raise _core.ManagerError(
                    f"中转站账号同步失败：{_core._redact_sensitive_text(exc, limit=320)}{detail}"
                ) from exc

