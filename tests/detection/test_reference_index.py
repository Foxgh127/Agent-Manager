from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from agent_manager.detection.reference_index import annotate_identities, MAX_REFERENCE_KEYS


NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)


def reference(**changes):
    row = {
        "usageEvidenceVersion": 2, "modelIdentityAuthority": "official_direct",
        "modelEvidenceSource": "response_body", "modelEvidence": "actual",
        "actualModel": "gpt-sample", "systemFingerprint": "fp_sample",
        "requestCount": 1, "failureCount": 0, "timestamp": NOW.isoformat(),
    }
    row.update(changes)
    return row


def provider(**changes):
    return reference(modelIdentityAuthority="provider", **changes)


def test_official_reference_yields_candidate_without_changing_billing_or_metadata():
    sample = reference()
    row = provider(actualModel="provider-alias", inputTokens=123, apiKey="do-not-copy")
    before = deepcopy(row)
    annotate_identities([sample, row], [], now=NOW)
    assert row["modelIdentity"] == {
        "status": "candidate", "candidates": ["gpt-sample"],
        "method": "official_observation_fingerprint", "referenceCount": 1,
    }
    assert sample["modelIdentity"]["status"] == "reference"
    assert {k: v for k, v in row.items() if k != "modelIdentity"} == before
    assert "do-not-copy" not in str(row["modelIdentity"])


@pytest.mark.parametrize("changes", [
    {"modelIdentityAuthority": "provider"}, {"usageEvidenceVersion": 1},
    {"modelEvidenceSource": "response_header"}, {"modelEvidence": "requested"},
    {"actualModel": ""}, {"systemFingerprint": ""}, {"requestCount": 0},
    {"requestCount": True}, {"failureCount": 1}, {"failureCount": None},
    {"timestamp": (NOW - timedelta(days=31)).isoformat()},
    {"timestamp": (NOW + timedelta(seconds=1)).isoformat()},
    {"timestamp": NOW.replace(tzinfo=None).isoformat()},
    {"timestamp": "broken"}, {"firstSeenAt": (NOW - timedelta(days=31)).isoformat()},
])
def test_untrusted_failed_legacy_or_stale_rows_cannot_train(changes):
    row = provider()
    annotate_identities([row, reference(**changes)], [], now=NOW)
    assert row["modelIdentity"]["status"] == "unknown"
    assert row["modelIdentity"]["referenceCount"] == 0


def test_provider_matching_its_own_claim_has_no_reference():
    row = provider()
    annotate_identities([row], [provider()], now=NOW)
    assert row["modelIdentity"]["status"] == "unknown"


def test_conflicting_models_remain_ambiguous_even_after_candidate_cap():
    row = provider()
    samples = [reference(actualModel=f"model-{i}") for i in range(12)]
    annotate_identities(samples, [row], now=NOW)
    assert row["modelIdentity"]["status"] == "ambiguous"
    assert len(row["modelIdentity"]["candidates"]) == 8
    assert row["modelIdentity"]["referenceCount"] == 12


def test_key_overflow_abstains_for_all_rows():
    row = provider()
    samples = [reference(systemFingerprint=f"fp_{i}") for i in range(MAX_REFERENCE_KEYS + 1)]
    annotate_identities(samples, [row], now=NOW)
    assert row["modelIdentity"]["status"] == "unknown"
    assert all(s["modelIdentity"]["status"] == "unknown" for s in samples)


def test_exact_30_day_boundary_is_valid_and_recent_duplicate_is_not_counted():
    sample = reference(timestamp=(NOW - timedelta(days=30)).isoformat())
    aggregate = dict(sample, requestCount=10)
    row = provider()
    annotate_identities([aggregate, row], [sample], now=NOW)
    assert row["modelIdentity"]["referenceCount"] == 10
    assert aggregate["modelIdentity"]["status"] == "reference"


def test_reannotation_drops_old_candidates_without_global_state():
    row = provider()
    annotate_identities([row, reference()], [], now=NOW)
    annotate_identities([row], [], now=NOW)
    assert row["modelIdentity"]["status"] == "unknown"


def test_legacy_target_and_invalid_tokens_abstain():
    rows = [provider(usageEvidenceVersion=1), provider(systemFingerprint="fp_sample\nkey: secret")]
    annotate_identities([reference(), *rows], [], now=NOW)
    assert all(row["modelIdentity"]["status"] == "unknown" for row in rows)


def test_recent_official_rows_cannot_train_the_aggregate_baseline():
    row = provider()
    annotate_identities([row], [reference()], now=NOW)
    assert row["modelIdentity"]["status"] == "unknown"


def test_aggregate_prefers_last_seen_at_over_old_timestamp():
    row = provider()
    sample = reference(lastSeenAt=NOW.isoformat(), timestamp=(NOW - timedelta(days=31)).isoformat())
    annotate_identities([sample], [row], now=NOW)
    assert row["modelIdentity"]["status"] == "candidate"
