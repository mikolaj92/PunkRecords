from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterator

fastapi_module = importlib.import_module("fastapi")
fastapi_responses_module = importlib.import_module("fastapi.responses")
fastapi_templating_module = importlib.import_module("fastapi.templating")
starlette_exceptions_module = importlib.import_module("starlette.exceptions")
uvicorn = importlib.import_module("uvicorn")

FastAPI = fastapi_module.FastAPI
Request = fastapi_module.Request
HTMLResponse = fastapi_responses_module.HTMLResponse
JSONResponse = fastapi_responses_module.JSONResponse
Response = fastapi_responses_module.Response
StreamingResponse = fastapi_responses_module.StreamingResponse
Jinja2Templates = fastapi_templating_module.Jinja2Templates
StarletteHTTPException = starlette_exceptions_module.HTTPException

from .failover import extract_retry_after
from .oauth import complete_browser_login, poll_device_login, start_browser_login, start_device_login, wait_browser_login_callback
from .paths import app_home
from .providers import BrowserLoginChallenge, DeviceLoginChallenge, OAuthError, all_local_routes, get_account_provider, get_provider, list_providers, providers_for_local_route, require_auth_provider, require_proxy_provider, require_usage_provider, supported_provider_metadata
from .routing import ordered_provider_ids, should_fallback_to_next_provider
from .settings_store import load_settings, update_settings, validate_settings_payload
from .stats_store import load_request_history, load_rollups, record_request
from .store import AccountRepository
from .transforms import RequestTransformContext, RequestTransformError, apply_request_transforms

def _error_envelope(message: str, *, error_type: str, code: str, param: str | None = None) -> dict[str, Any]:
    return {
        "error": {
            "message": message,
            "type": error_type,
            "param": param,
            "code": code,
        }
    }


def _admin_token() -> str:
    return os.getenv("PUNKRECORDS_ADMIN_TOKEN", "").strip()


def _template_root() -> Path:
    return Path(__file__).resolve().parent / "templates"


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
    provider_order = provider_order_value if isinstance(provider_order_value, list) else []
    route_overrides = route_overrides_value if isinstance(route_overrides_value, dict) else {}
    model_overrides = model_overrides_value if isinstance(model_overrides_value, dict) else {}
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
        "routing_provider_order": str(parsed.get("routing_provider_order", [""])[0]).strip(),
        "routing_route_overrides": str(parsed.get("routing_route_overrides", ["{}"])[0]).strip() or "{}",
        "routing_model_overrides": str(parsed.get("routing_model_overrides", ["{}"])[0]).strip() or "{}",
    }


def _settings_payload_from_form_state(form_state: dict[str, str]) -> dict[str, Any]:

    provider_order = [item.strip() for item in form_state["routing_provider_order"].replace("\n", ",").split(",") if item.strip()]
    if not form_state["proxy_host"]:
        raise ValueError("settings.proxy.host must be a non-empty string")
    try:
        proxy_port = int(form_state["proxy_port"])
    except ValueError as exc:
        raise ValueError("settings.proxy.port must be an integer between 1 and 65535") from exc
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
            "route_overrides": _coerce_settings_json(form_state["routing_route_overrides"], field_name="settings.routing.route_overrides"),
            "model_overrides": _coerce_settings_json(form_state["routing_model_overrides"], field_name="settings.routing.model_overrides"),
        },
    }
    return payload


def _dashboard_provider_metadata(provider_id: str) -> dict[str, str]:
    for provider in supported_provider_metadata():
        if provider.get("id") == provider_id:
            return provider
    return {"id": provider_id, "label": provider_id}


def _device_login_challenge_payload(challenge: DeviceLoginChallenge | None) -> dict[str, Any] | None:
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


def _browser_login_challenge_payload(challenge: BrowserLoginChallenge | None) -> dict[str, Any] | None:
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


