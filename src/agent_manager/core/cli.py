"""Cli services."""
from __future__ import annotations
from agent_manager import core as _core


def main(argv: list[str] | None = None) -> int:
    parser = _core.argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("state")
    sub.add_parser("preview")
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--sync-secrets", action="store_true")
    sub.add_parser("validate")
    sub.add_parser("export")
    args = parser.parse_args(argv)
    if args.command == "state":
        print(_core.json.dumps(_core.public_state(), ensure_ascii=False, indent=2))
    elif args.command == "preview":
        print(_core.preview_apply()["diff"])
    elif args.command == "apply":
        print(_core.json.dumps(_core.apply_configuration(args.sync_secrets), ensure_ascii=False, indent=2))
    elif args.command == "validate":
        result = _core.validate_configuration()
        print(_core.json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["valid"] else 1
    elif args.command == "export":
        print(_core.json.dumps(_core.export_bundle(), ensure_ascii=False, indent=2))
    return 0

