import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import agent_manager.core as core
from agent_manager.accounts.portability import portable_account_metadata
from tests.accounts.test_auth_export_formats import auth, jwt, cockpit_full_token_mirror


def official_auth():
    result=auth()
    token=jwt(client_id=core.CODEX_OAUTH_CLIENT_ID, iss="https://auth.openai.com").rsplit(".",1)[0]+".test-signature"
    result["tokens"].update(id_token=token,access_token=token)
    return result


class ExportIntegrationTests(unittest.TestCase):
    def test_export_prefers_new_live_bundle_and_excludes_machine_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            old=official_auth(); old["last_refresh"]="2026-09-01T00:00:00Z"
            new=official_auth(); new.update(auth_mode="chatgpt",last_refresh="2026-09-08T00:00:00Z",openai_base_url="https://old-relay.example.test/v1")
            new["tokens"]["refresh_token"]="new-refresh-token"
            (root/"auth.json").write_text(json.dumps(new),encoding="utf-8")
            account={"id":"same","email":"synthetic@example.test","authMode":"chatgpt","sourceType":"codex_auth","models":["gpt-6-astra"],"groupId":"official"}
            with (
                patch.object(core,"CODEX_HOME",root),
                patch.object(core,"load_settings",return_value={"accounts":[account]}),
                patch.object(core,"_load_account_snapshot",return_value={}),
                patch.object(core,"_decode_snapshot_files",return_value={"auth.json":json.dumps(old).encode(),"cap_sid":b"machine-private"}),
                patch.object(core,"_account_matches_identity",return_value=True),
            ):
                exported=core.export_codex_account("same")
            self.assertEqual(exported["tokens"]["refresh_token"],"new-refresh-token")
            self.assertIsNotNone(cockpit_full_token_mirror(exported))
            self.assertNotIn("capSidBase64",exported)
            self.assertNotIn("openai_base_url",exported)
            self.assertNotIn("authJson",exported)
            self.assertEqual(portable_account_metadata(exported)["models"],["gpt-6-astra"])

    def test_new_namespace_metadata_survives_local_batch_preview(self):
        from agent_manager.accounts.portability import build_portable_account_export
        document=build_portable_account_export(official_auth(),{"label":"Shared official","models":["gpt-6-astra"],"planDisplay":{"variant":"pro20x","confirmedAt":"2026-09-01","expiresAt":"2030-01-01"}})
        docs=core._batch_documents({"items":[document]})
        self.assertEqual(docs[0]["label"],"Shared official")
        self.assertEqual(docs[0]["models"],["gpt-6-astra"])
        self.assertNotIn("planDisplay",docs[0])
        self.assertEqual(core._batch_auth_identity(docs[0], {})[1],"codex_auth")

    def test_conflicting_official_jwt_pair_rejected_at_import_boundary(self):
        document=official_auth();document["tokens"]["access_token"]=jwt(user="other-user",client_id=core.CODEX_OAUTH_CLIENT_ID,iss="https://auth.openai.com")
        with self.assertRaises(core.ManagerError):
            core._normalize_import_auth_payload(document)


if __name__=="__main__":
    unittest.main()
