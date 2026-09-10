from datetime import datetime, timedelta, timezone
import base64
import copy
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent_manager.integrations import radar
from agent_manager.integrations import radar_monitor as monitor
from agent_manager.platform import notifications
import agent_manager.application as app


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 10, 19, 37, tzinfo=timezone.utc)

    def event(self, title="We will reset Codex usage limits for all paid users in the next hour.", ident="123", **extra):
        return {"id": ident, "title": title, "publishedAt": (self.now - timedelta(minutes=15)).isoformat(),
                "url": f"https://x.com/thsottiaux/status/{ident}", **extra}

    def assess(self, signal_events=(), **data):
        return monitor.assess_reset_signals({"forecastSignals": {"resetEvents": list(signal_events)}, **data}, now=self.now)

    def test_explicit_official_future_is_warning(self):
        result = self.assess([self.event()])
        self.assertEqual(result["severity"], "warning")
        self.assertFalse(result["officialConfirmed"])
        self.assertEqual(result["alert"]["level"], "A")

    def test_explicit_completed_with_scope_is_confirmation(self):
        result = self.assess([self.event("We have reset Codex usage limits for all paid users.")])
        self.assertEqual(result["severity"], "confirmed")
        self.assertTrue(result["officialConfirmed"])
        self.assertEqual(result["alert"]["level"], "C")

    def test_unscoped_personal_reset_does_not_confirm_global_reset(self):
        result = self.assess([self.event("I have reset my Codex usage limits.")])
        self.assertNotEqual(result["severity"], "confirmed")

    def test_personal_subscription_and_future_reset_do_not_alert_everyone(self):
        for title in ("I have reset my Codex subscription.", "I will reset my Codex quota tomorrow."):
            with self.subTest(title=title):
                self.assertEqual(self.assess([self.event(title)])["severity"], "information")

    def test_community_confirmation_label_cannot_impersonate_official_source(self):
        result = self.assess([self.event("We have reset Codex usage limits for all users.",
                                       url="https://x.com/a_community_user/status/123", sourceLabel="官方确认", status="confirmed")])
        self.assertEqual(result["severity"], "information")
        self.assertFalse(result["officialConfirmed"])
        self.assertIsNone(result["alert"])

    def test_negated_future_cannot_warn(self):
        result = self.assess([self.event("We will not reset Codex usage limits for all paid users.")])
        self.assertEqual(result["severity"], "information")

    def test_future_missing_or_old_timestamp_is_excluded(self):
        for stamp in (None, "2026-09-10T19:00:00", "2026-09-08T00:00:00Z", "2026-09-12T00:00:00Z"):
            with self.subTest(stamp=stamp):
                self.assertIsNone(self.assess([self.event(publishedAt=stamp)])["alert"])

    def test_syndicated_same_tweet_is_one_independent_signal(self):
        first = self.event()
        result = self.assess([first], events=[{**first, "id": "rss:other", "url": "https://twitter.com/thsottiaux/status/123"}])
        self.assertEqual(result["uniqueSignalCount"], 1)
        self.assertEqual(result["independentSourceCount"], 1)

    def test_official_status_corroborates_but_outage_does_not_confirm_reset(self):
        incident = {"id": "one", "name": "Codex usage limit incident", "latestUpdate": "We are investigating quota restoration.",
                    "status": "investigating", "updatedAt": self.now.isoformat(), "url": "https://status.openai.com/incidents/one"}
        result = self.assess([self.event()], statusIncidents=[incident])
        self.assertEqual(result["independentSourceCount"], 2)
        self.assertEqual(result["severity"], "warning")
        self.assertGreater(result["score"], self.assess([self.event()])["score"])
        incident["status"] = "resolved"
        self.assertIsNone(self.assess(statusIncidents=[incident])["alert"])

    def test_probability_100_is_only_community_attention(self):
        result = self.assess(forecastSignals={"checkedAt": self.now.isoformat(), "predictor": {"score": 100}})
        self.assertEqual(result["severity"], "watch")
        self.assertLess(result["score"], 70)
        self.assertEqual(result["alert"]["level"], "P")
        self.assertIn("不是重置发生概率", result["alert"]["evidence"])

    def test_card_and_full_reset_remain_distinct_events(self):
        result = self.assess([self.event(resetType="full-reset"), self.event(resetType="reset-card")])
        self.assertEqual(result["uniqueSignalCount"], 2)
        self.assertEqual(result["independentSourceCount"], 1)

    def test_historical_backfill_is_not_a_new_completion_notice(self):
        history = self.event("We have reset Codex usage limits for all paid users.", occurredAt="2026-08-01T10:00:00Z")
        self.assertIsNone(self.assess([history])["alert"])
        self.assertIsNone(self.assess(resetHistory=[self.event()])["alert"])

    def test_evaluation_persists_dedup_and_only_notifies_new_or_upgraded(self):
        service = SimpleNamespace(_state={})
        def evaluate(item):
            return monitor.evaluate_reset_refresh(service, {"forecastSignals": {"resetEvents": [item]}}, self.now)
        self.assertTrue(evaluate(self.event())["newAlert"])
        self.assertFalse(evaluate(self.event())["newAlert"])
        service = SimpleNamespace(_state=copy.deepcopy(service._state))
        self.assertFalse(evaluate(self.event())["newAlert"])
        self.assertTrue(evaluate(self.event("We have reset Codex usage limits for all paid users."))["newAlert"])
        self.assertFalse(evaluate(self.event())["newAlert"])

    def test_existing_confirmed_signal_does_not_hide_new_different_event(self):
        service = SimpleNamespace(_state={})
        confirmed = self.event("We have reset Codex usage limits for all paid users.")
        monitor.evaluate_reset_refresh(service, {"forecastSignals": {"resetEvents": [confirmed]}}, self.now)
        result = monitor.evaluate_reset_refresh(service, {"forecastSignals": {"resetEvents": [confirmed, self.event(ident="456")]}}, self.now)
        self.assertTrue(result["newAlert"])
        self.assertIn("456", result["alert"]["eventKey"])


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 9, 10, 19, 37, tzinfo=timezone.utc)  # Beijing 03:37
        self.service = radar.RadarService(Path(self.temp.name) / "radar.json", clock=lambda: self.now)
        self.service._refresh = Mock(return_value={"attempted": True, "success": True})

    def test_start_immediate_then_every_hour_including_overnight(self):
        monitor.begin_monitoring(self.service)
        self.assertEqual(monitor.seconds_until_next_monitor(self.service), 0)
        monitor.run_monitor(self.service)
        self.assertEqual(monitor.seconds_until_next_monitor(self.service), 3600)
        self.now += timedelta(seconds=3599)
        self.assertEqual(monitor.run_monitor(self.service)["suppressed"], "already-checked")
        self.now += timedelta(seconds=1)
        monitor.run_monitor(self.service)
        self.assertEqual(self.service._refresh.call_count, 2)
        self.assertEqual(monitor.seconds_until_next_monitor(self.service), 3600)

    def test_manual_does_not_reset_automatic_deadline_and_read_never_fetches(self):
        monitor.run_monitor(self.service)
        due = monitor.monitor_status(self.service)["nextCheckAt"]
        self.now += timedelta(minutes=20)
        monitor.run_monitor(self.service, force=True)
        for _ in range(3):
            self.assertEqual(monitor.monitor_status(self.service)["nextCheckAt"], due)
        self.assertEqual(self.service._refresh.call_count, 2)

    def test_monitor_first_then_ui_startup_only_fetches_other_channels(self):
        monitor.begin_monitoring(self.service)
        self.service.run_monitor()
        due = self.service.monitor_status()['nextCheckAt']
        self.now += timedelta(seconds=8)
        self.service.get_snapshot(startup=True)
        self.assertEqual(self.service._refresh.call_args_list[0].args[0], {'reset'})
        self.assertEqual(self.service._refresh.call_args_list[1].args[0], {'intelligence', 'quota'})
        self.assertEqual(self.service.monitor_status()['nextCheckAt'], due)

    def test_ui_snapshot_first_is_reused_by_first_auto_check(self):
        self.service.get_snapshot(startup=True)
        self.now += timedelta(seconds=8)
        monitor.begin_monitoring(self.service)
        result = self.service.run_monitor()
        self.assertEqual(result['suppressed'], 'recent-check')
        self.assertEqual(self.service._refresh.call_count, 1)
        self.assertEqual(monitor.seconds_until_next_monitor(self.service), 3600)

    def test_prior_process_cache_does_not_suppress_new_snapshot_and_manual_full_refresh_is_preserved(self):
        self.service.get_snapshot(startup=True)
        second = radar.RadarService(self.service.cache.path, clock=lambda: self.now)
        second._refresh = Mock(return_value={'attempted': True, 'success': True})
        second.get_snapshot(startup=True)
        self.assertIn('reset', second._refresh.call_args.args[0])
        second.get_snapshot(refresh=True)
        self.assertIn('reset', second._refresh.call_args.args[0])
        self.assertEqual(second._refresh.call_count, 2)

    def test_snapshot_alert_wakes_pending_delivery_monitor(self):
        self.service._monitor_wake_callback = Mock()
        self.service._refresh.return_value = {'attempted': True, 'success': True, 'newAlert': True}
        self.service.get_snapshot(startup=True)
        self.service._monitor_wake_callback.assert_called_once()

    def test_parallel_manual_coalesces_without_waiting_for_network(self):
        entered, release = threading.Event(), threading.Event()
        def refresh(_):
            entered.set()
            self.assertTrue(release.wait(5))
            return {"attempted": True, "success": True}
        self.service._refresh.side_effect = refresh
        thread = threading.Thread(target=monitor.run_monitor, args=(self.service,))
        thread.start()
        self.assertTrue(entered.wait(3))
        try:
            self.assertEqual(monitor.run_monitor(self.service, force=True)["suppressed"], "in-flight")
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.service._refresh.call_count, 1)

    def test_failure_has_bounded_retry_and_honors_retry_after(self):
        self.service._state["nextAllowedAt"] = (self.now + timedelta(hours=3)).isoformat()
        self.service._persist_state()
        self.service._refresh.return_value = {"attempted": False, "success": False, "suppressed": "backoff"}
        monitor.run_monitor(self.service)
        self.assertEqual(monitor.seconds_until_next_monitor(self.service), 10800)
        self.service._state["nextAllowedAt"] = None
        self.service._persist_state()
        self.now += timedelta(hours=3)
        self.service._refresh.side_effect = RuntimeError("network")
        result = monitor.run_monitor(self.service)
        self.assertFalse(result["success"])
        self.assertEqual(monitor.seconds_until_next_monitor(self.service), 300)

    def test_recent_shared_cache_coalesces_stale_process(self):
        monitor.run_monitor(self.service)
        second = radar.RadarService(self.service.cache.path, clock=lambda: self.now)
        second._refresh = Mock()
        self.assertEqual(monitor.run_monitor(second)["suppressed"], "recent-check")
        second._refresh.assert_not_called()

    def test_status_retry_after_is_persisted_and_status304_without_cache_is_error(self):
        service = self.service
        service._fetch_status_incidents = Mock(return_value=(None, True))
        monitor.fetch_status_for_refresh(service, self.now, Mock())
        self.assertIn("304", service._state["monitor"]["statusError"])
        service._state["monitor"]["statusNextAllowedAt"] = None
        service._fetch_status_incidents.side_effect = radar.RadarHTTPError("rate limit", retry_after=self.now + timedelta(hours=4))
        remembered = Mock()
        monitor.fetch_status_for_refresh(service, self.now, remembered)
        self.assertEqual(service._state["monitor"]["statusNextAllowedAt"], monitor._iso(self.now + timedelta(hours=4)))
        monitor.fetch_status_for_refresh(service, self.now + timedelta(hours=1), remembered)
        self.assertEqual(service._fetch_status_incidents.call_count, 2)

    def test_runtime_first_tick_ignores_legacy_delay_and_stop_prevents_check(self):
        runtime = app.ManagerRuntime.__new__(app.ManagerRuntime)
        runtime._closed = runtime._restart_prepared = False
        runtime.radar_monitor_status = {}
        runtime.radar_alert_notifier = Mock()
        stop = threading.Event()
        calls = []
        class Wake:
            def wait(self, delay):
                calls.append(delay)
                if len(calls) > 1:
                    stop.set()
                return False
            def clear(self):
                pass
        def run():
            return {"success": True, "state": {}}
        runtime.radar = SimpleNamespace(monitor_status=Mock(return_value={}), seconds_until_next_monitor=Mock(return_value=3600), run_monitor=Mock(side_effect=run))
        runtime._run_radar_monitor(stop, Wake())
        self.assertEqual(calls, [0.1, 3600])
        runtime.radar.run_monitor.assert_called_once()
        runtime._run_radar_monitor(stop, Wake())
        runtime.radar.run_monitor.assert_called_once()

    def test_official_status_refresh_succeeds_when_every_other_source_fails(self):
        service = radar.RadarService(self.service.cache.path, clock=lambda: self.now)
        incident = {"id": "only", "name": "Codex usage limits", "latestUpdate": "We will reset Codex usage limits for all users.",
                    "status": "monitoring", "updatedAt": self.now.isoformat(), "url": "https://status.openai.com/incidents/only"}
        with patch.object(service, '_fetch_summary', side_effect=radar.RadarError('summary unavailable')), \
             patch.object(service, '_fetch_public_post_translations', side_effect=radar.RadarError('html unavailable')), \
             patch.object(service, '_fetch_events', side_effect=radar.RadarError('rss unavailable')), \
             patch.object(service, '_fetch_forecast', side_effect=radar.RadarError('forecast unavailable')), \
             patch.object(service, '_fetch_status_incidents', return_value=([incident], False)), \
             patch.object(service, '_translate_status_incidents', side_effect=lambda rows: rows), \
             patch.object(service, '_translate_reset_payload'):
            result = service.run_monitor()
        self.assertTrue(result['success'])
        self.assertTrue(result['newAlert'])
        self.assertEqual(result['alert']['severity'], 'warning')
        self.assertEqual(result['alert']['sourceUrls'], [incident['url']])

    def queued_runtime(self):
        alert = {'signature': 'delivery-1', 'level': 'P', 'expiresAt': monitor._iso(self.now + timedelta(hours=1)), 'evidence': 'test'}
        self.service._state['monitor']['alertDeliveries'] = {'delivery-1': {'state': 'pending', 'attempts': 0, 'nextAttemptAt': monitor._iso(self.now), 'alert': alert}}
        self.service._persist_state()
        runtime = app.ManagerRuntime.__new__(app.ManagerRuntime)
        runtime._closed = runtime._restart_prepared = False
        runtime.radar = self.service
        runtime.radar_monitor_status = {}
        runtime.radar_monitor_wake = threading.Event()
        runtime.radar_alert_notifier = None
        return runtime, alert

    def test_late_notifier_registration_wakes_and_delivers_pending_once(self):
        runtime, alert = self.queued_runtime()
        runtime._deliver_radar_alert()
        self.assertEqual(self.service._state['monitor']['alertDeliveries']['delivery-1']['attempts'], 0)
        notifier = Mock(return_value=True)
        runtime.set_radar_alert_notifier(notifier)
        self.assertTrue(runtime.radar_monitor_wake.is_set())
        runtime._deliver_radar_alert()
        runtime._deliver_radar_alert(alert)
        notifier.assert_called_once_with(alert)

    def test_delivery_failure_retries_after_five_minutes_and_survives_restart(self):
        runtime, alert = self.queued_runtime()
        runtime.radar_alert_notifier = Mock(return_value=False)
        runtime._deliver_radar_alert()
        runtime._deliver_radar_alert()
        runtime.radar_alert_notifier.assert_called_once()
        self.assertEqual(monitor.seconds_until_pending_delivery(self.service), 300)
        self.now += timedelta(minutes=5)
        runtime.radar = radar.RadarService(self.service.cache.path, clock=lambda: self.now)
        runtime.radar_alert_notifier = Mock(return_value=True)
        runtime._deliver_radar_alert()
        runtime._deliver_radar_alert(alert)
        runtime.radar_alert_notifier.assert_called_once_with(alert)
        self.assertEqual(runtime.radar._state['monitor']['alertDeliveries']['delivery-1']['state'], 'submitted')

    def test_shutdown_and_source_expiry_prevent_replay(self):
        runtime, _ = self.queued_runtime()
        notifier = Mock(return_value=True)
        runtime.radar_alert_notifier = notifier
        runtime._closed = True
        runtime._deliver_radar_alert()
        notifier.assert_not_called()
        runtime._closed = False
        self.now += timedelta(hours=2)
        runtime._deliver_radar_alert()
        notifier.assert_not_called()
        self.assertEqual(self.service._state['monitor']['alertDeliveries']['delivery-1']['state'], 'expired')

    def test_delivery_exception_is_limited_to_three_attempts(self):
        runtime, _ = self.queued_runtime()
        runtime.radar_alert_notifier = Mock(side_effect=RuntimeError('unavailable'))
        for _ in range(5):
            runtime._deliver_radar_alert()
            self.now += timedelta(minutes=5)
        self.assertEqual(runtime.radar_alert_notifier.call_count, 3)
        self.assertEqual(self.service._state['monitor']['alertDeliveries']['delivery-1']['state'], 'failed')


