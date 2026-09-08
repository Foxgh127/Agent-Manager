"""Overlay services."""
from __future__ import annotations
from agent_manager import core as _core


def _broadcast_user_environment_change() -> None:
    if _core.os.name != "nt":
        return
    try:
        result = _core.wintypes.DWORD()
        _core.ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 3000, _core.ctypes.byref(result))
    except Exception:
        pass



def _read_user_environment(name: str) -> str | None:
    if _core.os.name != "nt":
        return _core.os.environ.get(name)
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_QUERY_VALUE) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except FileNotFoundError:
        return None



def _sync_user_environment(name: str, value: str) -> None:
    if str(name).upper() == "CODEX_CLI_PATH":
        raise _core.ManagerError(
            "安全策略禁止写入 CODEX_CLI_PATH；Codex Desktop 必须继续使用原生 codex.exe。"
        )
    if _core.os.name != "nt":
        raise _core.ManagerError("环境变量同步仅支持 Windows。")
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    _core.os.environ[name] = value
    _core._broadcast_user_environment_change()



def _remove_user_environment(name: str) -> None:
    if _core.os.name != "nt":
        _core.os.environ.pop(name, None)
        return
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        pass
    _core.os.environ.pop(name, None)
    _core._broadcast_user_environment_change()



def _overlay_value_hash(value: bytes | str | None) -> str:
    if value is None:
        return "missing"
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return _core.hashlib.sha256(raw).hexdigest()



def _overlay_encrypt_text(value: str | None) -> str | None:
    if value is None:
        return None
    return _core.base64.b64encode(_core.dpapi_protect(value)).decode("ascii")



def _overlay_decrypt_text(value: str | None) -> str | None:
    if value is None:
        return None
    return _core.dpapi_unprotect(_core.base64.b64decode(value, validate=True))



def _overlay_encrypt_bytes(value: bytes | None) -> str | None:
    if value is None:
        return None
    return _core._overlay_encrypt_text(_core.base64.b64encode(value).decode("ascii"))



def _overlay_decrypt_bytes(value: str | None) -> bytes | None:
    decoded = _core._overlay_decrypt_text(value)
    return None if decoded is None else _core.base64.b64decode(decoded, validate=True)



def _strip_manager_agents_block(content: bytes | None) -> bytes | None:
    if content is None:
        return None
    text = content.decode("utf-8")
    pattern = _core.re.compile(
        _core.re.escape(_core.MANAGED_BLOCK_START) + r".*?" + _core.re.escape(_core.MANAGED_BLOCK_END) + r"\s*",
        _core.re.DOTALL,
    )
    if not pattern.search(text):
        return content
    stripped = pattern.sub("", text).strip()
    return (stripped + "\n").encode("utf-8") if stripped else None



def _managed_agent_name(value: _core.Any) -> bool:
    return bool(_core.re.fullmatch(r"cam_(?:simple|normal|hard|expert)_[1-9][0-9]*", str(value or "")))



def _managed_agent_expected_path(value: _core.Any) -> _core.Path | None:
    match = _core.re.fullmatch(
        r"cam_(simple|normal|hard|expert)_([1-9][0-9]*)",
        str(value or ""),
    )
    if not match:
        return None
    return _core.AGENTS_DIR / f"cam-{match.group(1)}-{match.group(2)}.toml"



def _manager_owned_agent_table(
    name: object,
    table: object,
    _managed_names: set[str] | None = None,
) -> bool:
    """Recognize a generated role without treating the ``cam_*`` namespace as ownership."""

    expected_path = _core._managed_agent_expected_path(name)
    if expected_path is None or not hasattr(table, "get"):
        return False
    config_file = str(table.get("config_file") or "").strip()
    if not config_file:
        return False
    candidate = _core.Path(config_file).expanduser()
    try:
        if candidate.resolve() != expected_path.resolve():
            return False
        if candidate.is_file() and _core._looks_like_managed_agent(candidate.read_bytes()):
            return True
    except OSError:
        return False
    description = str(table.get("description") or "")
    generated_description = "级子代理，第 " in description and "由 Agent Manager 路由到" in description
    return generated_description



