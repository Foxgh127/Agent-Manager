"""Providers services."""
from __future__ import annotations
from agent_manager import core as _core


def _number_value(value: _core.Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        match = _core.re.search(r"-?\d+(?:\.\d+)?", cleaned)
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                return None
    return None



def _parse_provider_balance(payload: _core.Any) -> dict | None:
    if not isinstance(payload, dict):
        return None
    sub2api_usage = bool(
        str(payload.get("mode") or "") in {"quota_limited", "unrestricted"}
        and "isValid" in payload
    )
    for nested_key in ("data", "result", "billing", "credit", "credits", "quota"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            parsed = _core._parse_provider_balance(nested)
            if parsed:
                parent_currency = payload.get("currency") or payload.get("currency_code") or payload.get("unit")
                if not parsed.get("currency") and parent_currency:
                    parsed["currency"] = str(parent_currency or "").upper()[:8]
                    parsed["unit"] = "currency"
                if sub2api_usage:
                    parsed.update(
                        {
                            "integrationKind": "sub2api",
                            "mode": str(payload.get("mode") or ""),
                            "planName": str(payload.get("planName") or "")[:160],
                            "valid": bool(payload.get("isValid")),
                        }
                    )
                return parsed
    currency = str(
        payload.get("currency")
        or payload.get("currency_code")
        or payload.get("unit")
        or ""
    ).strip().upper()[:8]
    amount = None
    source_field = ""
    for key in (
        "total_available",
        "remaining_balance",
        "remainingBalance",
        "available_balance",
        "availableBalance",
        "balance",
        "remaining",
        "available",
        "credits_remaining",
        "credit_balance",
    ):
        amount = _core._number_value(payload.get(key))
        if amount is not None:
            source_field = key
            break
    if amount is None:
        granted = _core._number_value(payload.get("total_granted"))
        used = _core._number_value(payload.get("total_used"))
        if granted is not None and used is not None:
            amount = granted - used
            source_field = "total_granted-total_used"
    if amount is None:
        return None
    result = {
        "amount": round(amount, 6),
        "currency": currency,
        "unit": "currency" if currency else "credits",
        "sourceField": source_field,
    }
    if sub2api_usage:
        result.update(
            {
                "integrationKind": "sub2api",
                "mode": str(payload.get("mode") or ""),
                "planName": str(payload.get("planName") or "")[:160],
                "valid": bool(payload.get("isValid")),
            }
        )
    return result



def _provider_api_root(base_url: str) -> str:
    """Return the dashboard origin/path for an OpenAI-compatible Base URL."""

    parsed = _core.urllib.parse.urlsplit(_core._validated_provider_url(base_url, "中转站 Base URL"))
    path = parsed.path.rstrip("/")
    if path.casefold().endswith("/v1"):
        path = path[:-3].rstrip("/")
    return _core.urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")



def _provider_probe_url(base_url: str, endpoint: str, *, strip_v1: bool = True) -> str:
    root = _core._provider_api_root(base_url) if strip_v1 else base_url.rstrip("/")
    return f"{root}/{endpoint.lstrip('/')}"



def _provider_json_request(
    url: str,
    key: str,
    label: str,
    *,
    timeout: float = 4,
) -> _core.Any:
    request = _core.urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "Codex-Agent-Manager/6",
        },
    )
    with _core._open_same_origin_request(request, timeout=timeout) as response:
        return _core._read_limited_json_response(
            response,
            _core.PROVIDER_RESPONSE_LIMIT_BYTES,
            label,
        )



def _provider_payload_data(value: _core.Any) -> dict:
    if not isinstance(value, dict):
        return {}
    nested = value.get("data")
    return nested if isinstance(nested, dict) else value