class NotificationTests(unittest.TestCase):
    def test_native_payload_is_data_not_powershell_and_window_is_hidden(self):
        runner = Mock(return_value=SimpleNamespace(returncode=0, stdout="notification-submitted"))
        with patch.object(notifications.os, "name", "nt"):
            self.assertTrue(notifications.notify_windows("测试通知", "$(calc) ' <xml>", runner=runner))
        args = runner.call_args.args[0]
        self.assertIn("Hidden", args)
        script = base64.b64decode(args[-1]).decode("utf-16-le")
        self.assertNotIn("$(calc)", script)
        self.assertIn("ShowBalloonTip", script)

    def test_notification_keeps_community_qualifier_and_original_link(self):
        title, message = notifications.radar_notification_text({"level": "P", "sourceUrls": ["https://x.com/thsottiaux/status/1"]})
        self.assertIn("关注", title)
        self.assertIn("非官方确认", message)
        self.assertIn("https://x.com/thsottiaux/status/1", message)

    def test_server_without_tray_uses_native_backend(self):
        server = app.ManagerServer.__new__(app.ManagerServer)
        server.tray = None
        server.shutdown_started = threading.Event()
        with patch.object(notifications, "notify_windows", return_value=True) as send:
            self.assertTrue(server.notify_radar_alert({"level": "A", "evidence": "测试证据"}))
        send.assert_called_once()

    def test_hidden_tray_uses_native_backend(self):
        server = app.ManagerServer.__new__(app.ManagerServer)
        server.tray = Mock(visible=False)
        server.tray_error = None
        server.shutdown_started = threading.Event()
        with patch.object(notifications, 'notify_windows', return_value=True) as send:
            self.assertTrue(server.notify_radar_alert({'level': 'A'}))
        server.tray.notify.assert_not_called()
        send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
