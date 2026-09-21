"""Thread-safe, bounded rate limiting helpers.

The original fix notes describe a small sliding-window limiter.  This module
keeps that public API while adding two properties that matter for an HTTP
service: timestamps are based on a monotonic clock and client buckets are
bounded.  :class:`IPRateLimiter` deliberately trusts forwarded client headers
only when the immediate peer is configured as a trusted proxy.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping
import hashlib
import ipaddress
import threading
import time
from typing import Callable, TypeAlias


Clock: TypeAlias = Callable[[], float]
IPAddress: TypeAlias = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork: TypeAlias = ipaddress.IPv4Network | ipaddress.IPv6Network


class RateLimiter:
    """A thread-safe sliding-window rate limiter.

    ``max_requests`` requests from one client are accepted during each
    ``window_seconds`` interval.  The limiter retains at most ``max_clients``
    client buckets, evicting the least recently used bucket when necessary.
    Unknown clients are not inserted by :meth:`get_remaining`, which keeps a
    scan of arbitrary client IDs from growing the map on read-only requests.

    The optional ``clock`` argument is useful for deterministic tests and for
    callers that already have a monotonic clock.  Production callers should
    leave it unset so :func:`time.monotonic` is used.
    """

    _DEFAULT_MAX_CLIENTS = 10_000
    _MAX_CLIENT_ID_LENGTH = 256

    def __init__(
        self,
        max_requests: int = 10,
        window_seconds: float = 60,
        *,
        max_clients: int = _DEFAULT_MAX_CLIENTS,
        clock: Clock | None = None,
    ) -> None:
        if isinstance(max_requests, bool) or not isinstance(max_requests, int):
            raise TypeError("max_requests must be a positive integer")
        if max_requests < 1:
            raise ValueError("max_requests must be at least 1")
        if isinstance(window_seconds, bool) or not isinstance(window_seconds, (int, float)):
            raise TypeError("window_seconds must be a positive number")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be greater than 0")
        if isinstance(max_clients, bool) or not isinstance(max_clients, int):
            raise TypeError("max_clients must be a positive integer")
        if max_clients < 1:
            raise ValueError("max_clients must be at least 1")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")

        self.max_requests = max_requests
        self.window = float(window_seconds)
        # ``window_seconds`` was the name in the fix notes; keep a descriptive
        # alias for callers that prefer the full name.
        self.window_seconds = self.window
        self.max_clients = max_clients
        self._clock: Clock = clock or time.monotonic
        self._lock = threading.RLock()
        # Keep this attribute public for compatibility with the proposed
        # implementation and for operational introspection.  Values are
        # bounded lists of monotonic timestamps, never unbounded histories.
        self.requests: OrderedDict[str, list[float]] = OrderedDict()

    def _client_key(self, client_id: str) -> str:
        """Validate and bound a client key before it enters the map."""

        if not isinstance(client_id, str):
            raise TypeError("client_id must be a string")
        if not client_id:
            raise ValueError("client_id must not be empty")
        # A client identifier is data supplied by a caller or request.  Hash
        # oversized values instead of retaining attacker-controlled strings in
        # every bucket; the digest remains stable for reset/get_remaining.
        if len(client_id) > self._MAX_CLIENT_ID_LENGTH:
            return "sha256:" + hashlib.sha256(client_id.encode("utf-8")).hexdigest()
        return client_id

    def _purge_expired_locked(self, now: float) -> None:
        """Drop expired timestamps and empty buckets.

        The map is capped, so this bounded sweep is preferable to maintaining
        another unbounded expiry queue.  It also makes memory reclamation
        deterministic when traffic moves between clients.
        """

        expired_clients: list[str] = []
        for client_id, timestamps in self.requests.items():
            fresh = [timestamp for timestamp in timestamps if now - timestamp < self.window]
            if fresh:
                self.requests[client_id] = fresh
            else:
                expired_clients.append(client_id)
        for client_id in expired_clients:
            self.requests.pop(client_id, None)

    def is_allowed(self, client_id: str) -> bool:
        """Record a request and return whether it is within the limit."""

        key = self._client_key(client_id)
        with self._lock:
            now = self._clock()
            self._purge_expired_locked(now)
            timestamps = self.requests.get(key)
            if timestamps is None:
                if len(self.requests) >= self.max_clients:
                    self.requests.popitem(last=False)
                timestamps = []
                self.requests[key] = timestamps
            else:
                self.requests.move_to_end(key)

            if len(timestamps) >= self.max_requests:
                return False
            timestamps.append(now)
            return True

    def get_remaining(self, client_id: str) -> int:
        """Return how many requests ``client_id`` may still make."""

        key = self._client_key(client_id)
        with self._lock:
            now = self._clock()
            self._purge_expired_locked(now)
            timestamps = self.requests.get(key)
            if timestamps is None:
                return self.max_requests
            self.requests.move_to_end(key)
            return max(0, self.max_requests - len(timestamps))

    def retry_after(self, client_id: str) -> float:
        """Return seconds until the oldest counted request expires.

        A value of ``0.0`` means the client is currently allowed (or unknown).
        This helper is intentionally advisory; callers should still call
        :meth:`is_allowed` when accepting a request.
        """

        key = self._client_key(client_id)
        with self._lock:
            now = self._clock()
            self._purge_expired_locked(now)
            timestamps = self.requests.get(key)
            if not timestamps or len(timestamps) < self.max_requests:
                return 0.0
            return max(0.0, self.window - (now - timestamps[0]))

    def reset(self, client_id: str) -> None:
        """Forget all requests for ``client_id``."""

        key = self._client_key(client_id)
        with self._lock:
            self.requests.pop(key, None)

    def clear(self) -> None:
        """Forget all client buckets."""

        with self._lock:
            self.requests.clear()

    def __len__(self) -> int:
        """Return the current number of retained client buckets."""

        with self._lock:
            return len(self.requests)


class IPRateLimiter(RateLimiter):
    """Rate limiter that derives a key from an HTTP client's IP address.

    Forwarded headers can be forged by a direct client.  By default they are
    therefore ignored and the socket peer (``remote_addr`` or a
    ``Remote-Addr`` header) is used.  When a deployment explicitly configures
    ``trusted_proxies``, an X-Forwarded-For chain is evaluated from right to
    left and the first address outside those trusted networks is selected.
    """

    def __init__(
        self,
        max_requests: int = 10,
        window_seconds: float = 60,
        *,
        max_clients: int = RateLimiter._DEFAULT_MAX_CLIENTS,
        clock: Clock | None = None,
        trusted_proxies: Iterable[str | IPNetwork | IPAddress] | None = None,
    ) -> None:
        super().__init__(
            max_requests,
            window_seconds,
            max_clients=max_clients,
            clock=clock,
        )
        self._trusted_proxies = self._parse_trusted_proxies(trusted_proxies)

    @staticmethod
    def _parse_trusted_proxies(
        trusted_proxies: Iterable[str | IPNetwork | IPAddress] | None,
    ) -> tuple[IPNetwork, ...]:
        if trusted_proxies is None:
            return ()
        if isinstance(trusted_proxies, (str, bytes)):
            trusted_proxies = (trusted_proxies.decode() if isinstance(trusted_proxies, bytes) else trusted_proxies,)
        parsed: list[IPNetwork] = []
        for value in trusted_proxies:
            try:
                if isinstance(value, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
                    network = value
                elif isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
                    network = ipaddress.ip_network(f"{value}/{value.max_prefixlen}")
                else:
                    network = ipaddress.ip_network(str(value).strip(), strict=False)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid trusted proxy network: {value!r}") from exc
            parsed.append(network)
        return tuple(parsed)

    @staticmethod
    def _header_value(headers: Mapping[str, object], name: str) -> str:
        wanted = name.casefold()
        try:
            items = headers.items()
        except AttributeError:
            return ""
        for key, value in items:
            if str(key).casefold() == wanted:
                if isinstance(value, (list, tuple)):
                    value = value[0] if value else ""
                return str(value or "").strip()
        return ""

    @staticmethod
    def _parse_ip(value: object, *, allow_port: bool = False) -> IPAddress | None:
        if value is None:
            return None
        if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
            return value
        if isinstance(value, tuple) and value:
            value = value[0]
        text = str(value).strip().strip('"')
        if not text:
            return None
        # Socket peers and some proxy implementations use [IPv6]:port.
        if text.startswith("[") and "]" in text:
            end = text.find("]")
            address = text[1:end]
            remainder = text[end + 1 :]
            if remainder and (not allow_port or not remainder.startswith(":")):
                return None
            text = address
        try:
            return ipaddress.ip_address(text)
        except ValueError:
            if allow_port and text.count(":") == 1:
                host, port = text.rsplit(":", 1)
                if port.isdigit():
                    try:
                        return ipaddress.ip_address(host)
                    except ValueError:
                        return None
            return None

    @classmethod
    def _safe_peer_id(cls, value: object) -> str:
        parsed = cls._parse_ip(value, allow_port=True)
        if parsed is not None:
            return str(parsed)
        text = str(value or "").strip()
        if not text or len(text) > RateLimiter._MAX_CLIENT_ID_LENGTH:
            return "unknown"
        # A non-IP peer name is useful in tests and in local adapters, but do
        # not let control characters become a key or log injection vector.
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in text):
            return "unknown"
        return text

    def _is_trusted(self, address: IPAddress) -> bool:
        return any(address in network for network in self._trusted_proxies)

    def extract_client_id(
        self,
        request_headers: Mapping[str, object] | None,
        remote_addr: object | None = None,
    ) -> str:
        """Extract a stable client key from headers and an optional socket peer.

        ``remote_addr`` should be the actual socket peer when the HTTP server
        exposes it (for example ``handler.client_address[0]``).  It takes
        precedence over a same-named request header.  The header fallback is
        retained for adapters that only provide a header mapping.
        """

        headers: Mapping[str, object] = request_headers or {}
        peer_value = remote_addr
        if peer_value is None:
            for name in ("Remote-Addr", "Remote_Addr", "X-Peer-IP"):
                candidate = self._header_value(headers, name)
                if candidate:
                    peer_value = candidate
                    break
        peer_id = self._safe_peer_id(peer_value)
        peer_ip = self._parse_ip(peer_value, allow_port=True)

        # Never honor user-provided forwarding headers without an explicit
        # trusted proxy configuration and a trusted immediate peer.
        if peer_ip is None or not self._is_trusted(peer_ip):
            return peer_id

        forwarded = self._header_value(headers, "X-Forwarded-For")
        if forwarded:
            tokens = [token.strip() for token in forwarded.split(",")]
            # Bound parsing work for malformed headers.  Valid deployments
            # rarely need more than a few hops; a malformed long chain should
            # fail closed to the trusted proxy key.
            if 0 < len(tokens) <= 32:
                addresses: list[IPAddress] = []
                for token in tokens:
                    address = self._parse_ip(token)
                    if address is None:
                        break
                    addresses.append(address)
                else:
                    for address in reversed(addresses):
                        if not self._is_trusted(address):
                            return str(address)
                    # Every hop is trusted.  The leftmost value is the
                    # configured proxy chain's original client address.
                    return str(addresses[0])

        # Some trusted proxies emit X-Real-IP but no X-Forwarded-For.  It is
        # only accepted after the immediate peer check above.
        real_ip = self._parse_ip(self._header_value(headers, "X-Real-IP"))
        if real_ip is not None:
            return str(real_ip)
        return peer_id
