from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
import importlib
from dataclasses import dataclass
from typing import Any, BinaryIO, Iterator

fastapi_module = importlib.import_module("fastapi")
fastapi_responses_module = importlib.import_module("fastapi.responses")
starlette_exceptions_module = importlib.import_module("starlette.exceptions")
uvicorn = importlib.import_module("uvicorn")

FastAPI = fastapi_module.FastAPI
Request = fastapi_module.Request
JSONResponse = fastapi_responses_module.JSONResponse
Response = fastapi_responses_module.Response
StreamingResponse = fastapi_responses_module.StreamingResponse
StarletteHTTPException = starlette_exceptions_module.HTTPException

from .failover import classify_status, extract_retry_after, is_streaming_request
from .oauth import DEFAULT_CODEX_BASE_URL, DEFAULT_HEADERS, OAuthError, maybe_refresh_account
from .paths import app_home, hermes_auth_path
from .settings_store import load_settings, update_settings, validate_settings_payload
from .stats_store import load_rollups, record_request
from .store import AccountRepository

ROUTE_MAP = {
    "/v1/responses": "/responses",
    "/v1/chat/completions": "/chat/completions",
    "/v1/embeddings": "/embeddings",
}


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
    return os.getenv("HERMES_CODEX_ADMIN_TOKEN", "").strip()


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


def proxy_upstream_base() -> str:
    override = os.getenv("HERMES_CODEX_PROXY_UPSTREAM_BASE", "").strip()
    if override:
        return override.rstrip("/")
    return DEFAULT_CODEX_BASE_URL.rstrip("/")


def proxy_upstream_url(local_path: str) -> str:
    legacy_override = os.getenv("HERMES_CODEX_PROXY_UPSTREAM_URL", "").strip()
    if local_path == "/v1/responses" and legacy_override:
        return legacy_override

    path = ROUTE_MAP.get(local_path)
    if not path:
        raise KeyError(local_path)

    specific_key = f"HERMES_CODEX_PROXY_UPSTREAM_{local_path.strip('/').replace('/', '_').upper()}_URL"
    override = os.getenv(specific_key, "").strip()
    if override:
        return override
    return proxy_upstream_base() + path


@dataclass
class ProxyResult:
    status_code: int
    body: bytes
    headers: dict[str, str]
    stream: bool = False
    usage: dict[str, int | None] | None = None


@dataclass
class StreamProxyResult:
    status_code: int
    headers: dict[str, str]
    upstream_response: BinaryIO
    account_id: str
    local_path: str


class StreamUsageTracker:
    def __init__(self, local_path: str) -> None:
        self.local_path = local_path
        self.buffer = ""
        self.usage: dict[str, int | None] = {"input_tokens": None, "output_tokens": None, "total_tokens": None}

    def feed(self, chunk: bytes) -> None:
        self.buffer += chunk.decode(errors="replace")
        while "\n\n" in self.buffer:
            raw_event, self.buffer = self.buffer.split("\n\n", 1)
            self._parse_event(raw_event)

    def _parse_event(self, raw_event: str) -> None:
        lines = [line for line in raw_event.splitlines() if line]
        if not lines:
            return
        event_name = None
        data_parts: list[str] = []
        for line in lines:
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_parts.append(line.removeprefix("data:").strip())
        if not data_parts:
            return
        data_payload = "\n".join(data_parts)
        if data_payload == "[DONE]":
            return
        try:
            payload = json.loads(data_payload)
        except json.JSONDecodeError:
            return

        if self.local_path == "/v1/chat/completions":
            usage = payload.get("usage") if isinstance(payload, dict) else None
            if isinstance(usage, dict):
                self.usage = {
                    "input_tokens": int(usage["prompt_tokens"]) if isinstance(usage.get("prompt_tokens"), int) else None,
                    "output_tokens": int(usage["completion_tokens"]) if isinstance(usage.get("completion_tokens"), int) else None,
                    "total_tokens": int(usage["total_tokens"]) if isinstance(usage.get("total_tokens"), int) else None,
                }
            return

        if event_name == "response.completed" and isinstance(payload, dict):
            response = payload.get("response")
            if isinstance(response, dict):
                self.usage = _extract_usage(response, "/v1/responses")


class CodexProxyServer:
    def __init__(self, server_address: tuple[str, int], handler_class: object | None, repo: AccountRepository) -> None:
        del handler_class
        self.host, self.server_port = server_address
        self.repo = repo
        self.max_attempts = int(os.getenv("HERMES_CODEX_PROXY_MAX_ATTEMPTS", "3") or "3")
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


CodexProxyHandler = object


