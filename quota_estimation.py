"""Conditional, empirical quota calibration; never an official token allowance.

Integration contract
--------------------
``prepare_observations(attribution, observed_at=now_iso,
coverage_complete=..., coverage_epoch=...)`` scans an ALREADY computed account
attribution snapshot once. Pass each resulting account observation to
``observe(account, observation)``. ``account['usage']`` must be the fresh official
quota response, with updatedAt and weekly.remainingPercent/resetAt/windowMinutes.
The cheap observe path does not scan rollout files or call the network.

coverage_complete MUST establish that local indexing and usage capture completed;
the attribution snapshot currently does not itself establish that. coverage_epoch
must change after index rebuild/backfill, activation attribution changes, retention
changes or counters being replaced. A stable nonempty epoch identifies one
monotonic counter series. Set false/omit the epoch when this cannot be established.
The caller must use a real sample time, not the timestamp of the last token event.

For a completed existing index, a conservative integration epoch hashes cache
schemaVersion + activationFingerprint + retentionStart. Do NOT hash mtime, size,
cursor, token totals or pendingBytes: normal appends must not start a new series.
An explicit scanner counterGeneration is additionally needed to detect silent
reparse/backfill. Current recentRequests cannot establish live-interval coverage:
global/per-file truncation, aggregate fallbacks and absent completion watermarks
prevent proving that a time-bounded list is complete. With partialFiles > 0 keep
coverage_complete=False and show measured totals while awaiting paired coverage.

Local raw tokens are input + output (cached input/reasoning are subsets). The fit
is a robust median of nonoverlapping interval token/percentage-drop ratios, with
1 percentage-point endpoint quantization uncertainty. Three usable intervals and
6 percentage points of decline are required. Ranges describe quantization and
observed workload variation, NOT a statistical confidence interval. Unknown
other-device use, changing model/Fast/cache/reasoning/output mixes and delayed
quota accounting make the true all-device token budget unidentifiable. Estimates
are conditional workload-equivalent quantities; confidence never exceeds medium.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import tempfile
import threading
import time
from typing import Any

FILE_NAME = 'quota-estimation-v99.json'
SCHEMA = 1
MAX_ACCOUNTS = 256
MAX_SAMPLES = 48
MAX_FILE_BYTES = 2_000_000
MAX_AGE_SECONDS = 900
MAX_ALIGNMENT_SECONDS = 120
QUANTIZATION_PP = 1.0
MIN_SAMPLES = 3
MIN_DROP_PP = 6.0
_LOCK = threading.RLock()


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return _number(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _identity(account: dict) -> str:
    return _hash([account['id'], account.get('fingerprint'), account.get('accountId'),
                  account.get('principalId')])


def _scope(account: dict, usage: dict, weekly: dict, plan: str, reset_at: float,
           duration: float) -> str:
    from subscription_metadata import normalize_plan_label
    label = normalize_plan_label(str(plan))
    canonical_plan = "Pro 20x" if label == "Pro" else label
    return _hash([canonical_plan.casefold(), account.get('subscriptionStartedAt'),
                  usage.get('subscriptionExpiresAt') or account.get('subscriptionExpiresAt'),
                  weekly.get('limitId') or 'codex', reset_at, duration])


def _path() -> Path:
    # Resolve at call time: tests and portable installations change STATE_DIR.
    import agent_manager_core as core
    return Path(core.STATE_DIR) / FILE_NAME


@contextmanager
def _file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a+b') as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b'\0')
            handle.flush()
        deadline = time.monotonic() + 0.5
        acquired = False
        try:
            while not acquired:
                try:
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('quota estimation store busy')
                    time.sleep(0.01)
            yield
        finally:
            if acquired:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load(path: Path) -> dict:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
        if value.get('schemaVersion') != SCHEMA or not isinstance(value.get('accounts'), dict):
            return {}
        return {key: item for key, item in value['accounts'].items()
                if isinstance(key, str) and len(key) == 64 and isinstance(item, dict)}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _save(path: Path, accounts: dict) -> None:
    accounts = dict(sorted(accounts.items(), key=lambda pair: _number(pair[1].get('lastAt')) or 0,
                           reverse=True)[:MAX_ACCOUNTS])
    encoded = json.dumps({'schemaVersion': SCHEMA, 'accounts': accounts}, allow_nan=False,
                         separators=(',', ':')).encode('utf-8')
    if len(encoded) > MAX_FILE_BYTES:
        raise ValueError('quota estimation store too large')
    fd, temporary = tempfile.mkstemp(prefix='.quota-estimation-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def prepare_observations(attribution: dict, *, observed_at: Any,
                         coverage_complete: bool = False, coverage_epoch: str = '') -> dict[str, dict]:
    """One pass over account-attributed counters; never infer missing capture as zero.

    This returns observations only for represented accounts. For an account with
    verified zero local activity, callers may explicitly supply a zero cumulative
    observation under the same contract; absent accounts are not assumed unused.
    """
    if not isinstance(attribution, dict) or not isinstance(attribution.get('records'), list):
        return {}
    coverage = attribution.get('coverage') or {}
    complete = bool(coverage_complete and coverage_epoch and
                    not coverage.get('partialFiles') and not coverage.get('pendingBytes') and
                    not coverage.get('partialHistory'))
    result: dict[str, dict] = {}
    for record in attribution['records']:
        if not isinstance(record, dict) or not record.get('accountId'):
            continue
        account_id = str(record['accountId'])
        observation = result.setdefault(account_id, {
            'accountId': account_id, 'observedAt': observed_at,
            'cumulativeTokens': 0, 'coverageComplete': complete,
            'coverageEpoch': str(coverage_epoch)[:256], 'usageMissingCount': 0,
        })
        tokens = _number(record.get('totalTokens'))
        missing = _number(record.get('usageMissingCount', 0))
        if tokens is None or tokens < 0 or missing is None or missing < 0:
            observation['coverageComplete'] = False
        else:
            observation['cumulativeTokens'] += int(tokens)
            observation['usageMissingCount'] += int(missing)
            if missing > 0:
                observation['coverageComplete'] = False
    return result


def _base(reason: str, remaining: float | None = None) -> dict:
    return {
        'schemaVersion': SCHEMA, 'status': 'pending', 'reason': reason,
        'kind': 'conditional_workload_equivalent', 'officialTokenLimit': False,
        'unit': 'tokens', 'displayUnit': 'K tokens', 'tokensPerK': 1000,
        'remainingPercent': remaining, 'sampleCount': 0, 'rejectedSampleCount': 0,
        'confidence': 'none', 'localObservedTokens': None,
        'minimumSampleCount': MIN_SAMPLES, 'minimumQuotaDropPercent': MIN_DROP_PP,
        'localCumulativeTokens': None, 'estimatedUsedTokens': None,
        'localCumulativeKTokens': None, 'localCoverageComplete': None,
        'estimatedRemainingTokens': None, 'estimatedTotalTokens': None,
        'rangeMeaning': 'quantization_and_observed_workload_variation_not_confidence_interval',
        'assumptions': ['local_capture_complete_for_sample_intervals',
                        'other_device_usage_unknown', 'future_workload_mix_similar',
                        'model_fast_cache_reasoning_output_weights_unknown',
                        'official_accounting_delay_unknown'],
    }


def _measurement(account: dict, local: Any) -> dict | None:
    if not isinstance(local, dict) or str(local.get('accountId') or '') != str(account.get('id') or ''):
        return None
    tokens, at = _number(local.get('cumulativeTokens')), _epoch(local.get('observedAt'))
    if tokens is None or tokens < 0 or at is None:
        return None
    return {'tokens': int(tokens), 'at': at,
            'complete': local.get('coverageComplete') is True and not local.get('usageMissingCount')}


def _with_measurement(result: dict, measurement: dict | None) -> dict:
    if isinstance(measurement, dict) and _number(measurement.get('tokens')) is not None:
        result.update(localCumulativeTokens=measurement['tokens'],
                      localCumulativeKTokens=measurement['tokens'] / 1000,
                      localObservedAt=measurement.get('at'),
                      localCoverageComplete=measurement.get('complete') is True)
    return result


def _pending_observation(account: dict, local: Any, reason: str,
                         remaining: float | None = None) -> dict:
    """Keep measured totals available even when the official quota cannot be paired."""
    measurement = _measurement(account, local)
    result = _with_measurement(_base(reason, remaining), measurement)
    if measurement is None:
        return result
    try:
        path = _path()
        with _LOCK, _file_lock(path):
            accounts = _load(path)
            state = accounts.setdefault(_identity(account), {})
            old = state.get('measurement') if isinstance(state.get('measurement'), dict) else {}
            if measurement['at'] >= (_number(old.get('at')) or 0):
                state['measurement'] = measurement
                if 'anchorAt' not in state:
                    state.setdefault('pendingReason', reason)
                state.setdefault('lastAt', measurement['at'])
                _save(path, accounts)
    except (OSError, TimeoutError, ValueError, TypeError):
        pass
    return result


def _fit(samples: list[dict]) -> tuple[list[dict], int]:
    ratios = [item['tokens'] / item['drop'] for item in samples]
    logs = [math.log(value) for value in ratios]
    center = statistics.median(logs)
    mad = statistics.median(abs(value - center) for value in logs)
    threshold = max(math.log(3), 3 * 1.4826 * mad)
    kept = [item for item, value in zip(samples, logs) if abs(value - center) <= threshold]
    return kept, len(samples) - len(kept)


def _summary(state: dict, remaining: float, cumulative: float) -> dict:
    samples = state['samples']
    result = _base('insufficient_intervals', remaining)
    result.update(localObservedTokens=max(0, int(cumulative - state['startTokens'])),
                  localCumulativeTokens=int(cumulative), localCumulativeKTokens=cumulative / 1000,
                  localCoverageComplete=True, observedAt=state['lastAt'],
                  calibrationStartedAt=state['startAt'], windowResetAt=state['resetAt'],
                  sampleCount=len(samples), minimumSampleCount=MIN_SAMPLES,
                  minimumQuotaDropPercent=MIN_DROP_PP)
    if not samples:
        return result
    kept, rejected = _fit(samples)
    result.update(sampleCount=len(kept), rejectedSampleCount=rejected,
                  quotaDropPercent=sum(item['drop'] for item in kept))
    if len(kept) < MIN_SAMPLES or result['quotaDropPercent'] < MIN_DROP_PP:
        return result
    per_point = statistics.median(item['tokens'] / item['drop'] for item in kept)
    deviation = statistics.median(abs(item['tokens'] / item['drop'] - per_point) for item in kept)
    lower = min(statistics.median(item['tokens'] / (item['drop'] + QUANTIZATION_PP)
                                  for item in kept), max(0, per_point - 2.5 * deviation))
    upper = max(statistics.median(item['tokens'] / (item['drop'] - QUANTIZATION_PP)
                                  for item in kept), per_point + 2.5 * deviation)
    spread = (upper - lower) / per_point
    def quantity(percent: float, uncertainty: float = 0) -> dict:
        return {'estimate': round(per_point * percent),
                'lower': math.floor(lower * max(0, percent - uncertainty)),
                'upper': math.ceil(upper * min(100, percent + uncertainty))}
    result.update(status='calibrated', reason='conditional_estimate',
                  confidence='medium' if len(kept) >= 5 and result['quotaDropPercent'] >= 15
                  and spread <= 1 and not rejected else 'low',
                  estimatedTotalTokens=quantity(100),
                  estimatedUsedTokens=quantity(100 - remaining, QUANTIZATION_PP),
                  estimatedRemainingTokens=quantity(remaining, QUANTIZATION_PP),
                  workloadVariationRatio=round(spread, 4))
    return result


def read_summary(account: dict, *, now: float | None = None, _states: dict | None = None) -> dict:
    """Read the last paired estimate and latest measured totals, without mutations.

    Safe for card/UI polling: no rollout scan, network request, file write, new
    sample, or calibration deletion. A fresh same-window declining quota may be
    projected using the existing fit, with quotaProjectionOnly/as-of timestamps;
    this does not train the fit. Stale quota, regain or changed scope hides
    estimates, while measured local totals remain visible. Age is exposed.
    """
    now = time.time() if now is None else now
    if not isinstance(account, dict) or not account.get('id'):
        return _base('account_identity_unavailable')
    # Writers replace atomically, so a read needs no process lock or mkdir.
    with _LOCK:
        states = _states if _states is not None else _load(_path())
        state = states.get(_identity(account), {})
    measurement = state.get('measurement')
    if not measurement and _number(state.get('lastTokens')) is not None:
        measurement = {'tokens': state['lastTokens'], 'at': state.get('lastLocalAt'),
                       'complete': not state.get('pendingReason')}
    def pending(reason, remaining=None):
        return _with_measurement(_base(reason, remaining), measurement)
    usage = account.get('usage') if isinstance(account.get('usage'), dict) else {}
    weekly = usage.get('weekly') if isinstance(usage.get('weekly'), dict) else {}
    remaining = _number(weekly.get('remainingPercent'))
    remote_at, reset_at = _epoch(usage.get('updatedAt')), _epoch(weekly.get('resetAt'))
    duration = _number(weekly.get('windowMinutes'))
    if remaining is None or remote_at is None or reset_at is None or not duration or duration <= 0:
        return pending('official_quota_unavailable')
    remaining = min(100.0, max(0.0, remaining))
    if (weekly.get('stale') or usage.get('stale') or now - remote_at > MAX_AGE_SECONDS
            or remote_at > now + 5 or reset_at <= remote_at or reset_at <= now):
        return pending('official_quota_stale', remaining)
    plan = usage.get('planRaw') or usage.get('plan') or account.get('planRaw') or account.get('plan')
    if not plan or str(plan).casefold() in {'unknown', 'unavailable'}:
        return pending('plan_unavailable', remaining)
    if not state:
        return pending('awaiting_paired_observations', remaining)
    if state.get('scope') and state.get('scope') != _scope(account, usage, weekly, plan, reset_at, duration):
        return pending('quota_window_or_plan_changed', remaining)
    if state.get('pendingReason'):
        return pending(state['pendingReason'], remaining)
    if remote_at < (_number(state.get('lastAt')) or 0):
        return pending('awaiting_paired_observations', remaining)
    if remaining > (_number(state.get('lastRemaining')) or 0):
        return pending('quota_regained', remaining)
    try:
        result = _with_measurement(_summary(state, remaining, state['lastTokens']), measurement)
        result.update(calibrationObservedAt=state['lastAt'], quotaObservedAt=remote_at,
                      quotaProjectionOnly=remote_at != state['lastAt'])
        return result
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return pending('invalid_calibration_state', remaining)


def read_summaries(accounts: list[dict], *, now: float | None = None) -> dict[str, dict]:
    with _LOCK:
        states = _load(_path())
    return {str(account["id"]): read_summary(account, now=now, _states=states)
            for account in accounts if isinstance(account, dict) and account.get("id")}


def observe(account: dict, usage_snapshot: dict | None, *, now: float | None = None) -> dict:
    """Record one paired quota/counter observation; safe to call for each refresh.

    usage_snapshot = {accountId: account['id'], observedAt: UTC ISO or epoch,
      cumulativeTokens: raw locally attributed total, coverageComplete: True,
      coverageEpoch: stable nonempty counter-generation id, usageMissingCount: 0}.
    Returns pending (null estimates) on unknown/stale/misaligned evidence. Each
    record is scoped to local account id + principal fingerprint, plan/subscription
    and quota reset window. Secrets/identity strings are never persisted.
    ``now`` exists for deterministic tests; production callers should omit it.
    """
    now = time.time() if now is None else now
    if not isinstance(account, dict) or not account.get('id'):
        return _base('account_identity_unavailable')
    usage = account.get('usage') if isinstance(account.get('usage'), dict) else {}
    weekly = usage.get('weekly') if isinstance(usage.get('weekly'), dict) else {}
    remaining = _number(weekly.get('remainingPercent'))
    remote_at = _epoch(usage.get('updatedAt'))
    reset_at = _epoch(weekly.get('resetAt'))
    duration = _number(weekly.get('windowMinutes'))
    if remaining is None or remote_at is None or reset_at is None or not duration or duration <= 0:
        return _pending_observation(account, usage_snapshot, 'official_quota_unavailable')
    remaining = min(100.0, max(0.0, remaining))
    if (weekly.get('stale') or usage.get('stale') or now - remote_at > MAX_AGE_SECONDS
            or remote_at > now + 5 or reset_at <= remote_at or reset_at <= now):
        return _pending_observation(account, usage_snapshot, 'official_quota_stale', remaining)
    plan = usage.get('planRaw') or usage.get('plan') or account.get('planRaw') or account.get('plan')
    if not plan or str(plan).casefold() in {'unknown', 'unavailable'}:
        return _pending_observation(account, usage_snapshot, 'plan_unavailable', remaining)
    identity = _identity(account)
    scope = _scope(account, usage, weekly, plan, reset_at, duration)
    local = usage_snapshot if isinstance(usage_snapshot, dict) else {}
    local_at = _epoch(local.get('observedAt'))
    tokens = _number(local.get('cumulativeTokens'))
    coverage_epoch = local.get('coverageEpoch')
    local_reason = None
    if str(local.get('accountId') or '') != str(account['id']):
        local_reason = 'local_account_mismatch'
    elif not local.get('coverageComplete') or not coverage_epoch or local.get('usageMissingCount'):
        local_reason = 'local_coverage_incomplete'
    elif local_at is None or tokens is None or tokens < 0:
        local_reason = 'local_observation_unavailable'
    elif abs(remote_at - local_at) > MAX_ALIGNMENT_SECONDS or local_at > now + 5:
        local_reason = 'observation_time_mismatch'
    try:
        path = _path()
        with _LOCK, _file_lock(path):
            accounts = _load(path)
            previous = accounts.get(identity)
            if local_reason:
                # A partial/backfilled interval cannot be bridged by a later total.
                measurement = _measurement(account, local)
                accounts[identity] = {'scope': scope, 'pendingReason': local_reason,
                                      'lastAt': remote_at, 'measurement': measurement}
                _save(path, accounts)
                return _with_measurement(_base(local_reason, remaining), measurement)
            if previous and previous.get('pendingReason'):
                previous = None
            epoch_hash = _hash(str(coverage_epoch))
            reset_reason = None
            if previous:
                if previous.get('scope') != scope:
                    reset_reason = 'quota_window_or_plan_changed'
                elif previous.get('coverageEpoch') != epoch_hash:
                    reset_reason = 'local_counter_generation_changed'
                elif (remote_at == (_number(previous.get('lastAt')) or 0)
                      and remaining == previous.get('lastRemaining')
                      and tokens == previous.get('lastTokens')):
                    # Idempotent re-render: do not add a sample or replace the
                    # paired timestamps with a later UI polling timestamp.
                    return _summary(previous, remaining, tokens)
                elif remote_at <= (_number(previous.get('lastAt')) or 0):
                    result = _base('duplicate_or_out_of_order_quota', remaining)
                    return result
                elif remaining > (_number(previous.get('lastRemaining')) or 0):
                    reset_reason = 'quota_regained'
                elif tokens < (_number(previous.get('lastTokens')) or 0):
                    reset_reason = 'local_counter_decreased'
                elif local_at <= (_number(previous.get('lastLocalAt')) or 0):
                    return _base('duplicate_or_out_of_order_local', remaining)
            if not previous or reset_reason:
                state = {'scope': scope, 'coverageEpoch': epoch_hash, 'resetAt': reset_at,
                         'startAt': remote_at, 'startTokens': tokens, 'samples': [],
                         'anchorAt': remote_at, 'anchorRemaining': remaining, 'anchorTokens': tokens}
            else:
                state = previous
                raw_samples = state.get('samples')
                if not isinstance(raw_samples, list) or any(
                    not isinstance(item, dict) or not _number(item.get('tokens')) or
                    (_number(item.get('drop')) or 0) <= QUANTIZATION_PP for item in raw_samples):
                    accounts.pop(identity, None)
                    _save(path, accounts)
                    return _base('invalid_calibration_state', remaining)
                drop = state['anchorRemaining'] - remaining
                delta = tokens - state['anchorTokens']
                if drop > QUANTIZATION_PP:
                    if delta > 0:
                        state['samples'] = (state['samples'] + [
                            {'tokens': delta, 'drop': drop, 'startAt': state['anchorAt'], 'endAt': remote_at}
                        ])[-MAX_SAMPLES:]
                    else:
                        # Remote decline with no local tokens establishes unobserved
                        # consumption/accounting lag: invalidate instead of fitting 0.
                        state['samples'] = []
                        state['startTokens'] = tokens
                        state['startAt'] = remote_at
                        reset_reason = 'quota_drop_without_local_usage'
                    state.update(anchorAt=remote_at, anchorRemaining=remaining, anchorTokens=tokens)
            state.update(lastAt=remote_at, lastLocalAt=local_at, lastRemaining=remaining,
                         lastTokens=tokens, measurement=_measurement(account, local))
            accounts[identity] = state
            _save(path, accounts)
            result = _summary(state, remaining, tokens)
            if reset_reason:
                result['reason'] = reset_reason
            return result
    except (OSError, TimeoutError, ValueError, KeyError, TypeError, OverflowError):
        return _base('calibration_store_unavailable', remaining)
