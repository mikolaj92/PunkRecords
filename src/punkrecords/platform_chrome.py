"""Product shell chrome: menu data and per-request platform context."""

from __future__ import annotations

from typing import Any, Final

from app_factory.platform import (
    MenuGroup,
    MenuItem,
    PlatformConfig,
    build_platform_context,
    install_platform,
)
from fastapi import FastAPI
from jinja2 import Environment

APP_NAME: Final = "PunkRecords"
BRAND_META: Final = "Server and admin surfaces."

_MENU: Final = (
    MenuItem(label="Dashboard", href="/", key="dashboard", no_htmx=True),
    MenuGroup(
        label="Admin JSON",
        items=(
            MenuItem(
                label="Admin state",
                href="/_proxy/admin/state",
                key="admin-state",
                no_htmx=True,
            ),
            MenuItem(
                label="Accounts",
                href="/_proxy/admin/accounts",
                key="admin-accounts",
                no_htmx=True,
            ),
            MenuItem(
                label="Requests",
                href="/_proxy/admin/requests",
                key="admin-requests",
                no_htmx=True,
            ),
            MenuItem(
                label="Settings",
                href="/_proxy/admin/settings",
                key="admin-settings",
                no_htmx=True,
            ),
            MenuItem(
                label="Stats summary",
                href="/_proxy/stats/summary",
                key="stats-summary",
                no_htmx=True,
            ),
        ),
    ),
)

PLATFORM_CONFIG: Final = PlatformConfig(
    app_name=APP_NAME,
    brand_href="/",
    brand_meta=BRAND_META,
    brand_htmx=False,
    navigation_label="PunkRecords",
    menu=_MENU,
    enable_admin_users=False,
    # Auth UI packages are BOM-pinned; passkey/account routes are not wired yet.
    show_register=False,
    locales=(),
    default_locale="en",
    htmx_nav=False,
)


def install_platform_chrome(
    app: FastAPI, environments: list[Environment]
) -> PlatformConfig:
    """Install shared platform chrome with PunkRecords identity routes disabled."""
    install_platform(app, environments=environments, config=PLATFORM_CONFIG)
    return PLATFORM_CONFIG


def platform_request_context(*, current_path: str = "") -> dict[str, Any]:
    """Build per-request product shell context for the operator dashboard."""
    return build_platform_context(
        PLATFORM_CONFIG,
        user=None,
        current_path=current_path,
        locale="en",
    )