def _parse_new_api_provider_usage(
    subscription_payload: _core.Any,
    usage_payload: _core.Any,
    token_usage_payload: _core.Any = None,
) -> dict | None:
    """Normalize New API billing endpoints into the provider balance schema."""

    subscription = _core._provider_payload_data(subscription_payload)
    usage = _core._provider_payload_data(usage_payload)
    token_usage = _core._provider_payload_data(token_usage_payload)
    limit = next(
        (
            value
            for value in (
                _core._number_value(subscription.get("hard_limit_usd")),
                _core._number_value(subscription.get("soft_limit_usd")),
                _core._number_value(subscription.get("system_hard_limit_usd")),
            )
            if value is not None
        ),
        None,
    )
    total_usage_cents = _core._number_value(usage.get("total_usage"))
    used = total_usage_cents / 100 if total_usage_cents is not None else None
    unlimited = bool(token_usage.get("unlimited_quota"))
    if not unlimited:
        hard = _core._number_value(subscription.get("hard_limit_usd"))
        soft = _core._number_value(subscription.get("soft_limit_usd"))
        system = _core._number_value(subscription.get("system_hard_limit_usd"))
        unlimited = bool(
            hard == soft == system == 100_000_000
        )
    total_available = _core._number_value(token_usage.get("total_available"))
    total_granted = _core._number_value(token_usage.get("total_granted"))
    remaining = (
        max(0.0, limit - used)
        if limit is not None and used is not None and not unlimited
        else None
    )
    if not any(
        value is not None
        for value in (limit, used, total_available, total_granted)
    ) and not unlimited:
        return None
    result = {
        "amount": round(remaining, 6) if remaining is not None else None,
        "currency": "USD" if remaining is not None or limit is not None else "",
        "unit": "currency" if remaining is not None or limit is not None else "credits",
        "sourceField": "new_api_billing",
        "integrationKind": "new_api",
        "mode": "unrestricted" if unlimited else "quota_limited",
        "unlimited": unlimited,
        "limit": round(limit, 6) if limit is not None else None,
        "used": round(used, 6) if used is not None else None,
        "totalAvailable": round(total_available, 6) if total_available is not None else None,
        "totalGranted": round(total_granted, 6) if total_granted is not None else None,
        "expiresAt": token_usage.get("expires_at"),
        "modelLimitsEnabled": token_usage.get("model_limits_enabled"),
    }
    if result["amount"] is None and total_available is not None and not unlimited:
        result["amount"] = round(total_available, 6)
    return result



def _probe_new_api_provider_balance(
    base_url: str,
    key: str,
) -> tuple[dict | None, str]:
    subscription_url = _core._provider_probe_url(
        base_url,
        "dashboard/billing/subscription",
    )
    usage_url = _core._provider_probe_url(base_url, "dashboard/billing/usage")
    token_usage_url = _core._provider_probe_url(base_url, "api/usage/token/")
    subscription = _core._provider_json_request(
        subscription_url,
        key,
        "New API 订阅接口",
    )
    usage = _core._provider_json_request(usage_url, key, "New API 用量接口")
    token_usage = None
    try:
        token_usage = _core._provider_json_request(
            token_usage_url,
            key,
            "New API Key 额度接口",
        )
    except (
        _core.urllib.error.HTTPError,
        _core.urllib.error.URLError,
        TimeoutError,
        _core.socket.timeout,
        _core.ssl.SSLError,
        ConnectionError,
        _core.http.client.HTTPException,
        _core.ManagerError,
    ):
        # The two billing endpoints are sufficient. Older New API versions do
        # not expose the optional key-scoped detail endpoint.
        token_usage = None
    return (
        _core._parse_new_api_provider_usage(subscription, usage, token_usage),
        subscription_url,
    )



def _probe_provider_balance_with_key(
    base_url: str,
    key: str,
    *,
    configured_endpoint: object = "",
    integration_kind: object = "",
) -> tuple[dict | None, str, list[str]]:
    """Probe supported key-scoped usage APIs without persisting the secret."""

    base = _core._validated_provider_url(base_url, "中转站 Base URL")
    configured = _core._validated_provider_related_url(
        configured_endpoint,
        base,
        "余额接口",
        allow_empty=True,
        allow_query=True,
    )
    kind = str(integration_kind or "").strip().casefold()
    if kind not in {"", "new_api", "sub2api"}:
        raise _core.ManagerError("中转站集成类型无效。")
    failures: list[str] = []
    root = _core._provider_api_root(base)

    def probe_generic(endpoints: list[str]) -> tuple[dict | None, str]:
        for endpoint in dict.fromkeys(item for item in endpoints if item):
            diagnostic_endpoint = _core._public_diagnostic_url(endpoint)
            try:
                payload = _core._provider_json_request(endpoint, key, "余额接口", timeout=3)
                balance = _core._parse_provider_balance(payload)
                if not balance:
                    failures.append(f"{diagnostic_endpoint}: 返回内容没有可识别余额")
                    continue
                if endpoint.rstrip("/").casefold().endswith("/v1/usage"):
                    balance.setdefault("integrationKind", "sub2api")
                return balance, endpoint
            except _core.urllib.error.HTTPError as exc:
                failures.append(f"{diagnostic_endpoint}: HTTP {exc.code}")
                exc.close()
                continue
            except (
                _core.urllib.error.URLError,
                TimeoutError,
                _core.socket.timeout,
                _core.ssl.SSLError,
                ConnectionError,
                _core.http.client.HTTPException,
                _core.ManagerError,
            ) as exc:
                failures.append(
                    f"{diagnostic_endpoint}: {_core._redact_sensitive_text(exc, limit=240)}"
                )
                break
        return None, ""

    if configured and kind != "new_api":
        balance, endpoint = probe_generic([configured])
        return balance, endpoint, failures

    # Preserve the existing fast Sub2API path, then fall through to Cockpit's
    # New API billing pair and finally the legacy credit-grants endpoints.
    if kind in {"", "sub2api"}:
        balance, endpoint = probe_generic([f"{root}/v1/usage"])
        if balance or kind == "sub2api":
            return balance, endpoint, failures

    if kind in {"", "new_api"}:
        try:
            balance, endpoint = _core._probe_new_api_provider_balance(base, key)
            if balance:
                return balance, endpoint, failures
            failures.append("New API: 返回内容没有可识别额度")
        except _core.urllib.error.HTTPError as exc:
            failures.append(f"New API: HTTP {exc.code}")
            exc.close()
        except (
            _core.urllib.error.URLError,
            TimeoutError,
            _core.socket.timeout,
            _core.ssl.SSLError,
            ConnectionError,
            _core.http.client.HTTPException,
            _core.ManagerError,
        ) as exc:
            failures.append(f"New API: {_core._redact_sensitive_text(exc, limit=240)}")
        if kind == "new_api":
            return None, "", failures

    balance, endpoint = probe_generic(
        [
            f"{root}/dashboard/billing/credit_grants",
            f"{root}/v1/dashboard/billing/credit_grants",
        ]
    )
    if balance:
        return balance, endpoint, failures
    return None, "", failures



