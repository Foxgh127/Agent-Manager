"""Config editor services."""
from __future__ import annotations
from agent_manager import core as _core


def _codex_model_context_metadata(
    model_id: str,
    *,
    provider_id: str = "",
    settings: dict | None = None,
) -> dict[str, int | str]:
    metadata: dict[str, int | str] = {
        "modelId": model_id,
        "modelContextDefault": 0,
        "modelContextMax": 0,
        "modelContextEffectivePercent": 0,
        "modelContextReferenceMax": 0,
        "modelInputReferenceMax": 0,
    }
    if not model_id:
        return metadata
    if provider_id in {"", "openai"} and _core._official_context_reference_max(model_id):
        # API specification, kept separate from the native Codex catalog's
        # input-budget hints; never lend this limit to a third-party host.
        metadata["modelContextReferenceMax"] = _core._official_context_reference_max(model_id)
        metadata["modelInputReferenceMax"] = _core._official_input_reference_max(model_id)
        metadata["modelContextReferenceUrl"] = f"https://developers.openai.com/api/docs/models/{model_id}"

    def positive_integer(record: dict, key: str) -> int:
        value = record.get(key, 0)
        if isinstance(value, bool):
            return 0
        try:
            result = int(value)
        except (TypeError, ValueError):
            return 0
        return result if result > 0 else 0

    def apply_record(record: dict, keys: tuple[str, str, str]) -> dict[str, int | str]:
        metadata["modelContextDefault"] = positive_integer(record, keys[0])
        metadata["modelContextMax"] = positive_integer(record, keys[1])
        metadata["modelContextEffectivePercent"] = positive_integer(record, keys[2])
        return metadata

    if isinstance(settings, dict) and settings:
        source_id = str(settings.get("modelWorkspace", {}).get("activeSourceId") or "")
        if provider_id and provider_id != "openai":
            try:
                routed = _core.resolve_model_route(model_id, settings) if provider_id == _core.AGGREGATE_PROVIDER_ID else None
            except _core.ManagerError:
                routed = None
            if isinstance(routed, dict):
                if routed.get("sourceKind") == "account" and routed.get("id"):
                    # A routed official alias may carry only reasoning metadata.
                    # Recover exact native context hints, never a relay template.
                    native = _core._codex_model_context_metadata(str(routed["id"]), provider_id="openai")
                    metadata.update({key: value for key, value in native.items() if key != "modelId"})
                    for source_key, target_key in (
                        ("contextWindow", "modelContextDefault"),
                        ("maxContextWindow", "modelContextMax"),
                        ("effectiveContextWindowPercent", "modelContextEffectivePercent"),
                    ):
                        if value := positive_integer(routed, source_key):
                            metadata[target_key] = value
                    return metadata
                return apply_record(
                    routed,
                    ("contextWindow", "maxContextWindow", "effectiveContextWindowPercent"),
                )
            provider = next(
                (
                    item
                    for item in settings.get("providers", [])
                    if isinstance(item, dict) and str(item.get("id") or "") == provider_id
                ),
                None,
            )
            if provider:
                capabilities = _core._normalize_provider_model_capabilities(
                    provider.get("modelCapabilities"),
                    provider.get("models", []),
                )
                capability = capabilities.get(model_id)
                if isinstance(capability, dict):
                    return apply_record(
                        capability,
                        ("contextWindow", "maxContextWindow", "effectiveContextWindowPercent"),
                    )
                # A selected custom Provider with unknown metadata must not
                # borrow an official same-named model's cached limits.
                return metadata
        elif source_id.startswith("account:"):
            account_id = source_id.split(":", 1)[1]
            account = next(
                (
                    item
                    for item in settings.get("accounts", [])
                    if isinstance(item, dict) and str(item.get("id") or "") == account_id
                ),
                None,
            )
            if account:
                capabilities = _core._normalize_provider_model_capabilities(
                    account.get("modelCapabilities"),
                    account.get("models", []),
                )
                capability = capabilities.get(model_id)
                if isinstance(capability, dict) and any(
                    capability.get(key) is not None
                    for key in ("contextWindow", "maxContextWindow", "effectiveContextWindowPercent")
                ):
                    return apply_record(
                        capability,
                        ("contextWindow", "maxContextWindow", "effectiveContextWindowPercent"),
                    )
    if provider_id and provider_id != "openai":
        # The config selects a non-official source, but no matching Manager
        # metadata exists. Treat its limits as unknown rather than falling
        # through to an official same-named cache record.
        return metadata
    try:
        if not _core.MODELS_CACHE_FILE.is_file() or _core.MODELS_CACHE_FILE.stat().st_size > _core.CODEX_CONFIG_MAX_BYTES:
            return metadata
        payload = _core.json.loads(_core.MODELS_CACHE_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, _core.json.JSONDecodeError):
        return metadata
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return metadata
    record = next(
        (
            item
            for item in models
            if isinstance(item, dict)
            and str(item.get("slug") or item.get("id") or item.get("model") or "").strip() == model_id
        ),
        None,
    )
    if not isinstance(record, dict):
        return metadata
    return apply_record(
        record,
        ("context_window", "max_context_window", "effective_context_window_percent"),
    )



