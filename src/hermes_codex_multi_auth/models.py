from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class AccountTokens:
    access_token: str
    refresh_token: str
    account_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "account_id": self.account_id,
        }


@dataclass
class AccountRecord:
    id: str
    account_id: str
    email: str
    label: str
    provider: str = "openai-codex"
    created_at: str = ""
    last_refresh: str = ""
    last_used: str = ""
    auth_mode: str = "chatgpt"
    source: str = "device-flow"
    enabled: bool = True
    last_error: str | None = None
    cooldown_until: str | None = None
    last_proxy_error: str | None = None
    last_proxy_error_at: str | None = None
    tokens: AccountTokens = field(default_factory=lambda: AccountTokens("", "", ""))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["tokens"] = self.tokens.to_dict()
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AccountRecord":
        token_data = data.get("tokens") or {}
        return cls(
            id=str(data.get("id") or ""),
            account_id=str(data.get("account_id") or token_data.get("account_id") or ""),
            email=str(data.get("email") or ""),
            label=str(data.get("label") or ""),
            provider=str(data.get("provider") or "openai-codex"),
            created_at=str(data.get("created_at") or ""),
            last_refresh=str(data.get("last_refresh") or ""),
            last_used=str(data.get("last_used") or ""),
            auth_mode=str(data.get("auth_mode") or "chatgpt"),
            source=str(data.get("source") or "device-flow"),
            enabled=bool(data.get("enabled", True)),
            last_error=data.get("last_error"),
            cooldown_until=data.get("cooldown_until"),
            last_proxy_error=data.get("last_proxy_error"),
            last_proxy_error_at=data.get("last_proxy_error_at"),
            tokens=AccountTokens(
                access_token=str(token_data.get("access_token") or ""),
                refresh_token=str(token_data.get("refresh_token") or ""),
                account_id=str(token_data.get("account_id") or data.get("account_id") or ""),
            ),
        )


@dataclass
class StateSnapshot:
    version: int = 1
    active_account_id: str | None = None
    accounts: list[AccountRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "active_account_id": self.active_account_id,
            "accounts": [account.to_dict() for account in self.accounts],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StateSnapshot":
        accounts = [
            AccountRecord.from_dict(item)
            for item in data.get("accounts", [])
            if isinstance(item, dict)
        ]
        return cls(
            version=int(data.get("version", 1)),
            active_account_id=data.get("active_account_id"),
            accounts=accounts,
        )


@dataclass
class UsageWindow:
    used_percent: float | None = None
    limit_window_seconds: int | None = None
    reset_after_seconds: int | None = None
    reset_at: int | None = None

    def to_dict(self) -> dict[str, float | int | None]:
        return {
            "used_percent": self.used_percent,
            "limit_window_seconds": self.limit_window_seconds,
            "reset_after_seconds": self.reset_after_seconds,
            "reset_at": self.reset_at,
        }


@dataclass
class AccountUsage:
    account_id: str
    label: str
    plan_type: str | None = None
    primary_window: UsageWindow = field(default_factory=UsageWindow)
    secondary_window: UsageWindow = field(default_factory=UsageWindow)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "label": self.label,
            "plan_type": self.plan_type,
            "primary_window": self.primary_window.to_dict(),
            "secondary_window": self.secondary_window.to_dict(),
            "error": self.error,
        }
