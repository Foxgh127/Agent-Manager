"""Catalog services."""
from __future__ import annotations
from agent_manager import core as _core


def codex_version() -> str:
    with _core.MODEL_CACHE_LOCK:
        cached = _core.CODEX_VERSION_CACHE.get("value")
        if isinstance(cached, str) and _core.time.monotonic() - float(_core.CODEX_VERSION_CACHE.get("at", 0)) < 3_600:
            return cached
    try:
        result = _core.run_codex_capture(["--version"], timeout=10)
        value = result.stdout.strip() if result.returncode == 0 else "Codex unavailable"
    except Exception:
        value = "Codex unavailable"
    with _core.MODEL_CACHE_LOCK:
        _core.CODEX_VERSION_CACHE.update({"at": _core.time.monotonic(), "value": value})
    return value



def invalidate_codex_version_cache() -> None:
    """Force the next public-state read to observe a newly installed CLI."""
    with _core.MODEL_CACHE_LOCK:
        _core.CODEX_VERSION_CACHE.update({"at": 0.0, "value": None})
        _core.MODEL_CACHE.update({"at": 0.0, "raw": None, "models": None})



def _codex_supports_mcp_optional_startup_grace() -> bool:
    match = _core.re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)", _core.codex_version())
    if not match:
        return False
    return tuple(int(part) for part in match.groups()) >= (0, 151, 0)



def _raw_local_model_catalog(force: bool = False) -> dict:
    # Never use `debug models` without --bundled: it honors our own generated
    # model_catalog_json and can indefinitely recycle old selections as truth.
    try:
        stat = _core.MODELS_CACHE_FILE.stat()
        cache_key = (str(_core.MODELS_CACHE_FILE), stat.st_mtime_ns, stat.st_size)
    except OSError:
        cache_key = (str(_core.MODELS_CACHE_FILE), None, None)
    with _core.MODEL_CACHE_LOCK:
        cached = _core.MODEL_CACHE.get("raw")
        if (
            not force
            and isinstance(cached, dict)
            and _core.MODEL_CACHE.get("fileKey") == cache_key
            and _core.time.monotonic() - float(_core.MODEL_CACHE["at"]) < _core.MODEL_CACHE_TTL_SECONDS
        ):
            return _core.json.loads(_core.json.dumps(cached))
    payload = None
    try:
        with _core.MODELS_CACHE_FILE.open("rb") as cache_file:
            raw = cache_file.read(_core.CODEX_CONFIG_MAX_BYTES + 1)
        if len(raw) <= _core.CODEX_CONFIG_MAX_BYTES:
            candidate = _core.json.loads(raw.decode("utf-8-sig"))
            if (isinstance(candidate, dict) and isinstance(candidate.get("models"), list)
                    and candidate["models"]
                    and not _core._timestamp_is_stale(candidate.get("fetched_at"), _core.MODEL_CACHE_TTL_SECONDS)
                    and candidate.get("client_version") == _core._codex_client_version()):
                payload = {"models": candidate["models"]}
    except (OSError, ValueError):
        pass
    if payload is None:
        completed = _core.run_codex_capture(["debug", "models", "--bundled"], timeout=30)
        if completed.returncode != 0:
            raise _core.ManagerError(_core._redact_sensitive_text(completed.stderr, limit=320) or "无法读取 Codex 原生模型目录，请更新 Codex 运行时。")
        try:
            payload = _core.json.loads(completed.stdout)
        except _core.json.JSONDecodeError as exc:
            raise _core.ManagerError("Codex 返回了无效模型目录。") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise _core.ManagerError("Codex 返回的模型目录格式无效。")
    with _core.MODEL_CACHE_LOCK:
        _core.MODEL_CACHE["at"] = _core.time.monotonic()
        _core.MODEL_CACHE["raw"] = _core.json.loads(_core.json.dumps(payload))
        _core.MODEL_CACHE["models"] = None
        _core.MODEL_CACHE["fileKey"] = cache_key
    return payload



