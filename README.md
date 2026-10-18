# PunkRecords

PunkRecords is a self-contained local proxy runtime with plugin-based provider support and built-in multi-account OAuth management.

This project keeps provider credentials, failover state, and proxy telemetry in its own local store and exposes a server surface for:

- running a local OpenAI-compatible failover proxy for supported routes,
- serving the operator dashboard at `GET /` plus admin/state/settings JSON APIs,
- routing requests across provider credential pools,

The CLI starts the server; administration happens through the operator dashboard and admin JSON API.

## Current scope

Version `0.1.0` ships with one built-in provider plugin:

- built-in provider: `openai-codex`
- auth type: OAuth logins, not API keys
- server-oriented workflow with the operator dashboard at `GET /` plus admin JSON APIs
- staged OpenAI-compatible proxy support for selected routes

The provider system itself is plugin-based. Built-in plugins live in the repository under the `providers/` package, and external plugins can be loaded by setting:

- `PUNKRECORDS_PROVIDER_MODULES=my_provider_module,another_provider_module`

## Commands

```bash
punkrecords proxy --host 0.0.0.0 --port 4141
```

After the proxy starts, open the operator dashboard at `http://127.0.0.1:4141/` (`GET /`). JSON admin routes stay under `/_proxy/admin/`.

## Platform UI stack

The operator dashboard uses the shared app-factory product shell (Basecoat + HTMX + Alpine) from same-origin `/static/platform/...`. Host templates extend `app_factory/product_shell.html` and supply menu data only — no forked sidebar/header/theme chrome.

Git sources (`pyproject.toml` → `uv.lock`):

| Repository (`github.com/mikolaj92/`) | Tag | Locked commit |
|------------------------------------|-----|---------------|
| app-factory | `v0.7.9` | `eea39ae621eefe6a681f905a954abe016aef724b` |
| my-auth | `v0.5.7` | `bd0775f1993299b27d2bbd785dd252821dbff06e` |
| my-usermanager | `v0.6.10` | `1844d41e0caad5a458a16bc16d3dcc54444b35fc` |

Passkey login and account/admin user routes are not wired in this host yet (admin access remains `PUNKRECORDS_ADMIN_TOKEN`). Chart.js is loaded through `app_factory.cdn` as an optional product extra.

### HTTP stack

FastAPI serves the application through Uvicorn. Upstream requests use Python's standard-library `urllib.request`; HTTPX2 belongs to the optional `dev` dependencies, not the production transport.

## Development

```bash
uv sync --locked --extra dev
uv run --locked --extra dev pytest
```

## Storage

By default the project stores its state in:

- `./.punkrecords/accounts.json`
- `./.punkrecords/settings.json`
- `./.punkrecords/stats/proxy-rollups.json`
- `./.punkrecords/stats/proxy-requests.jsonl`

Override the state directory with:

- `PUNKRECORDS_HOME=/path/to/home`

## OpenAI Codex upstream configuration

The built-in `openai-codex` provider resolves upstream URLs from one modern path:

- `PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_BASE` — base URL prepended with the mapped route path (default: `https://chatgpt.com/backend-api/codex`)
- `PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_<ROUTE>_URL` — full URL override for one local route, where `<ROUTE>` is the local path with `/` replaced by `_` and uppercased (for example `PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_V1_RESPONSES_URL` for `/v1/responses`)

Route-specific overrides win over the base. There is no legacy `PUNKRECORDS_OPENAI_CODEX_PROXY_UPSTREAM_URL` fallback.

## Local proxy

The proxy server is implemented with FastAPI and served through Uvicorn.

FastAPI also exposes built-in API docs and schema when the proxy is running:

- `GET /openapi.json`
- `GET /docs`
- `GET /redoc`

The proxy selects a healthy saved account, forwards the request upstream, and fails over to the next eligible account on qualifying transient/account-scoped failures such as `deactivated_workspace`.

Current compatibility notes:

- non-streaming requests are supported for both proxied routes
- live streaming passthrough is supported for `stream=true` requests on both supported routes
- non-streaming embeddings requests are supported through `/v1/embeddings`
- local proxy stats are stored on disk and exposed through `/_proxy/stats/summary`
- `/v1/models` is available as a minimal compatibility discovery route
- this is not yet a universal drop-in replacement for every OpenAI API endpoint
