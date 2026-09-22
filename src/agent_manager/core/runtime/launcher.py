"""Codex launcher and environment setup"""
from __future__ import annotations
from agent_manager import core as _core

def _detect_codex_windows_app(force: bool = False) -> dict | None:
    """Serialize AppX discovery and coalesce simultaneous forced refreshes."""

    requested_at = _core.time.monotonic()
    with _core.CODEX_WINDOWS_APP_CACHE_LOCK:
        refreshed_while_waiting = bool(
            force and float(_core.CODEX_WINDOWS_APP_CACHE.get("at") or 0.0) >= requested_at
        )
        return _core._detect_codex_windows_app_locked(force=force and not refreshed_while_waiting)



def _detect_codex_windows_app_locked(force: bool = False) -> dict | None:
    if _core.os.name != "nt":
        return None
    discovery_file = _core.STATE_DIR / "codex-windows-app.json"

    def durable_record(value: Any) -> dict | None:
        if not isinstance(value, dict):
            return None
        app_id = str(value.get("appUserModelId") or "").strip()
        executable = _core.Path(str(value.get("executable") or "").strip())
        valid_app_id = bool(
            _core.re.fullmatch(r"[A-Za-z0-9._-]+_[A-Za-z0-9]+![A-Za-z0-9._-]+", app_id)
        )
        valid_executable = (
            executable.name.casefold() in {"chatgpt.exe", "codex.exe", "openai.codex.exe"}
            and executable.is_file()
        )
        if not valid_app_id and not valid_executable:
            return None
        return {
            "appUserModelId": app_id if valid_app_id else "",
            "executable": str(executable) if valid_executable else str(value.get("executable") or ""),
            "package": str(value.get("package") or ""),
            "version": str(value.get("version") or ""),
            **({"source": str(value.get("source"))} if value.get("source") else {}),
        }

    cached = _core.CODEX_WINDOWS_APP_CACHE.get("value")
    cache_scope = str(_core.STATE_DIR.resolve())
    if (
        not force and cached is None
        and _core.CODEX_WINDOWS_APP_CACHE.get("scope") == cache_scope
        and 0 <= _core.time.monotonic() - float(_core.CODEX_WINDOWS_APP_CACHE.get("at") or 0) < 30.0
    ):
        return None
    cached_valid = durable_record(cached)
    if cached_valid and not _core.Path(str(cached_valid.get("executable") or "")).is_file() and not discovery_file.is_file():
        cached_valid = None
    if cached_valid is None:
        try:
            cached_valid = durable_record(_core.read_json(discovery_file, None))
        except (_core.ManagerError, OSError):
            cached_valid = None
    # The AppUserModelId is stable across Store updates. Reuse the validated
    # in-memory or durable record until an explicit refresh or a launch fallback
    # proves it stale; Get-AppxPackage costs noticeable time on every cold open.
    if not force and cached_valid:
        _core.CODEX_WINDOWS_APP_CACHE.update({"at": _core.time.monotonic(), "value": cached_valid})
        return dict(cached_valid)
    flags = getattr(_core.subprocess, "CREATE_NO_WINDOW", 0)
    script = (
        "$ErrorActionPreference='SilentlyContinue'; $records=@(); "
        "$packages=@(Get-AppxPackage | Where-Object { "
        "$_.Name -match '(?i)(openai|codex|chatgpt)' -or "
        "$_.PackageFamilyName -match '(?i)(openai|codex|chatgpt)' } | Sort-Object Version -Descending); "
        "foreach($pkg in $packages){ try { "
        "$manifest=Get-AppxPackageManifest -Package $pkg.PackageFullName -ErrorAction Stop; "
        "foreach($app in @($manifest.Package.Applications.Application)){ "
        "$id=[string]$app.Id; $exe=[string]$app.Executable; "
        "if($id -and $exe){ $path=Join-Path $pkg.InstallLocation ($exe -replace '/','\\'); "
        "$records += [pscustomobject]@{ appUserModelId=\"$($pkg.PackageFamilyName)!$id\"; "
        "executable=$path; exists=(Test-Path -LiteralPath $path); package=$pkg.PackageFullName; "
        "version=[string]$pkg.Version; applicationId=$id } } } } catch { continue } }; "
        "$records | ConvertTo-Json -Compress"
    )
    try:
        completed = _core.subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            creationflags=flags,
        )
        payload = _core.json.loads(completed.stdout.strip() or "[]") if completed.returncode == 0 else []
        records = payload if isinstance(payload, list) else [payload] if isinstance(payload, dict) else []
        selected = None
        for item in records:
            if not isinstance(item, dict) or not item.get("exists"):
                continue
            app_id = str(item.get("appUserModelId") or "").strip()
            executable = _core.Path(str(item.get("executable") or "").strip())
            if not _core.re.fullmatch(r"[A-Za-z0-9._-]+_[A-Za-z0-9]+![A-Za-z0-9._-]+", app_id):
                continue
            if executable.name.casefold() not in {"chatgpt.exe", "codex.exe", "openai.codex.exe"}:
                continue
            if not executable.is_file():
                continue
            selected = {
                "appUserModelId": app_id,
                "executable": str(executable),
                "package": str(item.get("package") or ""),
                "version": str(item.get("version") or ""),
            }
            break
    except (OSError, _core.subprocess.TimeoutExpired, _core.json.JSONDecodeError):
        selected = None
    if selected is None:
        # This is the lower-level OS scan. Calling running_codex_processes here
        # would recurse back into installation discovery on a fresh computer.
        processes = _core._running_windows_codex_candidates()
        process_ids = {str(item.get("pid") or "") for item in processes}
        roots = [
            item
            for item in processes
            if str(item.get("parentPid") or "") not in process_ids
            and str(item.get("name") or "").casefold() in {"chatgpt.exe", "openai.codex.exe", "codex.exe"}
        ]
        roots.sort(key=lambda item: str(item.get("name") or "").casefold() == "codex.exe")
        for item in roots:
            executable = _core.Path(str(item.get("executable") or ""))
            if not executable.is_file():
                continue
            if executable.name.casefold() == "codex.exe":
                bundled = executable.parent / "resources" / "codex.exe"
                if not bundled.is_file() or bundled.resolve() == executable.resolve():
                    continue
            selected = {
                "appUserModelId": "",
                "executable": str(executable),
                "package": "",
                "version": "",
                "source": "running_process",
            }
            break
    if selected is None and cached_valid:
        selected = cached_valid
    if selected and selected.get("appUserModelId"):
        try:
            _core.atomic_write_json(discovery_file, {**selected, "detectedAt": _core.now_iso()})
        except OSError:
            pass
    _core.CODEX_WINDOWS_APP_CACHE.update({"at": _core.time.monotonic(), "value": selected, "scope": cache_scope})
    return dict(selected) if selected else None



