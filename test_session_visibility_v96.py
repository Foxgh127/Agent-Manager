from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

import codex_session_visibility as visibility


THREAD_SCHEMA = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT,
    created_at INTEGER,
    updated_at INTEGER,
    source TEXT,
    model_provider TEXT,
    cwd TEXT,
    title TEXT,
    has_user_event INTEGER,
    archived INTEGER,
    archived_at INTEGER,
    first_user_message TEXT,
    thread_source TEXT,
    preview TEXT,
    originator TEXT,
    agent_nickname TEXT,
    agent_role TEXT,
    agent_path TEXT
);
CREATE TABLE thread_spawn_edges (
    parent_thread_id TEXT NOT NULL,
    child_thread_id TEXT NOT NULL PRIMARY KEY,
    status TEXT NOT NULL
);
"""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_rollout(
    home: Path,
    thread_id: str,
    *,
    provider: str = "old-provider",
    archived: bool = False,
    source="cli",
    user_message: bool = True,
    encrypted: bool = False,
) -> Path:
    bucket = "archived_sessions" if archived else "sessions"
    directory = home / bucket / "2026" / "09" / "07"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-{thread_id}.jsonl"
    records = [
        {
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "model_provider": provider,
                "cwd": "\\\\?\\C:\\Workspace\\",
                "source": source,
            },
        }
    ]
    if user_message:
        payload = {"type": "message", "role": "user", "content": "keep this exact body"}
        if encrypted:
            payload["encrypted_content"] = "opaque-ciphertext-123"
        records.append({"type": "response_item", "payload": payload})
    records.append(
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": "answer"},
        }
    )
    path.write_bytes(
        b"".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\r\n"
            for record in records
        )
    )
    return path


def _insert_thread(
    connection: sqlite3.Connection,
    thread_id: str,
    rollout_path: str,
    *,
    provider: str = "old-provider",
    archived: int = 0,
    source: str = "cli",
    first_message: str = "hello",
    has_user_event: int = 0,
    thread_source: str | None = None,
    preview: str = "",
    agent_path: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO threads (
            id, rollout_path, created_at, updated_at, source, model_provider,
            cwd, title, has_user_event, archived, archived_at,
            first_user_message, thread_source, preview, originator,
            agent_nickname, agent_role, agent_path
        ) VALUES (?, ?, 1, 2, ?, ?, ?, '', ?, ?, NULL, ?, ?, ?, NULL, NULL, NULL, ?)
        """,
        (
            thread_id,
            rollout_path,
            source,
            provider,
            "\\\\?\\C:\\Workspace\\",
            has_user_event,
            archived,
            first_message,
            thread_source,
            preview,
            agent_path,
        ),
    )


def _fixture(home: Path) -> dict[str, Path]:
    home.mkdir(parents=True)
    (home / "sqlite").mkdir()
    (home / "config.toml").write_text('model_provider = "relay"\n', encoding="utf-8")

    user_rollout = _write_rollout(home, "user-root", encrypted=True)
    child_rollout = _write_rollout(home, "child-agent", source={"subagent": {}})
    agent_rollout = _write_rollout(home, "agent-column")
    archived_rollout = _write_rollout(home, "archived-root", archived=True)

    db = home / "sqlite" / "state_12.sqlite"
    connection = sqlite3.connect(db)
    connection.executescript(THREAD_SCHEMA)
    _insert_thread(connection, "user-root", user_rollout.relative_to(home).as_posix())
    _insert_thread(connection, "child-agent", child_rollout.relative_to(home).as_posix())
    _insert_thread(
        connection,
        "agent-column",
        agent_rollout.relative_to(home).as_posix(),
        agent_path="/root/reviewer",
    )
    _insert_thread(
        connection,
        "archived-root",
        archived_rollout.relative_to(home).as_posix(),
        archived=1,
    )
    connection.execute(
        "INSERT INTO thread_spawn_edges VALUES ('user-root', 'child-agent', 'running')"
    )
    connection.commit()
    connection.close()

    # A valid but superseded legacy database must remain diagnostic-only.
    legacy = home / "state_5.sqlite"
    connection = sqlite3.connect(legacy)
    connection.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, first_user_message TEXT)"
    )
    connection.execute("INSERT INTO threads VALUES ('legacy', 'old-provider', 'legacy user')")
    connection.commit()
    connection.close()
    return {
        "db": db,
        "legacy": legacy,
        "user": user_rollout,
        "child": child_rollout,
        "agent": agent_rollout,
        "archived": archived_rollout,
    }