def _read_limited_json_response(response: _core.Any, max_bytes: int, label: str) -> _core.Any:
    try:
        declared = int(response.headers.get("Content-Length") or 0)
    except (AttributeError, TypeError, ValueError):
        declared = 0
    if declared > max_bytes:
        raise _core.ManagerError(f"{label}响应超过 {max_bytes // 1_000_000} MB 安全限制，已停止读取。")
    raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise _core.ManagerError(f"{label}响应超过 {max_bytes // 1_000_000} MB 安全限制，已停止读取。")
    try:
        return _core.json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, _core.json.JSONDecodeError) as exc:
        raise _core.ManagerError(f"{label}返回了无效 JSON。") from exc



def fetch_provider_balance(provider_id: str) -> dict | None:
    settings = _core.load_settings()
    provider = _core.provider_by_id(provider_id, settings)
    if provider.get("kind") != "custom":
        return None
    key = _core.load_provider_key(provider_id, required=True)
    probe_token = _core._provider_probe_token(provider)
    configured = str(provider.get("balanceEndpoint") or "").strip()
    balance, endpoint, failures = _core._probe_provider_balance_with_key(
        str(provider.get("baseUrl") or ""),
        str(key or ""),
        configured_endpoint=configured,
        integration_kind=provider.get("integrationKind"),
    )
    if balance:
        with _core.SETTINGS_LOCK:
            latest = _core.load_settings()
            if not _core._provider_probe_is_current(latest, provider_id, probe_token):
                raise _core._ProviderRefreshSuperseded("中转站配置已在刷新期间变化，已丢弃旧余额结果。")
            for record in latest.get("providers", []):
                if record.get("id") == provider_id:
                    record["balance"] = balance
                    record["balanceUpdatedAt"] = _core.now_iso()
                    record["balanceError"] = None
                    if endpoint and (not configured or balance.get("integrationKind") == "new_api"):
                        record["balanceEndpoint"] = endpoint
                    detected_kind = str(balance.get("integrationKind") or "").strip()
                    if detected_kind in {"new_api", "sub2api"}:
                        record["integrationKind"] = detected_kind
            _core.save_settings(latest)
        return balance
    with _core.SETTINGS_LOCK:
        latest = _core.load_settings()
        if not _core._provider_probe_is_current(latest, provider_id, probe_token):
            raise _core._ProviderRefreshSuperseded("中转站配置已在刷新期间变化，已丢弃旧余额错误。")
        for record in latest.get("providers", []):
            if record.get("id") == provider_id:
                record["balanceUpdatedAt"] = _core.now_iso()
                record["balanceError"] = (
                    "未提供兼容的额度接口"
                    if not configured
                    else "额度接口探测失败"
                )
                if failures:
                    record["balanceProbeError"] = "；".join(failures)[:640]
        _core.save_settings(latest)
    return None



