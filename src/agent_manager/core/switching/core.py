"""Core account switching functions"""
from __future__ import annotations
from agent_manager import core as _core


def _prepare_account_switch_target(settings: dict, account_id: str) -> tuple[dict, dict[str, bytes | None]]:
    account = next((item for item in settings.get("accounts", []) if item.get("id") == account_id), None)
    if not account:
        raise _core.ManagerError("账号不存在。")
    if account.get("sourceType") == "web_session":
        if not _core._account_codex_compatible(account):
            raise _core.ManagerError(
                "该账号是浏览器 Web Session，只能查询额度和模型目录；"
                "它没有通过 Codex 推理授权检测。"
            )
        raise _core.ManagerError(
            "该 Web Session 需要通过管理器的本地转换反代启动，不能把合成认证文件直接写入 Codex。"
        )
    if not _core._account_codex_compatible(account):
        raise _core.ManagerError(
            "该账号是浏览器 Web Session，只能查询额度和模型目录；"
            "它不包含 Codex OAuth 凭据，无法切换后用于对话。"
        )
    target_files = dict(_core._decode_snapshot_files(_core._load_account_snapshot(account_id)))
    target_files["auth.json"] = _core._codex_auth_projection_bytes(target_files["auth.json"] or b"")
    live_cap_sid_path = _core.CODEX_HOME / "cap_sid"
    if target_files.get("cap_sid") is None and live_cap_sid_path.is_file():
        target_files["cap_sid"] = live_cap_sid_path.read_bytes()
    return account, target_files



def _account_model_source(settings: dict, account_id: str) -> dict:
    source_id = f"account:{account_id}"
    source = next((item for item in _core.model_sources(settings) if item.get("id") == source_id), None)
    if not source or not source.get("available") or not source.get("models"):
        raise _core.ManagerError("该官方账号没有可用模型，请先刷新账号后重试。")
    return source



def switch_codex_account(
    account_id: str,
    force: bool = False,
    *,
    credentials_preflighted: bool = False,
) -> dict:
    if _core._credential_store_mode() == "keyring":
        raise _core.ManagerError("当前 Codex 使用 keyring 凭据存储，auth.json 快照切换不可用。")
    processes = _core.running_codex_processes()
    if processes and not force:
        names = ", ".join(f"{item['name']} ({item['pid']})" for item in processes[:4])
        raise _core.ManagerError(f"检测到正在运行的 Codex 进程：{names}。关闭后重试，或明确确认强制切换。")
    preflight_settings = _core.load_settings()
    preflight_account = next(
        (item for item in preflight_settings.get("accounts", []) if item.get("id") == account_id),
        None,
    )
    if not preflight_account:
        raise _core.ManagerError("账号不存在。")
    if (
        not credentials_preflighted
        and preflight_account.get("authMode") == "chatgpt"
        and preflight_account.get("sourceType") == "codex_auth"
    ):
        _core._account_chatgpt_credentials(account_id)
    with _core._exclusive_switch_operation("account", account_id):
        if _core._credential_store_mode() == "keyring":
            raise _core.ManagerError("当前 Codex 使用 keyring 凭据存储，auth.json 快照切换不可用。")
        processes = _core._require_known_codex_processes()
        if processes and not force:
            names = ", ".join(f"{item['name']} ({item['pid']})" for item in processes[:4])
            raise _core.ManagerError(f"检测到正在运行的 Codex 进程：{names}。关闭后重试，或明确确认强制切换。")

        settings = _core.load_settings()
        account = next((item for item in settings.get("accounts", []) if item.get("id") == account_id), None)
        if not account:
            raise _core.ManagerError("账号不存在。")
        account, target_files = _core._prepare_account_switch_target(settings, account_id)
        target_source = _core._account_model_source(settings, account_id)
        snapshot = _core._capture_switch_transaction_snapshot(settings)
        backup_dir: Path | None = None
        try:
            try:
                live_snapshot, live_identity = _core._read_live_snapshot()
            except _core.ManagerError:
                live_snapshot, live_identity = None, None
            changed = not (
                live_identity
                and _core._account_matches_identity(account, live_identity)
                and _core._live_auth_files_match(target_files)
            )
            if changed and live_snapshot and live_identity:
                live_record = _core._find_account_for_identity(settings, live_identity)
                if live_record and live_record.get("id") != account_id:
                    _core._store_account_snapshot(live_record["id"], live_snapshot)
                    live_record["updatedAt"] = _core.now_iso()
            if changed:
                stamp = _core.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                backup_dir = _core.BACKUPS_DIR / f"auth-switch-{stamp}"
                backup_dir.mkdir(parents=True, exist_ok=False)
                for name in _core.AUTH_FILES:
                    content = snapshot["files"].get(_core.CODEX_HOME / name)
                    if content is not None:
                        _core.atomic_write_bytes(backup_dir / name, content)
                _core.atomic_write_json(
                    backup_dir / "manifest.json",
                    {"createdAt": _core.now_iso(), "targetAccountId": account_id, "files": list(_core.AUTH_FILES)},
                )
                _core._restore_auth_files(target_files)
                _core._runtime_overlay_record_applied(paths=[_core.CODEX_HOME / name for name in _core.AUTH_FILES])
                if not _core._live_auth_files_match(target_files):
                    _core.time.sleep(0.05)
                    if not _core._live_auth_files_match(target_files):
                        raise _core.ManagerError("认证文件在切换后被其他 Codex 进程改写。")
                _, switched_identity = _core._read_live_snapshot()
                if not _core._account_matches_identity(account, switched_identity):
                    raise _core.ManagerError("切换后的账号身份与目标快照不一致。")

            activation_at = _core.now_iso()
            account["lastUsedAt"] = activation_at
            account["updatedAt"] = activation_at
            workspace = _core._select_workspace_source(settings, target_source, independent=True)
            profile = _core._active_main(settings)
            profile["provider"] = "openai"
            selected_key = str(workspace.get("defaultModelKey") or "")
            selected_model = next(
                (item for item in target_source.get("models", []) if str(item.get("key") or "") == selected_key),
                target_source["models"][0],
            )
            profile["model"] = str(selected_model.get("id") or profile.get("model") or "")
            web2api = settings.setdefault("web2api", _core._default_web2api_settings())
            web2api["activeForCodex"] = False
            web2api["activeAccountId"] = None
            _core.save_settings(settings)
            try:
                _core.record_direct_account_activation(account_id, activation_at)
            except (_core.ManagerError, OSError):
                pass
            _core._reset_switch_caches()
            session_sync = _core.auto_sync_sessions_after_switch("openai")
            result = {
                "changed": changed,
                "account": account,
                "sessionSync": session_sync,
            }
            if backup_dir is not None:
                result["backupPath"] = str(backup_dir)
            if not changed:
                result["message"] = "该账号已经是当前账号。"
            return result
        except Exception as exc:
            rollback_errors = _core._restore_switch_transaction_snapshot(snapshot)
            detail = f"；回滚回验异常：{'；'.join(rollback_errors)}" if rollback_errors else ""
            if isinstance(exc, _core.ManagerError):
                raise _core.ManagerError(f"账号切换未保持稳定，已自动回滚并原样恢复：{exc}{detail}") from exc
            raise _core.ManagerError(f"账号切换失败，已自动回滚并原样恢复：{exc}{detail}") from exc