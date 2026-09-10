#!/usr/bin/env python3
"""Low-frequency, cache-first access to public Codex Reset Radar data.

Only unauthenticated public pages, JSON, and RSS are consumed. Protected API
access is deliberately unsupported. Callers may supply account reset metadata
for an in-memory merge; this module never reads account stores or tokens.

Service methods return an envelope (``data`` plus cache/source metadata). UI or
HTTP adapters may flatten ``data`` while retaining ``fetchedAt``, ``stale``,
``nextAllowedAt``, and ``source`` from that envelope.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, Callable, Iterable, Mapping
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlparse
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from zoneinfo import ZoneInfo
from . import radar_monitor as monitor_engine


PUBLIC_SUMMARY_URL = "https://codexradar.com/current.json"
PUBLIC_INTELLIGENCE_URL = "https://codexradar.com/api/intelligence-efficiency-metrics"
PUBLIC_VISUAL_INTELLIGENCE_URL = "https://codexradar.com/api/visual-spatial-reasoning"
PUBLIC_INTELLIGENCE_DETAIL_URL = "https://codexradar.com/api/intelligence-efficiency"
PUBLIC_HTML_URL = "https://codexradar.com/"
PUBLIC_FEED_URL = "https://codexradar.com/feed.xml"
PUBLIC_FORECAST_URL = "https://www.willcodexquotareset.com/api/forecast"
PUBLIC_TRANSLATE_URL = "https://api.mymemory.translated.net/get"
OPENAI_STATUS_INCIDENTS_URL = "https://status.openai.com/api/v2/incidents.json"
CACHE_SCHEMA_VERSION = 5
MAX_PUBLIC_BYTES = 6 * 1024 * 1024
MAX_SUMMARY_BYTES = MAX_PUBLIC_BYTES
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_FEED_BYTES = 256 * 1024
MAX_CACHE_BYTES = 3 * 1024 * 1024
QUOTA_REFRESH_INTERVAL = timedelta(days=7)
BACKOFF_BASE = timedelta(minutes=5)
BACKOFF_MAX = timedelta(hours=24)
RETRY_AFTER_MAX = timedelta(days=7)
DEFAULT_TIMEOUT_SECONDS = 8.0
BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
MONITOR_START_HOUR = 8
MONITOR_END_HOUR = 23
COMMUNITY_ALERT_THRESHOLD = 70
COMMUNITY_SIGNAL_MAX_AGE = timedelta(hours=6)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 "
    "AgentManager-Radar/7.0"
)


class RadarError(RuntimeError):
    """Base class for safe, user-displayable radar errors."""


class RadarSchemaError(RadarError):
    """Raised when public data does not match a bounded expected schema."""


class RadarHTTPError(RadarError):
    def __init__(self, message: str, *, status: int | None = None, retry_after: datetime | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


_RADAR_ALLOWED_HOSTS = frozenset(
    (urlparse(url).hostname or "").casefold()
    for url in (
        PUBLIC_SUMMARY_URL,
        PUBLIC_INTELLIGENCE_URL,
        PUBLIC_VISUAL_INTELLIGENCE_URL,
        PUBLIC_INTELLIGENCE_DETAIL_URL,
        PUBLIC_HTML_URL,
        PUBLIC_FEED_URL,
        PUBLIC_FORECAST_URL,
        PUBLIC_TRANSLATE_URL,
        OPENAI_STATUS_INCIDENTS_URL,
        # Keep the Pages deployment as a validated compatibility mirror while
        # the canonical public source is codexradar.com.
        "https://codex-reset-radar.pages.dev/",
    )
)

_RADAR_LINK_ALLOWED_HOSTS = _RADAR_ALLOWED_HOSTS | frozenset(
    {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
)


def _validate_public_source_url(url: str) -> None:
    parsed = urlparse(str(url or ""))
    hostname = (parsed.hostname or "").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or not hostname
        or hostname not in _RADAR_ALLOWED_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise RadarError("雷达数据源地址未通过 HTTPS 与公开域名校验。")
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        return
    if not address.is_global:
        raise RadarError("雷达数据源不能指向本机、内网或保留地址。")


def _safe_public_link(value: Any, *, allowed_hosts: Iterable[str] | None = None) -> str | None:
    """Return only bounded HTTPS links that are safe to expose to the UI."""
    link = _text(value, 1000)
    if not link:
        return None
    hosts = _RADAR_LINK_ALLOWED_HOSTS if allowed_hosts is None else {
        str(host).casefold() for host in allowed_hosts
    }
    try:
        parsed = urlparse(link)
        hostname = (parsed.hostname or "").casefold()
        if (
            parsed.scheme.casefold() != "https"
            or hostname not in hosts
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            return None
    except ValueError:
        return None
    return link


class _SafeRadarRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        _validate_public_source_url(new_url)
        return super().redirect_request(request, fp, code, message, headers, new_url)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return _aware(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return _aware(datetime.fromisoformat(text))
    except ValueError:
        try:
            return _aware(parsedate_to_datetime(value))
        except (TypeError, ValueError, OverflowError):
            return None


def _text(value: Any, limit: int = 800) -> str | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    result = re.sub(r"\s+", " ", str(value)).strip()
    return result[:limit] if result else None


def _safe_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _bounded_json(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> Any:
    """Copy public JSON to a bounded JSON-safe tree."""
    if budget is None:
        budget = [30000]
    if budget[0] <= 0 or depth > 8:
        return None
    budget[0] -= 1
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value[:2000]
    number = _safe_number(value)
    if number is not None:
        return number
    if isinstance(value, list):
        return [_bounded_json(item, depth=depth + 1, budget=budget) for item in value[:200]]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:200]:
            key = _text(raw_key, 120)
            if key:
                result[key] = _bounded_json(raw_value, depth=depth + 1, budget=budget)
        return result
    return None


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _select(source: Any, fields: Iterable[str]) -> dict:
    source = _mapping(source)
    return {key: _bounded_json(source[key]) for key in fields if key in source}


def _quota_source_label(value: Any) -> str:
    text = (_text(value, 120) or "").casefold()
    labels = {
        "distributed radar": "分布式雷达",
        "distributed_community_runs": "分布式社区实测",
        "estimated": "推算",
        "estimate": "推算",
        "measured": "实测",
        "public": "公开数据",
    }
    return labels.get(text, _text(value, 120) or "推算")


def _decode_json(raw: bytes | str, *, label: str, max_bytes: int = MAX_PUBLIC_BYTES) -> Any:
    if isinstance(raw, bytes):
        if len(raw) > max_bytes:
            raise RadarSchemaError(f"{label} 超过响应大小限制。")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RadarSchemaError(f"{label} 不是有效的 UTF-8 文本。") from exc
    else:
        text = raw
        if len(text.encode("utf-8")) > max_bytes:
            raise RadarSchemaError(f"{label} 超过响应大小限制。")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RadarSchemaError(f"{label} 不是有效的 JSON。") from exc


def _default_cache_path() -> Path:
    root = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    return root / "agent-manager" / "radar-cache.json"


def data_sources() -> list[dict]:
    return [
        {"url": PUBLIC_SUMMARY_URL, "format": "json", "purpose": "public intelligence, quota, and reset radar snapshot", "authentication": "none"},
        {"url": PUBLIC_INTELLIGENCE_URL, "format": "json", "purpose": "software-engineering intelligence component", "authentication": "none"},
        {"url": PUBLIC_VISUAL_INTELLIGENCE_URL, "format": "json", "purpose": "visual-spatial intelligence component", "authentication": "none"},
        {"url": PUBLIC_FEED_URL, "format": "rss", "purpose": "verified historical reset events", "authentication": "none"},
        {"url": PUBLIC_FORECAST_URL, "format": "json", "purpose": "public reset forecast and @thsottiaux signal mirror", "authentication": "none"},
        {"url": PUBLIC_TRANSLATE_URL, "format": "json", "purpose": "fallback Chinese translation for public reset posts missing a reviewed site translation", "authentication": "none"},
        {"url": OPENAI_STATUS_INCIDENTS_URL, "format": "json", "purpose": "official OpenAI incident signals", "authentication": "none"},
        {"url": PUBLIC_HTML_URL, "format": "html", "purpose": "current community USD quota measurements, monthly reset evidence, and reviewed translations", "authentication": "none"},
    ]


def validate_public_payload(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise RadarSchemaError("公开雷达摘要必须是 JSON 对象。")
    if _text(payload.get("service"), 120) != "codex-reset-radar":
        raise RadarSchemaError("公开雷达摘要的服务标识不受支持。")
    schema_version = payload.get("schema_version")
    if isinstance(schema_version, bool):
        raise RadarSchemaError("公开雷达的数据版本不受支持。")
    match = re.fullmatch(r"(\d{1,2})(?:\.\d{1,3})?", str(schema_version or "").strip())
    if not match or not 1 <= int(match.group(1)) <= 20:
        raise RadarSchemaError("公开雷达的数据版本不受支持。")
    if payload.get("model_iq") is not None and not isinstance(payload.get("model_iq"), dict):
        raise RadarSchemaError("公开雷达的智力数据结构无效。")
    if not isinstance(payload.get("window"), (dict, type(None))):
        raise RadarSchemaError("公开雷达的时间窗口结构无效。")
    return payload


def parse_public_json(raw: bytes | str) -> dict:
    payload = validate_public_payload(_decode_json(raw, label="公开雷达摘要"))
    model_iq = _mapping(payload.get("model_iq"))
    quota_radar = _mapping(model_iq.get("quota_radar"))
    rows = []
    for item in _list(quota_radar.get("rows"))[:40]:
        if not isinstance(item, dict):
            continue
        normalized = _bounded_json(item)
        seven_day = item.get("seven_d")
        if isinstance(seven_day, (int, float)) and not isinstance(seven_day, bool):
            normalized["estimated7d"] = f"${seven_day:,.2f}"
        normalized["sourceLabel"] = _quota_source_label(item.get("basis"))
        rows.append(normalized)
    trend = quota_radar.get("trend")
    history = quota_radar.get("history") or quota_radar.get("recent_days")
    trend_points = trend.get("points") if isinstance(trend, dict) else trend
    normalized_trend = []
    for item in _list(trend_points)[-128:]:
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            normalized_trend.append(item)
            continue
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            value = item.get("seven_d_20x")
        normalized_trend.append({**_bounded_json(item), "value": value})
    comparisons = []
    raw_comparisons = model_iq.get("comparisons")
    comparison_values = raw_comparisons.items() if isinstance(raw_comparisons, dict) else enumerate(_list(raw_comparisons))
    for key, item in list(comparison_values)[:64]:
        if not isinstance(item, dict):
            continue
        latest = _mapping(item.get("latest"))
        model = _text(item.get("model") or latest.get("model"), 160)
        lowered = (model or "").casefold()
        family = (
            "Sol" if "sol" in lowered else
            "Terra" if "terra" in lowered else
            "Luna" if "luna" in lowered else
            "GPT-5.5" if "5.5" in lowered else
            "DeepSeek" if "deepseek" in lowered else
            (model or "Codex")
        )
        cost = latest.get("average_cost_usd")
        comparisons.append({
            "id": str(key),
            "label": _text(item.get("label"), 200) or model,
            "model": model,
            "family": family,
            "effort": _text(item.get("reasoning_effort") or latest.get("reasoning_effort"), 80),
            "score": latest.get("score"),
            "status": _text(latest.get("status"), 80),
            "sampleCount": latest.get("valid_tasks") or latest.get("tasks"),
            "cost": f"${cost:,.2f}" if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
            "duration": _text(latest.get("average_task_time_human"), 80),
            "updatedAt": _text(latest.get("date"), 80),
        })
    # current.json keeps the leading configuration in model_iq.latest and the
    # remaining configurations under comparisons.  Include the leading point
    # so the fallback grid is complete even when the lightweight metrics API
    # is temporarily unavailable.
    leading = _mapping(model_iq.get("latest"))
    leading_model = _text(leading.get("model"), 160)
    leading_effort = _text(leading.get("reasoning_effort"), 80)
    leading_id = f"{leading_model}/{leading_effort}" if leading_model and leading_effort else None
    if leading_id and not any(item.get("id") == leading_id for item in comparisons):
        lowered = leading_model.casefold()
        family = (
            "Sol" if "sol" in lowered else
            "Terra" if "terra" in lowered else
            "Luna" if "luna" in lowered else
            "GPT-5.5" if "5.5" in lowered else
            "DeepSeek" if "deepseek" in lowered else
            leading_model
        )
        cost = _safe_number(leading.get("average_cost_usd"))
        comparisons.insert(0, {
            "id": leading_id,
            "label": f"{'5.5' if family == 'GPT-5.5' else family} {leading_effort}",
            "model": leading_model,
            "family": family,
            "effort": leading_effort,
            "score": leading.get("score"),
            "iq": leading.get("score"),
            "status": _text(leading.get("status"), 80),
            "sampleCount": leading.get("valid_tasks") or leading.get("tasks"),
            "cost": f"${cost:,.2f}" if cost is not None else None,
            "duration": _text(leading.get("average_task_time_human"), 80),
            "updatedAt": _text(leading.get("date"), 80),
        })
    window = _mapping(payload.get("window"))
    public_events = []
    event_at = window.get("opened_at") if window.get("open") else window.get("closed_at")
    if event_at:
        event_title = _text(window.get("title"), 240) or "Codex 用量限制重置"
        event_description = _text(window.get("message"), 600)
        reset_type = _reset_type(event_title, event_description)
        public_events.append(_attach_public_translation({
            "title": event_title,
            "publishedAt": _text(event_at, 80),
            "resetAt": _text(event_at, 80),
            "description": event_description,
            "source": "public-current-json",
            "sourceLabel": "Codex 雷达公开记录",
            "url": _safe_public_link(window.get("source_url")),
            "resetType": reset_type,
            "resetTypeLabel": _reset_type_label(reset_type),
            "completed": not bool(window.get("open"))
            and str(window.get("label") or "").casefold() not in {"weekly", "primary", "secondary", "five-hour", "seven-day"}
            and _completed_reset_event(event_title, event_description),
        }))
    policies = [
        {"window": "five-hour", "policy": _bounded_json(quota_radar.get("five_hour_policy"))},
        {"window": "seven-day", "policy": _bounded_json(quota_radar.get("seven_day_policy"))},
    ]
    policies = [item for item in policies if item["policy"] is not None]
    normalized = {
        "schemaVersion": CACHE_SCHEMA_VERSION,
        "remoteSchemaVersion": payload["schema_version"],
        "format": "json",
        "degraded": False,
        "intelligence": {
            "updatedAt": _text(model_iq.get("updated_at"), 80),
            "dataSource": _bounded_json(model_iq.get("data_source")),
            "latest": _bounded_json(_mapping(model_iq.get("latest"))),
            "comparisons": comparisons,
            "items": comparisons,
            "recentDays": _bounded_json(_list(model_iq.get("recent_days"))[-64:]),
        },
        "quota": {
            "updatedAt": _text(quota_radar.get("updated_at") or model_iq.get("updated_at"), 80),
            "check": _bounded_json(_mapping(model_iq.get("quota_check"))),
            "calibration": _bounded_json(_mapping(model_iq.get("quota_calibration"))),
            "tiers": rows,
            "plans": policies,
            "history": _bounded_json(_list(history)[-64:]),
            "trendPoints": normalized_trend,
            "radar": _select(quota_radar, (
                "date", "basis_date", "basis_window", "basis_window_label", "cost_usd", "tasks",
                "raw_delta", "trend", "rows", "five_hour_policy", "seven_day_policy", "source",
                "source_kind", "updated_at",
            )),
        },
        "reset": {
            "status": _text(payload.get("status"), 120),
            "monitoredAt": _text(payload.get("monitored_at"), 80),
            "timezone": _text(payload.get("timezone"), 80),
            "windowOpen": payload.get("window_open") if isinstance(payload.get("window_open"), bool) else None,
            "window": _bounded_json(window),
            "prediction": _bounded_json(_mapping(payload.get("prediction"))),
            "recommendedAction": _bounded_json(payload.get("recommended_action")),
            "tiboPresence": _bounded_json(_mapping(payload.get("tibo_presence"))),
            "events": public_events,
        },
    }
    _attach_translation_fields(
        normalized["reset"],
        fields=("status", "summary", "recommendedAction"),
    )
    _attach_translation_fields(
        normalized["reset"]["window"],
        fields=("title", "label", "message", "description", "status", "impact"),
    )
    _attach_translation_fields(
        normalized["reset"]["prediction"],
        fields=("title", "label", "message", "summary", "reason", "status", "impact"),
    )
    return normalized


def parse_intelligence_metrics(raw: bytes | str) -> dict:
    """Normalize compact metrics or the larger public benchmark payload."""
    payload = _decode_json(raw, label="智力效率数据")
    if not isinstance(payload, dict):
        raise RadarSchemaError("智力效率响应必须是对象。")
    known = {
        "benchmark_id", "combos", "tier_windows_usd", "cells", "metrics",
        "updated_at", "generated_at", "source_updated_at", "points",
    }
    if not known.intersection(payload):
        raise RadarSchemaError("智力效率响应没有可识别字段。")
    points: list[dict] = []
    for item in _list(payload.get("points"))[:64]:
        if not isinstance(item, dict):
            continue
        model = _text(item.get("model"), 160)
        effort = _text(item.get("effort") or item.get("reasoning_effort"), 80)
        weighted_total = _safe_number(item.get("weighted_total"))
        runs_total = _safe_number(item.get("runs_total"))
        # The public endpoint may contain one-off experimental probes which
        # the website intentionally does not include in its comparison grid.
        # Keep the full established matrix while excluding those incomplete
        # probes, matching the public page rather than inventing extra cards.
        if not model or not effort or _safe_number(item.get("iq")) is None:
            continue
        if (weighted_total or 0) < 10 and (runs_total or 0) < 20:
            continue
        lowered = model.casefold()
        family = (
            "Sol" if "sol" in lowered else
            "Terra" if "terra" in lowered else
            "Luna" if "luna" in lowered else
            "GPT-5.5" if "5.5" in lowered else
            "DeepSeek" if "deepseek" in lowered else
            model
        )
        family_label = "5.5" if family == "GPT-5.5" else family
        cost = _safe_number(item.get("average_price_usd"))
        minutes = _safe_number(item.get("average_minutes"))
        samples = _safe_number(item.get("runs_24h"))
        points.append({
            "id": f"{model}/{effort}",
            "label": f"{family_label} {effort}",
            "model": model,
            "family": family,
            "effort": effort,
            "score": item.get("iq"),
            "iq": item.get("iq"),
            "sampleCount": samples,
            "cost": f"${cost:,.2f}" if cost is not None else None,
            "duration": f"{round(minutes):g}分钟" if minutes is not None else None,
            "updatedAt": _text(item.get("source_updated_at") or payload.get("source_updated_at"), 80),
        })
    result = {
        "updatedAt": _text(payload.get("source_updated_at") or payload.get("updated_at") or payload.get("generated_at"), 80),
        "metrics": _bounded_json(payload.get("metrics")),
        "counts": _select(payload, ("item_count", "configuration_count", "cell_count", "combo_count")),
        "runs24h": _safe_number(payload.get("runs_24h_total")),
        "runs48h": _safe_number(payload.get("runs_48h_total")),
        "runsTotal": _safe_number(payload.get("runs_total")),
    }
    # A temporarily empty or newly-shaped supplemental response must not erase
    # the complete comparison matrix already obtained from current.json.
    if points:
        result["items"] = points
        result["comparisons"] = points
    return result


_COMPOSITE_MODEL_INFO = {
    "gpt-6-astra": ("Astra", -1),
    "gpt-5.6-sol": ("Sol", 0),
    "gpt-5.6-terra": ("Terra", 1),
    "gpt-5.6-luna": ("Luna", 2),
    "gpt-5.5": ("GPT-5.5", 3),
    "deepseek-v4-flash": ("DSV4 Flash", 4),
    "deepseek-v4-pro": ("DSV4 Pro", 5),
}
_COMPOSITE_EFFORT_ORDER = {
    "ultra": 0,
    "max": 1,
    "xhigh": 2,
    "high": 3,
    "medium": 4,
    "low": 5,
    "off": 6,
}


def _intelligence_component(raw: bytes | str, *, label: str) -> dict:
    """Keep only fields used by the public composite-intelligence formula."""
    payload = _decode_json(raw, label=label)
    if not isinstance(payload, dict) or not isinstance(payload.get("points"), list):
        raise RadarSchemaError(f"{label}没有返回有效的 points 数组。")
    points: list[dict] = []
    numeric_fields = (
        "average_price_usd",
        "price_samples",
        "average_minutes",
        "duration_samples",
        "average_agent_steps",
        "agent_steps_samples",
        "average_total_tokens",
        "token_samples",
        "cache_hit_rate",
        "cache_token_samples",
        "runs_24h",
        "runs_48h",
        "runs_total",
    )
    for item in _list(payload.get("points"))[:200]:
        if not isinstance(item, dict):
            continue
        model = _text(item.get("model"), 160)
        effort = (_text(item.get("effort") or item.get("reasoning_effort"), 80) or "").casefold()
        iq = _safe_number(item.get("iq"))
        if not model or not effort or iq is None or iq < 0:
            continue
        valid_tasks = _safe_number(item.get("valid_tasks"))
        if valid_tasks is None:
            valid_tasks = _safe_number(item.get("total") or item.get("weighted_total"))
        if valid_tasks is not None and valid_tasks <= 0:
            continue
        benchmark_tasks = _safe_number(item.get("benchmark_tasks"))
        point: dict[str, Any] = {
            "model": model,
            "effort": effort,
            "iq": iq,
            "valid_tasks": valid_tasks,
            "benchmark_tasks": benchmark_tasks if benchmark_tasks is not None else valid_tasks,
            "latest_graded_at": _text(
                item.get("latest_graded_at") or item.get("source_updated_at"), 80
            ),
        }
        for field in numeric_fields:
            point[field] = _safe_number(item.get(field))
        price_bands = item.get("average_price_usd_by_band")
        if model.startswith("deepseek-") and isinstance(price_bands, Mapping):
            off_peak = _safe_number(price_bands.get("off_peak"))
            if off_peak is not None:
                point["average_price_usd"] = off_peak
        points.append(point)
    if not points:
        raise RadarSchemaError(f"{label}没有可用于综合智能计算的评分。")
    return {
        "updatedAt": _text(
            payload.get("source_updated_at")
            or payload.get("updated_at")
            or payload.get("generated_at"),
            80,
        ),
        "runs24h": _safe_number(payload.get("runs_24h_total")),
        "runs48h": _safe_number(payload.get("runs_48h_total")),
        "runsTotal": _safe_number(payload.get("runs_total")),
        "points": points,
    }


def _weighted_component_metric(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    field: str,
    sample_field: str,
) -> float | None:
    left_value = _safe_number(left.get(field))
    right_value = _safe_number(right.get(field))
    if left_value is None:
        return right_value
    if right_value is None:
        return left_value
    left_weight = max(
        1.0,
        _safe_number(left.get(sample_field)) or _safe_number(left.get("valid_tasks")) or 1.0,
    )
    right_weight = max(
        1.0,
        _safe_number(right.get(sample_field)) or _safe_number(right.get("valid_tasks")) or 1.0,
    )
    return (left_value * left_weight + right_value * right_weight) / (left_weight + right_weight)


def _summed_component_metric(
    left: Mapping[str, Any], right: Mapping[str, Any], field: str
) -> float | None:
    available = [
        value
        for value in (_safe_number(left.get(field)), _safe_number(right.get(field)))
        if value is not None
    ]
    return sum(available) if available else None


def _older_component_time(*values: Any) -> str | None:
    timestamps = [parsed for parsed in (_parse_time(value) for value in values) if parsed is not None]
    return _iso(min(timestamps)) if timestamps else None


def _compose_intelligence_components(
    software: Mapping[str, Any], visual: Mapping[str, Any]
) -> dict:
    visual_by_key = {
        (str(point.get("model")), str(point.get("effort"))): point
        for point in _list(visual.get("points"))
        if isinstance(point, Mapping)
    }
    points: list[dict] = []
    for software_point in _list(software.get("points")):
        if not isinstance(software_point, Mapping):
            continue
        model = str(software_point.get("model") or "")
        effort = str(software_point.get("effort") or "").casefold()
        model_info = _COMPOSITE_MODEL_INFO.get(model, (model, 100))
        visual_point = visual_by_key.get((model, effort))
        if not isinstance(visual_point, Mapping):
            continue
        software_iq = _safe_number(software_point.get("iq"))
        visual_iq = _safe_number(visual_point.get("iq"))
        if (
            not effort
            or software_iq is None
            or visual_iq is None
            or software_iq < 0
            or visual_iq < 0
        ):
            continue
        cost = _weighted_component_metric(
            software_point, visual_point, "average_price_usd", "valid_tasks"
        ) if all(_safe_number(p.get("average_price_usd")) is not None for p in (software_point, visual_point)) else None
        minutes = _weighted_component_metric(
            software_point, visual_point, "average_minutes", "valid_tasks"
        ) if all(_safe_number(p.get("average_minutes")) is not None for p in (software_point, visual_point)) else None
        runs_24h = _summed_component_metric(software_point, visual_point, "runs_24h")
        score = _weighted_component_metric(software_point, visual_point, "iq", "valid_tasks")
        family, family_order = model_info
        points.append(
            {
                "id": f"{model}/{effort}",
                "label": f"{family.replace('GPT-', '')} {effort}",
                "model": model,
                "family": family,
                "familyOrder": family_order,
                "effort": effort,
                "score": round(score, 2),
                "iq": round(score, 2),
                "softwareIq": round(software_iq, 2),
                "visualIq": round(visual_iq, 2),
                "sampleCount": round(runs_24h) if runs_24h is not None else None,
                "cost": f"${cost:,.2f}" if cost is not None else None,
                "duration": f"{round(minutes):g}分钟" if minutes is not None else None,
                "updatedAt": _older_component_time(
                    software_point.get("latest_graded_at"),
                    visual_point.get("latest_graded_at"),
                ),
            }
        )
    points.sort(
        key=lambda item: (
            item["familyOrder"],
            item["model"],
            _COMPOSITE_EFFORT_ORDER.get(item["effort"], 99),
        )
    )
    if not points:
        raise RadarSchemaError("两个公开榜单没有可配对的综合智能评分。")
    return {
        "mode": "composite-weighted-mean",
        "formula": "IQ、费用、耗时按两侧有效题量加权；仅纳入两个维度均有有效成绩的模型档位。",
        "updatedAt": _older_component_time(software.get("updatedAt"), visual.get("updatedAt")),
        "runs24h": _summed_component_metric(software, visual, "runs24h"),
        "runs48h": _summed_component_metric(software, visual, "runs48h"),
        "runsTotal": _summed_component_metric(software, visual, "runsTotal"),
        "items": points,
        "comparisons": points,
    }


def parse_composite_intelligence_metrics(
    software_raw: bytes | str, visual_raw: bytes | str
) -> dict:
    """Reproduce Codex Radar's public valid-task-weighted composite."""
    software = _intelligence_component(software_raw, label="软件工程能力数据")
    visual = _intelligence_component(visual_raw, label="视觉空间推理数据")
    return _compose_intelligence_components(software, visual)


