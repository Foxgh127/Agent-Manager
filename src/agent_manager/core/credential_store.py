"""Secrets services."""
from __future__ import annotations
from agent_manager import core as _core


class DataBlob(_core.ctypes.Structure):
    _fields_ = [("cbData", _core.wintypes.DWORD), ("pbData", _core.ctypes.POINTER(_core.ctypes.c_ubyte))]



def _make_blob(data: bytes) -> tuple[_core.DataBlob, _core.ctypes.Array]:
    buffer = _core.ctypes.create_string_buffer(data, len(data))
    return _core.DataBlob(len(data), _core.ctypes.cast(buffer, _core.ctypes.POINTER(_core.ctypes.c_ubyte))), buffer



def dpapi_protect(secret: str) -> bytes:
    if _core.os.name != "nt":
        raise _core.ManagerError("DPAPI 密钥存储仅支持 Windows。")
    in_blob, in_buffer = _core._make_blob(secret.encode("utf-8"))
    out_blob = _core.DataBlob()
    ok = _core.ctypes.windll.crypt32.CryptProtectData(
        _core.ctypes.byref(in_blob), _core.APP_NAME, None, None, None, 0x1, _core.ctypes.byref(out_blob)
    )
    del in_buffer
    if not ok:
        raise _core.ctypes.WinError()
    try:
        return _core.ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _core.ctypes.windll.kernel32.LocalFree(out_blob.pbData)



def dpapi_unprotect(ciphertext: bytes) -> str:
    if _core.os.name != "nt":
        raise _core.ManagerError("DPAPI 密钥存储仅支持 Windows。")
    in_blob, in_buffer = _core._make_blob(ciphertext)
    out_blob = _core.DataBlob()
    description = _core.wintypes.LPWSTR()
    ok = _core.ctypes.windll.crypt32.CryptUnprotectData(
        _core.ctypes.byref(in_blob), _core.ctypes.byref(description), None, None, None, 0x1, _core.ctypes.byref(out_blob)
    )
    del in_buffer
    if not ok:
        raise _core.ctypes.WinError()
    try:
        return _core.ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8")
    finally:
        if description:
            _core.ctypes.windll.kernel32.LocalFree(description)
        _core.ctypes.windll.kernel32.LocalFree(out_blob.pbData)



def _secret_store() -> dict:
    with _core.SECRETS_LOCK:
        payload = _core.read_json(
            _core.SECRETS_FILE,
            {
                "scheme": "windows-dpapi-current-user-v1",
                "providers": {},
                "accounts": {},
                "services": {},
                "relayAccounts": {},
            },
        )
    if not isinstance(payload, dict) or payload.get("scheme") != "windows-dpapi-current-user-v1":
        raise _core.ManagerError("Provider 密钥文件格式无效。")
    if not isinstance(payload.get("providers"), dict):
        payload["providers"] = {}
    if not isinstance(payload.get("accounts"), dict):
        payload["accounts"] = {}
    if not isinstance(payload.get("services"), dict):
        payload["services"] = {}
    if not isinstance(payload.get("relayAccounts"), dict):
        payload["relayAccounts"] = {}
    return payload



def _relay_secret_key_id(value: object) -> str:
    key_id = str(value or "").strip()
    if not key_id or len(key_id) > 120 or any(ord(char) < 32 for char in key_id):
        raise _core.ManagerError("中转站 Key ID 无效。")
    return key_id



def _relay_account_secret_keys(payload: dict, account_id: str) -> dict:
    account_store = payload.setdefault("relayAccounts", {}).get(account_id)
    if not isinstance(account_store, dict):
        return {}
    keys = account_store.get("keys")
    return keys if isinstance(keys, dict) else {}



def load_relay_account_key(account_id: str, key_id: str, required: bool = False) -> str | None:
    account_id = _core.slugify(account_id, "中转站账号 ID")
    normalized_key_id = _core._relay_secret_key_id(key_id)
    encoded = _core._relay_account_secret_keys(_core._secret_store(), account_id).get(normalized_key_id)
    if not encoded:
        if required:
            raise _core.ManagerError("该中转站 Key 尚未安全导入，请重新登录同步账号。")
        return None
    try:
        return _core.dpapi_unprotect(_core.base64.b64decode(encoded, validate=True))
    except Exception as exc:
        raise _core.ManagerError("无法解密该中转站账号的 API Key。") from exc