def _extract_usage(payload: dict[str, Any], local_path: str) -> dict[str, int | None]:
    if local_path == "/v1/embeddings":
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            return {"input_tokens": None, "output_tokens": None, "total_tokens": None}
        return {
            "input_tokens": int(usage["prompt_tokens"]) if isinstance(usage.get("prompt_tokens"), int) else None,
            "output_tokens": 0,
            "total_tokens": int(usage["total_tokens"]) if isinstance(usage.get("total_tokens"), int) else None,
        }

    if local_path == "/v1/chat/completions":
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            return {"input_tokens": None, "output_tokens": None, "total_tokens": None}
        return {
            "input_tokens": int(usage["prompt_tokens"]) if isinstance(usage.get("prompt_tokens"), int) else None,
            "output_tokens": int(usage["completion_tokens"]) if isinstance(usage.get("completion_tokens"), int) else None,
            "total_tokens": int(usage["total_tokens"]) if isinstance(usage.get("total_tokens"), int) else None,
        }

    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    return {
        "input_tokens": int(usage["input_tokens"]) if isinstance(usage.get("input_tokens"), int) else None,
        "output_tokens": int(usage["output_tokens"]) if isinstance(usage.get("output_tokens"), int) else None,
        "total_tokens": int(usage["total_tokens"]) if isinstance(usage.get("total_tokens"), int) else None,
    }


