"""Bounded RFC 6455 downstream adapter; upstream remains ordinary HTTP/SSE.

One response at a time, no socket-local upstream state, warmup, steering, or
multiplexing. All routing/authentication/usage rules stay in Web2APIManager.
"""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
from urllib.parse import urlparse

MAX_MESSAGE_BYTES = 8_000_000
MAX_TURNS = 128
IDLE_TIMEOUT_SECONDS = 120.0


class ProtocolError(Exception):
    def __init__(self, message, code=1002):
        super().__init__(message)
        self.code = code


def _exact(stream, count):
    data = stream.read(count)
    if len(data) != count:
        raise EOFError()
    return data


def _frame(stream):
    first, second = _exact(stream, 2)
    final, opcode = bool(first & 128), first & 15
    if first & 112 or not second & 128 or opcode not in {0, 1, 2, 8, 9, 10}:
        raise ProtocolError("Invalid WebSocket frame")
    length = second & 127
    if length == 126:
        length = struct.unpack("!H", _exact(stream, 2))[0]
        if length < 126:
            raise ProtocolError("Noncanonical frame length")
    elif length == 127:
        length = struct.unpack("!Q", _exact(stream, 8))[0]
        if length < 65536 or length >= 2**63:
            raise ProtocolError("Noncanonical frame length")
    if opcode >= 8 and (not final or length > 125):
        raise ProtocolError("Invalid control frame")
    if length > MAX_MESSAGE_BYTES:
        raise ProtocolError("Frame too large", 1009)
    mask = _exact(stream, 4)
    payload = _exact(stream, length)
    return final, opcode, bytes(value ^ mask[index % 4] for index, value in enumerate(payload))


def _encoded_frame(opcode, data):
    size = len(data)
    header = bytes([128 | opcode])
    if size < 126:
        header += bytes([size])
    elif size < 65536:
        header += b"\x7e" + struct.pack("!H", size)
    else:
        header += b"\x7f" + struct.pack("!Q", size)
    return header + data


def _events(chunks):
    """Decode complete SSE events across arbitrary byte boundaries."""
    buffer = bytearray()
    data = []
    total = 0
    for chunk in chunks:
        buffer.extend(chunk)
        while b"\n" in buffer:
            raw, _, remainder = buffer.partition(b"\n")
            buffer = bytearray(remainder)
            line = raw.rstrip(b"\r")
            if line.startswith(b"data:"):
                value = line[5:].lstrip(b" ")
                data.append(value)
                total += len(value)
                if total > MAX_MESSAGE_BYTES:
                    raise ProtocolError("SSE event too large", 1009)
            elif not line and data:
                payload = b"\n".join(data)
                data.clear()
                total = 0
                if payload != b"[DONE]":
                    event = json.loads(payload)
                    if not isinstance(event, dict):
                        raise ProtocolError("Invalid upstream event")
                    yield event
        if len(buffer) > MAX_MESSAGE_BYTES:
            raise ProtocolError("SSE line too large", 1009)
    if buffer or data:
        # An unterminated SSE event is not a complete deliverable message.
        raise ProtocolError("Unterminated upstream SSE event")


