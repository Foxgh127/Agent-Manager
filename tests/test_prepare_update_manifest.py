import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.prepare_update_manifest import build_manifest, validate_manifest


def test_manifest_notes_are_a_plain_string_and_fit_the_client_limit(tmp_path):
    asset = tmp_path / "AgentManager-1.2.0.exe"
    payload = b"synthetic executable"
    asset.write_bytes(payload)
    notes = tmp_path / "release-notes.md"
    notes.write_text("修复更新检查。\n", encoding="utf-8")

    document = build_manifest(version="1.2.0", release_epoch=1, repository="owner/app",
                              asset_name=asset.name, asset_path=asset, notes_path=notes,
                              published_at="2026-09-18T19:30:37Z")
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
    assert isinstance(document["releaseNotes"], str)
    assert document["releaseNotes"] == "修复更新检查。\n"
    assert document["assets"][0]["size"] == len(payload)
    assert document["assets"][0]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert len(encoded) < 2 * 1024 * 1024


def test_manifest_rejects_provider_object_notes():
    document = {
        "schemaVersion": 1, "appId": "openai-agent-manager", "version": "1.2.0",
        "channel": "stable", "publishedAt": "2026-09-18T19:30:37Z",
        "releaseNotes": {"value": "bad", "PSProvider": {"huge": True}}, "releaseEpoch": 1,
        "assets": [{"platform": "windows-x64", "name": "AgentManager-1.2.0.exe",
                     "url": "https://github.com/owner/app/releases/download/v1.2.0/AgentManager-1.2.0.exe",
                     "size": 1, "sha256": "0" * 64}],
    }
    with pytest.raises(ValueError, match="releaseNotes"):
        validate_manifest(document)


def test_manifest_rejects_oversized_notes(tmp_path):
    asset = tmp_path / "AgentManager-1.2.0.exe"
    asset.write_bytes(b"x")
    notes = tmp_path / "release-notes.md"
    notes.write_text("x" * 12001, encoding="utf-8")
    with pytest.raises(ValueError, match="12,000"):
        build_manifest(version="1.2.0", release_epoch=1, repository="owner/app",
                       asset_name=asset.name, asset_path=asset, notes_path=notes)


def _aliased_manifest(tmp_path):
    asset = tmp_path / "Agent-Manager-1.3.3.exe"
    asset.write_bytes(b"MZ identical release bytes")
    return build_manifest(
        version="1.3.3", release_epoch=1, repository="owner/app",
        asset_name=asset.name, asset_path=asset, notes_path=None,
        alias_names=("AgentManager-1.3.3.exe",),
    )


def test_manifest_compatibility_alias_preserves_bytes_and_versioned_download(tmp_path):
    document = _aliased_manifest(tmp_path)
    primary, alias = document["assets"]
    assert primary["name"] == "Agent-Manager-1.3.3.exe"
    assert alias["name"] == "AgentManager-1.3.3.exe"
    assert primary["sha256"] == alias["sha256"]
    assert primary["size"] == alias["size"]
    assert alias["url"] == "https://github.com/owner/app/releases/download/v1.3.3/AgentManager-1.3.3.exe"


@pytest.mark.parametrize("field,value", [("size", 999), ("sha256", "0" * 64)])
def test_manifest_rejects_alias_with_different_executable_bytes(tmp_path, field, value):
    document = _aliased_manifest(tmp_path)
    document["assets"][1][field] = value
    with pytest.raises(ValueError, match="same executable bytes"):
        validate_manifest(document)


def test_manifest_rejects_duplicate_alias_names(tmp_path):
    document = _aliased_manifest(tmp_path)
    document["assets"][1] = dict(document["assets"][0])
    with pytest.raises(ValueError, match="unique"):
        validate_manifest(document)


def test_manifest_rejects_alias_from_another_release(tmp_path):
    document = _aliased_manifest(tmp_path)
    document["assets"][1]["url"] = document["assets"][1]["url"].replace("/v1.3.3/", "/v1.3.2/")
    with pytest.raises(ValueError, match="same GitHub release"):
        validate_manifest(document)


def test_manifest_cli_writes_and_validates_both_compatibility_names(tmp_path):
    asset = tmp_path / "Agent-Manager-1.3.3.exe"
    asset.write_bytes(b"MZ release fixture")
    output = tmp_path / "app-update-manifest.json"
    command = [
        sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/prepare_update_manifest.py"),
        "--version", "1.3.3", "--release-epoch", "1", "--repository", "owner/app",
        "--asset-name", asset.name, "--alias-name", "AgentManager-1.3.3.exe",
        "--asset-path", str(asset), "--output", str(output),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    subprocess.run([*command, "--validate"], check=True, capture_output=True, text=True)
    document = json.loads(output.read_text(encoding="utf-8"))
    assert [item["name"] for item in document["assets"]] == [asset.name, "AgentManager-1.3.3.exe"]
