"""Pure, conservative ChatGPT subscription classification; never makes requests.

Only inspect subscription-shaped fields. Callers must select the intended account
before passing an accounts/check record; account collections are not traversed.
Unknown product IDs and generic Pro never imply a multiplier.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


_ALIASES = {
    "Pro 20x": {"pro20x", "promax", "chatgptpro20x", "chatgptpromax", "codexpro20x"},
    "Pro 5x": {"pro5x", "prolite", "chatgptpro5x", "chatgptprolite", "codexpro5x"},
    "Pro": {"pro", "proplan", "chatgptpro", "chatgptproplan"},
    "Plus": {"plus", "plusplan", "chatgptplus", "chatgptplusplan"},
    "Free": {"free", "freeplan", "freetier", "basic", "chatgptfree"},
    "Team": {"team", "teamplan", "chatgptteam"},
    "Business": {"business", "businessplan", "chatgptbusiness"},
    "Enterprise": {"enterprise", "enterpriseplan", "chatgptenterprise"},
    "Edu": {"edu", "education", "chatgptedu"},
    "Go": {"go", "goplan", "chatgptgo"},
}
_PLAN_KEYS = {
    "plan", "planraw", "planlabel", "plantype", "planname", "subscriptionplan",
    "subscriptiontier", "codexplantype", "productplan", "tier", "productid",
    "productname", "sku", "authfileplantype", "chatgptplantype",
}
_CONTAINERS = {
    "data", "account", "subscription", "currentsubscription", "activesubscription",
    "entitlement", "entitlements", "product", "plan", "billing", "subscriptiondetails",
}
_SUBSCRIPTION_CONTAINERS = {
    "subscription", "currentsubscription", "activesubscription", "entitlement",
    "entitlements", "subscriptiondetails",
}
_MULTIPLIER_KEYS = {"promultiplier", "planmultiplier", "subscriptionmultiplier", "usagemultiplier"}
_EXPIRED_STATES = {"expired", "inactive", "ended", "unpaid", "incompleteexpired"}


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", value.strip())[:96] if isinstance(value, str) else ""


def normalize_plan_label(value: Any) -> str:
    raw = _text(value)
    token = _key(raw)
    for label, aliases in _ALIASES.items():
        if token in aliases:
            return label
    # Explicit Pro product slugs with a billing cadence remain unambiguous.
    match = re.fullmatch(r"((?:chatgpt|codex)?pro(?:20x|5x|max|lite))(?:monthly|annual|yearly)", token)
    if match:
        return normalize_plan_label(match[1])
    return raw[:48]


def _stamp(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        if isinstance(value, (int, float)):
            return float(value) / (1000 if value > 100_000_000_000 else 1)
        if isinstance(value, str) and value.strip():
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
    except (ValueError, OverflowError, OSError):
        pass
    return 0.0


def _first(record: dict, names: tuple[str, ...], fallback: Any = None) -> Any:
    return next((record[name] for name in names if record.get(name) is not None), fallback)


def extract_plan_metadata(payload: Any, *, source: str = "usage", observed_at: Any = None,
                          now: datetime | None = None) -> dict:
    """Return only plan fields plus a small, safe provenance record.

    Supported product/tier/multiplier spellings are compatibility inputs, not a
    claim that every private endpoint exposes them. ``rate_multiplier`` is
    deliberately excluded: per-model quota boosts do not identify a Pro tier.
    """
    if not isinstance(payload, dict):
        return {}
    now_stamp = (now or datetime.now(timezone.utc)).timestamp()
    candidates = []

    def visit(record: dict, path: str, inherited: dict, pro_context: bool, depth: int) -> None:
        if depth > 8:
            return
        fields = {_key(k): v for k, v in record.items()}
        state = _key(_first(fields, ("subscriptionstatus", "status"), inherited.get("status", "")))
        expiry = _first(fields, ("subscriptionexpiresat", "currentperiodend", "activeuntil", "expiresat"), inherited.get("expiresAt"))
        # An expiry is a validity bound, never evidence of when a plan changed.
        effective = _first(fields, ("planeffectiveat", "effectiveat", "currentperiodstart", "subscriptionstartedat", "updatedat"), inherited.get("effectiveAt"))
        checked = _first(fields, ("subscriptionlastcheckedat", "checkedat", "observedat"), inherited.get("observedAt", observed_at))
        meta = {"source": source, "observedAt": checked, "effectiveAt": effective,
                "status": state, "expiresAt": expiry}
        if state in _EXPIRED_STATES or fields.get("isactive") is False or (0 < _stamp(expiry) <= now_stamp):
            return
        local_labels = [normalize_plan_label(v) for k, v in fields.items() if k in _PLAN_KEYS and isinstance(v, str)]
        # accounts/check stores the base plan beside entitlement, in account.
        account = fields.get("account")
        if isinstance(account, dict):
            local_labels += [normalize_plan_label(v) for k, v in account.items() if _key(k) in _PLAN_KEYS and isinstance(v, str)]
        known_local = [label for label in local_labels if label in _ALIASES]
        pro_context = any(label.startswith("Pro") for label in known_local) if known_local else pro_context
        for key, value in fields.items():
            raw = _text(value)
            label = normalize_plan_label(raw) if key in _PLAN_KEYS else ""
            if key in {"id", "name"} and path.rsplit(".", 1)[-1] in {"product", "plan"}:
                label = normalize_plan_label(raw)
            multiplier_key = key in _MULTIPLIER_KEYS or (
                key == "multiplier" and any(p in _SUBSCRIPTION_CONTAINERS for p in path.split(".")))
            if pro_context and (multiplier_key or key in {"tier", "subscriptiontier"}):
                token = _key(value)
                if not isinstance(value, bool) and token in {"5", "5x", "20", "20x"}:
                    label = "Pro 20x" if token in {"20", "20x"} else "Pro 5x"
                    raw = str(value)
            if label in _ALIASES:
                evidence = {**meta, "field": f"{path}.{key}".strip("."),
                            "specificity": 2 if label in {"Pro 5x", "Pro 20x"} else 1}
                plan = label.lower().replace(" ", "") if label.startswith("Pro ") else raw
                candidates.append({"plan": plan, "planRaw": raw, "planLabel": label, "planEvidence": evidence})
            if key in _CONTAINERS:
                child_path = f"{path}.{key}".strip(".")
                if isinstance(value, dict):
                    visit(value, child_path, meta, pro_context, depth + 1)
                elif isinstance(value, list):
                    for child in value[:100]:
                        if isinstance(child, dict):
                            visit(child, child_path, meta, pro_context, depth + 1)

    visit(payload, "", {}, False, 0)
    return select_plan_metadata(*candidates, now=now)


def select_plan_metadata(*snapshots: Any, now: datetime | None = None) -> dict:
    """Reconcile parsed snapshots without combining unrelated scalar strings.

    Explicit effective/change times beat older evidence. When both snapshots
    have observation times, prefer the fresh observation; otherwise subscription
    authority wins, then specificity.
    Equally ranked contradictory Pro variants degrade to generic Pro.
    """
    now_stamp = (now or datetime.now(timezone.utc)).timestamp()
    usable = []
    for item in snapshots:
        if not isinstance(item, dict):
            continue
        label = normalize_plan_label(item.get("planLabel") or item.get("plan"))
        if label not in _ALIASES:
            continue
        supplied_evidence = item.get("planEvidence") if isinstance(item.get("planEvidence"), dict) else {}
        evidence = {key: value for key, value in supplied_evidence.items()
                    if key in {"source", "observedAt", "effectiveAt", "status", "expiresAt", "field", "conflict"}}
        if _key(evidence.get("status", "")) in _EXPIRED_STATES:
            continue
        if 0 < _stamp(evidence.get("expiresAt")) <= now_stamp:
            continue
        evidence["specificity"] = 2 if label in {"Pro 5x", "Pro 20x"} else 1
        usable.append({"plan": _text(item.get("plan")) or label,
                       "planRaw": _text(item.get("planRaw") or item.get("plan")) or label,
                       "planLabel": label, "planEvidence": evidence})
    if not usable:
        return {}
    # The app displays fresh official Pro as 20x. A newer official observation
    # may replace an older cached 5x, while an explicit tier in the same batch
    # still wins over a generic family field and stale token claims.
    explicit = [item for item in usable if item["planLabel"] in {"Pro 5x", "Pro 20x"}]
    if explicit:
        def newer_than_variants(item: dict) -> bool:
            observed = _stamp(item["planEvidence"].get("observedAt"))
            if item["planEvidence"].get("source") in {"usage", "entitlement", "subscription"} and observed and all(
                _stamp(variant["planEvidence"].get("observedAt")) < observed for variant in explicit
            ):
                return True
            changed = _stamp(item["planEvidence"].get("effectiveAt"))
            return bool(changed and all(
                0 < _stamp(variant["planEvidence"].get("effectiveAt")) < changed
                for variant in explicit))
        newer_generic = [item for item in usable if item["planLabel"] == "Pro" and newer_than_variants(item)]
        if newer_generic:
            usable = [item for item in usable if item["planLabel"] not in {"Pro 5x", "Pro 20x"}]
        else:
            usable = [item for item in usable if item["planLabel"] != "Pro"]
    # A known change time on one source cannot date another undated source.
    dated = [_stamp(i["planEvidence"].get("effectiveAt")) for i in usable]
    compare_dates = all(dated)
    compare_observed = all(_stamp(i["planEvidence"].get("observedAt")) for i in usable)
    authority = {"entitlement": 3, "subscription": 3, "usage": 2, "token": 1, "cache": 0}

    def rank(item: dict) -> tuple:
        evidence = item["planEvidence"]
        return (_stamp(evidence.get("effectiveAt")) if compare_dates else 0,
                _stamp(evidence.get("observedAt")) if compare_observed else 0,
                authority.get(evidence.get("source"), 0),
                int(evidence["specificity"]), _stamp(evidence.get("observedAt")))

    best = max(usable, key=rank)
    peers = [item for item in usable if rank(item) == rank(best)]
    if {item["planLabel"] for item in peers} >= {"Pro 5x", "Pro 20x"}:
        return {"plan": "pro", "planRaw": "pro", "planLabel": "Pro",
                "planEvidence": {**best["planEvidence"], "specificity": 1, "conflict": True}}
    return best
