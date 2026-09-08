from __future__ import annotations

import base64
from email.message import EmailMessage
import json
from pathlib import Path
import tempfile
import traceback
import unittest
from unittest.mock import patch
import urllib.parse

import agent_manager.core as core
import agent_manager.integrations.toolbox as toolbox


class TOTPTests(unittest.TestCase):
    def test_rfc6238_vectors_for_all_algorithms(self):
        vectors = (
            (b"12345678901234567890", "SHA1", "94287082"),
            (b"12345678901234567890123456789012", "SHA256", "46119246"),
            (b"1234567890123456789012345678901234567890123456789012345678901234", "SHA512", "90693936"),
        )
        for secret, algorithm, expected in vectors:
            encoded = base64.b32encode(secret).decode("ascii").rstrip("=")
            uri = f"otpauth://totp/Example:user?secret={encoded}&algorithm={algorithm}&digits=8&period=30&issuer=Example"
            with self.subTest(algorithm=algorithm):
                result = toolbox.generate_totp(uri, 59)
                self.assertEqual(result["code"], expected)
                self.assertEqual(result["seconds_remaining"], 1)
                self.assertNotIn("secret", result)

    def test_plain_base32_tolerates_spaces_hyphens_and_reports_boundary(self):
        noisy = "gezd gnbv-gy3t qojq gezd gnbv gy3t qojq"
        self.assertEqual(toolbox.generate_totp(noisy, 59)["code"], "287082")
        self.assertEqual(toolbox.generate_totp(noisy, 60)["seconds_remaining"], 30)

    def test_uri_metadata_and_verification_window(self):
        uri = (
            "otpauth://totp/Acme%20Corp:alice%40example.com?"
            "secret=JBSWY3DPEHPK3PXP&issuer=Acme%20Corp&period=45&digits=6"
        )
        generated = toolbox.generate_totp(uri, 1_234)
        self.assertEqual(generated["label"], "Acme Corp:alice@example.com")
        self.assertEqual(generated["issuer"], "Acme Corp")
        self.assertTrue(toolbox.verify_totp(uri, generated["code"], 1_234, window=0))
        self.assertFalse(toolbox.verify_totp(uri, "000000", 1_234, window=0))

    def test_strict_totp_validation(self):
        invalid = (
            "abc!",
            "otpauth://hotp/name?secret=JBSWY3DPEHPK3PXP",
            "otpauth://totp/name?secret=JBSWY3DPEHPK3PXP&digits=7",
            "otpauth://totp/name?secret=AAA&secret=BBB",
            "otpauth://totp/One:user?secret=JBSWY3DPEHPK3PXP&issuer=Two",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(toolbox.ValidationError):
                toolbox.generate_totp(value, 0)

        invalid_configs = (
            toolbox.TOTPConfig(secret=b"secret", algorithm="MD5"),
            toolbox.TOTPConfig(secret=b"secret", digits=7),
            toolbox.TOTPConfig(secret=b"secret", period=0),
            toolbox.TOTPConfig(secret=b"", period=30),
        )
        for config in invalid_configs:
            with self.subTest(config=config), self.assertRaises(toolbox.ValidationError):
                toolbox.generate_totp(config, 0)

    def test_common_export_wrappers_and_login_bundles_are_recognized_locally(self):
        expected = toolbox.generate_totp("JBSWY3DPEHPK3PXP", 59)["code"]
        variants = [
            "https://2fa.show/2fa/JBSWY3DPEHPK3PXP",
            '{"account":"user@example.com","otp_secret":"JBSWY3DPEHPK3PXP"}',
            "secret = JBSWY3DPEHPK3PXP",
            "user@example.com----JBSWY3DPEHPK3PXP",
            "user@example.com----password-value----JBSWY3DPEHPK3PXP",
            "user@example.com——password-value——JBSWY3DPEHPK3PXP",
            "user@example.com|password-value|JBSWY3DPEHPK3PXP",
            "```text\nJBSW Y3DP-EHPK 3PXP\n```",
        ]
        for value in variants:
            with self.subTest(value=value):
                self.assertEqual(toolbox.generate_totp(value, 59)["code"], expected)

    def test_single_google_authenticator_migration_uri_is_supported(self):
        def varint(value):
            encoded = bytearray()
            while True:
                byte = value & 0x7F
                value >>= 7
                encoded.append(byte | (0x80 if value else 0))
                if not value:
                    return bytes(encoded)

        def field(number, value, wire=2):
            tag = varint((number << 3) | wire)
            if wire == 0:
                return tag + varint(value)
            return tag + varint(len(value)) + value

        secret = base64.b32decode("JBSWY3DPEHPK3PXP")
        parameters = b"".join(
            (
                field(1, secret),
                field(2, b"alice@example.com"),
                field(3, b"Example"),
                field(4, 1, wire=0),
                field(5, 1, wire=0),
                field(6, 2, wire=0),
            )
        )
        payload = field(1, parameters)
        encoded = urllib.parse.quote(base64.b64encode(payload).decode("ascii"), safe="")
        uri = f"otpauth-migration://offline?data={encoded}"
        generated = toolbox.generate_totp(uri, 59)
        self.assertEqual(
            generated["code"],
            toolbox.generate_totp("JBSWY3DPEHPK3PXP", 59)["code"],
        )
        self.assertEqual(generated["label"], "alice@example.com")
        self.assertEqual(generated["issuer"], "Example")


class TOTPStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "totp-secrets.json"
        self.protect = patch.object(
            toolbox.core,
            "dpapi_protect",
            side_effect=lambda value: b"enc:" + value.encode("utf-8"),
        )
        self.unprotect = patch.object(
            toolbox.core,
            "dpapi_unprotect",
            side_effect=lambda value: value.removeprefix(b"enc:").decode("utf-8"),
        )
        self.protect.start()
        self.unprotect.start()

    def tearDown(self):
        self.protect.stop()
        self.unprotect.stop()
        self.temp.cleanup()

    def test_dpapi_store_deduplicates_lists_and_deletes_without_exposing_secret(self):
        with patch.object(toolbox.secrets, "token_hex", return_value="0123456789abcdef"):
            saved = toolbox.save_totp_item(
                "JBSWY3DPEHPK3PXP",
                label="OpenAI",
                path=self.path,
            )
        self.assertEqual(saved["id"], "totp_0123456789abcdef")
        self.assertNotIn("JBSWY3DPEHPK3PXP", self.path.read_text(encoding="utf-8"))
        toolbox.save_totp_item("JBSWY3DPEHPK3PXP", label="工作账号", path=self.path)
        items = toolbox.list_totp_items(path=self.path, timestamp=59)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["label"], "工作账号")
        self.assertTrue(items[0]["code"].isdigit())
        self.assertTrue(toolbox.delete_totp_item(items[0]["id"], path=self.path))
        self.assertEqual(toolbox.list_totp_items(path=self.path), [])

    def test_saved_totp_can_edit_label_or_replace_secret_atomically(self):
        with patch.object(toolbox.secrets, "token_hex", return_value="0123456789abcdef"):
            saved = toolbox.save_totp_item("JBSWY3DPEHPK3PXP", label="旧备注", path=self.path)
        original_code = toolbox.list_totp_items(path=self.path, timestamp=59)[0]["code"]
        edited = toolbox.save_totp_item(
            json.dumps({"_toolboxEdit": True, "id": saved["id"]}),
            label="新备注",
            path=self.path,
        )
        self.assertEqual(edited["id"], saved["id"])
        self.assertEqual(edited["label"], "新备注")
        self.assertEqual(toolbox.list_totp_items(path=self.path, timestamp=59)[0]["code"], original_code)
        replaced = toolbox.update_totp_item(
            saved["id"],
            source="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
            path=self.path,
        )
        self.assertEqual(replaced["id"], saved["id"])
        self.assertNotEqual(toolbox.list_totp_items(path=self.path, timestamp=59)[0]["code"], original_code)
        store_text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("JBSWY3DPEHPK3PXP", store_text)
        self.assertNotIn("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", store_text)


