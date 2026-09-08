"""Projection services."""
from __future__ import annotations
from agent_manager import core as _core


def public_account_records(settings: dict | None = None) -> list[dict]:
    settings = settings or _core.load_settings()
    return [
        {
            **{key: value for key, value in account.items() if key != "fingerprint"},
            "invalid": _core.account_invalid_reason(account) is not None,
            "invalidReason": _core.account_invalid_reason(account),
        }
        for account in settings.get("accounts", [])
    ]



def public_account_snapshot() -> dict:
    """Return the frequently changing account fields without building full app state."""

    settings = _core.load_settings()
    try:
        revision = _core.SETTINGS_FILE.stat().st_mtime_ns
    except OSError:
        revision = 0
    return {
        "accounts": _core.public_account_records(settings),
        "auth": _core.current_auth_state(settings),
        "revision": revision,
        "checkedAt": _core.now_iso(),
    }



def _public_settings_projection(settings: dict) -> dict:
    public_settings = _core.json.loads(_core.json.dumps(settings))
    public_settings["accounts"] = _core.public_account_records(settings)
    try:
        relay_secret_payload = _core._secret_store()
    except _core.ManagerError:
        relay_secret_payload = {"relayAccounts": {}}
    for relay_account in public_settings.get("relayAccounts", []):
        account_id = str(relay_account.get("id") or "")
        configured = set(_core._relay_account_secret_keys(relay_secret_payload, account_id))
        account_secret_store = relay_secret_payload.get("relayAccounts", {}).get(account_id)
        relay_account["dashboardSessionStored"] = bool(
            isinstance(account_secret_store, dict) and account_secret_store.get("dashboardSession")
        )
        relay_account["dashboardSessionUpdatedAt"] = (
            str(account_secret_store.get("dashboardSessionUpdatedAt") or "")
            if isinstance(account_secret_store, dict)
            else ""
        )
        for key in relay_account.get("keys", []):
            if isinstance(key, dict):
                key["secretConfigured"] = str(key.get("id") or "") in configured
    public_settings.setdefault("web2api", _core._default_web2api_settings())["keyConfigured"] = _core.service_secret_configured(
        "web2api"
    )
    providers = []
    for item in settings["providers"]:
        record = dict(item)
        record["keyConfigured"] = _core.provider_key_configured(item["id"])
        providers.append(record)
    public_settings["providers"] = providers
    return public_settings



def _selected_connection_model_keys(settings: dict, sources: list[dict]) -> list[str]:
    records = [
        {
            **model,
            "sourceId": source["id"],
            "available": source["available"],
        }
        for source in sources
        for model in source.get("models", [])
        if isinstance(model, dict)
    ]
    workspace = settings.get("modelWorkspace", _core._default_model_workspace())
    if workspace.get("mode") != "aggregate":
        active_source_id = str(
            next((item["id"] for item in sources if item.get("active")), "")
        )
        records = [item for item in records if item.get("sourceId") == active_source_id]
    selected = {str(item) for item in workspace.get("selectedModels", [])}
    return [
        item["key"]
        for item in records
        if item.get("available")
        and (workspace.get("selectAll", True) or item["key"] in selected)
    ]



def _public_connections_state(
    settings: dict,
    local_models: list[dict],
    public_settings: dict | None = None,
) -> dict:
    public_settings = public_settings or _core._public_settings_projection(settings)
    sources = _core.model_sources(settings, local_models)
    auth = _core.current_auth_state(settings)
    live_selection = auth.get("liveSelection") or {}
    display_settings = settings
    if live_selection.get("configured") and live_selection.get("kind") != "manager_gateway":
        display_settings = _core._json_clone(settings)
        active_source = next((source for source in sources if source.get("active")), None)
        if active_source:
            workspace = _core._select_workspace_source(display_settings, active_source, independent=True)
            actual_key = f"{active_source['id']}::{live_selection.get('model') or ''}"
            if any(model.get("key") == actual_key for model in active_source.get("models", [])):
                workspace["defaultModelKey"] = actual_key
        else:
            display_settings.setdefault("modelWorkspace", {})["activeSourceId"] = ""
            display_settings["modelWorkspace"]["defaultModelKey"] = ""
        public_settings["modelWorkspace"] = _core._json_clone(display_settings["modelWorkspace"])
    default_main = _core._default_main_record(display_settings, _core._configuration_model_records(display_settings))
    connection_keys = (
        "accounts",
        "providers",
        "relayAccounts",
        "accountGroups",
        "web2api",
        "modelWorkspace",
    )
    return {
        "settings": {
            key: _core.json.loads(_core.json.dumps(public_settings.get(key)))
            for key in connection_keys
        },
        "modelSources": sources,
        "selectedModelKeys": _core._selected_connection_model_keys(display_settings, sources),
        "effectiveSubagentRouting": _core._effective_subagent_routing(
            settings,
            sources,
            default_main,
        ),
        "auth": auth,
    }



def public_connections_state() -> dict:
    """Return the connection cards without scanning Agents, history or status."""

    settings = _core.load_settings()
    try:
        local_models = _core.local_model_catalog()
    except Exception:
        local_models = []
    return _core._public_connections_state(settings, local_models)



def public_state() -> dict:
    settings = _core.load_settings()
    public_settings = _core._public_settings_projection(settings)
    agents = []
    for record in _core.discover_agents():
        data = record.get("data", {})
        agents.append(
            {
                "name": data.get("name") or record["path"].stem,
                "description": data.get("description", ""),
                "model": data.get("model", ""),
                "effort": data.get("model_reasoning_effort", ""),
                "provider": data.get("model_provider") or "openai",
                "sandbox": data.get("sandbox_mode") or "inherit",
                "instructions": data.get("developer_instructions", ""),
                "path": str(record["path"]),
                "valid": not bool(record.get("error")),
                "error": record.get("error"),
            }
        )
    try:
        local_models = _core.local_model_catalog()
        model_error = None
    except Exception as exc:
        local_models = []
        model_error = _core._redact_sensitive_text(exc, limit=320)
    connections = _core._public_connections_state(settings, local_models, public_settings)
    return {
        "settings": public_settings,
        "agents": agents,
        "localModels": local_models,
        "modelSources": connections["modelSources"],
        "selectedModelKeys": connections["selectedModelKeys"],
        "effectiveSubagentRouting": connections["effectiveSubagentRouting"],
        "subagentRuntime": _core.subagent_runtime_summary(settings),
        "modelError": model_error,
        "difficultyMeta": _core.DIFFICULTY_META,
        "efforts": list(_core.VALID_EFFORTS),
        "sandboxes": list(_core.VALID_SANDBOXES),
        "codexVersion": _core.codex_version(),
        "codexHome": str(_core.CODEX_HOME),
        "configPath": str(_core.CONFIG_FILE),
        "status": _core.configuration_status(settings),
        "auth": connections["auth"],
        "historySummary": _core.history_inventory(),
    }

