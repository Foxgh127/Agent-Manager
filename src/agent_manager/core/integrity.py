"""Integrity services."""
from __future__ import annotations
from agent_manager import core as _core


def settings_reference_integrity(settings: dict | None = None) -> dict:
    """Inspect persisted IDs without resolving or returning any credential value.

    References are deliberately checked separately from network/model health. A
    temporarily unavailable model catalog must never cause a saved route to be
    deleted, while a source ID that no longer exists can be repaired safely.
    """

    settings = settings or _core.load_settings()
    issues: list[dict] = []

    def add(code: str, path: str, detail: str, *, repairable: bool) -> None:
        issues.append(
            {
                "code": code,
                "path": path,
                "detail": detail,
                "repairable": bool(repairable),
            }
        )

    def ids(records: Any, kind: str) -> tuple[list[str], set[str]]:
        values = [
            str(item.get("id") or "").strip()
            for item in records if isinstance(item, dict)
        ] if isinstance(records, list) else []
        populated = [value for value in values if value]
        missing = len(values) - len(populated)
        if missing:
            add(f"missing_{kind}_id", kind, f"有 {missing} 条记录缺少 ID。", repairable=False)
        duplicate_count = len(populated) - len(set(populated))
        if duplicate_count:
            add(
                f"duplicate_{kind}_id",
                kind,
                f"有 {duplicate_count} 条记录使用了重复 ID。",
                repairable=False,
            )
        return populated, set(populated)

    account_values, account_ids = ids(settings.get("accounts", []), "account")
    provider_values, all_provider_ids = ids(settings.get("providers", []), "provider")
    profile_values, profile_ids = ids(settings.get("mainProfiles", []), "main_profile")
    _group_values, group_ids = ids(settings.get("accountGroups", []), "account_group")
    custom_provider_ids = {
        str(item.get("id"))
        for item in settings.get("providers", [])
        if isinstance(item, dict) and item.get("kind") == "custom" and item.get("id")
    }
    valid_provider_ids = set(all_provider_ids) | {"openai", _core.AGGREGATE_PROVIDER_ID}
    valid_source_ids = {
        *(f"account:{item}" for item in account_ids),
        *(f"provider:{item}" for item in custom_provider_ids),
    }
    valid_agent_names = {
        str(record.get("data", {}).get("name") or "").strip()
        for record in _core.discover_agents()
        if not record.get("error") and str(record.get("data", {}).get("name") or "").strip()
    }
    legacy_routes = settings.get("routes")
    if not isinstance(legacy_routes, dict):
        add("invalid_legacy_routes", "routes", "旧版 Agent 路由不是对象。", repairable=False)
    else:
        for level, route in legacy_routes.items():
            if not isinstance(route, dict):
                add(
                    "invalid_legacy_agent_route",
                    f"routes.{level}",
                    "旧版 Agent 路由不是对象。",
                    repairable=False,
                )
                continue
            route_agents = route.get("agents", [])
            if not isinstance(route_agents, list):
                add(
                    "invalid_legacy_agent_list",
                    f"routes.{level}.agents",
                    "旧版 Agent 路由列表不是数组。",
                    repairable=False,
                )
                continue
            for index, name in enumerate(route_agents):
                candidate = str(name or "").strip()
                if candidate and candidate not in valid_agent_names:
                    add(
                        "dangling_legacy_agent_route",
                        f"routes.{level}.agents[{index}]",
                        f"旧版 {level} 路由引用了不存在的 Agent `{candidate}`。",
                        repairable=True,
                    )
    explicit_model_keys: dict[str, set[str]] = {}
    for account in settings.get("accounts", []):
        if not isinstance(account, dict) or not account.get("id"):
            continue
        source_id = f"account:{account['id']}"
        models = {str(item).strip() for item in account.get("models", []) if str(item).strip()}
        if models:
            explicit_model_keys[source_id] = {_core._model_key(source_id, model_id) for model_id in models}
    for provider in settings.get("providers", []):
        if not isinstance(provider, dict) or provider.get("kind") != "custom" or not provider.get("id"):
            continue
        source_id = f"provider:{provider['id']}"
        models = {str(item).strip() for item in provider.get("models", []) if str(item).strip()}
        if models:
            explicit_model_keys[source_id] = {_core._model_key(source_id, model_id) for model_id in models}

    for index, account in enumerate(settings.get("accounts", [])):
        if not isinstance(account, dict):
            add("invalid_account_record", f"accounts[{index}]", "账号记录不是对象。", repairable=False)
            continue
        group_id = str(account.get("groupId") or "")
        if group_id and group_id not in group_ids:
            add(
                "dangling_account_group",
                f"accounts[{index}].groupId",
                f"账号引用了不存在的分组 `{group_id}`。",
                repairable=True,
            )

    for index, provider in enumerate(settings.get("providers", [])):
        if not isinstance(provider, dict):
            add("invalid_provider_record", f"providers[{index}]", "Provider 记录不是对象。", repairable=False)
            continue
        group_id = str(provider.get("groupId") or "")
        if group_id and group_id not in group_ids:
            add(
                "dangling_provider_group",
                f"providers[{index}].groupId",
                f"Provider 引用了不存在的分组 `{group_id}`。",
                repairable=True,
            )

    for index, profile in enumerate(settings.get("mainProfiles", [])):
        if not isinstance(profile, dict):
            add("invalid_main_profile", f"mainProfiles[{index}]", "主模型配置不是对象。", repairable=False)
            continue
        provider_id = str(profile.get("provider") or "openai")
        if provider_id not in valid_provider_ids:
            add(
                "dangling_main_profile_provider",
                f"mainProfiles[{index}].provider",
                f"主模型配置引用了不存在的 Provider `{provider_id}`。",
                repairable=True,
            )
    active_profile_id = str(settings.get("activeMainProfileId") or "")
    if active_profile_id and active_profile_id not in profile_ids:
        add(
            "dangling_active_main_profile",
            "activeMainProfileId",
            f"当前主模型配置 `{active_profile_id}` 已不存在。",
            repairable=True,
        )

    for provider_id in settings.get("managedProviderIds", []):
        value = str(provider_id or "")
        if value and value not in custom_provider_ids:
            add(
                "dangling_managed_provider",
                "managedProviderIds",
                f"托管 Provider `{value}` 已不存在。",
                repairable=True,
            )

    web2api = settings.get("web2api") if isinstance(settings.get("web2api"), dict) else {}
    for index, account_id in enumerate(web2api.get("accountIds", [])):
        value = str(account_id or "")
        if value not in account_ids:
            add(
                "dangling_pool_account",
                f"web2api.accountIds[{index}]",
                f"本地 API 号池引用了不存在的账号 `{value}`。",
                repairable=True,
            )
    for index, provider_id in enumerate(web2api.get("providerIds", [])):
        value = str(provider_id or "")
        if value not in custom_provider_ids:
            add(
                "dangling_pool_provider",
                f"web2api.providerIds[{index}]",
                f"本地 API 号池引用了不存在的 Provider `{value}`。",
                repairable=True,
            )
    active_account_id = str(web2api.get("activeAccountId") or "")
    if active_account_id and active_account_id not in account_ids:
        add(
            "dangling_active_pool_account",
            "web2api.activeAccountId",
            f"当前本地反代账号 `{active_account_id}` 已不存在。",
            repairable=True,
        )
    for index, source_id in enumerate(web2api.get("sourceOrder", [])):
        value = str(source_id or "")
        if value not in valid_source_ids:
            add(
                "dangling_pool_source_order",
                f"web2api.sourceOrder[{index}]",
                f"号池顺序引用了不存在的来源 `{value}`。",
                repairable=True,
            )

    workspace = settings.get("modelWorkspace") if isinstance(settings.get("modelWorkspace"), dict) else {}
    active_source_id = str(workspace.get("activeSourceId") or "")
    if active_source_id and active_source_id not in valid_source_ids:
        add(
            "dangling_workspace_source",
            "modelWorkspace.activeSourceId",
            f"主模型工作区引用了不存在的来源 `{active_source_id}`。",
            repairable=True,
        )

    def source_from_model_key(value: object) -> str:
        text = str(value or "")
        return text.rsplit("::", 1)[0] if "::" in text else ""

    def exact_model_is_stale(value: object) -> bool:
        text = str(value or "")
        source_id = source_from_model_key(text)
        known = explicit_model_keys.get(source_id)
        return bool(source_id in valid_source_ids and known is not None and text not in known)

    default_source = source_from_model_key(workspace.get("defaultModelKey"))
    if default_source and default_source not in valid_source_ids:
        add(
            "dangling_default_model_source",
            "modelWorkspace.defaultModelKey",
            f"默认模型引用了不存在的来源 `{default_source}`。",
            repairable=True,
        )
    elif exact_model_is_stale(workspace.get("defaultModelKey")):
        add(
            "stale_default_model",
            "modelWorkspace.defaultModelKey",
            "默认模型已不在该账号或中转站当前保存的模型目录中。",
            repairable=True,
        )
    for index, model_key in enumerate(workspace.get("selectedModels", [])):
        source_id = source_from_model_key(model_key)
        if source_id and source_id not in valid_source_ids:
            add(
                "dangling_selected_model_source",
                f"modelWorkspace.selectedModels[{index}]",
                f"已选模型引用了不存在的来源 `{source_id}`。",
                repairable=True,
            )
        elif exact_model_is_stale(model_key):
            add(
                "stale_selected_model",
                f"modelWorkspace.selectedModels[{index}]",
                "已选模型已不在该来源当前保存的模型目录中。",
                repairable=True,
            )
    routing = settings.get("subagentRouting") if isinstance(settings.get("subagentRouting"), dict) else {}
    routes = routing.get("routes") if isinstance(routing.get("routes"), dict) else {}
    for level, route in routes.items():
        if not isinstance(route, dict):
            add("invalid_subagent_route", f"subagentRouting.routes.{level}", "子代理路由不是对象。", repairable=False)
            continue
        models = route.get("models") if isinstance(route.get("models"), list) else []
        efforts = route.get("efforts") if isinstance(route.get("efforts"), list) else []
        if len(efforts) > len(models):
            add(
                "orphan_subagent_effort",
                f"subagentRouting.routes.{level}.efforts",
                "子代理思考程度数量多于模型槽位。",
                repairable=True,
            )
        for index, model_key in enumerate(models):
            source_id = source_from_model_key(model_key)
            if source_id and source_id not in valid_source_ids:
                add(
                    "dangling_subagent_model_source",
                    f"subagentRouting.routes.{level}.models[{index}]",
                    f"子代理模型引用了不存在的来源 `{source_id}`。",
                    repairable=True,
                )
            elif exact_model_is_stale(model_key):
                add(
                    "stale_subagent_model",
                    f"subagentRouting.routes.{level}.models[{index}]",
                    "子代理模型已不在该来源当前保存的模型目录中。",
                    repairable=True,
                )

    repairable = sum(1 for item in issues if item["repairable"])
    return {
        "healthy": not issues,
        "issues": issues,
        "issueCount": len(issues),
        "repairableCount": repairable,
        "nonRepairableCount": len(issues) - repairable,
        "accounts": len(account_values),
        "providers": len(provider_values),
        "profiles": len(profile_values),
    }