def _codex_config_common_values(parsed: dict) -> dict:
    providers = parsed.get("model_providers")
    providers = providers if isinstance(providers, dict) else {}
    provider_id = str(parsed.get("model_provider") or "").strip()
    provider = providers.get(provider_id) if provider_id else None
    provider = provider if isinstance(provider, dict) else None
    if provider is None:
        openai_base_url = str(parsed.get("openai_base_url") or "").rstrip("/")
        if openai_base_url:
            provider_id, provider = next(
                (
                    (str(candidate_id), candidate)
                    for candidate_id, candidate in providers.items()
                    if isinstance(candidate, dict)
                    and str(candidate.get("base_url") or "").rstrip("/") == openai_base_url
                ),
                ("", None),
            )
    if provider is None and not provider_id and not str(parsed.get("openai_base_url") or "").strip():
        provider = providers.get("openai", {})
    provider = provider if isinstance(provider, dict) else {}

    def integer(container: dict, key: str, default: int) -> int:
        value = container.get(key, default)
        if isinstance(value, bool):
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    stream_retries = integer(provider, "stream_max_retries", 5)
    supports_websockets = bool(provider.get("supports_websockets", False))
    provider_has_retry_fix = "stream_max_retries" in provider or "supports_websockets" in provider
    scope = str(parsed.get("model_auto_compact_token_limit_scope") or "total")
    if scope not in _core.VALID_COMPACTION_SCOPES:
        scope = "total"
    summary = str(parsed.get("model_reasoning_summary") or "auto")
    if summary not in _core.VALID_REASONING_SUMMARIES:
        summary = "auto"
    verbosity = str(parsed.get("model_verbosity") or "")
    if verbosity not in _core.VALID_MODEL_VERBOSITIES:
        verbosity = ""
    web_search = str(parsed.get("web_search") or "")
    if web_search not in _core.VALID_WEB_SEARCH_MODES:
        web_search = ""
    tools = parsed.get("tools") if isinstance(parsed.get("tools"), dict) else {}
    web_search_tool = tools.get("web_search")
    web_search_context_size = (
        str(web_search_tool.get("context_size") or "")
        if isinstance(web_search_tool, dict)
        else ""
    )
    if web_search_context_size not in _core.VALID_WEB_SEARCH_CONTEXT_SIZES:
        web_search_context_size = ""
    personality = str(parsed.get("personality") or "")
    if personality not in _core.VALID_PERSONALITIES:
        personality = ""
    service_tier = str(parsed.get("service_tier") or "")
    if service_tier not in _core.VALID_SERVICE_TIERS:
        service_tier = ""
    check_for_updates = parsed.get("check_for_update_on_startup", True)
    if not isinstance(check_for_updates, bool):
        check_for_updates = True
    tui = parsed.get("tui") if isinstance(parsed.get("tui"), dict) else {}
    tui_animations = tui.get("animations", True)
    if not isinstance(tui_animations, bool):
        tui_animations = True
    raw_mcp_grace = parsed.get("mcp_optional_startup_grace_ms", -1)
    if isinstance(raw_mcp_grace, bool):
        mcp_optional_startup_grace_ms = -1
    else:
        try:
            mcp_optional_startup_grace_ms = int(raw_mcp_grace)
        except (TypeError, ValueError):
            mcp_optional_startup_grace_ms = -1
    if mcp_optional_startup_grace_ms < 0 or mcp_optional_startup_grace_ms > 60_000:
        mcp_optional_startup_grace_ms = -1
    features = parsed.get("features") if isinstance(parsed.get("features"), dict) else {}
    prevent_idle_sleep = bool(features.get("prevent_idle_sleep", False))
    model_id = str(parsed.get("model") or "").strip()
    # Configuration inspection is a read-only operation and can run before a
    # transaction has captured SETTINGS_FILE.  load_settings() may persist a
    # schema migration, so use the existing JSON snapshot directly here.
    settings = _core.read_json(_core.SETTINGS_FILE, {})
    if not isinstance(settings, dict):
        settings = None
    return {
        **_core._codex_model_context_metadata(
            model_id,
            provider_id=provider_id,
            settings=settings,
        ),
        "modelContextWindow": integer(parsed, "model_context_window", 0),
        "autoCompactTokenLimit": integer(parsed, "model_auto_compact_token_limit", 0),
        "autoCompactScope": scope,
        "mcpOptionalStartupGraceMs": mcp_optional_startup_grace_ms,
        "reasoningSummary": summary,
        "verbosity": verbosity,
        "webSearch": web_search,
        "webSearchContextSize": web_search_context_size,
        "personality": personality,
        "serviceTier": service_tier,
        "checkForUpdates": check_for_updates,
        "tuiAnimations": tui_animations,
        "requestMaxRetries": integer(provider, "request_max_retries", 4),
        "streamMaxRetries": stream_retries,
        "streamIdleTimeoutMs": integer(provider, "stream_idle_timeout_ms", 300_000),
        "supportsWebsockets": supports_websockets,
        "vpnCompatibility": bool(provider_has_retry_fix and stream_retries == 0 and not supports_websockets),
        "preventIdleSleep": prevent_idle_sleep,
        "providerId": provider_id,
    }



