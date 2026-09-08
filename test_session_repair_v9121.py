from contextlib import nullcontext
from unittest.mock import Mock
import json
import threading
import urllib.error
import urllib.request

import pytest

import agent_manager_app as app
import session_repair_service as service


@pytest.fixture
def environment(monkeypatch):
    order = []
    scan = Mock(side_effect=[[{"pid": "42"}], []])
    close = Mock(side_effect=lambda: order.append("close"))
    launch = Mock(side_effect=lambda **kwargs: order.append("launch"))
    repair = Mock(side_effect=lambda: order.append("repair") or {
        "changed": True, "status": "repaired", "backupId": "retained",
        "completion": {"partial": False},
    })
    monkeypatch.setattr(service.core, "_exclusive_switch_operation", lambda *args: nullcontext())
    monkeypatch.setattr(service.core, "read_toml", lambda *args: {"model_provider": "openai"})
    monkeypatch.setattr(service.core, "_require_codex_process_scan_known", scan)
    monkeypatch.setattr(service.core, "resolve_codex_launch_plan", lambda: {"strategy": "desktop_executable"})
    monkeypatch.setattr(service.core, "launch_codex_app", launch)
    monkeypatch.setattr(service.history, "list_recoveries", lambda **kwargs: {"recoveries": []})
    monkeypatch.setattr(service.visibility, "repair_current_provider", repair)
    return order, close, launch, repair, scan


def test_one_click_stops_then_repairs_and_restarts_official_environment(environment):
    order, close, launch, repair, scan = environment
    result = service.repair_sessions(close_codex=close)
    assert order == ["close", "repair", "launch"]
    assert result["status"] == "repaired" and result["restarted"]
    assert result["visibility"]["backupId"] == "retained"
    assert launch.call_args.kwargs["launch_plan"]["officialAccountId"] == "current"


def test_stopped_codex_is_not_started_unnecessarily(environment):
    order, close, launch, repair, scan = environment
    scan.side_effect = [[], []]
    result = service.repair_sessions(close_codex=close)
    assert order == ["repair"] and not result["restarted"]
    close.assert_not_called()
    launch.assert_not_called()


def test_resolves_launch_before_closing(environment, monkeypatch):
    _, close, _, repair, _ = environment
    def unavailable():
        raise service.core.ManagerError("missing launcher")
    monkeypatch.setattr(service.core, "resolve_codex_launch_plan", unavailable)
    with pytest.raises(service.core.ManagerError, match="missing launcher"):
        service.repair_sessions(close_codex=close)
    close.assert_not_called()
    repair.assert_not_called()


def test_unknown_process_scan_never_closes_or_repairs(environment):
    _, close, _, repair, scan = environment
    scan.side_effect = service.core.ManagerError("unknown process scan")
    with pytest.raises(service.core.ManagerError, match="unknown process scan"):
        service.repair_sessions(close_codex=close)
    close.assert_not_called()
    repair.assert_not_called()


def test_missing_current_provider_is_rejected_before_stopping(environment, monkeypatch):
    _, close, _, repair, _ = environment
    monkeypatch.setattr(service.core, "read_toml", lambda *args: {"model_provider": "cam_aggregate"})
    with pytest.raises(service.core.ManagerError, match="当前服务配置不完整"):
        service.repair_sessions(close_codex=close)
    close.assert_not_called()
    repair.assert_not_called()


def test_failed_stop_gate_never_writes(environment):
    _, close, _, repair, scan = environment
    scan.side_effect = [[{"pid": "42"}], [{"pid": "42"}]]
    result = service.repair_sessions(close_codex=close)
    assert result["partial"] and not result["changed"]
    repair.assert_not_called()


def test_failure_still_reopens_codex_and_retains_error(environment):
    order, close, _, repair, _ = environment
    repair.side_effect = service.core.ManagerError("backup could not be written")
    result = service.repair_sessions(close_codex=close)
    assert result["status"] == "partial" and result["restarted"]
    assert "backup could not be written" in result["warnings"][0]
    assert order == ["close", "launch"]


def test_launch_failure_does_not_claim_full_success(environment):
    _, close, launch, _, _ = environment
    launch.side_effect = service.core.ManagerError("launch failed")
    result = service.repair_sessions(close_codex=close)
    assert result["changed"] and result["partial"] and not result["restarted"]
    assert result["visibility"]["backupId"] == "retained"


def test_history_recovery_precedes_provider_repair_and_preserves_conflicts(environment, monkeypatch):
    order, close, _, _, _ = environment
    monkeypatch.setattr(service.history, "list_recoveries", lambda **kwargs: {"recoveries": [
        {"id": "good", "recoverable": True}, {"id": "conflict", "recoverable": True},
        {"id": "untrusted", "recoverable": False},
    ]})
    monkeypatch.setattr(service.history, "preview_recovery", lambda rid: {"conflicts": rid == "conflict", "fingerprint": "verified"})
    restore = Mock(side_effect=lambda rid, fingerprint: order.append("restore") or {"restored": True, "changed": 1})
    monkeypatch.setattr(service.history, "restore_recovery", restore)
    result = service.repair_sessions(close_codex=close)
    assert order == ["close", "restore", "repair", "launch"]
    restore.assert_called_once_with("good", "verified")
    assert result["partial"] and len(result["recoveries"]) == 1 and len(result["warnings"]) == 2


def test_failed_journal_restore_stops_followup_writes(environment, monkeypatch):
    _, close, _, repair, _ = environment
    monkeypatch.setattr(service.history, "list_recoveries", lambda **kwargs: {"recoveries": [{"id": "failed", "recoverable": True}]})
    monkeypatch.setattr(service.history, "preview_recovery", lambda rid: {"conflicts": 0, "fingerprint": "verified"})
    monkeypatch.setattr(service.history, "restore_recovery", Mock(side_effect=service.core.ManagerError("restore failed")))
    result = service.repair_sessions(close_codex=close)
    assert result["partial"] and result["restarted"]
    repair.assert_not_called()


def test_duplicate_click_cannot_start_second_operation(environment):
    _, close, _, repair, _ = environment
    service._REPAIR_LOCK.acquire()
    try:
        with pytest.raises(service.core.ManagerError, match="正在执行"):
            service.repair_sessions(close_codex=close)
    finally:
        service._REPAIR_LOCK.release()
    close.assert_not_called()
    repair.assert_not_called()


def test_endpoint_is_authenticated_and_uses_manager_safe_close(monkeypatch):
    repair = Mock(return_value={"status": "healthy", "changed": False})
    monkeypatch.setattr(service, "repair_sessions", repair)
    server = app.ManagerServer(("127.0.0.1", 0), app.RequestHandler, object())
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/api/sessions/repair"
        request = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as unauthorized:
            urllib.request.urlopen(request, timeout=3)
        assert unauthorized.value.code == 403
        unauthorized.value.close()
        repair.assert_not_called()
        request.add_header("X-Agent-Manager-Token", server.api_token)
        with urllib.request.urlopen(request, timeout=3) as response:
            assert json.load(response)["result"]["status"] == "healthy"
        repair.assert_called_once_with(close_codex=app._close_codex_processes_safely)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