def repair_settings_references() -> dict:
    """Remove only dangling IDs and keep a byte-exact rollback snapshot."""

    with _core.SETTINGS_LOCK, _core._settings_file_lock():
        settings = _core.load_settings()
        before = _core.settings_reference_integrity(settings)
        if before["nonRepairableCount"]:
            raise _core.ManagerError("设置中存在重复或无 ID 记录，不能自动推断应保留哪一条。")
        if not before["issueCount"]:
            return {"changed": False, "removed": 0, "before": before, "after": before}
        snapshot = _core._capture_file_bytes((_core.SETTINGS_FILE,))
        account_ids = {
            str(item.get("id"))
            for item in settings.get("accounts", [])
            if isinstance(item, dict) and item.get("id")
        }
        custom_provider_ids = {
            str(item.get("id"))
            for item in settings.get("providers", [])
            if isinstance(item, dict) and item.get("kind") == "custom" and item.get("id")
        }
        valid_provider_ids = {"openai", _core.AGGREGATE_PROVIDER_ID, *custom_provider_ids}
        valid_source_ids = {
            *(f"account:{item}" for item in account_ids),
            *(f"provider:{item}" for item in custom_provider_ids),
        }
        valid_group_ids = {
            str(item.get("id"))
            for item in settings.get("accountGroups", [])
            if isinstance(item, dict) and item.get("id")
        }
        valid_agent_names = {
            str(record.get("data", {}).get("name") or "").strip()
            for record in _core.discover_agents()
            if not record.get("error") and str(record.get("data", {}).get("name") or "").strip()
        }
        removed = 0
        for account in settings.get("accounts", []):
            if isinstance(account, dict) and account.get("groupId") not in valid_group_ids:
                account["groupId"] = "official"
                removed += 1
        for provider in settings.get("providers", []):
            if isinstance(provider, dict) and provider.get("groupId") not in valid_group_ids:
                provider["groupId"] = "official" if provider.get("kind") == "builtin" else "relay"
                removed += 1

        for route in settings.get("routes", {}).values():
            if not isinstance(route, dict) or not isinstance(route.get("agents", []), list):
                continue
            route_agents = [str(item or "").strip() for item in route.get("agents", [])]
            kept_agents = list(
                dict.fromkeys(
                    name for name in route_agents if name and name in valid_agent_names
                )
            )
            removed += len(route_agents) - len(kept_agents)
            route["agents"] = kept_agents
            if not kept_agents:
                route["enabled"] = False

        profiles = [
            item for item in settings.get("mainProfiles", [])
            if isinstance(item, dict) and str(item.get("provider") or "openai") in valid_provider_ids
        ]
        removed += len(settings.get("mainProfiles", [])) - len(profiles)
        if not profiles:
            config = _core.read_toml(_core.CONFIG_FILE)
            effort = str(config.get("model_reasoning_effort") or "").strip()
            profiles = [{
                "id": "current",
                "name": "当前主模型",
                "provider": "openai",
                "model": str(config.get("model") or ""),
                "effort": effort if effort in _core.VALID_EFFORTS else "",
            }]
        settings["mainProfiles"] = profiles
        profile_ids = {str(item.get("id")) for item in profiles if item.get("id")}
        if str(settings.get("activeMainProfileId") or "") not in profile_ids:
            fallback = next((item for item in profiles if item.get("provider") == "openai"), profiles[0])
            settings["activeMainProfileId"] = str(fallback.get("id") or "")
            removed += 1
        managed = [
            str(item) for item in settings.get("managedProviderIds", [])
            if str(item) in custom_provider_ids
        ]
        removed += len(settings.get("managedProviderIds", [])) - len(managed)
        settings["managedProviderIds"] = list(dict.fromkeys(managed))

        web2api = settings.setdefault("web2api", _core._default_web2api_settings())
        account_pool = [str(item) for item in web2api.get("accountIds", []) if str(item) in account_ids]
        provider_pool = [str(item) for item in web2api.get("providerIds", []) if str(item) in custom_provider_ids]
        removed += len(web2api.get("accountIds", [])) - len(account_pool)
        removed += len(web2api.get("providerIds", [])) - len(provider_pool)
        web2api["accountIds"] = list(dict.fromkeys(account_pool))
        web2api["providerIds"] = list(dict.fromkeys(provider_pool))
        order = [str(item) for item in web2api.get("sourceOrder", []) if str(item) in valid_source_ids]
        removed += len(web2api.get("sourceOrder", [])) - len(order)
        web2api["sourceOrder"] = list(dict.fromkeys(order))
        if str(web2api.get("activeAccountId") or "") not in account_ids:
            if web2api.get("activeAccountId"):
                removed += 1
            web2api["activeAccountId"] = None
        if web2api.get("activeForCodex") and not (
            web2api.get("activeAccountId") or web2api["accountIds"] or web2api["providerIds"]
        ):
            web2api["activeForCodex"] = False
            removed += 1
        account_pool_set = set(web2api["accountIds"])
        provider_pool_set = set(web2api["providerIds"])
        for account in settings.get("accounts", []):
            if isinstance(account, dict):
                account["proxyEnabled"] = str(account.get("id") or "") in account_pool_set
        for provider in settings.get("providers", []):
            if isinstance(provider, dict):
                provider["proxyEnabled"] = str(provider.get("id") or "") in provider_pool_set

        invalid_sources: set[str] = set()
        workspace = settings.setdefault("modelWorkspace", _core._default_model_workspace())
        active_source = str(workspace.get("activeSourceId") or "")
        if active_source and active_source not in valid_source_ids:
            invalid_sources.add(active_source)
        for key in [workspace.get("defaultModelKey"), *workspace.get("selectedModels", [])]:
            text = str(key or "")
            source = text.rsplit("::", 1)[0] if "::" in text else ""
            if source and source not in valid_source_ids:
                invalid_sources.add(source)
        for route in settings.get("subagentRouting", {}).get("routes", {}).values():
            if not isinstance(route, dict):
                continue
            for key in route.get("models", []) if isinstance(route.get("models"), list) else []:
                text = str(key or "")
                source = text.rsplit("::", 1)[0] if "::" in text else ""
                if source and source not in valid_source_ids:
                    invalid_sources.add(source)
            models = route.get("models", []) if isinstance(route.get("models"), list) else []
            efforts = route.get("efforts", []) if isinstance(route.get("efforts"), list) else []
            if len(efforts) > len(models):
                removed += len(efforts) - len(models)
                route["efforts"] = efforts[:len(models)]
        removed += _core._remove_model_source_references(settings, invalid_sources)
        explicit_model_keys: dict[str, set[str]] = {}
        for account in settings.get("accounts", []):
            if not isinstance(account, dict) or not account.get("id"):
                continue
            source_id = f"account:{account['id']}"
            models = {str(item).strip() for item in account.get("models", []) if str(item).strip()}
            if models:
                explicit_model_keys[source_id] = {_core._model_key(source_id, model_id) for model_id in models}
        for provider in settings.get("providers", []):
            if not isinstance(provider, dict) or provider.get("kind") != "custom" or not provider.get("id"):
                continue
            source_id = f"provider:{provider['id']}"
            models = {str(item).strip() for item in provider.get("models", []) if str(item).strip()}
            if models:
                explicit_model_keys[source_id] = {_core._model_key(source_id, model_id) for model_id in models}

        def valid_exact_model_key(value: object) -> bool:
            text = str(value or "")
            source_id = text.rsplit("::", 1)[0] if "::" in text else ""
            known = explicit_model_keys.get(source_id)
            return known is None or text in known

        if workspace.get("defaultModelKey") and not valid_exact_model_key(workspace["defaultModelKey"]):
            workspace["defaultModelKey"] = ""
            removed += 1
        selected_models = [
            str(item) for item in workspace.get("selectedModels", [])
            if valid_exact_model_key(item)
        ]
        removed += len(workspace.get("selectedModels", [])) - len(selected_models)
        workspace["selectedModels"] = selected_models
        for route in settings.get("subagentRouting", {}).get("routes", {}).values():
            if not isinstance(route, dict):
                continue
            models = route.get("models", []) if isinstance(route.get("models"), list) else []
            efforts = route.get("efforts", []) if isinstance(route.get("efforts"), list) else []
            kept = [
                (str(model), str(efforts[index] or "") if index < len(efforts) else "")
                for index, model in enumerate(models)
                if valid_exact_model_key(model)
            ]
            removed += len(models) - len(kept)
            route["models"] = [model for model, _effort in kept]
            route["efforts"] = [effort for _model, effort in kept]
        try:
            _core.save_settings(settings)
            after = _core.settings_reference_integrity(_core.load_settings())
            if after["issueCount"]:
                raise _core.ManagerError("修复后复检仍发现悬空引用。")
        except Exception as exc:
            rollback_errors = _core._restore_file_bytes(snapshot)
            if rollback_errors:
                raise _core.ManagerError(
                    f"设置引用修复失败：{exc}；回滚也未完成：{'；'.join(rollback_errors)}"
                ) from exc
            raise
        return {"changed": True, "removed": removed, "before": before, "after": after}



