#!/usr/bin/env python3
"""Safe, filtered exports of the usage statistics shown by UsageView."""

from __future__ import annotations

from datetime import date as date_value, datetime, timedelta, timezone
from typing import Any

import agent_manager_core as core


EXPORT_FORMAT = "agent-manager-usage-statistics"
EXPORT_VERSION = 1
MAX_FILTER_LENGTH = 256
MAX_SOURCE_RECORDS = 200_000
MAX_EXPORT_RECORDS = 200_000

_SOURCES = {"accounts", "codex", "gateway"}
_ROLE_VALUES = {"mainAgent", "subagent", "unclassified"}
_COUNTER_ALIASES = {
    "requestCount": ("requestCount", "requests"),
    "failureCount": ("failureCount", "failures"),
    "usageReportedCount": ("usageReportedCount",),
    "usageMissingCount": ("usageMissingCount",),
    "inputTokens": ("inputTokens", "input_tokens"),
    "cachedInputTokens": ("cachedInputTokens", "cached_input_tokens", "cachedTokens", "cached_tokens"),
    "cacheWriteTokens": ("cacheWriteTokens", "cache_write_tokens"),
    "outputTokens": ("outputTokens", "output_tokens"),
    "reasoningOutputTokens": ("reasoningOutputTokens", "reasoning_output_tokens", "reasoningTokens"),
    "totalTokens": ("totalTokens", "total_tokens", "tokens"),
}


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise core.ManagerError(f"{label}格式无效。")
    value = value.strip()
    if not value or len(value) > MAX_FILTER_LENGTH or any(ord(char) < 32 for char in value):
        raise core.ManagerError(f"{label}格式无效。")
    return value


def _validate_payload(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise core.ManagerError("用量导出参数格式无效。")
    source = _text(payload.get("source"), "数据源")
    source_filter = _text(payload.get("sourceFilter"), "来源筛选")
    model = _text(payload.get("model"), "模型筛选")
    selected_date = _text(payload.get("date"), "日期筛选")
    if source not in _SOURCES:
        raise core.ManagerError("用量导出数据源无效。")
    if source_filter != "all":
        prefix, separator, label = source_filter.partition(":")
        if separator != ":" or not label or prefix not in {"account", "provider", "role", "unknown"}:
            raise core.ManagerError("来源筛选无效。")
        if prefix == "role" and label not in _ROLE_VALUES:
            raise core.ManagerError("代理角色筛选无效。")
    if selected_date != "all":
        if len(selected_date) != 10:
            raise core.ManagerError("日期筛选必须是 YYYY-MM-DD。")
        try:
            parsed = date_value.fromisoformat(selected_date)
        except ValueError as exc:
            raise core.ManagerError("日期筛选必须是有效的 YYYY-MM-DD 日期。") from exc
        if parsed.isoformat() != selected_date:
            raise core.ManagerError("日期筛选必须是有效的 YYYY-MM-DD 日期。")
    normalized = {"source": source, "sourceFilter": source_filter, "model": model, "date": selected_date}
    if "dateFrom" in payload or "dateTo" in payload:
        for key in ("dateFrom", "dateTo"):
            value = _text(payload.get(key), "统计日期范围")
            try:
                valid = date_value.fromisoformat(value).isoformat() == value
            except ValueError:
                valid = False
            if not valid:
                raise core.ManagerError("统计日期范围必须使用有效的 YYYY-MM-DD 日期。")
            normalized[key] = value
        if normalized["dateFrom"] > normalized["dateTo"]:
            raise core.ManagerError("开始日期不能晚于结束日期。")
    return normalized


def _select_source(snapshot: Any, source: str) -> dict:
    if not isinstance(snapshot, dict):
        raise core.ManagerError("用量统计快照格式无效。")
    if source == "gateway":
        return snapshot
    key = "accountAttribution" if source == "accounts" else "codexSessions"
    selected = snapshot.get(key)
    if not isinstance(selected, dict):
        raise core.ManagerError(f"所选用量数据源 {source} 当前不可用。")
    return selected


def _source_records(selected: dict) -> list[dict]:
    for key in ("records", "items", "requests"):
        candidate = selected.get(key)
        if isinstance(candidate, list) and candidate:
            if len(candidate) > MAX_SOURCE_RECORDS:
                raise core.ManagerError("用量统计记录过多，无法安全导出。")
            return [item for item in candidate if isinstance(item, dict)]
    days = selected.get("days")
    if not isinstance(days, dict):
        return []
    records: list[dict] = []
    for day_key, day in days.items():
        if not isinstance(day, dict) or not isinstance(day.get("routes"), dict):
            continue
        for route in day["routes"].values():
            if isinstance(route, dict):
                records.append({**route, "date": str(day_key)})
                if len(records) > MAX_SOURCE_RECORDS:
                    raise core.ManagerError("用量统计记录过多，无法安全导出。")
    return records


def _first_text(item: dict, fields: tuple[str, ...], fallback: str = "") -> str:
    for field in fields:
        value = item.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()[:MAX_FILTER_LENGTH]
    return fallback


def _role(item: dict) -> str:
    claimed = _first_text(item, ("agentRole",))
    if claimed:
        return claimed if claimed in _ROLE_VALUES else "unclassified"
    classification = _first_text(item, ("requestClassification", "classification"), "unclassified")
    if classification == "explicit_subagent":
        return "subagent"
    if classification == "explicit_main_agent":
        return "mainAgent"
    return "unclassified"


def _source_key(item: dict, source: str, role: str) -> str:
    if source == "codex":
        return f"role:{role}"
    account_id = _first_text(item, ("accountId",))
    if account_id:
        return f"account:{account_id}"
    provider_id = _first_text(item, ("providerId",))
    if provider_id:
        return f"provider:{provider_id}"
    label = _first_text(item, ("account", "accountName"), "未归因账号")
    return f"unknown:{label}"


def _counter(item: dict, aliases: tuple[str, ...], default: int = 0) -> int:
    for name in aliases:
        if name not in item or isinstance(item[name], bool):
            continue
        try:
            number = int(item[name])
        except (TypeError, ValueError, OverflowError):
            continue
        return max(0, number)
    return default


def _normalize(item: dict, source: str) -> dict:
    role = _role(item)
    timestamp = _first_text(item, ("timestamp", "lastSeenAt", "createdAt", "date", "day"))
    day = _first_text(item, ("date", "day"))
    if not day and timestamp:
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone(timedelta(hours=8)))
            day = parsed.date().isoformat()
        except ValueError:
            day = "未知日期"
    model = _first_text(item, ("model", "modelName", "routedModel", "requestedModel"), "未确定模型")
    normalized = {
        "sourceKey": _source_key(item, source, role),
        "date": day[:10],
        "model": model,
        "role": role,
        "requestClassification": _first_text(
            item, ("requestClassification", "classification"), "unclassified"
        ),
    }
    if timestamp:
        normalized["timestamp"] = timestamp
    for field in ("firstSeenAt", "lastSeenAt"):
        value = _first_text(item, (field,))
        if value:
            normalized[field] = value
    for canonical, aliases in _COUNTER_ALIASES.items():
        normalized[canonical] = _counter(item, aliases, 1 if canonical == "requestCount" else 0)
    if not any(alias in item for alias in _COUNTER_ALIASES["totalTokens"]):
        normalized["totalTokens"] = normalized["inputTokens"] + normalized["outputTokens"]
    return normalized


