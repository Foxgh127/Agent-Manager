"""Passive serving-stack fingerprints; no HTTP client, prompts or probe calls.

Header-presence rules adapted from unclecode/modelprint, net-headerdna.js
(b220f28d2dbb5e8978d6f6b3baff254e543ef09d, author ItIsCuthNotCup, MIT).
Protocol reduction adapted from SailingLoong/LoongPort passive reducers
(91f67aaf79b101ab290b18e2fbfe76abecaed78c, MIT). See resources/licenses
and docs/model-fingerprints.md for provenance and deliberate differences.

These are protocol/stack observations, NOT proof of model weights. In
particular a declared model and the mere presence of a signature do not
identify a model. Only finite feature labels are retained, never text/IDs.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

RULES_VERSION = 1
ENGINE = "modelprint-headerdna+loongport-passive"
PROTOCOLS = frozenset({"openai_responses", "openai_chat", "anthropic_messages", "gemini"})
FEATURES = frozenset({
    "cf-ray", "aws", "envoy", "oai-proc-ms", "oai-version", "ant-rl", "rl-*",
    "server:cloudflare", "server:envoy", "server:nginx", "server:gunicorn", "server:uvicorn",
    "request-id", "id:resp_", "id:chatcmpl-", "id:chatcmpl_", "id:msg_", "id:gen-",
    "system-fingerprint", "reasoning:encrypted", "thinking:block", "thinking:signature",
    "tool:call", "usage:openai", "usage:anthropic", "usage:gemini", "terminal",
})


def _labels(value: Any, allowed: frozenset[str]) -> list[str]:
    return sorted({item for item in value[:64] if isinstance(item, str) and item in allowed}) if isinstance(value, list) else []


def _result(protocols: Any = None, features: Any = None) -> dict:
    protocols, features = _labels(protocols, PROTOCOLS), _labels(features, FEATURES)
    encoded = json.dumps([RULES_VERSION, protocols, features], separators=(",", ":")).encode()
    return {
        "version": RULES_VERSION, "engine": ENGINE,
        "signature": "pfp_" + hashlib.sha256(encoded).hexdigest()[:24] if protocols or features else "",
        "protocols": protocols, "features": features,
    }


def safe_fingerprint(value: Any) -> dict:
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != RULES_VERSION:
        return _result()
    return _result(value.get("protocols"), value.get("features"))


def observe_headers(previous: Any, headers: Any) -> dict:
    """Port only modelprint's pure header parser, never its ctx.chat probe."""
    result = safe_fingerprint(previous)
    features = set(result["features"])
    names = set()
    server = ""
    if headers is not None and hasattr(headers, "items"):
        for index, (key, value) in enumerate(headers.items()):
            if index >= 256:
                break
            if not isinstance(key, str) or len(key) > 128 or not value:
                continue
            name = key.lower()
            names.add(name)
            if name == "server" and isinstance(value, str) and len(value) <= 128:
                server = value.lower().split("/", 1)[0].strip()
    for nameset, label in (
        ({"cf-ray"}, "cf-ray"), ({"x-amzn-requestid", "x-amz-request-id"}, "aws"),
        ({"x-envoy-upstream-service-time"}, "envoy"), ({"openai-processing-ms"}, "oai-proc-ms"),
        ({"openai-version"}, "oai-version"), ({"anthropic-ratelimit-requests-limit"}, "ant-rl"),
        ({"x-request-id"}, "request-id"),
        ({"system-fingerprint", "x-system-fingerprint", "openai-system-fingerprint", "x-openai-system-fingerprint"}, "system-fingerprint"),
    ):
        if names & nameset:
            features.add(label)
    if any(name.startswith("x-ratelimit") for name in names):
        features.add("rl-*")
    if "server:" + server in FEATURES:
        features.add("server:" + server)
    return _result(result["protocols"], list(features))