def _admin_state_payload(repo: AccountRepository) -> dict[str, Any]:
    accounts = repo.admin_accounts_snapshot()
    active = next((account for account in accounts if account["active"]), None)
    eligible = sum(1 for account in accounts if account["eligible"])
    cooldown = sum(1 for account in accounts if account["enabled"] and not account["eligible"])
    return {
        "ok": True,
        "accounts_total": len(accounts),
        "accounts_enabled": sum(1 for account in accounts if account["enabled"]),
        "eligible_accounts": eligible,
        "cooldown_accounts": cooldown,
        "active_account_id": active["account_id"] if active else None,
        "active_account_label": active["label"] if active else None,
        "stats": load_rollups(),
        "settings": load_settings(),
    }


def _truncate_label(value: str, *, limit: int = 28) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 1]}…"


def _dashboard_charts(stats: dict[str, Any], requests: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_endpoint_value = stats.get("by_endpoint")
    by_endpoint = by_endpoint_value if isinstance(by_endpoint_value, dict) else {}
    endpoint_pairs = sorted(
        ((str(key), int(value)) for key, value in by_endpoint.items() if isinstance(value, int)),
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
            "values": [int(stats.get("success_count") or 0), int(stats.get("error_count") or 0)],
        },
        "recent_latency": {
            "title": "Recent latency",
            "kind": "line",
            "labels": [_truncate_label(str(item.get("endpoint") or "unknown"), limit=20) for item in recent_requests],
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
    state = _admin_state_payload(repo)
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
        "device_login_challenge": _device_login_challenge_payload(device_login_challenge),
        "browser_login_challenge": _browser_login_challenge_payload(browser_login_challenge),
        "charts": _dashboard_charts(state["stats"], requests),
        "provider_metadata": provider_metadata,
        "openai_provider": _dashboard_provider_metadata("openai-codex"),
    }


def _check_admin_auth(request: Request) -> JSONResponse | None:
    token = _admin_token()
    if not token:
        return None

    auth_header = request.headers.get("Authorization", "")
    x_admin_token = request.headers.get("X-Admin-Token", "")
    bearer = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""
    if x_admin_token == token or bearer == token:
        return None
    return JSONResponse(status_code=401, content=_error_envelope("Admin authentication required.", error_type="authentication_error", code="admin_auth_required"))


@dataclass
class ProxyResult:
    status_code: int
    body: bytes
    headers: dict[str, str]
    provider_id: str
    stream: bool = False
    usage: dict[str, int | None] | None = None


@dataclass
class StreamProxyResult:
    status_code: int
    headers: dict[str, str]
    upstream_response: BinaryIO
    account_id: str
    account_internal_id: str
    provider_id: str
    local_path: str


def get_fetch_account_usage() -> Any:
    def _fetch(account: Any) -> Any:
        provider = get_account_provider(account)
        if provider.usage is None:
            from .models import AccountUsage

            return account, AccountUsage(account_id=account.account_id, label=account.label, provider=account.provider, error="usage capability unavailable")
        return require_usage_provider(provider).fetch_account_usage(account)

    return _fetch


def _active_proxy_provider_id(repo: AccountRepository) -> str | None:
    active_account = repo.get_active()
    if active_account is not None:
        return active_account.provider
    accounts = repo.list_accounts()
    if accounts:
        return accounts[0].provider
    return None


def _provider_for_request(repo: AccountRepository, local_path: str, payload: dict[str, Any]) -> str | None:
    active_provider_id = _active_proxy_provider_id(repo)
    if active_provider_id:
        active_provider = require_proxy_provider(get_provider(active_provider_id))
        if active_provider.matches_request(local_path, payload):
            return active_provider_id

    seen: set[str] = set()
    for account in repo.list_accounts():
        provider_id = account.provider
        if provider_id in seen:
            continue
        seen.add(provider_id)
        provider = require_proxy_provider(get_provider(provider_id))
        if provider.matches_request(local_path, payload):
            return provider_id

    for provider in list_providers():
        if require_proxy_provider(provider).matches_request(local_path, payload):
            return provider.provider_id
    return None


def _providers_for_local_route(local_path: str, method: str) -> list[Any]:
    return list(providers_for_local_route(local_path, method))


def _proxy_max_attempts_env() -> int:
    return int(os.getenv("PUNKRECORDS_PROXY_MAX_ATTEMPTS", "3") or "3")


class ProxyServer:
    def __init__(self, server_address: tuple[str, int], handler_class: object | None, repo: AccountRepository) -> None:
        del handler_class
        self.host, self.server_port = server_address
        self.repo = repo
        self.max_attempts = _proxy_max_attempts_env()
        self.app = create_app(repo, max_attempts=self.max_attempts)
        self._server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                host=self.host,
                port=self.server_port,
                log_level="warning",
                lifespan="off",
            )
        )

    def serve_forever(self) -> None:
        self._server.run()

    def shutdown(self) -> None:
        self._server.should_exit = True

    def server_close(self) -> None:
        return


