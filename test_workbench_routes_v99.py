import unittest
from unittest.mock import patch
import agent_manager_app as app
import dashboard_order
import test_app_workbench_routes as routes


class WorkbenchRoutesV99Tests(unittest.TestCase):
    setUp = routes.WorkbenchRouteTests.setUp
    tearDown = routes.WorkbenchRouteTests.tearDown
    request = routes.WorkbenchRouteTests.request

    def test_group_drop_uses_transactional_dashboard_service(self):
        payload={"sourceId":"relay:r","visibleIds":["relay:r","account:a"],"groupId":"official"}
        expected={"dashboardOrder":["relay:r","account:a"],"groupId":"official","changed":True}
        with patch.object(dashboard_order,"move_dashboard_card",return_value=expected) as move:
            response=self.request("/api/dashboard/move",method="POST",payload=payload)
        move.assert_called_once_with(app.core,payload)
        self.assertEqual(response["result"],expected)


if __name__=="__main__":
    unittest.main()
