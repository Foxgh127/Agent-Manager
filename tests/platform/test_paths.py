from agent_manager.platform.paths import ensure_writable_directory, is_windows_store_path, normalize_path


def test_windows_store_detection_accepts_windows_style_paths() -> None:
    assert is_windows_store_path(r"C:\Program Files\WindowsApps\Package\codex.exe")
    assert is_windows_store_path(r"C:\Program Files\Microsoft.WindowsStore_123\codex.exe")
    assert is_windows_store_path("C:/Program Files/WindowsApps/Package/codex.exe")
    assert not is_windows_store_path(r"C:\Program Files\Normal\codex.exe")


def test_normalize_and_writable_directory(tmp_path) -> None:
    target = tmp_path / "nested"
    assert normalize_path(target).is_absolute()
    assert ensure_writable_directory(target)
    assert target.is_dir()