def local_model_catalog(force: bool = False) -> list[dict]:
    # The raw cache checks the native file fingerprint even within its TTL.
    payload = _core._raw_local_model_catalog(force=force)
    models = []
    for item in payload.get("models", []):
        if not isinstance(item, dict) or not item.get("slug") or not _core._model_is_picker_visible(item):
            continue
        models.append(
            {
                "id": item["slug"],
                "name": item.get("display_name") or item["slug"],
                "description": item.get("description", ""),
                "efforts": [level.get("effort") for level in (item.get("supported_reasoning_levels") or []) if isinstance(level, dict) and level.get("effort") in _core.VALID_EFFORTS],
                "defaultEffort": item.get("default_reasoning_level"),
                "priority": item.get("priority") if isinstance(item.get("priority"), (int, float)) else 999,
            }
        )
    models = sorted(models, key=lambda item: (item["priority"], item["name"]))
    with _core.MODEL_CACHE_LOCK:
        _core.MODEL_CACHE["models"] = _core.json.loads(_core.json.dumps(models))
    return models



def _reasoning_capabilities(local_models: list[dict] | None = None) -> dict[str, dict]:
    """Return model-specific reasoning levels without making them mandatory.

    Imported relays can expose model IDs that are absent from Codex's local
    catalog. Unknown models remain configurable, but callers must omit an
    effort until that model's own source advertises supported levels.
    """
    if local_models is None:
        try:
            local_models = _core.local_model_catalog()
        except Exception:
            local_models = []
    capabilities: dict[str, dict] = {}
    for item in local_models:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            continue
        efforts = [
            str(value)
            for value in item.get("efforts", [])
            if str(value) in _core.VALID_EFFORTS
        ]
        default_effort = str(item.get("defaultEffort") or "").strip()
        capabilities[model_id] = {
            "efforts": list(dict.fromkeys(efforts)),
            "defaultEffort": default_effort if default_effort in _core.VALID_EFFORTS else "",
            "reasoningKnown": True,
            "reasoningSupported": bool(efforts),
        }
    for model_id, fallback in _core.KNOWN_REMOTE_REASONING_CAPABILITIES.items():
        capabilities.setdefault(model_id, _core.json.loads(_core.json.dumps(fallback)))
    return capabilities



def _codex_compatible_reasoning_efforts(values: list[str], client_version: str | None = None) -> list[str]:
    version = client_version if client_version is not None else _core.codex_version()
    match = _core.re.search(r"(\d+)\.(\d+)\.(\d+)", version or "")
    extended = bool(match and tuple(map(int, match.groups())) >= (0, 144, 0))
    return [value for value in dict.fromkeys(values) if value in _core.VALID_EFFORTS and (extended or value not in {"max", "ultra"})]



def _effective_provider_model_capabilities(provider: dict, local_models: list[dict] | None = None) -> dict[str, dict]:
    """Source declarations win; missing GPT effort ranges use native compatibility.

    Compatibility metadata controls the Codex picker, not a claim that a relay
    implements every optional tool/context feature of an identically named model.
    """
    model_ids = [str(item) for item in provider.get("models", [])]
    advertised = _core._normalize_provider_model_capabilities(provider.get("modelCapabilities"), model_ids)
    overrides = _core._normalize_provider_model_capabilities(provider.get("modelReasoningOverrides"), model_ids)
    native = _core._reasoning_capabilities(local_models)
    result = {}
    for model_id in model_ids:
        capability = dict(advertised.get(model_id) or {})
        reasoning_source = "provider" if capability.get("reasoningKnown") else "unknown"
        if model_id in overrides:
            capability.update(overrides[model_id])
            reasoning_source = "custom"
        elif not capability.get("reasoningKnown"):
            native_id = model_id
            if native_id not in native and model_id.count("/") == 1:
                namespace, upstream = model_id.split("/", 1)
                if namespace and upstream.startswith("gpt-"):
                    native_id = upstream
            fallback = native.get(native_id) if native_id.startswith("gpt-") else None
            if isinstance(fallback, dict) and fallback.get("efforts"):
                default = str(capability.get("defaultEffort") or fallback.get("defaultEffort") or "")
                capability.update({"reasoningKnown": True, "reasoningSupported": True,
                                   "efforts": list(fallback["efforts"]), "defaultEffort": default})
                reasoning_source = "codex_compatibility"
        if capability:
            capability["efforts"] = _core._codex_compatible_reasoning_efforts(capability.get("efforts", []))
            if capability.get("defaultEffort") not in capability["efforts"] and capability.get("reasoningKnown"):
                capability["defaultEffort"] = ""
            capability["reasoningSource"] = reasoning_source
            result[model_id] = capability
    return result



