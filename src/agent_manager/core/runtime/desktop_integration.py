"""Desktop integration and candidates"""
from __future__ import annotations
from agent_manager import core as _core

def install_codex_cli_latest() -> dict:
    """Install the standalone official CLI, even when Desktop has a runtime."""
    if not _core.CODEX_RUNTIME_DEPLOY_LOCK.acquire(blocking=False):
        raise _core.ManagerError("Codex CLI 正在安装，请等待当前操作完成。")
    try:
        if _core.os.name != "nt":
            raise _core.ManagerError("官方独立 Codex CLI 自动安装目前仅支持 Windows。")
        flags = getattr(_core.subprocess, "CREATE_NO_WINDOW", 0)
        errors: list[str] = []
        runtime = _core._discover_node_npm_runtime(refresh_registry=True, update_process_path=True)
        if runtime:
            npm_command = list(runtime.get("npmCommand") or [])
            if npm_command:
                node_value = str(runtime.get("node") or "").strip()
                if node_value:
                    _core._configure_windows_runtime_path([_core.Path(node_value).parent])
                try:
                    completed = _core.subprocess.run(
                        [*npm_command, "install", "--global", "@openai/codex@latest",
                         "--registry=https://registry.npmjs.org", "--no-audit", "--no-fund"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=900, creationflags=flags,
                    )
                except (OSError, _core.subprocess.TimeoutExpired) as exc:
                    errors.append(f"npm 安装：{str(exc)[:300]}")
                else:
                    if completed.returncode == 0:
                        cli_path = _core._locate_npm_codex_cli(npm_command)
                        if cli_path is not None:
                            try:
                                version = _core._verify_codex_cli(cli_path)
                            except _core.ManagerError as exc:
                                errors.append(f"npm CLI 复检：{str(exc)[:300]}")
                            else:
                                _core.invalidate_codex_version_cache()
                                return {
                                    "installed": True,
                                    "method": "npm",
                                    "version": version,
                                    "path": str(cli_path.resolve()),
                                    "message": "已通过官方 npm 安装最新版 Codex CLI。",
                                }
                        else:
                            errors.append("npm 安装完成，但没有找到 Codex CLI。")
                    else:
                        detail = (completed.stderr or completed.stdout or "未知错误").strip()[-500:]
                        errors.append(f"npm 安装：{detail}")
        try:
            direct = _core._download_official_codex_runtime()
            _core.invalidate_codex_version_cache()
            return {
                "installed": bool(direct.get("downloaded")),
                "method": "official_native_package",
                "version": str(direct.get("version") or ""),
                "path": str(direct.get("path") or ""),
                "message": "已下载并校验官方最新版 Codex CLI，无需预装 Node.js 或 npm。",
            }
        except _core.ManagerError as exc:
            errors.append(f"官方平台包：{str(exc)[:400]}")
        raise _core.ManagerError(
            "自动安装 Codex CLI 失败：" + ("；".join(errors)[:700] or "没有找到可用的安装方式。")
        )
    finally:
        _core.CODEX_RUNTIME_DEPLOY_LOCK.release()



def _desktop_managed_codex_candidates() -> list[_core.Path]:
    """Find the versioned runtime downloaded by current Codex/ChatGPT desktop builds."""
    if _core.os.name != "nt":
        return []
    local_app_data = _core.Path(_core.os.environ.get("LOCALAPPDATA") or _core.app_paths.user_home() / "AppData/Local")
    roaming_app_data = _core.Path(_core.os.environ.get("APPDATA") or _core.app_paths.user_home() / "AppData/Roaming")
    roots = [
        local_app_data / "OpenAI" / "Codex" / "bin",
        local_app_data / "OpenAI" / "ChatGPT" / "bin",
        roaming_app_data / "OpenAI" / "Codex" / "bin",
        roaming_app_data / "OpenAI" / "ChatGPT" / "bin",
    ]
    candidates: list[_core.Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        candidates.extend(root.glob("*/codex.exe"))
        candidates.extend((root / "codex.exe", root / "current" / "codex.exe"))
    unique: dict[str, _core.Path] = {}
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            if not resolved.is_file():
                continue
        except OSError:
            continue
        unique[str(resolved).casefold()] = resolved
    def modified(item: _core.Path) -> float:
        try:
            return item.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(unique.values(), key=modified, reverse=True)



def _is_desktop_managed_codex_path(path: _core.Path) -> bool:
    lowered = [part.casefold() for part in path.parts]
    return "openai" in lowered and "bin" in lowered and path.name.casefold() == "codex.exe"



def _is_manager_downloaded_codex_path(path: _core.Path) -> bool:
    try:
        path.resolve().relative_to(_core.MANAGED_CODEX_RUNTIME_DIR.resolve())
    except (OSError, ValueError):
        return False
    return path.name.casefold() == "codex.exe"



def _codex_cli_override_path(raw: str | None) -> _core.Path | None:
    """Accept only a user-supplied native executable as a CLI override.

    Codex Desktop passes ``CODEX_CLI_PATH`` directly to Node's ``spawn``.
    Windows command wrappers such as ``codex.cmd`` are therefore not valid
    Desktop runtimes and can fail with ``spawn EINVAL``.  The manager may use
    npm through ``node.exe + codex.js`` internally, but must never expose a
    wrapper through this environment variable.
    """
    value = str(raw or "").strip().strip('"')
    if not value:
        return None
    try:
        candidate = _core.Path(_core.os.path.expandvars(value)).expanduser()
    except (OSError, ValueError):
        return None
    if candidate.suffix.casefold() != ".exe":
        return None
    try:
        return candidate.resolve() if candidate.is_file() else None
    except OSError:
        return None



def _codex_cli_override_diagnosis() -> dict:
    process_value = str(_core.os.environ.get("CODEX_CLI_PATH") or "").strip()
    user_value = str(_core._user_environment_value("CODEX_CLI_PATH") or "").strip()
    unsafe = []
    for scope, value in (("process", process_value), ("user", user_value)):
        if not value or _core._codex_cli_override_path(value) is not None:
            continue
        suffix = _core.Path(value.strip('"')).suffix.casefold()
        reason = "windows_command_wrapper" if suffix in _core._UNSAFE_CODEX_CLI_OVERRIDE_SUFFIXES else "not_native_executable"
        unsafe.append({"scope": scope, "path": value, "reason": reason})
    return {
        "detected": bool(unsafe),
        "items": unsafe,
        "warning": (
            "检测到 CODEX_CLI_PATH 指向非原生可执行文件。Codex Desktop 直接启动此路径时"
            "可能出现 spawn EINVAL；请使用“扫描并修复”清除该旧配置。"
            if unsafe
            else None
        ),
    }



def _clear_unsafe_codex_cli_override() -> bool:
    """Remove a poisoned legacy override during an explicit repair action.

    This function deliberately never assigns CODEX_CLI_PATH.  A valid native
    executable chosen by the user is preserved; only wrappers, missing paths,
    and other non-native values are removed.
    """
    diagnosis = _core._codex_cli_override_diagnosis()
    if not diagnosis["detected"]:
        return False
    unsafe_scopes = {str(item.get("scope")) for item in diagnosis["items"]}
    if "user" in unsafe_scopes:
        _core._remove_user_environment("CODEX_CLI_PATH")
    else:
        _core.os.environ.pop("CODEX_CLI_PATH", None)
    return True