def _looks_like_managed_agent(content: bytes | None) -> bool:
    if content is None:
        return False
    try:
        payload = _core.tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, _core.tomllib.TOMLDecodeError):
        return False
    return _core._managed_agent_name(payload.get("name")) and (
        payload.get("model_provider") == _core.AGGREGATE_PROVIDER_ID
        or "ordered fallback route" in str(payload.get("developer_instructions") or "")
    )



def _release_owned_subagent_hint(doc: object) -> None:
    try:
        saved = _core.read_json(_core.SETTINGS_FILE, {})
    except Exception:
        return
    if isinstance(saved, dict) and saved.get("managedSubagentPolicy"):
        _core._apply_managed_subagent_mode_hint(doc, {
            **saved, "subagentRouting": {"strategyId": "verification_first"},
        })



def _clean_runtime_config(content: bytes | None) -> bytes | None:
    if content is None:
        return None
    try:
        doc = _core.tomlkit.parse(content.decode("utf-8"))
    except Exception:
        return content
    _core._release_owned_subagent_hint(doc)
    if str(doc.get("model_provider") or "") == _core.AGGREGATE_PROVIDER_ID:
        doc.pop("model_provider", None)
        doc.pop("model", None)
    if str(doc.get("model_catalog_json") or "") == str(_core.MODEL_CATALOG_FILE):
        doc.pop("model_catalog_json", None)
    try:
        managed_base_urls = _core._managed_provider_base_urls(_core.load_settings())
    except Exception:
        managed_base_urls = set()
    if str(doc.get("openai_base_url") or "") in managed_base_urls:
        doc.pop("openai_base_url", None)
    providers = doc.get("model_providers")
    if providers is not None:
        providers.pop(_core.AGGREGATE_PROVIDER_ID, None)
        if not providers:
            doc.pop("model_providers", None)
    agents = doc.get("agents")
    if agents is not None:
        try:
            settings = _core.load_settings()
            managed_names = {str(item) for item in settings.get("managedAgentNames", [])}
        except Exception:
            managed_names = set()
        for name in list(agents.keys()):
            if _core._manager_owned_agent_table(name, agents.get(name), managed_names):
                agents.pop(name, None)
        if not agents:
            doc.pop("agents", None)
    rendered = _core.tomlkit.dumps(doc)
    return rendered.encode("utf-8") if rendered else None



def _merge_runtime_config_restore(current: bytes, baseline: bytes | None) -> bytes:
    try:
        current_doc = _core.tomlkit.parse(current.decode("utf-8"))
        baseline_doc = _core.tomlkit.parse((baseline or b"").decode("utf-8"))
    except Exception:
        return baseline or b""
    _core._release_owned_subagent_hint(current_doc)
    for key in ("model", "model_provider", "model_reasoning_effort", "model_catalog_json", "openai_base_url"):
        if key in baseline_doc:
            current_doc[key] = baseline_doc[key]
        else:
            current_doc.pop(key, None)
    current_providers = current_doc.get("model_providers")
    baseline_providers = baseline_doc.get("model_providers")
    if current_providers is not None:
        if baseline_providers is not None and _core.AGGREGATE_PROVIDER_ID in baseline_providers:
            current_providers[_core.AGGREGATE_PROVIDER_ID] = baseline_providers[_core.AGGREGATE_PROVIDER_ID]
        else:
            current_providers.pop(_core.AGGREGATE_PROVIDER_ID, None)
        if not current_providers:
            current_doc.pop("model_providers", None)
    current_agents = current_doc.get("agents")
    baseline_agents = baseline_doc.get("agents")
    if current_agents is not None:
        try:
            settings = _core.load_settings()
            managed_names = {str(item) for item in settings.get("managedAgentNames", [])}
        except Exception:
            managed_names = set()
        for name in list(current_agents.keys()):
            if not _core._manager_owned_agent_table(name, current_agents.get(name), managed_names):
                continue
            if baseline_agents is not None and name in baseline_agents:
                current_agents[name] = baseline_agents[name]
            else:
                current_agents.pop(name, None)
        if not current_agents:
            current_doc.pop("agents", None)
    return _core.tomlkit.dumps(current_doc).encode("utf-8")