class MailImportTests(unittest.TestCase):
    def test_json_aliases_provider_defaults_and_safe_preview(self):
        source = json.dumps(
            {
                "accounts": [
                    {"邮箱": "Alice@Gmail.com", "应用密码": "app-secret", "备注": "个人"},
                    {
                        "mail": "bob@outlook.com",
                        "refreshToken": "refresh-secret",
                        "clientId": "client-id",
                    },
                ]
            },
            ensure_ascii=False,
        )
        parsed = toolbox.parse_email_accounts(source)
        self.assertEqual(parsed[0]["imap_host"], "imap.gmail.com")
        self.assertEqual(parsed[0]["auth_method"], "password")
        self.assertEqual(parsed[1]["provider"], "microsoft")
        self.assertEqual(parsed[1]["auth_method"], "oauth2")
        preview = toolbox.preview_mail_import(source)
        encoded_preview = json.dumps(preview, ensure_ascii=False)
        self.assertNotIn("app-secret", encoded_preview)
        self.assertNotIn("refresh-secret", encoded_preview)
        self.assertTrue(preview[0]["credential_status"]["password"])

    def test_jsonl_comments_csv_and_key_value_formats(self):
        jsonl = """
        # exported accounts
        {"email":"a@gmail.com","password":"one"}
        {"email":"b@outlook.com","access_token":"two"}
        """
        self.assertEqual(len(toolbox.parse_email_accounts(jsonl)), 2)
        csv_text = "email|app_password|provider\nc@gmail.com|three|google\n"
        self.assertEqual(toolbox.parse_email_accounts(csv_text)[0]["password"], "three")
        key_values = "邮箱: d@example.net\n密码: four\n服务器: imap.example.net\n端口: 143\n加密: STARTTLS"
        parsed = toolbox.parse_email_accounts(key_values)[0]
        self.assertEqual(parsed["security"], "starttls")
        self.assertEqual(parsed["imap_port"], 143)

    def test_accepts_password_plus_token_bundles_but_rejects_insecure_inputs(self):
        access = toolbox.parse_email_accounts(
            "user@outlook.com----mail-password----access-token"
        )[0]
        self.assertEqual(access["auth_method"], "oauth2")
        self.assertEqual(access["password"], "mail-password")
        self.assertEqual(access["access_token"], "access-token")
        mixed = toolbox.parse_email_accounts(
            "user@outlook.com----mail-password----refresh-token----client-id"
        )[0]
        self.assertEqual(mixed["auth_method"], "oauth2")
        self.assertEqual(mixed["password"], "mail-password")
        self.assertEqual(mixed["refresh_token"], "refresh-token")
        cases = (
            {"email": "a@example.com", "password": "x", "imap_host": "imap.example.com", "security": "none"},
            {"email": "a@example.com", "refresh_token": "x", "client_id": "y", "imap_host": "imap.example.com"},
            {"email": "bad-address", "password": "x", "imap_host": "imap.example.com"},
        )
        for value in cases:
            with self.subTest(value=value), self.assertRaises(toolbox.ValidationError):
                toolbox.parse_email_accounts(value)
        with self.assertRaises(toolbox.ValidationError):
            toolbox.parse_email_accounts(b"x" * (toolbox.MAX_IMPORT_BYTES + 1))

    def test_vendor_oauth_is_pinned_to_official_imap_ssl_endpoints(self):
        invalid = (
            {
                "email": "a@gmail.com",
                "provider": "google",
                "access_token": "google-token",
                "imap_host": "imap.attacker.example",
            },
            {
                "email": "a@outlook.com",
                "provider": "microsoft",
                "access_token": "microsoft-token",
                "imap_port": 143,
                "security": "starttls",
            },
            {
                "email": "a@example.com",
                "provider": "custom",
                "access_token": "vendor-access-token",
                "imap_host": "imap.example.com",
            },
            {
                "email": "a@example.com",
                "provider": "custom",
                "refresh_token": "vendor-refresh-token",
                "client_id": "client-id",
                "imap_host": "imap.example.com",
            },
            {
                "email": "a@gmail.com",
                "password": "app-password",
                "imap_host": "imap.example.com",
            },
        )
        for account in invalid:
            with self.subTest(account=account), self.assertRaises(toolbox.ValidationError) as raised:
                toolbox.parse_email_accounts(account)
            message = str(raised.exception)
            self.assertNotIn("google-token", message)
            self.assertNotIn("microsoft-token", message)
            self.assertNotIn("vendor-access-token", message)
            self.assertNotIn("vendor-refresh-token", message)

        custom = toolbox.parse_email_accounts(
            {
                "email": "a@gmail.com",
                "provider": "custom",
                "password": "app-password",
                "imap_host": "imap.example.com",
                "imap_port": 143,
                "security": "starttls",
            }
        )[0]
        self.assertEqual(custom["provider"], "custom")

    def test_preview_isolates_invalid_records(self):
        preview = toolbox.preview_mail_import_items(
            [
                {"email": "ok@gmail.com", "password": "app-password"},
                {"email": "not-an-email", "password": "broken"},
            ]
        )
        self.assertEqual(preview["total"], 2)
        self.assertEqual(preview["valid"], 1)
        self.assertTrue(preview["items"][0]["valid"])
        self.assertFalse(preview["items"][1]["valid"])


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "mail-secrets.json"
        self.protect = patch.object(toolbox.core, "dpapi_protect", side_effect=lambda value: b"enc:" + value.encode("utf-8"))
        self.unprotect = patch.object(
            toolbox.core,
            "dpapi_unprotect",
            side_effect=lambda value: value.removeprefix(b"enc:").decode("utf-8"),
        )
        self.protect.start()
        self.unprotect.start()

    def tearDown(self):
        self.protect.stop()
        self.unprotect.stop()
        self.temp.cleanup()

    def test_save_list_delete_never_return_or_store_plaintext(self):
        secret = "do-not-leak-password"
        saved = toolbox.save_mail_import(
            {"id": "primary", "email": "a@gmail.com", "password": secret},
            path=self.path,
        )
        self.assertNotIn(secret, json.dumps(saved))
        self.assertNotIn(secret, self.path.read_text(encoding="utf-8"))
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["schema"], toolbox.EMAIL_STORE_SCHEMA)
        self.assertEqual(list(stored["accounts"]), ["primary"])
        listed = toolbox.list_mail_accounts(path=self.path)
        self.assertNotIn(secret, json.dumps(listed))
        self.assertTrue(listed[0]["credential_status"]["password"])
        self.assertTrue(toolbox.delete_mail_account("primary", path=self.path))
        self.assertEqual(toolbox.list_mail_accounts(path=self.path), [])

    def test_replace_and_corrupt_store_handling(self):
        toolbox.save_mail_import({"id": "one", "email": "a@gmail.com", "password": "x"}, path=self.path)
        toolbox.save_mail_import(
            {"id": "two", "email": "b@gmail.com", "password": "y"},
            path=self.path,
            replace=True,
        )
        self.assertEqual([item["id"] for item in toolbox.list_mail_accounts(path=self.path)], ["two"])
        self.path.write_text('{"schema":"wrong","accounts":{}}', encoding="utf-8")
        with self.assertRaises(toolbox.CredentialStoreError):
            toolbox.list_mail_accounts(path=self.path)

    def test_edit_retains_omitted_secret_and_updates_safe_metadata(self):
        secret = "retained-app-password"
        toolbox.save_mail_import(
            {"id": "primary", "email": "a@gmail.com", "password": secret, "label": "旧名称"},
            path=self.path,
        )
        updated = toolbox.save_mail_import_selection(
            json.dumps({
                "id": "primary",
                "email": "a@gmail.com",
                "label": "工作邮箱",
                "imap_host": "imap.gmail.com",
                "imap_port": 993,
                "security": "ssl",
                "auth_method": "password",
                "mailbox": "INBOX",
            }),
            [0],
            path=self.path,
        )[0]
        self.assertEqual(updated["label"], "工作邮箱")
        self.assertTrue(updated["credential_status"]["password"])
        self.assertEqual(toolbox._load_email_account("primary", path=self.path)["password"], secret)
        self.assertNotIn(secret, self.path.read_text(encoding="utf-8"))

    def test_manual_fetch_persists_safe_health_summary_for_success_and_error(self):
        toolbox.save_mail_import(
            {"id": "primary", "email": "a@gmail.com", "password": "app-password"},
            path=self.path,
        )
        with (
            patch.object(toolbox, "fetch_email_messages", return_value=[{"uid": "1"}]),
            patch.object(toolbox.core, "now_iso", return_value="2026-08-12T12:00:00+00:00"),
        ):
            toolbox.fetch_saved_email_messages("primary", path=self.path)
        healthy = toolbox.list_mail_accounts(path=self.path)[0]["credential_status"]["health"]
        self.assertEqual(healthy, {
            "status": "healthy",
            "checkedAt": "2026-08-12T12:00:00+00:00",
            "messageCount": 1,
        })
        with (
            patch.object(toolbox, "fetch_email_messages", side_effect=toolbox.MailAccessError("offline")),
            patch.object(toolbox.core, "now_iso", return_value="2026-08-12T12:01:00+00:00"),
            self.assertRaises(toolbox.MailAccessError),
        ):
            toolbox.fetch_saved_email_messages("primary", path=self.path)
        failed = toolbox.list_mail_accounts(path=self.path)[0]["credential_status"]["health"]
        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["checkedAt"], "2026-08-12T12:01:00+00:00")
        self.assertNotIn("app-password", json.dumps(failed))

    def test_fetch_health_write_does_not_resurrect_deleted_account(self):
        toolbox.save_mail_import(
            {"id": "primary", "email": "a@gmail.com", "password": "old-password"},
            path=self.path,
        )

        def delete_during_fetch(_account, **_options):
            self.assertTrue(toolbox.delete_mail_account("primary", path=self.path))
            return []

        with patch.object(toolbox, "fetch_email_messages", side_effect=delete_during_fetch):
            toolbox.fetch_saved_email_messages("primary", path=self.path)
        self.assertEqual(toolbox.list_mail_accounts(path=self.path), [])

    def test_fetch_health_write_never_attaches_result_to_changed_credentials(self):
        toolbox.save_mail_import(
            {"id": "primary", "email": "a@gmail.com", "password": "old-password"},
            path=self.path,
        )

        def edit_during_fetch(_account, **_options):
            toolbox.update_email_account("primary", {"password": "new-password"}, path=self.path)
            return [{"uid": "1"}]

        with patch.object(toolbox, "fetch_email_messages", side_effect=edit_during_fetch):
            toolbox.fetch_saved_email_messages("primary", path=self.path)
        current = toolbox._load_email_account("primary", path=self.path)
        self.assertEqual(current["password"], "new-password")
        self.assertNotIn("_health", current)

    def test_fetch_health_write_merges_with_concurrent_metadata_edit(self):
        toolbox.save_mail_import(
            {"id": "primary", "email": "a@gmail.com", "password": "old-password"},
            path=self.path,
        )

        def edit_during_fetch(_account, **_options):
            toolbox.update_email_account("primary", {"label": "工作邮箱"}, path=self.path)
            return [{"uid": "1"}]

        with patch.object(toolbox, "fetch_email_messages", side_effect=edit_during_fetch):
            toolbox.fetch_saved_email_messages("primary", path=self.path)
        current = toolbox._load_email_account("primary", path=self.path)
        self.assertEqual(current["label"], "工作邮箱")
        self.assertEqual(current["password"], "old-password")
        self.assertEqual(current["_health"]["status"], "healthy")

    def test_rotated_refresh_token_uses_revision_cas(self):
        toolbox.save_mail_import(
            {
                "id": "primary",
                "email": "a@gmail.com",
                "refresh_token": "old-refresh",
                "client_id": "client-id",
            },
            path=self.path,
        )
        account = toolbox._load_email_account("primary", path=self.path)

        def opener(_request, timeout):
            toolbox.update_email_account("primary", {"refresh_token": "user-edited-refresh"}, path=self.path)
            return FakeResponse(
                {
                    "access_token": f"access-{timeout}",
                    "refresh_token": "stale-rotated-refresh",
                    "expires_in": 3600,
                }
            )

        token = toolbox._resolve_access_token(account, opener=opener, timeout=4, path=self.path)
        self.assertEqual(token, "access-4.0")
        current = toolbox._load_email_account("primary", path=self.path)
        self.assertEqual(current["refresh_token"], "user-edited-refresh")
        with toolbox._OAUTH_CACHE_LOCK:
            self.assertEqual(toolbox._OAUTH_ACCESS_TOKEN_CACHE, {})

    def test_rotated_refresh_token_does_not_write_into_deleted_and_recreated_id(self):
        original = {
            "id": "primary",
            "email": "a@gmail.com",
            "refresh_token": "old-refresh",
            "client_id": "client-id",
        }
        toolbox.save_mail_import(original, path=self.path)
        account = toolbox._load_email_account("primary", path=self.path)

        def opener(_request, timeout):
            self.assertTrue(toolbox.delete_mail_account("primary", path=self.path))
            toolbox.save_mail_import(
                {**original, "refresh_token": "recreated-refresh"},
                path=self.path,
            )
            return FakeResponse(
                {
                    "access_token": f"access-{timeout}",
                    "refresh_token": "stale-rotated-refresh",
                    "expires_in": 3600,
                }
            )

        toolbox._resolve_access_token(account, opener=opener, timeout=4, path=self.path)
        current = toolbox._load_email_account("primary", path=self.path)
        self.assertEqual(current["refresh_token"], "recreated-refresh")

    def test_rotated_refresh_token_advances_revision_and_is_cached_under_new_fingerprint(self):
        toolbox.save_mail_import(
            {
                "id": "primary",
                "email": "a@gmail.com",
                "refresh_token": "old-refresh",
                "client_id": "client-id",
            },
            path=self.path,
        )
        account = toolbox._load_email_account("primary", path=self.path)
        old_revision = account["_credential_revision"]
        calls = []

        def opener(_request, timeout):
            calls.append(timeout)
            return FakeResponse(
                {
                    "access_token": "cached-access",
                    "refresh_token": "rotated-refresh",
                    "expires_in": 3600,
                }
            )

        self.assertEqual(
            toolbox._resolve_access_token(account, opener=opener, timeout=4, path=self.path),
            "cached-access",
        )
        current = toolbox._load_email_account("primary", path=self.path)
        self.assertNotEqual(current["_credential_revision"], old_revision)
        self.assertEqual(current["refresh_token"], "rotated-refresh")
        self.assertEqual(
            toolbox._resolve_access_token(current, opener=opener, timeout=4, path=self.path),
            "cached-access",
        )
        self.assertEqual(calls, [4.0])


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Length": str(len(self.payload))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, maximum: int) -> bytes:
        return self.payload[:maximum]


