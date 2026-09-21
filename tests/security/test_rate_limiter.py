"""Focused tests for the bounded rate limiter and trusted proxy handling."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_manager.core.rate_limiter import IPRateLimiter, RateLimiter


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def test_sliding_window_and_remaining() -> None:
    clock = FakeClock()
    limiter = RateLimiter(max_requests=2, window_seconds=10, clock=clock)

    assert limiter.get_remaining("client") == 2
    assert limiter.is_allowed("client") is True
    assert limiter.is_allowed("client") is True
    assert limiter.is_allowed("client") is False
    assert limiter.get_remaining("client") == 0

    clock.value = 10.001
    assert limiter.is_allowed("client") is True
    assert limiter.get_remaining("client") == 1


def test_reset_and_unknown_reads_do_not_create_buckets() -> None:
    limiter = RateLimiter(max_requests=1, max_clients=2)

    assert limiter.get_remaining("unknown") == 1
    assert len(limiter) == 0
    assert limiter.is_allowed("first") is True
    limiter.reset("first")
    assert limiter.get_remaining("first") == 1
    assert len(limiter) == 0


def test_client_bucket_memory_is_bounded_and_oldest_is_evicted() -> None:
    limiter = RateLimiter(max_requests=1, max_clients=2)

    assert limiter.is_allowed("first") is True
    assert limiter.is_allowed("second") is True
    assert limiter.is_allowed("third") is True

    assert len(limiter) == 2
    assert limiter.get_remaining("first") == 1
    assert limiter.get_remaining("second") == 0
    assert limiter.get_remaining("third") == 0


def test_concurrent_requests_are_counted_atomically() -> None:
    limiter = RateLimiter(max_requests=5, window_seconds=60)

    with ThreadPoolExecutor(max_workers=32) as executor:
        results = list(executor.map(lambda _: limiter.is_allowed("same-client"), range(100)))

    assert sum(results) == 5
    assert limiter.get_remaining("same-client") == 0


def test_oversized_client_ids_are_bounded_before_storage() -> None:
    limiter = RateLimiter(max_requests=1)
    client_id = "x" * 10_000

    assert limiter.is_allowed(client_id) is True
    assert len(next(iter(limiter.requests))) < 100
    assert limiter.get_remaining(client_id) == 0


def test_direct_clients_cannot_spoof_forwarded_headers() -> None:
    limiter = IPRateLimiter(max_requests=1)

    headers = {
        "X-Forwarded-For": "198.51.100.10",
        "X-Real-IP": "198.51.100.10",
    }
    assert limiter.extract_client_id(headers, remote_addr="203.0.113.20") == "203.0.113.20"


def test_trusted_proxy_selects_first_untrusted_forwarded_hop() -> None:
    limiter = IPRateLimiter(
        max_requests=1,
        trusted_proxies=("10.0.0.0/8", "192.0.2.10"),
    )

    headers = {"x-forwarded-for": "198.51.100.10, 10.1.2.3"}
    assert limiter.extract_client_id(headers, remote_addr="192.0.2.10") == "198.51.100.10"

    # A socket peer must take precedence over a forged Remote-Addr header.
    headers["Remote-Addr"] = "198.51.100.99"
    assert limiter.extract_client_id(headers, remote_addr="192.0.2.10") == "198.51.100.10"


def test_trusted_proxy_falls_back_safely_for_malformed_forwarding_chain() -> None:
    limiter = IPRateLimiter(max_requests=1, trusted_proxies=("192.0.2.10",))

    headers = {
        "X-Forwarded-For": "not-an-ip, 198.51.100.10",
        "X-Real-IP": "198.51.100.20",
    }
    assert limiter.extract_client_id(headers, remote_addr="192.0.2.10") == "198.51.100.20"
    assert limiter.extract_client_id(headers, remote_addr="198.51.100.11") == "198.51.100.11"


def test_invalid_configuration_and_client_ids_fail_early() -> None:
    with pytest.raises(ValueError):
        RateLimiter(max_requests=0)
    with pytest.raises(ValueError):
        IPRateLimiter(trusted_proxies=("not-an-ip",))

    limiter = RateLimiter()
    with pytest.raises(TypeError):
        limiter.is_allowed(1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        limiter.is_allowed("")