def fetch_provider_models(provider_id: str) -> list[str]:
    if provider_id == "openai":
        return [item["id"] for item in _core.local_model_catalog(force=True)]
    settings = _core.load_settings()
    provider = _core.provider_by_id(provider_id, settings)
    key = _core.load_provider_key(provider_id, required=True)
    probe_token = _core._provider_probe_token(provider)
    configured_base = _core._validated_provider_url(provider.get("baseUrl"), "中转站 Base URL")
    resolved_base = _core._validated_provider_related_url(
        provider.get("resolvedBaseUrl"),
        configured_base,
        "中转站已探测 API Base URL",
        allow_empty=True,
    )
    configured_models_endpoint = _core._validated_provider_related_url(
        provider.get("modelsEndpoint"),
        configured_base,
        "中转站模型目录接口",
        allow_empty=True,
        allow_query=True,
    )
    # A previously confirmed runtime base is authoritative.  Probe the
    # configured root only when there is no such address, otherwise a relay
    # that happens to expose a root /models endpoint could erase /v1.
    base = resolved_base or configured_base
    endpoints = [configured_models_endpoint] if configured_models_endpoint else []
    endpoints.append(f"{base}/models")
    if not base.endswith("/v1"):
        endpoints.append(f"{base}/v1/models")
    failures = []
    authentication_failure = False
    for endpoint in dict.fromkeys(endpoints):
        diagnostic_endpoint = _core._public_diagnostic_url(endpoint)
        started_at = _core.time.monotonic()
        request = _core.urllib.request.Request(
            endpoint,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": "Codex-Agent-Manager/2"},
        )
        try:
            with _core._open_same_origin_request(request, timeout=25) as response:
                payload = _core._read_limited_json_response(response, _core.PROVIDER_RESPONSE_LIMIT_BYTES, "模型接口")
            catalog = _core._parse_provider_model_catalog(payload)
            models = catalog["models"]
            if models:
                parsed_endpoint = _core.urllib.parse.urlsplit(endpoint)
                endpoint_path = parsed_endpoint.path.rstrip("/")
                resolved_base_url = (
                    _core.urllib.parse.urlunsplit(
                        (
                            parsed_endpoint.scheme,
                            parsed_endpoint.netloc,
                            endpoint_path[: -len("/models")].rstrip("/"),
                            "",
                            "",
                        )
                    )
                    if endpoint != configured_models_endpoint
                    and endpoint_path.casefold().endswith("/models")
                    else ""
                )
                with _core.SETTINGS_LOCK:
                    latest = _core.load_settings()
                    if not _core._provider_probe_is_current(latest, provider_id, probe_token):
                        raise _core._ProviderRefreshSuperseded(
                            "中转站配置已在刷新期间变化，已丢弃旧模型目录。"
                        )
                    for record in latest["providers"]:
                        if record.get("id") == provider_id:
                            previous_signature = _core._provider_runtime_signature(record)
                            _core._sync_source_model_selections(
                                latest,
                                f"provider:{provider_id}",
                                record.get("models", []),
                                models,
                            )
                            record["models"] = models
                            record["modelCapabilities"] = catalog["modelCapabilities"]
                            record["modelCapabilitiesRefreshedAt"] = _core.now_iso()
                            # A Cockpit/CCSwitch export often stores the service
                            # root while OpenAI-compatible inference actually
                            # lives below /v1. Remember the endpoint that really
                            # answered instead of making Codex guess later, but
                            # never replace an address the user already pinned.
                            if resolved_base_url and not str(record.get("resolvedBaseUrl") or "").strip():
                                record["resolvedBaseUrl"] = resolved_base_url
                            record["discoveredAt"] = _core.now_iso()
                            record["lastCheckedAt"] = _core.now_iso()
                            record["lastLatencyMs"] = round((_core.time.monotonic() - started_at) * 1000)
                            record["lastCheckStatus"] = "ok"
                            record["lastCheckError"] = None
                            record["modelDiscoveryState"] = "ready"
                            record["modelDiscoveryError"] = None
                            if _core._provider_runtime_signature(record) != previous_signature:
                                current_revision, applied_revision = _core._provider_runtime_revisions(record)
                                record["runtimeRevision"] = current_revision + 1
                                record["appliedRuntimeRevision"] = applied_revision
                    _core.save_settings(latest)
                return models
            failures.append(f"{diagnostic_endpoint}: 没有模型 ID")
        except _core.urllib.error.HTTPError as exc:
            failures.append(f"{diagnostic_endpoint}: HTTP {exc.code}")
            authentication_failure = authentication_failure or exc.code in {401, 402}
            should_try_alternate = exc.code in {403, 404, 405}
            exc.close()
            if not should_try_alternate:
                break
        except _core._ProviderRefreshSuperseded:
            raise
        except (
            _core.urllib.error.URLError,
            TimeoutError,
            _core.socket.timeout,
            _core.ssl.SSLError,
            ConnectionError,
            _core.http.client.HTTPException,
            _core.ManagerError,
        ) as exc:
            failures.append(f"{diagnostic_endpoint}: {_core._redact_sensitive_text(exc, limit=240)}")
            break
    diagnostic_error = "；".join(failures)
    with _core.SETTINGS_LOCK:
        latest = _core.load_settings()
        if not _core._provider_probe_is_current(latest, provider_id, probe_token):
            raise _core._ProviderRefreshSuperseded("中转站配置已在刷新期间变化，已丢弃旧探测错误。")
        for record in latest["providers"]:
            if record.get("id") == provider_id:
                has_cached_models = bool(
                    [str(item).strip() for item in record.get("models", []) if str(item).strip()]
                )
                record["lastCheckedAt"] = _core.now_iso()
                # A catalog outage or permission error does not invalidate a
                # provider that still has a usable cached/manual model.  An
                # explicit 401/402 is different: it indicates credentials or
                # account billing are unusable and must remain a hard error.
                status = (
                    "error"
                    if authentication_failure or not has_cached_models
                    else "stale"
                )
                record["lastCheckStatus"] = status
                record["lastCheckError"] = diagnostic_error[:640]
                record["modelDiscoveryState"] = status
                record["modelDiscoveryError"] = diagnostic_error[:640]
        _core.save_settings(latest)
    raise _core._ProviderProbeError("无法自动获取模型。" + diagnostic_error)



