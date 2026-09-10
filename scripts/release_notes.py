"""Select one explicitly numbered release section; reject stale/ambiguous notes."""
from __future__ import annotations

import argparse
from pathlib import Path
import re

HEADING = re.compile(r"^#{1,6}\s+(?:Agent\s+Manager\s+)?v?(\d+\.\d+\.\d+)\s*$", re.MULTILINE | re.IGNORECASE)


def select_release_notes(content: str, version: str) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Release version must be an explicit x.y.z value.")
    headings = list(HEADING.finditer(content))
    matching = [i for i, heading in enumerate(headings) if heading.group(1) == version]
    if len(matching) != 1:
        raise ValueError(f"Expected exactly one release-notes section for {version}; found {len(matching)}.")
    index = matching[0]
    end = headings[index + 1].start() if index + 1 < len(headings) else len(content)
    result = content[headings[index].start():end].strip()
    if not result.splitlines()[1:]:
        raise ValueError("Release notes contain no changes.")
    return result + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.input.stat().st_size > 200_000:
        parser.error("Release notes exceed 200 KB.")
    try:
        notes = select_release_notes(args.input.read_text(encoding="utf-8-sig"), args.version)
    except ValueError as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(notes, encoding="utf-8")


if __name__ == "__main__":
    main()
