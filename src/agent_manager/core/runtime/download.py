"""Runtime download and installation"""
from __future__ import annotations
from agent_manager import core as _core


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
