from __future__ import annotations

import json
from typing import Any


def extract_retry_after(headers: dict[str, str]) -> int | None:
    value = headers.get("Retry-After") or headers.get("retry-after")
    if not value:
        return None
    try:
        seconds = int(value)
    except ValueError:
        return None
    return max(0, seconds)


def classify_status(status_code: int, body: bytes) -> tuple[bool, int]:
    if status_code in {408, 409, 429, 500, 502, 503, 504}:
        return True, 60

    if status_code == 402:
        try:
            payload = json.loads(body.decode() or "{}")
        except Exception:
            return False, 0
        detail = payload.get("detail") if isinstance(payload, dict) else None
        code = detail.get("code") if isinstance(detail, dict) else None
        if code in {"deactivated_workspace", "usage_limit_reached", "rate_limited"}:
            return True, 300
    return False, 0


def is_streaming_request(payload: dict[str, Any]) -> bool:
    return bool(payload.get("stream"))
