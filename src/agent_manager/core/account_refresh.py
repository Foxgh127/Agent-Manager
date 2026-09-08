"""Account refresh services."""
from __future__ import annotations
from agent_manager import core as _core


def _account_refresh_lock_for(account_id: str) -> _core.threading.RLock:
    with _core.ACCOUNT_REFRESH_LOCKS_LOCK:
        # Redeeming a reset card performs a follow-up quota refresh while it
        # still owns this per-account gate.  An RLock keeps that nested refresh
        # serialized without deadlocking, and prevents a rapid double-click
        # from submitting two independent redeem operations for one card.
        return _core.ACCOUNT_REFRESH_LOCKS.setdefault(str(account_id), _core.threading.RLock())



def _persist_account_oauth_auth(
    account_id: str,
    account: dict,
    auth_bytes: bytes,
    cap_sid_bytes: bytes | None,
    *,
    replace_live_if_active: bool,
) -> None:
    snapshot, identity = _core._snapshot_from_bytes(auth_bytes, cap_sid_bytes)
    if not _core._account_matches_identity(account, identity):
        raise _core.ManagerError("OAuth 续期后的账号身份发生变化，已拒绝覆盖本地凭据。")
    encrypted_snapshot = _core._encrypted_account_snapshot(snapshot)
    projected_auth = _core._codex_auth_projection_bytes(auth_bytes)
    auth_path = _core.CODEX_HOME / "auth.json"
    with _core.SWITCH_OPERATION_LOCK, _core.SETTINGS_LOCK, _core.SECRETS_LOCK, _core._settings_file_lock():
        latest = _core.load_settings()
        live_account = next(
            (item for item in latest.get("accounts", []) if str(item.get("id") or "") == account_id),
            None,
        )
        if not live_account or not _core._account_matches_identity(live_account, identity):
            raise _core.ManagerError("账号已被删除或身份已变化，未写入 OAuth 续期结果。")
        active = False
        if replace_live_if_active and auth_path.is_file():
            try:
                active_identity = _core._identity_from_auth_bytes(auth_path.read_bytes())
                active = _core._account_matches_identity(live_account, active_identity)
            except (_core.ManagerError, OSError):
                active = False
        paths = [_core.SECRETS_FILE]
        if active:
            paths.append(auth_path)
        before = _core._capture_file_bytes(paths)
        secrets_payload = _core._secret_store()
        secrets_payload["accounts"][account_id] = encrypted_snapshot
        try:
            # If this account is active, write the newly rotated refresh token
            # to Codex first. A process interruption between the two atomic
            # replaces then leaves the live authority available to repair the
            # encrypted snapshot on the next switch/refresh, rather than
            # leaving Codex with a consumed refresh token.
            if active:
                _core.atomic_write_bytes(auth_path, projected_auth)
            _core.atomic_write_json(_core.SECRETS_FILE, secrets_payload)
        except Exception as exc:
            rollback_errors = _core._restore_file_bytes(before)
            if rollback_errors:
                raise _core.ManagerError(
                    f"OAuth 续期写回失败：{_core._redact_sensitive_text(exc, limit=220)}；"
                    f"回滚也未完成：{'；'.join(rollback_errors)}"
                ) from exc
            raise
    if active:
        _core._reset_switch_caches()



def _account_chatgpt_credentials(account_id: str, *, force_refresh: bool = False, rejected_access_token: str | None = None) -> dict:
    """Return usable account credentials, refreshing official OAuth when due.

    The encrypted account snapshot is authoritative for inactive accounts. For
    the active account, Codex may have already rotated its refresh token, so
    merge the live official auth file back into the encrypted snapshot before
    deciding whether another refresh is necessary.
    """

    with _core._account_refresh_lock_for(account_id):
        settings = _core.load_settings()
        account = next(
            (item for item in settings.get("accounts", []) if str(item.get("id") or "") == account_id),
            None,
        )
        if not account:
            raise _core.ManagerError("账号不存在。")
        snapshot = _core._load_account_snapshot(account_id)
        files = _core._decode_snapshot_files(snapshot)
        snapshot_auth = files["auth.json"] or b""
        effective_auth = snapshot_auth
        cap_sid_bytes = files.get("cap_sid")
        merged_live = False
        if account.get("authMode") == "chatgpt" and (_core.CODEX_HOME / "auth.json").is_file():
            try:
                live_snapshot, live_identity = _core._read_live_snapshot()
                if _core._account_matches_identity(account, live_identity):
                    live_files = _core._decode_snapshot_files(live_snapshot)
                    live_auth = live_files["auth.json"] or b""
                    if _core._codex_oauth_auth_is_newer(live_auth, snapshot_auth):
                        effective_auth = _core._merge_codex_oauth_auth_bytes(
                            snapshot_auth,
                            newer_auth_bytes=live_auth,
                        )
                        cap_sid_bytes = live_files.get("cap_sid")
                        merged_live = effective_auth != snapshot_auth
            except (_core.ManagerError, OSError):
                pass

        if account.get("authMode") != "chatgpt" or account.get("sourceType") != "codex_auth":
            return _core._chatgpt_credentials_from_auth_bytes(effective_auth)
        if not _core._auth_bytes_support_codex(effective_auth):
            return _core._chatgpt_credentials_from_auth_bytes(effective_auth)
        auth = _core._codex_oauth_auth_document(effective_auth)
        refresh_token = str(auth["tokens"].get("refresh_token") or "").strip()
        if not refresh_token:
            return _core._chatgpt_credentials_from_auth_bytes(effective_auth)

        if force_refresh and rejected_access_token and str(auth["tokens"].get("access_token") or "") != rejected_access_token:
            # Another request (or Codex itself) already rotated the rejected
            # token while we waited for this account's refresh lock.
            force_refresh = False
        if not force_refresh and not _core._codex_oauth_refresh_due(effective_auth):
            if merged_live:
                identity = _core._identity_from_auth_bytes(effective_auth)
                if not _core._account_matches_identity(account, identity):
                    raise _core.ManagerError("当前 Codex 登录身份与账号快照不一致，未同步 OAuth 凭据。")
                _core._persist_account_oauth_auth(
                    account_id,
                    account,
                    effective_auth,
                    cap_sid_bytes,
                    replace_live_if_active=False,
                )
            return _core._chatgpt_credentials_from_auth_bytes(effective_auth)

        refreshed_tokens = _core._request_codex_oauth_refresh(refresh_token)
        refreshed_auth = _core._merge_codex_oauth_auth_bytes(
            effective_auth,
            refreshed_tokens=refreshed_tokens,
        )
        refreshed_identity = _core._identity_from_auth_bytes(refreshed_auth)
        if not _core._account_matches_identity(account, refreshed_identity):
            raise _core.ManagerError("OAuth 续期返回了其他账号的凭据，已拒绝写入。")
        _core._persist_account_oauth_auth(
            account_id,
            account,
            refreshed_auth,
            cap_sid_bytes,
            replace_live_if_active=True,
        )
        return _core._chatgpt_credentials_from_auth_bytes(refreshed_auth)