def relay_account_key_configured(account_id: str, key_id: str) -> bool:
    try:
        account_id = _core.slugify(account_id, "中转站账号 ID")
        normalized_key_id = _core._relay_secret_key_id(key_id)
        return bool(_core._relay_account_secret_keys(_core._secret_store(), account_id).get(normalized_key_id))
    except _core.ManagerError:
        return False



def _normalize_relay_dashboard_session(value: object) -> dict:
    """Validate the encrypted relay-dashboard credential payload.

    Dashboard credentials never belong in settings.json or an export bundle.
    This normalized record is encrypted as one DPAPI blob under the relay
    account's existing secret-store entry.
    """

    if not isinstance(value, dict):
        raise _core.ManagerError("中转站网页登录凭据格式无效。")
    origin_value = str(value.get("origin") or "").strip()
    portal_value = str(value.get("portalUrl") or origin_value).strip()
    origin_url = _core._validated_provider_portal_url(origin_value)
    portal_url = _core._validated_provider_portal_url(portal_value)
    if _core._provider_url_origin(origin_url) != _core._provider_url_origin(portal_url):
        raise _core.ManagerError("中转站网页登录凭据与网站地址不属于同一来源。")
    adapter = str(value.get("adapter") or "").strip()
    if adapter not in {"new-api", "sub2api"}:
        raise _core.ManagerError("中转站网页登录凭据类型不受支持。")
    access_token = str(value.get("accessToken") or "").strip()
    refresh_token = str(value.get("refreshToken") or "").strip()
    if len(access_token) > 16_384 or len(refresh_token) > 16_384:
        raise _core.ManagerError("中转站网页登录凭据长度异常。")
    if any(character in credential for credential in (access_token, refresh_token, str(value.get("sessionId") or "")) for character in "\r\n\0"):
        raise _core.ManagerError("中转站网页登录凭据含无效控制字符。")
    cookies: list[dict] = []
    raw_cookies = value.get("cookies") if isinstance(value.get("cookies"), list) else []
    for item in raw_cookies[:80]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        cookie_value = str(item.get("value") or "")
        domain = str(item.get("domain") or "").strip().casefold()
        path = str(item.get("path") or "/").strip() or "/"
        if (
            not name
            or len(name) > 256
            or len(cookie_value) > 16_384
            or len(domain) > 253
            or len(path) > 2_048
            or any(ord(char) < 32 for char in name)
        ):
            continue
        cookies.append(
            {
                "name": name,
                "value": cookie_value,
                "domain": domain,
                "path": path,
                "secure": bool(item.get("secure")),
                "httpOnly": bool(item.get("httpOnly")),
                "hostOnly": bool(item.get("hostOnly")),
                "expires": str(item.get("expires") or "")[:160],
            }
        )
    if not access_token and not refresh_token and not cookies:
        raise _core.ManagerError("没有识别到可保存的中转站网页登录凭据。")
    return {
        "version": 1,
        "origin": _core.urllib.parse.urlunsplit((*_core.urllib.parse.urlsplit(origin_url)[:2], "", "", "")),
        "portalUrl": portal_url,
        "adapter": adapter,
        "authMode": str(value.get("authMode") or "bearer")[:20],
        "userId": str(value.get("userId") or "")[:120],
        "sessionId": str(value.get("sessionId") or "")[:256],
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "cookies": cookies,
        "updatedAt": str(value.get("updatedAt") or _core.now_iso())[:80],
    }



