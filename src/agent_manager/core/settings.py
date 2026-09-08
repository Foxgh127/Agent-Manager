"""Settings services."""
from __future__ import annotations
from agent_manager import core as _core


def _default_model_workspace() -> dict:
    return {
        "mode": "independent",
        "activeSourceId": "",
        "selectAll": True,
        "selectedModels": [],
        "defaultModelKey": "",
        "syncToCodex": True,
    }



def _default_runtime_tuning() -> dict:
    return {
        # Existing user config remains untouched until the related controls
        # are saved. Planning instructions are tracked independently.
        "configManaged": False,
        "managedFields": [],
        "planningManaged": False,
        "enabled": False,
        "modelContextWindow": 0,
        "autoCompactTokenLimit": 0,
        "autoCompactScope": "total",
        "mcpOptionalStartupGraceMs": -1,
        "reasoningSummary": "auto",
        "verbosity": "",
        "webSearch": "",
        "webSearchContextSize": "",
        "personality": "",
        "serviceTier": "",
        "checkForUpdates": True,
        "tuiAnimations": True,
        "requestMaxRetries": 4,
        "streamMaxRetries": 5,
        "streamIdleTimeoutMs": 300_000,
        "supportsWebsockets": False,
        "vpnCompatibility": False,
        "preventIdleSleep": False,
        "planningMode": "auto",
    }



def _normalize_runtime_tuning(value: object, *, strict: bool = False) -> dict:
    defaults = _core._default_runtime_tuning()
    if not isinstance(value, dict):
        if strict:
            raise _core.ManagerError("Codex 运行参数格式无效。")
        value = {}
    original = value
    source = {**defaults, **value}

    raw_managed_fields = original.get("managedFields")
    if isinstance(raw_managed_fields, list):
        managed_fields = list(
            dict.fromkeys(
                str(item)
                for item in raw_managed_fields
                if str(item) in _core.MANAGED_RUNTIME_TUNING_FIELDS
            )
        )
        if strict and len(managed_fields) != len(raw_managed_fields):
            raise _core.ManagerError("Codex 常用配置所有权列表包含无效字段。")
    elif bool(original.get("configManaged", False)):
        # Safe migration from the old all-or-nothing owner flag.  Retain only
        # non-default values belonging to controls that are still visible.
        # Hidden answer-style and advanced transport fields are deliberately
        # not inferred, so they immediately return to Codex/user ownership.
        managed_fields = [
            key
            for key in _core.MANAGED_RUNTIME_TUNING_FIELDS
            if source.get(key, defaults.get(key)) != defaults.get(key)
        ]
    else:
        managed_fields = []

    def integer(key: str, label: str, minimum: int, maximum: int, *, allow_zero: bool = False) -> int:
        try:
            number = int(source.get(key, defaults[key]))
        except (TypeError, ValueError) as exc:
            if strict:
                raise _core.ManagerError(f"{label}必须是整数。") from exc
            return int(defaults[key])
        if (allow_zero and number == 0) or minimum <= number <= maximum:
            return number
        if strict:
            zero_hint = "，或设为 0 以跟随模型默认值" if allow_zero else ""
            raise _core.ManagerError(f"{label}必须在 {minimum:,} 到 {maximum:,} 之间{zero_hint}。")
        return int(defaults[key])

    def choice(key: str, label: str, allowed: tuple[str, ...]) -> str:
        selected = str(source.get(key, defaults[key]) or "")
        if selected in allowed:
            return selected
        if strict:
            raise _core.ManagerError(f"{label}选项无效。")
        return str(defaults[key])

    def optional_milliseconds(key: str, label: str, maximum: int) -> int:
        value = source.get(key, defaults[key])
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            if strict:
                raise _core.ManagerError(f"{label}必须是整数。") from exc
            return int(defaults[key])
        if number == -1 or 0 <= number <= maximum:
            return number
        if strict:
            raise _core.ManagerError(f"{label}必须在 0 到 {maximum:,} 毫秒之间，或设为 -1 跟随 Codex。")
        return int(defaults[key])

    result = {
        "configManaged": bool(managed_fields),
        "managedFields": managed_fields,
        "planningManaged": bool(source.get("planningManaged", False)),
        "enabled": bool(source.get("enabled", False)),
        "modelContextWindow": integer(
            "modelContextWindow", "模型上下文长度", 16_384, 2_000_000, allow_zero=True
        ),
        "autoCompactTokenLimit": integer(
            "autoCompactTokenLimit", "自动压缩阈值", 8_192, 2_000_000, allow_zero=True
        ),
        "autoCompactScope": choice(
            "autoCompactScope", "自动压缩统计范围", _core.VALID_COMPACTION_SCOPES
        ),
        "mcpOptionalStartupGraceMs": optional_milliseconds(
            "mcpOptionalStartupGraceMs", "可选 MCP 启动等待", 60_000
        ),
        "reasoningSummary": choice(
            "reasoningSummary", "推理摘要", _core.VALID_REASONING_SUMMARIES
        ),
        "verbosity": choice("verbosity", "回答详细程度", _core.VALID_MODEL_VERBOSITIES),
        "webSearch": choice("webSearch", "联网搜索模式", _core.VALID_WEB_SEARCH_MODES),
        "webSearchContextSize": choice(
            "webSearchContextSize", "联网搜索深度", _core.VALID_WEB_SEARCH_CONTEXT_SIZES
        ),
        "personality": choice("personality", "默认沟通风格", _core.VALID_PERSONALITIES),
        "serviceTier": choice("serviceTier", "服务等级", _core.VALID_SERVICE_TIERS),
        "checkForUpdates": bool(source.get("checkForUpdates", True)),
        "tuiAnimations": bool(source.get("tuiAnimations", True)),
        "requestMaxRetries": integer("requestMaxRetries", "请求重试次数", 0, 10),
        "streamMaxRetries": integer("streamMaxRetries", "流式重连次数", 0, 20),
        "streamIdleTimeoutMs": integer(
            "streamIdleTimeoutMs", "流空闲超时", 10_000, 900_000
        ),
        "supportsWebsockets": bool(source.get("supportsWebsockets", False)),
        "vpnCompatibility": bool(source.get("vpnCompatibility", False)),
        "preventIdleSleep": bool(source.get("preventIdleSleep", False)),
        "planningMode": choice("planningMode", "规划交互模式", _core.VALID_PLANNING_MODES),
    }
    context_window = result["modelContextWindow"]
    compact_limit = result["autoCompactTokenLimit"]
    if context_window and compact_limit and compact_limit >= context_window:
        if strict:
            raise _core.ManagerError("自动压缩阈值必须小于模型上下文长度。")
        result["autoCompactTokenLimit"] = 0
    return result