def _recent_codex_workspace() -> _core.Path | None:
    explicit = str(_core.os.environ.get("CODEX_WORKSPACE_PATH") or "").strip()
    if explicit:
        candidate = _core.Path(explicit).expanduser()
        if candidate.is_dir():
            return candidate.resolve()
    for database in _core._session_database_candidates():
        connection = None
        try:
            connection = _core.sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=1)
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(threads)").fetchall()}
            if "cwd" not in columns:
                continue
            order_column = next((name for name in ("updated_at", "created_at") if name in columns), None)
            order_clause = f" ORDER BY {order_column} DESC" if order_column else ""
            rows = connection.execute(
                "SELECT DISTINCT cwd FROM threads WHERE cwd IS NOT NULL AND TRIM(cwd) <> ''"
                + order_clause
                + " LIMIT 50"
            ).fetchall()
            for (raw_path,) in rows:
                candidate = _core.Path(str(raw_path)).expanduser()
                if candidate.is_dir():
                    return candidate.resolve()
        except _core.sqlite3.Error:
            continue
        finally:
            if connection:
                connection.close()
    return None



def resolve_codex_launch_plan(command_prefix: list[str] | None = None) -> dict:
    """Resolve a safe desktop launch before closing the currently running app."""
    unsafe_override = _core._codex_cli_override_diagnosis()
    if unsafe_override.get("detected"):
        raise _core.ManagerError(
            "检测到 CODEX_CLI_PATH 指向 codex.cmd 或其他非原生文件。为避免 Codex Desktop "
            "出现 spawn EINVAL，本次不会关闭或重启 Codex；请先在设置中执行“扫描并修复”。"
        )
    windows_app = _core._detect_codex_windows_app() or _core._detect_codex_windows_app(force=True)
    if windows_app:
        strategy = "windows_app" if windows_app.get("appUserModelId") else "desktop_executable"
        workspace = _core._recent_codex_workspace()
        return {
            "strategy": strategy,
            **windows_app,
            "refreshBeforeLaunch": True,
            # The GUI launcher does not consume this field, but the App Server
            # readiness probe does: it must load requirements from the same
            # workspace as the desktop session.
            **({"workspace": str(workspace)} if workspace else {}),
        }
    workspace = _core._recent_codex_workspace()
    if not workspace:
        raise _core.ManagerError(
            "未找到可恢复的 Codex 工作区。为避免从管理器目录创建新项目，"
            "请先手动打开一次已有项目，或设置 CODEX_WORKSPACE_PATH。"
        )
    prefix = list(command_prefix or _core.codex_prefix())
    if not prefix:
        raise _core.ManagerError("Codex 启动命令为空。")
    return {
        "strategy": "cli_workspace",
        "command": prefix + ["app", str(workspace)],
        "workspace": str(workspace),
    }