def _live_official_account_matches(account: dict) -> bool:
    """Return whether the saved auth.json identity matches ``account``.

    This file check is not proof of an App Server's effective authentication.
    Native fallback operations also verify account/read in the same process.
    """

    if not isinstance(account, dict) or account.get("authMode") != "chatgpt":
        return False
    try:
        _snapshot, identity = _core._read_live_snapshot()
    except (_core.ManagerError, OSError):
        return False
    return bool(identity and _core._account_matches_identity(account, identity))



def _active_codex_account_request(account: dict, method: str, params: dict) -> dict:
    if not _core._live_official_account_matches(account):
        raise _core.ManagerError("保存的账号不是当前 Codex 官方账号，未读取运行时数据。")
    expected_email = str(account.get("email") or "").strip().casefold()
    if not expected_email:
        raise _core.ManagerError("目标官方账号缺少可核对的身份，未读取运行时数据。")
    results = _core.codex_app_server_requests(
        [("account/read", {"refreshToken": False}), (method, params)], timeout=10,
        launch_plan={"officialAccountId": str(account.get("id") or "official")},
    )
    actual = results[0].get("account") if len(results) == 2 and isinstance(results[0], dict) else None
    if not isinstance(actual, dict) or str(actual.get("email") or "").strip().casefold() != expected_email:
        raise _core.ManagerError("Codex 原生探针的账号身份与目标不一致，已丢弃运行时数据。")
    if actual.get("type") not in {None, "chatgpt", "chatgptAuthTokens"}:
        raise _core.ManagerError("Codex 原生探针未使用目标 ChatGPT 登录身份。")
    actual_account_id = str(actual.get("accountId") or actual.get("account_id") or "")
    if actual_account_id and account.get("accountId") and actual_account_id != str(account["accountId"]):
        raise _core.ManagerError("Codex 原生探针的工作区与目标账号不一致，已丢弃运行时数据。")
    return results[1]



def _active_codex_rate_limits(account: dict) -> dict:
    return _core._active_codex_account_request(account, "account/rateLimits/read", {})



def _active_codex_model_catalog(account: dict, *, max_pages: int = 5) -> dict:
    """Read the live, account-scoped native catalog with bounded pagination."""

    if not _core._live_official_account_matches(account):
        raise _core.ManagerError("保存的账号不是当前 Codex 官方账号，未读取运行时模型目录。")
    # model/list and debug models both honor model_catalog_json. Reading our
    # own generated catalog here would turn an old selection into discovery.
    if _core.CONFIG_FILE.is_file():
        with _core.CONFIG_FILE.open("rb") as config_file:
            raw_config = config_file.read(_core.CODEX_CONFIG_MAX_BYTES + 1)
        if len(raw_config) > _core.CODEX_CONFIG_MAX_BYTES:
            raise _core.ManagerError("Codex 配置过大，未读取运行时目录。")
        config = _core.tomllib.loads(_core.decode_toml_bytes(raw_config))
        if (config.get("model_catalog_json") or config.get("model_provider", "openai") != "openai"
                or _core._official_route_has_overrides(config)):
            raise _core.ManagerError("当前运行时使用自定义模型目录，无法作为官方模型发现来源。")
    records: list[dict] = []
    cursor: Any = None
    seen_cursors: set[str] = set()
    for _ in range(max(1, min(int(max_pages), 10))):
        result = _core._active_codex_account_request(
            account,
            "model/list",
            {"cursor": cursor, "limit": 100, "includeHidden": False},
        )
        page = result.get("data") if isinstance(result, dict) else None
        if not isinstance(page, list):
            raise _core.ManagerError("Codex 运行时模型目录返回格式无效。")
        records.extend(item for item in page if isinstance(item, dict))
        cursor = result.get("nextCursor") or result.get("next_cursor")
        if not cursor:
            break
        cursor_key = str(cursor)
        if cursor_key in seen_cursors:
            raise _core.ManagerError("Codex 运行时模型目录返回了重复游标，已停止分页。")
        seen_cursors.add(cursor_key)
    if cursor:
        raise _core.ManagerError("Codex 模型目录超过分页上限，已保留原目录。")
    return {"data": records}



