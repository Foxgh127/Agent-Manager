"""Presentation services."""
from __future__ import annotations
from agent_manager import application as _app


def _totp_api_item(item: dict) -> dict:
    remaining = max(1, int(item.get("seconds_remaining") or 1))
    return {
        "id": item.get("id"),
        "code": str(item.get("code") or ""),
        "label": str(item.get("label") or ""),
        "issuer": str(item.get("issuer") or ""),
        "algorithm": str(item.get("algorithm") or "SHA1"),
        "digits": int(item.get("digits") or 6),
        "period": int(item.get("period") or 30),
        "secondsRemaining": remaining,
        "validUntil": (_app.datetime.now(_app.timezone.utc) + _app.timedelta(seconds=remaining)).isoformat(),
        "createdAt": item.get("createdAt"),
    }



def _mail_account_api_item(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "label": item.get("label"),
        "email": item.get("email"),
        "provider": item.get("provider"),
        "imapHost": item.get("imap_host"),
        "imapPort": item.get("imap_port"),
        "security": item.get("security"),
        "authMode": item.get("auth_method"),
        "mailbox": item.get("mailbox"),
        "credentialStatus": item.get("credential_status"),
    }



def _mail_message_api_item(item: dict) -> dict:
    senders = item.get("from") if isinstance(item.get("from"), list) else []
    recipients = item.get("to") if isinstance(item.get("to"), list) else []
    text = str(item.get("text") or "")
    return {
        "id": str(item.get("uid") or ""),
        "subject": str(item.get("subject") or ""),
        "from": ", ".join(str(value) for value in senders),
        "to": ", ".join(str(value) for value in recipients),
        "receivedAt": item.get("date"),
        "unread": bool(item.get("unread")),
        "codes": item.get("verification_codes") if isinstance(item.get("verification_codes"), list) else [],
        "preview": text[:2_000],
        "truncated": bool(item.get("truncated")) or len(text) > 2_000,
    }



def _radar_api_section(envelope: dict | None) -> dict:
    """Flatten the cache envelope while keeping provenance visible to the UI."""
    wrapped = envelope if isinstance(envelope, dict) else {}
    data = wrapped.get("data")
    section = dict(data) if isinstance(data, dict) else {}
    source = wrapped.get("source") if isinstance(wrapped.get("source"), dict) else {}
    section["stale"] = bool(wrapped.get("stale"))
    section["meta"] = {
        "sourceLabel": "Codex Radar 社区公开数据",
        "sourceUrl": source.get("url"),
        "sourceFormat": source.get("format"),
        "fetchedAt": wrapped.get("fetchedAt"),
        "checkedAt": wrapped.get("lastAttemptAt"),
        "nextAllowedAt": wrapped.get("nextAllowedAt"),
        "stale": bool(wrapped.get("stale")),
        "cached": bool(wrapped.get("cached")),
        "refreshSuppressed": bool(wrapped.get("refreshSuppressed")),
        "error": wrapped.get("error"),
    }
    return section