ProxyHandler = object


def _convert_response_if_needed(local_path: str, body: bytes) -> bytes:
    if local_path != "/v1/chat/completions":
        return body
    try:
        payload = json.loads(body.decode())
    except Exception:
        return body
    if not isinstance(payload, dict):
        return body
    if "choices" in payload or "error" in payload:
        return body
    from punkrecords.providers.openai_codex import responses_api_to_chat_completions
    converted = responses_api_to_chat_completions(payload)
    return json.dumps(converted).encode()


def _forward_request(local_path: str, account: Any, payload: dict[str, Any], idempotency_key: str) -> ProxyResult | StreamProxyResult:
    provider = require_proxy_provider(get_account_provider(account))
    stream = provider.is_streaming_request(payload)
    request_spec = provider.build_proxy_request(account, local_path=local_path, payload=payload, idempotency_key=idempotency_key)
    request = urllib.request.Request(request_spec.url, data=request_spec.data, headers=request_spec.headers, method=request_spec.method)
    try:
        response = urllib.request.urlopen(request, timeout=60.0)
        headers = dict(response.headers.items())
        headers["X-Proxy-Account-Id"] = account.account_id
        if stream:
            return StreamProxyResult(response.status, headers, response, account.account_id, account.id, account.provider, local_path)

        body = response.read()
        response.close()
        body = _convert_response_if_needed(local_path, body)
        usage = provider.proxy_extract_usage_from_body(body, local_path)
        return ProxyResult(response.status, body, headers, account.provider, stream=False, usage=usage)
    except urllib.error.HTTPError as exc:
        headers = dict(exc.headers.items())
        headers["X-Proxy-Account-Id"] = account.account_id
        return ProxyResult(exc.code, exc.read(), headers, account.provider)


