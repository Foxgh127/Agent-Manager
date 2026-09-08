"""Self-updater tests: mocked public HTTP metadata and temporary files only."""
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import threading
import time

import pytest

import agent_manager.updates.service as updater


SOURCE = {"kind": "manifest", "url": "https://updates.example.com/latest.json"}
PAYLOAD = b"MZ synthetic update fixture, never executable"


def manifest(version="9.12.0", **overrides):
    value = {"schemaVersion": 1, "appId": updater.APP_ID, "version": version, "channel": "stable",
        "releaseNotes": "A verified fixture release.", "assets": [{"platform": "windows-x64", "name": "Agent-Manager.exe",
        "url": "https://updates.example.com/Agent-Manager.exe", "size": len(PAYLOAD), "sha256": hashlib.sha256(PAYLOAD).hexdigest()}]}
    value.update(overrides)
    return value


class Stream(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers if headers is not None else {"Content-Length": str(len(body))}
        self.aborted = False

    def abort(self):
        self.aborted = True


class Fetcher:
    def __init__(self):
        self.metadata = manifest()
        self.body = PAYLOAD
        self.calls = []
        self.metadata_error = None
        self.asset_factory = None

    def open(self, url, allowed_hosts, **kwargs):
        self.calls.append((url, allowed_hosts, kwargs))
        if "/releases" in url and "github.com" in url or url.endswith(".json"):
            if self.metadata_error:
                raise self.metadata_error
            return Stream(json.dumps(self.metadata).encode())
        return self.asset_factory() if self.asset_factory else Stream(self.body)


@pytest.fixture
def service(tmp_path):
    fetcher = Fetcher()
    instance = updater.AppUpdateService("9.11.2", tmp_path / "state" / "app-update.json", tmp_path / "downloads", fetcher=fetcher)
    yield instance, fetcher
    instance.close(timeout=2)


def ready(instance):
    instance.configure(SOURCE)
    state = instance.check()
    assert state["state"] == "update_available", state
    return state["latestRelease"]["releaseToken"]


def wait_download(instance):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = instance.status()
        if status["download"]["state"] != "downloading":
            return status
        time.sleep(0.005)
    raise AssertionError("download did not finish")


@pytest.mark.parametrize("older,newer", [("9.9", "9.10"), ("v9.11.2", "9.12.0"), ("1.0.0-alpha", "1.0.0-alpha.1"),
    ("1.0.0-alpha.9", "1.0.0-alpha.10"), ("1.0.0-alpha.10", "1.0.0-beta"), ("1.0.0-rc.1", "1.0.0"), ("1.0.0", "1.0.1-rc.1")])
def test_semver_precedence(older, newer):
    assert updater.Version.parse(older) < updater.Version.parse(newer)


@pytest.mark.parametrize("value", ["9", "01.2.3", "1.2.3-01", "1.2.3.4", "1.2.3-", "next", None])
def test_invalid_versions_rejected(value):
    with pytest.raises(updater.UpdateError):
        updater.Version.parse(value)


def test_build_metadata_does_not_change_precedence():
    assert updater.Version.parse("v1.2.3+build.5") == updater.Version.parse("1.2.3+other")


def test_unconfigured_is_explicit_and_never_contacts_network(service):
    instance, fetcher = service
    assert instance.status()["state"] == instance.check()["state"] == "unconfigured"
    assert not instance.status()["canDownload"]
    assert fetcher.calls == []


def test_embedded_source_local_override_and_reset(tmp_path):
    bundled = tmp_path / "app-update-source.json"
    bundled.write_text(json.dumps(SOURCE))
    instance = updater.AppUpdateService("9.11", tmp_path / "local.json", tmp_path / "downloads", bundled)
    assert instance.status()["sourceOrigin"] == "bundled"
    instance.configure({"kind": "github", "repository": "publisher/agent-manager"})
    assert instance.status()["sourceOrigin"] == "local"
    instance.configure(None)
    assert instance.status()["source"] == updater.validate_source(SOURCE)
    instance.configure({"kind": "disabled"})
    assert instance.status()["state"] == "unconfigured"


@pytest.mark.parametrize("url", ["http://updates.example.com/a", "https://user:pass@updates.example.com/a", "https://127.0.0.1/a",
    "https://10.1.2.3/a", "https://169.254.169.254/a", "https://[::1]/a", "https://localhost/a", "https://server.local/a",
    "https://updates.example.com:8443/a", "https://updates.example.com/a?token=secret", "https://updates.example.com/a#fragment", "file:///C:/app.exe"])
def test_source_url_is_https_public_and_credential_free(url):
    with pytest.raises(updater.UpdateError):
        updater.validate_source({"kind": "manifest", "url": url})


def test_arbitrary_headers_and_tokens_cannot_enter_transport_configuration():
    with pytest.raises(updater.UpdateError):
        updater.validate_source({**SOURCE, "headers": {"X-Agent-Manager-Control": "secret"}})
    with pytest.raises(updater.UpdateError):
        updater.validate_source({"kind": "github", "repository": "owner/repo", "token": "secret"})


def test_check_compares_versions_preserves_cache_on_failure(service):
    instance, fetcher = service
    token = ready(instance)
    fetcher.metadata_error = OSError("secret-in-exception https://private.example/token")
    status = instance.check()
    assert status["state"] == "check_failed" and status["stale"]
    assert status["latestRelease"]["releaseToken"] == token
    assert not status["canDownload"]
    assert "secret-in-exception" not in json.dumps(status)


def test_cached_result_after_restart_requires_fresh_check(service):
    instance, fetcher = service
    ready(instance)
    second = updater.AppUpdateService("9.11.2", instance.config_path, instance.download_dir, fetcher=fetcher)
    status = second.status()
    assert status["latestRelease"]["version"] == "9.12.0" and status["stale"]
    assert not status["canDownload"]


def test_newer_local_build_is_not_downgraded(service):
    instance, fetcher = service
    fetcher.metadata = manifest("9.10.0")
    instance.configure(SOURCE)
    status = instance.check()
    assert status["state"] == "current" and not status["updateAvailable"] and not status["canDownload"]


@pytest.mark.parametrize("change", ["app", "sha", "size", "platform", "name", "host", "channel"])
def test_untrusted_manifest_rejected_before_download(service, change):
    instance, fetcher = service
    asset = fetcher.metadata["assets"][0]
    if change == "app": fetcher.metadata["appId"] = "other-app"
    elif change == "sha": asset["sha256"] = "not-a-digest"
    elif change == "size": asset["size"] = updater.MAX_ASSET_BYTES + 1
    elif change == "platform": asset["platform"] = "macos-arm64"
    elif change == "name": asset["name"] = "../Agent.exe"
    elif change == "host": asset["url"] = "https://unapproved.example.com/Agent.exe"
    else: fetcher.metadata["version"] = "9.12.0-rc.1"
    instance.configure(SOURCE)
    status = instance.check()
    assert status["state"] == "check_failed" and not status["canDownload"]
    assert len(fetcher.calls) == 1


@pytest.mark.parametrize("name", ["..\\app.exe", "C:app.exe", "CON.exe", "COM1.exe", "app.exe ", "app.exe.", "app.exe:payload", "app.bat"])
def test_asset_filename_fencing(name):
    with pytest.raises(updater.UpdateError): updater._safe_name(name)


def test_github_release_digest_and_exact_asset_selection(service):
    instance, fetcher = service
    source = {"kind": "github", "repository": "publisher/agent-manager", "assetName": "Agent-{version}.exe"}
    fetcher.metadata = {"tag_name": "v9.12.0", "draft": False, "prerelease": False, "assets": [{"name": "Agent-9.12.0.exe", "size": len(PAYLOAD),
        "browser_download_url": "https://github.com/publisher/agent-manager/releases/download/v9.12.0/Agent-9.12.0.exe",
        "digest": "sha256:" + hashlib.sha256(PAYLOAD).hexdigest()}]}
    instance.configure(source)
    status = instance.check()
    assert status["canDownload"] and status["latestRelease"]["version"] == "9.12.0"
    assert fetcher.calls[0][0] == "https://api.github.com/repos/publisher/agent-manager/releases/latest"
    fetcher.metadata["assets"][0].pop("digest")
    assert instance.check()["errorCode"] == "missing_checksum"


def test_github_ambiguous_exes_require_name(service):
    instance, fetcher = service
    fetcher.metadata = {"tag_name": "v9.12.0", "assets": [{"name": "one.exe"}, {"name": "two.exe"}]}
    instance.configure({"kind": "github", "repository": "publisher/agent-manager"})
    assert instance.check()["errorCode"] == "asset_selection"


def test_atomic_verified_download_and_reuse_without_network(service):
    instance, fetcher = service
    token = ready(instance)
    instance.start_download(token)
    result = wait_download(instance)
    assert result["download"]["state"] == "ready" and result["download"]["verified"]
    path = instance.verified_download_path()
    assert path.parent == instance.download_dir and path.read_bytes() == PAYLOAD
    assert not list(instance.download_dir.glob("*.part"))
    assert not result["canInstall"]
    count = len(fetcher.calls)
    instance.start_download(token)
    assert wait_download(instance)["download"]["state"] == "ready"
    assert len(fetcher.calls) == count


@pytest.mark.parametrize("failure", ["short", "long", "hash", "network"])
def test_incomplete_or_corrupt_download_never_becomes_ready(service, failure):
    instance, fetcher = service
    token = ready(instance)
    if failure == "short": fetcher.body = PAYLOAD[:-1]
    elif failure == "long": fetcher.body = PAYLOAD + b"x"
    elif failure == "hash": fetcher.body = b"x" * len(PAYLOAD)
    else:
        class Broken(Stream):
            def read(self, _size): raise OSError("secret-upstream-url")
        fetcher.asset_factory = lambda: Broken(PAYLOAD)
    instance.start_download(token)
    result = wait_download(instance)
    assert result["download"]["state"] == "failed" and not result["download"]["verified"]
    assert "secret-upstream-url" not in json.dumps(result)
    assert list(instance.download_dir.iterdir()) == []
    with pytest.raises(updater.UpdateError): instance.verified_download_path()


def test_changed_download_is_rejected_and_can_be_redownloaded(service):
    instance, _fetcher = service
    token = ready(instance)
    instance.start_download(token)
    wait_download(instance)
    path = instance.verified_download_path()
    path.write_bytes(b"x" * len(PAYLOAD))
    with pytest.raises(updater.UpdateError): instance.verified_download_path()
    instance.start_download(token)
    assert wait_download(instance)["download"]["state"] == "ready"
    assert instance.verified_download_path().read_bytes() == PAYLOAD


def test_old_inspection_token_cannot_download_after_source_change(service):
    instance, fetcher = service
    token = ready(instance)
    instance.configure({"kind": "manifest", "url": "https://new.example.com/update.json"})
    with pytest.raises(updater.UpdateError) as error: instance.start_download(token)
    assert error.value.code == "stale_release" and len(fetcher.calls) == 1


def test_cancel_aborts_stream_cleans_partial_and_close_is_bounded(service):
    instance, fetcher = service
    entered, released = threading.Event(), threading.Event()
    class Blocking(Stream):
        def read(self, _size):
            entered.set()
            assert released.wait(2)
            return b""
        def abort(self): released.set()
    fetcher.asset_factory = lambda: Blocking(PAYLOAD)
    instance.start_download(ready(instance))
    assert entered.wait(2)
    assert instance.close(timeout=1)
    result = instance.status()
    assert result["download"]["state"] == "cancelled"
    assert not list(instance.download_dir.glob("*.part"))


def test_pending_connection_cancellation_reports_not_closed_until_worker_exits(service):
    instance, fetcher = service
    token = ready(instance)
    entered, released = threading.Event(), threading.Event()
    original = fetcher.open
    def delayed(url, hosts, **kwargs):
        entered.set()
        assert released.wait(2)
        return original(url, hosts, **kwargs)
    fetcher.open = delayed
    instance.start_download(token)
    assert entered.wait(2)
    assert instance.close(timeout=0.01) is False
    assert not list(instance.download_dir.glob("*.part"))
    released.set()
    assert instance.close(timeout=1)


def test_cleanup_only_exact_old_owned_partial_pattern(service):
    instance, _fetcher = service
    instance.download_dir.mkdir()
    old = instance.download_dir / ".app-update-abcdefgh.part"
    recent = instance.download_dir / ".app-update-12345678.part"
    unrelated = instance.download_dir / "user-download.part"
    for path in (old, recent, unrelated): path.write_bytes(b"keep")
    old_time = time.time() - 2 * 86400
    os.utime(old, (old_time, old_time))
    os.utime(unrelated, (old_time, old_time))
    instance._cleanup_expired_parts(instance.download_dir)
    assert not old.exists() and recent.exists() and unrelated.exists()


def test_dns_private_and_mixed_answers_rejected_before_connection(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(updater.UpdateError) as error: updater._public_addresses("updates.example.com")
    assert error.value.code == "private_address"


def test_transport_redirects_revalidate_host_and_never_forward_local_secrets(monkeypatch):
    monkeypatch.setattr(updater, "_system_proxy", lambda _host: None)
    calls = []
    class Response(Stream):
        status = 302
        def getheader(self, name, default=None): return {"Location": "https://evil.example.com/update.exe"}.get(name, default)
    class Connection:
        def __init__(self, host, address, timeout): self.host = host
        def request(self, method, target, headers): calls.append((self.host, method, target, headers))
        def getresponse(self): return Response(b"")
        def close(self): pass
    monkeypatch.setattr(updater, "_public_addresses", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(updater, "_PinnedHTTPS", Connection)
    monkeypatch.setenv("HTTPS_PROXY", "http://local-control-token@127.0.0.1:1234")
    with pytest.raises(updater.UpdateError) as error:
        updater.HTTPSFetcher().open("https://updates.example.com/a", {"updates.example.com"})
    assert error.value.code == "host_not_allowed" and len(calls) == 1
    names = {key.lower() for key in calls[0][3]}
    assert not names & {"authorization", "cookie", "x-agent-manager-control", "x-api-key", "proxy-authorization"}
    assert "local-control-token" not in json.dumps(calls)


def test_transport_rejects_https_downgrade(monkeypatch):
    monkeypatch.setattr(updater, "_system_proxy", lambda _host: None)
    class Response(Stream):
        status = 302
        def getheader(self, name, default=None): return "http://updates.example.com/a" if name == "Location" else default
    class Connection:
        def __init__(self, *_args): pass
        def request(self, *_args, **_kwargs): pass
        def getresponse(self): return Response(b"")
        def close(self): pass
    monkeypatch.setattr(updater, "_public_addresses", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(updater, "_PinnedHTTPS", Connection)
    with pytest.raises(updater.UpdateError): updater.HTTPSFetcher().open("https://updates.example.com/a", {"updates.example.com"})


def test_oversized_metadata_stops_before_parsing(service):
    instance, fetcher = service
    fetcher.open = lambda *_args, **_kwargs: Stream(b"", {"Content-Length": str(updater.MAX_METADATA_BYTES + 1)})
    instance.configure(SOURCE)
    assert instance.check()["errorCode"] == "metadata_too_large"


@pytest.mark.parametrize("proxy,code", [("http://user:secret@127.0.0.1:7890", "proxy_auth_unsupported"),
    ("socks5://127.0.0.1:7890", "proxy_scheme_unsupported"), ("https://127.0.0.1:7890", "proxy_scheme_unsupported")])
def test_proxy_auth_and_unsupported_schemes_are_explicitly_rejected(monkeypatch, proxy, code):
    monkeypatch.setattr(updater.urllib.request, "getproxies", lambda: {"https": proxy})
    monkeypatch.setattr(updater.urllib.request, "proxy_bypass", lambda _host: False)
    with pytest.raises(updater.UpdateError) as error: updater._system_proxy("api.github.com")
    assert error.value.code == code
    assert "secret" not in str(error.value)


def test_clash_proxy_resolves_target_via_connect_without_allowing_fake_ip_direct(monkeypatch):
    calls = []
    class Response(Stream):
        status = 200
        def getheader(self, _name, default=None): return default
    class Connection:
        socket_reference = None
        def __init__(self, host, address, timeout, connect_port=443):
            calls.append(("connect", host, address, connect_port))
        def set_tunnel(self, host, port): calls.append(("tunnel", host, port))
        def request(self, method, target, headers): calls.append(("request", method, target, headers))
        def getresponse(self): return Response(b"{}")
        def close(self): pass
    monkeypatch.setattr(updater.urllib.request, "getproxies", lambda: {"https": "http://127.0.0.1:7890"})
    monkeypatch.setattr(updater.urllib.request, "proxy_bypass", lambda _host: False)
    def dns(host, port, **_kwargs):
        assert host == "127.0.0.1", "target DNS must be left to the trusted proxy"
        return [(2, 1, 6, "", ("127.0.0.1", port))]
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(updater, "_PinnedHTTPS", Connection)
    stream = updater.HTTPSFetcher().open("https://api.github.com/repos/publisher/app/releases/latest", {"api.github.com"})
    assert stream.read(100) == b"{}"
    stream.close()
    assert calls[0] == ("connect", "api.github.com", "127.0.0.1", 7890)
    assert calls[1] == ("tunnel", "api.github.com", 443)
    assert not {name.lower() for name in calls[2][3]} & {"authorization", "cookie", "proxy-authorization", "x-agent-manager-control"}
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [(2, 1, 6, "", ("198.18.0.1", 443))])
    with pytest.raises(updater.UpdateError) as error: updater._public_addresses("api.github.com")
    assert error.value.code == "private_address"


def test_proxy_tls_still_uses_target_hostname_and_certificate_validation(monkeypatch):
    calls = []
    class Sock:
        def settimeout(self, timeout): pass
        def connect(self, target): calls.append(("tcp", target))
        def close(self): pass
        def do_handshake(self): calls.append(("tls_handshake",))
    class Context:
        def wrap_socket(self, sock, **kwargs):
            calls.append(("tls", kwargs))
            return sock
    monkeypatch.setattr(socket, "socket", lambda *_args: Sock())
    connection = updater._PinnedHTTPS("api.github.com", "127.0.0.1", 3, connect_port=7890)
    assert connection._context.check_hostname
    connection._context = Context()
    connection.set_tunnel("api.github.com", port=443)
    connection._tunnel = lambda: calls.append(("connect_tunnel", connection._tunnel_host, connection._tunnel_port))
    connection.connect()
    assert calls == [("tcp", ("127.0.0.1", 7890)), ("connect_tunnel", "api.github.com", 443),
                     ("tls", {"server_hostname": "api.github.com", "do_handshake_on_connect": False}), ("tls_handshake",)]
    connection.close()


def test_no_proxy_environment_alone_does_not_hide_windows_system_proxy(monkeypatch):
    monkeypatch.setattr(updater.urllib.request, "getproxies", lambda: {"no": "localhost,127.0.0.1"})
    monkeypatch.setattr(updater.urllib.request, "getproxies_registry", lambda: {"https": "http://127.0.0.1:7897"}, raising=False)
    monkeypatch.setattr(updater.urllib.request, "proxy_bypass", lambda _host: False)
    assert updater._system_proxy("api.github.com") == ("127.0.0.1", 7897)


def test_dns_timeout_is_bounded_and_abandoned_lookup_cannot_connect(monkeypatch):
    release = threading.Event()
    def slow(_host):
        release.wait(1)
        return ["93.184.216.34"]
    monkeypatch.setattr(updater, "_public_addresses", slow)
    monkeypatch.setattr(updater, "NETWORK_TIMEOUT", 0.01)
    started = time.monotonic()
    try:
        with pytest.raises(updater.UpdateError) as error:
            updater._resolve_bounded("updates.example.com", time.monotonic() + 1, None)
        assert error.value.code == "timeout" and time.monotonic() - started < 0.5
    finally:
        release.set()


def test_network_guard_interrupts_slow_headers_at_overall_deadline(monkeypatch):
    interrupted = threading.Event()
    class Sock:
        def shutdown(self, _how): interrupted.set()
    class Connection:
        socket_reference = Sock()
        def __init__(self, *_args): pass
        def request(self, *_args, **_kwargs): pass
        def getresponse(self):
            assert interrupted.wait(1)
            raise TimeoutError()
        def close(self): pass
    monkeypatch.setattr(updater, "_system_proxy", lambda _host: None)
    monkeypatch.setattr(updater, "_public_addresses", lambda _host: ["93.184.216.34"])
    monkeypatch.setattr(updater, "_PinnedHTTPS", Connection)
    started = time.monotonic()
    with pytest.raises(updater.UpdateError):
        updater.HTTPSFetcher().open("https://updates.example.com/a", {"updates.example.com"}, timeout=0.06)
    assert time.monotonic() - started < 0.5


@pytest.mark.parametrize("address", ["198.18.0.1", "224.0.0.1", "ff02::1"])
def test_reserved_or_multicast_addresses_are_not_public_download_targets(address):
    assert not updater._is_public_address(address)


def test_rename_failure_never_exposes_part_as_ready(service, monkeypatch):
    instance, _fetcher = service
    token = ready(instance)
    original = updater.os.replace
    def fail_asset(source, destination):
        if str(destination).endswith(".exe"):
            raise OSError("injected replace failure")
        return original(source, destination)
    monkeypatch.setattr(updater.os, "replace", fail_asset)
    instance.start_download(token)
    status = wait_download(instance)
    assert status["download"]["state"] == "failed"
    assert not list(instance.download_dir.iterdir())
