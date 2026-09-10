"""Offline, versioned standard-short-context API equivalents, not bills.

Rates verified 2026-09-10 from https://developers.openai.com/api/docs/pricing
and the gpt-6-astra / gpt-5.6-sol model pages. Aggregated logs do not prove
per-request context tier, cache writes or service tier: this intentionally
computes a named reference-price equivalent, never actual API charges.
"""
from __future__ import annotations

import math

PRICE_VERSION = 'openai-standard-short-2026-09-10'
PRICE_SOURCE = 'https://developers.openai.com/api/docs/pricing'
# USD per million input, cached input, output. Exact IDs only; no guessing aliases.
RATES = {
    'gpt-6-astra': (10.0, 1.0, 50.0),
    'gpt-5.6-sol': (4.0, 0.4, 20.0),
    'gpt-5.6-terra': (2.0, 0.2, 12.0),
    'gpt-5.6-luna': (0.2, 0.02, 1.2),
}


def _counter(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return int(number) if math.isfinite(number) and number >= 0 and number.is_integer() else None
    except (TypeError, ValueError, OverflowError):
        return None


def equivalent(rows: list[dict]) -> dict:
    """Value measured token types; cached input/reasoning are subsets."""
    total = 0.0
    priced_tokens = unknown_tokens = 0
    unknown_models = set()
    details = {}
    invalid_rows = 0
    for row in rows:
        model = str(row.get('routedModel') or row.get('model') or 'unknown')[:160]
        counts = [_counter(row.get(field)) for field in ('inputTokens', 'cachedInputTokens', 'outputTokens')]
        if any(value is None for value in counts) or counts[1] > counts[0] or row.get('usageMissingCount'):
            invalid_rows += 1
            continue
        input_tokens, cached, output = counts
        tokens = input_tokens + output
        if not tokens:
            continue
        rates = RATES.get(model)
        if rates is None:
            unknown_tokens += tokens
            unknown_models.add(model)
            continue
        parts = ((input_tokens - cached) * rates[0] / 1_000_000,
                 cached * rates[1] / 1_000_000, output * rates[2] / 1_000_000)
        total += sum(parts)
        priced_tokens += tokens
        entry = details.setdefault(model, {'model': model, 'uncachedInputUsd': 0.0,
                                          'cachedInputUsd': 0.0, 'outputUsd': 0.0})
        for key, value in zip(('uncachedInputUsd', 'cachedInputUsd', 'outputUsd'), parts):
            entry[key] += value
    status = 'unknown' if not priced_tokens else 'partial' if unknown_tokens or invalid_rows else 'available'
    return {'status': status, 'currency': 'USD', 'kind': 'reference_api_equivalent',
            'actualBalance': False, 'priceVersion': PRICE_VERSION, 'priceSource': PRICE_SOURCE,
            'priceCheckedAt': '2026-09-10', 'priceBasis': 'standard_short_context_no_cache_writes',
            'knownUsd': round(total, 6) if priced_tokens else None,
            'pricedTokens': priced_tokens, 'unpricedTokens': unknown_tokens,
            'unknownModels': sorted(unknown_models), 'invalidRowCount': invalid_rows,
            'breakdown': [{key: round(value, 6) if isinstance(value, float) else value
                           for key, value in row.items()} for row in details.values()]}


def snapshot_equivalent(account_id: str, snapshot: dict) -> dict:
    # Gateway day routes are complete retained aggregates, unlike recentRequests.
    rows = []
    dates = []
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
    result.update(scope='retained_native_direct_main_plus_gateway_reported',
                  sourceHistoryComplete=not bool((sessions.get('coverage') or {}).get('partialFiles')),
                  observedAt=snapshot.get('updatedAt') or sessions.get('updatedAt'),
                  firstDate=min(dates) if dates else None, lastDate=max(dates) if dates else None)
    return result