def observe_payload(previous: Any, payload: Any) -> dict:
    """Reduce an already parsed JSON/SSE event, inspecting protocol fields only."""
    result = safe_fingerprint(previous)
    if not isinstance(payload, dict):
        return result
    protocols, features = set(result["protocols"]), set(result["features"])
    kind = payload.get("type")
    kind = kind if isinstance(kind, str) else ""
    obj = payload.get("object")
    source = payload
    if kind.startswith("response.") or obj == "response" or kind == "response":
        protocols.add("openai_responses")
        if isinstance(payload.get("response"), dict):
            source = payload["response"]
    if obj in ("chat.completion", "chat.completion.chunk") or isinstance(payload.get("choices"), list):
        protocols.add("openai_chat")
    if kind in {"message", "message_start", "message_delta", "message_stop", "content_block_start", "content_block_delta", "content_block_stop"}:
        protocols.add("anthropic_messages")
        if isinstance(payload.get("message"), dict):
            source = payload["message"]
    if isinstance(payload.get("candidates"), list) and isinstance(payload.get("usageMetadata"), dict):
        protocols.add("gemini")
        features.add("usage:gemini")
    response_id = source.get("id")
    if isinstance(response_id, str):
        for prefix in ("resp_", "chatcmpl-", "chatcmpl_", "msg_", "gen-"):
            if response_id.startswith(prefix):
                features.add("id:" + prefix)
                break
    if source.get("system_fingerprint") or source.get("systemFingerprint"):
        features.add("system-fingerprint")
    usage = source.get("usage")
    if isinstance(usage, dict):
        if any(key in usage for key in ("prompt_tokens", "input_tokens_details", "output_tokens_details")):
            features.add("usage:openai")
        if any(key in usage for key in ("cache_read_input_tokens", "cache_creation_input_tokens")):
            features.add("usage:anthropic")
    blocks = [payload.get("item"), payload.get("content_block"), payload.get("delta")]
    for field in ("output", "content"):
        if isinstance(source.get(field), list):
            blocks.extend(source[field][:128])
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        block_type = block_type if isinstance(block_type, str) else ""
        if block_type == "reasoning" and isinstance(block.get("encrypted_content"), str) and block["encrypted_content"]:
            features.add("reasoning:encrypted")
        if block_type == "thinking":
            features.add("thinking:block")
        if block_type in {"thinking", "signature_delta"} and isinstance(block.get("signature"), str) and block["signature"]:
            features.add("thinking:signature")
        if block_type in {"function_call", "tool_use", "input_json_delta"}:
            features.add("tool:call")
    if kind in {"response.completed", "response.incomplete", "message_stop"} or source.get("status") in ("completed", "incomplete"):
        features.add("terminal")
    if isinstance(payload.get("choices"), list) and any(
        isinstance(choice, dict) and choice.get("finish_reason") is not None for choice in payload["choices"][:128]
    ):
        features.add("terminal")
    return _result(list(protocols), list(features))


class ModelFingerprintDetector:
    """Compatibility entry point for passive parsing; never performs I/O."""

    @staticmethod
    def inspect_response(response: Any = None, headers: Any = None, *, claimed_model: str = "") -> dict:
        fingerprint = observe_payload(observe_headers(None, headers), response)
        return {"detected_model": "unknown", "confidence": None, "diluted": False,
                "active_probe": False, "fingerprint": fingerprint, "identity_status": "unknown"}

    async def detect_model(self, endpoint: str, api_key: str, claimed_model: str, timeout: float = 30.0,
                           *, response: Any = None, headers: Any = None) -> dict:
        return self.inspect_response(response, headers, claimed_model=claimed_model)


async def detect_model_fingerprint(endpoint: str, api_key: str, claimed_model: str, timeout: float = 30.0,
                                   *, response: Any = None, headers: Any = None) -> dict:
    return ModelFingerprintDetector.inspect_response(response, headers, claimed_model=claimed_model)


def format_fingerprint_result(result: dict[str, Any]) -> str:
    fingerprint = safe_fingerprint(result.get("fingerprint"))
    return "已采集协议指纹，精确模型未确认" if fingerprint["signature"] else "暂无可识别指纹，精确模型未确认"
