import ctypes
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import os

import pytest
import agent_manager.platform.window_theme as theme
import tests.integration.test_app_workbench_routes as routes
import unittest


def native_fixture(owner_pid=None, unsupported=False):
    def owner(hwnd, out):
        ctypes.cast(out, ctypes.POINTER(wintypes.DWORD)).contents.value = os.getpid() if owner_pid is None else owner_pid
        return 123
    recorded = {}
    def setter(hwnd, attribute, value, size):
        recorded[attribute] = (hwnd, ctypes.cast(value, ctypes.POINTER(wintypes.DWORD)).contents.value)
        return -1 if unsupported else 0
    native = SimpleNamespace(Handle=SimpleNamespace(ToInt64=lambda: 2**34 + 17))
    return SimpleNamespace(native=native), SimpleNamespace(GetWindowThreadProcessId=Mock(side_effect=owner)), SimpleNamespace(DwmSetWindowAttribute=Mock(side_effect=setter)), recorded


def test_caption_uses_native_64bit_handle_and_workspace_palette():
    window, user32, dwm, recorded = native_fixture()
    result = theme.apply_window_appearance(window, {"theme":"light","workspace":"claude"}, user32=user32, dwmapi=dwm)
    assert result["applied"]
    assert recorded[35] == (2**34 + 17, 0xe6f0f7)
    assert recorded[36][1] == 0x1f2c40
    assert recorded[20][1] == 0


def test_foreign_window_never_changed():
    window, user32, dwm, recorded = native_fixture(owner_pid=os.getpid()+1000)
    assert not theme.apply_window_appearance(window, {"theme":"dark","workspace":"codex"}, user32=user32, dwmapi=dwm)["applied"]
    assert not recorded


def test_older_windows_failure_does_not_raise():
    window, user32, dwm, _ = native_fixture(unsupported=True)
    assert not theme.apply_window_appearance(window, {"theme":"dark","workspace":"codex"}, user32=user32, dwmapi=dwm)["applied"]


def test_pending_window_remembers_latest_appearance():
    server = SimpleNamespace(native_window=True, native_window_object=None)
    for workspace in ("home","codex","claude"):
        result = theme.sync_server_appearance(server, {"theme":"light","workspace":workspace})
        assert result.get("pending")
    assert server.window_appearance == {"theme":"light","workspace":"claude"}


@pytest.mark.parametrize("value", [None, [], {"theme":[],"workspace":"codex"}, {"theme":"dark","workspace":"unknown"}])
def test_reject_invalid_appearance(value):
    with pytest.raises(ValueError): theme.validate_appearance(value)


def test_caption_palette_tracks_workspace_backgrounds():
    css = (Path(__file__).resolve().parents[2] / "frontend/src/workspaceThemes.css").read_text(encoding="utf-8")
    for (mode, workspace), (background, _) in theme.PALETTES.items():
        selector = f':root[data-workspace="{workspace}"]' if mode == "dark" else f':root[data-theme="light"][data-workspace="{workspace}"]'
        block = css.split(selector + " {",1)[1].split("}",1)[0]
        assert f"--bg: #{background};" in block


class NativeAppearanceRouteTests(unittest.TestCase):
    setUp = routes.WorkbenchRouteTests.setUp
    tearDown = routes.WorkbenchRouteTests.tearDown
    request = routes.WorkbenchRouteTests.request

    def test_authenticated_route_tracks_workspace(self):
        response = self.request("/api/window/appearance", method="POST", payload={"theme":"light","workspace":"codex"})
        self.assertTrue(response["ok"])
        self.assertEqual(self.server.window_appearance, {"theme":"light","workspace":"codex"})
