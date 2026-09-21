"""CLI verification, override and management"""
from __future__ import annotations
from agent_manager import core as _core

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


