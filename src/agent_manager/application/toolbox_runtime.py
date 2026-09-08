"""Toolbox services."""
from __future__ import annotations
from agent_manager import application as _app


class ToolboxRuntime:
    """Manual-only mailbox access with single-flight and a short anti-repeat cache."""

    CACHE_SECONDS = 15.0

    def __init__(self) -> None:
        self.lock = _app.threading.RLock()
        self.active_mailboxes: set[str] = set()
        self.mail_cache: dict[tuple[str, int, bool], tuple[float, list[dict]]] = {}
        self.temporary_mail_cache: dict[tuple[str, int, bool], tuple[float, list[dict]]] = {}
        self.mail_generation = 0
        self.mail_revisions: dict[str, int] = {}

    def state(self) -> dict:
        return {
            "totpItems": [_app._totp_api_item(item) for item in _app.toolbox.list_totp_items()],
            "mailAccounts": [_app._mail_account_api_item(item) for item in _app.toolbox.list_mail_accounts()],
        }

    def fetch_mail(self, account_id: str, *, limit: int, unread_only: bool) -> list[dict]:
        key = (account_id, limit, unread_only)
        now = _app.time.monotonic()
        with self.lock:
            cached = self.mail_cache.get(key)
            if cached and now - cached[0] < self.CACHE_SECONDS:
                return _app.json.loads(_app.json.dumps(cached[1]))
            if account_id in self.active_mailboxes:
                raise _app.core.ManagerError("这个邮箱正在读取中，请勿重复请求。")
            self.active_mailboxes.add(account_id)
            generation = (self.mail_generation, self.mail_revisions.get(account_id, 0))
        try:
            messages = _app.toolbox.fetch_mail_messages(
                account_id,
                limit=limit,
                unread_only=unread_only,
            )
            public_messages = [_app._mail_message_api_item(item) for item in messages]
            with self.lock:
                if generation != (self.mail_generation, self.mail_revisions.get(account_id, 0)):
                    raise _app.core.ManagerError("邮箱凭据已修改或恢复，请重新读取邮件。")
                self.mail_cache[key] = (_app.time.monotonic(), public_messages)
            return _app.json.loads(_app.json.dumps(public_messages))
        finally:
            with self.lock:
                self.active_mailboxes.discard(account_id)

    def forget_mail(self, account_id: str) -> None:
        with self.lock:
            self.mail_revisions[account_id] = self.mail_revisions.get(account_id, 0) + 1
            for key in [key for key in self.mail_cache if key[0] == account_id]:
                self.mail_cache.pop(key, None)

    def forget_all_mail(self) -> None:
        with self.lock:
            self.mail_generation += 1
            self.mail_revisions.clear()
            self.mail_cache.clear()
            self.temporary_mail_cache.clear()

    def fetch_temporary_mail(self, source: str, *, limit: int, unread_only: bool) -> list[dict]:
        """Read one unsaved mailbox bundle without retaining its credentials."""
        raw = str(source or "")
        accounts = _app.toolbox.parse_email_accounts(raw, max_accounts=1)
        if len(accounts) != 1:
            raise _app.toolbox.ValidationError("请输入一个邮箱账号。")
        digest = _app.hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
        mailbox_key = f"temporary:{digest}"
        key = (digest, limit, unread_only)
        now = _app.time.monotonic()
        with self.lock:
            cached = self.temporary_mail_cache.get(key)
            if cached and now - cached[0] < self.CACHE_SECONDS:
                return _app.json.loads(_app.json.dumps(cached[1]))
            if mailbox_key in self.active_mailboxes:
                raise _app.core.ManagerError("这个邮箱正在读取中，请稍候。")
            self.active_mailboxes.add(mailbox_key)
            generation = self.mail_generation
        try:
            messages = _app.toolbox.fetch_email_messages(
                accounts[0],
                limit=limit,
                unread_only=unread_only,
            )
            public_messages = [_app._mail_message_api_item(item) for item in messages]
            with self.lock:
                if generation != self.mail_generation:
                    raise _app.core.ManagerError("邮箱缓存已清理，请重新读取邮件。")
                self.temporary_mail_cache[key] = (_app.time.monotonic(), public_messages)
            return _app.json.loads(_app.json.dumps(public_messages))
        finally:
            # `accounts` and `raw` fall out of scope here; no credential is
            # written to the mailbox store or returned by the API.
            with self.lock:
                self.active_mailboxes.discard(mailbox_key)

