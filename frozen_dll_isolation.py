"""Keep onefile DLL directories process-local instead of leaking into children.

PyInstaller on Windows calls SetDllDirectoryW(_MEIPASS), which external child
processes inherit. A long-lived ChatGPT child was observed loading the manager's
temporary VCRUNTIME140.dll, preventing bootloader cleanup. This startup-only
policy uses non-inherited AddDllDirectory cookies, preserves them for process
lifetime, then clears SetDllDirectory exactly once before application threads.
It does not unload DLLs, terminate processes, delete files, or suppress warnings.

Reference: https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html
section "Launching External Programs from the Frozen Application / Windows".
"""
from __future__ import annotations

import ctypes
import functools
import ntpath
import os
from pathlib import Path
import platform
import subprocess
import sys

_DLL_DIRECTORY_HANDLES: list = []
_INSTALLED = False
_ORIGINAL_POPEN_INIT = None
LOAD_LIBRARY_SEARCH_DEFAULT_DIRS = 0x1000


def runtime_status() -> dict:
    """Credential-free diagnostic; never returns the temporary directory path."""
    result = {"installed": _INSTALLED, "retainedDirectories": len(_DLL_DIRECTORY_HANDLES), "inheritedDllDirectoryEmpty": None}
    if _INSTALLED and sys.platform == 'win32':
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        getter = kernel32.GetDllDirectoryW
        getter.argtypes = [ctypes.c_ulong, ctypes.c_wchar_p]
        getter.restype = ctypes.c_ulong
        # A zero-sized query returns the required buffer size (1 even for an
        # empty directory), not the copied string length. Read into a buffer.
        buffer = ctypes.create_unicode_buffer(32768)
        ctypes.set_last_error(0)
        length = getter(len(buffer), buffer)
        result['inheritedDllDirectoryEmpty'] = (not buffer.value) if length or not ctypes.get_last_error() else None
    return result


def _inside_windows_path(candidate: str, root: str) -> bool:
    candidate = ntpath.normcase(ntpath.abspath(candidate.strip().strip('"')))
    root = ntpath.normcase(ntpath.abspath(root)).rstrip('\\/')
    return candidate == root or candidate.startswith(root + '\\')


def sanitize_environment(environment: dict, bundle_root: str) -> dict:
    """Copy child environment and remove only PATH entries inside this bundle.

    Third-party runtimes (including pywebview) may append native DLL directories
    to PATH after our early startup hook. Filtering at Popen time handles that
    without mutating the parent's environment or its process-global DLL state.
    PyInstaller's private variables and explicit restart settings are preserved.
    """
    result = dict(environment)
    for key, value in list(result.items()):
        if key.casefold() == 'path' and isinstance(value, str):
            result[key] = ';'.join(entry for entry in value.split(';')
                                   if not entry.strip() or not _inside_windows_path(entry, bundle_root))
    return result


def dll_directories(bundle_root: Path, *, machine: str | None = None,
                    pointer_bits: int | None = None) -> list[Path]:
    """Add only compatible native folders; never mix x86/x64/ARM loader DLLs."""
    machine = (machine or platform.machine()).casefold()
    pointer_bits = pointer_bits or ctypes.sizeof(ctypes.c_void_p) * 8
    arch = 'arm64' if machine in {'arm64', 'aarch64'} and pointer_bits == 64 else 'x64' if pointer_bits == 64 else 'x86'
    clr_arch = 'amd64' if arch == 'x64' else arch
    candidates = [bundle_root, bundle_root / 'webview' / 'lib',
                  bundle_root / 'webview' / 'lib' / 'runtimes' / f'win-{arch}' / 'native',
                  bundle_root / 'clr_loader' / 'ffi' / 'dlls' / clr_arch,
                  bundle_root / 'pythonnet' / 'runtime']
    return [path for path in candidates if path.is_dir()]


def install() -> bool:
    """Install once, only inside a frozen Windows process, before app imports.

    Failure is explicit: silently continuing would recreate the DLL leak.
    SetDefaultDllDirectories and AddDllDirectory affect this process only;
    unlike SetDllDirectory, their added directories are not inherited.
    """
    global _INSTALLED, _ORIGINAL_POPEN_INIT
    if _INSTALLED:
        return True
    if sys.platform != 'win32' or not getattr(sys, 'frozen', False):
        return False
    raw_root = getattr(sys, '_MEIPASS', None)
    if not raw_root or not Path(raw_root).is_absolute() or not Path(raw_root).is_dir():
        raise RuntimeError('Frozen application DLL directory is unavailable')
    bundle_root = Path(raw_root)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.SetDefaultDllDirectories.argtypes = [ctypes.c_ulong]
    kernel32.SetDefaultDllDirectories.restype = ctypes.c_int
    kernel32.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
    kernel32.SetDllDirectoryW.restype = ctypes.c_int
    handles = []
    try:
        for directory in dll_directories(bundle_root):
            handles.append(os.add_dll_directory(str(directory)))
        if not kernel32.SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel32.SetDllDirectoryW(None):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        for handle in handles:
            handle.close()
        raise
    _DLL_DIRECTORY_HANDLES.extend(handles)

    # Do not change SetDllDirectory around Popen: that would race concurrent
    # imports, CLR/WebView startup and other process launches. Only copy env.
    original_init = subprocess.Popen.__init__
    @functools.wraps(original_init)
    def isolated_init(instance, *args, **kwargs):
        if len(args) > 10:  # Popen's positional `env` parameter
            values = list(args)
            values[10] = sanitize_environment(os.environ if values[10] is None else values[10], str(bundle_root))
            args = tuple(values)
        else:
            env = kwargs.get('env')
            kwargs['env'] = sanitize_environment(os.environ if env is None else env, str(bundle_root))
        return original_init(instance, *args, **kwargs)
    _ORIGINAL_POPEN_INIT = original_init
    subprocess.Popen.__init__ = isolated_init
    _INSTALLED = True
    return True
