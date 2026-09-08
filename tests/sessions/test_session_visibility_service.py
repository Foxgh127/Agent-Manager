from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

import agent_manager.core as core
import agent_manager.sessions.visibility_engine as engine
import agent_manager.sessions.visibility as service
from tests.sessions.test_visibility_recovery import _fixture, _sha, _thread


@pytest.fixture
def storage(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    files = _fixture(home)
    monkeypatch.setattr(core, "CODEX_HOME", home)
    monkeypatch.setattr(core, "BACKUPS_DIR", home / "agent-manager" / "backups")
    monkeypatch.setattr(core, "running_codex_processes", lambda: [])
    return home, files


def test_lazy_core_import():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import agent_manager.sessions.visibility; assert 'agent_manager.core' not in sys.modules"],
        cwd=Path(__file__).parent, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("mode", ["quick", "deep"])
def test_inspection_is_read_only_and_never_exposes_conversation(storage, mode):
    home, files = storage
    before = {name: _sha(path) for name, path in files.items()}
    report = service.inspect(mode)
    assert report["currentProvider"] == report["targetProvider"] == "relay"
    assert report["counts"]["sqliteRows"] == 1
    assert len(report["repairToken"]) == 64
    assert "preview" in next(item["fields"] for item in report["plan"] if item["kind"] == "sqlite_update")
    encoded = json.dumps(report)
    for sensitive in ("hello", "keep this exact body", "opaque-ciphertext-123", "Workspace", '"set":', '"expected":', '"payload":'):
        assert sensitive not in encoded
    assert {name: _sha(path) for name, path in files.items()} == before
    assert not core.BACKUPS_DIR.exists()


def test_missing_model_provider_and_missing_config_default_to_openai(storage):
    home, _ = storage
    (home / "config.toml").write_text('model = "test"\n', encoding="utf-8")
    assert service.inspect()["currentProvider"] == "openai"
    (home / "config.toml").unlink()
    assert service.inspect()["currentProvider"] == "openai"


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-16-be"])
def test_configuration_uses_core_supported_encoding(storage, encoding):
    home, _ = storage
    content = 'model_provider = "encoded-provider"\n'.encode(encoding)
    if encoding == "utf-16-be":
        content = b"\xfe\xff" + content
    (home / "config.toml").write_bytes(content)
    report = service.inspect()
    assert report["currentProvider"] == "encoded-provider"
    assert service.repair(report["repairToken"])["changed"]


def test_repair_restore_round_trip_and_preserve_unrelated_writes(storage):
    home, files = storage
    original = _thread(files["db"], "user-root")
    rollout = files["user"].read_bytes()
    inspected = service.inspect(session_ids=["user-root"])
    result = service.repair(inspected["repairToken"], session_ids=["user-root"])
    assert result["changed"] and result["backupId"]
    assert "backupDir" not in result and "hello" not in json.dumps(result)
    backup = core.BACKUPS_DIR / "session-visibility" / result["backupId"]
    assert (backup / "manifest.json").is_file()
    assert _thread(files["db"], "user-root")[0] == "relay"
    assert files["user"].read_bytes().splitlines()[1:] == rollout.splitlines()[1:]
    with sqlite3.connect(files["db"]) as connection:
        connection.execute("UPDATE threads SET title='new title' WHERE id='user-root'")
    restored = service.restore(result["backupId"])
    assert restored["restored"] and restored["preservedUnrelatedChanges"]
    assert _thread(files["db"], "user-root") == original
    assert files["user"].read_bytes() == rollout
    with sqlite3.connect(files["db"]) as connection:
        assert connection.execute("SELECT title FROM threads WHERE id='user-root'").fetchone()[0] == "new title"


@pytest.mark.parametrize("change", ["config", "database", "scope", "mode"])
def test_stale_and_scope_tokens_are_rejected_before_backup(storage, change):
    home, files = storage
    report = service.inspect()
    kwargs = {}
    if change == "config":
        # Same provider but changed config content still invalidates approval.
        (home / "config.toml").write_text('model_provider = "relay"\n# changed\n', encoding="utf-8")
    elif change == "database":
        with sqlite3.connect(files["db"]) as connection:
            connection.execute("UPDATE threads SET first_user_message='different content' WHERE id='user-root'")
    elif change == "scope":
        kwargs["session_ids"] = ["user-root"]
    else:
        kwargs["mode"] = "deep"
    with pytest.raises(core.ManagerError, match="过期|范围"):
        service.repair(report["repairToken"], **kwargs)
    assert not core.BACKUPS_DIR.exists()


@pytest.mark.parametrize("unknown", [False, True])
def test_mutations_require_authoritative_stopped_process_scan(storage, monkeypatch, unknown):
    _, files = storage
    report = service.inspect()
    repaired = service.repair(report["repairToken"])
    monkeypatch.setattr(core, "running_codex_processes", lambda: core.CodexProcessScan(known=False, error="scan failed") if unknown else [{"pid": 123}])
    before = _sha(files["db"])
    with pytest.raises(core.ManagerError, match="停止|可靠"):
        service.repair(service.inspect()["repairToken"])
    with pytest.raises(core.ManagerError, match="停止|可靠"):
        service.restore(repaired["backupId"])
    assert service.auto_repair("other")["status"] == "skipped"
    assert _sha(files["db"]) == before


@pytest.mark.parametrize("lock_name", ["SWITCH_OPERATION_LOCK", "CONFIG_FILE_LOCK"])
def test_concurrent_operations_fail_closed_without_waiting(storage, lock_name):
    report = service.inspect()
    entered = threading.Event()
    release = threading.Event()
    def hold():
        with getattr(core, lock_name):
            entered.set()
            assert release.wait(10)
    worker = threading.Thread(target=hold)
    worker.start()
    assert entered.wait(5)
    try:
        with pytest.raises(core.ManagerError, match="正在执行"):
            service.repair(report["repairToken"])
        assert service.auto_repair("relay")["status"] == "skipped"
    finally:
        release.set()
        worker.join(5)
    assert not core.BACKUPS_DIR.exists()
    assert service.inspect()["repairToken"] == report["repairToken"]


def test_auto_repair_is_reentrant_with_switch_and_uses_requested_target(storage):
    _, files = storage
    with core.SWITCH_OPERATION_LOCK, core.CONFIG_FILE_LOCK:
        result = service.auto_repair("next-provider")
    assert result["changed"]
    assert result["verification"]["scanMode"] == "quick"
    assert result["verification"]["targetProvider"] == "next-provider"
    assert result["verification"]["currentProvider"] == "relay"
    assert _thread(files["db"], "user-root")[0] == "next-provider"
    assert service.restore(result["backupId"])["restored"]


def test_auto_repair_no_history_and_ambiguous_schema_are_nonblocking(storage):
    home, files = storage
    for path in (files["db"], files["legacy"]):
        path.unlink()
    result = service.auto_repair("relay")
    assert result["reason"] == "no_history"
    with sqlite3.connect(home / "state_99.sqlite") as connection:
        connection.execute("CREATE TABLE unexpected (value TEXT)")
    assert service.auto_repair("relay")["reason"] == "ambiguous"
    assert not core.BACKUPS_DIR.exists()


@pytest.mark.parametrize("backup_id", ["../elsewhere", "C:\\elsewhere", "/tmp/elsewhere", "visibility-20260908-010101-123456-123456abcdef/../x", "", None])
def test_restore_accepts_ids_only(storage, backup_id):
    with pytest.raises(core.ManagerError, match="备份 ID"):
        service.restore(backup_id)


def test_restore_conflict_is_rejected_without_partial_rollout_restore(storage):
    _, files = storage
    result = service.auto_repair("relay")
    with sqlite3.connect(files["db"]) as connection:
        connection.execute("UPDATE threads SET model_provider='external' WHERE id='user-root'")
    rollout = _sha(files["user"])
    with pytest.raises(core.ManagerError):
        service.restore(result["backupId"])
    assert _sha(files["user"]) == rollout
    assert _thread(files["db"], "user-root")[0] == "external"


def test_restore_rejects_other_instance_manifest(storage, tmp_path):
    result = service.auto_repair("relay")
    manifest_path = core.BACKUPS_DIR / "session-visibility" / result["backupId"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["home"] = str(tmp_path / "other-home")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(core.ManagerError, match="不属于"):
        service.restore(result["backupId"])


@pytest.mark.parametrize("change", ["nondict", "protected_field", "missing_expected", "outside_backup"])
def test_malformed_manifest_cannot_bypass_restore_boundary(storage, change):
    _, files = storage
    result = service.auto_repair("relay")
    manifest_path = core.BACKUPS_DIR / "session-visibility" / result["backupId"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    action = next(item for item in manifest["actions"] if item["kind"] == "sqlite_update")
    if change == "nondict":
        manifest["actions"] = [None]
    elif change == "protected_field":
        action["expected"]["archived"] = 0
        action["set"]["archived"] = 1
    elif change == "missing_expected":
        del action["expected"]
    else:
        manifest["files"][0]["backupPath"] = "../../outside.sqlite"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = _sha(files["db"]), _sha(files["user"])
    with pytest.raises(core.ManagerError):
        service.restore(result["backupId"])
    assert before == (_sha(files["db"]), _sha(files["user"]))


def _directory_link(link, target):
    if os.name == "nt":
        # mklink is used only to create a fixture junction, never to delete/move.
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        assert result.returncode == 0, result.stderr
    else:
        link.symlink_to(target, target_is_directory=True)


def test_backup_root_junction_is_rejected(storage, tmp_path):
    _, files = storage
    outside = tmp_path / "outside"
    outside.mkdir()
    core.BACKUPS_DIR.mkdir(parents=True)
    link = core.BACKUPS_DIR / "session-visibility"
    _directory_link(link, outside)
    try:
        before = _sha(files["db"])
        with pytest.raises(core.ManagerError, match="联接|链接"):
            service.repair(service.inspect()["repairToken"])
        assert _sha(files["db"]) == before and list(outside.iterdir()) == []
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_backup_id_junction_is_rejected(storage, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = core.BACKUPS_DIR / "session-visibility"
    parent.mkdir(parents=True)
    backup_id = "visibility-20260908-010101-123456-123456abcdef"
    link = parent / backup_id
    _directory_link(link, outside)
    try:
        with pytest.raises(core.ManagerError, match="联接|链接"):
            service.restore(backup_id)
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_list_backups_is_read_only_bounded_and_excludes_invalid_entries(storage, tmp_path):
    assert service.list_backups() == {"backups": [], "truncated": False}
    assert not core.BACKUPS_DIR.exists()
    result = service.auto_repair("relay")
    listed = service.list_backups()
    assert len(listed["backups"]) == 1
    assert listed["backups"][0]["backupId"] == result["backupId"]
    assert set(listed["backups"][0]) == {"backupId", "createdAt"}
    assert "hello" not in json.dumps(listed)
    parent = core.BACKUPS_DIR / "session-visibility"
    wrong = parent / "visibility-20260908-010101-123456-123456abcdef"
    wrong.mkdir()
    (wrong / "manifest.json").write_text(json.dumps({"schemaVersion": engine.SCHEMA_VERSION,
        "home": str(tmp_path / "outside"), "status": "applied", "createdAt": "2026-09-08T01:01:01Z"}), encoding="utf-8")
    broken = parent / "visibility-20260908-010101-123457-123456abcdef"
    broken.mkdir()
    (broken / "manifest.json").write_text("not-json", encoding="utf-8")
    assert service.list_backups() == listed


def test_list_backups_skips_junction_entries(storage, tmp_path):
    parent = core.BACKUPS_DIR / "session-visibility"
    parent.mkdir(parents=True)
    target = tmp_path / "outside"
    target.mkdir()
    link = parent / "visibility-20260908-010101-123456-123456abcdef"
    _directory_link(link, target)
    try:
        assert service.list_backups() == {"backups": [], "truncated": False}
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_busy_database_never_creates_backup(storage):
    _, files = storage
    report = service.inspect()
    with sqlite3.connect(files["db"]) as connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(core.ManagerError):
            service.repair(report["repairToken"])
    assert not core.BACKUPS_DIR.exists()


def test_engine_failure_rolls_back_and_is_manager_error(storage, monkeypatch):
    _, files = storage
    original = _thread(files["db"], "user-root")
    def fail(*args, **kwargs):
        raise engine.SessionVisibilityError("injected failure")
    monkeypatch.setattr(engine, "_apply_rollout_action", fail)
    with pytest.raises(core.ManagerError, match="自动回滚"):
        service.repair(service.inspect()["repairToken"])
    assert _thread(files["db"], "user-root") == original
