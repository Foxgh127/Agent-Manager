"""Quota services."""
from __future__ import annotations
from agent_manager import core as _core


def _parse_quota_window(window: _core.Any) -> dict | None:
    if not isinstance(window, dict):
        return None
    try:
        used_value = window.get("used_percent")
        if used_value is None:
            used_value = window.get("usedPercent")
        used = max(0, min(100, int(used_value)))
    except (TypeError, ValueError):
        return None
    reset_value = window.get("reset_at")
    if reset_value is None:
        reset_value = window.get("resetsAt", window.get("resetAt"))
    reset_at = _core._session_expiry(reset_value)
    if not reset_at:
        try:
            after_value = window.get("reset_after_seconds")
            if after_value is None:
                after_value = window.get("resetAfterSeconds")
            after_seconds = max(0, int(after_value))
            reset_at = _core._iso_from_timestamp(_core.time.time() + after_seconds)
        except (TypeError, ValueError):
            reset_at = None
    try:
        minutes_value = window.get("windowDurationMins")
        if minutes_value is None:
            minutes_value = window.get("window_minutes")
        if minutes_value is not None:
            window_minutes = max(0, int(minutes_value)) or None
        else:
            seconds_value = window.get("limit_window_seconds")
            if seconds_value is None:
                seconds_value = window.get("limitWindowSeconds")
            seconds = int(seconds_value)
            window_minutes = (seconds + 59) // 60 if seconds > 0 else None
    except (TypeError, ValueError):
        window_minutes = None
    return {"remainingPercent": 100 - used, "usedPercent": used, "resetAt": reset_at, "windowMinutes": window_minutes}



def _quota_window_rank(indexed_window: tuple[int, dict]) -> tuple[int, int]:
    index, window = indexed_window
    try:
        minutes = max(0, int(window.get("windowMinutes") or 0))
    except (TypeError, ValueError):
        minutes = 0
    return minutes, index



def _normalize_plan_label(value: _core.Any) -> str:
    return _core.normalize_plan_label(value)



def _plan_snapshot_metadata(snapshot: dict, *, source: str = "cache", observed_at: str | None = None) -> dict:
    """Attach validity and provenance to legacy as well as current snapshots."""
    evidence = snapshot.get("planEvidence")
    evidence = dict(evidence) if isinstance(evidence, dict) else {}
    evidence.setdefault("source", source)
    if not evidence.get("expiresAt"):
        evidence["expiresAt"] = snapshot.get("subscriptionExpiresAt")
    if not evidence.get("status"):
        evidence["status"] = snapshot.get("subscriptionStatus")
    if observed_at is not None:
        evidence["observedAt"] = observed_at
    else:
        evidence.setdefault("observedAt", snapshot.get("subscriptionLastCheckedAt") or snapshot.get("updatedAt")
                            or snapshot.get("lastRefreshedAt") or "1970-01-01T00:00:01Z")
        checked_at = _core._parsed_datetime(evidence.get("observedAt"))
        if checked_at and checked_at > _core.datetime.now(_core.timezone.utc):
            # An upstream/cache clock in the future cannot outrank a response
            # we actually observed now (or become permanent freshness).
            evidence["observedAt"] = "1970-01-01T00:00:01Z"
    return {key: snapshot.get(key) for key in ("plan", "planRaw", "planLabel")} | {"planEvidence": evidence}



def _usage_plan(payload: dict) -> tuple[str, str]:
    metadata = _core.extract_plan_metadata(payload)
    if metadata:
        return metadata["plan"], metadata["planLabel"]
    # Preserve unknown display text only from this response's own plan fields;
    # never combine unrelated quota, history, or account records into a tier.
    raw = _core._account_check_value(payload, (
        "plan", "plan_type", "planType", "plan_name", "subscription_plan",
        "subscription_tier", "codex_plan_type", "product_plan", "tier",
    ))
    return raw, _core._normalize_plan_label(raw)



def _usage_subscription_expiry(payload: dict) -> str | None:
    expiry_keys = {
        "subscription_expires_at",
        "subscription_expiry",
        "subscription_end",
        "subscription_end_at",
        "plan_expires_at",
        "plan_expiry",
        "current_period_end",
    }
    stack: list[tuple[str, Any]] = [("", payload)]
    while stack:
        path, current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                folded = str(key).casefold()
                child_path = f"{path}.{folded}" if path else folded
                if folded in expiry_keys or (
                    folded in {"expires_at", "expires", "end_at"}
                    and any(marker in path for marker in ("subscription", "plan", "billing"))
                ):
                    parsed = _core._session_expiry(value)
                    if parsed:
                        return parsed
                if isinstance(value, (dict, list)):
                    stack.append((child_path, value))
        elif isinstance(current, list):
            for value in current:
                stack.append((path, value))
    return None



