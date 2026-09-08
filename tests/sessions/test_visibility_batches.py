"""Switch protection regressions. All storage is synthetic and temporary."""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

import agent_manager.core as core
import agent_manager.sessions.visibility_engine as engine
import agent_manager.sessions.visibility as service
from tests.sessions.test_visibility_recovery import _fixture, _insert_thread, _sha, _thread, _write_rollout


@pytest.fixture
def storage(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    files = _fixture(home)
    monkeypatch.setattr(core, "CODEX_HOME", home)
    monkeypatch.setattr(core, "BACKUPS_DIR", home / "agent-manager" / "backups")
    monkeypatch.setattr(core, "running_codex_processes", lambda: [])
    return home, files


def _add(home, files, thread_id="second", **kwargs):
    rollout = _write_rollout(home, thread_id)
    with sqlite3.connect(files["db"]) as connection:
        _insert_thread(connection, thread_id, rollout.relative_to(home).as_posix(), **kwargs)
    return rollout


def _catalog(files):
    with sqlite3.connect(files["db"]) as connection:
        connection.executescript("""
        CREATE TABLE local_thread_catalog_hosts (host_id TEXT PRIMARY KEY, host_kind TEXT);
        INSERT INTO local_thread_catalog_hosts VALUES ('local', 'local');
        CREATE TABLE local_thread_catalog (
          host_id TEXT NOT NULL, thread_id TEXT NOT NULL, display_title TEXT NOT NULL,
          source_created_at REAL NOT NULL, source_updated_at REAL NOT NULL, cwd TEXT,
          source_kind TEXT NOT NULL, source_detail TEXT, model_provider TEXT, git_branch TEXT,
          observation_sequence INTEGER NOT NULL, missing_candidate INTEGER NOT NULL DEFAULT 0,
          thread_source TEXT, source_recency_at REAL NOT NULL DEFAULT 0,
          pending_observed_title INTEGER NOT NULL DEFAULT 0, project_id TEXT,
          PRIMARY KEY(host_id, thread_id));
        INSERT INTO local_thread_catalog(host_id,thread_id,display_title,source_created_at,
          source_updated_at,cwd,source_kind,model_provider,observation_sequence,thread_source)
          VALUES ('local','user-root','old title',1,2,'C:\\Workspace','cli','old-provider',1,'user');
        """)


@pytest.mark.parametrize("broken", ["missing", "invalid", "mismatch", "duplicate_meta", "child_meta"])
def test_unrelated_unverified_session_does_not_block_valid_root(storage, broken):
    home, files = storage
    second = _add(home, files)
    if broken == "missing":
        second.unlink()
    elif broken == "invalid":
        second.write_bytes(b"broken jsonl")
    elif broken == "mismatch":
        second.write_bytes(second.read_bytes().replace(b'"id":"second"', b'"id":"other"'))
    elif broken == "duplicate_meta":
        with second.open("ab") as stream:
            stream.write(second.read_bytes().splitlines(keepends=True)[0])
    else:
        second.write_bytes(second.read_bytes().replace(b'"source":"cli"', b'"source":"cli","parent_thread_id":"parent"'))
    before = second.read_bytes() if second.exists() else None
    result = service.auto_repair("relay")
    assert result["changed"] and _thread(files["db"], "user-root")[0] == "relay"
    assert _thread(files["db"], "second")[0] == "old-provider"
    assert (second.read_bytes() if second.exists() else None) == before
    if broken != "child_meta":
        assert result["status"] == "partial"
        assert result["completion"]["deferredSessions"] >= 1
        assert "部分" in result["message"]


def test_real_user_event_repairs_missing_preview_without_copying_injected_context(storage):
    home, files = storage
    rollout = _add(home, files, first_message="", provider="relay", has_user_event=0)
    content = rollout.read_bytes().replace(b'"model_provider":"old-provider"', b'"model_provider":"relay"')
    content += json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "actual user request"}}).encode() + b"\n"
    rollout.write_bytes(content)
    result = service.auto_repair("relay")
    with sqlite3.connect(files["db"]) as connection:
        row = connection.execute("SELECT first_user_message, preview, has_user_event, thread_source FROM threads WHERE id='second'").fetchone()
    assert row == ("actual user request", "actual user request", 1, "user")
    assert rollout.read_bytes() == content
    assert "actual user request" not in json.dumps(result)


def test_input_context_is_not_used_as_preview(storage):
    home, files = storage
    _add(home, files, first_message="")
    assert service.auto_repair("relay")["status"] == "partial"
    with sqlite3.connect(files["db"]) as connection:
        assert connection.execute("SELECT first_user_message, preview FROM threads WHERE id='second'").fetchone() == ("", "")


