"""Present provider balance snapshots without inventing native Codex quotas.

Provider currency balances and Codex Credits have different units. Only actual
upstream Codex headers are forwarded for API providers; billing snapshots remain
plain catalog descriptions with their freshness status.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re
import time
from typing import Any, Mapping


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _epoch_seconds(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    numeric = _finite_number(value)
    if numeric is not None:
        return int(numeric) if numeric > 0 else None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _is_provider(account: Mapping[str, Any]) -> bool:
    return (
        str(account.get("kind") or "") in {"custom", "provider"}
        or account.get("sourceKind") == "provider"
        or any(key in account for key in ("baseUrl", "balance", "balanceSnapshot"))
    )


def codex_quota_headers(
    upstream_headers: Mapping[str, str], account: dict | None = None,
) -> dict[str, str]:
    """Preserve filtered upstream headers and the official weekly fallback.

    ``upstream_headers`` must already have passed the gateway header allowlist.
    No API provider billing amount, expiry, plan or unlimited flag is converted
    into a native Codex rate-limit or Credits field.
    """
    forwarded = dict(upstream_headers)
    if not isinstance(account, dict) or _is_provider(account):
        return forwarded
    usage = account.get("usage") if isinstance(account.get("usage"), dict) else {}
    weekly = usage.get("weekly") if isinstance(usage.get("weekly"), dict) else {}
    remaining = _finite_number(weekly.get("remainingPercent"))
    reset_at = _epoch_seconds(weekly.get("resetAt"))
    if remaining is not None and 0 <= remaining <= 100 and not (reset_at and reset_at <= time.time()):
        window_minutes = _finite_number(weekly.get("windowMinutes"))
        if window_minutes is None or window_minutes <= 0 or not window_minutes.is_integer():
            window_minutes = 10_080
        forwarded.setdefault("x-codex-primary-used-percent", f"{100 - remaining:g}")
        forwarded.setdefault("x-codex-primary-window-minutes", str(int(window_minutes)))
        if reset_at:
            forwarded.setdefault("x-codex-primary-reset-at", str(reset_at))
    plan = str(account.get("plan") or account.get("planLabel") or "").strip()
    if plan and len(plan) <= 128 and not any(ord(char) < 32 for char in plan):
        forwarded.setdefault("x-codex-plan-type", plan)
    return forwarded


def apply_quota_headers(response: Any, headers: Mapping[str, str]) -> None:
    """Best-effortly add Codex headers without overwriting upstream evidence."""
    target = getattr(response, "headers", None)
    if target is None or not hasattr(target, "__setitem__") or not hasattr(target, "items"):
        return
    existing = {str(name).strip().casefold() for name, _ in target.items()}
    for name, value in headers.items():
        normalized = str(name).strip().casefold()
        if not normalized.startswith("x-codex-") or normalized in existing:
            continue
        try:
            target[name] = value
            existing.add(normalized)
        except (TypeError, ValueError, AttributeError):
            continue


def source_balance_text(balance: object, *, updated_at: Any = None, error: Any = None) -> str:
    """Render a bounded, non-secret billing snapshot, including stale status."""
    if not isinstance(balance, dict):
        return " · 余额暂不可用（刷新失败）" if error else ""
    unlimited = (
        balance.get("unlimited") is True
        or str(balance.get("unlimited") or "").casefold() in {"true", "1", "yes"}
        or balance.get("mode") == "unrestricted"
    )
    amount = _finite_number(balance.get("amount"))
    if amount is None and not unlimited:
        return " · 余额暂不可用（刷新失败）" if error else ""
    currency = str(balance.get("currency") or "").strip().upper()
    currency = currency if re.fullmatch(r"[A-Z0-9]{1,8}", currency) else ""
    if unlimited:
        rendered = "额度不限"
    else:
        rendered = f"余额 {amount:g} {currency}" if currency else f"余额 {amount:g} 额度"
    status = ["快照"]
    expires_at = _epoch_seconds(balance.get("expiresAt"))
    valid = balance.get("valid", balance.get("isValid"))
    if expires_at is not None and expires_at <= time.time():
        status.append("已过期")
    elif valid is False or str(valid).casefold() in {"false", "0", "no"}:
        status.append("已失效")
    if error:
        # balanceUpdatedAt can be the time of the failed refresh, so it cannot
        # date the retained successful balance in this case.
        status.append("刷新失败，非当前额度")
    else:
        snapshot_at = _epoch_seconds(updated_at)
        try:
            observed_at = datetime.fromtimestamp(snapshot_at, timezone.utc) if snapshot_at else None
        except (ValueError, OverflowError, OSError):
            observed_at = None
        status.append(observed_at.strftime("%Y-%m-%d %H:%M UTC") if observed_at else "时间未知")
    return f" · {rendered}（{'；'.join(status)}）"
