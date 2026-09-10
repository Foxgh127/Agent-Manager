"""All cleanup I/O is confined to temporary fixtures, including real locks."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from agent_manager.updates import cleanup
from agent_manager.updates.service import AppUpdateService, _atomic_json


def receipt_fixture(tmp_path, number=1, *, completed=True):
    root = tmp_path / "用户 & '资料' [一]" / "app-update-install"
    stage = root / f"{number:048x}"
    stage.mkdir(parents=True)
    install = tmp_path / "安装 & '程序' [二]"
    install.mkdir(exist_ok=True)
    target = install / f"管理器-{number}.exe"
    target.write_bytes(b"verified current executable")
    staged = stage / "AgentManager.exe"
    staged.write_bytes(target.read_bytes())
    download = tmp_path / "downloads" / f"{number}-AgentManager.exe"
    download.parent.mkdir(exist_ok=True)
    download.write_bytes(target.read_bytes())
    spec = {"schemaVersion": 1, "installId": stage.name, "target": str(target), "source": str(staged),
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "size": target.stat().st_size,
            "originalSha256": hashlib.sha256(b"old executable").hexdigest(), "originalSize": len(b"old executable")}
    cleanup.prepare_cleanup(stage, spec, download)
    Path(spec["backup"]).write_bytes(b"old executable")
    _atomic_json(stage / "install.json", spec)
    _atomic_json(stage / "result.json", {"installId": stage.name, "state": "complete" if completed else "installed"})
    (stage / "install.ps1").write_text("# temporary test script", encoding="utf-8")
    if completed:
        assert cleanup.confirm_cleanup(stage, spec, {"ready": True, "sha256": spec["sha256"],
                                                    "verifiedAt": f"2026-09-{number:02d}T00:00:00Z"})
    return root, stage, spec, download


def test_only_authenticated_completed_paths_are_removed(tmp_path):
    root, stage, spec, download = receipt_fixture(tmp_path)
    unrelated = Path(spec["target"]).with_name(".agent-manager-old-" + "f" * 48 + ".exe")
    unrelated.write_bytes(b"old executable")
    ordinary = download.with_name("my-downloaded-tool.exe")
    ordinary.write_bytes(download.read_bytes())
    result = cleanup.cleanup_verified_updates(root)
    assert result == {"removed": 3, "deferred": 0, "pruned": 0}
    assert Path(spec["target"]).exists() and unrelated.exists() and ordinary.exists()
    assert not Path(spec["source"]).exists() and not Path(spec["backup"]).exists() and not download.exists()
    assert (stage / "install.json").exists()


@pytest.mark.parametrize("state", ["prepared", "waiting_for_exit", "installed", "failed", "complete"])
def test_unverified_and_failed_receipts_never_delete(state, tmp_path):
    root, stage, spec, download = receipt_fixture(tmp_path, completed=False)
    _atomic_json(stage / "result.json", {"state": state, "restart": {"ready": True}})
    assert cleanup.cleanup_verified_updates(root)["removed"] == 0
    assert all(Path(path).exists() for path in (spec["source"], spec["target"], spec["backup"], download))


def test_modified_receipt_or_authority_cannot_authorize_deletion(tmp_path):
    root, stage, spec, download = receipt_fixture(tmp_path)
    receipt = stage / cleanup.RECEIPT_NAME
    envelope = json.loads(receipt.read_text(encoding="utf-8"))
    value = json.loads(base64.b64decode(envelope["payload"]))
    value["files"][0]["path"] = str(download)
    envelope["payload"] = base64.b64encode(json.dumps(value).encode()).decode()
    _atomic_json(receipt, envelope)
    assert cleanup.cleanup_verified_updates(root)["removed"] == 0
    assert Path(spec["backup"]).exists() and download.exists()


def test_changed_result_after_signed_success_preserves_every_file(tmp_path):
    root, stage, spec, download = receipt_fixture(tmp_path)
    _atomic_json(stage / "result.json", {"installId": stage.name, "state": "failed", "code": "install_failed"})
    assert cleanup.cleanup_verified_updates(root)["removed"] == 0
    assert Path(spec["source"]).exists() and Path(spec["backup"]).exists() and download.exists()


def test_changed_file_digest_and_current_download_are_retained(tmp_path):
    root, _, spec, download = receipt_fixture(tmp_path)
    backup = Path(spec["backup"])
    backup.write_bytes(b"unrelated newer file")
    result = cleanup.cleanup_verified_updates(root, protected_paths=[download, spec["target"]])
    assert result["removed"] == 1 and result["deferred"] == 2
    assert backup.read_bytes() == b"unrelated newer file"
    assert download.exists() and Path(spec["target"]).exists()


@pytest.mark.skipif(os.name != "nt", reason="real Windows exclusive file lock")
def test_locked_file_is_deferred_then_retried(tmp_path):
    root, stage, spec, download = receipt_fixture(tmp_path)
    with Path(spec["backup"]).open("rb"):
        result = cleanup.cleanup_verified_updates(root)
        assert result["removed"] == 2 and result["deferred"] == 1
        assert Path(spec["backup"]).exists()
        assert (stage / cleanup.RECEIPT_NAME).exists()
    result = cleanup.cleanup_verified_updates(root)
    assert result["removed"] == 1 and result["deferred"] == 0
    assert not Path(spec["backup"]).exists()


def test_pending_install_protects_reused_download_and_recovery_originals(tmp_path):
    root, _, spec, download = receipt_fixture(tmp_path)
    _, pending, pending_spec, pending_download = receipt_fixture(tmp_path, 2, completed=False)
    cleanup.prepare_cleanup(pending, pending_spec, download)
    _atomic_json(pending / "install.json", pending_spec)
    result = cleanup.cleanup_verified_updates(root)
    assert result["removed"] == 2
    assert download.exists() and pending_download.exists()
    assert Path(pending_spec["backup"]).exists() and Path(pending_spec["source"]).exists()
    assert Path(spec["target"]).exists()


def test_active_install_id_is_never_cleaned(tmp_path):
    root, stage, spec, download = receipt_fixture(tmp_path)
    assert cleanup.cleanup_verified_updates(root, active_install_id=stage.name)["removed"] == 0
    assert Path(spec["backup"]).exists() and download.exists()


def test_verified_receipt_storage_is_bounded_and_latest_pointer_preserved(tmp_path):
    fixtures = [receipt_fixture(tmp_path, index) for index in range(1, 10)]
    root, latest, _, _ = fixtures[-1]
    _atomic_json(root / "latest.json", {"installId": latest.name, "path": str(latest / "result.json")})
    result = cleanup.cleanup_verified_updates(root)
    assert result["removed"] == 27 and result["pruned"] == 4
    assert len([entry for entry in root.iterdir() if entry.is_dir()]) == cleanup.KEEP_RECEIPTS
    assert (latest / "result.json").exists()
    assert all(Path(spec["target"]).exists() for _, _, spec, _ in fixtures)


def test_unknown_stage_content_prevents_metadata_pruning(tmp_path):
    fixtures = [receipt_fixture(tmp_path, index) for index in range(1, 8)]
    root, stage, _, _ = fixtures[0]
    unrelated = stage / "user-notes.txt"
    unrelated.write_text("retain me")
    cleanup.cleanup_verified_updates(root)
    assert unrelated.read_text() == "retain me"
    assert (stage / cleanup.RECEIPT_NAME).exists()


@pytest.mark.skipif(os.name != "nt", reason="real Windows metadata lock")
def test_locked_receipt_defers_entire_metadata_prune_until_retry(tmp_path):
    fixtures = [receipt_fixture(tmp_path, index) for index in range(1, 7)]
    root, stage, _, _ = fixtures[0]
    metadata = {entry.name for entry in stage.iterdir() if entry.suffix != ".exe"}
    with (stage / cleanup.RECEIPT_NAME).open("rb"):
        result = cleanup.cleanup_verified_updates(root)
        assert result["pruned"] == 0
        assert {entry.name for entry in stage.iterdir()} == metadata
    result = cleanup.cleanup_verified_updates(root)
    assert result["pruned"] == 1
    assert not stage.exists()


def test_service_cleanup_excludes_download_in_flight_and_retries_after_operation(tmp_path):
    root, _, spec, download = receipt_fixture(tmp_path)
    service = AppUpdateService("99.0.0", root.parent / "config.json", download.parent, install_supported=True)
    service._download = {"state": "ready", "path": str(download)}
    assert service._operation.acquire(blocking=False)
    try:
        service._cleanup_verified_installations()
        assert Path(spec["backup"]).exists()
    finally:
        service._operation.release()
    service._cleanup_verified_installations()
    assert not Path(spec["backup"]).exists() and download.exists()
    service._download = {"state": "idle"}
    service._cleanup_at = 0
    service._cleanup_verified_installations()
    assert not download.exists()
    service.close()


def test_reparse_directory_never_allows_cleanup(tmp_path):
    root, stage, spec, download = receipt_fixture(tmp_path)
    alternate = tmp_path / "not-owned"
    alternate.mkdir()
    backup = Path(spec["backup"])
    alternate_file = alternate / backup.name
    alternate_file.write_bytes(backup.read_bytes())
    # Directory junctions exercise the real reparse guard even when this
    # Windows host does not grant symbolic-link creation privileges.
    alias = tmp_path / "link"
    try:
        alias.symlink_to(alternate, target_is_directory=True)
    except OSError:
        powershell = shutil.which("powershell.exe")
        if not powershell:
            pytest.skip("Host does not grant symbolic-link creation")
        script = tmp_path / "create-test-junction.ps1"
        quote = lambda path: "'" + str(path).replace("'", "''") + "'"
        script.write_text("$ErrorActionPreference='Stop'\nNew-Item -ItemType Junction -Path "
                          + quote(alias) + " -Target " + quote(alternate) + " | Out-Null\n", encoding="utf-8-sig")
        result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                                capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    key = cleanup._key(root)
    value = cleanup._unseal(stage / cleanup.RECEIPT_NAME, key)
    value["target"] = str(alias / "current.exe")
    value["files"][0]["path"] = str(alias / backup.name)
    _atomic_json(stage / cleanup.RECEIPT_NAME, cleanup._seal(value, key))
    assert cleanup.cleanup_verified_updates(root)["removed"] == 0
    assert alternate_file.exists() and download.exists()
