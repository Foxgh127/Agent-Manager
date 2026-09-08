"""Agent Manager update discovery and verified downloads; never executes assets.

API integration: GET /api/app-update -> {ok:true,status:service.status()};
POST source/check/download/cancel -> the same envelope. Download body contains
only releaseToken, not an arbitrary URL/path. open-folder uses
verified_download_path().parent after explicit user action.

Bundled app-update-source.json and a local override accept source dictionaries:
  {kind: "github", repository: "owner/repo", assetName: "App-{version}.exe"}
  {kind: "manifest", url: "https://updates.example/latest.json",
   allowedAssetHosts: ["downloads.example"]}
Both optionally accept channel: stable (default) or prerelease.

Manifest v1:
  {schemaVersion:1, appId:"openai-agent-manager", version:"9.12.0",
   channel:"stable", publishedAt:"...", releaseNotes:"plain text",
   assets:[{platform:"windows-x64",name:"App-9.12.0.exe",url:"https://...",
            size:123,sha256:"64 lowercase/uppercase hex characters"}]}

A checksum from the same HTTPS source proves transfer integrity, not publisher
authenticity. No signatures, silent installation, privileged writes, or source
repository publication are performed by this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import total_ordering
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import tempfile
import threading
import time
from urllib.parse import quote, unquote, urljoin, urlsplit
import urllib.request

APP_ID = "openai-agent-manager"
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_CONFIG_BYTES = 32 * 1024
MAX_ASSET_BYTES = 1024 * 1024 * 1024
NETWORK_TIMEOUT = 10.0
METADATA_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 15 * 60.0
MAX_REDIRECTS = 4
GITHUB_ASSET_HOSTS = frozenset({"github.com", "release-assets.githubusercontent.com",
                              "objects.githubusercontent.com", "github-releases.githubusercontent.com"})
_DNS_SLOTS = threading.BoundedSemaphore(4)
INTEGRITY_NOTE = "SHA-256 和文件大小用于验证下载完整性；校验值来自同一 HTTPS 发布源，不代表数字签名或发布者身份认证。"


class UpdateError(RuntimeError):
    def __init__(self, message: str, code: str = "update_error"):
        super().__init__(message)
        self.code = code


@total_ordering
@dataclass(frozen=True, eq=False)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value):
        if not isinstance(value, str) or len(value) > 100:
            raise UpdateError("版本号格式无效。", "invalid_version")
        match = re.fullmatch(r"[vV]?(0|[1-9]\d*)\.(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?", value.strip())
        if not match:
            raise UpdateError("版本号格式无效。", "invalid_version")
        prerelease = tuple((match[4] or "").split(".")) if match[4] else ()
        if any(part.isdigit() and len(part) > 1 and part.startswith("0") for part in prerelease):
            raise UpdateError("预发布版本号格式无效。", "invalid_version")
        return cls(int(match[1]), int(match[2]), int(match[3] or 0), prerelease)

    def __eq__(self, other):
        if not isinstance(other, Version):
            return NotImplemented
        return (self.major, self.minor, self.patch, self.prerelease) == (other.major, other.minor, other.patch, other.prerelease)

    def __lt__(self, other):
        if not isinstance(other, Version):
            return NotImplemented
        left, right = (self.major, self.minor, self.patch), (other.major, other.minor, other.patch)
        if left != right:
            return left < right
        if not self.prerelease or not other.prerelease:
            return bool(self.prerelease) and not other.prerelease
        for left_part, right_part in zip(self.prerelease, other.prerelease):
            if left_part == right_part:
                continue
            if left_part.isdigit() and right_part.isdigit():
                return int(left_part) < int(right_part)
            if left_part.isdigit() != right_part.isdigit():
                return left_part.isdigit()
            return left_part < right_part
        return len(self.prerelease) < len(other.prerelease)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_public_address(address):
    value = ipaddress.ip_address(address)
    return value.is_global and not (value.is_multicast or value.is_reserved or value.is_unspecified)


def _host(value):
    if not isinstance(value, str) or not value or len(value) > 253 or value != value.strip():
        raise UpdateError("更新源主机名无效。", "invalid_host")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        try:
            value = value.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise UpdateError("更新源主机名无效。", "invalid_host") from exc
        if (not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value)
                or any(not part or len(part) > 63 or part.startswith("-") or part.endswith("-") for part in value.split("."))
                or "." not in value or value.endswith((".localhost", ".local", ".internal"))):
            raise UpdateError("更新源必须使用公开主机名。", "invalid_host")
    else:
        if not _is_public_address(address):
            raise UpdateError("更新源不允许本机、内网或保留地址。", "private_address")
    return value.lower()


def _url(value, allowed_hosts=None, *, allow_query=False):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(char) < 33 or char == "\\" for char in value):
        raise UpdateError("更新 URL 无效。", "invalid_url")
    try:
        parsed = urlsplit(value)
        host = _host(parsed.hostname or "")
        port = parsed.port
    except ValueError as exc:
        raise UpdateError("更新 URL 无效。", "invalid_url") from exc
    if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
            or port not in (None, 443) or parsed.fragment or (parsed.query and not allow_query)):
        raise UpdateError("更新源必须是无凭据的 HTTPS URL（443 端口）。", "invalid_url")
    if allowed_hosts is not None and host not in allowed_hosts:
        raise UpdateError("更新请求或重定向超出已授权主机。", "host_not_allowed")
    return parsed, host


def _safe_name(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 160 or value != value.strip()
            or value.endswith((".", " ")) or any(ord(char) < 32 or char in '/\\:*?"<>|' for char in value)
            or value in {".", ".."} or not value.lower().endswith((".exe", ".zip", ".msi"))
            or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", value.split(".")[0].rstrip().upper())):
        raise UpdateError("更新文件名不安全或文件类型不支持。", "invalid_asset_name")
    return value


def validate_source(source):
    if not isinstance(source, dict):
        raise UpdateError("更新源配置必须是对象。", "invalid_source")
    kind = source.get("kind")
    allowed = {"schemaVersion", "kind", "channel"}
    allowed |= {"url", "allowedAssetHosts"} if kind == "manifest" else {"repository", "assetName"} if kind == "github" else set()
    if set(source) - allowed or kind not in {"manifest", "github", "disabled"}:
        raise UpdateError("更新源类型或字段无效；不接受认证头或访问令牌。", "invalid_source")
    if source.get("schemaVersion", 1) != 1:
        raise UpdateError("更新源配置版本不支持。", "invalid_source")
    channel = source.get("channel", "stable")
    if channel not in {"stable", "prerelease"}:
        raise UpdateError("更新渠道无效。", "invalid_source")
    result = {"kind": kind, "channel": channel}
    if kind == "manifest":
        _url(source.get("url"))
        hosts = source.get("allowedAssetHosts", [])
        if not isinstance(hosts, list) or len(hosts) > 12:
            raise UpdateError("允许的下载主机列表无效。", "invalid_source")
        result.update(url=source["url"], allowedAssetHosts=sorted({_host(item) for item in hosts}))
    elif kind == "github":
        repository = source.get("repository")
        if (not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", repository)
                or repository.split("/")[-1] in {".", ".."}):
            raise UpdateError("GitHub 仓库请填写 owner/repository。", "invalid_source")
        template = source.get("assetName", "")
        if not isinstance(template, str) or len(template) > 160:
            raise UpdateError("更新资产名称无效。", "invalid_source")
        if template:
            _safe_name(template.replace("{version}", "1.2.3").replace("{tag}", "v1.2.3"))
            if "{" in re.sub(r"\{(?:version|tag)\}", "", template) or "}" in re.sub(r"\{(?:version|tag)\}", "", template):
                raise UpdateError("资产名称只支持 {version} 和 {tag}。", "invalid_source")
        result.update(repository=repository, assetName=template)
    return result


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _public_addresses(host):
    try:
        addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    except OSError as exc:
        raise UpdateError("更新源 DNS 查询失败。", "dns_error") from exc
    if not addresses or any(not _is_public_address(address) for address in addresses):
        raise UpdateError("更新源解析到内网或保留地址，已停止连接。", "private_address")
    return addresses[:4]


def _resolve_bounded(host, deadline, cancel, resolver=None):
    # The OS DNS resolver cannot be synchronously interrupted. Isolate it in a
    # bounded daemon slot; a timed-out resolver never opens an HTTP connection.
    if not _DNS_SLOTS.acquire(blocking=False):
        raise UpdateError("更新 DNS 查询繁忙，请稍后重试。", "dns_busy")
    done, result = threading.Event(), {}
    def resolve():
        try:
            result["addresses"] = (resolver or _public_addresses)(host)
        except Exception as exc:
            result["error"] = exc
        finally:
            _DNS_SLOTS.release()
            done.set()
    worker = threading.Thread(target=resolve, daemon=True, name="app-update-dns")
    try:
        worker.start()
    except Exception:
        _DNS_SLOTS.release()
        raise
    dns_deadline = min(deadline, time.monotonic() + NETWORK_TIMEOUT)
    while not done.wait(0.05):
        if cancel and cancel.is_set():
            raise UpdateError("下载已取消。", "cancelled")
        if time.monotonic() >= dns_deadline:
            raise UpdateError("更新源 DNS 查询超时。", "timeout")
    if "error" in result:
        raise result["error"]
    return result["addresses"]


class _TransferGuard:
    def __init__(self, connection, deadline, cancel):
        self.done = threading.Event()
        self.connection = connection
        def watch():
            while not self.done.wait(0.05):
                if time.monotonic() >= deadline or cancel and cancel.is_set():
                    sock = getattr(connection, "socket_reference", None)
                    if sock is not None:
                        try:
                            sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
        self.thread = threading.Thread(target=watch, daemon=True, name="app-update-network-guard")
        self.thread.start()

    def close(self):
        self.done.set()


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout, connect_port=443):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self._address = address
        self._connect_port = connect_port
        self.socket_reference = None

    def connect(self):
        family = socket.AF_INET6 if ipaddress.ip_address(self._address).version == 6 else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        self.socket_reference = sock
        try:
            sock.connect((self._address, self._connect_port))
            self.sock = sock
            if self._tunnel_host:
                self._tunnel()
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host, do_handshake_on_connect=False)
            self.socket_reference = self.sock
            self.sock.do_handshake()
        except Exception:
            if self.sock is not None:
                self.sock.close()
            sock.close()
            raise


def _system_proxy(host):
    """Honor an explicitly configured OS/environment HTTPS proxy, without auth."""
    proxies = dict(urllib.request.getproxies())
    # NO_PROXY alone suppresses CPython's Windows registry fallback. Preserve
    # explicit HTTPS/ALL environment endpoints, otherwise consult OS settings.
    registry_reader = getattr(urllib.request, "getproxies_registry", None)
    if not (proxies.get("https") or proxies.get("all")) and callable(registry_reader):
        try:
            registry = registry_reader()
        except (OSError, ValueError):
            registry = {}
        if isinstance(registry, dict):
            for scheme in ("https", "all"):
                if registry.get(scheme):
                    proxies[scheme] = registry[scheme]
    value = proxies.get("https") or proxies.get("all")
    if not value or urllib.request.proxy_bypass(host):
        return None
    if not isinstance(value, str) or len(value) > 2048 or any(ord(char) < 33 for char in value):
        raise UpdateError("系统更新代理配置无效。", "invalid_proxy")
    if "://" not in value:
        value = "http://" + value
    try:
        parsed = urlsplit(value)
        port = parsed.port or 80
        hostname = parsed.hostname
    except ValueError as exc:
        raise UpdateError("系统更新代理配置无效。", "invalid_proxy") from exc
    if parsed.username is not None or parsed.password is not None:
        raise UpdateError("更新下载暂不支持需要认证的代理；请使用系统无认证 HTTP 代理。", "proxy_auth_unsupported")
    if parsed.scheme != "http":
        raise UpdateError("更新下载暂仅支持系统无认证 HTTP CONNECT 代理，不支持 SOCKS 或 HTTPS 代理协议。", "proxy_scheme_unsupported")
    if not hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or not 0 < port < 65536:
        raise UpdateError("系统更新代理地址无效。", "invalid_proxy")
    # A local proxy is deliberately permitted here. It is selected by the
    # machine's proxy configuration, never by an untrusted update manifest.
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UpdateError("系统更新代理主机名无效。", "invalid_proxy") from exc
    return hostname, port


def _proxy_addresses(host, port):
    try:
        values = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    except OSError as exc:
        raise UpdateError("系统代理 DNS 查询失败。", "proxy_dns_error") from exc
    if not values:
        raise UpdateError("系统代理没有可连接地址。", "proxy_dns_error")
    return values[:4]


class _Stream:
    def __init__(self, connection, response, guard=None):
        self.connection, self.response = connection, response
        self.headers = response.headers
        self.guard = guard

    def read(self, size):
        return getattr(self.response, "read1", self.response.read)(size)

    def abort(self):
        sock = self.connection.socket_reference
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self):
        try:
            self.response.close()
        finally:
            self.connection.close()
            if self.guard:
                self.guard.close()


class HTTPSFetcher:
    """Public HTTPS, pinned direct DNS or explicit unauthenticated CONNECT proxy.

    Proxy configuration is respected, but no ambient credentials or headers are
    copied. In proxy mode the trusted proxy resolves the public target host.
    """
    def open(self, url, allowed_hosts, *, timeout=METADATA_TIMEOUT, cancel=None):
        deadline = time.monotonic() + timeout
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            if cancel and cancel.is_set():
                raise UpdateError("下载已取消。", "cancelled")
            if time.monotonic() >= deadline:
                raise UpdateError("更新请求超时。", "timeout")
            parsed, host = _url(current, allowed_hosts, allow_query=True)
            proxy = _system_proxy(host)
            if proxy:
                proxy_host, proxy_port = proxy
                addresses = _resolve_bounded(proxy_host, deadline, cancel, lambda value: _proxy_addresses(value, proxy_port))
            else:
                addresses = _resolve_bounded(host, deadline, cancel)
            if cancel and cancel.is_set():
                raise UpdateError("下载已取消。", "cancelled")
            timeout_remaining = min(NETWORK_TIMEOUT, max(0.1, deadline - time.monotonic()))
            connection = _PinnedHTTPS(host, addresses[0], timeout_remaining,
                                      **({"connect_port": proxy_port} if proxy else {}))
            if proxy:
                connection.set_tunnel(host, port=443)
            guard = _TransferGuard(connection, deadline, cancel)
            try:
                target = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
                connection.request("GET", target, headers={"User-Agent": "Agent-Manager-Updater/1",
                    "Accept": "application/json, application/octet-stream", "Accept-Encoding": "identity", "Connection": "close"})
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    response.close()
                    connection.close()
                    guard.close()
                    if not location or hop == MAX_REDIRECTS:
                        raise UpdateError("更新源重定向无效或次数过多。", "redirect_limit")
                    current = urljoin(current, location)
                    _url(current, allowed_hosts, allow_query=True)
                    continue
                if response.status != 200:
                    code = response.status
                    response.close()
                    connection.close()
                    raise UpdateError(f"更新源返回 HTTP {code}。", "http_error")
                if response.getheader("Content-Encoding", "identity").casefold() != "identity":
                    response.close()
                    connection.close()
                    raise UpdateError("更新源返回了不支持的内容编码。", "invalid_encoding")
                return _Stream(connection, response, guard)
            except UpdateError:
                connection.close()
                guard.close()
                raise
            except (OSError, http.client.HTTPException) as exc:
                connection.close()
                guard.close()
                raise UpdateError("无法连接更新源或连接超时。", "network_error") from exc
        raise UpdateError("更新源重定向次数过多。", "redirect_limit")


def _regular_path(path, *, missing=True):
    path = Path(os.path.abspath(path))
    for candidate in reversed([path, *path.parents]):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise UpdateError("更新文件不存在。", "file_missing")
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
            raise UpdateError("更新路径不允许符号链接或目录联接。", "unsafe_path")
    return path


def _atomic_json(path, value):
    path = _regular_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".update-config-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path, limit):
    path = _regular_path(path)
    if not path.is_file():
        return None
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise UpdateError("更新配置或缓存超过读取上限。", "file_too_large")
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise UpdateError("更新配置或缓存 JSON 无效。", "invalid_json") from exc


class AppUpdateService:
    def __init__(self, current_version, config_path, download_dir, bundled_source_path=None,
                 cache_path=None, *, platform="windows-x64", fetcher=None, install_supported=False):
        self.current_version = str(current_version)
        self._version = Version.parse(current_version)
        self.config_path, self.download_dir = Path(config_path), Path(download_dir)
        self.bundled_source_path = Path(bundled_source_path) if bundled_source_path else None
        self.cache_path = Path(cache_path) if cache_path else self.config_path.with_name("app-update-cache.json")
        self.platform, self.fetcher = platform, fetcher or HTTPSFetcher()
        self.install_supported = bool(install_supported)
        self._lock = threading.RLock()
        self._operation = threading.Lock()
        self._cancel = threading.Event()
        self._thread = None
        self._active_stream = None
        self._closed = False
        self._latest = None
        self._source_id = ""
        self._checked_at = None
        self._last_error = ""
        self._error_code = ""
        self._fresh = False
        self._checking = False
        self._download = {"state": "idle", "downloadedBytes": 0, "totalBytes": 0}
        self._installation = {"state": "idle"}
        self._installation_expected_id = None
        try:
            cache = _read_json(self.cache_path, MAX_METADATA_BYTES)
            if isinstance(cache, dict):
                self._source_id = str(cache.get("sourceId") or "")
                self._latest = cache.get("release") if isinstance(cache.get("release"), dict) else None
                self._checked_at = cache.get("checkedAt")
        except (UpdateError, OSError):
            pass

    def _source(self):
        local = _read_json(self.config_path, MAX_CONFIG_BYTES)
        origin = "local"
        if local is None or (isinstance(local, dict) and local.get("useBundled") is True):
            local = _read_json(self.bundled_source_path, MAX_CONFIG_BYTES) if self.bundled_source_path else None
            origin = "bundled"
        if local is None:
            return None, "none"
        if isinstance(local, dict) and "source" in local:
            local = local["source"]
        source = validate_source(local)
        return (None if source["kind"] == "disabled" else source), origin

    def configure(self, source):
        if not self._operation.acquire(blocking=False):
            raise UpdateError("正在检查或下载更新，请先等待或取消。", "busy")
        try:
            value = {"schemaVersion": 1, "useBundled": True} if source is None else {"schemaVersion": 1, "source": validate_source(source)}
            _atomic_json(self.config_path, value)
            with self._lock:
                self._fresh = False
                self._latest = None
                self._checked_at = None
                self._last_error = self._error_code = ""
            return self.status()
        finally:
            self._operation.release()

    def status(self):
        self._read_installation_result()
        try:
            source, origin = self._source()
            config_error = ""
        except (UpdateError, OSError):
            source, origin, config_error = None, "invalid", "更新源配置无效，请重新保存。"
        with self._lock:
            matches = source is not None and _fingerprint(source) == self._source_id
            latest = self._latest if matches else None
            newer = False
            if latest:
                try:
                    newer = Version.parse(latest.get("version")) > self._version
                except UpdateError:
                    latest = None
            state = "unconfigured" if source is None else "checking" if self._checking else "check_failed" if self._last_error else "update_available" if latest and newer else "current" if latest and self._fresh else "not_checked"
            if config_error:
                state = "config_error"
            public_release = {key: latest[key] for key in ("version", "name", "size", "sha256", "publishedAt", "releaseNotes", "releaseToken") if key in latest} if latest else None
            download = dict(self._download)
            return {"currentVersion": self.current_version, "state": state, "configured": source is not None,
                    "source": source, "sourceOrigin": origin, "checkedAt": self._checked_at,
                    "latestRelease": public_release, "updateAvailable": newer,
                    "stale": bool(latest and not self._fresh), "error": config_error or self._last_error,
                    "errorCode": self._error_code, "canDownload": bool(newer and self._fresh and not self._last_error and not self._checking and download["state"] != "downloading" and not self._closed),
                    "download": download, "installation": dict(self._installation), "downloadDirectory": str(self.download_dir.absolute()),
                    "integrityNote": INTEGRITY_NOTE, "installSupported": self.install_supported,
                    "canInstall": bool(self.install_supported and newer and self._fresh and latest
                        and latest.get("sha256") == download.get("sha256") and download.get("state") == "ready"
                        and download.get("verified") and not self._closed
                        and self._installation.get("state") in {"idle", "failed", "complete"}),
                    "message": "尚未配置 Agent Manager 发布源。" if source is None and not config_error else ""}

    def _json_remote(self, url, hosts):
        deadline = time.monotonic() + METADATA_TIMEOUT
        stream = self.fetcher.open(url, hosts, timeout=METADATA_TIMEOUT)
        try:
            length = stream.headers.get("Content-Length")
            if length is not None and (not str(length).isdigit() or int(length) > MAX_METADATA_BYTES):
                raise UpdateError("更新元数据超过读取上限。", "metadata_too_large")
            raw = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise UpdateError("更新元数据读取超时。", "timeout")
                chunk = stream.read(min(65536, MAX_METADATA_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > MAX_METADATA_BYTES:
                    raise UpdateError("更新元数据超过读取上限。", "metadata_too_large")
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise UpdateError("更新元数据 JSON 无效。", "invalid_manifest") from exc
        finally:
            stream.close()

    def _asset(self, raw, source, version, *, tag="", notes="", published=""):
        if not isinstance(raw, dict):
            raise UpdateError("更新资产结构无效。", "invalid_asset")
        name = _safe_name(raw.get("name"))
        size, digest = raw.get("size"), raw.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_ASSET_BYTES:
            raise UpdateError("更新文件大小缺失或超过 1 GiB 上限。", "invalid_asset_size")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise UpdateError("发布资产缺少有效 SHA-256；无法提供经过校验的下载。", "missing_checksum")
        if source["kind"] == "github":
            hosts = set(GITHUB_ASSET_HOSTS)
            parsed, host = _url(raw.get("url"), {"github.com"})
            prefix = f"/{source['repository']}/releases/download/"
            if (not parsed.path[:len(prefix)].casefold() == prefix.casefold()
                    or unquote(parsed.path[len(prefix):]) != f"{tag}/{name}"):
                raise UpdateError("GitHub 资产不属于所配置仓库的该版本。", "asset_origin_mismatch")
        else:
            hosts = {_url(source["url"])[1], *source["allowedAssetHosts"]}
            _url(raw.get("url"), hosts)
        return {"version": version, "name": name, "size": size, "sha256": digest.lower(),
                "url": raw["url"], "allowedHosts": sorted(hosts), "publishedAt": str(published or "")[:80],
                "releaseNotes": str(notes or "")[:12000]}

    def _discover(self, source):
        if source["kind"] == "manifest":
            raw = self._json_remote(source["url"], {_url(source["url"])[1]})
            if not isinstance(raw, dict) or raw.get("schemaVersion") != 1 or raw.get("appId") != APP_ID:
                raise UpdateError("更新 manifest 版本或应用标识不匹配。", "invalid_manifest")
            version = raw.get("version")
            parsed = Version.parse(version)
            if source["channel"] == "stable" and (parsed.prerelease or raw.get("channel", "stable") != "stable"):
                raise UpdateError("稳定渠道返回了预发布版本。", "channel_mismatch")
            assets = raw.get("assets")
            if not isinstance(assets, list) or len(assets) > 100:
                raise UpdateError("更新资产列表无效。", "invalid_manifest")
            selected = [asset for asset in assets if isinstance(asset, dict) and asset.get("platform") == self.platform]
            if len(selected) != 1:
                raise UpdateError("该版本缺少唯一的当前平台更新文件。", "asset_selection")
            return self._asset(selected[0], source, version, notes=raw.get("releaseNotes"), published=raw.get("publishedAt"))
        endpoint = f"https://api.github.com/repos/{source['repository']}/releases"
        raw = self._json_remote(endpoint + ("/latest" if source["channel"] == "stable" else "?per_page=20"), {"api.github.com"})
        releases = raw if isinstance(raw, list) else [raw]
        candidates = []
        for release in releases[:20]:
            if not isinstance(release, dict) or release.get("draft") or (source["channel"] == "stable" and release.get("prerelease")):
                continue
            try:
                version = Version.parse(release.get("tag_name"))
            except UpdateError:
                continue
            if source["channel"] == "stable" and version.prerelease:
                continue
            candidates.append((version, release))
        if not candidates:
            raise UpdateError("未找到可识别的公开 Release 版本。", "release_not_found")
        _, release = max(candidates, key=lambda pair: pair[0])
        tag = release["tag_name"]
        version = tag[1:] if tag[:1].lower() == "v" else tag
        template = source.get("assetName")
        wanted = template.replace("{version}", version).replace("{tag}", tag) if template else None
        assets = release.get("assets")
        if not isinstance(assets, list) or len(assets) > 100:
            raise UpdateError("GitHub Release 资产列表无效。", "invalid_asset")
        selected = [item for item in assets if isinstance(item, dict) and item.get("state", "uploaded") == "uploaded"
                    and (item.get("name") == wanted if wanted else str(item.get("name", "")).lower().endswith(".exe"))]
        if len(selected) != 1:
            raise UpdateError("请配置该平台的准确 GitHub 资产名称；当前没有唯一匹配。", "asset_selection")
        asset = selected[0]
        digest = asset.get("digest", "")
        return self._asset({"name": asset.get("name"), "size": asset.get("size"), "url": asset.get("browser_download_url"),
                            "sha256": digest[7:] if isinstance(digest, str) and digest.startswith("sha256:") else ""},
                           source, version, tag=tag, notes=release.get("body"), published=release.get("published_at"))

    def check(self):
        if not self._operation.acquire(blocking=False):
            raise UpdateError("正在检查或下载更新。", "busy")
        try:
            if self._closed:
                raise UpdateError("更新服务正在关闭。", "closed")
            source, _origin = self._source()
            if source is None:
                return self.status()
            with self._lock:
                self._checking = True
            release = self._discover(source)
            source_id = _fingerprint(source)
            release["releaseToken"] = _fingerprint({"source": source_id, "release": release})
            checked_at = _now()
            with self._lock:
                self._latest, self._source_id, self._checked_at = release, source_id, checked_at
                self._fresh, self._last_error, self._error_code = True, "", ""
            try:
                _atomic_json(self.cache_path, {"schemaVersion": 1, "sourceId": source_id, "checkedAt": checked_at, "release": release})
            except OSError:
                pass  # A metadata cache failure must not invent a failed check.
        except Exception as exc:
            with self._lock:
                self._fresh = False
                self._last_error = str(exc) if isinstance(exc, UpdateError) else "检查更新失败；保留上次检查结果。"
                self._error_code = exc.code if isinstance(exc, UpdateError) else "check_failed"
        finally:
            with self._lock:
                self._checking = False
            self._operation.release()
        return self.status()

    def start_download(self, release_token, *, on_ready=None):
        if not self._operation.acquire(blocking=False):
            raise UpdateError("已有更新操作正在执行。", "busy")
        try:
            state = self.status()
            if not state["canDownload"] or not isinstance(release_token, str) or release_token != (self._latest or {}).get("releaseToken"):
                raise UpdateError("下载需使用本次成功检查的新版本，请重新检查。", "stale_release")
            release = dict(self._latest)
            self._cancel.clear()
            with self._lock:
                self._installation_expected_id = ""
                self._installation = {"state": "idle"}
                self._download = {"state": "downloading", "version": release["version"], "name": release["name"],
                                  "downloadedBytes": 0, "totalBytes": release["size"], "verified": False, "error": "",
                                  "autoInstallQueued": bool(on_ready)}
            self._thread = threading.Thread(target=self._download_worker, args=(release, on_ready), daemon=True, name="app-update-download")
            self._thread.start()
        except Exception:
            self._operation.release()
            raise
        return self.status()

    def _check_cancel(self, deadline):
        if self._cancel.is_set():
            raise UpdateError("下载已取消。", "cancelled")
        if time.monotonic() >= deadline:
            raise UpdateError("下载超过时间上限。", "timeout")

    def _verify_file(self, path, release):
        path = _regular_path(path, missing=False)
        if not path.is_file() or path.stat().st_size != release["size"]:
            raise UpdateError("下载文件大小不一致。", "size_mismatch")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != release["sha256"]:
            raise UpdateError("下载文件 SHA-256 不一致。", "checksum_mismatch")
        return path

    def _download_worker(self, release, on_ready=None):
        temporary = None
        stream = None
        deadline = time.monotonic() + DOWNLOAD_TIMEOUT
        try:
            directory = _regular_path(self.download_dir)
            directory.mkdir(parents=True, exist_ok=True)
            self._cleanup_expired_parts(directory)
            version = re.sub(r"[^0-9A-Za-z.-]", "_", release["version"])
            target = _regular_path(directory / f"{version}-{release['sha256'][:12]}-{release['name']}")
            reuse = False
            if target.exists():
                try:
                    self._verify_file(target, release)
                    reuse = True
                except UpdateError as exc:
                    if exc.code not in {"size_mismatch", "checksum_mismatch"}:
                        raise
            if not reuse:
                self._check_cancel(deadline)
                stream = self.fetcher.open(release["url"], set(release["allowedHosts"]), timeout=DOWNLOAD_TIMEOUT, cancel=self._cancel)
                with self._lock:
                    self._active_stream = stream
                self._check_cancel(deadline)
                length = stream.headers.get("Content-Length")
                if length is not None and (not str(length).isdigit() or int(length) != release["size"]):
                    raise UpdateError("下载响应大小与发布信息不一致。", "size_mismatch")
                descriptor, temporary = tempfile.mkstemp(prefix=".app-update-", suffix=".part", dir=directory)
                digest, received = hashlib.sha256(), 0
                with os.fdopen(descriptor, "wb") as output:
                    while True:
                        self._check_cancel(deadline)
                        chunk = stream.read(min(1024 * 1024, release["size"] + 1 - received))
                        self._check_cancel(deadline)
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > release["size"]:
                            raise UpdateError("下载超过发布文件大小，已停止。", "size_mismatch")
                        output.write(chunk)
                        digest.update(chunk)
                        with self._lock:
                            self._download["downloadedBytes"] = received
                    if received != release["size"]:
                        raise UpdateError("下载未完成，文件大小不一致。", "size_mismatch")
                    if digest.hexdigest() != release["sha256"]:
                        raise UpdateError("下载校验失败：SHA-256 不一致。", "checksum_mismatch")
                    output.flush()
                    os.fsync(output.fileno())
                self._check_cancel(deadline)
                _regular_path(target)
                os.replace(temporary, target)
                temporary = None
            self._check_cancel(deadline)
            with self._lock:
                self._download.update(state="ready", verified=True, downloadedBytes=release["size"],
                                      path=str(target), sha256=release["sha256"], verifiedAt=_now())
        except Exception as exc:
            cancelled = self._cancel.is_set() or isinstance(exc, UpdateError) and exc.code == "cancelled"
            with self._lock:
                self._download.update(state="cancelled" if cancelled else "failed", verified=False,
                    error="下载已取消。" if cancelled else str(exc) if isinstance(exc, UpdateError) else "下载失败，未生成可用更新文件。",
                    errorCode="cancelled" if cancelled else exc.code if isinstance(exc, UpdateError) else "download_failed")
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
            with self._lock:
                self._active_stream = None
            self._operation.release()

        if on_ready and not self._closed and not self._cancel.is_set() and self._download.get("state") == "ready":
            try:
                on_ready()
            except Exception as exc:
                with self._lock:
                    self._installation = {"state": "failed", "message": str(exc)[:700]}

    def _cleanup_expired_parts(self, directory):
        """Only our exact mkstemp pattern in the fixed folder, older than a day."""
        cutoff = time.time() - 24 * 60 * 60
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index >= 1000:
                    break
                if not re.fullmatch(r"\.app-update-[a-z0-9_]{8}\.part", entry.name):
                    continue
                try:
                    path = _regular_path(directory / entry.name, missing=False)
                    info = path.stat()
                    if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                        path.unlink()
                except (UpdateError, OSError):
                    continue

    def cancel_download(self):
        self._cancel.set()
        with self._lock:
            stream = self._active_stream
        if stream is not None:
            try:
                stream.abort()
            except (OSError, ValueError):
                pass
        return self.status()

    def close(self, timeout=2.0):
        self._closed = True
        self.cancel_download()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(max(0.0, min(float(timeout), 15.0)))
        return not bool(thread and thread.is_alive())

    def verified_download_path(self):
        with self._lock:
            download = dict(self._download)
        if download.get("state") != "ready" or not download.get("verified"):
            raise UpdateError("还没有完成校验的更新文件。", "download_not_ready")
        path = Path(download["path"])
        directory = _regular_path(self.download_dir)
        if path.parent != directory:
            raise UpdateError("下载文件越出更新目录。", "unsafe_path")
        try:
            return self._verify_file(path, {"size": download["totalBytes"], "sha256": download["sha256"]})
        except UpdateError:
            with self._lock:
                self._download.update(state="failed", verified=False, error="下载文件已变化，请重新下载。")
            raise

    def _read_installation_result(self):
        if self._installation_expected_id == "":
            return
        try:
            root = _regular_path(self.config_path.parent / "app-update-install")
            pointer = _read_json(root / "latest.json", 8192)
            if not isinstance(pointer, dict) or not re.fullmatch(r"[a-f0-9]{16,64}", str(pointer.get("installId") or "")):
                return
            path = _regular_path(Path(str(pointer.get("path") or "")))
            if path.name != "result.json" or path.parent.parent.resolve() != root.resolve() or not re.fullmatch(r"[a-f0-9]{16,64}", path.parent.name):
                return
            result = _read_json(path, 65536)
            if not isinstance(result, dict) or result.get("installId") != pointer["installId"]:
                return
            if self._installation_expected_id and result.get("installId") != self._installation_expected_id:
                return
            if result.get("state") not in {"waiting_for_exit", "installed", "complete", "failed"}:
                return
            with self._lock:
                self._installation = {key: result[key] for key in ("state", "message", "backup", "restart") if key in result}
        except (UpdateError, OSError, ValueError):
            return

    def monitor_installation(self, result_path, install_id):
        """The caller transfers its acquired operation lock to this monitor."""
        with self._lock:
            self._installation = {"state": "waiting_for_exit", "message": "正在恢复配置并准备重启"}
        _atomic_json(self.config_path.parent / "app-update-install" / "latest.json",
                     {"path": str(Path(result_path).absolute()), "installId": install_id})
        self._installation_expected_id = install_id

        def watch():
            deadline = time.monotonic() + 420
            try:
                while not self._closed and time.monotonic() < deadline:
                    try:
                        result = _read_json(Path(result_path), 65536)
                    except (UpdateError, OSError):
                        result = None
                    if isinstance(result, dict) and result.get("installId") == install_id:
                        state = str(result.get("state") or "")
                        with self._lock:
                            self._installation = {key: result[key] for key in ("state", "message", "backup", "restart") if key in result}
                        if state in {"complete", "failed"}:
                            return
                    time.sleep(0.5)
                if not self._closed:
                    with self._lock:
                        self._installation = {"state": "failed", "message": "更新助手未确认完成，请查看安装结果和备份后重试。"}
            finally:
                self._operation.release()

        threading.Thread(target=watch, daemon=True, name="app-update-install-monitor").start()
