import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from agent_manager import application as app
from agent_manager.updates import location
from agent_manager.updates.service import UpdateError


@pytest.fixture
def handoff(monkeypatch):
    service = SimpleNamespace(_operation=threading.Lock())
    server = SimpleNamespace(runtime=SimpleNamespace(get_app_updates=lambda: service), runtime_nonce="synthetic_nonce_123456789")
    monkeypatch.setattr(app, "assert_application_location_idle", lambda _: None)
    # The coordination module uses its local name for this internal call.
    from agent_manager.application import location as coordinator
    monkeypatch.setattr(coordinator, "assert_application_location_idle", lambda _: None)
    thread, timer = Mock(), Mock()
    monkeypatch.setattr(app.threading, "Thread", thread)
    monkeypatch.setattr(app.threading, "Timer", timer)
    prepare = Mock(return_value={"spec": {"moveId": "fixture"}})
    launch = Mock(return_value={"started": True})
    monkeypatch.setattr(location, "prepare_location_move", prepare)
    monkeypatch.setattr(location, "launch_location_move", launch)
    return server, service, prepare, launch, thread, timer


def test_move_starts_guarded_shutdown_only_after_helper_ready(handoff):
    server, service, prepare, launch, thread, timer = handoff
    assert app.prepare_application_relocation(server, "D:\\应用\\工具") == {"started": True}
    assert prepare.call_args.kwargs["destination"] == "D:\\应用\\工具"
    assert prepare.call_args.kwargs["source_nonce"] == server.runtime_nonce
    launch.assert_called_once_with(prepare.return_value)
    thread.return_value.start.assert_called_once()
    timer.assert_called_once_with(0.25, app.request_application_shutdown, args=(server,))
    timer.return_value.start.assert_called_once()
    assert service._operation.locked()
    service._operation.release()  # Real watcher owns this release.


def test_failed_preflight_keeps_app_running_and_releases_operation(handoff):
    server, service, prepare, launch, thread, timer = handoff
    launch.side_effect = UpdateError("助手未就绪", "helper_not_ready")
    with pytest.raises(UpdateError):
        app.prepare_application_relocation(server, "D:\\应用")
    assert not service._operation.locked()
    thread.assert_not_called(); timer.assert_not_called()


def test_same_directory_is_a_noop_and_update_lock_blocks_move(handoff):
    server, service, prepare, launch, thread, timer = handoff
    prepare.side_effect = UpdateError("程序已在所选目录中。", "same_location")
    assert app.prepare_application_relocation(server, "D:\\应用")["started"] is False
    assert not service._operation.locked()
    launch.assert_not_called(); timer.assert_not_called()
    service._operation.acquire()
    try:
        with pytest.raises(app.core.ManagerError, match="正在更新或移动"):
            app.prepare_application_relocation(server, "D:\\另一个目录")
    finally:
        service._operation.release()


def test_ready_publication_precedes_startup_cleanup(monkeypatch):
    server = SimpleNamespace(ui_ready=threading.Event())
    events = []
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app, "_write_runtime_discovery", lambda _: events.append("runtime-ready"))
    thread = Mock()
    thread.return_value.start.side_effect = lambda: events.append("cleanup")
    monkeypatch.setattr(app.threading, "Thread", thread)
    app._mark_ui_ready(server)
    app._mark_ui_ready(server)
    assert events == ["runtime-ready", "cleanup", "runtime-ready"]
    assert server.ui_ready.is_set()
    thread.assert_called_once()
