"""线程列表和管理操作"""
from __future__ import annotations
from agent_manager import core as _core
from .thread_utils import _codex_thread_list_params, _codex_thread_rows


def list_codex_thread_groups(
    active_limit: int = 500,
    archived_limit: int = 500,
    rebuild: bool = False
) -> list[dict]:
    """列出 Codex 线程分组（活跃和归档）"""
    active_limit = max(1, min(int(active_limit), 500))
    archived_limit = max(1, min(int(archived_limit), 500))

    all_rows: list[dict] = []

    # Fetch the first active/archived pages in one App Server session.  This
    # keeps listing cheap and makes the batch boundary explicit for callers.
    initial_requests = []
    for archived, limit in ((False, active_limit), (True, archived_limit)):
        if limit > 0:
            initial_requests.append((
                "thread/list",
                _codex_thread_list_params(min(100, limit), archived, rebuild, None),
            ))
    initial_results = _core.codex_app_server_requests(initial_requests, timeout=30) if initial_requests else []
    for (method, params), listed in zip(initial_requests, initial_results):
        archived = bool(params.get("archived"))
        limit = archived_limit if archived else active_limit
        rows = _codex_thread_rows(listed, archived)
        all_rows.extend(rows)
        cursor = listed.get("nextCursor") or listed.get("next_cursor")
        seen_cursors: set[str] = set()
        while cursor and len(rows) < limit and str(cursor) not in seen_cursors:
            seen_cursors.add(str(cursor))
            page = _core.codex_app_server_requests([(
                method,
                _codex_thread_list_params(min(100, limit - len(rows)), archived, rebuild, cursor),
            )], timeout=30)[0]
            page_rows = _codex_thread_rows(page, archived)
            rows.extend(page_rows)
            all_rows.extend(page_rows)
            cursor = page.get("nextCursor") or page.get("next_cursor")

    return sorted(all_rows, key=lambda r: r.get("updatedEpoch", 0), reverse=True)


def refresh_codex_history_index() -> dict:
    """刷新 Codex 历史索引"""
    return _core.codex_app_server_request("history/reindex", {}, timeout=60)


def manage_codex_threads(action: str, thread_ids: list[str]) -> dict:
    """管理 Codex 线程（归档、恢复、删除等）"""
    if not thread_ids or len(thread_ids) > 200:
        raise _core.ManagerError(f"线程 ID 列表必须包含 1-200 个项目。")

    valid_actions = {"archive", "restore", "delete", "pin", "unpin"}
    if action not in valid_actions:
        raise _core.ManagerError(f"无效的操作：{action}。支持的操作：{', '.join(valid_actions)}")

    if action in {"pin", "unpin"}:
        from agent_manager.sessions.preferences import update_pins
        return update_pins(thread_ids, action == "pin")

    method = {"archive": "thread/archive", "restore": "thread/unarchive", "delete": "thread/delete"}[action]
    results = _core.codex_app_server_requests(
        [(method, {"threadId": tid}) for tid in thread_ids],
        timeout=60,
        return_outcomes=True
    )

    thread_ids_confirmed = [
        thread_id for thread_id, outcome in zip(thread_ids, results)
        if isinstance(outcome, dict) and outcome.get("ok")
    ]
    failed = [
        {"threadId": thread_id, **outcome}
        for thread_id, outcome in zip(thread_ids, results)
        if isinstance(outcome, dict) and outcome.get("ok") is False and not outcome.get("unconfirmed")
    ]
    unconfirmed = [
        {"threadId": thread_id, **outcome}
        for thread_id, outcome in zip(thread_ids, results)
        if isinstance(outcome, dict) and outcome.get("unconfirmed")
    ]

    return {
        "action": action,
        "total": len(thread_ids),
        "changed": len(thread_ids_confirmed),
        "threadIds": thread_ids_confirmed,
        "succeeded": len(thread_ids_confirmed),
        "failed": failed,
        "unconfirmed": unconfirmed,
        "results": results
    }


def rename_codex_thread(thread_id: str, name: str) -> dict:
    """重命名 Codex 线程"""
    if not thread_id or not isinstance(thread_id, str):
        raise _core.ManagerError("线程 ID 不能为空。")

    if not name or not isinstance(name, str):
        raise _core.ManagerError("线程名称不能为空。")

    name = " ".join(name.split())[:240]
    if not name:
        raise _core.ManagerError("线程名称不能为空。")

    result = _core.codex_app_server_request(
        "thread/name/set",
        {"threadId": thread_id, "name": name},
        timeout=30
    )
    if isinstance(result, dict) and "name" not in result:
        return {**result, "name": name}
    return result