def _rename_model_source_references(settings: dict, original_source_id: str, source_id: str) -> None:
    original_prefix = f"{original_source_id}::"
    prefix = f"{source_id}::"

    def renamed(value: object) -> str:
        text = str(value or "")
        return prefix + text[len(original_prefix):] if text.startswith(original_prefix) else text

    workspace = settings.setdefault("modelWorkspace", _core._default_model_workspace())
    if str(workspace.get("activeSourceId") or "") == original_source_id:
        workspace["activeSourceId"] = source_id
    workspace["defaultModelKey"] = renamed(workspace.get("defaultModelKey"))
    workspace["selectedModels"] = list(
        dict.fromkeys(
            renamed(item)
            for item in workspace.get("selectedModels", [])
            if str(item).strip()
        )
    )

    routing = settings.setdefault("subagentRouting", _core._default_subagent_routing())
    for route in routing.get("routes", {}).values():
        if not isinstance(route, dict) or not isinstance(route.get("models"), list):
            continue
        route["models"] = [renamed(item) for item in route["models"]]



def remove_codex_account(account_id: str) -> None:
    with _core.SETTINGS_LOCK, _core.SECRETS_LOCK, _core._settings_file_lock():
        settings = _core.load_settings()
        if not any(item.get("id") == account_id for item in settings.get("accounts", [])):
            raise _core.ManagerError("账号不存在。")
        snapshot = _core._capture_file_bytes((_core.SETTINGS_FILE, _core.SECRETS_FILE))
        settings["accounts"] = [item for item in settings["accounts"] if item.get("id") != account_id]
        web2api = settings.setdefault("web2api", _core._default_web2api_settings())
        web2api["accountIds"] = [item for item in web2api.get("accountIds", []) if item != account_id]
        source_id = f"account:{account_id}"
        web2api["sourceOrder"] = [
            item for item in web2api.get("sourceOrder", []) if item != source_id
        ]
        if str(web2api.get("activeAccountId") or "") == account_id:
            web2api["activeAccountId"] = None
            web2api["activeForCodex"] = False
        _core._remove_model_source_references(settings, {source_id})
        secrets_payload = _core._secret_store()
        secrets_payload["accounts"].pop(account_id, None)
        try:
            _core.save_settings(settings)
            _core.atomic_write_json(_core.SECRETS_FILE, secrets_payload)
        except Exception as exc:
            rollback_errors = _core._restore_file_bytes(snapshot)
            if rollback_errors:
                raise _core.ManagerError(
                    f"删除账号失败：{exc}；回滚也未完成：{'；'.join(rollback_errors)}"
                ) from exc
            raise