def _model_reasoning_metadata(model_id: str, capabilities: dict[str, dict]) -> dict:
    capability = capabilities.get(str(model_id))
    reasoning_known = bool(
        isinstance(capability, dict)
        and (
            capability.get("reasoningKnown") is True
            if "reasoningKnown" in capability else "efforts" in capability
        )
    )
    metadata: dict[str, Any] = {
        "efforts": _core._codex_compatible_reasoning_efforts(list(capability.get("efforts", []))) if reasoning_known else [],
        "defaultEffort": str(capability.get("defaultEffort") or "") if reasoning_known else "",
        "reasoningKnown": reasoning_known,
        "reasoningSupported": (
            bool(capability.get("reasoningSupported"))
            if reasoning_known and "reasoningSupported" in capability
            else bool(capability.get("efforts"))
            if reasoning_known
            else None
        ),
    }
    if isinstance(capability, dict) and capability.get("reasoningSource"):
        metadata["reasoningSource"] = capability["reasoningSource"]
    if not isinstance(capability, dict):
        return metadata
    for key in ("contextWindow", "maxContextWindow", "effectiveContextWindowPercent"):
        value = _core._catalog_positive_integer(
            capability.get(key),
            maximum=100 if key == "effectiveContextWindowPercent" else 1_000_000_000,
        )
        if value is not None:
            metadata[key] = value
    for key in ("supportsPersonality", "supportsVerbosity"):
        if isinstance(capability.get(key), bool):
            metadata[key] = capability[key]
    default_verbosity = str(capability.get("defaultVerbosity") or "").strip().casefold()
    if default_verbosity in _core.VALID_MODEL_VERBOSITIES and default_verbosity:
        metadata["defaultVerbosity"] = default_verbosity
    return metadata



def _model_key(source_id: str, model_id: str) -> str:
    return f"{source_id}::{model_id}"



def _model_sort_key(model_id: str) -> tuple[int, str]:
    value = model_id.casefold()
    preferred = (
        "gpt-6-astra",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex-spark",
    )
    if value == "codex-auto-review":
        return (999, value)
    try:
        return (preferred.index(value), value)
    except ValueError:
        return (100, value)



