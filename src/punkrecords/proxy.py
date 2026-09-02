from __future__ import annotations

import importlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

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

from app_factory.cdn import extend_manifest, install_manifest

from .admin_http import build_admin_router, build_health_router
from .dashboard_http import build_dashboard_router
from .failover import extract_retry_after
from .http_common import error_envelope as _error_envelope
from .paths import app_home
from .platform_chrome import install_platform_chrome
from .providers import (
    OAuthError,
    all_local_routes,
    get_account_provider,
    get_provider,
    list_providers,
    providers_for_local_route,
    require_auth_provider,
    require_proxy_provider,
    require_usage_provider,
)
from .routing import ordered_provider_ids, should_fallback_to_next_provider
from .settings_store import load_settings
from .stats_store import record_request
from .store import AccountRepository
from .transforms import (
    RequestTransformContext,
    RequestTransformError,
    apply_request_transforms,
)


def _template_root() -> Path:
    return Path(__file__).resolve().parent / "templates"


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

            return account, AccountUsage(
                external_id=account.external_id,
                display_name=account.display_name,
                provider=account.provider,
                error="usage capability unavailable",
            )
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


def _provider_for_request(
    repo: AccountRepository, local_path: str, payload: dict[str, Any]
) -> str | None:
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
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: object | None,
        repo: AccountRepository,
    ) -> None:
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


def _perform_proxy_request(
    spec: Any, stream: bool = False, timeout: float = 60.0
) -> tuple[int, Any, dict[str, str]]:
    request = urllib.request.Request(
        spec.url, data=spec.data, headers=spec.headers, method=spec.method
    )
    response = urllib.request.urlopen(request, timeout=timeout)
    headers = dict(response.headers.items())
    if stream:
        return response.status, response, headers
    body = response.read()
    response.close()
    return response.status, body, headers


def _forward_request(
    local_path: str, account: Any, payload: dict[str, Any], idempotency_key: str
) -> ProxyResult | StreamProxyResult:
    provider = require_proxy_provider(get_account_provider(account))
    stream = provider.is_streaming_request(payload)
    request_spec = provider.build_proxy_request(
        account, local_path=local_path, payload=payload, idempotency_key=idempotency_key
    )
    try:
        status_code, payload_or_response, headers = _perform_proxy_request(
            request_spec, stream=stream, timeout=60.0
        )
        headers = dict(headers)
        headers["X-Proxy-Account-Id"] = account.external_id
        if stream:
            return StreamProxyResult(
                status_code,
                headers,
                payload_or_response,
                account.external_id,
                account.id,
                account.provider,
                local_path,
            )

        body = _convert_response_if_needed(local_path, payload_or_response)
        usage = provider.proxy_extract_usage_from_body(body, local_path)
        return ProxyResult(
            status_code, body, headers, account.provider, stream=False, usage=usage
        )
    except urllib.error.HTTPError as exc:
        headers = dict(exc.headers.items())
        headers["X-Proxy-Account-Id"] = account.external_id
        return ProxyResult(exc.code, exc.read(), headers, account.provider)