def _runtime_overlay_targets() -> list[tuple[_core.Path, str]]:
    targets = [(_core.CONFIG_FILE, "config"), (_core.AGENTS_FILE, "agents"), (_core.MODEL_CATALOG_FILE, "catalog")]
    targets.extend(
        (_core.AGENTS_DIR / f"cam-{level}-{slot}.toml", "managed_agent")
        for level in _core.DIFFICULTIES
        for slot in range(1, 4)
    )
    return targets



def _runtime_overlay_relative(path: _core.Path) -> str:
    root = _core.CODEX_HOME.resolve()
    resolved = path.resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise _core.ManagerError("运行时配置覆盖只能管理 CODEX_HOME 内的文件。") from exc



def _runtime_overlay_read() -> dict | None:
    payload = _core.read_json(_core.RUNTIME_OVERLAY_FILE, None)
    if payload is None:
        return None
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        raise _core.ManagerError("运行时配置恢复记录格式无效。")
    if str(payload.get("codexHome") or "") != str(_core.CODEX_HOME):
        raise _core.ManagerError("运行时配置恢复记录不属于当前 CODEX_HOME。")
    if not isinstance(payload.get("files"), dict) or not isinstance(payload.get("environment"), dict):
        raise _core.ManagerError("运行时配置恢复记录内容不完整。")
    return payload



def _runtime_overlay_capture_files(paths: list[tuple[_core.Path, str]]) -> None:
    with _core.RUNTIME_OVERLAY_LOCK:
        payload = _core._runtime_overlay_read()
        if not payload or int(payload.get("ownerPid") or 0) != _core.os.getpid():
            return
        changed = False
        for path, kind in paths:
            relative = _core._runtime_overlay_relative(path)
            if relative in payload["files"]:
                continue
            current = path.read_bytes() if path.is_file() else None
            baseline = current
            if kind == "config":
                baseline = _core._clean_runtime_config(current)
            elif kind == "agents":
                baseline = _core._strip_manager_agents_block(current)
            elif kind == "catalog":
                baseline = None
            elif kind == "managed_agent" and _core._looks_like_managed_agent(current):
                baseline = None
            payload["files"][relative] = {
                "kind": kind,
                "baseline": _core._overlay_encrypt_bytes(baseline),
                "baselineHash": _core._overlay_value_hash(baseline),
                "capturedHash": _core._overlay_value_hash(current),
                "appliedHash": None,
            }
            changed = True
        if changed:
            _core.atomic_write_json(_core.RUNTIME_OVERLAY_FILE, payload)



def _runtime_overlay_capture_environment(names: list[str], clean_manager_state: bool = False) -> None:
    with _core.RUNTIME_OVERLAY_LOCK:
        payload = _core._runtime_overlay_read()
        if not payload or int(payload.get("ownerPid") or 0) != _core.os.getpid():
            return
        changed = False
        for name in names:
            if name in payload["environment"]:
                continue
            current = _core._read_user_environment(name)
            baseline = None if clean_manager_state and name == _core.AGGREGATE_ENV_KEY else current
            payload["environment"][name] = {
                "baseline": _core._overlay_encrypt_text(baseline),
                "baselineHash": _core._overlay_value_hash(baseline),
                "capturedHash": _core._overlay_value_hash(current),
                "appliedHash": None,
            }
            changed = True
        if changed:
            _core.atomic_write_json(_core.RUNTIME_OVERLAY_FILE, payload)



