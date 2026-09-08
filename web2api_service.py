#!/usr/bin/env python3
"""Authenticated loopback OpenAI-compatible gateway for saved ChatGPT sessions."""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request
import uuid
from zoneinfo import ZoneInfo

import agent_manager_core as core


UPSTREAM_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
UPSTREAM_ALPHA_SEARCH_URL = "https://chatgpt.com/backend-api/codex/alpha/search"
MAX_REQUEST_BYTES = 8_000_000
MAX_RESPONSE_BYTES = 32_000_000
MAX_SSE_LINE_BYTES = 8_000_000
MAX_SSE_EVENT_BYTES = 8_000_000
STREAM_PREOUTPUT_MAX_BYTES = 1_000_000
STREAM_PREOUTPUT_MAX_SECONDS = 2.0
UPSTREAM_OPEN_TIMEOUT_SECONDS = 45.0
UPSTREAM_STREAM_IDLE_TIMEOUT_SECONDS = 180.0
# Kept as the public compatibility name for callers/tests that import it.
UPSTREAM_TIMEOUT_SECONDS = UPSTREAM_OPEN_TIMEOUT_SECONDS
UPSTREAM_COOLDOWN_DEFAULT_SECONDS = 2.0
UPSTREAM_COOLDOWN_MIN_SECONDS = 0.25
UPSTREAM_COOLDOWN_MAX_SECONDS = 300.0
UPSTREAM_CAPACITY_MAX_ATTEMPTS = 2
RESPONSE_BINDING_TTL_SECONDS = 3_600.0
RESPONSE_BINDING_MAX_ENTRIES = 2_048
RESPONSE_BINDING_MAX_ID_BYTES = 1_024
MAX_GATEWAY_THREADS = 32
GATEWAY_CLIENT_TIMEOUT_SECONDS = 20.0
MAX_UPSTREAM_CONCURRENCY = 8
RESPONSES_AUXILIARY_PATHS = {"/v1/responses/compact", "/v1/responses/input_tokens"}
UPSTREAM_QUEUE_TIMEOUT_SECONDS = 2.0
SAFE_FORWARD_HEADERS = {"openai-model", "x-reasoning-included", "retry-after", "x-codex-history-compatibility"}
CODEX_RESPONSES_LITE_HEADER = "X-OpenAI-Internal-Codex-Responses-Lite"
SAFE_CLIENT_IDENTITY_HEADERS = (
    "User-Agent",
    "Originator",
    "Version",
    "Session-Id",
    "Session_id",
    "X-Session-ID",
    "X-Client-Request-Id",
    "X-Codex-Window-Id",
    "Thread-Id",
    "X-OpenAI-Actor-Authorization",
    CODEX_RESPONSES_LITE_HEADER,
)
USAGE_STATS_SCHEMA_VERSION = 5
USAGE_TIMEZONE_NAME = "Asia/Shanghai"
USAGE_TIMEZONE = ZoneInfo(USAGE_TIMEZONE_NAME)
USAGE_STATS_FILE_NAME = "web2api-usage.json"
USAGE_STATS_RETENTION_DAYS = 370
USAGE_STATS_MAX_ROUTES_PER_DAY = 512
USAGE_STATS_MAX_RECENT_REQUESTS = 1_000
USAGE_STATS_MAX_FILE_BYTES = 16_000_000
USAGE_STATS_FLUSH_SECONDS = 15.0
USAGE_STATS_FLUSH_EVENTS = 20
USAGE_STATS_LOCK_TIMEOUT_SECONDS = 0.5
USAGE_COUNTER_FIELDS = (
    "requestCount",
    "failureCount",
    "usageReportedCount",
    "usageMissingCount",
    "inputTokens",
    "cachedInputTokens",
    "cacheWriteTokens",
    "outputTokens",
    "reasoningOutputTokens",
    "totalTokens",
)


def protocol_capabilities() -> dict:
    """Static, credential-free support contract for the pool UI and /status."""
    return {
        "schemaVersion": 1,
        "transports": {"http": True, "sse": True, "websocket": True,
                       "websocketMode": "serial_http_sse_bridge", "nativeUpstreamWebsocket": False},
        "endpoints": {
            "responses": {"path": "/v1/responses", "account": "native_http", "provider": "passthrough"},
            "chatCompletions": {"path": "/v1/chat/completions", "account": "responses_conversion", "provider": "passthrough"},
            "compact": {"path": "/v1/responses/compact", "account": "native_compaction_trigger_bridge", "provider": "passthrough"},
            "inputTokens": {"path": "/v1/responses/input_tokens", "account": "unsupported", "provider": "passthrough_no_generation_stats"},
        },
        "features": {"functionTools": True, "customTools": True, "reasoningSummary": True,
                     "structuredOutputs": True, "chatStreamUsage": True,
                     "responseIdentityBinding": True, "replayAfterOutput": False,
                     "statelessHistoryCompatibility": "explicit_encrypted_rejection_only"},
        "limitations": ["upstream_endpoint_support_required", "websocket_one_response_at_a_time",
                        "no_websocket_warmup_multiplexing_steering", "websocket_continuation_uses_http_state",
                        "no_response_retrieve_delete_cancel_api", "no_oauth_token_count_estimate"],
    }


def _identity_fingerprint(kind: str, identity: str, secret: str) -> str:
    # Digests stay in the bounded in-memory binding table, never public status.
    return hashlib.sha256(json.dumps([kind, identity, secret], separators=(",", ":")).encode("utf-8")).hexdigest()


def _oauth_binding_fingerprint(credentials: dict) -> str:
    token = str(credentials.get("accessToken") or "")
    claims = core._jwt_payload(token)
    subject = str(claims.get("sub") or "")
    # A normal OAuth access-token refresh preserves subject/workspace. Opaque
    # tokens without verifiable claims bind to the exact credential instead.
    return _identity_fingerprint("account", str(credentials.get("accountId") or ""), subject or token)


def _usage_stats_path() -> Path:
    return Path(core.STATE_DIR) / USAGE_STATS_FILE_NAME


def _empty_usage_counters() -> dict[str, int]:
    return {field: 0 for field in USAGE_COUNTER_FIELDS}


def _empty_usage_document() -> dict:
    return {
        "schemaVersion": USAGE_STATS_SCHEMA_VERSION,
        "counterGeneration": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        "timeZone": USAGE_TIMEZONE_NAME,
        "dayBoundary": "00:00",
        "updatedAt": None,
        "totals": _empty_usage_counters(),
        "days": {},
        "recentRequests": [],
    }


def _usage_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _beijing_usage_day(value: Any) -> str:
    parsed = _usage_timestamp(value)
    return parsed.astimezone(USAGE_TIMEZONE).date().isoformat() if parsed else ""


def _agent_role_from_classification(value: Any) -> str:
    classification = str(value or "").strip()[:80].casefold()
    if classification == "explicit_main_agent":
        return "mainAgent"
    if classification == "explicit_subagent":
        return "subagent"
    return "unclassified"


