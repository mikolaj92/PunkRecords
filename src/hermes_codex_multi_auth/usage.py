from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .models import AccountRecord, AccountUsage, UsageWindow
from .oauth import DEFAULT_CODEX_BASE_URL, DEFAULT_HEADERS, OAuthError, maybe_refresh_account


def usage_url() -> str:
    override = os.getenv("HERMES_CODEX_USAGE_URL", "").strip()
    if override:
        return override

    base_url = os.getenv("HERMES_CODEX_BASE_URL", DEFAULT_CODEX_BASE_URL).strip() or DEFAULT_CODEX_BASE_URL
    if base_url.endswith("/codex"):
        return base_url[: -len("/codex")] + "/wham/usage"
    return base_url.rstrip("/") + "/wham/usage"


def _coerce_window(payload: dict[str, Any] | None) -> UsageWindow:
    if not isinstance(payload, dict):
        return UsageWindow()
    used_percent = payload.get("used_percent")
    return UsageWindow(
        used_percent=float(used_percent) if isinstance(used_percent, (int, float)) else None,
        limit_window_seconds=int(payload["limit_window_seconds"]) if isinstance(payload.get("limit_window_seconds"), (int, float)) else None,
        reset_after_seconds=int(payload["reset_after_seconds"]) if isinstance(payload.get("reset_after_seconds"), (int, float)) else None,
        reset_at=int(payload["reset_at"]) if isinstance(payload.get("reset_at"), (int, float)) else None,
    )


def fetch_account_usage(account: AccountRecord, timeout: float = 15.0) -> tuple[AccountRecord, AccountUsage]:
    refreshed_account = maybe_refresh_account(account)
    request = urllib.request.Request(
        usage_url(),
        headers={
            **DEFAULT_HEADERS,
            "Authorization": f"Bearer {refreshed_account.tokens.access_token}",
            "ChatGPT-Account-Id": refreshed_account.account_id,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        return refreshed_account, AccountUsage(
            account_id=refreshed_account.account_id,
            label=refreshed_account.label,
            error=f"HTTP {exc.code}: {body or exc.reason}",
        )
    except (urllib.error.URLError, OAuthError, json.JSONDecodeError) as exc:
        return refreshed_account, AccountUsage(
            account_id=refreshed_account.account_id,
            label=refreshed_account.label,
            error=str(exc),
        )

    rate_limit = payload.get("rate_limit") if isinstance(payload, dict) else {}
    usage = AccountUsage(
        account_id=refreshed_account.account_id,
        label=refreshed_account.label,
        plan_type=str(payload.get("plan_type")) if isinstance(payload, dict) and payload.get("plan_type") is not None else None,
        primary_window=_coerce_window(rate_limit.get("primary_window") if isinstance(rate_limit, dict) else None),
        secondary_window=_coerce_window(rate_limit.get("secondary_window") if isinstance(rate_limit, dict) else None),
    )
    return refreshed_account, usage