def account_invalid_reason(account: dict) -> str | None:
    refresh_capable = bool(account.get("refreshCapable"))
    expires_at = _core._session_expiry(account.get("tokenExpiresAt"))
    if expires_at and not refresh_capable:
        try:
            parsed = _core.datetime.fromisoformat(expires_at)
            if parsed.astimezone(_core.timezone.utc) <= _core.datetime.now(_core.timezone.utc):
                return "Token 已过期"
        except ValueError:
            pass
    errors = account.get("refreshErrors") if isinstance(account.get("refreshErrors"), dict) else {}
    message = " ".join(str(item) for item in errors.values())
    if (
        account.get("refreshState") == "error"
        and _core._is_definitive_credential_error(message)
    ):
        return "认证已失效"
    return None



def delete_invalid_accounts(group_id: str = "all") -> dict:
    with _core.SETTINGS_LOCK, _core.SECRETS_LOCK, _core._settings_file_lock():
        settings = _core.load_settings()
        if group_id != "all":
            _core._account_group(settings, group_id)
        targets = [
            account
            for account in settings.get("accounts", [])
            if (group_id == "all" or account.get("groupId") == group_id) and _core.account_invalid_reason(account)
        ]
        if not targets:
            return {"deleted": 0, "groupId": group_id, "labels": []}
        snapshot = _core._capture_file_bytes((_core.SETTINGS_FILE, _core.SECRETS_FILE))
        target_ids = {str(item.get("id")) for item in targets}
        settings["accounts"] = [item for item in settings.get("accounts", []) if item.get("id") not in target_ids]
        web2api = settings.setdefault("web2api", _core._default_web2api_settings())
        web2api["accountIds"] = [item for item in web2api.get("accountIds", []) if item not in target_ids]
        web2api["sourceOrder"] = [
            item
            for item in web2api.get("sourceOrder", [])
            if not (str(item).startswith("account:") and str(item)[8:] in target_ids)
        ]
        if str(web2api.get("activeAccountId") or "") in target_ids:
            web2api["activeAccountId"] = None
            web2api["activeForCodex"] = False
        _core._remove_model_source_references(settings, {f"account:{item}" for item in target_ids})
        secrets_payload = _core._secret_store()
        for account_id in target_ids:
            secrets_payload["accounts"].pop(account_id, None)
        try:
            _core.save_settings(settings)
            _core.atomic_write_json(_core.SECRETS_FILE, secrets_payload)
        except Exception as exc:
            rollback_errors = _core._restore_file_bytes(snapshot)
            if rollback_errors:
                raise _core.ManagerError(
                    f"删除失效账号失败：{exc}；回滚也未完成：{'；'.join(rollback_errors)}"
                ) from exc
            raise
        return {
            "deleted": len(targets),
            "groupId": group_id,
            "labels": [str(item.get("label") or item.get("email") or item.get("id")) for item in targets],
        }



