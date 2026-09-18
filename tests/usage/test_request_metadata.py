from agent_manager.usage.request_metadata import (
    enrich_context,
    model_routing_diagnostic,
    observe_metadata,
    observe_response_headers,
    safe_metadata,
)


def response(input_tokens=272000, **details):
    return {"model": "gpt-6-astra", "service_tier": "default", "usage": {
        "input_tokens": input_tokens, "output_tokens": 100,
        "input_tokens_details": {"cached_tokens": 12000, "cache_write_tokens": 0, **details},
        "output_tokens_details": {"reasoning_tokens": 80}}}


def test_boundary_uses_one_request_input_not_output_or_aggregate_tokens():
    short = observe_metadata(None, response())
    long = observe_metadata(None, {"response": response(272001)})
    assert short["contextTier"] == "short" and long["contextTier"] == "long"
    assert short["cacheWriteEvidence"] == short["cachedInputEvidence"] == "known"
    assert short["inputOutputEvidence"] == short["reasoningEvidence"] == "known"
    assert safe_metadata({"inputTokens": 1000000})["contextTier"] == "unknown"


def test_sse_creation_metadata_survives_terminal_usage_only_event():
    initial = observe_metadata(None, {"type": "response.created", "response": {"model": "gpt-5.6-sol", "service_tier": "priority"}})
    terminal = observe_metadata(initial, {"type": "response.completed", "response": {"usage": response()["usage"]}})
    assert terminal["actualModel"] == "gpt-5.6-sol"
    assert terminal["serviceTier"] == "priority" and terminal["contextTier"] == "short"
    assert "usage" not in terminal and "response" not in terminal


def test_missing_cache_or_tier_never_becomes_reported_zero_or_standard():
    result = observe_metadata(None, {"model": "gpt-6-astra", "service_tier": "auto", "usage": {"input_tokens": 10, "output_tokens": 3}})
    assert result["serviceTier"] == result["cacheWriteEvidence"] == result["cachedInputEvidence"] == "unknown"
    assert result["reasoningEvidence"] == "unknown"
    assert result["inputOutputEvidence"] == "known"


def test_invalid_subsets_numbers_and_models_cannot_certify_usage():
    malformed = safe_metadata({"usageEvidenceVersion": 2, "serviceTier": {}, "contextTier": []})
    assert malformed["serviceTier"] == malformed["contextTier"] == "unknown"
    result = observe_metadata(None, response(50, cached_tokens=40, cache_write_tokens=20))
    assert result["cacheWriteEvidence"] == result["cachedInputEvidence"] == "unknown"
    for invalid in (True, -1, 1.5, float("nan"), "infinity"):
        result = observe_metadata(None, response(invalid))
        assert result["inputOutputEvidence"] == "unknown" and result["contextTier"] == "unknown"
    result = observe_metadata(None, {"model": "private\ntext", "instructions": "never retain", "service_tier": {"secret": "never retain"}})
    assert result["actualModel"] == "" and "never retain" not in str(result)


def test_chat_details_and_route_enrichment_are_nonmutating():
    payload = {"model": "gpt-5.6-luna", "service_tier": "flex", "usage": {
        "prompt_tokens": 20, "completion_tokens": 4,
        "prompt_tokens_details": {"cached_tokens": 5, "cache_write_tokens": 3},
        "completion_tokens_details": {"reasoning_tokens": 2}}}
    context = {"accountId": "a", "requestedModel": "alias"}
    result = enrich_context(context, payload)
    assert result["actualModel"] == "gpt-5.6-luna" and result["serviceTier"] == "flex"
    assert result["reasoningEvidence"] == "known"
    assert context == {"accountId": "a", "requestedModel": "alias"}
    assert safe_metadata({"usageEvidenceVersion": 1, "actualModel": "gpt-6-astra", "modelEvidence": "actual"})["modelEvidence"] == "requested"


def test_response_fingerprint_and_header_model_are_bounded_passive_evidence():
    body = observe_metadata(None, {
        "response": {
            "model": "gpt-6-astra",
            "system_fingerprint": "fp_demo_123",
        },
    })
    assert body["actualModel"] == "gpt-6-astra"
    assert body["systemFingerprint"] == "fp_demo_123"
    assert body["fingerprintEvidence"] == "response_body"
    # A header cannot replace a stronger body fact.
    retained = observe_response_headers(body, {
        "openai-model": "gpt-5.6-luna",
        "x-system-fingerprint": "fp_header",
    })
    assert retained["actualModel"] == "gpt-6-astra"
    assert retained["systemFingerprint"] == "fp_demo_123"
    header_only = observe_response_headers(None, {"openai-model": "gpt-5.6-luna", "x-system-fingerprint": "fp_header"})
    assert header_only["usageEvidenceVersion"] == 2
    assert header_only["actualModel"] == "gpt-5.6-luna"
    assert header_only["modelEvidenceSource"] == "response_header"
    assert header_only["fingerprintEvidence"] == "response_header"
    invalid = observe_response_headers(None, {"x-model": "private\nsecret", "x-system-fingerprint": "bad value"})
    assert invalid["actualModel"] == "" and invalid["systemFingerprint"] == ""


def test_route_diagnostic_only_flags_a_response_route_mismatch():
    consistent = enrich_context(
        {"requestedModel": "default", "routedModel": "gpt-6-astra"},
        {"model": "gpt-6-astra"},
    )
    assert consistent["modelRoutingStatus"] == "consistent"
    assert consistent["modelMismatch"] is False
    mismatch = enrich_context(
        {"requestedModel": "gpt-6-astra", "routedModel": "gpt-6-astra"},
        {"model": "gpt-5.6-luna"},
    )
    assert mismatch["modelRoutingStatus"] == "mismatch"
    assert mismatch["modelMismatch"] is True
    assert model_routing_diagnostic({"requestedModel": "gpt-6-astra"})["modelRoutingStatus"] == "unknown"
