"""Node.js runtime discovery and npm integration"""
from __future__ import annotations
from agent_manager import core as _core

def _node_runtime_search_directories() -> list[_core.Path]:
    directories: list[str | _core.Path] = []
    directories.extend(_core._split_runtime_path(_core.os.environ.get("PATH")))
    if _core.os.name == "nt":
        for value in _core._windows_registry_path_values():
            directories.extend(_core._split_runtime_path(value))
    home = _core.app_paths.user_home()
    program_files = _core.Path(_core.os.environ.get("ProgramFiles") or "C:/Program Files")
    program_files_x86 = _core.Path(_core.os.environ.get("ProgramFiles(x86)") or "C:/Program Files (x86)")
    local_app_data = _core.Path(_core.os.environ.get("LOCALAPPDATA") or home / "AppData/Local")
    app_data = _core.Path(_core.os.environ.get("APPDATA") or home / "AppData/Roaming")
    program_data = _core.Path(_core.os.environ.get("ProgramData") or "C:/ProgramData")
    user_profile = _core.Path(_core.os.environ.get("USERPROFILE") or home)
    directories.extend(
        (
            program_files / "nodejs",
            program_files_x86 / "nodejs",
            local_app_data / "Programs" / "nodejs",
            local_app_data / "Microsoft" / "WinGet" / "Links",
            app_data / "npm",
            user_profile / "scoop" / "apps" / "nodejs-lts" / "current",
            user_profile / "scoop" / "apps" / "nodejs" / "current",
            program_data / "chocolatey" / "bin",
        )
    )
    for name in ("NVM_SYMLINK", "VOLTA_HOME", "FNM_MULTISHELL_PATH"):
        value = _core.os.environ.get(name)
        if value:
            path = _core.Path(value)
            directories.append(path / "bin" if name == "VOLTA_HOME" else path)
    dynamic_roots = [
        _core.Path(_core.os.environ.get("NVM_HOME") or app_data / "nvm"),
        local_app_data / "nvm",
        app_data / "fnm" / "node-versions",
        local_app_data / "fnm" / "node-versions",
    ]
    for root in dynamic_roots:
        if not root.is_dir():
            continue
        for candidate in root.glob("v*"):
            directories.extend((candidate, candidate / "installation"))
    winget_packages = local_app_data / "Microsoft" / "WinGet" / "Packages"
    if winget_packages.is_dir():
        for package in winget_packages.glob("OpenJS.NodeJS*"):
            try:
                directories.extend(item.parent for item in package.rglob("node.exe"))
            except OSError:
                continue
    return [_core.Path(item) for item in _core._dedupe_runtime_paths(directories)]



def _discover_node_npm_runtime(refresh_registry: bool = False, update_process_path: bool = False) -> dict | None:
    if refresh_registry:
        _core._refresh_windows_process_path()
    node_candidates: list[_core.Path] = []
    npm_candidates: list[_core.Path] = []
    npm_cli_candidates: list[_core.Path] = []
    direct_node = _core.shutil.which("node.exe") or _core.shutil.which("node")
    direct_npm = _core.shutil.which("npm.cmd") or _core.shutil.which("npm")
    if direct_node:
        node_candidates.append(_core.Path(direct_node))
    if direct_npm:
        npm_candidates.append(_core.Path(direct_npm))
    for directory in _core._node_runtime_search_directories():
        node_candidates.extend((directory / "node.exe", directory / "node"))
        npm_candidates.extend((directory / "npm.cmd", directory / "npm"))
        npm_cli_candidates.append(directory / "node_modules" / "npm" / "bin" / "npm-cli.js")
    nodes = [item for item in node_candidates if item.is_file()]
    npms = [item for item in npm_candidates if item.is_file()]
    npm_clis = [item for item in npm_cli_candidates if item.is_file()]
    if not nodes or (not npms and not npm_clis):
        return None
    node = nodes[0]
    npm = next((item for item in npms if item.parent == node.parent), npms[0] if npms else None)
    npm_cli = next(
        (item for item in npm_clis if item.parents[3] == node.parent),
        npm_clis[0] if npm_clis else None,
    )
    npm_command = [str(npm)] if npm else [str(node), str(npm_cli)]
    if update_process_path:
        directories = [node.parent]
        if npm:
            directories.append(npm.parent)
        _core._refresh_windows_process_path(directories)
    return {"node": str(node), "npm": str(npm or npm_cli), "npmCommand": npm_command}



def _npm_global_prefix(npm_command: list[str]) -> _core.Path | None:
    flags = getattr(_core.subprocess, "CREATE_NO_WINDOW", 0) if _core.os.name == "nt" else 0
    try:
        completed = _core.subprocess.run(
            [*npm_command, "prefix", "--global"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=flags,
        )
    except (OSError, _core.subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    return _core.Path(completed.stdout.strip())



def _locate_npm_codex_cli(npm_command: list[str]) -> _core.Path | None:
    prefix_dir = _core._npm_global_prefix(npm_command)
    app_data = _core.Path(_core.os.environ.get("APPDATA") or _core.app_paths.user_home() / "AppData/Roaming")
    candidates = [
        (prefix_dir / "node_modules" / "@openai" / "codex" / "bin" / "codex.js") if prefix_dir else None,
        app_data / "npm" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js",
        (prefix_dir / "codex.cmd") if prefix_dir else None,
        (prefix_dir / "codex.exe") if prefix_dir else None,
        app_data / "npm" / "codex.cmd",
        app_data / "npm" / "codex.exe",
    ]
    return next((item for item in candidates if item is not None and item.is_file()), None)



def _safe_codex_cli_command(cli_path: _core.Path) -> list[str]:
    """Resolve an npm wrapper to node.exe + codex.js without spawning it.

    ``.cmd``/``.bat``/``.ps1`` files are never valid direct child-process
    executables for Codex Desktop and are unreliable with CreateProcess.  They
    may be used only as a location hint for the real JavaScript entry point.
    """
    path = _core.Path(cli_path)
    suffix = path.suffix.casefold()
    if suffix not in _core._UNSAFE_CODEX_CLI_OVERRIDE_SUFFIXES and suffix != ".js":
        return [str(path)]
    js_candidates = []
    if suffix == ".js":
        js_candidates.append(path)
    js_candidates.extend(
        (
            path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js",
            path.parent.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js",
        )
    )
    js_path = next((candidate for candidate in js_candidates if candidate.is_file()), None)
    runtime = _core._discover_node_npm_runtime()
    node = _core.Path(str((runtime or {}).get("node") or ""))
    if js_path is None or not node.is_file():
        raise _core.ManagerError(
            "发现 npm Codex 包装脚本，但无法解析 node.exe + codex.js；"
            "为避免 Windows spawn EINVAL，未直接执行包装脚本。"
        )
    return [str(node), str(js_path)]

