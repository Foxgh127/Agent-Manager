"""Delayed update readiness: real loopback health, isolated EXE/metadata fixtures."""
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading

import pytest

from agent_manager.updates import installer
from agent_manager.updates.service import AppUpdateService


@pytest.fixture
def installation(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows frozen updater proof")
    target = tmp_path / "AgentManager.exe"
    target.write_bytes(b"isolated new executable; never run")
    created = int((datetime.now(timezone.utc).timestamp() + 11644473600) * 10_000_000)
    spec = {"schemaVersion": 1, "installId": "a" * 48, "version": "99.0.0",
            "target": str(target), "runtimeFile": str(tmp_path / "app-runtime.json"),
            "sourceStartFileTime": str(created - 10_000_000), "sourceNonce": "previous_nonce_123456789",
            "preparedAt": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            "size": target.stat().st_size, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
    health = {"ok": True, "appId": "openai-agent-manager", "runtimePid": os.getpid(),
              "runtimeNonce": "current_nonce_123456789", "uiReady": True, "independentLifecycle": True}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(health).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    runtime = {**health, "pid": os.getpid(), "port": server.server_port}
    runtime_path = tmp_path / "app-runtime.json"
    runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
    monkeypatch.setattr(installer.sys, "frozen", True, raising=False)
    monkeypatch.setattr(installer.sys, "executable", str(target))
    monkeypatch.setattr(installer, "_process_creation_filetime", lambda pid: created)
    try:
        yield spec, runtime, health, target, runtime_path
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)


def prove(installation):
    spec, _, _, _, runtime_path = installation
    return installer.verify_current_installation(spec, current_version="99.0.0", runtime_file=runtime_path)


def test_late_ready_requires_current_process_health_and_executable_digest(installation):
    proof = prove(installation)
    assert proof["ready"] is True
    assert proof["pid"] == os.getpid()
    assert proof["sha256"] == installation[0]["sha256"]


@pytest.mark.parametrize("change", [
    {"runtimeNonce": "different_nonce_123456789"}, {"runtimePid": 1},
    {"uiReady": False}, {"independentLifecycle": False}, {"uiReady": "true"}, {"ok": "true"},
])
def test_health_mismatch_cannot_clear_history(installation, change):
    installation[2].update(change)
    assert prove(installation) is None


@pytest.mark.parametrize("change", [
    {"runtimeNonce": "previous_nonce_123456789"}, {"pid": 1}, {"uiReady": False},
    {"uiReady": "true"}, {"independentLifecycle": False}, {"port": True},
])
def test_runtime_mismatch_cannot_clear_history(installation, change):
    _, runtime, _, _, runtime_path = installation
    runtime.update(change)
    runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
    assert prove(installation) is None


@pytest.mark.parametrize("field,value", [
    ("version", "100.0.0"), ("sourceStartFileTime", "999999999999999999"),
    ("preparedAt", "2099-01-01T00:00:00+00:00"), ("preparedAt", "2026-01-01T00:00:00"),
    ("sha256", "0" * 64), ("size", 2), ("installId", "b"),
])
def test_install_identity_mismatch_cannot_clear_history(installation, field, value):
    installation[0][field] = value
    assert prove(installation) is None


def test_other_executable_with_same_version_cannot_clear_history(installation):
    spec, _, _, target, _ = installation
    other = target.with_name("Different.exe")
    other.write_bytes(target.read_bytes())
    spec["target"] = str(other)
    assert prove(installation) is None


def test_tampered_executable_cannot_clear_history(installation):
    installation[3].write_bytes(b"tampered executable")
    assert prove(installation) is None


@pytest.mark.parametrize("state,code", [("failed", None), ("installed", "startup_unverified")])
def test_service_reconciles_historical_timeout_without_overwriting_evidence(installation, state, code):
    spec, _, health, _, runtime_path = installation
    root = runtime_path.parent / "app-update-install"
    stage = root / spec["installId"]
    stage.mkdir(parents=True)
    (stage / "install.json").write_text(json.dumps(spec), encoding="utf-8")
    result = {"installId": spec["installId"], "state": state, "message": "original helper timeout",
              "code": code, "backup": "retained-backup.exe", "restart": {"ready": False}}
    result_path = stage / "result.json"
    original = json.dumps(result)
    result_path.write_text(original, encoding="utf-8")
    (root / "latest.json").write_text(json.dumps({"path": str(result_path), "installId": spec["installId"]}))
    service = AppUpdateService("99.0.0", runtime_path.with_name("config.json"), root / "downloads", install_supported=True)
    try:
        health["uiReady"] = False
        pending = service.status()["installation"]
        assert pending["state"] == state
        assert pending["restart"]["ready"] is False
        health["uiReady"] = True
        service._installation_probe_at = 0.0
        status = service.status()["installation"]
        assert status["state"] == "complete"
        assert status["code"] == "update_ready_reconciled"
        assert status["previousState"] == state
        assert status["detail"] == "original helper timeout"
        assert status["backup"] == result["backup"]
        assert status["verification"]["method"] == "current_process_health_and_sha256"
        assert result_path.read_text(encoding="utf-8") == original
        installation[3].write_bytes(b"executable changed after the first proof")
        service._installation_probe_at = 0.0
        assert service.status()["installation"]["state"] == state
    finally:
        service.close()


@pytest.mark.parametrize("scenario,expected", [("trial_path", "idle"), ("newer_version", "idle"), ("changed_bytes", "failed")])
def test_other_installation_is_history_not_success(installation, monkeypatch, scenario, expected):
    spec, _, _, target, runtime_path = installation
    root = runtime_path.parent / "app-update-install"
    stage = root / spec["installId"]
    stage.mkdir(parents=True)
    (stage / "install.json").write_text(json.dumps(spec), encoding="utf-8")
    result_path = stage / "result.json"
    record = {"installId": spec["installId"], "state": "failed", "message": "previous attempt failed"}
    result_path.write_text(json.dumps(record), encoding="utf-8")
    (root / "latest.json").write_text(json.dumps({"path": str(result_path), "installId": spec["installId"]}))
    version = "99.0.0"
    if scenario == "trial_path":
        trial = target.with_name("AgentManager-local.exe")
        trial.write_bytes(b"different local trial build")
        monkeypatch.setattr(installer.sys, "executable", str(trial))
    elif scenario == "newer_version":
        version = "99.0.1"
        target.write_bytes(b"newer executable installed manually")
    else:
        target.write_bytes(b"unexpected change at the same path and version")
    service = AppUpdateService(version, runtime_path.with_name("config.json"), root / "downloads", install_supported=True)
    try:
        status = service.status()["installation"]
        assert status["state"] == expected
        assert status["state"] != "complete"
        if expected == "idle":
            assert status["code"] == "previous_installation"
            assert status["previousState"] == "failed"
            assert status["detail"] == "previous attempt failed"
        assert json.loads(result_path.read_text(encoding="utf-8")) == record
    finally:
        service.close()
