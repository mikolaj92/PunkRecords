from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class AccountTokens:
    access_token: str = ""
    refresh_token: str = ""
    account_id: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "account_id": self.account_id,
        }


@dataclass
class AccountRecord:
    id: str = ""
    external_id: str = ""
    contact: str = ""
    display_name: str = ""
    provider: str = ""
    created_at: str = ""
    last_refresh: str = ""
    last_used: str = ""
    auth_kind: str = ""
    creation_source: str = ""
    enabled: bool = True
    last_error: str | None = None
    cooldown_until: str | None = None
    last_proxy_error: str | None = None
    last_proxy_error_at: str | None = None
    provider_state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AccountRecord:
        provider_state = data.get("provider_state")
        return cls(
            id=str(data.get("id") or ""),
            external_id=str(data.get("external_id") or ""),
            contact=str(data.get("contact") or ""),
            display_name=str(data.get("display_name") or ""),
            provider=str(data.get("provider") or ""),
            created_at=str(data.get("created_at") or ""),
            last_refresh=str(data.get("last_refresh") or ""),
            last_used=str(data.get("last_used") or ""),
            auth_kind=str(data.get("auth_kind") or "provider-plugin"),
            creation_source=str(data.get("creation_source") or "device-flow"),
            enabled=bool(data.get("enabled", True)),
            last_error=data.get("last_error"),
            cooldown_until=data.get("cooldown_until"),
            last_proxy_error=data.get("last_proxy_error"),
            last_proxy_error_at=data.get("last_proxy_error_at"),
            provider_state=provider_state if isinstance(provider_state, dict) else {},
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
    def from_dict(cls, data: dict[str, Any]) -> StateSnapshot:
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


@dataclass(init=False)
class AccountUsage:
    external_id: str = ""
    display_name: str = ""
    provider: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def __init__(
        self,
        external_id: str = "",
        display_name: str = "",
        provider: str = "",
        details: dict[str, Any] | None = None,
        error: str | None = None,
        plan_type: str | None = None,
        primary_window: UsageWindow | None = None,
        secondary_window: UsageWindow | None = None,
    ) -> None:
        self.external_id = external_id
        self.display_name = display_name
        self.provider = provider
        self.details = details.copy() if isinstance(details, dict) else {}
        if plan_type is not None:
            self.details["plan_type"] = plan_type
        if primary_window is not None:
            self.details["primary_window"] = primary_window.to_dict()
        if secondary_window is not None:
            self.details["secondary_window"] = secondary_window.to_dict()
        self.error = error

    @property
    def plan_type(self) -> str | None:
        value = self.details.get("plan_type")
        return str(value) if value is not None else None

    @plan_type.setter
    def plan_type(self, value: str | None) -> None:
        if value is None:
            self.details.pop("plan_type", None)
        else:
            self.details["plan_type"] = value

    @property
    def primary_window(self) -> UsageWindow:
        value = self.details.get("primary_window")
        if isinstance(value, UsageWindow):
            return value
        if isinstance(value, dict):
            return UsageWindow(
                used_percent=float(value["used_percent"])
                if isinstance(value.get("used_percent"), (int, float))
                else None,
                limit_window_seconds=int(value["limit_window_seconds"])
                if isinstance(value.get("limit_window_seconds"), (int, float))
                else None,
                reset_after_seconds=int(value["reset_after_seconds"])
                if isinstance(value.get("reset_after_seconds"), (int, float))
                else None,
                reset_at=int(value["reset_at"])
                if isinstance(value.get("reset_at"), (int, float))
                else None,
            )
        return UsageWindow()

    @primary_window.setter
    def primary_window(self, value: UsageWindow) -> None:
        self.details["primary_window"] = value.to_dict()

    @property
    def secondary_window(self) -> UsageWindow:
        value = self.details.get("secondary_window")
        if isinstance(value, UsageWindow):
            return value
        if isinstance(value, dict):
            return UsageWindow(
                used_percent=float(value["used_percent"])
                if isinstance(value.get("used_percent"), (int, float))
                else None,
                limit_window_seconds=int(value["limit_window_seconds"])
                if isinstance(value.get("limit_window_seconds"), (int, float))
                else None,
                reset_after_seconds=int(value["reset_after_seconds"])
                if isinstance(value.get("reset_after_seconds"), (int, float))
                else None,
                reset_at=int(value["reset_at"])
                if isinstance(value.get("reset_at"), (int, float))
                else None,
            )
        return UsageWindow()

    @secondary_window.setter
    def secondary_window(self, value: UsageWindow) -> None:
        self.details["secondary_window"] = value.to_dict()

    def to_dict(self) -> dict[str, Any]:
        return {
            "external_id": self.external_id,
            "display_name": self.display_name,
            "provider": self.provider,
            "details": self.details,
            "plan_type": self.plan_type,
            "primary_window": self.primary_window.to_dict(),
            "secondary_window": self.secondary_window.to_dict(),
            "error": self.error,
        }