def _codex_runtime_environment(env_overrides: dict | None = None, *, official: bool = False) -> dict:
    """Construct a child-only environment; never publish or persist credentials."""
    env = _core.os.environ.copy()
    for key, value in (env_overrides or {}).items():
        if isinstance(key, str) and key and isinstance(value, str):
            env[key] = value
    blocked = {"codex_cli_path", "codex_home"}
    if official:
        blocked.update(name.casefold() for name in _core._OFFICIAL_AUTH_ENV_OVERRIDES)
        try:
            settings = _core.load_settings()
            blocked.update(
                str(provider.get("envKey") or "").casefold()
                for provider in settings.get("providers", [])
                if isinstance(provider, dict) and provider.get("envKey")
            )
        except Exception:
            # Environment construction must remain available during first-run
            # setup when settings have not been created yet.
            pass
    for key in list(env):
        if key.casefold() in blocked:
            env.pop(key, None)
    env["CODEX_HOME"] = str(_core.CODEX_HOME)
    return env



def _codex_launch_probe_prefix(plan: dict | None) -> list[str]:
    """Prefer the selected desktop's bundled App Server over an unrelated PATH CLI."""
    plan = plan or {}
    desktop = _core.Path(str(plan.get("executable") or ""))

    def safe_prefix(prefix: object) -> list[str]:
        if not isinstance(prefix, (list, tuple)) or not prefix:
            return []
        values = [str(item) for item in prefix if str(item)]
        if not values or _is_windows_store_path(_core.Path(values[0])):
            return []
        return values

    explicit = plan.get("appServerExecutable")
    explicit_path = _core.Path(str(explicit or ""))
    if explicit:
        candidate = _core.Path(str(explicit))
        if (
            candidate.is_file()
            and candidate.name.casefold() in {"codex.exe", "codex"}
            and candidate != desktop
            and not _is_windows_store_path(candidate)
        ):
            return [str(candidate)]
    # Store GUI binaries are activated through their package identity. Their
    # sibling helper can be present yet refuse a direct stdio launch with
    # ERROR_ACCESS_DENIED; prefer a validated standalone/native CLI when one
    # is available. The protected sibling is never used as a direct stdio
    # process, even when it exists on disk.
    store_path = _is_windows_store_path(desktop) or _is_windows_store_path(explicit_path)
    if store_path:
        try:
            native_prefix = _core.codex_prefix()
        except Exception:
            native_prefix = []
        safe_native_prefix = safe_prefix(native_prefix)
        if safe_native_prefix and safe_native_prefix != [str(desktop)]:
            return safe_native_prefix
    candidates = [plan.get("appServerExecutable")]
    if desktop.is_file():
        candidates.extend((desktop.parent / "resources" / "codex.exe",
                           desktop.parent / "resources" / "bin" / "codex.exe",
                           desktop.parent / "resources" / "codex",
                           desktop.parent / "codex.exe"))
    for raw in candidates:
        if raw:
            candidate = _core.Path(raw)
            if (
                candidate.is_file()
                and candidate.name.casefold() in {"codex.exe", "codex"}
                and candidate != desktop
                and not _is_windows_store_path(candidate)
            ):
                return [str(candidate)]
    command = plan.get("command")
    if plan.get("strategy") == "cli_workspace" and isinstance(command, list) and "app" in command:
        workspace_prefix = safe_prefix(command[:command.index("app")])
        if workspace_prefix:
            return workspace_prefix
        if store_path:
            raise _core.ManagerError(
                "Codex 商店包的工作区启动命令仍指向受保护的 WindowsApps 目录；"
                "未找到可用的独立原生 CLI 回退。"
            )
    fallback_error = None
    try:
        raw_fallback = _core.codex_prefix()
    except Exception as exc:
        raw_fallback = []
        fallback_error = exc
    fallback = safe_prefix(raw_fallback)
    if fallback:
        return fallback
    if store_path or (
        isinstance(raw_fallback, (list, tuple))
        and raw_fallback
        and _is_windows_store_path(_core.Path(str(raw_fallback[0])))
    ):
        raise _core.ManagerError(
            "Codex 商店包的 App Server 位于受保护的 WindowsApps 目录，不能直接启动；"
            "未找到可用的独立原生 CLI 回退。"
        )
    if fallback_error is not None:
        raise fallback_error
    return list(raw_fallback) if isinstance(raw_fallback, (list, tuple)) else []


