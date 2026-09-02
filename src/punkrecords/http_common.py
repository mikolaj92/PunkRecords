"""Shared HTTP response primitives."""

from __future__ import annotations

import os
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


def error_envelope(
    message: str, *, error_type: str, code: str, param: str | None = None
) -> dict[str, Any]:
    return {
        "error": {"message": message, "type": error_type, "param": param, "code": code}
    }


def check_admin_auth(request: Request) -> JSONResponse | None:
    token = os.getenv("PUNKRECORDS_ADMIN_TOKEN", "").strip()
    if not token:
        return None
    auth_header = request.headers.get("Authorization", "")
    bearer = (
        auth_header.removeprefix("Bearer ").strip()
        if auth_header.startswith("Bearer ")
        else ""
    )
    if request.headers.get("X-Admin-Token", "") == token or bearer == token:
        return None
    return JSONResponse(
        status_code=401,
        content=error_envelope(
            "Admin authentication required.",
            error_type="authentication_error",
            code="admin_auth_required",
        ),
    )
