"""Evidence-based token API equivalents, never subscription balances.
Official pricing/model/prompt-caching pages fetched 2026-09-10.
"""
from __future__ import annotations
import hashlib
import json
import math

PRICE_VERSION = 'openai-context-evidence-2026-09-10-v2'
PRICE_SOURCE = 'https://developers.openai.com/api/docs/pricing'
CONTEXT_THRESHOLD = 272_000
RATES = {
    'gpt-6-astra': (10.0, 1.0, 50.0),
    'gpt-5.6-sol': (4.0, 0.4, 20.0),
    'gpt-5.6-terra': (2.0, 0.2, 12.0),
    'gpt-5.6-luna': (0.2, 0.02, 1.2),
}
SERVICE_MULTIPLIERS = {'default': 1.0, 'fast': 2.0, 'flex': 0.5, 'batch': 0.5}


def _counter(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return int(number) if math.isfinite(number) and number >= 0 and number.is_integer() else None
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_context_tier(request_input_tokens):
    count = _counter(request_input_tokens)
    return 'unknown' if count is None else 'long' if count > CONTEXT_THRESHOLD else 'short'


def _conditional_cost(row, counts, model):
    """Additive per-bucket bounds conditional on listed public tiers/model.

    Unknown service tier is NOT claimed to have an unconditional upper bound.
    Ultrafast and unknown models cannot be represented by this reference.
    """
    if model not in RATES or any(v is None for v in counts) or row.get('usageMissingCount'):
        return None
    if row.get('inputOutputEvidence') == 'unknown':
        return None
    inputs, cached, output = counts
    if cached > inputs:
        return None
    tier = str(row.get('serviceTier') or 'unknown').casefold()
    tier = {'priority': 'fast', 'standard': 'default'}.get(tier, tier)
    if tier not in {*SERVICE_MULTIPLIERS, 'unknown', 'auto'}:
        return None
    tiers = [SERVICE_MULTIPLIERS[tier]] if tier in SERVICE_MULTIPLIERS else [.5, 1, 2]
    contexts = [row['contextTier']] if row.get('contextTier') in {'short', 'long'} else ['short', 'long']
    cache_known = row.get('cachedInputEvidence') == 'known'
    write_known = row.get('cacheWriteEvidence') == 'known' and _counter(row.get('cacheWriteTokens')) is not None
    writes = _counter(row.get('cacheWriteTokens')) if write_known else 0
    if cached + writes > inputs:
        return None
    # Linear disjoint input rates attain extrema at these feasible vertices.
    vertices = []
    for read in ([cached] if cache_known else [0, inputs - writes]):
        for write in ([writes] if write_known else [0, inputs - read]):
            vertices.append((read, write))
    costs = []
    for multiplier in tiers:
        for context in contexts:
            a, b, c = RATES[model]
            a *= multiplier * (2 if context == 'long' else 1)
            b *= multiplier * (2 if context == 'long' else 1)
            c *= multiplier * (1.5 if context == 'long' else 1)
            costs.extend(((inputs - read - write) * a + read * b + write * a * 1.25 + output * c) / 1_000_000
                         for read, write in vertices)
    key = '|'.join((model, tier, str(row.get('contextTier') or 'unknown'),
                    str(cache_known), str(write_known), str(bool(row.get('actualModel')))))
    return key, min(costs), max(costs)


def equivalent(rows: list[dict]) -> dict:
    """Price evidence-partitioned aggregates; never derive context from their sum.

    Cached reads and writes are disjoint input subsets, reasoning an output
    subset. Unknown historical evidence remains visible and can cancel at
    paired endpoints; it is never silently short context, standard tier or zero.
    """
    total = 0.0
    priced_tokens = unknown_tokens = invalid_rows = legacy_rows = 0
    unknown_models, reasons, unknown_evidence = set(), set(), []
    details, workloads, conditional = {}, {}, {}
    conditional_unknown = []
    conditional_unpriced_tokens = 0
    for row in rows:
        if not isinstance(row, dict):
            invalid_rows += 1
            continue
        model = str(row.get('actualModel') or row.get('routedModel') or row.get('model') or 'unknown')[:160]
        counts = [_counter(row.get(field)) for field in ('inputTokens', 'cachedInputTokens', 'outputTokens')]
        issue = None
        if any(value is None for value in counts) or counts[1] > counts[0] or row.get('usageMissingCount'):
            invalid_rows += 1
            issue = 'invalid_or_missing_usage'
        if row.get('usageEvidenceVersion') != 2:
            legacy_rows += 1
        tokens = (counts[0] or 0) + (counts[2] or 0)
        reference = _conditional_cost(row, counts, model)
        if reference and tokens:
            key, lower, upper = reference
            item = conditional.setdefault(key, dict(tokens=0, lower=0.0, upper=0.0))
            item['tokens'] += tokens
            item['lower'] += lower
            item['upper'] += upper
        elif tokens or issue:
            conditional_unpriced_tokens += tokens
            conditional_unknown.append([model, counts, row.get('usageMissingCount'), row.get('serviceTier'),
                                        row.get('inputOutputEvidence')])
        tier = str(row.get('serviceTier') or 'unknown').casefold()
        tier = {'priority': 'fast', 'standard': 'default'}.get(tier, tier)
        context = row.get('contextTier')
        writes = _counter(row.get('cacheWriteTokens'))
        rates = RATES.get(model)
        if not issue and rates is None:
            unknown_models.add(model)
            issue = 'unknown_model_price'
        if not issue and (not row.get('actualModel') or row.get('modelEvidence') == 'requested'):
            issue = 'actual_model_unknown'
        if not issue and tier not in SERVICE_MULTIPLIERS:
            issue = 'service_tier_unknown_or_unpriced'
        if not issue and context not in {'short', 'long'}:
            issue = 'request_context_unknown'
        if not issue and (writes is None or row.get('cacheWriteEvidence') != 'known'):
            issue = 'cache_writes_unknown'
        if not issue and (row.get('inputOutputEvidence') != 'known' or row.get('cachedInputEvidence') != 'known'):
            issue = 'token_subset_evidence_unknown'
        if not issue and counts[1] + writes > counts[0]:
            invalid_rows += 1
            issue = 'cache_subsets_exceed_input'
        if issue:
            unknown_tokens += tokens
            reasons.add(issue)
            # Evidence only: never prompt text, credentials or request IDs.
            unknown_evidence.append([model, tier, context, counts, writes,
                                     row.get('cacheWriteEvidence'), row.get('usageMissingCount'),
                                     row.get('requestCount', row.get('requests'))])
            continue
        if not tokens:
            continue
        input_tokens, cached, output = counts
        multiplier = SERVICE_MULTIPLIERS[tier]
        input_rate = rates[0] * multiplier * (2 if context == 'long' else 1)
        cached_rate = rates[1] * multiplier * (2 if context == 'long' else 1)
        output_rate = rates[2] * multiplier * (1.5 if context == 'long' else 1)
        parts = ((input_tokens - cached - writes) * input_rate / 1_000_000,
                 cached * cached_rate / 1_000_000, writes * input_rate * 1.25 / 1_000_000,
                 output * output_rate / 1_000_000)
        total += sum(parts)
        priced_tokens += tokens
        key = '|'.join((model, tier, context))
        entry = details.setdefault(key, {'model': model, 'serviceTier': tier, 'contextTier': context,
                                        'uncachedInputUsd': 0.0, 'cachedInputUsd': 0.0,
                                        'cacheWriteUsd': 0.0, 'outputUsd': 0.0})
        for field, value in zip(('uncachedInputUsd', 'cachedInputUsd', 'cacheWriteUsd', 'outputUsd'), parts):
            entry[field] += value
        load = workloads.setdefault(key, dict(input=0, cached=0, writes=0, output=0, reasoning=0, requests=0,
                                              complete=True))
        reasoning = _counter(row.get('reasoningOutputTokens'))
        requests = _counter(row.get('requestCount', row.get('requests')))
        load['complete'] = (load['complete'] and reasoning is not None and reasoning <= output and bool(requests)
                            and row.get('reasoningEvidence') == 'known')
        for field, value in zip(('input', 'cached', 'writes', 'output', 'reasoning', 'requests'),
                                (input_tokens, cached, writes, output, reasoning or 0, requests or 0)):
            load[field] += value
    status = 'unknown' if not priced_tokens else 'partial' if unknown_evidence or invalid_rows else 'available'
    fingerprint = hashlib.sha256(json.dumps(sorted(unknown_evidence, key=lambda x: json.dumps(x)),
                                            sort_keys=True).encode()).hexdigest()
    return {'status': status, 'currency': 'USD', 'kind': 'reference_api_equivalent',
            'actualBalance': False, 'priceVersion': PRICE_VERSION, 'priceSource': PRICE_SOURCE,
            'priceCheckedAt': '2026-09-10', 'priceBasis': 'observed_model_context_service_cache_tokens',
            'knownUsd': round(total, 9) if priced_tokens else None,
            'pricedTokens': priced_tokens, 'unpricedTokens': unknown_tokens,
            'unknownModels': sorted(unknown_models), 'invalidRowCount': invalid_rows,
            'legacyRowCount': legacy_rows, 'uncertaintyReasons': sorted(reasons),
            'unpricedFingerprint': fingerprint, 'workloadCounters': workloads,
            'conditionalReferenceUsd': {
                'lower': sum(v['lower'] for v in conditional.values()) if conditional else None,
                'upper': sum(v['upper'] for v in conditional.values()) if conditional else None,
                'pricedTokens': sum(v['tokens'] for v in conditional.values()),
                'unpricedTokens': conditional_unpriced_tokens, 'counters': conditional,
                'unpricedFingerprint': hashlib.sha256(json.dumps(sorted(conditional_unknown, key=lambda x: json.dumps(x))).encode()).hexdigest(),
                'assumptions': ['recorded_model_is_served_model', 'only_standard_fast_flex_batch',
                                'unknown_context_short_or_long', 'unknown_cache_subsets_within_input'],
            },
            'excludedCosts': ['tools', 'regional_processing', 'unobserved_modalities'],
            'breakdown': [{key: round(value, 9) if isinstance(value, float) else value
                           for key, value in row.items()} for row in details.values()]}


def snapshot_equivalent(account_id: str, snapshot: dict) -> dict:
    # Day routes are complete retained aggregates. recentRequests duplicates them.
    rows, dates = [], []
    for date, day in (snapshot.get('days') or {}).items():
        for row in (day.get('routes') or {}).values():
            if isinstance(row, dict) and str(row.get('accountId') or '') == account_id:
                rows.append(row)
                dates.append(str(date))
    sessions = snapshot.get('codexSessions') or {}
    for row in sessions.get('records') or []:
        if (isinstance(row, dict) and str(row.get('accountId') or '') == account_id
                and row.get('agentRole') == 'mainAgent'
                and str(row.get('provider') or '').casefold() in {'openai', 'chatgpt'}):
            rows.append(row)
            if row.get('date'):
                dates.append(str(row['date']))
    result = equivalent(rows)
    coverage = sessions.get('coverage') or {}
    result.update(scope='retained_native_direct_main_plus_gateway_reported',
                  sourceHistoryComplete=not any(coverage.get(key) for key in
                                               ('partialFiles', 'partialHistory', 'pendingBytes')),
                  observedAt=snapshot.get('updatedAt') or sessions.get('updatedAt'),
                  firstDate=min(dates) if dates else None, lastDate=max(dates) if dates else None)
    return result
