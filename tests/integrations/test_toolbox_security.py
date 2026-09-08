import json
from pathlib import Path
import tempfile
import threading
import traceback
import unittest
from unittest.mock import patch

import agent_manager.core as core
import agent_manager.integrations.toolbox as toolbox


class RawResponse:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.headers = {"Content-Length": str(len(raw))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, maximum):
        return self.raw[:maximum]


class RefusedStartTLS:
    def __init__(self):
        self.login_called = False
        self.logout_called = False

    def starttls(self, **_kwargs):
        return "NO", [b"TLS unavailable"]

    def login(self, *_args):
        self.login_called = True
        return "OK", []

    def logout(self):
        self.logout_called = True


class ToolboxSecurityV8Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.email_path = self.root / "mail.json"
        self.totp_path = self.root / "totp.json"
        self.protect = patch.object(
            core,
            "dpapi_protect",
            side_effect=lambda value: b"enc:" + value.encode("utf-8"),
        )
        self.unprotect = patch.object(
            core,
            "dpapi_unprotect",
            side_effect=lambda value: value.removeprefix(b"enc:").decode("utf-8"),
        )
        self.protect.start()
        self.unprotect.start()
        self.addCleanup(self.protect.stop)
        self.addCleanup(self.unprotect.stop)
        toolbox._clear_oauth_state()

    def test_totp_rejects_unrepresentable_timestamps_and_boolean_window(self):
        source = "JBSWY3DPEHPK3PXP"
        for timestamp in (True, "not-a-time", 1e300):
            with self.subTest(timestamp=timestamp), self.assertRaises(toolbox.ValidationError):
                toolbox.generate_totp(source, timestamp=timestamp)
        with self.assertRaises(toolbox.ValidationError):
            toolbox.verify_totp(source, "123456", window=True)
        self.assertFalse(toolbox.verify_totp(source, "١٢٣٤٥٦"))

    def test_totp_encryption_failure_drops_secret_bearing_cause(self):
        secret = "JBSWY3DPEHPK3PXP"
        with patch.object(core, "dpapi_protect", side_effect=RuntimeError(f"failed for {secret}")):
            with self.assertRaises(toolbox.CredentialStoreError) as raised:
                toolbox.save_totp_item(secret, path=self.totp_path)
        rendered = "".join(traceback.format_exception(raised.exception))
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, rendered)
        self.assertIsNone(raised.exception.__cause__)

    def test_dpapi_round_trip_is_verified_before_publishing_credentials(self):
        with patch.object(core, "dpapi_protect", return_value=b"enc:different-plaintext"):
            with self.assertRaises(toolbox.CredentialStoreError):
                toolbox.save_totp_item("JBSWY3DPEHPK3PXP", path=self.totp_path)
            with self.assertRaises(toolbox.CredentialStoreError):
                toolbox.save_email_accounts(
                    {"email": "a@gmail.com", "password": "secret"},
                    path=self.email_path,
                )
        self.assertFalse(self.totp_path.exists())
        self.assertFalse(self.email_path.exists())

    def test_duplicate_json_fields_are_never_silently_selected(self):
        with self.assertRaisesRegex(toolbox.ValidationError, "重复字段"):
            toolbox.parse_email_accounts(
                '{"email":"a@gmail.com","password":"first","password":"second"}'
            )
        with self.assertRaisesRegex(toolbox.ValidationError, "重复字段"):
            toolbox.parse_totp_source(
                '{"secret":"JBSWY3DPEHPK3PXP","secret":"GEZDGNBVGY3TQOJQ"}'
            )
        self.email_path.write_text(
            '{"schema":"toolbox-email-accounts-dpapi-v1","accounts":{},"accounts":{}}',
            encoding="utf-8",
        )
        with self.assertRaises(toolbox.CredentialStoreError):
            toolbox.list_email_accounts(path=self.email_path)

    def test_iterable_import_is_bounded_and_fractional_selection_is_rejected(self):
        yielded = 0

        def records():
            nonlocal yielded
            while True:
                yielded += 1
                yield {"email": f"user{yielded}@gmail.com", "password": "secret"}

        with self.assertRaisesRegex(toolbox.ValidationError, "数量超过"):
            toolbox.parse_email_accounts(records(), max_accounts=2)
        self.assertEqual(yielded, 3)
        payload = [{"email": "a@gmail.com", "password": "secret"}]
        with self.assertRaisesRegex(toolbox.ValidationError, "序号格式"):
            toolbox.save_mail_import_selection(payload, [0.5], path=self.email_path)

    def test_selected_import_holds_lock_until_stale_credentials_are_committed(self):
        toolbox.save_email_accounts(
            {"id": "primary", "email": "a@gmail.com", "password": "old-password"},
            path=self.email_path,
        )
        payload = json.dumps({"id": "primary", "email": "a@gmail.com", "label": "selected"})
        entered_merge = threading.Event()
        release_merge = threading.Event()
        updater_started = threading.Event()
        updater_done = threading.Event()
        failures = []
        original_normalize = toolbox._normalize_email_record
        blocked_once = False

        def blocking_normalize(record, *, existing=None):
            nonlocal blocked_once
            if threading.current_thread().name == "selector" and existing is not None and not blocked_once:
                blocked_once = True
                entered_merge.set()
                if not release_merge.wait(3):
                    raise AssertionError("selection merge was not released")
            return original_normalize(record, existing=existing)

        def select_import():
            try:
                toolbox.save_mail_import_selection(payload, [0], path=self.email_path)
            except Exception as exc:  # pragma: no cover - reported by the assertion below
                failures.append(exc)

        def update_password():
            updater_started.set()
            try:
                toolbox.update_email_account("primary", {"password": "new-password"}, path=self.email_path)
            except Exception as exc:  # pragma: no cover - reported by the assertion below
                failures.append(exc)
            finally:
                updater_done.set()

        with patch.object(toolbox, "_normalize_email_record", side_effect=blocking_normalize):
            selector = threading.Thread(target=select_import, name="selector")
            selector.start()
            self.assertTrue(entered_merge.wait(2))
            updater = threading.Thread(target=update_password, name="updater")
            updater.start()
            self.assertTrue(updater_started.wait(1))
            updater_done.wait(0.2)
            release_merge.set()
            selector.join(3)
            updater.join(3)
        self.assertFalse(selector.is_alive())
        self.assertFalse(updater.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(
            toolbox._load_email_account("primary", path=self.email_path)["password"],
            "new-password",
        )

    def test_recovery_generation_blocks_old_rotated_token_and_health_writeback(self):
        toolbox.save_email_accounts(
            {
                "id": "primary",
                "email": "a@gmail.com",
                "refresh_token": "snapshot-refresh",
                "client_id": "client-id",
            },
            path=self.email_path,
        )
        account, generation = toolbox._load_email_account_snapshot("primary", path=self.email_path)
        toolbox._clear_oauth_state()  # recovery coordinator invalidates in-flight work after restoring bytes
        token = toolbox._resolve_access_token(
            account,
            timeout=4,
            path=self.email_path,
            expected_store_generation=generation,
            opener=lambda _request, timeout: RawResponse(
                json.dumps(
                    {
                        "access_token": f"ephemeral-{timeout}",
                        "refresh_token": "stale-rotated-refresh",
                        "expires_in": 3600,
                    }
                ).encode()
            ),
        )
        self.assertEqual(token, "ephemeral-4.0")
        current = toolbox._load_email_account("primary", path=self.email_path)
        self.assertEqual(current["refresh_token"], "snapshot-refresh")
        self.assertFalse(
            toolbox._save_email_health(
                "primary",
                status="healthy",
                expected_revision=account["_credential_revision"],
                expected_generation=generation,
                path=self.email_path,
            )
        )
        self.assertNotIn("_health", toolbox._load_email_account("primary", path=self.email_path))

    def test_email_store_silent_corruption_is_detected_and_rolled_back(self):
        toolbox.save_email_accounts(
            {"id": "primary", "email": "a@gmail.com", "password": "old-password"},
            path=self.email_path,
        )
        before = self.email_path.read_bytes()
        real_write = core.atomic_write_json

        def corrupt_after_write(path, payload):
            real_write(path, payload)
            if path == self.email_path:
                path.write_text("{broken", encoding="utf-8")

        with patch.object(core, "atomic_write_json", side_effect=corrupt_after_write):
            with self.assertRaises(toolbox.CredentialStoreError):
                toolbox.update_email_account("primary", {"label": "new"}, path=self.email_path)
        self.assertEqual(self.email_path.read_bytes(), before)
        self.assertEqual(
            toolbox._load_email_account("primary", path=self.email_path)["password"],
            "old-password",
        )

    def test_totp_store_silent_corruption_is_detected_and_rolled_back(self):
        saved = toolbox.save_totp_item("JBSWY3DPEHPK3PXP", path=self.totp_path)
        before = self.totp_path.read_bytes()
        real_write = core.atomic_write_json

        def corrupt_after_write(path, payload):
            real_write(path, payload)
            if path == self.totp_path:
                path.write_text("{broken", encoding="utf-8")

        with patch.object(core, "atomic_write_json", side_effect=corrupt_after_write):
            with self.assertRaises(toolbox.CredentialStoreError):
                toolbox.update_totp_item(saved["id"], label="new", path=self.totp_path)
        self.assertEqual(self.totp_path.read_bytes(), before)
        self.assertEqual(toolbox.list_totp_items(path=self.totp_path)[0]["label"], "")

    def test_starttls_refusal_never_reaches_password_login(self):
        connection = RefusedStartTLS()
        with self.assertRaisesRegex(toolbox.MailAccessError, "安全的 IMAP"):
            toolbox.fetch_email_messages(
                {
                    "email": "user@example.com",
                    "password": "mail-secret",
                    "imap_host": "imap.example.com",
                    "imap_port": 143,
                    "security": "starttls",
                },
                imap_factory=lambda *_args, **_kwargs: connection,
            )
        self.assertFalse(connection.login_called)
        self.assertTrue(connection.logout_called)

    def test_invalid_fetch_options_do_not_poison_saved_credential_health(self):
        toolbox.save_email_accounts(
            {"id": "primary", "email": "a@gmail.com", "password": "password"},
            path=self.email_path,
        )
        account = toolbox._load_email_account("primary", path=self.email_path)
        toolbox._save_email_health(
            "primary",
            status="healthy",
            expected_revision=account["_credential_revision"],
            path=self.email_path,
        )
        with self.assertRaises(toolbox.ValidationError):
            toolbox.fetch_saved_email_messages("primary", path=self.email_path, limit=False)
        health = toolbox.list_email_accounts(path=self.email_path)[0]["credential_status"]["health"]
        self.assertEqual(health["status"], "healthy")

    def test_store_index_must_match_encrypted_account_identity(self):
        account = toolbox.parse_email_accounts(
            {"id": "inside", "email": "a@gmail.com", "password": "password"}
        )[0]
        toolbox._set_credential_revision(account)
        core.atomic_write_json(
            self.email_path,
            {
                "schema": toolbox.EMAIL_STORE_SCHEMA,
                "accounts": {"outside": toolbox._encrypt_email_account(account)},
            },
        )
        with self.assertRaisesRegex(toolbox.CredentialStoreError, "索引不一致"):
            toolbox.list_email_accounts(path=self.email_path)

    def test_oauth_response_duplicates_controls_and_bad_timeout_are_rejected(self):
        account = toolbox.parse_email_accounts(
            {"email": "a@gmail.com", "refresh_token": "refresh", "client_id": "client"}
        )[0]
        cases = (
            b'{"access_token":"first","access_token":"second"}',
            b'{"access_token":"bad\\u0001token"}',
        )
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(toolbox.MailAccessError):
                toolbox._refresh_oauth_access_token(
                    account,
                    timeout=4,
                    opener=lambda _request, timeout, raw=raw: RawResponse(raw),
                )
        with self.assertRaises(toolbox.MailAccessError):
            toolbox._refresh_oauth_access_token(account, timeout=True, opener=lambda *_args: None)
        with self.assertRaises(toolbox.ValidationError):
            toolbox.parse_email_accounts(
                {
                    "email": "a@gmail.com",
                    "refresh_token": "refresh",
                    "client_id": "client",
                    "token_endpoint": "https://:password@oauth2.googleapis.com/token",
                }
            )


if __name__ == "__main__":
    unittest.main()
