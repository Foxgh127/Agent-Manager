from types import SimpleNamespace
from unittest.mock import Mock, patch
import threading
import unittest

import agent_manager_app as app
import agent_manager_core as core
import test_app_workbench_routes as routes


class RadarRuntimeTests(unittest.TestCase):
    def runtime(self):
        runtime = app.ManagerRuntime.__new__(app.ManagerRuntime)
        runtime._closed = runtime._restart_prepared = False
        runtime.radar_monitor_stop = threading.Event()
        runtime.radar_monitor_wake = threading.Event()
        runtime.radar_monitor_thread = None
        runtime.radar_monitor_status = {}
        return runtime

    def test_monitor_is_opt_in_and_reenable_keeps_old_stop_token_cancelled(self):
        runtime = self.runtime()
        enabled = {"appBehavior":{"radarMonitoring":False}}
        with patch.object(core,"load_settings",return_value=enabled), patch.object(app.threading,"Thread") as factory:
            runtime._start_radar_monitor(); factory.assert_not_called()
            enabled["appBehavior"]["radarMonitoring"] = True
            first = Mock(); first.is_alive.return_value = True
            second = Mock(); factory.side_effect = [first,second]
            runtime._start_radar_monitor(); old_stop = runtime.radar_monitor_stop
            runtime._start_radar_monitor(); self.assertEqual(factory.call_count,1)
            runtime._stop_radar_monitor(); self.assertTrue(old_stop.is_set())
            runtime._start_radar_monitor()
            self.assertEqual(factory.call_count,2); self.assertTrue(old_stop.is_set())
            self.assertIsNot(runtime.radar_monitor_stop,old_stop); self.assertFalse(runtime.radar_monitor_stop.is_set())

    def test_manual_check_records_new_time_and_delivers_prediction(self):
        runtime=self.runtime(); alert={"level":"P","kind":"community-prediction"}
        runtime.radar=SimpleNamespace(run_monitor=Mock(return_value={"success":True,"newAlert":True,"alert":alert,"state":{"lastSuccessAt":"2026-09-08T14:00:00Z","lastResult":"预测预警"}}))
        runtime.radar_alert_notifier=Mock()
        runtime.check_radar_now()
        runtime.radar.run_monitor.assert_called_once_with(force=True)
        self.assertEqual(runtime.radar_monitor_status["lastCheckedAt"],"2026-09-08T14:00:00Z")
        runtime.radar_alert_notifier.assert_called_once_with(alert)

    def test_preference_rejects_non_boolean(self):
        with patch.object(core,"load_settings",return_value={"appBehavior":{}}), patch.object(core,"save_settings") as save:
            with self.assertRaises(core.ManagerError): core._save_app_behavior_locked({"radarMonitoring":"false"})
            save.assert_not_called()
            self.assertTrue(core._save_app_behavior_locked({"radarMonitoring":True})["radarMonitoring"])


class UpdateAndRadarRoutesTests(unittest.TestCase):
    setUp=routes.WorkbenchRouteTests.setUp
    tearDown=routes.WorkbenchRouteTests.tearDown
    request=routes.WorkbenchRouteTests.request

    def test_update_status_is_local_and_download_uses_token_not_url(self):
        service=Mock();service.status.return_value={"state":"unconfigured","configured":False}
        service.start_download.return_value={"download":{"state":"downloading"}}
        self.server.runtime.get_app_updates=Mock(return_value=service)
        self.assertFalse(self.request("/api/app-update")["status"]["configured"])
        service.check.assert_not_called()
        self.request("/api/app-update/download",method="POST",payload={"releaseToken":"opaque","url":"https://unused.example"})
        service.start_download.assert_called_once_with("opaque", on_ready=None)

    def test_radar_snapshot_poll_does_not_refresh_external_sources(self):
        self.server.runtime.radar.get_reset_radar=Mock(return_value={"data":{"monitor":{"lastResult":"预测预警"}}})
        response=self.request("/api/radar/reset-status")
        self.assertEqual(response["reset"]["monitor"]["lastResult"],"预测预警")
        self.assertIs(self.server.runtime.radar.get_reset_radar.call_args.kwargs["refresh"],False)