def _forward_request(local_path: str, account: Any, payload: dict[str, Any], idempotency_key: str) -> ProxyResult | StreamProxyResult:
    data = json.dumps(payload).encode()
    stream = is_streaming_request(payload)
    request = urllib.request.Request(
        proxy_upstream_url(local_path),
        data=data,
        headers={
            **DEFAULT_HEADERS,
            "Content-Type": "application/json",
            "Authorization": f"Bearer {account.tokens.access_token}",
            "ChatGPT-Account-Id": account.account_id,
            "Idempotency-Key": idempotency_key,
            "Accept": "text/event-stream" if stream else "application/json",
        },
        method="POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=60.0)
        headers = dict(response.headers.items())
        headers["X-Proxy-Account-Id"] = account.account_id
        if stream:
            return StreamProxyResult(response.status, headers, response, account.account_id, local_path)

        body = response.read()
        response.close()
        usage = _extract_usage(json.loads(body.decode()), local_path)
        return ProxyResult(response.status, body, headers, stream=False, usage=usage)
    except urllib.error.HTTPError as exc:
        headers = dict(exc.headers.items())
        headers["X-Proxy-Account-Id"] = account.account_id
        return ProxyResult(exc.code, exc.read(), headers)


def _forward_with_failover(repo: AccountRepository, max_attempts: int, local_path: str, payload: dict[str, Any], idempotency_key: str) -> ProxyResult | StreamProxyResult:
    candidates = repo.list_proxy_candidates()[:max_attempts]
    if not candidates:
        return ProxyResult(503, json.dumps(_error_envelope("No eligible accounts are currently available.", error_type="server_error", code="no_eligible_accounts")).encode(), {"Content-Type": "application/json"})

    last_result: ProxyResult | None = None
    for account in candidates:
        try:
            refreshed = maybe_refresh_account(account)
            repo.replace_account(refreshed, make_active=None)
            result = _forward_request(local_path, refreshed, payload, idempotency_key)
        except OAuthError as exc:
            repo.mark_proxy_failure(account.account_id, error=str(exc), cooldown_seconds=300)
            last_result = ProxyResult(503, json.dumps(_error_envelope(f"Account refresh failed: {exc}", error_type="server_error", code="account_refresh_failed")).encode(), {"Content-Type": "application/json"})
            continue
        except urllib.error.URLError as exc:
            repo.mark_proxy_failure(account.account_id, error=str(exc), cooldown_seconds=60)
            last_result = ProxyResult(502, json.dumps(_error_envelope(f"Upstream connection error: {exc}", error_type="server_error", code="upstream_connection_error")).encode(), {"Content-Type": "application/json"})
            continue

        if isinstance(result, StreamProxyResult):
            return result

        retryable, cooldown_seconds = classify_status(result.status_code, result.body)
        if retryable:
            retry_after = extract_retry_after(result.headers) or cooldown_seconds or 60
            repo.mark_proxy_failure(account.account_id, error=result.body.decode(errors="replace"), cooldown_seconds=retry_after)
            last_result = result
            continue

        repo.mark_proxy_success(account.account_id)
        return result

    return last_result or ProxyResult(503, json.dumps(_error_envelope("All eligible accounts failed.", error_type="server_error", code="all_accounts_failed")).encode(), {"Content-Type": "application/json"})


def _build_response(result: ProxyResult) -> Response:
    headers = {key: value for key, value in result.headers.items() if key.lower() not in {"content-length", "transfer-encoding", "connection"}}
    return Response(content=result.body, status_code=result.status_code, headers=headers, media_type=headers.get("Content-Type"))


def _stream_generator(repo: AccountRepository, result: StreamProxyResult, started_at: float) -> Iterator[bytes]:
    tracker = StreamUsageTracker(result.local_path)
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
            repo.mark_proxy_failure(result.account_id, error=stream_error, cooldown_seconds=60)
        else:
            repo.mark_proxy_success(result.account_id)

        record_request(
            {
                "request_id": str(uuid.uuid4()),
                "endpoint": result.local_path,
                "stream": True,
                "status_code": result.status_code,
                "latency_ms": int((time.time() - started_at) * 1000),
                "account_id": result.account_id,
                "input_tokens": tracker.usage.get("input_tokens"),
                "output_tokens": tracker.usage.get("output_tokens"),
                "total_tokens": tracker.usage.get("total_tokens"),
            }
        )


def create_app(repo: AccountRepository, *, max_attempts: int | None = None) -> FastAPI:
    attempts = max_attempts or int(os.getenv("HERMES_CODEX_PROXY_MAX_ATTEMPTS", "3") or "3")
    app = FastAPI()

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
        candidates = repo.list_proxy_candidates()
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
        accounts = repo.admin_accounts_snapshot()
        active = next((account for account in accounts if account["active"]), None)
        eligible = sum(1 for account in accounts if account["eligible"])
        cooldown = sum(1 for account in accounts if account["enabled"] and not account["eligible"])
        return JSONResponse(
            {
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
        )

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
        stats_store_module = importlib.import_module("hermes_codex_multi_auth.stats_store")
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

    @app.get("/v1/models")
    async def list_models() -> JSONResponse:
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {"id": "gpt-5.4", "object": "model", "created": 1743091200, "owned_by": "openai"},
                    {"id": "gpt-5.4-mini", "object": "model", "created": 1743091200, "owned_by": "openai"},
                ],
            }
        )

    async def _handle_generation(local_path: str, request: Request) -> Response:
        raw_body = await request.body()
        try:
            payload = json.loads(raw_body.decode() or "{}")
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content=_error_envelope("Request body is not valid JSON.", error_type="invalid_request_error", code="invalid_json"))
        if not isinstance(payload, dict):
            return JSONResponse(status_code=400, content=_error_envelope("Request body must be a JSON object.", error_type="invalid_request_error", code="invalid_payload"))

        started_at = time.time()
        result = _forward_with_failover(repo, attempts, local_path, payload, request.headers.get("Idempotency-Key") or str(uuid.uuid4()))

        if isinstance(result, StreamProxyResult):
            headers = {key: value for key, value in result.headers.items() if key.lower() not in {"content-length", "transfer-encoding", "connection"}}
            return StreamingResponse(_stream_generator(repo, result, started_at), status_code=result.status_code, headers=headers, media_type=headers.get("Content-Type"))

        usage = result.usage or {}
        record_request(
            {
                "request_id": str(uuid.uuid4()),
                "endpoint": local_path,
                "stream": is_streaming_request(payload),
                "status_code": result.status_code,
                "latency_ms": int((time.time() - started_at) * 1000),
                "account_id": result.headers.get("X-Proxy-Account-Id"),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "total_tokens": usage.get("total_tokens"),
            }
        )
        return _build_response(result)

    @app.post("/v1/responses")
    async def responses(request: Request) -> Response:
        return await _handle_generation("/v1/responses", request)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        return await _handle_generation("/v1/chat/completions", request)

    @app.post("/v1/embeddings")
    async def embeddings(request: Request) -> Response:
        return await _handle_generation("/v1/embeddings", request)

    return app


def run_proxy_server(repo: AccountRepository, host: str, port: int) -> int:
    print("PunkRecords proxy")
    print(f"Runtime root:           {app_home()}")
    print(f"Hermes auth path:       {hermes_auth_path()}")
    print(f"Proxy listening on http://{host}:{port}")
    print(f"Responses route:        http://{host}:{port}/v1/responses")
    print(f"Chat completions route: http://{host}:{port}/v1/chat/completions")
    print(f"Embeddings route:       http://{host}:{port}/v1/embeddings")
    print(f"Models route:           http://{host}:{port}/v1/models")
    print(f"Health route:           http://{host}:{port}/healthz")
    print(f"Stats route:            http://{host}:{port}/_proxy/stats/summary")
    print(f"Admin state route:      http://{host}:{port}/_proxy/admin/state")
    uvicorn.run(create_app(repo), host=host, port=port, log_level="warning", lifespan="off")
    return 0
