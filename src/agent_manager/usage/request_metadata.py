"""Bounded upstream billing evidence; never infer request facts from daily totals.

Only protocol metadata and token counts are retained. This module neither reads
credentials nor stores response text. Missing fields stay explicitly unknown.
"""
from __future__ import annotations

import math
import re
from typing import Any

EVIDENCE_VERSION = 2
SHORT_CONTEXT_LIMIT = 272_000
IDENTITY_FIELDS = (
    "actualModel", "modelEvidence", "serviceTier", "contextTier",
    "cacheWriteEvidence", "cachedInputEvidence", "inputOutputEvidence", "reasoningEvidence",
    "usageEvidenceVersion",
)
SERVICE_TIERS = {"default", "priority", "fast", "flex", "batch", "ultrafast"}
EVIDENCE_FIELDS = ("cacheWriteEvidence", "cachedInputEvidence", "inputOutputEvidence", "reasoningEvidence")


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
        if math.isfinite(number) and number.is_integer() and 0 <= number <= 10**15:
            return int(number)
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _model(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,199}", value) else ""


def safe_metadata(value: Any) -> dict:
    """Sanitize already-captured fields, preserving legacy evidence as unknown."""
    value = value if isinstance(value, dict) else {}
    current = type(value.get("usageEvidenceVersion")) is int and value["usageEvidenceVersion"] == EVIDENCE_VERSION
    actual = _model(value.get("actualModel")) if current else ""
    actual = actual if value.get("modelEvidence") == "actual" else ""
    return {
        "usageEvidenceVersion": EVIDENCE_VERSION if current else 0,
        "actualModel": actual,
        "modelEvidence": "actual" if actual else "requested",
        "serviceTier": value.get("serviceTier") if current and isinstance(value.get("serviceTier"), str) and value["serviceTier"] in SERVICE_TIERS else "unknown",
        "contextTier": value.get("contextTier") if current and isinstance(value.get("contextTier"), str) and value["contextTier"] in {"short", "long"} else "unknown",
        **{key: "known" if current and value.get(key) == "known" else "unknown" for key in EVIDENCE_FIELDS},
    }


def observe_metadata(previous: Any, payload: Any) -> dict:
    """Merge a Responses/Chat JSON object or SSE event into its request capture."""
    result = safe_metadata(previous)
    result["usageEvidenceVersion"] = EVIDENCE_VERSION
    if not isinstance(payload, dict):
        return result
    response = payload.get("response")
    source = response if isinstance(response, dict) else payload
    if "model" in source:
        actual = _model(source.get("model"))
        result.update(actualModel=actual, modelEvidence="actual" if actual else "requested")
    if "service_tier" in source:
        tier = source.get("service_tier")
        result["serviceTier"] = tier if isinstance(tier, str) and tier in SERVICE_TIERS else "unknown"
    usage = source.get("usage")
    if not isinstance(usage, dict):
        return result
    input_count = _count(usage.get("input_tokens", usage.get("prompt_tokens", usage.get("inputTokens"))))
    output_count = _count(usage.get("output_tokens", usage.get("completion_tokens", usage.get("outputTokens"))))
    result["inputOutputEvidence"] = "known" if input_count is not None and output_count is not None else "unknown"
    result["contextTier"] = "unknown" if input_count is None else "short" if input_count <= SHORT_CONTEXT_LIMIT else "long"
    input_details = usage.get("input_tokens_details", usage.get("prompt_tokens_details"))
    input_details = input_details if isinstance(input_details, dict) else {}
    output_details = usage.get("output_tokens_details", usage.get("completion_tokens_details"))
    output_details = output_details if isinstance(output_details, dict) else {}
    cached = _count(input_details.get("cached_tokens", usage.get("cached_input_tokens", usage.get("cachedInputTokens"))))
    written = _count(input_details.get("cache_write_tokens",
        usage.get("cache_write_tokens", usage.get("cache_write_input_tokens", usage.get("cacheWriteTokens")))))
    reasoning = _count(output_details.get("reasoning_tokens", usage.get("reasoning_output_tokens", usage.get("reasoningOutputTokens"))))
    result["cachedInputEvidence"] = "known" if input_count is not None and cached is not None and cached <= input_count else "unknown"
    result["cacheWriteEvidence"] = "known" if input_count is not None and written is not None and written <= input_count else "unknown"
    if input_count is not None and cached is not None and written is not None and cached + written > input_count:
        result["cacheWriteEvidence"] = result["cachedInputEvidence"] = "unknown"
    result["reasoningEvidence"] = "known" if output_count is not None and reasoning is not None and reasoning <= output_count else "unknown"
    return result


def enrich_context(context: dict, capture: Any) -> dict:
    """Attach capture.billing_metadata (or a JSON response) to a route context."""
    if isinstance(capture, dict):
        metadata = safe_metadata(capture) if "usageEvidenceVersion" in capture else observe_metadata(None, capture)
    else:
        metadata = safe_metadata(getattr(capture, "billing_metadata", None))
    return {**context, **metadata}