def _runtime_overlay_record_applied(paths: list[_core.Path] | None = None, environment: list[str] | None = None) -> None:
    with _core.RUNTIME_OVERLAY_LOCK:
        payload = _core._runtime_overlay_read()
        if not payload or int(payload.get("ownerPid") or 0) != _core.os.getpid():
            return
        for path in paths or []:
            relative = _core._runtime_overlay_relative(path)
            record = payload["files"].get(relative)
            if record is not None:
                record["appliedHash"] = _core._overlay_value_hash(path.read_bytes() if path.is_file() else None)
        for name in environment or []:
            record = payload["environment"].get(name)
            if record is not None:
                record["appliedHash"] = _core._overlay_value_hash(_core._read_user_environment(name))
        payload["updatedAt"] = _core.now_iso()
        _core.atomic_write_json(_core.RUNTIME_OVERLAY_FILE, payload)



def _runtime_overlay_rebase_user_file_checked(path: _core.Path, content: bytes | None) -> bool:
    overlay = _core._runtime_overlay_read()
    tracked = bool(overlay and _core._runtime_overlay_relative(path) in overlay["files"])
    rebased = _core._runtime_overlay_rebase_user_file(path, content)
    if tracked and not rebased:
        raise _core.ManagerError("该配置仍由其他临时恢复记录跟踪，未能更新恢复基线，已取消保存。")
    return rebased



def _runtime_overlay_rebase_user_file(path: _core.Path, content: bytes | None) -> bool:
    """Make an explicit user save the new restore baseline for an active overlay."""

    with _core.RUNTIME_OVERLAY_LOCK:
        payload = _core._runtime_overlay_read()
        if not payload or int(payload.get("ownerPid") or 0) != _core.os.getpid():
            return False
        relative = _core._runtime_overlay_relative(path)
        record = payload["files"].get(relative)
        if not isinstance(record, dict):
            return False
        kind = str(record.get("kind") or "")
        baseline = _core._clean_runtime_config(content) if kind == "config" else _core._strip_manager_agents_block(content) if kind == "agents" else content
        record["baseline"] = _core._overlay_encrypt_bytes(baseline)
        record["baselineHash"] = _core._overlay_value_hash(baseline)
        record["appliedHash"] = _core._overlay_value_hash(content)
        payload["updatedAt"] = _core.now_iso()
        _core.atomic_write_json(_core.RUNTIME_OVERLAY_FILE, payload)
        return True



