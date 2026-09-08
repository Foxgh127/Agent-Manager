"""Runtime services."""
from __future__ import annotations
from agent_manager import core as _core


def _split_runtime_path(value: str | None) -> list[str]:
    return [item.strip().strip('"') for item in str(value or "").split(_core.os.pathsep) if item.strip().strip('"')]



def _dedupe_runtime_paths(paths: list[str | _core.Path]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        value = _core.os.path.expandvars(str(raw).strip().strip('"'))
        if not value:
            continue
        key = _core.os.path.normcase(_core.os.path.normpath(value))
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result



def _windows_registry_path_values() -> list[str]:
    if _core.os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    locations = (
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    )
    values: list[str] = []
    for root, subkey in locations:
        try:
            with winreg.OpenKey(root, subkey, 0, winreg.KEY_QUERY_VALUE) as key:
                value, _kind = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        values.append(str(value or ""))
    return values



def _refresh_windows_process_path(extra_directories: list[str | _core.Path] | None = None) -> list[str]:
    if _core.os.name != "nt":
        return _core._split_runtime_path(_core.os.environ.get("PATH"))
    registry_entries: list[str] = []
    for value in _core._windows_registry_path_values():
        registry_entries.extend(_core._split_runtime_path(value))
    merged = _core._dedupe_runtime_paths(
        [*(extra_directories or []), *registry_entries, *_core._split_runtime_path(_core.os.environ.get("PATH"))]
    )
    _core.os.environ["PATH"] = _core.os.pathsep.join(merged)
    return merged



def _node_runtime_search_directories() -> list[_core.Path]:
    directories: list[str | _core.Path] = []
    directories.extend(_core._split_runtime_path(_core.os.environ.get("PATH")))
    if _core.os.name == "nt":
        for value in _core._windows_registry_path_values():
            directories.extend(_core._split_runtime_path(value))
    home = _core.Path.home()
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
    app_data = _core.Path(_core.os.environ.get("APPDATA") or _core.Path.home() / "AppData/Roaming")
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



def _verify_codex_cli(cli_path: _core.Path) -> str:
    flags = getattr(_core.subprocess, "CREATE_NO_WINDOW", 0) if _core.os.name == "nt" else 0
    command = _core._safe_codex_cli_command(cli_path)
    try:
        completed = _core.subprocess.run(
            [*command, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=flags,
        )
    except (OSError, _core.subprocess.TimeoutExpired) as exc:
        raise _core.ManagerError(f"Codex CLI 版本验证失败：{exc}") from exc
    version = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0 or not version:
        detail = version[-500:] or "命令没有返回版本信息"
        raise _core.ManagerError(f"Codex CLI 版本验证失败：{detail}")
    return version.splitlines()[0].strip()



def _registry_json(url: str, *, max_bytes: int = 1_000_000) -> dict:
    parsed = _core.urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "registry.npmjs.org":
        raise _core.ManagerError("官方 Codex 运行时元数据地址无效。")
    request = _core.urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "Agent-Manager/7.1.8"},
    )
    try:
        with _core._open_same_origin_request(request, timeout=30) as response:
            raw = response.read(max_bytes + 1)
    except _core.urllib.error.HTTPError as exc:
        raise _core.ManagerError(f"读取官方 Codex 运行时元数据失败：HTTP {exc.code}。") from exc
    except (_core.urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _core.ManagerError("无法连接官方 npm 仓库，请检查网络或代理后重试。") from exc
    if len(raw) > max_bytes:
        raise _core.ManagerError("官方 Codex 运行时元数据响应过大。")
    try:
        payload = _core.json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, _core.json.JSONDecodeError) as exc:
        raise _core.ManagerError("官方 Codex 运行时元数据格式无效。") from exc
    if not isinstance(payload, dict):
        raise _core.ManagerError("官方 Codex 运行时元数据格式无效。")
    return payload



def _windows_codex_platform() -> tuple[str, str]:
    architecture = str(
        _core.os.environ.get("PROCESSOR_ARCHITEW6432")
        or _core.os.environ.get("PROCESSOR_ARCHITECTURE")
        or ""
    ).casefold()
    if architecture in {"amd64", "x86_64", "x64"}:
        return "win32-x64", "x86_64-pc-windows-msvc"
    if architecture in {"arm64", "aarch64"}:
        return "win32-arm64", "aarch64-pc-windows-msvc"
    raise _core.ManagerError(f"暂不支持自动部署此 Windows 架构：{architecture or 'unknown'}。")