def _is_windows_store_path(path: _core.Path) -> bool:
    try:
        return any(str(part).casefold() == "windowsapps" for part in path.parts)
    except Exception:
        return False


def _safe_cli_workspace_prefix(
    plan: dict | None,
    command_prefix: list[str] | None = None,
) -> list[str]:
    """Choose a standalone CLI prefix without crossing the Store package boundary."""

    plan = plan or {}
    candidates: list[object] = []
    if command_prefix:
        candidates.append(command_prefix)
    command = plan.get("command")
    if isinstance(command, list) and "app" in command:
        candidates.append(command[: command.index("app")])
    try:
        candidates.append(_core.codex_prefix())
    except Exception:
        pass
    for raw in candidates:
        if not isinstance(raw, (list, tuple)) or not raw:
            continue
        prefix = [str(item) for item in raw if str(item)]
        if prefix and not _is_windows_store_path(_core.Path(prefix[0])):
            return prefix
    return []



def _codex_source_environment(plan: dict | None, env_overrides: dict | None = None) -> dict:
    plan = plan or {}
    provider_id = str(plan.get("apiProviderId") or "")
    isolated = bool(plan.get("officialAccountId") or provider_id)
    env = _core._codex_runtime_environment(env_overrides, official=isolated)
    if provider_id:
        provider = _core.provider_by_id(provider_id)
        name = _core._validate_provider_env_key(str(provider.get("envKey") or ""))
        env[name] = _core.load_provider_key(provider_id, required=True)
    return env


def _powershell_single_quote(value: object) -> str:
    return "'" + str(value or "").replace("'", "''") + "'"


def _powershell_encoded_command(script: str) -> str:
    return _core.base64.b64encode(str(script).encode("utf-16le")).decode("ascii")


