"""Authenticated application location operations and guarded restart handoff."""
from __future__ import annotations
import logging

from agent_manager import application as _app


def application_location_status(server):
    from agent_manager.updates.location import read_location_status
    status = read_location_status(state_dir=_app.core.STATE_DIR, runtime_file=_app.RUNTIME_FILE)
    return {**status, "executable": status.get("currentExecutable"),
            "directory": status.get("currentDirectory"), "relocation": status.get("move"),
            "canBrowse": bool(status.get("supported") and getattr(server, "native_window", False)
                              and getattr(server, "native_window_object", None) is not None)}


def select_application_directory(server):
    window = getattr(server, "native_window_object", None)
    if not getattr(server, "native_window", False) or window is None:
        raise _app.core.ManagerError("当前使用浏览器界面，请直接输入目标文件夹路径。")
    import webview
    selected = window.create_file_dialog(webview.FileDialog.FOLDER, directory=str(_app.Path(_app.sys.executable).parent))
    if not selected:
        return {"directory": None, "cancelled": True}
    return {"directory": str(selected[0]), "cancelled": False}


def assert_application_location_idle(server):
    move = application_location_status(server).get("relocation") or {}
    if move.get("helperRunning") and move.get("state") not in {"complete", "failed"}:
        raise _app.core.ManagerError("程序位置正在变更，请等待完成后再操作。")


def _watch_location_move(server, service):
    """Keep update and relocation mutually exclusive until their handoff ends."""
    deadline = _app.time.monotonic() + 600
    try:
        while not getattr(server.runtime, "_closed", False) and _app.time.monotonic() < deadline:
            try:
                status = application_location_status(server).get("relocation") or {}
            except Exception:
                _app.time.sleep(0.5)
                continue
            if status.get("state") in {"complete", "failed"} or (
                    status.get("state") == "cleanup_pending" and status.get("helperRunning") is False):
                return
            _app.time.sleep(0.5)
    finally:
        service._operation.release()


def prepare_application_relocation(server, directory):
    from agent_manager.updates.location import prepare_location_move, launch_location_move
    service = server.runtime.get_app_updates()
    if not service._operation.acquire(blocking=False):
        raise _app.core.ManagerError("正在更新或移动程序，请等待当前操作完成。")
    handed_off = False
    try:
        assert_application_location_idle(server)
        try:
            prepared = prepare_location_move(destination=directory, state_dir=_app.core.STATE_DIR,
                shutdown_status=_app.SHUTDOWN_STATUS_FILE, source_pid=_app.os.getpid(),
                source_nonce=server.runtime_nonce, runtime_file=_app.RUNTIME_FILE)
        except _app.app_updates.UpdateError as exc:
            if exc.code == "same_location":
                return {"started": False, "message": str(exc)}
            raise
        result = launch_location_move(prepared)
        if not result.get("started"):
            raise _app.core.ManagerError("移动助手未确认就绪，已取消移动。")
        watcher = _app.threading.Thread(target=_watch_location_move, args=(server, service),
            name="app-location-move-monitor", daemon=True)
        watcher.start()
        handed_off = True
        timer = _app.threading.Timer(0.25, _app.request_application_shutdown, args=(server,))
        timer.daemon = True
        timer.start()
        return result
    finally:
        if not handed_off:
            service._operation.release()


def create_application_shortcut(server):
    from agent_manager.updates.location import create_desktop_shortcut
    service = server.runtime.get_app_updates()
    if not service._operation.acquire(blocking=False):
        raise _app.core.ManagerError("正在更新或移动程序，请稍后创建快捷方式。")
    try:
        assert_application_location_idle(server)
        return create_desktop_shortcut(state_dir=_app.core.STATE_DIR)
    finally:
        service._operation.release()


def reveal_application_shortcut(server):
    from agent_manager.updates.location import reveal_desktop_shortcut
    service = server.runtime.get_app_updates()
    if not service._operation.acquire(blocking=False):
        raise _app.core.ManagerError("正在更新或移动程序，请稍后定位快捷方式。")
    try:
        assert_application_location_idle(server)
        return reveal_desktop_shortcut(state_dir=_app.core.STATE_DIR)
    finally:
        service._operation.release()


def finish_application_location_handoff(server):
    """Resume a verified relocation cleanup once the new window is ready."""
    try:
        application_location_status(server)
    except Exception:
        # Status remains available from Settings; startup must not fail because
        # a previous installation or a locked old file needs another attempt.
        logging.getLogger(__name__).exception("Could not finish application location handoff")
