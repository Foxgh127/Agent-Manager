"""Hourly monitoring and explainable public reset evidence, without model calls.

Scores rank evidence strength; they are never calibrated reset probabilities.
Source labels and third-party ``confirmed`` flags do not confer official status.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
import threading
from typing import Any, Mapping
from urllib.parse import urlparse

INTERVAL_SECONDS = 3600
SIGNAL_MAX_AGE = timedelta(hours=24)
PREDICTION_MAX_AGE = timedelta(hours=6)
LEVELS = {"information": 0, "watch": 1, "warning": 2, "confirmed": 3}
LEVEL_LABELS = {"information": "信息", "watch": "关注", "warning": "预警", "confirmed": "确认"}
OFFICIAL_HANDLES = {"openai", "openaidevs", "thsottiaux", "romainhuet", "gdb", "sama"}
_INIT_LOCK = threading.Lock()


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # Undated/timezone-less public records cannot prove freshness.
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _dict(value: Any) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _url(value: Any) -> str | None:
    try:
        parsed = urlparse(str(value or "")[:1200])
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
            return None
        from .radar import _safe_public_link
        return _safe_public_link(parsed.geturl())
    except ValueError:
        return None


def _identity(item: Mapping[str, Any], channel: str) -> tuple[str, str, bool, str | None]:
    url = _url(item.get("url") or item.get("sourceUrl"))
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").removeprefix("www.").casefold()
    parts = parsed.path.strip("/").split("/")
    if host in {"x.com", "twitter.com"} and len(parts) >= 3 and parts[1] == "status":
        official = parts[0].casefold() in OFFICIAL_HANDLES
        return f"x:{parts[2]}", "openai-posts" if official else f"community-x:{parts[0].casefold()}", official, url
    if host == "status.openai.com" and channel == "status":
        return f"status:{item.get('id') or parsed.path}", "openai-status", True, url
    key = str(item.get("eventId") or item.get("id") or url or "")
    if not key:
        key = hashlib.sha256(str(item.get("title") or item.get("name") or "").encode()).hexdigest()[:20]
    return f"public:{host}:{key}", host or channel, False, url


def _event_type(item: Mapping[str, Any], text: str) -> str:
    supplied = str(item.get("resetType") or "")
    if supplied in {"reset-card", "full-reset", "limit-increase"}:
        return supplied
    if re.search(r"reset (?:card|credit)|banked reset|banked credit|重置卡|补发.*卡|发卡", text):
        return "reset-card"
    if re.search(r"(?:increase|higher|double).{0,30}(?:limit|quota)|(?:limit|quota).{0,20}(?:increase|double)|额度提升|提高额度", text):
        return "limit-increase"
    return "full-reset"


def _candidate(item: Mapping[str, Any], channel: str, now: datetime, since: datetime) -> dict | None:
    stamp = next((_time(item.get(key)) for key in ("updatedAt", "publishedAt", "createdAt") if _time(item.get(key))), None)
    if stamp is None or not max(since, now - SIGNAL_MAX_AGE) < stamp <= now + timedelta(minutes=5):
        return None
    if str(item.get("status") or "").casefold() in {"cancelled", "canceled", "expired", "failed"}:
        return None
    text = " ".join(str(item.get(key) or "") for key in ("title", "context", "name", "latestUpdate")).casefold()
    target = any(term in text for term in ("codex", "chatgpt work"))
    quota = bool(re.search(r"usage limit|rate limit|quota|weekly limit|reset|额度|用量限制|重置|发卡", text))
    if not target or not quota:
        return None
    identity, origin, official, url = _identity(item, channel)
    if not url:
        return None
    negated = bool(re.search(r"(?:not|never|no plans? to|won't|will not|haven't|hasn't|didn't).{0,35}(?:reset|restor|replenish)|不会重置|尚未重置|并未重置|取消.*重置", text))
    future = bool(re.search(r"will (?:reset|restore|replenish|increase)|reset.{0,25}(?:next hour|tomorrow|later today)|landing.{0,25}(?:next hour|tomorrow)|将(?:重置|恢复|补发|提高)|即将.{0,15}(?:重置|发卡)", text)) and not negated
    completed = bool(re.search(r"(?:have|has|had|we've|i've|i|we) (?:just |already |now )?(?:reset|restored|replenished)|(?:limits?|quota|credits?).{0,25}(?:have been|has been|were|was|are now) (?:reset|restored|replenished)|已(?:经)?(?:完成)?重置|额度.{0,10}已恢复|已补发", text)) and not future and not negated
    occurred = _time(item.get("occurredAt"))
    if completed and occurred and occurred < now - SIGNAL_MAX_AGE:
        return None
    # An incident being resolved confirms the incident, not a global reset.
    if channel == "status" and str(item.get("status") or "").casefold() in {"resolved", "completed"} and not completed:
        return None
    age = max(0.0, (now - stamp).total_seconds()) / 3600
    fresh_points = 10 if age <= 6 else 5 if age <= 12 else 0
    broad_scope = bool(re.search(r"\ball\b|\bpaid\b|\busers\b|\bsubscriptions?\b|\bevery(?:one)?\b|所有|全部|用户|订阅", text))
    personal_scope = bool(re.search(r"(?:reset|restor|replenish)\w*.{0,12}\bmy\b|\bmy\b.{0,25}(?:quota|limits?|subscription|account)|重置我的|我的.{0,10}(?:额度|订阅|账号)", text))
    explicit_global = bool(re.search(r"\ball (?:paid |pro |free )?(?:users|accounts|subscriptions)\b|for everyone|所有用户|全部用户|所有订阅", text))
    if personal_scope and not explicit_global:
        severity, score, legacy = "information", 15 + fresh_points, "I"
    elif completed and official and broad_scope:
        severity, score, legacy = "confirmed", 90 + fresh_points, "C"
    elif future and official:
        effective = _time(item.get("effectiveAt") or item.get("scheduledAt"))
        severity, score, legacy = ("warning", 75 + fresh_points, "A") if effective is None or now < effective <= now + timedelta(hours=24) else ("watch", 50 + fresh_points, "B")
    elif official and not negated:
        severity, score, legacy = "watch", 45 + fresh_points, "B"
    else:
        severity, score, legacy = "information", 15 + fresh_points, "I"
    kind = _event_type(item, text)
    title = str(item.get("titleZh") or item.get("nameZh") or item.get("title") or item.get("name") or "公开额度信号")[:220]
    return {
        "eventKey": f"{identity}:{kind}", "signalIds": [identity], "origin": origin,
        "sourceUrls": [url], "official": official, "severity": severity,
        "level": legacy, "kind": kind, "score": score, "evidence": title,
        "publishedAt": _iso(stamp), "expiresAt": _iso(stamp + SIGNAL_MAX_AGE),
        "window": str(item.get("effectiveAt") or item.get("scheduledAt") or ("已出现来源确认；到账以实际账户为准" if severity == "confirmed" else "实际执行时间尚未确认")),
        "basis": ["官方来源链接" if official else "第三方公开信号", f"距发布 {round(age, 1)} 小时", "明确完成陈述" if completed else "明确未来行动" if future else "范围或行动仍需核对"],
    }


def assess_reset_signals(reset_data: Mapping[str, Any], *, now: datetime, since: datetime | None = None) -> dict:
    """Return an auditable assessment and unique candidate events."""
    now = _time(now)
    since = _time(since) or now - SIGNAL_MAX_AGE
    forecast = _dict(reset_data.get("forecastSignals"))
    rows = [(item, "forecast") for item in [*_list(forecast.get("posts")), *_list(forecast.get("resetEvents")), forecast.get("latestSignal")]]
    rows.extend((item, "feed") for item in _list(reset_data.get("events")))
    rows.extend((item, "status") for item in _list(reset_data.get("statusIncidents")))
    candidates: dict[str, dict] = {}
    for item, channel in rows:
        if not isinstance(item, Mapping):
            continue
        candidate = _candidate(item, channel, now, since)
        if candidate is not None:
            key = candidate["eventKey"]
            previous = candidates.get(key)
            if previous is None or (LEVELS[candidate["severity"]], candidate["score"]) > (LEVELS[previous["severity"]], previous["score"]):
                candidates[key] = candidate
    predictor = _dict(forecast.get("predictor"))
    raw_score = _number(predictor.get("score"))
    checked = _time(forecast.get("checkedAt"))
    prediction_fresh = checked is not None and now - PREDICTION_MAX_AGE <= checked <= now + timedelta(minutes=5)
    if raw_score is not None and raw_score >= 70 and prediction_fresh:
        # One continuous forecast episode, independent of score jitter and fetch timestamp.
        key = f"prediction:{predictor.get('latestResetAt') or 'unknown'}"
        from .radar import PUBLIC_FORECAST_URL
        candidates[key] = {
            "eventKey": key, "signalIds": [key], "origin": "community-predictor",
            "sourceUrls": [PUBLIC_FORECAST_URL], "official": False, "severity": "watch", "level": "P",
            "kind": "community-prediction", "score": round(35 + min(30, raw_score - 70) / 3),
            "evidence": f"第三方信号分 {min(100, round(raw_score)):g}/100；这是预测评分，不是重置发生概率或官方承诺。",
            "window": "实际发生时间未确认", "publishedAt": _iso(checked),
            "sourceCheckedAt": _iso(checked), "expiresAt": _iso(checked + PREDICTION_MAX_AGE),
            "basis": ["第三方预测模型", "仅作关注提示，不证明即将重置"],
        }
    ordered = sorted(candidates.values(), key=lambda item: (LEVELS[item["severity"]], item["score"], item["publishedAt"]), reverse=True)
    primary = copy.deepcopy(ordered[0]) if ordered else None
    # A website score or a syndicated copy of a post is not independent corroboration.
    origins = sorted({item["origin"] for item in ordered if item["official"] and primary and item["kind"] == primary["kind"]})
    corroboration = min(10, max(0, len(origins) - 1) * 5)
    score = min(100, (primary["score"] if primary else 0) + corroboration)
    severity = primary["severity"] if primary else "information"
    assessment = {
        "version": 1, "evaluatedAt": _iso(now), "severity": severity,
        "label": LEVEL_LABELS[severity], "score": score, "scoreMeaning": "公开证据强度，非重置概率",
        "officialConfirmed": bool(primary and severity == "confirmed" and primary["official"]),
        "independentSourceCount": len(origins), "independentSources": origins,
        "uniqueSignalCount": len(ordered), "thirdPartyScore": raw_score,
        "thirdPartyScoreFresh": prediction_fresh, "basis": (primary or {}).get("basis", ["暂无新鲜且可核对的额度信号"]),
        "candidates": ordered,
    }
    if primary and severity != "information":
        primary.update(score=score, severityLabel=LEVEL_LABELS[severity], detectedAt=_iso(now),
                       scoreMeaning=assessment["scoreMeaning"], independentSourceCount=len(origins),
                       officialConfirmed=assessment["officialConfirmed"],
                       advice="核对原始消息与实际账户；按需使用额度。")
        primary["signature"] = hashlib.sha256(f"{primary['eventKey']}:{primary['severity']}".encode()).hexdigest()[:24]
        assessment["alert"] = primary
    else:
        assessment["alert"] = None
    return assessment


def classify_reset_alert(forecast, status_incidents, *, since, now):
    return assess_reset_signals({"forecastSignals": forecast, "statusIncidents": list(status_incidents)}, now=now, since=since)["alert"]


def evaluate_reset_refresh(service, reset_data: dict, now: datetime) -> dict:
    assessment = assess_reset_signals(reset_data, now=now)
    monitor = _dict(service._state.get("monitor"))
    ledger = _dict(monitor.get("signalLedger"))
    alert = assessment["alert"]
    fresh = [candidate for candidate in assessment["candidates"]
             if candidate["severity"] != "information"
             and LEVELS[candidate["severity"]] > int(_dict(ledger.get(candidate["eventKey"])).get("rank", -1))]
    new_alert = bool(fresh)
    if fresh:
        # An already-known confirmation must not hide a separate new event.
        alert = copy.deepcopy(fresh[0])
        alert.update(severityLabel=LEVEL_LABELS[alert["severity"]], detectedAt=_iso(now),
                     scoreMeaning="公开证据强度，非重置概率", officialConfirmed=alert["official"] and alert["severity"] == "confirmed",
                     advice="核对原始消息与实际账户；按需使用额度。")
        alert["signature"] = hashlib.sha256(f"{alert['eventKey']}:{alert['severity']}".encode()).hexdigest()[:24]
    # Mark every observed signal, including historical lower-level ones. Only
    # the strongest newly observed event is eligible in one refresh.
    for candidate in assessment["candidates"]:
        key = candidate["eventKey"]
        old = _dict(ledger.get(key))
        ledger[key] = {"rank": max(int(old.get("rank", -1)), LEVELS[candidate["severity"]]),
                       "lastSeenAt": _iso(now), "firstSeenAt": old.get("firstSeenAt") or _iso(now)}
    ledger = dict(sorted(ledger.items(), key=lambda pair: str(pair[1].get("lastSeenAt")), reverse=True)[:512])
    result_label = "预测预警" if alert and alert["level"] == "P" else f"{assessment['label']}：公开额度信号" if alert else "无新增预警"
    if not alert and assessment["thirdPartyScore"] is not None and not assessment["thirdPartyScoreFresh"]:
        result_label = "来源数据已过期，暂不发出预测预警"
    mode = getattr(service, "_monitor_check_mode", "manual-refresh")
    monitor.update(lastRunAt=_iso(now), lastSuccessAt=_iso(now), lastResult=result_label, lastCheckMode=mode,
                   lastSourceCheckedAt=_dict(reset_data.get("forecastSignals")).get("checkedAt"),
                   lastAlert=alert, lastError=None, signalLedger=ledger, assessment={k:v for k,v in assessment.items() if k != "candidates"})
    seen = [str(value) for value in _list(monitor.get("seenAlertSignatures"))]
    if alert and alert["signature"] not in seen:
        seen.append(alert["signature"])
    monitor["seenAlertSignatures"] = seen[-128:]
    monitor["runs"] = [{"at": _iso(now), "result": result_label, "mode": mode}, *_list(monitor.get("runs"))[:95]]
    if new_alert:
        deliveries = _dict(monitor.get("alertDeliveries"))
        deliveries.setdefault(alert["signature"], {"state": "pending", "attempts": 0, "nextAttemptAt": _iso(now), "alert": alert})
        monitor["alertDeliveries"] = dict(list(deliveries.items())[-32:])
    service._state["monitor"] = monitor
    reset_data["assessment"] = {key: value for key, value in assessment.items() if key != "candidates"}
    return {"alert": alert, "newAlert": new_alert}


def fetch_status_for_refresh(service, now: datetime, remember_retry_after) -> list:
    monitor = _dict(service._state.get("monitor"))
    cached = _list(monitor.get("lastStatusIncidents"))
    next_allowed = _time(monitor.get("statusNextAllowedAt"))
    if next_allowed and now < next_allowed:
        return cached
    try:
        incidents, unchanged = service._fetch_status_incidents()
        if unchanged and not isinstance(monitor.get("lastStatusIncidents"), list):
            from .radar import RadarError
            raise RadarError("OpenAI 状态源返回 304，但本地没有对应缓存。")
        if unchanged:
            incidents = cached
        incidents = service._translate_status_incidents(incidents or [])
        monitor.update(lastStatusIncidents=incidents[:100], statusLastSuccessAt=_iso(now), statusError=None,
                       statusFailureCount=0, statusNextAllowedAt=None)
    except Exception as exc:
        remember_retry_after(exc)
        failures = min(8, int(monitor.get("statusFailureCount") or 0) + 1)
        due = now + timedelta(seconds=min(3600, 300 * 2 ** (failures - 1)))
        retry = _time(getattr(exc, "retry_after", None))
        monitor.update(statusError=str(exc)[:300], statusFailureCount=failures,
                       statusNextAllowedAt=_iso(max(due, retry) if retry else due))
        incidents = cached
    service._state["monitor"] = monitor
    return incidents


def begin_monitoring(service) -> None:
    """One runtime start, never a page-open hook."""
    with service._lock:
        service._monitor_next_due = service._now()


def seconds_until_pending_delivery(service) -> float | None:
    now = service._now()
    pending = [_time(row.get("nextAttemptAt")) for row in _dict(_dict(service._state.get("monitor")).get("alertDeliveries")).values()
               if isinstance(row, dict) and row.get("state") == "pending"]
    return max(0, (min(pending) - now).total_seconds()) if pending and all(pending) else None


def deliver_pending_alert(service, notifier, *, alert=None, stop=None) -> dict:
    """Persist successful submission separately from seeing source evidence."""
    if not callable(notifier) or (stop is not None and stop.is_set()):
        return {}
    with _INIT_LOCK:
        if not hasattr(service, "_radar_delivery_lock"):
            service._radar_delivery_lock = threading.Lock()
    if not service._radar_delivery_lock.acquire(blocking=False):
        return {}
    try:
        with service._lock:
            now = service._now()
            state = _dict(service._state.get("monitor"))
            deliveries = _dict(state.get("alertDeliveries"))
            if isinstance(alert, dict) and alert.get("signature"):
                deliveries.setdefault(alert["signature"], {"state": "pending", "attempts": 0, "nextAttemptAt": _iso(now), "alert": alert})
            selected = None
            for key, row in deliveries.items():
                if not isinstance(row, dict) or row.get("state") != "pending":
                    continue
                expires = _time(_dict(row.get("alert")).get("expiresAt"))
                if expires and now >= expires:
                    row["state"] = "expired"
                    continue
                if (_time(row.get("nextAttemptAt")) or now) <= now:
                    selected = (key, copy.deepcopy(row))
                    break
            state["alertDeliveries"] = dict(list(deliveries.items())[-32:])
            service._state["monitor"] = state
            service._persist_state()
        if selected is None or (stop is not None and stop.is_set()):
            return {}
        key, row = selected
        error = None
        try:
            submitted = notifier(row["alert"]) is not False
        except Exception as exc:
            submitted = False
            error = str(exc)[:200]
        with service._lock:
            state = _dict(service._state.get("monitor"))
            deliveries = _dict(state.get("alertDeliveries"))
            row["attempts"] = int(row.get("attempts") or 0) + 1
            row.update(state="submitted" if submitted else "failed" if row["attempts"] >= 3 else "pending",
                       lastAttemptAt=_iso(service._now()), nextAttemptAt=_iso(service._now() + timedelta(minutes=5)), lastError=error)
            deliveries[key] = row
            state["alertDeliveries"] = dict(list(deliveries.items())[-32:])
            service._state["monitor"] = state
            service._persist_state()
        return {"notificationStatus": "submitted" if submitted else "retry-pending" if row["state"] == "pending" else "failed",
                "notificationError": error, "notificationAttempts": row["attempts"]}
    finally:
        service._radar_delivery_lock.release()


def next_monitor_time(now: datetime, last_automatic: Any = None) -> datetime:
    previous = _time(last_automatic)
    return max(now, previous + timedelta(seconds=INTERVAL_SECONDS)) if previous else now


def monitor_status(service) -> dict:
    with service._lock:
        status = copy.deepcopy(_dict(service._state.get("monitor")))
        due = getattr(service, "_monitor_next_due", None)
        status.update(timezone="Asia/Shanghai", schedule="启动立即检查，运行期间每 60 分钟（全天）",
                      intervalSeconds=INTERVAL_SECONDS, nextCheckAt=_iso(due) if due else None)
        return status


def seconds_until_next_monitor(service) -> float:
    due = getattr(service, "_monitor_next_due", None)
    return max(0.0, (due - service._now()).total_seconds()) if due else 0.0


def run_monitor(service, *, force: bool = False) -> dict:
    # A nonblocking gate is outside the data lock so parallel clicks/automatic
    # checks coalesce instead of queuing a second fetch when the first ends.
    with _INIT_LOCK:
        if not hasattr(service, "_monitor_execution_lock"):
            service._monitor_execution_lock = threading.Lock()
    if not service._monitor_execution_lock.acquire(blocking=False):
        return {"attempted": False, "success": True, "suppressed": "in-flight", "newAlert": False,
                "state": copy.deepcopy(_dict(service._state.get("monitor")))}
    try:
        from .radar import _exclusive_cache_file_lock, RadarError
        monitor_lock = service.cache.path.with_suffix(service.cache.path.suffix + ".monitor.lock")
        try:
            claim = _exclusive_cache_file_lock(monitor_lock, timeout=0.0)
            claim.__enter__()
        except RadarError:
            if not force:
                service._monitor_next_due = service._now() + timedelta(minutes=1)
            return {"attempted": False, "success": True, "suppressed": "in-flight", "newAlert": False,
                    "state": copy.deepcopy(_dict(service._state.get("monitor")))}
        try:
            return _run_claimed_monitor(service, force=force)
        finally:
            claim.__exit__(None, None, None)
    finally:
        service._monitor_execution_lock.release()


def _run_claimed_monitor(service, *, force: bool) -> dict:
        with service._lock:
            # Reload while holding the cross-process claim, before evaluating
            # signatures or fetching, so stale instances cannot re-notify.
            from .radar import _merge_cache_changes
            current = service.cache.load()
            service._state = _merge_cache_changes(current, service._persisted_state, service._state)
            service._persisted_state = copy.deepcopy(current)
            now = service._now()
            if not force and seconds_until_next_monitor(service) > 0:
                return {"attempted": False, "success": True, "suppressed": "already-checked", "newAlert": False, "state": monitor_status(service)}
            if not force:
                service._monitor_next_due = now + timedelta(seconds=INTERVAL_SECONDS)
            recent = getattr(service, "_monitor_last_finished", None) or _time(_dict(service._state.get("monitor")).get("lastRefreshFinishedAt"))
            if recent and timedelta(0) <= now - recent < timedelta(seconds=30):
                return {"attempted": False, "success": True, "suppressed": "recent-check", "newAlert": False, "state": monitor_status(service)}
            service._monitor_check_mode = "manual-refresh" if force else "scheduled"
            try:
                result = service._refresh({"reset"})
            except Exception as exc:
                result = {"attempted": True, "success": False, "error": str(exc)[:300], "newAlert": False}
            finally:
                service._monitor_check_mode = "manual-refresh"
            finished = service._now()
            monitor = _dict(service._state.get("monitor"))
            if result.get("attempted"):
                service._monitor_last_finished = finished
                monitor["lastRunAt"] = _iso(now)
                monitor["lastRefreshFinishedAt"] = _iso(finished)
            if not force:
                monitor["lastAutomaticRunAt"] = _iso(now)
            if not result.get("success"):
                monitor.update(lastError=result.get("error") or "数据源退避中，等待下次重试", lastResult="检查失败，已安排重试")
                if not force:
                    allowed = _time(service._state.get("nextAllowedAt"))
                    service._monitor_next_due = max(finished + timedelta(minutes=5), allowed or finished)
            monitor["nextCheckAt"] = _iso(service._monitor_next_due) if getattr(service, "_monitor_next_due", None) else None
            service._state["monitor"] = monitor
            service._persist_state()
            return {**result, "state": monitor_status(service)}
