"""Bounded, ephemeral correlation against locally observed official responses.

A matching fingerprint supplies candidates, never proof of model weights. Only
the gateway may assign ``official_direct`` after checking the actual upstream.
No response content, credentials, network access or persistent state is used.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

MAX_REFERENCE_KEYS = 1024
MAX_CANDIDATES = 8
REFERENCE_MAX_AGE = timedelta(days=30)
METHOD = "official_observation_fingerprint"
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,199}\Z")


def _token(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if _TOKEN.fullmatch(value) and value.casefold() not in {"unknown", "none", "null"} else ""


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _is_reference(row: dict, now: datetime) -> bool:
    if not (
        row.get("modelIdentityAuthority") == "official_direct"
        and type(row.get("usageEvidenceVersion")) is int
        and row["usageEvidenceVersion"] == 2
        and row.get("modelEvidenceSource") == "response_body"
        and row.get("modelEvidence") == "actual"
        and _token(row.get("actualModel"))
        and _token(row.get("systemFingerprint"))
        and type(row.get("requestCount")) is int and row["requestCount"] > 0
        and type(row.get("failureCount")) is int and row["failureCount"] == 0
    ):
        return False
    observed = _time(row.get("lastSeenAt") or row.get("timestamp"))
    if observed is None or not timedelta(0) <= now - observed <= REFERENCE_MAX_AGE:
        return False
    # An aggregate spanning expired observations is not a fresh baseline.
    if row.get("firstSeenAt"):
        first = _time(row["firstSeenAt"])
        if first is None or not timedelta(0) <= now - first <= REFERENCE_MAX_AGE:
            return False
    return True


def annotate_identities(
    records: list[dict], recent: list[dict], *, now: datetime | None = None,
) -> None:
    """Annotate both views without changing routing, billing or metadata values.

    Only aggregate ``records`` train the index; ``recent`` is a display view
    of the same traffic and must not count again. Exceeding the key budget abandons
    the whole index rather than accepting an incomplete, falsely unique match.
    """
    now = now or datetime.now(timezone.utc)
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    index: dict[str, dict] = {}
    overflow = False
    for row in records:
        if not isinstance(row, dict) or not _is_reference(row, now):
            continue
        fingerprint = _token(row.get("systemFingerprint"))
        if fingerprint not in index:
            if len(index) >= MAX_REFERENCE_KEYS:
                overflow = True
                index.clear()
                break
            index[fingerprint] = {"models": set(), "ambiguous": False, "count": 0}
        entry = index[fingerprint]
        model = _token(row.get("actualModel"))
        if model not in entry["models"]:
            if entry["models"]:
                entry["ambiguous"] = True
            if len(entry["models"]) < MAX_CANDIDATES:
                entry["models"].add(model)
        entry["count"] += row["requestCount"]

    for rows in (records, recent):
        for row in rows:
            if not isinstance(row, dict):
                continue
            result = {"status": "unknown", "candidates": [], "method": METHOD, "referenceCount": 0}
            fingerprint = _token(row.get("systemFingerprint"))
            if not overflow and type(row.get("usageEvidenceVersion")) is int and row["usageEvidenceVersion"] == 2:
                entry = index.get(fingerprint)
                if entry:
                    result.update(
                        status="ambiguous" if entry["ambiguous"] else "candidate",
                        candidates=sorted(entry["models"]), referenceCount=entry["count"],
                    )
                if _is_reference(row, now):
                    result["status"] = "reference"
                    if not entry:
                        result.update(candidates=[_token(row.get("actualModel"))], referenceCount=row["requestCount"])
            row["modelIdentity"] = result