def _bounded_text(value: Any, limit: int = 200, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text[:limit] if text else fallback


def _nonnegative_token_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed < 0:
        return None
    return min(parsed, 10**15)


def _normalized_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    input_tokens = _nonnegative_token_count(
        value.get("input_tokens", value.get("prompt_tokens", value.get("inputTokens")))
    )
    output_tokens = _nonnegative_token_count(
        value.get("output_tokens", value.get("completion_tokens", value.get("outputTokens")))
    )
    total_tokens = _nonnegative_token_count(value.get("total_tokens", value.get("totalTokens")))
    input_details = value.get("input_tokens_details")
    if not isinstance(input_details, dict):
        input_details = value.get("prompt_tokens_details")
    if not isinstance(input_details, dict):
        input_details = {}
    output_details = value.get("output_tokens_details")
    if not isinstance(output_details, dict):
        output_details = value.get("completion_tokens_details")
    if not isinstance(output_details, dict):
        output_details = {}
    cached_input_tokens = _nonnegative_token_count(
        input_details.get(
            "cached_tokens",
            value.get("cached_input_tokens", value.get("cachedInputTokens", value.get("cachedTokens"))),
        )
    )
    cache_write_tokens = _nonnegative_token_count(
        input_details.get(
            "cache_write_tokens",
            value.get(
                "cache_write_tokens",
                value.get("cache_write_input_tokens", value.get("cacheWriteTokens")),
            ),
        )
    )
    reasoning_output_tokens = _nonnegative_token_count(
        output_details.get(
            "reasoning_tokens",
            value.get(
                "reasoning_output_tokens",
                value.get("reasoningOutputTokens", value.get("reasoningTokens")),
            ),
        )
    )
    if input_tokens is not None and cached_input_tokens is not None:
        cached_input_tokens = min(cached_input_tokens, input_tokens)
    if output_tokens is not None and reasoning_output_tokens is not None:
        reasoning_output_tokens = min(reasoning_output_tokens, output_tokens)
    if (
        input_tokens is None
        and output_tokens is None
        and total_tokens is None
        and cached_input_tokens is None
        and cache_write_tokens is None
        and reasoning_output_tokens is None
    ):
        return None
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return {
        "inputTokens": input_tokens or 0,
        # Cached input and reasoning output are subsets of input/output.  They
        # are deliberately reported separately and never added to totalTokens.
        "cachedInputTokens": cached_input_tokens or 0,
        "cacheWriteTokens": cache_write_tokens or 0,
        "outputTokens": output_tokens or 0,
        "reasoningOutputTokens": reasoning_output_tokens or 0,
        "totalTokens": total_tokens or 0,
    }


def _payload_usage(payload: Any) -> dict[str, int] | None:
    if not isinstance(payload, dict):
        return None
    direct = _normalized_usage(payload.get("usage"))
    if direct:
        return direct
    response = payload.get("response")
    if isinstance(response, dict):
        return _normalized_usage(response.get("usage"))
    return None


def _payload_failed(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    event_type = _bounded_text(payload.get("type"), 80).casefold()
    if event_type in {"error", "response.failed"} or payload.get("error"):
        return True
    response = payload.get("response")
    status = _bounded_text(
        response.get("status") if isinstance(response, dict) else payload.get("status"),
        40,
    ).casefold()
    return status == "failed"


def _explicit_error_object(payload: Any) -> dict | None:
    """Return only an upstream-owned structured error object.

    Request echoes and free-form top-level messages are deliberately ignored so
    they cannot turn quota, authentication, or policy failures into retries.
    """

    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        return error
    response = payload.get("response")
    if isinstance(response, dict) and isinstance(response.get("error"), dict):
        return response["error"]
    body = payload.get("body")
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return body["error"]
    return None


def _capacity_error_object(payload: Any) -> dict | None:
    error = _explicit_error_object(payload)
    if error is None:
        return None
    code = str(error.get("code") or "").strip().casefold()
    kind = str(error.get("type") or "").strip().casefold()
    if code and code not in {"server_is_overloaded", "slow_down"}:
        return None
    if kind in {
        "usage_limit_reached",
        "rate_limit_error",
        "rate_limit_exceeded",
        "authentication_error",
        "permission_error",
    }:
        return None
    if code in {"server_is_overloaded", "slow_down"}:
        return error
    message = str(error.get("message") or "").strip().casefold()
    if any(
        marker in message
        for marker in (
            "selected model is at capacity",
            "servers are currently overloaded",
            "servers are overloaded",
            "server is overloaded",
        )
    ):
        return error
    return None


def _capacity_error_from_body(body: bytes) -> dict | None:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    error = _capacity_error_object(payload)
    if error is not None:
        return error
    for event in _sse_events(body):
        error = _capacity_error_object(event)
        if error is not None:
            return error
    return None


def _quota_error_kind(payload: Any) -> str:
    error = _explicit_error_object(payload)
    if error is None:
        return ""
    code = str(error.get("code") or "").strip().casefold()
    kind = str(error.get("type") or "").strip().casefold()
    if kind == "usage_limit_reached" or code == "usage_limit_reached":
        return "quota_exhausted"
    if kind in {"rate_limit_error", "rate_limit_exceeded"} or code == "rate_limit_exceeded":
        return "rate_limited"
    return ""


class _SSEUsageCapture:
    """Incrementally observe terminal SSE usage without retaining response bodies."""

    MAX_BUFFER_BYTES = MAX_SSE_EVENT_BYTES

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.usage: dict[str, int] | None = None
        self.failed = False
        self.done = False
        self.response_terminal = False
        self.terminal_type: str | None = None
        self.semantic_output = False
        self.overflowed = False
        self.last_sequence_number: int | None = None
        self.rate_limited = False
        self.quota_exhausted = False
        self.capacity_limited = False
        self.capacity_error: dict | None = None
        self.credits_usable: bool | None = None
        self.retry_after_seconds: float | None = None
        self.response_id: str | None = None

    def observe_event(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        usage = _payload_usage(event)
        if usage:
            self.usage = usage
        self.failed = self.failed or _payload_failed(event)
        event_type = _bounded_text(event.get("type"), 80).casefold()
        response = event.get("response") if isinstance(event.get("response"), dict) else {}
        response_id = response.get("id")
        if not response_id and (
            str(event.get("object") or "").casefold() == "response"
            or event_type in {"response.created", "response.completed", "response.incomplete"}
            or event.get("status") in {"completed", "incomplete"}
        ):
            response_id = event.get("id")
        if isinstance(response_id, str):
            normalized_response_id = response_id.strip()
            if (
                normalized_response_id
                and len(normalized_response_id.encode("utf-8", errors="ignore"))
                <= RESPONSE_BINDING_MAX_ID_BYTES
            ):
                self.response_id = normalized_response_id
        error = event.get("error") or response.get("error")
        if isinstance(error, dict):
            error_kind = str(error.get("code") or error.get("type") or "").strip().casefold()
            self.rate_limited = self.rate_limited or error_kind == "rate_limit_exceeded"
            quota_kind = _quota_error_kind(event)
            self.quota_exhausted = self.quota_exhausted or quota_kind == "quota_exhausted"
            self.rate_limited = self.rate_limited or quota_kind == "rate_limited"
            capacity_error = _capacity_error_object(event)
            if capacity_error is not None:
                self.capacity_limited = True
                self.capacity_error = dict(capacity_error)
            raw_retry_after = (
                error.get("retry_after_seconds")
                if error.get("retry_after_seconds") is not None
                else error.get("retry_after")
                if error.get("retry_after") is not None
                else error.get("retryAfter")
            )
            try:
                retry_after = float(raw_retry_after)
            except (TypeError, ValueError, OverflowError):
                retry_after = -1.0
            if math.isfinite(retry_after) and retry_after >= 0:
                self.retry_after_seconds = retry_after
            raw_retry_after_ms = error.get("retry_after_ms", error.get("retryAfterMs"))
            try:
                retry_after_ms = float(raw_retry_after_ms)
            except (TypeError, ValueError, OverflowError):
                retry_after_ms = -1.0
            if math.isfinite(retry_after_ms) and retry_after_ms >= 0:
                self.retry_after_seconds = retry_after_ms / 1000.0
        observed_credits = _credits_usable_from_event(event)
        if observed_credits is not None:
            self.credits_usable = observed_credits
        sequence_number = event.get("sequence_number")
        if isinstance(sequence_number, int) and not isinstance(sequence_number, bool):
            self.last_sequence_number = sequence_number
        if event_type in {
            "response.output_text.delta",
            "response.function_call_arguments.delta",
            "response.custom_tool_call_input.delta",
            "response.output_item.added",
            "response.output_item.done",
        }:
            self.semantic_output = True
        if event_type in {"response.completed", "response.failed", "response.incomplete", "error"}:
            self.done = True
            self.response_terminal = True
            self.terminal_type = event_type
        if event.get("status") in {"completed", "incomplete", "failed"}:
            self.done = True
            self.response_terminal = True
            self.terminal_type = str(event.get("status"))
        choices = event.get("choices")
        if isinstance(choices, list) and any(isinstance(choice, dict) and choice.get("finish_reason") is not None for choice in choices):
            self.done = True

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.buffer.extend(chunk)
        while True:
            lf = self.buffer.find(b"\n\n")
            crlf = self.buffer.find(b"\r\n\r\n")
            candidates = [(index, size) for index, size in ((lf, 2), (crlf, 4)) if index >= 0]
            if not candidates:
                if len(self.buffer) > self.MAX_BUFFER_BYTES:
                    self.overflowed = True
                    self.failed = True
                    self.buffer.clear()
                return
            index, separator_size = min(candidates, key=lambda item: item[0])
            if index > self.MAX_BUFFER_BYTES:
                self.overflowed = True
                self.failed = True
                self.buffer.clear()
                return
            block = bytes(self.buffer[:index])
            del self.buffer[: index + separator_size]
            data_lines = [
                line[5:].lstrip()
                for line in block.decode("utf-8", errors="replace").replace("\r\n", "\n").splitlines()
                if line.startswith("data:")
            ]
            if not data_lines:
                continue
            raw = "\n".join(data_lines)
            if raw == "[DONE]":
                self.done = True
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            self.observe_event(event)

    def finish(self) -> None:
        if not self.buffer:
            return
        remaining = bytes(self.buffer)
        self.buffer.clear()
        text = remaining.decode("utf-8", errors="replace").replace("\r\n", "\n")
        data_lines = [line[5:].lstrip() for line in text.splitlines() if line.startswith("data:")]
        raw = "\n".join(data_lines) if data_lines else text
        if raw == "[DONE]":
            self.done = True
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        self.observe_event(payload)


def _inspect_usage_body(
    body: bytes,
    capture: _SSEUsageCapture | None = None,
) -> tuple[dict[str, int] | None, bool]:
    capture = capture or _SSEUsageCapture()
    capture.feed(body)
    capture.finish()
    if capture.usage or capture.failed:
        return capture.usage, capture.failed
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, False
    return _payload_usage(payload), _payload_failed(payload)


@contextmanager
def _exclusive_usage_file_lock(lock_path: Path, timeout: float = USAGE_STATS_LOCK_TIMEOUT_SECONDS):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    acquired = False
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("usage stats lock timeout")
                time.sleep(0.01)
        yield
    finally:
        if acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


class UsageStatsStore:
    """Bounded, non-sensitive token counters with batched atomic persistence."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or _usage_stats_path())
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.lock = threading.RLock()
        self.io_lock = threading.RLock()
        self.pending: dict[tuple[str, str], dict] = {}
        self.pending_recent: list[dict] = []
        self.dirty_events = 0
        self.last_flush = time.monotonic()
        self.flush_timer: threading.Timer | None = None
        self.generation = 0
        self.counter_generation_needs_persist = True

    @staticmethod
    def _route_key(route: dict) -> str:
        identity = {
            key: route.get(key)
            for key in (
                "source",
                "sourceKind",
                "sourceRecordId",
                "accountId",
                "providerId",
                "requestedModel",
                "routedModel",
                "routeKey",
                "requestClassification",
                "agentRole",
            )
        }
        encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:24]

    @staticmethod
    def _safe_route(raw_route: dict) -> dict:
        route = {
            "source": _bounded_text(raw_route.get("source"), 80, "unclassified"),
            "sourceKind": _bounded_text(raw_route.get("sourceKind"), 80, "unknown"),
            "sourceRecordId": _bounded_text(raw_route.get("sourceRecordId"), 200),
            "accountId": _bounded_text(raw_route.get("accountId"), 200),
            "providerId": _bounded_text(raw_route.get("providerId"), 200),
            "requestedModel": _bounded_text(raw_route.get("requestedModel"), 200, "unknown"),
            "routedModel": _bounded_text(raw_route.get("routedModel"), 200, "unknown"),
            "routeKey": _bounded_text(raw_route.get("routeKey"), 240),
            "requestClassification": _bounded_text(
                raw_route.get("requestClassification"), 80, "unclassified"
            ),
            "configuredForSubagents": bool(raw_route.get("configuredForSubagents")),
            "firstSeenAt": _bounded_text(raw_route.get("firstSeenAt"), 80),
            "lastSeenAt": _bounded_text(
                raw_route.get("lastSeenAt") or raw_route.get("timestamp"), 80
            ),
        }
        claimed_role = _bounded_text(raw_route.get("agentRole"), 40)
        route["agentRole"] = (
            claimed_role
            if claimed_role in {"mainAgent", "subagent", "unclassified"}
            else _agent_role_from_classification(route["requestClassification"])
        )
        for field in USAGE_COUNTER_FIELDS:
            route[field] = _nonnegative_token_count(raw_route.get(field)) or 0
        if _nonnegative_token_count(raw_route.get("inputTokens")) is not None:
            route["cachedInputTokens"] = min(route["cachedInputTokens"], route["inputTokens"])
        if _nonnegative_token_count(raw_route.get("outputTokens")) is not None:
            route["reasoningOutputTokens"] = min(
                route["reasoningOutputTokens"],
                route["outputTokens"],
            )
        return route

    @staticmethod
    def _safe_document(payload: Any) -> dict:
        document = _empty_usage_document()
        if not isinstance(payload, dict) or not isinstance(payload.get("days"), dict):
            return document
        if (payload.get("schemaVersion") == USAGE_STATS_SCHEMA_VERSION
                and re.fullmatch(r"[0-9a-f]{64}", str(payload.get("counterGeneration") or ""))):
            document["counterGeneration"] = payload["counterGeneration"]
        for day, raw_day in payload["days"].items():
            day_text = str(day or "").strip()
            if (
                not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day_text)
                or _safe_date_ordinal(day_text) <= 0
                or not isinstance(raw_day, dict)
            ):
                continue
            raw_routes = raw_day.get("routes") if isinstance(raw_day.get("routes"), dict) else {}
            safe_routes = {}
            for raw_route in raw_routes.values():
                if not isinstance(raw_route, dict):
                    continue
                route = UsageStatsStore._safe_route(raw_route)
                key = UsageStatsStore._route_key(route)
                if key not in safe_routes and len(safe_routes) >= USAGE_STATS_MAX_ROUTES_PER_DAY:
                    key = "overflow"
                    safe_routes.setdefault(
                        key,
                        {
                            "source": "overflow",
                            "sourceKind": "overflow",
                            "sourceRecordId": "",
                            "accountId": "",
                            "providerId": "",
                            "requestedModel": "other",
                            "routedModel": "other",
                            "routeKey": "overflow",
                            "requestClassification": "unclassified",
                            "configuredForSubagents": False,
                            "agentRole": "unclassified",
                            **_empty_usage_counters(),
                        },
                    )
                target = safe_routes.setdefault(key, {**route, **_empty_usage_counters()})
                UsageStatsStore._merge_route(target, route)
            totals = _empty_usage_counters()
            for route in safe_routes.values():
                for field in USAGE_COUNTER_FIELDS:
                    totals[field] += route[field]
            document["days"][day_text] = {"totals": totals, "routes": safe_routes}
        raw_recent = payload.get("recentRequests")
        if isinstance(raw_recent, list):
            recent = []
            for raw_request in raw_recent[-USAGE_STATS_MAX_RECENT_REQUESTS:]:
                if not isinstance(raw_request, dict):
                    continue
                request = UsageStatsStore._safe_route(raw_request)
                timestamp = _bounded_text(raw_request.get("timestamp"), 80)
                if len(timestamp) < 20:
                    continue
                day = _beijing_usage_day(timestamp)
                if not day:
                    continue
                request["timestamp"] = timestamp
                request["date"] = day
                recent.append(request)
            document["recentRequests"] = sorted(
                recent,
                key=lambda item: item.get("timestamp", ""),
                reverse=True,
            )[:USAGE_STATS_MAX_RECENT_REQUESTS]
        UsageStatsStore._prune_document(document)
        UsageStatsStore._recalculate_totals(document)
        document["updatedAt"] = _bounded_text(payload.get("updatedAt"), 80) or None
        return document

    @staticmethod
    def _prune_document(document: dict) -> None:
        days = document.get("days", {})
        keep = sorted(days, reverse=True)[:USAGE_STATS_RETENTION_DAYS]
        if set(days) - set(keep):
            document["counterGeneration"] = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        document["days"] = {day: days[day] for day in sorted(keep)}

    @staticmethod
    def _recalculate_totals(document: dict) -> None:
        totals = _empty_usage_counters()
        for day in document.get("days", {}).values():
            for field in USAGE_COUNTER_FIELDS:
                totals[field] += int(day.get("totals", {}).get(field, 0) or 0)
        document["totals"] = totals

    @staticmethod
    def _merge_counters(target: dict, delta: dict) -> None:
        for field in USAGE_COUNTER_FIELDS:
            target[field] = int(target.get(field, 0) or 0) + int(delta.get(field, 0) or 0)

    @staticmethod
    def _merge_route(target: dict, delta: dict) -> None:
        UsageStatsStore._merge_counters(target, delta)
        first_seen = [
            str(value)
            for value in (target.get("firstSeenAt"), delta.get("firstSeenAt"))
            if value
        ]
        last_seen = [
            str(value)
            for value in (target.get("lastSeenAt"), delta.get("lastSeenAt"))
            if value
        ]
        target["firstSeenAt"] = min(first_seen) if first_seen else ""
        target["lastSeenAt"] = max(last_seen) if last_seen else ""

    def _load_file(self) -> dict:
        self.counter_generation_needs_persist = True
        try:
            if not self.path.is_file() or self.path.stat().st_size > USAGE_STATS_MAX_FILE_BYTES:
                return _empty_usage_document()
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            document = self._safe_document(raw)
            self.counter_generation_needs_persist = not (
                isinstance(raw, dict) and raw.get("counterGeneration") == document["counterGeneration"]
            )
            return document
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return _empty_usage_document()

    def _schedule_flush_locked(self) -> None:
        if self.flush_timer and self.flush_timer.is_alive():
            return
        timer = threading.Timer(USAGE_STATS_FLUSH_SECONDS, self.flush)
        timer.daemon = True
        self.flush_timer = timer
        timer.start()

    def record(self, route: dict, usage: dict[str, int] | None, failed: bool = False) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        day = _beijing_usage_day(timestamp)
        safe_route = self._safe_route(route)
        usage = _normalized_usage(usage)
        safe_route["firstSeenAt"] = timestamp
        safe_route["lastSeenAt"] = timestamp
        key = self._route_key(safe_route)
        with self.lock:
            pending = self.pending.setdefault((day, key), {**safe_route, **_empty_usage_counters()})
            pending["firstSeenAt"] = min(
                value for value in (pending.get("firstSeenAt"), timestamp) if value
            )
            pending["lastSeenAt"] = timestamp
            pending["requestCount"] += 1
            pending["failureCount"] += int(bool(failed))
            if usage:
                pending["usageReportedCount"] += 1
                for field in (
                    "inputTokens",
                    "cachedInputTokens",
                    "cacheWriteTokens",
                    "outputTokens",
                    "reasoningOutputTokens",
                    "totalTokens",
                ):
                    pending[field] += _nonnegative_token_count(usage.get(field)) or 0
            else:
                pending["usageMissingCount"] += 1
            recent = {
                **safe_route,
                **_empty_usage_counters(),
                "timestamp": timestamp,
                "date": day,
                "requestCount": 1,
                "failureCount": int(bool(failed)),
                "usageReportedCount": int(bool(usage)),
                "usageMissingCount": int(not usage),
            }
            if usage:
                for field in (
                    "inputTokens",
                    "cachedInputTokens",
                    "cacheWriteTokens",
                    "outputTokens",
                    "reasoningOutputTokens",
                    "totalTokens",
                ):
                    recent[field] = _nonnegative_token_count(usage.get(field)) or 0
            self.pending_recent.append(recent)
            if len(self.pending_recent) > USAGE_STATS_MAX_RECENT_REQUESTS:
                self.pending_recent = self.pending_recent[-USAGE_STATS_MAX_RECENT_REQUESTS:]
            self.dirty_events += 1
            should_flush = (
                self.dirty_events >= USAGE_STATS_FLUSH_EVENTS
                or time.monotonic() - self.last_flush >= USAGE_STATS_FLUSH_SECONDS
            )
            if not should_flush:
                self._schedule_flush_locked()
        if should_flush:
            self.flush()

    def flush(self, force: bool = False) -> bool:
        # Keep detached deltas inside the I/O gate so a simultaneous snapshot
        # or safety backup cannot miss records waiting to be committed.
        with self.io_lock:
            return self._flush_locked(force)

    def _flush_locked(self, force: bool = False) -> bool:
        with self.lock:
            if not self.pending and not self.pending_recent:
                return False
            pending = self.pending
            self.pending = {}
            pending_recent = self.pending_recent
            self.pending_recent = []
            generation = self.generation
            self.dirty_events = 0
            timer = self.flush_timer
            self.flush_timer = None
            if timer and timer is not threading.current_thread():
                timer.cancel()
        try:
            timeout = 2.0 if force else USAGE_STATS_LOCK_TIMEOUT_SECONDS
            with self.io_lock:
                with self.lock:
                    if generation != self.generation:
                        return False
                with _exclusive_usage_file_lock(self.lock_path, timeout):
                    document = self._load_file()
                    for (day, key), delta in pending.items():
                        day_bucket = document["days"].setdefault(
                            day, {"totals": _empty_usage_counters(), "routes": {}}
                        )
                        routes = day_bucket["routes"]
                        if key not in routes and len(routes) >= USAGE_STATS_MAX_ROUTES_PER_DAY:
                            key = "overflow"
                            routes.setdefault(
                                key,
                                {
                                    "source": "overflow",
                                    "sourceKind": "overflow",
                                    "sourceRecordId": "",
                                    "accountId": "",
                                    "providerId": "",
                                    "requestedModel": "other",
                                    "routedModel": "other",
                                    "routeKey": "overflow",
                                    "requestClassification": "unclassified",
                                    "configuredForSubagents": False,
                                    **_empty_usage_counters(),
                                },
                            )
                        route_bucket = routes.setdefault(key, {**delta, **_empty_usage_counters()})
                        self._merge_route(route_bucket, delta)
                        self._merge_counters(day_bucket["totals"], delta)
                    document["recentRequests"] = sorted(
                        [*pending_recent, *document.get("recentRequests", [])],
                        key=lambda item: item.get("timestamp", ""),
                        reverse=True,
                    )[:USAGE_STATS_MAX_RECENT_REQUESTS]
                    self._prune_document(document)
                    self._recalculate_totals(document)
                    document["updatedAt"] = datetime.now(timezone.utc).isoformat()
                    encoded = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
                    if len(encoded) > USAGE_STATS_MAX_FILE_BYTES:
                        raise OSError("usage stats exceed bounded file size")
                    core.atomic_write_bytes(self.path, encoded)
            with self.lock:
                self.last_flush = time.monotonic()
            return True
        except (OSError, TimeoutError):
            with self.lock:
                for pending_key, delta in pending.items():
                    target = self.pending.setdefault(pending_key, {**delta, **_empty_usage_counters()})
                    self._merge_route(target, delta)
                self.pending_recent = [
                    *pending_recent,
                    *self.pending_recent,
                ][-USAGE_STATS_MAX_RECENT_REQUESTS:]
                self.dirty_events += sum(item["requestCount"] for item in pending.values())
                self._schedule_flush_locked()
            return False

    @staticmethod
    def _aggregate(document: dict, dimension: str) -> list[dict]:
        buckets: dict[str, dict] = {}
        for day in document.get("days", {}).values():
            for route in day.get("routes", {}).values():
                key = _bounded_text(route.get(dimension), 200, "unknown")
                bucket = buckets.setdefault(key, {dimension: key, **_empty_usage_counters()})
                for field in USAGE_COUNTER_FIELDS:
                    bucket[field] += int(route.get(field, 0) or 0)
        return sorted(buckets.values(), key=lambda item: (-item["totalTokens"], item[dimension]))

    @staticmethod
    def _daily_totals(document: dict) -> list[dict]:
        result = []
        for day, day_bucket in sorted(document.get("days", {}).items()):
            roles = {
                role: _empty_usage_counters()
                for role in ("mainAgent", "subagent", "unclassified")
            }
            for route in day_bucket.get("routes", {}).values():
                role = route.get("agentRole")
                role = role if role in roles else "unclassified"
                for field in USAGE_COUNTER_FIELDS:
                    roles[role][field] += int(route.get(field, 0) or 0)
            result.append(
                {
                    "date": day,
                    **{
                        field: int(day_bucket.get("totals", {}).get(field, 0) or 0)
                        for field in USAGE_COUNTER_FIELDS
                    },
                    "byAgentRole": roles,
                }
            )
        return result

    def snapshot(self) -> dict:
        with self.io_lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict:
        try:
            with self.io_lock:
                with _exclusive_usage_file_lock(self.lock_path):
                    document = self._load_file()
                    if self.counter_generation_needs_persist:
                        core.atomic_write_bytes(self.path, json.dumps(
                            document, ensure_ascii=False, separators=(",", ":")
                        ).encode("utf-8"))
                        self.counter_generation_needs_persist = False
        except (OSError, TimeoutError):
            document = self._load_file()
        with self.lock:
            pending = {
                key: json.loads(json.dumps(value, ensure_ascii=False))
                for key, value in self.pending.items()
            }
            pending_recent = json.loads(json.dumps(self.pending_recent, ensure_ascii=False))
        for (day, key), delta in pending.items():
            day_bucket = document["days"].setdefault(day, {"totals": _empty_usage_counters(), "routes": {}})
            route_bucket = day_bucket["routes"].setdefault(key, {**delta, **_empty_usage_counters()})
            self._merge_route(route_bucket, delta)
            self._merge_counters(day_bucket["totals"], delta)
        document["recentRequests"] = sorted(
            [*pending_recent, *document.get("recentRequests", [])],
            key=lambda item: item.get("timestamp", ""),
            reverse=True,
        )[:USAGE_STATS_MAX_RECENT_REQUESTS]
        self._prune_document(document)
        self._recalculate_totals(document)
        total_requests = int(document.get("totals", {}).get("requestCount", 0) or 0)
        reported_requests = int(document.get("totals", {}).get("usageReportedCount", 0) or 0)
        explicitly_classified = sum(
            int(item.get("requestCount", 0) or 0)
            for item in self._aggregate(document, "agentRole")
            if item.get("agentRole") in {"mainAgent", "subagent"}
        )
        return {
            **document,
            "byAccount": self._aggregate(document, "accountId"),
            "byProvider": self._aggregate(document, "providerId"),
            "byModel": self._aggregate(document, "routedModel"),
            "bySource": self._aggregate(document, "requestClassification"),
            "byAgentRole": self._aggregate(document, "agentRole"),
            "dailyTotals": self._daily_totals(document),
            "classificationEvidence": {
                "mainAgent": "explicit gateway metadata only",
                "subagent": "explicit gateway metadata only",
                "unclassified": "no observable request-role evidence",
            },
            "coverage": {
                "counterGenerationValid": not self.counter_generation_needs_persist,
                "usageMissingRequests": int(document.get("totals", {}).get("usageMissingCount", 0) or 0),
                "usageReportedRequests": reported_requests,
                "totalRequests": total_requests,
                "usageReportedRatio": (reported_requests / total_requests) if total_requests else None,
                "roleClassifiedRequests": explicitly_classified,
                "roleClassifiedRatio": (explicitly_classified / total_requests) if total_requests else None,
                "tokenSemantics": "total includes input and output; cached/reasoning are subsets",
            },
            "pendingFlush": bool(pending or pending_recent),
            "records": document.get("recentRequests", []),
        }

    def reset(self) -> dict:
        document = _empty_usage_document()
        document["updatedAt"] = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        with self.io_lock, self.lock:
            # Persist first. A full disk or denied write must not discard the
            # pending in-memory counters or invalidate an in-flight flush.
            with _exclusive_usage_file_lock(self.lock_path, 2.0):
                core.atomic_write_bytes(self.path, encoded)
            self.generation += 1
            self.pending = {}
            self.pending_recent = []
            self.dirty_events = 0
            timer = self.flush_timer
            self.flush_timer = None
            if timer:
                timer.cancel()
            self.last_flush = time.monotonic()
        return self.snapshot()


CODEX_SESSION_USAGE_CACHE_FILE = "codex-session-usage-index.json"
CODEX_SESSION_USAGE_CACHE_SCHEMA = 7
CODEX_SESSION_MAX_RECENT_PER_FILE = 100
CODEX_SESSION_BOOTSTRAP_MAX_BYTES = 64 * 1024 * 1024
CODEX_SESSION_INCREMENT_MAX_BYTES = 64 * 1024 * 1024
CODEX_SESSION_STATE_PROBE_BYTES = 16 * 1024 * 1024
CODEX_SESSION_MAX_JSONL_LINE_BYTES = 8 * 1024 * 1024
_CODEX_SESSION_USAGE_LOCK = threading.RLock()
CODEX_SESSION_BACKFILL_CHUNK_BYTES = 16 * 1024 * 1024
CODEX_SESSION_BACKFILL_PAUSE_SECONDS = 0.2
_CODEX_SESSION_BACKFILL_LOCK = threading.Lock()
_CODEX_SESSION_BACKFILL_WORKERS: dict[str, threading.Thread] = {}
_CODEX_SESSION_BACKFILL_STOPS: dict[str, threading.Event] = {}
_CODEX_SESSION_BACKFILL_DISABLED: set[str] = set()


def _codex_session_role(payload: dict) -> str:
    thread_source = _bounded_text(payload.get("thread_source"), 80).casefold()
    source = payload.get("source")
    source_keys = {
        str(key).strip().casefold()
        for key in source.keys()
    } if isinstance(source, dict) else set()
    if thread_source in {"subagent", "child", "delegate", "agent"}:
        return "subagent"
    if thread_source in {"user", "main", "primary"}:
        return "mainAgent"
    if isinstance(source, str) and source.strip().casefold() in {"user", "main", "primary"}:
        return "mainAgent"
    if source_keys & {"subagent", "child", "delegate", "agent"}:
        return "subagent"
    # Older builds may omit thread_source/source but retain both the parent
    # relationship and a managed child-agent identity.  A fork id alone is
    # deliberately insufficient: user sessions can be forked too.
    parent_id = str(
        payload.get("parent_thread_id")
        or payload.get("parentThreadId")
        or payload.get("forked_from_id")
        or payload.get("forkedFromId")
        or ""
    ).strip()
    agent_identity = any(
        str(payload.get(key) or "").strip()
        for key in (
            "agent_path",
            "agentPath",
            "agent_nickname",
            "agentNickname",
            "agent_role",
            "agentRole",
        )
    )
    if parent_id and agent_identity:
        return "subagent"
    return "unclassified"


def _timestamp_epoch(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _direct_account_at(timestamp: str, activations: list[tuple[float, str]]) -> str:
    target = _timestamp_epoch(timestamp)
    if target is None:
        return ""
    account_id = ""
    for activated_at, candidate in activations:
        if activated_at > target:
            break
        account_id = candidate
    return account_id


def _default_codex_session_parser_state() -> dict:
    return {
        "role": "unclassified",
        "model": "unknown",
        "provider": "unknown",
        "originator": "Codex",
        "previousCumulative": None,
    }


def _codex_session_initial_parser_state(path: Path) -> dict:
    state = _default_codex_session_parser_state()
    try:
        with path.open("rb") as handle:
            first = handle.readline(min(CODEX_SESSION_MAX_JSONL_LINE_BYTES, 1_048_576) + 1)
        if len(first) > CODEX_SESSION_MAX_JSONL_LINE_BYTES or b'"session_meta"' not in first:
            return state
        event = json.loads(first.decode("utf-8"))
        payload = event.get("payload") if isinstance(event, dict) else None
        if isinstance(payload, dict) and _bounded_text(payload.get("type") or event.get("type"), 80) == "session_meta":
            state.update(
                {
                    "role": _codex_session_role(payload),
                    "provider": _bounded_text(payload.get("model_provider"), 120, "unknown"),
                    "originator": _bounded_text(payload.get("originator"), 120, "Codex"),
                }
            )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return state


def _parse_codex_session_usage_file(
    path: Path,
    activations: list[tuple[float, str]] | None = None,
    *,
    start_offset: int = 0,
    end_offset: int | None = None,
    parser_state: dict | None = None,
    align_start: bool = False,
    max_bytes: int | None = None,
    live_since: str | None = None,
) -> dict[str, Any]:
    """Incrementally read only structured usage events with bounded lines.

    Rollout JSONL can contain very large prompt/tool-output lines.  Parsing is
    binary and line-bounded so those unrelated records are skipped without
    allocating their full contents.  ``offset`` always identifies the next
    safe resume point; ``lineAligned`` tells the next pass whether it must first
    finish skipping a previously truncated oversized line.
    """
    state = {
        **_default_codex_session_parser_state(),
        **(parser_state if isinstance(parser_state, dict) else {}),
    }
    role = _bounded_text(state.get("role"), 40, "unclassified")
    model = _bounded_text(state.get("model"), 160, "unknown")
    provider = _bounded_text(state.get("provider"), 120, "unknown")
    originator = _bounded_text(state.get("originator"), 120, "Codex")
    raw_previous = state.get("previousCumulative")
    previous_cumulative: tuple[int, ...] | None = (
        tuple(int(value or 0) for value in raw_previous[:5])
        if isinstance(raw_previous, list) and len(raw_previous) >= 5
        else None
    )
    activations = activations or []
    buckets: dict[tuple[str, str, str, str, str, str], dict] = {}
    recent_requests: list[dict] = []
    live_since_epoch = _timestamp_epoch(live_since)
    live_usage: dict[str, int] = {}
    live_complete = True
    offset = 0
    line_aligned = True
    partial = False
    try:
        file_size = path.stat().st_size
        requested_start = max(0, min(int(start_offset), file_size))
        requested_end = file_size if end_offset is None else max(requested_start, min(int(end_offset), file_size))
        scan_end = requested_end
        if max_bytes is not None:
            scan_end = min(scan_end, requested_start + max(1, int(max_bytes)))
        partial = requested_start > 0 or scan_end < file_size
        with path.open("rb") as handle:
            handle.seek(requested_start)
            if align_start and requested_start > 0:
                chunk = b""
                while handle.tell() < scan_end:
                    remaining = scan_end - handle.tell()
                    chunk = handle.readline(min(1_048_576, remaining))
                    if chunk.endswith((b"\n", b"\r")):
                        break
                if handle.tell() >= scan_end and not chunk.endswith((b"\n", b"\r")):
                    return {
                        "records": [],
                        "recentRequests": [],
                        "parserState": state,
                        "offset": handle.tell(),
                        "lineAligned": False,
                        "partial": True,
                        "parseSuccess": True,
                        "liveComplete": True,
                        "liveUsage": {},
                    }
            offset = handle.tell()
            while handle.tell() < scan_end:
                line_start = handle.tell()
                remaining = scan_end - line_start
                line = handle.readline(min(CODEX_SESSION_MAX_JSONL_LINE_BYTES + 1, remaining))
                if not line:
                    break
                decoded_event = None
                complete_line = line.endswith((b"\n", b"\r"))
                complete_json_at_eof = False
                if not complete_line:
                    if handle.tell() >= file_size and scan_end >= file_size and len(line) <= CODEX_SESSION_MAX_JSONL_LINE_BYTES:
                        # A closed rollout is allowed to omit its final newline;
                        # an actively-written partial JSON record is not. Only
                        # advance when the complete JSON value already parses.
                        try:
                            decoded_event = json.loads(line.decode("utf-8"))
                        except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
                            handle.seek(line_start)
                            offset = line_start
                            break
                        complete_line = True
                        complete_json_at_eof = True
                    if not complete_line and scan_end < file_size and handle.tell() >= scan_end:
                        # A bounded page ended inside an otherwise valid line.
                        # Retry from the line start on the next page; advancing
                        # to scan_end would make align_start discard the event.
                        handle.seek(line_start)
                        offset = line_start
                        line_aligned = True
                        partial = True
                        break
                    while handle.tell() < scan_end and not complete_line:
                        remaining = scan_end - handle.tell()
                        chunk = handle.readline(min(1_048_576, remaining))
                        complete_line = chunk.endswith((b"\n", b"\r"))
                    offset = handle.tell()
                    if not complete_line:
                        line_aligned = False
                        partial = True
                        break
                    if not complete_json_at_eof:
                        # Oversized conversation/tool line: intentionally skip it.
                        continue
                offset = handle.tell()
                # Avoid decoding the large majority of conversation/tool events.
                if not any(marker in line for marker in (b'"session_meta"', b'"turn_context"', b'"token_count"')):
                    continue
                try:
                    event = decoded_event if decoded_event is not None else json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
                    live_complete = False
                    continue
                payload = event.get("payload") if isinstance(event, dict) else None
                if not isinstance(payload, dict):
                    continue
                event_type = _bounded_text(payload.get("type") or event.get("type"), 80)
                if event_type == "session_meta":
                    role = _codex_session_role(payload)
                    provider = _bounded_text(payload.get("model_provider"), 120, "unknown")
                    originator = _bounded_text(payload.get("originator"), 120, "Codex")
                    continue
                if event_type == "turn_context":
                    model = _bounded_text(payload.get("model"), 160, "unknown")
                    continue
                if event_type != "token_count":
                    continue
                info = payload.get("info")
                last_usage = info.get("last_token_usage") if isinstance(info, dict) else None
                cumulative_usage = _normalized_usage(
                    info.get("total_token_usage") if isinstance(info, dict) else None
                )
                usage = _normalized_usage(last_usage)
                if not usage:
                    if last_usage is not None:
                        live_complete = False
                    continue
                cumulative_key = tuple(
                    int((cumulative_usage or {}).get(field, 0) or 0)
                    for field in (
                        "inputTokens",
                        "cachedInputTokens",
                        "outputTokens",
                        "reasoningOutputTokens",
                        "totalTokens",
                    )
                )
                # Codex may emit token_count repeatedly while only rate-limit
                # metadata changes. The cumulative usage tuple is the stable
                # evidence that a new model call actually completed.
                if cumulative_key == previous_cumulative:
                    continue
                previous_cumulative = cumulative_key
                timestamp = _bounded_text(event.get("timestamp"), 80)
                event_epoch = _timestamp_epoch(timestamp)
                if event_epoch is None:
                    live_complete = False
                day = _beijing_usage_day(timestamp)
                if len(day) != 10:
                    continue
                account_id = (
                    _direct_account_at(timestamp, activations)
                    if role == "mainAgent" and provider.casefold() in {"openai", "chatgpt"}
                    else ""
                )
                key = (day, model, role, provider, originator, account_id)
                if live_since_epoch is not None and event_epoch is not None and event_epoch >= live_since_epoch:
                    if role == "mainAgent" and provider.casefold() in {"openai", "chatgpt"}:
                        if account_id:
                            # Uncapped raw tokens; cached/reasoning are subsets.
                            live_usage[account_id] = live_usage.get(account_id, 0) + int(
                                usage.get("inputTokens", 0) or 0
                            ) + int(usage.get("outputTokens", 0) or 0)
                        else:
                            live_complete = False
                    elif role == "unclassified" and provider.casefold() in {"openai", "chatgpt", "unknown"}:
                        live_complete = False
                classification = (
                    "explicit_subagent"
                    if role == "subagent"
                    else "explicit_main_agent"
                    if role == "mainAgent"
                    else "unclassified"
                )
                request_record = {
                    "date": day,
                    "timestamp": timestamp,
                    "source": "codex_session",
                    "sourceKind": "codex_session",
                    "originator": originator,
                    "provider": provider,
                    "accountId": account_id,
                    "model": model,
                    "routedModel": model,
                    "agentRole": role,
                    "requestClassification": classification,
                    "requestCount": 1,
                    "failureCount": 0,
                    "usageReportedCount": 1,
                    "usageMissingCount": 0,
                    **{field: int(usage.get(field, 0) or 0) for field in (
                        "inputTokens",
                        "cachedInputTokens",
                        "cacheWriteTokens",
                        "outputTokens",
                        "reasoningOutputTokens",
                        "totalTokens",
                    )},
                }
                recent_requests.append(request_record)
                if len(recent_requests) > CODEX_SESSION_MAX_RECENT_PER_FILE:
                    del recent_requests[:-CODEX_SESSION_MAX_RECENT_PER_FILE]
                bucket = buckets.setdefault(
                    key,
                    {
                        "date": day,
                        "source": "codex_session",
                        "sourceKind": "codex_session",
                        "originator": originator,
                        "provider": provider,
                        "accountId": account_id,
                        "model": model,
                        "routedModel": model,
                        "agentRole": role,
                        "requestClassification": classification,
                        "firstSeenAt": timestamp,
                        "lastSeenAt": timestamp,
                        **_empty_usage_counters(),
                    },
                )
                if timestamp:
                    bucket["firstSeenAt"] = min(bucket.get("firstSeenAt") or timestamp, timestamp)
                    bucket["lastSeenAt"] = max(bucket.get("lastSeenAt") or timestamp, timestamp)
                bucket["requestCount"] += 1
                bucket["usageReportedCount"] += 1
                for field in (
                    "inputTokens",
                    "cachedInputTokens",
                    "cacheWriteTokens",
                    "outputTokens",
                    "reasoningOutputTokens",
                    "totalTokens",
                ):
                    bucket[field] += int(usage.get(field, 0) or 0)
    except (OSError, UnicodeDecodeError, TypeError, ValueError, OverflowError):
        return {
            "records": [],
            "recentRequests": [],
            "parserState": state,
            "offset": max(0, int(start_offset or 0)),
            "lineAligned": True,
            "partial": True,
            "parseSuccess": False,
            "liveComplete": False,
            "liveUsage": {},
        }
    return {
        "records": list(buckets.values()),
        "recentRequests": recent_requests,
        "parserState": {
            "role": role,
            "model": model,
            "provider": provider,
            "originator": originator,
            "previousCumulative": list(previous_cumulative) if previous_cumulative is not None else None,
        },
        "offset": offset,
        "lineAligned": line_aligned,
        "partial": partial,
        "parseSuccess": True,
        "liveComplete": live_complete,
        "liveUsage": live_usage,
    }


def _merge_codex_session_usage_records(existing: list[dict], additions: list[dict]) -> list[dict]:
    keys = ("date", "model", "agentRole", "provider", "originator", "accountId")
    merged: dict[tuple[str, ...], dict] = {}
    for record in [*existing, *additions]:
        if not isinstance(record, dict):
            continue
        key = tuple(_bounded_text(record.get(field), 160, "unknown") for field in keys)
        target = merged.get(key)
        if target is None:
            merged[key] = json.loads(json.dumps(record))
            continue
        _merge_usage_record(target, record)
        if record.get("firstSeenAt"):
            target["firstSeenAt"] = min(
                value for value in (target.get("firstSeenAt"), record.get("firstSeenAt")) if value
            )
        if record.get("lastSeenAt"):
            target["lastSeenAt"] = max(
                value for value in (target.get("lastSeenAt"), record.get("lastSeenAt")) if value
            )
    return list(merged.values())


def _merge_usage_record(target: dict, delta: dict) -> None:
    for field in USAGE_COUNTER_FIELDS:
        target[field] = int(target.get(field, 0) or 0) + int(delta.get(field, 0) or 0)


def _codex_session_resume_digest(path: Path, offset: int) -> str:
    offset = max(0, int(offset))
    start = max(0, offset - 65_536)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = offset - start
        while remaining > 0:
            chunk = handle.read(min(remaining, 65_536))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    digest.update(str(offset).encode("ascii"))
    return digest.hexdigest()[:32]


def _codex_backfill_progress(files: dict) -> dict:
    pending = [entry for entry in files.values()
               if isinstance(entry, dict) and entry.get("partialHistory")]
    targets = sum(max(0, int(entry.get("processedOffset") or 0)) for entry in pending)
    scanned = sum(min(max(0, int((entry.get("historyBackfill") or {}).get("offset") or 0)),
                      max(0, int(entry.get("processedOffset") or 0))) for entry in pending)
    return {"pendingFiles": len(pending), "scannedBytes": scanned,
            "targetBytes": targets, "remainingBytes": max(0, targets - scanned),
            "chunkBytes": CODEX_SESSION_BACKFILL_CHUNK_BYTES,
            "status": "pending" if pending else "complete"}


def _codex_backfill_entry_revision(entry: dict) -> tuple:
    backfill = entry.get("historyBackfill") or {}
    return (entry.get("size"), entry.get("mtimeNs"), entry.get("processedOffset"),
            entry.get("resumeDigest"), entry.get("partialHistory"),
            backfill.get("offset"), backfill.get("resumeDigest"), backfill.get("updatedAt"))


def codex_session_usage_backfill_step(*, max_bytes: int | None = None,
                                     sessions_root: Path | None = None,
                                     cache_path: Path | None = None,
                                     stop_event: threading.Event | None = None) -> dict:
    """Rebuild one bounded historical page, without adding to live counters.

    Parsing runs outside the snapshot lock. A compare-and-swap rejects the page
    if a concurrent scanner changed its source/cursor/attribution. The shadow
    aggregate replaces the old tail aggregate only once it reaches the current
    main cursor; adding prefix totals to tail totals would duplicate boundary
    token_count states. Source JSONL files are read-only throughout.
    """
    sessions_root = Path(sessions_root or (Path(core.CODEX_HOME) / "sessions"))
    cache_path = Path(cache_path or (Path(core.STATE_DIR) / CODEX_SESSION_USAGE_CACHE_FILE))
    if stop_event is not None and stop_event.is_set():
        return {"status": "cancelled"}
    # A chunk must be able to establish that a JSONL line is oversized before
    # skipping it. Smaller pages could otherwise retry one long line forever.
    budget = max(CODEX_SESSION_MAX_JSONL_LINE_BYTES + 1,
                 min(int(max_bytes or CODEX_SESSION_BACKFILL_CHUNK_BYTES),
                     CODEX_SESSION_BACKFILL_CHUNK_BYTES))
    def load():
        try:
            value = json.loads(cache_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) and isinstance(value.get("files"), dict) else {}
        except (OSError, UnicodeDecodeError, ValueError):
            return {}
    with _CODEX_SESSION_USAGE_LOCK:
        cached = load()
        files = cached.get("files", {})
        candidates = [(key, entry) for key, entry in files.items()
                      if isinstance(entry, dict) and entry.get("partialHistory")]
        if not candidates:
            return {**_codex_backfill_progress(files), "status": "idle"}
        key, entry = min(candidates, key=lambda item: (
            float((item[1].get("historyBackfill") or {}).get("updatedAt") or 0), item[0]))
        revision = _codex_backfill_entry_revision(entry)
        authority = cached.get("activationFingerprint")
        schema = cached.get("schemaVersion")
    try:
        path = next((candidate for candidate in sessions_root.rglob("*.jsonl")
                     if hashlib.sha256(candidate.relative_to(sessions_root).as_posix().encode("utf-8"))
                     .hexdigest()[:32] == key), None)
        if path is None:
            return {"status": "retry", "reason": "source_missing"}
        stat = path.stat()
        target = int(entry.get("processedOffset") or 0)
        if (stat.st_size != entry.get("size") or stat.st_mtime_ns != entry.get("mtimeNs")
                or target > stat.st_size or not entry.get("resumeDigest")
                or _codex_session_resume_digest(path, target) != entry["resumeDigest"]):
            return {"status": "retry", "reason": "source_changed"}
        activations = sorted(
            ((epoch, _bounded_text(item.get("accountId"), 200))
             for item in core.account_activation_timeline()
             if isinstance(item, dict)
             and (epoch := _timestamp_epoch(item.get("timestamp"))) is not None
             and _bounded_text(item.get("accountId"), 200)),
            key=lambda item: item[0],
        )
        fingerprint = hashlib.sha256(json.dumps(activations, separators=(",", ":")).encode()).hexdigest()[:24]
        if authority != fingerprint:
            return {"status": "retry", "reason": "attribution_changed"}
        shadow = entry.get("historyBackfill")
        shadow = shadow if isinstance(shadow, dict) else {}
        offset = int(shadow.get("offset") or 0)
        if offset > target or (offset and _codex_session_resume_digest(path, offset) != shadow.get("resumeDigest")):
            shadow, offset = {}, 0
        parsed = _parse_codex_session_usage_file(
            path, activations, start_offset=offset, end_offset=target,
            parser_state=shadow.get("parserState"),
            align_start=not shadow.get("lineAligned", True), max_bytes=budget,
        )
        parsed_offset = int(parsed.get("offset") or 0)
        if stop_event is not None and stop_event.is_set():
            return {"status": "cancelled"}
        if not parsed.get("parseSuccess") or parsed_offset <= offset:
            return {"status": "blocked", "reason": "incomplete_or_unreadable_jsonl"}
        shadow_records = _merge_codex_session_usage_records(shadow.get("records", []), parsed["records"])
        cutoff = datetime.now(USAGE_TIMEZONE).date().toordinal() - USAGE_STATS_RETENTION_DAYS + 1
        shadow_records = [record for record in shadow_records
                          if _safe_date_ordinal(str(record.get("date") or "")) >= cutoff]
        shadow_recent = sorted([*shadow.get("recentRequests", []), *parsed["recentRequests"]],
                               key=lambda record: str(record.get("timestamp") or ""))[-CODEX_SESSION_MAX_RECENT_PER_FILE:]
        next_shadow = {"schemaVersion": 1, "offset": parsed_offset,
                       "parserState": parsed["parserState"], "lineAligned": parsed["lineAligned"],
                       "records": shadow_records, "recentRequests": shadow_recent,
                       "resumeDigest": _codex_session_resume_digest(path, parsed_offset),
                       "updatedAt": time.time()}
        final_stat = path.stat()
        if final_stat.st_size != stat.st_size or final_stat.st_mtime_ns != stat.st_mtime_ns:
            return {"status": "retry", "reason": "source_changed_during_parse"}
        with _CODEX_SESSION_USAGE_LOCK, _exclusive_usage_file_lock(
                cache_path.with_suffix(cache_path.suffix + ".lock")):
            if stop_event is not None and stop_event.is_set():
                return {"status": "cancelled"}
            current = load()
            current_entry = current.get("files", {}).get(key)
            if (not isinstance(current_entry, dict)
                    or current.get("activationFingerprint") != authority
                    or current.get("schemaVersion") != schema
                    or _codex_backfill_entry_revision(current_entry) != revision):
                return {"status": "retry", "reason": "concurrent_index_change"}
            promoted = parsed_offset == target and parsed.get("lineAligned") is True
            if promoted:
                current_entry.update(records=shadow_records, recentRequests=shadow_recent,
                                     parserState=parsed["parserState"], partialHistory=False)
                current_entry.pop("historyBackfill", None)
            else:
                current_entry["historyBackfill"] = next_shadow
            current["updatedAt"] = datetime.now(timezone.utc).isoformat()
            core.atomic_write_bytes(cache_path, json.dumps(current, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8"))
            progress = _codex_backfill_progress(current["files"])
        return {**progress, "status": "promoted" if promoted else "progressed",
                "processedBytes": parsed_offset - offset}
    except (OSError, TypeError, ValueError, OverflowError):
        return {"status": "blocked", "reason": "backfill_unavailable"}


def request_codex_session_usage_backfill(*, restart: bool = False) -> bool:
    """Start at most one cooperative background worker for this cache path.

    Each page is bounded and yields between writes. A changed/unreadable source
    stops the worker; the next normal snapshot refresh can reschedule after its
    live cursor has caught up. No historical source file is modified.
    """
    sessions_root = Path(core.CODEX_HOME) / "sessions"
    cache_path = Path(core.STATE_DIR) / CODEX_SESSION_USAGE_CACHE_FILE
    worker_key = str(cache_path.resolve())
    with _CODEX_SESSION_BACKFILL_LOCK:
        if restart:
            _CODEX_SESSION_BACKFILL_DISABLED.discard(worker_key)
        if worker_key in _CODEX_SESSION_BACKFILL_DISABLED:
            return False
        existing = _CODEX_SESSION_BACKFILL_WORKERS.get(worker_key)
        if existing and existing.is_alive():
            return False
        stop_event = threading.Event()
        def run():
            try:
                while not stop_event.is_set():
                    result = codex_session_usage_backfill_step(
                        sessions_root=sessions_root, cache_path=cache_path, stop_event=stop_event)
                    if result.get("status") not in {"progressed", "promoted"} or not result.get("pendingFiles"):
                        break
                    stop_event.wait(CODEX_SESSION_BACKFILL_PAUSE_SECONDS)
            finally:
                with _CODEX_SESSION_BACKFILL_LOCK:
                    if _CODEX_SESSION_BACKFILL_WORKERS.get(worker_key) is threading.current_thread():
                        _CODEX_SESSION_BACKFILL_WORKERS.pop(worker_key, None)
                        _CODEX_SESSION_BACKFILL_STOPS.pop(worker_key, None)
        worker = threading.Thread(target=run, name="codex-usage-history-backfill", daemon=True)
        _CODEX_SESSION_BACKFILL_WORKERS[worker_key] = worker
        _CODEX_SESSION_BACKFILL_STOPS[worker_key] = stop_event
        worker.start()
    return True


def enable_codex_session_usage_backfill() -> None:
    """Allow the next normal snapshot to schedule work; never start I/O here."""
    cache_path = Path(core.STATE_DIR) / CODEX_SESSION_USAGE_CACHE_FILE
    with _CODEX_SESSION_BACKFILL_LOCK:
        _CODEX_SESSION_BACKFILL_DISABLED.discard(str(cache_path.resolve()))


def stop_codex_session_usage_backfill(timeout_seconds: float = 1.0) -> bool:
    """Cancel this cache's worker and prevent new scheduling during shutdown.

    Returns True only when it is quiescent. A False result means an already
    running read/write has not yet returned within the bounded wait; cancellation
    prevents subsequent page commits. A fresh process starts enabled; an
    in-process restart can explicitly request(..., restart=True).
    """
    cache_path = Path(core.STATE_DIR) / CODEX_SESSION_USAGE_CACHE_FILE
    worker_key = str(cache_path.resolve())
    with _CODEX_SESSION_BACKFILL_LOCK:
        _CODEX_SESSION_BACKFILL_DISABLED.add(worker_key)
        stop_event = _CODEX_SESSION_BACKFILL_STOPS.get(worker_key)
        worker = _CODEX_SESSION_BACKFILL_WORKERS.get(worker_key)
        if stop_event:
            stop_event.set()
    if worker and worker is not threading.current_thread():
        worker.join(max(0.0, min(float(timeout_seconds), 5.0)))
    return not worker or not worker.is_alive()


def codex_session_usage_snapshot() -> dict:
    """Incrementally aggregate official Codex rollout token_count events.

    The cache contains only day/model/role counters and file fingerprints. It
    never stores prompts, responses, cwd values, account tokens, or session IDs.
    """
    sessions_root = Path(core.CODEX_HOME) / "sessions"
    cache_path = Path(core.STATE_DIR) / CODEX_SESSION_USAGE_CACHE_FILE
    cutoff = datetime.now(USAGE_TIMEZONE).date().toordinal() - USAGE_STATS_RETENTION_DAYS + 1
    try:
        raw_activations = core.account_activation_timeline()
    except Exception:
        raw_activations = []
    activations = sorted(
        (
            (epoch, _bounded_text(item.get("accountId"), 200))
            for item in raw_activations
            if isinstance(item, dict)
            and (epoch := _timestamp_epoch(item.get("timestamp"))) is not None
            and _bounded_text(item.get("accountId"), 200)
        ),
        key=lambda item: item[0],
    )
    activation_fingerprint = hashlib.sha256(
        json.dumps(activations, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    with _CODEX_SESSION_USAGE_LOCK, _exclusive_usage_file_lock(
            cache_path.with_suffix(cache_path.suffix + ".lock")):
        cached: dict[str, Any] = {"schemaVersion": CODEX_SESSION_USAGE_CACHE_SCHEMA, "files": {}}
        cache_schema = 0
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
            cache_schema = int(raw.get("schemaVersion") or 0)
            if cache_schema in {6, CODEX_SESSION_USAGE_CACHE_SCHEMA} and isinstance(raw.get("files"), dict):
                cached = raw
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            pass
        old_files = cached.get("files", {})
        next_files: dict[str, dict] = {}
        activation_changed = cached.get("activationFingerprint") != activation_fingerprint
        changed = cache_schema != CODEX_SESSION_USAGE_CACHE_SCHEMA or activation_changed
        raw_live = cached.get("liveCoverage")
        live = raw_live if isinstance(raw_live, dict) else {}
        live_valid = bool(
            live.get("schemaVersion") == 1
            and isinstance(live.get("epoch"), str)
            and len(live["epoch"]) == 64
            and _timestamp_epoch(live.get("startedAt")) is not None
            and isinstance(live.get("accounts"), dict)
        )
        live_reset = not live_valid or activation_changed or cache_schema != CODEX_SESSION_USAGE_CACHE_SCHEMA
        live_reason = "baseline_started" if not live_valid else "source_authority_changed" if live_reset else None
        live_start = live.get("startedAt") if live_valid else None
        live_accounts = {
            str(key): max(0, int(value))
            for key, value in (live.get("accounts", {}) if live_valid else {}).items()
            if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)
        }
        live_complete = True
        scanned_paths: list[tuple[Path, int, int]] = []
        try:
            paths = list(sessions_root.rglob("*.jsonl")) if sessions_root.is_dir() else []
        except OSError:
            paths = []
            live_complete = False
        for path in paths:
            try:
                stat = path.stat()
                relative = path.relative_to(sessions_root).as_posix()
            except (OSError, ValueError):
                live_complete = False
                continue
            cache_key = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:32]
            fingerprint = {"size": stat.st_size, "mtimeNs": stat.st_mtime_ns}
            previous = old_files.get(cache_key) if isinstance(old_files.get(cache_key), dict) else None
            records: list[dict]
            recent_requests: list[dict]
            parser_state: dict
            processed_offset: int
            line_aligned: bool
            partial_history: bool
            history_backfill = None
            live_file_complete = True
            unchanged_digest_matches = False
            if (
                previous
                and previous.get("size") == fingerprint["size"]
                and previous.get("mtimeNs") == fingerprint["mtimeNs"]
                and int(previous.get("processedOffset") or 0) >= stat.st_size
            ):
                try:
                    unchanged_digest_matches = bool(previous.get("resumeDigest")) and secrets.compare_digest(
                        str(previous.get("resumeDigest")),
                        _codex_session_resume_digest(path, stat.st_size),
                    )
                except OSError:
                    unchanged_digest_matches = False
            if (
                previous
                and cache_schema == CODEX_SESSION_USAGE_CACHE_SCHEMA
                and not activation_changed
                and previous.get("size") == fingerprint["size"]
                and previous.get("mtimeNs") == fingerprint["mtimeNs"]
                and int(previous.get("processedOffset") or 0) >= stat.st_size
                and unchanged_digest_matches
            ):
                records = previous.get("records") if isinstance(previous.get("records"), list) else []
                recent_requests = (
                    previous.get("recentRequests")
                    if isinstance(previous.get("recentRequests"), list)
                    else []
                )
                parser_state = (
                    previous.get("parserState")
                    if isinstance(previous.get("parserState"), dict)
                    else _codex_session_initial_parser_state(path)
                )
                processed_offset = min(stat.st_size, int(previous.get("processedOffset") or stat.st_size))
                line_aligned = bool(previous.get("lineAligned", True))
                partial_history = bool(previous.get("partialHistory", False))
                history_backfill = previous.get("historyBackfill")
                live_file_complete = previous.get("liveParseComplete", True) is True
            else:
                can_resume = False
                base_records: list[dict] = []
                base_recent: list[dict] = []
                resume_offset = 0
                resume_state = _codex_session_initial_parser_state(path)
                resume_aligned = True
                partial_history = False
                if previous and not activation_changed and int(previous.get("size") or 0) <= stat.st_size:
                    previous_size = max(0, int(previous.get("size") or 0))
                    previous_offset = int(previous.get("processedOffset") or 0)
                    if previous_offset > 0 and isinstance(previous.get("parserState"), dict):
                        try:
                            digest_matches = (
                                previous_offset <= stat.st_size
                                and str(previous.get("resumeDigest") or "")
                                == _codex_session_resume_digest(path, previous_offset)
                            )
                        except OSError:
                            digest_matches = False
                        if digest_matches:
                            can_resume = True
                            resume_offset = previous_offset
                            resume_state = previous["parserState"]
                            resume_aligned = bool(previous.get("lineAligned", True))
                    elif cache_schema == 6 and previous_size > 0:
                        # Schema 6 already contains complete aggregates but no
                        # parser cursor. Recover only the small tail state and
                        # continue from the old size instead of rescanning a
                        # multi-gigabyte rollout.
                        probe_start = max(0, previous_size - CODEX_SESSION_STATE_PROBE_BYTES)
                        recovered = _parse_codex_session_usage_file(
                            path,
                            activations,
                            start_offset=probe_start,
                            end_offset=previous_size,
                            parser_state=resume_state,
                            align_start=probe_start > 0,
                            max_bytes=CODEX_SESSION_STATE_PROBE_BYTES,
                        )
                        can_resume = True
                        resume_offset = int(recovered.get("offset") or previous_size)
                        resume_state = recovered.get("parserState") or resume_state
                        resume_aligned = bool(recovered.get("lineAligned", True))
                    if can_resume:
                        history_backfill = previous.get("historyBackfill")
                        base_records = previous.get("records") if isinstance(previous.get("records"), list) else []
                        base_recent = (
                            previous.get("recentRequests")
                            if isinstance(previous.get("recentRequests"), list)
                            else []
                        )
                        partial_history = bool(previous.get("partialHistory", False))

                if can_resume:
                    parsed = _parse_codex_session_usage_file(
                        path,
                        activations,
                        start_offset=resume_offset,
                        end_offset=stat.st_size,
                        parser_state=resume_state,
                        align_start=not resume_aligned,
                        max_bytes=CODEX_SESSION_INCREMENT_MAX_BYTES,
                        live_since=live_start,
                    )
                    records = _merge_codex_session_usage_records(base_records, parsed["records"])
                    recent_requests = sorted(
                        [*base_recent, *parsed["recentRequests"]],
                        key=lambda item: str(item.get("timestamp") or ""),
                    )[-CODEX_SESSION_MAX_RECENT_PER_FILE:]
                else:
                    bootstrap_start = max(0, stat.st_size - CODEX_SESSION_BOOTSTRAP_MAX_BYTES)
                    if previous or bootstrap_start > 0:
                        live_reset = True
                        live_reason = "source_reparsed_or_partial_bootstrap"
                    parsed = _parse_codex_session_usage_file(
                        path,
                        activations,
                        start_offset=bootstrap_start,
                        end_offset=stat.st_size,
                        parser_state=resume_state,
                        align_start=bootstrap_start > 0,
                        max_bytes=CODEX_SESSION_BOOTSTRAP_MAX_BYTES,
                        live_since=live_start,
                    )
                    records = parsed["records"]
                    recent_requests = parsed["recentRequests"]
                    partial_history = bootstrap_start > 0
                parser_state = parsed.get("parserState") or resume_state
                processed_offset = max(0, min(stat.st_size, int(parsed.get("offset") or 0)))
                line_aligned = bool(parsed.get("lineAligned", True))
                live_file_complete = (
                    parsed.get("liveComplete") is True if parsed.get("parseSuccess")
                    else (previous or {}).get("liveParseComplete", True) is True
                )
                live_complete = live_complete and parsed.get("parseSuccess") is True
                if can_resume and previous.get("liveParseComplete") is False:
                    # An earlier malformed structured event may have changed
                    # parser/account state. Do not quietly certify the gap.
                    live_file_complete = False
                for account_id, token_count in parsed.get("liveUsage", {}).items():
                    live_accounts[account_id] = live_accounts.get(account_id, 0) + int(token_count)
                changed = True
            records = [
                item
                for item in records
                if isinstance(item, dict)
                and isinstance(item.get("date"), str)
                and len(item["date"]) == 10
                and _safe_date_ordinal(item["date"]) >= cutoff
            ]
            try:
                resume_digest = _codex_session_resume_digest(path, processed_offset)
            except OSError:
                resume_digest = ""
                live_file_complete = False
            live_complete = bool(live_complete and live_file_complete and line_aligned
                                 and processed_offset == stat.st_size and resume_digest)
            scanned_paths.append((path, stat.st_size, stat.st_mtime_ns))
            next_files[cache_key] = {
                **fingerprint,
                "records": records,
                "recentRequests": recent_requests[-CODEX_SESSION_MAX_RECENT_PER_FILE:],
                "parserState": parser_state,
                "processedOffset": processed_offset,
                "lineAligned": line_aligned,
                "partialHistory": partial_history,
                "pendingBytes": max(0, stat.st_size - processed_offset),
                "resumeDigest": resume_digest,
                "liveParseComplete": live_file_complete,
                **({"historyBackfill": history_backfill} if isinstance(history_backfill, dict) else {}),
            }
        if set(next_files) != set(old_files):
            changed = True
        if set(old_files) - set(next_files):
            live_reset = True
            live_reason = "source_removed"
        # Detect appends/replacements after the stat boundary used for parsing.
        # Parser state never advances beyond that stored boundary, even if a
        # writer appended while the parser was opening the file.
        for path, size, mtime_ns in scanned_paths:
            try:
                final_stat = path.stat()
                if final_stat.st_size != size or final_stat.st_mtime_ns != mtime_ns:
                    live_complete = False
            except OSError:
                live_complete = False
        live_observed_at = datetime.now(timezone.utc).isoformat()
        if live_reset:
            live_accounts = {}
            live_start = live_observed_at
        for _activated_at, account_id in activations:
            live_accounts.setdefault(account_id, 0)
        live = {
            "schemaVersion": 1,
            "scope": "native_direct_main",
            "epoch": hashlib.sha256(uuid.uuid4().bytes).hexdigest() if live_reset else live["epoch"],
            "startedAt": live_start,
            "observedAt": live_observed_at,
            "complete": live_complete,
            "accounts": live_accounts,
            "reason": live_reason or ("caught_up" if live_complete else "scan_incomplete"),
            "tokenSemantics": "post-baseline native direct main input+output; no gateway or historical bootstrap",
        }
        # Return a current watermark without rewriting the entire historical
        # index on every unchanged UI read. Counter changes are persisted.
        if changed or live_reset or live_complete != (raw_live or {}).get("complete"):
            encoded = json.dumps(
                {
                    "schemaVersion": CODEX_SESSION_USAGE_CACHE_SCHEMA,
                    "updatedAt": datetime.now(timezone.utc).isoformat(),
                    "activationFingerprint": activation_fingerprint,
                    "files": next_files,
                    "liveCoverage": live,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            try:
                core.atomic_write_bytes(cache_path, encoded)
            except OSError:
                live["complete"] = False
                live["reason"] = "cache_persistence_failed"

        records_by_key: dict[tuple[str, str, str, str, str, str], dict] = {}
        for entry in next_files.values():
            for record in entry.get("records", []):
                key = tuple(
                    _bounded_text(record.get(field), 160, "unknown")
                    for field in ("date", "model", "agentRole", "provider", "originator", "accountId")
                )
                target = records_by_key.setdefault(key, {**record, **_empty_usage_counters()})
                _merge_usage_record(target, record)
                if record.get("firstSeenAt"):
                    target["firstSeenAt"] = min(
                        value
                        for value in (target.get("firstSeenAt"), record.get("firstSeenAt"))
                        if value
                    )
                if record.get("lastSeenAt"):
                    target["lastSeenAt"] = max(
                        value
                        for value in (target.get("lastSeenAt"), record.get("lastSeenAt"))
                        if value
                    )
        records = sorted(
            records_by_key.values(),
            key=lambda item: (item.get("lastSeenAt") or item["date"], item["model"], item["agentRole"]),
            reverse=True,
        )
        recent_requests = sorted(
            (
                request
                for entry in next_files.values()
                for request in entry.get("recentRequests", [])
                if isinstance(request, dict)
            ),
            key=lambda item: str(item.get("timestamp") or ""),
            reverse=True,
        )[:USAGE_STATS_MAX_RECENT_REQUESTS]
        totals = _empty_usage_counters()
        for record in records:
            _merge_usage_record(totals, record)
        by_model: dict[str, dict] = {}
        by_role: dict[str, dict] = {}
        daily: dict[str, dict] = {}
        for record in records:
            model_bucket = by_model.setdefault(record["model"], {"model": record["model"], **_empty_usage_counters()})
            role_bucket = by_role.setdefault(record["agentRole"], {"agentRole": record["agentRole"], **_empty_usage_counters()})
            day_bucket = daily.setdefault(record["date"], {"date": record["date"], **_empty_usage_counters()})
            _merge_usage_record(model_bucket, record)
            _merge_usage_record(role_bucket, record)
            _merge_usage_record(day_bucket, record)
        classified = sum(
            item["requestCount"] for role, item in by_role.items() if role in {"mainAgent", "subagent"}
        )
        partial_files = sum(
            1
            for entry in next_files.values()
            if entry.get("partialHistory") or int(entry.get("pendingBytes") or 0) > 0
        )
        pending_bytes = sum(int(entry.get("pendingBytes") or 0) for entry in next_files.values())
        return {
            "schemaVersion": 1,
            "source": "codex_session_logs",
            "timeZone": USAGE_TIMEZONE_NAME,
            "dayBoundary": "00:00",
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "totals": totals,
            "records": records,
            "recentRequests": recent_requests,
            "liveCoverage": live,
            "byModel": sorted(by_model.values(), key=lambda item: (-item["totalTokens"], item["model"])),
            "byAgentRole": sorted(by_role.values(), key=lambda item: (-item["totalTokens"], item["agentRole"])),
            "dailyTotals": [daily[key] for key in sorted(daily)],
            "coverage": {
                "usageReportedRequests": totals["usageReportedCount"],
                "totalRequests": totals["requestCount"],
                "usageReportedRatio": 1 if totals["requestCount"] else None,
                "roleClassifiedRequests": classified,
                "roleClassifiedRatio": (classified / totals["requestCount"]) if totals["requestCount"] else None,
                "cacheWriteAvailable": False,
                "indexedFiles": len(next_files),
                "partialFiles": partial_files,
                "pendingBytes": pending_bytes,
                "backfill": _codex_backfill_progress(next_files),
                "boundedScanBytesPerFile": CODEX_SESSION_INCREMENT_MAX_BYTES,
                "tokenSemantics": "official Codex token_count events; cached/reasoning are subsets",
            },
        }


def account_attribution_snapshot(gateway: dict, sessions: dict | None) -> dict:
    """Combine disjoint, account-identifiable paths without double counting.

    Direct official main-agent calls are attributed from the account activation
    timeline embedded in the Codex-session snapshot. Requests that traversed
    the local gateway use the account/provider that actually handled them.
    Aggregate Codex session events are deliberately excluded because the same
    calls are already represented by the gateway counters.
    """
    records: list[dict] = []
    for record in (sessions or {}).get("records", []):
        if not isinstance(record, dict):
            continue
        if record.get("agentRole") != "mainAgent" or not record.get("accountId"):
            continue
        if str(record.get("provider") or "").casefold() not in {"openai", "chatgpt"}:
            continue
        records.append(
            {
                **record,
                "source": "codex_direct_account",
                "sourceKind": "account",
                "sourceRecordId": str(record.get("accountId") or ""),
            }
        )
    for day, day_bucket in gateway.get("days", {}).items():
        if not isinstance(day_bucket, dict):
            continue
        routes = day_bucket.get("routes") if isinstance(day_bucket.get("routes"), dict) else {}
        for route in routes.values():
            if not isinstance(route, dict) or not (route.get("accountId") or route.get("providerId")):
                continue
            records.append({**route, "date": day})
    records.sort(
        key=lambda item: (
            str(item.get("lastSeenAt") or item.get("timestamp") or item.get("date") or ""),
            str(item.get("accountId") or item.get("providerId") or ""),
        ),
        reverse=True,
    )
    totals = _empty_usage_counters()
    for record in records:
        _merge_usage_record(totals, record)
    recent = [
        json.loads(json.dumps(item))
        for item in gateway.get("recentRequests", [])
        if isinstance(item, dict) and (item.get("accountId") or item.get("providerId"))
    ]
    # Older Desktop builds do not forward a request-role header to an
    # OpenAI-compatible gateway.  Correlate only high-confidence, one-to-one
    # token events from the official rollout log: same model, same reported
    # usage and a nearby completion timestamp.  No prompt/session identifier is
    # read or persisted, and ambiguous matches remain unclassified.
    session_events = [
        item
        for item in (sessions or {}).get("recentRequests", [])
        if isinstance(item, dict) and item.get("agentRole") in {"mainAgent", "subagent"}
    ]
    used_session_events: set[int] = set()
    for request in recent:
        if request.get("agentRole") in {"mainAgent", "subagent"}:
            continue
        request_epoch = _timestamp_epoch(request.get("timestamp"))
        request_models = {
            _bounded_text(request.get(field), 200).casefold()
            for field in ("requestedModel", "routedModel", "model")
            if _bounded_text(request.get(field), 200)
        }
        request_total = int(request.get("totalTokens", 0) or 0)
        request_input = int(request.get("inputTokens", 0) or 0)
        request_output = int(request.get("outputTokens", 0) or 0)
        candidates: list[tuple[float, int, dict]] = []
        for index, event in enumerate(session_events):
            if index in used_session_events:
                continue
            event_epoch = _timestamp_epoch(event.get("timestamp"))
            if request_epoch is None or event_epoch is None:
                continue
            distance = abs(request_epoch - event_epoch)
            if distance > 120:
                continue
            event_model = _bounded_text(event.get("model"), 200).casefold()
            if request_models and event_model and event_model not in request_models:
                continue
            event_total = int(event.get("totalTokens", 0) or 0)
            event_input = int(event.get("inputTokens", 0) or 0)
            event_output = int(event.get("outputTokens", 0) or 0)
            totals_match = bool(request_total and event_total and request_total == event_total)
            parts_match = bool(
                (request_input or request_output)
                and request_input == event_input
                and request_output == event_output
            )
            if not (totals_match and parts_match):
                continue
            candidates.append((distance, index, event))
        candidates.sort(key=lambda item: item[0])
        if not candidates:
            continue
        # Equal-distance matches with contradictory roles are ambiguous.
        best_distance = candidates[0][0]
        best = [item for item in candidates if item[0] == best_distance]
        if len({item[2].get("agentRole") for item in best}) != 1:
            continue
        _distance, matched_index, matched = best[0]
        used_session_events.add(matched_index)
        request["agentRole"] = matched["agentRole"]
        request["requestClassification"] = (
            "correlated_subagent_session"
            if matched["agentRole"] == "subagent"
            else "correlated_main_session"
        )
        request["roleEvidence"] = "official_token_event_match"
        request["roleConfidence"] = "high"
    if not recent:
        recent.extend(
            record
            for record in records
            if record.get("source") != "codex_direct_account"
        )
    direct_session_recent = [
        {
            **event,
            "source": "codex_direct_account",
            "sourceKind": "account",
            "sourceRecordId": str(event.get("accountId") or ""),
        }
        for event in session_events
        if event.get("agentRole") == "mainAgent"
        and event.get("accountId")
        and str(event.get("provider") or "").casefold() in {"openai", "chatgpt"}
    ]
    if direct_session_recent:
        recent.extend(direct_session_recent)
    else:
        recent.extend(
            record
            for record in records
            if record.get("source") == "codex_direct_account"
        )
    recent.sort(
        key=lambda item: str(item.get("timestamp") or item.get("lastSeenAt") or item.get("date") or ""),
        reverse=True,
    )
    total_requests = int(totals.get("requestCount", 0) or 0)
    reported_requests = int(totals.get("usageReportedCount", 0) or 0)
    classified_requests = sum(
        int(item.get("requestCount", 0) or 0)
        for item in records
        if item.get("agentRole") in {"mainAgent", "subagent"}
    )
    updated_values = [
        str(value)
        for value in (
            gateway.get("updatedAt"),
            (sessions or {}).get("updatedAt"),
        )
        if value
    ]
    return {
        "schemaVersion": 1,
        "source": "account_attribution",
        "updatedAt": max(updated_values) if updated_values else None,
        "totals": totals,
        "records": records,
        "recentRequests": recent[:USAGE_STATS_MAX_RECENT_REQUESTS],
        "coverage": {
            "usageReportedRequests": reported_requests,
            "totalRequests": total_requests,
            "usageReportedRatio": (reported_requests / total_requests) if total_requests else None,
            "roleClassifiedRequests": classified_requests,
            "roleClassifiedRatio": (classified_requests / total_requests) if total_requests else None,
            "accountAttributedRequests": total_requests,
            "accountEvidence": "direct activation timeline or actual local gateway route",
            "cacheWriteAvailable": bool(gateway.get("days")),
            "tokenSemantics": "disjoint direct-main and gateway paths; cached/reasoning are subsets",
        },
    }


def _safe_date_ordinal(value: str) -> int:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().toordinal()
    except (TypeError, ValueError):
        return 0


class GatewayError(RuntimeError):
    def __init__(
        self,
        message: str,
        status: int = 400,
        body: bytes | None = None,
        content_type: str = "application/json",
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = headers or {}


def _parse_expiry(value: Any) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return None


def _quota_score(account: dict) -> float:
    usage = account.get("usage") if isinstance(account.get("usage"), dict) else {}
    window = usage.get("weekly") if isinstance(usage.get("weekly"), dict) else {}
    try:
        remaining = float(window.get("remainingPercent"))
        if remaining <= 0 and _credits_usable_from_account(account) is True:
            # Keep paid/unlimited Credits behind positive subscription quota,
            # while still ranking the account as usable instead of exhausted.
            return 1.0
        return remaining
    except (TypeError, ValueError):
        return 50.0


def _safe_response_headers(headers: Any) -> dict[str, str]:
    """Forward only Codex presentation metadata, never hop-by-hop headers."""
    forwarded: dict[str, str] = {}
    if headers is None or not hasattr(headers, "items"):
        return forwarded
    for raw_name, raw_value in headers.items():
        name = str(raw_name or "").strip().casefold()
        value = str(raw_value or "").strip()
        if not (name.startswith("x-codex-") or name in SAFE_FORWARD_HEADERS):
            continue
        if not name or len(name) > 128 or not value or len(value) > 2048:
            continue
        if "\r" in value or "\n" in value:
            continue
        forwarded[name] = value
    return forwarded


def _header_value(headers: Any, name: str) -> str:
    if headers is None or not hasattr(headers, "items"):
        return ""
    expected = name.casefold()
    for raw_name, raw_value in headers.items():
        if str(raw_name or "").strip().casefold() == expected:
            return str(raw_value or "").strip()
    return ""


def _safe_header_text(value: Any, limit: int = 512) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit or "\r" in text or "\n" in text:
        return ""
    return text


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    text = str(value or "").strip().casefold()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _positive_number(value: Any) -> bool | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed > 0


def _credits_mapping_usable(credits: Any) -> bool | None:
    if not isinstance(credits, dict):
        return None
    observed = False
    for key in ("unlimited", "has_credits", "hasCredits"):
        if key not in credits:
            continue
        flag = _optional_bool(credits.get(key))
        if flag is not None:
            observed = True
            if flag:
                return True
    for key in ("balance", "remaining", "available_credits", "availableCredits", "remaining_percent", "remainingPercent"):
        if key not in credits:
            continue
        positive = _positive_number(credits.get(key))
        if positive is not None:
            observed = True
            if positive:
                return True
    total = next((credits.get(key) for key in ("limit", "total") if key in credits), None)
    used = credits.get("used") if "used" in credits else None
    if total is not None and used is not None:
        try:
            remaining = float(total) - float(used)
        except (TypeError, ValueError, OverflowError):
            remaining = math.nan
        if math.isfinite(remaining):
            observed = True
            if remaining > 0:
                return True
    return False if observed else None


def _credits_usable_from_event(event: Any) -> bool | None:
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("type") or "").strip().casefold()
    if event_type == "codex.rate_limits" or "rate_limits" in event or "rateLimits" in event:
        rate_limits = event.get("rate_limits")
        if not isinstance(rate_limits, dict):
            rate_limits = event.get("rateLimits")
        for candidate in (
            event.get("credits"),
            rate_limits.get("credits") if isinstance(rate_limits, dict) else None,
        ):
            usable = _credits_mapping_usable(candidate)
            if usable is not None:
                return usable
    embedded_headers = event.get("headers")
    if isinstance(embedded_headers, dict):
        return _credits_usable_from_headers(embedded_headers)
    return None


def _credits_usable_from_headers(headers: Any) -> bool | None:
    observed = False
    for name in ("x-codex-credits-unlimited", "x-codex-credits-has-credits"):
        raw = _header_value(headers, name)
        if not raw:
            continue
        flag = _optional_bool(raw)
        if flag is not None:
            observed = True
            if flag:
                return True
    raw_balance = _header_value(headers, "x-codex-credits-balance")
    if raw_balance:
        positive = _positive_number(raw_balance)
        if positive is not None:
            observed = True
            if positive:
                return True
    return False if observed else None


def _credits_usable_from_account(account: dict | None) -> bool | None:
    if not isinstance(account, dict):
        return None
    usage = account.get("usage") if isinstance(account.get("usage"), dict) else {}
    quota = account.get("quota") if isinstance(account.get("quota"), dict) else {}
    raw_quota = quota.get("rawData") if isinstance(quota.get("rawData"), dict) else quota.get("raw_data")
    raw_usage = usage.get("rawData") if isinstance(usage.get("rawData"), dict) else usage.get("raw_data")
    candidates = [account.get("credits"), usage.get("credits"), quota.get("credits")]
    for raw in (raw_usage, raw_quota):
        if not isinstance(raw, dict):
            continue
        candidates.append(raw.get("credits"))
        spend = raw.get("spend_control")
        if not isinstance(spend, dict):
            spend = raw.get("spendControl")
        if isinstance(spend, dict):
            limit = spend.get("individual_limit")
            if not isinstance(limit, dict):
                limit = spend.get("individualLimit")
            candidates.append(limit)
    observed_false = False
    for candidate in candidates:
        usable = _credits_mapping_usable(candidate)
        if usable is True:
            return True
        if usable is False:
            observed_false = True
    return False if observed_false else None


def _codex_credits_usable(
    headers: Any = None,
    account: dict | None = None,
    capture: _SSEUsageCapture | None = None,
) -> bool | None:
    if capture is not None and capture.credits_usable is not None:
        return capture.credits_usable
    header_value = _credits_usable_from_headers(headers)
    if header_value is not None:
        return header_value
    return _credits_usable_from_account(account)


def _retry_after_seconds(headers: Any, *, now: float | None = None) -> float | None:
    raw = _header_value(headers, "retry-after")
    if raw:
        try:
            delay = float(raw)
        except (TypeError, ValueError, OverflowError):
            try:
                retry_at = parsedate_to_datetime(raw)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = retry_at.timestamp() - (time.time() if now is None else now)
            except (AttributeError, TypeError, ValueError, OverflowError):
                delay = -1.0
        if math.isfinite(delay) and delay >= 0:
            return delay
    reset_at = _header_value(headers, "x-codex-primary-reset-at")
    try:
        delay = float(reset_at) - (time.time() if now is None else now)
    except (TypeError, ValueError, OverflowError):
        return None
    return delay if math.isfinite(delay) and delay >= 0 else None


class _PrefixedResponse:
    """Replay a bounded probe prefix before continuing the original response."""

    def __init__(self, response: Any, prefix: bytes):
        self._response = response
        self._buffer = bytearray(prefix)
        self.headers = getattr(response, "headers", {})
        self.status = getattr(response, "status", 200)
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            head = bytes(self._buffer)
            self._buffer.clear()
            return head + self._response.read()
        if size == 0:
            return b""
        while len(self._buffer) < size:
            chunk = self._response.read(size - len(self._buffer))
            if not chunk:
                break
            self._buffer.extend(chunk)
        data = bytes(self._buffer[:size])
        del self._buffer[: len(data)]
        return data

    def readline(self, size: int = -1) -> bytes:
        while True:
            limit = len(self._buffer) if size is None or size < 0 else min(size, len(self._buffer))
            newline = self._buffer.find(b"\n", 0, limit)
            if newline >= 0:
                length = newline + 1
                line = bytes(self._buffer[:length])
                del self._buffer[:length]
                return line
            if size is not None and size >= 0 and len(self._buffer) >= size:
                line = bytes(self._buffer[:size])
                del self._buffer[:size]
                return line
            reader = getattr(self._response, "readline", None)
            remaining = -1 if size is None or size < 0 else max(1, size - len(self._buffer))
            chunk = reader(remaining) if callable(reader) else self._response.read(8192 if remaining < 0 else remaining)
            if not chunk:
                line = bytes(self._buffer)
                self._buffer.clear()
                return line
            self._buffer.extend(chunk)

    def read1(self, size: int = -1) -> bytes:
        if self._buffer:
            count = len(self._buffer) if size < 0 else min(size, len(self._buffer))
            result = bytes(self._buffer[:count])
            del self._buffer[:count]
            return result
        return getattr(self._response, "read1", self._response.read)(size)

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        line = self.readline()
        if line:
            return line
        raise StopIteration

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._buffer.clear()
        self._response.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)


def _capacity_gateway_error(
    error: dict | None,
    headers: Any,
    account: dict | None = None,
    retry_after_seconds: float | None = None,
) -> GatewayError:
    public_error = dict(error or {})
    public_error["type"] = "server_error"
    public_error["code"] = "server_error"
    public_error.setdefault("message", "上游模型暂时繁忙，请稍后重试。")
    forwarded = _codex_quota_headers(headers, account)
    if not _header_value(forwarded, "retry-after") and retry_after_seconds is not None:
        if math.isfinite(retry_after_seconds) and retry_after_seconds >= 0:
            forwarded["retry-after"] = f"{retry_after_seconds:g}"
    body = json.dumps({"error": public_error}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    gateway_error = GatewayError(
        "上游模型暂时繁忙，请稍后重试。",
        503,
        body,
        "application/json; charset=utf-8",
        forwarded,
    )
    gateway_error.transient_capacity = True
    gateway_error.route_account = account
    return gateway_error


def _set_response_idle_timeout(
    response: Any,
    timeout: float = UPSTREAM_STREAM_IDLE_TIMEOUT_SECONDS,
) -> bool:
    """Apply a per-read idle timeout across urllib response wrapper shapes."""

    pending: list[tuple[Any, int]] = [(response, 0)]
    seen: set[int] = set()
    while pending:
        candidate, depth = pending.pop(0)
        if candidate is None or id(candidate) in seen or depth > 4:
            continue
        seen.add(id(candidate))
        setter = getattr(candidate, "settimeout", None)
        if callable(setter):
            try:
                setter(timeout)
                return True
            except Exception:
                pass
        for attribute in ("fp", "raw", "_sock", "sock"):
            try:
                nested = getattr(candidate, attribute, None)
            except Exception:
                nested = None
            if nested is not None:
                pending.append((nested, depth + 1))
    return False


def _response_session_affine(payload: dict) -> bool:
    if str(payload.get("previous_response_id") or "").strip():
        return True
    conversation = payload.get("conversation")
    if isinstance(conversation, dict):
        return bool(str(conversation.get("id") or "").strip())
    return bool(str(conversation or "").strip())


def _has_opaque_history(payload: dict) -> bool:
    items = payload.get("input")
    return isinstance(items, list) and any(
        isinstance(item, dict) and item.get("encrypted_content")
        for item in items
    )


def _portable_history_replay(payload: dict) -> dict | None:
    """Copy an explicit stateless history after an encrypted-state rejection.

    Cursor-backed requests and compacted windows do not establish the original
    context. Never guess their missing messages, tools or encrypted contents.
    Only an ordinary message/function/custom-tool history can be replayed.
    """
    if _response_session_affine(payload) or not _has_opaque_history(payload):
        return None
    items = payload.get("input")
    portable = []
    pending = {}
    seen_calls = set()
    user_seen = assistant_seen = False
    for item in items:
        if not isinstance(item, dict):
            return None
        kind = item.get("type", "message" if item.get("role") else "")
        if not isinstance(kind, str):
            return None
        if kind == "reasoning" and item.get("encrypted_content"):
            continue
        if kind == "message":
            role = item.get("role")
            content = item.get("content")
            if not isinstance(role, str) or role not in {"user", "assistant", "system", "developer"}:
                return None
            if not isinstance(content, (str, list)) or not content:
                return None
            if isinstance(content, list):
                for part in content:
                    if (not isinstance(part, dict)
                            or part.get("type") not in ("input_text", "output_text", "refusal", "input_image", "input_file")
                            or part.get("file_id")):
                        return None
            if role == "user":
                if pending:
                    return None
                user_seen = True
            elif role == "assistant":
                if not user_seen:
                    return None
                assistant_seen = True
        elif kind in {"function_call", "custom_tool_call"}:
            call_id = item.get("call_id")
            if not user_seen or not isinstance(call_id, str) or not call_id or call_id in seen_calls:
                return None
            if not isinstance(item.get("name"), str) or not item["name"]:
                return None
            if not isinstance(item.get("arguments" if kind == "function_call" else "input"), str):
                return None
            seen_calls.add(call_id)
            pending[call_id] = kind + "_output"
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or pending.pop(call_id, None) != kind or "output" not in item:
                return None
            assistant_seen = True
        else:
            # Includes compaction, item_reference and provider-owned tool state.
            return None
        copied = dict(item)
        # Item IDs are server-owned. Tool association uses call_id, which is
        # preserved with arguments/output, message phase and all content parts.
        copied.pop("id", None)
        portable.append(copied)
    if not user_seen or not assistant_seen or pending:
        return None
    replay = json.loads(json.dumps(payload))
    replay["input"] = json.loads(json.dumps(portable))
    return replay


def _encrypted_history_replay(payload: dict, status: int, body: bytes) -> dict | None:
    """Retry only an explicit pre-inference encrypted-content validation error."""
    if status != 400:
        return None
    try:
        error = _explicit_error_object(json.loads(body))
    except (ValueError, UnicodeError):
        return None
    if not error or (error.get("code") != "invalid_encrypted_content" and error.get("type") != "invalid_encrypted_content"):
        return None
    replay = _portable_history_replay(payload)
    if replay is None:
        raise GatewayError(
            "当前上游无法读取旧会话的加密上下文。请切回原账号/服务继续，或提供包含原始消息及配对工具调用的完整历史；"
            "压缩上下文或 previous_response_id 不能直接跨账号恢复。原会话未修改。", 409,
        )
    return replay


def _abort_upstream_response(response: Any) -> None:
    """Interrupt a blocked HTTP read before closing its buffered wrapper."""
    pending = [(response, 0)]
    seen = set()
    while pending:
        candidate, depth = pending.pop()
        if candidate is None or id(candidate) in seen or depth > 6:
            continue
        seen.add(id(candidate))
        shutdown = getattr(candidate, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown(socket.SHUT_RDWR)
            except (OSError, ValueError):
                pass
        for attribute in ("fp", "raw", "_sock", "sock", "_response"):
            nested = getattr(candidate, attribute, None)
            if nested is not None:
                pending.append((nested, depth + 1))
    try:
        response.close()
    except (OSError, ValueError):
        pass


def _epoch_seconds(value: Any) -> int | None:
    parsed = _parse_expiry(value)
    return int(parsed) if parsed is not None else None


def _codex_quota_headers(headers: Any, account: dict | None = None) -> dict[str, str]:
    """Preserve upstream quota headers and fill a weekly-only fallback.

    Codex parses the x-codex-* family into its native RateLimitSnapshot. The
    fallback is used only when an upstream response omits window headers.
    """
    forwarded = _safe_response_headers(headers)
    if not account:
        return forwarded
    usage = account.get("usage") if isinstance(account.get("usage"), dict) else {}
    weekly = usage.get("weekly") if isinstance(usage.get("weekly"), dict) else {}
    try:
        remaining = float(weekly.get("remainingPercent"))
    except (TypeError, ValueError):
        remaining = None
    if remaining is not None:
        used = max(0.0, min(100.0, 100.0 - remaining))
        try:
            window_minutes = int(weekly.get("windowMinutes") or 10_080)
        except (TypeError, ValueError):
            window_minutes = 10_080
        forwarded.setdefault("x-codex-primary-used-percent", f"{used:g}")
        forwarded.setdefault("x-codex-primary-window-minutes", str(max(1, window_minutes)))
        reset_at = _epoch_seconds(weekly.get("resetAt"))
        if reset_at:
            forwarded.setdefault("x-codex-primary-reset-at", str(reset_at))
    plan = str(account.get("plan") or account.get("planLabel") or "").strip()
    if plan:
        forwarded.setdefault("x-codex-plan-type", plan)
    return forwarded


def _read_limited(response: Any) -> bytes:
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise GatewayError("上游响应超过 32 MB 安全限制。", 502)
    return body


def _sse_events(body: bytes) -> list[dict]:
    events = []
    for block in body.decode("utf-8", errors="replace").replace("\r\n", "\n").split("\n\n"):
        data_lines = [line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")]
        if not data_lines:
            continue
        raw = "\n".join(data_lines)
        if raw == "[DONE]":
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _completed_response(body: bytes) -> dict:
    events = _sse_events(body)
    completed_items: list[tuple[int, dict]] = []
    for event in events:
        if event.get("type") != "response.output_item.done" or not isinstance(event.get("item"), dict):
            continue
        try:
            output_index = int(event.get("output_index"))
        except (TypeError, ValueError):
            output_index = len(completed_items)
        completed_items.append((max(0, output_index), dict(event["item"])))

    def with_completed_items(response: dict) -> dict:
        if not completed_items:
            return response
        merged = dict(response)
        terminal_output = [dict(item) for item in response.get("output", []) if isinstance(item, dict)]
        by_identity = {
            str(item.get("id") or item.get("call_id") or ""): index
            for index, item in enumerate(terminal_output)
            if item.get("id") or item.get("call_id")
        }
        for output_index, item in completed_items:
            identity = str(item.get("id") or item.get("call_id") or "")
            existing_index = by_identity.get(identity) if identity else None
            if existing_index is not None:
                terminal_output[existing_index] = item
                merged_index = existing_index
            elif output_index < len(terminal_output) and terminal_output[output_index].get("type") == item.get("type"):
                terminal_output[output_index] = item
                merged_index = output_index
            else:
                terminal_output.append(item)
                merged_index = len(terminal_output) - 1
            if identity:
                by_identity[identity] = merged_index
        merged["output"] = terminal_output
        return merged

    for event in reversed(events):
        event_type = event.get("type")
        response = event.get("response") if isinstance(event.get("response"), dict) else None
        if event_type in {"response.completed", "response.incomplete"} and response is not None:
            return with_completed_items(response)
        if event_type in {"response.failed", "error"}:
            error = event.get("error") or (response or {}).get("error") or {}
            message = str(error.get("message") or "上游响应失败。") if isinstance(error, dict) else str(error)
            raise GatewayError(core._redact_sensitive_text(message, limit=300), 502)
        if response is not None and response.get("status") in {"completed", "incomplete"}:
            return with_completed_items(response)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise GatewayError("上游没有返回完整 response.completed 事件。", 502) from exc
    if isinstance(payload, dict):
        response = payload.get("response") if isinstance(payload.get("response"), dict) else payload
        if response.get("status") == "failed" or response.get("error"):
            error = response.get("error") or payload.get("error") or {}
            message = str(error.get("message") or "上游响应失败。") if isinstance(error, dict) else str(error)
            raise GatewayError(core._redact_sensitive_text(message, limit=300), 502)
        return response
    raise GatewayError("上游响应格式无效。", 502)


def _response_text(response: dict) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str):
        return direct
    chunks = []
    for item in response.get("output", []) if isinstance(response.get("output"), list) else []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []) if isinstance(item.get("content"), list) else []:
            if isinstance(content, dict) and content.get("type") in {"output_text", "text"}:
                chunks.append(str(content.get("text") or ""))
    return "".join(chunks)


def _custom_tool_input(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _custom_tool_chat_arguments(value: Any) -> str:
    return json.dumps(
        {"input": _custom_tool_input(value)},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _custom_input_from_chat_arguments(value: Any) -> str:
    raw = _custom_tool_input(value)
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(decoded, dict) and set(decoded) == {"input"}:
        return _custom_tool_input(decoded["input"])
    return raw


def _chat_custom_tool_call(item: dict, *, index: int | None = None) -> dict:
    name = str(item.get("name") or "tool")
    call = {
        "id": str(item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:16]}"),
        "type": "function",
        "function": {
            "name": name,
            "arguments": _custom_tool_chat_arguments(item.get("input")),
        },
        # Chat Completions only permits ``function`` here. Preserve the
        # Responses tool kind as an extension so a following tool-output turn
        # can reconstruct ``custom_tool_call_output`` without guessing.
        "x_openai_tool_type": "custom",
    }
    if index is not None:
        call["index"] = index
    return call


def _response_tool_calls(response: dict) -> list[dict]:
    calls = []
    for item in response.get("output", []) if isinstance(response.get("output"), list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "custom_tool_call":
            calls.append(_chat_custom_tool_call(item))
            continue
        if item.get("type") not in {"function_call", "tool_call"}:
            continue
        calls.append(
            {
                "id": str(item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:16]}"),
                "type": "function",
                "function": {
                    "name": str(item.get("name") or "tool"),
                    "arguments": str(item.get("arguments") or "{}"),
                },
            }
        )
    return calls


def _normalize_chat_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    normalized = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type == "text":
            normalized.append({"type": "input_text", "text": str(part.get("text") or "")})
        elif part_type == "image_url":
            image = part.get("image_url")
            image_url = image.get("url") if isinstance(image, dict) else image
            normalized.append({"type": "input_image", "image_url": image_url, "detail": (image or {}).get("detail", "auto") if isinstance(image, dict) else "auto"})
        else:
            normalized.append(part)
    return normalized


def _chat_tool_definitions(payload: dict) -> tuple[list[dict], set[str]]:
    tools = []
    custom_names: set[str] = set()
    for tool in payload.get("tools", []) if isinstance(payload.get("tools"), list) else []:
        if not isinstance(tool, dict):
            continue
        tool_type = str(tool.get("type") or "").casefold()
        function = tool.get("function") if isinstance(tool.get("function"), dict) else None
        if tool_type == "function":
            if function is None:
                raise GatewayError("function 工具必须包含有效的 function 对象。", 400)
            name = str(function.get("name") or "").strip()
            if not name:
                raise GatewayError("function 工具缺少名称。", 400)
            tools.append(
                {
                    "type": "function",
                    "name": name,
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters", {"type": "object", "properties": {}}),
                    "strict": bool(function.get("strict", False)),
                }
            )
            continue
        if tool_type in {"custom", "freeform"}:
            custom = tool.get("custom") if isinstance(tool.get("custom"), dict) else tool
            name = str(custom.get("name") or tool.get("name") or "").strip()
            if not name:
                raise GatewayError("custom/freeform 工具缺少名称。", 400)
            converted = {"type": "custom", "name": name}
            for key in ("description", "format"):
                if key in custom:
                    converted[key] = custom[key]
                elif key in tool:
                    converted[key] = tool[key]
            tools.append(converted)
            custom_names.add(name)
            continue
        label = tool_type or "unknown"
        raise GatewayError(
            f"chat/completions 无法无损表示 {label} 工具；请改用 /v1/responses。",
            400,
        )
    return tools, custom_names


def _chat_tool_choice(value: Any, custom_names: set[str]) -> Any:
    if not isinstance(value, dict):
        return value
    choice_type = str(value.get("type") or "").casefold()
    details = value.get(choice_type) if isinstance(value.get(choice_type), dict) else value
    name = str(details.get("name") or value.get("name") or "").strip()
    if choice_type in {"custom", "freeform"} or name in custom_names:
        if not name:
            raise GatewayError("custom tool_choice 缺少名称。", 400)
        return {"type": "custom", "name": name}
    if choice_type == "function":
        if not name:
            raise GatewayError("function tool_choice 缺少名称。", 400)
        return {"type": "function", "name": name}
    return dict(value)


def _chat_input(payload: dict) -> dict:
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise GatewayError("chat/completions 请求必须包含 messages。")
    tools, custom_tool_names = _chat_tool_definitions(payload)
    instructions = []
    input_items = []
    custom_call_ids: set[str] = set()
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user")
        content = item.get("content", "")
        if role in {"system", "developer"}:
            if isinstance(content, str):
                instructions.append(content)
            continue
        if role == "tool":
            call_id = str(item.get("tool_call_id") or item.get("call_id") or "")
            output_type = (
                "custom_tool_call_output"
                if call_id in custom_call_ids
                or str(item.get("x_openai_tool_type") or "").casefold() in {"custom", "freeform"}
                else "function_call_output"
            )
            input_items.append(
                {
                    "type": output_type,
                    "call_id": call_id,
                    "output": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
                }
            )
            continue
        normalized_content = _normalize_chat_content(content)
        if normalized_content is not None and normalized_content != "":
            input_items.append({"role": role, "content": normalized_content})
        if role == "assistant" and isinstance(item.get("tool_calls"), list):
            for call in item["tool_calls"]:
                if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                    raise GatewayError("assistant.tool_calls 必须包含有效的 function 对象。", 400)
                function = call.get("function") if isinstance(call, dict) and isinstance(call.get("function"), dict) else {}
                call_id = str(call.get("id") or f"call_{uuid.uuid4().hex[:16]}")
                name = str(function.get("name") or "tool")
                custom_call = (
                    str(call.get("x_openai_tool_type") or "").casefold() in {"custom", "freeform"}
                    or name in custom_tool_names
                )
                if custom_call:
                    custom_call_ids.add(call_id)
                    input_items.append(
                        {
                            "type": "custom_tool_call",
                            "call_id": call_id,
                            "name": name,
                            "input": _custom_input_from_chat_arguments(function.get("arguments")),
                        }
                    )
                else:
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": name,
                            "arguments": str(function.get("arguments") or "{}"),
                        }
                    )
    result = {
        "model": str(payload.get("model") or ""),
        "input": input_items,
        "instructions": "\n\n".join(part for part in instructions if part),
        "stream": True,
        "store": False,
        "parallel_tool_calls": bool(payload.get("parallel_tool_calls", True)),
    }
    if payload.get("max_completion_tokens") is not None:
        result["max_output_tokens"] = payload["max_completion_tokens"]
    elif payload.get("max_tokens") is not None:
        result["max_output_tokens"] = payload["max_tokens"]
    for key in ("metadata", "reasoning", "temperature", "top_p", "service_tier", "prompt_cache_key", "safety_identifier"):
        if key in payload:
            result[key] = payload[key]
    if "reasoning_effort" in payload:
        reasoning = dict(result.get("reasoning") or {})
        if reasoning.get("effort") not in (None, payload["reasoning_effort"]):
            raise GatewayError("reasoning 与 reasoning_effort 相互冲突。", 400)
        reasoning["effort"] = payload["reasoning_effort"]
        result["reasoning"] = reasoning
    if "response_format" in payload:
        format_value = payload["response_format"]
        if not isinstance(format_value, dict):
            raise GatewayError("response_format 必须为对象。", 400)
        format_type = format_value.get("type")
        if format_type == "json_schema":
            schema = format_value.get("json_schema")
            if not isinstance(schema, dict) or not schema.get("name") or not isinstance(schema.get("schema"), dict):
                raise GatewayError("json_schema 缺少 name 或 schema。", 400)
            result["text"] = {"format": {**schema, "type": "json_schema"}}
        elif format_type in {"text", "json_object"}:
            result["text"] = {"format": {"type": format_type}}
        else:
            raise GatewayError("不支持该 response_format 类型。", 400)
    if tools:
        result["tools"] = tools
        if "tool_choice" in payload:
            result["tool_choice"] = _chat_tool_choice(payload["tool_choice"], custom_tool_names)
    elif payload.get("tool_choice") is not None and payload.get("tool_choice") not in ("auto", "none"):
        raise GatewayError("tool_choice 指定了工具，但 tools 为空。", 400)
    return result


def _responses_lite_tool_allowed(tool: Any) -> bool:
    if not isinstance(tool, dict):
        return False
    tool_type = str(tool.get("type") or "").strip().casefold()
    if tool_type in {"function", "custom", "namespace"}:
        return True
    return tool_type == "tool_search" and str(tool.get("execution") or "").strip().casefold() == "client"


def _responses_lite_tool_choice(choice: Any) -> Any:
    if isinstance(choice, str):
        return choice if choice.strip().casefold() in {"auto", "none", "required"} else None
    if not isinstance(choice, dict):
        return None
    choice_type = str(choice.get("type") or "").strip().casefold()
    if choice_type in {"function", "custom", "namespace"}:
        return choice
    if choice_type == "tool_search":
        return choice if _responses_lite_tool_allowed(choice) else None
    if choice_type != "allowed_tools":
        return None
    normalized = json.loads(json.dumps(choice))
    kept_any = False
    for key in ("tools", "allowed_tools"):
        tools = normalized.get(key)
        if not isinstance(tools, list):
            continue
        filtered = [tool for tool in tools if _responses_lite_tool_allowed(tool)]
        if filtered:
            normalized[key] = filtered
            kept_any = True
        else:
            normalized.pop(key, None)
    nested = normalized.get("allowed_tools")
    if isinstance(nested, dict) and isinstance(nested.get("tools"), list):
        filtered = [tool for tool in nested["tools"] if _responses_lite_tool_allowed(tool)]
        if filtered:
            nested["tools"] = filtered
            kept_any = True
        else:
            normalized.pop("allowed_tools", None)
    return normalized if kept_any else None


def _normalize_responses_lite_tool_object(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    normalized = json.loads(json.dumps(value))
    tools = normalized.get("tools")
    if isinstance(tools, list):
        filtered = [tool for tool in tools if _responses_lite_tool_allowed(tool)]
        if filtered:
            normalized["tools"] = filtered
        else:
            normalized.pop("tools", None)
    if "tool_choice" in normalized:
        choice = _responses_lite_tool_choice(normalized.get("tool_choice"))
        if choice is None:
            normalized.pop("tool_choice", None)
        else:
            normalized["tool_choice"] = choice
    return normalized


def _normalize_responses_lite_payload(payload: dict) -> dict:
    normalized = _normalize_responses_lite_tool_object(payload) or {}
    raw_input = normalized.get("input")
    if isinstance(raw_input, list):
        filtered_input = []
        for item in raw_input:
            if not isinstance(item, dict) or str(item.get("type") or "").strip().casefold() != "additional_tools":
                # Historical messages and opaque reasoning state are not tool
                # declarations. Preserve them byte-for-byte at the JSON value level.
                filtered_input.append(item)
                continue
            filtered_item = _normalize_responses_lite_tool_object(item)
            if filtered_item is not None and isinstance(filtered_item.get("tools"), list):
                filtered_input.append(filtered_item)
        normalized["input"] = filtered_input
    if isinstance(normalized.get("response"), dict):
        normalized["response"] = _normalize_responses_lite_tool_object(normalized["response"])
    normalized["parallel_tool_calls"] = False
    return normalized


def _responses_lite_enabled(
    model: str,
    route: dict | None = None,
    client_headers: dict[str, str] | None = None,
) -> bool:
    if any(str(name).casefold() == CODEX_RESPONSES_LITE_HEADER.casefold() for name in (client_headers or {})):
        return True
    candidates = [route]
    if isinstance(route, dict):
        candidates.extend(
            route.get(key)
            for key in ("capability", "capabilities", "modelCapability")
            if isinstance(route.get(key), dict)
        )
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ("use_responses_lite", "useResponsesLite"):
            if key in candidate:
                return candidate.get(key) is True
    # The native catalog is already loaded while building an aggregate profile.
    # Read only that in-memory snapshot; never launch model discovery on a request.
    cache = getattr(core, "MODEL_CACHE", None)
    lock = getattr(core, "MODEL_CACHE_LOCK", None)
    try:
        if isinstance(cache, dict) and lock is not None:
            with lock:
                raw = json.loads(json.dumps(cache.get("raw"))) if isinstance(cache.get("raw"), dict) else None
        else:
            raw = None
    except Exception:
        raw = None
    for item in (raw or {}).get("models", []):
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("slug") or item.get("id") or "").strip()
        if item_id == str(model or "").strip() and "use_responses_lite" in item:
            return item.get("use_responses_lite") is True
    return False


def _responses_input(payload: dict, *, responses_lite: bool = False) -> dict:
    if "input" not in payload and not payload.get("previous_response_id") and not payload.get("prompt"):
        raise GatewayError("responses 请求必须包含 input、previous_response_id 或 prompt。")
    result = json.loads(json.dumps(payload))
    if result.get("instructions") is None:
        result["instructions"] = ""
    elif not isinstance(result["instructions"], (str, list)):
        raise GatewayError("instructions 必须为字符串或消息列表。", 400)
    result["stream"] = True
    result.setdefault("store", False)
    result.setdefault("parallel_tool_calls", True)
    return _normalize_responses_lite_payload(result) if responses_lite else result


def _chat_finish_reason(response: dict, has_tool_calls: bool = False) -> str:
    details = response.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, dict) else None
    if reason == "max_output_tokens":
        return "length"
    if reason == "content_filter":
        return "content_filter"
    return "tool_calls" if has_tool_calls else "stop"


def _chat_completion(response: dict, requested_model: str) -> dict:
    tool_calls = _response_tool_calls(response)
    message = {"role": "assistant", "content": _response_text(response)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    reasoning = _response_reasoning_summary(response)
    if reasoning:
        message["reasoning_content"] = reasoning
    return {
        "id": str(response.get("id") or f"chatcmpl_{uuid.uuid4().hex}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": str(response.get("model") or requested_model),
        "choices": [{"index": 0, "message": message, "finish_reason": _chat_finish_reason(response, bool(tool_calls))}],
        "usage": _chat_usage(response.get("usage")),
    }


def _response_reasoning_summary(response: dict) -> str:
    parts = []
    for item in response.get("output", []) if isinstance(response.get("output"), list) else []:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            continue
        for part in item.get("summary", []) if isinstance(item.get("summary"), list) else []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
    return "".join(parts)


def _chat_usage(raw: Any) -> dict:
    usage = _normalized_usage(raw) or _empty_usage_counters()
    result = {"prompt_tokens": usage["inputTokens"], "completion_tokens": usage["outputTokens"],
              "total_tokens": usage["totalTokens"]}
    if isinstance(raw, dict) and isinstance(raw.get("input_tokens_details"), dict):
        result["prompt_tokens_details"] = dict(raw["input_tokens_details"])
    if isinstance(raw, dict) and isinstance(raw.get("output_tokens_details"), dict):
        result["completion_tokens_details"] = dict(raw["output_tokens_details"])
    return result


def _chat_sse(body: bytes, requested_model: str, *, include_usage: bool = False) -> bytes:
    # Buffered and live streams must share tool-call and completion semantics.
    return b"".join(Web2APIManager._chat_stream_chunks(io.BytesIO(body), requested_model, include_usage=include_usage))


def _gateway_bind_error(host: str, port: int, error: OSError) -> tuple[str, str]:
    winerror = getattr(error, "winerror", None)
    error_code = winerror if winerror is not None else getattr(error, "errno", None)
    address = f"{host}:{port}"
    if error_code in {10048, errno.EADDRINUSE}:
        detail = f"{address} 已被其他程序占用"
        if error_code is not None:
            detail += f"（系统错误 {error_code}）"
        return (
            f"本地 API 启动失败：{detail}。请关闭占用该端口的程序，"
            "或在 Web2API 设置中手动选择另一端口后重试。",
            f"本地 API 端口被占用（{error_code if error_code is not None else 'address in use'}）",
        )
    if error_code in {10013, errno.EACCES, errno.EPERM}:
        windows_denied = error_code == 10013
        detail = f"Windows 拒绝绑定 {address}" if windows_denied else f"系统拒绝绑定 {address}"
        if error_code is not None:
            detail += f"（系统错误 {error_code}）"
        suggestion = (
            "该端口可能受权限策略或 Hyper-V/WSL 排除/保留端口范围保护。"
            "可用只读命令 `netsh interface ipv4 show excludedportrange protocol=tcp` 和 "
            "`netsh interface ipv6 show excludedportrange protocol=tcp` 核对，"
            "然后在 Web2API 设置中手动选择未保留端口；程序不会自动修改端口。"
            if os.name == "nt" or windows_denied
            else "请检查当前用户的本地监听权限，或在 Web2API 设置中手动选择允许的端口。"
        )
        return (
            f"本地 API 启动失败：{detail}。{suggestion}",
            f"本地 API 端口绑定被拒绝（{error_code if error_code is not None else 'permission denied'}）",
        )
    suffix = f"（系统错误 {error_code}）" if error_code is not None else ""
    return (
        f"本地 API 启动失败{suffix}；请检查本地监听权限和 Web2API 端口设置。",
        f"本地 API 端口无法监听{suffix}",
    )


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], manager: "Web2APIManager"):
        super().__init__(address, GatewayHandler)
        self.manager = manager
        self._thread_slots = threading.BoundedSemaphore(MAX_GATEWAY_THREADS)
        self._upstream_slots = threading.BoundedSemaphore(MAX_UPSTREAM_CONCURRENCY)

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        ThreadingHTTPServer.server_bind(self)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._thread_slots.acquire(blocking=False):
            with self.manager.lock:
                self.manager.rejected_request_count += 1
            try:
                body = b'{"error":{"message":"Web2API is busy; retry shortly","type":"busy"}}'
                headers = (
                    b"HTTP/1.1 429 Too Many Requests\r\n"
                    b"Content-Type: application/json; charset=utf-8\r\n"
                    b"Cache-Control: no-store\r\nRetry-After: 2\r\nConnection: close\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                )
                request.sendall(headers + body)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        with self.manager.lock:
            self.manager.active_request_count += 1
        try:
            super().process_request(request, client_address)
        except Exception:
            with self.manager.lock:
                self.manager.active_request_count = max(0, self.manager.active_request_count - 1)
            self._thread_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            if hasattr(request, "settimeout"):
                request.settimeout(GATEWAY_CLIENT_TIMEOUT_SECONDS)
            super().process_request_thread(request, client_address)
        finally:
            with self.manager.lock:
                self.manager.active_request_count = max(0, self.manager.active_request_count - 1)
            self._thread_slots.release()

    @contextmanager
    def upstream_slot(self):
        if not self._upstream_slots.acquire(timeout=UPSTREAM_QUEUE_TIMEOUT_SECONDS):
            with self.manager.lock:
                self.manager.rejected_request_count += 1
            raise GatewayError(
                "Web2API 当前请求较多，请稍后重试。",
                429,
                headers={"Retry-After": str(max(1, int(UPSTREAM_QUEUE_TIMEOUT_SECONDS)))},
            )
        try:
            yield
        finally:
            self._upstream_slots.release()


class GatewayHandler(BaseHTTPRequestHandler):
    server: GatewayServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _headers(
        self,
        status: int,
        content_type: str,
        length: int,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _send(
        self,
        body: bytes,
        status: int = 200,
        content_type: str = "application/json; charset=utf-8",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._headers(status, content_type, len(body), extra_headers)
        self.wfile.write(body)

    def _stream(
        self,
        chunks: Any,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        iterator = iter(chunks)
        headers_sent = False
        writing_downstream = False

        def close_upstream() -> None:
            seen: set[int] = set()
            for candidate in (iterator, chunks):
                if id(candidate) in seen:
                    continue
                seen.add(id(candidate))
                close = getattr(candidate, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass

        def mark_client_cancelled() -> None:
            manager = self.server.manager
            lock = getattr(manager, "lock", None)
            if lock is None:
                manager.client_cancelled_count = int(getattr(manager, "client_cancelled_count", 0)) + 1
                return
            with lock:
                manager.client_cancelled_count = int(getattr(manager, "client_cancelled_count", 0)) + 1

        def write_downstream(chunk: bytes) -> None:
            nonlocal writing_downstream
            writing_downstream = True
            self.wfile.write(chunk)
            self.wfile.flush()
            writing_downstream = False

        try:
            first_chunk = next((chunk for chunk in iterator if chunk), None)
            if first_chunk is None:
                raise GatewayError("上游流式响应为空。", 502)
            writing_downstream = True
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            writing_downstream = False
            headers_sent = True
            self.close_connection = True
            write_downstream(first_chunk)
            for chunk in iterator:
                if chunk:
                    write_downstream(chunk)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, socket.timeout) as exc:
            if writing_downstream:
                mark_client_cancelled()
                return
            if not headers_sent:
                raise
            self.server.manager.last_error = f"上游流式连接已中断（{type(exc).__name__}）"
            error = GatewayError("上游流式响应提前结束。", 502)
            try:
                write_downstream(self._stream_failure_event(error))
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, socket.timeout):
                mark_client_cancelled()
        except Exception as exc:
            if not headers_sent:
                raise
            self.server.manager.last_error = f"上游流式连接已中断（{type(exc).__name__}）"
            try:
                write_downstream(self._stream_failure_event(exc))
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, socket.timeout):
                mark_client_cancelled()
        finally:
            close_upstream()

    def _stream_failure_event(self, exc: Exception) -> bytes:
        message = (
            str(exc)
            if isinstance(exc, GatewayError)
            else "上游流式响应提前结束。"
        )
        error = {
            "message": core._redact_sensitive_text(message, limit=300),
            "type": "upstream_stream_truncated",
            "code": "upstream_stream_truncated",
        }
        path = urlparse(str(getattr(self, "path", ""))).path
        payload = (
            {
                "type": "response.failed",
                "response": {"status": "failed", "error": error},
                "error": error,
            }
            if path == "/v1/responses"
            else {"error": error}
        )
        return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")

    def _json(self, payload: object, status: int = 200) -> None:
        self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"), status)

    def _header_values(self, name: str) -> list[str]:
        try:
            values = self.headers.get_all(name) or []
        except (AttributeError, TypeError):
            values = []
        return [str(value).strip() for value in values if str(value).strip()]

    def _client_identity_headers(self) -> dict[str, str]:
        forwarded: dict[str, str] = {}
        for name in SAFE_CLIENT_IDENTITY_HEADERS:
            if name == CODEX_RESPONSES_LITE_HEADER:
                try:
                    raw_values = self.headers.get_all(name)
                except (AttributeError, TypeError):
                    raw_values = None
                if raw_values is not None and len(raw_values) == 1:
                    value = _safe_header_text(raw_values[0])
                    forwarded[name] = value or "true"
                continue
            values = self._header_values(name)
            if len(values) != 1:
                continue
            value = _safe_header_text(
                values[0],
                8192 if name == "X-OpenAI-Actor-Authorization" else 512,
            )
            if value:
                forwarded[name] = value
        return forwarded

    def _validate_http_request(self, *, json_body: bool = False) -> bool:
        """Reject ambiguous framing before authentication or body reads."""

        supplied_host = str(self.headers.get("Host") or "").strip()
        if not supplied_host or any(character in supplied_host for character in "\r\n/\\"):
            self.close_connection = True
            self._error(GatewayError("请求 Host 无效。", 400))
            return False
        try:
            parsed_host = urlparse(f"//{supplied_host}")
            hostname = str(parsed_host.hostname or "").rstrip(".").casefold()
            port = parsed_host.port or int(self.server.server_address[1])
        except (TypeError, ValueError):
            self.close_connection = True
            self._error(GatewayError("请求 Host 无效。", 400))
            return False
        allowed_host = str(self.server.server_address[0] or "127.0.0.1").casefold()
        if hostname not in {allowed_host, "127.0.0.1", "localhost"} or port != int(self.server.server_address[1]):
            self.close_connection = True
            self._error(GatewayError("请求 Host 与本地网关监听地址不匹配。", 421))
            return False
        if self._header_values("Transfer-Encoding"):
            self.close_connection = True
            self._error(GatewayError("不支持 Transfer-Encoding 请求，请使用带长度的 JSON 请求。", 400))
            return False
        lengths = self._header_values("Content-Length")
        if len(lengths) > 1 or (lengths and not re.fullmatch(r"[0-9]+", lengths[0])):
            self.close_connection = True
            self._error(GatewayError("请求长度无效或包含重复的 Content-Length。", 400))
            return False
        if json_body:
            content_type = str(self.headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
            if content_type != "application/json" and not content_type.endswith("+json"):
                self.close_connection = True
                self._error(GatewayError("JSON 请求必须使用 Content-Type: application/json。", 400))
                return False
        return True

    def _error(self, error: Exception) -> None:
        if isinstance(error, GatewayError) and error.body is not None:
            self._send(error.body, error.status, error.content_type, error.headers)
            return
        if isinstance(error, GatewayError):
            body = json.dumps(
                {"error": {"message": str(error), "type": "web2api_error"}},
                ensure_ascii=False,
            ).encode("utf-8")
            self._send(
                body,
                error.status,
                "application/json; charset=utf-8",
                error.headers,
            )
            return
        # Unexpected exception text may contain a URL, bearer token, local
        # path, or upstream response fragment. Keep the concrete exception
        # out of both the HTTP response and the public status snapshot.
        self.server.manager.last_error = f"内部请求错误（{type(error).__name__}）"
        self._json(
            {"error": {"message": "本地 API 处理请求时发生内部错误。", "type": "web2api_error"}},
            500,
        )

    @staticmethod
    def _secret_matches(expected: object, supplied: object) -> bool:
        expected_bytes = str(expected or "").encode("utf-8")
        supplied_bytes = str(supplied or "").encode("utf-8")
        return bool(expected_bytes and supplied_bytes and secrets.compare_digest(expected_bytes, supplied_bytes))

    def _access_scope(self) -> str | None:
        authorization_values = self._header_values("Authorization")
        api_key_values = self._header_values("X-API-Key")
        if (
            len(authorization_values) > 1
            or len(api_key_values) > 1
            or (authorization_values and api_key_values)
        ):
            return None
        authorization = authorization_values[0] if authorization_values else ""
        supplied = (
            authorization[7:].strip()
            if authorization.lower().startswith("bearer ")
            else api_key_values[0] if api_key_values else ""
        )
        try:
            public_secret = core.load_service_secret("web2api") or ""
        except core.ManagerError:
            public_secret = ""
        # A secret collision must fail closed as public rather than silently
        # granting internal aggregate access.
        if self._secret_matches(public_secret, supplied):
            return "public"
        try:
            internal_secret = core.load_service_secret("gateway_internal") or ""
        except core.ManagerError:
            internal_secret = ""
        if self._secret_matches(internal_secret, supplied):
            return "internal"
        return None

    def _authorized(self) -> bool:
        return self._access_scope() is not None

    def _require_auth(self) -> str:
        access_scope = self._access_scope()
        if access_scope is None:
            raise GatewayError("Web2API 密钥无效。", 401)
        return access_scope

    def do_OPTIONS(self) -> None:
        self._send(b"", 204, "text/plain")

    def do_GET(self) -> None:
        try:
            if not self._validate_http_request():
                return
            path = urlparse(self.path).path
            if path == "/health":
                self._json({"ok": True, "service": "Agent Manager Web2API"})
                return
            access_scope = self._require_auth()
            if path == "/v1/responses" and self._header_values("Upgrade"):
                from web2api_websocket import serve_responses_websocket
                serve_responses_websocket(self, access_scope)
                return
            if path == "/status":
                self._json(self.server.manager.status(access_scope=access_scope))
                return
            if path == "/v1/models":
                self._json(self.server.manager.models(access_scope=access_scope))
                return
            raise GatewayError("接口不存在。", 404)
        except Exception as exc:
            self._error(exc)

    def do_POST(self) -> None:
        try:
            if not self._validate_http_request(json_body=True):
                return
            access_scope = self._require_auth()
            lengths = self._header_values("Content-Length")
            try:
                length = int(lengths[0], 10) if lengths else 0
            except (TypeError, ValueError) as exc:
                raise GatewayError("请求长度无效。") from exc
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise GatewayError("请求为空或超过 8 MB。", 413)
            try:
                raw = self.rfile.read(length)
                if len(raw) != length:
                    self.close_connection = True
                    raise GatewayError("请求内容未完整接收。", 400)
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GatewayError("请求 JSON 无效。") from exc
            if not isinstance(payload, dict):
                raise GatewayError("请求必须是 JSON 对象。")
            path = urlparse(self.path).path
            client_headers = self._client_identity_headers()
            manager = self.server.manager
            manager_kwargs: dict[str, Any] = {"access_scope": access_scope}
            if isinstance(manager, Web2APIManager):
                manager_kwargs["client_headers"] = client_headers
            with self.server.upstream_slot():
                if payload.get("stream") and path != "/v1/alpha/search" and path not in RESPONSES_AUXILIARY_PATHS:
                    stream = manager.stream(
                        path,
                        payload,
                        **manager_kwargs,
                    )
                    self._stream(stream["chunks"], stream["contentType"], stream.get("headers"))
                    return
                result = manager.execute(
                    path,
                    payload,
                    **manager_kwargs,
                )
                self._send(result["body"], result["status"], result["contentType"], result.get("headers"))
                delivered = result.get("_onDelivered")
                if callable(delivered):
                    try:
                        delivered()
                    except Exception:
                        pass
        except Exception as exc:
            self._error(exc)


class Web2APIManager:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.server: GatewayServer | None = None
        self.thread: threading.Thread | None = None
        self.round_robin_index = 0
        self.started_at: str | None = None
        self.last_error: str | None = None
        self.request_count = 0
        self.account_last_used: dict[str, float] = {}
        self.account_cooldowns: dict[tuple[str, str], dict[str, Any]] = {}
        self.response_bindings: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.alpha_search_bindings: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.last_account: dict | None = None
        self.last_quota: dict | None = None
        self.subagent_keys_cache: set[str] = set()
        self.subagent_keys_cache_at = 0.0
        self.usage_stats = UsageStatsStore()
        self.last_usage_error: str | None = None
        self.active_request_count = 0
        self.rejected_request_count = 0
        self.client_cancelled_count = 0

    def _account_cooldown_remaining(
        self,
        account_id: str,
        model: str,
        *,
        now: float | None = None,
    ) -> float:
        checked_at = time.monotonic() if now is None else now
        key = (str(account_id), str(model))
        with self.lock:
            record = self.account_cooldowns.get(key)
            remaining = float((record or {}).get("until", 0.0)) - checked_at
            if remaining <= 0:
                self.account_cooldowns.pop(key, None)
                return 0.0
            return remaining

    def _cooldown_account(
        self,
        account: dict,
        model: str,
        headers: Any,
        *,
        reason: str = "upstream_http_429",
        recoverable: bool = False,
    ) -> float:
        advertised = _retry_after_seconds(headers)
        delay = (
            UPSTREAM_COOLDOWN_DEFAULT_SECONDS
            if advertised is None
            else advertised
        )
        delay = min(
            UPSTREAM_COOLDOWN_MAX_SECONDS,
            max(UPSTREAM_COOLDOWN_MIN_SECONDS, delay),
        )
        now = time.monotonic()
        key = (str(account.get("id") or ""), str(model))
        with self.lock:
            previous = self.account_cooldowns.get(key)
            until = max(float((previous or {}).get("until", 0.0)), now + delay)
            self.account_cooldowns[key] = {
                "until": until,
                "delaySeconds": delay,
                "reason": str(reason or "upstream_http_429"),
                "recoverable": bool(recoverable),
                "source": "local" if recoverable else "remote",
                "advertisedRetryAfter": advertised is not None,
            }
        return max(0.0, until - now)

    def clear_account_runtime_state(self, account_ids: Any) -> dict[str, int]:
        """Clear only runtime state owned by the explicitly named accounts."""

        if isinstance(account_ids, str):
            values = [account_ids]
        else:
            try:
                values = list(account_ids)
            except TypeError:
                values = []
        selected = {
            str(value).strip()
            for value in values[:4096]
            if str(value or "").strip()
        }
        if not selected:
            return {"cooldowns": 0, "lastUsed": 0, "responseBindings": 0, "alphaBindings": 0}
        removed = {"cooldowns": 0, "lastUsed": 0, "responseBindings": 0, "alphaBindings": 0}
        with self.lock:
            for key in list(self.account_cooldowns):
                if key[0] in selected:
                    self.account_cooldowns.pop(key, None)
                    removed["cooldowns"] += 1
            for account_id in selected:
                if account_id in self.account_last_used:
                    self.account_last_used.pop(account_id, None)
                    removed["lastUsed"] += 1
            for key, record in list(self.response_bindings.items()):
                if record.get("identityKind") == "account" and str(record.get("identityId") or "") in selected:
                    self.response_bindings.pop(key, None)
                    removed["responseBindings"] += 1
            for key, record in list(self.alpha_search_bindings.items()):
                if str(record.get("identityId") or "") in selected:
                    self.alpha_search_bindings.pop(key, None)
                    removed["alphaBindings"] += 1
            if str((self.last_account or {}).get("id") or "") in selected:
                self.last_account = None
                self.last_quota = None
        return removed

    def _recover_local_account_cooldowns(self, account_ids: set[str], model: str) -> int:
        """Drop explicitly recoverable local records, never remote retry gates."""

        recovered = 0
        with self.lock:
            for key, record in list(self.account_cooldowns.items()):
                if key[0] not in account_ids or key[1] != str(model):
                    continue
                if record.get("source") != "local" or record.get("recoverable") is not True:
                    continue
                if record.get("advertisedRetryAfter") is True:
                    continue
                self.account_cooldowns.pop(key, None)
                recovered += 1
        return recovered

    def _cooldown_summary(self) -> tuple[int, float | None]:
        now = time.monotonic()
        with self.lock:
            expired = [
                key
                for key, record in self.account_cooldowns.items()
                if float(record.get("until", 0.0)) <= now
            ]
            for key in expired:
                self.account_cooldowns.pop(key, None)
            remaining = [
                max(0.0, float(record.get("until", 0.0)) - now)
                for record in self.account_cooldowns.values()
            ]
        return len(remaining), min(remaining) if remaining else None

    @staticmethod
    def _response_binding_key(response_id: Any) -> str:
        if not isinstance(response_id, str):
            return ""
        normalized = response_id.strip()
        encoded = normalized.encode("utf-8", errors="ignore")
        if not encoded or len(encoded) > RESPONSE_BINDING_MAX_ID_BYTES:
            return ""
        return hashlib.sha256(b"web2api-response-binding-v1\0" + encoded).hexdigest()

    def _prune_response_bindings_locked(self, now: float) -> None:
        expired = [
            key
            for key, record in self.response_bindings.items()
            if float(record.get("expiresAt", 0.0)) <= now
        ]
        for key in expired:
            self.response_bindings.pop(key, None)
        while len(self.response_bindings) > RESPONSE_BINDING_MAX_ENTRIES:
            self.response_bindings.popitem(last=False)

    def _remember_response_binding(
        self,
        response_id: Any,
        *,
        access_scope: str,
        requested_model: str,
        routed_model: str,
        identity_kind: str,
        identity_id: str,
        identity_fingerprint: str = "",
    ) -> bool:
        key = self._response_binding_key(response_id)
        if (
            not key
            or access_scope not in {"public", "internal"}
            or identity_kind not in {"account", "provider"}
            or not str(identity_id or "").strip()
        ):
            return False
        now = time.monotonic()
        record = {
            "accessScope": access_scope,
            "requestedModel": str(requested_model or ""),
            "routedModel": str(routed_model or ""),
            "identityKind": identity_kind,
            "identityId": str(identity_id),
            "identityFingerprint": identity_fingerprint,
            "expiresAt": now + RESPONSE_BINDING_TTL_SECONDS,
            "lastUsedAt": now,
        }
        with self.lock:
            self._prune_response_bindings_locked(now)
            previous = self.response_bindings.get(key)
            if previous and (previous.get("ambiguous") or any(previous.get(field) != record.get(field) for field in (
                "accessScope", "requestedModel", "routedModel", "identityKind", "identityId", "identityFingerprint"
            ))):
                record["ambiguous"] = True
            self.response_bindings.pop(key, None)
            self.response_bindings[key] = record
            self._prune_response_bindings_locked(now)
        return True

    def _lookup_response_binding(self, response_id: Any) -> dict | None:
        key = self._response_binding_key(response_id)
        if not key:
            return None
        now = time.monotonic()
        with self.lock:
            self._prune_response_bindings_locked(now)
            record = self.response_bindings.pop(key, None)
            if record is None:
                return None
            record["lastUsedAt"] = now
            self.response_bindings[key] = record
            return dict(record)

    def _forget_response_binding(self, response_id: Any) -> None:
        key = self._response_binding_key(response_id)
        if not key:
            return
        with self.lock:
            self.response_bindings.pop(key, None)

    @staticmethod
    def _alpha_search_binding_key(access_scope: str, session_id: Any) -> str:
        if access_scope not in {"public", "internal"} or not isinstance(session_id, str):
            return ""
        normalized = session_id.strip()
        encoded = normalized.encode("utf-8", errors="ignore")
        if not encoded or len(encoded) > RESPONSE_BINDING_MAX_ID_BYTES:
            return ""
        return hashlib.sha256(
            b"web2api-alpha-search-binding-v1\0"
            + access_scope.encode("ascii")
            + b"\0"
            + encoded
        ).hexdigest()

    def _lookup_alpha_search_binding(self, access_scope: str, session_id: Any) -> dict | None:
        key = self._alpha_search_binding_key(access_scope, session_id)
        if not key:
            return None
        now = time.monotonic()
        with self.lock:
            expired = [
                item_key
                for item_key, record in self.alpha_search_bindings.items()
                if float(record.get("expiresAt", 0.0)) <= now
            ]
            for item_key in expired:
                self.alpha_search_bindings.pop(item_key, None)
            record = self.alpha_search_bindings.pop(key, None)
            if record is None:
                return None
            record["lastUsedAt"] = now
            self.alpha_search_bindings[key] = record
            return dict(record)

    def _remember_alpha_search_binding(
        self,
        access_scope: str,
        session_id: Any,
        *,
        requested_model: str,
        routed_model: str,
        identity_id: str,
    ) -> None:
        key = self._alpha_search_binding_key(access_scope, session_id)
        if not key or not str(identity_id or "").strip():
            return
        now = time.monotonic()
        record = {
            "accessScope": access_scope,
            "requestedModel": str(requested_model or ""),
            "routedModel": str(routed_model or ""),
            "identityId": str(identity_id),
            "expiresAt": now + RESPONSE_BINDING_TTL_SECONDS,
            "lastUsedAt": now,
        }
        with self.lock:
            self.alpha_search_bindings.pop(key, None)
            self.alpha_search_bindings[key] = record
            while len(self.alpha_search_bindings) > RESPONSE_BINDING_MAX_ENTRIES:
                self.alpha_search_bindings.popitem(last=False)

    def _forget_alpha_search_binding(self, access_scope: str, session_id: Any) -> None:
        key = self._alpha_search_binding_key(access_scope, session_id)
        if not key:
            return
        with self.lock:
            self.alpha_search_bindings.pop(key, None)

    def _resolve_response_binding(
        self,
        payload: dict,
        *,
        access_scope: str,
        requested_model: str,
        route: dict | None,
    ) -> dict | None:
        conversation = payload.get("conversation")
        if (
            (isinstance(conversation, dict) and str(conversation.get("id") or "").strip())
            or (not isinstance(conversation, dict) and str(conversation or "").strip())
        ):
            raise GatewayError(
                "conversation 续轮需要独立的可信归属协议，当前网关不会猜测跨账号绑定。",
                409,
            )
        previous_response_id = str(payload.get("previous_response_id") or "").strip()
        if not previous_response_id:
            return None
        binding = self._lookup_response_binding(previous_response_id)
        if binding is None:
            return None
        if (
            binding.get("ambiguous")
            or binding.get("accessScope") != access_scope
            or binding.get("requestedModel") != requested_model
        ):
            raise GatewayError(
                "previous_response_id 的访问范围或模型与原响应不一致，已拒绝跨边界续轮。",
                409,
            )
        identity_kind = str(binding.get("identityKind") or "")
        identity_id = str(binding.get("identityId") or "")
        route_kind = str((route or {}).get("sourceKind") or "")
        route_id = str((route or {}).get("sourceRecordId") or "")
        if identity_kind == "provider":
            if route_kind != "provider" or route_id != identity_id:
                self._forget_response_binding(previous_response_id)
                raise GatewayError(
                    "原响应绑定的 Provider 已移除或不再允许当前访问范围使用。",
                    409,
                )
        elif route_kind == "provider" or (route_kind == "account" and route_id != identity_id):
            raise GatewayError(
                "previous_response_id 的账号归属与当前模型路由不一致。",
                409,
            )
        routed_model = str((route or {}).get("id") or requested_model)
        if binding.get("routedModel") != routed_model:
            raise GatewayError(
                "previous_response_id 的实际路由模型已变化，无法安全续轮。",
                409,
            )
        return binding

    def _bound_identity_error(
        self,
        binding: dict | None,
        previous_response_id: Any,
        error: Exception,
    ) -> Exception:
        if (
            not binding
            or not isinstance(error, GatewayError)
            or error.status not in {401, 403, 503}
            or getattr(error, "transient_capacity", False) is True
        ):
            return error
        self._forget_response_binding(previous_response_id)
        identity_label = (
            "Provider"
            if binding.get("identityKind") == "provider"
            else "账号"
        )
        return GatewayError(
            f"原响应绑定的{identity_label}已移除、认证失效或不再允许当前访问范围使用。",
            409,
        )

    def _configured_subagent_model_keys(self) -> set[str]:
        now = time.monotonic()
        with self.lock:
            if now - self.subagent_keys_cache_at < 5.0:
                return set(self.subagent_keys_cache)
        settings = core.load_settings()
        configured_keys = {
            str(model_key)
            for route_setting in settings.get("subagentRouting", {}).get("routes", {}).values()
            if isinstance(route_setting, dict)
            for model_key in route_setting.get("models", [])
        }
        with self.lock:
            self.subagent_keys_cache = configured_keys
            self.subagent_keys_cache_at = now
        return set(configured_keys)

    def _request_classification(self, payload: dict, route: dict | None) -> tuple[str, bool]:
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        claimed = _bounded_text(
            metadata.get("agent_manager_source") or metadata.get("agentManagerSource"),
            80,
        ).casefold()
        if (route or {}).get("subagentAlias"):
            return "explicit_subagent", True
        route_key = _bounded_text((route or {}).get("key"), 240)
        configured = False
        if route_key:
            try:
                configured = route_key in self._configured_subagent_model_keys()
            except Exception:
                pass
        if claimed in {"subagent", "agent", "child_agent", "child-agent"}:
            return "explicit_subagent", configured
        if claimed in {"main", "main_agent", "main-agent", "primary", "primary_agent", "primary-agent"}:
            return "explicit_main_agent", configured
        if configured:
            return "configured_subagent_model", True
        return "unclassified", False

    def _usage_route_context(
        self,
        payload: dict,
        requested_model: str,
        route: dict | None = None,
        account: dict | None = None,
        provider_id: str = "",
    ) -> dict:
        account_id = _bounded_text((account or {}).get("id"), 200)
        resolved_provider_id = _bounded_text(provider_id, 200)
        source_kind = _bounded_text((route or {}).get("sourceKind"), 80)
        source_record_id = _bounded_text((route or {}).get("sourceRecordId"), 200)
        if account_id:
            source = "account_route" if route else "account_pool"
            source_kind = "account"
            source_record_id = account_id
        elif resolved_provider_id or source_kind == "provider":
            source = "provider_route"
            source_kind = "provider"
            resolved_provider_id = resolved_provider_id or source_record_id
            source_record_id = resolved_provider_id
        elif route:
            source = "model_route"
        else:
            source = "unresolved_route"
            source_kind = source_kind or "unknown"
        classification, configured = self._request_classification(payload, route)
        return {
            "source": source,
            "sourceKind": source_kind,
            "sourceRecordId": source_record_id,
            "accountId": account_id,
            "providerId": resolved_provider_id,
            "requestedModel": requested_model,
            "routedModel": _bounded_text((route or {}).get("id"), 200, requested_model or "unknown"),
            "routeKey": _bounded_text(
                (route or {}).get("key") or (route or {}).get("slug"),
                240,
                f"{source}:{requested_model or 'unknown'}",
            ),
            "requestClassification": classification,
            "agentRole": _agent_role_from_classification(classification),
            "configuredForSubagents": configured,
        }

    def _record_usage(self, context: dict, usage: dict[str, int] | None, failed: bool = False) -> None:
        try:
            self.usage_stats.record(context, usage, failed)
            self.last_usage_error = None
        except Exception as exc:
            # Observability must never break the inference request it observes.
            self.last_usage_error = f"用量统计暂不可用（{type(exc).__name__}）"

    def _tracked_chunks(
        self,
        chunks: Any,
        context: dict,
        capture: _SSEUsageCapture | None = None,
        observe_output: bool = True,
    ) -> Any:
        observer = capture or _SSEUsageCapture()
        completed = False
        failed = False
        cancelled = False
        try:
            for chunk in chunks:
                if observe_output:
                    observer.feed(chunk)
                yield chunk
            completed = True
        except GeneratorExit:
            cancelled = True
            raise
        except BaseException:
            failed = True
            raise
        finally:
            close = getattr(chunks, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    failed = True
            observer.finish()
            self._record_usage(
                context,
                observer.usage,
                failed=(
                    observer.failed
                    or (not cancelled and (failed or not completed or not observer.done))
                ),
            )

    def _cooldown_after_rate_limited_stream(
        self,
        chunks: Any,
        capture: _SSEUsageCapture,
        account: dict,
        model: str,
        headers: dict[str, str],
    ) -> Any:
        try:
            yield from chunks
        finally:
            quota_exhausted = capture.quota_exhausted
            should_cool = capture.rate_limited or (
                quota_exhausted
                and _codex_credits_usable(headers, account, capture) is not True
            )
            if should_cool and not capture.capacity_limited:
                cooldown_headers = dict(headers)
                if (
                    not _header_value(cooldown_headers, "retry-after")
                    and capture.retry_after_seconds is not None
                ):
                    cooldown_headers["retry-after"] = str(capture.retry_after_seconds)
                self._cooldown_account(
                    account,
                    model,
                    cooldown_headers,
                    reason=(
                        "upstream_quota_exhausted"
                        if quota_exhausted
                        else "upstream_rate_limit"
                    ),
                )

    def _bind_successful_response_stream(
        self,
        chunks: Any,
        capture: _SSEUsageCapture,
        *,
        access_scope: str,
        requested_model: str,
        routed_model: str,
        identity_kind: str,
        identity_id: str,
        identity_fingerprint: str = "",
    ) -> Any:
        completed = False
        try:
            yield from chunks
            completed = True
        finally:
            if (
                completed
                and capture.response_terminal
                and not capture.failed
                and capture.response_id
            ):
                self._remember_response_binding(
                    capture.response_id,
                    access_scope=access_scope,
                    requested_model=requested_model,
                    routed_model=routed_model,
                    identity_kind=identity_kind,
                    identity_id=identity_id,
                    identity_fingerprint=identity_fingerprint,
                )

    def _response_binding_delivery_callback(
        self,
        capture: _SSEUsageCapture,
        *,
        access_scope: str,
        requested_model: str,
        routed_model: str,
        identity_kind: str,
        identity_id: str,
        identity_fingerprint: str = "",
    ) -> Any:
        if (
            not capture.response_terminal
            or capture.failed
            or not capture.response_id
        ):
            return None

        def delivered() -> None:
            self._remember_response_binding(
                capture.response_id,
                access_scope=access_scope,
                requested_model=requested_model,
                routed_model=routed_model,
                identity_kind=identity_kind,
                identity_id=identity_id,
                identity_fingerprint=identity_fingerprint,
            )

        return delivered

    def usage_snapshot(self) -> dict:
        snapshot = self.usage_stats.snapshot()
        snapshot["lastError"] = self.last_usage_error
        try:
            snapshot["codexSessions"] = codex_session_usage_snapshot()
            snapshot["codexSessionsError"] = None
        except Exception as exc:
            # Session telemetry is optional observability and must never affect
            # the gateway or account switching paths.
            snapshot["codexSessions"] = None
            snapshot["codexSessionsError"] = f"Codex 会话用量暂不可用（{type(exc).__name__}）"
        snapshot["accountAttribution"] = account_attribution_snapshot(
            snapshot,
            snapshot.get("codexSessions"),
        )
        return snapshot

    def reset_usage_stats(self) -> dict:
        snapshot = self.usage_stats.reset()
        self.last_usage_error = None
        snapshot["lastError"] = None
        return snapshot

    def start(self) -> dict:
        with self.lock:
            if self.server:
                return self.status()
            settings = core.load_settings()
            config = settings.get("web2api", core._default_web2api_settings())
            if str(config.get("bindHost")) != "127.0.0.1":
                raise core.ManagerError("Web2API 只允许监听 127.0.0.1。")
            if not core.service_secret_configured("web2api"):
                raise core.ManagerError("请先生成 Web2API 客户端密钥。")
            try:
                port = int(config.get("port", 17860))
                server = GatewayServer(("127.0.0.1", port), self)
            except OSError as exc:
                message, status_message = _gateway_bind_error("127.0.0.1", port, exc)
                self.last_error = status_message
                raise core.ManagerError(message) from exc
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.25},
                daemon=True,
            )
            started_at = core.now_iso()
            thread_started = False
            try:
                self.server = server
                self.thread = thread
                self.started_at = started_at
                self.last_error = None
                thread.start()
                thread_started = True
                settings = core.load_settings()
                settings["web2api"]["enabled"] = True
                settings["web2api"]["lastStartedAt"] = started_at
                settings["web2api"]["lastError"] = None
                core.save_settings(settings)
            except Exception as exc:
                self.server = None
                self.thread = None
                self.started_at = None
                self.last_error = f"本地 API 启动未完成（{type(exc).__name__}）"
                if thread_started:
                    try:
                        server.shutdown()
                    except Exception:
                        pass
                try:
                    server.server_close()
                finally:
                    if thread_started:
                        thread.join(timeout=3)
                raise
            return self.status()

    def stop(self, disable: bool = True) -> dict:
        with self.lock:
            server = self.server
            thread = self.thread
            self.server = None
            self.thread = None
        if server:
            server.shutdown()
            server.server_close()
        if thread:
            thread.join(timeout=3)
        self.usage_stats.flush(force=True)
        settings = core.load_settings()
        if disable:
            settings["web2api"]["enabled"] = False
        settings["web2api"]["requestCount"] = self.request_count
        core.save_settings(settings)
        return self.status()

    def status(self, access_scope: str = "internal") -> dict:
        if access_scope not in {"public", "internal"}:
            raise GatewayError("网关访问范围无效。", 403)
        settings = core.load_settings()
        config = settings.get("web2api", core._default_web2api_settings())
        public_account_ids = {
            str(item) for item in config.get("accountIds", []) if str(item).strip()
        }
        active_account_id = str(config.get("activeAccountId") or "").strip()
        active_account = next(
            (item for item in settings.get("accounts", []) if str(item.get("id")) == active_account_id),
            None,
        )
        reported_account = self.last_account
        reported_quota = self.last_quota
        if active_account_id and str((reported_account or {}).get("id") or "") != active_account_id:
            # Never display stale quota/identity data from a previous pool route
            # while Codex is locked to a freshly selected Web Session.
            reported_account = {
                "id": active_account_id,
                "label": str((active_account or {}).get("label") or (active_account or {}).get("email") or "Web Session"),
                "planLabel": str((active_account or {}).get("planLabel") or (active_account or {}).get("plan") or ""),
            }
            reported_quota = None
        visible_active_account_id = active_account_id or None
        if access_scope == "public":
            if str((reported_account or {}).get("id") or "") not in public_account_ids:
                reported_account = None
                reported_quota = None
            if active_account_id not in public_account_ids:
                visible_active_account_id = None
        running = self.server is not None
        port = int(self.server.server_address[1]) if self.server else int(config.get("port", 17860))
        cooldown_count, next_cooldown = self._cooldown_summary()
        return {
            "running": running,
            "activeForCodex": bool(config.get("activeForCodex")),
            "activeAccountId": visible_active_account_id,
            "activeAccount": reported_account if visible_active_account_id else None,
            "url": f"http://127.0.0.1:{port}",
            "port": port,
            "routing": config.get("routing", "ordered"),
            "memberCount": len(config.get("accountIds", [])) + len(config.get("providerIds", [])),
            "keyConfigured": core.service_secret_configured("web2api"),
            "startedAt": self.started_at,
            "lastError": (
                self.last_error
                if access_scope == "internal" or self.last_error is None
                else "服务最近一次请求失败。"
            ),
            "requestCount": self.request_count,
            "activeRequestCount": self.active_request_count,
            "rejectedRequestCount": self.rejected_request_count,
            "clientCancelledCount": self.client_cancelled_count,
            "cooldownCount": cooldown_count,
            "nextCooldownRetrySeconds": (
                max(1, int(math.ceil(next_cooldown)))
                if next_cooldown is not None
                else None
            ),
            "maxConcurrentRequests": MAX_UPSTREAM_CONCURRENCY,
            "lastAccount": reported_account,
            "lastQuota": reported_quota,
            "protocolCapabilities": protocol_capabilities(),
        }

    def models(self, access_scope: str = "public") -> dict:
        if access_scope not in {"public", "internal"}:
            raise GatewayError("网关访问范围无效。", 403)
        settings = core.load_settings()
        config = settings.get("web2api", {})
        if access_scope == "public":
            # ``activeForCodex`` may pin an internal Web Session that is not a
            # public pool member. Build the public catalog from configured
            # members without mutating the live settings object.
            public_settings = dict(settings)
            public_config = dict(config)
            public_config["activeForCodex"] = False
            public_config["activeAccountId"] = None
            public_settings["web2api"] = public_config
            records = core.web2api_pool_model_records(public_settings)
            return {
                "object": "list",
                "data": [
                    {
                        "id": item.get("slug") or item.get("id"),
                        "object": "model",
                        "owned_by": item.get("sourceName") or "agent-manager-pool",
                    }
                    for item in records
                    if item.get("slug") or item.get("id")
                ],
            }
        routed = core.gateway_model_records(settings)
        if config.get("accountIds") or config.get("providerIds") or routed:
            records = [
                *core.web2api_pool_model_records(settings),
                *([] if config.get("accountIds") or config.get("providerIds") else routed),
                *core.managed_subagent_model_records(settings),
            ]
            seen = set()
            data = []
            for item in records:
                model_id = item.get("slug") or item.get("id")
                if not model_id or model_id in seen:
                    continue
                seen.add(model_id)
                data.append(
                    {
                        "id": model_id,
                        "object": "model",
                        "owned_by": (
                            "agent-manager-subagent"
                            if item.get("subagentAlias")
                            else item.get("sourceName") or "agent-manager-pool"
                        ),
                        **(
                            {"x_upstream_model": item["id"]}
                            if item.get("subagentAlias") or item.get("slug") != item.get("id")
                            else {}
                        ),
                    }
                )
            return {
                "object": "list",
                "data": data,
            }
        selected = set(settings.get("web2api", {}).get("accountIds", []))
        names = sorted(
            {
                str(model)
                for account in settings.get("accounts", [])
                if account.get("id") in selected
                for model in account.get("models", [])
                if str(model).strip()
            }
        )
        return {"object": "list", "data": [{"id": name, "object": "model", "owned_by": "openai-chatgpt"} for name in names]}

    def _accounts(
        self,
        allowed_ids: set[str] | None = None,
        requested_model: str = "",
        *,
        access_scope: str = "public",
        session_affine: bool = False,
        include_cooling: bool = False,
    ) -> list[dict]:
        if access_scope not in {"public", "internal"}:
            raise GatewayError("网关访问范围无效。", 403)
        settings = core.load_settings()
        config = settings.get("web2api", {})
        configured_order = [str(item) for item in config.get("accountIds", []) if str(item).strip()]
        configured_ids = set(configured_order)
        active_account_id = str(config.get("activeAccountId") or "").strip()
        if access_scope == "public":
            selected = configured_ids if allowed_ids is None else configured_ids.intersection(allowed_ids)
        elif allowed_ids is not None:
            selected = {str(item) for item in allowed_ids}
        elif config.get("activeForCodex") and active_account_id:
            # A home-page Web Session switch must never silently spill over to
            # another pool identity after an authentication or quota failure.
            selected = {active_account_id}
            configured_order = [active_account_id]
        else:
            selected = set(configured_order)
        account_map = {
            str(item.get("id")): item
            for item in settings.get("accounts", [])
            if item.get("id")
        }
        ordered_ids = [item for item in configured_order if item in selected]
        ordered_ids.extend(
            account_id for account_id in account_map
            if account_id in selected and account_id not in ordered_ids
        )
        accounts = []
        for account_id in ordered_ids:
            account = account_map.get(account_id)
            if not account or account.get("authMode") != "chatgpt":
                continue
            if not core._account_codex_compatible(account):
                continue
            if requested_model and requested_model not in {str(item) for item in account.get("models", [])}:
                continue
            accounts.append(account)
        if not accounts:
            raise GatewayError("Web2API 账号池没有可用账号；请启用账号并检查 Token 有效期。", 503)
        if session_affine and len(accounts) > 1:
            raise GatewayError(
                "previous_response_id/conversation 需要固定到原账号；当前多账号池没有可验证的响应归属，请改用单账号池或账号专属路由。",
                409,
            )
        if include_cooling:
            return accounts
        checked_at = time.monotonic()
        healthy = []
        cooling: list[tuple[dict, float]] = []
        for account in accounts:
            key = (str(account.get("id") or ""), str(requested_model))
            with self.lock:
                cooldown = self.account_cooldowns.get(key)
                if (
                    cooldown
                    and "quota" in str(cooldown.get("reason") or "").casefold()
                    and _credits_usable_from_account(account) is True
                ):
                    self.account_cooldowns.pop(key, None)
            remaining = self._account_cooldown_remaining(
                str(account.get("id") or ""),
                requested_model,
                now=checked_at,
            )
            if remaining > 0:
                cooling.append((account, remaining))
            else:
                healthy.append(account)
        if not healthy:
            recovered = self._recover_local_account_cooldowns(
                {str(account.get("id") or "") for account in accounts},
                requested_model,
            )
            if recovered:
                healthy = [
                    account
                    for account in accounts
                    if self._account_cooldown_remaining(
                        str(account.get("id") or ""),
                        requested_model,
                        now=checked_at,
                    )
                    <= 0
                ]
        if not healthy:
            account, retry_after = min(cooling, key=lambda item: item[1])
            error = GatewayError(
                "可用账号均在上游限流冷却中，请按 Retry-After 稍后重试。",
                429,
                headers={"Retry-After": str(max(1, int(math.ceil(retry_after))))},
            )
            error.route_account = account
            raise error
        accounts = healthy
        if config.get("routing") == "round_robin":
            with self.lock:
                start = self.round_robin_index % len(accounts)
                self.round_robin_index += 1
            return accounts[start:] + accounts[:start]
        if config.get("routing") == "ordered":
            return accounts
        return sorted(
            accounts,
            key=lambda item: (-_quota_score(item), self.account_last_used.get(str(item.get("id")), 0.0)),
        )

    def _remember_quota(self, account: dict, headers: dict[str, str]) -> None:
        self.last_account = {
            "id": str(account.get("id") or ""),
            "label": str(account.get("label") or account.get("email") or "ChatGPT 账号"),
            "planLabel": str(account.get("planLabel") or account.get("plan") or ""),
        }
        try:
            used_percent = float(headers.get("x-codex-primary-used-percent", ""))
        except (TypeError, ValueError):
            used_percent = None
        try:
            window_minutes = int(headers.get("x-codex-primary-window-minutes", ""))
        except (TypeError, ValueError):
            window_minutes = None
        try:
            reset_at = int(headers.get("x-codex-primary-reset-at", ""))
        except (TypeError, ValueError):
            reset_at = None
        self.last_quota = {
            "usedPercent": used_percent,
            "remainingPercent": 100.0 - used_percent if used_percent is not None else None,
            "windowMinutes": window_minutes,
            "resetAt": datetime.fromtimestamp(reset_at, timezone.utc).isoformat() if reset_at else None,
            "updatedAt": core.now_iso(),
        }

    @staticmethod
    def _oauth_identity_headers() -> dict[str, str]:
        version = _safe_header_text(core._codex_client_version(), 128)
        if not version:
            raise core.ManagerError("Codex 客户端版本无效。")
        return {
            "User-Agent": f"codex_cli_rs/{version}",
            "Version": version,
            "Originator": "codex_cli_rs",
        }

    def _open_upstream(
        self,
        payload: dict,
        allowed_ids: set[str] | None = None,
        *,
        access_scope: str = "public",
        responses_lite: bool = False,
        expected_identity: str = "",
    ) -> tuple[Any, dict, dict[str, str]]:
        retry_error: GatewayError | None = None
        last_attempt_account: dict | None = None
        requested_model = str(payload.get("model") or "")
        session_affine = _response_session_affine(payload)
        opaque_history = _has_opaque_history(payload)
        request_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        for account in self._accounts(
            allowed_ids,
            requested_model,
            access_scope=access_scope,
            session_affine=session_affine,
        ):
            last_attempt_account = account
            refresh_after_401 = False
            rejected_access_token = ""
            compatibility_retried = False
            replay_identity = ""
            while True:
                access_token = ""
                try:
                    account_id = str(account["id"])
                    if refresh_after_401:
                        credentials = core._account_chatgpt_credentials(
                            account_id,
                            force_refresh=True,
                            rejected_access_token=rejected_access_token,
                        )
                    else:
                        credentials = core._account_chatgpt_credentials(account_id)
                    access_token = str(credentials.get("accessToken") or "").strip()
                    workspace_account_id = str(credentials.get("accountId") or "").strip()
                    if not access_token or not workspace_account_id:
                        raise core.ManagerError("账号凭据不完整，无法访问 Codex Responses。")
                    fingerprint = _oauth_binding_fingerprint(credentials)
                    if expected_identity and expected_identity != fingerprint:
                        raise GatewayError("原响应的账号身份已改变，已拒绝向新身份续轮。", 409)
                    if replay_identity and replay_identity != fingerprint:
                        raise GatewayError("兼容重试期间账号身份已改变，请重新加载原会话后重试。", 409)
                    request = Request(
                        UPSTREAM_RESPONSES_URL,
                        data=request_body,
                        method="POST",
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "ChatGPT-Account-Id": workspace_account_id,
                            "Content-Type": "application/json",
                            "Accept": "text/event-stream",
                            **self._oauth_identity_headers(),
                            **({CODEX_RESPONSES_LITE_HEADER: "true"} if responses_lite else {}),
                        },
                    )
                    response = core._open_same_origin_request(
                        request,
                        timeout=UPSTREAM_OPEN_TIMEOUT_SECONDS,
                    )
                    response._gateway_identity_fingerprint = fingerprint
                    _set_response_idle_timeout(response)
                    self.account_last_used[account_id] = time.monotonic()
                    self.last_error = None
                    headers = _codex_quota_headers(response.headers, account)
                    if compatibility_retried:
                        headers["X-Codex-History-Compatibility"] = "plaintext-replay"
                    self._remember_quota(account, headers)
                    return response, account, headers
                except HTTPError as exc:
                    raw_headers = exc.headers
                    try:
                        error_body = _read_limited(exc)
                        content_type = raw_headers.get_content_type() if raw_headers else "application/json"
                        headers = _codex_quota_headers(raw_headers, account)
                        self._remember_quota(account, headers)
                    finally:
                        exc.close()
                    if not compatibility_retried:
                        replay = _encrypted_history_replay(payload, exc.code, error_body)
                        if replay is not None:
                            compatibility_retried = True
                            replay_identity = fingerprint
                            request_body = json.dumps(replay, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                            continue
                    if exc.code == 401 and not refresh_after_401:
                        refresh_after_401 = True
                        rejected_access_token = access_token
                        continue
                    capacity_error = None if exc.code in {401, 403} else _capacity_error_from_body(error_body)
                    if capacity_error is not None:
                        raise _capacity_gateway_error(capacity_error, headers, account)
                    retry_error = GatewayError(
                        f"上游返回 HTTP {exc.code}。",
                        exc.code,
                        error_body,
                        content_type,
                        headers,
                    )
                    if exc.code == 429:
                        try:
                            error_payload = json.loads(error_body)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            error_payload = None
                        quota_kind = _quota_error_kind(error_payload)
                        if not (
                            quota_kind == "quota_exhausted"
                            and _codex_credits_usable(headers, account) is True
                        ):
                            self._cooldown_account(
                                account,
                                requested_model,
                                raw_headers if raw_headers is not None else headers,
                                reason=(
                                    "upstream_quota_exhausted"
                                    if quota_kind == "quota_exhausted"
                                    else "upstream_rate_limit"
                                    if quota_kind == "rate_limited"
                                    else "upstream_http_429"
                                ),
                            )
                    # Authentication, policy and quota responses are explicit
                    # pre-inference rejections. Transport failures remain
                    # ambiguous and are never replayed here.
                    if exc.code not in {401, 403, 429}:
                        retry_error.route_account = account
                        raise retry_error
                    if session_affine or opaque_history:
                        retry_error.route_account = account
                        self.last_error = str(retry_error)[:500]
                        raise retry_error
                    break
                except core.ManagerError as exc:
                    detail = core._redact_sensitive_text(exc, limit=240)
                    retry_error = GatewayError(f"账号 {account.get('label')} 不可用：{detail}", 502)
                    if session_affine or opaque_history:
                        retry_error.route_account = account
                        raise retry_error from exc
                    break
                except (URLError, TimeoutError) as exc:
                    error = GatewayError(
                        "上游连接在结果确认前中断，已停止自动换号以避免重复请求；请稍后重试。",
                        502,
                    )
                    self.last_error = str(error)[:500]
                    error.route_account = account
                    raise error from exc
        error = retry_error or GatewayError("没有账号成功完成请求。", 502)
        error.route_account = last_attempt_account
        self.last_error = str(error)[:500]
        raise error

    @staticmethod
    def _probe_capacity_before_output(
        response: Any,
        account: dict,
        headers: dict[str, str],
    ) -> tuple[Any | None, GatewayError | None]:
        capture = _SSEUsageCapture()
        staged = bytearray()
        started_at = time.monotonic()
        try:
            while True:
                chunk = response.read(8192)
                if not chunk:
                    capture.finish()
                    break
                staged.extend(chunk)
                capture.feed(chunk)
                if capture.overflowed:
                    raise GatewayError("上游 SSE 事件超过安全限制。", 502)
                # Any substantive output makes replay unsafe, even when the
                # same read also contains a later capacity failure.
                if capture.semantic_output:
                    return _PrefixedResponse(response, bytes(staged)), None
                if capture.response_terminal:
                    break
                if (
                    len(staged) >= STREAM_PREOUTPUT_MAX_BYTES
                    or time.monotonic() - started_at >= STREAM_PREOUTPUT_MAX_SECONDS
                ):
                    return _PrefixedResponse(response, bytes(staged)), None
        except Exception:
            response.close()
            raise
        if capture.capacity_limited and not capture.semantic_output:
            response.close()
            error = _capacity_gateway_error(
                capture.capacity_error,
                headers,
                account,
                capture.retry_after_seconds,
            )
            return None, error
        return _PrefixedResponse(response, bytes(staged)), None

    def _open_upstream_with_capacity_retry(
        self,
        payload: dict,
        allowed_ids: set[str] | None = None,
        *,
        access_scope: str = "public",
        responses_lite: bool = False,
        expected_identity: str = "",
    ) -> tuple[Any, dict, dict[str, str]]:
        last_capacity_error: GatewayError | None = None
        for attempt in range(UPSTREAM_CAPACITY_MAX_ATTEMPTS):
            try:
                response, account, headers = self._open_upstream(
                    payload,
                    allowed_ids,
                    access_scope=access_scope,
                    responses_lite=responses_lite,
                    **({"expected_identity": expected_identity} if expected_identity else {}),
                )
            except GatewayError as exc:
                if getattr(exc, "transient_capacity", False) is not True:
                    raise
                last_capacity_error = exc
                if attempt + 1 < UPSTREAM_CAPACITY_MAX_ATTEMPTS and not _has_opaque_history(payload):
                    continue
                self.last_error = str(exc)[:500]
                raise
            prepared, capacity_error = self._probe_capacity_before_output(
                response,
                account,
                headers,
            )
            if capacity_error is None and prepared is not None:
                return prepared, account, headers
            last_capacity_error = capacity_error
            if attempt + 1 >= UPSTREAM_CAPACITY_MAX_ATTEMPTS or _has_opaque_history(payload):
                self.last_error = str(capacity_error)[:500]
                raise capacity_error
        error = last_capacity_error or GatewayError("上游模型暂时繁忙，请稍后重试。", 503)
        self.last_error = str(error)[:500]
        raise error

    @staticmethod
    def _provider_url(base_url: str, path: str) -> str:
        base = str(base_url or "").rstrip("/")
        suffix = path if path.startswith("/") else f"/{path}"
        if base.endswith("/v1") and suffix.startswith("/v1/"):
            suffix = suffix[3:]
        elif not base.endswith("/v1") and not suffix.startswith("/v1/"):
            suffix = f"/v1{suffix}"
        return f"{base}{suffix}"

    def _open_provider_upstream(
        self,
        path: str,
        payload: dict,
        route: dict,
        client_headers: dict[str, str] | None = None,
        *,
        expected_identity: str = "",
    ) -> Any:
        provider = core.provider_by_id(str(route["sourceRecordId"]))
        key = core.load_provider_key(provider["id"], required=True)
        base_url = core._provider_runtime_base_url(provider)
        fingerprint = _identity_fingerprint("provider", base_url, key)
        if expected_identity and expected_identity != fingerprint:
            raise GatewayError("原响应的 Provider 地址或凭据已改变，已拒绝跨身份续轮。", 409)
        forwarded = json.loads(json.dumps(payload))
        forwarded["model"] = route["id"]
        body = json.dumps(forwarded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        identity_headers: dict[str, str] = {}
        for name in ("User-Agent", "Originator", "Version"):
            value = _safe_header_text((client_headers or {}).get(name))
            if value:
                identity_headers[name] = value
        identity_headers.setdefault(
            "User-Agent",
            f"Codex-Agent-Manager/{core._codex_client_version()}",
        )
        request = Request(
            self._provider_url(
                base_url,
                path,
            ),
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream, application/json",
                **identity_headers,
            },
        )
        try:
            try:
                response = core._open_same_origin_request(
                    request, timeout=UPSTREAM_OPEN_TIMEOUT_SECONDS,
                )
            except HTTPError as exc:
                if path != "/v1/responses" or exc.code != 400:
                    raise
                try:
                    rejected_body = _read_limited(exc)
                    rejected_type = exc.headers.get_content_type() if exc.headers else "application/json"
                    rejected_headers = _safe_response_headers(exc.headers)
                finally:
                    exc.close()
                replay = _encrypted_history_replay(forwarded, exc.code, rejected_body)
                if replay is None:
                    raise GatewayError(f"{provider['name']} 返回 HTTP {exc.code}。", exc.code,
                                       rejected_body, rejected_type, rejected_headers) from exc
                # Same URL, credential, native model and client identity. Only
                # a rejected request is replayed; SSE/transport failures are not.
                request.data = json.dumps(replay, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                response = core._open_same_origin_request(request, timeout=UPSTREAM_OPEN_TIMEOUT_SECONDS)
                response.headers["X-Codex-History-Compatibility"] = "plaintext-replay"
            response._gateway_identity_fingerprint = fingerprint
            _set_response_idle_timeout(response)
            self.last_error = None
            return response
        except HTTPError as exc:
            try:
                body = _read_limited(exc)
                content_type = exc.headers.get_content_type() if exc.headers else "application/json"
                headers = _safe_response_headers(exc.headers)
            finally:
                exc.close()
            error = GatewayError(
                f"{provider['name']} 返回 HTTP {exc.code}。",
                exc.code,
                body,
                content_type,
                headers,
            )
            self.last_error = str(error)[:500]
            raise error
        except (URLError, TimeoutError, core.ManagerError) as exc:
            if isinstance(exc, core.ManagerError):
                detail = core._redact_sensitive_text(exc, limit=240)
            else:
                detail = "网络连接在结果确认前中断，请稍后重试"
            error = GatewayError(f"{provider['name']} 不可用：{detail}", 502)
            self.last_error = str(error)[:500]
            raise error

    def _alpha_search(
        self,
        payload: dict,
        *,
        access_scope: str,
        client_headers: dict[str, str] | None = None,
    ) -> dict:
        requested_model = str(payload.get("model") or "").strip()
        payload_session_id = str(payload.get("id") or "").strip()
        header_session_ids = {
            value
            for name in ("Session-Id", "Session_id", "X-Session-ID")
            if (value := _safe_header_text((client_headers or {}).get(name)))
        }
        if len(header_session_ids) > 1 or (
            payload_session_id
            and header_session_ids
            and payload_session_id not in header_session_ids
        ):
            raise GatewayError("Alpha Search 会话标识不一致。", 409)
        session_id = payload_session_id or next(iter(header_session_ids), "")
        if session_id and not self._alpha_search_binding_key(access_scope, session_id):
            raise GatewayError("Alpha Search 会话 ID 无效或过长。", 400)
        route = (
            core.resolve_model_route(requested_model, access_scope=access_scope)
            if requested_model
            else None
        )
        if route and route.get("sourceKind") == "provider":
            raise GatewayError("API Key Provider 不支持 /v1/alpha/search。", 404)
        binding = self._lookup_alpha_search_binding(access_scope, session_id) if session_id else None
        routed_model = str((route or {}).get("id") or requested_model)
        allowed_ids: set[str] | None = None
        if binding:
            bound_model = str(binding.get("routedModel") or "")
            if routed_model and bound_model and routed_model != bound_model:
                raise GatewayError("Alpha Search 会话模型与原绑定不一致。", 409)
            route_id = str((route or {}).get("sourceRecordId") or "")
            bound_id = str(binding.get("identityId") or "")
            if route_id and route_id != bound_id:
                raise GatewayError("Alpha Search 会话账号与当前模型路由不一致。", 409)
            routed_model = bound_model or routed_model
            allowed_ids = {bound_id}
        elif route:
            allowed_ids = {str(route.get("sourceRecordId") or "")}
        elif not requested_model:
            # A model-less search request has no safe routing key. It may use a
            # single configured OAuth identity, but must never guess across a pool.
            candidates = self._accounts(
                requested_model="",
                access_scope=access_scope,
                include_cooling=True,
            )
            if len(candidates) != 1:
                raise GatewayError(
                    "Alpha Search 请求缺少 model，当前账号池无法唯一确定账号。",
                    409,
                )
            allowed_ids = {str(candidates[0].get("id") or "")}
        accounts = self._accounts(
            allowed_ids,
            routed_model,
            access_scope=access_scope,
            session_affine=binding is not None,
        )
        account = accounts[0]
        account_id = str(account.get("id") or "")
        if session_id:
            self._remember_alpha_search_binding(
                access_scope,
                session_id,
                requested_model=requested_model,
                routed_model=routed_model,
                identity_id=account_id,
            )
        forwarded_payload = json.loads(json.dumps(payload))
        if requested_model and routed_model:
            forwarded_payload["model"] = routed_model
        request_body = json.dumps(forwarded_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        def open_search(credentials: dict) -> Any:
            access_token = str(credentials.get("accessToken") or "").strip()
            workspace_account_id = str(credentials.get("accountId") or "").strip()
            if not access_token or not workspace_account_id:
                raise core.ManagerError("账号凭据不完整，无法访问 Codex Alpha Search。")
            headers = {
                "Authorization": f"Bearer {access_token}",
                "ChatGPT-Account-Id": workspace_account_id,
                "Content-Type": "application/json",
                "Accept": "application/json",
                **self._oauth_identity_headers(),
            }
            for name in (
                "Session-Id",
                "Session_id",
                "X-Session-ID",
                "X-Client-Request-Id",
                "X-Codex-Window-Id",
                "Thread-Id",
                "X-OpenAI-Actor-Authorization",
            ):
                value = _safe_header_text(
                    (client_headers or {}).get(name),
                    8192 if name == "X-OpenAI-Actor-Authorization" else 512,
                )
                if value:
                    headers[name] = value
            if session_id and not any(
                name in headers for name in ("Session-Id", "Session_id", "X-Session-ID")
            ):
                headers["Session-Id"] = _safe_header_text(session_id)
            request = Request(
                UPSTREAM_ALPHA_SEARCH_URL,
                data=request_body,
                method="POST",
                headers=headers,
            )
            return core._open_same_origin_request(
                request,
                timeout=UPSTREAM_OPEN_TIMEOUT_SECONDS,
            )

        try:
            credentials = core._account_chatgpt_credentials(account_id)
            try:
                response = open_search(credentials)
            except HTTPError as first_error:
                if first_error.code != 401:
                    raise
                try:
                    _read_limited(first_error)
                finally:
                    first_error.close()
                refreshed = core._account_chatgpt_credentials(
                    account_id,
                    force_refresh=True,
                    rejected_access_token=str(credentials.get("accessToken") or "").strip(),
                )
                response = open_search(refreshed)
            with response:
                body = _read_limited(response)
                status = int(getattr(response, "status", 200))
                response_headers = _codex_quota_headers(response.headers, account)
                content_type = str(response.headers.get("Content-Type") or "application/json; charset=utf-8")
        except HTTPError as exc:
            raw_headers = exc.headers
            try:
                body = _read_limited(exc)
                content_type = raw_headers.get_content_type() if raw_headers else "application/json"
                response_headers = _codex_quota_headers(raw_headers, account)
            finally:
                exc.close()
            status = int(exc.code)
            if status == 429:
                try:
                    error_payload = json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    error_payload = None
                quota_kind = _quota_error_kind(error_payload)
                if not (
                    quota_kind == "quota_exhausted"
                    and _codex_credits_usable(response_headers, account) is True
                ):
                    self._cooldown_account(
                        account,
                        routed_model,
                        raw_headers if raw_headers is not None else response_headers,
                        reason=(
                            "upstream_quota_exhausted"
                            if quota_kind == "quota_exhausted"
                            else "upstream_rate_limit"
                        ),
                    )
            if status in {401, 403} and session_id:
                self._forget_alpha_search_binding(access_scope, session_id)
        except core.ManagerError as exc:
            if session_id:
                self._forget_alpha_search_binding(access_scope, session_id)
            detail = core._redact_sensitive_text(exc, limit=240)
            raise GatewayError(f"Alpha Search 账号不可用：{detail}", 502) from exc
        except (URLError, TimeoutError) as exc:
            raise GatewayError("Alpha Search 上游连接中断，未自动换号。", 502) from exc
        self.account_last_used[account_id] = time.monotonic()
        self._remember_quota(account, response_headers)
        self.last_error = None if status < 400 else f"Alpha Search 上游返回 HTTP {status}。"
        self.request_count += 1
        return {
            "status": status,
            "contentType": content_type,
            "body": body,
            "headers": response_headers,
        }

    def _upstream(
        self,
        payload: dict,
        allowed_ids: set[str] | None = None,
        include_headers: bool = False,
        *,
        access_scope: str = "public",
        responses_lite: bool = False,
        expected_identity: str = "",
    ) -> bytes | dict:
        response, account, headers = self._open_upstream_with_capacity_retry(
            payload,
            allowed_ids,
            access_scope=access_scope,
            responses_lite=responses_lite,
            **({"expected_identity": expected_identity} if expected_identity else {}),
        )
        with response:
            body = _read_limited(response)
        return {"body": body, "headers": headers, "account": account,
                "identityFingerprint": str(getattr(response, "_gateway_identity_fingerprint", ""))} if include_headers else body

    @staticmethod
    def _chat_stream_chunks(
        response: Any,
        requested_model: str,
        event_observer: _SSEUsageCapture | None = None,
        *,
        include_usage: bool = False,
    ) -> Any:
        completion_id = f"chatcmpl_{uuid.uuid4().hex}"
        started = False
        saw_tool_call = False
        tool_indices: dict[str, int] = {}
        custom_calls: dict[int, dict] = {}
        function_calls: dict[int, dict] = {}
        emitted_text = ""
        emitted_reasoning = ""
        data_lines: list[str] = []
        event_bytes = 0
        terminal = False

        def next_tool_index(*identities: str) -> int:
            for identity in identities:
                if identity and identity in tool_indices:
                    return tool_indices[identity]
            index = len(set(tool_indices.values()))
            for identity in identities:
                if identity:
                    tool_indices[identity] = index
            return index

        def custom_state(value: dict) -> dict:
            item_id = str(value.get("item_id") or value.get("id") or "")
            call_id = str(value.get("call_id") or "")
            output_identity = (
                f"output:{value['output_index']}"
                if value.get("output_index") is not None
                else ""
            )
            index = next_tool_index(item_id, call_id, output_identity)
            state = custom_calls.setdefault(
                index,
                {
                    "index": index,
                    "item_id": item_id,
                    "call_id": call_id or item_id or f"call_{uuid.uuid4().hex[:16]}",
                    "name": "",
                    "input": "",
                    "emitted": False,
                },
            )
            if item_id:
                state["item_id"] = item_id
                tool_indices[item_id] = index
            if call_id:
                state["call_id"] = call_id
                tool_indices[call_id] = index
            if value.get("name"):
                state["name"] = str(value["name"])
            if "input" in value:
                state["input"] = _custom_tool_input(value.get("input"))
            return state

        def chat_chunk(delta: dict, finish_reason: str | None = None) -> bytes:
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": requested_model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
            return f"data: {json.dumps(chunk, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")

        def emit_custom(state: dict) -> bytes | None:
            nonlocal saw_tool_call
            if state.get("emitted"):
                return None
            state["emitted"] = True
            saw_tool_call = True
            call = _chat_custom_tool_call(
                {
                    "call_id": state.get("call_id"),
                    "name": state.get("name") or "tool",
                    "input": state.get("input") or "",
                },
                index=int(state["index"]),
            )
            return chat_chunk({"tool_calls": [call]})

        def emit_function(item: dict) -> bytes | None:
            nonlocal saw_tool_call
            call_id = str(item.get("call_id") or item.get("id") or "")
            item_id = str(item.get("id") or call_id)
            if not call_id:
                raise GatewayError("上游工具调用缺少 call_id。", 502)
            index = next_tool_index(item_id, call_id)
            state = function_calls.setdefault(index, {"header": False, "arguments": ""})
            function = {}
            call = {"index": index, "function": function}
            if not state["header"]:
                call.update(id=call_id, type="function")
                function["name"] = str(item.get("name") or "tool")
                state["header"] = True
            if "arguments" in item:
                arguments = str(item.get("arguments") or "")
                if not arguments.startswith(state["arguments"]):
                    raise GatewayError("上游工具参数终态与已输出增量不一致。", 502)
                function["arguments"] = arguments[len(state["arguments"]):]
                state["arguments"] = arguments
            saw_tool_call = True
            return chat_chunk({"tool_calls": [call]}) if function.get("name") or function.get("arguments") else None

        def convert(event: dict) -> bytes | None:
            nonlocal completion_id, started, saw_tool_call, terminal, emitted_text, emitted_reasoning
            event_type = event.get("type")
            if event_type == "response.created" and isinstance(event.get("response"), dict):
                completion_id = str(event["response"].get("id") or completion_id)
                return None
            if event_type == "response.output_text.delta":
                delta = str(event.get("delta") or "")
                emitted_text += delta
                converted = chat_chunk(
                    {**({"role": "assistant"} if not started else {}), "content": delta}
                )
                started = True
                return converted
            if event_type == "response.reasoning_summary_text.delta":
                delta = str(event.get("delta") or "")
                emitted_reasoning += delta
                converted = chat_chunk({**({"role": "assistant"} if not started else {}), "reasoning_content": delta})
                started = True
                return converted
            if event_type == "response.output_item.added" and isinstance(event.get("item"), dict):
                item = event["item"]
                if item.get("type") == "custom_tool_call":
                    custom_state({**item, "output_index": event.get("output_index")})
                    return None
                if item.get("type") in {"function_call", "tool_call"}:
                    return emit_function(item)
            if event_type == "response.function_call_arguments.delta":
                item_id = str(event.get("item_id") or event.get("call_id") or "")
                index = next_tool_index(item_id, str(event.get("call_id") or ""))
                state = function_calls.setdefault(index, {"header": False, "arguments": ""})
                state["arguments"] += str(event.get("delta") or "")
                return chat_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "function": {"arguments": str(event.get("delta") or "")},
                            }
                        ]
                    }
                )
            if event_type == "response.custom_tool_call_input.delta":
                state = custom_state(event)
                state["input"] += _custom_tool_input(event.get("delta"))
                return None
            if event_type == "response.custom_tool_call_input.done":
                state = custom_state(event)
                if "input" in event:
                    state["input"] = _custom_tool_input(event.get("input"))
                return emit_custom(state)
            if event_type == "response.output_item.done" and isinstance(event.get("item"), dict):
                item = event["item"]
                if item.get("type") == "custom_tool_call":
                    return emit_custom(custom_state({**item, "output_index": event.get("output_index")}))
                if item.get("type") in {"function_call", "tool_call"}:
                    return emit_function(item)
            if event_type in {"response.completed", "response.incomplete"}:
                terminal = True
                final_response = event.get("response") if isinstance(event.get("response"), dict) else {}
                pending = []
                for final_text, emitted, field in (
                    (_response_text(final_response), emitted_text, "content"),
                    (_response_reasoning_summary(final_response), emitted_reasoning, "reasoning_content"),
                ):
                    if final_text and final_text.startswith(emitted) and len(final_text) > len(emitted):
                        pending.append(chat_chunk({field: final_text[len(emitted):]}))
                final_output = final_response.get("output", []) if isinstance(final_response.get("output"), list) else []
                for output_index, item in enumerate(final_output):
                    if isinstance(item, dict) and item.get("type") == "custom_tool_call":
                        converted = emit_custom(
                            custom_state({**item, "output_index": output_index})
                        )
                        if converted:
                            pending.append(converted)
                    elif isinstance(item, dict) and item.get("type") in {"function_call", "tool_call"}:
                        converted = emit_function(item)
                        if converted:
                            pending.append(converted)
                for state in sorted(custom_calls.values(), key=lambda item: int(item["index"])):
                    converted = emit_custom(state)
                    if converted:
                        pending.append(converted)
                pending.append(chat_chunk({}, _chat_finish_reason(final_response, saw_tool_call)))
                if include_usage and isinstance(final_response.get("usage"), dict):
                    usage_chunk = {"id": completion_id, "object": "chat.completion.chunk",
                                   "created": int(time.time()), "model": requested_model,
                                   "choices": [], "usage": _chat_usage(final_response["usage"])}
                    pending.append(("data: " + json.dumps(usage_chunk, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode("utf-8"))
                return b"".join(pending)
            if event_type in {"error", "response.failed"}:
                terminal = True
                failed_response = event.get("response") if isinstance(event.get("response"), dict) else {}
                error = event.get("error") or failed_response.get("error") or {"message": "上游流式响应失败。"}
                return f"data: {json.dumps({'error': error}, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")
            return None

        with response:
            for raw_line in response:
                if len(raw_line) > MAX_SSE_LINE_BYTES:
                    raise GatewayError("上游 SSE 单行超过安全限制。", 502)
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("data:"):
                    data = line[5:].lstrip()
                    event_bytes += len(data.encode("utf-8"))
                    if event_bytes > MAX_SSE_EVENT_BYTES:
                        raise GatewayError("上游 SSE 事件超过安全限制。", 502)
                    data_lines.append(data)
                    continue
                if line or not data_lines:
                    continue
                raw = "\n".join(data_lines)
                data_lines.clear()
                event_bytes = 0
                if raw == "[DONE]":
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    if event_observer:
                        event_observer.observe_event(event)
                    converted = convert(event)
                    if converted:
                        yield converted
        if data_lines:
            raw = "\n".join(data_lines)
            if raw != "[DONE]":
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    event = None
                if isinstance(event, dict):
                    if event_observer:
                        event_observer.observe_event(event)
                    converted = convert(event)
                    if converted:
                        yield converted
        if not terminal:
            raise GatewayError("上游流式响应提前结束，未收到完成事件。", 502)
        yield b"data: [DONE]\n\n"

    @staticmethod
    def _response_stream_chunks(
        response: Any,
        event_observer: _SSEUsageCapture | None = None,
        *,
        require_response_terminal: bool = True,
    ) -> Any:
        observer = event_observer or _SSEUsageCapture()
        content_type = str(getattr(response, "headers", {}).get("Content-Type", "")).casefold()
        if require_response_terminal and "application/json" in content_type:
            with response:
                value = _completed_response(_read_limited(response))
            terminal = "response.incomplete" if value.get("status") == "incomplete" else "response.completed"
            event = {"type": terminal, "response": value}
            observer.observe_event(event)
            yield ("event: " + terminal + "\ndata: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode("utf-8")
            return
        staged: list[bytes] = []
        staged_bytes = 0
        staged_at = time.monotonic()
        committed = False
        with response:
            while True:
                chunk = getattr(response, "read1", response.read)(8192)
                if not chunk:
                    break
                observer.feed(chunk)
                if observer.overflowed:
                    raise GatewayError("上游 SSE 事件超过安全限制。", 502)
                if committed:
                    yield chunk
                    continue
                staged.append(chunk)
                staged_bytes += len(chunk)
                if (
                    observer.semantic_output
                    or observer.response_terminal
                    or staged_bytes >= STREAM_PREOUTPUT_MAX_BYTES
                    or time.monotonic() - staged_at >= STREAM_PREOUTPUT_MAX_SECONDS
                ):
                    committed = True
                    yield from staged
                    staged.clear()
        observer.finish()
        if observer.overflowed:
            raise GatewayError("上游 SSE 事件超过安全限制。", 502)
        if require_response_terminal and not observer.response_terminal:
            raise GatewayError("上游流式响应提前结束，未收到 Responses 终态事件。", 502)
        if not committed:
            yield from staged

    def stream(
        self,
        path: str,
        payload: dict,
        *,
        access_scope: str = "public",
        client_headers: dict[str, str] | None = None,
    ) -> dict:
        if access_scope not in {"public", "internal"}:
            raise GatewayError("网关访问范围无效。", 403)
        if path not in {"/v1/responses", "/v1/chat/completions"}:
            raise GatewayError("接口不存在。", 404)
        requested_model = str(payload.get("model") or "")
        route = core.resolve_model_route(requested_model, access_scope=access_scope)
        binding = self._resolve_response_binding(
            payload,
            access_scope=access_scope,
            requested_model=requested_model,
            route=route,
        )
        if route and route.get("sourceKind") == "provider":
            context = self._usage_route_context(
                payload,
                requested_model,
                route=route,
                provider_id=str(route.get("sourceRecordId") or ""),
            )
            try:
                response = self._open_provider_upstream(path, payload, route, client_headers,
                    **({"expected_identity": binding["identityFingerprint"]} if binding and binding.get("identityFingerprint") else {}))
            except Exception as exc:
                self._record_usage(context, None, failed=True)
                raise self._bound_identity_error(
                    binding,
                    payload.get("previous_response_id"),
                    exc,
                )
            self.request_count += 1
            content_type = response.headers.get("Content-Type", "text/event-stream; charset=utf-8")
            if path == "/v1/responses" and "application/json" in content_type.casefold():
                content_type = "text/event-stream; charset=utf-8"
            capture = _SSEUsageCapture()
            chunks = self._tracked_chunks(
                self._response_stream_chunks(
                    response,
                    capture,
                    require_response_terminal=path == "/v1/responses",
                ),
                context,
                capture,
                observe_output=False,
            )
            if path == "/v1/responses":
                chunks = self._bind_successful_response_stream(
                    chunks,
                    capture,
                    access_scope=access_scope,
                    requested_model=requested_model,
                    routed_model=str(route.get("id") or requested_model),
                    identity_kind="provider",
                    identity_id=str(route.get("sourceRecordId") or ""),
                    identity_fingerprint=str(getattr(response, "_gateway_identity_fingerprint", "")),
                )
            return {
                "contentType": content_type,
                "chunks": chunks,
                "headers": _safe_response_headers(response.headers),
                "_abort": lambda: _abort_upstream_response(response),
            }
        routed_payload = json.loads(json.dumps(payload))
        allowed_ids = None
        if binding and binding.get("identityKind") == "account":
            routed_payload["model"] = str(binding.get("routedModel") or requested_model)
            allowed_ids = {str(binding.get("identityId") or "")}
        elif route:
            routed_payload["model"] = route["id"]
            allowed_ids = {str(route["sourceRecordId"])}
        responses_lite = path == "/v1/responses" and _responses_lite_enabled(
            str(routed_payload.get("model") or ""),
            route,
            client_headers,
        )
        upstream_payload = (
            _responses_input(routed_payload, responses_lite=responses_lite)
            if path == "/v1/responses"
            else _chat_input(routed_payload)
        )
        try:
            response, account, headers = self._open_upstream_with_capacity_retry(
                upstream_payload,
                allowed_ids,
                access_scope=access_scope,
                responses_lite=responses_lite,
                **({"expected_identity": binding["identityFingerprint"]} if binding and binding.get("identityFingerprint") else {}),
            )
        except Exception as exc:
            exc = self._bound_identity_error(
                binding,
                payload.get("previous_response_id"),
                exc,
            )
            failed_account = getattr(exc, "route_account", None)
            context = self._usage_route_context(
                payload,
                requested_model,
                route=route,
                account=failed_account if isinstance(failed_account, dict) else None,
            )
            self._record_usage(context, None, failed=True)
            raise exc
        context = self._usage_route_context(payload, requested_model, route=route, account=account)
        self.request_count += 1
        routed_model = str(upstream_payload.get("model") or "")
        if path == "/v1/responses":
            capture = _SSEUsageCapture()
            chunks = self._tracked_chunks(
                self._response_stream_chunks(response, capture),
                context,
                capture,
                observe_output=False,
            )
            chunks = self._cooldown_after_rate_limited_stream(
                chunks,
                capture,
                account,
                routed_model,
                headers,
            )
            chunks = self._bind_successful_response_stream(
                chunks,
                capture,
                access_scope=access_scope,
                requested_model=requested_model,
                routed_model=routed_model,
                identity_kind="account",
                identity_id=str(account.get("id") or ""),
                identity_fingerprint=str(getattr(response, "_gateway_identity_fingerprint", "")),
            )
            return {
                "contentType": "text/event-stream; charset=utf-8",
                "chunks": chunks,
                "headers": headers,
                "_abort": lambda: _abort_upstream_response(response),
            }
        capture = _SSEUsageCapture()
        chunks = self._tracked_chunks(
            self._chat_stream_chunks(response, requested_model, capture,
                                     include_usage=isinstance(payload.get("stream_options"), dict) and payload["stream_options"].get("include_usage") is True),
            context,
            capture,
            observe_output=False,
        )
        return {
            "contentType": "text/event-stream; charset=utf-8",
            "chunks": self._cooldown_after_rate_limited_stream(
                chunks,
                capture,
                account,
                routed_model,
                headers,
            ),
            "headers": headers,
        }

    def _responses_auxiliary(self, path: str, payload: dict, *, access_scope: str,
                             client_headers: dict[str, str] | None = None) -> dict:
        """Unary endpoint routing; counting must never masquerade as generation."""
        if payload.get("stream"):
            raise GatewayError("该 Responses 辅助端点仅支持非流式请求。", 400)
        if not isinstance(payload.get("model"), str) or not payload["model"].strip():
            raise GatewayError("请求必须包含 model。", 400)
        model = payload["model"]
        route = core.resolve_model_route(model, access_scope=access_scope)
        binding = self._resolve_response_binding(payload, access_scope=access_scope, requested_model=model, route=route)
        if not route or route.get("sourceKind") != "provider":
            if path == "/v1/responses/input_tokens":
                raise GatewayError("Web Session 未提供可验证的 input_tokens 接口；请使用支持此端点的 API Provider。", 501)
            if not isinstance(payload.get("input"), list):
                raise GatewayError("Web Session 压缩需要完整的 input 列表。", 400)
            allowed = {"model", "input", "instructions", "previous_response_id", "reasoning", "metadata", "service_tier"}
            if set(payload) - allowed - {"stream"}:
                raise GatewayError("Web Session 压缩包含不支持的字段。", 400)
            native = {key: value for key, value in payload.items() if key in allowed}
            native["input"] = [*payload["input"]]
            if not native["input"] or not isinstance(native["input"][-1], dict) or native["input"][-1].get("type") != "compaction_trigger":
                native["input"].append({"type": "compaction_trigger"})
            native["store"] = False
            allowed_ids = None
            if binding and binding.get("identityKind") == "account":
                native["model"] = binding["routedModel"]
                allowed_ids = {binding["identityId"]}
            elif route:
                native["model"] = route["id"]
                allowed_ids = {str(route["sourceRecordId"])}
            context = self._usage_route_context(payload, model, route=route)
            usage = None
            try:
                result = self._upstream(_responses_input(native), include_headers=True,
                    access_scope=access_scope,
                    **({"allowed_ids": allowed_ids} if allowed_ids is not None else {}),
                    **({"expected_identity": binding["identityFingerprint"]} if binding and binding.get("identityFingerprint") else {}))
                if not isinstance(result, dict):
                    result = {"body": result, "headers": {}}
                context = self._usage_route_context(payload, model, route=route, account=result.get("account"))
                usage, _failed = _inspect_usage_body(result["body"])
                response = _completed_response(result["body"])
                output = response.get("output")
                if not isinstance(output, list) or not any(isinstance(item, dict) and item.get("type") == "compaction" for item in output):
                    raise GatewayError("上游未返回 compaction 项，压缩未完成；原上下文未被替换。", 502)
            except Exception:
                self._record_usage(context, usage, failed=True)
                raise
            self.request_count += 1
            self._record_usage(context, usage, failed=False)
            body = {"object": "response.compaction", "output": output}
            for key in ("id", "created_at", "usage"):
                if key in response:
                    body[key] = response[key]
            # Compaction creates a new input window, not a continuation binding.
            return {"status": 200, "contentType": "application/json; charset=utf-8",
                    "headers": result.get("headers", {}), "body": json.dumps(body, ensure_ascii=False).encode("utf-8")}
        response = self._open_provider_upstream(path, payload, route, client_headers,
            **({"expected_identity": binding["identityFingerprint"]} if binding and binding.get("identityFingerprint") else {}))
        try:
            body = _read_limited(response)
            status = int(getattr(response, "status", 200))
            headers = _safe_response_headers(response.headers)
            decoded = json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise GatewayError("Responses 辅助端点未返回有效 JSON。", 502) from exc
        finally:
            response.close()
        if not isinstance(decoded, dict):
            raise GatewayError("Responses 辅助端点返回结构无效。", 502)
        if path.endswith("/input_tokens"):
            tokens = decoded.get("input_tokens")
            if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
                raise GatewayError("上游未返回有效的 input_tokens。", 502)
        else:
            output = decoded.get("output")
            if not isinstance(output, list) or not any(isinstance(item, dict) and item.get("type") == "compaction" for item in output):
                raise GatewayError("上游未返回有效的压缩上下文。", 502)
            context = self._usage_route_context(payload, model, route=route, provider_id=str(route["sourceRecordId"]))
            self._record_usage(context, _payload_usage(decoded), failed=status >= 400)
        self.request_count += 1
        return {"status": status, "contentType": "application/json; charset=utf-8", "body": body, "headers": headers}

    def execute(
        self,
        path: str,
        payload: dict,
        *,
        access_scope: str = "public",
        client_headers: dict[str, str] | None = None,
    ) -> dict:
        if access_scope not in {"public", "internal"}:
            raise GatewayError("网关访问范围无效。", 403)
        if path in RESPONSES_AUXILIARY_PATHS:
            return self._responses_auxiliary(path, payload, access_scope=access_scope, client_headers=client_headers)
        if path == "/v1/alpha/search":
            return self._alpha_search(
                payload,
                access_scope=access_scope,
                client_headers=client_headers,
            )
        if path not in {"/v1/responses", "/v1/chat/completions"}:
            raise GatewayError("接口不存在。", 404)
        client_stream = bool(payload.get("stream"))
        requested_model = str(payload.get("model") or "")
        route = core.resolve_model_route(requested_model, access_scope=access_scope)
        binding = self._resolve_response_binding(
            payload,
            access_scope=access_scope,
            requested_model=requested_model,
            route=route,
        )
        if route and route.get("sourceKind") == "provider":
            context = self._usage_route_context(
                payload,
                requested_model,
                route=route,
                provider_id=str(route.get("sourceRecordId") or ""),
            )
            try:
                response = self._open_provider_upstream(path, payload, route, client_headers,
                    **({"expected_identity": binding["identityFingerprint"]} if binding and binding.get("identityFingerprint") else {}))
            except Exception as exc:
                self._record_usage(context, None, failed=True)
                raise self._bound_identity_error(
                    binding,
                    payload.get("previous_response_id"),
                    exc,
                )
            try:
                body = _read_limited(response)
                content_type = response.headers.get("Content-Type", "application/json; charset=utf-8")
                status = int(getattr(response, "status", 200))
                headers = _safe_response_headers(response.headers)
            except Exception:
                self._record_usage(context, None, failed=True)
                raise
            finally:
                response.close()
            self.request_count += 1
            capture = _SSEUsageCapture()
            usage, body_failed = _inspect_usage_body(body, capture)
            if path == "/v1/responses" and not client_stream and "text/event-stream" in content_type.casefold():
                try:
                    body = json.dumps(_completed_response(body), ensure_ascii=False).encode("utf-8")
                    content_type = "application/json; charset=utf-8"
                except Exception:
                    self._record_usage(context, usage, failed=True)
                    raise
            self._record_usage(context, usage, failed=status >= 400 or body_failed)
            result = {"status": status, "contentType": content_type, "body": body, "headers": headers}
            if path == "/v1/responses" and status < 400 and not body_failed:
                delivered = self._response_binding_delivery_callback(
                    capture,
                    access_scope=access_scope,
                    requested_model=requested_model,
                    routed_model=str(route.get("id") or requested_model),
                    identity_kind="provider",
                    identity_id=str(route.get("sourceRecordId") or ""),
                    identity_fingerprint=str(getattr(response, "_gateway_identity_fingerprint", "")),
                )
                if callable(delivered):
                    result["_onDelivered"] = delivered
            return result
        routed_payload = json.loads(json.dumps(payload))
        allowed_ids = None
        if binding and binding.get("identityKind") == "account":
            routed_payload["model"] = str(binding.get("routedModel") or requested_model)
            allowed_ids = {str(binding.get("identityId") or "")}
        elif route:
            routed_payload["model"] = route["id"]
            allowed_ids = {str(route["sourceRecordId"])}
        responses_lite = path == "/v1/responses" and _responses_lite_enabled(
            str(routed_payload.get("model") or ""),
            route,
            client_headers,
        )
        upstream_payload = (
            _responses_input(routed_payload, responses_lite=responses_lite)
            if path == "/v1/responses"
            else _chat_input(routed_payload)
        )
        try:
            upstream = (
                self._upstream(
                    upstream_payload,
                    allowed_ids,
                    include_headers=True,
                    access_scope=access_scope,
                    responses_lite=responses_lite,
                    **({"expected_identity": binding["identityFingerprint"]} if binding and binding.get("identityFingerprint") else {}),
                )
                if allowed_ids is not None
                else self._upstream(
                    upstream_payload,
                    include_headers=True,
                    access_scope=access_scope,
                    responses_lite=responses_lite,
                )
            )
        except Exception as exc:
            exc = self._bound_identity_error(
                binding,
                payload.get("previous_response_id"),
                exc,
            )
            failed_account = getattr(exc, "route_account", None)
            context = self._usage_route_context(
                payload,
                requested_model,
                route=route,
                account=failed_account if isinstance(failed_account, dict) else None,
            )
            self._record_usage(context, None, failed=True)
            raise exc
        if not isinstance(upstream, dict):
            upstream = {"body": upstream, "headers": {}}
        body = upstream["body"]
        headers = upstream.get("headers", {})
        account = upstream.get("account") if isinstance(upstream.get("account"), dict) else None
        context = self._usage_route_context(payload, requested_model, route=route, account=account)
        capture = _SSEUsageCapture()
        usage, body_failed = _inspect_usage_body(body, capture)
        if account is not None and capture.rate_limited:
            cooldown_headers = dict(headers)
            if (
                not _header_value(cooldown_headers, "retry-after")
                and capture.retry_after_seconds is not None
            ):
                cooldown_headers["retry-after"] = str(capture.retry_after_seconds)
            self._cooldown_account(
                account,
                str(upstream_payload.get("model") or ""),
                cooldown_headers,
            )
        self.request_count += 1
        try:
            if path == "/v1/responses":
                if client_stream:
                    result = {"status": 200, "contentType": "text/event-stream; charset=utf-8", "body": body, "headers": headers}
                else:
                    response = _completed_response(body)
                    result = {
                        "status": 200,
                        "contentType": "application/json; charset=utf-8",
                        "body": json.dumps(response, ensure_ascii=False).encode("utf-8"),
                        "headers": headers,
                    }
            elif client_stream:
                result = {
                    "status": 200,
                    "contentType": "text/event-stream; charset=utf-8",
                    "body": _chat_sse(body, requested_model, include_usage=isinstance(payload.get("stream_options"), dict) and payload["stream_options"].get("include_usage") is True),
                    "headers": headers,
                }
            else:
                response = _completed_response(body)
                completion = _chat_completion(response, requested_model)
                result = {
                    "status": 200,
                    "contentType": "application/json; charset=utf-8",
                    "body": json.dumps(completion, ensure_ascii=False).encode("utf-8"),
                    "headers": headers,
                }
        except Exception:
            self._record_usage(context, usage, failed=True)
            raise
        if path == "/v1/responses" and not body_failed and account is not None:
            delivered = self._response_binding_delivery_callback(
                capture,
                access_scope=access_scope,
                requested_model=requested_model,
                routed_model=str(upstream_payload.get("model") or ""),
                identity_kind="account",
                identity_id=str(account.get("id") or ""),
                identity_fingerprint=str(upstream.get("identityFingerprint") or ""),
            )
            if callable(delivered):
                result["_onDelivered"] = delivered
        self._record_usage(context, usage, failed=body_failed)
        return result
