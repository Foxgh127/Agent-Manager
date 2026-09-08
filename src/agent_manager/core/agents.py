"""Agents services."""
from __future__ import annotations
from agent_manager import core as _core


def discover_agents() -> list[dict]:
    _core.AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    records = []
    for path in sorted(_core.AGENTS_DIR.glob("*.toml"), key=lambda item: item.name.casefold()):
        try:
            data = _core.read_toml(path)
            error = None
        except _core.ManagerError as exc:
            data = {}
            error = str(exc)
        records.append({"path": path, "data": data, "error": error})
    return records



def agent_by_name(name: str) -> dict:
    for record in _core.discover_agents():
        if record["data"].get("name") == name or record["path"].stem == name.replace("_", "-"):
            if record.get("error"):
                raise _core.ManagerError(record["error"])
            return record
    raise _core.ManagerError(f"找不到 Agent：{name}")



def render_agent_toml(
    name: str,
    description: str,
    model: str,
    effort: str | None,
    instructions: str,
    provider: str | None,
    sandbox: str | None,
) -> str:
    doc = _core.tomlkit.document()
    doc.add("name", name)
    doc.add("description", description)
    doc.add("model", model)
    if effort:
        doc.add("model_reasoning_effort", effort)
    if provider and provider != "openai":
        doc.add("model_provider", provider)
    if sandbox:
        doc.add("sandbox_mode", sandbox)
    doc.add(_core.tomlkit.nl())
    doc.add("developer_instructions", _core.tomlkit.string(instructions.strip(), multiline=True))
    return _core.tomlkit.dumps(doc)



def write_agent(payload: dict, force: bool = False) -> _core.Path:
    name = str(payload.get("name", "")).strip()
    if not _core.re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
        raise _core.ManagerError("Agent 名称必须以字母开头，只能包含字母、数字、下划线和连字符。")
    description = str(payload.get("description", "")).strip()
    model = str(payload.get("model", "")).strip()
    effort = str(payload.get("effort", "")).strip()
    instructions = str(payload.get("instructions", "")).strip()
    provider = str(payload.get("provider") or "openai")
    sandbox = str(payload.get("sandbox") or "") or None
    if not description or not model or not instructions:
        raise _core.ManagerError("Agent 的说明、模型和 Instructions 都不能为空。")
    if effort not in _core.VALID_EFFORTS:
        raise _core.ManagerError("Agent 推理强度无效。")
    if sandbox and sandbox not in _core.VALID_SANDBOXES:
        raise _core.ManagerError("Agent 权限模式无效。")
    _core.provider_by_id(provider)
    path = _core.AGENTS_DIR / f"{name.replace('_', '-')}.toml"
    if path.exists() and not force:
        raise _core.ManagerError(f"Agent 已存在：{name}")
    content = _core.render_agent_toml(name, description, model, effort, instructions, provider, sandbox)
    _core.tomllib.loads(content)
    _core.backup_file(path)
    _core.atomic_write_text(path, content)
    return path



def archive_agent(name: str) -> _core.Path:
    settings = _core.load_settings()
    for level, route in settings.get("routes", {}).items():
        if name in route.get("agents", []):
            raise _core.ManagerError(f"Agent 正被“{_core.DIFFICULTY_META[level]['name']}”路由使用。")
    source = _core.Path(_core.agent_by_name(name)["path"])
    _core.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    destination = _core.BACKUPS_DIR / f"{source.name}.{_core.datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.removed"
    _core.shutil.move(str(source), str(destination))
    return destination



def _merge_by_id(items: list[dict], record: dict) -> list[dict]:
    existing = next((item for item in items if item.get("id") == record["id"]), None)
    if existing:
        items[items.index(existing)] = record
    else:
        items.append(record)
    return items



def _main_profile_model_capability(
    settings: dict,
    provider_id: str,
    model_id: str,
    main_record: dict | None = None,
) -> dict | None:
    """Return reasoning metadata from the exact source selected for the main model."""

    if isinstance(main_record, dict) and str(main_record.get("id") or "") == model_id:
        if main_record.get("reasoningKnown") is True:
            return {
                "reasoningKnown": True,
                "reasoningSupported": main_record.get("reasoningSupported"),
                "efforts": [
                    str(item)
                    for item in main_record.get("efforts", [])
                    if str(item) in _core.VALID_EFFORTS
                ],
                "defaultEffort": str(main_record.get("defaultEffort") or ""),
            }
        return None

    if provider_id != "openai":
        provider = _core.provider_by_id(provider_id, settings)
        capabilities = _core._effective_provider_model_capabilities(provider)
        if model_id not in capabilities:
            return None
        return _core._model_reasoning_metadata(model_id, capabilities)

    # Official account metadata is account-scoped.  It takes precedence over
    # the local fallback for the active official source, while custom Provider
    # metadata with the same model ID is never consulted here.
    source_id = str(settings.get("modelWorkspace", {}).get("activeSourceId") or "")
    if source_id.startswith("account:"):
        account_id = source_id.split(":", 1)[1]
        account = next(
            (
                item
                for item in settings.get("accounts", [])
                if isinstance(item, dict) and str(item.get("id") or "") == account_id
            ),
            None,
        )
        if account:
            capabilities = _core._normalize_provider_model_capabilities(
                account.get("modelCapabilities"),
                account.get("models", []),
            )
            if model_id in capabilities:
                return _core._model_reasoning_metadata(model_id, capabilities)
    capabilities = _core._reasoning_capabilities()
    if model_id not in capabilities:
        return None
    return _core._model_reasoning_metadata(model_id, capabilities)