def export_codex_account(account_id: str) -> dict:
    with _core._account_refresh_lock_for(account_id):
        return _core._export_codex_account_locked(account_id)



def _export_codex_account_locked(account_id: str) -> dict:
    settings = _core.load_settings()
    account = next((item for item in settings.get("accounts", []) if item.get("id") == account_id), None)
    if not account:
        raise _core.ManagerError("账号不存在。")
    snapshot = _core._load_account_snapshot(account_id)
    files = _core._decode_snapshot_files(snapshot)
    official_oauth = account.get("authMode") == "chatgpt" and account.get("sourceType") == "codex_auth"
    if official_oauth:
        live_path = _core.CODEX_HOME / "auth.json"
        try:
            if live_path.is_file() and live_path.stat().st_size <= _core.MAX_IMPORT_DOCUMENT_BYTES:
                live_auth = live_path.read_bytes()
                live_identity = _core._identity_from_auth_bytes(live_auth)
                if _core._account_matches_identity(account, live_identity) and _core._codex_oauth_auth_is_newer(live_auth, files.get("auth.json") or b""):
                    # Export one complete latest bundle; never splice a refresh
                    # token from a different login or transfer machine cap_sid.
                    files["auth.json"] = live_auth
        except (OSError, _core.ManagerError):
            pass
    try:
        auth_json = _core.json.loads((files.get("auth.json") or b"").decode("utf-8"))
    except (UnicodeDecodeError, _core.json.JSONDecodeError) as exc:
        raise _core.ManagerError("账号快照中的 auth.json 无法导出。") from exc
    group = next((item for item in settings.get("accountGroups", []) if item.get("id") == account.get("groupId")), {})
    document = {
        "format": "codex-agent-manager-account",
        "version": 1,
        "exportedAt": _core.now_iso(),
        "containsSecrets": True,
        "label": account.get("label"),
        "group": {"id": account.get("groupId"), "name": group.get("name")},
        "sourceType": account.get("sourceType"),
        "importedAt": account.get("importedAt") or account.get("createdAt"),
        "subscriptionExpiresAt": account.get("subscriptionExpiresAt"),
        # Credentials are portable by themselves, but carrying the last known
        # model catalog prevents a newly imported account from disappearing on
        # an offline/rate-limited machine before its first metadata refresh.
        "models": list(account.get("models", [])),
        "modelsLastCheckedAt": account.get("modelsLastCheckedAt"),
        "modelsRefreshedAt": account.get("modelsRefreshedAt"),
        "authJson": auth_json,
        "capSidBase64": _core.base64.b64encode(files["cap_sid"]).decode("ascii") if files.get("cap_sid") else None,
    }
    if official_oauth:
        from agent_manager.accounts.portability import build_portable_account_export, PortableAccountError
        try:
            return build_portable_account_export(auth_json, document)
        except PortableAccountError as exc:
            raise _core.ManagerError(str(exc)) from exc
    return document



def _restore_auth_files(files: dict[str, bytes | None]) -> None:
    for name in _core.AUTH_FILES:
        path = _core.CODEX_HOME / name
        content = files.get(name)
        if content is None:
            path.unlink(missing_ok=True)
        else:
            _core.atomic_write_bytes(path, content)



def _live_auth_files_match(expected: dict[str, bytes | None]) -> bool:
    for name in _core.AUTH_FILES:
        path = _core.CODEX_HOME / name
        content = expected.get(name)
        if content is None:
            if path.exists():
                return False
        elif not path.is_file() or path.read_bytes() != content:
            return False
    return True



@_core.contextmanager
def _exclusive_switch_operation(operation: str, target: str):
    acquired = _core.SWITCH_OPERATION_LOCK.acquire(blocking=False)
    if not acquired:
        raise _core.ManagerError("已有账号或中转站切换正在进行，请等待完成后重试。")
    _core.SETTINGS_LOCK.acquire()
    try:
        with _core._settings_file_lock():
            yield {"operation": str(operation), "target": str(target)}
    finally:
        _core.SETTINGS_LOCK.release()
        _core.SWITCH_OPERATION_LOCK.release()