def _records(value: Any, limit: int, allowed: tuple[str, ...]) -> list[dict]:
    source: list[Any]
    if isinstance(value, dict):
        source = []
        for key, item in list(value.items())[:limit]:
            if isinstance(item, dict):
                source.append({"id": key, **item})
    else:
        source = _list(value)[:limit]
    result: list[dict] = []
    for item in source:
        if not isinstance(item, dict):
            continue
        selected = _select(item, allowed)
        if selected:
            result.append(selected)
    return result


def parse_intelligence_detail(raw: bytes | str) -> dict:
    """Normalize the public matrix without caching its multi-megabyte raw body."""
    payload = _decode_json(raw, label="智力效率矩阵")
    if not isinstance(payload, dict):
        raise RadarSchemaError("智力效率矩阵必须是对象。")
    known = {"benchmark_id", "items", "configurations", "combos", "cells", "tier_windows_usd"}
    if not known.intersection(payload):
        raise RadarSchemaError("智力效率矩阵没有可识别字段。")
    configurations = payload.get("configurations") or payload.get("combos")
    configuration_fields = (
        "id", "key", "name", "label", "model", "model_id", "tier", "window", "window_usd",
        "window_label", "reasoning_effort", "service_tier", "cost_usd", "score", "iq", "efficiency",
    )
    item_fields = ("id", "key", "name", "label", "title", "category", "suite", "weight", "order", "prompt_tokens")
    cell_fields = (
        "id", "item_id", "item", "configuration_id", "config_id", "configuration", "model", "tier",
        "window", "score", "iq", "efficiency", "cost_usd", "tokens", "latency_ms", "rank", "status",
    )
    return {
        "benchmarkId": _text(payload.get("benchmark_id"), 160),
        "tierWindowsUsd": _bounded_json(payload.get("tier_windows_usd")),
        "configurations": _records(configurations, 18, configuration_fields),
        "items": _records(payload.get("items"), 80, item_fields),
        "cells": _records(payload.get("cells"), 1440, cell_fields),
    }


