"""Offline checks for separating API billing from native Codex quota evidence."""
from email.message import Message
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_manager.core.catalog import _source_balance_text
from agent_manager.gateway.service import _apply_quota_headers, _codex_quota_headers
from agent_manager.usage.provider_quota_feedback import source_balance_text


@pytest.mark.parametrize("balance", [
    {"amount": 7.5, "limit": 10, "used": 2.5, "currency": "USD", "planName": "Team"},
    {"unlimited": True},
    {"amount": 7, "currency": "credits"},
    {"amount": 7, "expiresAt": "2033-05-18T03:33:20+00:00"},
    {"amount": 7, "resetAt": "2033-05-18T03:33:20+00:00", "windowMinutes": 10_080},
    {"amount": 7, "valid": False},
])
def test_provider_balance_never_becomes_native_codex_credits_or_window(balance):
    provider = {"balance": balance, "plan": "Team", "balanceError": "refresh failed"}
    assert _codex_quota_headers({}, provider) == {}
    assert _codex_quota_headers({}, {"balanceSnapshot": balance}) == {}


@pytest.mark.parametrize("invalid", [True, False, float("nan"), float("inf"), -float("inf"), "NaN", "Infinity"])
def test_invalid_amounts_do_not_become_balance_text_or_quota(invalid):
    balance = {"amount": invalid, "limit": invalid, "used": invalid, "currency": "USD"}
    assert source_balance_text(balance) == ""
    assert _codex_quota_headers({}, {"balance": balance}) == {}
    assert _codex_quota_headers({}, {"usage": {"weekly": {"remainingPercent": invalid}}}) == {}


def test_provider_with_native_upstream_headers_preserves_only_real_evidence():
    provider = {"kind": "custom", "balance": {"amount": 900, "currency": "USD"}}
    headers = _codex_quota_headers({
        "X-Codex-Credits-Balance": "12",
        "X-Codex-Credits-Unlimited": "false",
        "X-Codex-Primary-Used-Percent": "40",
        "X-Codex-Primary-Reset-At": "2000000000",
        "X-Codex-Plan-Type": "pro",
        "Set-Cookie": "secret",
        "Authorization": "secret",
    }, provider)
    assert headers == {
        "x-codex-credits-balance": "12",
        "x-codex-credits-unlimited": "false",
        "x-codex-primary-used-percent": "40",
        "x-codex-primary-reset-at": "2000000000",
        "x-codex-plan-type": "pro",
    }


def test_snapshot_expiry_is_not_a_quota_reset_and_is_visibly_expired():
    balance = {"amount": 4, "currency": "usd", "expiresAt": "2020-01-01T00:00:00Z"}
    assert _codex_quota_headers({}, {"balance": balance}) == {}
    rendered = _source_balance_text(balance, updated_at="2019-12-01T01:02:03Z")
    assert "余额 4 USD" in rendered
    assert "快照" in rendered and "已过期" in rendered
    assert "2019-12-01 01:02 UTC" in rendered


def test_snapshot_refresh_error_cannot_claim_failure_time_as_balance_observation():
    rendered = _source_balance_text(
        {"amount": 4, "currency": "USD"},
        updated_at="2033-05-18T03:33:20+00:00", error="private URL and credential",
    )
    assert "快照" in rendered and "刷新失败，非当前额度" in rendered
    assert "2033" not in rendered and "private" not in rendered


def test_missing_or_invalid_snapshot_freshness_remains_explicitly_unknown():
    balance = {"amount": 4, "currency": "USD\nsecret", "valid": "false"}
    rendered = source_balance_text(balance, updated_at="not a timestamp")
    assert "余额 4 额度" in rendered
    assert "时间未知" in rendered and "已失效" in rendered
    assert "secret" not in rendered and "\n" not in rendered
    assert "快照" in source_balance_text({"unlimited": True})
    assert source_balance_text(None, error="private") == " · 余额暂不可用（刷新失败）"


def test_official_weekly_fallback_preserves_upstream_and_rejects_expired_snapshot():
    account = {"plan": "free", "usage": {"weekly": {
        "remainingPercent": 81, "windowMinutes": 10_080,
        "resetAt": "2033-05-18T03:33:20+00:00",
    }}}
    with patch("agent_manager.usage.provider_quota_feedback.time.time", return_value=1_900_000_000):
        headers = _codex_quota_headers({"X-Codex-Primary-Used-Percent": "7"}, account)
    assert headers == {
        "x-codex-primary-used-percent": "7",
        "x-codex-primary-window-minutes": "10080",
        "x-codex-primary-reset-at": "2000000000",
        "x-codex-plan-type": "free",
    }
    with patch("agent_manager.usage.provider_quota_feedback.time.time", return_value=2_000_000_001):
        assert _codex_quota_headers({}, account) == {"x-codex-plan-type": "free"}


def test_apply_quota_headers_is_case_insensitive_and_does_not_override_upstream():
    target = Message()
    target["X-Codex-Credits-Balance"] = "12"
    response = SimpleNamespace(headers=target)
    _apply_quota_headers(response, {
        "x-codex-credits-balance": "999", "x-codex-plan-type": "pro", "Authorization": "secret",
    })
    assert target.get_all("X-Codex-Credits-Balance") == ["12"]
    assert target["x-codex-plan-type"] == "pro"
    assert target.get("Authorization") is None
    _apply_quota_headers(SimpleNamespace(), {"x-codex-plan-type": "pro"})