def serve_responses_websocket(handler, access_scope):
    from agent_manager.gateway.service import GatewayError

    def single(name):
        values = handler._header_values(name)
        return values[0] if len(values) == 1 else ""

    key = single("Sec-WebSocket-Key")
    try:
        key_bytes = base64.b64decode(key, validate=True)
    except (ValueError, UnicodeError):
        key_bytes = b""
    if (single("Sec-WebSocket-Version") != "13" or len(key_bytes) != 16
            or single("Upgrade").casefold() != "websocket"
            or "upgrade" not in {value.strip().casefold() for value in single("Connection").split(",")}
            or single("Content-Length") not in {"", "0"}):
        raise GatewayError("WebSocket 握手无效。", 400)
    origins = handler._header_values("Origin")
    if origins:
        parsed = urlparse(origins[0])
        if len(origins) != 1 or parsed.scheme not in {"http", "https"} or parsed.netloc.casefold() != single("Host").casefold():
            raise GatewayError("WebSocket Origin 不属于本地网关。", 403)
    accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
    # BaseHTTPRequestHandler otherwise responds using HTTP/1.0.
    handler.protocol_version = "HTTP/1.1"
    handler.send_response(101)
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept)
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.flush()
    handler.close_connection = True
    handler.connection.settimeout(IDLE_TIMEOUT_SECONDS)
    send_lock = threading.Lock()
    state_lock = threading.Lock()
    disconnected = threading.Event()
    state = {"active": False, "abort": None, "generation": 0}
    client_headers = handler._client_identity_headers()

    def send(opcode, payload):
        if disconnected.is_set():
            raise EOFError()
        with send_lock:
            handler.wfile.write(_encoded_frame(opcode, payload))
            handler.wfile.flush()

    def event(value):
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_MESSAGE_BYTES:
            raise ProtocolError("Response event too large", 1009)
        send(1, payload)

    def error(message, code="unsupported_operation", status=400):
        event({"type": "error", "status": status, "error": {"type": "invalid_request_error", "code": code, "message": message}})

    def generate(payload, slot, generation):
        chunks = None
        slot_released = False
        def release_upstream():
            nonlocal chunks, slot_released
            if chunks is not None:
                try:
                    chunks.close()
                except Exception:
                    pass
                chunks = None
            if not slot_released:
                slot_released = True
                slot.__exit__(None, None, None)
        try:
            result = handler.server.manager.stream("/v1/responses", payload,
                access_scope=access_scope, client_headers=client_headers)
            chunks = result["chunks"]
            abort = result.get("_abort")
            with state_lock:
                state["abort"] = abort
            if disconnected.is_set():
                if callable(abort):
                    abort()
                return
            pending_terminal = None
            for value in _events(chunks):
                if disconnected.is_set():
                    return
                if value.get("type") in {"response.completed", "response.incomplete", "response.failed", "error"}:
                    if pending_terminal is not None and pending_terminal != value:
                        raise ProtocolError("Conflicting response terminal events")
                    pending_terminal = value
                else:
                    event(value)
            # Drain the manager iterator first so response-ID ownership is
            # committed before the client receives the terminal response.
            if pending_terminal is None:
                raise GatewayError("上游提前断流，未返回终态。", 502)
            # A client may send its next turn as soon as the terminal arrives.
            # Release the shared HTTP/WS slot before advertising completion.
            release_upstream()
            with state_lock:
                if state["generation"] == generation:
                    state["active"] = False
                    state["abort"] = None
            event(pending_terminal)
        except Exception as exc:
            release_upstream()
            with state_lock:
                if state["generation"] == generation:
                    state["active"] = False
                    state["abort"] = None
            if not disconnected.is_set():
                try:
                    error(str(exc) if isinstance(exc, GatewayError) else "上游响应未能完成。", "upstream_error",
                          exc.status if isinstance(exc, GatewayError) else 502)
                except Exception:
                    disconnected.set()
        finally:
            release_upstream()
            with state_lock:
                if state["generation"] == generation:
                    state["active"] = False
                    state["abort"] = None

    fragmented = bytearray()
    fragment_opcode = None
    turns = 0
    deadline = time.monotonic() + 900.0
    try:
        while time.monotonic() < deadline:
            final, opcode, payload = _frame(handler.rfile)
            if opcode == 8:
                if len(payload) == 1:
                    raise ProtocolError("Invalid close frame")
                if len(payload) >= 2:
                    close_code = struct.unpack("!H", payload[:2])[0]
                    if close_code not in {1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014} and not 3000 <= close_code < 5000:
                        raise ProtocolError("Invalid close code")
                    payload[2:].decode("utf-8")
                send(8, payload)
                break
            if opcode == 9:
                send(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode == 2:
                raise ProtocolError("Only JSON text messages are supported", 1003)
            if opcode == 1:
                if fragment_opcode is not None:
                    raise ProtocolError("Interleaved fragmented messages")
                fragment_opcode = 1
            elif fragment_opcode is None:
                raise ProtocolError("Unexpected continuation")
            fragmented.extend(payload)
            if len(fragmented) > MAX_MESSAGE_BYTES:
                raise ProtocolError("Message too large", 1009)
            if not final:
                continue
            raw = bytes(fragmented)
            fragmented.clear()
            fragment_opcode = None
            text = raw.decode("utf-8")
            try:
                request = json.loads(text)
            except ValueError:
                error("请求 JSON 无效。", "invalid_json")
                continue
            if not isinstance(request, dict) or request.get("type") != "response.create":
                error("此 HTTP 后端串行桥仅支持 response.create；关闭连接可取消请求。")
                continue
            if any(key in request for key in ("stream_id", "stream")) or request.get("background") or request.get("generate") is False:
                error("此桥不支持 stream_id、多路复用、background 或 warmup；上游仍使用 HTTP/SSE。")
                continue
            with state_lock:
                busy = state["active"]
            if busy:
                error("此连接已有进行中的响应；请等待终态后再发送下一轮。", "response_in_progress", 409)
                continue
            if turns >= MAX_TURNS:
                error("此连接已达到轮次上限，请重新连接。", "connection_turn_limit", 429)
                break
            payload = {key: value for key, value in request.items() if key not in {"type", "generate", "background"}}
            payload["stream"] = True
            slot = handler.server.upstream_slot()
            try:
                slot.__enter__()
            except GatewayError as exc:
                error(str(exc), "gateway_busy", exc.status)
                continue
            with state_lock:
                state["active"] = True
                state["generation"] += 1
                generation = state["generation"]
            worker = threading.Thread(target=generate, args=(payload, slot, generation), daemon=True, name="web2api-ws-turn")
            try:
                worker.start()
            except Exception:
                slot.__exit__(None, None, None)
                raise
            turns += 1
    except (EOFError, OSError):
        pass
    except (ProtocolError, UnicodeError) as exc:
        try:
            send(8, struct.pack("!H", exc.code if isinstance(exc, ProtocolError) else 1007))
        except (EOFError, OSError):
            pass
    finally:
        disconnected.set()
        with state_lock:
            abort = state["abort"]
            active = state["active"]
        if active:
            with handler.server.manager.lock:
                handler.server.manager.client_cancelled_count += 1
        if callable(abort):
            abort()
