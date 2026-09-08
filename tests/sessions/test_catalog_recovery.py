"""Request native Desktop catalog reconstruction in synthetic stopped storage."""
import sqlite3

import pytest

from agent_manager import core
from agent_manager.sessions import visibility as service
from agent_manager.sessions import visibility_engine as engine
from tests.sessions.test_provider_resume_repair import storage, _desktop


def coordinator(path):
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE local_thread_catalog_sync_state (
                host_id TEXT PRIMARY KEY, watermark_updated_at REAL,
                initial_build_complete INTEGER NOT NULL DEFAULT 0,
                observation_sequence INTEGER NOT NULL DEFAULT 0,
                last_full_reconciled_at INTEGER);
            CREATE TABLE local_thread_catalog_scan_checkpoints (
                host_id TEXT PRIMARY KEY, checkpoint TEXT NOT NULL, failed_at INTEGER);
            CREATE TABLE local_thread_catalog_scan_entries (
                host_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                removed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(host_id,thread_id)) WITHOUT ROWID;
            INSERT INTO local_thread_catalog_sync_state VALUES ('local',123.5,1,44,1000);
            INSERT INTO local_thread_catalog_sync_state VALUES ('remote',321.5,1,55,2000);
            DELETE FROM local_thread_catalog WHERE host_id='local';
        """)


def state(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT * FROM local_thread_catalog_sync_state ORDER BY host_id").fetchall()


def test_native_full_rebuild_request_is_backed_up_scoped_and_not_reported_as_restored(storage):
    home, files = storage
    service.auto_repair("openai")
    desktop = _desktop(home)
    coordinator(desktop)
    original = files["user"].read_bytes()
    before = state(desktop)
    report = service.inspect()
    assert report["completion"]["catalogMissing"] == 1
    assert report["completion"]["catalogRecovery"] == "native_rebuild_available"
    result = service.repair(report["repairToken"])
    assert result["changed"] and result["completion"]["partial"]
    assert result["completion"]["catalogRecovery"] == "native_rebuild_pending"
    assert "不代表目录已恢复" in result["message"]
    assert state(desktop) == [(*before[0][:-1], None), before[1]]
    with sqlite3.connect(desktop) as db:
        assert db.execute("SELECT COUNT(*) FROM local_thread_catalog WHERE host_id='local'").fetchone()[0] == 0
    assert files["user"].read_bytes() == original
    assert service.repair_current_provider()["completion"]["catalogRecovery"] == "native_rebuild_pending"
    service.restore(result["backupId"])
    assert state(desktop) == before and files["user"].read_bytes() == original


@pytest.mark.parametrize("blocked", ["unknown_column", "checkpoint", "scan_entries", "trigger", "missing_state", "remote_duplicate"])
def test_unverified_coordinator_is_never_reset(storage, blocked):
    home, _files = storage
    desktop = _desktop(home)
    coordinator(desktop)
    with sqlite3.connect(desktop) as db:
        if blocked == "unknown_column":
            db.execute("ALTER TABLE local_thread_catalog_sync_state ADD COLUMN identity TEXT")
        elif blocked == "checkpoint":
            db.execute("INSERT INTO local_thread_catalog_scan_checkpoints VALUES ('local','existing checkpoint',NULL)")
        elif blocked == "scan_entries":
            db.execute("INSERT INTO local_thread_catalog_scan_entries VALUES ('local','unfinished-scan',0)")
        elif blocked == "trigger":
            db.execute("CREATE TRIGGER extra AFTER UPDATE ON local_thread_catalog_sync_state BEGIN SELECT 1; END")
        elif blocked == "missing_state":
            db.execute("DELETE FROM local_thread_catalog_sync_state WHERE host_id='local'")
        else:
            db.execute("UPDATE local_thread_catalog SET thread_id='user-root' WHERE host_id='remote'")
    before = state(desktop)
    result = service.repair_current_provider()
    assert result["completion"]["partial"]
    assert state(desktop) == before


def test_request_requires_stopped_and_restore_detects_native_scan_after_restart(storage, monkeypatch):
    home, _files = storage
    desktop = _desktop(home)
    coordinator(desktop)
    monkeypatch.setattr(core, "running_codex_processes", lambda: [{"pid":123}])
    with pytest.raises(core.ManagerError, match="停止"):
        service.repair_current_provider()
    assert state(desktop)[0][-1] == 1000
    monkeypatch.setattr(core, "running_codex_processes", lambda: [])
    result = service.repair_current_provider()
    with sqlite3.connect(desktop) as db:
        db.execute("UPDATE local_thread_catalog_sync_state SET last_full_reconciled_at=3000 WHERE host_id='local'")
    with pytest.raises(core.ManagerError, match="冲突"):
        service.restore_provider_repair(result)
    assert state(desktop)[0][-1] == 3000


def test_failed_rollout_write_restores_native_scan_request(storage, monkeypatch):
    home, _files = storage
    desktop = _desktop(home)
    coordinator(desktop)
    before = state(desktop)
    def fail(*args):
        raise OSError("synthetic write failure")
    monkeypatch.setattr(engine, "_apply_rollout_action", fail)
    with pytest.raises(core.ManagerError, match="回滚"):
        service.repair_current_provider()
    assert state(desktop) == before