class OAuthTests(unittest.TestCase):
    def setUp(self):
        with toolbox._OAUTH_CACHE_LOCK:
            toolbox._OAUTH_ACCESS_TOKEN_CACHE.clear()
            toolbox._OAUTH_REFRESH_LOCKS.clear()

    def test_google_and_microsoft_refresh_requests(self):
        cases = (
            ("google", "user@gmail.com", "oauth2.googleapis.com"),
            ("microsoft", "user@outlook.com", "login.microsoftonline.com"),
        )
        for provider, address, expected_host in cases:
            captured = {}

            def opener(request, timeout):
                captured["request"] = request
                captured["timeout"] = timeout
                return FakeResponse({"access_token": "ephemeral-access", "expires_in": 3600})

            account = toolbox.parse_email_accounts(
                {
                    "email": address,
                    "provider": provider,
                    "refresh_token": "refresh-secret",
                    "client_id": "client-id",
                    "client_secret": "client-secret",
                }
            )[0]
            with self.subTest(provider=provider):
                token, metadata = toolbox._refresh_oauth_access_token(account, opener=opener, timeout=7)
                self.assertEqual(token, "ephemeral-access")
                self.assertEqual(captured["timeout"], 7)
                self.assertEqual(urllib.parse.urlsplit(captured["request"].full_url).hostname, expected_host)
                form = urllib.parse.parse_qs(captured["request"].data.decode("ascii"))
                self.assertEqual(form["grant_type"], ["refresh_token"])
                self.assertEqual(form["refresh_token"], ["refresh-secret"])
                self.assertEqual(form["client_id"], ["client-id"])
                self.assertEqual(form["client_secret"], ["client-secret"])
                self.assertIn("expires_at", metadata)

    def test_refresh_errors_do_not_leak_secret(self):
        account = toolbox.parse_email_accounts(
            {"email": "a@gmail.com", "refresh_token": "refresh-secret", "client_id": "client-id"}
        )[0]

        def failing_opener(_request, timeout):
            raise OSError(f"network rejected refresh-secret after {timeout}")

        with self.assertRaises(toolbox.MailAccessError) as raised:
            toolbox._refresh_oauth_access_token(account, opener=failing_opener)
        self.assertNotIn("refresh-secret", str(raised.exception))
        self.assertNotIn("refresh-secret", "".join(traceback.format_exception(raised.exception)))

    def test_access_token_refresh_is_cached_and_rotated_token_is_retained(self):
        calls = []

        def opener(_request, timeout):
            calls.append(timeout)
            return FakeResponse(
                {
                    "access_token": "cached-access",
                    "refresh_token": "rotated-refresh",
                    "expires_in": 3600,
                }
            )

        account = toolbox.parse_email_accounts(
            {"email": "cache@gmail.com", "refresh_token": "old-refresh", "client_id": "client-id"}
        )[0]
        self.assertEqual(toolbox._resolve_access_token(account, opener=opener, timeout=4), "cached-access")
        self.assertEqual(toolbox._resolve_access_token(account, opener=opener, timeout=4), "cached-access")
        self.assertEqual(calls, [4])
        self.assertEqual(account["refresh_token"], "rotated-refresh")

    def test_cache_key_is_bound_to_the_full_refresh_credential_fingerprint(self):
        calls = []

        def opener(request, timeout):
            form = urllib.parse.parse_qs(request.data.decode("ascii"))
            calls.append((form["client_id"][0], form["refresh_token"][0], timeout))
            return FakeResponse({"access_token": f"access-{len(calls)}", "expires_in": 3600})

        base = {
            "id": "shared-id",
            "email": "cache@gmail.com",
            "provider": "google",
            "client_id": "client-one",
            "refresh_token": "refresh-one",
        }
        first = toolbox.parse_email_accounts(base)[0]
        second = toolbox.parse_email_accounts(
            {**base, "client_id": "client-two", "refresh_token": "refresh-two"}
        )[0]
        self.assertEqual(toolbox._resolve_access_token(first, opener=opener, timeout=4), "access-1")
        self.assertEqual(toolbox._resolve_access_token(second, opener=opener, timeout=4), "access-2")
        self.assertEqual(
            calls,
            [("client-one", "refresh-one", 4.0), ("client-two", "refresh-two", 4.0)],
        )

    def test_store_mutations_clear_oauth_cache_and_idle_refresh_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mail-secrets.json"
            with (
                patch.object(toolbox.core, "dpapi_protect", side_effect=lambda value: b"enc:" + value.encode("utf-8")),
                patch.object(
                    toolbox.core,
                    "dpapi_unprotect",
                    side_effect=lambda value: value.removeprefix(b"enc:").decode("utf-8"),
                ),
            ):
                operations = (
                    lambda: toolbox.save_mail_import(
                        {
                            "id": "primary",
                            "email": "a@gmail.com",
                            "refresh_token": "refresh-one",
                            "client_id": "client-id",
                        },
                        path=path,
                    ),
                    lambda: toolbox.update_email_account("primary", {"label": "edited"}, path=path),
                    lambda: toolbox.save_mail_import(
                        {
                            "id": "replacement",
                            "email": "b@gmail.com",
                            "refresh_token": "refresh-two",
                            "client_id": "client-id",
                        },
                        path=path,
                        replace=True,
                    ),
                    lambda: toolbox.delete_mail_account("replacement", path=path),
                )
                for operation in operations:
                    with toolbox._OAUTH_CACHE_LOCK:
                        toolbox._OAUTH_ACCESS_TOKEN_CACHE["stale"] = (float("inf"), "secret-access")
                        toolbox._OAUTH_REFRESH_LOCKS["stale"] = toolbox._OAuthRefreshState()
                    operation()
                    with toolbox._OAUTH_CACHE_LOCK:
                        self.assertEqual(toolbox._OAUTH_ACCESS_TOKEN_CACHE, {})
                        self.assertEqual(toolbox._OAUTH_REFRESH_LOCKS, {})

    def test_refresh_lock_is_removed_after_success_and_error(self):
        account = toolbox.parse_email_accounts(
            {"email": "cleanup@gmail.com", "refresh_token": "refresh-secret", "client_id": "client-id"}
        )[0]
        toolbox._resolve_access_token(
            account,
            opener=lambda _request, timeout: FakeResponse(
                {"access_token": f"access-{timeout}", "expires_in": 3600}
            ),
            timeout=4,
        )
        with toolbox._OAUTH_CACHE_LOCK:
            self.assertEqual(toolbox._OAUTH_REFRESH_LOCKS, {})

        failing = toolbox.parse_email_accounts(
            {"email": "failure@gmail.com", "refresh_token": "refresh-secret", "client_id": "client-id"}
        )[0]
        with self.assertRaises(toolbox.MailAccessError):
            toolbox._resolve_access_token(
                failing,
                opener=lambda _request, timeout: (_ for _ in ()).throw(OSError(f"secret refresh-secret {timeout}")),
                timeout=4,
            )
        with toolbox._OAUTH_CACHE_LOCK:
            self.assertEqual(toolbox._OAUTH_REFRESH_LOCKS, {})

    def test_rejects_cross_provider_or_non_https_token_endpoint(self):
        endpoints = (
            "http:" + "//oauth2.googleapis.com/token",
            "https:" + "//login.microsoftonline.com/common/oauth2/v2.0/token",
            "https:" + "//oauth2.googleapis.com:444/token",
            "https:" + "//oauth2.googleapis.com:invalid/token",
        )
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint), self.assertRaises(toolbox.ValidationError):
                toolbox.parse_email_accounts(
                    {
                        "email": "a@gmail.com",
                        "refresh_token": "r",
                        "client_id": "c",
                        "token_endpoint": endpoint,
                    }
                )


