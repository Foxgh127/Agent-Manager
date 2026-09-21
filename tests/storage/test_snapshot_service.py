from __future__ import annotations

from agent_manager.storage.snapshot_service import (
    AccountSnapshotProvider,
    Snapshot,
    SnapshotService,
)


def test_snapshot_roundtrip_and_cleanup(tmp_path) -> None:
    accounts = {"a": {"email": "user@example.com"}}
    provider = AccountSnapshotProvider(accounts)
    service = SnapshotService(tmp_path)
    snapshot = service.create_snapshot(provider, metadata={"kind": "test"})
    service.save_snapshot(snapshot, "first")
    loaded = service.load_snapshot("first")
    assert loaded is not None
    assert loaded.content_hash == snapshot.content_hash
    assert loaded.data == snapshot.data
    accounts["a"]["email"] = "changed@example.com"
    service.restore_snapshot(loaded, provider)
    assert accounts["a"]["email"] == "user@example.com"
    for index in range(5):
        service.save_snapshot(Snapshot(f"{index:064x}", float(index), {"n": index}, {}), f"snap{index}")
    removed = service.cleanup_old_snapshots(keep_count=3)
    assert len(removed) == 3
    assert len(service.list_snapshots()) == 3


def test_snapshot_name_cannot_escape_storage(tmp_path) -> None:
    service = SnapshotService(tmp_path)
    snapshot = Snapshot("0" * 64, 1.0, {}, {})
    try:
        service.save_snapshot(snapshot, "../outside")
    except ValueError:
        pass
    else:
        raise AssertionError("path traversal name was accepted")
