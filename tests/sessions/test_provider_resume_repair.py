"""Synthetic regression coverage for stale Desktop and rollout providers."""
from __future__ import annotations

import json
import sqlite3

import pytest

import agent_manager.core as core
import agent_manager.sessions.live_selection as live
import agent_manager.sessions.visibility_engine as engine
import agent_manager.sessions.visibility as service
from tests.sessions.test_visibility_recovery import _fixture, _thread, _insert_thread, _write_rollout
from tests.sessions.test_visibility_batches import _catalog


@pytest.fixture
def storage(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    files = _fixture(home)
    monkeypatch.setattr(core, "CODEX_HOME", home)
    monkeypatch.setattr(core, "CONFIG_FILE", home / "config.toml")
    monkeypatch.setattr(core, "BACKUPS_DIR", home / "agent-manager" / "backups")
    monkeypatch.setattr(core, "running_codex_processes", lambda: [])
    (home / "config.toml").write_text('model = "gpt-fixture"\n', encoding="utf-8")
    return home, files


def _desktop(home):
    path = home / engine.DESKTOP_CATALOG_PATH
    _catalog({"db": path})
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE local_thread_catalog ADD COLUMN conversation_origin TEXT")
        db.execute("UPDATE local_thread_catalog SET model_provider='cam_aggregate'")
        db.executemany("INSERT INTO local_thread_catalog_hosts VALUES (?,?)",
                       [("remote", "ssh"), ("cloud", "chatgpt")])
        for host in ("remote", "cloud"):
            db.execute("INSERT INTO local_thread_catalog SELECT ?,?,display_title,source_created_at,"
                       "source_updated_at,cwd,source_kind,source_detail,model_provider,git_branch,"
                       "observation_sequence,missing_candidate,thread_source,source_recency_at,"
                       "pending_observed_title,project_id,conversation_origin FROM local_thread_catalog "
                       "WHERE host_id='local'", (host, host + "-thread"))
    return path


def _catalog_rows(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT * FROM local_thread_catalog ORDER BY host_id,thread_id").fetchall()


def test_separate_desktop_catalog_repaired_with_state_and_rollout_then_inverse_restored(storage):
    home, files = storage
    desktop = _desktop(home)
    before_catalog = _catalog_rows(desktop)
    before_thread = _thread(files["db"], "user-root")
    before_rollout = files["user"].read_bytes()
    protected = {key: files[key].read_bytes() for key in ("child", "agent", "archived", "legacy")}
    report = service.inspect()
    assert report["desktopCatalog"]["status"] == "supported"
    assert any(item["table"] == "local_thread_catalog" for item in report["plan"])
    result = service.repair_current_provider()
    assert result["status"] == "repaired" and result["sqliteRowsUpdated"] == 2
    assert _thread(files["db"], "user-root")[0] == "openai"
    rows = _catalog_rows(desktop)
    assert rows[0] == before_catalog[0] and rows[2] == before_catalog[2]
    assert rows[1][8] == "openai"
    assert files["user"].read_bytes().splitlines()[1:] == before_rollout.splitlines()[1:]
    assert {key: files[key].read_bytes() for key in protected} == protected
    with sqlite3.connect(desktop) as db:
        db.execute("UPDATE local_thread_catalog SET display_title='later title' WHERE host_id='local'")
    restored = service.restore_provider_repair(result)
    assert restored["preservedUnrelatedChanges"]
    assert _thread(files["db"], "user-root") == before_thread
    assert files["user"].read_bytes() == before_rollout
    rows = _catalog_rows(desktop)
    assert rows[1][8] == "cam_aggregate" and rows[1][2] == "later title"


def test_rollout_only_provider_drift_is_audited_when_sql_is_already_healthy(storage):
    home, files = storage
    service.auto_repair("openai")
    files["user"].write_bytes(files["user"].read_bytes().replace(b'"openai"', b'"cam_aggregate"'))
    assert service.inspect()["plan"] == []  # The old SQL-candidate scan misses it.
    result = service.repair_current_provider()
    assert result["rolloutFilesUpdated"] == 1 and result["sqliteRowsUpdated"] == 0
    assert json.loads(files["user"].read_bytes().splitlines()[0])["payload"]["model_provider"] == "openai"


def test_catalog_only_drift_is_detected_without_touching_rollout(storage):
    home, files = storage
    service.auto_repair("openai")
    desktop = _desktop(home)
    before = files["user"].read_bytes()
    result = service.repair_current_provider()
    assert result["sqliteRowsUpdated"] == 1 and result["rolloutFilesUpdated"] == 0
    assert _catalog_rows(desktop)[1][8] == "openai"
    assert files["user"].read_bytes() == before


def test_desktop_changes_invalidate_inspection_before_backup(storage):
    home, _files = storage
    desktop = _desktop(home)
    report = service.inspect()
    with sqlite3.connect(desktop) as db:
        db.execute("UPDATE local_thread_catalog SET display_title='changed'")
    with pytest.raises(core.ManagerError, match="过期"):
        service.repair(report["repairToken"])
    assert not core.BACKUPS_DIR.exists()


def test_rollout_write_failure_rolls_back_both_databases(storage, monkeypatch):
    home, files = storage
    desktop = _desktop(home)
    before = _catalog_rows(desktop), _thread(files["db"], "user-root"), files["user"].read_bytes()
    def fail(*_args):
        raise OSError("synthetic rollout failure")
    monkeypatch.setattr(engine, "_apply_rollout_action", fail)
    with pytest.raises(core.ManagerError, match="自动回滚"):
        service.repair_current_provider()
    assert (_catalog_rows(desktop), _thread(files["db"], "user-root"), files["user"].read_bytes()) == before


@pytest.mark.parametrize("conflict", ["remote_duplicate", "unknown_schema", "multiple_local_hosts", "unknown_origin"])
def test_desktop_ambiguous_rows_are_never_rewritten(storage, conflict):
    home, _files = storage
    desktop = _desktop(home)
    with sqlite3.connect(desktop) as db:
        if conflict == "remote_duplicate":
            db.execute("UPDATE local_thread_catalog SET thread_id='user-root' WHERE host_id='remote'")
        elif conflict == "unknown_schema":
            db.execute("ALTER TABLE local_thread_catalog ADD COLUMN credential_identity TEXT")
        elif conflict == "unknown_origin":
            db.execute("UPDATE local_thread_catalog SET conversation_origin='cloud' WHERE host_id='local'")
        else:
            db.execute("UPDATE local_thread_catalog_hosts SET host_kind='local' WHERE host_id='remote'")
    before = _catalog_rows(desktop)
    result = service.repair_current_provider()
    assert result["completion"]["partial"]
    assert _catalog_rows(desktop) == before


def test_current_repair_requires_stopped_and_never_invents_missing_provider_alias(storage, monkeypatch):
    home, _files = storage
    (home / "config.toml").write_text('model_provider = "cam_aggregate"\n', encoding="utf-8")
    with pytest.raises(core.ManagerError, match="定义缺失"):
        service.repair_current_provider()
    selection = live.inspect_live_selection({"modelWorkspace": {}, "providers": []})
    assert not selection["recognized"] and selection["reason"] == "model_provider_not_found"
    monkeypatch.setattr(core, "running_codex_processes", lambda: [{"pid": 42}])
    with pytest.raises(core.ManagerError, match="停止"):
        service.repair_current_provider()
    assert not core.BACKUPS_DIR.exists()


def test_provider_audit_preserves_quick_budgets_and_reports_partial(storage, monkeypatch):
    home, files = storage
    monkeypatch.setattr(engine, "DEFAULT_QUICK_REPAIR_BYTES", 1)
    result = service.repair_current_provider()
    assert result["status"] == "deferred" and not result["changed"]
    assert result["completion"]["partial"]
    assert result["verification"]["scanBudget"]["plannedRepairBytes"] == 0
    assert _thread(files["db"], "user-root")[0] == "old-provider"


def test_switch_enables_rollout_provider_audit_and_rollback_contract(storage, monkeypatch):
    monkeypatch.setattr(core, "load_settings", lambda: {"sessionSync": {"enabled": True}})
    called = []
    monkeypatch.setattr(service, "auto_repair", lambda target, **kw: called.append((target, kw)) or {})
    core._repair_switch_session_visibility()
    assert called == [("openai", {"check_all_providers": True})]
    monkeypatch.setattr(service, "restore_provider_repair", lambda result: called.append(result))
    core._restore_switch_session_visibility({"backupId": "synthetic"})
    assert called[-1] == {"backupId": "synthetic"}


def test_stopped_wal_databases_repair_despite_reader_sidecar_updates(storage):
    home, files = storage
    desktop = _desktop(home)
    for path in (files["db"], desktop):
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA journal_mode=WAL")
    first = service.inspect()
    second = service.inspect()
    assert first["safeToRepair"] and second["safeToRepair"]
    assert first["repairToken"] == second["repairToken"]
    result = service.repair_current_provider()
    assert result["changed"] and result["sqliteRowsUpdated"] == 2
    assert _thread(files["db"], "user-root")[0] == "openai"
    assert _catalog_rows(desktop)[1][8] == "openai"
    service.restore_provider_repair(result)
    assert _catalog_rows(desktop)[1][8] == "cam_aggregate"


def test_real_wal_writes_still_invalidate_inspection(storage):
    home, files = storage
    db = sqlite3.connect(files["db"])
    try:
        db.execute("PRAGMA journal_mode=WAL")
        before = service.inspect()
        db.execute("UPDATE threads SET model_provider='another-provider' WHERE id='user-root'")
        db.commit()
        after = service.inspect()
        assert before["repairToken"] != after["repairToken"]
        with pytest.raises(core.ManagerError, match="过期"):
            service.repair(before["repairToken"])
    finally:
        db.close()


def _audit_roots(home, files, count=4, *, stale_sql=False):
    service.auto_repair("openai")
    paths = []
    with sqlite3.connect(files["db"]) as db:
        for index in range(count):
            thread_id = f"a-healthy-{index}" if index < count - 1 else "zz-stale-target"
            path = _write_rollout(home, thread_id, provider="openai")
            _insert_thread(db, thread_id, path.relative_to(home).as_posix(), provider="openai",
                           has_user_event=1, thread_source="user", preview="hello")
            db.execute("UPDATE threads SET cwd='C:\\Workspace' WHERE id=?", (thread_id,))
            path.write_bytes(path.read_bytes() + b"{}\n" * 1800)
            paths.append(path)
        if stale_sql:
            db.execute("UPDATE threads SET model_provider='cam_aggregate' WHERE id='zz-stale-target'")
    paths[-1].write_bytes(paths[-1].read_bytes().replace(b'"openai"', b'"cam_aggregate"'))
    return paths


@pytest.mark.parametrize("mismatch", ["sql", "catalog"])
def test_known_stale_target_precedes_many_healthy_roots_under_tiny_scan_budget(storage, mismatch):
    home, files = storage
    _audit_roots(home, files, count=8, stale_sql=mismatch == "sql")
    if mismatch == "catalog":
        desktop = _desktop(home)
        with sqlite3.connect(desktop) as db:
            db.execute("UPDATE local_thread_catalog SET thread_id='zz-stale-target' WHERE host_id='local'")
    report = engine.inspect_session_visibility(home, "openai", check_all_providers=True, max_scan_bytes=4096)
    assert report["providerScan"]["scannedSessionIds"] == ["zz-stale-target"]
    assert any(action["threadId"] == "zz-stale-target" for action in report["actions"])


def test_one_click_advances_healthy_batches_to_rollout_only_drift_without_rescanning(storage, monkeypatch):
    home, files = storage
    paths = _audit_roots(home, files, count=4)
    monkeypatch.setattr(engine, "DEFAULT_QUICK_SCAN_BYTES", 4096)
    scopes = []
    original = service._inspect
    def observe(*args, **kwargs):
        report = original(*args, **kwargs)
        scopes.append(report[0]["providerScan"]["scannedSessionIds"])
        return report
    monkeypatch.setattr(service, "_inspect", observe)
    result = service.repair_current_provider()
    assert result["batches"] > 1 and result["status"] == "repaired"
    assert result["rolloutFilesUpdated"] == 1
    assert len([value for group in scopes for value in group]) == len(set(value for group in scopes for value in group))
    assert json.loads(paths[-1].read_bytes().splitlines()[0])["payload"]["model_provider"] == "openai"


def test_multiple_explicit_batches_restore_all_backups_and_later_failure_rolls_back(storage, monkeypatch):
    home, files = storage
    paths = _audit_roots(home, files, count=3, stale_sql=True)
    paths[0].write_bytes(paths[0].read_bytes().replace(b'"openai"', b'"cam_aggregate"'))
    monkeypatch.setattr(engine, "DEFAULT_QUICK_SCAN_BYTES", 4096)
    before = {path: path.read_bytes() for path in paths}
    row = _thread(files["db"], "zz-stale-target")
    result = service.repair_current_provider()
    assert len(result["backupIds"]) == 2 and result["backupId"] is None
    assert service.restore_provider_repair(result)["restored"]
    assert {path: path.read_bytes() for path in paths} == before
    assert _thread(files["db"], "zz-stale-target") == row
    original = service._apply
    calls = []
    def fail_later(*args):
        calls.append(None)
        if len(calls) == 2:
            raise engine.SessionVisibilityError("synthetic second batch failure")
        return original(*args)
    monkeypatch.setattr(service, "_apply", fail_later)
    with pytest.raises(core.ManagerError, match="已完成批次已自动回滚"):
        service.repair_current_provider()
    assert {path: path.read_bytes() for path in paths} == before
    assert _thread(files["db"], "zz-stale-target") == row


def test_explicit_repair_stops_at_total_scan_budget_without_repeating_first_batch(storage, monkeypatch):
    home, files = storage
    _audit_roots(home, files, count=5)
    monkeypatch.setattr(engine, "DEFAULT_QUICK_SCAN_BYTES", 4096)
    monkeypatch.setattr(service, "MAX_PROVIDER_REPAIR_SCAN_BYTES", 3 * 4096)
    result = service.repair_current_provider()
    assert result["batches"] == 1 and result["stopReason"] == "total_work_budget"
    assert result["completion"]["partial"] and result["completion"]["deferredSessions"] > 0
    assert result["workBudget"]["reservedPrefixReadBytes"] <= 3 * 4096


def test_explicit_repair_stops_when_no_new_root_is_scanned_and_no_write_occurs(storage, monkeypatch):
    original = service._inspect
    calls = []
    def stalled(*args, **kwargs):
        report, provider, token = original(*args, **kwargs)
        report["providerScan"].update(scannedSessionIds=[], deferredSessionIds=["user-root"])
        report["actions"] = []
        calls.append(None)
        return report, provider, token
    monkeypatch.setattr(service, "_inspect", stalled)
    monkeypatch.setattr(service, "_apply", lambda _core, report, provider: {
        "changed": False, "sqliteRowsUpdated": 0, "rolloutFilesUpdated": 0,
        "verification": service._public(report, provider)})
    result = service.repair_current_provider()
    assert len(calls) == 1 and result["stopReason"] == "no_progress"
