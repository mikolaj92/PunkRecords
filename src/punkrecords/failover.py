from __future__ import annotations

def extract_retry_after(headers: dict[str, str]) -> int | None:
    value = headers.get("Retry-After") or headers.get("retry-after")
    if not value:
        return None
    try:
        seconds = int(value)
    except ValueError:
        return None
    return max(0, seconds)