def _normalize_main_profile_effort(
    settings: dict,
    provider_id: str,
    model_id: str,
    requested: object,
    *,
    main_record: dict | None = None,
    reject_conflict: bool,
) -> str:
    """Keep explicit unknown efforts, but never emit a known-incompatible one."""

    effort = str(requested or "").strip()
    if not effort:
        return ""
    if effort not in _core._codex_compatible_reasoning_efforts([effort]):
        if reject_conflict:
            raise _core.ManagerError(f"当前 Codex 运行时不支持 {effort}，请更新 Codex 后再选择此档位。")
        return ""
    if effort not in _core.VALID_EFFORTS:
        if reject_conflict:
            raise _core.ManagerError("主模型推理强度无效。")
        return ""
    capability = _core._main_profile_model_capability(
        settings,
        provider_id,
        model_id,
        main_record,
    )
    if not capability or capability.get("reasoningKnown") is not True:
        return effort
    supported = [
        str(item)
        for item in capability.get("efforts", [])
        if str(item) in _core.VALID_EFFORTS
    ]
    explicitly_unsupported = capability.get("reasoningSupported") is False
    incompatible = explicitly_unsupported or bool(supported) and effort not in supported
    if not incompatible:
        return effort
    if not reject_conflict:
        return ""
    if explicitly_unsupported:
        raise _core.ManagerError(
            f"模型 {model_id} 的 Provider 已明确声明不支持推理强度；请改为自动跟随。"
        )
    raise _core.ManagerError(
        f"模型 {model_id} 仅支持推理强度：{'、'.join(supported)}；当前选择 {effort} 不可用。"
    )



def save_main_profile(payload: dict) -> dict:
    settings = _core.load_settings()
    profile_id = _core.slugify(str(payload.get("id", "")), "主模型预设 ID")
    provider_id = str(payload.get("provider", "openai"))
    model_id = str(payload.get("model", "")).strip()
    record = {
        "id": profile_id,
        "name": str(payload.get("name", "")).strip(),
        "provider": provider_id,
        "model": model_id,
        "effort": "",
    }
    if not record["name"] or not record["model"]:
        raise _core.ManagerError("主模型预设名称和模型不能为空。")
    _core.provider_by_id(record["provider"], settings)
    record["effort"] = _core._normalize_main_profile_effort(
        settings,
        provider_id,
        model_id,
        payload.get("effort"),
        reject_conflict=True,
    )
    _core._merge_by_id(settings["mainProfiles"], record)
    _core.save_settings(settings)
    return record



def remove_main_profile(profile_id: str) -> None:
    settings = _core.load_settings()
    if settings.get("activeMainProfileId") == profile_id:
        raise _core.ManagerError("正在使用的主模型预设不能删除。")
    settings["mainProfiles"] = [item for item in settings["mainProfiles"] if item.get("id") != profile_id]
    _core.save_settings(settings)



def set_active_main(profile_id: str) -> None:
    settings = _core.load_settings()
    if not any(item.get("id") == profile_id for item in settings["mainProfiles"]):
        raise _core.ManagerError("主模型预设不存在。")
    settings["activeMainProfileId"] = profile_id
    _core.save_settings(settings)



def save_strategy(payload: dict) -> dict:
    settings = _core.load_settings()
    strategy_id = _core.slugify(str(payload.get("id", "")), "策略 ID")
    record = {
        "id": strategy_id,
        "name": str(payload.get("name", "")).strip(),
        "description": str(payload.get("description", "")).strip(),
        "instructions": str(payload.get("instructions", "")).strip(),
    }
    if not all(record[key] for key in ("name", "description", "instructions")):
        raise _core.ManagerError("策略名称、说明和调用规则都不能为空。")
    _core._merge_by_id(settings["strategies"], record)
    _core.save_settings(settings)
    return record



def remove_strategy(strategy_id: str) -> None:
    settings = _core.load_settings()
    if settings.get("activeStrategyId") == strategy_id:
        raise _core.ManagerError("正在使用的调用策略不能删除。")
    settings["strategies"] = [item for item in settings["strategies"] if item.get("id") != strategy_id]
    _core.save_settings(settings)



