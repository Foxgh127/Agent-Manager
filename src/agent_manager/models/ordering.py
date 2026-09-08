"""Dashboard placement and explicit drops into the API pool.

HTTP integration: POST a JSON object to ``move_dashboard_card(core, payload)``.
Payload: sourceId, visibleIds, beforeId OR afterId, and/or groupId. The caller
should refresh dashboard data after success; no optimistic settings are needed.
"""


def known_card_ids(settings):
    """Keep relay cards first as the dashboard's default, including hidden IDs."""
    return list(dict.fromkeys(
        f"{kind}:{record['id']}"
        for kind, collection in (("relay", "relayAccounts"), ("account", "accounts"), ("provider", "providers"))
        for record in settings.get(collection, [])
        if isinstance(record, dict) and record.get("id")
    ))


def normalized_dashboard_order(settings):
    known = known_card_ids(settings)
    allowed = set(known)
    saved = settings.get("dashboardOrder", [])
    saved = saved if isinstance(saved, list) else []
    return list(dict.fromkeys([item for item in saved if isinstance(item, str) and item in allowed] + known))


def reorder_visible(order, visible_ids, source_id, *, before_id=None, after_id=None):
    """Permute only visible slots, keeping every hidden card at its old index."""
    visible = set(visible_ids)
    subset = [item for item in order if item in visible]
    anchor = before_id if before_id is not None else after_id
    if anchor == source_id:
        return list(order)
    subset.remove(source_id)
    position = subset.index(anchor) + (1 if after_id is not None else 0)
    subset.insert(position, source_id)
    replacement = iter(subset)
    return [next(replacement) if item in visible else item for item in order]


def _move_group(core, settings, source_id, group_id):
    """Use core membership helpers, expanding relay families before committing."""
    kind, record_id = source_id.split(":", 1)
    if kind == "account":
        core.assign_sources_to_group(group_id, [record_id], [])
        return
    providers = settings.get("providers", [])
    relays = settings.get("relayAccounts", [])
    relay_ids = {record_id} if kind == "relay" else {
        str(item["id"]) for item in relays if str(item.get("providerId") or "") == record_id
    }
    if kind == "provider":
        relay_ids.update(str(item["relayAccountId"]) for item in providers
                         if str(item.get("id")) == record_id and item.get("relayAccountId"))
    # Include primary providers even when legacy records lack relayAccountId.
    provider_ids = {record_id} if kind == "provider" else set()
    provider_ids.update(str(item["id"]) for item in providers
                        if str(item.get("relayAccountId") or "") in relay_ids)
    known_providers = {str(item["id"]) for item in providers}
    provider_ids.update(str(item.get("providerId")) for item in relays
                        if str(item.get("id")) in relay_ids and str(item.get("providerId")) in known_providers)
    for relay in relays:
        if str(relay.get("id")) in relay_ids:
            core.move_relay_account_group(str(relay["id"]), group_id)
    # The helper caps each operation at 100; all batches share our transaction.
    selected = sorted(provider_ids)
    for start in range(0, len(selected), 100):
        core.assign_sources_to_group(group_id, [], selected[start:start + 100])


def _join_pool(core, settings, source_id):
    kind, record_id = source_id.split(":", 1)
    pool = settings.get("web2api") or {}
    if kind == "account":
        account = next(item for item in settings.get("accounts", []) if str(item.get("id")) == record_id)
        if (account.get("authMode") != "chatgpt" or not core._account_codex_compatible(account)
                or core.account_invalid_reason(account)):
            raise core.ManagerError("该账号需要重新认证，或只有额度查询能力，暂不能加入 API 号池。")
        files = core._decode_snapshot_files(core._load_account_snapshot(record_id))
        if not core._auth_bytes_support_codex(files.get("auth.json") or b""):
            raise core.ManagerError("该账号缺少可用的 Codex 凭据，请重新认证。")
        already_member = record_id in pool.get("accountIds", [])
        core.set_account_proxy_enabled(record_id, True)
        pool_source = source_id
    else:
        relay = None
        if kind == "relay":
            relay = next(item for item in settings.get("relayAccounts", []) if str(item.get("id")) == record_id)
            record_id = str(relay.get("providerId") or "")
            key_id = str(relay.get("selectedKeyId") or "")
            selected = next((item for item in relay.get("keys", []) if str(item.get("id")) == key_id and item.get("active") is not False), None)
            if not record_id or not key_id or selected is None:
                raise core.ManagerError("该中转站没有选中的可用 Key，请先刷新或选择 Key。")
        provider = next((item for item in settings.get("providers", []) if str(item.get("id")) == record_id and item.get("kind") == "custom"), None)
        if not provider or not provider.get("models"):
            raise core.ManagerError("该 API 尚无可用模型，请先刷新模型目录。")
        key = core.load_provider_key(record_id, required=True)
        if relay:
            import hmac
            selected_key = core.load_relay_account_key(str(relay["id"]), key_id, required=True)
            if (provider.get("relayAccountId") not in (None, "", relay["id"])
                    or provider.get("relayKeyId") not in (None, "", key_id)
                    or not key or not selected_key or not hmac.compare_digest(key, selected_key)):
                raise core.ManagerError("中转站当前 Key 与关联 API 不一致，请刷新后重试。")
        if not key:
            raise core.ManagerError("该 API Key 不可用，请重新配置。")
        already_member = record_id in pool.get("providerIds", [])
        core.set_provider_proxy_enabled(record_id, True)
        pool_source = "provider:" + record_id
    current = core.load_settings().get("web2api") or {}
    return {"sourceId": source_id, "dropTarget": "apiPool", "poolSourceId": pool_source,
            "changed": not already_member, "alreadyMember": already_member,
            "pool": {key: current.get(key, []) for key in ("accountIds", "providerIds", "sourceOrder")}}


