"""Read the current Codex source without trusting a manager's saved selection.

All credential comparisons stay local. Public results contain IDs/status only.
This module never writes auth.json, config.toml, or environment variables.
"""

from __future__ import annotations

import json
import os
import secrets
from urllib.parse import urlsplit

import agent_manager.core as core


def _endpoint_identity(value: object) -> tuple | None:
    try:
        parsed = urlsplit(str(value or "").strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return None
        path = parsed.path.rstrip("/")
        if path.endswith("/v1"):
            path = path[:-3]
        return (
            parsed.scheme,
            parsed.hostname.casefold(),
            parsed.port or (443 if parsed.scheme == "https" else 80),
            path,
        )
    except ValueError:
        return None


def _auth_key() -> str:
    path = core.CODEX_HOME / "auth.json"
    try:
        if not path.is_file() or path.stat().st_size > 2_000_000:
            return ""
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        key = value.get("OPENAI_API_KEY") if isinstance(value, dict) else None
        return key.strip() if isinstance(key, str) else ""
    except (OSError, ValueError):
        return ""


def _configured_key(table: dict, *, allow_auth_fallback: bool) -> str:
    literal = table.get("experimental_bearer_token")
    if isinstance(literal, str) and literal.strip():
        return literal.strip()
    headers = table.get("http_headers")
    if isinstance(headers, dict):
        bearer = next(
            (
                str(v)
                for k, v in headers.items()
                if str(k).casefold() == "authorization"
            ),
            "",
        )
        if bearer.casefold().startswith("bearer "):
            return bearer[7:].strip()
    env_key = str(table.get("env_key") or "").strip()
    if env_key:
        try:
            core._validate_provider_env_key(
                env_key, allow_internal=env_key == core.AGGREGATE_ENV_KEY
            )
            # Cockpit may change persistent variables after Manager was opened;
            # do not let our inherited environment hide that external change.
            persisted = core._read_user_environment(env_key)
            value = persisted if persisted is not None else os.environ.get(env_key, "")
            return str(value or "").strip()
        except (core.ManagerError, OSError):
            return ""
    return _auth_key() if allow_auth_fallback else ""


def _expected_key(provider: dict) -> str:
    try:
        if (
            provider.get("sourceType") == "relay_account"
            and provider.get("relayAccountId")
            and provider.get("relayKeyId")
        ):
            return (
                core.load_relay_account_key(
                    provider["relayAccountId"], provider["relayKeyId"], required=False
                )
                or ""
            )
        return (
            core.load_provider_key(str(provider.get("id") or ""), required=False) or ""
        )
    except core.ManagerError:
        return ""


def inspect_live_selection(settings: dict, *, auth: dict | None = None) -> dict:
    config = core.read_toml(core.CONFIG_FILE)
    provider_id = str(config.get("model_provider") or "openai")
    model = str(config.get("model") or "")
    saved_source = str(settings.get("modelWorkspace", {}).get("activeSourceId") or "")
    result = {
        "sourceId": "",
        "recognized": False,
        "configured": False,
        "providerId": provider_id,
        "model": model,
        "kind": "none",
    }
    definitions = config.get("model_providers")
    if provider_id != "openai" and (
        not isinstance(definitions, dict) or not isinstance(definitions.get(provider_id), dict)
    ):
        return {**result, "configured": True, "kind": "external",
                "reason": "model_provider_not_found"}
    if provider_id == core.AGGREGATE_PROVIDER_ID:
        table = (config.get("model_providers") or {}).get(provider_id, {})
        if not isinstance(table, dict):
            table = {}
        endpoint = _endpoint_identity(table.get("base_url"))
        expected = _endpoint_identity(
            f"http://127.0.0.1:{int(settings.get('web2api', {}).get('port') or 17860)}/v1"
        )
        own_gateway = (
            endpoint == expected and table.get("env_key") == core.AGGREGATE_ENV_KEY
        )
        return {
            **result,
            "configured": True,
            "kind": "manager_gateway" if own_gateway else "external",
            "recognized": own_gateway,
            "sourceId": saved_source if own_gateway else "",
        }

    custom = provider_id != "openai" or core._official_route_has_overrides(config)
    if not custom:
        if auth is None:
            try:
                _snapshot, identity = core._read_live_snapshot()
                matched = core._find_account_for_identity(settings, identity)
                auth = {
                    "signedIn": True,
                    "authMode": identity.get("authMode"),
                    "activeAccountId": (matched or {}).get("id"),
                }
            except core.ManagerError:
                auth = {}
        account_id = str(auth.get("activeAccountId") or "")
        if account_id:
            return {
                **result,
                "sourceId": f"account:{account_id}",
                "recognized": True,
                "configured": True,
                "kind": "account",
            }
        if auth.get("signedIn") or _auth_key():
            return {**result, "configured": True, "kind": "external"}
        return result

    result.update(configured=True, kind="external")
    providers_table = config.get("model_providers")
    table = (
        providers_table.get(provider_id, {})
        if isinstance(providers_table, dict)
        else {}
    )
    table = table if isinstance(table, dict) else {}
    endpoint = _endpoint_identity(
        table.get("base_url") or config.get("openai_base_url") or config.get("chatgpt_base_url")
    )
    actual_key = _configured_key(table, allow_auth_fallback=True)
    if not endpoint or not actual_key:
        return result
    matches = []
    for provider in settings.get("providers", []):
        if not isinstance(provider, dict) or provider.get("kind") != "custom":
            continue
        try:
            candidate_endpoint = _endpoint_identity(
                core._provider_runtime_base_url(provider)
            )
        except core.ManagerError:
            continue
        if candidate_endpoint != endpoint:
            continue
        candidate_key = _expected_key(provider)
        if candidate_key and secrets.compare_digest(
            candidate_key.encode("utf-8"), actual_key.encode("utf-8")
        ):
            matches.append(str(provider.get("id") or ""))
    if provider_id in matches:
        matches = [provider_id]
    if len(matches) == 1:
        return {
            **result,
            "sourceId": f"provider:{matches[0]}",
            "recognized": True,
            "kind": "provider",
        }
    return result


def _externally_changed(settings: dict, live: dict) -> bool:
    saved_source = str(settings.get("modelWorkspace", {}).get("activeSourceId") or "")
    changed = bool(
        live["configured"]
        and (
            not live["recognized"]
            or live["sourceId"] != saved_source
            or (
                live["kind"] == "provider"
                and live["providerId"] != live["sourceId"].partition(":")[2]
            )
        )
    )
    overlay = core._runtime_overlay_read()
    if live["configured"] and isinstance(overlay, dict):
        record = overlay.get("files", {}).get("config.toml", {})
        if record.get("appliedHash"):
            current = (
                core.CONFIG_FILE.read_bytes() if core.CONFIG_FILE.is_file() else None
            )
            changed = changed or record["appliedHash"] != core._overlay_value_hash(
                current
            )
        elif record.get("capturedHash"):
            # A previous passive startup captured this state but never applied
            # Manager configuration. Keep preserving it across close/reopen.
            changed = True
    return changed


def preserve_live_configuration_on_restart() -> bool:
    """Read-only restart decision; a manager restart is not an account switch."""
    settings = core.load_settings()
    return _externally_changed(settings, inspect_live_selection(settings))


def reconcile_startup_selection() -> dict:
    """Preserve external selections; reconcile only Manager's own saved IDs."""
    settings = core.load_settings()
    live = inspect_live_selection(settings)
    saved_source = str(settings.get("modelWorkspace", {}).get("activeSourceId") or "")
    changed = _externally_changed(settings, live)
    if changed and live["recognized"] and live["sourceId"] != saved_source:
        source = next(
            (
                item
                for item in core.model_sources(settings)
                if item.get("id") == live["sourceId"]
            ),
            None,
        )
        if source:
            workspace = core._select_workspace_source(
                settings, source, independent=True
            )
            actual_model_key = f"{live['sourceId']}::{live['model']}"
            if actual_model_key in {
                item.get("key") for item in source.get("models", [])
            }:
                workspace["defaultModelKey"] = actual_model_key
            profile = core._active_main(settings)
            profile["provider"] = (
                source["recordId"] if source["kind"] == "provider" else "openai"
            )
            if live["model"]:
                profile["model"] = live["model"]
            settings.setdefault("web2api", {})["activeForCodex"] = False
            settings["web2api"]["activeAccountId"] = None
            core.save_settings(settings)
    return {
        "preserveCurrent": changed,
        "sourceId": live["sourceId"],
        "recognized": live["recognized"],
        "message": "检测到外部工具修改了 Codex，已保留当前账号与配置。选择账号或保存调度设置后再应用管理器配置。"
        if changed
        else "",
    }
