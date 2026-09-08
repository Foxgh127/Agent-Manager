import unittest
from unittest.mock import patch
import agent_manager_app as app
import dashboard_order
import test_app_workbench_routes as routes


class PoolWorkbenchRoutesTests(unittest.TestCase):
    setUp = routes.WorkbenchRouteTests.setUp
    tearDown = routes.WorkbenchRouteTests.tearDown
    request = routes.WorkbenchRouteTests.request

    def test_copy_existing_public_key_does_not_rotate(self):
        with patch.object(app.core,"load_service_secret",side_effect=lambda name, **kwargs: "public-fixture" if name == "web2api" else "internal-fixture"), patch.object(app.core,"rotate_web2api_key") as rotate:
            response=self.request("/api/web2api/key")
        self.assertEqual(response["apiKey"],"public-fixture")
        rotate.assert_not_called()

    def test_pool_drop_dispatches_only_to_membership_transaction(self):
        payload={"sourceId":"relay:fixture","dropTarget":"apiPool"}
        result={"dropTarget":"apiPool","poolSourceId":"provider:fixture","alreadyMember":False}
        with patch.object(dashboard_order,"move_dashboard_card",return_value=result) as move:
            response=self.request("/api/dashboard/move",method="POST",payload=payload)
        move.assert_called_once_with(app.core,payload)
        self.assertEqual(response["result"],result)