def _switch_snapshot_paths() -> list[_core.Path]:
    paths = [
        _core.SETTINGS_FILE,
        _core.SECRETS_FILE,
        _core.ACCOUNT_ACTIVATION_HISTORY_FILE,
        _core.RUNTIME_OVERLAY_FILE,
        _core.RUNTIME_RESTORE_STATUS_FILE,
        *(_core.CODEX_HOME / name for name in _core.AUTH_FILES),
        *(path for path, _kind in _core._runtime_overlay_targets()),
    ]
    unique: list[_core.Path] = []
    seen: set[_core.Path] = set()
    for path in paths:
        normalized = _core.Path(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique



def _switch_environment_names(settings: dict) -> list[str]:
    names = [_core.AGGREGATE_ENV_KEY]
    for provider in settings.get("providers", []):
        if isinstance(provider, dict) and provider.get("kind") == "custom":
            names.append(str(provider.get("envKey") or ""))
    config = _core.read_toml(_core.CONFIG_FILE)
    providers = config.get("model_providers", {})
    if isinstance(providers, dict):
        for provider in providers.values():
            if isinstance(provider, dict):
                names.append(str(provider.get("env_key") or ""))
    safe = []
    for name in names:
        candidate = str(name or "").strip()
        if not candidate or candidate.upper() == "CODEX_CLI_PATH":
            continue
        try:
            _core._validate_provider_env_key(
                candidate,
                allow_internal=candidate.upper() == _core.AGGREGATE_ENV_KEY,
            )
        except _core.ManagerError:
            continue
        safe.append(candidate)
    return list(dict.fromkeys(safe))



def _capture_switch_transaction_snapshot(settings: dict | None = None) -> dict:
    settings = settings or _core.load_settings()
    try:
        files = {
            path: path.read_bytes() if path.is_file() else None
            for path in _core._switch_snapshot_paths()
        }
        environment = {
            name: _core._read_user_environment(name)
            for name in _core._switch_environment_names(settings)
        }
    except OSError as exc:
        raise _core.ManagerError(f"无法完整读取切换前状态：{exc}") from exc
    return {
        "files": files,
        "environment": environment,
        "settingsDocument": settings,
        "settingsValue": _core._json_clone(settings),
    }



def _reset_switch_caches() -> None:
    with _core.AUTH_STATE_CACHE_LOCK:
        _core.AUTH_STATE_CACHE.update({"key": None, "at": 0.0, "value": None})
    with _core.MODEL_CACHE_LOCK:
        _core.MODEL_CACHE.update({"at": 0.0, "models": None, "raw": None})



def _restore_switch_transaction_snapshot(
    snapshot: dict,
    *,
    process_state_checked: bool = False,
) -> list[str]:
    if not process_state_checked:
        try:
            _core._require_codex_process_scan_known()
        except _core.ManagerError as exc:
            return [f"未恢复切换快照：{str(exc)[:240]}"]
    errors: list[str] = []
    files = snapshot.get("files", {}) if isinstance(snapshot, dict) else {}
    overlay_content = files.get(_core.RUNTIME_OVERLAY_FILE)
    for path, content in files.items():
        if path == _core.RUNTIME_OVERLAY_FILE:
            continue
        try:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                _core.atomic_write_bytes(path, content)
        except OSError as exc:
            errors.append(f"恢复 {path.name}：{str(exc)[:180]}")
    for name, value in snapshot.get("environment", {}).items():
        try:
            if value is None:
                _core._remove_user_environment(name)
            else:
                _core._sync_user_environment(name, value)
        except Exception as exc:
            errors.append(f"恢复环境变量 {name}：{str(exc)[:180]}")
    try:
        if overlay_content is None:
            _core.RUNTIME_OVERLAY_FILE.unlink(missing_ok=True)
        else:
            _core.atomic_write_bytes(_core.RUNTIME_OVERLAY_FILE, overlay_content)
    except OSError as exc:
        errors.append(f"恢复 {_core.RUNTIME_OVERLAY_FILE.name}：{str(exc)[:180]}")
    for path, expected in files.items():
        try:
            actual = path.read_bytes() if path.is_file() else None
        except OSError as exc:
            errors.append(f"回验 {path.name}：{str(exc)[:180]}")
            continue
        if actual != expected:
            errors.append(f"回验 {path.name}：内容未恢复")
    for name, expected in snapshot.get("environment", {}).items():
        try:
            actual = _core._read_user_environment(name)
        except Exception as exc:
            errors.append(f"回验环境变量 {name}：{str(exc)[:180]}")
            continue
        if actual != expected:
            errors.append(f"回验环境变量 {name}：值未恢复")
    settings_document = snapshot.get("settingsDocument")
    settings_value = snapshot.get("settingsValue")
    if isinstance(settings_document, dict) and isinstance(settings_value, dict):
        settings_document.clear()
        settings_document.update(_core._json_clone(settings_value))
        if isinstance(settings_document, _core.SettingsDocument):
            settings_document._baseline = _core._json_clone(settings_value)
    _core._reset_switch_caches()
    return errors



def _session_database_candidates() -> list[_core.Path]:
    # Codex currently uses state_5.sqlite. Restrict repairs to the two official
    # locations so unrelated/legacy state_*.sqlite files are never rewritten.
    candidates = [_core.CODEX_HOME / "sqlite" / "state_5.sqlite", _core.CODEX_HOME / "state_5.sqlite"]
    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved not in seen and candidate.is_file():
            seen.add(resolved)
            unique.append(candidate)
    return unique



def session_storage_health(max_rollouts: int = 500) -> dict:
    """Run a bounded read-only integrity check over Codex session storage."""

    try:
        rollout_limit = max(1, min(int(max_rollouts), 2_000))
    except (TypeError, ValueError):
        rollout_limit = 500
    issues: list[dict] = []
    databases = _core._session_database_candidates()
    if len(databases) > 1:
        issues.append(
            {
                "kind": "multiple_databases",
                "severity": "warning",
                "detail": "同时发现两个官方 state_5.sqlite 位置；Codex 版本切换后可能保留了旧索引。",
            }
        )
    checked_databases = 0
    thread_count = 0
    for database in databases:
        connection = None
        try:
            uri = f"file:{database.resolve().as_posix()}?mode=ro"
            connection = _core.sqlite3.connect(uri, uri=True, timeout=1)
            connection.execute("PRAGMA busy_timeout = 1000")
            check = connection.execute("PRAGMA quick_check(1)").fetchone()
            if not check or str(check[0]).casefold() != "ok":
                issues.append(
                    {
                        "kind": "database_corrupt",
                        "severity": "error",
                        "path": str(database),
                        "detail": f"会话数据库完整性检查失败：{str(check[0] if check else '无结果')[:180]}",
                    }
                )
                continue
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(threads)").fetchall()
            }
            if not columns:
                issues.append(
                    {
                        "kind": "threads_table_missing",
                        "severity": "error",
                        "path": str(database),
                        "detail": "会话数据库缺少 threads 表。",
                    }
                )
                continue
            thread_count += int(connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0])
            checked_databases += 1
        except _core.sqlite3.OperationalError as exc:
            message = str(exc).casefold()
            issues.append(
                {
                    "kind": "database_busy" if "locked" in message or "busy" in message else "database_unreadable",
                    "severity": "warning" if "locked" in message or "busy" in message else "error",
                    "path": str(database),
                    "detail": (
                        "Codex 正在使用会话数据库，暂时无法完成完整检查。"
                        if "locked" in message or "busy" in message
                        else f"无法只读检查会话数据库：{str(exc)[:180]}"
                    ),
                }
            )
        except (OSError, _core.sqlite3.Error, TypeError, ValueError) as exc:
            issues.append(
                {
                    "kind": "database_unreadable",
                    "severity": "error",
                    "path": str(database),
                    "detail": f"无法只读检查会话数据库：{str(exc)[:180]}",
                }
            )
        finally:
            if connection is not None:
                connection.close()

    rollout_count = 0
    invalid_rollouts = 0
    truncated = False
    for bucket in ("sessions", "archived_sessions"):
        folder = _core.CODEX_HOME / bucket
        if not folder.is_dir():
            continue
        for current, directories, files in _core.os.walk(folder, followlinks=False):
            directories[:] = [
                name for name in directories if not (_core.Path(current) / name).is_symlink()
            ]
            for name in files:
                if not name.casefold().endswith(".jsonl"):
                    continue
                if rollout_count >= rollout_limit:
                    truncated = True
                    break
                rollout_count += 1
                path = _core.Path(current) / name
                try:
                    with path.open("rb") as stream:
                        first = stream.readline(1_048_577)
                    if len(first) > 1_048_576 and not first.endswith((b"\n", b"\r")):
                        raise ValueError("首行超过 1 MB")
                    record = _core.json.loads(first.decode("utf-8"))
                    if not isinstance(record, dict) or record.get("type") != "session_meta":
                        raise ValueError("首行不是 session_meta")
                except (OSError, UnicodeDecodeError, _core.json.JSONDecodeError, ValueError):
                    invalid_rollouts += 1
            if truncated:
                break
        if truncated:
            break
    if invalid_rollouts:
        issues.append(
            {
                "kind": "invalid_rollout",
                "severity": "error",
                "detail": f"抽查发现 {invalid_rollouts} 个会话 JSONL 缺少有效 session_meta。",
            }
        )
    severity = "error" if any(item["severity"] == "error" for item in issues) else "warning" if issues else "ok"
    return {
        "healthy": severity == "ok",
        "status": severity,
        "databaseCount": len(databases),
        "checkedDatabases": checked_databases,
        "threadCount": thread_count,
        "rolloutCount": rollout_count,
        "invalidRolloutCount": invalid_rollouts,
        "rolloutScanTruncated": truncated,
        "issues": issues,
    }