class FakeIMAP:
    def __init__(self, messages: dict[bytes, tuple[bytes, str]], *, login_error: Exception | None = None):
        self.messages = messages
        self.login_error = login_error
        self.calls = []
        self.auth_payload = b""

    def starttls(self, **kwargs):
        self.calls.append(("starttls", kwargs))
        return "OK", []

    def login(self, email, password):
        self.calls.append(("login", email, password))
        if self.login_error:
            raise self.login_error
        return "OK", []

    def authenticate(self, mechanism, callback):
        self.auth_payload = callback(None)
        self.calls.append(("authenticate", mechanism))
        return b"OK", []

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [b"2"]

    def uid(self, command, *args):
        self.calls.append(("uid", command, args))
        if command == "search":
            return "OK", [b" ".join(self.messages)]
        identifier = args[0]
        raw, flags = self.messages[identifier]
        metadata = f"UID {identifier.decode()} FLAGS ({flags})".encode("ascii")
        return "OK", [(metadata, raw)]

    def close(self):
        self.calls.append(("close",))

    def logout(self):
        self.calls.append(("logout",))


def make_message(subject: str, body: str) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "Security <security@example.com>"
    message["To"] = "User <user@example.com>"
    message["Date"] = "Mon, 10 Aug 2026 12:30:00 +0000"
    message.set_content(body)
    return message.as_bytes()