def model_sources(settings: dict | None = None, local_models: list[dict] | None = None) -> list[dict]:
    settings = settings or _core.load_settings()
    accounts = [item for item in settings.get("accounts", []) if isinstance(item, dict)]
    needs_local_fallback = any(
        _core._account_codex_compatible(account)
        and not [str(item).strip() for item in account.get("models", []) if str(item).strip()]
        for account in accounts
    )
    if local_models is None and needs_local_fallback:
        try:
            # Cockpit/auth.json exports normally contain credentials, not a
            # model catalog.  Reuse Codex's own local catalog so a valid,
            # already-selected account does not disappear merely because its
            # first remote metadata refresh was rate-limited or offline.
            local_models = _core.local_model_catalog()
        except Exception:
            local_models = []
    capabilities = (
        _core._reasoning_capabilities(local_models)
        if local_models is not None
        else _core.json.loads(_core.json.dumps(_core.KNOWN_REMOTE_REASONING_CAPABILITIES))
    )
    fallback_model_ids = [
        str(item.get("id") or "").strip()
        for item in (local_models or [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    groups = {str(item.get("id")): item for item in settings.get("accountGroups", []) if isinstance(item, dict)}
    live_auth = _core.current_auth_state(settings)
    active_auth_id = live_auth.get("activeAccountId")
    workspace = settings.get("modelWorkspace", {})
    active_source_id = str(workspace.get("activeSourceId") or "")
    # Current usage comes from Codex's actual provider/credentials. A saved
    # Manager selection must not hide a switch made by Cockpit or another tool.
    live_selection = live_auth.get("liveSelection")
    if isinstance(live_selection, dict) and live_selection.get("configured"):
        active_source_id = str(live_selection.get("sourceId") or "")
    elif (
        active_auth_id
        and workspace.get("mode") == "independent"
        and not active_source_id.startswith("provider:")
    ):
        active_source_id = f"account:{active_auth_id}"
    elif not active_source_id and active_auth_id:
        active_source_id = f"account:{active_auth_id}"
    sources: list[dict] = []
    for account in accounts:
        account_id = str(account.get("id") or "")
        if not account_id:
            continue
        source_id = f"account:{account_id}"
        codex_compatible = _core._account_codex_compatible(account)
        stored_models = (
            [str(item).strip() for item in account.get("models", []) if str(item).strip()]
            if codex_compatible
            else []
        )
        models = stored_models or (fallback_model_ids if codex_compatible else [])
        invalid_reason = _core.account_invalid_reason(account)
        refresh_errors = account.get("refreshErrors") if isinstance(account.get("refreshErrors"), dict) else {}
        refresh_error = next((str(value) for value in refresh_errors.values() if str(value).strip()), "")
        account_capabilities = dict(capabilities)
        remote_capabilities = account.get("modelCapabilities")
        if isinstance(remote_capabilities, dict):
            account_capabilities.update({
                key: value for key, value in remote_capabilities.items()
                if isinstance(value, dict) and isinstance(value.get("efforts"), list)
            })
        sources.append(
            {
                "id": source_id,
                "kind": "account",
                "recordId": account_id,
                "name": str(account.get("label") or account.get("email") or "ChatGPT 账号"),
                "subtitle": str(account.get("email") or account.get("plan") or "OpenAI / ChatGPT"),
                "groupId": str(account.get("groupId") or "official"),
                "groupName": str(groups.get(str(account.get("groupId")), {}).get("name") or ""),
                "active": source_id == active_source_id,
                "activationRequired": bool(
                    source_id == active_source_id
                    and account_id == active_auth_id
                    and live_auth.get("requiresReapply")
                ),
                "authMode": account.get("authMode"),
                "sourceType": account.get("sourceType"),
                "codexCompatible": codex_compatible,
                "quotaOnly": not codex_compatible,
                # A transient quota/model refresh failure does not invalidate
                # OAuth credentials.  Only explicit authentication failures
                # make the source unavailable; cached/local models stay usable.
                "available": codex_compatible and invalid_reason is None and bool(models),
                "invalidReason": invalid_reason,
                "refreshState": str(account.get("refreshState") or "pending"),
                "refreshError": refresh_error[:320],
                "modelCatalogSource": "account" if stored_models else "local_runtime" if models else "missing",
                "modelsSource": account.get("modelsSource") or ("local_runtime" if not stored_models else "imported"),
                "modelsRefreshedAt": account.get("modelsRefreshedAt"),
                "modelsStale": bool(refresh_errors.get("models")) or _core._timestamp_is_stale(account.get("modelsRefreshedAt"), _core.ACCOUNT_MODELS_TTL_SECONDS),
                "modelsError": str(refresh_errors.get("models") or "")[:320],
                "models": [
                    {
                        "key": _core._model_key(source_id, model_id),
                        "id": model_id,
                        "name": model_id,
                        **_core._model_reasoning_metadata(model_id, account_capabilities),
                    }
                    for model_id in sorted(dict.fromkeys(models), key=_core._model_sort_key)
                ],
            }
        )
    for provider in settings.get("providers", []):
        if provider.get("kind") != "custom":
            continue
        provider_id = str(provider.get("id") or "")
        if not provider_id:
            continue
        source_id = f"provider:{provider_id}"
        models = [str(item).strip() for item in provider.get("models", []) if str(item).strip()]
        provider_capabilities = _core._effective_provider_model_capabilities(provider, local_models)
        discovery_state = str(
            provider.get("modelDiscoveryState")
            or provider.get("lastCheckStatus")
            or "pending"
        )
        sources.append(
            {
                "id": source_id,
                "kind": "provider",
                "recordId": provider_id,
                "name": str(provider.get("name") or provider_id),
                "subtitle": str(provider.get("baseUrl") or "API Provider"),
                "groupId": str(provider.get("groupId") or "relay"),
                "groupName": str(groups.get(str(provider.get("groupId")), {}).get("name") or ""),
                "active": source_id == active_source_id,
                "activationRequired": _core._provider_runtime_requires_reapply(settings, provider),
                "sourceType": str(provider.get("sourceType") or "manual_api"),
                "relayAccountId": str(provider.get("relayAccountId") or ""),
                "relayKeyId": str(provider.get("relayKeyId") or ""),
                "relayPlatform": str(provider.get("relayPlatform") or ""),
                "relayRateMultiplier": provider.get("relayRateMultiplier"),
                "available": (
                    _core.provider_key_configured(provider_id)
                    and bool(models)
                    and discovery_state != "error"
                ),
                "invalidReason": (
                    "中转站 API Key 或账号已被远端拒绝（401/402），请编辑凭据后重试。"
                    if discovery_state == "error"
                    else None
                ),
                "modelDiscoveryState": discovery_state,
                "modelDiscoveryError": str(provider.get("modelDiscoveryError") or provider.get("lastCheckError") or "")[:320],
                "modelCapabilitiesRefreshedAt": provider.get("modelCapabilitiesRefreshedAt"),
                "models": [
                    {
                        "key": _core._model_key(source_id, model_id),
                        "id": model_id,
                        "name": model_id,
                        "capabilitySource": (
                            provider_capabilities[model_id].get("reasoningSource", "provider_catalog")
                            if model_id in provider_capabilities
                            else "unknown"
                        ),
                        **_core._model_reasoning_metadata(model_id, provider_capabilities),
                    }
                    for model_id in sorted(dict.fromkeys(models), key=_core._model_sort_key)
                ],
            }
        )
    return sorted(sources, key=lambda item: (not item["active"], item["kind"] != "account", item["name"].casefold()))



def _all_model_records(
    settings: dict | None = None,
    local_models: list[dict] | None = None,
) -> list[dict]:
    records = []
    for source in _core.model_sources(settings, local_models):
        for model in source["models"]:
            records.append(
                {
                    **model,
                    "sourceId": source["id"],
                    "sourceKind": source["kind"],
                    "sourceRecordId": source["recordId"],
                    "sourceName": source["name"],
                    "available": source["available"],
                }
            )
    return records



def selected_model_records(settings: dict | None = None, aggregate: bool | None = None) -> list[dict]:
    settings = settings or _core.load_settings()
    workspace = settings.get("modelWorkspace", {})
    use_aggregate = workspace.get("mode") == "aggregate" if aggregate is None else aggregate
    sources = _core.model_sources(settings)
    records = [
        {
            **model,
            "sourceId": source["id"],
            "sourceKind": source["kind"],
            "sourceRecordId": source["recordId"],
            "sourceName": source["name"],
            "available": source["available"],
        }
        for source in sources
        for model in source["models"]
    ]
    if not use_aggregate:
        # Configuration generation follows the explicitly selected target.
        # The display's live source can still be the previous account while
        # an atomic switch prepares its replacement configuration.
        active_source_id = str(workspace.get("activeSourceId") or "")
        if not any(source.get("id") == active_source_id for source in sources):
            active_source_id = str(next((source["id"] for source in sources if source.get("active")), ""))
        records = [item for item in records if item["sourceId"] == active_source_id]
    selected = {str(item) for item in workspace.get("selectedModels", [])}
    if not bool(workspace.get("selectAll", True)):
        records = [item for item in records if item["key"] in selected]
    records = [item for item in records if item.get("available")]
    counts: dict[str, int] = {}
    alias_universe = _core._all_model_records(settings) if use_aggregate else records
    for item in alias_universe:
        counts[item["id"]] = counts.get(item["id"], 0) + 1
    for item in records:
        if counts[item["id"]] == 1:
            item["slug"] = item["id"]
            item["displayName"] = item["id"]
        else:
            suffix = _core.hashlib.sha256(item["sourceId"].encode("utf-8")).hexdigest()[:8]
            safe_model = _core.re.sub(r"[^A-Za-z0-9._-]+", "-", item["id"]).strip("-")[:70] or "model"
            item["slug"] = f"cam-{safe_model}-{suffix}"
            item["displayName"] = f"{item['id']} · {item['sourceName']}"
    return records



def gateway_model_records(settings: dict | None = None) -> list[dict]:
    settings = settings or _core.load_settings()
    selected = {item["key"]: item for item in _core.selected_model_records(settings, aggregate=True)}
    route_keys = {
        str(key)
        for route in settings.get("subagentRouting", {}).get("routes", {}).values()
        if isinstance(route, dict)
        for key in route.get("models", [])
    }
    if settings.get("web2api", {}).get("activeForCodex"):
        route_keys.update(item["key"] for item in _core.web2api_pool_model_records(settings))
    for item in _core._all_model_records(settings):
        if item["key"] not in route_keys or not item.get("available"):
            continue
        if item["key"] not in selected:
            counts = sum(1 for candidate in _core._all_model_records(settings) if candidate["id"] == item["id"])
            if counts == 1:
                item["slug"] = item["id"]
                item["displayName"] = item["id"]
            else:
                suffix = _core.hashlib.sha256(item["sourceId"].encode("utf-8")).hexdigest()[:8]
                safe_model = _core.re.sub(r"[^A-Za-z0-9._-]+", "-", item["id"]).strip("-")[:70] or "model"
                item["slug"] = f"cam-{safe_model}-{suffix}"
                item["displayName"] = f"{item['id']} · {item['sourceName']}"
            selected[item["key"]] = item
    return list(selected.values())



def web2api_pool_model_records(
    settings: dict | None = None,
    account_ids: set[str] | None = None,
) -> list[dict]:
    """Return the shared model surface exposed by the ordered Web2API pool.

    Duplicate model IDs intentionally collapse to one public model. Requests for
    that ID then flow through the configured pool policy instead of being pinned
    to a single account-specific alias.
    """
    settings = settings or _core.load_settings()
    config = settings.get("web2api", {})
    if account_ids is not None:
        allowed_ids = {str(item) for item in account_ids if str(item).strip()}
        configured_ids = [str(item) for item in config.get("accountIds", []) if str(item).strip()]
        ordered_source_ids = [f"account:{item}" for item in configured_ids if item in allowed_ids]
        ordered_source_ids.extend(
            f"account:{item}"
            for item in sorted(allowed_ids.difference(configured_ids))
        )
    else:
        active_account_id = str(config.get("activeAccountId") or "").strip()
        if config.get("activeForCodex") and active_account_id:
            ordered_source_ids = [f"account:{active_account_id}"]
        else:
            default_sources = [
                *(f"account:{item}" for item in config.get("accountIds", []) if str(item).strip()),
                *(f"provider:{item}" for item in config.get("providerIds", []) if str(item).strip()),
            ]
            allowed_sources = set(default_sources)
            ordered_source_ids = [
                str(item)
                for item in config.get("sourceOrder", [])
                if str(item) in allowed_sources
            ]
            ordered_source_ids.extend(
                item for item in default_sources if item not in ordered_source_ids
            )
    order = {source_id: index for index, source_id in enumerate(ordered_source_ids)}
    records = [
        item
        for item in _core._all_model_records(settings)
        if item.get("sourceId") in order
        and item.get("available")
    ]
    records.sort(
        key=lambda item: (
            order.get(str(item.get("sourceId")), 999999),
            _core._model_sort_key(item["id"]),
        )
    )
    result = []
    seen = set()
    for item in records:
        model_id = str(item.get("id") or "")
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        result.append(
            {
                **item,
                "slug": model_id,
                "displayName": model_id,
                "sourceName": "本地反代 API 号池",
            }
        )
    return result



def resolve_model_route(
    requested_model: str,
    settings: dict | None = None,
    *,
    access_scope: str = "internal",
) -> dict | None:
    settings = settings or _core.load_settings()
    requested = str(requested_model or "").strip()
    if not requested:
        return None
    if access_scope == "public":
        # Codex's selected session and private Agent aliases do not grant the
        # exported API key access to sources outside the configured public pool.
        public_settings = {
            **settings,
            "web2api": {**settings.get("web2api", {}), "activeForCodex": False},
        }
        record = next(
            (item for item in _core.web2api_pool_model_records(public_settings)
             if str(item.get("id") or "") == requested),
            None,
        )
        return record if record and record.get("sourceKind") == "provider" else None
    if access_scope != "internal":
        raise _core.ManagerError("未知的本地路由访问范围。")
    # Agent configs use a private, stable alias per difficulty/slot.  Resolve
    # that before the shared pool model surface so a child request cannot lose
    # its selected account and become indistinguishable from the main agent.
    for spec in _core._managed_subagent_specs(settings):
        if spec.get("routingMode") != "gateway":
            continue
        if str(spec.get("model") or "") != requested:
            continue
        record = next(
            (item for item in _core.gateway_model_records(settings) if item.get("key") == spec.get("modelKey")),
            None,
        )
        if record:
            return {
                **record,
                "slug": requested,
                "subagentAlias": True,
                "subagentLevel": spec.get("level"),
                "subagentSlot": spec.get("slot"),
            }
    web2api = settings.get("web2api", {})
    if web2api.get("enabled") or web2api.get("activeForCodex"):
        pool_record = next(
            (
                item
                for item in _core.web2api_pool_model_records(settings)
                if str(item.get("slug") or item.get("id") or "") == requested
            ),
            None,
        )
        if pool_record:
            # ChatGPT records intentionally return None so the existing account
            # pool can apply ordered/round-robin/quota routing and safe fallback.
            # A Provider record is pinned to its imported upstream.
            return pool_record if pool_record.get("sourceKind") == "provider" else None
    records = _core.gateway_model_records(settings)
    exact = next((item for item in records if item.get("slug") == requested), None)
    if exact:
        return exact
    matching = [item for item in records if item.get("id") == requested]
    return matching[0] if len(matching) == 1 else None



def build_synced_model_catalog(settings: dict | None = None) -> tuple[dict, list[dict]]:
    settings = settings or _core.load_settings()
    tuning = _core._normalize_runtime_tuning(settings.get("runtimeTuning"))
    proxy_active = bool(settings.get("web2api", {}).get("activeForCodex"))
    records = _core._configuration_model_records(settings)
    if not records:
        return {"models": []}, []
    raw = _core._raw_local_model_catalog()
    templates = [item for item in raw.get("models", []) if isinstance(item, dict) and item.get("slug")]
    by_slug = {str(item.get("slug")): item for item in templates}
    template = by_slug.get("gpt-5.4") or by_slug.get("gpt-5.6-sol") or (templates[0] if templates else None)
    if not template:
        raise _core.ManagerError("Codex 本地模型目录没有可用模板。")
    catalog = []
    catalog_records = [*records, *_core.managed_subagent_model_records(settings)]
    gateway_catalog = proxy_active or settings.get("modelWorkspace", {}).get("mode") == "aggregate" or _core._subagents_require_shared_gateway(settings)
    for priority, record in enumerate(catalog_records, start=1):
        source = by_slug.get(record["id"]) or template
        item = _core.json.loads(_core.json.dumps(source))
        provider_model = record.get("sourceKind") == "provider"
        if provider_model:
            # A same-named official template is not evidence about a relay's
            # deployed model. Remove source-dependent claims before applying
            # only metadata advertised by this Provider.
            for key in (
                "supported_reasoning_levels",
                "default_reasoning_level",
                "default_reasoning_summary",
                "supports_reasoning_summary_parameter",
                "supports_reasoning_summaries",
                "reasoning_summary_format",
                "multi_agent_reasoning_effort",
                "shell_type",
                "additional_speed_tiers",
                "service_tiers",
                "availability_nux",
                "upgrade",
                "model_messages",
                "include_skills_usage_instructions",
                "include_plugin_usage_instructions",
                "include_apps_usage_instructions",
                "apply_patch_tool_type",
                "web_search_tool_type",
                "truncation_policy",
                "supports_parallel_tool_calls",
                "supports_image_detail_original",
                "context_window",
                "max_context_window",
                "effective_context_window_percent",
                "supports_personality",
                "support_verbosity",
                "default_verbosity",
                "experimental_supported_tools",
                "input_modalities",
                "supports_search_tool",
                "use_responses_lite",
                "node_repl_auto_review_required",
                "node_repl_disabled",
                "tool_mode",
                "multi_agent_version",
                "comp_hash",
            ):
                item.pop(key, None)
            # These are structural fields in Codex model catalogs, not claims
            # that the upstream model implements an optional capability.  Use
            # conservative cross-version values instead of copying them from
            # an unrelated official model template.  Codex 0.130 additionally
            # requires the two legacy supports_* fields; newer versions safely
            # ignore them.
            item.update(
                {
                    "supported_reasoning_levels": [],
                    "shell_type": "default",
                    "support_verbosity": False,
                    "truncation_policy": {"mode": "bytes", "limit": 10_000},
                    "experimental_supported_tools": [],
                    "supports_reasoning_summary_parameter": False,
                    "supports_reasoning_summaries": False,
                    "supports_parallel_tool_calls": False,
                }
            )
        item["slug"] = record["slug"]
        # Codex uses display_name in its composer/status bar. Source-qualified
        # Manager labels belong in descriptions, not in the model's name.
        # Keep the opaque slug unchanged for routing and resumed conversations.
        item["display_name"] = record["id"]
        item["description"] = f"由 Agent Manager 路由到 {record['sourceName']}。"
        item["visibility"] = "hide" if record.get("subagentAlias") else "list"
        item["priority"] = priority
        item["supported_in_api"] = True
        if record.get("reasoningKnown") and record.get("efforts"):
            descriptions = {
                str(level.get("effort") or ""): str(level.get("description") or "")
                for level in source.get("supported_reasoning_levels", [])
                if isinstance(level, dict) and level.get("effort")
            }
            efforts = [
                str(effort)
                for effort in record.get("efforts", [])
                if str(effort) in _core.VALID_EFFORTS
            ]
            item["supported_reasoning_levels"] = [
                {
                    "effort": effort,
                    "description": descriptions.get(effort) or f"{effort} reasoning effort",
                }
                for effort in efforts
            ]
            requested_default = str(record.get("defaultEffort") or "")
            current_default = str(item.get("default_reasoning_level") or "")
            item["default_reasoning_level"] = (
                requested_default
                if requested_default in efforts
                else current_default
                if current_default in efforts
                else efforts[0]
            )
        elif provider_model and record.get("reasoningKnown") and record.get("reasoningSupported") is False:
            item["supported_reasoning_levels"] = []
        allowed_efforts = _core._codex_compatible_reasoning_efforts([
            str(level.get("effort") or "") for level in item.get("supported_reasoning_levels", []) if isinstance(level, dict)
        ])
        item["supported_reasoning_levels"] = [
            level for level in item.get("supported_reasoning_levels", []) if isinstance(level, dict) and level.get("effort") in allowed_efforts
        ]
        if item.get("default_reasoning_level") not in allowed_efforts:
            if allowed_efforts:
                item["default_reasoning_level"] = "medium" if "medium" in allowed_efforts else allowed_efforts[0]
            else:
                item.pop("default_reasoning_level", None)
        if provider_model:
            for record_key, catalog_key in (
                ("contextWindow", "context_window"),
                ("maxContextWindow", "max_context_window"),
                ("effectiveContextWindowPercent", "effective_context_window_percent"),
            ):
                if record.get(record_key) is not None:
                    item[catalog_key] = record[record_key]
            if isinstance(record.get("supportsPersonality"), bool):
                item["supports_personality"] = record["supportsPersonality"]
            if isinstance(record.get("supportsVerbosity"), bool):
                item["support_verbosity"] = record["supportsVerbosity"]
                if record["supportsVerbosity"] and record.get("defaultVerbosity"):
                    item["default_verbosity"] = record["defaultVerbosity"]
        # For official-account models, WebSocket preference is model catalog
        # metadata rather than a partial [model_providers.openai] override.
        # The latter is invalid because a custom provider table requires the
        # complete provider definition.  Writing the preference here makes the
        # existing visual switch effective for both official and routed models.
        managed_runtime_fields = set(tuning.get("managedFields") or [])
        if gateway_catalog or (
            "vpnCompatibility" in managed_runtime_fields
            and tuning.get("vpnCompatibility")
        ):
            item["prefer_websockets"] = False
        catalog.append(item)
    return {"models": catalog}, records

