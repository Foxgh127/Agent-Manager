"""App server services."""
from __future__ import annotations
from agent_manager import core as _core


def codex_app_server_requests(requests: list[tuple[str, dict]], timeout: int | float = 30, *, return_outcomes: bool = False, launch_plan: dict | None = None) -> list[dict]:
    if not requests or len(requests) > 200:
        raise _core.ManagerError("Codex App Server 批量请求数量必须在 1 到 200 之间。")
    for method, params in requests:
        if not isinstance(method, str) or not method or not isinstance(params, dict):
            raise _core.ManagerError("Codex App Server 请求格式无效。")
    env = _core._codex_source_environment(launch_plan)
    flags = getattr(_core.subprocess, "CREATE_NO_WINDOW", 0) if _core.os.name == "nt" else 0
    process = _core.subprocess.Popen(
        _core._codex_launch_probe_prefix(launch_plan) + ["app-server", "--listen", "stdio://"],
        stdin=_core.subprocess.PIPE,
        stdout=_core.subprocess.PIPE,
        stderr=_core.subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=flags,
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        process.kill()
        raise _core.ManagerError("无法连接 Codex App Server 标准输入输出。")
    lines: _core.queue.Queue[str | None] = _core.queue.Queue()
    # App Server diagnostics can be noisy.  Only the tail is actionable and a
    # bounded buffer prevents an unresponsive child from growing memory until
    # the request timeout fires.
    stderr_lines: _core.deque[str] = _core.deque(maxlen=64)

    def read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    def read_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            stderr_lines.append(line.rstrip())

    stdout_thread = _core.threading.Thread(target=read_stdout, daemon=True)
    stderr_thread = _core.threading.Thread(target=read_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    def send(payload: dict) -> None:
        assert process.stdin is not None
        process.stdin.write(_core.json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        process.stdin.flush()

    operation_deadline = _core.time.monotonic() + max(0.1, float(timeout))

    def next_response() -> dict:
        while _core.time.monotonic() < operation_deadline:
            try:
                line = lines.get(
                    timeout=min(0.5, max(0.01, operation_deadline - _core.time.monotonic()))
                )
            except _core.queue.Empty:
                continue
            if line is None:
                break
            try:
                candidate = _core.json.loads(line)
            except _core.json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and candidate.get("id") is not None:
                return candidate
        detail = _core._redact_sensitive_text("\n".join(list(stderr_lines)[-8:]), limit=600) or f"exit {process.poll()}"
        raise _core.ManagerError(f"等待 Codex App Server 批量响应超时：{detail}")

    try:
        send(
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {"name": "codex_agent_manager", "title": _core.APP_NAME, "version": "3"},
                    "capabilities": {"experimentalApi": False},
                },
            }
        )
        initialized = next_response()
        if initialized.get("id") != 1:
            raise _core.ManagerError("Codex App Server 初始化响应 ID 无效。")
        if initialized.get("error"):
            raise _core.ManagerError(f"Codex App Server 初始化失败：{initialized['error']}")
        send({"method": "initialized", "params": {}})
        request_ids: list[int] = []
        for index, (method, params) in enumerate(requests, start=2):
            send({"method": method, "id": index, "params": params})
            request_ids.append(index)
        responses: dict[int, dict] = {}
        expected_ids = set(request_ids)
        unconfirmed_error = None
        while expected_ids - set(responses):
            try:
                response = next_response()
            except _core.ManagerError as exc:
                if not return_outcomes:
                    raise
                unconfirmed_error = _core._redact_sensitive_text(exc, limit=320)
                break
            try:
                response_id = int(response.get("id"))
            except (TypeError, ValueError, OverflowError):
                continue
            if response_id in expected_ids:
                responses[response_id] = response
        results = []
        for index, (method, _params) in zip(request_ids, requests):
            response = responses.get(index)
            if return_outcomes:
                if response is None:
                    results.append({"ok": False, "unconfirmed": True, "error": unconfirmed_error or "未收到操作确认，请刷新会话后核对。"})
                elif response.get("error"):
                    results.append({"ok": False, "error": _core._redact_sensitive_text(response["error"], limit=320)})
                elif not isinstance(response.get("result"), dict):
                    results.append({"ok": False, "unconfirmed": True, "error": "操作返回格式无效，请刷新会话后核对。"})
                else:
                    results.append({"ok": True, "result": response["result"]})
                continue
            if response.get("error"):
                raise _core.ManagerError(f"Codex App Server `{method}` 失败：{response['error']}")
            result = response.get("result")
            if not isinstance(result, dict):
                raise _core.ManagerError(f"Codex App Server `{method}` 返回格式无效。")
            results.append(result)
    finally:
        for stream in (process.stdin,):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        try:
            process.wait(timeout=2)
        except _core.subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        # Readers normally finish when the child closes its pipe ends.  Join
        # them before closing our handles so repeated skill/session requests do
        # not leave reader threads or TextIO wrappers waiting for GC.
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
    return results



def codex_app_server_request(method: str, params: dict, timeout: int = 30, *, launch_plan: dict | None = None) -> dict:
    options = {"launch_plan": launch_plan} if launch_plan is not None else {}
    return _core.codex_app_server_requests([(method, params)], timeout=timeout, **options)[0]



def _timestamp_iso(value: _core.Any) -> str | None:
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return _core.datetime.fromtimestamp(number, _core.timezone.utc).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return None



def _codex_thread_list_params(limit: int, archived: bool, rebuild: bool, cursor: str | None = None) -> dict:
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
    rows = []
    for item in result.get("data", []):
        if not isinstance(item, dict):
            continue
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
        updated_value = item.get("updatedAt") or item.get("recencyAt")
        try:
            updated_epoch = float(updated_value)
            if updated_epoch > 10_000_000_000:
                updated_epoch /= 1000
        except (TypeError, ValueError, OverflowError):
            updated_epoch = 0.0
        raw_status = item.get("status")
        status = raw_status if isinstance(raw_status, dict) else {}
        status_type = str(
            status.get("type")
            if status
            else raw_status
            if isinstance(raw_status, str)
            else "unknown"
        ).strip() or "unknown"
        rows.append(
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or item.get("preview") or "未命名任务")[:240],
                "preview": str(item.get("preview") or "")[:320],
                "cwd": str(item.get("cwd") or ""),
                "provider": str(item.get("modelProvider") or "unknown"),
                "source": source_kind or "unknown",
                "createdAt": _core._timestamp_iso(item.get("createdAt")),
                "updatedAt": _core._timestamp_iso(updated_value),
                "updatedEpoch": updated_epoch,
                "status": {
                    "type": status_type[:80],
                    "activeFlags": [str(value)[:80] for value in status.get("activeFlags", [])]
                    if isinstance(status.get("activeFlags"), list)
                    else [],
                },
                "archived": bool(archived),
                "pinned": bool(item.get("isPinned")),
            }
        )
    return rows



