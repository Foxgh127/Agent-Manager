"""Preview and remove explicitly selected generated files, without following links."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat

ROOT = Path(__file__).resolve().parents[1]
GENERATED = (
    "artifacts/build", "artifacts/publish", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".coverage", "frontend/.preview", "frontend/work",
)
ARCHIVES = ("audit", "work")
REPORTS = ("artifacts/checks", "artifacts/reports", "artifacts/preview")
DEPENDENCIES = ("frontend/node_modules", "frontend/dist", "src/agent_manager/resources/ui")


def direct(path: Path, root: Path) -> bool:
    """Require lexical containment and reject symlinks and Windows reparse points."""
    path = Path(os.path.abspath(path))
    if path == root or not path.is_relative_to(root):
        return False
    for item in (path, *path.parents):
        if item == root:
            break
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            return False
    return True


def report_path(value: str, root: Path) -> Path:
    candidate = Path(os.path.abspath(root / value))
    # Never accept the entire artifacts root or an arbitrary source/data path.
    if candidate == root / "artifacts" or not candidate.is_relative_to(root / "artifacts"):
        raise ValueError("Report paths must name a file or subdirectory inside artifacts/.")
    return candidate


def clean(root: Path, targets: list[Path], *, apply: bool, bytecode: bool) -> dict:
    files: set[Path] = set()
    folders: set[Path] = set()
    retained: set[str] = set()

    def label(path: Path) -> str:
        return str(path.relative_to(root)) if path.is_relative_to(root) else str(path)

    def walk(top: Path, *, only_bytecode: bool = False) -> None:
        if not direct(top, root):
            retained.add(label(top))
            return
        if not top.exists():
            return
        if top.is_file():
            files.add(top)
            return
        for current, directories, names in os.walk(top, followlinks=False):
            base = Path(current)
            selected = not only_bytecode or base.name == "__pycache__"
            for directory in directories[:]:
                if not direct(base / directory, root):
                    directories.remove(directory)
                    retained.add(label(base / directory))
            if not selected:
                continue
            folders.add(base)
            for name in names:
                path = base / name
                if direct(path, root) and path.is_file():
                    files.add(path)
                else:
                    retained.add(label(path))

    for target in targets:
        walk(target)
    if bytecode:
        for name in ("src", "tests", "scripts"):
            walk(root / name, only_bytecode=True)

    snapshot = []
    for path in sorted(files):
        try:
            info = path.lstat()
            snapshot.append((path, info.st_ino, info.st_size, info.st_mtime_ns))
        except OSError:
            retained.add(label(path))
    summary = {
        "targets": [label(path) for path in targets], "files": len(snapshot),
        "bytes": sum(item[2] for item in snapshot), "applied": apply,
    }
    if apply:
        removed = 0
        for path, inode, size, modified in snapshot:
            try:
                now = path.lstat()
                if not direct(path, root) or (now.st_ino, now.st_size, now.st_mtime_ns) != (inode, size, modified):
                    retained.add(label(path))
                    continue
                if os.name == "nt" and now.st_file_attributes & stat.FILE_ATTRIBUTE_READONLY:
                    # Retired git audit packs can be read-only; identity was checked above.
                    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                path.unlink()
                removed += 1
            except OSError:
                retained.add(label(path))
        for path in sorted(folders, key=lambda value: len(value.parts), reverse=True):
            try:
                if direct(path, root):
                    path.rmdir()  # Empty directories only; never recursively delete.
            except OSError:
                pass
        summary["removedFiles"] = removed
    summary["retained"] = sorted(retained)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Remove selected files; default is a dry run.")
    scopes = parser.add_mutually_exclusive_group()
    scopes.add_argument("--generated-only", action="store_true", help="Only reproducible build outputs and caches.")
    scopes.add_argument("--archives-only", action="store_true", help="Only retired audit/work directories.")
    scopes.add_argument("--reports-only", action="store_true", help="Only development checks, reports and previews.")
    parser.add_argument("--report-path", action="append", default=[], help="Explicit diagnostic path inside artifacts/; repeat as needed.")
    parser.add_argument("--dependencies", action="store_true", help="Also remove reproducible frontend dependencies and assets.")
    parser.add_argument("--legacy-release", action="store_true", help="Also remove retired release/ output; close its executable first.")
    args = parser.parse_args()
    if (args.archives_only or args.reports_only) and (args.dependencies or args.legacy_release):
        parser.error("This scope cannot remove dependencies or installed executables.")
    if args.generated_only and args.legacy_release:
        parser.error("Generated-only cleanup must preserve installed executables.")
    if args.report_path and not args.reports_only:
        parser.error("Use --reports-only when selecting individual diagnostic paths.")

    names = list(ARCHIVES if args.archives_only else
                 (() if args.report_path else REPORTS) if args.reports_only else GENERATED)
    if not (args.generated_only or args.archives_only or args.reports_only):
        names.extend(ARCHIVES)
    if args.dependencies:
        names.extend(DEPENDENCIES)
    if args.legacy_release:
        names.append("release")
    targets = [ROOT / name for name in names]
    try:
        targets.extend(report_path(value, ROOT) for value in args.report_path)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(clean(ROOT, targets, apply=args.apply, bytecode=not (args.archives_only or args.reports_only)),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