def move_dashboard_card(core, payload):
    """Atomically persist visual order and optional group membership.

    ``core`` is injected to avoid import cycles and to allow file-free tests.
    Locks follow the application's switch -> settings -> settings-file order.
    Only settings.json is snapshotted or written; credentials/config are untouched.
    """
    if not isinstance(payload, dict):
        raise core.ManagerError("卡片移动参数必须是对象。")
    source_id = payload.get("sourceId")
    before_id, after_id = payload.get("beforeId"), payload.get("afterId")
    group_id = payload.get("groupId")
    drop_target = payload.get("dropTarget")
    if not isinstance(source_id, str) or not source_id:
        raise core.ManagerError("请选择要移动的卡片。")
    if before_id is not None and after_id is not None:
        raise core.ManagerError("不能同时指定前后位置。")
    anchor = before_id if before_id is not None else after_id
    if anchor is not None and not isinstance(anchor, str):
        raise core.ManagerError("目标卡片 ID 必须是字符串。")
    if group_id is not None and (not isinstance(group_id, str) or not group_id or group_id == "all"):
        raise core.ManagerError("请选择有效的目标分组。")
    if drop_target not in (None, "apiPool") or (drop_target and (anchor is not None or group_id is not None)):
        raise core.ManagerError("卡片落点无效。")
    if anchor is None and group_id is None and drop_target is None:
        raise core.ManagerError("请选择目标卡片或分组。")
    with core.SWITCH_OPERATION_LOCK, core.SETTINGS_LOCK, core._settings_file_lock():
        snapshot = core._capture_file_bytes((core.SETTINGS_FILE,))
        try:
            settings = core.load_settings()
            order = normalized_dashboard_order(settings)
            if source_id not in order:
                raise core.ManagerError("卡片已不存在，请刷新后重试。")
            if drop_target == "apiPool":
                return _join_pool(core, settings, source_id)
            if group_id is not None:
                core._account_group(settings, group_id)
            if anchor is not None or "visibleIds" in payload:
                visible = payload.get("visibleIds")
                if (not isinstance(visible, list) or not visible
                        or any(not isinstance(item, str) or item not in order for item in visible)
                        or len(visible) != len(set(visible))
                        or source_id not in visible or (anchor is not None and anchor not in visible)):
                    raise core.ManagerError("可见卡片已变化，请刷新后重试。")
                # First reorder starts from the UI's existing presentation order.
                if not settings.get("dashboardOrder"):
                    visible_set = set(visible)
                    replacements = iter(visible)
                    order = [next(replacements) if item in visible_set else item for item in order]
                if anchor is not None:
                    order = reorder_visible(order, visible, source_id, before_id=before_id, after_id=after_id)
            previous_order = settings.get("dashboardOrder")
            group_assignments = {}
            if group_id is not None:
                original_groups = {
                    collection: {str(item["id"]): item.get("groupId") for item in settings.get(collection, [])}
                    for collection in ("accounts", "providers", "relayAccounts")
                }
                _move_group(core, settings, source_id, group_id)
                settings = core.load_settings()
                group_assignments = {
                    collection: {str(item["id"]): item.get("groupId") for item in settings.get(collection, [])
                                 if original_groups[collection].get(str(item["id"])) != item.get("groupId")}
                    for collection in original_groups
                }
            settings["dashboardOrder"] = order
            core.save_settings(settings)
            return {"sourceId": source_id, "dashboardOrder": order,
                    "groupId": group_id, "groupAssignments": group_assignments,
                    "changed": previous_order != order or group_id is not None}
        except Exception as exc:
            errors = core._restore_file_bytes(snapshot)
            if errors:
                raise core.ManagerError("卡片移动失败，设置回滚未完成：" + "; ".join(errors)) from exc
            raise