def _codex_config_fingerprint(raw: bytes) -> str:
    return _core.hashlib.sha256(raw).hexdigest()



def codex_config_document() -> dict:
    try:
        exists = _core.CONFIG_FILE.is_file()
        raw = _core.CONFIG_FILE.read_bytes() if exists else b""
        stat = _core.CONFIG_FILE.stat() if exists else None
    except OSError as exc:
        raise _core.ManagerError(f"无法读取 Codex 配置文件：{exc}") from exc
    if len(raw) > _core.CODEX_CONFIG_MAX_BYTES:
        raise _core.ManagerError(
            f"Codex 配置文件超过 {_core.CODEX_CONFIG_MAX_BYTES // 1_000_000} MB，无法在界面中安全编辑。"
        )
    try:
        content = _core.decode_toml_bytes(raw)
    except UnicodeDecodeError as exc:
        return {
            "path": str(_core.CONFIG_FILE),
            "exists": exists,
            "size": len(raw),
            "fingerprint": _core._codex_config_fingerprint(raw),
            "modifiedAt": _core.datetime.fromtimestamp(stat.st_mtime, _core.timezone.utc).isoformat() if stat else None,
            "content": raw.decode("utf-8", errors="replace"),
            "valid": False,
            "error": f"配置文件不是有效的 UTF-8：{exc}",
            "entries": [],
            "common": _core._codex_config_common_values({}),
        }
    try:
        parsed = _core.tomllib.loads(content) if content.strip() else {}
        valid = True
        error = None
    except _core.tomllib.TOMLDecodeError as exc:
        parsed = {}
        valid = False
        error = f"TOML 格式无效：{exc}"
    return {
        "path": str(_core.CONFIG_FILE),
        "exists": exists,
        "size": len(raw),
        "fingerprint": _core._codex_config_fingerprint(raw),
        "modifiedAt": _core.datetime.fromtimestamp(stat.st_mtime, _core.timezone.utc).isoformat() if stat else None,
        "content": content,
        "valid": valid,
        "error": error,
        "entries": _core._flatten_codex_config(parsed) if valid else [],
        "common": _core._codex_config_common_values(parsed),
    }



def _lookup_codex_config_value(parsed: dict, path: list[str]) -> _core.Any:
    current: Any = parsed
    for part in path:
        if not isinstance(current, dict) or part not in current:
            raise _core.ManagerError(f"配置字段 `{'.'.join(path)}` 已不存在，请刷新后重试。")
        current = current[part]
    return current



