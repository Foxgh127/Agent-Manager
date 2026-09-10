import http.client
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from agent_manager import application as app


@pytest.fixture
def location_server():
    server = app.ManagerServer(("127.0.0.1", 0), app.RequestHandler, SimpleNamespace())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(route, body=None, *, authorized=True):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
        try:
            headers = {"Content-Type": "application/json"}
            if authorized:
                headers["X-Agent-Manager-Token"] = server.api_token
            connection.request("GET" if body is None else "POST", route,
                None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8"), headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()
    yield server, request
    server.shutdown(); server.server_close(); thread.join(timeout=3)


def test_location_routes_preserve_unicode_and_require_authorization(location_server, monkeypatch):
    server, request = location_server
    directory = "D:\\应用 程序\\张三's [文件夹] & 工具"
    status = {"supported": True, "executable": directory + "\\AgentManager.exe"}
    read = Mock(return_value=status)
    move = Mock(return_value={"started": True})
    shortcut = Mock(return_value={"created": True})
    monkeypatch.setattr(app, "application_location_status", read)
    monkeypatch.setattr(app, "prepare_application_relocation", move)
    monkeypatch.setattr(app, "select_application_directory", lambda _: {"directory": directory})
    monkeypatch.setattr(app, "create_application_shortcut", shortcut)
    assert request("/api/application/location", authorized=False)[0] == 403
    assert request("/api/application/location/move", {"directory": directory}, authorized=False)[0] == 403
    read.assert_not_called(); move.assert_not_called()
    assert request("/api/application/location")[1]["location"] == status
    assert request("/api/application/location/select", {})[1]["directory"] == directory
    assert request("/api/application/location/move", {"directory": directory})[0] == 200
    move.assert_called_once_with(server, directory)
    assert request("/api/application/location/shortcut", {})[0] == 200
    shortcut.assert_called_once_with(server)


def test_location_operations_respect_shutdown_and_input_validation(location_server, monkeypatch):
    server, request = location_server
    move = Mock()
    shortcut = Mock()
    monkeypatch.setattr(app, "prepare_application_relocation", move)
    monkeypatch.setattr(app, "create_application_shortcut", shortcut)
    for value in (None, " ", [], {"path": "D:/"}):
        assert request("/api/application/location/move", {"directory": value})[0] == 400
    move.assert_not_called()
    server.stop_accepting_mutations()
    assert request("/api/application/location/move", {"directory": "D:/Apps"})[0] == 503
    assert request("/api/application/location/shortcut", {})[0] == 503
    shortcut.assert_not_called()


def test_directory_picker_cancel_and_chinese_path(monkeypatch):
    import webview
    window = SimpleNamespace(create_file_dialog=Mock(return_value=None))
    server = SimpleNamespace(native_window=True, native_window_object=window)
    assert app.select_application_directory(server) == {"directory": None, "cancelled": True}
    window.create_file_dialog.return_value = ("D:\\应用 程序\\中文",)
    assert app.select_application_directory(server)["directory"] == "D:\\应用 程序\\中文"
    assert window.create_file_dialog.call_args.args[0] == webview.FileDialog.FOLDER
    server.native_window = False
    with pytest.raises(app.core.ManagerError, match="直接输入"):
        app.select_application_directory(server)
