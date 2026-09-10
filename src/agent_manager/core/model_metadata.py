"""Model metadata services."""
from __future__ import annotations
from agent_manager import core as _core

_EXTENDED_CONTEXT_MODELS = {"gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}


def _official_context_reference_max(model_id: str) -> int:
    # Verified 2026-09-10: https://developers.openai.com/api/docs/models/gpt-6-astra
    # This is a total window, before Codex's effective-context headroom.
    return 1_050_000 if model_id in _EXTENDED_CONTEXT_MODELS else 0


def _official_input_reference_max(model_id: str) -> int:
    # Total context includes up to 128K output; it is not an input allowance.
    return 922_000 if model_id in _EXTENDED_CONTEXT_MODELS else 0


def _managed_context_catalog_ceiling(record: dict, tuning: dict) -> int:
    """Honor an explicit official-model override without inventing relay limits."""
    if record.get("sourceKind") != "account" or "modelContextWindow" not in (tuning.get("managedFields") or []):
        return 0
    requested = int(tuning.get("modelContextWindow") or 0)
    reference = _official_input_reference_max(str(record.get("id") or ""))
    return min(requested, reference) if requested > 0 and reference else 0


def _bounded_model_id(value: _core.Any) -> str:
    """Normalize one upstream model identifier before it reaches settings."""

    if not isinstance(value, str):
        return ""
    if any(ord(character) < 0x20 for character in value):
        return ""
    model_id = _core.re.sub(r"\s+", " ", value).strip()
    if not model_id:
        return ""
    if len(model_id.encode("utf-8", errors="ignore")) > _core.MAX_MODEL_ID_BYTES:
        return ""
    return model_id



def _model_entries(payload: _core.Any) -> list[tuple[str, _core.Any]]:
    """Extract common OpenAI-compatible model-list envelopes.

    Relays in the wild use ``data``, ``models``, ``items`` or a nested
    ``result`` object.  A mapping is treated as ``fallback_id -> record`` so
    providers that return ``{"models": {"gpt-x": {...}}}`` remain usable.
    """

    if isinstance(payload, list):
        return [("", item) for item in payload]
    if isinstance(payload, str):
        return [("", item) for item in _core.re.split(r"[\s,;]+", payload) if item]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "models", "items", "result", "model_list", "available_models"):
        value = payload.get(key)
        if isinstance(value, list):
            return [("", item) for item in value]
        if isinstance(value, str):
            return [("", item) for item in _core.re.split(r"[\s,;]+", value) if item]
        if isinstance(value, dict):
            if any(identity_key in value for identity_key in ("id", "slug", "model", "name")):
                return [("", value)]
            if not any(
                nested_key in value
                for nested_key in ("data", "models", "items", "result", "model_list", "available_models")
            ):
                return [(str(fallback), item) for fallback, item in value.items()]
            nested = _core._model_entries(value)
            if nested:
                return nested
    return []



def _model_id_from_entry(value: _core.Any, fallback: str = "") -> str:
    if isinstance(value, str):
        return _core._bounded_model_id(value)
    if isinstance(value, dict):
        for key in ("slug", "id", "model", "name"):
            candidate = _core._bounded_model_id(value.get(key))
            if candidate:
                return candidate
    return _core._bounded_model_id(fallback)



def _parse_model_ids(payload: _core.Any) -> list[str]:
    models: list[str] = []
    seen: set[str] = set()
    for fallback, entry in _core._model_entries(payload)[:_core.MAX_MODEL_CATALOG_ITEMS]:
        model_id = _core._model_id_from_entry(entry, fallback)
        if model_id and model_id not in seen:
            seen.add(model_id)
            models.append(model_id)
    return sorted(models, key=str.casefold)



def _catalog_boolean(value: _core.Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "yes", "supported", "enabled", "1"}:
            return True
        if normalized in {"false", "no", "unsupported", "disabled", "0"}:
            return False
    return None



def _catalog_positive_integer(value: _core.Any, *, maximum: int = 1_000_000_000) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if 0 < parsed <= maximum else None