def restore_runtime_configuration_overlay(force: bool = False) -> dict:
    with _core.RUNTIME_OVERLAY_LOCK:
        payload = _core._runtime_overlay_read()
        if not payload:
            return {"restored": False, "files": 0, "environment": 0, "conflicts": [], "warnings": []}
        _core._require_codex_process_scan_known()
        restored_files = 0
        restored_environment = 0
        conflicts = []
        warnings = []
        for relative, record in list(payload["files"].items()):
            try:
                candidate = (_core.CODEX_HOME / relative).resolve()
                candidate.relative_to(_core.CODEX_HOME.resolve())
                current = candidate.read_bytes() if candidate.is_file() else None
                baseline = _core._overlay_decrypt_bytes(record.get("baseline"))
                current_hash = _core._overlay_value_hash(current)
                expected = {
                    str(record.get("capturedHash") or ""),
                    str(record.get("appliedHash") or ""),
                    str(record.get("baselineHash") or ""),
                }
                kind = str(record.get("kind") or "")
                replacement = baseline
                safe = force or current_hash in expected
                if not safe and current is not None and kind == "config":
                    replacement = _core._merge_runtime_config_restore(current, baseline)
                    safe = True
                elif not safe and current is not None and kind == "agents":
                    replacement = _core._strip_manager_agents_block(current)
                    safe = True
                elif not safe and kind == "catalog":
                    safe = True
                elif not safe and kind == "managed_agent" and _core._looks_like_managed_agent(current):
                    safe = True
                if not safe:
                    conflicts.append(relative)
                    continue
                if current is not None and current != replacement:
                    _core.backup_file(candidate)
                if replacement is None:
                    candidate.unlink(missing_ok=True)
                elif current != replacement:
                    _core.atomic_write_bytes(candidate, replacement)
                restored_files += 1
                payload["files"].pop(relative, None)
            except Exception as exc:
                warnings.append(f"{relative}：{str(exc)[:240]}")
        for name, record in list(payload["environment"].items()):
            try:
                if str(name).upper() == "CODEX_CLI_PATH":
                    # Old manager builds could capture a codex.cmd override in
                    # the runtime overlay. Never restore that poisoned value.
                    _core._remove_user_environment("CODEX_CLI_PATH")
                    restored_environment += 1
                    warnings.append("已丢弃旧版保存的 CODEX_CLI_PATH，避免 Codex Desktop spawn EINVAL。")
                    payload["environment"].pop(name, None)
                    continue
                current = _core._read_user_environment(name)
                baseline = _core._overlay_decrypt_text(record.get("baseline"))
                current_hash = _core._overlay_value_hash(current)
                expected = {
                    str(record.get("capturedHash") or ""),
                    str(record.get("appliedHash") or ""),
                    str(record.get("baselineHash") or ""),
                }
                # CODEX_AGENT_MANAGER_API_KEY is a reserved, manager-owned
                # gateway credential.  Old builds could rotate it after the
                # overlay journal was written, leaving a different manager key
                # in the user environment and then treating that value as a
                # user edit.  When the pre-manager baseline was absent, no user
                # value can be lost: discard the orphaned key and complete the
                # restore instead of permanently blocking every future start.
                manager_owned_orphan = (
                    str(name).upper() == _core.AGGREGATE_ENV_KEY
                    and baseline is None
                    and str(record.get("baselineHash") or "") == _core._overlay_value_hash(None)
                )
                if not force and current_hash not in expected and not manager_owned_orphan:
                    conflicts.append(f"环境变量 {name}")
                    continue
                if not force and current_hash not in expected and manager_owned_orphan:
                    warnings.append(
                        "已清理旧版遗留的 CODEX_AGENT_MANAGER_API_KEY；"
                        "该变量仅属于本地网关，不是 Codex 官方登录配置。"
                    )
                if baseline is None:
                    _core._remove_user_environment(name)
                else:
                    _core._sync_user_environment(name, baseline)
                restored_environment += 1
                payload["environment"].pop(name, None)
            except Exception as exc:
                warnings.append(f"环境变量 {name}：{str(exc)[:240]}")
        remaining_files = sorted(payload["files"])
        remaining_environment = sorted(payload["environment"])
        complete = not remaining_files and not remaining_environment and not conflicts
        result = {
            "restored": complete,
            "partial": not complete and bool(restored_files or restored_environment),
            "sessionId": payload.get("sessionId"),
            "files": restored_files,
            "environment": restored_environment,
            "conflicts": conflicts,
            "warnings": warnings,
            "remainingFiles": remaining_files,
            "remainingEnvironment": remaining_environment,
            "restoredAt": _core.now_iso(),
        }
        _core.atomic_write_json(_core.RUNTIME_RESTORE_STATUS_FILE, result)
        if complete:
            _core.RUNTIME_OVERLAY_FILE.unlink(missing_ok=True)
        else:
            payload["ownerPid"] = 0
            payload["updatedAt"] = _core.now_iso()
            payload["lastRestoreAttempt"] = result
            _core.atomic_write_json(_core.RUNTIME_OVERLAY_FILE, payload)
        return result