def _launch_codex_via_package_identity(
    plan: dict,
    env: dict,
    env_overrides: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Start a Store app with a custom environment inside its package identity.

    Explorer's ``shell:AppsFolder`` activation is ideal for the default
    profile, but Windows does not forward a caller's ``CODEX_HOME`` or provider
    environment through that shell boundary.  Cockpit's supported path is to
    run a small PowerShell child through ``Invoke-CommandInDesktopPackage`` and
    create the GUI process with ``UseShellExecute = false`` inside the package.
    """

    app_id = str(plan.get("appUserModelId") or "").strip()
    family, separator, application_id = app_id.partition("!")
    executable = _core.Path(str(plan.get("executable") or "")).resolve()
    if not family or not separator or not application_id or not executable.is_file():
        return False, "商店包身份启动参数不完整。"
    pairs: dict[str, str] = {}
    codex_home = str(env.get("CODEX_HOME") or "").strip()
    if codex_home:
        pairs["CODEX_HOME"] = codex_home
    provider_id = str(plan.get("apiProviderId") or "").strip()
    if provider_id:
        try:
            provider = _core.provider_by_id(provider_id)
            key_name = _core._validate_provider_env_key(str(provider.get("envKey") or ""))
        except Exception as exc:
            return False, f"Provider 环境变量无效：{_core._redact_sensitive_text(exc, limit=180)}"
        if key_name and env.get(key_name):
            pairs[key_name] = str(env[key_name])
    for key, value in (env_overrides or {}).items():
        if (
            isinstance(key, str)
            and key
            and isinstance(value, str)
            and key.casefold() not in {item.casefold() for item in _core._OFFICIAL_AUTH_ENV_OVERRIDES}
            and key.casefold() not in {"codex_home", "codex_cli_path"}
        ):
            pairs[key] = value
    env_lines = "\n".join(
        f"$env:{key}={_powershell_single_quote(value)}"
        for key, value in pairs.items()
    )
    inner = (
        "$ErrorActionPreference='Stop'\n"
        f"{env_lines}\n"
        "$psi=New-Object System.Diagnostics.ProcessStartInfo\n"
        f"$psi.FileName={_powershell_single_quote(executable)}\n"
        "$psi.UseShellExecute=$false\n"
        "[void][System.Diagnostics.Process]::Start($psi)\n"
    )
    encoded = _powershell_encoded_command(inner)
    outer = (
        "$ErrorActionPreference='Stop'\n"
        "$pkg=Get-AppxPackage | Where-Object { $_.PackageFamilyName -ieq "
        f"{_powershell_single_quote(family)} }} | Select-Object -First 1\n"
        "if (-not $pkg) { throw '未找到匹配的 Codex 商店包' }\n"
        "Invoke-CommandInDesktopPackage "
        "-PackageFamilyName $pkg.PackageFamilyName "
        f"-AppId {_powershell_single_quote(application_id)} "
        "-Command 'powershell.exe' "
        f"-Args '-NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}'\n"
    )
    powershell = _core.shutil.which("powershell.exe") or str(
        _core.Path(_core.os.environ.get("WINDIR") or "C:/Windows") / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    flags = getattr(_core.subprocess, "CREATE_NO_WINDOW", 0) if _core.os.name == "nt" else 0
    try:
        completed = _core.subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", outer],
            stdin=_core.subprocess.DEVNULL,
            stdout=_core.subprocess.PIPE,
            stderr=_core.subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            env=env,
            creationflags=flags,
        )
    except (OSError, _core.subprocess.TimeoutExpired) as exc:
        return False, f"包身份启动调用失败：{_core._redact_sensitive_text(exc, limit=240)}"
    if completed.returncode != 0:
        detail = _core._redact_sensitive_text(
            str(completed.stderr or completed.stdout or "").strip(),
            limit=360,
        )
        return False, f"包身份启动失败：{detail or completed.returncode}"
    return True, "package_identity"



def launch_codex_app(
    command_prefix: list[str] | None = None,
    env_overrides: dict[str, str] | None = None,
    launch_plan: dict | None = None,
) -> dict:
    """Launch Codex without an implicit current-directory project."""
    plan = dict(launch_plan or _core.resolve_codex_launch_plan(command_prefix))
    refresh_before_launch = bool(plan.pop("refreshBeforeLaunch", False))
    if _core.os.name == "nt" and refresh_before_launch:
        app_id = str(plan.get("appUserModelId") or "")
        valid_app_id = bool(
            _core.re.fullmatch(r"[A-Za-z0-9._-]+_[A-Za-z0-9]+![A-Za-z0-9._-]+", app_id)
        )
        executable = _core.Path(str(plan.get("executable") or ""))
        valid_executable = bool(
            executable.is_file()
            and executable.name.casefold() in {"chatgpt.exe", "codex.exe", "openai.codex.exe"}
        )
        if not valid_app_id and not valid_executable:
            refreshed = _core._detect_codex_windows_app(force=True)
            if refreshed:
                plan = {
                    **plan,
                    **refreshed,
                    "strategy": "windows_app" if refreshed.get("appUserModelId") else "desktop_executable",
                }
    # A running Store package can still be reported as a plain executable when
    # AppX discovery was temporarily unavailable.  Never pass a
    # ``WindowsApps`` path to CreateProcess/Popen: Windows deliberately blocks
    # that route with ERROR_ACCESS_DENIED, including from an elevated manager.
    # Re-resolve the registered AppUserModelId and use the same package-aware
    # path as a normal Store launch.  A portable installation remains eligible
    # for the desktop-executable strategy.
    if _core.os.name == "nt" and plan.get("strategy") == "desktop_executable":
        candidate = _core.Path(str(plan.get("executable") or ""))
        if _is_windows_store_path(candidate):
            refreshed = _core._detect_codex_windows_app(force=True) or {}
            refreshed_id = str(refreshed.get("appUserModelId") or "").strip()
            if _core.re.fullmatch(r"[A-Za-z0-9._-]+_[A-Za-z0-9]+![A-Za-z0-9._-]+", refreshed_id):
                plan = {
                    **plan,
                    **refreshed,
                    "strategy": "windows_app",
                }
            else:
                raise _core.ManagerError(
                    "检测到 Codex 位于 Microsoft Store 的 WindowsApps 目录，但未取得有效的应用标识；"
                    "为避免 WinError 5，已停止直接运行受保护的 exe。请先正常打开一次 Codex，"
                    "再重试账号切换。"
                )
    env = _core._codex_source_environment(plan, env_overrides)
    cli_flags = 0
    gui_flags = 0
    if _core.os.name == "nt":
        cli_flags |= getattr(_core.subprocess, "CREATE_NO_WINDOW", 0)
        cli_flags |= getattr(_core.subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        cli_flags |= getattr(_core.subprocess, "DETACHED_PROCESS", 0)
        # Do not add CREATE_NO_WINDOW to the GUI executable. Recent Codex
        # desktop builds can otherwise fail while spawning their App Server,
        # leaving the window indefinitely on the logo screen.
        gui_flags |= getattr(_core.subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        gui_flags |= getattr(_core.subprocess, "DETACHED_PROCESS", 0)
    if plan.get("strategy") == "windows_app":
        app_id = str(plan.get("appUserModelId") or "")
        if not _core.re.fullmatch(r"[A-Za-z0-9._-]+_[A-Za-z0-9]+![A-Za-z0-9._-]+", app_id):
            raise _core.ManagerError("Codex Windows App ID 无效。")
        for key, value in (env_overrides or {}).items():
            if (
                isinstance(key, str)
                and key
                and key.casefold() != "codex_cli_path"
                and key.casefold() != "codex_home"
                and key.casefold() not in {
                    item.casefold() for item in _core._OFFICIAL_AUTH_ENV_OVERRIDES
                }
                and isinstance(value, str)
            ):
                _core._sync_user_environment(key, value)
        explorer = _core.shutil.which("explorer.exe") or str(_core.Path(_core.os.environ.get("WINDIR") or "C:/Windows") / "explorer.exe")
        launch_errors = []
        last_process_scan_error = None
        launch_method = "app_user_model_id"
        store_entry_launched = False
        custom_environment = bool(
            plan.get("officialAccountId")
            or plan.get("apiProviderId")
            or any(
                isinstance(key, str)
                and isinstance(value, str)
                and key.casefold() not in {"codex_cli_path", "codex_home"}
                and key.casefold() not in {item.casefold() for item in _core._OFFICIAL_AUTH_ENV_OVERRIDES}
                for key, value in (env_overrides or {}).items()
            )
        )
        if custom_environment:
            candidate = _core.Path(str(plan.get("executable") or ""))
            if not candidate.is_file() or _is_windows_store_path(candidate):
                refreshed = _core._detect_codex_windows_app(force=True) or {}
                refreshed_id = str(refreshed.get("appUserModelId") or "").strip()
                if _core.re.fullmatch(r"[A-Za-z0-9._-]+_[A-Za-z0-9]+![A-Za-z0-9._-]+", refreshed_id):
                    plan = {**plan, **refreshed, "strategy": "windows_app"}
                    app_id = refreshed_id
            if not _core.Path(str(plan.get("executable") or "")).is_file():
                raise _core.ManagerError(
                    "需要隔离 Codex 账号或 Provider，但未找到当前商店包的可执行路径；"
                    "已停止使用未隔离的系统入口，请先正常打开一次 Codex 后重试。"
                )
        requires_package_identity = bool(
            custom_environment
            or str(_core.os.environ.get("CODEX_HOME") or "").strip()
        )
        start_deadline = _core.time.monotonic() + max(
            0.0,
            float(_core.CODEX_WINDOWS_APP_START_TIMEOUT_SECONDS),
        )

        def wait_for_process_until(deadline: float) -> list[dict]:
            nonlocal last_process_scan_error
            # Always probe once, including in tests or deployments that set a
            # zero timeout.  All launch strategies share one deadline so a
            # broken AppUserModelId cannot multiply the user's wait time.
            while True:
                processes, scan_error = _core._codex_launch_process_observation()
                if scan_error:
                    last_process_scan_error = scan_error
                if processes or _core.time.monotonic() >= deadline:
                    return processes
                _core.time.sleep(min(0.2, max(0.01, deadline - _core.time.monotonic())))

        allow_shell_activation = True
        if requires_package_identity:
            # Match Cockpit's managed Store path: package identity is the only
            # launch boundary that preserves CODEX_HOME/provider variables.
            package_started, package_detail = _core._launch_codex_via_package_identity(
                plan,
                env,
                env_overrides,
            )
            if package_started:
                store_entry_launched = True
                launch_method = "package_identity"
                allow_shell_activation = False
            else:
                allow_shell_activation = False
                launch_errors.append(package_detail)
        if allow_shell_activation:
            try:
                completed = _core.subprocess.run(
                    [explorer, f"shell:AppsFolder\\{app_id}"],
                    env=env,
                    stdin=_core.subprocess.DEVNULL,
                    stdout=_core.subprocess.DEVNULL,
                    stderr=_core.subprocess.DEVNULL,
                    timeout=8,
                    creationflags=getattr(_core.subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, _core.subprocess.TimeoutExpired) as exc:
                completed = None
                launch_errors.append(f"应用标识启动失败：{exc}")
            if completed is not None and completed.returncode == 0:
                store_entry_launched = True
                launch_method = "app_user_model_id"
            elif completed is not None:
                launch_errors.append(f"应用标识启动代码 {completed.returncode}")

        # AppUserModelId is normally immediate. Give it a short first share,
        # then use only the CLI workspace fallback if the system entry itself
        # failed. Windows App is single-instance, so never duplicate it with a
        # direct WindowsApps executable launch.
        primary_deadline = min(
            start_deadline,
            _core.time.monotonic() + max(0.0, float(_core.CODEX_WINDOWS_APP_PRIMARY_WAIT_SECONDS)),
        )
        processes = wait_for_process_until(primary_deadline)

        if not processes and not store_entry_launched and _core.time.monotonic() < start_deadline:
            workspace = _core._recent_codex_workspace()
            if workspace is not None:
                workspace_prefix = _safe_cli_workspace_prefix(plan, command_prefix)
                if not workspace_prefix:
                    launch_errors.append(
                        "CLI 工作区兜底已跳过：可用命令仍指向受保护的 WindowsApps 目录"
                    )
                else:
                    launch_method = "cli_workspace_fallback"
                    try:
                        fallback = _core.subprocess.Popen(
                            workspace_prefix + ["app", str(workspace)],
                            env=env,
                            stdin=_core.subprocess.DEVNULL,
                            stdout=_core.subprocess.DEVNULL,
                            stderr=_core.subprocess.DEVNULL,
                            creationflags=cli_flags,
                        )
                        try:
                            return_code = fallback.wait(timeout=1.5)
                        except _core.subprocess.TimeoutExpired:
                            return_code = None
                        if return_code not in {None, 0}:
                            launch_errors.append(f"CLI 启动器退出代码 {return_code}")
                    except (OSError, _core.ManagerError) as exc:
                        launch_errors.append(f"CLI 工作区启动失败：{exc}")
                    processes = wait_for_process_until(start_deadline)

        if not processes:
            if last_process_scan_error:
                launch_errors.append(f"进程检测暂不可用：{last_process_scan_error}")
            detail = "；".join(launch_errors[-4:]) or "没有检测到新的 Codex 进程"
            raise _core.ManagerError(f"Codex Windows App 启动失败：{detail}。")
        return {
            "started": True,
            "strategy": "windows_app",
            "appUserModelId": app_id,
            "package": plan.get("package"),
            "launchMethod": launch_method,
            "launchedAt": _core.now_iso(),
        }
    if plan.get("strategy") == "desktop_executable":
        executable = _core.Path(str(plan.get("executable") or ""))
        if (
            not executable.is_file()
            or executable.name.casefold() not in {"chatgpt.exe", "codex.exe", "openai.codex.exe"}
        ):
            raise _core.ManagerError("检测到的 Codex 桌面程序路径无效。")
        try:
            process = _core.subprocess.Popen(
                [str(executable)],
                env=env,
                stdin=_core.subprocess.DEVNULL,
                stdout=_core.subprocess.DEVNULL,
                stderr=_core.subprocess.DEVNULL,
                creationflags=gui_flags,
            )
        except OSError as exc:
            raise _core.ManagerError(f"无法启动检测到的 Codex 桌面程序：{exc}") from exc
        deadline = _core.time.monotonic() + _core.CODEX_WINDOWS_APP_START_TIMEOUT_SECONDS
        processes: list[dict] = []
        last_process_scan_error = None
        while _core.time.monotonic() < deadline:
            processes, scan_error = _core._codex_launch_process_observation()
            if scan_error:
                last_process_scan_error = scan_error
            if processes:
                break
            _core.time.sleep(0.2)
        if not processes:
            detail = f"（最后一次检测：{last_process_scan_error}）" if last_process_scan_error else ""
            raise _core.ManagerError(f"已启动 Codex 桌面程序，但在限定时间内未检测到运行进程{detail}。")
        return {
            "started": True,
            "strategy": "desktop_executable",
            "executable": str(executable),
            "launcherPid": process.pid,
            "launchedAt": _core.now_iso(),
        }
    command = plan.get("command")
    if not isinstance(command, list) or not command:
        raise _core.ManagerError("Codex 启动计划无效。")
    try:
        process = _core.subprocess.Popen(
            command,
            env=env,
            stdin=_core.subprocess.DEVNULL,
            stdout=_core.subprocess.DEVNULL,
            stderr=_core.subprocess.DEVNULL,
            creationflags=cli_flags,
        )
    except OSError as exc:
        raise _core.ManagerError(f"无法启动 Codex App：{exc}") from exc
    # The launcher normally hands off to the installed desktop app and exits.
    # Catch immediate failures while avoiding a long blocking wait.
    try:
        return_code = process.wait(timeout=1.5)
    except _core.subprocess.TimeoutExpired:
        return_code = None
    if return_code not in {None, 0}:
        raise _core.ManagerError(f"Codex App 启动器退出，代码 {return_code}。")
    return {
        "started": True,
        "strategy": "cli_workspace",
        "workspace": plan.get("workspace"),
        "launcherPid": process.pid,
        "launchedAt": _core.now_iso(),
    }