def _reset_credit_container(payload: _core.Any) -> dict | None:
    if not isinstance(payload, dict):
        return None
    for key in ("rate_limit_reset_credits", "rateLimitResetCredits", "reset_credits", "resetCredits"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    if any(key in payload for key in ("available_count", "availableCount", "credits", "items")):
        return payload
    nested = payload.get("data")
    if isinstance(nested, dict):
        return _core._reset_credit_container(nested)
    return None



def _parse_reset_credits(*payloads: _core.Any) -> dict | None:
    containers = [container for container in (_core._reset_credit_container(item) for item in payloads) if container]
    if not containers:
        return None
    authoritative = containers[-1]
    count_value = next(
        (
            container.get("available_count", container.get("availableCount"))
            for container in reversed(containers)
            if container.get("available_count", container.get("availableCount")) is not None
        ),
        None,
    )
    available_count = None
    if count_value is not None:
        try:
            available_count = max(0, int(count_value))
        except (TypeError, ValueError):
            available_count = None
    # Cockpit deliberately combines the count from /wham/usage with rows from
    # /wham/rate-limit-reset-credits.  App Server snapshots can likewise carry
    # an authoritative count while omitting/capping the row list.  Choose the
    # newest count, but retain the newest container that actually has details.
    detail_container = next(
        (
            container
            for container in reversed(containers)
            if isinstance(container.get("credits"), list)
            or isinstance(container.get("items"), list)
            or isinstance(container.get("data"), list)
        ),
        authoritative,
    )
    raw_credits = detail_container.get("credits")
    if not isinstance(raw_credits, list):
        raw_credits = detail_container.get("items")
    if not isinstance(raw_credits, list):
        raw_credits = detail_container.get("data")
    details_available = isinstance(raw_credits, list)
    credits = []
    for index, item in enumerate(raw_credits if isinstance(raw_credits, list) else []):
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "available").strip().casefold()
        if status and status not in {"available", "ready", "active", "granted"}:
            continue
        credit_id = str(item.get("id") or item.get("credit_id") or item.get("creditId") or "").strip()
        credits.append(
            {
                "id": credit_id or f"reset-credit-{index + 1}",
                "resetType": str(item.get("reset_type") or item.get("resetType") or "codexRateLimits"),
                "status": status or "available",
                "grantedAt": _core._session_expiry(
                    item.get("granted_at") or item.get("grantedAt") or item.get("issued_at") or item.get("issuedAt")
                ),
                "expiresAt": _core._session_expiry(
                    item.get("expires_at") or item.get("expiresAt") or item.get("expiry")
                ),
                "title": str(item.get("title") or item.get("name") or "Codex 完整额度重置"),
                "description": str(item.get("description") or "可重置当前 Codex 使用额度"),
            }
        )
    if available_count is None and credits:
        available_count = len(credits)
    if available_count is None and not details_available:
        return None
    if available_count is None:
        available_count = 0
    return {
        "availableCount": available_count,
        "credits": credits,
        "detailsAvailable": details_available,
        "checkedAt": _core.now_iso(),
    }