def _safe_rollout_path(value: _core.Any) -> tuple[_core.Path | None, _core.Path | None, str | None]:
    raw = str(value or "").strip()
    if not raw:
        return None, None, None
    candidate = _core.Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = _core.CODEX_HOME / candidate
    try:
        codex_root = _core.CODEX_HOME.expanduser().resolve()
        resolved = candidate.resolve()
        relative = resolved.relative_to(codex_root)
    except (OSError, ValueError):
        return None, None, "rollout_path 超出 CODEX_HOME，已跳过。"
    if not resolved.is_file():
        return None, None, f"会话文件不存在：{relative}"
    return resolved, relative, None



def _rewrite_rollout_provider(
    rollout: _core.Path,
    relative: _core.Path,
    target_provider: str,
    backup_root: _core.Path,
) -> tuple[bool, str | None, str | None]:
    temporary_name: str | None = None
    try:
        with rollout.open("rb") as source:
            first_line = source.readline(1_048_577)
            if len(first_line) > 1_048_576 and not first_line.endswith((b"\n", b"\r")):
                return False, None, f"{relative} 首行超过 1 MB，已跳过。"
            newline = b"\r\n" if first_line.endswith(b"\r\n") else b"\n" if first_line.endswith(b"\n") else b""
            raw_json = first_line[: -len(newline)] if newline else first_line
            record = _core.json.loads(raw_json.decode("utf-8"))
            payload = record.get("payload") if isinstance(record, dict) else None
            if not isinstance(record, dict) or record.get("type") != "session_meta" or not isinstance(payload, dict):
                return False, None, f"{relative} 首行不是 session_meta，已跳过。"
            if payload.get("model_provider") == target_provider:
                return False, None, None
            payload["model_provider"] = target_provider
            replacement = _core.json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + newline

            backup_path = backup_root / "rollouts" / relative
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            _core.shutil.copy2(rollout, backup_path)

            descriptor, temporary_name = _core.tempfile.mkstemp(
                prefix=f".{rollout.name}.", suffix=".tmp", dir=rollout.parent
            )
            with _core.os.fdopen(descriptor, "wb") as destination:
                destination.write(replacement)
                _core.shutil.copyfileobj(source, destination, length=1024 * 1024)
                destination.flush()
                _core.os.fsync(destination.fileno())

        # Windows refuses to replace an open file. Keep the source open while
        # streaming the remainder, then close it before the atomic replacement.
        _core.shutil.copymode(rollout, temporary_name)
        _core.os.replace(temporary_name, rollout)
        temporary_name = None
        return True, str(backup_path), None
    except (OSError, UnicodeDecodeError, _core.json.JSONDecodeError) as exc:
        return False, None, f"{relative}：{str(exc)[:240]}"
    finally:
        if temporary_name and _core.os.path.exists(temporary_name):
            _core.os.unlink(temporary_name)



