"""Coordinate restore points with the services that own their files."""

from contextlib import ExitStack, contextmanager

import agent_manager_core as core
import claude_manager_service as claude
import recovery_service as recovery
import toolbox_service as toolbox


@contextmanager
def _guard(runtime, *, usage=False):
    manager = runtime.web2api
    store = getattr(manager, "usage_stats", None) if usage else None
    # Match recovery's outer lock order and account switching's gateway order.
    # The usage writer always takes io_lock before its in-memory delta lock.
    with ExitStack() as stack:
        for lock in (recovery.LOCK, core.SWITCH_OPERATION_LOCK,
                     getattr(manager, "lock", None),
                     getattr(store, "io_lock", None), getattr(store, "lock", None),
                     toolbox._TOTP_STORE_LOCK, toolbox._EMAIL_STORE_LOCK, claude._LOCK):
            if lock is not None:
                stack.enter_context(lock)
        yield store


def _flush_usage(store):
    if store is None:
        return
    store.flush(force=True)
    # flush() intentionally keeps failed deltas for a later retry. A backup or
    # reset must not proceed on a partially persisted usage document.
    if getattr(store, "pending", None) or getattr(store, "pending_recent", None):
        raise core.ManagerError("用量记录尚未完整写入，已保留当前统计；请稍后重试备份或清空。")


def create(runtime, scope="configuration", name=""):
    with _guard(runtime, usage=scope == "usage") as store:
        _flush_usage(store)
        return recovery.create(scope, name)


def reset_usage(runtime):
    with _guard(runtime, usage=True) as store:
        _flush_usage(store)
        point = recovery.create("usage", "清空统计前", reason="before_clear")
        return runtime.web2api.reset_usage_stats(), point


def before_account_delete(runtime, name, operation):
    """Retain an authenticated undo point before removing local credentials."""
    with _guard(runtime):
        point = recovery.create("configuration", name, reason="before_delete")
        try:
            before = {str(item.get("id")) for item in core.load_settings().get("accounts", [])}
            result = operation()
            after = {str(item.get("id")) for item in core.load_settings().get("accounts", [])}
            clear_state = getattr(runtime.web2api, "clear_account_runtime_state", None)
            if before - after and callable(clear_state):
                clear_state(before - after)
            return result, point
        except Exception as exc:
            raise core.ManagerError(
                f"删除未完成：{core._redact_sensitive_text(exc, limit=320)}；删除前恢复点已保留：{point['id']}"
            ) from exc


def restore(runtime, point_id, fingerprint):
    with _guard(runtime, usage=True) as store:
        status = runtime.web2api.status()
        if status.get("running") or status.get("activeRequestCount", 0):
            raise core.ManagerError("请先停止本地反代服务，并等待现有请求结束，再恢复备份。")
        _flush_usage(store)
        result = recovery.restore(point_id, fingerprint)
        if result.get("restored") and result.get("scope") == "configuration":
            toolbox._clear_oauth_state()
            forget = getattr(getattr(runtime, "toolbox", None), "forget_all_mail", None)
            if callable(forget):
                forget()
        return result