def store_relay_account_dashboard_session(account_id: str, session: dict) -> None:
    """Persist one relay login session in the current Windows user's DPAPI vault."""

    account_id = _core.slugify(account_id, "中转站账号 ID")
    normalized = _core._normalize_relay_dashboard_session(session)
    serialized = _core.json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > 256 * 1024:
        raise _core.ManagerError("中转站网页登录凭据体积异常，已拒绝保存。")
    with _core.SETTINGS_LOCK, _core.SECRETS_LOCK, _core._settings_file_lock():
        settings = _core.load_settings()
        account = next(
            (
                item
                for item in settings.get("relayAccounts", [])
                if str(item.get("id") or "") == account_id
            ),
            None,
        )
        if not account:
            raise _core.ManagerError("中转站账号不存在，无法保存网页登录凭据。")
        account_origin = _core._validated_provider_portal_url(
            account.get("origin") or account.get("portalUrl")
        )
        if _core._provider_url_origin(account_origin) != _core._provider_url_origin(normalized["origin"]):
            raise _core.ManagerError("网页登录凭据与中转站账号不匹配，已拒绝保存。")
        payload = _core._secret_store()
        account_store = payload.setdefault("relayAccounts", {}).setdefault(account_id, {})
        if not isinstance(account_store, dict):
            account_store = {}
            payload["relayAccounts"][account_id] = account_store
        account_store.setdefault("keys", {})
        account_store["dashboardSession"] = _core.base64.b64encode(_core.dpapi_protect(serialized)).decode("ascii")
        account_store["dashboardSessionUpdatedAt"] = normalized["updatedAt"]
        account_store["updatedAt"] = _core.now_iso()
        _core.atomic_write_json(_core.SECRETS_FILE, payload)



def load_relay_account_dashboard_session(account_id: str, required: bool = False) -> dict | None:
    account_id = _core.slugify(account_id, "中转站账号 ID")
    account_store = _core._secret_store().setdefault("relayAccounts", {}).get(account_id)
    encoded = account_store.get("dashboardSession") if isinstance(account_store, dict) else None
    if not encoded:
        if required:
            raise _core.ManagerError("该中转站账号尚未保存网页登录凭据，请重新登录一次。")
        return None
    try:
        raw = _core.dpapi_unprotect(_core.base64.b64decode(encoded, validate=True))
        return _core._normalize_relay_dashboard_session(_core.json.loads(raw))
    except _core.ManagerError:
        raise
    except Exception as exc:
        raise _core.ManagerError("无法解密该中转站账号的网页登录凭据，请重新登录一次。") from exc



def relay_account_dashboard_session_configured(account_id: str) -> bool:
    try:
        account_id = _core.slugify(account_id, "中转站账号 ID")
        account_store = _core._secret_store().setdefault("relayAccounts", {}).get(account_id)
        return bool(isinstance(account_store, dict) and account_store.get("dashboardSession"))
    except _core.ManagerError:
        return False



def store_service_secret(service_id: str, secret: str) -> None:
    service_id = _core.slugify(service_id, "服务 ID")
    if not secret.strip():
        raise _core.ManagerError("服务密钥不能为空。")
    with _core.SECRETS_LOCK, _core._settings_file_lock():
        payload = _core._secret_store()
        payload["services"][service_id] = _core.base64.b64encode(_core.dpapi_protect(secret.strip())).decode("ascii")
        _core.atomic_write_json(_core.SECRETS_FILE, payload)



def load_service_secret(service_id: str, required: bool = False) -> str | None:
    service_id = _core.slugify(service_id, "服务 ID")
    encoded = _core._secret_store()["services"].get(service_id)
    if not encoded:
        if required:
            raise _core.ManagerError("本地 API 服务尚未生成访问密钥。")
        return None
    try:
        return _core.dpapi_unprotect(_core.base64.b64decode(encoded, validate=True))
    except Exception as exc:
        raise _core.ManagerError("无法解密本地 API 服务密钥。") from exc



def service_secret_configured(service_id: str) -> bool:
    try:
        return bool(_core._secret_store()["services"].get(_core.slugify(service_id, "服务 ID")))
    except _core.ManagerError:
        return False



def rotate_web2api_key() -> str:
    secret = f"cam_{_core.secrets_token(42)}"
    _core.store_service_secret("web2api", secret)
    return secret



def ensure_internal_gateway_secret() -> str:
    """Keep Codex's private routing credential separate from the exported key."""
    with _core.SECRETS_LOCK, _core._settings_file_lock():
        secret = _core.load_service_secret("gateway_internal")
        public_secret = _core.load_service_secret("web2api")
        if secret and secret != public_secret:
            return secret
        secret = f"cam_internal_{_core.secrets_token(48)}"
        _core.store_service_secret("gateway_internal", secret)
        return secret



