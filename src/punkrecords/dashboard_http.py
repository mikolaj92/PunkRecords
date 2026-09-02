"""Server-rendered dashboard and OAuth form routes."""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from .admin_http import admin_state_payload
from .http_common import check_admin_auth
from .oauth import (
    complete_browser_login,
    poll_device_login,
    start_browser_login,
    start_device_login,
    wait_browser_login_callback,
)
from .platform_chrome import platform_request_context
from .providers import (
    BrowserLoginChallenge,
    DeviceLoginChallenge,
    OAuthError,
    supported_provider_metadata,
)
from .settings_store import load_settings, update_settings, validate_settings_payload
from .stats_store import load_request_history
from .store import AccountRepository


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def _settings_form_state(settings: dict[str, Any]) -> dict[str, str]:
    proxy_value = settings.get("proxy")
    routing_value = settings.get("routing")
    proxy = proxy_value if isinstance(proxy_value, dict) else {}
    routing = routing_value if isinstance(routing_value, dict) else {}
    provider_order_value = routing.get("provider_order")
    route_overrides_value = routing.get("route_overrides")
    model_overrides_value = routing.get("model_overrides")
    provider_order = (
        provider_order_value if isinstance(provider_order_value, list) else []
    )
    route_overrides = (
        route_overrides_value if isinstance(route_overrides_value, dict) else {}
    )
    model_overrides = (
        model_overrides_value if isinstance(model_overrides_value, dict) else {}
    )
    return {
        "proxy_host": str(proxy.get("host") or ""),
        "proxy_port": str(proxy.get("port") or ""),
        "proxy_max_attempts": str(proxy.get("max_attempts") or ""),
        "routing_provider_order": ", ".join(str(item) for item in provider_order),
        "routing_route_overrides": _json_dumps(route_overrides),
        "routing_model_overrides": _json_dumps(model_overrides),
    }


def _coerce_settings_json(raw_value: str, *, field_name: str) -> dict[str, Any]:
    text = raw_value.strip() or "{}"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{field_name} must be a JSON object.")
    return payload


def _settings_form_state_from_raw_body(raw_body: bytes) -> dict[str, str]:
    parsed = urllib.parse.parse_qs(raw_body.decode(), keep_blank_values=True)
    return {
        "proxy_host": str(parsed.get("proxy_host", [""])[0]).strip(),
        "proxy_port": str(parsed.get("proxy_port", [""])[0]).strip(),
        "proxy_max_attempts": str(parsed.get("proxy_max_attempts", [""])[0]).strip(),
        "routing_provider_order": str(
            parsed.get("routing_provider_order", [""])[0]
        ).strip(),
        "routing_route_overrides": str(
            parsed.get("routing_route_overrides", ["{}"])[0]
        ).strip()
        or "{}",
        "routing_model_overrides": str(
            parsed.get("routing_model_overrides", ["{}"])[0]
        ).strip()
        or "{}",
    }


def _settings_payload_from_form_state(form_state: dict[str, str]) -> dict[str, Any]:

    provider_order = [
        item.strip()
        for item in form_state["routing_provider_order"].replace("\n", ",").split(",")
        if item.strip()
    ]
    if not form_state["proxy_host"]:
        raise ValueError("settings.proxy.host must be a non-empty string")
    try:
        proxy_port = int(form_state["proxy_port"])
    except ValueError as exc:
        raise ValueError(
            "settings.proxy.port must be an integer between 1 and 65535"
        ) from exc
    try:
        proxy_max_attempts = int(form_state["proxy_max_attempts"])
    except ValueError as exc:
        raise ValueError("settings.proxy.max_attempts must be an integer >= 1") from exc

    payload = {
        "proxy": {
            "host": form_state["proxy_host"],
            "port": proxy_port,
            "max_attempts": proxy_max_attempts,
        },
        "routing": {
            "provider_order": provider_order,
            "route_overrides": _coerce_settings_json(
                form_state["routing_route_overrides"],
                field_name="settings.routing.route_overrides",
            ),
            "model_overrides": _coerce_settings_json(
                form_state["routing_model_overrides"],
                field_name="settings.routing.model_overrides",
            ),
        },
    }
    return payload


def _dashboard_provider_metadata(provider_id: str) -> dict[str, str]:
    for provider in supported_provider_metadata():
        if provider.get("id") == provider_id:
            return provider
    return {"id": provider_id, "label": provider_id}