def begin_runtime_configuration_overlay() -> dict:
    recovered = _core.restore_runtime_configuration_overlay()
    if _core.RUNTIME_OVERLAY_FILE.exists():
        remaining = [
            *[str(item) for item in recovered.get("remainingFiles", [])],
            *[f"环境变量 {item}" for item in recovered.get("remainingEnvironment", [])],
        ]
        detail = "、".join(remaining[:4]) or "未知项目"
        raise _core.ManagerError(
            "上一次临时 Codex 配置尚未完全恢复，已保留恢复记录，未应用新的临时配置。"
            f"Agent Manager 将保持安全修复模式，请在“紧急修复”中处理：{detail}"
        )
    _core._require_codex_process_scan_known()
    _core.ensure_state()
    payload = {
        "schemaVersion": 1,
        "sessionId": _core.uuid.uuid4().hex,
        "ownerPid": _core.os.getpid(),
        "codexHome": str(_core.CODEX_HOME),
        "createdAt": _core.now_iso(),
        "updatedAt": _core.now_iso(),
        "files": {},
        "environment": {},
    }
    with _core.RUNTIME_OVERLAY_LOCK:
        _core.atomic_write_json(_core.RUNTIME_OVERLAY_FILE, payload)
    _core._runtime_overlay_capture_files(_core._runtime_overlay_targets())
    current_config = _core.CONFIG_FILE.read_bytes() if _core.CONFIG_FILE.is_file() else None
    manager_state_present = current_config != _core._clean_runtime_config(current_config)
    _core._runtime_overlay_capture_environment([_core.AGGREGATE_ENV_KEY], clean_manager_state=manager_state_present)
    return {
        "active": True,
        "sessionId": payload["sessionId"],
        "createdAt": payload["createdAt"],
        "recovered": recovered,
    }



def adopt_runtime_configuration_overlay() -> dict:
    """Transfer an active temporary overlay to a quickly restarted manager.

    A quick manager restart must not restore and immediately reapply Codex
    configuration, because doing so can disrupt a running Codex process.  The
    original encrypted baselines remain unchanged; only ownership moves to the
    new process so a later full exit can still restore them exactly once.
    """
    _core.ensure_state()
    with _core.RUNTIME_OVERLAY_LOCK:
        payload = _core._runtime_overlay_read()
        if not payload:
            raise _core.ManagerError("没有可接管的临时 Codex 配置；请执行普通启动。")
        previous_owner = int(payload.get("ownerPid") or 0)
        payload["ownerPid"] = _core.os.getpid()
        payload["updatedAt"] = _core.now_iso()
        payload["adoptedAt"] = payload["updatedAt"]
        payload["previousOwnerPid"] = previous_owner
        _core.atomic_write_json(_core.RUNTIME_OVERLAY_FILE, payload)
    return {
        "active": True,
        "sessionId": payload.get("sessionId"),
        "createdAt": payload.get("createdAt"),
        "adopted": True,
        "previousOwnerPid": previous_owner,
    }



def _render_managed_agent(spec: dict) -> str:
    instructions = _core.textwrap.dedent(
        f"""\
        You are the {_core.DIFFICULTY_META[spec['level']]['name']} difficulty worker in an ordered fallback route.
        Begin working immediately when a bounded task arrives; do not merely acknowledge it and then idle. If
        executable inputs or required access are missing, report blocked immediately with the exact missing
        evidence. Complete only the delegated task and its acceptance criteria. Do not broaden the parent
        goal or delegate again. Other agents share this workspace: preserve their edits, stay within your
        owned files, and report an ownership conflict before changing overlapping work.
        Check the task's requirements before implementation quality. Run relevant checks and report actual
        results; do not claim success from an unexecuted test or repeat broad checks without new evidence.
        Return status (complete, blocked, or failed), outcome, changed files or artifact paths, checks run
        with results, and remaining risks. Keep supporting logs in artifacts. If asked for a correction,
        reuse established context and verify the changed behavior without restarting completed work.
        """
    ).strip()
    # Apply only the selected model's contract, using its native ID rather
    # than the private gateway alias. These are task-shaping instructions,
    # not inferred tool/context capabilities (see docs/subagent-policy.md).
    model_contracts = {
        "gpt-6-astra": (
            "Carry authorized work through the acceptance evidence. Resolve routine reversible details "
            "from context; surface only ambiguities that materially change the result. Calibrate testing "
            "to the actual change and stop expanding checks after the acceptance criteria pass."
        ),
        "gpt-5.6-sol": (
            "Use the task's outcome, domain context and constraints to choose an implementation path. "
            "Preserve all required evidence and caveats in the result even when the response is short. "
            "Do not invent extra approval gates for already authorized in-scope work."
        ),
        "gpt-5.6-terra": (
            "Keep the assigned investigation or implementation bounded by its entry points and "
            "integration boundary. Return distilled findings with file references and evidence. "
            "Separate established facts from assumptions needed by the integrating primary agent."
        ),
        "gpt-5.6-luna": (
            "Work from the supplied inputs and explicit acceptance criteria. Preserve the requested "
            "output fields and validate the concrete result. If an essential input or decision is "
            "missing, name it and return to the primary instead of expanding the task."
        ),
    }
    native_model = str(spec.get("nativeModel") or spec.get("model") or "")
    if native_model == "gpt-5.6":
        native_model = "gpt-5.6-sol"
    if native_model in model_contracts:
        instructions += "\n\n" + model_contracts[native_model]
    return _core.render_agent_toml(
        spec["name"],
        spec["description"],
        spec["model"],
        spec["effort"],
        instructions,
        None,
        None,
    )



