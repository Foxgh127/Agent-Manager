"""Synthetic/offline portability contract tests; no manager state or network.

The small Cockpit mirror models only the verified full-token extraction path
in codex_account_import.rs:3047–3105 and object/array dispatch at :1622–1645.
It is not a server-validity test or a replacement for executing upstream Rust.
"""
import base64
import copy
import json
import unittest

from account_portability import (
    METADATA_KEY, PORTABLE_FORMAT, PortableAccountError,
    build_portable_account_export, normalize_portable_oauth_accounts,
    normalize_portable_oauth_auth, portable_account_metadata,
)


def jwt(user="user-test", account="workspace-test", **extra):
    claims = {"sub": user, "iss": "https://auth.example.test", "exp": 2100000000,
              "email": "synthetic@example.test", "https://api.openai.com/auth": {
                  "chatgpt_account_id": account, "chatgpt_user_id": user,
              }, **extra}
    encode = lambda obj: base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{encode({'alg': 'none'})}.{encode(claims)}.synthetic"


def auth():
    return {"tokens": {"id_token": jwt(), "access_token": jwt(),
                       "refresh_token": "synthetic-refresh-current", "account_id": "workspace-test"},
            "last_refresh": "2030-01-01T00:00:00Z", "OPENAI_API_KEY": None}


def cockpit_full_token_mirror(document):
    """Verified supported subset; namespaced metadata is never traversed."""
    if isinstance(document, list):
        return [item for item in (cockpit_full_token_mirror(row) for row in document) if item]
    if not isinstance(document, dict):
        return None
    for candidate in (document, document.get("tokens")):
        if not isinstance(candidate, dict):
            continue
        pick = lambda snake, camel: candidate.get(snake) or candidate.get(camel)
        if pick("id_token", "idToken") and pick("access_token", "accessToken"):
            return {"id_token": pick("id_token", "idToken"), "access_token": pick("access_token", "accessToken"),
                    "refresh_token": pick("refresh_token", "refreshToken"),
                    "account_id": pick("account_id", "accountId") or document.get("account_id") or document.get("accountId")}
    return None


