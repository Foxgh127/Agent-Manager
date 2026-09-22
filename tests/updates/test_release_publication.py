"""Exercise release staging in an isolated fixture without external commands."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(sys.platform != "win32" or not POWERSHELL,
                    reason="Windows PowerShell is required for release staging")
def test_local_publisher_stages_identical_current_and_legacy_assets(tmp_path):
    project = tmp_path / "release fixture"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    repository = Path(__file__).resolve().parents[2]
    for name in ("publish.ps1", "prepare_update_manifest.py", "release_notes.py"):
        shutil.copy2(repository / "scripts" / name, scripts / name)
    resources = project / "src/agent_manager/resources"
    resources.mkdir(parents=True)
    (resources / "version.json").write_text('{"version":"1.3.3","releaseEpoch":1}', encoding="utf-8")
    frontend = project / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text('{"version":"1.3.3"}', encoding="utf-8")
    distribution = project / "dist"
    distribution.mkdir()
    payload = b"MZ inert release fixture"
    digest = hashlib.sha256(payload).hexdigest()
    (distribution / "AgentManager.exe").write_bytes(payload)
    (distribution / "SHA256.txt").write_text(f"{digest}  AgentManager.exe\n", encoding="utf-8")
    notes = project / "notes.md"
    notes.write_text("# Agent Manager 1.3.3\n\n- Current fix.\n\n# Agent Manager 1.3.0\n\n- Old fix.\n", encoding="utf-8")

    # Only package inspection is stubbed: the fixture has no runnable EXE.
    # Manifest creation, file copies, hashes and release-note selection are real.
    harness = tmp_path / "stage.ps1"
    harness.write_text(r"""
param([string]$Project, [string]$TestPython)
$ErrorActionPreference = 'Stop'
function python {
    if ($args[0] -eq '-c' -or $args[0] -like '*sync_version.py' -or $args[0] -like '*verify_package.py') {
        $global:LASTEXITCODE = 0
        return
    }
    & $TestPython @args
    $global:LASTEXITCODE = $LASTEXITCODE
}
function Get-Item {
    $item = Microsoft.PowerShell.Management\Get-Item @args
    if ($item.Extension -eq '.exe') {
        return [pscustomobject]@{Length=$item.Length; VersionInfo=[pscustomobject]@{FileVersion='1.3.3'}}
    }
    return $item
}
function gh { throw 'Local release staging must never contact GitHub.' }
& (Join-Path $Project 'scripts/publish.ps1') -Repository owner/app -NotesFile (Join-Path $Project 'notes.md')
""", encoding="utf-8")
    completed = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(harness), str(project), sys.executable],
        capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Nothing uploaded" in completed.stdout
    staged = project / "artifacts/publish/1.3.3"
    names = ("Agent-Manager-1.3.3.exe", "AgentManager-1.3.3.exe")
    assert all((staged / name).read_bytes() == payload for name in names)
    assert (staged / "SHA256.txt").read_text(encoding="utf-8").splitlines() == [
        f"{digest}  {name}" for name in names
    ]
    manifest = json.loads((staged / "app-update-manifest.json").read_text(encoding="utf-8"))
    assert [asset["name"] for asset in manifest["assets"]] == list(names)
    assert {asset["sha256"] for asset in manifest["assets"]} == {digest}
    assert "Current fix" in manifest["releaseNotes"]
    assert "Old fix" not in manifest["releaseNotes"]
    bundled_source = json.loads((resources / "app-update-source.json").read_text(encoding="utf-8"))
    assert bundled_source["assetName"] == "Agent-Manager-{version}.exe"
