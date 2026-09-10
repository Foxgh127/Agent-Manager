"""Conservative cross-window comparison of observed, matched workload equivalents."""
from __future__ import annotations
import statistics
from agent_manager.usage.pricing import PRICE_VERSION

FIELDS = ('input', 'cached', 'writes', 'output', 'reasoning', 'requests')
MIN_COMPARISON_SAMPLES = 5
MIN_COMPARISON_DROP = 15
MIN_HISTORY_WINDOWS = 2


def interval_reference(old, new, delta):
    """Subtract additive matched-bucket contributions, never arbitrary intervals."""
    if (old.get('priceVersion') != PRICE_VERSION or new.get('priceVersion') != PRICE_VERSION
            or not old.get('sourceHistoryComplete') or not new.get('sourceHistoryComplete')):
        return None
    a, b = old.get('conditionalReferenceUsd') or {}, new.get('conditionalReferenceUsd') or {}
    if (not a.get('unpricedFingerprint') or a.get('unpricedFingerprint') != b.get('unpricedFingerprint')
            or a.get('unpricedTokens') != b.get('unpricedTokens')
            or old.get('invalidRowCount') != new.get('invalidRowCount')):
        return None
    before, after = a.get('counters'), b.get('counters')
    if not isinstance(before, dict) or not isinstance(after, dict) or len(after) > 128:
        return None
    tokens = 0
    lower = upper = 0.0
    for key in before.keys() | after.keys():
        if key not in after:
            return None
        left, right = before.get(key) or dict(tokens=0, lower=0, upper=0), after[key]
        dt = right['tokens'] - left['tokens']
        dl, du = right['lower'] - left['lower'], right['upper'] - left['upper']
        if dt < 0 or dl < -1e-9 or du < dl - 1e-9 or (dt == 0 and (abs(dl) > 1e-9 or abs(du) > 1e-9)):
            return None
        tokens += dt
        lower += max(0, dl)
        upper += max(0, du)
    if tokens != delta or lower <= 0 or upper <= 0:
        return None
    return {'lower': lower, 'upper': upper, 'priceVersion': PRICE_VERSION}


def interval_workload(old, new, delta):
    """Difference complete dimension counters; absent evidence blocks comparison."""
    before, after = old.get('workloadCounters'), new.get('workloadCounters')
    if not isinstance(before, dict) or not isinstance(after, dict) or not after or len(after) > 32:
        return None
    result = {}
    for key in before.keys() | after.keys():
        a, b = before.get(key), after.get(key)
        if b is None or not b.get('complete') or (a is not None and not a.get('complete')):
            return None
        counts = {field: b.get(field, 0) - (a or {}).get(field, 0) for field in FIELDS}
        if any(not isinstance(value, (int, float)) or value < 0 for value in counts.values()):
            return None
        if counts['input'] + counts['output']:
            if not counts['requests'] or counts['cached'] + counts['writes'] > counts['input'] or counts['reasoning'] > counts['output']:
                return None
            result[key] = counts
    if sum(row['input'] + row['output'] for row in result.values()) != delta:
        return None
    return result or None


def _profile(samples):
    result = {}
    for sample in samples:
        for key, row in sample['workload'].items():
            total = result.setdefault(key, dict.fromkeys(FIELDS, 0))
            for field in FIELDS:
                total[field] += row[field]
    return result


def _comparable(left, right):
    if set(left) != set(right):
        return False
    totals = [sum(row['input'] + row['output'] for row in profile.values()) for profile in (left, right)]
    if min(totals) <= 0:
        return False
    if sum(abs((left[key]['input'] + left[key]['output']) / totals[0] -
               (right[key]['input'] + right[key]['output']) / totals[1]) for key in left) / 2 > .10:
        return False
    for key in left:
        a, b = left[key], right[key]
        for field, denominator in (('cached', 'input'), ('writes', 'input'), ('reasoning', 'output')):
            if abs(a[field] / max(1, a[denominator]) - b[field] / max(1, b[denominator])) > .10:
                return False
        if abs(a['output'] / max(1, a['input'] + a['output']) - b['output'] / max(1, b['input'] + b['output'])) > .10:
            return False
        sizes = [(row['input'] + row['output']) / max(1, row['requests']) for row in (a, b)]
        if min(sizes) <= 0 or max(sizes) / min(sizes) > 1.25:
            return False
    return True