class _PublicHTMLProbe(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.scripts: list[str] = []
        self._script_type = ""
        self._script_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "script":
            attributes = {key.casefold(): (value or "") for key, value in attrs}
            self._script_type = attributes.get("type", "").casefold()
            self._script_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._script_parts is not None:
            if self._script_type in {"application/json", "application/ld+json"}:
                self.scripts.append("".join(self._script_parts))
            self._script_parts = None
            self._script_type = ""

    def handle_data(self, data: str) -> None:
        if self._script_parts is not None:
            self._script_parts.append(data)
        else:
            value = re.sub(r"\s+", " ", data).strip()
            if value:
                self.text.append(value)


class _ResetPostTranslationParser(HTMLParser):
    """Extract the public site's bounded original/Chinese post pairs."""

    _FIELD_BY_CLASS = {
        "reset-tibo-post-original": "original",
        "reset-tibo-post-translation": "translationZh",
        "reset-tibo-post-analysis": "analysisZh",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.posts: dict[str, dict[str, str]] = {}
        self._post: dict[str, str] | None = None
        self._field: str | None = None
        self._parts: list[str] = []

    @staticmethod
    def _clean(value: str) -> str:
        text = re.sub(r"\s+", " ", value).strip()
        text = re.sub(
            r"^(?:English(?:\s+original)?|Chinese(?:\s+translation)?|Analysis|英文原文|中文翻译|模型语境解读(?:\s*·[^：:]+)?)[：:]?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        return text[:2400]

    def _finish_field(self) -> None:
        if self._post is not None and self._field:
            value = self._clean(" ".join(self._parts))
            if value:
                self._post[self._field] = value
        self._field = None
        self._parts = []

    def _finish_post(self) -> None:
        self._finish_field()
        if self._post:
            post_id = str(self._post.get("id") or "").strip()
            if post_id:
                self.posts[post_id] = dict(self._post)
        self._post = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): value or "" for key, value in attrs}
        classes = set(str(attributes.get("class") or "").split())
        if tag.casefold() == "li" and "reset-tibo-post" in classes:
            self._finish_post()
            self._post = {"id": str(attributes.get("data-tibo-post-id") or "").strip()}
            return
        if self._post is not None:
            field = next((self._FIELD_BY_CLASS[name] for name in classes if name in self._FIELD_BY_CLASS), None)
            if field:
                self._finish_field()
                self._field = field

    def handle_endtag(self, tag: str) -> None:
        if self._post is None:
            return
        if self._field and tag.casefold() in {"p", "div"}:
            self._finish_field()
        if tag.casefold() == "li":
            self._finish_post()

    def handle_data(self, data: str) -> None:
        if self._post is not None and self._field:
            self._parts.append(data)


def parse_public_html_translations(raw: bytes | str) -> dict[str, dict[str, str]]:
    """Return the public site's translated reset-post snippets by post id."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_HTML_BYTES:
            raise RadarSchemaError("公开雷达网页超过响应大小限制。")
        text = raw.decode("utf-8", errors="replace")
    else:
        text = raw
        if len(text.encode("utf-8")) > MAX_HTML_BYTES:
            raise RadarSchemaError("公开雷达网页超过响应大小限制。")
    parser = _ResetPostTranslationParser()
    parser.feed(text)
    parser.close()
    return {
        post_id: item
        for post_id, item in parser.posts.items()
        if item.get("translationZh") or item.get("original")
    }


_AUTO_TRANSLATION_EXACT = {
    "little surprise for you tomorrow.": "明天会给你们一个小惊喜。",
    "codex limits have been reset": "Codex 额度限制已重置。",
    "usage limits have been reset for all paid chatgpt work and codex users.": "所有付费 ChatGPT Work 与 Codex 用户的使用额度已重置。",
    "we will reset codex usage limits within the next 24 hours.": "我们将在未来 24 小时内重置 Codex 使用额度。",
    "reset monday": "周一重置。",
    "we are investigating codex usage limits.": "我们正在调查 Codex 使用额度问题。",
    "do not worry, we have compute": "别担心，我们有算力。",
    "you probably do have a blind spot": "你可能确实有一个盲点。",
    "mari me": "嫁给我吧。",
    "openai devday 2026 will be our best devday in the history of the company. it will not be close.": "OpenAI DevDay 2026 将会是公司历史上最好的一届 DevDay，而且会遥遥领先。",
    # Reviewed UI vocabulary and source phrases.  Keeping these translations
    # local avoids spending the anonymous translation quota on stable labels.
    "watching": "监测中",
    "waiting": "等待中",
    "wait": "等待",
    "weekly": "每周",
    "closed": "已关闭",
    "open": "已开启",
    "pending": "待处理",
    "completed": "已完成",
    "resolved": "已解决",
    "investigating": "调查中",
    "identified": "已定位",
    "monitoring": "监控中",
    "minor": "轻微",
    "major": "重大",
    "critical": "严重",
    "baseline": "基准分",
    "recent-reset cooldown": "近期重置冷却",
    "openai team vagueposting": "OpenAI 团队发布模糊信号",
    "source post": "来源帖子",
    "public reset event": "公开重置事件",
    "elevated errors on codex": "Codex 错误率升高",
    "all impacted services have recovered.": "所有受影响的服务均已恢复。",
}


_PUBLIC_TRANSLATION_FIELDS = (
    "title", "context", "description", "summary", "message", "reason",
    "status", "impact", "latestUpdate", "label", "action", "name",
    "recommendedAction",
)


def _contains_chinese(value: Any) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", _text(value, 2400) or ""))


def _translation_alias(field: str) -> str:
    return f"{field}Zh"


def _translation_candidate(source: Mapping[str, Any], field: str) -> str:
    """Read a translated field while rejecting English copied into a zh key."""
    candidates: list[Any] = [source.get(_translation_alias(field))]
    translated = source.get("translated")
    if isinstance(translated, Mapping):
        candidates.append(translated.get(field))
    if field == "title":
        candidates.extend((
            source.get("translationZh"), source.get("translatedTextZh"),
            source.get("translatedText"),
        ))
    for value in candidates:
        text = _text(value, 2400) or ""
        if text and _contains_chinese(text):
            return text
    return ""


def _refresh_translation_state(item: dict) -> dict:
    fields = _mapping(item.get("translationFields"))
    states = [str(_mapping(value).get("state") or "pending") for value in fields.values()]
    if not states:
        state = "not-needed"
    elif all(value == "complete" for value in states):
        state = "complete"
    elif any(value == "complete" for value in states):
        state = "partial"
    elif any(value == "failed" for value in states):
        state = "failed"
    else:
        state = "pending"
    item["translationState"] = state
    item["translationComplete"] = state in {"complete", "not-needed"}
    errors = [
        _text(_mapping(value).get("error"), 160)
        for value in fields.values()
        if _mapping(value).get("state") == "failed"
    ]
    item["translationError"] = next((value for value in errors if value), None)
    return item


def _register_translation_field(
    item: dict,
    field: str,
    original: Any,
    *,
    translated_zh: Any = None,
    source: str | None = None,
    failed_error: str | None = None,
) -> None:
    original_text = _text(original, 2400) or ""
    if not original_text:
        return
    fields = item.setdefault("translationFields", {})
    translated = item.setdefault("translated", {})
    existing = _mapping(fields.get(field))
    valid_translation = _text(translated_zh, 2400) or ""
    if valid_translation and not _contains_chinese(valid_translation):
        valid_translation = ""
    if not valid_translation:
        valid_translation = _translation_candidate(item, field)
    if not valid_translation and _contains_chinese(original_text):
        valid_translation = original_text
        source = source or "original-zh"
    if valid_translation:
        translated[field] = valid_translation
        item[_translation_alias(field)] = valid_translation
        fields[field] = {
            "state": "complete",
            "source": source or _text(existing.get("source"), 80) or "unknown",
            "original": original_text,
        }
    else:
        translated.pop(field, None)
        item.pop(_translation_alias(field), None)
        fields[field] = {
            "state": "failed" if failed_error else "pending",
            "source": None,
            "original": original_text,
            **({"error": _text(failed_error, 160)} if failed_error else {}),
        }


def _attach_translation_fields(
    item: dict,
    translation: Mapping[str, Any] | None = None,
    *,
    fields: Iterable[str] = _PUBLIC_TRANSLATION_FIELDS,
) -> dict:
    """Attach truthful field-level Chinese translations to one public record."""
    reviewed = _mapping(translation)
    for field in fields:
        original = item.get(field)
        original_text = _text(original, 2400) or ""
        if not original_text:
            continue
        reviewed_text = _translation_candidate(reviewed, field)
        existing_text = _translation_candidate(item, field)
        local_text = _auto_translate_public_text(original_text)
        translated = reviewed_text or existing_text or local_text
        source = (
            "codexradar.com" if reviewed_text
            else _text(_mapping(_mapping(item.get("translationFields")).get(field)).get("source"), 80)
            if existing_text
            else "local-reviewed" if local_text and not _contains_chinese(original_text)
            else "original-zh" if local_text
            else None
        )
        _register_translation_field(
            item,
            field,
            original_text,
            translated_zh=translated,
            source=source,
        )
    _refresh_translation_state(item)
    return item


def _auto_translate_public_text(value: Any) -> str:
    """Return only a known-good local translation, never a fabricated paraphrase."""
    text = _text(value, 2400) or ""
    if not text:
        return ""
    if re.search(r"[\u3400-\u9fff]", text):
        return text
    normalized = text.casefold().strip()
    return _AUTO_TRANSLATION_EXACT.get(normalized, "")


def _translation_chunks(value: Any, *, limit: int = 420) -> list[str]:
    """Split a public post into bounded translation requests.

    MyMemory's unauthenticated endpoint rejects oversized queries.  Tweets are
    normally shorter than one chunk, while longer release posts are split on
    sentence boundaries and reassembled without dropping their original text.
    """
    text = _text(value, 2400) or ""
    if not text:
        return []
    pieces = re.split(r"(?<=[.!?。！？])\s+", text)
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        while len(piece) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(piece[:limit])
            piece = piece[limit:]
        candidate = f"{current} {piece}".strip()
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks[:6]


def parse_machine_translation(raw: bytes | str) -> str:
    """Validate one bounded MyMemory response and return Chinese text only."""
    payload = _decode_json(raw, label="公开中文翻译", max_bytes=64 * 1024)
    if not isinstance(payload, dict):
        raise RadarSchemaError("公开翻译接口返回的数据结构无效。")
    status = _safe_number(payload.get("responseStatus"))
    if status is not None and status >= 400:
        raise RadarSchemaError("公开翻译接口暂时不可用。")
    response_data = _mapping(payload.get("responseData"))
    translated = _text(unescape(str(response_data.get("translatedText") or "")), 2400) or ""
    if not translated or not _contains_chinese(translated):
        raise RadarSchemaError("公开翻译接口没有返回有效中文。")
    return translated


def _attach_public_translation(item: dict, translation: Mapping[str, Any] | None = None) -> dict:
    original = _text(item.get("title") or item.get("originalText"), 2400) or ""
    item["originalText"] = original
    _attach_translation_fields(item, translation)
    title_translation = _translation_candidate(item, "title")
    item["translationZh"] = title_translation or None
    title_meta = _mapping(_mapping(item.get("translationFields")).get("title"))
    item["translationSource"] = _text(title_meta.get("source"), 80) or "unavailable"
    source = _mapping(translation)
    analysis = _text(source.get("analysisZh") or item.get("analysisZh"), 2400)
    if analysis:
        item["analysisZh"] = analysis
    return item


def _near_keyword(text: str, keywords: tuple[str, ...], limit: int = 900) -> str | None:
    folded = text.casefold()
    positions = [folded.find(keyword.casefold()) for keyword in keywords]
    positions = [position for position in positions if position >= 0]
    if not positions:
        return None
    start = max(0, min(positions) - 180)
    return text[start : start + limit]


def parse_public_html(raw: bytes | str) -> dict:
    """Parse inline JSON when present, otherwise return text-only cards."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_HTML_BYTES:
            raise RadarSchemaError("公开雷达网页超过响应大小限制。")
        text = raw.decode("utf-8", errors="replace")
    else:
        text = raw
        if len(text.encode("utf-8")) > MAX_HTML_BYTES:
            raise RadarSchemaError("公开雷达网页超过响应大小限制。")
    probe = _PublicHTMLProbe()
    probe.feed(text)
    for candidate in probe.scripts:
        try:
            embedded = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(embedded, dict) and embedded.get("service") == "codex-reset-radar":
            parsed = parse_public_json(json.dumps(embedded, ensure_ascii=False))
            parsed["format"] = "html-embedded-json"
            return parsed
    visible = re.sub(r"\s+", " ", " ".join(probe.text)).strip()
    folded = visible.casefold()
    markers = ("radar", "reset", "quota", "雷达", "重置", "额度")
    if "codex" not in folded or not any(marker in folded for marker in markers):
        raise RadarSchemaError("公开页面没有可识别的 Codex 雷达内容。")
    normalized = {
        "schemaVersion": CACHE_SCHEMA_VERSION,
        "remoteSchemaVersion": None,
        "format": "html-text",
        "degraded": True,
        "intelligence": {"updatedAt": None, "dataSource": "public-html-fallback", "latest": {}, "comparisons": [], "recentDays": [], "summary": _near_keyword(visible, ("model iq", "intelligence", "智力"))},
        "quota": {"updatedAt": None, "check": {}, "calibration": {}, "radar": {}, "summary": _near_keyword(visible, ("quota", "额度"))},
        "reset": {"status": None, "monitoredAt": None, "timezone": None, "windowOpen": None, "window": {}, "prediction": {}, "recommendedAction": None, "tiboPresence": {}, "events": [], "summary": _near_keyword(visible, ("reset", "window", "重置", "窗口"))},
    }
    for section in (normalized["intelligence"], normalized["quota"], normalized["reset"]):
        _attach_translation_fields(section, fields=("title", "summary", "description", "message", "status"))
    return normalized


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = re.sub(r"\s+", " ", data).strip()
        if value:
            self.parts.append(value)


def _plain_html(value: str, limit: int = 1200) -> str:
    parser = _HTMLText()
    parser.feed(value)
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()[:limit]


class _PublicRadarCardsParser(HTMLParser):
    """Capture bounded visible source cards, without evaluating page scripts."""

    TARGETS = {"quota-radar-head", "quota-radar-current-card", "tibo-radar-monthly-row",
               "tibo-radar-monthly-note", "tibo-radar-chart-description"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.active: list[dict] = []
        self.records: list[dict] = []
        self.monthly_updated_at = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get("data-tibo-monthly-updated-at"):
            self.monthly_updated_at = attributes["data-tibo-monthly-updated-at"]
        if tag in {"br", "img", "input", "meta", "link", "hr", "source", "wbr"}:
            return
        self.depth += 1
        classes = set((attributes.get("class") or "").split())
        matched = self.TARGETS & classes
        if matched:
            self.active.append({"kind": sorted(matched)[0], "attrs": attributes,
                                "depth": self.depth, "parts": []})

    def handle_data(self, data):
        for record in self.active:
            if len(record["parts"]) < 500:
                record["parts"].append(data[:2000])

    def handle_endtag(self, tag):
        if tag in {"br", "img", "input", "meta", "link", "hr", "source", "wbr"}:
            return
        remaining = []
        for record in self.active:
            if record["depth"] == self.depth:
                record["text"] = _text(" ".join(record.pop("parts")), 6000) or ""
                if len(self.records) < 200:
                    self.records.append(record)
            else:
                remaining.append(record)
        self.active = remaining
        self.depth = max(0, self.depth - 1)


def parse_public_html_cards(raw: bytes | str) -> dict:
    """Read current USD measurements and published monthly reset evidence.

    Monthly totals remain aggregates: they must never manufacture individual
    card-delivery dates or use our fetch time as their occurrence timestamp.
    """
    body = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    if len(body.encode("utf-8")) > MAX_HTML_BYTES:
        raise RadarSchemaError("公开雷达 HTML 超过响应大小限制。")
    parser = _PublicRadarCardsParser()
    parser.feed(body)
    rows, monthly, events = [], [], []
    update_text = None
    note = next((r["text"] for r in parser.records if r["kind"] == "tibo-radar-monthly-note"), "")
    for record in parser.records:
        text = record["text"]
        if record["kind"] == "quota-radar-head":
            update_text = text.removeprefix("额度雷达").strip()
        elif record["kind"] == "quota-radar-current-card":
            match = re.search(r"(.+?)\s*·\s*只跑\s*(.+?)\s*\$([\d,]+(?:\.\d+)?)\s*(.+)", text)
            if match:
                tier, model, amount, label = match.groups()
                value = float(amount.replace(",", ""))
                if math.isfinite(value) and value >= 0:
                    rows.append({"tier": tier, "model": model, "label": f"{tier} · 只跑 {model}",
                                 "amountUsd": value, "currency": "USD", "measurement": "community-measured",
                                 "window": None, "basis": label, "sourceLabel": "社区额度实测",
                                 "sourceUrl": PUBLIC_HTML_URL + "#quota-radar"})
        elif record["kind"] == "tibo-radar-monthly-row":
            month = record["attrs"].get("data-month", "")
            counts = re.findall(r"(\d+)\s*次", text)
            if re.fullmatch(r"\d{4}-\d{2}", month) and len(counts) == 2:
                monthly.append({"month": month, "directResetCount": int(counts[0]),
                                "resetCardCount": int(counts[1]), "sourceUrl": PUBLIC_HTML_URL,
                                "updatedAt": parser.monthly_updated_at, "note": note,
                                "confirmation": "community-reported"})
    # The site's reviewed note can explicitly confirm a date-only action.
    confirmed = re.search(r"(\d{1,2})月已由站长确认(\d+)次重置卡及(\d{1,2})月(\d{1,2})日(\d+)次直接重置完成", note)
    if confirmed:
        card_month, card_count, month, day, count = map(int, confirmed.groups())
        matching = next((m for m in monthly if int(m["month"][5:]) == card_month), None)
        if matching and count == 1:
            year = matching["month"][:4]
            date = f"{year}-{month:02d}-{day:02d}"
            if _parse_time(date):
                events.append({"id": f"html-direct-{date}", "title": "站长确认 Codex 直接重置完成",
                               "description": note, "url": PUBLIC_HTML_URL,
                               "publishedAt": None, "sourceUpdatedAt": parser.monthly_updated_at,
                               "completed": True, "resetType": "full-reset", "source": "public-html",
                               "sourceLabel": "Codex 雷达站长确认（社区记录）",
                               **_occurrence_metadata(date, basis="site-reviewed-date")})
        if matching and card_count > 0:
            reported_count = matching["resetCardCount"]
            matching["noteCount"] = card_count
            if card_count != matching["resetCardCount"]:
                matching["discrepancy"] = f"月度表列出 {matching['resetCardCount']} 次卡，说明文字仅确认 {card_count} 次；独立完成日期未公开。"
            events.append({"id": f"html-cards-{matching['month']}", "title": f"来源月表统计 {reported_count} 次重置卡发放",
                           "description": note, "url": PUBLIC_HTML_URL, "publishedAt": None,
                           "sourceUpdatedAt": parser.monthly_updated_at, "occurredAt": matching["month"],
                           "occurrencePrecision": "month", "occurrenceBasis": "site-reviewed-monthly-total",
                           "aggregate": True, "count": reported_count, "reportedCount": reported_count,
                           "noteCount": card_count, "discrepancy": bool(matching.get("discrepancy")),
                           "verification": "conflicting_source_summary" if matching.get("discrepancy") else "community_summary",
                           "completed": True, "resetType": "reset-card",
                           "source": "public-html", "sourceLabel": "Codex 雷达来源统计（月度汇总）"})
    result = {"monthlyHistory": monthly, "resetHistory": events}
    if rows:
        result["quota"] = {"tiers": rows, "updatedAt": None, "sourceUpdatedText": update_text,
                           "measurement": "community-measured", "currency": "USD", "plans": [],
                           "trendPoints": [], "history": [], "radar": {},
                           "sourceUrl": PUBLIC_HTML_URL + "#quota-radar",
                           "summary": "社区单模型额度实测（美元口径）；页面未公开测量窗口及完整方法，不能换算为固定 token 额度或官方订阅保证。"}
    return result


def _reset_type(title: Any, description: Any = None) -> str:
    text = f"{_text(title, 1000) or ''} {_text(description, 2000) or ''}".casefold()
    if any(marker in text for marker in ("重置卡", "reset card", "banked reset", "reset credit")):
        return "reset-card"
    if any(marker in text for marker in ("软重置", "soft reset", "soft-reset")):
        return "soft-reset"
    return "full-reset"


def _reset_type_label(value: str) -> str:
    return {"reset-card": "重置卡", "soft-reset": "软重置"}.get(value, "全量重置")


def _completed_reset_event(title: Any, description: Any = None) -> bool:
    title_text = (_text(title, 1000) or "").casefold()
    description_text = (_text(description, 2000) or "").casefold()
    if "窗口开启" in title_text or "window opened" in title_text:
        return False
    if any(marker in title_text for marker in (
        "已重置", "窗口关闭", "窗口已关闭", "reset completed",
        "limits have been reset", "have reset usage limits", "reset applied",
        "have now reset usage", "has been propagated", "已发放", "已到账",
    )):
        return True
    return bool(re.search(r"(?:权益真实生效|窗口关闭|重置完成)[：:]", description_text))


def _effective_reset_time(description: str, published: datetime) -> datetime | None:
    """Extract a Beijing effective/close time from the public RSS prose."""
    patterns = (
        r"(?:权益真实生效|窗口关闭|重置完成)[：:]\s*(?:(\d{4})[-/])?(\d{1,2})月?(?:[-/])?(\d{1,2})日?\s+(\d{1,2}):(\d{2})",
    )
    for pattern in patterns:
        match = re.search(pattern, description, flags=re.IGNORECASE)
        if not match:
            continue
        year_text, month_text, day_text, hour_text, minute_text = match.groups()
        published_local = published.astimezone(BEIJING_TIMEZONE)
        years = (
            [int(year_text)]
            if year_text
            else [published_local.year - 1, published_local.year, published_local.year + 1]
        )
        candidates = []
        for year in years:
            try:
                candidates.append(
                    datetime(
                        year,
                        int(month_text),
                        int(day_text),
                        int(hour_text),
                        int(minute_text),
                        tzinfo=BEIJING_TIMEZONE,
                    )
                )
            except ValueError:
                continue
        if candidates:
            nearest = min(candidates, key=lambda item: abs((item - published_local).total_seconds()))
            return nearest.astimezone(timezone.utc)
    return None


def _occurrence_metadata(value: Any, *, basis: str = "source-reported") -> dict:
    raw = _text(value, 80)
    if not raw:
        return {"occurredAt": None, "occurrencePrecision": "unknown", "occurrenceBasis": "not-published"}
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) and _parse_time(raw) is not None:
        return {"occurredAt": raw, "occurrencePrecision": "date", "occurrenceBasis": basis}
    if _parse_time(raw) is None or not re.search(r"(?:Z|[+-]\d{2}:?\d{2})$", raw):
        return {"occurredAt": None, "occurrencePrecision": "unknown", "occurrenceBasis": "timezone-unpublished",
                "sourceTimeText": raw}
    precision = "second" if re.search(r"\d{2}:\d{2}:\d{2}", raw) else "minute"
    return {"occurredAt": raw, "occurrencePrecision": precision, "occurrenceBasis": basis}


def _history_record(event: Mapping[str, Any], discovered_at: str) -> dict | None:
    if not (event.get("completed") is True or str(event.get("status") or "").casefold() == "completed"):
        return None
    text = _signal_text(event)
    if event.get("scope") == "account" or any(term in text for term in ("每周自动重置", "个人账号周期重置")):
        return None
    reset_type = event.get("resetType") or _reset_type(event.get("title"), event.get("context") or event.get("description"))
    raw_identity = str(event.get("id") or event.get("guid") or "")
    searchable = " ".join(str(event.get(key) or "") for key in ("url", "description", "context"))
    post = re.search(r"https://(?:www\.)?(?:x|twitter)\.com/[^/\s]+/status/(\d+)", searchable)
    identity = f"post:{post.group(1)}" if post else raw_identity or str(event.get("url") or event.get("title"))
    if not identity:
        return None
    selected = {key: _bounded_json(event[key]) for key in (
        "title", "titleZh", "description", "context", "url", "publishedAt", "source", "sourceLabel",
        "occurredAt", "occurrencePrecision", "occurrenceBasis", "sourceTimeText", "resetAt",
        "aggregate", "count", "sourceUpdatedAt", "reportedCount", "noteCount", "discrepancy", "verification",
    ) if key in event}
    # Legacy cache resetAt may be only a copied publication timestamp. Never
    # silently upgrade that to the event's occurrence time.
    if "occurrencePrecision" not in selected:
        selected.update(_occurrence_metadata(None))
    selected["resetAt"] = selected.get("occurredAt")
    return {**selected, "eventId": event.get("eventId") or hashlib.sha256(f"{reset_type}:{identity}".encode()).hexdigest()[:24],
            "resetType": reset_type, "resetTypeLabel": _reset_type_label(reset_type),
            "completed": True, "scope": "public-global-report",
            "confirmation": "official-post-mirror" if post and "thsottiaux" in searchable else "community-reported",
            "discoveredAt": discovered_at}


def _xml_local_name(element: Any) -> str:
    tag = getattr(element, "tag", "")
    return str(tag).rsplit("}", 1)[-1].casefold()


def _xml_child(element: Any, name: str) -> Any | None:
    wanted = name.casefold()
    return next((child for child in list(element) if _xml_local_name(child) == wanted), None)


def _xml_child_text(element: Any, name: str) -> str | None:
    child = _xml_child(element, name)
    return child.text if child is not None else None


def parse_public_feed(raw: bytes | str) -> list[dict]:
    if isinstance(raw, bytes):
        if len(raw) > MAX_FEED_BYTES:
            raise RadarSchemaError("公开雷达 RSS 超过响应大小限制。")
        try:
            xml_text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RadarSchemaError("公开雷达 RSS 不是有效的 UTF-8 文本。") from exc
    else:
        xml_text = raw
        if len(xml_text.encode("utf-8")) > MAX_FEED_BYTES:
            raise RadarSchemaError("公开雷达 RSS 超过响应大小限制。")
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise RadarSchemaError("公开雷达 RSS 不是有效的 XML。") from exc
    channel = _xml_child(root, "channel") if _xml_local_name(root) == "rss" else None
    if channel is None:
        raise RadarSchemaError("公开雷达 RSS 缺少频道信息。")
    events: list[dict] = []
    for item in [child for child in list(channel) if _xml_local_name(child) == "item"][:64]:
        title = _text(_xml_child_text(item, "title"), 300)
        published = _parse_time(_xml_child_text(item, "pubDate"))
        if not title or published is None:
            continue
        link = _safe_public_link(
            _xml_child_text(item, "link"),
            allowed_hosts={"codexradar.com", "www.codexradar.com", "codex-reset-radar.pages.dev"},
        )
        description = _plain_html(_xml_child_text(item, "description") or "")
        reset_type = _reset_type(title, description)
        effective = _effective_reset_time(description, published)
        events.append(_attach_public_translation({
            "title": title,
            "url": link,
            "guid": _text(_xml_child_text(item, "guid"), 500),
            "publishedAt": _iso(published),
            "description": description,
            "source": "public-rss",
            "sourceLabel": "Codex 雷达公开记录",
            "resetType": reset_type,
            "resetTypeLabel": _reset_type_label(reset_type),
            "completed": _completed_reset_event(title, description),
            "resetAt": _iso(effective) if effective else None,
            **_occurrence_metadata(effective.isoformat(timespec="minutes") if effective else None,
                                   basis="rss-explicit-effective-minute"),
        }))
    if not events:
        raise RadarSchemaError("公开雷达 RSS 没有可用事件。")
    return events


def parse_public_forecast(raw: bytes | str) -> dict:
    payload = _decode_json(raw, label="Public reset forecast", max_bytes=MAX_FEED_BYTES * 2)
    if not isinstance(payload, dict) or not isinstance(payload.get("forecast"), dict):
        raise RadarSchemaError("公开重置预测的数据结构无效。")
    forecast = _mapping(payload.get("forecast"))
    posts = []
    for item in _list(payload.get("tiboPosts"))[:80]:
        if not isinstance(item, dict):
            continue
        post_id = _text(item.get("guid"), 120)
        published = _text(item.get("pubDate") or item.get("publishedAt"), 80)
        if not post_id or not published:
            continue
        posts.append(_attach_public_translation({
            "id": post_id,
            "title": _text(item.get("title"), 1600),
            "context": _text(item.get("context"), 1600),
            "publishedAt": published,
            "url": _safe_public_link(item.get("link")),
            "activityType": _text(item.get("activityType"), 40),
            "replyToAuthor": _text(item.get("replyToAuthor"), 120),
            "replyToId": _text(item.get("replyToGuid"), 120),
        }))
    reset_events = []
    reset_history = []
    for item in _list(payload.get("resetEvents"))[:80]:
        if not isinstance(item, dict):
            continue
        event_id = _text(item.get("guid"), 120)
        status = _text(item.get("persistedResetStatus") or item.get("resetStatus"), 60) or "unknown"
        title = _text(item.get("title"), 1800)
        context = _text(item.get("context"), 1800)
        reset_at = _text(
            item.get("activationAt") or item.get("effectiveAt") or item.get("publishedAt") or item.get("pubDate"),
            80,
        )
        if not event_id or not reset_at:
            continue
        # This mirror also emits synthetic activationAt copied from pubDate.
        # Such a value confirms the post time, not the actual rollout time.
        published_at = _text(item.get("publishedAt") or item.get("pubDate"), 80)
        activation_at = item.get("activationAt")
        if activation_at and _parse_time(activation_at) == _parse_time(published_at):
            activation_at = None
        occurrence = _occurrence_metadata(activation_at)
        normalized = _attach_public_translation({
            "id": event_id,
            "title": title,
            "context": context,
            "status": status,
            "publishedAt": published_at,
            "effectiveAt": _text(item.get("effectiveAt"), 80),
            "resetAt": occurrence.get("occurredAt"),
            **occurrence,
            "scheduledAt": _text(item.get("effectiveAt"), 80),
            "url": _safe_public_link(item.get("link")),
            "source": "thsottiaux-public-post",
            "sourceLabel": "@thsottiaux 公开帖子",
            "resetType": _reset_type(title, context),
        })
        normalized["resetTypeLabel"] = _reset_type_label(normalized["resetType"])
        reset_events.append(normalized)
        if status.casefold() == "completed":
            reset_history.append(normalized)
    reset_history.sort(key=lambda item: str(item.get("occurredAt") or item.get("publishedAt") or ""), reverse=True)
    score = _safe_number(forecast.get("score"))
    breakdown = []
    for item in _list(forecast.get("breakdown"))[:12]:
        if not isinstance(item, dict):
            continue
        label = _text(item.get("label"), 80)
        points = _safe_number(item.get("points"))
        if label and points is not None:
            breakdown.append({"label": label, "points": points})

    def normalize_signal_post(item: Any, signal_type: str, label: str) -> dict | None:
        if not isinstance(item, dict):
            return None
        post_id = _text(item.get("guid"), 120)
        published = _text(item.get("publishedAt") or item.get("pubDate"), 80)
        title = _text(item.get("title"), 2400)
        if not post_id or not published or not title:
            return None
        safe_url = _safe_public_link(
            item.get("link"),
            allowed_hosts={"x.com", "www.x.com", "twitter.com", "www.twitter.com"},
        )
        assessment = _mapping(item.get("tweetAssessment")) or _mapping(item.get("vaguepostAssessment"))
        reason = _text(assessment.get("reason"), 500)
        confidence = _safe_number(assessment.get("resetSignalStrength"))
        if confidence is None:
            raw_confidence = _safe_number(assessment.get("confidence"))
            confidence = raw_confidence * 100 if raw_confidence is not None and raw_confidence <= 1 else raw_confidence
        return _attach_public_translation({
            "id": post_id,
            "signalType": signal_type,
            "label": label,
            "title": title,
            "context": _text(item.get("context"), 1800),
            "publishedAt": published,
            "url": safe_url,
            "reason": reason,
            "confidence": confidence,
        })

    signal = _mapping(payload.get("tiboSignal"))
    signal_candidates: list[dict] = []
    signal_groups = (
        ("pendingResetAnnouncements", "pending-reset", "最新额度重置预告"),
        ("completedResetAnnouncements", "completed-reset", "最新已完成重置"),
        ("releaseHints", "release-hint", "最新产品发布信号"),
        ("eventHints", "event-hint", "最新 OpenAI 活动信号"),
        ("vagueposts", "vague-hint", "最新模糊推文信号"),
    )
    for key, signal_type, label in signal_groups:
        for item in _list(signal.get(key))[:40]:
            normalized = normalize_signal_post(item, signal_type, label)
            if normalized:
                signal_candidates.append(normalized)
    signal_candidates.sort(key=lambda item: str(item.get("publishedAt") or ""), reverse=True)

    score_history = []
    for entry in _list(payload.get("history"))[:168]:
        if not isinstance(entry, dict):
            continue
        at = _text(entry.get("at"), 80)
        from_score = _safe_number(entry.get("fromScore"))
        to_score = _safe_number(entry.get("toScore"))
        if not at or from_score is None or to_score is None:
            continue
        changes = []
        for change in _list(entry.get("changes"))[:16]:
            if not isinstance(change, dict):
                continue
            change_label = _text(change.get("label"), 120)
            delta = _safe_number(change.get("delta"))
            if not change_label or delta is None:
                continue
            details = []
            for detail in _list(change.get("details"))[:8]:
                if not isinstance(detail, dict):
                    continue
                action = _text(detail.get("action"), 120)
                name = _text(detail.get("name"), 2400)
                if not action and not name:
                    continue
                safe_url = _safe_public_link(
                    detail.get("url"),
                    allowed_hosts={"x.com", "www.x.com", "twitter.com", "www.twitter.com"},
                )
                details.append({
                    "action": action,
                    "name": name,
                    "kind": _text(detail.get("kind"), 40),
                    "url": safe_url,
                })
            changes.append({
                "label": change_label,
                "delta": delta,
                "from": _safe_number(change.get("from")),
                "to": _safe_number(change.get("to")),
                "details": details,
            })
        score_history.append({
            "at": at,
            "fromScore": from_score,
            "toScore": to_score,
            "scoreDelta": _safe_number(entry.get("scoreDelta")),
            "changes": changes,
        })
    score_history.sort(key=lambda item: str(item.get("at") or ""), reverse=True)
    return {
        "checkedAt": _text(payload.get("fetchedAt"), 80),
        "nextSourceRefreshAt": _text(payload.get("nextRefreshAt"), 80),
        "predictor": {
            "score": score,
            "probability": score,
            "latestResetAt": _text(forecast.get("latestResetAt"), 80),
            "daysSinceReset": _safe_number(forecast.get("daysSinceReset")),
            "hoursSinceReset": _safe_number(forecast.get("hoursSinceReset")),
            "resetAnnounced": bool(forecast.get("resetAnnounced")),
            "breakdown": breakdown,
            "sourceLabel": "Will Codex Quota Reset 公开预测",
        },
        "posts": posts,
        "latestSignal": signal_candidates[0] if signal_candidates else None,
        "scoreHistory": score_history,
        "resetEvents": reset_events,
        "resetHistory": reset_history,
        "sourceErrors": _bounded_json(_mapping(payload.get("sourceErrors"))),
    }


def parse_openai_status_incidents(raw: bytes | str) -> list[dict]:
    payload = _decode_json(raw, label="OpenAI status incidents", max_bytes=MAX_FEED_BYTES * 2)
    if not isinstance(payload, dict) or not isinstance(payload.get("incidents"), list):
        raise RadarSchemaError("OpenAI 状态事件的数据结构无效。")
    incidents = []
    for item in payload["incidents"][:100]:
        if not isinstance(item, dict):
            continue
        updates = _list(item.get("incident_updates"))
        bodies = [_text(update.get("body"), 1000) for update in updates[:20] if isinstance(update, dict)]
        searchable = f"{_text(item.get('name'), 500) or ''} {' '.join(body or '' for body in bodies)}".casefold()
        if "codex" not in searchable:
            continue
        incident_id = _text(item.get("id"), 120)
        incidents.append(_attach_translation_fields({
            "id": incident_id,
            "name": _text(item.get("name"), 500),
            "status": _text(item.get("status"), 80),
            "impact": _text(item.get("impact"), 80),
            "createdAt": _text(item.get("created_at"), 80),
            "updatedAt": _text(item.get("updated_at"), 80),
            "resolvedAt": _text(item.get("resolved_at"), 80),
            "latestUpdate": next((body for body in bodies if body), None),
            "url": f"https://status.openai.com/incidents/{incident_id}" if incident_id else "https://status.openai.com/history",
            "sourceLabel": "OpenAI 官方状态页",
        }, fields=("name", "status", "impact", "latestUpdate")))
    return incidents


def _signal_time(item: Mapping[str, Any], *keys: str) -> datetime | None:
    for key in keys:
        parsed = _parse_time(item.get(key))
        if parsed is not None:
            return parsed
    return None


def _signal_text(item: Mapping[str, Any]) -> str:
    return " ".join(
        _text(item.get(key), 2000) or ""
        for key in ("title", "context", "name", "latestUpdate")
    ).casefold()


def classify_reset_alert(forecast, status_incidents, *, since, now):
    return monitor_engine.classify_reset_alert(forecast, status_incidents, since=since, now=now)


def next_monitor_time(now: datetime, last_slot: Any = None) -> datetime:
    return monitor_engine.next_monitor_time(now, last_slot)


def _default_state() -> dict:
    return {
        "schemaVersion": CACHE_SCHEMA_VERSION,
        "validators": {},
        "intelligenceComponents": {},
        "postTranslations": {},
        "machineTranslations": {},
        "sections": {},
        "resetHistoryLedger": {},
        "lastAttemptAt": None,
        "lastSuccessAt": None,
        "quotaLastAttemptAt": None,
        "failureCount": 0,
        "nextAllowedAt": None,
        "sectionErrors": {},
        "monitor": {
            "lastRunAt": None,
            "lastRunSlot": None,
            "lastSuccessAt": None,
            "lastResult": "尚未检查",
            "nextCheckAt": None,
            "lastError": None,
            "lastAlert": None,
            "seenAlertSignatures": [],
            "runs": [],
        },
    }


def _merge_cache_changes(current: Any, previous: Any, desired: Any) -> Any:
    """Apply only this writer's changes over the latest on-disk snapshot."""
    if desired == previous:
        return copy.deepcopy(current)
    if isinstance(previous, Mapping) and isinstance(desired, Mapping):
        result = copy.deepcopy(dict(current)) if isinstance(current, Mapping) else {}
        for key, value in desired.items():
            if key not in previous:
                result[key] = copy.deepcopy(value)
            else:
                result[key] = _merge_cache_changes(
                    result.get(key), previous[key], value
                )
        for key, value in previous.items():
            if key not in desired and result.get(key) == value:
                result.pop(key, None)
        return result
    return copy.deepcopy(desired)


@contextmanager
def _exclusive_cache_file_lock(lock_path: Path, timeout: float = 3.0):
    """Serialize cache read/merge/write across application processes."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    acquired = False
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise RadarError("雷达缓存正被另一个进程更新，请稍后重试。")
                time.sleep(0.01)
        yield
    finally:
        if acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


class RadarCache:
    """Small atomic JSON cache containing normalized public data only."""
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else _default_cache_path()

    def load(self) -> dict:
        try:
            if self.path.stat().st_size > MAX_CACHE_BYTES:
                return _default_state()
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return _default_state()
        if isinstance(value, dict) and value.get("schemaVersion") == 4:
            # Revalidate changed parsers without losing historical evidence or
            # previously delivered alert identities during a routine upgrade.
            migrated = _default_state()
            for field in ("resetHistoryLedger", "monitor", "machineTranslations"):
                if isinstance(value.get(field), dict):
                    migrated[field] = value[field]
            return migrated
        if not isinstance(value, dict) or value.get("schemaVersion") != CACHE_SCHEMA_VERSION:
            return _default_state()
        if not isinstance(value.get("sections"), dict) or not isinstance(value.get("validators"), dict):
            return _default_state()
        if "intelligenceComponents" in value and not isinstance(value.get("intelligenceComponents"), dict):
            return _default_state()
        state = _default_state()
        state.update(value)
        return state

    def save(
        self,
        state: Mapping[str, Any],
        *,
        previous: Mapping[str, Any] | None = None,
    ) -> dict:
        """Atomically save state without erasing independent process updates."""
        desired = copy.deepcopy(dict(state))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_cache_file_lock(self.path.with_suffix(self.path.suffix + ".lock")):
            if previous is not None:
                desired = _merge_cache_changes(self.load(), dict(previous), desired)
            encoded = json.dumps(desired, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
            if len(encoded) > MAX_CACHE_BYTES:
                raise RadarError("雷达缓存超过安全大小限制。")
            handle, temp_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
            )
            temporary = Path(temp_name)
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
        return desired

    def health(self) -> dict:
        """Report cache corruption explicitly instead of silently replacing it."""

        if not self.path.exists():
            return {
                "healthy": True,
                "status": "ok",
                "exists": False,
                "path": str(self.path),
                "detail": "雷达尚未创建本地缓存。",
            }
        try:
            size = self.path.stat().st_size
            if size <= 0:
                raise RadarSchemaError("雷达缓存为空。")
            if size > MAX_CACHE_BYTES:
                raise RadarSchemaError("雷达缓存超过安全大小限制。")
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schemaVersion") not in (4, CACHE_SCHEMA_VERSION):
                raise RadarSchemaError("雷达缓存版本或结构无效。")
            if not isinstance(value.get("sections"), dict) or not isinstance(value.get("validators"), dict):
                raise RadarSchemaError("雷达缓存缺少 sections 或 validators。")
            if "intelligenceComponents" in value and not isinstance(value.get("intelligenceComponents"), dict):
                raise RadarSchemaError("雷达缓存中的综合智能组件无效。")
            return {
                "healthy": True,
                "status": "ok",
                "exists": True,
                "path": str(self.path),
                "size": size,
                "sectionCount": len(value["sections"]),
                "detail": "雷达缓存结构正常。",
            }
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RadarSchemaError) as exc:
            return {
                "healthy": False,
                "status": "warning",
                "exists": True,
                "path": str(self.path),
                "detail": f"雷达缓存损坏，可安全清理后手动刷新：{str(exc)[:220]}",
            }

    def clear_corrupt(self) -> dict:
        with _exclusive_cache_file_lock(self.path.with_suffix(self.path.suffix + ".lock")):
            status = self.health()
            if status["healthy"]:
                return {"changed": False, "path": str(self.path), "health": status}
            self.path.unlink(missing_ok=True)
            return {"changed": True, "path": str(self.path), "health": self.health()}


@dataclass(frozen=True)
class _HTTPResult:
    status: int
    body: bytes
    headers: dict[str, str]

    @property
    def not_modified(self) -> bool:
        return self.status == 304


def _header_map(headers: Any) -> dict[str, str]:
    if headers is None:
        return {}
    try:
        return {str(key).casefold(): str(value) for key, value in headers.items()}
    except AttributeError:
        return {}


def _retry_after(value: str | None, now: datetime) -> datetime | None:
    if not value:
        return None
    try:
        seconds = max(0, int(value.strip()))
        return now + timedelta(seconds=min(seconds, int(RETRY_AFTER_MAX.total_seconds())))
    except (ValueError, OverflowError):
        parsed = _parse_time(value)
        if parsed is None:
            return None
        return min(max(parsed, now), now + RETRY_AFTER_MAX)


class _HTTPClient:
    def __init__(self, opener: Callable[..., Any] | None, timeout: float, clock: Callable[[], datetime]):
        self.opener = opener or urllib.request.build_opener(_SafeRadarRedirectHandler()).open
        self.timeout = max(1.0, min(float(timeout), 30.0))
        self.clock = clock

    def get(
        self,
        url: str,
        *,
        accept: str,
        max_bytes: int,
        validators: Mapping[str, Any],
        _retry_transient_tls: bool = True,
    ) -> _HTTPResult:
        _validate_public_source_url(url)
        headers = {
            "Accept": accept,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": PUBLIC_HTML_URL,
            "User-Agent": USER_AGENT,
        }
        etag = _text(validators.get("etag"), 500)
        modified = _text(validators.get("lastModified"), 500)
        if etag:
            headers["If-None-Match"] = etag
        if modified:
            headers["If-Modified-Since"] = modified
        request = urllib.request.Request(url, headers=headers)
        try:
            response = self.opener(request, timeout=self.timeout)
            with response:
                status = int(getattr(response, "status", response.getcode()))
                response_headers = _header_map(getattr(response, "headers", {}))
                length = response_headers.get("content-length")
                if length:
                    try:
                        if int(length) > max_bytes:
                            raise RadarError("雷达响应超过安全大小限制。")
                    except ValueError:
                        pass
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise RadarError("雷达响应超过安全大小限制。")
                if status == 304:
                    return _HTTPResult(status, b"", response_headers)
                if status != 200:
                    retry_at = (
                        _retry_after(response_headers.get("retry-after"), _aware(self.clock()))
                        if status == 429
                        else None
                    )
                    raise RadarHTTPError(
                        f"雷达数据源返回 HTTP {status}。",
                        status=status,
                        retry_after=retry_at,
                    )
                return _HTTPResult(status, body, response_headers)
        except urllib.error.HTTPError as exc:
            try:
                headers_map = _header_map(exc.headers)
                if exc.code == 304:
                    return _HTTPResult(304, b"", headers_map)
                retry_at = (
                    _retry_after(headers_map.get("retry-after"), _aware(self.clock()))
                    if exc.code == 429
                    else None
                )
                raise RadarHTTPError(
                    f"雷达数据源返回 HTTP {exc.code}。",
                    status=exc.code,
                    retry_after=retry_at,
                ) from exc
            finally:
                exc.close()
        except RadarError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            detail = str(exc).casefold()
            if _retry_transient_tls and any(
                marker in detail for marker in ("unexpected_eof", "eof occurred", "connection reset", "forcibly closed")
            ):
                return self.get(
                    url,
                    accept=accept,
                    max_bytes=max_bytes,
                    validators=validators,
                    _retry_transient_tls=False,
                )
            raise RadarHTTPError("暂时无法连接公开雷达，请稍后手动刷新。") from exc


def _validator(result: _HTTPResult, previous: Mapping[str, Any]) -> dict:
    return {
        "etag": result.headers.get("etag") or previous.get("etag"),
        "lastModified": result.headers.get("last-modified") or previous.get("lastModified"),
    }


def _window_metadata(value: Any) -> dict | None:
    if not isinstance(value, Mapping):
        return None
    reset_at = _text(value.get("resetAt") or value.get("reset_at"), 80)
    if not reset_at:
        return None
    result: dict[str, Any] = {"resetAt": reset_at}
    for key in ("remainingPercent", "usedPercent", "windowMinutes"):
        number = _safe_number(value.get(key))
        if number is not None:
            result[key] = number
    return result


def _credit_metadata(value: Any) -> dict | None:
    if not isinstance(value, Mapping):
        return None
    expires_at = _text(value.get("expiresAt") or value.get("expires_at"), 80)
    credit_id = _text(value.get("id"), 160)
    if not expires_at and not credit_id:
        return None
    result = {
        "id": credit_id,
        "status": _text(value.get("status"), 80),
        "grantedAt": _text(value.get("grantedAt") or value.get("granted_at") or value.get("issuedAt") or value.get("issued_at"), 80),
        "expiresAt": expires_at,
        "resetType": _text(value.get("resetType") or value.get("reset_type"), 120),
        "title": _text(value.get("title") or value.get("label") or value.get("name"), 240),
        "description": _text(value.get("description"), 500),
    }
    return {key: item for key, item in result.items() if item is not None}


def merge_reset_metadata(public_reset: Mapping[str, Any] | None, accounts: Iterable[Mapping[str, Any]] | None) -> dict:
    """Merge public events with allowlisted local reset metadata only.

    Token, cookie, authorization, provider, and arbitrary account fields are
    never copied. The merged account view is not written to the public cache.
    """
    result = _bounded_json(dict(public_reset or {}))
    if not isinstance(result, dict):
        result = {}
    public_events = [item for item in _list(result.get("events")) if isinstance(item, dict)][:64]
    local_accounts: list[dict] = []
    reset_history = [item for item in _list(result.get("resetHistory")) if isinstance(item, dict)]
    if not reset_history:
        reset_history = [item for item in public_events if item.get("completed")][:80]
    for raw_account in list(accounts or [])[:200]:
        if not isinstance(raw_account, Mapping):
            continue
        usage = raw_account.get("usage") if isinstance(raw_account.get("usage"), Mapping) else {}
        account_id = _text(raw_account.get("id"), 160)
        account_name = _text(raw_account.get("name") or raw_account.get("label"), 160)
        account: dict[str, Any] = {
            "id": account_id,
            "name": account_name,
            "plan": _text(usage.get("planLabel") or usage.get("plan"), 80),
            "updatedAt": _text(usage.get("updatedAt"), 80),
            "windows": [],
            "windowsByKind": {},
            "resetCredits": None,
        }
        aliases = {
            "primary": ("primary", "fiveHour", "five_hour"),
            "secondary": ("secondary", "sevenDay", "seven_day"),
            "weekly": ("weekly",),
        }
        for normalized, names in aliases.items():
            window = next((_window_metadata(usage.get(name)) for name in names if usage.get(name) is not None), None)
            if window:
                normalized_window = {"kind": normalized, **window}
                account["windows"].append(normalized_window)
                account["windowsByKind"][normalized] = window
        reset_credits = usage.get("resetCredits")
        if not isinstance(reset_credits, Mapping):
            candidate = raw_account.get("resetCredits")
            reset_credits = candidate if isinstance(candidate, Mapping) else None
        if isinstance(reset_credits, Mapping):
            try:
                available_count = max(0, min(1000, int(reset_credits.get("availableCount") or 0)))
            except (TypeError, ValueError):
                available_count = 0
            credits = [item for item in (_credit_metadata(value) for value in _list(reset_credits.get("credits"))[:100]) if item]
            account["resetCredits"] = {
                "availableCount": available_count,
                "detailsAvailable": bool(reset_credits.get("detailsAvailable")),
                "checkedAt": _text(reset_credits.get("checkedAt"), 80),
                "stale": bool(reset_credits.get("stale")),
                "credits": credits,
            }
        if account["windows"] or account["resetCredits"] is not None:
            local_accounts.append(account)
    reset_history = [event for event in reset_history if event.get("occurredAt") or event.get("resetAt") or event.get("publishedAt") or event.get("discoveredAt")]
    reset_history.sort(key=lambda event: str(event.get("resetAt") or event.get("publishedAt")), reverse=True)
    result["events"] = public_events
    result["localAccounts"] = local_accounts
    result["resetHistory"] = reset_history
    # Keep the legacy key for old frontends, but it now contains only genuine
    # historical reset events. Future weekly reset times and card expiries are
    # deliberately excluded.
    result["timeline"] = reset_history
    return result


class RadarService:
    """Cache-first public radar service with per-channel refresh policy."""
    def __init__(
        self,
        cache_path: str | Path | None = None,
        *,
        opener: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.cache = RadarCache(cache_path)
        self.clock = clock or _utc_now
        self.http = _HTTPClient(opener, timeout, self.clock)
        self._lock = threading.RLock()
        self._state = self.cache.load()
        self._persisted_state = copy.deepcopy(self._state)
        self._startup_attempted = False

    def _now(self) -> datetime:
        return _aware(self.clock())

    def cache_health(self) -> dict:
        return self.cache.health()

    def repair_cache(self) -> dict:
        with self._lock:
            result = self.cache.clear_corrupt()
            self._state = self.cache.load()
            self._persisted_state = copy.deepcopy(self._state)
            self._startup_attempted = False
            return result

    def _persist_state(self) -> None:
        self._state = self.cache.save(
            self._state,
            previous=self._persisted_state,
        )
        self._persisted_state = copy.deepcopy(self._state)

    def _retain_reset_history(self, reset_data: dict, now: datetime) -> None:
        ledger = dict(_mapping(self._state.get("resetHistoryLedger")))
        previous = _mapping(_mapping(_mapping(self._state.get("sections")).get("reset")).get("data"))
        forecast = _mapping(reset_data.get("forecastSignals"))
        candidates = [*_list(previous.get("resetHistory")), *_list(reset_data.get("resetHistory")),
                      *_list(forecast.get("resetHistory")), *_list(reset_data.get("events"))]
        for event in candidates:
            if not isinstance(event, Mapping):
                continue
            record = _history_record(event, _iso(now))
            if record is None:
                continue
            key = record["eventId"]
            old = _mapping(ledger.get(key))
            record["discoveredAt"] = old.get("discoveredAt") or record["discoveredAt"]
            if old.get("occurredAt") and not record.get("occurredAt"):
                for field in ("occurredAt", "occurrencePrecision", "occurrenceBasis"):
                    record[field] = old.get(field)
            record["resetAt"] = record.get("occurredAt")
            ledger[key] = {**old, **record}
        self._state["resetHistoryLedger"] = ledger
        reset_data["resetHistory"] = sorted(ledger.values(), key=lambda event: str(
            event.get("occurredAt") or event.get("publishedAt") or event.get("discoveredAt") or ""), reverse=True)

    def _evaluate_refreshed_forecast(self, forecast: Mapping[str, Any], now: datetime) -> dict:
        return monitor_engine.evaluate_reset_refresh(self, {"forecastSignals": forecast}, now)

    def _record_reset_refresh_failure(self, now: datetime, error: Exception) -> None:
        monitor = {**_default_state()["monitor"], **_mapping(self._state.get("monitor"))}
        monitor.update(lastRunAt=_iso(now), lastCheckMode="manual-refresh",
                       lastResult="预测源检查失败，保留上次结果", lastError=str(error)[:300])
        monitor["runs"] = [{"at": _iso(now), "result": monitor["lastResult"], "mode": "manual-refresh"},
                           *_list(monitor.get("runs"))[:95]]
        self._state["monitor"] = monitor

    def _can_attempt(self, now: datetime) -> bool:
        next_allowed = _parse_time(self._state.get("nextAllowedAt"))
        return (
            next_allowed is None
            or now >= next_allowed
            or next_allowed > now + RETRY_AFTER_MAX
        )

    def _quota_due(self, now: datetime) -> bool:
        attempted = _parse_time(self._state.get("quotaLastAttemptAt"))
        return (
            attempted is None
            or attempted > now + timedelta(minutes=5)
            or now - attempted >= QUOTA_REFRESH_INTERVAL
        )

    def _record_failure(self, exc: Exception, now: datetime, sections: set[str]) -> None:
        try:
            previous_failures = max(0, int(self._state.get("failureCount") or 0))
        except (TypeError, ValueError, OverflowError):
            previous_failures = 0
        failures = min(20, previous_failures + 1)
        delay = min(BACKOFF_MAX.total_seconds(), BACKOFF_BASE.total_seconds() * (2 ** (failures - 1)))
        next_allowed = now + timedelta(seconds=delay)
        if isinstance(exc, RadarHTTPError) and exc.retry_after is not None:
            next_allowed = max(next_allowed, exc.retry_after)
        self._state["failureCount"] = failures
        self._state["nextAllowedAt"] = _iso(next_allowed)
        errors = self._state.setdefault("sectionErrors", {})
        for section in sections:
            errors[section] = str(exc)[:300]

    def _fetch_intelligence(self) -> tuple[dict | None, bool]:
        validators = self._state.setdefault("validators", {})
        components = self._state.get("intelligenceComponents")
        if not isinstance(components, dict):
            components = {}
            self._state["intelligenceComponents"] = components

        def fetch_component(name: str, url: str, label: str) -> tuple[dict, bool]:
            validator_name = f"intelligence{name.title()}"
            previous = _mapping(validators.get(validator_name))
            response = self.http.get(
                url,
                accept="application/json",
                max_bytes=MAX_PUBLIC_BYTES,
                validators=previous,
            )
            validators[validator_name] = _validator(response, previous)
            cached = _mapping(components.get(name))
            if response.not_modified and _list(cached.get("points")):
                return cached, True
            if response.not_modified:
                response = self.http.get(
                    url,
                    accept="application/json",
                    max_bytes=MAX_PUBLIC_BYTES,
                    validators={},
                )
                validators[validator_name] = _validator(response, {})
                if response.not_modified:
                    raise RadarSchemaError(f"{label}返回未修改，但本地没有可用组件缓存。")
            parsed = _intelligence_component(response.body, label=label)
            components[name] = parsed
            return parsed, False

        software, software_not_modified = fetch_component(
            "software", PUBLIC_INTELLIGENCE_URL, "软件工程能力数据"
        )
        visual, visual_not_modified = fetch_component(
            "visual", PUBLIC_VISUAL_INTELLIGENCE_URL, "视觉空间推理数据"
        )
        cached_intelligence = _mapping(
            _mapping(self._state.get("sections")).get("intelligence")
        )
        cached_data = _mapping(cached_intelligence.get("data"))
        if software_not_modified and visual_not_modified and _list(cached_data.get("items")):
            return None, True
        return _compose_intelligence_components(software, visual), False

    def _fetch_summary(self) -> tuple[dict | None, str, bool]:
        validators = self._state.setdefault("validators", {})
        previous = _mapping(validators.get("summary"))
        try:
            response = self.http.get(
                PUBLIC_SUMMARY_URL,
                accept="application/json",
                max_bytes=MAX_SUMMARY_BYTES,
                validators=previous,
            )
            validators["summary"] = _validator(response, previous)
            if response.not_modified:
                return None, "json", True
            return parse_public_json(response.body), "json", False
        except RadarHTTPError as exc:
            if exc.status not in {404, 410}:
                raise
        except RadarSchemaError:
            pass
        html_previous = _mapping(validators.get("html"))
        response = self.http.get(
            PUBLIC_HTML_URL,
            accept="text/html, application/xhtml+xml;q=0.9",
            max_bytes=MAX_HTML_BYTES,
            validators=html_previous,
        )
        validators["html"] = _validator(response, html_previous)
        if response.not_modified:
            return None, "html", True
        return parse_public_html(response.body), "html", False

    def _fetch_events(self) -> tuple[list[dict] | None, bool]:
        validators = self._state.setdefault("validators", {})
        previous = _mapping(validators.get("feed"))
        response = self.http.get(
            PUBLIC_FEED_URL,
            accept="application/rss+xml, application/xml;q=0.9",
            max_bytes=MAX_FEED_BYTES,
            validators=previous,
        )
        validators["feed"] = _validator(response, previous)
        if response.not_modified:
            return None, True
        return parse_public_feed(response.body), False

    def _fetch_forecast(self) -> tuple[dict | None, bool]:
        validators = self._state.setdefault("validators", {})
        previous = _mapping(validators.get("forecast"))
        response = self.http.get(
            PUBLIC_FORECAST_URL,
            accept="application/json",
            max_bytes=MAX_FEED_BYTES * 2,
            validators=previous,
        )
        validators["forecast"] = _validator(response, previous)
        if response.not_modified:
            return None, True
        return parse_public_forecast(response.body), False

    def _fetch_public_post_translations(self) -> dict[str, dict[str, str]]:
        """Read the canonical site's rendered translation pairs.

        The forecast API intentionally keeps the source post text.  The public
        page publishes the reviewed Chinese rendering beside it, so this small
        supplemental request lets the desktop app show the same translation
        without requiring an API key or sending text to a third-party service.
        """
        validators = self._state.setdefault("validators", {})
        previous = _mapping(validators.get("htmlTranslations"))
        response = self.http.get(
            PUBLIC_HTML_URL,
            accept="text/html, application/xhtml+xml;q=0.9",
            max_bytes=MAX_HTML_BYTES,
            validators=previous,
        )
        validators["htmlTranslations"] = _validator(response, previous)
        if response.not_modified:
            cached = _mapping(self._state.get("postTranslations"))
            return {str(key): _mapping(value) for key, value in cached.items() if isinstance(value, dict)}
        translations = parse_public_html_translations(response.body)
        self._state["postTranslations"] = translations
        self._state["publicHtmlCards"] = parse_public_html_cards(response.body)
        return translations

    @staticmethod
    def _merge_forecast_translations(forecast: dict, translations: Mapping[str, Any]) -> dict:
        if not translations:
            return forecast
        for collection_name in ("posts", "resetEvents", "resetHistory"):
            collection = forecast.get(collection_name)
            if not isinstance(collection, list):
                continue
            for item in collection:
                if not isinstance(item, dict):
                    continue
                item_translation = translations.get(str(item.get("id") or ""))
                _attach_public_translation(item, item_translation if isinstance(item_translation, Mapping) else None)
        latest = forecast.get("latestSignal")
        if isinstance(latest, dict):
            item_translation = translations.get(str(latest.get("id") or ""))
            _attach_public_translation(latest, item_translation if isinstance(item_translation, Mapping) else None)
        return forecast

    def _machine_translate_public_text(self, value: Any) -> str:
        original = _text(value, 2400) or ""
        if not original:
            return ""
        reviewed = _auto_translate_public_text(original)
        if reviewed:
            return reviewed
        cache_key = hashlib.sha256(original.encode("utf-8")).hexdigest()
        cache = self._state.setdefault("machineTranslations", {})
        cached = _mapping(cache.get(cache_key))
        cached_text = _text(cached.get("translationZh"), 2400) or ""
        if cached_text and _contains_chinese(cached_text):
            return cached_text

        translated_chunks: list[str] = []
        for chunk in _translation_chunks(original):
            query = urlencode({"q": chunk, "langpair": "en|zh-CN", "mt": "1"})
            response = self.http.get(
                f"{PUBLIC_TRANSLATE_URL}?{query}",
                accept="application/json",
                max_bytes=64 * 1024,
                validators={},
            )
            translated_chunks.append(parse_machine_translation(response.body))
        translated = " ".join(part.strip() for part in translated_chunks if part.strip()).strip()
        if not translated:
            return ""
        cache[cache_key] = {
            "translationZh": translated,
            "updatedAt": _iso(self._now()),
            "source": "mymemory",
        }
        while len(cache) > 128:
            cache.pop(next(iter(cache)))
        return translated

    def _translate_public_record(
        self,
        item: dict,
        *,
        fields: Iterable[str] = _PUBLIC_TRANSLATION_FIELDS,
    ) -> tuple[int, list[str]]:
        """Fill every requested public display field and retain retry state."""
        requested = tuple(fields)
        _attach_translation_fields(item, fields=requested)
        translated_count = 0
        errors: list[str] = []
        for field in requested:
            meta = _mapping(_mapping(item.get("translationFields")).get(field))
            if not meta or meta.get("state") == "complete":
                continue
            original = _text(meta.get("original") or item.get(field), 2400) or ""
            if not original:
                continue
            try:
                local_reviewed = _auto_translate_public_text(original)
                translated = local_reviewed or self._machine_translate_public_text(original)
                if not translated or not _contains_chinese(translated):
                    raise RadarSchemaError("公开翻译接口没有返回有效中文。")
                _register_translation_field(
                    item,
                    field,
                    original,
                    translated_zh=translated,
                    source="local-reviewed" if local_reviewed else "mymemory",
                )
                translated_count += 1
            except Exception as exc:
                error = str(exc)[:160] or "公开中文翻译暂时不可用。"
                errors.append(error)
                # Failed values are deliberately not inserted into the shared
                # success cache.  A later refresh sees this field and retries.
                _register_translation_field(item, field, original, failed_error=error)
        _refresh_translation_state(item)
        if "title" in requested:
            title_translation = _translation_candidate(item, "title")
            item["translationZh"] = title_translation or None
            title_meta = _mapping(_mapping(item.get("translationFields")).get("title"))
            item["translationSource"] = _text(title_meta.get("source"), 80) or "unavailable"
        return translated_count, errors

    @staticmethod
    def _forecast_translation_targets(forecast: Mapping[str, Any]) -> list[tuple[dict, tuple[str, ...]]]:
        targets: list[tuple[dict, tuple[str, ...]]] = []
        latest = forecast.get("latestSignal")
        if isinstance(latest, dict):
            targets.append((latest, ("title", "context", "reason", "label", "status", "impact")))
        for collection_name in ("posts", "resetEvents", "resetHistory"):
            for item in _list(forecast.get(collection_name)):
                if isinstance(item, dict):
                    targets.append((item, ("title", "context", "description", "reason", "status", "impact")))
        predictor = _mapping(forecast.get("predictor"))
        for item in _list(predictor.get("breakdown")):
            if isinstance(item, dict):
                targets.append((item, ("label", "description", "reason")))
        for entry in _list(forecast.get("scoreHistory")):
            if not isinstance(entry, dict):
                continue
            for change in _list(entry.get("changes")):
                if not isinstance(change, dict):
                    continue
                targets.append((change, ("label", "description", "reason")))
                for detail in _list(change.get("details")):
                    if isinstance(detail, dict):
                        targets.append((detail, ("action", "name", "description", "reason")))
        # A source may reuse the exact same dict in two collections.
        seen_objects: set[int] = set()
        unique: list[tuple[dict, tuple[str, ...]]] = []
        for item, fields in targets:
            if id(item) in seen_objects:
                continue
            seen_objects.add(id(item))
            unique.append((item, fields))
        return unique

    def _translate_missing_forecast_posts(self, forecast: dict) -> dict:
        """Translate all public forecast text that the radar can display."""
        candidates = self._forecast_translation_targets(forecast)
        translated_count = 0
        errors: list[str] = []
        for item, fields in candidates:
            count, item_errors = self._translate_public_record(item, fields=fields)
            translated_count += count
            errors.extend(item_errors)
        all_fields = [
            _mapping(value)
            for item, _fields in candidates
            for value in _mapping(item.get("translationFields")).values()
        ]
        states = [str(meta.get("state") or "pending") for meta in all_fields]
        complete_items = sum(1 for item, _fields in candidates if item.get("translationState") == "complete")
        partial_items = sum(1 for item, _fields in candidates if item.get("translationState") == "partial")
        failed_items = sum(1 for item, _fields in candidates if item.get("translationState") == "failed")
        pending_items = len(candidates) - complete_items - partial_items - failed_items
        forecast["translationStatus"] = {
            "translated": translated_count,
            "unavailable": sum(1 for state in states if state != "complete"),
            "state": (
                "complete" if states and all(state == "complete" for state in states)
                else "partial" if any(state == "complete" for state in states)
                else "failed" if any(state == "failed" for state in states)
                else "pending" if states else "not-needed"
            ),
            "fields": {"total": len(states), "complete": states.count("complete"), "failed": states.count("failed"), "pending": states.count("pending")},
            "items": {"total": len(candidates), "complete": complete_items, "partial": partial_items, "failed": failed_items, "pending": pending_items},
            "error": errors[0] if errors else None,
            "checkedAt": _iso(self._now()),
        }
        return forecast

    def _translate_reset_payload(self, reset: dict, *, include_forecast: bool = True) -> dict:
        """Normalize every public reset text surface before it reaches cache."""
        self._translate_public_record(reset, fields=("status", "summary", "recommendedAction"))
        for nested_name, fields in (
            ("window", ("title", "label", "message", "description", "status", "impact")),
            ("prediction", ("title", "label", "message", "summary", "reason", "status", "impact")),
            ("tiboPresence", ("title", "label", "message", "summary", "reason", "status", "impact")),
        ):
            nested = reset.get(nested_name)
            if isinstance(nested, dict):
                self._translate_public_record(nested, fields=fields)
        for collection_name in ("events",):
            for item in _list(reset.get(collection_name)):
                if isinstance(item, dict):
                    self._translate_public_record(
                        item,
                        fields=("title", "context", "description", "reason", "status", "impact", "latestUpdate"),
                    )
        forecast = reset.get("forecastSignals")
        if include_forecast and isinstance(forecast, dict):
            self._translate_missing_forecast_posts(forecast)
        return reset

    def _translate_status_incidents(self, incidents: Iterable[dict]) -> list[dict]:
        normalized: list[dict] = []
        for incident in incidents:
            if not isinstance(incident, dict):
                continue
            self._translate_public_record(
                incident,
                fields=("name", "status", "impact", "latestUpdate"),
            )
            normalized.append(incident)
        return normalized

    def _fetch_status_incidents(self) -> tuple[list[dict] | None, bool]:
        validators = self._state.setdefault("validators", {})
        previous = _mapping(validators.get("openaiStatus"))
        response = self.http.get(
            OPENAI_STATUS_INCIDENTS_URL,
            accept="application/json",
            max_bytes=MAX_FEED_BYTES * 2,
            validators=previous,
        )
        validators["openaiStatus"] = _validator(response, previous)
        if response.not_modified:
            return None, True
        return parse_openai_status_incidents(response.body), False

    def _store_section(self, name: str, data: dict, now: datetime, *, url: str, format_name: str, degraded: bool = False) -> None:
        self._state.setdefault("sections", {})[name] = {
            "data": data,
            "fetchedAt": _iso(now),
            "source": {"url": url, "format": format_name, "degraded": degraded},
        }
        self._state.setdefault("sectionErrors", {}).pop(name, None)

    def _refresh(self, sections: set[str]) -> dict:
        now = self._now()
        if not self._can_attempt(now):
            return {"attempted": False, "suppressed": "backoff", "success": False, "changedSections": []}
        retry_after_until: datetime | None = None

        def remember_retry_after(exc: Exception) -> None:
            nonlocal retry_after_until
            if not isinstance(exc, RadarHTTPError) or exc.retry_after is None:
                return
            retry_after_until = max(
                retry_after_until or exc.retry_after,
                exc.retry_after,
            )

        self._state["lastAttemptAt"] = _iso(now)
        if "quota" in sections:
            self._state["quotaLastAttemptAt"] = _iso(now)
        cached_sections = self._state.setdefault("sections", {})
        reset_evaluation = {"alert": None, "newAlert": False}
        changed: set[str] = set()
        succeeded: set[str] = set()
        failures: dict[str, Exception] = {}
        summary = None
        summary_format = "json"
        summary_not_modified = False
        summary_error: Exception | None = None
        try:
            summary, summary_format, summary_not_modified = self._fetch_summary()
        except Exception as exc:
            # Each supplemental endpoint can refresh its own section even when
            # current.json is unavailable.  Decide failures per section below.
            summary_error = exc
            remember_retry_after(exc)

        intelligence_metrics = None
        intelligence_not_modified = False
        reset_events = None
        reset_events_not_modified = False
        forecast = None
        forecast_not_modified = False
        post_translations: dict[str, dict[str, str]] = {}
        supplemental_errors: dict[str, list[str]] = {}
        html_cards = {}
        html_error = None
        if sections & {"quota", "reset"}:
            try:
                post_translations = self._fetch_public_post_translations()
                html_cards = _mapping(self._state.get("publicHtmlCards"))
            except Exception as exc:
                html_error = exc
                remember_retry_after(exc)
                for section in sections & {"quota", "reset"}:
                    supplemental_errors.setdefault(section, []).append(f"公开网页补充失败：{str(exc)[:240]}")
                html_cards = _mapping(self._state.get("publicHtmlCards"))
        if summary_error is not None:
            for name in sections & {"intelligence", "reset"}:
                supplemental_errors.setdefault(name, []).append(
                    f"公开摘要获取失败：{str(summary_error)[:240]}"
                )
        if "intelligence" in sections:
            try:
                intelligence_metrics, intelligence_not_modified = self._fetch_intelligence()
            except Exception as exc:
                remember_retry_after(exc)
                supplemental_errors.setdefault("intelligence", []).append(str(exc)[:300])
        if "reset" in sections:
            try:
                reset_events, reset_events_not_modified = self._fetch_events()
            except Exception as exc:
                remember_retry_after(exc)
                supplemental_errors.setdefault("reset", []).append(str(exc)[:300])
            try:
                forecast, forecast_not_modified = self._fetch_forecast()
            except Exception as exc:
                self._record_reset_refresh_failure(now, exc)
                remember_retry_after(exc)
                supplemental_errors.setdefault("reset", []).append(str(exc)[:300])
            if isinstance(forecast, dict):
                if any(
                    isinstance(item, dict) and item.get("id")
                    for item in (forecast.get("posts", []) or [])
                ):
                    self._merge_forecast_translations(forecast, post_translations)
                self._translate_missing_forecast_posts(forecast)

        status_incidents = []
        status_refreshed = False
        if "reset" in sections:
            status_incidents = monitor_engine.fetch_status_for_refresh(self, now, remember_retry_after)
            status_monitor = _mapping(self._state.get("monitor"))
            status_refreshed = status_monitor.get("statusLastSuccessAt") == _iso(now) and not status_monitor.get("statusError")
            if status_monitor.get("statusError"):
                supplemental_errors.setdefault("reset", []).append(str(status_monitor["statusError"])[:300])

        for name in sections:
                if name in failures:
                    continue
                if name not in {"intelligence", "quota", "reset"}:
                    continue
                previous = _mapping(_mapping(cached_sections.get(name)).get("data"))
                if (
                    name == "intelligence"
                    and intelligence_metrics is None
                    and not intelligence_not_modified
                    and supplemental_errors.get(name)
                ):
                    failures[name] = summary_error or RadarError(
                        "；".join(supplemental_errors[name])[:300]
                    )
                    continue
                if summary is not None:
                    incoming = summary.get(name)
                    if not isinstance(incoming, dict):
                        failures[name] = RadarSchemaError(f"公开摘要缺少有效的 {name} 区段。")
                        continue
                    section_data = dict(incoming)
                else:
                    section_data = dict(previous)
                    source_healthy = summary_not_modified and bool(previous)
                    if name == "intelligence":
                        source_healthy = source_healthy or intelligence_metrics is not None or intelligence_not_modified
                    elif name == "quota":
                        source_healthy = source_healthy or bool(html_cards.get("quota"))
                    elif name == "reset":
                        source_healthy = (
                            source_healthy
                            or status_refreshed
                            or reset_events is not None
                            or (reset_events_not_modified and bool(previous.get("events")))
                            or forecast is not None
                            or (forecast_not_modified and bool(previous.get("forecastSignals")))
                            or bool(html_cards.get("resetHistory"))
                        )
                    if not source_healthy:
                        not_modified_without_cache = (
                            summary_not_modified
                            or intelligence_not_modified
                            or reset_events_not_modified
                            or forecast_not_modified
                        )
                        failures[name] = summary_error or RadarError(
                            (
                                f"{name} 数据源返回 304，但本地没有对应缓存。"
                                if not_modified_without_cache
                                else "; ".join(supplemental_errors.get(name, []))
                                or f"没有可用的 {name} 数据源。"
                            )
                        )
                        continue

                if name == "intelligence":
                    if isinstance(intelligence_metrics, dict):
                        section_data.update(intelligence_metrics)
                    elif previous.get("mode") == "composite-weighted-mean" and _list(previous.get("items")):
                        # A composite is meaningful only when both public
                        # dimensions are available. Never replace it with the
                        # single-dimension summary after a 304 or partial error.
                        for key in (
                            "mode",
                            "items",
                            "comparisons",
                            "updatedAt",
                            "runs24h",
                            "runs48h",
                            "runsTotal",
                        ):
                            if key in previous:
                                section_data[key] = previous[key]
                    if section_data.get("mode") != "composite-weighted-mean":
                        failures[name] = RadarSchemaError(
                            "综合智能需要软件工程与视觉空间两项公开评分，当前未取得完整数据。"
                        )
                        continue
                    if not _list(section_data.get("items")) and not _list(section_data.get("comparisons")):
                        failures[name] = RadarSchemaError("智力效率接口没有返回可用的综合智能评分。")
                        continue
                elif name == "quota":
                    if previous.get("measurement") == "community-measured" and (html_error or not html_cards.get("quota")):
                        failures[name] = html_error or RadarSchemaError("公开网页未返回额度实测卡片，保留上次实测并等待源站恢复。")
                        continue
                    if html_cards.get("quota"):
                        section_data.update(html_cards["quota"])
                    elif previous.get("measurement") == "community-measured":
                        section_data = dict(previous)
                    else:
                        section_data["legacySnapshot"] = True
                elif name == "reset":
                    if reset_events is not None:
                        section_data["events"] = reset_events
                    elif not section_data.get("events") and previous.get("events"):
                        section_data["events"] = _list(previous.get("events"))[:64]
                    if isinstance(forecast, dict):
                        section_data["forecastSignals"] = forecast
                        section_data["predictionSource"] = forecast.get("predictor")
                    elif previous.get("forecastSignals"):
                        section_data["forecastSignals"] = previous.get("forecastSignals")
                        section_data["predictionSource"] = previous.get("predictionSource")
                    if html_cards:
                        section_data["monthlyHistory"] = html_cards.get("monthlyHistory", [])
                        section_data["resetHistory"] = html_cards.get("resetHistory", [])
                    self._retain_reset_history(section_data, now)
                    section_data["statusIncidents"] = status_incidents
                    reset_evaluation = monitor_engine.evaluate_reset_refresh(self, section_data, now)
                    # Revisit cached failed/pending fields on every successful
                    # reset refresh, including when one source returned 304.
                    self._translate_reset_payload(section_data, include_forecast=not isinstance(forecast, dict))
                if name in {"intelligence", "quota"}:
                    # HTML fallback cards can expose a prose summary for these
                    # sections as well.
                    self._translate_public_record(
                        section_data,
                        fields=("title", "summary", "description", "message", "status"),
                    )
                if supplemental_errors.get(name):
                    section_data["supplementalErrors"] = supplemental_errors[name]
                else:
                    section_data.pop("supplementalErrors", None)

                actually_changed = summary is not None or bool(html_cards and name in {"quota", "reset"})
                if name == "intelligence":
                    actually_changed = actually_changed or intelligence_metrics is not None
                elif name == "reset":
                    actually_changed = actually_changed or reset_events is not None or forecast is not None or status_refreshed
                if actually_changed:
                    if name == "quota" and html_cards.get("quota"):
                        url, format_name = PUBLIC_HTML_URL, "html"
                    elif summary is not None:
                        url = PUBLIC_SUMMARY_URL if summary_format == "json" else PUBLIC_HTML_URL
                        format_name = (summary or {}).get("format", summary_format)
                    elif name == "intelligence" and intelligence_metrics is not None:
                        url, format_name = PUBLIC_INTELLIGENCE_URL, "json"
                    elif name == "reset" and forecast is not None:
                        url, format_name = PUBLIC_FORECAST_URL, "json"
                    elif name == "reset" and reset_events is not None:
                        url, format_name = PUBLIC_FEED_URL, "rss"
                    else:
                        previous_source = _mapping(_mapping(cached_sections.get(name)).get("source"))
                        url = _text(previous_source.get("url"), 1000) or PUBLIC_SUMMARY_URL
                        format_name = _text(previous_source.get("format"), 40) or summary_format
                    self._store_section(
                        name,
                        section_data,
                        now,
                        url=url,
                        format_name=format_name,
                        degraded=bool((summary or {}).get("degraded")),
                    )
                    changed.add(name)
                succeeded.add(name)
                self._state.setdefault("sectionErrors", {}).pop(name, None)

        for name, exc in failures.items():
            self._state.setdefault("sectionErrors", {})[name] = str(exc)[:300]
        if succeeded:
            self._state["lastSuccessAt"] = _iso(now)
        if failures and not succeeded:
            strongest = max(
                failures.values(),
                key=lambda exc: (
                    exc.retry_after.timestamp()
                    if isinstance(exc, RadarHTTPError) and exc.retry_after is not None
                    else 0
                ),
            )
            self._record_failure(strongest, now, set(failures))
        else:
            self._state["failureCount"] = 0
            self._state["nextAllowedAt"] = (
                _iso(retry_after_until)
                if retry_after_until is not None and retry_after_until > now
                else None
            )
        if retry_after_until is not None and retry_after_until > now:
            current_next = _parse_time(self._state.get("nextAllowedAt"))
            self._state["nextAllowedAt"] = _iso(
                max(current_next or retry_after_until, retry_after_until)
            )
        self._persist_state()
        return {
            "attempted": True,
            "success": bool(succeeded),
            "partial": bool(succeeded and failures),
            "error": "; ".join(str(value)[:140] for value in failures.values()) or None,
            "changedSections": sorted(changed),
            "notModified": bool(succeeded and not changed),
            **reset_evaluation,
        }

    def _section(self, name: str, *, refresh_result: Mapping[str, Any] | None = None) -> dict:
        entry = _mapping(self._state.get("sections")).get(name)
        entry = entry if isinstance(entry, dict) else {}
        data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        error = _mapping(self._state.get("sectionErrors")).get(name)
        result = {
            "data": _bounded_json(data),
            "source": _bounded_json(entry.get("source")),
            "fetchedAt": entry.get("fetchedAt"),
            "lastAttemptAt": self._state.get("lastAttemptAt"),
            "lastSuccessAt": self._state.get("lastSuccessAt"),
            "nextAllowedAt": self._state.get("nextAllowedAt"),
            "cached": True,
            "stale": bool(error),
            "error": error,
            "available": bool(data),
        }
        if name == "reset":
            from .reset_history import verified_reset_history
            result["data"]["verifiedResetHistory"] = verified_reset_history()
        if refresh_result:
            changed = set(refresh_result.get("changedSections") or [])
            result["cached"] = name not in changed
            result["refreshAttempted"] = bool(refresh_result.get("attempted"))
            result["refreshSuppressed"] = refresh_result.get("suppressed")
            if name == "reset":
                result["alert"] = _bounded_json(refresh_result.get("alert"))
                result["newAlert"] = bool(refresh_result.get("newAlert"))
        return result

    def monitor_status(self) -> dict:
        return monitor_engine.monitor_status(self)

    def seconds_until_next_monitor(self) -> float:
        return monitor_engine.seconds_until_next_monitor(self)

    def run_monitor(self, *, force: bool = False) -> dict:
        return monitor_engine.run_monitor(self, force=force)

    def get_intelligence(self, *, refresh: bool = False, startup: bool = False) -> dict:
        with self._lock:
            refresh_result = None
            if startup and not self._startup_attempted:
                self._startup_attempted = True
                refresh_result = self._refresh({"intelligence"})
            elif refresh:
                refresh_result = self._refresh({"intelligence"})
            result = self._section("intelligence", refresh_result=refresh_result)
            result["needsManualRefresh"] = not result["available"] and not startup and not refresh
            return result

    def get_quota(self, *, refresh: bool = False, force: bool = False) -> dict:
        with self._lock:
            refresh_result = None
            if refresh:
                now = self._now()
                if not force and not self._quota_due(now):
                    refresh_result = {"attempted": False, "suppressed": "weekly-cadence", "success": False, "changedSections": []}
                else:
                    refresh_result = self._refresh({"quota"})
            result = self._section("quota", refresh_result=refresh_result)
            attempted = _parse_time(self._state.get("quotaLastAttemptAt"))
            result["nextWeeklyRefreshAt"] = _iso(attempted + QUOTA_REFRESH_INTERVAL) if attempted else None
            return result

    def get_reset_radar(
        self,
        accounts: Iterable[Mapping[str, Any]] | None = None,
        *,
        refresh: bool = False,
        startup: bool = False,
    ) -> dict:
        refresh_result = None
        if refresh or (startup and not self._startup_attempted):
            self._startup_attempted = True
            refresh_result = self.run_monitor(force=True)
        with self._lock:
            result = self._section("reset", refresh_result=refresh_result)
            result["data"] = merge_reset_metadata(result["data"], accounts)
            result["data"]["monitor"] = self.monitor_status()
            return result

    def get_snapshot(
        self,
        accounts: Iterable[Mapping[str, Any]] | None = None,
        *,
        refresh: bool = False,
        startup: bool = False,
    ) -> dict:
        """Return all channels while coalescing a refresh into one cycle."""
        with self._lock:
            refresh_result = None
            should_start = startup and not self._startup_attempted
            if should_start:
                self._startup_attempted = True
            if should_start or refresh:
                sections = {"intelligence", "reset"}
                # The backend monitor and first UI snapshot share this lock.
                # Reuse only a recent completion in this service instance;
                # an old process's cache must not suppress a new startup.
                recent_reset = getattr(self, "_monitor_last_finished", None)
                if not refresh and recent_reset is not None and timedelta(0) <= self._now() - recent_reset < timedelta(seconds=30):
                    sections.discard("reset")
                if self._quota_due(self._now()):
                    sections.add("quota")
                refresh_result = self._refresh(sections)
                if "reset" in sections and refresh_result.get("attempted") and refresh_result.get("success") and "reset" not in _mapping(self._state.get("sectionErrors")):
                    self._monitor_last_finished = self._now()
                    self._state.setdefault("monitor", {})["lastRefreshFinishedAt"] = _iso(self._monitor_last_finished)
                    self._persist_state()
                wake_monitor = getattr(self, "_monitor_wake_callback", None)
                if refresh_result.get("newAlert") and callable(wake_monitor):
                    wake_monitor()
            intelligence = self._section("intelligence", refresh_result=refresh_result)
            quota = self._section("quota", refresh_result=refresh_result)
            reset = self._section("reset", refresh_result=refresh_result)
            reset["data"] = merge_reset_metadata(reset["data"], accounts)
            reset["data"]["monitor"] = self.monitor_status()
            attempted = _parse_time(self._state.get("quotaLastAttemptAt"))
            quota["nextWeeklyRefreshAt"] = _iso(attempted + QUOTA_REFRESH_INTERVAL) if attempted else None
            return {
                "schemaVersion": CACHE_SCHEMA_VERSION,
                "intelligence": intelligence,
                "quota": quota,
                "reset": reset,
                "sources": data_sources(),
            }


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "MAX_FEED_BYTES",
    "MAX_HTML_BYTES",
    "MAX_PUBLIC_BYTES",
    "MAX_SUMMARY_BYTES",
    "PUBLIC_FEED_URL",
    "PUBLIC_FORECAST_URL",
    "PUBLIC_TRANSLATE_URL",
    "PUBLIC_HTML_URL",
    "PUBLIC_INTELLIGENCE_DETAIL_URL",
    "PUBLIC_INTELLIGENCE_URL",
    "PUBLIC_VISUAL_INTELLIGENCE_URL",
    "PUBLIC_SUMMARY_URL",
    "OPENAI_STATUS_INCIDENTS_URL",
    "QUOTA_REFRESH_INTERVAL",
    "RadarCache",
    "RadarError",
    "RadarHTTPError",
    "RadarSchemaError",
    "RadarService",
    "data_sources",
    "classify_reset_alert",
    "merge_reset_metadata",
    "parse_composite_intelligence_metrics",
    "parse_intelligence_detail",
    "parse_intelligence_metrics",
    "parse_public_feed",
    "parse_public_forecast",
    "parse_public_html",
    "parse_public_html_cards",
    "parse_public_html_translations",
    "parse_machine_translation",
    "_auto_translate_public_text",
    "parse_public_json",
    "parse_openai_status_incidents",
    "next_monitor_time",
    "validate_public_payload",
]