class MailFetchTests(unittest.TestCase):
    def test_saved_health_check_authenticates_without_searching_or_fetching_mail(self):
        fake = FakeIMAP({b"1": (make_message("Code", "123456"), "")})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mail.json"
            with (
                patch.object(toolbox.core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
                patch.object(toolbox.core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            ):
                toolbox.save_mail_import(
                    {"id": "primary", "email": "user@gmail.com", "password": "app-password"},
                    path=path,
                )
                result = toolbox.check_saved_email_health(
                    "primary",
                    path=path,
                    timeout=5,
                    imap_ssl_factory=lambda *_args, **_kwargs: fake,
                )
                public = toolbox.list_mail_accounts(path=path)[0]

        self.assertTrue(result["healthy"])
        self.assertTrue(result["saved"])
        self.assertEqual(public["credential_status"]["health"]["status"], "healthy")
        self.assertIn(("select", "INBOX", True), fake.calls)
        self.assertFalse(any(call[0] == "uid" for call in fake.calls))

    def test_saved_health_check_classifies_login_failure_without_leaking_secret(self):
        fake = FakeIMAP({}, login_error=RuntimeError("echoed secret-password"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mail.json"
            with (
                patch.object(toolbox.core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
                patch.object(toolbox.core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            ):
                toolbox.save_mail_import(
                    {"id": "primary", "email": "user@gmail.com", "password": "secret-password"},
                    path=path,
                )
                result = toolbox.check_saved_email_health(
                    "primary",
                    path=path,
                    imap_ssl_factory=lambda *_args, **_kwargs: fake,
                )
                public = toolbox.list_mail_accounts(path=path)[0]
                stored = path.read_text(encoding="utf-8")

        self.assertEqual(result["status"], "auth_error")
        self.assertFalse(result["healthy"])
        self.assertEqual(public["credential_status"]["health"]["status"], "auth_error")
        self.assertNotIn("secret-password", json.dumps(result))
        self.assertNotIn("secret-password", stored)

    def test_stale_mail_accounts_are_due_once_per_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mail.json"
            with (
                patch.object(toolbox.core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
                patch.object(toolbox.core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            ):
                toolbox.save_mail_import(
                    {"id": "primary", "email": "user@gmail.com", "password": "app-password"},
                    path=path,
                )
                now = toolbox.datetime(2026, 8, 13, 12, 0, tzinfo=toolbox.timezone.utc)
                self.assertEqual(toolbox.stale_mail_account_ids(86_400, path=path, now=now), ["primary"])
                account = toolbox._load_email_account("primary", path=path)
                account["_health"] = {
                    "status": "healthy",
                    "checkedAt": "2026-08-13T11:00:00+00:00",
                    "messageCount": 0,
                }
                toolbox.save_email_accounts([account], path=path)
                self.assertEqual(toolbox.stale_mail_account_ids(86_400, path=path, now=now), [])

    def test_password_fetch_is_readonly_recent_and_uses_body_peek(self):
        fake = FakeIMAP(
            {
                b"1": (make_message("Old", "Already seen 111111"), "\\Seen"),
                b"2": (make_message("Your verification code", "Verification code is 654321"), ""),
            }
        )

        def factory(host, port, **kwargs):
            self.assertEqual((host, port), ("imap.gmail.com", 993))
            self.assertEqual(kwargs["timeout"], 5)
            return fake

        result = toolbox.fetch_email_messages(
            {"email": "user@gmail.com", "password": "app-password"},
            unread_only=True,
            limit=2,
            timeout=5,
            imap_ssl_factory=factory,
        )
        self.assertEqual([item["uid"] for item in result], ["2", "1"])
        self.assertIn("654321", result[0]["verification_codes"])
        self.assertTrue(result[0]["unread"])
        self.assertFalse(result[1]["unread"])
        self.assertIn(("select", "INBOX", True), fake.calls)
        search_call = next(call for call in fake.calls if call[:2] == ("uid", "search"))
        self.assertEqual(search_call[2][-1], "UNSEEN")
        fetch_calls = [call for call in fake.calls if call[:2] == ("uid", "fetch")]
        self.assertTrue(all("BODY.PEEK" in call[2][-1] for call in fetch_calls))
        self.assertNotIn("app-password", json.dumps(result))

    def test_official_oauth_access_token_xoauth2(self):
        fake = FakeIMAP({b"7": (make_message("Login code", "OTP: A1B2C3"), "")})

        def factory(host, port, **kwargs):
            self.assertEqual((host, port), ("imap.gmail.com", 993))
            return fake

        result = toolbox.fetch_email_messages(
            {
                "email": "user@gmail.com",
                "access_token": "short-lived-token",
            },
            imap_ssl_factory=factory,
        )
        self.assertIn(b"auth=Bearer short-lived-token", fake.auth_payload)
        self.assertIn("A1B2C3", result[0]["verification_codes"])
        self.assertNotIn("short-lived-token", json.dumps(result))

    def test_login_error_message_does_not_leak_password(self):
        fake = FakeIMAP({}, login_error=RuntimeError("server echoed very-secret-password"))
        with self.assertRaises(toolbox.MailAccessError) as raised:
            toolbox.fetch_email_messages(
                {"email": "user@gmail.com", "password": "very-secret-password"},
                imap_ssl_factory=lambda *_args, **_kwargs: fake,
            )
        self.assertNotIn("very-secret-password", str(raised.exception))
        self.assertNotIn("very-secret-password", "".join(traceback.format_exception(raised.exception)))

    def test_fetch_limits_are_validated_before_network(self):
        with self.assertRaises(toolbox.ValidationError):
            toolbox.fetch_email_messages({"email": "a@gmail.com", "password": "x"}, limit=101)
        with self.assertRaises(toolbox.ValidationError):
            toolbox.fetch_email_messages(
                {"email": "a@gmail.com", "password": "x"},
                max_message_bytes=100,
            )


class StorageHealthTests(unittest.TestCase):
    def test_storage_health_validates_envelopes_without_dpapi_decryption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            totp = root / "totp.json"
            email = root / "email.json"
            core.atomic_write_json(
                totp,
                {
                    "schema": toolbox.TOTP_STORE_SCHEMA,
                    "items": {"totp_0123456789abcdef": base64.b64encode(b"encrypted").decode()},
                },
            )
            core.atomic_write_json(
                email,
                {
                    "schema": toolbox.EMAIL_STORE_SCHEMA,
                    "accounts": {"mail_1": base64.b64encode(b"encrypted").decode()},
                },
            )
            with patch.object(core, "dpapi_unprotect") as decrypt:
                result = toolbox.storage_health(totp_path=totp, email_path=email)
            self.assertTrue(result["healthy"])
            self.assertFalse(result["credentialsRead"])
            decrypt.assert_not_called()

    def test_storage_health_reports_corrupt_record_without_deleting_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            totp = root / "totp.json"
            email = root / "email.json"
            totp.write_text('{"schema":"wrong","items":{}}', encoding="utf-8")
            core.atomic_write_json(email, {"schema": toolbox.EMAIL_STORE_SCHEMA, "accounts": {}})
            before = totp.read_bytes()
            result = toolbox.storage_health(totp_path=totp, email_path=email)
            self.assertFalse(result["healthy"])
            self.assertEqual(result["stores"]["totp"]["status"], "error")
            self.assertEqual(totp.read_bytes(), before)

    def test_credential_store_readers_reject_oversized_files_before_json_decode(self):
        oversized = toolbox.MAX_IMPORT_BYTES * 8 + 1
        fake_stat = type("FakeStat", (), {"st_size": oversized})()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            totp = root / "totp.json"
            email = root / "email.json"
            totp.write_text("{}", encoding="utf-8")
            email.write_text("{}", encoding="utf-8")
            with patch.object(Path, "stat", return_value=fake_stat):
                with self.assertRaises(toolbox.CredentialStoreError):
                    toolbox._read_totp_store(totp)
                with self.assertRaises(toolbox.CredentialStoreError):
                    toolbox._read_email_store(email)


if __name__ == "__main__":
    unittest.main()
