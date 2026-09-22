"""Bounded upstream billing evidence; never infer request facts from daily totals.

Only protocol metadata and token counts are retained. This module neither reads
credentials nor stores response text. Missing fields stay explicitly unknown.
"""
from __future__ import annotations

import math
import re
from typing import Any
from agent_manager.detection.model_fingerprint import (
    observe_headers as observe_stack_headers,
    observe_payload as observe_stack_payload,
    safe_fingerprint,
)

EVIDENCE_VERSION = 2
SHORT_CONTEXT_LIMIT = 272_000
IDENTITY_FIELDS = (
    "actualModel", "modelEvidence", "serviceTier", "contextTier",
    "cacheWriteEvidence", "cachedInputEvidence", "inputOutputEvidence", "reasoningEvidence",
    "usageEvidenceVersion", "systemFingerprint", "modelFingerprint", "modelIdentityAuthority",
)
SERVICE_TIERS = {"default", "priority", "fast", "flex", "batch", "ultrafast"}
EVIDENCE_FIELDS = ("cacheWriteEvidence", "cachedInputEvidence", "inputOutputEvidence", "reasoningEvidence")
FINGERPRINT_HEADERS = (
    "system-fingerprint",
    "x-system-fingerprint",
    "openai-system-fingerprint",
    "x-openai-system-fingerprint",
)
MODEL_HEADERS = (
    "openai-model",
    "x-openai-model",
    "x-model",
    "x-codex-model",
)
_UNKNOWN_MODELS = {"", "unknown", "未确定模型"}


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


def _fingerprint(value: Any) -> str:
    """Keep only a short opaque response fingerprint, never arbitrary headers."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+=\-]{0,199}", value) else ""


def _model_or_empty(value: Any) -> str:
    model = _model(value)
    return "" if model.casefold() in _UNKNOWN_MODELS else model


def safe_metadata(value: Any) -> dict:
    """Sanitize already-captured fields, preserving legacy evidence as unknown."""
    value = value if isinstance(value, dict) else {}
    current = type(value.get("usageEvidenceVersion")) is int and value["usageEvidenceVersion"] == EVIDENCE_VERSION
    actual = _model(value.get("actualModel")) if current else ""
    actual = actual if value.get("modelEvidence") == "actual" else ""
    fingerprint = _fingerprint(value.get("systemFingerprint")) if current else ""
    fingerprint_evidence = (
        value.get("fingerprintEvidence")
        if current and value.get("fingerprintEvidence") in {"response_body", "response_header"}
        else "unknown"
    )
    model_evidence_source = (
        value.get("modelEvidenceSource")
        if current and value.get("modelEvidenceSource") in {"response_body", "response_header"}
        else ("response_body" if actual else "unknown")
    )
    return {
        "usageEvidenceVersion": EVIDENCE_VERSION if current else 0,
        "actualModel": actual,
        "modelEvidence": "actual" if actual else "requested",
        "modelEvidenceSource": model_evidence_source,
        "systemFingerprint": fingerprint,
        "fingerprintEvidence": fingerprint_evidence,
        "modelFingerprint": safe_fingerprint(value.get("modelFingerprint")),
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
    result["modelFingerprint"] = observe_stack_payload(result["modelFingerprint"], payload)
    response = payload.get("response")
    source = response if isinstance(response, dict) else payload
    sources = [source]
    if source is not payload:
        sources.append(payload)
    for candidate in sources:
        if "model" in candidate:
            actual = _model(candidate.get("model"))
            if actual:
                result.update(actualModel=actual, modelEvidence="actual", modelEvidenceSource="response_body")
                break
            if not result.get("actualModel"):
                result.update(actualModel="", modelEvidence="requested", modelEvidenceSource="unknown")
    if result.get("fingerprintEvidence") != "response_body":
        for candidate in sources:
            for key in ("system_fingerprint", "systemFingerprint"):
                if key in candidate:
                    fingerprint = _fingerprint(candidate.get(key))
                    if fingerprint:
                        result.update(systemFingerprint=fingerprint, fingerprintEvidence="response_body")
                        break
            if result.get("fingerprintEvidence") == "response_body":
                break
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


def observe_response_headers(previous: Any, headers: Any) -> dict:
    """Capture optional response-side model/fingerprint metadata without retaining arbitrary headers.

    Headers are weaker evidence than a JSON response field.  They are used only
    when the body did not already provide the same fact, and are bounded to a
    small explicit allowlist so arbitrary provider headers never enter storage.
    """
    result = safe_metadata(previous)
    # Header-only responses still carry a complete, current evidence record.
    # Without this marker ``safe_metadata`` would intentionally discard the
    # model/fingerprint when the usage record is persisted.
    result["usageEvidenceVersion"] = EVIDENCE_VERSION
    result["modelFingerprint"] = observe_stack_headers(result["modelFingerprint"], headers)
    if headers is None or not hasattr(headers, "items"):
        return result
    values: dict[str, str] = {}
    try:
        iterator = headers.items()
    except (AttributeError, TypeError):
        return result
    for raw_name, raw_value in iterator:
        name = str(raw_name or "").strip().casefold()
        value = str(raw_value or "").strip()
        if name and value and len(value) <= 512 and "\r" not in value and "\n" not in value:
            values.setdefault(name, value)
    if not result.get("actualModel"):
        for name in MODEL_HEADERS:
            actual = _model(values.get(name))
            if actual:
                result.update(actualModel=actual, modelEvidence="actual", modelEvidenceSource="response_header")
                break
    if not result.get("systemFingerprint"):
        for name in FINGERPRINT_HEADERS:
            fingerprint = _fingerprint(values.get(name))
            if fingerprint:
                result.update(systemFingerprint=fingerprint, fingerprintEvidence="response_header")
                break
    return result


def model_routing_diagnostic(context: dict, metadata: Any = None) -> dict:
    """Compare the configured route with response evidence.

    This is a consistency check only.  A mismatch means the response metadata
    differs from the route target; it does not claim that model quality or
    account standing was reduced by the service.
    """
    source = safe_metadata(metadata if isinstance(metadata, dict) else context)
    requested = _model_or_empty((context or {}).get("requestedModel"))
    routed = _model_or_empty((context or {}).get("routedModel"))
    expected = routed or requested
    actual = _model_or_empty(source.get("actualModel")) if source.get("modelEvidence") == "actual" else ""
    if not actual or not expected:
        status = "unknown"
    elif actual.casefold() == expected.casefold():
        status = "consistent"
    else:
        status = "mismatch"
    return {
        "modelRoutingStatus": status,
        "modelRoutingExpected": expected,
        "modelMismatch": status == "mismatch",
    }


def enrich_context(context: dict, capture: Any) -> dict:
    """Attach capture.billing_metadata (or a JSON response) to a route context."""
    if isinstance(capture, dict):
        metadata = safe_metadata(capture) if "usageEvidenceVersion" in capture else observe_metadata(None, capture)
    else:
        metadata = safe_metadata(getattr(capture, "billing_metadata", None))
    enriched = {**context, **metadata}
    enriched.update(model_routing_diagnostic(enriched, metadata))
    return enriched