def _managed_environment_values(settings: dict) -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    if _core.service_secret_configured("gateway_internal"):
        values[_core.AGGREGATE_ENV_KEY] = _core.load_service_secret("gateway_internal")
    elif _core.service_secret_configured("web2api"):
        # Recognize a pre-upgrade overlay only for safe removal/replacement.
        values[_core.AGGREGATE_ENV_KEY] = _core.load_service_secret("web2api")
    for provider in settings.get("providers", []):
        if not isinstance(provider, dict) or provider.get("kind") != "custom":
            continue
        env_key = str(provider.get("envKey") or "").strip()
        if not env_key or env_key.upper() == "CODEX_CLI_PATH":
            continue
        try:
            _core._validate_provider_env_key(env_key)
            provider_id = str(provider.get("id") or "")
            values[env_key] = _core.load_provider_key(provider_id) if _core.provider_key_configured(provider_id) else None
        except _core.ManagerError:
            values[env_key] = None
    return values



def _inactive_provider_environment_overrides(settings: dict, active_env_keys: list[str] | set[str]) -> list[str]:
    active = {str(item) for item in active_env_keys}
    overrides = []
    for name, managed_value in _core._managed_environment_values(settings).items():
        if name in active or managed_value is None:
            continue
        if _core._read_user_environment(name) == managed_value:
            overrides.append(name)
    return overrides



def _clear_inactive_provider_environment_overrides(
    settings: dict,
    active_env_keys: list[str] | set[str],
) -> list[str]:
    active = {str(item) for item in active_env_keys}
    changed: list[str] = []
    with _core.RUNTIME_OVERLAY_LOCK:
        overlay = _core._runtime_overlay_read()
        overlay_changed = False
        for name, managed_value in _core._managed_environment_values(settings).items():
            if name in active:
                continue
            record = overlay.get("environment", {}).get(name) if overlay else None
            current = _core._read_user_environment(name)
            if record is None and (managed_value is None or current != managed_value):
                continue
            replacement = _core._overlay_decrypt_text(record.get("baseline")) if record else None
            if managed_value is not None and replacement == managed_value:
                replacement = None
                record["baseline"] = _core._overlay_encrypt_text(None)
                record["baselineHash"] = _core._overlay_value_hash(None)
                overlay_changed = True
            if current != replacement:
                if replacement is None:
                    _core._remove_user_environment(name)
                else:
                    _core._sync_user_environment(name, replacement)
                changed.append(name)
            if record is not None:
                record["appliedHash"] = _core._overlay_value_hash(replacement)
                overlay_changed = True
        if overlay and overlay_changed:
            overlay["updatedAt"] = _core.now_iso()
            _core.atomic_write_json(_core.RUNTIME_OVERLAY_FILE, overlay)
    return changed

