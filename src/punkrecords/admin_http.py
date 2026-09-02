"""Admin JSON and statistics HTTP routes."""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .http_common import check_admin_auth, error_envelope
from .settings_store import load_settings, update_settings, validate_settings_payload
from .stats_store import load_rollups
from .store import AccountRepository


def admin_state_payload(repo: AccountRepository) -> dict[str, Any]:
    accounts = repo.admin_accounts_snapshot()
    active = next((account for account in accounts if account["active"]), None)
    return {
        "ok": True,
        "accounts_total": len(accounts),
        "accounts_enabled": sum(1 for account in accounts if account["enabled"]),
        "eligible_accounts": sum(1 for account in accounts if account["eligible"]),
        "cooldown_accounts": sum(
            1 for account in accounts if account["enabled"] and not account["eligible"]
        ),
        "active_account_id": active["account_id"] if active else None,
        "active_account_label": active["label"] if active else None,
        "stats": load_rollups(),
        "settings": load_settings(),
    }


def build_health_router(
    repo: AccountRepository,
    active_provider_id: Callable[[AccountRepository], str | None],
) -> APIRouter:
    router = APIRouter()

    @router.get("/healthz")
    async def healthz() -> JSONResponse:
        candidates = repo.list_proxy_candidates(active_provider_id(repo))
        return JSONResponse(
            {
                "ok": True,
                "accounts": len(repo.list_accounts()),
                "eligible_accounts": len(candidates),
            }
        )

    return router


def build_admin_router(repo: AccountRepository) -> APIRouter:
    router = APIRouter()

    @router.get("/_proxy/stats/summary")
    async def stats_summary(request: Request) -> JSONResponse:
        unauthorized = check_admin_auth(request)
        if unauthorized:
            return unauthorized
        return JSONResponse(load_rollups())

    @router.get("/_proxy/admin/state")
    async def admin_state(request: Request) -> JSONResponse:
        unauthorized = check_admin_auth(request)
        if unauthorized:
            return unauthorized
        return JSONResponse(admin_state_payload(repo))

    @router.get("/_proxy/admin/accounts")
    async def admin_accounts(request: Request) -> JSONResponse:
        unauthorized = check_admin_auth(request)
        if unauthorized:
            return unauthorized
        return JSONResponse({"data": repo.admin_accounts_snapshot()})

    @router.get("/_proxy/admin/requests")
    async def admin_requests(request: Request, limit: int = 100) -> JSONResponse:
        unauthorized = check_admin_auth(request)
        if unauthorized:
            return unauthorized
        stats_store_module = importlib.import_module("punkrecords.stats_store")
        return JSONResponse(
            {"data": stats_store_module.load_request_history(limit=limit)}
        )

    @router.get("/_proxy/admin/settings")
    async def admin_settings(request: Request) -> JSONResponse:
        unauthorized = check_admin_auth(request)
        if unauthorized:
            return unauthorized
        return JSONResponse(load_settings())

    @router.put("/_proxy/admin/settings")
    async def admin_settings_put(request: Request) -> JSONResponse:
        unauthorized = check_admin_auth(request)
        if unauthorized:
            return unauthorized
        raw_body = await request.body()
        try:
            payload = json.loads(raw_body.decode() or "{}")
        except json.JSONDecodeError:
            return JSONResponse(
                status_code=400,
                content=error_envelope(
                    "Request body is not valid JSON.",
                    error_type="invalid_request_error",
                    code="invalid_json",
                ),
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                status_code=400,
                content=error_envelope(
                    "Request body must be a JSON object.",
                    error_type="invalid_request_error",
                    code="invalid_payload",
                ),
            )
        try:
            validate_settings_payload(payload)
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content=error_envelope(
                    str(exc),
                    error_type="invalid_request_error",
                    code="invalid_settings",
                ),
            )
        return JSONResponse(update_settings(payload))

    @router.patch("/_proxy/admin/settings")
    async def admin_settings_patch(request: Request) -> JSONResponse:
        unauthorized = check_admin_auth(request)
        if unauthorized:
            return unauthorized
        raw_body = await request.body()
        try:
            payload = json.loads(raw_body.decode() or "{}")
        except json.JSONDecodeError:
            return JSONResponse(
                status_code=400,
                content=error_envelope(
                    "Request body is not valid JSON.",
                    error_type="invalid_request_error",
                    code="invalid_json",
                ),
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                status_code=400,
                content=error_envelope(
                    "Request body must be a JSON object.",
                    error_type="invalid_request_error",
                    code="invalid_payload",
                ),
            )
        try:
            validate_settings_payload(payload)
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content=error_envelope(
                    str(exc),
                    error_type="invalid_request_error",
                    code="invalid_settings",
                ),
            )
        return JSONResponse(update_settings(payload))

    return router