def test_two_bounded_passes_make_progress_without_rolling_back_first_batch(storage):
    home, files = storage
    _add(home, files, thread_id="user-second")
    first = engine.inspect_session_visibility(home, "relay", max_rollouts=1)
    result = engine.repair_session_visibility(first, confirm_codex_stopped=True)
    assert result["completion"]["partial"] and result["completion"]["remainingActions"]
    assert _thread(files["db"], "user-root")[0] == "relay"
    assert _thread(files["db"], "user-second")[0] == "old-provider"
    second = engine.inspect_session_visibility(home, "relay", max_rollouts=1)
    result = engine.repair_session_visibility(second, confirm_codex_stopped=True)
    assert _thread(files["db"], "user-second")[0] == "relay"
    assert not result["completion"]["partial"]


def test_repair_io_budget_is_local_and_reported(storage, monkeypatch):
    home, files = storage
    huge = _add(home, files, thread_id="huge")
    with huge.open("ab") as stream:
        stream.write(b"x" * 10_000)
    monkeypatch.setattr(engine, "DEFAULT_QUICK_REPAIR_BYTES", 2000)
    result = service.auto_repair("relay")
    assert result["status"] == "partial"
    assert _thread(files["db"], "user-root")[0] == "relay"
    assert _thread(files["db"], "huge")[0] == "old-provider"
    assert result["verification"]["scanBudget"]["plannedRepairBytes"] <= 2000


def test_full_validation_isolates_conflicting_metadata_outside_prefix(storage, monkeypatch):
    home, files = storage
    other = _add(home, files, thread_id="z-hidden")
    first_meta = other.read_bytes().splitlines(keepends=True)[0]
    with other.open("ab") as stream:
        stream.write(b'{}\n' * 300 + first_meta)
    monkeypatch.setattr(engine, "QUICK_PREFIX_BYTES_PER_ROLLOUT", 700)
    result = service.auto_repair("relay")
    assert result["changed"] and result["status"] == "partial"
    assert _thread(files["db"], "user-root")[0] == "relay"
    assert _thread(files["db"], "z-hidden")[0] == "old-provider"


def test_catalog_provider_only_repair_and_targeted_restore(storage):
    _, files = storage
    service.auto_repair("relay")
    _catalog(files)
    report = service.inspect()
    assert len(report["plan"]) == 1 and report["plan"][0]["table"] == "local_thread_catalog"
    result = service.repair(report["repairToken"])
    with sqlite3.connect(files["db"]) as connection:
        assert connection.execute("SELECT model_provider FROM local_thread_catalog").fetchone()[0] == "relay"
        connection.execute("UPDATE local_thread_catalog SET display_title='later title'")
    service.restore(result["backupId"])
    with sqlite3.connect(files["db"]) as connection:
        assert connection.execute("SELECT model_provider, display_title FROM local_thread_catalog").fetchone() == ("old-provider", "later title")
    assert _thread(files["db"], "user-root")[0] == "relay"


@pytest.mark.parametrize("conflict", ["remote_id", "subagent", "missing_candidate", "cwd", "two_hosts", "unknown_schema"])
def test_catalog_conflicts_fail_closed(storage, conflict):
    _, files = storage
    _catalog(files)
    with sqlite3.connect(files["db"]) as connection:
        if conflict == "remote_id":
            connection.execute("INSERT INTO local_thread_catalog SELECT 'remote',thread_id,display_title,source_created_at,source_updated_at,cwd,source_kind,source_detail,model_provider,git_branch,observation_sequence,missing_candidate,thread_source,source_recency_at,pending_observed_title,project_id FROM local_thread_catalog")
        elif conflict == "subagent":
            connection.execute("UPDATE local_thread_catalog SET source_kind='subagent'")
        elif conflict == "missing_candidate":
            connection.execute("UPDATE local_thread_catalog SET missing_candidate=1")
        elif conflict == "cwd":
            connection.execute("UPDATE local_thread_catalog SET cwd='C:\\Other'")
        elif conflict == "two_hosts":
            connection.execute("INSERT INTO local_thread_catalog_hosts VALUES ('other','local')")
        else:
            connection.execute("ALTER TABLE local_thread_catalog ADD COLUMN account_identity TEXT")
        before = connection.execute("SELECT * FROM local_thread_catalog ORDER BY host_id").fetchall()
    result = service.auto_repair("relay")
    assert result["status"] in {"partial", "deferred"}
    with sqlite3.connect(files["db"]) as connection:
        assert connection.execute("SELECT * FROM local_thread_catalog ORDER BY host_id").fetchall() == before