def repair_codex_session_visibility(target_provider: str = "openai") -> dict:
    databases = _core._session_database_candidates()
    if not databases:
        return {
            "databases": 0,
            "rowsChanged": 0,
            "rolloutFilesChanged": 0,
            "backups": [],
            "rolloutBackups": [],
            "warnings": [],
            "index": None,
        }
    stamp = _core.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_root = _core.BACKUPS_DIR / f"session-switch-{stamp}"
    backup_root.mkdir(parents=True, exist_ok=False)
    rows_changed = 0
    rollout_files_changed = 0
    backups = []
    rollout_backups = []
    warnings = []
    processed_rollouts: set[Path] = set()
    for database in databases:
        location = "sqlite" if database.parent.name == "sqlite" else "root"
        backup_path = backup_root / f"{location}-{database.name}"
        source = None
        destination = None
        try:
            source = _core.sqlite3.connect(database, timeout=2)
            source.execute("PRAGMA busy_timeout = 2000")
            destination = _core.sqlite3.connect(backup_path)
            source.backup(destination)
            destination.close()
            destination = None
            backups.append(str(backup_path))
            columns = {str(row[1]) for row in source.execute("PRAGMA table_info(threads)").fetchall()}
            if not columns:
                warnings.append(f"{database.name} 没有 threads 表。")
                continue
            if "rollout_path" in columns:
                for (raw_rollout,) in source.execute(
                    "SELECT DISTINCT rollout_path FROM threads "
                    "WHERE rollout_path IS NOT NULL AND TRIM(rollout_path) <> ''"
                ).fetchall():
                    rollout, relative, warning = _core._safe_rollout_path(raw_rollout)
                    if warning:
                        warnings.append(warning)
                    if not rollout or not relative or rollout in processed_rollouts:
                        continue
                    processed_rollouts.add(rollout)
                    changed, rollout_backup, rollout_warning = _core._rewrite_rollout_provider(
                        rollout,
                        relative,
                        target_provider,
                        backup_root,
                    )
                    if changed:
                        rollout_files_changed += 1
                    if rollout_backup:
                        rollout_backups.append(rollout_backup)
                    if rollout_warning:
                        warnings.append(rollout_warning)
            before = source.total_changes
            source.execute("BEGIN IMMEDIATE")
            if "model_provider" in columns:
                source.execute(
                    "UPDATE threads SET model_provider = ? WHERE model_provider IS NULL OR model_provider <> ?",
                    (target_provider, target_provider),
                )
            if "has_user_event" in columns and "first_user_message" in columns:
                source.execute(
                    "UPDATE threads SET has_user_event = 1 "
                    "WHERE COALESCE(has_user_event, 0) = 0 AND LENGTH(TRIM(COALESCE(first_user_message, ''))) > 0"
                )
            if "thread_source" in columns:
                source.execute(
                    "UPDATE threads SET thread_source = 'user' "
                    "WHERE thread_source IS NULL OR TRIM(thread_source) = ''"
                )
            source.commit()
            rows_changed += source.total_changes - before
        except _core.sqlite3.Error as exc:
            if source:
                try:
                    source.rollback()
                except _core.sqlite3.Error:
                    pass
            warnings.append(f"{database.name}：{str(exc)[:240]}")
        finally:
            if destination:
                destination.close()
            if source:
                source.close()
    return {
        "databases": len(databases),
        "rowsChanged": rows_changed,
        "rolloutFilesChanged": rollout_files_changed,
        "backups": backups,
        "rolloutBackups": rollout_backups,
        "warnings": warnings,
        # Rebuilding the app-server index during every account switch can create
        # duplicate side effects and is unnecessary once DB + rollout agree.
        "index": None,
    }



def auto_sync_sessions_after_switch(target_provider: str = "openai") -> dict:
    """Record a provider switch without rewriting Codex session storage.

    Session rows and rollout metadata are historical facts. Re-labeling every
    session with the newly selected provider made intact threads disappear from
    Codex's filtered views. History mirroring is likewise an explicit user
    action, not part of an account/provider transaction.
    """
    settings = _core.load_settings()
    config = settings.setdefault("sessionSync", _core._default_session_sync_settings())
    if not config.get("enabled", True):
        return {
            "enabled": False,
            "preserved": True,
            "targetProvider": str(target_provider or "openai"),
            "visibility": None,
            "history": None,
            "warnings": [],
        }
    summary = {
        "enabled": True,
        "preserved": True,
        "targetProvider": str(target_provider or "openai"),
        "visibility": {"changed": False, "reason": "switch_preserves_session_metadata"},
        "history": {"changed": False, "reason": "manual_sync_only"},
        "warnings": [],
        "syncedAt": _core.now_iso(),
    }
    session_config = settings.setdefault("sessionSync", _core._default_session_sync_settings())
    session_config["lastSyncedAt"] = summary["syncedAt"]
    session_config["lastSummary"] = summary
    _core.save_settings(settings)
    return summary

