"""Bounded input adapters; never import a foreign application's configuration."""
from __future__ import annotations

import json

MAX_PROVIDER_CONTEXTS = 256
MAX_PROVIDER_DEPTH = 8


class ImportFormatError(ValueError):
    pass


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ImportFormatError("账号 JSON 包含重复字段，已拒绝歧义凭据。")
        result[key] = value
    return result


def strict_loads(value):
    return json.loads(value, object_pairs_hook=unique_pairs)


# These are application state, incoming proxy/admin credentials or catalog data,
# not upstream account collections. Do not recurse into them looking for tokens.
NON_ACCOUNT_CONTAINERS = {
    "admin", "administrator", "management", "remotemanagement", "settings",
    "config", "configuration", "routing", "routes", "routingrules", "apikeys",
    "clientkeys", "adminkeys", "secrets", "environment", "env", "models",
    "modelcatalog", "modelcapabilities", "users", "sessions", "plugins",
}


def container_name(value):
    return "".join(char for char in str(value).casefold() if char.isalnum())


def assert_account_scope(payload):
    for field in ("type", "auth_type", "authType", "purpose", "role"):
        value = payload.get(field)
        if isinstance(value, str) and container_name(value) in {
            "admin", "administrator", "management", "inbound", "router", "clientkey", "adminkey",
        }:
            raise ImportFormatError("检测到管理或入口凭据；请只导入上游账号凭据。")


def provider_contexts(payload):
    """Known wrappers only; retain outer base URL for nested credentials."""
    contexts = []
    visited = set()

    def visit(value, depth):
        if not isinstance(value, dict):
            return
        if depth > MAX_PROVIDER_DEPTH:
            raise ImportFormatError("API 凭据包装嵌套超过 8 层，已停止解析。")
        if id(value) in visited:
            raise ImportFormatError("API 凭据包装包含重复或循环对象，已停止解析。")
        if len(contexts) >= MAX_PROVIDER_CONTEXTS:
            raise ImportFormatError("API 凭据包装节点超过 256 个，已停止解析。")
        visited.add(id(value))
        assert_account_scope(value)
        contexts.append(value)
        for key in ("provider", "config", "credentials", "env", "authJson", "auth_json", "auth", "data", "payload", "result"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.lstrip().startswith("{"):
                try:
                    nested = strict_loads(nested)
                except json.JSONDecodeError:
                    raise ImportFormatError("API 凭据包装不是有效 JSON。") from None
            if isinstance(nested, dict):
                visit(nested, depth + 1)

    visit(payload, 0)
    return contexts