def _normalize_codex_config_update(current: _core.Any, value: _core.Any, path: list[str]) -> _core.Any:
    value_type = _core._codex_config_value_type(current)
    label = ".".join(path)
    if value_type == "boolean":
        if not isinstance(value, bool):
            raise _core.ManagerError(f"配置字段 `{label}` 必须是布尔值。")
        return value
    if value_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _core.ManagerError(f"配置字段 `{label}` 必须是整数。")
        return value
    if value_type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _core.ManagerError(f"配置字段 `{label}` 必须是数字。")
        return float(value)
    if value_type == "string":
        if not isinstance(value, str) or "\0" in value:
            raise _core.ManagerError(f"配置字段 `{label}` 必须是有效文本。")
        return value
    if value_type == "datetime":
        if not isinstance(value, str) or not value.strip():
            raise _core.ManagerError(f"配置字段 `{label}` 必须是有效日期或时间。")
        try:
            if isinstance(current, _core.datetime):
                return _core.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if isinstance(current, _core.date_value):
                return _core.date_value.fromisoformat(value.strip())
            if isinstance(current, _core.time_value):
                return _core.time_value.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise _core.ManagerError(f"配置字段 `{label}` 必须保持 ISO 日期或时间格式。") from exc
    if value_type == "array":
        if not isinstance(value, list) or not _core._json_toml_value(value):
            raise _core.ManagerError(f"配置字段 `{label}` 必须是可转换为 TOML 的数组 JSON。")
        return value
    raise _core.ManagerError(f"配置字段 `{label}` 需要在原始 TOML 编辑器中修改。")



def _render_codex_config_updates(updates: object) -> str:
    if not isinstance(updates, list) or not updates:
        raise _core.ManagerError("没有需要保存的配置字段。")
    original = _core.read_toml_text(_core.CONFIG_FILE)
    try:
        doc = _core.tomlkit.parse(original) if original.strip() else _core.tomlkit.document()
        parsed = _core.tomllib.loads(original) if original.strip() else {}
    except Exception as exc:
        raise _core.ManagerError(f"无法解析当前 Codex 配置：{exc}") from exc
    seen: set[tuple[str, ...]] = set()
    for update in updates:
        if not isinstance(update, dict) or not isinstance(update.get("path"), list):
            raise _core.ManagerError("配置字段更新格式无效。")
        path = [str(part) for part in update["path"]]
        if not path or len(path) > 32 or any(not part or len(part) > 256 for part in path):
            raise _core.ManagerError("配置字段路径无效。")
        path_key = tuple(path)
        if path_key in seen:
            raise _core.ManagerError(f"配置字段 `{'.'.join(path)}` 重复提交。")
        seen.add(path_key)
        current_value = _core._lookup_codex_config_value(parsed, path)
        normalized = _core._normalize_codex_config_update(current_value, update.get("value"), path)
        parent: Any = doc
        for part in path[:-1]:
            try:
                parent = parent[part]
            except (KeyError, TypeError) as exc:
                raise _core.ManagerError(f"配置字段 `{'.'.join(path)}` 已不存在，请刷新后重试。") from exc
        parent[path[-1]] = normalized
    return _core.tomlkit.dumps(doc)



def _prepare_codex_config_document(payload: dict) -> tuple[bytes, str, bytes]:
    if not isinstance(payload, dict):
        raise _core.ManagerError("Codex 配置保存请求无效。")
    try:
        current_raw = _core.CONFIG_FILE.read_bytes() if _core.CONFIG_FILE.is_file() else b""
    except OSError as exc:
        raise _core.ManagerError(f"无法读取当前 Codex 配置：{exc}") from exc
    expected_fingerprint = str(payload.get("expectedFingerprint") or "").strip().lower()
    if expected_fingerprint:
        if not _core.re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint):
            raise _core.ManagerError("Codex 配置版本指纹无效，请刷新后重试。")
        if not _core.secrets.compare_digest(expected_fingerprint, _core._codex_config_fingerprint(current_raw)):
            raise _core.ManagerError(
                "Codex 配置已被其他程序或窗口修改。为避免覆盖新内容，请重新读取后再保存。"
            )
    if "content" in payload:
        content = payload.get("content")
        if not isinstance(content, str):
            raise _core.ManagerError("原始 TOML 内容必须是文本。")
    elif "updates" in payload:
        content = _core._render_codex_config_updates(payload.get("updates"))
    else:
        raise _core.ManagerError("请提交原始 TOML 或可视化字段更新。")
    if "\0" in content:
        raise _core.ManagerError("Codex 配置不能包含空字符。")
    encoded = content.encode("utf-8")
    if len(encoded) > _core.CODEX_CONFIG_MAX_BYTES:
        raise _core.ManagerError(
            f"Codex 配置不能超过 {_core.CODEX_CONFIG_MAX_BYTES // 1_000_000} MB。"
        )
    try:
        if content.strip():
            _core.tomlkit.parse(content)
            _core.tomllib.loads(content)
    except Exception as exc:
        raise _core.ManagerError(f"TOML 格式无效，未保存：{exc}") from exc
    return current_raw, content, encoded



