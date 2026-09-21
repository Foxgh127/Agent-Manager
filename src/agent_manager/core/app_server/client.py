"""App Server 请求处理核心"""
from __future__ import annotations
import json
import queue
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any

from agent_manager import core as _core
from agent_manager.core.i18n import t


def codex_app_server_requests(
    requests: list[tuple[str, dict]],
    timeout: int | float = 30,
    *,
    return_outcomes: bool = False,
    launch_plan: dict | None = None
) -> list[dict]:
    """
    向 Codex App Server 发送批量请求

    Args:
        requests: 请求列表，每项为 (method, params) 元组
        timeout: 超时时间（秒）
        return_outcomes: 是否返回操作结果而不抛出异常
        launch_plan: 启动计划

    Returns:
        响应列表
    """
    if not requests or len(requests) > 200:
        raise _core.ManagerError(t("errors.batch_request_range", min=1, max=200))

    for method, params in requests:
        if not isinstance(method, str) or not method or not isinstance(params, dict):
            raise _core.ManagerError(t("errors.request_invalid"))

    # 准备环境和启动配置
    env = _core._codex_source_environment(launch_plan)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _core.os.name == "nt" else 0
    primary_prefix = _core._codex_launch_probe_prefix(launch_plan)

    # 确定工作目录
    probe_cwd = _determine_probe_cwd(launch_plan)

    # 启动进程
    process = _spawn_app_server_process(
        primary_prefix, env, flags, probe_cwd, launch_plan
    )

    # 设置通信管道
    lines: queue.Queue[str | None] = queue.Queue()
    stderr_lines: deque[str] = deque(maxlen=64)

    # 启动读取线程
    _start_io_threads(process, lines, stderr_lines)

    # 发送请求并接收响应
    try:
        results = _execute_request_batch(
            process, lines, stderr_lines, requests,
            timeout, return_outcomes
        )
        return results
    finally:
        _cleanup_process(process)


def _determine_probe_cwd(launch_plan: dict | None) -> str | None:
    """确定探针工作目录"""
    probe_cwd = None

    if isinstance(launch_plan, dict):
        raw_workspace = str(launch_plan.get("workspace") or "").strip()
        if raw_workspace:
            workspace = Path(raw_workspace).expanduser()
            if workspace.is_dir():
                probe_cwd = str(workspace.resolve())

        if probe_cwd is None:
            workspace = _core._recent_codex_workspace()
            if workspace and workspace.is_dir():
                probe_cwd = str(workspace.resolve())

    return probe_cwd


def _spawn_app_server_process(
    primary_prefix: list[str],
    env: dict,
    flags: int,
    probe_cwd: str | None,
    launch_plan: dict | None
) -> subprocess.Popen:
    """启动 App Server 进程"""
    prefixes = [primary_prefix]
    spawn_errors: list[tuple[list[str], OSError]] = []
    process = None

    for prefix in prefixes:
        try:
            process = subprocess.Popen(
                prefix + ["app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=flags,
                **({"cwd": probe_cwd} if probe_cwd else {}),
            )
            break
        except OSError as exc:
            spawn_errors.append((prefix, exc))
            if not launch_plan or not isinstance(launch_plan, dict):
                break

            # 检查是否需要回退
            winerror = getattr(exc, "winerror", None)
            if winerror not in {5, 2, 3} and getattr(exc, "errno", None) not in {2, 13}:
                break

            # 尝试回退路径
            try:
                fallback = _core.codex_prefix()
            except Exception:
                fallback = []

            fallback_executable = Path(str(fallback[0])) if fallback else None
            if (
                fallback
                and fallback_executable is not None
                and not _core._is_windows_store_path(fallback_executable)
                and fallback != prefix
                and fallback not in prefixes
            ):
                prefixes.append(fallback)

    if process is None:
        details = []
        for prefix, exc in spawn_errors[-2:]:
            executable = str(prefix[0]) if prefix else "<empty>"
            details.append(f"{executable}: {exc}")
        raise _core.ManagerError(
            "无法启动 Codex App Server 探针：" + "；".join(details or ["未知启动错误"])
        ) from (spawn_errors[-1][1] if spawn_errors else None)

    if process.stdin is None or process.stdout is None or process.stderr is None:
        process.kill()
        raise _core.ManagerError("无法连接 Codex App Server 标准输入输出。")

    return process


def _start_io_threads(
    process: subprocess.Popen,
    lines: queue.Queue,
    stderr_lines: deque
) -> None:
    """启动 I/O 读取线程"""
    def read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    def read_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            stderr_lines.append(line.rstrip())

    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()


def _execute_request_batch(
    process: subprocess.Popen,
    lines: queue.Queue,
    stderr_lines: deque,
    requests: list[tuple[str, dict]],
    timeout: float,
    return_outcomes: bool
) -> list[dict]:
    """执行请求批次并收集响应"""
    import time

    def send(payload: dict) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        process.stdin.flush()

    operation_deadline = time.monotonic() + max(0.1, float(timeout))

    def next_response() -> dict:
        while time.monotonic() < operation_deadline:
            try:
                line = lines.get(
                    timeout=min(0.5, max(0.01, operation_deadline - time.monotonic()))
                )
            except queue.Empty:
                continue
            if line is None:
                break
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and candidate.get("id") is not None:
                return candidate
        detail = _core._redact_sensitive_text("\n".join(list(stderr_lines)[-8:]), limit=600) or f"exit {process.poll()}"
        raise _core.ManagerError(f"等待 Codex App Server 批量响应超时：{detail}")

    # 初始化
    send({
        "method": "initialize",
        "id": 1,
        "params": {
            "clientInfo": {"name": "codex_agent_manager", "title": _core.APP_NAME, "version": "3"},
            "capabilities": {"experimentalApi": False},
        },
    })
    initialized = next_response()
    if initialized.get("id") != 1:
        raise _core.ManagerError("Codex App Server 初始化响应 ID 无效。")
    if initialized.get("error"):
        raise _core.ManagerError(f"Codex App Server 初始化失败：{initialized['error']}")
    send({"method": "initialized", "params": {}})

    # 发送所有请求
    request_ids: list[int] = []
    for index, (method, params) in enumerate(requests, start=2):
        send({"method": method, "id": index, "params": params})
        request_ids.append(index)

    # 收集响应
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

    # 构建结果
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

    return results


def _cleanup_process(process: subprocess.Popen) -> None:
    """清理进程资源"""
    for stream in (process.stdin,):
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)

    for stream in (process.stdout, process.stderr):
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass


def codex_app_server_request(
    method: str,
    params: dict,
    timeout: int = 30,
    *,
    launch_plan: dict | None = None
) -> dict:
    """
    向 Codex App Server 发送单个请求

    Args:
        method: 方法名
        params: 参数字典
        timeout: 超时时间（秒）
        launch_plan: 启动计划

    Returns:
        响应字典
    """
    results = codex_app_server_requests([(method, params)], timeout, launch_plan=launch_plan)
    return results[0]