def test_missing_catalog_reports_deferred_without_insertion(storage):
    _, files = storage
    _catalog(files)
    with sqlite3.connect(files["db"]) as connection:
        connection.execute("DELETE FROM local_thread_catalog")
    result = service.auto_repair("relay")
    assert result["completion"]["catalogMissing"] == 1
    assert "缺少目录记录" in result["message"]
    again = service.auto_repair("relay")
    assert again["status"] == "deferred"
    with sqlite3.connect(files["db"]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_thread_catalog").fetchone()[0] == 0


def test_catalog_and_thread_update_rollback_after_rollout_failure(storage, monkeypatch):
    _, files = storage
    _catalog(files)
    original = _thread(files["db"], "user-root")
    original_rollout = files["user"].read_bytes()
    def fail(*_args):
        raise OSError("injected rollout write failure")
    monkeypatch.setattr(engine, "_apply_rollout_action", fail)
    result = service.auto_repair("relay")
    assert result["status"] == "skipped"
    assert _thread(files["db"], "user-root") == original
    assert files["user"].read_bytes() == original_rollout
    with sqlite3.connect(files["db"]) as connection:
        assert connection.execute("SELECT model_provider FROM local_thread_catalog").fetchone()[0] == "old-provider"


def test_official_api1_api2_official_round_trip_preserves_ids_and_bodies(storage):
    home, files = storage
    _catalog(files)
    rollouts = list((home / "sessions").rglob("*.jsonl")) + list((home / "archived_sessions").rglob("*.jsonl"))
    bodies = {path: hashlib.sha256(b"".join(path.read_bytes().splitlines(keepends=True)[1:])).hexdigest() for path in rollouts}
    protected = {name: _sha(files[name]) for name in ("child", "agent", "archived", "legacy")}
    with sqlite3.connect(files["db"]) as connection:
        ids = connection.execute("SELECT id, archived, archived_at FROM threads ORDER BY id").fetchall()
    for provider in ("openai", "api1", "api2", "openai"):
        result = service.auto_repair(provider)
        assert result["changed"] and result["status"] == "repaired"
        assert _thread(files["db"], "user-root")[0] == provider
        with sqlite3.connect(files["db"]) as connection:
            assert connection.execute("SELECT id, archived, archived_at FROM threads ORDER BY id").fetchall() == ids
            assert connection.execute("SELECT model_provider FROM local_thread_catalog").fetchone()[0] == provider
        assert {path: hashlib.sha256(b"".join(path.read_bytes().splitlines(keepends=True)[1:])).hexdigest() for path in rollouts} == bodies
        assert {name: _sha(files[name]) for name in protected} == protected


def test_targeted_postwrite_verification_failure_rolls_back(storage, monkeypatch):
    _, files = storage
    original = _thread(files["db"], "user-root")
    original_rollout = files["user"].read_bytes()
    def fail(*_args):
        raise engine.SessionVisibilityError("injected verification failure")
    monkeypatch.setattr(engine, "_verify_applied_actions", fail)
    assert service.auto_repair("relay")["status"] == "skipped"
    assert _thread(files["db"], "user-root") == original
    assert files["user"].read_bytes() == original_rollout


def test_absent_catalog_is_supported_without_missing_or_partial_report(storage):
    _, files = storage
    result = service.auto_repair("relay")
    assert result["status"] == "repaired"
    assert result["completion"]["catalogMissing"] == 0
    with sqlite3.connect(files["db"]) as connection:
        assert connection.execute("PRAGMA table_info(local_thread_catalog)").fetchall() == []


def test_catalog_restore_rejects_changed_identity_before_any_inverse_write(storage):
    _, files = storage
    _catalog(files)
    result = service.auto_repair("relay")
    before_rollout = files["user"].read_bytes()
    with sqlite3.connect(files["db"]) as connection:
        connection.execute("UPDATE local_thread_catalog SET source_kind='subagent'")
    with pytest.raises(core.ManagerError, match="恢复冲突"):
        service.restore(result["backupId"])
    assert _thread(files["db"], "user-root")[0] == "relay"
    assert files["user"].read_bytes() == before_rollout


def test_sqlite_only_changes_also_require_full_rollout_identity(storage, monkeypatch):
    _, files = storage
    # The provider is already correct; only the missing preview/flags need SQL.
    content = files["user"].read_bytes().replace(b'"old-provider"', b'"relay"')
    first_meta = content.splitlines(keepends=True)[0]
    files["user"].write_bytes(content + b'{}\n' * 300 + first_meta)
    with sqlite3.connect(files["db"]) as connection:
        connection.execute("UPDATE threads SET model_provider='relay' WHERE id='user-root'")
    before = _thread(files["db"], "user-root")
    monkeypatch.setattr(engine, "QUICK_PREFIX_BYTES_PER_ROLLOUT", 700)
    result = service.auto_repair("relay")
    assert result["status"] == "deferred" and not result["changed"]
    assert result["backupId"] is None
    assert _thread(files["db"], "user-root") == before