def refresh_provider_models(provider_id: str) -> dict:
    """Refresh optional catalog metadata without discarding manual models.

    Many OpenAI-compatible relays support inference but intentionally deny
    ``/models``.  A cached/manual catalog therefore produces a successful,
    explicitly stale response.  Authentication/billing failures and an empty
    catalog remain hard failures.
    """

    try:
        models = _core.fetch_provider_models(provider_id)
        provider = _core.provider_by_id(provider_id)
        return {
            "models": models,
            "modelCapabilities": _core._normalize_provider_model_capabilities(
                provider.get("modelCapabilities"),
                models,
            ),
            "needsModel": not bool(models),
            "status": "ready",
            "warning": None,
        }
    except _core.ManagerError as exc:
        provider = _core.provider_by_id(provider_id)
        models = [
            str(item).strip()
            for item in provider.get("models", [])
            if str(item).strip()
        ]
        # Only downgrade a catalog request that actually reached the remote
        # probe.  Missing keys, malformed URLs, and other local validation
        # errors must not be hidden by an old stale status.
        probe_recorded = bool(getattr(exc, "probe_recorded", False))
        if (
            probe_recorded
            and models
            and str(provider.get("lastCheckStatus") or "") == "stale"
        ):
            return {
                "models": list(dict.fromkeys(models)),
                "modelCapabilities": _core._normalize_provider_model_capabilities(
                    provider.get("modelCapabilities"),
                    models,
                ),
                "needsModel": False,
                "status": "stale",
                "warning": (
                    "服务商未开放或暂时无法访问自动模型目录；"
                    "已保留现有模型，不影响按已配置模型使用。"
                ),
            }
        raise



def _provider_catalog_auth_failure(value: object) -> bool:
    return bool(_core.re.search(r"\bHTTP\s+(?:401|402)\b", str(value or ""), flags=_core.re.IGNORECASE))



def _preferred_discovered_model(models: list[str]) -> str:
    if not models:
        return ""
    preferences = ("gpt-6-astra", "gpt-5.6", "gpt-5.6-sol", "gpt-5.5", "gpt-5.4")
    folded = {item.casefold(): item for item in models}
    for preference in preferences:
        if preference in folded:
            return folded[preference]
    for needle in ("gpt-5.6", "gpt-5.5", "codex", "gpt"):
        match = next((item for item in models if needle in item.casefold()), None)
        if match:
            return match
    return models[0]