def _config_runtime_edits(previous_raw: bytes, proposed_text: str) -> tuple[dict, set[str]]:
    try:
        previous = _core.tomllib.loads(_core.decode_toml_bytes(previous_raw))
    except (UnicodeDecodeError, _core.tomllib.TOMLDecodeError):
        previous = {}
    before = _core._codex_config_common_values(previous)
    proposed = _core._codex_config_common_values(_core.tomllib.loads(proposed_text))
    return proposed, {
        field for field in _core.MANAGED_RUNTIME_TUNING_FIELDS
        if before.get(field) != proposed.get(field)
    }



def _save_codex_config_document_locked(payload: dict, *, sync_runtime_ownership: bool = True) -> dict:
    current_raw, content, encoded = _core._prepare_codex_config_document(payload)
    snapshot = _core._capture_file_bytes((_core.CONFIG_FILE, _core.RUNTIME_OVERLAY_FILE, _core.SETTINGS_FILE))
    if snapshot[_core.CONFIG_FILE] == encoded:
        return {
            "changed": False,
            "backupPath": None,
            "restartRequired": False,
            "document": _core.codex_config_document(),
        }
    backup = None
    try:
        latest_raw = _core.CONFIG_FILE.read_bytes() if _core.CONFIG_FILE.is_file() else b""
        if latest_raw != current_raw:
            raise _core.ManagerError(
                "Codex 配置在保存过程中发生变化。为避免覆盖新内容，请重新读取后再保存。"
            )
        backup = _core.backup_file(_core.CONFIG_FILE)
        _core.atomic_write_text(_core.CONFIG_FILE, content)
        if _core.CONFIG_FILE.read_bytes() != encoded:
            raise _core.ManagerError("Codex 配置写入回验失败。")
        _core._runtime_overlay_rebase_user_file_checked(_core.CONFIG_FILE, encoded)
        if sync_runtime_ownership:
            settings = _core.load_settings()
            tuning = _core._normalize_runtime_tuning(settings.get("runtimeTuning"))
            proposed, edited_fields = _core._config_runtime_edits(current_raw, content)
            released = edited_fields.intersection(tuning["managedFields"])
            if released:
                for field in released:
                    tuning[field] = proposed[field]
                tuning["managedFields"] = [field for field in tuning["managedFields"] if field not in released]
                tuning["configManaged"] = bool(tuning["managedFields"])
                settings["runtimeTuning"] = tuning
                _core.save_settings(settings)
        document = _core.codex_config_document()
        if not document.get("valid"):
            raise _core.ManagerError(str(document.get("error") or "Codex 配置回验失败。"))
    except Exception as exc:
        rollback_errors = _core._restore_file_bytes(snapshot)
        suffix = f"；回滚异常：{'；'.join(rollback_errors)}" if rollback_errors else ""
        if isinstance(exc, _core.ManagerError):
            raise _core.ManagerError(f"Codex 配置保存失败：{exc}{suffix}") from exc
        raise _core.ManagerError(f"Codex 配置保存失败：{exc}{suffix}") from exc
    return {
        "changed": True,
        "backupPath": str(backup) if backup else None,
        "restartRequired": True,
        "document": document,
    }



def save_codex_config_document(payload: dict) -> dict:
    with _core.SWITCH_OPERATION_LOCK, _core.CONFIG_FILE_LOCK, _core.RUNTIME_OVERLAY_LOCK, _core.SETTINGS_LOCK, _core.SECRETS_LOCK, _core._settings_file_lock():
        return _core._save_codex_config_document_locked(payload)

