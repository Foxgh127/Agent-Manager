"""子代理健康检查和清理"""
from __future__ import annotations
import time
import re
from agent_manager import core as _core
from .client import codex_app_server_request, codex_app_server_requests
from .thread_utils import _codex_thread_list_params, _codex_thread_rows, _subagent_turn_status


def stale_subagent_health(
    settings: dict | None = None,
    *,
    stale_seconds: int = None,
    max_threads: int = 500,
) -> dict:
    """
    查找孤儿子代理任务，不干扰活跃的 Codex 工作

    Runtime 状态属于拥有 turn 的 App Server 进程。第二个诊断进程
    不能中断 Desktop 的活跃子进程。仅在所有 Codex 进程关闭后才提供清理，
    并归档（而非删除）那些持久化的最后一个 turn 仍在进行中或以 systemError 结束的旧子代理线程。
    """
    if stale_seconds is None:
        stale_seconds = _core.STUCK_SUBAGENT_MIN_AGE_SECONDS

    _ = settings
    running = _core.running_codex_processes()

    # 检查是否能可靠确认 Codex 状态
    if getattr(running, "known", True) is False:
        detail = str(getattr(running, "error", "") or "").strip()
        return {
            "status": "warning",
            "healthy": True,
            "recoverable": False,
            "checked": False,
            "deferred": True,
            "candidates": [],
            "detail": (
                "无法可靠确认 Codex 是否已关闭；为避免误伤仍在工作的子代理，"
                f"本次未执行清理检查。{f'（{detail}）' if detail else ''}"
            ),
        }

    # 如果 Codex 正在运行，不执行清理
    if len(running) > 0:
        return {
            "status": "ok",
            "healthy": True,
            "recoverable": False,
            "checked": False,
            "deferred": True,
            "candidates": [],
            "detail": "Codex 正在运行；为避免误伤仍在工作的子代理，本次未执行孤儿任务清理检查。",
        }

    # 获取子代理线程列表
    limit = max(1, min(int(max_threads), 500))
    rows: list[dict] = []
    cursor = None
    seen_cursors: set[str] = set()

    while len(rows) < limit:
        params = _codex_thread_list_params(min(100, limit - len(rows)), False, False, cursor)
        params["sourceKinds"] = list(_core.SUBAGENT_SOURCE_KINDS)
        listed = codex_app_server_request("thread/list", params, timeout=30)
        rows.extend(_codex_thread_rows(listed, False))
        next_cursor = listed.get("nextCursor") or listed.get("next_cursor")
        if not next_cursor or str(next_cursor) in seen_cursors:
            break
        seen_cursors.add(str(next_cursor))
        cursor = next_cursor

    # 筛选旧线程
    now_epoch = time.time()
    old_rows = [
        row for row in rows
        if row.get("id")
        and str(row.get("source") or "").casefold().startswith("subagent")
        and row.get("updatedEpoch", 0) > 0
        and now_epoch - float(row.get("updatedEpoch") or 0) >= max(60, int(stale_seconds))
    ]

    # 检查每个旧线程的详细状态
    candidates = []
    if old_rows:
        results = []
        for offset in range(0, len(old_rows), 100):
            results.extend(
                codex_app_server_requests(
                    [
                        ("thread/read", {"threadId": row["id"], "includeTurns": True})
                        for row in old_rows[offset : offset + 100]
                    ],
                    timeout=45,
                )
            )

        for row, result in zip(old_rows, results):
            thread = result.get("thread") if isinstance(result.get("thread"), dict) else result
            if not isinstance(thread, dict):
                continue

            turn_status, turn_id = _subagent_turn_status(thread)
            raw_thread_status = thread.get("status")
            thread_status = str(
                (raw_thread_status or {}).get("type")
                if isinstance(raw_thread_status, dict)
                else raw_thread_status
                if isinstance(raw_thread_status, str)
                else row.get("status", {}).get("type") or ""
            )

            normalized_turn = re.sub(r"[^a-z]", "", turn_status.casefold())
            normalized_thread = re.sub(r"[^a-z]", "", thread_status.casefold())

            if normalized_turn not in {"inprogress", "running"} and normalized_thread != "systemerror":
                continue

            candidates.append({
                "threadId": row["id"],
                "name": row.get("name") or "未命名子代理任务",
                "updatedAt": row.get("updatedAt"),
                "ageSeconds": max(0, int(now_epoch - float(row.get("updatedEpoch") or 0))),
                "turnId": turn_id,
                "reason": "system_error" if normalized_thread == "systemerror" else "orphaned_in_progress",
            })

    return {
        "status": "warning" if candidates else "ok",
        "healthy": not candidates,
        "recoverable": bool(candidates),
        "checked": True,
        "candidates": candidates,
        "detail": (
            f"发现 {len(candidates)} 个 Codex 已关闭后仍未终态的旧子代理任务，可安全归档并随时从会话管理恢复。"
            if candidates
            else "没有发现 Codex 已关闭后仍未终态的旧子代理任务。"
        ),
    }


def cleanup_stale_subagents(thread_ids: list[str] | None = None) -> dict:
    """清理卡死的子代理任务"""
    if _core.running_codex_processes():
        raise _core.ManagerError("Codex 仍在运行。请先保存工作并关闭 Codex，再清理卡死子代理。")

    health = stale_subagent_health()
    allowed = {str(item.get("threadId") or "") for item in health.get("candidates", [])}
    requested = set(str(value or "").strip() for value in (thread_ids or allowed))
    targets = sorted((allowed & requested) - {""})

    if not targets:
        return {"changed": False, "archived": [], "health": health}

    archived: list[str] = []
    try:
        for offset in range(0, len(targets), 100):
            if _core.running_codex_processes():
                raise _core.ManagerError("清理期间检测到 Codex 已启动，已停止并回滚本次清理。")
            batch = targets[offset : offset + 100]
            _core.manage_codex_threads("archive", batch)
            archived.extend(batch)
    except Exception as exc:
        rollback_errors = []
        for offset in range(len(archived), 0, -100):
            batch = archived[max(0, offset - 100) : offset]
            try:
                _core.manage_codex_threads("restore", batch)
            except Exception as rollback_exc:
                rollback_errors.append(str(rollback_exc)[:240])
        detail = f"；回滚异常：{'；'.join(rollback_errors)}" if rollback_errors else ""
        raise _core.ManagerError(
            f"清理失败：{_core._redact_sensitive_text(exc, limit=320)}{detail}"
        ) from exc

    return {"changed": True, "archived": archived, "health": health}