def _window(samples):
    if len(samples) < MIN_COMPARISON_SAMPLES or sum(s.get('drop', 0) for s in samples) < MIN_COMPARISON_DROP:
        return None
    if any(s.get('priceVersion') != PRICE_VERSION or not s.get('usd') or not s.get('workload') for s in samples):
        return None
    profile = _profile(samples)
    if any(not _comparable(profile, s['workload']) for s in samples):
        return None
    ratios = [s['usd'] / s['drop'] for s in samples]
    center = statistics.median(ratios)
    variation = statistics.median(abs(r - center) for r in ratios)
    low = min(statistics.median(s['usd'] / (s['drop'] + 1) for s in samples), max(0, center - 2.5 * variation))
    high = max(statistics.median(s['usd'] / (s['drop'] - 1) for s in samples), center + 2.5 * variation)
    return {'estimate': center * 100, 'lower': low * 100, 'upper': high * 100,
            'profile': profile, 'sampleCount': len(samples)}


def compare_capacity(state):
    result = {'status': 'insufficient_comparable_evidence', 'officialReductionProven': False,
              'minimumSamplesPerWindow': MIN_COMPARISON_SAMPLES, 'minimumDropPerWindow': MIN_COMPARISON_DROP,
              'minimumHistoricalWindows': MIN_HISTORY_WINDOWS, 'comparableWindowCount': 0,
              'reason': 'current_window_evidence_incomplete',
              'limitations': ['other_devices_unknown', 'accounting_delay_unknown', 'tool_usage_unmatched']}
    current = _window(state.get('samples') or [])
    if current is None or not state.get('comparisonScope') or not state.get('workloadScope'):
        return result
    # At most one vote per reset window, even if interrupted capture produced
    # multiple archived segments. Segments are never pooled across reset dates.
    grouped = {}
    for segment in state.get('history') or []:
        reset = segment.get('resetAt')
        if (not reset or reset >= state.get('resetAt', 0) or
                segment.get('comparisonScope') != state['comparisonScope'] or
                segment.get('workloadScope') != state['workloadScope']):
            continue
        grouped.setdefault(reset, []).extend(segment.get('samples') or [])
    windows = []
    for samples in grouped.values():
        candidate = _window(samples)
        if candidate and _comparable(current['profile'], candidate['profile']):
            windows.append(candidate)
    result.update(comparableWindowCount=len(windows), reason='historical_windows_not_comparable_or_insufficient')
    if len(windows) < MIN_HISTORY_WINDOWS:
        return result
    baseline = {key: statistics.median(window[key] for window in windows) for key in ('estimate', 'lower', 'upper')}
    # Baseline range covers all retained comparable windows, not only their median.
    baseline['lower'] = min(w['lower'] for w in windows)
    baseline['upper'] = max(w['upper'] for w in windows)
    if baseline['lower'] <= 0:
        return result
    change = {'estimate': 100 * (current['estimate'] / baseline['estimate'] - 1),
              'lower': 100 * (current['lower'] / baseline['upper'] - 1),
              'upper': 100 * (current['upper'] / baseline['lower'] - 1)}
    decline = change['upper'] < 0
    result.update(status='comparable_capacity_decline_signal' if decline else 'comparable_no_clear_decline',
                  reason='nonoverlapping_observed_ranges' if decline else 'observed_ranges_overlap_or_increase',
                  baselineTotalUsd={k: round(v, 6) for k, v in baseline.items()},
                  currentTotalUsd={k: round(current[k], 6) for k in ('estimate', 'lower', 'upper')},
                  changePercent={k: round(v, 2) for k, v in change.items()},
                  currentSampleCount=current['sampleCount'])
    return result
