import io
import json
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

import agent_manager.application as app
import agent_manager.core as core


class FakeCallbackServer:
    def __init__(self):
        self.closed = threading.Event()

    def serve_forever(self, poll_interval=0.1):
        self.closed.wait(5)

    def shutdown(self):
        self.closed.set()

    def server_close(self):
        self.closed.set()


class OAuthLifecycleTests(unittest.TestCase):
    TOKENS = {
        "id_token": "id-token",
        "access_token": "access-token",
        "refresh_token": "refresh-token",
    }

    def make_oauth(self):
        oauth = app.OAuthDeviceLogin(timeout_seconds=30)
        self.addCleanup(oauth.close)
        return oauth

    def start_login(self, oauth, server, **payload):
        request = {"groupId": "official", **payload}
        with (
            patch.object(core, "load_settings", return_value={}),
            patch.object(core, "_account_group", return_value={"id": "official"}),
            patch.object(oauth, "_bind_callback_server", return_value=(server, 1455)),
            patch.object(app, "open_browser_window", return_value=True),
        ):
            return oauth.start(request)

    @staticmethod
    def join_exchange(login_id):
        name = f"codex-agent-manager-oauth-exchange-{login_id}"
        deadline = time.time() + 2
        while time.time() < deadline:
            worker = next((thread for thread in threading.enumerate() if thread.name == name), None)
            if worker is None:
                return
            worker.join(timeout=0.05)
        raise AssertionError(f"OAuth exchange worker {name} did not exit")

    @staticmethod
    def seed_waiting_login(oauth):
        with oauth.lock:
            oauth.login_id = "login-current"
            oauth.callback_port = 1455
            oauth.expected_state = "state-current"
            oauth.code_verifier = "verifier-current"
            oauth.deadline_monotonic = time.monotonic() + 30
            oauth.data = oauth._new_state("waiting")
            oauth.data["loginId"] = oauth.login_id

    def test_cancel_while_token_exchange_is_in_flight_does_not_save(self):
        oauth = self.make_oauth()
        server = FakeCallbackServer()
        started = threading.Event()
        release = threading.Event()

        def exchange(_code, _verifier, _port):
            started.set()
            self.assertTrue(release.wait(2))
            return dict(self.TOKENS)

        login = self.start_login(oauth, server)
        with (
            patch.object(oauth, "_exchange_code", side_effect=exchange),
            patch.object(core, "save_codex_account") as save,
        ):
            oauth.submit_callback(
                f"code=code-old&state={oauth.expected_state}",
                expected_login_id=login["loginId"],
            )
            self.assertTrue(started.wait(1))
            cancelled = oauth.cancel()
            release.set()
            self.join_exchange(login["loginId"])

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(oauth.state()["status"], "cancelled")
        self.assertTrue(server.closed.is_set())
        save.assert_not_called()

    def test_old_exchange_cannot_save_or_finish_a_new_login(self):
        oauth = self.make_oauth()
        first_server = FakeCallbackServer()
        second_server = FakeCallbackServer()
        started = threading.Event()
        release = threading.Event()

        def exchange(_code, _verifier, _port):
            started.set()
            self.assertTrue(release.wait(2))
            return dict(self.TOKENS)

        first = self.start_login(oauth, first_server)
        with (
            patch.object(oauth, "_exchange_code", side_effect=exchange),
            patch.object(core, "save_codex_account") as save,
        ):
            oauth.submit_callback(
                f"code=code-old&state={oauth.expected_state}",
                expected_login_id=first["loginId"],
            )
            self.assertTrue(started.wait(1))
            oauth.cancel()
            second = self.start_login(oauth, second_server)
            with self.assertRaisesRegex(core.ManagerError, "会话不匹配"):
                oauth.submit_callback(
                    f"code=stale-listener&state={oauth.expected_state}",
                    expected_login_id=first["loginId"],
                )
            release.set()
            self.join_exchange(first["loginId"])

        current = oauth.state()
        self.assertEqual(current["loginId"], second["loginId"])
        self.assertEqual(current["status"], "waiting")
        self.assertFalse(second_server.closed.is_set())
        save.assert_not_called()

    def test_concurrent_starts_create_only_one_login_generation(self):
        oauth = self.make_oauth()
        servers = []
        barrier = threading.Barrier(3)
        results = []
        errors = []

        def bind(_login_id):
            server = FakeCallbackServer()
            servers.append(server)
            return server, 1455

        def start():
            barrier.wait()
            try:
                results.append(oauth.start({"groupId": "official"}))
            except Exception as exc:
                errors.append(exc)

        with (
            patch.object(core, "load_settings", return_value={}),
            patch.object(core, "_account_group", return_value={"id": "official"}),
            patch.object(oauth, "_bind_callback_server", side_effect=bind),
            patch.object(app, "open_browser_window", return_value=True),
        ):
            workers = [threading.Thread(target=start) for _ in range(2)]
            for worker in workers:
                worker.start()
            barrier.wait()
            for worker in workers:
                worker.join(2)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "waiting")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], core.ManagerError)
        self.assertIn("已有 OAuth 登录", str(errors[0]))
        self.assertEqual(len(servers), 1)

    def test_stale_timeout_finish_cannot_expire_or_close_new_listener(self):
        oauth = self.make_oauth()
        first_server = FakeCallbackServer()
        second_server = FakeCallbackServer()
        with oauth.lock:
            oauth.login_id = "login-stale"
            oauth.callback_port = 1455
            oauth.callback_server = first_server
            oauth.callback_server_login_id = oauth.login_id
            oauth.expected_state = "state-stale"
            oauth.code_verifier = "verifier-stale"
            oauth.deadline_monotonic = time.monotonic() - 1
            oauth.data = oauth._new_state("waiting")
            oauth.data["loginId"] = oauth.login_id
        first_login_id = oauth.login_id

        entered_finish = threading.Event()
        release_finish = threading.Event()
        real_finish = oauth._finish

        def delayed_finish(status, **kwargs):
            if threading.current_thread().name == "stale-oauth-timeout":
                entered_finish.set()
                self.assertTrue(release_finish.wait(2))
            return real_finish(status, **kwargs)

        with patch.object(oauth, "_finish", side_effect=delayed_finish):
            stale_timeout = threading.Thread(
                target=oauth._watch_timeout,
                args=(first_login_id,),
                name="stale-oauth-timeout",
            )
            stale_timeout.start()
            self.assertTrue(entered_finish.wait(1))
            oauth.cancel()
            second = self.start_login(oauth, second_server)
            release_finish.set()
            stale_timeout.join(2)

        self.assertFalse(stale_timeout.is_alive())
        current = oauth.state()
        self.assertEqual(current["loginId"], second["loginId"])
        self.assertEqual(current["status"], "waiting")
        self.assertFalse(second_server.closed.is_set())

    def test_rejects_duplicate_oversized_userinfo_and_fragment_callbacks(self):
        oauth = self.make_oauth()
        self.seed_waiting_login(oauth)
        state = oauth.expected_state
        malformed = {
            "duplicate code": f"code=one&code=two&state={state}",
            "duplicate state": f"code=one&state={state}&state={state}",
            "oversized callback": f"code={'x' * app.OAuthDeviceLogin.MAX_CALLBACK_INPUT_BYTES}&state={state}",
            "userinfo": f"http://user@localhost:1455/auth/callback?code=one&state={state}",
            "fragment": f"http://localhost:1455/auth/callback?code=one&state={state}#ignored",
            "bad percent encoding": f"code=%ZZ&state={state}",
            "invalid unicode": f"code=\ud800&state={state}",
        }

        for label, callback in malformed.items():
            with self.subTest(label=label):
                with self.assertRaises(core.ManagerError):
                    oauth.submit_callback(callback)
                self.assertEqual(oauth.state()["status"], "waiting")
                self.assertFalse(oauth.exchange_started)

    def test_access_denied_finishes_wait_and_normal_callback_saves_once(self):
        denied = self.make_oauth()
        denied_server = FakeCallbackServer()
        denied_login = self.start_login(denied, denied_server)
        denied_result = denied.submit_callback(
            f"error=access_denied&error_description=cancelled&state={denied.expected_state}",
            expected_login_id=denied_login["loginId"],
        )
        self.assertEqual(denied_result["status"], "cancelled")
        self.assertEqual(denied.state()["status"], "cancelled")
        self.assertTrue(denied_server.closed.is_set())

        oauth = self.make_oauth()
        server = FakeCallbackServer()
        login = self.start_login(oauth, server, label="Saved")
        with (
            patch.object(oauth, "_exchange_code", return_value=dict(self.TOKENS)),
            patch.object(
                core,
                "save_codex_account",
                return_value={"id": "saved-id", "label": "Saved"},
            ) as save,
        ):
            submitted = oauth.submit_callback(
                f"http://localhost:1455/auth/callback?code=valid&state={oauth.expected_state}",
                expected_login_id=login["loginId"],
            )
            self.assertEqual(submitted["status"], "exchanging")
            self.join_exchange(login["loginId"])

        completed = oauth.state()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["account"], {"id": "saved-id", "label": "Saved"})
        self.assertTrue(server.closed.is_set())
        save.assert_called_once()
        auth_document = json.loads(save.call_args.kwargs["auth_bytes"].decode("utf-8"))
        self.assertEqual(auth_document["tokens"], self.TOKENS)

    def test_token_http_error_response_is_closed(self):
        oauth = self.make_oauth()
        response_body = io.BytesIO(b'{"error":"invalid_grant"}')
        http_error = urllib.error.HTTPError(
            oauth.TOKEN_ENDPOINT,
            400,
            "Bad Request",
            {},
            response_body,
        )
        with patch.object(core, "_open_same_origin_request", side_effect=http_error):
            with self.assertRaisesRegex(core.ManagerError, "HTTP 400"):
                oauth._exchange_code("code", "verifier", 1455)
        self.assertTrue(response_body.closed)


if __name__ == "__main__":
    unittest.main()