class AccountPortabilityV98Tests(unittest.TestCase):
    def test_old_envelope_fails_cockpit_subset_but_new_export_passes(self):
        old = {"format": PORTABLE_FORMAT, "version": 1, "authJson": auth(), "label": "Portable account"}
        self.assertIsNone(cockpit_full_token_mirror(old))
        exported = build_portable_account_export(old)
        self.assertEqual(cockpit_full_token_mirror(exported), auth()["tokens"])
        self.assertEqual(exported[METADATA_KEY]["label"], "Portable account")
        self.assertIsNone(exported["OPENAI_API_KEY"])
        self.assertEqual(exported["auth_mode"], "chatgpt")

    def test_export_whitelists_credentials_and_metadata(self):
        source = auth()
        source.update(config={"model_provider": "wrong"}, base_url="https://wrong.test", profile="wrong",
                      cap_sid="machine-secret", capSidBase64="machine-secret", dpapi="protected-secret",
                      keyring={"password": "secret"}, agent_identity={"private_key": "secret"})
        source["tokens"]["password"] = "secret"
        metadata = {**source, "label": "Account", "group": {"id": "official", "name": "Official", "apiKey": "secret"},
                    "models": ["model-test", {"id": "wrong", "apiKey": "secret"}],
                    "usage": {"remoteValid": True}, "refreshState": "ready", "proxyEnabled": True,
                    "planDisplay": {"variant": "pro5x", "confirmedAt": "2030-01-01T00:00:00Z", "private_key": "secret"}}
        exported = build_portable_account_export(source, metadata)
        self.assertEqual(set(exported), {"tokens", "auth_mode", "OPENAI_API_KEY", "last_refresh", METADATA_KEY})
        self.assertNotIn("secret", json.dumps(exported))
        self.assertEqual(exported[METADATA_KEY]["models"], ["model-test"])
        self.assertEqual(exported[METADATA_KEY]["group"], {"id": "official", "name": "Official"})

    def test_round_trip_keeps_bundle_exactly_and_does_not_mutate_input(self):
        source = auth()
        before = copy.deepcopy(source)
        first = build_portable_account_export(source, {"label": "Round trip", "models": ["model-test"]})
        second = build_portable_account_export(json.dumps(first))
        self.assertEqual(first, second)
        self.assertEqual(before, source)
        self.assertEqual(second["tokens"]["refresh_token"], "synthetic-refresh-current")

    def test_cockpit_record_array_and_internal_id(self):
        row = {"id": "cockpit-local-record-not-workspace", "account_id": "workspace-test", "email": "hint@example.test",
               "tokens": {key: value for key, value in auth()["tokens"].items() if key != "account_id"},
               "quota": {"remaining": 100}, "api_base_url": "http://local.test"}
        normalized = normalize_portable_oauth_accounts(json.dumps([row, row]))
        self.assertEqual(len(cockpit_full_token_mirror(normalized)), 2)
        self.assertEqual(normalized[0]["tokens"], auth()["tokens"])
        self.assertNotIn("quota", json.dumps(normalized))

    def test_supported_flat_camelcase_aliases(self):
        original = auth()["tokens"]
        row = {"idToken": original["id_token"], "accessToken": original["access_token"],
               "refreshToken": original["refresh_token"], "accountId": original["account_id"]}
        self.assertEqual(normalize_portable_oauth_auth(row)["tokens"], original)
        self.assertEqual(cockpit_full_token_mirror(row), original)

    def test_conflicting_credential_aliases_fail_without_echoing_secrets(self):
        source = auth()
        source["tokens"]["refreshToken"] = "other-secret-refresh"
        with self.assertRaises(PortableAccountError) as caught:
            build_portable_account_export(source)
        self.assertNotIn("other-secret", str(caught.exception))

    def test_conflicting_account_hint_fails(self):
        for location in ("root", "token"):
            with self.subTest(location=location):
                source = auth()
                if location == "root":
                    source["account_id"] = "other-workspace"
                else:
                    source["tokens"]["account_id"] = "other-workspace"
                with self.assertRaises(PortableAccountError):
                    build_portable_account_export(source)

    def test_different_user_same_workspace_fails(self):
        source = auth()
        source["tokens"]["id_token"] = jwt(user="other-user")
        with self.assertRaises(PortableAccountError):
            build_portable_account_export(source)

    def test_same_user_different_organization_id_token_is_allowed(self):
        source = auth()
        source["tokens"]["id_token"] = jwt(account="previous-workspace")
        result = build_portable_account_export(source)
        self.assertEqual(result["tokens"]["account_id"], "workspace-test")

    def test_workspace_mismatch_without_shared_user_evidence_fails(self):
        source = auth()
        source["tokens"]["id_token"] = jwt(user="", account="previous-workspace")
        with self.assertRaises(PortableAccountError):
            build_portable_account_export(source)

    def test_infers_workspace_only_from_credentials(self):
        source = auth()
        del source["tokens"]["account_id"]
        self.assertEqual(build_portable_account_export(source)["tokens"]["account_id"], "workspace-test")

    def test_invalid_or_duplicate_jwt_payload_fails(self):
        for token in ("not-a-jwt", "header.@@@.signature", "header.W10.signature"):
            with self.subTest(token=token):
                source = auth()
                source["tokens"]["access_token"] = token
                with self.assertRaises(PortableAccountError):
                    build_portable_account_export(source)

    def test_missing_refresh_preserved_without_validity_claim(self):
        source = auth()
        del source["tokens"]["refresh_token"]
        result = build_portable_account_export(source, {"remoteValid": True, "refreshCapable": True, "refreshState": "ready"})
        self.assertIsNone(result["tokens"]["refresh_token"])
        self.assertEqual(set(result[METADATA_KEY]), {"format", "version"})

    def test_mixed_key_or_unsupported_mode_fails(self):
        for patch in ({"OPENAI_API_KEY": "synthetic-api-key"}, {"auth_mode": "apikey"}, {"auth_mode": "agent_identity"}):
            with self.subTest(patch=patch):
                with self.assertRaises(PortableAccountError):
                    build_portable_account_export({**auth(), **patch})

    def test_web_session_capability_is_not_laundered_to_official_oauth(self):
        for source in (
            {"format": PORTABLE_FORMAT, "authJson": auth(), "sourceType": "web_session"},
            {**auth(), "session_meta": {"credentialCapability": "quota_only"}},
        ):
            with self.assertRaises(PortableAccountError):
                build_portable_account_export(source)

    def test_batch_rejects_bad_item_instead_of_silent_loss(self):
        for rows in ([], [auth(), {"tokens": {}}], [auth(), None]):
            with self.subTest(rows=len(rows)):
                with self.assertRaises(PortableAccountError):
                    normalize_portable_oauth_accounts(rows)

    def test_duplicate_json_fields_and_dual_envelope_rejected(self):
        with self.assertRaises(PortableAccountError):
            normalize_portable_oauth_auth('{"tokens": {}, "tokens": {}}')
        with self.assertRaises(PortableAccountError):
            normalize_portable_oauth_auth({"format": PORTABLE_FORMAT, "authJson": auth(), "tokens": auth()["tokens"]})

    def test_metadata_is_namespaced_versioned_and_presentation_only(self):
        self.assertEqual(portable_account_metadata({"label": "Unrecognized"}), {})
        raw = {METADATA_KEY: {"format": PORTABLE_FORMAT, "version": 2, "label": "Accepted", "originalId": "other-local-account",
                              "models": ["a", "a"], "sourceType": "codex_auth", "usage": {"verified": True}}}
        self.assertEqual(portable_account_metadata(raw), {"label": "Accepted", "models": ["a"]})


    def test_explicit_oauth_can_export_rotated_opaque_access_with_id_and_refresh(self):
        source=auth()
        source["auth_mode"]="chatgpt"
        source["tokens"]["access_token"]="opaque-rotated-access"
        result=normalize_portable_oauth_auth(source)
        self.assertEqual(result["tokens"]["access_token"],"opaque-rotated-access")
        self.assertEqual(result["tokens"]["account_id"],"workspace-test")
        del source["auth_mode"]
        with self.assertRaises(PortableAccountError):
            normalize_portable_oauth_auth(source)


if __name__ == "__main__":
    unittest.main()
