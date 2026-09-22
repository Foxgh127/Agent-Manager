"""Passively observe protocol DNA without interpreting declarations as proof."""
import asyncio
import json
import socket
import pytest

from agent_manager.detection.model_fingerprint import (
    ModelFingerprintDetector, format_fingerprint_result, observe_headers, observe_payload, safe_fingerprint,
)


def test_open_source_header_rules_do_not_retain_headers_or_issue_requests(monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("No probe traffic allowed"))
    value = ModelFingerprintDetector.inspect_response(
        {"id": "chatcmpl-secret-request", "model": "claimed", "object": "chat.completion",
         "system_fingerprint": "fp_123", "choices": [{"message": {"content": "private output"}}]},
        {"CF-Ray": "secret-ray", "X-Amzn-RequestId": "private-id", "OpenAI-Processing-Ms": "42",
         "x-envoy-upstream-service-time": "3", "x-request-id": "secret-id", "Server": "cloudflare",
         "authorization": "Bearer secret-key"},
    )
    assert value["detected_model"] == "unknown" and value["confidence"] is None
    assert value["active_probe"] is False and value["diluted"] is False
    fp = value["fingerprint"]
    assert fp["protocols"] == ["openai_chat"]
    assert {"cf-ray", "aws", "envoy", "oai-proc-ms", "server:cloudflare", "request-id", "id:chatcmpl-"} <= set(fp["features"])
    serialized = json.dumps(value)
    for secret in ("secret-ray", "private-id", "secret-id", "secret-key", "private output", "claimed"):
        assert secret not in serialized


def test_normal_responses_provide_fingerprint_even_if_model_and_system_fp_are_omitted():
    result = observe_payload(None, {"type": "response.completed", "response": {
        "id": "resp_private", "output": [{"type": "reasoning", "encrypted_content": "private-encrypted"}],
        "usage": {"input_tokens_details": {"cached_tokens": 0}},
    }})
    assert result["signature"].startswith("pfp_")
    assert result["protocols"] == ["openai_responses"]
    assert {"reasoning:encrypted", "terminal", "usage:openai"} <= set(result["features"])
    assert "private" not in json.dumps(result)


def test_thinking_signature_presence_is_not_identity_or_cryptographic_verification():
    result = observe_payload(None, {"type": "content_block_start", "content_block": {"type": "thinking"}})
    result = observe_payload(result, {"type": "content_block_delta", "delta": {
        "type": "signature_delta", "signature": "unverified-opaque-value",
    }})
    assert result["protocols"] == ["anthropic_messages"]
    assert {"thinking:block", "thinking:signature"} <= set(result["features"])
    assert "unverified-opaque" not in str(result)


def test_declarations_and_self_identification_are_not_fingerprint_rules():
    a = observe_payload(None, {"model": "a", "text": "I am Claude"})
    b = observe_payload(None, {"model": "b", "text": "I am GPT"})
    assert a == b and a["signature"] == ""


def test_safe_projection_drops_arbitrary_labels_and_recomputes_signature():
    result = safe_fingerprint({"version": 1, "signature": "secret", "protocols": ["openai_chat", {}],
                               "features": ["aws", "authorization:secret", {}]})
    assert result["features"] == ["aws"]
    assert "secret" not in str(result)
    assert safe_fingerprint({"version": True})["signature"] == ""


@pytest.mark.parametrize("payload", [None, [], 42, {"type": {}, "object": []},
    {"type": "response.completed", "response": {"output": [{"type": {}}, {"type": []}], "usage": []}}])
def test_malformed_metadata_never_breaks_inference(payload):
    assert isinstance(observe_payload(None, payload), dict)


def test_compatibility_api_never_sends_probe():
    result = asyncio.run(ModelFingerprintDetector().detect_model("unused", "secret", "claimed"))
    assert result["detected_model"] == "unknown"
    assert "未确认" in format_fingerprint_result(result)
    assert "secret" not in str(result)