def _forward_with_failover(
    repo: AccountRepository,
    provider_id: str | None,
    max_attempts: int,
    local_path: str,
    payload: dict[str, Any],
    idempotency_key: str,
) -> ProxyResult | StreamProxyResult:
    if not provider_id:
        return ProxyResult(
            404,
            json.dumps(
                _error_envelope(
                    "No provider could handle the requested endpoint.",
                    error_type="invalid_request_error",
                    code="provider_not_found",
                )
            ).encode(),
            {"Content-Type": "application/json"},
            "unknown",
        )
    candidates = repo.list_proxy_candidates(provider_id)[:max_attempts]
    if not candidates:
        return ProxyResult(
            503,
            json.dumps(
                _error_envelope(
                    "No eligible accounts are currently available.",
                    error_type="server_error",
                    code="no_eligible_accounts",
                )
            ).encode(),
            {"Content-Type": "application/json"},
            provider_id or "unknown",
        )

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
            repo.mark_proxy_failure(
                account.external_id,
                provider_id=account.provider,
                error=str(exc),
                cooldown_seconds=300,
            )
            last_result = ProxyResult(
                503,
                json.dumps(
                    _error_envelope(
                        f"Account refresh failed: {exc}",
                        error_type="server_error",
                        code="account_refresh_failed",
                    )
                ).encode(),
                {"Content-Type": "application/json"},
                account.provider,
            )
            continue
        except urllib.error.URLError as exc:
            repo.mark_proxy_failure(
                account.external_id,
                provider_id=account.provider,
                error=str(exc),
                cooldown_seconds=60,
            )
            last_result = ProxyResult(
                502,
                json.dumps(
                    _error_envelope(
                        f"Upstream connection error: {exc}",
                        error_type="server_error",
                        code="upstream_connection_error",
                    )
                ).encode(),
                {"Content-Type": "application/json"},
                account.provider,
            )
            continue

        if isinstance(result, StreamProxyResult):
            return result

        retryable, cooldown_seconds = proxy_provider.classify_proxy_failure(
            result.status_code, result.body
        )
        if retryable:
            retry_after = extract_retry_after(result.headers) or cooldown_seconds or 60
            repo.mark_proxy_failure(
                account.external_id,
                provider_id=account.provider,
                error=result.body.decode(errors="replace"),
                cooldown_seconds=retry_after,
            )
            last_result = result
            continue

        repo.mark_proxy_success(account.external_id, provider_id=account.provider)
        return result

    return last_result or ProxyResult(
        503,
        json.dumps(
            _error_envelope(
                "All eligible accounts failed.",
                error_type="server_error",
                code="all_accounts_failed",
            )
        ).encode(),
        {"Content-Type": "application/json"},
        provider_id or "unknown",
    )


def _route_with_provider_fallback(
    repo: AccountRepository,
    provider_ids: list[str],
    max_attempts: int,
    local_path: str,
    payloads_by_provider: dict[str, dict[str, Any]],
    idempotency_key: str,
) -> ProxyResult | StreamProxyResult:
    if not provider_ids:
        return ProxyResult(
            404,
            json.dumps(
                _error_envelope(
                    "No provider could handle the requested endpoint.",
                    error_type="invalid_request_error",
                    code="provider_not_found",
                )
            ).encode(),
            {"Content-Type": "application/json"},
            "unknown",
        )

    last_result: ProxyResult | None = None
    for provider_id in provider_ids:
        payload = payloads_by_provider.get(provider_id)
        if payload is None:
            continue
        result = _forward_with_failover(
            repo, provider_id, max_attempts, local_path, payload, idempotency_key
        )
        if isinstance(result, StreamProxyResult):
            return result
        last_result = result
        if should_fallback_to_next_provider(
            provider_id, result.status_code, result.body
        ):
            continue
        return result
    return last_result or ProxyResult(
        503,
        json.dumps(
            _error_envelope(
                "All configured providers failed.",
                error_type="server_error",
                code="all_providers_failed",
            )
        ).encode(),
        {"Content-Type": "application/json"},
        "unknown",
    )


def _build_response(result: ProxyResult) -> Response:
    headers = {
        key: value
        for key, value in result.headers.items()
        if key.lower() not in {"content-length", "transfer-encoding", "connection"}
    }
    return Response(
        content=result.body,
        status_code=result.status_code,
        headers=headers,
        media_type=headers.get("Content-Type"),
    )


def _stream_generator(
    repo: AccountRepository, result: StreamProxyResult, started_at: float
) -> Iterator[bytes]:
    account = repo.resolve_account(result.account_internal_id)
    tracker = require_proxy_provider(
        get_account_provider(account)
    ).create_stream_usage_tracker(result.local_path)
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
            repo.mark_proxy_failure(
                result.account_id,
                provider_id=result.provider_id,
                error=stream_error,
                cooldown_seconds=60,
            )
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


