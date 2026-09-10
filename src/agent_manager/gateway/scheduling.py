"""Small process-local scheduling primitives for the independent Python gateway.

Design references (not copied implementation): CLIProxyAPI d1a024e auth selector
and session cache. No external engine, protocol, credentials or runtime dependency.
"""
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time


def session_key(scope, model, headers):
    values = {str(k).lower(): str(v).strip() for k, v in (headers or {}).items()}
    value = next((values.get(k) for k in ("session-id", "session_id", "x-session-id", "thread-id") if values.get(k)), "")
    if not value or len(value.encode("utf-8")) > 1024:
        return ""
    return hashlib.sha256(json.dumps([scope, model, value], separators=(",", ":")).encode()).hexdigest()


class SessionStateError(RuntimeError):
    """Persistent identity state could not be verified or committed."""


class Scheduler:
    def __init__(self, lock, session_path=None):
        self.lock = lock
        self.inflight = {}
        self.last = OrderedDict()
        self.sessions = OrderedDict()
        self.session_path = Path(session_path) if session_path else None
        self.persistence_error = False
        self._load_sessions()

    def _load_sessions(self):
        if self.session_path is None:
            return
        try:
            if not self.session_path.exists():
                return
            if self.session_path.stat().st_size > 2_000_000:
                raise ValueError("oversized session state")
            payload = json.loads(self.session_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("schemaVersion") != 1 or not isinstance(payload.get("sessions"), dict):
                raise ValueError("invalid session state")
            now = time.time()
            for key, record in list(payload["sessions"].items())[-2048:]:
                if not isinstance(record, dict) or not re.fullmatch(r"[0-9a-f]{64}", key):
                    raise ValueError("invalid session identity")
                if (record.get("kind") not in {"account", "provider"}
                    or not isinstance(record.get("id"), str) or not 0 < len(record["id"]) <= 200
                    or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("fingerprint") or ""))
                    or type(record.get("expires")) not in {int, float}
                    or not now < record["expires"] <= now + 3600):
                    # Expired records are normal; malformed live records make
                    # session routing fail closed instead of silently rebinding.
                    if type(record.get("expires")) in {int, float} and record["expires"] <= now:
                        continue
                    raise ValueError("invalid session binding")
                self.sessions[key] = {field: record[field] for field in ("kind", "id", "fingerprint", "expires")}
        except (OSError, ValueError, TypeError):
            self.persistence_error = True

    def _persist_sessions(self):
        if self.session_path is None:
            return
        temporary = None
        try:
            self.session_path.parent.mkdir(parents=True, exist_ok=True)
            records = {key: value for key, value in self.sessions.items() if value["fingerprint"]}
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.session_path.parent,
                                             prefix=".gateway-sessions-", suffix=".tmp", delete=False) as stream:
                temporary = stream.name
                json.dump({"schemaVersion": 1, "sessions": records}, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.session_path)
        except OSError as exc:
            self.persistence_error = True
            raise SessionStateError("Session identity state could not be persisted") from exc
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def bound(self, key):
        if not key:
            return None
        with self.lock:
            if self.persistence_error:
                raise SessionStateError("Session identity state is unavailable")
            now = time.time()
            for old, record in list(self.sessions.items()):
                if record["expires"] <= now:
                    self.sessions.pop(old, None)
            record = self.sessions.get(key)
            if record:
                self.sessions.move_to_end(key)
                record["expires"] = now + 3600
            return dict(record) if record else None

    def bind(self, key, kind, identity, fingerprint=""):
        if not key:
            return True
        with self.lock:
            prior = self.bound(key)
            if prior and (prior["kind"] != kind or prior["id"] != identity or
                          (fingerprint and prior["fingerprint"] and fingerprint != prior["fingerprint"])):
                return False
            self.sessions[key] = {"kind": kind, "id": identity,
                                  "fingerprint": fingerprint or (prior or {}).get("fingerprint", ""),
                                  "expires": time.time() + 3600}
            self.sessions.move_to_end(key)
            while len(self.sessions) > 2048:
                self.sessions.popitem(last=False)
            if fingerprint:
                self._persist_sessions()
            return True

    def order(self, records, *, kind, model, policy, identity=lambda row: str(row["id"]), score=lambda row: 0):
        with self.lock:
            records = list(records)
            if policy == "ordered":
                return records
            shard = (kind, model)
            last = self.last.get(shard)
            ids = [identity(row) for row in records]
            if last in ids:
                offset = ids.index(last) + 1
                records = records[offset:] + records[:offset]
            # Stable ties retain the identity cursor, even when candidates cool
            # down or other models are requested between two selections.
            records.sort(key=lambda row: (self.inflight.get((kind, identity(row)), 0),
                                          -score(row) if policy == "quota_first" else 0))
            if records:
                self.last[shard] = identity(records[0])
                self.last.move_to_end(shard)
                while len(self.last) > 2048:
                    self.last.popitem(last=False)
            return records

    def acquire(self, kind, identity):
        key = (kind, identity)
        with self.lock:
            self.inflight[key] = self.inflight.get(key, 0) + 1
        released = False

        def release():
            nonlocal released
            with self.lock:
                if released:
                    return
                released = True
                count = self.inflight.get(key, 0) - 1
                if count > 0:
                    self.inflight[key] = count
                else:
                    self.inflight.pop(key, None)
        return release


class LeasedResponse:
    """Keep the selected identity busy until read completion, failure or cancel."""
    def __init__(self, response, release):
        self._response = response
        self._release = release
        self._close_lock = threading.Lock()
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._response, name)

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._response.close()
        finally:
            self._release()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