def _probe_provider_models_with_key(
    base_url: str,
    key: str,
    *,
    models_endpoint: object = "",
) -> dict:
    """Read a provider model catalog without writing the API Key to disk."""

    configured_base = _core._validated_provider_url(base_url, "中转站 Base URL")
    configured_models_endpoint = _core._validated_provider_related_url(
        models_endpoint,
        configured_base,
        "中转站模型目录接口",
        allow_empty=True,
        allow_query=True,
    )
    endpoints = [configured_models_endpoint] if configured_models_endpoint else []
    endpoints.append(f"{configured_base}/models")
    if not configured_base.endswith("/v1"):
        endpoints.append(f"{configured_base}/v1/models")
    failures: list[str] = []
    authentication_failure = False
    for endpoint in dict.fromkeys(item for item in endpoints if item):
        diagnostic_endpoint = _core._public_diagnostic_url(endpoint)
        started_at = _core.time.monotonic()
        try:
            payload = _core._provider_json_request(
                endpoint,
                key,
                "模型接口",
                timeout=25,
            )
            catalog = _core._parse_provider_model_catalog(payload)
            models = catalog["models"]
            if not models:
                failures.append(f"{diagnostic_endpoint}: 没有模型 ID")
                continue
            parsed_endpoint = _core.urllib.parse.urlsplit(endpoint)
            endpoint_path = parsed_endpoint.path.rstrip("/")
            resolved_base_url = ""
            if (
                endpoint != configured_models_endpoint
                and endpoint_path.casefold().endswith("/models")
            ):
                resolved_base_url = _core.urllib.parse.urlunsplit(
                    (
                        parsed_endpoint.scheme,
                        parsed_endpoint.netloc,
                        endpoint_path[: -len("/models")].rstrip("/"),
                        "",
                        "",
                    )
                )
            return {
                "models": models,
                "modelCapabilities": catalog["modelCapabilities"],
                "modelsEndpoint": endpoint,
                "resolvedBaseUrl": resolved_base_url,
                "latencyMs": round((_core.time.monotonic() - started_at) * 1000),
            }
        except _core.urllib.error.HTTPError as exc:
            failures.append(f"{diagnostic_endpoint}: HTTP {exc.code}")
            authentication_failure = authentication_failure or exc.code in {401, 402}
            should_try_alternate = exc.code in {403, 404, 405}
            exc.close()
            if not should_try_alternate:
                break
        except (
            _core.urllib.error.URLError,
            TimeoutError,
            _core.socket.timeout,
            _core.ssl.SSLError,
            ConnectionError,
            _core.http.client.HTTPException,
            _core.ManagerError,
        ) as exc:
            failures.append(
                f"{diagnostic_endpoint}: {_core._redact_sensitive_text(exc, limit=240)}"
            )
            break
    error = _core._ProviderProbeError("无法自动获取模型。" + "；".join(failures))
    error.authentication_failure = authentication_failure
    raise error



def _suggest_api_provider_identity(payload: dict, base_url: str, key: str) -> dict:
    parsed = _core.urllib.parse.urlsplit(base_url)
    hostname = str(parsed.hostname or "provider").casefold().rstrip(".")
    display_host = _core.re.sub(r"^(?:api|www)\.", "", hostname) or hostname
    name = str(payload.get("name") or "").strip() or (
        f"本地 API · {parsed.netloc}"
        if hostname in {"localhost", "127.0.0.1", "::1"}
        else display_host
    )
    requested_id = str(payload.get("id") or "").strip()
    if requested_id:
        provider_id = _core.slugify(requested_id, "Provider ID")
    else:
        host_slug = _core.re.sub(r"[^a-z0-9]+", "_", hostname).strip("_") or "provider"
        if not host_slug[0].isalpha():
            host_slug = f"host_{host_slug}"
        fingerprint = _core.hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
        provider_id = _core.slugify(
            f"api_{host_slug[:43]}_{fingerprint}"[:64],
            "Provider ID",
        )
    env_key = str(payload.get("envKey") or "").strip() or (
        f"{provider_id.upper()}_API_KEY"
    )
    env_key = _core._validate_provider_env_key(env_key)
    portal_url = str(payload.get("portalUrl") or "").strip()
    if not portal_url:
        portal_url = _core.urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, "", "", "")
        ).rstrip("/")
    return {
        "id": provider_id,
        "name": name[:160],
        "envKey": env_key,
        "portalUrl": portal_url,
    }



def probe_api_account(payload: dict) -> dict:
    """Inspect an API account from only Base URL + API Key, without saving it."""

    if not isinstance(payload, dict):
        raise _core.ManagerError("API 账号检测内容无效。")
    base_url = _core._validated_provider_url(payload.get("baseUrl"), "Base URL")
    key = str(payload.get("key") or "").strip()
    if not key:
        raise _core.ManagerError("API Key 不能为空。")
    identity = _core._suggest_api_provider_identity(payload, base_url, key)
    model_probe = None
    model_error = ""
    model_auth_failure = False
    try:
        model_probe = _core._probe_provider_models_with_key(
            base_url,
            key,
            models_endpoint=payload.get("modelsEndpoint"),
        )
    except _core.ManagerError as exc:
        model_error = _core._redact_sensitive_text(exc, limit=640)
        model_auth_failure = bool(getattr(exc, "authentication_failure", False))

    balance, balance_endpoint, balance_failures = _core._probe_provider_balance_with_key(
        base_url,
        key,
        configured_endpoint=payload.get("balanceEndpoint"),
        integration_kind=payload.get("integrationKind"),
    )
    integration_kind = str(
        (balance or {}).get("integrationKind")
        or payload.get("integrationKind")
        or ""
    ).strip()
    warnings = []
    if model_error:
        warnings.append(model_error)
    if not balance:
        warnings.append("未检测到兼容的额度接口；这不会阻止使用已识别的模型。")
    models = list((model_probe or {}).get("models") or [])
    model_capabilities = _core._normalize_provider_model_capabilities(
        (model_probe or {}).get("modelCapabilities"),
        models,
    )
    if model_auth_failure and not balance:
        status = "error"
    elif models:
        status = "ready" if balance else "partial"
    else:
        status = "partial"
    kind_labels = {
        "new_api": "New API",
        "sub2api": "Sub2API",
    }
    return {
        **identity,
        "baseUrl": base_url,
        "resolvedBaseUrl": str((model_probe or {}).get("resolvedBaseUrl") or ""),
        "modelsEndpoint": str((model_probe or {}).get("modelsEndpoint") or ""),
        "balanceEndpoint": balance_endpoint,
        "integrationKind": integration_kind,
        "integrationLabel": kind_labels.get(integration_kind, "OpenAI 兼容 API"),
        "models": models,
        "modelCapabilities": model_capabilities,
        "needsModel": not bool(models),
        "preferredModel": _core._preferred_discovered_model(models),
        "modelLatencyMs": (model_probe or {}).get("latencyMs"),
        "balance": balance,
        "status": status,
        "warnings": warnings,
        "balanceProbeFailures": balance_failures[:6],
        "detectedAt": _core.now_iso(),
    }



