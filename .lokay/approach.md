# Approach plan

<!-- lokay-approach source=deterministic repo=mikolaj92/PunkRecords issue=21 -->

Repository: `mikolaj92/PunkRecords`  
Issue: #21 — BOM pinów v0.5.24 vs COMPAT v0.6.5; extra UM nie ten; brak Actions

## Goal

Align host BOM pins with the latest app-factory `COMPAT.md` row:
app-factory `v0.6.5` + my-auth `v0.4.2` + my-usermanager `v0.5.4`.
Use `my-usermanager[fastapi-htmx,myauth]` and override `app-factory[platform]` only.

## Files likely touched

- `pyproject.toml`
- `README.md`
- `uv.lock`
- `tests/test_proxy_server.py`

## Test plan

- `uv run pytest tests/test_proxy_server.py::test_platform_stack_smoke_and_shell_contract`
- Targeted store/proxy tests after lock refresh

## Non-goals

- Do not wire passkey/account/admin user routes (still `PUNKRECORDS_ADMIN_TOKEN`).
- Do not add `.github` workflows from this pin-alignment change (outside localize scope).

## Notes

- Trust intentional issue; this plan is evidence for later review, not a human gate.
- Coding agent may refine details but should stay on the stated goal and non-goals.
- Collector boundary: if implementation introduces unbounded collection, ship only a bounded collector patch that starts durably in the background after merge. The coding agent and mill must not populate data or wait for collection to finish.