def _manager_downloaded_codex_candidates() -> list[_core.Path]:
    if _core.os.name != "nt" or not _core.MANAGED_CODEX_RUNTIME_DIR.is_dir():
        return []
    candidates = list(_core.MANAGED_CODEX_RUNTIME_DIR.glob("*/bin/codex.exe"))
    valid: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            if resolved.is_file():
                valid.append(resolved)
        except OSError:
            continue

    def modified(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(valid, key=modified, reverse=True)



def _safe_runtime_archive_destination(staging: _core.Path, relative: _core.PurePosixPath) -> _core.Path:
    """Map a verified POSIX archive member inside a Windows staging root."""

    parts = relative.parts
    if not parts:
        raise _core.ManagerError("官方 Codex 平台包包含空路径。")
    for part in parts:
        if (
            part in {"", ".", ".."}
            or "\\" in part
            or ":" in part
            or any(ord(character) < 32 for character in part)
            or part.endswith((" ", "."))
            or part.split(".", 1)[0].upper() in _core._WINDOWS_RESERVED_ARCHIVE_NAMES
        ):
            raise _core.ManagerError("官方 Codex 平台包包含不安全的 Windows 路径。")
    destination = staging.joinpath(*parts)
    try:
        destination.resolve(strict=False).relative_to(staging.resolve(strict=False))
    except (OSError, ValueError) as exc:
        raise _core.ManagerError("官方 Codex 平台包路径越出安装目录。") from exc
    return destination



def _download_official_codex_runtime() -> dict:
    """Install the signed npm platform payload without requiring Node or npm."""
    if _core.os.name != "nt":
        raise _core.ManagerError("官方独立运行时自动部署目前仅支持 Windows。")
    platform_name, target_triple = _core._windows_codex_platform()
    latest = _core._registry_json("https://registry.npmjs.org/@openai%2Fcodex/latest")
    version = str(latest.get("version") or "").strip()
    if not _core.re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version):
        raise _core.ManagerError("官方 Codex 最新版本号格式无效。")
    platform_version = f"{version}-{platform_name}"
    metadata = _core._registry_json(
        "https://registry.npmjs.org/@openai%2Fcodex/"
        + _core.urllib.parse.quote(platform_version, safe="")
    )
    if str(metadata.get("version") or "") != platform_version:
        raise _core.ManagerError("官方 Codex 平台包版本不匹配。")
    distribution = metadata.get("dist") if isinstance(metadata.get("dist"), dict) else {}
    tarball_url = str(distribution.get("tarball") or "")
    integrity = str(distribution.get("integrity") or "")
    parsed_tarball = _core.urllib.parse.urlsplit(tarball_url)
    if (
        parsed_tarball.scheme != "https"
        or parsed_tarball.hostname != "registry.npmjs.org"
        or not integrity.startswith("sha512-")
    ):
        raise _core.ManagerError("官方 Codex 平台包下载信息无效。")
    try:
        expected_digest = _core.base64.b64decode(integrity[7:], validate=True)
    except (ValueError, _core.binascii.Error) as exc:
        raise _core.ManagerError("官方 Codex 平台包完整性信息无效。") from exc
    if len(expected_digest) != _core.hashlib.sha512().digest_size:
        raise _core.ManagerError("官方 Codex 平台包完整性信息无效。")

    _core.MANAGED_CODEX_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    release_id = f"{platform_version}-{expected_digest.hex()[:12]}"
    final_directory = _core.MANAGED_CODEX_RUNTIME_DIR / release_id
    existing_cli = final_directory / "bin" / "codex.exe"
    if existing_cli.is_file():
        try:
            verified = _core._verify_codex_cli(existing_cli)
        except _core.ManagerError:
            pass
        else:
            return {"path": str(existing_cli.resolve()), "version": verified, "downloaded": False}

    with _core.tempfile.TemporaryDirectory(prefix=".runtime-install-", dir=_core.MANAGED_CODEX_RUNTIME_DIR) as temporary:
        temporary_root = _core.Path(temporary)
        archive_path = temporary_root / "codex-platform.tgz"
        digest = _core.hashlib.sha512()
        downloaded = 0
        request = _core.urllib.request.Request(
            tarball_url,
            headers={"Accept": "application/octet-stream", "User-Agent": "Agent-Manager/7.1.8"},
        )
        try:
            with _core._open_same_origin_request(request, timeout=90) as response, archive_path.open("wb") as output:
                while True:
                    chunk = response.read(1_048_576)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > 600_000_000:
                        raise _core.ManagerError("官方 Codex 平台包超过安全大小限制。")
                    digest.update(chunk)
                    output.write(chunk)
        except _core.ManagerError:
            raise
        except _core.urllib.error.HTTPError as exc:
            raise _core.ManagerError(f"下载官方 Codex 平台包失败：HTTP {exc.code}。") from exc
        except (_core.urllib.error.URLError, TimeoutError, OSError) as exc:
            raise _core.ManagerError("下载官方 Codex 平台包失败，请检查网络、代理或磁盘空间。") from exc
        if not downloaded or not _core.secrets.compare_digest(digest.digest(), expected_digest):
            raise _core.ManagerError("官方 Codex 平台包完整性校验失败，已拒绝安装。")

        staging = temporary_root / "runtime"
        staging.mkdir()
        vendor_prefix = _core.PurePosixPath("package", "vendor", target_triple)
        extracted_files = 0
        extracted_bytes = 0
        extracted_destinations: set[str] = set()
        try:
            with _core.tarfile.open(archive_path, mode="r:gz") as archive:
                for member in archive:
                    if "\\" in member.name:
                        raise _core.ManagerError("官方 Codex 平台包包含不安全的 Windows 路径。")
                    member_path = _core.PurePosixPath(member.name)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise _core.ManagerError("官方 Codex 平台包包含不安全路径。")
                    if member_path.parts[: len(vendor_prefix.parts)] != vendor_prefix.parts:
                        continue
                    relative = _core.PurePosixPath(*member_path.parts[len(vendor_prefix.parts) :])
                    if not relative.parts:
                        continue
                    destination = _core._safe_runtime_archive_destination(staging, relative)
                    destination_key = str(destination.resolve(strict=False)).casefold()
                    if destination_key in extracted_destinations:
                        raise _core.ManagerError("官方 Codex 平台包包含重复或大小写冲突路径。")
                    extracted_destinations.add(destination_key)
                    if member.isdir():
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    if not member.isfile():
                        raise _core.ManagerError("官方 Codex 平台包包含不支持的文件类型。")
                    extracted_files += 1
                    extracted_bytes += int(member.size)
                    if extracted_files > 500 or extracted_bytes > 1_000_000_000:
                        raise _core.ManagerError("官方 Codex 平台包解压内容超过安全限制。")
                    source = archive.extractfile(member)
                    if source is None:
                        raise _core.ManagerError("官方 Codex 平台包文件读取失败。")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with source, destination.open("wb") as output:
                        _core.shutil.copyfileobj(source, output, length=1_048_576)
        except (_core.tarfile.TarError, OSError) as exc:
            raise _core.ManagerError("官方 Codex 平台包解压失败。") from exc

        staged_cli = staging / "bin" / "codex.exe"
        if not staged_cli.is_file():
            raise _core.ManagerError("官方 Codex 平台包缺少 codex.exe。")
        verified = _core._verify_codex_cli(staged_cli)
        quarantined_directory = None
        if final_directory.exists() or final_directory.is_symlink():
            current_cli = final_directory / "bin" / "codex.exe"
            if current_cli.is_file():
                try:
                    current_version = _core._verify_codex_cli(current_cli)
                except _core.ManagerError:
                    pass
                else:
                    return {
                        "path": str(current_cli.resolve()),
                        "version": current_version,
                        "downloaded": False,
                    }
            quarantined_directory = temporary_root / "replaced-runtime"
            try:
                final_directory.replace(quarantined_directory)
            except OSError as exc:
                raise _core.ManagerError("无法替换损坏的官方 Codex 运行时目录。") from exc
        try:
            staging.replace(final_directory)
        except OSError as exc:
            if quarantined_directory is not None and not final_directory.exists():
                try:
                    quarantined_directory.replace(final_directory)
                except OSError:
                    pass
            raise _core.ManagerError("官方 Codex 运行时写入失败。") from exc
        installed_cli = final_directory / "bin" / "codex.exe"
        if not installed_cli.is_file():
            raise _core.ManagerError("官方 Codex 运行时写入失败。")
    return {"path": str(installed_cli.resolve()), "version": verified, "downloaded": True}



