#!/usr/bin/env python3
"""Local TOTP and read-only mailbox utilities without background polling."""

from __future__ import annotations

import base64
import binascii
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.header import decode_header
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
import hashlib
import hmac
from html import unescape
import imaplib
import io
import itertools
import json
import math
from pathlib import Path
import re
import secrets
import ssl
import threading
import time
from typing import Any, Callable, Iterable, Mapping
import urllib.error
import urllib.parse
import urllib.request

import agent_manager_core as core


TOTP_ALGORITHMS = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}
EMAIL_STORE_SCHEMA = "toolbox-email-accounts-dpapi-v1"
TOTP_STORE_SCHEMA = "toolbox-totp-items-dpapi-v1"
DEFAULT_EMAIL_STORE = core.STATE_DIR / "toolbox-email-accounts.json"
DEFAULT_TOTP_STORE = core.STATE_DIR / "toolbox-totp-items.json"
MAX_IMPORT_BYTES = 2_000_000
MAX_IMPORT_ACCOUNTS = 1_000
MAX_TOKEN_RESPONSE_BYTES = 256_000
MAX_FETCH_MESSAGES = 100
MAX_MESSAGE_BYTES = 1_000_000
MAX_TOTAL_MESSAGE_BYTES = 5_000_000
MAX_TEXT_CHARS = 20_000
DEFAULT_NETWORK_TIMEOUT = 12.0
MAX_STORE_RECORDS = 1_000
MAX_STORE_BYTES = MAX_IMPORT_BYTES * 8


class _OAuthRefreshState:
    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users = 0


_EMAIL_STORE_LOCK = threading.RLock()
_TOTP_STORE_LOCK = threading.RLock()
_OAUTH_CACHE_LOCK = threading.RLock()
_OAUTH_ACCESS_TOKEN_CACHE: dict[str, tuple[float, str]] = {}
_OAUTH_REFRESH_LOCKS: dict[str, _OAuthRefreshState] = {}
_OAUTH_CACHE_GENERATION = 0
_EMAIL_CREDENTIAL_GENERATION = 0
_TOTP_ID_PATTERN = re.compile(r"totp_[a-f0-9]{16}")
_EMAIL_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")


class ToolboxError(RuntimeError):
    """Base error with a message safe for a UI or local API response."""


class ValidationError(ToolboxError):
    pass


class CredentialStoreError(ToolboxError):
    pass


class MailAccessError(ToolboxError):
    pass


class _DuplicateJSONKeyError(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError("duplicate JSON key")
        result[key] = value
    return result


def _json_loads(value: str | bytes) -> Any:
    return json.loads(value, object_pairs_hook=_unique_json_object)


@dataclass(frozen=True)
class TOTPConfig:
    secret: bytes
    algorithm: str = "SHA1"
    digits: int = 6
    period: int = 30
    label: str = ""
    issuer: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "digits": self.digits,
            "period": self.period,
            "label": self.label,
            "issuer": self.issuer,
        }


def _validate_totp_config(config: TOTPConfig) -> TOTPConfig:
    if not isinstance(config.secret, bytes) or not config.secret:
        raise ValidationError("TOTP 密钥必须是非空字节数据。")
    if len(config.secret) > 128:
        raise ValidationError("TOTP 密钥过长。")
    if not isinstance(config.algorithm, str) or config.algorithm.upper() not in TOTP_ALGORITHMS:
        raise ValidationError("TOTP algorithm 仅支持 SHA1、SHA256 或 SHA512。")
    if isinstance(config.digits, bool) or config.digits not in (6, 8):
        raise ValidationError("TOTP digits 仅支持 6 或 8。")
    if isinstance(config.period, bool) or not isinstance(config.period, int) or not 1 <= config.period <= 3_600:
        raise ValidationError("TOTP period 必须在 1 到 3600 秒之间。")
    if (not isinstance(config.label, str) or len(config.label) > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in config.label)):
        raise ValidationError("TOTP 账户标签格式无效。")
    if (not isinstance(config.issuer, str) or len(config.issuer) > 256
            or any(ord(char) < 32 or ord(char) == 127 for char in config.issuer)):
        raise ValidationError("TOTP issuer 格式无效。")
    return TOTPConfig(
        secret=config.secret,
        algorithm=config.algorithm.upper(),
        digits=config.digits,
        period=config.period,
        label=config.label,
        issuer=config.issuer,
    )


def _single_query_value(query: Mapping[str, list[str]], key: str, default: str | None = None) -> str | None:
    values = query.get(key)
    if values is None:
        return default
    if len(values) != 1:
        raise ValidationError(f"TOTP 参数 `{key}` 不能重复。")
    return values[0]


def _decode_base32_secret(value: str) -> bytes:
    if not isinstance(value, str):
        raise ValidationError("TOTP 密钥必须是文本。")
    compact = re.sub(r"[\s-]+", "", value).upper()
    if not compact:
        raise ValidationError("TOTP 密钥不能为空。")
    if len(compact) > 1_024:
        raise ValidationError("TOTP 密钥过长。")
    if "=" in compact:
        if not re.fullmatch(r"[A-Z2-7]+=*", compact):
            raise ValidationError("TOTP Base32 密钥格式无效。")
        compact = compact.rstrip("=")
    if not re.fullmatch(r"[A-Z2-7]+", compact):
        raise ValidationError("TOTP Base32 密钥只能包含 A-Z 和 2-7。")
    padded = compact + "=" * ((8 - len(compact) % 8) % 8)
    try:
        decoded = base64.b32decode(padded, casefold=False)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError("TOTP Base32 密钥格式无效。") from exc
    if not decoded:
        raise ValidationError("TOTP 密钥不能为空。")
    if len(decoded) > 128:
        raise ValidationError("TOTP 密钥过长。")
    return decoded


_TOTP_JSON_KEYS = {
    "secret",
    "totp",
    "totpsecret",
    "otpsecret",
    "otp",
    "otpauth",
    "otpurl",
    "twofa",
    "2fa",
}