def _device_login_challenge_payload(
    challenge: DeviceLoginChallenge | None,
) -> dict[str, Any] | None:
    if challenge is None:
        return None
    return {
        "provider_id": challenge.provider_id,
        "device_auth_id": challenge.device_auth_id,
        "user_code": challenge.user_code,
        "verification_url": challenge.verification_url,
        "poll_interval": challenge.poll_interval,
        "issuer": challenge.issuer,
        "token_url": challenge.token_url,
        "client_id": challenge.client_id,
        "label": challenge.label or "",
    }


def _device_login_challenge_from_form(raw_body: bytes) -> DeviceLoginChallenge:
    parsed = urllib.parse.parse_qs(raw_body.decode(), keep_blank_values=True)
    return DeviceLoginChallenge(
        provider_id=str(parsed.get("provider_id", [""])[0]).strip(),
        device_auth_id=str(parsed.get("device_auth_id", [""])[0]).strip(),
        user_code=str(parsed.get("user_code", [""])[0]).strip(),
        verification_url=str(parsed.get("verification_url", [""])[0]).strip(),
        poll_interval=int(str(parsed.get("poll_interval", ["5"])[0]).strip() or "5"),
        issuer=str(parsed.get("issuer", [""])[0]).strip(),
        token_url=str(parsed.get("token_url", [""])[0]).strip(),
        client_id=str(parsed.get("client_id", [""])[0]).strip(),
        label=str(parsed.get("label", [""])[0]).strip() or None,
    )


def _browser_login_challenge_payload(
    challenge: BrowserLoginChallenge | None,
) -> dict[str, Any] | None:
    if challenge is None:
        return None
    return {
        "provider_id": challenge.provider_id,
        "authorize_url": challenge.authorize_url,
        "redirect_uri": challenge.redirect_uri,
        "code_verifier": challenge.code_verifier,
        "state": challenge.state,
        "issuer": challenge.issuer,
        "token_url": challenge.token_url,
        "client_id": challenge.client_id,
        "label": challenge.label or "",
    }


def _browser_login_challenge_from_form(raw_body: bytes) -> BrowserLoginChallenge:
    parsed = urllib.parse.parse_qs(raw_body.decode(), keep_blank_values=True)
    return BrowserLoginChallenge(
        provider_id=str(parsed.get("provider_id", [""])[0]).strip(),
        authorize_url=str(parsed.get("authorize_url", [""])[0]).strip(),
        redirect_uri=str(parsed.get("redirect_uri", [""])[0]).strip(),
        code_verifier=str(parsed.get("code_verifier", [""])[0]).strip(),
        state=str(parsed.get("state", [""])[0]).strip(),
        issuer=str(parsed.get("issuer", [""])[0]).strip(),
        token_url=str(parsed.get("token_url", [""])[0]).strip(),
        client_id=str(parsed.get("client_id", [""])[0]).strip(),
        label=str(parsed.get("label", [""])[0]).strip() or None,
    )


def _truncate_label(value: str, *, limit: int = 28) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 1]}…"