def _desktop_managed_codex_candidates() -> list[_core.Path]:
    """Find the versioned runtime downloaded by current Codex/ChatGPT desktop builds."""
    if _core.os.name != "nt":
        return []
    local_app_data = _core.Path(_core.os.environ.get("LOCALAPPDATA") or _core.Path.home() / "AppData/Local")
    roaming_app_data = _core.Path(_core.os.environ.get("APPDATA") or _core.Path.home() / "AppData/Roaming")
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
    return {
        "available": bool(prefix),
        "source": source,
        "command": prefix,
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
        return {"strategy": strategy, **windows_app, "refreshBeforeLaunch": True}
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
    for key in list(env):
        if key.casefold() in blocked:
            env.pop(key, None)
    env["CODEX_HOME"] = str(_core.CODEX_HOME)
    return env



def _codex_launch_probe_prefix(plan: dict | None) -> list[str]:
    """Prefer the selected desktop's bundled App Server over an unrelated PATH CLI."""
    plan = plan or {}
    desktop = _core.Path(str(plan.get("executable") or ""))
    candidates = [plan.get("appServerExecutable")]
    if desktop.is_file():
        candidates.extend((desktop.parent / "resources" / "codex.exe",
                           desktop.parent / "resources" / "bin" / "codex.exe",
                           desktop.parent / "resources" / "codex",
                           desktop.parent / "codex.exe"))
    for raw in candidates:
        if raw:
            candidate = _core.Path(raw)
            if candidate.is_file() and candidate.name.casefold() in {"codex.exe", "codex"} and candidate != desktop:
                return [str(candidate)]
    command = plan.get("command")
    if plan.get("strategy") == "cli_workspace" and isinstance(command, list) and "app" in command:
        return list(command[:command.index("app")])
    return _core.codex_prefix()



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
    official = bool(plan.get("officialAccountId"))
    isolated_source = bool(official or plan.get("apiProviderId"))
    env = _core._codex_source_environment(plan, env_overrides)
    if isolated_source and plan.get("strategy") == "windows_app":
        # Explorer activation can reuse an existing shell with a different
        # environment. Explicit account switches need a deterministic child.
        candidate = _core.Path(str(plan.get("executable") or ""))
        if not candidate.is_file():
            refreshed = _core._detect_codex_windows_app(force=True) or {}
            candidate = _core.Path(str(refreshed.get("executable") or ""))
        if not candidate.is_file() or candidate.name.casefold() not in {"chatgpt.exe", "codex.exe", "openai.codex.exe"}:
            raise _core.ManagerError("无法定位可直接启动的 Codex 桌面程序，已停止官方账号启动以避免使用其他环境。")
        plan.update(strategy="desktop_executable", executable=str(candidate))
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
                and isinstance(value, str)
            ):
                _core._sync_user_environment(key, value)
        explorer = _core.shutil.which("explorer.exe") or str(_core.Path(_core.os.environ.get("WINDIR") or "C:/Windows") / "explorer.exe")
        launch_errors = []
        last_process_scan_error = None
        launch_method = "app_user_model_id"
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
        if completed is not None and completed.returncode != 0:
            launch_errors.append(f"应用标识启动代码 {completed.returncode}")

        # AppUserModelId is normally immediate. Give it a short first share,
        # then use the current package executable and CLI workspace with the
        # remaining global budget. Windows App is single-instance, so the
        # direct fallback does not create duplicate interactive sessions.
        primary_deadline = min(
            start_deadline,
            _core.time.monotonic() + max(0.0, float(_core.CODEX_WINDOWS_APP_PRIMARY_WAIT_SECONDS)),
        )
        processes = wait_for_process_until(primary_deadline)

        if not processes:
            refreshed = _core._detect_codex_windows_app(force=True) or {}
            candidates = [plan.get("executable"), refreshed.get("executable")]
            executable = next(
                (
                    _core.Path(str(item))
                    for item in candidates
                    if item
                    and _core.Path(str(item)).is_file()
                    and _core.Path(str(item)).name.casefold() in {"chatgpt.exe", "codex.exe", "openai.codex.exe"}
                ),
                None,
            )
            if executable is not None:
                launch_method = "current_appx_executable"
                try:
                    _core.subprocess.Popen(
                        [str(executable)],
                        env=env,
                        stdin=_core.subprocess.DEVNULL,
                        stdout=_core.subprocess.DEVNULL,
                        stderr=_core.subprocess.DEVNULL,
                        creationflags=gui_flags,
                    )
                except OSError as exc:
                    launch_errors.append(f"当前安装路径启动失败：{exc}")
                direct_deadline = min(start_deadline, _core.time.monotonic() + 5.0)
                processes = wait_for_process_until(direct_deadline)

        if not processes and _core.time.monotonic() < start_deadline:
            workspace = _core._recent_codex_workspace()
            if workspace is not None:
                launch_method = "cli_workspace_fallback"
                try:
                    fallback = _core.subprocess.Popen(
                        list(command_prefix or _core.codex_prefix()) + ["app", str(workspace)],
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



def run_codex_capture(args: list[str], timeout: int = 60, env: dict | None = None) -> _core.subprocess.CompletedProcess:
    flags = getattr(_core.subprocess, "CREATE_NO_WINDOW", 0) if _core.os.name == "nt" else 0
    # Agent Manager can itself be launched from a captured/non-interactive
    # terminal where TERM=dumb is inherited.  Codex Doctor treats that marker
    # as a terminal failure even though this helper intentionally captures all
    # output and never needs terminal capabilities.  Do not let the parent's
    # presentation hint turn an otherwise healthy configuration red.
    child_env = dict(_core.os.environ if env is None else env)
    if str(child_env.get("TERM") or "").casefold() == "dumb":
        child_env.pop("TERM", None)
    return _core.subprocess.run(
        _core.codex_prefix() + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=child_env,
        creationflags=flags,
    )