def _totp_source_from_json(value: Any, *, depth: int = 0) -> str | None:
    if depth > 8:
        return None
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if normalized in _TOTP_JSON_KEYS and isinstance(item, str) and item.strip():
                return item.strip()
        for item in value.values():
            found = _totp_source_from_json(item, depth=depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for item in value[:1_000]:
            found = _totp_source_from_json(item, depth=depth + 1)
            if found:
                return found
    return None


def _unwrap_totp_source(source: str) -> str:
    value = source.strip().strip("\ufeff")
    fenced = re.fullmatch(r"```(?:json|text|txt)?\s*([\s\S]*?)\s*```", value, flags=re.IGNORECASE)
    if fenced:
        value = fenced.group(1).strip()
    if len(value) > 1_000_000:
        raise ValidationError("TOTP 输入过长。")

    if value[:1] in {"{", "[", '"'}:
        try:
            decoded = _json_loads(value)
        except _DuplicateJSONKeyError as exc:
            raise ValidationError("TOTP JSON 包含重复字段。") from exc
        except (json.JSONDecodeError, RecursionError):
            decoded = None
        if isinstance(decoded, str):
            value = decoded.strip()
        elif decoded is not None:
            extracted = _totp_source_from_json(decoded)
            if not extracted:
                raise ValidationError("JSON 中没有找到可识别的 TOTP 密钥字段。")
            value = extracted

    uri_match = re.search(r"otpauth(?:-migration)?://[^\s'\"<>]+", value, flags=re.IGNORECASE)
    if uri_match:
        return uri_match.group(0).rstrip(".,;，；")

    show_match = re.search(
        r"(?:https?://)?(?:www\.)?2fa\.show/2fa/([A-Za-z2-7=\s-]+)",
        value,
        flags=re.IGNORECASE,
    )
    if show_match:
        return show_match.group(1).strip().rstrip("/")

    key_value = re.search(
        r"(?:^|[\s,;，；])(?:totp[_ -]?secret|otp[_ -]?secret|secret|2fa|twofa|otp)\s*[:=]\s*"
        r"([^\s,;，；]+)",
        value,
        flags=re.IGNORECASE,
    )
    if key_value:
        return key_value.group(1).strip().strip("'\"")

    # Common account exports use either ``account----2FA`` or
    # ``account----password----2FA``.  Some clipboard sources render the
    # separator as two em/en dashes (``——``), so normalize all repeated dash
    # forms before deciding which field is the TOTP secret.  Validate the last
    # field instead of blindly treating a password as an OTP secret.
    bundle_parts = [
        part.strip()
        for part in re.split(r"\s*(?:-{2,}|_{2,}|[—–－]{2,})\s*", value)
        if part.strip()
    ]
    if len(bundle_parts) >= 2:
        candidate = bundle_parts[-1].strip().strip("'\"")
        if candidate.casefold().startswith(("otpauth://", "otpauth-migration://")):
            return candidate
        if re.search(r"(?:https?://)?(?:www\.)?2fa\.show/2fa/", candidate, flags=re.IGNORECASE):
            return candidate
        try:
            _decode_base32_secret(candidate)
        except ValidationError:
            pass
        else:
            return candidate
    for separator in ("\t", "|"):
        parts = [part.strip() for part in value.split(separator)]
        if len(parts) == 3 and all(parts):
            return parts[-1]
    return value.strip().strip("'\"")


def _protobuf_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift <= 63:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValidationError("Google Authenticator 迁移数据损坏。")


def _protobuf_fields(data: bytes) -> list[tuple[int, int, bytes | int]]:
    fields: list[tuple[int, int, bytes | int]] = []
    offset = 0
    while offset < len(data):
        tag, offset = _protobuf_varint(data, offset)
        number, wire_type = tag >> 3, tag & 7
        if number <= 0:
            raise ValidationError("Google Authenticator 迁移字段无效。")
        if wire_type == 0:
            value, offset = _protobuf_varint(data, offset)
        elif wire_type == 2:
            length, offset = _protobuf_varint(data, offset)
            if length < 0 or offset + length > len(data):
                raise ValidationError("Google Authenticator 迁移字段长度无效。")
            value = data[offset : offset + length]
            offset += length
        elif wire_type == 1:
            if offset + 8 > len(data):
                raise ValidationError("Google Authenticator 迁移字段不完整。")
            value = data[offset : offset + 8]
            offset += 8
        elif wire_type == 5:
            if offset + 4 > len(data):
                raise ValidationError("Google Authenticator 迁移字段不完整。")
            value = data[offset : offset + 4]
            offset += 4
        else:
            raise ValidationError("Google Authenticator 迁移包含不支持的字段。")
        fields.append((number, wire_type, value))
        if len(fields) > 10_000:
            raise ValidationError("Google Authenticator 迁移字段过多。")
    return fields


def _parse_google_authenticator_migration(value: str) -> list[TOTPConfig]:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.casefold() != "otpauth-migration" or parsed.netloc.casefold() != "offline":
        raise ValidationError("Google Authenticator 迁移链接格式无效。")
    try:
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, max_num_fields=16)
    except ValueError as exc:
        raise ValidationError("Google Authenticator 迁移链接参数过多或格式无效。") from exc
    values = query.get("data") or []
    if len(values) != 1 or not values[0]:
        raise ValidationError("Google Authenticator 迁移链接缺少 data 参数。")
    encoded = values[0].replace(" ", "+")
    if len(encoded) > 1_000_000:
        raise ValidationError("Google Authenticator 迁移数据过大。")
    try:
        payload = base64.b64decode(
            encoded + "=" * ((4 - len(encoded) % 4) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise ValidationError("Google Authenticator 迁移数据不是有效 Base64。") from exc
    if len(payload) > 750_000:
        raise ValidationError("Google Authenticator 迁移数据过大。")

    configs: list[TOTPConfig] = []
    algorithms = {0: "SHA1", 1: "SHA1", 2: "SHA256", 3: "SHA512"}
    digits_map = {0: 6, 1: 6, 2: 8}
    for number, wire_type, raw_parameters in _protobuf_fields(payload):
        if number != 1 or wire_type != 2 or not isinstance(raw_parameters, bytes):
            continue
        values_by_field: dict[int, bytes | int] = {}
        for field_number, _field_wire, field_value in _protobuf_fields(raw_parameters):
            values_by_field.setdefault(field_number, field_value)
        secret = values_by_field.get(1)
        otp_type = values_by_field.get(6, 2)
        if not isinstance(secret, bytes) or not secret:
            continue
        if otp_type == 1:
            raise ValidationError("暂不支持 HOTP 计数器账号；请导入 TOTP 账号。")
        if otp_type not in {0, 2}:
            raise ValidationError("Google Authenticator 迁移包含未知 OTP 类型。")
        algorithm_value = values_by_field.get(4, 1)
        digits_value = values_by_field.get(5, 1)
        if not isinstance(algorithm_value, int) or algorithm_value not in algorithms:
            raise ValidationError("Google Authenticator 迁移包含不支持的算法。")
        if not isinstance(digits_value, int) or digits_value not in digits_map:
            raise ValidationError("Google Authenticator 迁移包含不支持的验证码位数。")
        if len(secret) > 128:
            raise ValidationError("Google Authenticator 迁移密钥过长。")

        def text_field(field: int) -> str:
            raw = values_by_field.get(field, b"")
            if not isinstance(raw, bytes):
                return ""
            try:
                decoded = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValidationError("Google Authenticator 迁移标签不是有效 UTF-8。") from exc
            if (len(decoded) > 512
                    or any(ord(character) < 32 or ord(character) == 127 for character in decoded)):
                raise ValidationError("Google Authenticator 迁移标签格式无效。")
            return decoded

        configs.append(
            TOTPConfig(
                secret=secret,
                algorithm=algorithms[algorithm_value],
                digits=digits_map[digits_value],
                period=30,
                label=text_field(2),
                issuer=text_field(3),
            )
        )
        if len(configs) > 1_000:
            raise ValidationError("Google Authenticator 迁移账号过多。")
    if not configs:
        raise ValidationError("Google Authenticator 迁移中没有找到 TOTP 账号。")
    return configs


def parse_totp_source(source: str | TOTPConfig) -> TOTPConfig:
    """Parse common local TOTP exports without networking."""
    if isinstance(source, TOTPConfig):
        return _validate_totp_config(source)
    if not isinstance(source, str) or not source.strip():
        raise ValidationError("TOTP 输入不能为空。")
    value = _unwrap_totp_source(source)
    if value.casefold().startswith("otpauth-migration://"):
        configs = _parse_google_authenticator_migration(value)
        if len(configs) != 1:
            raise ValidationError(
                f"Google Authenticator 迁移中包含 {len(configs)} 个账号；请单独导出或选择一个账号。"
            )
        return _validate_totp_config(configs[0])
    if not value.lower().startswith("otpauth://"):
        return _validate_totp_config(TOTPConfig(secret=_decode_base32_secret(value)))
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() != "otpauth" or parsed.netloc.lower() != "totp":
        raise ValidationError("仅支持 otpauth://totp URI。")
    if parsed.fragment:
        raise ValidationError("TOTP URI 不能包含片段。")
    try:
        query = urllib.parse.parse_qs(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=64,
            errors="strict",
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValidationError("TOTP URI 查询参数格式无效。") from exc
    secret_text = _single_query_value(query, "secret")
    if not secret_text:
        raise ValidationError("TOTP URI 缺少 secret 参数。")
    algorithm = (_single_query_value(query, "algorithm", "SHA1") or "").upper()
    if algorithm not in TOTP_ALGORITHMS:
        raise ValidationError("TOTP algorithm 仅支持 SHA1、SHA256 或 SHA512。")
    try:
        digits = int(_single_query_value(query, "digits", "6") or "")
        period = int(_single_query_value(query, "period", "30") or "")
    except ValueError as exc:
        raise ValidationError("TOTP digits 和 period 必须是整数。") from exc
    if digits not in (6, 8):
        raise ValidationError("TOTP digits 仅支持 6 或 8。")
    if not 1 <= period <= 3_600:
        raise ValidationError("TOTP period 必须在 1 到 3600 秒之间。")
    try:
        label = urllib.parse.unquote(parsed.path.lstrip("/"), errors="strict")
    except UnicodeDecodeError as exc:
        raise ValidationError("TOTP 账户标签不是有效 UTF-8。") from exc
    if not label:
        raise ValidationError("TOTP URI 缺少账户标签。")
    if len(label) > 512 or any(ord(character) < 32 or ord(character) == 127 for character in label):
        raise ValidationError("TOTP 账户标签格式无效。")
    issuer = _single_query_value(query, "issuer", "") or ""
    if len(issuer) > 256 or any(ord(character) < 32 or ord(character) == 127 for character in issuer):
        raise ValidationError("TOTP issuer 格式无效。")
    label_issuer = label.split(":", 1)[0].strip() if ":" in label else ""
    if issuer and label_issuer and issuer.casefold() != label_issuer.casefold():
        raise ValidationError("TOTP 标签与 issuer 参数不一致。")
    return _validate_totp_config(TOTPConfig(
        secret=_decode_base32_secret(secret_text),
        algorithm=algorithm,
        digits=digits,
        period=period,
        label=label,
        issuer=issuer or label_issuer,
    ))


def _totp_time_counter(timestamp: float | int | None, period: int) -> tuple[float, int]:
    if isinstance(timestamp, bool):
        raise ValidationError("TOTP 时间戳必须是非负有限数值。")
    try:
        current = time.time() if timestamp is None else float(timestamp)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValidationError("TOTP 时间戳必须是非负有限数值。") from exc
    if not math.isfinite(current) or current < 0:
        raise ValidationError("TOTP 时间戳必须是非负有限数值。")
    counter = int(current // period)
    if counter > (1 << 64) - 1:
        raise ValidationError("TOTP 时间戳超出支持范围。")
    return current, counter


def generate_totp(source: str | TOTPConfig, timestamp: float | int | None = None) -> dict[str, Any]:
    """Return code, remaining seconds, and non-secret TOTP metadata."""
    config = parse_totp_source(source)
    current, counter = _totp_time_counter(timestamp, config.period)
    digest = hmac.new(config.secret, counter.to_bytes(8, "big"), TOTP_ALGORITHMS[config.algorithm]).digest()
    offset = digest[-1] & 0x0F
    binary = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    code = str(binary % (10**config.digits)).zfill(config.digits)
    remaining = max(1, int(math.ceil((counter + 1) * config.period - current)))
    return {"code": code, "seconds_remaining": remaining, **config.public_dict()}


def verify_totp(
    source: str | TOTPConfig,
    code: str,
    timestamp: float | int | None = None,
    window: int = 1,
) -> bool:
    """Constant-time verification over the current and nearby time windows."""
    config = parse_totp_source(source)
    if not isinstance(code, str) or not re.fullmatch(rf"[0-9]{{{config.digits}}}", code):
        return False
    if type(window) is not int or not 0 <= window <= 10:
        raise ValidationError("TOTP 校验窗口必须在 0 到 10 之间。")
    current, _counter = _totp_time_counter(timestamp, config.period)
    for offset in range(-window, window + 1):
        candidate_time = current + offset * config.period
        if candidate_time >= 0 and hmac.compare_digest(generate_totp(config, candidate_time)["code"], code):
            return True
    return False


def _read_store_json(path: Path, description: str) -> dict[str, Any]:
    try:
        size = path.stat().st_size
        if size <= 0 or size > MAX_STORE_BYTES:
            raise CredentialStoreError(f"{description}大小异常。")
        with path.open("rb") as stream:
            raw = stream.read(MAX_STORE_BYTES + 1)
        if not raw or len(raw) > MAX_STORE_BYTES:
            raise CredentialStoreError(f"{description}大小异常。")
        payload = _json_loads(raw.decode("utf-8"))
    except CredentialStoreError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise CredentialStoreError(f"无法读取{description}。") from exc
    if not isinstance(payload, dict):
        raise CredentialStoreError(f"{description}格式无效。")
    return payload


def _validate_store_envelope(
    payload: dict[str, Any],
    *,
    schema: str,
    field: str,
    id_pattern: re.Pattern[str],
    description: str,
) -> dict[str, Any]:
    records = payload.get(field)
    if set(payload) != {"schema", field} or payload.get("schema") != schema or not isinstance(records, dict):
        raise CredentialStoreError(f"{description}格式无效。")
    if len(records) > MAX_STORE_RECORDS:
        raise CredentialStoreError(f"{description}记录数量超过限制。")
    for record_id, encoded in records.items():
        if not isinstance(record_id, str) or not id_pattern.fullmatch(record_id):
            raise CredentialStoreError(f"{description}包含无效记录 ID。")
        if not isinstance(encoded, str) or not encoded:
            raise CredentialStoreError(f"{description}包含无效加密记录。")
        try:
            ciphertext = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise CredentialStoreError(f"{description}包含无效加密记录。") from exc
        if not ciphertext:
            raise CredentialStoreError(f"{description}包含空加密记录。")
    return payload


def _read_totp_store(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": TOTP_STORE_SCHEMA, "items": {}}
    return _validate_store_envelope(
        _read_store_json(path, "验证码保险箱"),
        schema=TOTP_STORE_SCHEMA,
        field="items",
        id_pattern=_TOTP_ID_PATTERN,
        description="验证码保险箱",
    )


def storage_health(
    *,
    totp_path: str | Path | None = None,
    email_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate encrypted store envelopes without decrypting saved credentials."""

    stores = (
        (
            "totp",
            Path(totp_path) if totp_path is not None else DEFAULT_TOTP_STORE,
            TOTP_STORE_SCHEMA,
            "items",
            _TOTP_ID_PATTERN,
        ),
        (
            "email",
            Path(email_path) if email_path is not None else DEFAULT_EMAIL_STORE,
            EMAIL_STORE_SCHEMA,
            "accounts",
            _EMAIL_ID_PATTERN,
        ),
    )
    results: dict[str, Any] = {}
    issues: list[dict[str, str]] = []
    for name, path, schema, key, id_pattern in stores:
        if not path.exists():
            results[name] = {"status": "ok", "exists": False, "count": 0, "path": str(path)}
            continue
        try:
            payload = _validate_store_envelope(
                _read_store_json(path, f"{name.upper()} 存储"),
                schema=schema,
                field=key,
                id_pattern=id_pattern,
                description=f"{name.upper()} 存储",
            )
            records = payload[key]
            results[name] = {
                "status": "ok",
                "exists": True,
                "count": len(records),
                "path": str(path),
            }
        except (OSError, UnicodeError, ValueError, RecursionError, CredentialStoreError) as exc:
            detail = f"{name.upper()} 存储无法验证：{str(exc)[:220]}"
            issues.append({"store": name, "detail": detail})
            results[name] = {
                "status": "error",
                "exists": True,
                "count": None,
                "path": str(path),
                "detail": detail,
            }
    return {
        "healthy": not issues,
        "status": "ok" if not issues else "error",
        "stores": results,
        "issues": issues,
        "credentialsRead": False,
    }


def _encrypt_totp_item(item: Mapping[str, Any]) -> str:
    plaintext = json.dumps(dict(item), ensure_ascii=False, separators=(",", ":"))
    try:
        protected = core.dpapi_protect(plaintext)
        if not hmac.compare_digest(core.dpapi_unprotect(protected).encode("utf-8"), plaintext.encode("utf-8")):
            raise ValueError("DPAPI round trip mismatch")
        return base64.b64encode(protected).decode("ascii")
    except Exception:
        raise CredentialStoreError("无法加密验证码密钥。") from None


def _totp_fingerprint(config: TOTPConfig) -> str:
    return hashlib.sha256(
        config.secret + b"\0" + f"{config.algorithm}:{config.digits}:{config.period}".encode("ascii")
    ).hexdigest()


def _decrypt_totp_item(encoded: str, *, expected_id: str | None = None) -> dict[str, Any]:
    try:
        plaintext = core.dpapi_unprotect(base64.b64decode(encoded, validate=True))
        value = _json_loads(plaintext)
    except Exception:
        raise CredentialStoreError("无法解密验证码密钥。") from None
    if not isinstance(value, dict) or not isinstance(value.get("source"), str):
        raise CredentialStoreError("验证码密钥记录格式无效。")
    item_id = value.get("id")
    if not isinstance(item_id, str) or not _TOTP_ID_PATTERN.fullmatch(item_id):
        raise CredentialStoreError("验证码密钥记录 ID 无效。")
    if expected_id is not None and not hmac.compare_digest(item_id, expected_id):
        raise CredentialStoreError("验证码密钥记录与保险箱索引不一致。")
    label = value.get("label", "")
    if not isinstance(label, str) or len(label) > 256 or any(ord(character) < 32 or ord(character) == 127 for character in label):
        raise CredentialStoreError("验证码密钥记录备注无效。")
    config = parse_totp_source(value["source"])
    fingerprint = value.get("fingerprint")
    expected_fingerprint = _totp_fingerprint(config)
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise CredentialStoreError("验证码密钥记录指纹无效。")
    if not hmac.compare_digest(fingerprint, expected_fingerprint):
        raise CredentialStoreError("验证码密钥记录完整性校验失败。")
    return value


def _store_bytes(path: Path) -> bytes | None:
    if not path.exists():
        return None
    try:
        if path.stat().st_size > MAX_STORE_BYTES:
            raise CredentialStoreError("凭据存储事务快照过大。")
        with path.open("rb") as stream:
            raw = stream.read(MAX_STORE_BYTES + 1)
    except CredentialStoreError:
        raise
    except OSError as exc:
        raise CredentialStoreError("无法读取凭据存储事务快照。") from exc
    if len(raw) > MAX_STORE_BYTES:
        raise CredentialStoreError("凭据存储事务快照过大。")
    return raw


def _restore_store_bytes(path: Path, before: bytes | None) -> bool:
    try:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            core.atomic_write_bytes(path, before)
        return _store_bytes(path) == before
    except Exception:
        return False


def _commit_totp_store(path: Path, store: dict[str, Any], error_message: str) -> None:
    before = _store_bytes(path)
    try:
        core.atomic_write_json(path, store)
        observed = _read_totp_store(path)
        if observed != store:
            raise CredentialStoreError("验证码保险箱落盘内容不一致。")
    except Exception:
        rolled_back = _restore_store_bytes(path, before)
        suffix = "" if rolled_back else "；原保险箱也未能回滚"
        raise CredentialStoreError(error_message + suffix + "。") from None


def save_totp_item(
    source: str,
    *,
    label: str = "",
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate and DPAPI-encrypt one TOTP source for the current Windows user."""
    raw_source = str(source or "").strip()
    if raw_source.startswith("{"):
        try:
            edit_request = _json_loads(raw_source)
        except _DuplicateJSONKeyError as exc:
            raise ValidationError("TOTP 编辑请求包含重复字段。") from exc
        except (json.JSONDecodeError, RecursionError):
            edit_request = None
        if isinstance(edit_request, dict) and edit_request.get("_toolboxEdit") is True:
            item_id = str(edit_request.get("id") or "")
            replacement = edit_request.get("source")
            return update_totp_item(
                item_id,
                source=None if replacement in (None, "") else str(replacement),
                label=label,
                path=path,
            )
    config = parse_totp_source(raw_source)
    clean_label = str(label or config.label or "").strip()
    if len(clean_label) > 256 or any(ord(character) < 32 or ord(character) == 127 for character in clean_label):
        raise ValidationError("验证码备注格式无效。")
    store_path = Path(path) if path is not None else DEFAULT_TOTP_STORE
    fingerprint = _totp_fingerprint(config)
    with _TOTP_STORE_LOCK:
        store = _read_totp_store(store_path)
        existing_id = None
        existing_item = None
        for item_id, encoded in store["items"].items():
            existing = _decrypt_totp_item(encoded, expected_id=item_id)
            if existing.get("fingerprint") == fingerprint:
                existing_id = item_id
                existing_item = existing
                break
        item_id = existing_id
        if item_id is None:
            for _attempt in range(8):
                candidate = f"totp_{secrets.token_hex(8)}"
                if candidate not in store["items"]:
                    item_id = candidate
                    break
            if item_id is None:
                raise CredentialStoreError("无法生成唯一的验证码项目 ID。")
        item = {
            "id": item_id,
            "source": raw_source,
            "label": clean_label,
            "fingerprint": fingerprint,
            "createdAt": (existing_item or {}).get("createdAt") or core.now_iso(),
            "updatedAt": core.now_iso(),
        }
        store["items"][item_id] = _encrypt_totp_item(item)
        _commit_totp_store(store_path, store, "无法保存验证码密钥")
    return _public_totp_item(item)


def update_totp_item(
    item_id: str,
    *,
    source: str | None = None,
    label: str | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Atomically update a saved TOTP item without exposing its stored source."""
    if not isinstance(item_id, str) or not _TOTP_ID_PATTERN.fullmatch(item_id):
        raise ValidationError("验证码项目 ID 格式无效。")
    store_path = Path(path) if path is not None else DEFAULT_TOTP_STORE
    with _TOTP_STORE_LOCK:
        store = _read_totp_store(store_path)
        encoded = store["items"].get(item_id)
        if not isinstance(encoded, str) or not encoded:
            raise CredentialStoreError("未找到验证码项目。")
        current = _decrypt_totp_item(encoded, expected_id=item_id)
        next_source = current["source"] if source is None else str(source).strip()
        config = parse_totp_source(next_source)
        next_label = current.get("label", "") if label is None else str(label).strip()
        if len(next_label) > 256 or any(ord(character) < 32 or ord(character) == 127 for character in next_label):
            raise ValidationError("验证码备注格式无效。")
        fingerprint = _totp_fingerprint(config)
        for other_id, other_encoded in store["items"].items():
            if (other_id != item_id
                    and _decrypt_totp_item(other_encoded, expected_id=other_id).get("fingerprint") == fingerprint):
                raise ValidationError("该验证码密钥已存在。")
        updated = {
            **current,
            "id": item_id,
            "source": next_source,
            "label": next_label,
            "fingerprint": fingerprint,
            "createdAt": current.get("createdAt") or core.now_iso(),
            "updatedAt": core.now_iso(),
        }
        store["items"][item_id] = _encrypt_totp_item(updated)
        _commit_totp_store(store_path, store, "无法更新验证码密钥")
    return _public_totp_item(updated)


def _public_totp_item(item: Mapping[str, Any], *, timestamp: float | int | None = None) -> dict[str, Any]:
    generated = generate_totp(str(item["source"]), timestamp=timestamp)
    generated["id"] = str(item.get("id") or "")
    generated["label"] = str(item.get("label") or generated.get("label") or "")
    generated["createdAt"] = item.get("createdAt")
    generated["updatedAt"] = item.get("updatedAt")
    return generated


def list_totp_items(
    *,
    path: str | Path | None = None,
    timestamp: float | int | None = None,
) -> list[dict[str, Any]]:
    store_path = Path(path) if path is not None else DEFAULT_TOTP_STORE
    with _TOTP_STORE_LOCK:
        store = _read_totp_store(store_path)
        items = [
            _decrypt_totp_item(encoded, expected_id=item_id)
            for item_id, encoded in store["items"].items()
        ]
    return sorted(
        (_public_totp_item(item, timestamp=timestamp) for item in items),
        key=lambda item: (str(item.get("label") or "").casefold(), item["id"]),
    )


def delete_totp_item(item_id: str, *, path: str | Path | None = None) -> bool:
    if not isinstance(item_id, str) or not _TOTP_ID_PATTERN.fullmatch(item_id):
        raise ValidationError("验证码项目 ID 格式无效。")
    store_path = Path(path) if path is not None else DEFAULT_TOTP_STORE
    with _TOTP_STORE_LOCK:
        store = _read_totp_store(store_path)
        removed = store["items"].pop(item_id, None) is not None
        if removed:
            _commit_totp_store(store_path, store, "无法更新验证码保险箱")
    return removed


_FIELD_ALIASES = {
    "id": "id", "accountid": "id", "编号": "id",
    "name": "label", "label": "label", "displayname": "label", "备注": "label", "名称": "label",
    "email": "email", "mail": "email", "emailaddress": "email", "address": "email",
    "username": "email", "user": "email", "account": "email", "login": "email",
    "邮箱": "email", "账号": "email", "用户名": "email",
    "password": "password", "pass": "password", "passwd": "password", "pwd": "password",
    "apppassword": "password", "applicationpassword": "password", "应用密码": "password", "密码": "password",
    "accesstoken": "access_token", "oauthtoken": "access_token", "token": "access_token", "访问令牌": "access_token",
    "refreshtoken": "refresh_token", "刷新令牌": "refresh_token",
    "clientid": "client_id", "appid": "client_id", "applicationid": "client_id", "客户端id": "client_id",
    "clientsecret": "client_secret", "客户端密钥": "client_secret",
    "tenant": "tenant_id", "tenantid": "tenant_id", "租户": "tenant_id",
    "provider": "provider", "service": "provider", "vendor": "provider", "服务商": "provider",
    "imap": "imap_host", "imaphost": "imap_host", "imapserver": "imap_host",
    "host": "imap_host", "server": "imap_host", "服务器": "imap_host",
    "imapport": "imap_port", "port": "imap_port", "端口": "imap_port",
    "security": "security", "encryption": "security", "tls": "security", "ssl": "security", "加密": "security",
    "auth": "auth_method", "authmethod": "auth_method", "authentication": "auth_method", "认证": "auth_method",
    "scope": "scope", "oauthscope": "scope", "tokenendpoint": "token_endpoint", "tokenurl": "token_endpoint",
    "mailbox": "mailbox", "folder": "mailbox", "文件夹": "mailbox",
}
_CANONICAL_FIELDS = set(_FIELD_ALIASES.values())
_PROVIDER_ALIASES = {
    "gmail": "google", "google": "google", "googlemail": "google",
    "outlook": "microsoft", "office365": "microsoft", "microsoft": "microsoft",
    "hotmail": "microsoft", "live": "microsoft", "ms": "microsoft",
    "custom": "custom", "other": "custom", "自定义": "custom",
}
_PROVIDER_DEFAULTS = {
    "google": {"imap_host": "imap.gmail.com", "imap_port": 993, "security": "ssl"},
    "microsoft": {"imap_host": "outlook.office365.com", "imap_port": 993, "security": "ssl"},
}
_PUBLIC_ACCOUNT_FIELDS = {
    "id", "label", "email", "provider", "imap_host", "imap_port", "security", "auth_method", "mailbox",
}
_MAIL_HEALTH_STATUSES = {
    "unknown",
    "healthy",
    "error",
    "connection_error",
    "auth_error",
    "mailbox_error",
}


def _canonical_field_name(value: Any) -> str:
    original = str(value).strip()
    compact = re.sub(r"[\s_.:/\\-]+", "", original.lower())
    return _FIELD_ALIASES.get(compact, original)


def _canonicalize_record(record: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw_key, raw_value in record.items():
        key = _canonical_field_name(raw_key)
        if key in result and raw_value not in (None, "", result[key]):
            raise ValidationError(f"邮箱字段 `{key}` 重复且值冲突。")
        result[key] = raw_value
    return result


def _records_from_json(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        records = value
    elif isinstance(value, dict):
        records = None
        for key in ("accounts", "items", "data", "emails", "mailboxes"):
            if isinstance(value.get(key), list):
                records = value[key]
                break
        if records is None:
            records = [value]
    else:
        raise ValidationError("邮箱导入 JSON 必须是对象或对象数组。")
    if not all(isinstance(item, Mapping) for item in records):
        raise ValidationError("邮箱导入记录必须是对象。")
    return list(records)


def _strip_import_wrappers(text: str) -> str:
    value = text.lstrip("\ufeff").strip()
    fenced = re.fullmatch(r"```(?:json|jsonl|csv|tsv|text)?\s*(.*?)\s*```", value, flags=re.DOTALL | re.IGNORECASE)
    return fenced.group(1).strip() if fenced else value


def _try_json_records(text: str) -> list[Mapping[str, Any]] | None:
    try:
        return _records_from_json(_json_loads(text))
    except _DuplicateJSONKeyError as exc:
        raise ValidationError("邮箱导入 JSON 包含重复字段。") from exc
    except RecursionError as exc:
        raise ValidationError("邮箱导入 JSON 嵌套过深。") from exc
    except json.JSONDecodeError:
        pass
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith(("#", "//"))]
    if not lines:
        return []
    records: list[Mapping[str, Any]] = []
    for line in lines:
        try:
            value = _json_loads(line)
        except _DuplicateJSONKeyError as exc:
            raise ValidationError("邮箱导入 JSONL 包含重复字段。") from exc
        except RecursionError as exc:
            raise ValidationError("邮箱导入 JSONL 嵌套过深。") from exc
        except json.JSONDecodeError:
            return None
        records.extend(_records_from_json(value))
    return records


def _try_key_value_records(text: str) -> list[Mapping[str, Any]] | None:
    blocks = re.split(r"(?:\r?\n){2,}|\r?\n\s*---+\s*\r?\n", text.strip())
    parsed: list[Mapping[str, Any]] = []
    for block in blocks:
        record: dict[str, str] = {}
        lines = [line.strip() for line in block.splitlines() if line.strip() and not line.lstrip().startswith(("#", "//"))]
        if not lines:
            continue
        for line in lines:
            match = re.match(r"^([^:=]{1,80})\s*[:=]\s*(.*)$", line)
            if not match:
                return None
            key = _canonical_field_name(match.group(1))
            if key in record:
                raise ValidationError(f"邮箱字段 `{key}` 重复。")
            record[key] = match.group(2).strip()
        parsed.append(record)
    return parsed or None


def _try_four_part_records(text: str) -> list[Mapping[str, Any]] | None:
    """Parse email/password plus access-token or refresh-token bundles."""
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "//"))
    ]
    if not lines or not all("----" in line for line in lines):
        return None
    records: list[Mapping[str, Any]] = []
    for line in lines:
        parts = [part.strip() for part in line.split("----")]
        if len(parts) not in {2, 3, 4}:
            raise ValidationError(
                "邮箱组合必须是 email----password、email----password----access_token，"
                "或 email----password----refresh_token----client_id。"
            )
        record: dict[str, Any] = {"email": parts[0]}
        if len(parts) >= 2 and parts[1]:
            record["password"] = parts[1]
        if len(parts) == 3 and parts[2]:
            record["access_token"] = parts[2]
        elif len(parts) >= 4 and parts[2]:
            record["refresh_token"] = parts[2]
        if len(parts) >= 4 and parts[3]:
            record["client_id"] = parts[3]
        if record.get("access_token") or record.get("refresh_token") or record.get("client_id"):
            record["auth_method"] = "oauth2"
        records.append(record)
    return records


def _try_delimited_records(text: str) -> list[Mapping[str, Any]] | None:
    try:
        dialect = csv.Sniffer().sniff("\n".join(text.splitlines()[:20]), delimiters=",\t;|")
    except csv.Error:
        return None
    try:
        rows = [row for row in csv.reader(io.StringIO(text), dialect) if any(cell.strip() for cell in row)]
    except csv.Error as exc:
        raise ValidationError("邮箱分隔文本格式无效或字段过大。") from exc
    if not rows:
        return []
    header = [_canonical_field_name(cell) for cell in rows[0]]
    if sum(field in _CANONICAL_FIELDS for field in header) >= 2:
        data_rows = rows[1:]
    else:
        if len(rows[0]) > 6:
            raise ValidationError("无表头邮箱文本最多支持 6 列。")
        header = ["email", "password", "imap_host", "imap_port", "security", "provider"][: len(rows[0])]
        data_rows = rows
    if len(header) != len(set(header)):
        raise ValidationError("邮箱分隔文本包含重复列。")
    result = []
    for row in data_rows:
        if len(row) > len(header):
            raise ValidationError("邮箱分隔文本列数不一致。")
        result.append({header[index]: cell.strip() for index, cell in enumerate(row) if cell.strip()})
    return result


def _clean_optional_text(value: Any, field: str, maximum: int = 4_096) -> str:
    if value is None:
        return ""
    result = str(value).strip()
    if len(result) > maximum or any(ord(character) < 32 or ord(character) == 127 for character in result):
        raise ValidationError(f"邮箱字段 `{field}` 格式无效。")
    return result


def _normalize_provider(value: Any, email_address: str) -> str:
    raw = str(value or "").strip().lower().replace(" ", "")
    if raw:
        provider = _PROVIDER_ALIASES.get(raw)
        if not provider:
            raise ValidationError("邮箱 provider 仅支持 google、microsoft 或 custom。")
        return provider
    domain = email_address.rsplit("@", 1)[-1].lower()
    if domain in {"gmail.com", "googlemail.com"}:
        return "google"
    if domain in {"outlook.com", "hotmail.com", "live.com", "msn.com"} or domain.endswith(".onmicrosoft.com"):
        return "microsoft"
    return "custom"


def _normalize_email_record(
    record: Mapping[str, Any],
    *,
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    item = _canonicalize_record(record)
    if existing:
        item = {**dict(existing), **item}
    email_address = _clean_optional_text(item.get("email"), "email", 320).lower()
    if not re.fullmatch(r"[^\s@<>]+@[^\s@<>.]+(?:\.[^\s@<>.]+)+", email_address):
        raise ValidationError("邮箱地址格式无效。")
    provider = _normalize_provider(item.get("provider"), email_address)
    defaults = _PROVIDER_DEFAULTS.get(provider, {})
    host = _clean_optional_text(item.get("imap_host") or defaults.get("imap_host"), "imap_host", 253).lower()
    if not host or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?|\[[0-9a-f:]+\])", host):
        raise ValidationError("IMAP 主机名格式无效。")
    try:
        port = int(item.get("imap_port") or defaults.get("imap_port") or 993)
    except (TypeError, ValueError) as exc:
        raise ValidationError("IMAP 端口必须是整数。") from exc
    if not 1 <= port <= 65_535:
        raise ValidationError("IMAP 端口必须在 1 到 65535 之间。")
    security_raw = str(item.get("security") or defaults.get("security") or ("ssl" if port == 993 else "starttls"))
    security_key = re.sub(r"[\s_-]+", "", security_raw.strip().lower())
    security = {"ssl": "ssl", "ssltls": "ssl", "tls": "starttls", "starttls": "starttls"}.get(security_key)
    if not security:
        raise ValidationError("IMAP 仅支持 SSL/TLS 或 STARTTLS。")
    if provider in {"google", "microsoft"}:
        official = _PROVIDER_DEFAULTS[provider]
        if host != official["imap_host"] or port != official["imap_port"] or security != official["security"]:
            raise ValidationError("Google 与 Microsoft 邮箱必须使用各自官方的 IMAP SSL 端点。")

    password = _clean_optional_text(item.get("password"), "password", 16_384)
    access_token = _clean_optional_text(item.get("access_token"), "access_token", 65_536)
    refresh_token = _clean_optional_text(item.get("refresh_token"), "refresh_token", 65_536)
    client_id = _clean_optional_text(item.get("client_id"), "client_id", 1_024)
    client_secret = _clean_optional_text(item.get("client_secret"), "client_secret", 16_384)
    if provider == "custom" and (access_token or refresh_token):
        raise ValidationError("自定义 IMAP 主机不接受 OAuth access_token 或 refresh_token。")
    explicit_auth = re.sub(r"[\s_-]+", "", str(item.get("auth_method") or "").strip().lower())
    if explicit_auth in {"password", "pass", "basic", "apppassword"}:
        auth_method = "password"
    elif explicit_auth in {"oauth", "oauth2", "xoauth2", "token"}:
        auth_method = "oauth2"
    elif explicit_auth:
        raise ValidationError("邮箱 auth_method 仅支持 password 或 oauth2。")
    else:
        auth_method = "oauth2" if access_token or refresh_token else "password"
    if auth_method == "password":
        if not password:
            raise ValidationError("密码登录缺少 password 或 app_password。")
    else:
        if not access_token and not (refresh_token and client_id):
            raise ValidationError("OAuth2 登录需要 access_token，或 refresh_token 与 client_id。")

    tenant_id = _clean_optional_text(item.get("tenant_id") or "common", "tenant_id", 128)
    if provider == "microsoft" and not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", tenant_id):
        raise ValidationError("Microsoft tenant_id 格式无效。")
    token_endpoint = _clean_optional_text(item.get("token_endpoint"), "token_endpoint", 2_048)
    if token_endpoint:
        parsed_endpoint = urllib.parse.urlsplit(token_endpoint)
        try:
            endpoint_port = parsed_endpoint.port
        except ValueError:
            endpoint_port = -1
        if (
            parsed_endpoint.scheme != "https"
            or not parsed_endpoint.hostname
            or parsed_endpoint.username is not None
            or parsed_endpoint.password is not None
            or parsed_endpoint.fragment
            or endpoint_port not in (None, 443)
        ):
            raise ValidationError("OAuth2 token_endpoint 必须是 HTTPS URL。")
        allowed_host = "oauth2.googleapis.com" if provider == "google" else "login.microsoftonline.com"
        if provider not in {"google", "microsoft"} or parsed_endpoint.hostname != allowed_host:
            raise ValidationError("OAuth2 token_endpoint 与邮箱服务商不匹配。")
    mailbox = _clean_optional_text(item.get("mailbox") or "INBOX", "mailbox", 255)
    if not mailbox:
        raise ValidationError("IMAP mailbox 不能为空。")
    label = _clean_optional_text(item.get("label"), "label", 256)
    raw_id = _clean_optional_text(item.get("id"), "id", 64).lower()
    if raw_id and not _EMAIL_ID_PATTERN.fullmatch(raw_id):
        raise ValidationError("邮箱账户 ID 格式无效。")
    account_id = raw_id or "mail_" + hashlib.sha256(f"{email_address}\0{host}".encode()).hexdigest()[:16]
    credential_revision = _clean_optional_text(item.get("_credential_revision"), "_credential_revision", 128)
    if credential_revision and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", credential_revision):
        raise ValidationError("邮箱凭据 revision 格式无效。")
    result: dict[str, Any] = {
        "id": account_id,
        "label": label,
        "email": email_address,
        "provider": provider,
        "imap_host": host,
        "imap_port": port,
        "security": security,
        "auth_method": auth_method,
        "mailbox": mailbox,
    }
    if credential_revision:
        result["_credential_revision"] = credential_revision
    extras = (
        ("password", password), ("access_token", access_token), ("refresh_token", refresh_token),
        ("client_id", client_id), ("client_secret", client_secret),
        ("tenant_id", tenant_id if provider == "microsoft" else ""),
        ("scope", _clean_optional_text(item.get("scope"), "scope", 4_096)),
        ("token_endpoint", token_endpoint),
    )
    for key, value in extras:
        if value:
            result[key] = value
    raw_health = item.get("_health")
    if isinstance(raw_health, Mapping):
        status = str(raw_health.get("status") or "unknown").strip().lower()
        if status not in _MAIL_HEALTH_STATUSES:
            status = "unknown"
        checked_at = _clean_optional_text(raw_health.get("checkedAt"), "checkedAt", 64)
        try:
            message_count = max(0, int(raw_health.get("messageCount") or 0))
        except (TypeError, ValueError):
            message_count = 0
        result["_health"] = {
            "status": status,
            "checkedAt": checked_at,
            "messageCount": message_count,
        }
    return result


def _email_import_records(
    payload: str | bytes | Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    max_accounts: int = MAX_IMPORT_ACCOUNTS,
) -> list[Mapping[str, Any]]:
    if type(max_accounts) is not int or not 1 <= max_accounts <= MAX_IMPORT_ACCOUNTS:
        raise ValidationError(f"max_accounts 必须在 1 到 {MAX_IMPORT_ACCOUNTS} 之间。")
    if isinstance(payload, bytes):
        if len(payload) > MAX_IMPORT_BYTES:
            raise ValidationError("邮箱导入内容过大。")
        try:
            payload = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValidationError("邮箱导入内容必须使用 UTF-8。") from exc
    if isinstance(payload, str):
        try:
            encoded_size = len(payload.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValidationError("邮箱导入内容必须是有效 UTF-8 文本。") from exc
        if encoded_size > MAX_IMPORT_BYTES:
            raise ValidationError("邮箱导入内容过大。")
        text = _strip_import_wrappers(payload)
        if not text:
            return []
        records = _try_json_records(text)
        if records is None:
            records = _try_key_value_records(text)
        if records is None:
            records = _try_four_part_records(text)
        if records is None:
            records = _try_delimited_records(text)
        if records is None:
            raise ValidationError("无法识别邮箱导入格式。")
    elif isinstance(payload, Mapping):
        records = _records_from_json(payload)
    else:
        try:
            iterator = iter(payload)
        except TypeError as exc:
            raise ValidationError("邮箱导入内容格式无效。") from exc
        records = list(itertools.islice(iterator, max_accounts + 1))
        if not all(isinstance(item, Mapping) for item in records):
            raise ValidationError("邮箱导入记录必须是对象。")
    if len(records) > max_accounts:
        raise ValidationError("邮箱导入账户数量超过限制。")
    return list(records)


def parse_email_accounts(
    payload: str | bytes | Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    max_accounts: int = MAX_IMPORT_ACCOUNTS,
) -> list[dict[str, Any]]:
    """Parse JSON, JSONL, arrays, key/value blocks, CSV, TSV, or pipe text."""
    records = _email_import_records(payload, max_accounts=max_accounts)
    normalized = [_normalize_email_record(record) for record in records]
    identifiers = [item["id"] for item in normalized]
    if len(identifiers) != len(set(identifiers)):
        raise ValidationError("邮箱导入包含重复账户 ID。")
    return normalized


def preview_mail_import_items(
    payload: str | bytes | Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    max_accounts: int = MAX_IMPORT_ACCOUNTS,
) -> dict[str, Any]:
    """Preview every parsed record independently so one bad account does not block a batch."""
    records = _email_import_records(payload, max_accounts=max_accounts)
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, record in enumerate(records):
        try:
            account = _normalize_email_record(record)
            duplicate = account["id"] in seen_ids
            seen_ids.add(account["id"])
            public = public_email_account(account)
            items.append(
                {
                    "index": index,
                    "valid": not duplicate,
                    "duplicateInBatch": duplicate,
                    **public,
                    **({"error": "本批次中存在重复邮箱。"} if duplicate else {}),
                }
            )
        except ToolboxError as exc:
            try:
                canonical = _canonicalize_record(record)
            except ToolboxError:
                canonical = {}
            items.append(
                {
                    "index": index,
                    "valid": False,
                    "duplicateInBatch": False,
                    "email": str(canonical.get("email") or "")[:320],
                    "error": str(exc)[:300],
                }
            )
    return {
        "total": len(items),
        "valid": sum(1 for item in items if item["valid"]),
        "invalid": sum(1 for item in items if not item["valid"]),
        "items": items,
    }


def save_mail_import_selection(
    payload: str | bytes | Mapping[str, Any] | Iterable[Mapping[str, Any]],
    selected_indices: Iterable[int],
    *,
    path: str | Path | None = None,
) -> list[dict[str, Any]]:
    records = _email_import_records(payload)
    store_path = Path(path) if path is not None else DEFAULT_EMAIL_STORE
    try:
        indices = list(itertools.islice(iter(selected_indices), len(records) + 1))
    except TypeError as exc:
        raise ValidationError("邮箱选择序号格式无效。") from exc
    if len(indices) > len(records):
        raise ValidationError("邮箱选择序号数量超过导入记录。")
    with _EMAIL_STORE_LOCK:
        store = _read_email_store(store_path)
        selected: list[dict[str, Any]] = []
        seen_indices: set[int] = set()
        for raw_index in indices:
            if type(raw_index) is not int:
                raise ValidationError("邮箱选择序号格式无效。")
            index = raw_index
            if index in seen_indices or not 0 <= index < len(records):
                raise ValidationError("邮箱选择序号超出范围或重复。")
            seen_indices.add(index)
            canonical = _canonicalize_record(records[index])
            raw_id = str(canonical.get("id") or "").strip().lower()
            existing = None
            if raw_id and isinstance(store["accounts"].get(raw_id), str):
                existing = _decrypt_email_account(store["accounts"][raw_id], expected_id=raw_id)
            selected.append(_normalize_email_record(records[index], existing=existing))
        if not selected:
            raise ValidationError("请至少选择一个可导入邮箱。")
        identifiers = [item["id"] for item in selected]
        if len(identifiers) != len(set(identifiers)):
            raise ValidationError("所选邮箱中包含重复账户。")
        return save_email_accounts(selected, path=path)


def public_email_account(account: Mapping[str, Any]) -> dict[str, Any]:
    """Return metadata only; never return credential plaintext."""
    public = {key: account[key] for key in _PUBLIC_ACCOUNT_FIELDS if key in account}
    public["credential_status"] = {
        "password": bool(account.get("password")),
        "access_token": bool(account.get("access_token")),
        "refresh_token": bool(account.get("refresh_token")),
        "client_id": bool(account.get("client_id")),
        "client_secret": bool(account.get("client_secret")),
        "health": dict(account.get("_health") or {"status": "unknown", "checkedAt": "", "messageCount": 0}),
    }
    return public


def preview_mail_import(
    payload: str | bytes | Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    max_accounts: int = MAX_IMPORT_ACCOUNTS,
) -> list[dict[str, Any]]:
    """Validate an import and return only safe account metadata."""
    return [public_email_account(item) for item in parse_email_accounts(payload, max_accounts=max_accounts)]


def _read_email_store(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": EMAIL_STORE_SCHEMA, "accounts": {}}
    return _validate_store_envelope(
        _read_store_json(path, "邮箱凭据存储"),
        schema=EMAIL_STORE_SCHEMA,
        field="accounts",
        id_pattern=_EMAIL_ID_PATTERN,
        description="邮箱凭据存储",
    )


def _encrypt_email_account(account: Mapping[str, Any]) -> str:
    plaintext = json.dumps(dict(account), ensure_ascii=False, separators=(",", ":"))
    try:
        protected = core.dpapi_protect(plaintext)
        if not hmac.compare_digest(core.dpapi_unprotect(protected).encode("utf-8"), plaintext.encode("utf-8")):
            raise ValueError("DPAPI round trip mismatch")
        return base64.b64encode(protected).decode("ascii")
    except Exception:
        raise CredentialStoreError("无法加密邮箱凭据。") from None


def _decrypt_email_account(encoded: str, *, expected_id: str | None = None) -> dict[str, Any]:
    try:
        plaintext = core.dpapi_unprotect(base64.b64decode(encoded, validate=True))
        value = _json_loads(plaintext)
    except Exception:
        raise CredentialStoreError("无法解密邮箱凭据。") from None
    if not isinstance(value, dict):
        raise CredentialStoreError("邮箱凭据记录格式无效。")
    normalized = _normalize_email_record(value)
    if expected_id is not None and not hmac.compare_digest(normalized["id"], expected_id):
        raise CredentialStoreError("邮箱凭据记录与存储索引不一致。")
    if "_credential_revision" not in normalized:
        normalized["_credential_revision"] = "legacy-" + _credential_fingerprint(normalized)
    return normalized


def _commit_email_store(path: Path, store: dict[str, Any], error_message: str) -> None:
    before = _store_bytes(path)
    try:
        core.atomic_write_json(path, store)
        observed = _read_email_store(path)
        if observed != store:
            raise CredentialStoreError("邮箱凭据存储落盘内容不一致。")
    except Exception:
        rolled_back = _restore_store_bytes(path, before)
        suffix = "" if rolled_back else "；原凭据存储也未能回滚"
        raise CredentialStoreError(error_message + suffix + "。") from None


def _credential_fingerprint(account: Mapping[str, Any]) -> str:
    fields = (
        "email",
        "provider",
        "imap_host",
        "imap_port",
        "security",
        "auth_method",
        "password",
        "access_token",
        "refresh_token",
        "client_id",
        "client_secret",
        "tenant_id",
        "scope",
        "token_endpoint",
    )
    encoded = json.dumps(
        [account.get(field) or "" for field in fields],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _set_credential_revision(
    account: dict[str, Any],
    *,
    previous: Mapping[str, Any] | None = None,
) -> str:
    if previous is not None and hmac.compare_digest(
        _credential_fingerprint(account),
        _credential_fingerprint(previous),
    ):
        revision = str(
            previous.get("_credential_revision")
            or "legacy-" + _credential_fingerprint(previous)
        )
    else:
        revision = secrets.token_hex(16)
    account["_credential_revision"] = revision
    return revision


def _oauth_cache_key(account: Mapping[str, Any]) -> str:
    endpoint = account.get("token_endpoint") or _default_token_endpoint(account)
    fingerprint = json.dumps(
        [
            account.get("provider") or "",
            account.get("email") or "",
            account.get("client_id") or "",
            endpoint,
            account.get("refresh_token") or "",
            account.get("client_secret") or "",
            account.get("tenant_id") or "",
            account.get("scope") or _default_oauth_scope(account),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(fingerprint).hexdigest()


def _clear_oauth_state(*, invalidate_inflight: bool = True) -> int:
    global _EMAIL_CREDENTIAL_GENERATION, _OAUTH_CACHE_GENERATION
    with _OAUTH_CACHE_LOCK:
        _OAUTH_CACHE_GENERATION += 1
        if invalidate_inflight:
            _EMAIL_CREDENTIAL_GENERATION += 1
        _OAUTH_ACCESS_TOKEN_CACHE.clear()
        for cache_key, state in list(_OAUTH_REFRESH_LOCKS.items()):
            if state.users == 0 and not state.lock.locked():
                _OAUTH_REFRESH_LOCKS.pop(cache_key, None)
        return _OAUTH_CACHE_GENERATION


def _oauth_cache_generation() -> int:
    with _OAUTH_CACHE_LOCK:
        return _OAUTH_CACHE_GENERATION


def _email_credential_generation() -> int:
    with _OAUTH_CACHE_LOCK:
        return _EMAIL_CREDENTIAL_GENERATION


def _acquire_oauth_refresh_state(cache_key: str) -> _OAuthRefreshState:
    with _OAUTH_CACHE_LOCK:
        state = _OAUTH_REFRESH_LOCKS.setdefault(cache_key, _OAuthRefreshState())
        state.users += 1
    state.lock.acquire()
    return state


def _release_oauth_refresh_state(cache_key: str, state: _OAuthRefreshState) -> None:
    state.lock.release()
    with _OAUTH_CACHE_LOCK:
        state.users -= 1
        if state.users == 0 and _OAUTH_REFRESH_LOCKS.get(cache_key) is state:
            _OAUTH_REFRESH_LOCKS.pop(cache_key, None)


def _persist_rotated_refresh_token(
    account_id: str,
    expected_revision: str,
    refresh_token: str | None,
    *,
    expected_generation: int | None = None,
    path: str | Path | None = None,
) -> tuple[int, str] | None:
    if not expected_revision:
        return None
    store_path = Path(path) if path is not None else DEFAULT_EMAIL_STORE
    with _EMAIL_STORE_LOCK:
        if expected_generation is not None and _email_credential_generation() != expected_generation:
            return None
        store = _read_email_store(store_path)
        encoded = store["accounts"].get(account_id)
        if not isinstance(encoded, str) or not encoded:
            return None
        current = _decrypt_email_account(encoded, expected_id=account_id)
        current_revision = str(
            current.get("_credential_revision")
            or "legacy-" + _credential_fingerprint(current)
        )
        if not hmac.compare_digest(current_revision, expected_revision):
            return None
        if refresh_token is None:
            return _oauth_cache_generation(), current_revision
        current["refresh_token"] = refresh_token
        updated = _normalize_email_record(current)
        updated_revision = _set_credential_revision(updated)
        store["accounts"][account_id] = _encrypt_email_account(updated)
        _commit_email_store(store_path, store, "无法保存邮箱凭据")
        return _clear_oauth_state(), updated_revision


def save_email_accounts(
    accounts: str | bytes | Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    path: str | Path | None = None,
    replace: bool = False,
) -> list[dict[str, Any]]:
    """Encrypt complete accounts with DPAPI and atomically save them."""
    normalized = parse_email_accounts(accounts)
    store_path = Path(path) if path is not None else DEFAULT_EMAIL_STORE
    with _EMAIL_STORE_LOCK:
        store = {"schema": EMAIL_STORE_SCHEMA, "accounts": {}} if replace else _read_email_store(store_path)
        credential_changed = replace
        for item in normalized:
            existing = None
            if not replace:
                existing_encoded = store["accounts"].get(item["id"])
                if existing_encoded:
                    existing = _decrypt_email_account(existing_encoded, expected_id=item["id"])
                    if "_health" not in item and existing.get("_health"):
                        item["_health"] = existing["_health"]
            if existing is None or not hmac.compare_digest(
                _credential_fingerprint(item),
                _credential_fingerprint(existing),
            ):
                credential_changed = True
            _set_credential_revision(item, previous=existing)
        encrypted = {item["id"]: _encrypt_email_account(item) for item in normalized}
        store["accounts"].update(encrypted)
        _commit_email_store(store_path, store, "无法保存邮箱凭据")
        _clear_oauth_state(invalidate_inflight=credential_changed)
    return [public_email_account(item) for item in normalized]


def update_email_account(
    account_id: str,
    changes: Mapping[str, Any],
    *,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Update one encrypted mailbox while retaining omitted credential fields."""
    if not isinstance(changes, Mapping):
        raise ValidationError("邮箱更新内容必须是对象。")
    if not isinstance(account_id, str) or not _EMAIL_ID_PATTERN.fullmatch(account_id):
        raise ValidationError("邮箱账户 ID 格式无效。")
    store_path = Path(path) if path is not None else DEFAULT_EMAIL_STORE
    with _EMAIL_STORE_LOCK:
        store = _read_email_store(store_path)
        encoded = store["accounts"].get(account_id)
        if not isinstance(encoded, str) or not encoded:
            raise CredentialStoreError("未找到邮箱账户。")
        current = _decrypt_email_account(encoded, expected_id=account_id)
        merged = {**current, **dict(changes), "id": account_id, "_health": current.get("_health")}
        updated = parse_email_accounts([merged], max_accounts=1)[0]
        credential_changed = not hmac.compare_digest(
            _credential_fingerprint(updated),
            _credential_fingerprint(current),
        )
        _set_credential_revision(updated, previous=current)
        store["accounts"][account_id] = _encrypt_email_account(updated)
        _commit_email_store(store_path, store, "无法保存邮箱凭据")
        _clear_oauth_state(invalidate_inflight=credential_changed)
    return public_email_account(updated)


def _save_email_health(
    account_id: str,
    *,
    status: str,
    message_count: int = 0,
    expected_revision: str | None = None,
    expected_generation: int | None = None,
    commit_guard: Callable[[], bool] | None = None,
    path: str | Path | None = None,
) -> bool:
    if commit_guard is not None and not commit_guard():
        return False
    store_path = Path(path) if path is not None else DEFAULT_EMAIL_STORE
    with _EMAIL_STORE_LOCK:
        if commit_guard is not None and not commit_guard():
            return False
        if expected_generation is not None and _email_credential_generation() != expected_generation:
            return False
        store = _read_email_store(store_path)
        encoded = store["accounts"].get(account_id)
        if not isinstance(encoded, str) or not encoded:
            return False
        current = _decrypt_email_account(encoded, expected_id=account_id)
        current_revision = str(
            current.get("_credential_revision")
            or "legacy-" + _credential_fingerprint(current)
        )
        if expected_revision and not hmac.compare_digest(
            current_revision,
            str(expected_revision),
        ):
            # The user edited, deleted and recreated, or rotated this mailbox
            # while the network check was in flight.  Never attach a stale
            # result to the replacement credentials.
            return False
        current["_health"] = {
            "status": status if status in _MAIL_HEALTH_STATUSES else "unknown",
            "checkedAt": core.now_iso(),
            "messageCount": max(0, int(message_count)),
        }
        store["accounts"][account_id] = _encrypt_email_account(current)
        if commit_guard is not None and not commit_guard():
            return False
        _commit_email_store(store_path, store, "无法保存邮箱凭据")
    return True


def list_email_accounts(*, path: str | Path | None = None) -> list[dict[str, Any]]:
    store_path = Path(path) if path is not None else DEFAULT_EMAIL_STORE
    with _EMAIL_STORE_LOCK:
        store = _read_email_store(store_path)
        records = [
            _decrypt_email_account(encoded, expected_id=account_id)
            for account_id, encoded in store["accounts"].items()
        ]
    return sorted(
        (public_email_account(item) for item in records),
        key=lambda item: (item.get("email", ""), item["id"]),
    )


def _load_email_account_snapshot(
    account_id: str,
    *,
    path: str | Path | None = None,
) -> tuple[dict[str, Any], int]:
    if not isinstance(account_id, str) or not _EMAIL_ID_PATTERN.fullmatch(account_id):
        raise ValidationError("邮箱账户 ID 格式无效。")
    store_path = Path(path) if path is not None else DEFAULT_EMAIL_STORE
    with _EMAIL_STORE_LOCK:
        encoded = _read_email_store(store_path)["accounts"].get(account_id)
        if not isinstance(encoded, str) or not encoded:
            raise CredentialStoreError("未找到邮箱账户。")
        account = _decrypt_email_account(encoded, expected_id=account_id)
        generation = _email_credential_generation()
    return account, generation


def _load_email_account(account_id: str, *, path: str | Path | None = None) -> dict[str, Any]:
    return _load_email_account_snapshot(account_id, path=path)[0]


def delete_email_account(account_id: str, *, path: str | Path | None = None) -> bool:
    if not isinstance(account_id, str) or not _EMAIL_ID_PATTERN.fullmatch(account_id):
        raise ValidationError("邮箱账户 ID 格式无效。")
    store_path = Path(path) if path is not None else DEFAULT_EMAIL_STORE
    with _EMAIL_STORE_LOCK:
        store = _read_email_store(store_path)
        removed = store["accounts"].pop(account_id, None) is not None
        if removed:
            _commit_email_store(store_path, store, "无法更新邮箱凭据存储")
        _clear_oauth_state(invalidate_inflight=removed)
    return removed


def save_mail_import(
    payload: str | bytes | Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    path: str | Path | None = None,
    replace: bool = False,
) -> list[dict[str, Any]]:
    return save_email_accounts(payload, path=path, replace=replace)


def list_mail_accounts(*, path: str | Path | None = None) -> list[dict[str, Any]]:
    return list_email_accounts(path=path)


def delete_mail_account(account_id: str, *, path: str | Path | None = None) -> bool:
    return delete_email_account(account_id, path=path)


def _default_token_endpoint(account: Mapping[str, Any]) -> str:
    if account["provider"] == "google":
        return "https:" + "//oauth2.googleapis.com/token"
    tenant = account.get("tenant_id") or "common"
    return "https:" + f"//login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


def _default_oauth_scope(account: Mapping[str, Any]) -> str:
    if account["provider"] == "google":
        return "https:" + "//mail.google.com/"
    return "https:" + "//outlook.office.com/IMAP.AccessAsUser.All offline_access"


def _open_url(opener: Any, request: urllib.request.Request, timeout: float):
    if opener is None:
        return core._open_same_origin_request(request, timeout=timeout)
    if callable(opener) and not hasattr(opener, "open"):
        return opener(request, timeout=timeout)
    return opener.open(request, timeout=timeout)


def _refresh_oauth_access_token(
    account: Mapping[str, Any],
    *,
    timeout: float = DEFAULT_NETWORK_TIMEOUT,
    opener: Any = None,
) -> tuple[str, dict[str, Any]]:
    """Return an ephemeral token internally; CRUD APIs never expose it."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(float(timeout)) or not 1 <= float(timeout) <= 120:
        raise MailAccessError("OAuth2 网络超时配置无效。")
    if account.get("provider") not in {"google", "microsoft"}:
        raise MailAccessError("该邮箱服务商不支持自动刷新 OAuth2 令牌。")
    refresh_token = account.get("refresh_token")
    client_id = account.get("client_id")
    if not refresh_token or not client_id:
        raise MailAccessError("OAuth2 刷新配置不完整。")
    endpoint = account.get("token_endpoint") or _default_token_endpoint(account)
    parsed = urllib.parse.urlsplit(endpoint)
    allowed_host = "oauth2.googleapis.com" if account["provider"] == "google" else "login.microsoftonline.com"
    try:
        endpoint_port = parsed.port
    except ValueError:
        endpoint_port = -1
    if (parsed.scheme != "https" or parsed.hostname != allowed_host or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment or endpoint_port not in (None, 443)):
        raise MailAccessError("OAuth2 令牌端点配置无效。")
    fields = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "scope": account.get("scope") or _default_oauth_scope(account),
    }
    if account.get("client_secret"):
        fields["client_secret"] = account["client_secret"]
    request = urllib.request.Request(
        endpoint,
        data=urllib.parse.urlencode(fields).encode("ascii"),
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    refresh_error = False
    try:
        with _open_url(opener, request, float(timeout)) as response:
            declared = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
            if declared and int(declared) > MAX_TOKEN_RESPONSE_BYTES:
                raise MailAccessError("OAuth2 令牌响应过大。")
            raw = response.read(MAX_TOKEN_RESPONSE_BYTES + 1)
    except MailAccessError:
        raise
    except Exception:
        refresh_error = True
    if refresh_error:
        raise MailAccessError("OAuth2 令牌刷新失败。")
    if len(raw) > MAX_TOKEN_RESPONSE_BYTES:
        raise MailAccessError("OAuth2 令牌响应过大。")
    try:
        result = _json_loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise MailAccessError("OAuth2 令牌响应格式无效。") from None
    token = result.get("access_token") if isinstance(result, dict) else None
    if (not isinstance(token, str) or not token or len(token) > 65_536
            or any(ord(character) <= 32 or ord(character) == 127 for character in token)):
        raise MailAccessError("OAuth2 令牌响应缺少 access_token。")
    metadata: dict[str, Any] = {"token_type": str(result.get("token_type") or "Bearer")}
    try:
        expires_in = int(result.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    if 0 < expires_in <= 86_400:
        metadata["expires_in"] = expires_in
        metadata["expires_at"] = datetime.fromtimestamp(time.time() + expires_in, timezone.utc).isoformat()
    rotated = result.get("refresh_token")
    if isinstance(rotated, str) and rotated:
        if len(rotated) > 65_536 or any(ord(character) <= 32 or ord(character) == 127 for character in rotated):
            raise MailAccessError("OAuth2 令牌响应包含无效 refresh_token。")
        metadata["rotated_refresh_token"] = rotated
    return token, metadata


def _resolve_access_token(
    account: Mapping[str, Any],
    *,
    timeout: float,
    opener: Any = None,
    path: str | Path | None = None,
    expected_store_generation: int | None = None,
) -> str:
    if expected_store_generation is None:
        expected_store_generation = _email_credential_generation()
    can_refresh = bool(account.get("refresh_token") and account.get("client_id"))
    if account.get("access_token") and not can_refresh:
        return str(account["access_token"])
    cache_key = _oauth_cache_key(account)
    refresh_lock_key = cache_key
    now = time.monotonic()
    with _OAUTH_CACHE_LOCK:
        cached = _OAUTH_ACCESS_TOKEN_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
        generation = _OAUTH_CACHE_GENERATION
    refresh_state = _acquire_oauth_refresh_state(refresh_lock_key)
    try:
        now = time.monotonic()
        with _OAUTH_CACHE_LOCK:
            cached = _OAUTH_ACCESS_TOKEN_CACHE.get(cache_key)
            if cached and cached[0] > now:
                return cached[1]
            generation = _OAUTH_CACHE_GENERATION
        token, metadata = _refresh_oauth_access_token(account, timeout=timeout, opener=opener)
        rotated = metadata.get("rotated_refresh_token")
        cache_generation = generation
        account_id = str(account.get("id") or "")
        credential_revision = str(account.get("_credential_revision") or "")
        if account_id and credential_revision:
            try:
                persisted = _persist_rotated_refresh_token(
                    account_id,
                    credential_revision,
                    rotated if isinstance(rotated, str) and rotated else None,
                    expected_generation=expected_store_generation,
                    path=path,
                )
            except CredentialStoreError:
                raise MailAccessError("OAuth2 令牌轮换保存失败。") from None
            if persisted is None:
                return token
            cache_generation, persisted_revision = persisted
            if isinstance(rotated, str) and rotated:
                cache_account = dict(account)
                cache_account["refresh_token"] = rotated
                cache_account["_credential_revision"] = persisted_revision
                cache_key = _oauth_cache_key(cache_account)
                if isinstance(account, dict):
                    account.update(
                        refresh_token=rotated,
                        _credential_revision=persisted_revision,
                    )
        elif isinstance(rotated, str) and rotated and isinstance(account, dict):
            account["refresh_token"] = rotated
            cache_key = _oauth_cache_key(account)
        expires_in = int(metadata.get("expires_in") or 300)
        safe_ttl = max(30, min(expires_in - 60, 3_600)) if expires_in > 90 else max(10, expires_in)
        with _OAUTH_CACHE_LOCK:
            if _OAUTH_CACHE_GENERATION == cache_generation:
                _OAUTH_ACCESS_TOKEN_CACHE[cache_key] = (time.monotonic() + safe_ttl, token)
        return token
    finally:
        _release_oauth_refresh_state(refresh_lock_key, refresh_state)


def _connect_imap(
    account: Mapping[str, Any],
    *,
    timeout: float,
    imap_ssl_factory: Callable[..., Any] | None,
    imap_factory: Callable[..., Any] | None,
    ssl_context: ssl.SSLContext | None,
) -> Any:
    context = ssl_context or ssl.create_default_context()
    connection = None
    try:
        if account["security"] == "ssl":
            factory = imap_ssl_factory or imaplib.IMAP4_SSL
            return factory(account["imap_host"], account["imap_port"], ssl_context=context, timeout=timeout)
        factory = imap_factory or imaplib.IMAP4
        connection = factory(account["imap_host"], account["imap_port"], timeout=timeout)
        status, _data = connection.starttls(ssl_context=context)
        if not _status_ok(status):
            raise MailAccessError("IMAP 服务器拒绝 STARTTLS。")
        return connection
    except Exception:
        if connection is not None:
            try:
                connection.logout()
            except Exception:
                pass
        raise MailAccessError("无法建立安全的 IMAP 连接。") from None


def _login_imap(
    connection: Any,
    account: Mapping[str, Any],
    *,
    timeout: float,
    opener: Any = None,
    path: str | Path | None = None,
    expected_store_generation: int | None = None,
) -> None:
    authentication_error = False
    try:
        if account["auth_method"] == "password":
            status, _data = connection.login(account["email"], account["password"])
        else:
            token = _resolve_access_token(
                account,
                timeout=timeout,
                opener=opener,
                path=path,
                expected_store_generation=expected_store_generation,
            )
            auth = f"user={account['email']}\x01auth=Bearer {token}\x01\x01".encode("utf-8")
            status, _data = connection.authenticate("XOAUTH2", lambda _challenge: auth)
    except Exception:
        authentication_error = True
    if authentication_error:
        raise MailAccessError("IMAP 身份验证失败。")
    if not _status_ok(status):
        raise MailAccessError("IMAP 身份验证失败。")


def _decode_header_value(value: str | None, maximum: int = 2_000) -> str:
    if not value:
        return ""
    parts = []
    try:
        decoded = decode_header(value)
    except Exception:
        decoded = [(value, None)]
    for fragment, charset in decoded:
        if isinstance(fragment, bytes):
            try:
                parts.append(fragment.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                parts.append(fragment.decode("utf-8", errors="replace"))
        else:
            parts.append(fragment)
    return "".join(parts).replace("\x00", "")[:maximum]


def _message_text(message: Any, maximum: int) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if (part.get_content_disposition() or "").lower() == "attachment":
            continue
        content_type = part.get_content_type().lower()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True) or b""
            content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if isinstance(content, str):
            (plain_parts if content_type == "text/plain" else html_parts).append(content)
        if sum(len(item) for item in plain_parts + html_parts) >= maximum * 2:
            break
    text = "\n".join(plain_parts)
    if not text and html_parts:
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", "\n".join(html_parts))
        text = unescape(re.sub(r"(?s)<[^>]+>", " ", text))
    text = re.sub(r"[\t\r\f\v ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text[:maximum]


_CONTEXT_CODE_PATTERN = re.compile(
    r"(?i)(?:verification(?:[ -]?code)?|verify(?:[ -]?code)?|security(?:[ -]?code)?|"
    r"one[ -]?time(?:[ -]?(?:password|code))?|passcode|otp(?:[ -]?code)?|"
    r"auth(?:entication)?[ -]?code|验证码|校验码|动态码|登录码|安全码|一次性密码|代码)"
    r"\s*(?:(?:is|为|是)\s*|[:：=]\s*)?([A-Z0-9](?:[ -]?[A-Z0-9]){3,9})"
)
_GENERIC_CODE_PATTERN = re.compile(r"(?<!\d)(\d{6,8})(?!\d)")


def extract_verification_codes(text: str, *, limit: int = 10) -> list[str]:
    if not isinstance(text, str) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValidationError("验证码提取参数无效。")
    results: list[str] = []
    for match in _CONTEXT_CODE_PATTERN.finditer(text[:100_000]):
        code = re.sub(r"[ -]", "", match.group(1)).upper()
        if 4 <= len(code) <= 10 and any(character.isdigit() for character in code) and code not in results:
            results.append(code)
            if len(results) >= limit:
                return results
    for match in _GENERIC_CODE_PATTERN.finditer(text[:100_000]):
        code = match.group(1)
        if code not in results:
            results.append(code)
            if len(results) >= limit:
                break
    return results


def _message_addresses(message: Any, header: str) -> list[str]:
    values = [str(value) for value in message.get_all(header, [])]
    result = []
    for name, address in getaddresses(values):
        if address:
            display = _decode_header_value(name, 256)
            result.append(f"{display} <{address}>" if display else address)
    return result[:50]


def _message_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return _decode_header_value(str(value), 256)


def _extract_fetch_bytes(data: Any) -> tuple[bytes, str]:
    raw = b""
    flags = ""
    if isinstance(data, (list, tuple)):
        for item in data[:1_000]:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                if len(item[1]) > len(raw):
                    raw = item[1]
                if isinstance(item[0], bytes):
                    flags += item[0][:4_096].decode("ascii", errors="ignore")
            elif isinstance(item, bytes):
                flags += item[:4_096].decode("ascii", errors="ignore")
            if len(flags) >= 4_096:
                flags = flags[:4_096]
                break
    return raw, flags[:4_096]


def _mail_summary(uid: str, raw: bytes, flags: str, maximum_text: int, truncated: bool) -> dict[str, Any]:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    text = _message_text(message, maximum_text)
    subject = _decode_header_value(str(message.get("Subject") or ""))
    return {
        "uid": uid,
        "subject": subject,
        "from": _message_addresses(message, "From"),
        "to": _message_addresses(message, "To"),
        "date": _message_date(message.get("Date")),
        "unread": "\\Seen" not in flags,
        "text": text,
        "verification_codes": extract_verification_codes(f"{subject}\n{text}"),
        "truncated": truncated,
    }


def _status_ok(status: Any) -> bool:
    if isinstance(status, bytes):
        status = status.decode("ascii", errors="ignore")
    return str(status).upper() == "OK"


def fetch_email_messages(
    account: Mapping[str, Any],
    *,
    unread_only: bool = False,
    limit: int = 20,
    mailbox: str | None = None,
    timeout: float = DEFAULT_NETWORK_TIMEOUT,
    max_message_bytes: int = MAX_MESSAGE_BYTES,
    max_total_bytes: int = MAX_TOTAL_MESSAGE_BYTES,
    max_text_chars: int = MAX_TEXT_CHARS,
    opener: Any = None,
    imap_ssl_factory: Callable[..., Any] | None = None,
    imap_factory: Callable[..., Any] | None = None,
    ssl_context: ssl.SSLContext | None = None,
    _credential_path: str | Path | None = None,
    _expected_store_generation: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch recent mail via readonly SELECT and bounded BODY.PEEK reads."""
    normalized = _normalize_email_record(account)
    if not isinstance(unread_only, bool):
        raise ValidationError("未读邮件选项必须是布尔值。")
    if type(limit) is not int or not 1 <= limit <= MAX_FETCH_MESSAGES:
        raise ValidationError(f"邮件数量必须在 1 到 {MAX_FETCH_MESSAGES} 之间。")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout)) or not 1 <= float(timeout) <= 120):
        raise ValidationError("网络超时必须在 1 到 120 秒之间。")
    if type(max_message_bytes) is not int or not 1_024 <= max_message_bytes <= MAX_MESSAGE_BYTES:
        raise ValidationError(f"单封邮件上限必须在 1024 到 {MAX_MESSAGE_BYTES} 字节之间。")
    if type(max_total_bytes) is not int or not max_message_bytes <= max_total_bytes <= 20_000_000:
        raise ValidationError("邮件总读取上限无效。")
    if type(max_text_chars) is not int or not 256 <= max_text_chars <= MAX_TEXT_CHARS:
        raise ValidationError(f"邮件正文字符上限必须在 256 到 {MAX_TEXT_CHARS} 之间。")
    if _expected_store_generation is not None and type(_expected_store_generation) is not int:
        raise ValidationError("邮箱凭据版本无效。")
    selected_mailbox = _clean_optional_text(mailbox or normalized.get("mailbox") or "INBOX", "mailbox", 255)
    connection = _connect_imap(
        normalized,
        timeout=float(timeout),
        imap_ssl_factory=imap_ssl_factory,
        imap_factory=imap_factory,
        ssl_context=ssl_context,
    )
    try:
        _login_imap(
            connection,
            normalized,
            timeout=float(timeout),
            opener=opener,
            path=_credential_path,
            expected_store_generation=_expected_store_generation,
        )
        status, _data = connection.select(selected_mailbox, readonly=True)
        if not _status_ok(status):
            raise MailAccessError("无法以只读方式打开邮箱文件夹。")
        status, search_data = connection.uid("search", None, "UNSEEN" if unread_only else "ALL")
        if not _status_ok(status):
            raise MailAccessError("IMAP 邮件检索失败。")
        search_blob = search_data[0] if search_data and isinstance(search_data[0], bytes) else b""
        if len(search_blob) > 10_000_000:
            raise MailAccessError("IMAP 邮件索引响应过大。")
        identifiers = [
            identifier
            for identifier in search_blob.split()
            if re.fullmatch(rb"[1-9][0-9]{0,19}", identifier)
        ]
        messages: list[dict[str, Any]] = []
        total_bytes = 0
        for identifier in identifiers[-limit:][::-1]:
            uid = identifier.decode("ascii", errors="strict")
            status, fetch_data = connection.uid(
                "fetch",
                identifier,
                f"(BODY.PEEK[]<0.{max_message_bytes}> FLAGS INTERNALDATE)",
            )
            if not _status_ok(status):
                continue
            raw, flags = _extract_fetch_bytes(fetch_data)
            if not raw:
                continue
            was_truncated = len(raw) >= max_message_bytes
            raw = raw[:max_message_bytes]
            if total_bytes + len(raw) > max_total_bytes:
                break
            total_bytes += len(raw)
            messages.append(_mail_summary(uid, raw, flags, max_text_chars, was_truncated))
        return messages
    except ToolboxError:
        raise
    except Exception:
        raise MailAccessError("读取 IMAP 邮件失败。") from None
    finally:
        try:
            connection.close()
        except Exception:
            pass
        try:
            connection.logout()
        except Exception:
            pass


def fetch_saved_email_messages(
    account_id: str,
    *,
    path: str | Path | None = None,
    **fetch_options: Any,
) -> list[dict[str, Any]]:
    account, expected_generation = _load_email_account_snapshot(account_id, path=path)
    expected_revision = str(account.get("_credential_revision") or "")
    try:
        messages = fetch_email_messages(
            account,
            _credential_path=path,
            _expected_store_generation=expected_generation,
            **fetch_options,
        )
    except ValidationError:
        raise
    except Exception:
        try:
            _save_email_health(
                account_id,
                status="error",
                expected_revision=expected_revision,
                expected_generation=expected_generation,
                path=path,
            )
        except ToolboxError:
            pass
        raise
    _save_email_health(
        account_id,
        status="healthy",
        message_count=len(messages),
        expected_revision=expected_revision,
        expected_generation=expected_generation,
        path=path,
    )
    return messages


def check_saved_email_health(
    account_id: str,
    *,
    path: str | Path | None = None,
    timeout: float = DEFAULT_NETWORK_TIMEOUT,
    opener: Any = None,
    imap_ssl_factory: Callable[..., Any] | None = None,
    imap_factory: Callable[..., Any] | None = None,
    ssl_context: ssl.SSLContext | None = None,
    commit_guard: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Verify a saved mailbox login without listing or downloading messages.

    The check performs only a secure connection, authentication, and readonly
    mailbox SELECT.  It deliberately avoids SEARCH/FETCH so the background
    health scheduler cannot consume message content or generate inbox polling.
    """

    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout)) or not 1 <= float(timeout) <= 120):
        raise ValidationError("网络超时必须在 1 到 120 秒之间。")
    account, expected_generation = _load_email_account_snapshot(account_id, path=path)
    expected_revision = str(account.get("_credential_revision") or "")
    if commit_guard is not None and not commit_guard():
        return {"accountId": account_id, "status": "cancelled", "healthy": False, "saved": False}

    connection = None
    status = "connection_error"
    try:
        connection = _connect_imap(
            account,
            timeout=float(timeout),
            imap_ssl_factory=imap_ssl_factory,
            imap_factory=imap_factory,
            ssl_context=ssl_context,
        )
        status = "auth_error"
        _login_imap(
            connection,
            account,
            timeout=float(timeout),
            opener=opener,
            path=path,
            expected_store_generation=expected_generation,
        )
        status = "mailbox_error"
        selected, _data = connection.select(
            str(account.get("mailbox") or "INBOX"),
            readonly=True,
        )
        if not _status_ok(selected):
            raise MailAccessError("无法以只读方式打开邮箱文件夹。")
        status = "healthy"
    except ToolboxError:
        pass
    except Exception:
        # Never persist or surface server exception text: IMAP libraries and
        # proxies sometimes echo credentials in diagnostic messages.
        pass
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
            try:
                connection.logout()
            except Exception:
                pass

    saved = _save_email_health(
        account_id,
        status=status,
        expected_revision=expected_revision,
        expected_generation=expected_generation,
        commit_guard=commit_guard,
        path=path,
    )
    return {
        "accountId": account_id,
        "status": status,
        "healthy": status == "healthy",
        "saved": saved,
    }


def stale_mail_account_ids(
    stale_seconds: int = 24 * 60 * 60,
    *,
    path: str | Path | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Return mailboxes whose last credential check is due, without network I/O."""

    try:
        threshold = max(60, int(stale_seconds))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValidationError("邮箱健康检查间隔无效。") from exc
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    due: list[str] = []
    for account in list_mail_accounts(path=path):
        health = account.get("credential_status", {}).get("health", {})
        checked_at = health.get("checkedAt") if isinstance(health, Mapping) else None
        try:
            checked = datetime.fromisoformat(str(checked_at))
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=timezone.utc)
            fresh = (current.astimezone(timezone.utc) - checked.astimezone(timezone.utc)).total_seconds() < threshold
        except (TypeError, ValueError):
            fresh = False
        if not fresh and account.get("id"):
            due.append(str(account["id"]))
    return due


def fetch_mail_messages(
    account_id: str,
    *,
    path: str | Path | None = None,
    **fetch_options: Any,
) -> list[dict[str, Any]]:
    """Public saved-account fetch API; response contains mail only."""
    return fetch_saved_email_messages(account_id, path=path, **fetch_options)