def _subagent_turn_status(thread: dict) -> tuple[str, str | None]:
    turns = thread.get("turns") if isinstance(thread.get("turns"), list) else []
    last_turn = turns[-1] if turns and isinstance(turns[-1], dict) else {}
    raw_status = last_turn.get("status")
    if isinstance(raw_status, dict):
        status = str(raw_status.get("type") or "")
    else:
        status = str(raw_status or "")
    turn_id = str(last_turn.get("id") or "").strip() or None
    return status, turn_id



def stale_subagent_health(
    settings: dict | None = None,
    *,
    stale_seconds: int = _core.STUCK_SUBAGENT_MIN_AGE_SECONDS,
    max_threads: int = 500,
) -> dict:
    """Find orphaned child turns without touching live Codex work.

    Runtime status belongs to the App Server process that owns a turn.  A
    second diagnostic process therefore must never interrupt Desktop's live
    children.  Cleanup is offered only after every Codex process is closed,
    and archives (rather than deletes) old subagent threads whose persisted
    last turn is still in progress or whose thread ended in systemError.
    """
    # Do not require a currently enabled route here.  Orphans commonly remain
    # after their account, route, or the whole subagent feature was disabled.
    # The official subAgent source + stopped-runtime + age + persisted terminal
    # state gates are the authority for this repair.
    _ = settings
    running = _core.running_codex_processes()
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
    limit = max(1, min(int(max_threads), 500))
    rows: list[dict] = []
    cursor = None
    seen_cursors: set[str] = set()
    while len(rows) < limit:
        params = _core._codex_thread_list_params(min(100, limit - len(rows)), False, False, cursor)
        params["sourceKinds"] = list(_core.SUBAGENT_SOURCE_KINDS)
        listed = _core.codex_app_server_request("thread/list", params, timeout=30)
        rows.extend(_core._codex_thread_rows(listed, False))
        next_cursor = listed.get("nextCursor") or listed.get("next_cursor")
        if not next_cursor or str(next_cursor) in seen_cursors:
            break
        seen_cursors.add(str(next_cursor))
        cursor = next_cursor
    now_epoch = _core.time.time()
    old_rows = [
        row for row in rows
        if row.get("id")
        and str(row.get("source") or "").casefold().startswith("subagent")
        and row.get("updatedEpoch", 0) > 0
        and now_epoch - float(row.get("updatedEpoch") or 0) >= max(60, int(stale_seconds))
    ]
    candidates = []
    if old_rows:
        results = []
        for offset in range(0, len(old_rows), 100):
            results.extend(
                _core.codex_app_server_requests(
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
            turn_status, turn_id = _core._subagent_turn_status(thread)
            raw_thread_status = thread.get("status")
            thread_status = str(
                (raw_thread_status or {}).get("type")
                if isinstance(raw_thread_status, dict)
                else raw_thread_status
                if isinstance(raw_thread_status, str)
                else row.get("status", {}).get("type") or ""
            )
            normalized_turn = _core.re.sub(r"[^a-z]", "", turn_status.casefold())
            normalized_thread = _core.re.sub(r"[^a-z]", "", thread_status.casefold())
            if normalized_turn not in {"inprogress", "running"} and normalized_thread != "systemerror":
                continue
            candidates.append(
                {
                    "threadId": row["id"],
                    "name": row.get("name") or "未命名子代理任务",
                    "updatedAt": row.get("updatedAt"),
                    "ageSeconds": max(0, int(now_epoch - float(row.get("updatedEpoch") or 0))),
                    "turnId": turn_id,
                    "reason": "system_error" if normalized_thread == "systemerror" else "orphaned_in_progress",
                }
            )
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
    if _core.running_codex_processes():
        raise _core.ManagerError("Codex 仍在运行。请先保存工作并关闭 Codex，再清理卡死子代理。")
    health = _core.stale_subagent_health()
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
        raise _core.ManagerError(f"清理卡死子代理失败，已恢复已归档任务：{exc}{detail}") from exc
    return {"changed": True, "archived": archived, "count": len(archived)}



def list_codex_thread_groups(active_limit: int = 500, archived_limit: int = 500, rebuild: bool = False) -> list[dict]:
    limits = {False: max(1, min(int(active_limit), 1000)), True: max(1, min(int(archived_limit), 1000))}
    cursors: dict[bool, str | None] = {False: None, True: None}
    seen_cursors: dict[bool, set[str]] = {False: set(), True: set()}
    finished = {False: False, True: False}
    rows: dict[bool, list[dict]] = {False: [], True: []}
    while not all(finished.values()):
        requests = []
        states = []
        for archived in (False, True):
            remaining = limits[archived] - len(rows[archived])
            if finished[archived] or remaining <= 0:
                finished[archived] = True
                continue
            requests.append(
                (
                    "thread/list",
                    _core._codex_thread_list_params(min(100, remaining), archived, rebuild, cursors[archived]),
                )
            )
            states.append(archived)
        if not requests:
            break
        results = _core.codex_app_server_requests(requests, timeout=60 if rebuild else 30)
        for archived, result in zip(states, results):
            page = _core._codex_thread_rows(result, archived)
            rows[archived].extend(page)
            next_cursor = result.get("nextCursor") or result.get("next_cursor")
            next_cursor = str(next_cursor) if next_cursor else None
            if next_cursor and next_cursor in seen_cursors[archived]:
                next_cursor = None
            elif next_cursor:
                seen_cursors[archived].add(next_cursor)
            cursors[archived] = next_cursor
            if not next_cursor or not page or len(rows[archived]) >= limits[archived]:
                finished[archived] = True
    from agent_manager.sessions.preferences import apply_to_threads
    return apply_to_threads(rows[False][: limits[False]] + rows[True][: limits[True]])



def refresh_codex_history_index() -> dict:
    threads = _core.list_codex_thread_groups(active_limit=1, archived_limit=1, rebuild=True)
    active = [item for item in threads if not item["archived"]]
    archived = [item for item in threads if item["archived"]]
    return {"activeSample": len(active), "archivedSample": len(archived), "refreshedAt": _core.now_iso()}



def manage_codex_threads(action: str, thread_ids: list[str]) -> dict:
    if action not in {"archive", "restore", "pin", "unpin"}:
        raise _core.ManagerError("会话操作无效。")
    if not isinstance(thread_ids, list):
        raise _core.ManagerError("threadIds 必须是数组。")
    normalized = list(dict.fromkeys(str(item).strip() for item in thread_ids if str(item).strip()))
    if not normalized or len(normalized) > 100:
        raise _core.ManagerError("请选择 1 到 100 个会话。")
    if any(not isinstance(item, str) for item in thread_ids):
        raise _core.ManagerError("会话 ID 必须是字符串。")
    if any(len(item) > 160 or not _core.re.fullmatch(r"[A-Za-z0-9._:-]+", item) for item in normalized):
        raise _core.ManagerError("会话 ID 格式无效。")
    if action in {"pin", "unpin"}:
        from agent_manager.sessions.preferences import update_pins
        return update_pins(normalized, action == "pin")
    requests = []
    for thread_id in normalized:
        if len(thread_id) > 160 or not _core.re.fullmatch(r"[A-Za-z0-9._:-]+", thread_id):
            raise _core.ManagerError("会话 ID 格式无效。")
        if action == "archive":
            requests.append(("thread/archive", {"threadId": thread_id}))
        elif action == "restore":
            requests.append(("thread/unarchive", {"threadId": thread_id}))
    outcomes = _core.codex_app_server_requests(requests, timeout=45, return_outcomes=True)
    completed = []
    failed = []
    unconfirmed = []
    for index, thread_id in enumerate(normalized):
        outcome = outcomes[index] if index < len(outcomes) else {"unconfirmed": True, "error": "缺少操作确认，请刷新会话后核对。"}
        if outcome.get("ok"):
            completed.append(thread_id)
        else:
            target = unconfirmed if outcome.get("unconfirmed") else failed
            target.append({"threadId": thread_id, "error": outcome.get("error") or "操作失败。"})
    return {"action": action, "changed": len(completed), "threadIds": completed, "failed": failed, "unconfirmed": unconfirmed}



def rename_codex_thread(thread_id: str, name: str) -> dict:
    thread_id = str(thread_id or "").strip()
    name = _core.re.sub(r"\s+", " ", str(name or "")).strip()
    if not thread_id or len(thread_id) > 160 or not _core.re.fullmatch(r"[A-Za-z0-9._:-]+", thread_id):
        raise _core.ManagerError("会话 ID 格式无效。")
    if not name or len(name) > 160:
        raise _core.ManagerError("会话名称必须在 1 到 160 个字符之间。")
    _core.codex_app_server_request("thread/name/set", {"threadId": thread_id, "name": name}, timeout=30)
    return {"threadId": thread_id, "name": name}

