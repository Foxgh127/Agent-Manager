"""Destructive cleanup must stay within the selected generated-file scope."""
from pathlib import Path

import pytest

from scripts.cleanup_project import clean, report_path


def test_explicit_report_path_does_not_add_standard_report_directories(tmp_path, monkeypatch, capsys):
    from scripts import cleanup_project
    root = tmp_path.resolve()
    current = root / "artifacts" / "checks"
    current.mkdir(parents=True)
    (current / "keep.log").write_text("current")
    old = root / "artifacts" / "old.log"
    old.write_text("old")
    monkeypatch.setattr(cleanup_project, "ROOT", root)
    monkeypatch.setattr("sys.argv", ["cleanup", "--reports-only", "--report-path", "artifacts/old.log", "--apply"])
    cleanup_project.main()
    assert not old.exists()
    assert (current / "keep.log").read_text() == "current"


def test_preview_and_selected_report_cleanup_preserve_source_and_other_reports(tmp_path):
    root = tmp_path.resolve()
    report = root / "artifacts" / "old-report"
    report.mkdir(parents=True)
    log = report / "check.log"
    log.write_text("old")
    source = root / "src" / "__pycache__"
    source.mkdir(parents=True)
    bytecode = source / "keep.pyc"
    bytecode.write_bytes(b"cache")
    other = root / "artifacts" / "current.md"
    other.write_text("current evidence")
    preview = clean(root, [report], apply=False, bytecode=False)
    assert preview["files"] == 1 and log.exists()
    applied = clean(root, [report], apply=True, bytecode=False)
    assert applied["removedFiles"] == 1 and not report.exists()
    assert bytecode.read_bytes() == b"cache" and other.read_text() == "current evidence"


@pytest.mark.parametrize("value", ["artifacts", "artifacts/../src", "../outside", "dist/AgentManager.exe"])
def test_report_selection_rejects_broad_or_unrelated_targets(tmp_path, value):
    with pytest.raises(ValueError):
        report_path(value, tmp_path.resolve())


def test_cleanup_does_not_follow_nested_directory_link(tmp_path):
    root = tmp_path.resolve()
    report = root / "artifacts" / "report"
    report.mkdir(parents=True)
    outside = root / "source"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep")
    linked = report / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Host cannot create directory symlinks")
    result = clean(root, [report], apply=True, bytecode=False)
    assert sentinel.read_text() == "keep"
    assert str(linked.relative_to(root)) in result["retained"]
    assert linked.is_symlink()
