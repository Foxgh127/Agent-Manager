"""线程和会话管理相关工具"""
from __future__ import annotations
from typing import Any
from agent_manager import core as _core


def _timestamp_iso(value: Any) -> str | None:
    """将时间戳转换为 ISO 格式"""
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return _core.datetime.fromtimestamp(number, _core.timezone.utc).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _codex_thread_list_params(
    limit: int,
    archived: bool,
    rebuild: bool,
    cursor: str | None = None
) -> dict:
    """构建线程列表查询参数"""
    return {
        "cursor": cursor,
        "limit": max(1, min(int(limit), 100)),
        "sortKey": "updated_at",
        "sortDirection": "desc",
        "modelProviders": None,
        "sourceKinds": [
            "cli",
            "vscode",
            "exec",
            "appServer",
            *_core.SUBAGENT_SOURCE_KINDS,
            "unknown",
        ],
        "archived": bool(archived),
        "useStateDbOnly": not rebuild,
    }


def _codex_thread_rows(result: dict, archived: bool) -> list[dict]:
    """解析线程列表结果"""
    rows = []
    for item in result.get("data", []):
        if not isinstance(item, dict):
            continue

        # 解析 source
        raw_source = item.get("source")
        source = raw_source if isinstance(raw_source, dict) else {}
        source_kind = str(source.get("type") or item.get("sourceKind") or "").strip()
        if not source_kind and source:
            source_kind = next(
                (str(key) for key in source if str(key).casefold().startswith("subagent")),
                "",
            )
        if not source_kind and isinstance(raw_source, str):
            source_kind = raw_source

        # 解析更新时间
        updated_value = item.get("updatedAt") or item.get("recencyAt")
        try:
            updated_epoch = float(updated_value)
            if updated_epoch > 10_000_000_000:
                updated_epoch /= 1000
        except (TypeError, ValueError, OverflowError):
            updated_epoch = 0.0

        # 解析状态
        raw_status = item.get("status")
        status = raw_status if isinstance(raw_status, dict) else {}
        status_type = str(
            status.get("type")
            if status
            else raw_status
            if isinstance(raw_status, str)
            else "unknown"
        ).strip() or "unknown"

        rows.append({
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or item.get("preview") or "未命名任务")[:240],
            "preview": str(item.get("preview") or "")[:320],
            "cwd": str(item.get("cwd") or ""),
            "provider": str(item.get("modelProvider") or "unknown"),
            "source": source_kind or "unknown",
            "createdAt": _timestamp_iso(item.get("createdAt")),
            "updatedAt": _timestamp_iso(updated_value),
            "updatedEpoch": updated_epoch,
            "status": {
                "type": status_type[:80],
                "activeFlags": [str(value)[:80] for value in status.get("activeFlags", [])]
                if isinstance(status.get("activeFlags"), list)
                else [],
            },
            "archived": bool(archived),
            "pinned": bool(item.get("isPinned")),
        })
    return rows


def _subagent_turn_status(thread: dict) -> tuple[str, str | None]:
    """提取子代理回合状态"""
    turns = thread.get("turns") if isinstance(thread.get("turns"), list) else []
    last_turn = turns[-1] if turns and isinstance(turns[-1], dict) else {}
    raw_status = last_turn.get("status")
    if isinstance(raw_status, dict):
        status = str(raw_status.get("type") or "")
    else:
        status = str(raw_status or "")
    turn_id = str(last_turn.get("id") or "").strip() or None
    return status, turn_id