def _default_subagent_routing() -> dict:
    return {
        "advanced": False,
        "strategyId": "adaptive",
        "prompt": _core.OPTIMAL_ADAPTIVE_INSTRUCTIONS,
        "routes": {level: {"models": [], "efforts": []} for level in _core.DIFFICULTIES},
    }



def _normalize_usage_range(value: object, *, strict: bool = False) -> dict:
    source = value if isinstance(value, dict) else {}
    mode = source.get("mode", "last7")
    if mode not in {"today", "last7", "month", "custom"}:
        if strict:
            raise _core.ManagerError("统计时间范围无效。")
        mode = "last7"
    dates = {}
    for key in ("customStart", "customEnd"):
        candidate = source.get(key)
        try:
            parsed = _core.date_value.fromisoformat(candidate) if isinstance(candidate, str) else None
            dates[key] = candidate if parsed and parsed.isoformat() == candidate else None
        except ValueError:
            dates[key] = None
    if mode == "custom" and (not dates["customStart"] or not dates["customEnd"] or dates["customStart"] > dates["customEnd"]):
        if strict:
            raise _core.ManagerError("请选择有效的起止日期，开始日期不能晚于结束日期。")
        mode = "last7"
    return {"schemaVersion": 1, "mode": mode, **dates}



def _default_app_behavior() -> dict:
    return {
        "appearance": "system",
        "usageRange": _core._normalize_usage_range(None),
        "closeToTray": False,
        "radarMonitoring": False,
        "quotaRefreshMinutes": 10,
        # Mailbox checks validate only login + readonly SELECT.  They never
        # search or download messages and default to one check per day.
        "mailHealthCheckHours": 24,
    }



def _default_web2api_settings() -> dict:
    return {
        "enabled": False,
        "activeForCodex": False,
        # A Web Session selected from the account home page is a temporary,
        # single-account Codex route.  Keep it separate from ``accountIds`` so
        # selecting an account never mutates the user's persistent API pool.
        "activeAccountId": None,
        "bindHost": "127.0.0.1",
        "port": 17860,
        "accountIds": [],
        "providerIds": [],
        # Preserve one deterministic order across ChatGPT accounts and API
        # providers while keeping the legacy typed lists for compatibility.
        "sourceOrder": [],
        "routing": "ordered",
        "lastStartedAt": None,
        "lastError": None,
        "requestCount": 0,
    }



def _default_session_sync_settings() -> dict:
    return {
        "enabled": True,
        "repairVisibility": True,
        "lastSyncedAt": None,
        "lastSummary": None,
    }

