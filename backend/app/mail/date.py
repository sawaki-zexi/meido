from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def mail_date_key(received_at: str, message_id: str, item_id: str) -> tuple[float, str, str]:
    try:
        try:
            value = parsedate_to_datetime(received_at)
        except (TypeError, ValueError, OverflowError):
            value = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        timestamp = value.astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        timestamp = float("-inf")
    return timestamp, message_id, item_id
