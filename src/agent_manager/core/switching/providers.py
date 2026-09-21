"""Provider utilities and URL validation"""
from __future__ import annotations
from agent_manager import core as _core

def provider_by_id(provider_id: str, settings: dict | None = None) -> dict:
    settings = settings or _core.load_settings()
    provider = next((item for item in settings.get("providers", []) if item.get("id") == provider_id), None)
    if not provider:
        raise _core.ManagerError(f"找不到 Provider：{provider_id}")
    return provider



def _provider_portal_preset(preset_id: object, *, allow_empty: bool = False) -> dict | None:
    normalized = str(preset_id or "").strip().casefold()
    if not normalized and allow_empty:
        return None
    preset = next((item for item in _core.PROVIDER_PORTAL_PRESETS if item["id"] == normalized), None)
    if preset is None:
        raise _core.ManagerError("旧版中转站兼容记录无效，请重新填写 API 信息。")
    return preset



def _url_hostname(value: object) -> str:
    try:
        return str(_core.urllib.parse.urlsplit(str(value or "").strip()).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""



def detect_provider_portal_preset(*values: object) -> dict | None:
    """Match only exact curated hosts; lookalike suffixes must never match."""

    hostnames = {_core._url_hostname(value) for value in values}
    hostnames.discard("")
    for preset in _core.PROVIDER_PORTAL_PRESETS:
        allowed = {str(host).casefold().rstrip(".") for host in preset.get("hosts", ())}
        if hostnames & allowed:
            return preset
    return None



def _validated_provider_url(
    value: object,
    label: str,
    *,
    allow_empty: bool = False,
    allow_query: bool = False,
) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text and allow_empty:
        return ""
    if len(text.encode("utf-8", errors="replace")) > 4_096:
        raise _core.ManagerError(f"{label} 过长，已拒绝连接。")
    try:
        parsed = _core.urllib.parse.urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise _core.ManagerError(f"{label} 端口或 URL 格式无效。") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise _core.ManagerError(f"{label} 必须是完整的 HTTP(S) 地址。")
    if parsed.username is not None or parsed.password is not None:
        raise _core.ManagerError(f"{label} 不能在 URL 中包含用户名或密码。")
    if parsed.fragment:
        raise _core.ManagerError(f"{label} 不能包含 #fragment。")
    if parsed.query and not allow_query:
        raise _core.ManagerError(f"{label} 不能包含查询参数。")
    if port is not None and not 1 <= port <= 65535:
        raise _core.ManagerError(f"{label} 端口必须在 1 到 65535 之间。")
    hostname = str(parsed.hostname).strip().casefold().rstrip(".")
    loopback = hostname == "localhost"
    literal_address = None
    try:
        literal_address = _core.ipaddress.ip_address(hostname)
        loopback = literal_address.is_loopback
    except ValueError:
        pass
    if literal_address is not None and not loopback and (
        literal_address.is_unspecified
        or literal_address.is_link_local
        or literal_address.is_multicast
        or literal_address.is_reserved
    ):
        raise _core.ManagerError(f"{label} 指向不安全的保留或链路本地地址，已拒绝连接。")
    if parsed.scheme == "http" and not loopback:
        raise _core.ManagerError(f"{label} 会携带 API Key，远程地址必须使用 HTTPS；只有本机回环地址允许 HTTP。")
    return text



def _validated_provider_portal_url(
    value: object,
    *,
    allow_empty: bool = False,
    preset: dict | None = None,
) -> str:
    portal_url = _core._validated_provider_url(
        value,
        "官网 / 控制台地址",
        allow_empty=allow_empty,
    )
    if not portal_url:
        return ""
    if preset is not None:
        hostname = _core._url_hostname(portal_url)
        allowed = {str(host).casefold().rstrip(".") for host in preset.get("hosts", ())}
        if hostname not in allowed:
            raise _core.ManagerError("官网 / 控制台地址与所选快捷模板不匹配，已拒绝打开可疑站点。")
    return portal_url



def provider_portal_url(provider_id: str) -> str:
    provider = _core.provider_by_id(provider_id)
    portal_url = _core._validated_provider_portal_url(
        provider.get("portalUrl"),
        allow_empty=True,
        preset=_core._provider_portal_preset(provider.get("presetId"), allow_empty=True),
    )
    if not portal_url:
        raise _core.ManagerError("该中转站尚未配置官网 / 控制台地址。")
    return portal_url



def _provider_url_origin(value: str) -> tuple[str, str, int]:
    """Return a normalized origin for an already validated provider URL."""
    try:
        parsed = _core.urllib.parse.urlsplit(value)
        scheme = parsed.scheme.casefold()
        hostname = str(parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port or (443 if scheme == "https" else 80)
    except (TypeError, ValueError) as exc:
        raise _core.ManagerError("远端服务返回了无效的跳转地址，已拒绝继续发送凭据。") from exc
    if scheme not in {"http", "https"} or not hostname or not 1 <= int(port) <= 65_535:
        raise _core.ManagerError("远端服务返回了无效的跳转地址，已拒绝继续发送凭据。")
    return scheme, hostname, port



class _SameOriginRedirectHandler(_core.urllib.request.HTTPRedirectHandler):
    """Keep redirects on the credential's original origin.

    ``urllib`` copies ordinary request headers, including ``Authorization``,
    when it follows a redirect. Provider endpoints are user-configurable, so
    a cross-origin redirect must be rejected before a credential can leave the
    configured service.
    """

    def __init__(self, original_url: str):
        super().__init__()
        self._origin = _core._provider_url_origin(original_url)

    def redirect_request(self, request, fp, code, message, headers, new_url):
        if _core._provider_url_origin(new_url) != self._origin:
            raise _core.ManagerError("远端服务尝试跳转到其他来源，已拒绝继续发送凭据。")
        return super().redirect_request(request, fp, code, message, headers, new_url)



def _open_same_origin_request(request: _core.urllib.request.Request, *, timeout: float):
    """Open one request while rejecting cross-origin redirects."""

    opener = _core.urllib.request.build_opener(
        _core.urllib.request.ProxyHandler(_core._effective_url_proxies()),
        _core._SameOriginRedirectHandler(request.full_url),
    )
    return opener.open(request, timeout=timeout)



def _effective_url_proxies() -> dict[str, str]:
    """Return environment proxies with a Windows system-proxy fallback.

    CPython stops consulting the Windows Internet Settings registry as soon as
    *any* proxy-related environment variable exists. A parent process that
    exports only ``NO_PROXY`` therefore makes ``urllib`` silently bypass an
    otherwise enabled Windows HTTPS proxy. Codex Desktop still follows the
    system proxy in that situation, which made account quota probes time out
    while the same account continued to work in Codex.

    Keep explicit HTTP(S) environment proxies authoritative, then fill only
    missing schemes from the current Windows registry. The mapping is read on
    every opener construction so changing VPN/proxy modes does not require an
    Agent Manager restart.
    """

    try:
        proxies = {
            str(key).casefold(): str(value)
            for key, value in _core.urllib.request.getproxies().items()
            if str(key).strip() and str(value).strip()
        }
    except (OSError, ValueError):
        proxies = {}
    if _core.os.name != "nt":
        return proxies
    registry_reader = getattr(_core.urllib.request, "getproxies_registry", None)
    if not callable(registry_reader):
        return proxies
    try:
        registry_proxies = registry_reader()
    except (OSError, ValueError):
        registry_proxies = {}
    if isinstance(registry_proxies, dict):
        for key, value in registry_proxies.items():
            scheme = str(key).casefold().strip()
            endpoint = str(value).strip()
            if scheme and endpoint and scheme not in proxies:
                proxies[scheme] = endpoint
    return proxies



def _validated_provider_related_url(
    value: object,
    base_url: str,
    label: str,
    *,
    allow_empty: bool = False,
    allow_query: bool = False,
) -> str:
    related = _core._validated_provider_url(
        value,
        label,
        allow_empty=allow_empty,
        allow_query=allow_query,
    )
    if not related:
        return ""
    if _core._provider_url_origin(related) != _core._provider_url_origin(base_url):
        raise _core.ManagerError(f"{label} 必须与 Base URL 使用同一来源，避免 API Key 被发送到其他站点。")
    return related