def _dashboard_charts(
    stats: dict[str, Any], requests: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    by_endpoint_value = stats.get("by_endpoint")
    by_endpoint = by_endpoint_value if isinstance(by_endpoint_value, dict) else {}
    endpoint_pairs = sorted(
        (
            (str(key), int(value))
            for key, value in by_endpoint.items()
            if isinstance(value, int)
        ),
        key=lambda item: (-item[1], item[0]),
    )[:8]
    recent_requests = list(reversed(requests[:10]))
    return {
        "requests_by_endpoint": {
            "title": "Requests by endpoint",
            "kind": "bar",
            "labels": [_truncate_label(label) for label, _ in endpoint_pairs],
            "values": [value for _, value in endpoint_pairs],
        },
        "status_mix": {
            "title": "Success and errors",
            "kind": "doughnut",
            "labels": ["Success", "Errors"],
            "values": [
                int(stats.get("success_count") or 0),
                int(stats.get("error_count") or 0),
            ],
        },
        "recent_latency": {
            "title": "Recent latency",
            "kind": "line",
            "labels": [
                _truncate_label(str(item.get("endpoint") or "unknown"), limit=20)
                for item in recent_requests
            ],
            "values": [int(item.get("latency_ms") or 0) for item in recent_requests],
        },
    }


def _dashboard_context(
    repo: AccountRepository,
    *,
    request_limit: int = 20,
    settings_notice: str | None = None,
    settings_error: str | None = None,
    settings_form_state: dict[str, str] | None = None,
    accounts_notice: str | None = None,
    accounts_error: str | None = None,
    account_add_label: str = "",
    device_login_challenge: DeviceLoginChallenge | None = None,
    browser_login_challenge: BrowserLoginChallenge | None = None,
) -> dict[str, Any]:
    state = admin_state_payload(repo)
    settings = state["settings"]
    requests = load_request_history(limit=request_limit)
    accounts = repo.admin_accounts_snapshot()
    active_account = next((account for account in accounts if account["active"]), None)
    provider_metadata = supported_provider_metadata()
    return {
        "state": state,
        "stats": state["stats"],
        "settings": settings,
        "requests": requests,
        "accounts": accounts,
        "active_account": active_account,
        "settings_notice": settings_notice,
        "settings_error": settings_error,
        "settings_form": settings_form_state or _settings_form_state(settings),
        "accounts_notice": accounts_notice,
        "accounts_error": accounts_error,
        "account_add_label": account_add_label,
        "device_login_challenge": _device_login_challenge_payload(
            device_login_challenge
        ),
        "browser_login_challenge": _browser_login_challenge_payload(
            browser_login_challenge
        ),
        "charts": _dashboard_charts(state["stats"], requests),
        "provider_metadata": provider_metadata,
        "openai_provider": _dashboard_provider_metadata("openai-codex"),
    }


def build_dashboard_router(
    repo: AccountRepository, templates: Jinja2Templates
) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> Response:
        unauthorized = check_admin_auth(request)
        if unauthorized:
            return unauthorized
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "page_title": "Dashboard",
                "nav_active": "dashboard",
                "provider_metadata": supported_provider_metadata(),
                **platform_request_context(current_path=request.url.path),
            },
        )

    @router.get("/_proxy/dashboard/overview", response_class=HTMLResponse)
    async def dashboard_overview(request: Request) -> Response:
        unauthorized = check_admin_auth(request)
        if unauthorized:
            return unauthorized
        return templates.TemplateResponse(
            request=request,
            name="partials/dashboard_overview.html",
            context=_dashboard_context(repo),
        )

    @router.get("/_proxy/dashboard/charts", response_class=HTMLResponse)
    async def dashboard_charts(request: Request) -> Response:
        unauthorized = check_admin_auth(request)
        if unauthorized:
            return unauthorized
        return templates.TemplateResponse(
            request=request,
            name="partials/dashboard_charts.html",
            context=_dashboard_context(repo),
        )

    @router.get("/_proxy/dashboard/requests", response_class=HTMLResponse)
    async def dashboard_requests(request: Request, limit: int = 20) -> Response:
        unauthorized = check_admin_auth(request)
        if unauthorized:
            return unauthorized
        return templates.TemplateResponse(
            request=request,
            name="partials/dashboard_requests.html",
            context=_dashboard_context(repo, request_limit=limit),
        )

    @router.get("/_proxy/dashboard/accounts", response_class=HTMLResponse)
    async def dashboard_accounts(request: Request) -> Response:
        unauthorized = check_admin_auth(request)
        if unauthorized:
            return unauthorized
        return templates.TemplateResponse(
            request=request,
            name="partials/dashboard_accounts.html",
            context=_dashboard_context(repo),
        )

    @router.post("/_proxy/dashboard/accounts/device/start", response_class=HTMLResponse)
    async def dashboard_accounts_device_start(request: Request) -> Response:
        unauthorized = check_admin_auth(request)
        if unauthorized:
            return unauthorized
        parsed = urllib.parse.parse_qs(
            (await request.body()).decode(), keep_blank_values=True
        )
        label = str(parsed.get("label", [""])[0]).strip()
        try:
            challenge = start_device_login(
                provider_id="openai-codex", label=label or None
            )
        except (KeyError, OAuthError) as exc:
            return templates.TemplateResponse(
                request=request,
                name="partials/dashboard_accounts.html",
                context=_dashboard_context(
                    repo, accounts_error=str(exc), account_add_label=label
                ),
            )
        return templates.TemplateResponse(
            request=request,
            name="partials/dashboard_accounts.html",
            context=_dashboard_context(
                repo,
                accounts_notice="Open the verification link and enter the code to complete sign-in.",
                account_add_label=label,
                device_login_challenge=challenge,
            ),
        )

    @router.post("/_proxy/dashboard/accounts/device/poll", response_class=HTMLResponse)
    async def dashboard_accounts_device_poll(request: Request) -> Response:
        unauthorized = check_admin_auth(request)
        if unauthorized:
            return unauthorized
        challenge = _device_login_challenge_from_form(await request.body())
        try:
            result = poll_device_login(challenge)
        except OAuthError as exc:
            return templates.TemplateResponse(
                request=request,
                name="partials/dashboard_accounts.html",
                context=_dashboard_context(
                    repo,
                    accounts_error=str(exc),
                    account_add_label=challenge.label or "",
                ),
            )
        if result is None:
            return templates.TemplateResponse(
                request=request,
                name="partials/dashboard_accounts.html",
                context=_dashboard_context(
                    repo,
                    accounts_notice="Waiting for device login confirmation.",
                    account_add_label=challenge.label or "",
                    device_login_challenge=challenge,
                ),
            )
        repo.upsert_account(result.account, make_active=True)
        return templates.TemplateResponse(
            request=request,
            name="partials/dashboard_accounts.html",
            context=_dashboard_context(
                repo,
                accounts_notice=f"Added {result.account.display_name or result.account.external_id} via {result.base_url}.",
            ),
        )

    @router.post(
        "/_proxy/dashboard/accounts/browser/start", response_class=HTMLResponse
    )
    async def dashboard_accounts_browser_start(request: Request) -> Response:
        unauthorized = check_admin_auth(request)
        if unauthorized:
            return unauthorized
        parsed = urllib.parse.parse_qs(
            (await request.body()).decode(), keep_blank_values=True
        )
        label = str(parsed.get("label", [""])[0]).strip()
        try:
            challenge = start_browser_login(
                provider_id="openai-codex", label=label or None
            )
        except (KeyError, OAuthError) as exc:
            return templates.TemplateResponse(
                request=request,
                name="partials/dashboard_accounts.html",
                context=_dashboard_context(
                    repo, accounts_error=str(exc), account_add_label=label
                ),
            )
        return templates.TemplateResponse(
            request=request,
            name="partials/dashboard_accounts.html",
            context=_dashboard_context(
                repo,
                accounts_notice="Copy and open the URL in your browser, then paste the callback URL below.",
                account_add_label=label,
                browser_login_challenge=challenge,
            ),
        )

    @router.post(
        "/_proxy/dashboard/accounts/browser/complete", response_class=HTMLResponse
    )
    async def dashboard_accounts_browser_complete(request: Request) -> Response:
        unauthorized = check_admin_auth(request)
        if unauthorized:
            return unauthorized
        raw_body = await request.body()
        challenge = _browser_login_challenge_from_form(raw_body)
        try:
            authorization_code = wait_browser_login_callback(challenge, timeout=300.0)
            result = complete_browser_login(challenge, authorization_code)
        except OAuthError as exc:
            return templates.TemplateResponse(
                request=request,
                name="partials/dashboard_accounts.html",
                context=_dashboard_context(
                    repo,
                    accounts_error=str(exc),
                    account_add_label=challenge.label or "",
                ),
            )
        repo.upsert_account(result.account, make_active=True)
        return templates.TemplateResponse(
            request=request,
            name="partials/dashboard_accounts.html",
            context=_dashboard_context(
                repo,
                accounts_notice=f"Added {result.account.display_name or result.account.external_id} via {result.base_url}.",
            ),
        )

    @router.get("/_proxy/dashboard/settings", response_class=HTMLResponse)
    async def dashboard_settings(request: Request) -> Response:
        unauthorized = check_admin_auth(request)
        if unauthorized:
            return unauthorized
        return templates.TemplateResponse(
            request=request,
            name="partials/dashboard_settings.html",
            context=_dashboard_context(repo),
        )

    @router.post("/_proxy/dashboard/settings", response_class=HTMLResponse)
    async def dashboard_settings_save(request: Request) -> Response:
        unauthorized = check_admin_auth(request)
        if unauthorized:
            return unauthorized
        submitted_form_state = _settings_form_state_from_raw_body(await request.body())
        try:
            payload = _settings_payload_from_form_state(submitted_form_state)
            validate_settings_payload(payload)
            update_settings(payload)
        except ValueError as exc:
            return templates.TemplateResponse(
                request=request,
                name="partials/dashboard_settings.html",
                context=_dashboard_context(
                    repo,
                    settings_error=str(exc),
                    settings_form_state=submitted_form_state,
                ),
            )
        return templates.TemplateResponse(
            request=request,
            name="partials/dashboard_settings.html",
            context=_dashboard_context(
                repo,
                settings_notice="Settings saved.",
                settings_form_state=_settings_form_state(load_settings()),
            ),
        )

    return router
