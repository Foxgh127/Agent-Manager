"""Match the owned Windows caption to the current workspace without replacing native controls."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os

PALETTES = {
    ("dark", "home"): ("161b20", "ebeef5"),
    ("dark", "codex"): ("121c20", "e9f3f1"),
    ("dark", "claude"): ("211d1b", "f6eddf"),
    ("light", "home"): ("f5f5f0", "1d232f"),
    ("light", "codex"): ("eff5f2", "1b312d"),
    ("light", "claude"): ("f7f0e6", "402c1f"),
}


def colorref(hex_color):
    red, green, blue = (int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    return red | green << 8 | blue << 16


def validate_appearance(payload):
    if not isinstance(payload, dict):
        raise ValueError("窗口外观参数无效。")
    theme, workspace = payload.get("theme"), payload.get("workspace")
    if not isinstance(theme, str) or not isinstance(workspace, str) or (theme, workspace) not in PALETTES:
        raise ValueError("窗口外观只支持当前工作台与深浅主题。")
    return {"theme": theme, "workspace": workspace}


def apply_window_appearance(window, appearance, *, user32=None, dwmapi=None):
    appearance = validate_appearance(appearance)
    if os.name != "nt" and (user32 is None or dwmapi is None):
        return {"supported": False, "applied": False}
    native = getattr(window, "native", None)
    handle = getattr(native, "Handle", None)
    if handle is None:
        return {"supported": True, "applied": False, "pending": True}
    hwnd = int(handle.ToInt64())
    user32 = user32 or ctypes.WinDLL("user32", use_last_error=True)
    dwmapi = dwmapi or ctypes.WinDLL("dwmapi", use_last_error=True)
    owner = user32.GetWindowThreadProcessId
    owner.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    owner.restype = wintypes.DWORD
    process_id = wintypes.DWORD()
    owner(hwnd, ctypes.byref(process_id))
    if not hwnd or process_id.value != os.getpid():
        return {"supported": True, "applied": False, "reason": "window_not_owned"}
    setter = dwmapi.DwmSetWindowAttribute
    setter.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    setter.restype = ctypes.c_long
    background, foreground = PALETTES[(appearance["theme"], appearance["workspace"])]
    attributes = {
        20: int(appearance["theme"] == "dark"),  # Native caption buttons/theme.
        35: colorref(background),                 # Caption background, Windows 11.
        36: colorref(foreground),                 # Caption text.
        34: colorref(background),                 # Border.
    }
    # DWMWA_CAPTION_COLOR=35, TEXT_COLOR=36, BORDER_COLOR=34.
    results = {}
    for attribute, value in attributes.items():
        data = wintypes.DWORD(value)
        results[attribute] = setter(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data)) == 0
    return {"supported": True, "applied": results[35] and results[36],
            "darkModeApplied": results[20], "theme": appearance["theme"], "workspace": appearance["workspace"]}


def sync_server_appearance(server, payload=None):
    if payload is not None:
        server.window_appearance = validate_appearance(payload)
    appearance = getattr(server, "window_appearance", {"theme": "dark", "workspace": "home"})
    try:
        result = apply_window_appearance(getattr(server, "native_window_object", None), appearance) if getattr(server, "native_window", False) else {"supported": False, "applied": False}
    except (OSError, AttributeError, TypeError, ValueError):
        # Appearance cannot make a working window fail to start.
        result = {"supported": False, "applied": False}
    server.window_appearance_status = result
    return result
