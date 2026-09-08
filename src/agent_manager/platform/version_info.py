"""Read our release generation from Windows metadata without running an EXE."""
import ctypes
from ctypes import wintypes
import os


def executable_release_epoch(path) -> int:
    if os.name != "nt":
        return 0
    try:
        version = ctypes.WinDLL("version", use_last_error=True)
        version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        version.GetFileVersionInfoW.restype = wintypes.BOOL
        version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
        version.VerQueryValueW.restype = wintypes.BOOL
        size = version.GetFileVersionInfoSizeW(str(path), None)
        if not size or size > 1024 * 1024:
            return 0
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return 0
        value = ctypes.c_void_p(); length = wintypes.UINT()
        if not version.VerQueryValueW(buffer, r"\StringFileInfo\040904B0\ReleaseEpoch", ctypes.byref(value), ctypes.byref(length)):
            return 0
        text = ctypes.wstring_at(value.value, max(0, length.value - 1))
        return int(text) if text.isdigit() and len(text) <= 3 else 0
    except (OSError, ValueError):
        return 0