def _thread(db: Path, thread_id: str):
    connection = sqlite3.connect(db)
    try:
        return connection.execute(
            "SELECT model_provider, has_user_event, thread_source, preview, cwd, archived "
            "FROM threads WHERE id = ?",
            [thread_id],
        ).fetchone()
    finally:
        connection.close()


def test_inspection_is_read_only_selects_newest_real_schema_and_excludes_non_user_threads(tmp_path):
    home = tmp_path / "codex"
    paths = _fixture(home)
    before = {name: (_sha(path), path.stat().st_mtime_ns) for name, path in paths.items()}

    report = visibility.inspect_session_visibility(home, "relay")

    assert report["safeToRepair"] is True
    assert report["counts"] == {
        "sqliteRows": 1,
        "rolloutFiles": 1,
        "subagentsExcluded": 2,
        "archivedExcluded": 1,
    }
    assert report["scanMode"] == "quick"
    assert report["rollouts"] == {
        "scanned": 1,
        "scope": "repair_candidates",
        "active": 1,
        "archived": 0,
        "invalid": 0,
        "encrypted": 1,
        "encryptedScanComplete": False,
    }
    assert report["scanBudget"]["candidateRollouts"] == 1
    selected = [item for item in report["databases"] if item["selected"]]
    assert [item["path"] for item in selected] == ["sqlite/state_12.sqlite"]
    assert any(
        item["kind"] == "superseded_database" and item["path"] == "state_5.sqlite"
        for item in report["issues"]
    )
    sqlite_action = next(item for item in report["actions"] if item["kind"] == "sqlite_update")
    assert sqlite_action["threadId"] == "user-root"
    assert sqlite_action["set"] == {
        "model_provider": "relay",
        "has_user_event": 1,
        "thread_source": "user",
        "preview": "hello",
        "cwd": "C:\\Workspace",
    }
    assert {item["threadId"] for item in report["excluded"] if item["reason"] == "non_root_agent"} == {
        "child-agent",
        "agent-column",
    }
    assert report["encryptedContentWarning"]
    assert not (home / visibility.DEFAULT_BACKUP_DIR).exists()
    assert before == {name: (_sha(path), path.stat().st_mtime_ns) for name, path in paths.items()}