def export_usage(runtime: Any, payload: Any) -> dict:
    """Filter the current usage aggregate and save it to the Windows Downloads directory."""
    filters = _validate_payload(payload)
    try:
        snapshot = runtime.web2api.usage_snapshot()
    except AttributeError as exc:
        raise core.ManagerError("用量统计服务不可用。") from exc
    selected = _select_source(snapshot, filters["source"])
    normalized = (_normalize(item, filters["source"]) for item in _source_records(selected))
    records = [
        item
        for item in normalized
        if (filters["sourceFilter"] == "all" or item["sourceKey"] == filters["sourceFilter"])
        and (filters["model"] == "all" or item["model"] == filters["model"])
        and (filters["date"] == "all" or item["date"] == filters["date"])
        and (not filters.get("dateFrom") or filters["dateFrom"] <= item["date"] <= filters["dateTo"])
    ]
    if len(records) > MAX_EXPORT_RECORDS:
        raise core.ManagerError("筛选后的用量统计记录过多，无法安全导出。")
    document = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "generatedAt": core.now_iso(),
        "source": filters["source"],
        "filters": {
            "source": filters["sourceFilter"],
            "model": filters["model"],
            "date": filters["date"],
            **({key: filters[key] for key in ("dateFrom", "dateTo")} if filters.get("dateFrom") else {}),
        },
        "recordCount": len(records),
        "dataKind": "aggregated_usage_statistics",
        "containsRawConversations": False,
        "records": records,
    }
    coverage_fields = ("indexedFiles", "partialFiles", "pendingBytes", "boundedScanBytesPerFile", "totalRequests", "usageReportedRequests", "roleClassifiedRequests")
    coverage = selected.get("coverage")
    if isinstance(coverage, dict):
        document["coverage"] = {key: _counter(coverage, (key,)) for key in coverage_fields if key in coverage}
    sessions = snapshot.get("codexSessions")
    if filters["source"] == "accounts" and isinstance(sessions, dict) and isinstance(sessions.get("coverage"), dict):
        document["sessionCoverage"] = {key: _counter(sessions["coverage"], (key,)) for key in coverage_fields if key in sessions["coverage"]}
    return core.save_json_export_to_downloads(document, f"usage-{filters['source']}")
