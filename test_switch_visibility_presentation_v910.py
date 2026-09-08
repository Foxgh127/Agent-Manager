import threading
import agent_manager_app as app
from unittest.mock import patch
import web2api_service
from types import SimpleNamespace


def test_completed_switch_keeps_partial_history_result_for_polling():
    runtime = app.ManagerRuntime.__new__(app.ManagerRuntime)
    runtime.switch_operation_lock = threading.RLock()
    runtime.switch_operations = {}
    runtime.begin_switch_operation("switch-v910", target_id="fixture", target_name="fixture", target_kind="account")
    result = runtime.finish_switch_operation("switch-v910", result={"sessionSync":{"visibility":{
        "status":"partial", "changed":True, "message":"已完成部分历史会话同步。",
        "completion":{"partial":True,"deferredSessions":2,"catalogMissing":1,"remainingActions":0},
        "verification":{"internal":"not-for-progress"}, "backupId":"private-backup-reference",
    }}})
    visibility = result["sessionVisibility"]
    assert result["status"] == "completed"
    assert visibility["partial"] is True and visibility["deferredSessions"] == 2
    assert "verification" not in visibility and "backupId" not in visibility
    assert runtime.switch_operation_status("switch-v910")["sessionVisibility"] == visibility


def test_reenabling_backfill_is_lazy_without_file_or_thread_work():
    with patch.object(web2api_service.threading, "Thread") as thread, patch.object(web2api_service, "codex_session_usage_backfill_step") as step:
        web2api_service.enable_codex_session_usage_backfill()
    thread.assert_not_called()
    step.assert_not_called()


def test_internal_usage_snapshot_never_starts_background_history_work():
    manager = web2api_service.Web2APIManager.__new__(web2api_service.Web2APIManager)
    manager.usage_stats = SimpleNamespace(snapshot=lambda: {})
    manager.last_usage_error = None
    with patch.object(web2api_service, "codex_session_usage_snapshot", return_value={"coverage":{"backfill":{"pendingFiles":34}}}), patch.object(web2api_service, "account_attribution_snapshot", return_value={}), patch.object(web2api_service, "request_codex_session_usage_backfill") as start:
        assert manager.usage_snapshot()["codexSessions"]["coverage"]["backfill"]["pendingFiles"] == 34
    start.assert_not_called()
