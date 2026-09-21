"""线程列表和管理操作"""
from __future__ import annotations
from agent_manager import core as _core
from .client import codex_app_server_request, codex_app_server_requests
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

    # 获取活跃线程
    for archived in [False, True]:
        limit = archived_limit if archived else active_limit
        if limit <= 0:
            continue

        rows: list[dict] = []
        cursor = None
        seen_cursors: set[str] = set()

        while len(rows) < limit:
            params = _codex_thread_list_params(
                min(100, limit - len(rows)),
                archived,
                rebuild,
                cursor
            )
            listed = codex_app_server_request("thread/list", params, timeout=30)
            rows.extend(_codex_thread_rows(listed, archived))

            next_cursor = listed.get("nextCursor") or listed.get("next_cursor")
            if not next_cursor or str(next_cursor) in seen_cursors:
                break
            seen_cursors.add(str(next_cursor))
            cursor = next_cursor

        all_rows.extend(rows)

    return sorted(all_rows, key=lambda r: r.get("updatedEpoch", 0), reverse=True)


def refresh_codex_history_index() -> dict:
    """刷新 Codex 历史索引"""
    return codex_app_server_request("history/reindex", {}, timeout=60)


def manage_codex_threads(action: str, thread_ids: list[str]) -> dict:
    """管理 Codex 线程（归档、恢复、删除等）"""
    if not thread_ids or len(thread_ids) > 200:
        raise _core.ManagerError(f"线程 ID 列表必须包含 1-200 个项目。")

    valid_actions = {"archive", "restore", "delete", "pin", "unpin"}
    if action not in valid_actions:
        raise _core.ManagerError(f"无效的操作：{action}。支持的操作：{', '.join(valid_actions)}")

    results = codex_app_server_requests(
        [("thread/update", {"threadId": tid, action: True}) for tid in thread_ids],
        timeout=60,
        return_outcomes=True
    )

    succeeded = sum(1 for r in results if r.get("ok"))
    failed = len(results) - succeeded

    return {
        "action": action,
        "total": len(thread_ids),
        "succeeded": succeeded,
        "failed": failed,
        "results": results
    }


def rename_codex_thread(thread_id: str, name: str) -> dict:
    """重命名 Codex 线程"""
    if not thread_id or not isinstance(thread_id, str):
        raise _core.ManagerError("线程 ID 不能为空。")

    if not name or not isinstance(name, str):
        raise _core.ManagerError("线程名称不能为空。")

    name = name.strip()[:240]
    if not name:
        raise _core.ManagerError("线程名称不能为空。")

    return codex_app_server_request(
        "thread/update",
        {"threadId": thread_id, "name": name},
        timeout=30
    )