def _catalog_reasoning_efforts(value: _core.Any) -> list[str]:
    if isinstance(value, dict):
        values: list[Any] = [
            {"effort": key}
            for key, enabled in value.items()
            if _core._catalog_boolean(enabled) is not False
        ]
    elif isinstance(value, str):
        values = _core.re.split(r"[\s,;]+", value)
    elif isinstance(value, list):
        values = value
    else:
        return []
    efforts = []
    for item in values:
        candidate = (
            str(
                item.get("effort")
                or item.get("reasoningEffort")
                or item.get("name")
                or item.get("id")
                or ""
            )
            if isinstance(item, dict)
            else str(item or "")
        ).strip().casefold()
        if candidate in _core.VALID_EFFORTS and candidate not in efforts:
            efforts.append(candidate)
    return efforts



def _provider_model_capability(entry: _core.Any) -> dict:
    """Normalize only capabilities explicitly advertised by one Provider."""

    if not isinstance(entry, dict):
        return {}
    containers = [entry]
    advertised = entry.get("capabilities")
    if isinstance(advertised, dict):
        containers.append(advertised)
    reasoning = (
        entry.get("reasoning")
        if "reasoning" in entry
        else advertised.get("reasoning")
        if isinstance(advertised, dict)
        else None
    )
    if isinstance(reasoning, dict):
        containers.append(reasoning)

    def first(keys: tuple[str, ...]) -> tuple[bool, Any]:
        for container in containers:
            for key in keys:
                if key in container:
                    return True, container.get(key)
        return False, None

    capability: dict[str, Any] = {}
    levels_present, raw_levels = first(
        (
            "supported_reasoning_levels",
            "supportedReasoningEfforts",
            "supported_reasoning_efforts",
            "reasoning_efforts",
            "reasoningEfforts",
            "efforts",
        )
    )
    efforts = _core._catalog_reasoning_efforts(raw_levels) if levels_present else []
    levels_present = levels_present and entry.get("reasoningLevelsExplicit") is not False
    support_present, raw_support = first(
        (
            "supports_reasoning",
            "supportsReasoning",
            "reasoning_supported",
            "reasoningSupported",
        )
    )
    reasoning_supported = _core._catalog_boolean(raw_support) if support_present else None
    if reasoning_supported is None and isinstance(reasoning, dict) and "supported" in reasoning:
        reasoning_supported = _core._catalog_boolean(reasoning.get("supported"))
        support_present = reasoning_supported is not None
    if reasoning_supported is None and isinstance(reasoning, bool):
        reasoning_supported = reasoning
        support_present = True
    reasoning_known = entry.get("reasoningKnown") is True
    default_present, raw_default = first(
        (
            "default_reasoning_level",
            "defaultReasoningEffort",
            "default_reasoning_effort",
            "defaultEffort",
        )
    )
    default_effort = str(raw_default or "").strip().casefold()
    if default_effort not in _core.VALID_EFFORTS:
        default_effort = ""
    if reasoning_supported is None and levels_present:
        reasoning_supported = bool(efforts)
    if reasoning_supported is None and default_effort:
        reasoning_supported = True
    if support_present or levels_present or default_present or reasoning_known:
        if reasoning_supported is False:
            efforts = []
            default_effort = ""
        elif levels_present and default_effort not in efforts:
            default_effort = ""
        capability.update(
            {
                "reasoningKnown": reasoning_supported is False or levels_present,
                "reasoningSupported": reasoning_supported is not False,
                "efforts": efforts,
                "defaultEffort": default_effort,
            }
        )
        if not capability["reasoningKnown"]:
            # A default is not an exhaustive support list. Persist this marker
            # so a later normalization does not interpret our empty array as
            # an explicit declaration that only Medium (or no effort) works.
            capability["reasoningLevelsExplicit"] = False

    for output_key, keys in (
        ("contextWindow", ("context_window", "contextWindow")),
        ("maxContextWindow", ("max_context_window", "maxContextWindow")),
        (
            "effectiveContextWindowPercent",
            ("effective_context_window_percent", "effectiveContextWindowPercent"),
        ),
    ):
        present, value = first(keys)
        maximum = 100 if output_key == "effectiveContextWindowPercent" else 1_000_000_000
        parsed = _core._catalog_positive_integer(value, maximum=maximum) if present else None
        if parsed is not None:
            capability[output_key] = parsed
    if (
        capability.get("contextWindow")
        and capability.get("maxContextWindow")
        and capability["maxContextWindow"] < capability["contextWindow"]
    ):
        capability.pop("maxContextWindow", None)

    personality_present, raw_personality = first(
        ("supports_personality", "supportsPersonality")
    )
    personality = _core._catalog_boolean(raw_personality) if personality_present else None
    if personality is not None:
        capability["supportsPersonality"] = personality

    verbosity_present, raw_verbosity = first(
        (
            "support_verbosity",
            "supports_verbosity",
            "supportsVerbosity",
            "verbositySupported",
            "verbosity",
        )
    )
    verbosity = _core._catalog_boolean(raw_verbosity) if verbosity_present else None
    _default_verbosity_present, raw_default_verbosity = first(
        ("default_verbosity", "defaultVerbosity")
    )
    default_verbosity = str(raw_default_verbosity or "").strip().casefold()
    if default_verbosity not in _core.VALID_MODEL_VERBOSITIES or not default_verbosity:
        default_verbosity = ""
    if verbosity is None and default_verbosity:
        verbosity = True
    if verbosity is not None:
        capability["supportsVerbosity"] = verbosity
        capability["defaultVerbosity"] = default_verbosity if verbosity else ""
    return capability