def test_repair_is_scoped_preserves_body_and_encrypted_content_and_restore_is_exact(tmp_path):
    home = tmp_path / "codex"
    paths = _fixture(home)
    original_user = paths["user"].read_bytes()
    original_child = paths["child"].read_bytes()
    original_archived = paths["archived"].read_bytes()
    original_legacy = _sha(paths["legacy"])
    original_db_mtime = paths["db"].stat().st_mtime_ns
    original_rollout_mtime = paths["user"].stat().st_mtime_ns
    report = visibility.inspect_session_visibility(home, "relay")

    result = visibility.repair_session_visibility(
        report,
        confirm_codex_stopped=True,
        backup_parent=tmp_path / "backups",
    )

    assert result["changed"] is True
    assert result["sqliteRowsUpdated"] == 1
    assert result["rolloutFilesUpdated"] == 1
    assert result["verification"]["actions"] == []
    assert _thread(paths["db"], "user-root") == (
        "relay",
        1,
        "user",
        "hello",
        "C:\\Workspace",
        0,
    )
    assert _thread(paths["db"], "child-agent")[:3] == ("old-provider", 0, None)
    assert _thread(paths["db"], "archived-root")[0] == "old-provider"
    updated_user = paths["user"].read_bytes()
    assert json.loads(updated_user.splitlines()[0])["payload"]["model_provider"] == "relay"
    assert updated_user.split(b"\r\n", 1)[1] == original_user.split(b"\r\n", 1)[1]
    assert b'"encrypted_content":"opaque-ciphertext-123"' in updated_user
    assert paths["db"].stat().st_mtime_ns == original_db_mtime
    assert paths["user"].stat().st_mtime_ns == original_rollout_mtime
    assert paths["child"].read_bytes() == original_child
    assert paths["archived"].read_bytes() == original_archived
    assert _sha(paths["legacy"]) == original_legacy
    manifest = json.loads(
        (Path(result["backupDir"]) / "manifest.json").read_text(encoding="utf-8")
    )
    rollout_action = next(
        item for item in manifest["actions"] if item["kind"] == "rollout_meta_update"
    )
    assert len(rollout_action["expectedFileSha256"]) == 64

    # Simulate a short-lived new Codex launch before the surrounding account
    # transaction decides to roll back.  Unrelated DB updates, a newly created
    # thread, and newly appended rollout bytes must survive targeted restore.
    connection = sqlite3.connect(paths["db"])
    connection.execute("UPDATE threads SET updated_at = 999 WHERE id = 'user-root'")
    _insert_thread(
        connection,
        "post-launch-thread",
        "sessions/new.jsonl",
        provider="relay",
        first_message="new session",
        has_user_event=1,
        thread_source="user",
    )
    connection.commit()
    connection.close()
    db_mtime_before_restore = paths["db"].stat().st_mtime_ns
    appended = b'{"type":"event_msg","payload":{"type":"token_count"}}\r\n'
    paths["user"].write_bytes(updated_user + appended)
    new_rollout_mtime = paths["user"].stat().st_mtime_ns + 10_000_000
    os.utime(paths["user"], ns=(paths["user"].stat().st_atime_ns, new_rollout_mtime))

    restored = visibility.restore_session_visibility(
        result["backupDir"],
        confirm_codex_stopped=True,
        expected_codex_home=home,
    )
    assert restored["restored"] is True
    assert restored["restoreMode"] == "targeted_inverse_patch"
    assert restored["preservedUnrelatedChanges"] is True
    assert paths["user"].read_bytes() == original_user + appended
    assert paths["user"].stat().st_mtime_ns == new_rollout_mtime
    assert paths["db"].stat().st_mtime_ns == db_mtime_before_restore
    assert _thread(paths["db"], "user-root") == (
        "old-provider",
        0,
        None,
        "",
        "\\\\?\\C:\\Workspace\\",
        0,
    )
    connection = sqlite3.connect(paths["db"])
    assert connection.execute("SELECT updated_at FROM threads WHERE id = 'user-root'").fetchone() == (
        999,
    )
    assert connection.execute(
        "SELECT model_provider FROM threads WHERE id = 'post-launch-thread'"
    ).fetchone() == ("relay",)
    connection.close()
    after_restore = visibility.inspect_session_visibility(
        home, "relay", session_ids=["user-root"]
    )
    assert any(
        item["kind"] == "sqlite_update" and item["threadId"] == "user-root"
        for item in after_restore["actions"]
    )


def test_repair_requires_stopped_instance_and_rejects_stale_plan_before_backup(tmp_path):
    home = tmp_path / "codex"
    paths = _fixture(home)
    report = visibility.inspect_session_visibility(home, "relay", mode="deep")
    with pytest.raises(visibility.SessionVisibilityError, match="停止"):
        visibility.repair_session_visibility(report, backup_parent=tmp_path / "backups")

    paths["user"].write_bytes(paths["user"].read_bytes() + b'{"type":"event_msg"}\n')
    with pytest.raises(visibility.SessionVisibilityError, match="过期"):
        visibility.repair_session_visibility(
            report,
            confirm_codex_stopped=True,
            backup_parent=tmp_path / "backups",
        )
    assert not (tmp_path / "backups").exists()


