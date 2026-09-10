"""Reviewed occurrence dates; community announcement times are not occurrences.

Maintained from the linked official article, reviewed 2026-09-10. This is a
limited dated record, not an exhaustive or automatically ingested history.
The article gives dates but does not assign a timezone to those occurrences.
"""
from __future__ import annotations

SOURCE = "https://help.openai.com/en/articles/20001498-how-banked-codex-resets-work"


def verified_reset_history() -> list[dict]:
    records = [
        ("2026-09-07", "full-reset", "Plus、Pro 与 Business 获得一次立即生效的全局重置"),
        ("2026-09-04", "reset-card", "符合条件的 Plus、Pro 与 Business 用户获赠重置卡"),
        ("2026-09-03", "reset-card", "符合条件的 Plus、Pro 与 Business 用户获赠重置卡"),
    ]
    return [{"eventId": f"openai-{date}-{kind}", "occurredAt": date,
             "occurrencePrecision": "date", "occurrenceTimezone": None,
             "resetType": kind, "completed": True, "verification": "official_source",
             "titleZh": title, "sourceLabel": "OpenAI 帮助中心", "url": SOURCE,
             "reviewedAt": "2026-09-10", "publishedAt": None}
            for date, kind, title in records]
