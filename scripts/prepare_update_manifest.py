"""Create and validate the small, plain JSON document consumed by the updater.

This is deliberately implemented in Python instead of PowerShell's object
serializer.  Windows PowerShell can expose provider properties on values
returned by Get-Content; ConvertTo-Json then serializes those properties along
with the release notes and can turn a sub-kilobyte manifest into megabytes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_RELEASE_NOTES_CHARS = 12_000
MAX_ASSET_BYTES = 1024 * 1024 * 1024
APP_ID = "openai-agent-manager"
_VERSION = re.compile(r"\d+\.\d+\.\d+$")
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SHA256 = re.compile(r"[0-9a-f]{64}$")


def _plain_text(path: Path) -> str:
    value = path.read_text(encoding="utf-8-sig")
    if not isinstance(value, str):  # defensive: keep the JSON contract explicit
        raise ValueError("Release notes must be plain text.")
    if len(value) > MAX_RELEASE_NOTES_CHARS:
        raise ValueError("Release notes exceed 12,000 characters.")
    return value


def validate_manifest(document: object, *, expected_version: str | None = None,
                      expected_asset_name: str | None = None,
                      expected_asset_size: int | None = None,
                      expected_asset_sha256: str | None = None,
                      expected_alias_names: tuple[str, ...] | None = None) -> dict:
    """Validate the producer-side manifest contract and return the document."""
    if not isinstance(document, dict):
        raise ValueError("Update manifest must be a JSON object.")
    required = {"schemaVersion", "appId", "version", "channel", "publishedAt", "releaseNotes", "assets", "releaseEpoch"}
    if set(document) != required:
        raise ValueError("Update manifest has unexpected or missing fields.")
    if document["schemaVersion"] != 1 or document["appId"] != APP_ID:
        raise ValueError("Update manifest schema or app id is invalid.")
    version = document["version"]
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ValueError("Update manifest version is not a stable x.y.z version.")
    if expected_version is not None and version != expected_version:
        raise ValueError("Update manifest version does not match the package.")
    if document["channel"] != "stable":
        raise ValueError("Only the stable update channel is publishable here.")
    if not isinstance(document["publishedAt"], str) or not document["publishedAt"].strip():
        raise ValueError("Update manifest publishedAt must be a string.")
    notes = document["releaseNotes"]
    if not isinstance(notes, str) or len(notes) > MAX_RELEASE_NOTES_CHARS:
        raise ValueError("Update manifest releaseNotes must be plain text of at most 12,000 characters.")
    epoch = document["releaseEpoch"]
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise ValueError("Update manifest releaseEpoch is invalid.")
    assets = document["assets"]
    if not isinstance(assets, list) or not 1 <= len(assets) <= 8:
        raise ValueError("Update manifest must contain a Windows asset and at most seven compatibility aliases.")
    asset = assets[0]
    _validate_asset(asset)
    names = [asset["name"]]
    for alias in assets[1:]:
        _validate_asset(alias)
        if alias["size"] != asset["size"] or alias["sha256"] != asset["sha256"]:
            raise ValueError("Compatibility aliases must describe the same executable bytes.")
        if alias["url"].rsplit("/", 1)[0] != asset["url"].rsplit("/", 1)[0]:
            raise ValueError("Compatibility aliases must belong to the same GitHub release.")
        names.append(alias["name"])
    if len(set(names)) != len(names):
        raise ValueError("Update manifest asset names must be unique.")
    if expected_alias_names is not None and tuple(names[1:]) != expected_alias_names:
        raise ValueError("Update manifest compatibility aliases do not match the package.")
    if expected_asset_name is not None and asset["name"] != expected_asset_name:
        raise ValueError("Update manifest asset name does not match the package.")
    if expected_asset_size is not None and asset["size"] != expected_asset_size:
        raise ValueError("Update manifest asset size does not match the package.")
    if expected_asset_sha256 is not None and asset["sha256"] != expected_asset_sha256:
        raise ValueError("Update manifest asset SHA-256 does not match the package.")
    return document


def _validate_asset(asset: object) -> None:
    """Aliases share a platform and bytes; clients select their configured name."""
    if not isinstance(asset, dict) or set(asset) != {"platform", "name", "url", "size", "sha256"}:
        raise ValueError("Update manifest asset fields are invalid.")
    if asset["platform"] != "windows-x64" or not isinstance(asset["name"], str):
        raise ValueError("Update manifest does not describe a Windows asset.")
    if (not asset["name"].endswith(".exe") or "/" in asset["name"] or "\\" in asset["name"]
            or asset["name"] in {".", ".."}):
        raise ValueError("Update manifest asset name is unsafe.")
    size = asset["size"]
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_ASSET_BYTES:
        raise ValueError("Update manifest asset size is invalid.")
    digest = asset["sha256"]
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError("Update manifest asset SHA-256 is invalid.")
    if not isinstance(asset["url"], str) or not asset["url"].startswith("https://github.com/"):
        raise ValueError("Update manifest asset URL must be an HTTPS GitHub release URL.")


def build_manifest(*, version: str, release_epoch: int, repository: str,
                   asset_name: str, asset_path: Path, notes_path: Path | None,
                   published_at: str | None = None,
                   alias_names: tuple[str, ...] = ()) -> dict:
    if not _VERSION.fullmatch(version):
        raise ValueError("Version must be a stable x.y.z value.")
    if not _REPOSITORY.fullmatch(repository):
        raise ValueError("Repository must be owner/name.")
    if isinstance(release_epoch, bool) or not isinstance(release_epoch, int) or release_epoch < 0:
        raise ValueError("Release epoch must be a non-negative integer.")
    all_names = (asset_name, *alias_names)
    for name in all_names:
        if not isinstance(name, str) or not name.endswith(".exe") or "/" in name or "\\" in name:
            raise ValueError("Asset name must be a plain .exe filename.")
    asset_path = asset_path.resolve()
    if not asset_path.is_file():
        raise ValueError("Release executable does not exist.")
    size = asset_path.stat().st_size
    if not 0 < size <= MAX_ASSET_BYTES:
        raise ValueError("Release executable size is invalid.")
    digest = hashlib.sha256(asset_path.read_bytes()).hexdigest()
    notes = _plain_text(notes_path) if notes_path else "Agent Manager " + version
    document = {
        "schemaVersion": 1,
        "appId": APP_ID,
        "version": version,
        "channel": "stable",
        "publishedAt": published_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "releaseNotes": notes,
        "releaseEpoch": release_epoch,
        "assets": [{
            "platform": "windows-x64",
            "name": name,
            "url": f"https://github.com/{repository}/releases/download/v{version}/{name}",
            "size": size,
            "sha256": digest,
        } for name in all_names],
    }
    return validate_manifest(document, expected_version=version, expected_asset_name=asset_name,
                             expected_asset_size=size, expected_asset_sha256=digest,
                             expected_alias_names=tuple(alias_names))


def _write(document: dict, output: Path) -> None:
    encoded = (json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_METADATA_BYTES:
        raise ValueError("Update manifest exceeds the 2 MiB consumer limit.")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(encoded)
    # Read back the exact bytes that will be uploaded. This catches accidental
    # serializer changes before the release command contacts GitHub.
    validate_manifest(json.loads(encoded.decode("utf-8")))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--release-epoch", required=True, type=int)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--asset-name", required=True)
    parser.add_argument("--alias-name", action="append", default=[],
                        help="Compatibility filename for the same executable; may be repeated.")
    parser.add_argument("--asset-path", type=Path)
    parser.add_argument("--notes-file", type=Path)
    parser.add_argument("--published-at")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    try:
        if args.validate:
            document = json.loads(args.output.read_text(encoding="utf-8-sig"))
            validate_manifest(document, expected_version=args.version, expected_asset_name=args.asset_name,
                              expected_alias_names=tuple(args.alias_name))
            encoded_size = len(args.output.read_bytes())
            if encoded_size > MAX_METADATA_BYTES:
                raise ValueError("Update manifest exceeds the 2 MiB consumer limit.")
        else:
            if args.asset_path is None:
                parser.error("--asset-path is required when creating a manifest")
            _write(build_manifest(version=args.version, release_epoch=args.release_epoch,
                                  repository=args.repository, asset_name=args.asset_name,
                                  asset_path=args.asset_path, notes_path=args.notes_file,
                                  published_at=args.published_at,
                                  alias_names=tuple(args.alias_name)), args.output)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