def test_busy_database_is_rejected_before_backup(tmp_path):
    home = tmp_path / "codex"
    paths = _fixture(home)
    lock = sqlite3.connect(paths["db"], timeout=0, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    try:
        report = visibility.inspect_session_visibility(home, "relay")
        with pytest.raises(visibility.SessionVisibilityError, match="正在被使用"):
            visibility.repair_session_visibility(
                report,
                confirm_codex_stopped=True,
                backup_parent=tmp_path / "backups",
            )
        assert not (tmp_path / "backups").exists()
    finally:
        lock.rollback()
        lock.close()


def test_failure_after_database_commit_automatically_restores_everything(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    paths = _fixture(home)
    original_row = _thread(paths["db"], "user-root")
    original_rollout = paths["user"].read_bytes()
    report = visibility.inspect_session_visibility(home, "relay")

    def fail_rollout(*_args, **_kwargs):
        raise OSError("injected rollout write failure")

    monkeypatch.setattr(visibility, "_apply_rollout_action", fail_rollout)
    with pytest.raises(visibility.SessionVisibilityError, match="已自动回滚"):
        visibility.repair_session_visibility(
            report,
            confirm_codex_stopped=True,
            backup_parent=tmp_path / "backups",
        )
    assert _thread(paths["db"], "user-root") == original_row
    assert paths["user"].read_bytes() == original_rollout
    manifests = list((tmp_path / "backups").glob("*/manifest.json"))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text(encoding="utf-8"))["status"] == "restored"


def test_restore_conflict_is_detected_before_any_inverse_write(tmp_path):
    home = tmp_path / "codex"
    paths = _fixture(home)
    report = visibility.inspect_session_visibility(home, "relay")
    result = visibility.repair_session_visibility(
        report,
        confirm_codex_stopped=True,
        backup_parent=tmp_path / "backups",
    )
    connection = sqlite3.connect(paths["db"])
    connection.execute(
        "UPDATE threads SET model_provider = 'later-provider' WHERE id = 'user-root'"
    )
    connection.commit()
    connection.close()
    rollout_before = paths["user"].read_bytes()

    with pytest.raises(visibility.SessionVisibilityError, match="恢复冲突"):
        visibility.restore_session_visibility(
            result["backupDir"],
            confirm_codex_stopped=True,
            expected_codex_home=home,
        )
    assert _thread(paths["db"], "user-root")[0] == "later-provider"
    assert paths["user"].read_bytes() == rollout_before


def test_future_schema_alias_and_archived_opt_in_never_changes_archive_state(tmp_path):
    home = tmp_path / "codex"
    (home / "sqlite").mkdir(parents=True)
    rollout = _write_rollout(home, "future-thread", archived=True)
    db = home / "sqlite" / "state_99.sqlite"
    connection = sqlite3.connect(db)
    connection.execute(
        """
        CREATE TABLE threads (
            thread_id TEXT PRIMARY KEY,
            rollout_path TEXT,
            model_provider TEXT,
            working_directory TEXT,
            archived_at INTEGER,
            first_user_message TEXT,
            has_user_event INTEGER,
            thread_source TEXT,
            preview TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "future-thread",
            rollout.relative_to(home).as_posix(),
            "old-provider",
            "\\\\?\\C:\\Future\\",
            123,
            "future user",
            0,
            None,
            "",
        ),
    )
    connection.commit()
    connection.close()

    default_report = visibility.inspect_session_visibility(home, "relay")
    assert default_report["actions"] == []
    report = visibility.inspect_session_visibility(home, "relay", include_archived=True)
    assert report["counts"] == {
        "sqliteRows": 1,
        "rolloutFiles": 1,
        "subagentsExcluded": 0,
        "archivedExcluded": 0,
    }
    result = visibility.repair_session_visibility(
        report,
        confirm_codex_stopped=True,
        backup_parent=tmp_path / "backups",
    )
    assert result["changed"] is True
    connection = sqlite3.connect(db)
    row = connection.execute(
        "SELECT model_provider, working_directory, archived_at FROM threads"
    ).fetchone()
    connection.close()
    assert row == ("relay", "C:\\Future", 123)


def test_invalid_and_outside_rollouts_are_diagnostic_only(tmp_path):
    home = tmp_path / "codex"
    (home / "sqlite").mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_text(
        '{"type":"session_meta","payload":{"id":"outside","model_provider":"old-provider"}}\n',
        encoding="utf-8",
    )
    bad_dir = home / "sessions"
    bad_dir.mkdir()
    (bad_dir / "rollout-bad.jsonl").write_bytes(b"\xff\xfe\x00binary")
    db = home / "sqlite" / "state_6.sqlite"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, model_provider TEXT, first_user_message TEXT)"
    )
    connection.execute(
        "INSERT INTO threads VALUES ('outside', ?, 'old-provider', 'user')", [str(outside)]
    )
    connection.commit()
    connection.close()

    report = visibility.inspect_session_visibility(home, "relay", mode="deep")
    # A first message alone cannot prove that a legacy/provider-only row is a
    # root conversation.  It remains diagnostic-only when source evidence and
    # an in-root rollout are both absent.
    assert report["actions"] == []
    assert any(
        item["threadId"] == "outside" and item["reason"] == "ambiguous_root_identity"
        for item in report["excluded"]
    )
    assert any("超出实例目录" in str(item.get("detail")) for item in report["issues"])
    assert report["rollouts"]["invalid"] == 1
    assert outside.read_text(encoding="utf-8").find("old-provider") >= 0


def test_unusable_higher_state_version_blocks_fallback_writes(tmp_path):
    home = tmp_path / "codex"
    _fixture(home)
    (home / "sqlite" / "state_100.sqlite").write_bytes(b"not a sqlite database")

    report = visibility.inspect_session_visibility(home, "relay")

    assert report["safeToRepair"] is False
    assert report["actions"] == []
    assert not any(item["selected"] for item in report["databases"])
    assert any(
        item["path"] == "sqlite/state_100.sqlite" and item["kind"] == "database_unusable"
        for item in report["issues"]
    )


def test_quick_mode_does_not_walk_unreferenced_history(tmp_path):
    home = tmp_path / "codex"
    _fixture(home)
    unrelated = home / "sessions" / "2020" / "01" / "01"
    unrelated.mkdir(parents=True)
    for index in range(20):
        (unrelated / f"rollout-unrelated-{index}.jsonl").write_bytes(
            b'{"type":"session_meta","payload":{"id":"unrelated"}}\n'
            + b"x" * 128_000
        )

    report = visibility.inspect_session_visibility(home, "relay", mode="quick")

    assert report["safeToRepair"] is True
    assert report["scanBudget"]["candidateRollouts"] == 1
    assert report["scanBudget"]["scannedRollouts"] == 1
    assert report["scanBudget"]["scannedBytes"] < 20_000
    action = next(item for item in report["actions"] if item["kind"] == "rollout_meta_update")
    assert action["expectedFileSha256"] is None


def test_quick_budget_exhaustion_preserves_verified_partial_actions(tmp_path):
    home = tmp_path / "codex"
    paths = _fixture(home)
    second = _write_rollout(home, "user-second", encrypted=False)
    connection = sqlite3.connect(paths["db"])
    _insert_thread(connection, "user-second", second.relative_to(home).as_posix())
    connection.commit()
    connection.close()

    report = visibility.inspect_session_visibility(
        home,
        "relay",
        mode="quick",
        max_rollouts=1,
        max_scan_bytes=4096,
    )

    assert report["safeToRepair"] is True
    assert report["actions"]
    assert report["blockedActionCount"] > 0
    assert report["scanBudget"]["candidateRollouts"] == 2
    assert report["scanBudget"]["truncated"] is True
    assert any(item["kind"] == "quick_rollout_count_budget" for item in report["issues"])
    result = visibility.repair_session_visibility(
        report, confirm_codex_stopped=True, backup_parent=tmp_path / "backups",
    )
    assert result["changed"] and result["completion"]["partial"]
    assert _thread(paths["db"], "user-root")[0] == "relay"
    assert _thread(paths["db"], "user-second")[0] == "old-provider"
    assert result["verification"]["actions"]