def _normalize_provider_model_capabilities(
    value: _core.Any,
    model_ids: list[str] | tuple[str, ...] | set[str] | None = None,
) -> dict[str, dict]:
    if not isinstance(value, dict):
        return {}
    allowed = (
        {
            _core._bounded_model_id(item)
            for item in model_ids
            if _core._bounded_model_id(item)
        }
        if model_ids is not None
        else None
    )
    normalized: dict[str, dict] = {}
    for raw_model_id, raw_capability in list(value.items())[:_core.MAX_MODEL_CATALOG_ITEMS]:
        model_id = _core._bounded_model_id(raw_model_id)
        if not model_id or (allowed is not None and model_id not in allowed):
            continue
        capability = _core._provider_model_capability(raw_capability)
        if capability:
            normalized[model_id] = capability
    return normalized



def _parse_provider_model_catalog(payload: _core.Any) -> dict:
    """Keep Provider-advertised metadata without official picker filtering."""

    models: list[str] = []
    capabilities: dict[str, dict] = {}
    seen: set[str] = set()
    for fallback, entry in _core._model_entries(payload)[:_core.MAX_MODEL_CATALOG_ITEMS]:
        model_id = _core._model_id_from_entry(entry, fallback)
        if not model_id:
            continue
        if model_id not in seen:
            seen.add(model_id)
            models.append(model_id)
        capability = _core._provider_model_capability(entry)
        if capability:
            capabilities[model_id] = capability
    return {
        "models": sorted(models, key=str.casefold),
        "modelCapabilities": capabilities,
    }



def _model_is_picker_visible(entry: _core.Any) -> bool:
    return not isinstance(entry, dict) or not (
        entry.get("hidden") is True
        or str(entry.get("visibility") or "").casefold() in {"hide", "hidden"}
    )



def _parse_official_model_catalog(payload: _core.Any) -> dict:
    """Keep account-scoped capabilities and respect the official picker flags."""
    models: dict[str, dict] = {}
    for fallback, entry in _core._model_entries(payload)[:_core.MAX_MODEL_CATALOG_ITEMS]:
        model_id = _core._model_id_from_entry(entry, fallback)
        if not model_id or not _core._model_is_picker_visible(entry):
            continue
        capability = {}
        if isinstance(entry, dict):
            levels = entry.get("supported_reasoning_levels", entry.get("supportedReasoningEfforts"))
            if isinstance(levels, list):
                efforts = list(dict.fromkeys(
                    str(level.get("effort") or level.get("reasoningEffort") or "")
                    for level in levels if isinstance(level, dict)
                    and (level.get("effort") or level.get("reasoningEffort")) in _core.VALID_EFFORTS
                ))
                default = entry.get("default_reasoning_level", entry.get("defaultReasoningEffort"))
                capability = {"efforts": efforts, "defaultEffort": default if default in efforts else ""}
        models[model_id] = capability
    return {
        "models": sorted(models, key=str.casefold),
        "modelCapabilities": {key: value for key, value in models.items() if value},
    }