def _merge_reset_credit_snapshot(
    previous: _core.Any,
    observed: _core.Any,
    *,
    allow_clear: bool = False,
) -> dict | None:
    """Merge sparse reset-card responses without turning unknown into zero."""

    old = _core.json.loads(_core.json.dumps(previous)) if isinstance(previous, dict) else None
    new = _core.json.loads(_core.json.dumps(observed)) if isinstance(observed, dict) else None
    if new is None:
        if old is not None:
            old["stale"] = True
        return old
    count = new.get("availableCount")
    try:
        count = max(0, int(count))
    except (TypeError, ValueError):
        if old is not None:
            old["stale"] = True
            old["checkedAt"] = new.get("checkedAt") or _core.now_iso()
            return old
        return None
    new["availableCount"] = count
    new.setdefault("credits", [])
    new.setdefault("detailsAvailable", False)
    new["stale"] = False
    new.pop("zeroObservedAt", None)

    if old is None:
        return new
    try:
        old_count = max(0, int(old.get("availableCount") or 0))
    except (TypeError, ValueError):
        old_count = 0
    old_credits = old.get("credits") if isinstance(old.get("credits"), list) else []
    pending_redeem = old.get("pendingRedeem") if isinstance(old.get("pendingRedeem"), dict) else None
    if pending_redeem:
        try:
            pending_previous_count = max(0, int(pending_redeem.get("previousCount") or old_count))
        except (TypeError, ValueError):
            pending_previous_count = old_count
        if count < pending_previous_count:
            # The authoritative usage endpoint now reflects the redemption.
            # Drop the in-flight idempotency marker and accept a zero even when
            # the cached card row itself still has a future expiry.
            new.pop("pendingRedeem", None)
            if count == 0:
                return new
        else:
            # A transport failure leaves the POST result uncertain.  Preserve
            # the request id until either the same POST is retried or usage
            # proves the count changed; otherwise a background refresh could
            # accidentally make the next click consume another card.
            new["pendingRedeem"] = pending_redeem
    if count > 0 and not new.get("detailsAvailable") and old_credits:
        new["credits"] = old_credits[:count]
        new["detailsAvailable"] = bool(old.get("detailsAvailable"))
        new["detailsCheckedAt"] = old.get("detailsCheckedAt")
    if count > 0:
        return new
    if old_count <= 0 or allow_clear:
        return new

    # A card with a known future expiry cannot disappear merely because an
    # unrelated usage response omitted or zeroed the reset-credit field.
    future_expiries = [
        expiry
        for credit in old_credits
        if isinstance(credit, dict) and (expiry := _core._parsed_datetime(credit.get("expiresAt"))) is not None
        and expiry > _core.datetime.now(_core.timezone.utc)
    ]
    first_zero = _core._parsed_datetime(old.get("zeroObservedAt"))
    confirmed_later = bool(
        first_zero
        and (_core.datetime.now(_core.timezone.utc) - first_zero).total_seconds() >= 60
        and not future_expiries
    )
    if confirmed_later:
        return new
    old["stale"] = True
    old["checkedAt"] = new.get("checkedAt") or _core.now_iso()
    old["zeroObservedAt"] = old.get("zeroObservedAt") or _core.now_iso()
    return old



def _parse_chatgpt_usage(
    payload: dict,
    reset_payload: dict | None = None,
    *additional_reset_payloads: dict | None,
    observed_at: str | None = None,
) -> dict:
    limits = payload.get("rate_limit") if isinstance(payload.get("rate_limit"), dict) else None
    if limits is None:
        limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else None
    if limits is None:
        limits = payload.get("rateLimits") if isinstance(payload.get("rateLimits"), dict) else {}
    by_id = payload.get("rateLimitsByLimitId", payload.get("rate_limits_by_limit_id"))
    if isinstance(by_id, dict) and isinstance(by_id.get("codex"), dict):
        limits = by_id["codex"]
    elif limits.get("limitId") and limits.get("limitId") != "codex":
        limits = {}
    plan_raw, plan_label = _core._usage_plan(payload)
    observed_at = observed_at or _core.now_iso()
    metadata = _core.extract_plan_metadata(payload, source="usage", observed_at=observed_at)
    if not metadata:
        metadata = _core.extract_plan_metadata(limits, source="usage", observed_at=observed_at)
        plan_raw = metadata.get("planRaw", plan_raw)
        plan_label = metadata.get("planLabel", plan_label)
    primary = _core._parse_quota_window(
        limits.get("primary_window", limits.get("primaryWindow", limits.get("primary")))
    )
    secondary = _core._parse_quota_window(
        limits.get("secondary_window", limits.get("secondaryWindow", limits.get("secondary")))
    )
    windows = [window for window in (primary, secondary) if window]
    weekly = max(enumerate(windows), key=_core._quota_window_rank)[1] if windows else None
    return {
        "plan": plan_raw,
        "planRaw": plan_raw,
        "planLabel": plan_label,
        **metadata,
        "subscriptionExpiresAt": _core._usage_subscription_expiry(payload),
        "weekly": weekly,
        "resetCredits": _core._parse_reset_credits(payload, reset_payload, *additional_reset_payloads),
        "allowed": limits.get("allowed") if isinstance(limits.get("allowed"), bool) else None,
        "limitReached": (
            limits.get("limit_reached")
            if isinstance(limits.get("limit_reached"), bool)
            else limits.get("limitReached")
            if isinstance(limits.get("limitReached"), bool)
            else None
        ),
        "updatedAt": observed_at,
    }



def _merge_quota_window_snapshot(previous: _core.Any, observed: _core.Any) -> dict | None:
    """Keep the last usable quota when a successful response is temporarily sparse."""

    if isinstance(observed, dict) and observed.get("remainingPercent") is not None:
        current = _core.json.loads(_core.json.dumps(observed))
        current["stale"] = False
        return current
    if not isinstance(previous, dict) or previous.get("remainingPercent") is None:
        return None
    cached = _core.json.loads(_core.json.dumps(previous))
    cached["stale"] = True
    return cached