def set_active_strategy(strategy_id: str) -> None:
    settings = _core.load_settings()
    if not any(item.get("id") == strategy_id for item in settings["strategies"]):
        raise _core.ManagerError("调用策略不存在。")
    settings["activeStrategyId"] = strategy_id
    _core.save_settings(settings)



def save_routes(routes: dict) -> None:
    settings = _core.load_settings()
    agent_names = {record["data"].get("name") for record in _core.discover_agents() if not record.get("error")}
    normalized = {}
    for level in _core.DIFFICULTIES:
        route = routes.get(level, {})
        names = list(dict.fromkeys(str(item) for item in route.get("agents", [])))
        missing = [name for name in names if name not in agent_names]
        if missing:
            raise _core.ManagerError(f"{_core.DIFFICULTY_META[level]['name']}路由引用了不存在的 Agent：{', '.join(missing)}")
        normalized[level] = {
            "enabled": bool(route.get("enabled")) and bool(names),
            "agents": names,
            "description": str(route.get("description") or _core.DIFFICULTY_META[level]["description"]).strip(),
        }
    settings["routes"] = normalized
    _core.save_settings(settings)



def _active_main(settings: dict) -> dict:
    profile = next(
        (item for item in settings.get("mainProfiles", []) if item.get("id") == settings.get("activeMainProfileId")),
        None,
    )
    if not profile:
        raise _core.ManagerError("当前主模型预设不存在。")
    return profile



def _active_strategy(settings: dict) -> dict:
    strategy = next(
        (item for item in settings.get("strategies", []) if item.get("id") == settings.get("activeStrategyId")),
        None,
    )
    if not strategy:
        raise _core.ManagerError("当前调用策略不存在。")
    return strategy



def _uses_codex_native_subagent_policy(settings: dict) -> bool:
    """Return true when Agent Manager must leave Codex delegation untouched."""

    routing = settings.get("subagentRouting", {})
    strategy_id = str(
        routing.get("strategyId") or settings.get("activeStrategyId") or ""
        if isinstance(routing, dict)
        else settings.get("activeStrategyId") or ""
    )
    return strategy_id == "verification_first"



def _managed_subagent_mode_hint(settings: dict) -> str | None:
    """Return the Manager policy that replaces Codex's effort-derived V2 hint."""

    if _core._uses_codex_native_subagent_policy(settings):
        return None
    match = _core.re.search(r"(\d+)\.(\d+)\.(\d+)", _core.codex_version())
    if not match or tuple(int(part) for part in match.groups()) < (0, 153, 0):
        # Older parsers do not accept parameterized feature tables. They
        # still receive the managed roles and AGENTS policy through V1.
        return None
    routing = settings.get("subagentRouting", {})
    strategy_id = str(routing.get("strategyId") or settings.get("activeStrategyId") or "")
    strategy = next(
        (item for item in settings.get("strategies", []) if item.get("id") == strategy_id),
        None,
    )
    prompt = str(routing.get("prompt") or (strategy or {}).get("instructions") or "").strip()
    return prompt or None



def _default_model_reasoning_effort(record: dict | None, level: str) -> str:
    """Choose a default only when this exact source advertises the effort."""

    if not isinstance(record, dict) or record.get("reasoningKnown") is not True:
        return ""
    supported = [
        str(item)
        for item in record.get("efforts", [])
        if str(item) in _core.VALID_EFFORTS
    ]
    requested = _core.DEFAULT_EFFORT_BY_DIFFICULTY.get(level, "")
    if requested in supported:
        return requested
    advertised_default = str(record.get("defaultEffort") or "")
    return advertised_default if advertised_default in supported else ""



def _effective_subagent_routing(
    settings: dict,
    sources: list[dict],
    default_main: dict | None,
) -> dict:
    effective = _core.json.loads(
        _core.json.dumps(settings.get("subagentRouting", _core._default_subagent_routing()))
    )
    first_key = str(default_main.get("key") or "") if default_main else ""
    by_key = {
        str(model.get("key")): model
        for source in sources
        for model in source.get("models", [])
        if isinstance(model, dict) and model.get("key")
    }
    for level in _core.DIFFICULTIES:
        route = effective.setdefault("routes", {}).setdefault(
            level,
            {"models": [], "efforts": []},
        )
        if not route.get("models") and first_key:
            route["models"] = [first_key]
            route["efforts"] = [
                _core._default_model_reasoning_effort(by_key.get(first_key), level)
            ]
        models = route.get("models", []) if isinstance(route.get("models"), list) else []
        raw_efforts = route.get("efforts", []) if isinstance(route.get("efforts"), list) else []
        sanitized_efforts = []
        for index, model_key in enumerate(models[:3]):
            effort = str(raw_efforts[index] or "").strip() if index < len(raw_efforts) else ""
            model = by_key.get(str(model_key), {})
            supported = (
                model.get("efforts", [])
                if model.get("reasoningKnown")
                else list(_core.VALID_EFFORTS)
            )
            sanitized_efforts.append(effort if effort in supported else "")
        route["efforts"] = sanitized_efforts
    return effective