def import_api_account(payload: dict) -> dict:
    with _core.SETTINGS_LOCK, _core.SECRETS_LOCK, _core._settings_file_lock():
        snapshot = _core._capture_file_bytes((_core.SETTINGS_FILE, _core.SECRETS_FILE))
        try:
            if not isinstance(payload, dict):
                raise _core.ManagerError("API 账号导入内容无效。")
            base_url = _core._validated_provider_url(payload.get("baseUrl"), "Base URL")
            key = str(payload.get("key") or "").strip()
            if not key:
                raise _core.ManagerError("API Key 不能为空。")
            identity = _core._suggest_api_provider_identity(payload, base_url, key)
            manual_model = str(payload.get("model") or "").strip()
            supplied_models = (
                [str(item).strip() for item in payload.get("models", []) if str(item).strip()]
                if isinstance(payload.get("models"), list)
                else []
            )
            if manual_model and manual_model not in supplied_models:
                supplied_models.insert(0, manual_model)
            provider_payload = {
                "id": identity["id"],
                "name": identity["name"],
                "baseUrl": base_url,
                "resolvedBaseUrl": payload.get("resolvedBaseUrl") or "",
                "presetId": payload.get("presetId") or "",
                "portalUrl": identity["portalUrl"],
                "integrationKind": payload.get("integrationKind") or "",
                "envKey": identity["envKey"],
                "groupId": payload.get("groupId") or "relay",
                "modelsEndpoint": payload.get("modelsEndpoint") or "",
                "balanceEndpoint": payload.get("balanceEndpoint") or "",
                "sourceType": payload.get("sourceType") or "manual_api",
                "relayAccountId": payload.get("relayAccountId") or "",
                "relayKeyId": payload.get("relayKeyId") or "",
                "relayGroupId": payload.get("relayGroupId") or "",
                "relayGroupName": payload.get("relayGroupName") or "",
                "relayPlatform": payload.get("relayPlatform") or "",
                "relayRateMultiplier": payload.get("relayRateMultiplier"),
                "relayEndpointId": payload.get("relayEndpointId") or "",
                "relayEndpointName": payload.get("relayEndpointName") or "",
                # Seed a hand-entered model before discovery.  This lets a
                # relay without /models complete import while retaining a
                # diagnosable stale catalog state after a 403/404 response.
                "models": supplied_models,
                "modelCapabilities": payload.get("modelCapabilities") or {},
            }
            provider = _core.save_provider(
                provider_payload,
                str(payload.get("originalId") or "") or None,
                api_key=key,
            )
            # An authenticated relay dashboard can expose an account balance
            # even when the key-scoped /v1/usage endpoint is unavailable.  Seed
            # that freshly observed value so the provider card is useful
            # immediately; the normal key-scoped refresh below may replace it.
            balance_snapshot = _core._parse_provider_balance(payload.get("balanceSnapshot"))
            if balance_snapshot:
                used_snapshot = _core._number_value(
                    (payload.get("balanceSnapshot") or {}).get("used")
                    if isinstance(payload.get("balanceSnapshot"), dict)
                    else None
                )
                if used_snapshot is not None:
                    balance_snapshot["used"] = round(used_snapshot, 6)
                balance_snapshot["source"] = "relay-dashboard"
                latest = _core.load_settings()
                for record in latest.get("providers", []):
                    if record.get("id") != provider["id"]:
                        continue
                    record["balance"] = balance_snapshot
                    record["balanceUpdatedAt"] = _core.now_iso()
                    record["balanceError"] = None
                    provider = record
                    break
                _core.save_settings(latest)
            models = list(provider.get("models") or [])
            discovery_error = None
            if payload.get("fetchModels", True):
                try:
                    models = _core.fetch_provider_models(provider["id"])
                except _core.ManagerError as exc:
                    discovery_error = str(exc)
                    latest = _core.load_settings()
                    for record in latest.get("providers", []):
                        if record.get("id") != provider["id"]:
                            continue
                        has_cached_models = bool(
                            [str(item).strip() for item in record.get("models", []) if str(item).strip()]
                        )
                        record["lastCheckedAt"] = _core.now_iso()
                        current_status = str(record.get("lastCheckStatus") or "")
                        if current_status not in {"stale", "error"}:
                            current_status = (
                                "error"
                                if _core._provider_catalog_auth_failure(discovery_error) or not has_cached_models
                                else "stale"
                            )
                        record["lastCheckStatus"] = current_status
                        record["lastCheckError"] = _core._redact_sensitive_text(discovery_error, limit=640)
                        record["modelDiscoveryState"] = current_status
                        record["modelDiscoveryError"] = record["lastCheckError"]
                    _core.save_settings(latest)
                    failed_provider = _core.provider_by_id(provider["id"], latest)
                    if (
                        str(failed_provider.get("lastCheckStatus") or "") == "error"
                        and _core._provider_catalog_auth_failure(discovery_error)
                    ):
                        raise _core.ManagerError(
                            "API Key 或中转站账号不可用（远端返回 401/402），请检查凭据或余额后重试。"
                        ) from exc
            model = manual_model or _core._preferred_discovered_model(models)
            if not model:
                suffix = f" 自动获取失败：{discovery_error}" if discovery_error else ""
                raise _core.ManagerError("没有可用模型；请填写模型 ID 后重试。" + suffix)
            if model not in models:
                models = [model, *models]
            latest = _core.load_settings()
            for record in latest.get("providers", []):
                if record.get("id") == provider["id"]:
                    record["models"] = list(dict.fromkeys(models))
                    provider = record
            _core.save_settings(latest)
            balance = None
            balance_warning = None
            if payload.get("fetchBalance", True):
                try:
                    balance = _core.fetch_provider_balance(provider["id"])
                    if balance is None:
                        balance_warning = "未检测到兼容的额度接口；模型配置不受影响。"
                except _core.ManagerError as exc:
                    balance_warning = (
                        "额度同步暂不可用；模型配置已保存。"
                        f" {_core._redact_sensitive_text(exc, limit=220)}"
                    )
                provider = _core.provider_by_id(provider["id"])
            effort = str(payload.get("effort") or "").strip()
            if effort and effort not in _core.VALID_EFFORTS:
                raise _core.ManagerError("主模型推理强度无效。")
            profile_id = _core.slugify((f"api_{provider['id']}")[:64], "API 主模型预设 ID")
            profile = _core.save_main_profile(
                {
                    "id": profile_id,
                    "name": str(payload.get("profileName") or f"API · {provider['name']}").strip(),
                    "provider": provider["id"],
                    "model": model,
                    "effort": effort,
                }
            )
            if payload.get("activate", True):
                _core.set_active_main(profile_id)
            proxy_enabled = bool(payload.get("proxyEnabled", False))
            if proxy_enabled:
                provider = _core.set_provider_proxy_enabled(provider["id"], True)
            discovery_status = str(provider.get("lastCheckStatus") or "pending")
            discovery_warning = (
                "服务商未开放或暂时无法访问自动模型目录；"
                "已保留手动填写的模型，不影响使用。"
                if discovery_status == "stale"
                else None
            )
            effective_discovery_error = (
                "自动模型目录不可用，且没有可确认的模型。"
                if discovery_status == "error"
                else None
            )
            return {
                "provider": provider,
                "profile": profile,
                "models": models,
                "modelCapabilities": _core._normalize_provider_model_capabilities(
                    provider.get("modelCapabilities"),
                    models,
                ),
                "needsModel": not bool(models),
                "discoveryError": effective_discovery_error,
                "discoveryWarning": discovery_warning,
                "discoveryStatus": discovery_status,
                "discovery": {
                    "status": discovery_status,
                    "warning": discovery_warning,
                    "error": effective_discovery_error,
                },
                "balance": balance,
                "balanceWarning": balance_warning,
                "proxyEnabled": proxy_enabled,
            }
        except Exception as exc:
            rollback_errors = _core._restore_file_bytes(snapshot)
            if rollback_errors:
                raise _core.ManagerError(
                    "导入 API 账号失败："
                    f"{_core._redact_sensitive_text(exc, limit=320)}；回滚也未完成："
                    f"{'；'.join(_core._redact_sensitive_text(item, limit=240) for item in rollback_errors)}"
                ) from exc
            raise