def secrets_token(length: int = 32) -> str:
    """Return a URL-safe local secret without importing the app runtime."""
    return _core.base64.urlsafe_b64encode(_core.os.urandom(max(24, length))).decode("ascii").rstrip("=")[:length]



def store_provider_key(provider_id: str, secret: str) -> None:
    provider_id = _core.slugify(provider_id, "Provider ID")
    normalized_secret = _core._validated_provider_secret(secret)
    with _core.SETTINGS_LOCK, _core.SECRETS_LOCK, _core._settings_file_lock():
        settings = _core.load_settings()
        provider = next(
            (
                item
                for item in settings.get("providers", [])
                if isinstance(item, dict) and str(item.get("id") or "") == provider_id
            ),
            None,
        )
        if not provider or provider.get("kind") != "custom":
            raise _core.ManagerError(f"Provider `{provider_id}` 不存在或不能配置 API Key。")
        payload = _core._secret_store()
        old_secret = ""
        encoded = payload.get("providers", {}).get(provider_id)
        if encoded:
            try:
                old_secret = _core.dpapi_unprotect(_core.base64.b64decode(encoded, validate=True))
            except Exception as exc:
                raise _core.ManagerError(f"无法解密 Provider `{provider_id}` 的现有 API Key。") from exc
        if old_secret == normalized_secret:
            return
        snapshot = _core._capture_file_bytes((_core.SETTINGS_FILE, _core.SECRETS_FILE))
        payload["providers"][provider_id] = _core.base64.b64encode(
            _core.dpapi_protect(normalized_secret)
        ).decode("ascii")
        current_revision, applied_revision = _core._provider_runtime_revisions(provider)
        provider["runtimeRevision"] = current_revision + 1
        provider["appliedRuntimeRevision"] = applied_revision
        provider["updatedAt"] = _core.now_iso()
        try:
            _core.save_settings(settings)
            _core.atomic_write_json(_core.SECRETS_FILE, payload)
        except Exception:
            _core._restore_file_bytes(snapshot)
            raise



def load_provider_key(provider_id: str, required: bool = False) -> str | None:
    provider = _core.provider_by_id(provider_id)
    if provider.get("sourceType") == "relay_account" and provider.get("relayAccountId") and provider.get("relayKeyId"):
        # The selected website Key is explicit. An inherited environment from
        # Cockpit or an earlier Manager process must not substitute another Key.
        selected_key = _core.load_relay_account_key(provider["relayAccountId"], provider["relayKeyId"], required=required)
        return selected_key
    env_key = provider.get("envKey")
    payload = _core._secret_store()
    encoded = payload["providers"].get(provider_id)
    if not encoded:
        if env_key and _core.os.environ.get(env_key):
            return _core.os.environ[env_key]
        if required:
            raise _core.ManagerError(f"Provider `{provider_id}` 尚未配置 API Key。")
        return None
    try:
        return _core.dpapi_unprotect(_core.base64.b64decode(encoded, validate=True))
    except Exception as exc:
        raise _core.ManagerError(f"无法解密 Provider `{provider_id}` 的 API Key。") from exc



def delete_provider_key(provider_id: str) -> None:
    with _core.SECRETS_LOCK, _core._settings_file_lock():
        payload = _core._secret_store()
        payload["providers"].pop(provider_id, None)
        _core.atomic_write_json(_core.SECRETS_FILE, payload)



def provider_key_configured(provider_id: str) -> bool:
    try:
        provider = _core.provider_by_id(provider_id)
        if provider.get("kind") == "builtin":
            return True
        if provider.get("sourceType") == "relay_account" and provider.get("relayAccountId") and provider.get("relayKeyId"):
            return _core.relay_account_key_configured(provider["relayAccountId"], provider["relayKeyId"])
        env_key = provider.get("envKey")
        return bool((env_key and _core.os.environ.get(env_key)) or _core._secret_store()["providers"].get(provider_id))
    except _core.ManagerError:
        return False