def _install_proxy_routes(app: FastAPI, repo: AccountRepository, attempts: int) -> None:
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

    async def _handle_generation(
        local_path: str, method: str, request: Request
    ) -> Response:
        raw_body = await request.body()
        request_id = str(uuid.uuid4())
        candidate_providers = _providers_for_local_route(local_path, method)
        parse_error: str | None = None
        payloads_by_provider: dict[str, dict[str, Any]] = {}
        headers = dict(request.headers.items())
        for provider in candidate_providers:
            try:
                candidate_payload = require_proxy_provider(
                    provider
                ).parse_local_request(
                    local_path=local_path,
                    method=method,
                    raw_body=raw_body,
                    headers=headers,
                )
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
            if require_proxy_provider(provider).matches_request(
                local_path, transformed.payload
            ):
                payloads_by_provider[provider.provider_id] = transformed.payload
        if not payloads_by_provider:
            if parse_error:
                code = (
                    "invalid_json" if "valid JSON" in parse_error else "invalid_payload"
                )
                return JSONResponse(
                    status_code=400,
                    content=_error_envelope(
                        parse_error, error_type="invalid_request_error", code=code
                    ),
                )
            return JSONResponse(
                status_code=404,
                content=_error_envelope(
                    "No provider could handle the requested endpoint.",
                    error_type="invalid_request_error",
                    code="provider_not_found",
                ),
            )

        started_at = time.time()
        settings_attempts = load_settings().get("proxy", {}).get("max_attempts")
        effective_attempts = (
            int(settings_attempts) if isinstance(settings_attempts, int) else attempts
        )
        provider_ids = ordered_provider_ids(local_path, payloads_by_provider)
        result = _route_with_provider_fallback(
            repo,
            provider_ids,
            effective_attempts,
            local_path,
            payloads_by_provider,
            request.headers.get("Idempotency-Key") or str(uuid.uuid4()),
        )

        if isinstance(result, StreamProxyResult):
            headers = {
                key: value
                for key, value in result.headers.items()
                if key.lower()
                not in {"content-length", "transfer-encoding", "connection"}
            }
            return StreamingResponse(
                _stream_generator(repo, result, started_at),
                status_code=result.status_code,
                headers=headers,
                media_type=headers.get("Content-Type"),
            )

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

        async def _generation_endpoint(
            request: Request, _local_path: str = local_path, _method: str = method
        ) -> Response:
            return await _handle_generation(_local_path, _method, request)

        app.api_route(local_path, methods=[method])(_generation_endpoint)


def create_app(repo: AccountRepository, *, max_attempts: int | None = None) -> FastAPI:
    attempts = max_attempts or _proxy_max_attempts_env()
    app = FastAPI()
    install_manifest(extend_manifest(["chartjs"]))
    templates = Jinja2Templates(directory=str(_template_root()))
    install_platform_chrome(app, [templates.env])

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:  # type: ignore[override]
        if exc.status_code == 404:
            return JSONResponse(
                status_code=404,
                content=_error_envelope(
                    "The requested endpoint does not exist.",
                    error_type="invalid_request_error",
                    code="not_found",
                ),
            )
        if exc.status_code == 405:
            headers = dict(exc.headers or {})
            return JSONResponse(
                status_code=405,
                content=_error_envelope(
                    "Method not allowed for this endpoint.",
                    error_type="invalid_request_error",
                    code="method_not_allowed",
                ),
                headers=headers,
            )
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_envelope(
                str(exc.detail), error_type="invalid_request_error", code="http_error"
            ),
        )

    app.include_router(build_health_router(repo, _active_proxy_provider_id))
    app.include_router(build_admin_router(repo))
    app.include_router(build_dashboard_router(repo, templates))
    _install_proxy_routes(app, repo, attempts)

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
    uvicorn.run(
        create_app(repo), host=host, port=port, log_level="warning", lifespan="off"
    )
    return 0