def _forward_with_failover(repo: AccountRepository, provider_id: str | None, max_attempts: int, local_path: str, payload: dict[str, Any], idempotency_key: str) -> ProxyResult | StreamProxyResult:
    if not provider_id:
        return ProxyResult(404, json.dumps(_error_envelope("No provider could handle the requested endpoint.", error_type="invalid_request_error", code="provider_not_found")).encode(), {"Content-Type": "application/json"}, "unknown")
    candidates = repo.list_proxy_candidates(provider_id)[:max_attempts]
    if not candidates:
        return ProxyResult(503, json.dumps(_error_envelope("No eligible accounts are currently available.", error_type="server_error", code="no_eligible_accounts")).encode(), {"Content-Type": "application/json"}, provider_id or "unknown")

    last_result: ProxyResult | None = None
    for account in candidates:
        provider_info = get_account_provider(account)
        auth_provider = require_auth_provider(provider_info)
        proxy_provider = require_proxy_provider(provider_info)
        try:
            refreshed = auth_provider.maybe_refresh_account(account)
            repo.replace_account(refreshed, make_active=None)
            result = _forward_request(local_path, refreshed, payload, idempotency_key)
        except OAuthError as exc:
            repo.mark_proxy_failure(account.account_id, provider_id=account.provider, error=str(exc), cooldown_seconds=300)
            last_result = ProxyResult(503, json.dumps(_error_envelope(f"Account refresh failed: {exc}", error_type="server_error", code="account_refresh_failed")).encode(), {"Content-Type": "application/json"}, account.provider)
            continue
        except urllib.error.URLError as exc:
            repo.mark_proxy_failure(account.account_id, provider_id=account.provider, error=str(exc), cooldown_seconds=60)
            last_result = ProxyResult(502, json.dumps(_error_envelope(f"Upstream connection error: {exc}", error_type="server_error", code="upstream_connection_error")).encode(), {"Content-Type": "application/json"}, account.provider)
            continue

        if isinstance(result, StreamProxyResult):
            return result

        retryable, cooldown_seconds = proxy_provider.classify_proxy_failure(result.status_code, result.body)
        if retryable:
            retry_after = extract_retry_after(result.headers) or cooldown_seconds or 60
            repo.mark_proxy_failure(account.account_id, provider_id=account.provider, error=result.body.decode(errors="replace"), cooldown_seconds=retry_after)
            last_result = result
            continue

        repo.mark_proxy_success(account.account_id, provider_id=account.provider)
        return result

    return last_result or ProxyResult(503, json.dumps(_error_envelope("All eligible accounts failed.", error_type="server_error", code="all_accounts_failed")).encode(), {"Content-Type": "application/json"}, provider_id or "unknown")


def _route_with_provider_fallback(repo: AccountRepository, provider_ids: list[str], max_attempts: int, local_path: str, payloads_by_provider: dict[str, dict[str, Any]], idempotency_key: str) -> ProxyResult | StreamProxyResult:
    if not provider_ids:
        return ProxyResult(404, json.dumps(_error_envelope("No provider could handle the requested endpoint.", error_type="invalid_request_error", code="provider_not_found")).encode(), {"Content-Type": "application/json"}, "unknown")

    last_result: ProxyResult | None = None
    for provider_id in provider_ids:
        payload = payloads_by_provider.get(provider_id)
        if payload is None:
            continue
        result = _forward_with_failover(repo, provider_id, max_attempts, local_path, payload, idempotency_key)
        if isinstance(result, StreamProxyResult):
            return result
        last_result = result
        if should_fallback_to_next_provider(provider_id, result.status_code, result.body):
            continue
        return result
    return last_result or ProxyResult(503, json.dumps(_error_envelope("All configured providers failed.", error_type="server_error", code="all_providers_failed")).encode(), {"Content-Type": "application/json"}, "unknown")


def _build_response(result: ProxyResult) -> Response:
    headers = {key: value for key, value in result.headers.items() if key.lower() not in {"content-length", "transfer-encoding", "connection"}}
    return Response(content=result.body, status_code=result.status_code, headers=headers, media_type=headers.get("Content-Type"))


def _stream_generator(repo: AccountRepository, result: StreamProxyResult, started_at: float) -> Iterator[bytes]:
    account = repo.resolve_account(result.account_internal_id)
    tracker = require_proxy_provider(get_account_provider(account)).create_stream_usage_tracker(result.local_path)
    stream_error: str | None = None
    try:
        while True:
            chunk = result.upstream_response.readline()
            if not chunk:
                break
            tracker.feed(chunk)
            yield chunk
    except Exception as exc:
        stream_error = str(exc)
    finally:
        try:
            result.upstream_response.close()
        except Exception:
            pass

        if stream_error:
            repo.mark_proxy_failure(result.account_id, provider_id=result.provider_id, error=stream_error, cooldown_seconds=60)
        else:
            repo.mark_proxy_success(result.account_id, provider_id=result.provider_id)

        record_request(
            {
                "request_id": str(uuid.uuid4()),
                "endpoint": result.local_path,
                "stream": True,
                "status_code": result.status_code,
                "latency_ms": int((time.time() - started_at) * 1000),
                "account_id": result.account_id,
                "provider_id": result.provider_id,
                "input_tokens": tracker.usage.get("input_tokens"),
                "output_tokens": tracker.usage.get("output_tokens"),
                "total_tokens": tracker.usage.get("total_tokens"),
            }
        )