def _chatgpt_error_is_unauthorized(error: BaseException) -> bool:
    """Inspect HTTP status evidence, never guess from an error's prose."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for field in ("code", "status", "status_code"):
            status = getattr(current, field, None)
            if isinstance(status, (int, str)) and str(status).isdigit():
                return int(status) == 401
        current = current.__cause__ or current.__context__
    return False



def _probe_codex_account(
    account_id: str,
    parallel_operations: bool = False,
    *,
    force_metadata: bool = False,
    include_reset_details: bool = False,
    allow_reset_clear: bool = False,
) -> dict:
    settings = _core.load_settings()
    account = next((item for item in settings.get("accounts", []) if item.get("id") == account_id), None)
    if not account:
        raise _core.ManagerError("账号不存在。")
    credentials = _core._account_chatgpt_credentials(account_id)
    access_token = credentials["accessToken"]
    chatgpt_account_id = credentials["accountId"]
    observed_at = _core.now_iso()
    operations = {"usage": lambda: _core._fetch_chatgpt_json(_core.CHATGPT_USAGE_URL, access_token, chatgpt_account_id)}
    known_free = _core._is_free_plan(account.get("plan"), account.get("planLabel"))
    prior_usage = account.get("usage") if isinstance(account.get("usage"), dict) else {}
    known_plan = bool(account.get("plan") or account.get("planLabel") or prior_usage.get("plan"))
    prior_reset = prior_usage.get("resetCredits") if isinstance(prior_usage.get("resetCredits"), dict) else None
    try:
        prior_reset_count = max(0, int((prior_reset or {}).get("availableCount") or 0))
    except (TypeError, ValueError):
        prior_reset_count = 0
    models_checked_at = account.get("modelsLastCheckedAt") or account.get("modelsRefreshedAt")
    if force_metadata or _core._timestamp_is_stale(models_checked_at, _core.ACCOUNT_MODELS_TTL_SECONDS):
        operations["models"] = lambda: _core._fetch_chatgpt_json(
            _core.CHATGPT_MODELS_URL,
            access_token,
            chatgpt_account_id,
            {"client_version": _core._codex_client_version()},
        )
    next_reset_details_retry = _core._parsed_datetime((prior_reset or {}).get("detailsNextRetryAt"))
    reset_details_retry_allowed = (
        not next_reset_details_retry
        or next_reset_details_retry <= _core.datetime.now(_core.timezone.utc)
    )
    reset_details_due = bool(
        include_reset_details
        or (
            prior_reset_count > 0
            and not bool((prior_reset or {}).get("detailsAvailable"))
            and reset_details_retry_allowed
            and _core._timestamp_is_stale(
                (prior_reset or {}).get("detailsCheckedAt"),
                _core.ACCOUNT_RESET_DETAILS_TTL_SECONDS,
            )
        )
    )
    if reset_details_due:
        operations["resetCredits"] = lambda: _core._fetch_chatgpt_json(
            _core.CHATGPT_RESET_CREDITS_URL,
            access_token,
            chatgpt_account_id,
        )
    capability_checked_at = (account.get("codexAccessProbe") or {}).get("checkedAt")
    if (
        account.get("sourceType") == "web_session"
        and not known_free
        and (
            force_metadata
            or account.get("codexCompatible") is None
            or _core._timestamp_is_stale(capability_checked_at, _core.ACCOUNT_CAPABILITY_TTL_SECONDS)
        )
    ):
        operations["codexAccess"] = lambda: _core._probe_chatgpt_codex_access(
            access_token,
            chatgpt_account_id,
        )
    entitlement_expiry = credentials.get("subscriptionExpiresAt") or account.get("subscriptionExpiresAt")
    next_subscription_retry = _core._parsed_datetime(account.get("subscriptionNextRetryAt"))
    subscription_warning = " ".join(str((account.get(field) or {}).get("subscription") or "")
                                    for field in ("refreshWarnings", "refreshErrors"))
    subscription_retry_allowed = (
        not next_subscription_retry or next_subscription_retry <= _core.datetime.now(_core.timezone.utc)
        or (force_metadata and "429" not in subscription_warning)
    )
    if (
        known_plan
        and not known_free
        and (force_metadata or _core._subscription_missing_or_expired(entitlement_expiry))
        and subscription_retry_allowed
        and (
            force_metadata
            or _core._timestamp_is_stale(account.get("subscriptionLastCheckedAt"), _core.ACCOUNT_SUBSCRIPTION_TTL_SECONDS)
        )
    ):
        operations["subscription"] = lambda: _core._fetch_chatgpt_subscription_status(
            access_token,
            chatgpt_account_id,
        )
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    unauthorized_operations: set[str] = set()

    def record_error(name: str, exc: BaseException) -> None:
        message = _core._redact_sensitive_text(exc, limit=320)
        errors[name] = f"订阅有效期同步失败：{message}" if name == "subscription" else message
        if _core._chatgpt_error_is_unauthorized(exc):
            unauthorized_operations.add(name)

    if parallel_operations:
        with _core.ThreadPoolExecutor(max_workers=len(operations), thread_name_prefix="account-probe") as executor:
            pending = {executor.submit(operation): name for name, operation in operations.items()}
            for future in _core.as_completed(pending):
                name = pending[future]
                try:
                    results[name] = future.result()
                except Exception as exc:
                    record_error(name, exc)
    else:
        for name, operation in operations.items():
            try:
                results[name] = operation()
            except Exception as exc:
                record_error(name, exc)

    if (unauthorized_operations and account.get("authMode") == "chatgpt"
            and account.get("sourceType") == "codex_auth"):
        # All first-pass operations have settled. Rotate once per batch and
        # replay only rejected reads; successful endpoints and 403/429 stay put.
        try:
            credentials = _core._account_chatgpt_credentials(
                account_id, force_refresh=True, rejected_access_token=access_token,
            )
            access_token = credentials["accessToken"]
            chatgpt_account_id = credentials["accountId"]
        except Exception as exc:
            refresh_error = _core._redact_sensitive_text(exc, limit=220)
            for name in unauthorized_operations:
                errors[name] += f"；OAuth 续期失败：{refresh_error}"
        else:
            for name in operations:
                if name not in unauthorized_operations:
                    continue
                try:
                    results[name] = operations[name]()
                    errors.pop(name, None)
                except Exception as exc:
                    record_error(name, exc)

    # Prefer the direct account endpoints because they work for every saved
    # account.  When the target is the currently logged-in official identity,
    # App Server is a supported second source that survives private endpoint
    # changes and transient urllib/proxy differences.  Never pay this startup
    # cost on a healthy ordinary refresh.
    if "models" in operations:
        direct_models = _core._parse_official_model_catalog(results.get("models"))["models"]
        if direct_models:
            results["_modelsSource"] = "chatgpt_account"
        elif _core._live_official_account_matches(account):
            try:
                native_catalog = _core._active_codex_model_catalog(account)
                if _core._parse_official_model_catalog(native_catalog)["models"]:
                    results["models"] = native_catalog
                    results["_modelsSource"] = "codex_app_server"
                    errors.pop("models", None)
            except Exception:
                # The direct error (when present) is more useful and already
                # redacted.  App Server is a fallback, not a second failure the
                # user must decipher.
                pass

    observed_reset = _core._parse_reset_credits(results.get("usage"), results.get("resetCredits"))
    reset_fallback_needed = bool(
        "usage" not in results
        or observed_reset is None
        or (reset_details_due and not bool((observed_reset or {}).get("detailsAvailable")))
    )
    if reset_fallback_needed and _core._live_official_account_matches(account):
        try:
            native_rate_limits = _core._active_codex_rate_limits(account)
            native_usage = _core._parse_chatgpt_usage(native_rate_limits)
            results["rateLimitsAppServer"] = native_rate_limits
            if "usage" not in results and (
                native_usage.get("weekly") is not None
                or native_usage.get("resetCredits") is not None
            ):
                results["usage"] = native_rate_limits
                errors.pop("usage", None)
            merged_observation = _core._parse_reset_credits(
                results.get("usage"),
                results.get("resetCredits"),
                native_rate_limits,
            )
            if merged_observation is not None and (
                not reset_details_due or merged_observation.get("detailsAvailable")
            ):
                errors.pop("resetCredits", None)
        except Exception:
            pass

    reset_error = errors.pop("resetCredits", None)
    capability_error = errors.pop("codexAccess", None)
    subscription_error = errors.pop("subscription", None)
    prior_refresh_warnings = (
        account.get("refreshWarnings")
        if isinstance(account.get("refreshWarnings"), dict)
        else {}
    )
    refresh_warnings: dict[str, str] = {}
    for warning_name in ("resetCredits", "codexAccess", "subscription"):
        if warning_name not in operations and prior_refresh_warnings.get(warning_name):
            refresh_warnings[warning_name] = str(prior_refresh_warnings[warning_name])
    if reset_error:
        refresh_warnings["resetCredits"] = f"重置卡明细暂时不可用：{reset_error}"
    if capability_error:
        refresh_warnings["codexAccess"] = capability_error
    if subscription_error:
        refresh_warnings["subscription"] = subscription_error
    updates: dict[str, Any] = {
        "tokenExpiresAt": credentials.get("tokenExpiresAt"),
        "lastRefreshedAt": _core.now_iso(),
        "refreshErrors": errors,
        "refreshWarnings": refresh_warnings,
        "refreshState": "ready",
    }
    if "codexAccess" in operations:
        capability = results.get("codexAccess") if isinstance(results.get("codexAccess"), dict) else {}
        compatible = capability.get("compatible")
        if known_free:
            compatible = False
            capability = {
                "compatible": False,
                "status": None,
                "checkedAt": _core.now_iso(),
                "method": "free_plan_policy",
            }
        updates["codexAccessProbe"] = capability or {
            "compatible": None,
            "status": None,
            "checkedAt": _core.now_iso(),
            "method": "unavailable",
            "error": capability_error,
        }
        if compatible is True:
            updates.update(
                {
                    "codexCompatible": True,
                    "quotaOnly": False,
                    "credentialCapability": "codex_short_lived",
                }
            )
        elif compatible is False:
            updates.update(
                {
                    "codexCompatible": False,
                    "quotaOnly": True,
                    "credentialCapability": "quota_only",
                }
            )
    for field in (
        "subscriptionStartedAt",
        "subscriptionExpiresAt",
        "subscriptionLastCheckedAt",
        "subscriptionMetadataSource",
    ):
        if credentials.get(field):
            updates[field] = credentials[field]
    credential_expiry = credentials.get("subscriptionExpiresAt") or account.get("subscriptionExpiresAt")
    updates["subscriptionStatus"] = (
        str(account.get("subscriptionStatus") or "unknown")
        if not credential_expiry
        else "expired"
        if _core._subscription_missing_or_expired(credential_expiry)
        else "active"
    )
    if "usage" in results:
        usage = _core._parse_chatgpt_usage(
            results["usage"],
            results.get("resetCredits"),
            results.get("rateLimitsAppServer"),
            observed_at=observed_at,
        )
        observed_weekly = usage.get("weekly")
        usage["weekly"] = _core._merge_quota_window_snapshot(
            prior_usage.get("weekly"),
            observed_weekly,
        )
        if observed_weekly is None:
            errors["usageQuota"] = (
                "官方额度响应暂未包含周额度，已保留上次有效数据。"
                if usage.get("weekly")
                else "官方额度响应暂未包含可识别的 Codex 周额度。"
            )
            updates["refreshErrors"] = errors
            updates["refreshState"] = "partial"
        for field in ("subscriptionExpiresAt",):
            if not usage.get(field) and prior_usage.get(field):
                usage[field] = prior_usage[field]
        usage["resetCredits"] = _core._merge_reset_credit_snapshot(
            prior_reset,
            usage.get("resetCredits"),
            allow_clear=allow_reset_clear,
        )
        if "resetCredits" in results and isinstance(usage.get("resetCredits"), dict):
            usage["resetCredits"]["detailsCheckedAt"] = _core.now_iso()
        if isinstance(usage.get("resetCredits"), dict):
            native_reset = _core._parse_reset_credits(results.get("rateLimitsAppServer"))
            direct_reset = _core._parse_reset_credits(results.get("resetCredits"))
            reset_snapshot = usage["resetCredits"]
            reset_snapshot["detailsSource"] = (
                "codex_app_server"
                if native_reset and native_reset.get("detailsAvailable")
                else "chatgpt_reset_credits"
                if direct_reset and direct_reset.get("detailsAvailable")
                else str(reset_snapshot.get("detailsSource") or "chatgpt_usage")
            )
            if reset_error:
                detail_delay = (
                    _core.ACCOUNT_RATE_LIMIT_RETRY_SECONDS
                    if "429" in reset_error
                    else _core.ACCOUNT_REFRESH_ERROR_RETRY_SECONDS
                )
                reset_snapshot["detailsNextRetryAt"] = (
                    _core.datetime.now(_core.timezone.utc) + _core.timedelta(seconds=detail_delay)
                ).astimezone().isoformat(timespec="seconds")
            elif "resetCredits" in operations:
                reset_snapshot["detailsNextRetryAt"] = None
            elif (prior_reset or {}).get("detailsNextRetryAt"):
                # A fresh usage count does not contain detail-retry metadata.
                # Keep the independent retry gate until the detail endpoint is
                # actually retried, rather than falling back to global backoff.
                reset_snapshot["detailsNextRetryAt"] = prior_reset["detailsNextRetryAt"]
        refreshed_free = _core._is_free_plan(usage.get("plan"), usage.get("planLabel"))
        if refreshed_free:
            subscription_error = None
            refresh_warnings.pop("subscription", None)
        updates["usage"] = usage
        if usage.get("plan"):
            updates["plan"] = usage["plan"]
        if usage.get("planLabel"):
            updates["planLabel"] = usage["planLabel"]
        if usage.get("subscriptionExpiresAt"):
            updates["subscriptionExpiresAt"] = usage["subscriptionExpiresAt"]
            updates["subscriptionMetadataSource"] = "usage"
            updates["subscriptionStatus"] = (
                "expired"
                if _core._subscription_missing_or_expired(usage["subscriptionExpiresAt"])
                else "active"
            )
            usage["subscriptionMetadataSource"] = "usage"
    else:
        refreshed_free = known_free
    if "subscription" in results:
        subscription = results["subscription"]
        updates["subscriptionExpiresAt"] = subscription.get("subscriptionExpiresAt")
        updates["subscriptionLastCheckedAt"] = subscription.get("subscriptionLastCheckedAt") or _core.now_iso()
        updates["subscriptionMetadataSource"] = "entitlement"
        updates["subscriptionStatus"] = subscription.get("subscriptionStatus") or "unknown"
        updates["subscriptionNextRetryAt"] = None
        usage = updates.get("usage")
        if isinstance(usage, dict):
            usage["subscriptionExpiresAt"] = subscription.get("subscriptionExpiresAt")
            usage["subscriptionMetadataSource"] = "entitlement"
            usage["subscriptionStatus"] = updates["subscriptionStatus"]
    if "usage" in results or "subscription" in results:
        # Completion order is not freshness: every response in this probe uses
        # one observation time. Preserve provenance when carrying cached tiers.
        candidates = [_core._plan_snapshot_metadata(account), _core._plan_snapshot_metadata(prior_usage)]
        if "usage" in results:
            current_usage = _core._parse_chatgpt_usage(results["usage"], observed_at=observed_at)
            candidates.append(_core._plan_snapshot_metadata(current_usage, source="usage", observed_at=observed_at))
        if "subscription" in results:
            candidates.append(_core._plan_snapshot_metadata(
                results["subscription"], source="entitlement", observed_at=observed_at,
            ))
        selected_plan = _core.select_plan_metadata(*candidates)
        if selected_plan:
            updates.update(selected_plan)
            if not isinstance(updates.get("usage"), dict) and prior_usage:
                updates["usage"] = _core.json.loads(_core.json.dumps(prior_usage))
            if isinstance(updates.get("usage"), dict):
                updates["usage"].update(selected_plan)
            refreshed_free = _core._is_free_plan(selected_plan.get("planLabel"))
    if refreshed_free:
        _core._clear_subscription_metadata(updates)
        if account.get("sourceType") == "web_session":
            updates.update(
                {
                    "codexCompatible": False,
                    "quotaOnly": True,
                    "credentialCapability": "quota_only",
                    "codexAccessProbe": {
                        "compatible": False,
                        "status": None,
                        "checkedAt": _core.now_iso(),
                        "method": "free_plan_policy",
                    },
                }
            )
    if "models" in results:
        updates["modelsLastCheckedAt"] = _core.now_iso()
        catalog = _core._parse_official_model_catalog(results["models"])
        models = catalog["models"]
        if models:
            updates.update(catalog)
            updates["modelsRefreshedAt"] = _core.now_iso()
            updates["modelsSource"] = str(results.get("_modelsSource") or "chatgpt_account")
        else:
            errors["models"] = "模型接口未返回可用模型，已保留原目录。"
            updates["refreshErrors"] = errors
            updates["refreshState"] = "partial" if "usage" in results else "error"
    if subscription_error:
        delay = (
            _core.ACCOUNT_RATE_LIMIT_RETRY_SECONDS
            if "429" in subscription_error
            else _core.ACCOUNT_REFRESH_ERROR_RETRY_SECONDS
            if _core._is_transient_chatgpt_error_text(subscription_error)
            else _core.ACCOUNT_SUBSCRIPTION_TTL_SECONDS
        )
        updates["subscriptionNextRetryAt"] = (
            _core.datetime.now(_core.timezone.utc) + _core.timedelta(seconds=delay)
        ).astimezone().isoformat(timespec="seconds")
    core_successes = set()
    if "usage" in results:
        core_successes.add("usage")
    if isinstance(updates.get("models"), list) and updates["models"]:
        core_successes.add("models")
    updates["refreshErrors"] = errors
    updates["refreshWarnings"] = refresh_warnings
    updates["refreshState"] = (
        "ready" if not errors else "partial" if core_successes else "error"
    )
    all_error_text = " ".join(str(value) for value in errors.values())
    if "429" in all_error_text:
        updates["nextRefreshAt"] = (
            _core.datetime.now(_core.timezone.utc) + _core.timedelta(seconds=_core.ACCOUNT_RATE_LIMIT_RETRY_SECONDS)
        ).astimezone().isoformat(timespec="seconds")
    elif _core._is_transient_chatgpt_error_text(all_error_text) or updates["refreshState"] == "error":
        updates["nextRefreshAt"] = (
            _core.datetime.now(_core.timezone.utc) + _core.timedelta(seconds=_core.ACCOUNT_REFRESH_ERROR_RETRY_SECONDS)
        ).astimezone().isoformat(timespec="seconds")
    else:
        updates["nextRefreshAt"] = None
    return updates



def _sync_source_model_selections(
    settings: dict,
    source_id: str,
    previous_models: _core.Any,
    observed_models: _core.Any,
) -> list[str]:
    """Prune retired entries and follow additions for a fully selected source.

    ``selectAll`` is global, while independent mode presents one source at a
    time.  Consequently the UI can persist an explicit list even after the
    user clicked "select all" for the active account.  Preserve that intent
    when OpenAI or a relay adds a model, without changing curated subsets.
    A non-empty successful catalog is authoritative, so references to models
    that disappeared from that same source are removed atomically as well.
    """

    workspace = settings.setdefault("modelWorkspace", _core._default_model_workspace())
    previous_ids = {
        str(item).strip()
        for item in previous_models
        if str(item).strip()
    } if isinstance(previous_models, list) else set()
    observed_ids = [
        str(item).strip()
        for item in observed_models
        if str(item).strip()
    ] if isinstance(observed_models, list) else []
    if not observed_ids:
        return []
    source_prefix = f"{source_id}::"
    observed_keys = {f"{source_id}::{model_id}" for model_id in observed_ids}
    selected_before = [
        str(item) for item in workspace.get("selectedModels", []) if str(item).strip()
    ]
    selected_before_set = set(selected_before)
    selected = [
        key
        for key in selected_before
        if not key.startswith(source_prefix) or key in observed_keys
    ]
    selected_set = set(selected)
    if (
        str(workspace.get("defaultModelKey") or "").startswith(source_prefix)
        and str(workspace.get("defaultModelKey") or "") not in observed_keys
    ):
        workspace["defaultModelKey"] = ""
    for route in settings.get("subagentRouting", {}).get("routes", {}).values():
        if not isinstance(route, dict):
            continue
        models = route.get("models", []) if isinstance(route.get("models"), list) else []
        efforts = route.get("efforts", []) if isinstance(route.get("efforts"), list) else []
        kept = [
            (str(model), str(efforts[index] or "") if index < len(efforts) else "")
            for index, model in enumerate(models)
            if not str(model).startswith(source_prefix) or str(model) in observed_keys
        ]
        route["models"] = [model for model, _effort in kept]
        route["efforts"] = [effort for _model, effort in kept]
    if bool(workspace.get("selectAll", True)):
        workspace["selectedModels"] = selected
        return []
    previous_keys = {f"{source_id}::{model_id}" for model_id in previous_ids}
    if not previous_keys or not previous_keys.issubset(selected_before_set):
        workspace["selectedModels"] = selected
        return []
    added = []
    for model_id in observed_ids:
        key = f"{source_id}::{model_id}"
        if key not in selected_set:
            selected.append(key)
            selected_set.add(key)
            added.append(key)
    workspace["selectedModels"] = selected
    return added



def _merge_account_updates(settings: dict, account_id: str, updates: dict) -> dict:
    account = next((item for item in settings.get("accounts", []) if item.get("id") == account_id), None)
    if not account:
        raise _core.ManagerError("账号不存在。")
    if isinstance(updates.get("models"), list):
        _core._sync_source_model_selections(
            settings,
            f"account:{account_id}",
            account.get("models", []),
            updates["models"],
        )
    account.update(updates)
    web2api = settings.setdefault("web2api", _core._default_web2api_settings())
    member_ids = list(web2api.get("accountIds", []))
    if _core._account_codex_compatible(account) and account.get("proxyRequested"):
        account["proxyEnabled"] = True
        if account_id not in member_ids:
            member_ids.append(account_id)
    elif not _core._account_codex_compatible(account):
        account["proxyEnabled"] = False
        member_ids = [item for item in member_ids if item != account_id]
    web2api["accountIds"] = member_ids
    account["updatedAt"] = _core.now_iso()
    return account



def _perform_account_refresh(
    account_id: str,
    *,
    parallel_operations: bool = False,
    force_metadata: bool = False,
    include_reset_details: bool = False,
    allow_reset_clear: bool = False,
    commit_guard: _core.Callable[[], bool] | None = None,
) -> dict:
    lock = _core._account_refresh_lock_for(account_id)
    requested_at = _core.datetime.now(_core.timezone.utc)
    with lock:
        if commit_guard is not None and not commit_guard():
            return {"cancelled": True, "state": "cancelled", "account": None}
        settings = _core.load_settings()
        account = next((item for item in settings.get("accounts", []) if item.get("id") == account_id), None)
        if not account:
            raise _core.ManagerError("账号不存在。")
        if account.get("authMode") != "chatgpt":
            raise _core.ManagerError("API Key 账号不支持 ChatGPT 订阅额度探测。")
        next_refresh = _core._parsed_datetime(account.get("nextRefreshAt"))
        rate_limited = any("429" in str(error) for error in (account.get("refreshErrors") or {}).values())
        if (next_refresh and next_refresh > _core.datetime.now(_core.timezone.utc) and not allow_reset_clear
                and (not force_metadata or rate_limited)):
            state = str(account.get("refreshState") or "error")
            return {"cancelled": False, "state": state, "account": account, "coalesced": True, "skipReason": "backoff"}
        completed_at = _core._parsed_datetime(account.get("lastRefreshedAt"))
        models_checked_at = _core._parsed_datetime(account.get("modelsLastCheckedAt"))
        if (
            completed_at
            and completed_at >= requested_at - _core.timedelta(seconds=1)
            and not include_reset_details
            and not allow_reset_clear
            and (not force_metadata or (models_checked_at and models_checked_at >= requested_at - _core.timedelta(seconds=1)))
        ):
            state = str(account.get("refreshState") or "ready")
            return {"cancelled": False, "state": state, "account": account, "coalesced": True}
        try:
            if force_metadata:
                _core.invalidate_codex_version_cache()
            updates = _core._probe_codex_account(
                account_id,
                parallel_operations,
                force_metadata=force_metadata,
                include_reset_details=include_reset_details,
                allow_reset_clear=allow_reset_clear,
            )
        except Exception as exc:
            message = _core._redact_sensitive_text(exc, limit=320)
            delay = _core.ACCOUNT_RATE_LIMIT_RETRY_SECONDS if "429" in message else _core.ACCOUNT_REFRESH_ERROR_RETRY_SECONDS
            updates = {
                "lastRefreshedAt": _core.now_iso(),
                "nextRefreshAt": (
                    _core.datetime.now(_core.timezone.utc) + _core.timedelta(seconds=delay)
                ).astimezone().isoformat(timespec="seconds"),
                "refreshState": "error",
                "refreshErrors": {"account": message},
            }
        if commit_guard is not None and not commit_guard():
            return {"cancelled": True, "state": "cancelled", "account": None}
        with _core.SETTINGS_LOCK:
            if commit_guard is not None and not commit_guard():
                return {"cancelled": True, "state": "cancelled", "account": None}
            latest = _core.load_settings()
            account = _core._merge_account_updates(latest, account_id, updates)
            _core.save_settings(latest)
        return {"cancelled": False, "state": str(updates.get("refreshState") or "error"), "account": account}



def refresh_codex_account(
    account_id: str,
    *,
    force_metadata: bool = False,
    include_reset_details: bool = False,
    allow_reset_clear: bool = False,
) -> dict:
    result = _core._perform_account_refresh(
        account_id,
        force_metadata=force_metadata,
        include_reset_details=include_reset_details,
        allow_reset_clear=allow_reset_clear,
    )
    account = result["account"]
    return {**account, "refreshSkipped": result["skipReason"]} if result.get("skipReason") else account



def refresh_account_reset_credit_details(account_id: str, *, force: bool = False) -> dict:
    """Fetch reset-card rows on demand without refreshing models or subscription."""

    with _core._account_refresh_lock_for(account_id):
        settings = _core.load_settings()
        account = next((item for item in settings.get("accounts", []) if item.get("id") == account_id), None)
        if not account:
            raise _core.ManagerError("账号不存在。")
        if account.get("authMode") != "chatgpt":
            raise _core.ManagerError("该账号不支持官方重置卡。")
        usage = account.get("usage") if isinstance(account.get("usage"), dict) else {}
        previous = usage.get("resetCredits") if isinstance(usage.get("resetCredits"), dict) else None
        next_retry = _core._parsed_datetime((previous or {}).get("detailsNextRetryAt"))
        reset_warning = " ".join(str((account.get(field) or {}).get("resetCredits") or "")
                                 for field in ("refreshWarnings", "refreshErrors"))
        fresh = not _core._timestamp_is_stale(
            (previous or {}).get("detailsCheckedAt"),
            _core.ACCOUNT_RESET_DETAILS_TTL_SECONDS,
        )
        in_backoff = bool(next_retry and next_retry > _core.datetime.now(_core.timezone.utc))
        if (not force and (fresh or in_backoff)) or (in_backoff and "429" in reset_warning):
            return account
        credentials = _core._account_chatgpt_credentials(account_id)
        detail_errors: list[str] = []
        direct_payload: dict | None = None
        native_payload: dict | None = None
        try:
            direct_payload = _core._fetch_chatgpt_json(
                _core.CHATGPT_RESET_CREDITS_URL,
                credentials["accessToken"],
                credentials["accountId"],
            )
        except Exception as exc:
            if _core._chatgpt_error_is_unauthorized(exc) and account.get("sourceType") == "codex_auth":
                try:
                    credentials = _core._account_chatgpt_credentials(
                        account_id, force_refresh=True,
                        rejected_access_token=credentials["accessToken"],
                    )
                    direct_payload = _core._fetch_chatgpt_json(
                        _core.CHATGPT_RESET_CREDITS_URL,
                        credentials["accessToken"], credentials["accountId"],
                    )
                except Exception as retry_exc:
                    detail_errors.append(_core._redact_sensitive_text(exc, limit=120)
                                         + "；" + _core._redact_sensitive_text(retry_exc, limit=220))
            else:
                detail_errors.append(_core._redact_sensitive_text(exc, limit=240))

        observed = _core._parse_reset_credits(direct_payload)
        if (
            (observed is None or not observed.get("detailsAvailable"))
            and _core._live_official_account_matches(account)
        ):
            try:
                native_payload = _core._active_codex_rate_limits(account)
                observed = _core._parse_reset_credits(direct_payload, native_payload)
            except Exception as exc:
                detail_errors.append(_core._redact_sensitive_text(exc, limit=240))

        detail_error = None
        if observed is None:
            detail_error = "；".join(dict.fromkeys(item for item in detail_errors if item)) or "官方接口未返回重置卡状态。"
            merged = _core._merge_reset_credit_snapshot(previous, None)
        else:
            merged = _core._merge_reset_credit_snapshot(previous, observed)
            try:
                available = max(0, int(observed.get("availableCount") or 0))
            except (TypeError, ValueError):
                available = 0
            if available > 0 and not observed.get("detailsAvailable"):
                detail_error = (
                    "；".join(dict.fromkeys(item for item in detail_errors if item))
                    or "官方接口已返回重置卡数量，但本次未提供卡片明细。"
                )
        if isinstance(merged, dict):
            merged["detailsCheckedAt"] = _core.now_iso()
            merged["detailsSource"] = (
                "codex_app_server"
                if native_payload is not None
                else "chatgpt_reset_credits"
                if direct_payload is not None
                else str(merged.get("detailsSource") or "cached")
            )
            if detail_error:
                delay = _core.ACCOUNT_RATE_LIMIT_RETRY_SECONDS if "429" in detail_error else _core.ACCOUNT_REFRESH_ERROR_RETRY_SECONDS
                merged["detailsNextRetryAt"] = (
                    _core.datetime.now(_core.timezone.utc) + _core.timedelta(seconds=delay)
                ).astimezone().isoformat(timespec="seconds")
            else:
                merged["detailsNextRetryAt"] = None
        with _core.SETTINGS_LOCK:
            latest = _core.load_settings()
            live = next((item for item in latest.get("accounts", []) if item.get("id") == account_id), None)
            if not live:
                raise _core.ManagerError("账号已被删除，未写入重置卡详情。")
            live_usage = live.setdefault("usage", {})
            live_usage["resetCredits"] = merged
            refresh_errors = live.setdefault("refreshErrors", {})
            refresh_warnings = live.setdefault("refreshWarnings", {})
            legacy_reset_was_sole_partial_error = bool(
                live.get("refreshState") == "partial"
                and set(refresh_errors) == {"resetCredits"}
            )
            refresh_errors.pop("resetCredits", None)
            if detail_error:
                refresh_warnings["resetCredits"] = f"重置卡明细暂时不可用：{detail_error}"
            else:
                refresh_warnings.pop("resetCredits", None)
            if not refresh_errors:
                live["refreshErrors"] = {}
            if not refresh_warnings:
                live["refreshWarnings"] = {}
            if legacy_reset_was_sole_partial_error:
                live["refreshState"] = "ready"
                live["nextRefreshAt"] = None
            live["updatedAt"] = _core.now_iso()
            _core.save_settings(latest)
            return live



def consume_account_reset_credit(account_id: str) -> dict:
    with _core._account_refresh_lock_for(account_id):
        settings = _core.load_settings()
        account = next((item for item in settings.get("accounts", []) if item.get("id") == account_id), None)
        if not account:
            raise _core.ManagerError("账号不存在。")
        usage = account.get("usage") if isinstance(account.get("usage"), dict) else {}
        reset_credits = usage.get("resetCredits") if isinstance(usage.get("resetCredits"), dict) else {}
        try:
            available = max(0, int(reset_credits.get("availableCount") or 0))
        except (TypeError, ValueError):
            available = 0
        if available <= 0:
            raise _core.ManagerError("该账号当前没有可用的官方重置卡。")

        # Persist the idempotency key before the network call.  If the TLS
        # connection drops after the server handled the request, the next click
        # reuses the same key instead of consuming a second card.  A subsequent
        # authoritative quota response naturally replaces this pending marker.
        pending = reset_credits.get("pendingRedeem") if isinstance(reset_credits.get("pendingRedeem"), dict) else {}
        redeem_request_id = str(pending.get("requestId") or "").strip() or str(_core.uuid.uuid4())
        if not pending.get("requestId"):
            account.setdefault("usage", {}).setdefault("resetCredits", {}).update(
                {
                    "pendingRedeem": {
                        "requestId": redeem_request_id,
                        "startedAt": _core.now_iso(),
                        "previousCount": available,
                    }
                }
            )
            _core.save_settings(settings)

        credentials = _core._account_chatgpt_credentials(account_id)
        result = _core._post_chatgpt_json(
            _core.CHATGPT_RESET_CONSUME_URL,
            credentials["accessToken"],
            credentials["accountId"],
            {"redeem_request_id": redeem_request_id},
        )
        code = str(result.get("code") or result.get("outcome") or "").strip()
        normalized = {
            "reset": "reset",
            "already_redeemed": "alreadyRedeemed",
            "alreadyRedeemed": "alreadyRedeemed",
            "nothing_to_reset": "nothingToReset",
            "nothingToReset": "nothingToReset",
            "no_credit": "noCredit",
            "noCredit": "noCredit",
        }.get(code, code)

        def finalize_local(*, consumed: bool, clear_all: bool = False) -> None:
            with _core.SETTINGS_LOCK:
                latest = _core.load_settings()
                live = next((item for item in latest.get("accounts", []) if item.get("id") == account_id), None)
                if not live:
                    return
                live_usage = live.setdefault("usage", {})
                live_reset = live_usage.setdefault("resetCredits", {})
                live_pending = live_reset.get("pendingRedeem")
                if isinstance(live_pending, dict) and str(live_pending.get("requestId") or "") == redeem_request_id:
                    live_reset.pop("pendingRedeem", None)
                if clear_all:
                    live_reset.update({"availableCount": 0, "credits": [], "detailsAvailable": True})
                elif consumed:
                    try:
                        current_count = max(0, int(live_reset.get("availableCount") or available))
                    except (TypeError, ValueError):
                        current_count = available
                    live_reset["availableCount"] = max(0, current_count - 1)
                    credits = live_reset.get("credits")
                    if isinstance(credits, list) and credits:
                        live_reset["credits"] = credits[1:]
                    live_reset["checkedAt"] = _core.now_iso()
                live["updatedAt"] = _core.now_iso()
                _core.save_settings(latest)

        if normalized == "nothingToReset":
            finalize_local(consumed=False)
            raise _core.ManagerError("当前额度窗口尚未达到可重置状态，重置卡没有被消耗。")
        if normalized == "noCredit":
            finalize_local(consumed=False, clear_all=True)
            raise _core.ManagerError("官方返回该账号已没有可用重置卡，请刷新后重试。")
        if normalized not in {"reset", "alreadyRedeemed"}:
            # Keep pendingRedeem for an unknown-but-completed response.  A retry
            # will use the same idempotency key and cannot spend another card.
            raise _core.ManagerError("官方返回了无法识别的重置结果，未在本地修改额度状态。")

        finalize_local(consumed=True)
        refreshed = _core.refresh_codex_account(account_id, force_metadata=False, allow_reset_clear=True)
        return {
            "outcome": normalized,
            "windowsReset": result.get("windows_reset", result.get("windowsReset")),
            "redeemRequestId": redeem_request_id,
            "account": refreshed,
        }



def _refresh_is_stale(account: dict, stale_seconds: int) -> bool:
    threshold = max(1, int(stale_seconds))
    if account.get("refreshState") == "error":
        threshold = max(threshold, _core.ACCOUNT_REFRESH_ERROR_RETRY_SECONDS)
    next_refresh = _core._parsed_datetime(account.get("nextRefreshAt"))
    if next_refresh and next_refresh > _core.datetime.now(_core.timezone.utc):
        return False
    value = account.get("lastRefreshedAt")
    if not value:
        return True
    try:
        refreshed = _core.datetime.fromisoformat(str(value))
        if refreshed.tzinfo is None:
            refreshed = refreshed.replace(tzinfo=_core.timezone.utc)
        return (_core.datetime.now(_core.timezone.utc) - refreshed.astimezone(_core.timezone.utc)).total_seconds() >= threshold
    except (TypeError, ValueError):
        return True



def stale_codex_account_ids(stale_seconds: int = _core.ACCOUNT_REFRESH_STALE_SECONDS) -> list[str]:
    settings = _core.load_settings()
    return [
        str(item.get("id"))
        for item in settings.get("accounts", [])
        if item.get("id")
        and item.get("authMode") == "chatgpt"
        and _core._refresh_is_stale(item, stale_seconds)
    ]



def refresh_codex_accounts(
    account_ids: list[str],
    *,
    max_workers: int = 2,
    parallel_operations: bool = False,
    force_metadata: bool = False,
    include_reset_details: bool = False,
    commit_guard: _core.Callable[[], bool] | None = None,
) -> dict:
    settings = _core.load_settings()
    requested = list(dict.fromkeys(str(item) for item in account_ids if str(item).strip()))
    valid_ids = {
        str(item.get("id"))
        for item in settings.get("accounts", [])
        if item.get("id") and item.get("authMode") == "chatgpt"
    }
    requested = [account_id for account_id in requested if account_id in valid_ids]
    if not requested:
        return {"refreshed": 0, "ready": 0, "partial": 0, "error": 0}
    outcomes: dict[str, dict] = {}
    worker_count = max(1, min(2, int(max_workers), len(requested)))
    def invoke(account_id: str) -> dict:
        return _core._perform_account_refresh(
            account_id,
            parallel_operations=parallel_operations,
            force_metadata=force_metadata,
            include_reset_details=include_reset_details,
            commit_guard=commit_guard,
        )
    if worker_count == 1:
        for account_id in requested:
            try:
                outcomes[account_id] = invoke(account_id)
            except Exception as exc:
                outcomes[account_id] = {
                    "cancelled": False,
                    "state": "error",
                    "error": _core._redact_sensitive_text(exc, limit=320),
                }
    else:
        with _core.ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="accounts-refresh") as executor:
            pending = {executor.submit(invoke, account_id): account_id for account_id in requested}
            for future in _core.as_completed(pending):
                account_id = pending[future]
                try:
                    outcomes[account_id] = future.result()
                except Exception as exc:
                    outcomes[account_id] = {
                        "cancelled": False,
                        "state": "error",
                        "error": _core._redact_sensitive_text(exc, limit=320),
                    }
    cancelled = sum(1 for outcome in outcomes.values() if outcome.get("cancelled"))
    states = [outcome.get("state") for outcome in outcomes.values() if not outcome.get("cancelled")]
    return {
        "refreshed": len(states),
        "ready": states.count("ready"),
        "partial": states.count("partial"),
        "error": states.count("error"),
        "skipped": sum(1 for outcome in outcomes.values() if outcome.get("coalesced")),
        **({"cancelled": True} if cancelled else {}),
    }



def refresh_all_codex_accounts(stale_only: bool = False) -> dict:
    if stale_only:
        account_ids = _core.stale_codex_account_ids()
    else:
        settings = _core.load_settings()
        account_ids = [
            str(item.get("id"))
            for item in settings.get("accounts", [])
            if item.get("id") and item.get("authMode") == "chatgpt"
        ]
    return _core.refresh_codex_accounts(account_ids, force_metadata=not stale_only)



def _remove_model_source_references(settings: dict, source_ids: set[str]) -> int:
    """Remove model selections and their paired efforts without leaving stale slots."""
    prefixes = tuple(f"{source_id}::" for source_id in source_ids if source_id)
    if not prefixes:
        return 0
    removed = 0
    workspace = settings.setdefault("modelWorkspace", _core._default_model_workspace())
    if str(workspace.get("activeSourceId") or "") in source_ids:
        workspace["activeSourceId"] = ""
    if str(workspace.get("defaultModelKey") or "").startswith(prefixes):
        workspace["defaultModelKey"] = ""
    selected_models = [str(item) for item in workspace.get("selectedModels", []) if str(item).strip()]
    kept_selected = [item for item in selected_models if not item.startswith(prefixes)]
    removed += len(selected_models) - len(kept_selected)
    workspace["selectedModels"] = kept_selected

    routing = settings.setdefault("subagentRouting", _core._default_subagent_routing())
    for route in routing.get("routes", {}).values():
        if not isinstance(route, dict):
            continue
        models = route.get("models", []) if isinstance(route.get("models"), list) else []
        efforts = route.get("efforts", []) if isinstance(route.get("efforts"), list) else []
        candidates = [
            (str(model), str(efforts[index] or "") if index < len(efforts) else "")
            for index, model in enumerate(models)
            if str(model).strip() and not str(model).startswith(prefixes)
        ]
        removed += len(models) - len(candidates)
        route["models"] = [model for model, _effort in candidates]
        route["efforts"] = [effort for _model, effort in candidates]
    return removed

