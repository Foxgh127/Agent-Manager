from types import SimpleNamespace
import subprocess
import pytest
import codex_desktop_updates as updates

DESKTOP = {"version": "26.901.6511.0", "appUserModelId": "OpenAI.Codex_2p2nqsd0c76g0!App"}


@pytest.mark.parametrize("text,state", [("✅ 'ChatGPT' is already up to date", False),
    ("Error: Could not find installed product metadata.", None),
    ("Update available: 26.909.1.0", True), ("Checking updates…", None),
    ("No updates available", False), ("Checking updates failed; already up to date cache", None)])
def test_store_parser_never_treats_exit_zero_as_success(text, state):
    assert updates.parse_store_check(text)["updateAvailable"] is state


def test_exact_installed_family_and_check_has_no_apply_flag():
    calls = []
    def runner(command, timeout):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="'ChatGPT' is already up to date", stderr="")
    status = updates.check(DESKTOP, runner=runner, executable="store.exe")
    assert status["updateAvailable"] is False
    assert calls == [["store.exe", "update", "OpenAI.Codex_2p2nqsd0c76g0"]]
    assert status["manualInApp"] is False


def test_apply_checks_again_and_verifies_installed_version():
    calls = []
    output = iter(["Update available: 26.909.1.0", "Updated successfully", "already up to date"])
    def runner(command, timeout):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=next(output), stderr="")
    result = updates.apply(DESKTOP, detector=lambda **kw: {**DESKTOP, "version":"26.909.1.0"}, runner=runner, executable="store.exe")
    assert result["updated"] is True
    assert calls[1] == ["store.exe", "update", "OpenAI.Codex_2p2nqsd0c76g0", "--apply"]


def test_store_success_without_version_change_is_pending():
    output = iter(["Update available", "Updated successfully", "already up to date"])
    result = updates.apply(DESKTOP, detector=lambda **kw: DESKTOP,
        runner=lambda *args: SimpleNamespace(returncode=0, stdout=next(output), stderr=""), executable="store.exe")
    assert not result["updated"] and result["pendingVerification"]


def test_unrecognized_package_never_invokes_cli():
    result = updates.check({**DESKTOP, "appUserModelId":"Unrelated.App_xyz!App"}, executable="store.exe",
        runner=lambda *a: pytest.fail("must not call store for an unverified package"))
    assert result["updateAvailable"] is None and not result["canAutoUpdate"]


def test_timeout_is_unknown_not_latest():
    def run(*args): raise subprocess.TimeoutExpired("store", 50)
    result = updates.check(DESKTOP, runner=run, executable="store.exe")
    assert result["updateAvailable"] is None and result["updateState"] == "check_failed"