def create_app(repo: AccountRepository, *, max_attempts: int | None = None) -> FastAPI:
    attempts = max_attempts or _proxy_max_attempts_env()
    app = FastAPI()
    templates = Jinja2Templates(directory=str(_template_root()))

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:  # type: ignore[override]
        if exc.status_code == 404:
            return JSONResponse(status_code=404, content=_error_envelope("The requested endpoint does not exist.", error_type="invalid_request_error", code="not_found"))
        if exc.status_code == 405:
            headers = dict(exc.headers or {})
            return JSONResponse(
                status_code=405,
                content=_error_envelope("Method not allowed for this endpoint.", error_type="invalid_request_error", code="method_not_allowed"),
                headers=headers,
            )
        return JSONResponse(status_code=exc.status_code, content=_error_envelope(str(exc.detail), error_type="invalid_request_error", code="http_error"))

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        candidates = repo.list_proxy_candidates(_active_proxy_provider_id(repo))
        return JSONResponse({"ok": True, "accounts": len(repo.list_accounts()), "eligible_accounts": len(candidates)})

    @app.get("/_proxy/stats/summary")
    async def stats_summary(request: Request) -> JSONResponse:
        unauthorized = _check_admin_auth(request)
        if unauthorized:
            return unauthorized
        return JSONResponse(load_rollups())

    @app.get("/_proxy/admin/state")
    async def admin_state(request: Request) -> JSONResponse:
        unauthorized = _check_admin_auth(request)
        if unauthorized:
            return unauthorized
        return JSONResponse(_admin_state_payload(repo))

    @app.get("/_proxy/admin/accounts")
    async def admin_accounts(request: Request) -> JSONResponse:
        unauthorized = _check_admin_auth(request)
        if unauthorized:
            return unauthorized
        return JSONResponse({"data": repo.admin_accounts_snapshot()})

    @app.get("/_proxy/admin/requests")
    async def admin_requests(request: Request, limit: int = 100) -> JSONResponse:
        unauthorized = _check_admin_auth(request)
        if unauthorized:
            return unauthorized
        stats_store_module = importlib.import_module("punkrecords.stats_store")
        return JSONResponse({"data": stats_store_module.load_request_history(limit=limit)})

    @app.get("/_proxy/admin/settings")
    async def admin_settings(request: Request) -> JSONResponse:
        unauthorized = _check_admin_auth(request)
        if unauthorized:
            return unauthorized
        return JSONResponse(load_settings())

    @app.put("/_proxy/admin/settings")
    async def admin_settings_put(request: Request) -> JSONResponse:
        unauthorized = _check_admin_auth(request)
        if unauthorized:
            return unauthorized
        raw_body = await request.body()
        try:
            payload = json.loads(raw_body.decode() or "{}")
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content=_error_envelope("Request body is not valid JSON.", error_type="invalid_request_error", code="invalid_json"))
        if not isinstance(payload, dict):
            return JSONResponse(status_code=400, content=_error_envelope("Request body must be a JSON object.", error_type="invalid_request_error", code="invalid_payload"))
        try:
            validate_settings_payload(payload)
        except ValueError as exc:
            return JSONResponse(status_code=400, content=_error_envelope(str(exc), error_type="invalid_request_error", code="invalid_settings"))
        return JSONResponse(update_settings(payload))

    @app.patch("/_proxy/admin/settings")
    async def admin_settings_patch(request: Request) -> JSONResponse:
        unauthorized = _check_admin_auth(request)
        if unauthorized:
            return unauthorized
        raw_body = await request.body()
        try:
            payload = json.loads(raw_body.decode() or "{}")
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content=_error_envelope("Request body is not valid JSON.", error_type="invalid_request_error", code="invalid_json"))
        if not isinstance(payload, dict):
            return JSONResponse(status_code=400, content=_error_envelope("Request body must be a JSON object.", error_type="invalid_request_error", code="invalid_payload"))
        try:
            validate_settings_payload(payload)
        except ValueError as exc:
            return JSONResponse(status_code=400, content=_error_envelope(str(exc), error_type="invalid_request_error", code="invalid_settings"))
        return JSONResponse(update_settings(payload))

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        unauthorized = _check_admin_auth(request)
        if unauthorized:
            return unauthorized
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "page_title": "Dashboard",
                "provider_metadata": supported_provider_metadata(),
            },
        )

    @app.get("/_proxy/dashboard/overview", response_class=HTMLResponse)
    async def dashboard_overview(request: Request) -> HTMLResponse:
        unauthorized = _check_admin_auth(request)
        if unauthorized:
            return unauthorized
        return templates.TemplateResponse(
            request=request,
            name="partials/dashboard_overview.html",
            context=_dashboard_context(repo),
        )

    @app.get("/_proxy/dashboard/charts", response_class=HTMLResponse)
    async def dashboard_charts(request: Request) -> HTMLResponse:
        unauthorized = _check_admin_auth(request)
        if unauthorized:
            return unauthorized
        return templates.TemplateResponse(
            request=request,
            name="partials/dashboard_charts.html",
            context=_dashboard_context(repo),
        )

    @app.get("/_proxy/dashboard/requests", response_class=HTMLResponse)
    async def dashboard_requests(request: Request, limit: int = 20) -> HTMLResponse:
        unauthorized = _check_admin_auth(request)
        if unauthorized:
            return unauthorized
        return templates.TemplateResponse(
            request=request,
            name="partials/dashboard_requests.html",
            context=_dashboard_context(repo, request_limit=limit),
        )

    @app.get("/_proxy/dashboard/accounts", response_class=HTMLResponse)
    async def dashboard_accounts(request: Request) -> HTMLResponse:
        unauthorized = _check_admin_auth(request)
        if unauthorized:
            return unauthorized
        return templates.TemplateResponse(
            request=request,
            name="partials/dashboard_accounts.html",
            context=_dashboard_context(repo),
        )

    @app.post("/_proxy/dashboard/accounts/device/start", response_class=HTMLResponse)
    async def dashboard_accounts_device_start(request: Request) -> HTMLResponse:
        unauthorized = _check_admin_auth(request)
        if unauthorized:
            return unauthorized
        parsed = urllib.parse.parse_qs((await request.body()).decode(), keep_blank_values=True)
        label = str(parsed.get("label", [""])[0]).strip()
        try:
            challenge = start_device_login(provider_id="openai-codex", label=label or None)
        except (KeyError, OAuthError) as exc:
            return templates.TemplateResponse(
                request=request,
                name="partials/dashboard_accounts.html",
                context=_dashboard_context(repo, accounts_error=str(exc), account_add_label=label),
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

    @app.post("/_proxy/dashboard/accounts/device/poll", response_class=HTMLResponse)
    async def dashboard_accounts_device_poll(request: Request) -> HTMLResponse:
        unauthorized = _check_admin_auth(request)
        if unauthorized:
            return unauthorized
        challenge = _device_login_challenge_from_form(await request.body())
        try:
            result = poll_device_login(challenge)
        except OAuthError as exc:
            return templates.TemplateResponse(
                request=request,
                name="partials/dashboard_accounts.html",
                context=_dashboard_context(repo, accounts_error=str(exc), account_add_label=challenge.label or ""),
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
                accounts_notice=f"Added {result.account.label or result.account.account_id} via {result.base_url}.",
            ),
        )

    @app.post("/_proxy/dashboard/accounts/browser/start", response_class=HTMLResponse)
    async def dashboard_accounts_browser_start(request: Request) -> HTMLResponse:
        unauthorized = _check_admin_auth(request)
        if unauthorized:
            return unauthorized
        parsed = urllib.parse.parse_qs((await request.body()).decode(), keep_blank_values=True)
        label = str(parsed.get("label", [""])[0]).strip()
        try:
            challenge = start_browser_login(provider_id="openai-codex", label=label or None)
        except (KeyError, OAuthError) as exc:
            return templates.TemplateResponse(
                request=request,
                name="partials/dashboard_accounts.html",
                context=_dashboard_context(repo, accounts_error=str(exc), account_add_label=label),
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

    @app.post("/_proxy/dashboard/accounts/browser/complete", response_class=HTMLResponse)
    async def dashboard_accounts_browser_complete(request: Request) -> HTMLResponse:
        unauthorized = _check_admin_auth(request)
        if unauthorized:
            return unauthorized
        raw_body = await request.body()
        parsed = urllib.parse.parse_qs(raw_body.decode(), keep_blank_values=True)
        challenge = _browser_login_challenge_from_form(raw_body)
        try:
            authorization_code = wait_browser_login_callback(challenge.state, timeout=300.0)
            result = complete_browser_login(challenge, authorization_code)
        except OAuthError as exc:
            return templates.TemplateResponse(
                request=request,
                name="partials/dashboard_accounts.html",
                context=_dashboard_context(repo, accounts_error=str(exc), account_add_label=challenge.label or ""),
            )
        repo.upsert_account(result.account, make_active=True)
        return templates.TemplateResponse(
            request=request,
            name="partials/dashboard_accounts.html",
            context=_dashboard_context(
                repo,
                accounts_notice=f"Added {result.account.label or result.account.account_id} via {result.base_url}.",
            ),
        )

    @app.get("/_proxy/dashboard/settings", response_class=HTMLResponse)
    async def dashboard_settings(request: Request) -> HTMLResponse:
        unauthorized = _check_admin_auth(request)
        if unauthorized:
            return unauthorized
        return templates.TemplateResponse(
            request=request,
            name="partials/dashboard_settings.html",
            context=_dashboard_context(repo),
        )

    @app.post("/_proxy/dashboard/settings", response_class=HTMLResponse)
    async def dashboard_settings_save(request: Request) -> HTMLResponse:
        unauthorized = _check_admin_auth(request)
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
                context=_dashboard_context(repo, settings_error=str(exc), settings_form_state=submitted_form_state),
            )
        return templates.TemplateResponse(
            request=request,
            name="partials/dashboard_settings.html",
            context=_dashboard_context(repo, settings_notice="Settings saved.", settings_form_state=_settings_form_state(load_settings())),
        )

    @app.get("/v1/models")
    async def list_models() -> JSONResponse:
        combined: list[dict[str, Any]] = []
        for provider in list_providers():
            if provider.proxy is None:
                continue
            payload = provider.proxy.list_models()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                continue
            for item in data:
                if not isinstance(item, dict):
                    continue
                combined.append({**item, "provider": provider.provider_id})
        return JSONResponse({"object": "list", "data": combined})

    async def _handle_generation(local_path: str, method: str, request: Request) -> Response:
        raw_body = await request.body()
        request_id = str(uuid.uuid4())
        candidate_providers = _providers_for_local_route(local_path, method)
        parse_error: str | None = None
        payloads_by_provider: dict[str, dict[str, Any]] = {}
        headers = dict(request.headers.items())
        for provider in candidate_providers:
            try:
                candidate_payload = require_proxy_provider(provider).parse_local_request(local_path=local_path, method=method, raw_body=raw_body, headers=headers)
            except ValueError as exc:
                parse_error = str(exc)
                continue
            try:
                transformed = apply_request_transforms(
                    candidate_payload,
                    RequestTransformContext(
                        request_id=request_id,
                        local_path=local_path,
                        method=method,
                        provider_id=provider.provider_id,
                        headers=headers,
                    ),
                )
            except RequestTransformError as exc:
                return JSONResponse(
                    status_code=500,
                    content=_error_envelope(
                        f"Request transform failed in plugin {exc.plugin_id}: {exc}",
                        error_type="server_error",
                        code="request_transform_failed",
                    ),
                )
            if require_proxy_provider(provider).matches_request(local_path, transformed.payload):
                payloads_by_provider[provider.provider_id] = transformed.payload
        if not payloads_by_provider:
            if parse_error:
                code = "invalid_json" if "valid JSON" in parse_error else "invalid_payload"
                return JSONResponse(status_code=400, content=_error_envelope(parse_error, error_type="invalid_request_error", code=code))
            return JSONResponse(status_code=404, content=_error_envelope("No provider could handle the requested endpoint.", error_type="invalid_request_error", code="provider_not_found"))

        started_at = time.time()
        settings_attempts = load_settings().get("proxy", {}).get("max_attempts")
        effective_attempts = int(settings_attempts) if isinstance(settings_attempts, int) else attempts
        provider_ids = ordered_provider_ids(local_path, payloads_by_provider)
        result = _route_with_provider_fallback(repo, provider_ids, effective_attempts, local_path, payloads_by_provider, request.headers.get("Idempotency-Key") or str(uuid.uuid4()))

        if isinstance(result, StreamProxyResult):
            headers = {key: value for key, value in result.headers.items() if key.lower() not in {"content-length", "transfer-encoding", "connection"}}
            return StreamingResponse(_stream_generator(repo, result, started_at), status_code=result.status_code, headers=headers, media_type=headers.get("Content-Type"))

        usage = result.usage or {}
        record_request(
                {
                    "request_id": request_id,
                    "endpoint": local_path,
                    "stream": result.stream,
                    "status_code": result.status_code,
                    "latency_ms": int((time.time() - started_at) * 1000),
                    "account_id": result.headers.get("X-Proxy-Account-Id"),
                "provider_id": result.provider_id,
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "total_tokens": usage.get("total_tokens"),
            }
        )
        return _build_response(result)

    for route in all_local_routes():
        local_path = route.path
        method = route.method.upper()
        async def _generation_endpoint(request: Request, _local_path: str = local_path, _method: str = method) -> Response:
            return await _handle_generation(_local_path, _method, request)

        app.api_route(local_path, methods=[method])(_generation_endpoint)

    return app


def run_proxy_server(repo: AccountRepository, host: str, port: int) -> int:
    print("PunkRecords proxy")
    print(f"Runtime root:           {app_home()}")
    print(f"Proxy listening on http://{host}:{port}")
    base_url = f"http://{host}:{port}"
    for provider in list_providers():
        if provider.proxy is None:
            continue
        for label, url in provider.proxy.describe_local_routes(base_url=base_url):
            print(f"{label}: {url}")
    print(f"Health route:           http://{host}:{port}/healthz")
    print(f"Stats route:            http://{host}:{port}/_proxy/stats/summary")
    print(f"Admin state route:      http://{host}:{port}/_proxy/admin/state")
    print(f"Dashboard route:        http://{host}:{port}/")
    print(f"OpenAPI schema:         http://{host}:{port}/openapi.json")
    print(f"Swagger UI:             http://{host}:{port}/docs")
    print(f"ReDoc:                  http://{host}:{port}/redoc")
    uvicorn.run(create_app(repo), host=host, port=port, log_level="warning", lifespan="off")
    return 0
