"""Runtime status and deployment"""
from __future__ import annotations
from agent_manager import core as _core

def codex_prefix() -> list[str]:
    explicit = _core._codex_cli_override_path(_core.os.environ.get("CODEX_CLI_PATH"))
    if explicit is not None:
        return [str(explicit)]
    direct = _core.shutil.which("codex.exe")
    if direct:
        return [direct]
    node_runtime = _core._discover_node_npm_runtime()
    if node_runtime:
        node = node_runtime["node"]
        npm_command = node_runtime["npmCommand"]
        flags = getattr(_core.subprocess, "CREATE_NO_WINDOW", 0) if _core.os.name == "nt" else 0
        try:
            completed = _core.subprocess.run(
                [*npm_command, "root", "-g"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                creationflags=flags,
            )
        except (OSError, _core.subprocess.TimeoutExpired):
            completed = None
        if completed is not None and completed.returncode == 0 and completed.stdout.strip():
            cli = _core.Path(completed.stdout.strip()) / "@openai" / "codex" / "bin" / "codex.js"
            if cli.exists():
                return [node, str(cli)]
    fallback = _core.shutil.which("codex.cmd") or _core.shutil.which("codex")
    if fallback:
        try:
            return _core._safe_codex_cli_command(_core.Path(fallback))
        except _core.ManagerError:
            # Continue to native Desktop/managed runtime discovery.  Never
            # return a Windows command wrapper as a direct subprocess target.
            pass
    if _core.os.name == "nt":
        process_candidates = []
        for process in _core.running_codex_processes():
            executable = _core.Path(str(process.get("executable") or ""))
            if executable.name.casefold() == "codex.exe":
                process_candidates.append(executable)
        desktop = _core._detect_codex_windows_app()
        desktop_executable = _core.Path(str((desktop or {}).get("executable") or ""))
        desktop_candidates = []
        if desktop_executable.is_file():
            desktop_candidates.extend(
                (
                    desktop_executable.parent / "resources" / "codex.exe",
                    desktop_executable.parent / "resources" / "codex",
                    desktop_executable.parent / "resources" / "bin" / "codex.exe",
                    desktop_executable.parent / "codex.exe",
                    desktop_executable.parent.parent / "resources" / "codex.exe",
                )
            )
        flags = getattr(_core.subprocess, "CREATE_NO_WINDOW", 0)
        seen = set()
        for candidate in (
            process_candidates
            + _core._manager_downloaded_codex_candidates()
            + _core._desktop_managed_codex_candidates()
            + desktop_candidates
        ):
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            key = str(resolved).casefold()
            if key in seen or not resolved.is_file() or resolved == desktop_executable:
                continue
            seen.add(key)
            try:
                completed = _core.subprocess.run(
                    [str(resolved), "--version"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                    creationflags=flags,
                )
            except (OSError, _core.subprocess.TimeoutExpired):
                continue
            version_text = f"{completed.stdout}\n{completed.stderr}".casefold()
            if completed.returncode == 0 and "codex" in version_text:
                return [str(resolved)]
    raise _core.ManagerError(
        "未找到可用的 Codex 运行时。请安装最新版 Codex 桌面版，"
        "请在设置中使用“扫描并修复”；仍未找到时可一键部署官方 Codex CLI。"
    )



def codex_runtime_status(force: bool = False) -> dict:
    """Return a side-effect-free runtime diagnosis for the settings UI."""
    desktop = _core._detect_codex_windows_app(force=force) if _core.os.name == "nt" else None
    try:
        prefix = _core.codex_prefix()
    except _core.ManagerError as exc:
        prefix = []
        error = str(exc)
    else:
        error = None
    override_diagnosis = _core._codex_cli_override_diagnosis()
    safe_override = _core._codex_cli_override_path(_core.os.environ.get("CODEX_CLI_PATH"))
    source = "missing"
    if prefix:
        first = _core.Path(str(prefix[0]))
        if len(prefix) == 1 and _core._is_manager_downloaded_codex_path(first):
            source = "manager_managed"
        elif len(prefix) == 1 and _core._is_desktop_managed_codex_path(first):
            source = "desktop_managed"
        elif desktop and len(prefix) == 1 and "resources" in {part.casefold() for part in first.parts}:
            source = "desktop_bundled"
        elif safe_override is not None and first == safe_override:
            source = "configured"
        elif len(prefix) > 1:
            source = "npm_javascript"
        else:
            source = "system_path"
    runtime_version = None
    if source == "manager_managed" and prefix:
        # The manager's release directory is named ``<version>-<platform>-<digest>``.
        # Reading it avoids spawning a second process on every status refresh.
        candidate = _core.Path(str(prefix[-1]))
        release_name = candidate.parent.parent.name
        match = _core.re.search(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", release_name)
        runtime_version = match.group(0) if match else None
    return {
        "available": bool(prefix),
        "source": source,
        "command": prefix,
        "version": runtime_version,
        "desktop": desktop,
        "error": error,
        "canAutoDeploy": _core.os.name == "nt",
        "unsafeCliOverride": override_diagnosis,
        "checkedAt": _core.now_iso(),
    }



def _user_environment_value(name: str) -> str:
    if _core.os.name != "nt":
        return str(_core.os.environ.get(name) or "")
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _kind = winreg.QueryValueEx(key, name)
            return str(value or "")
    except (OSError, ImportError):
        return ""



def _configure_windows_runtime_path(directories: list[str | _core.Path]) -> None:
    normalized = _core._dedupe_runtime_paths(directories)
    if not normalized:
        return
    current_user_path = _core._user_environment_value("Path")
    parts = _core._split_runtime_path(current_user_path)
    known = {_core.os.path.normcase(_core.os.path.normpath(_core.os.path.expandvars(item))) for item in parts}
    changed = False
    for directory in normalized:
        key = _core.os.path.normcase(_core.os.path.normpath(directory))
        if key not in known:
            parts.append(directory)
            known.add(key)
            changed = True
    if changed:
        _core._sync_user_environment("Path", _core.os.pathsep.join(parts))
    _core._refresh_windows_process_path(normalized)



def deploy_codex_runtime() -> dict:
    if not _core.CODEX_RUNTIME_DEPLOY_LOCK.acquire(blocking=False):
        raise _core.ManagerError("Codex 运行时正在扫描或部署，请等待当前操作完成。")
    try:
        return _core._deploy_codex_runtime_locked()
    finally:
        _core.CODEX_RUNTIME_DEPLOY_LOCK.release()



def _deploy_codex_runtime_locked() -> dict:
    """Repair discovery, then install an official runtime without manual setup."""
    removed_unsafe_override = _core._clear_unsafe_codex_cli_override()
    status = _core.codex_runtime_status(force=True)
    if status.get("available"):
        cleanup_note = "；已清除会导致 Desktop spawn EINVAL 的旧 CODEX_CLI_PATH" if removed_unsafe_override else ""
        prefix = status.get("command") if isinstance(status.get("command"), list) else []
        if len(prefix) == 1:
            return {
                "installed": False,
                "repaired": True,
                "removedUnsafeCliOverride": removed_unsafe_override,
                "method": status.get("source"),
                "message": f"已找到并启用现有 Codex 运行时{cleanup_note}。",
                "runtime": status,
            }
        if _core.os.name != "nt":
            return {
                "installed": False,
                "repaired": True,
                "method": status.get("source"),
                "message": "已找到并启用现有 Codex 运行时。",
                "runtime": status,
            }
        node_runtime = _core._discover_node_npm_runtime(refresh_registry=True, update_process_path=True)
        if node_runtime:
            existing_cli = _core._locate_npm_codex_cli(list(node_runtime["npmCommand"]))
            if existing_cli is not None:
                version = _core._verify_codex_cli(existing_cli)
                _core._configure_windows_runtime_path([_core.Path(node_runtime["node"]).parent])
                status = _core.codex_runtime_status(force=True)
                if status.get("available"):
                    return {
                        "installed": False,
                        "nodeInstalled": False,
                        "repaired": True,
                        "removedUnsafeCliOverride": removed_unsafe_override,
                        "version": version,
                        "method": "npm_existing",
                        "message": (
                            "已复用现有 npm Codex；未设置 CODEX_CLI_PATH，"
                            "Codex Desktop 将继续使用安装包内的原生 codex.exe。"
                        ),
                        "runtime": status,
                    }
        return {
            "installed": False,
            "repaired": True,
            "removedUnsafeCliOverride": removed_unsafe_override,
            "method": status.get("source"),
            "message": "已找到可用的 Codex JavaScript 运行时；未修改 Codex Desktop 的原生运行时。",
            "runtime": status,
        }

    if _core.os.name != "nt":
        raise _core.ManagerError("一键部署目前仅支持 Windows。")

    flags = getattr(_core.subprocess, "CREATE_NO_WINDOW", 0)
    errors: list[str] = []
    node_runtime = _core._discover_node_npm_runtime(refresh_registry=True, update_process_path=True)
    node_installed = False

    def activate(
        cli_path: _core.Path,
        *,
        installed: bool,
        method: str,
        message: str,
        version: str | None = None,
    ) -> dict:
        verified = version or _core._verify_codex_cli(cli_path)
        _core.invalidate_codex_version_cache()
        ready = _core.codex_runtime_status(force=True)
        if not ready.get("available"):
            raise _core.ManagerError("Codex 运行时已安装，但安全复检失败。")
        return {
            "installed": installed,
            "nodeInstalled": node_installed,
            "repaired": True,
            "removedUnsafeCliOverride": removed_unsafe_override,
            "version": verified,
            "method": method,
            "message": message,
            "runtime": ready,
        }

    def install_with_npm(runtime: dict) -> dict | None:
        node_path = _core.Path(str(runtime["node"]))
        npm_command = list(runtime["npmCommand"])
        _core._configure_windows_runtime_path([node_path.parent])
        existing_cli = _core._locate_npm_codex_cli(npm_command)
        if existing_cli is not None:
            try:
                version = _core._verify_codex_cli(existing_cli)
                return activate(
                    existing_cli,
                    installed=False,
                    method="npm_existing",
                    version=version,
                    message="已找到并启用现有官方 Codex CLI。",
                )
            except _core.ManagerError as exc:
                errors.append(f"现有 npm Codex：{str(exc)[:300]}")
        try:
            completed = _core.subprocess.run(
                [*npm_command, "install", "--global", "@openai/codex@latest"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
                creationflags=flags,
            )
        except (OSError, _core.subprocess.TimeoutExpired) as exc:
            errors.append(f"npm 安装：{str(exc)[:300]}")
            return None
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "未知错误").strip()[-500:]
            errors.append(f"npm 安装：{detail}")
            return None
        cli_path = _core._locate_npm_codex_cli(npm_command)
        if cli_path is None:
            errors.append("npm 安装完成，但没有找到 codex.cmd。")
            return None
        try:
            return activate(
                cli_path,
                installed=True,
                method="npm",
                message=(
                    "官方 npm Codex 已部署；管理器通过 node.exe + codex.js 安全调用，"
                    "不会把 codex.cmd 写入 CODEX_CLI_PATH。"
                ),
            )
        except _core.ManagerError as exc:
            errors.append(f"npm 运行时复检：{str(exc)[:300]}")
            return None

    if node_runtime:
        npm_result = install_with_npm(node_runtime)
        if npm_result:
            return npm_result
    else:
        try:
            direct = _core._download_official_codex_runtime()
            return activate(
                _core.Path(str(direct["path"])),
                installed=bool(direct.get("downloaded")),
                method="official_native_package",
                version=str(direct.get("version") or ""),
                message=(
                    "已直接部署官方 Codex 独立运行时；无需预装 Node.js 或 npm，"
                    "且未修改 Codex Desktop 的 CODEX_CLI_PATH。"
                ),
            )
        except _core.ManagerError as exc:
            errors.append(f"官方独立运行时：{str(exc)[:400]}")

    if not node_runtime:
        winget = _core.shutil.which("winget.exe") or _core.shutil.which("winget")
        if winget:
            commands = (
                [
                    winget,
                    "install",
                    "--id",
                    "OpenJS.NodeJS.LTS",
                    "--exact",
                    "--scope",
                    "user",
                    "--silent",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                    "--disable-interactivity",
                ],
                [
                    winget,
                    "repair",
                    "--id",
                    "OpenJS.NodeJS.LTS",
                    "--exact",
                    "--silent",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                    "--disable-interactivity",
                ],
            )
            for command in commands:
                try:
                    completed = _core.subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=600,
                        creationflags=flags,
                    )
                except (OSError, _core.subprocess.TimeoutExpired) as exc:
                    errors.append(f"Node.js 安装：{str(exc)[:300]}")
                else:
                    if completed.returncode != 0:
                        detail = (completed.stderr or completed.stdout or "未知错误").strip()[-350:]
                        errors.append(f"Node.js 安装：{detail}")
                node_runtime = _core._discover_node_npm_runtime(
                    refresh_registry=True,
                    update_process_path=True,
                )
                if node_runtime:
                    node_installed = True
                    break
        else:
            errors.append("系统未提供 winget。")

    if node_runtime:
        npm_result = install_with_npm(node_runtime)
        if npm_result:
            return npm_result

    try:
        direct = _core._download_official_codex_runtime()
        return activate(
            _core.Path(str(direct["path"])),
            installed=bool(direct.get("downloaded")),
            method="official_native_package",
            version=str(direct.get("version") or ""),
            message=(
                "已通过官方平台包部署 Codex 独立运行时；管理器会直接发现它，"
                "不会修改 Codex Desktop 的 CODEX_CLI_PATH。"
            ),
        )
    except _core.ManagerError as exc:
        errors.append(f"最终独立运行时兜底：{str(exc)[:400]}")

    unique_errors = []
    for error in errors:
        cleaned = _core.re.sub(r"\s+", " ", error).strip()
        if cleaned and cleaned not in unique_errors:
            unique_errors.append(cleaned)
    detail = "；".join(unique_errors[-4:]) or "未知错误"
    raise _core.ManagerError(
        "Codex 运行时自动修复未完成。已依次尝试现有桌面运行时、npm、"
        f"Node.js 修复和官方独立运行时。详情：{detail}"
    )


