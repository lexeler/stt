from __future__ import annotations

import base64
import secrets

from fastapi import Header, HTTPException, status

from .config import settings


_BASIC_REALM = 'Basic realm="Texpin"'


def require_web_basic_auth(authorization: str | None = Header(default=None)) -> None:
    """HTTP Basic auth for the browser cabinet at /. Reads WEB_USER/WEB_PASS
    from .env. Disabled (503) until both are set."""
    expected_user = settings.web_user
    expected_pass = settings.web_pass
    if not expected_user or not expected_pass:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Web cabinet disabled: WEB_USER/WEB_PASS not set in .env",
        )
    if not authorization or not authorization.startswith("Basic "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": _BASIC_REALM},
        )
    try:
        decoded = base64.b64decode(authorization.removeprefix("Basic ")).decode("utf-8")
        user, _, password = decoded.partition(":")
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed Basic credentials",
            headers={"WWW-Authenticate": _BASIC_REALM},
        )
    # Compare both — constant-time on each side. Combining the comparisons
    # with `and` short-circuits, leaking *which* of the two was wrong;
    # bit-OR keeps the timing flat.
    user_ok = secrets.compare_digest(user, expected_user)
    pass_ok = secrets.compare_digest(password, expected_pass)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": _BASIC_REALM},
        )


def require_api_token(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency: enforce Bearer-token auth on /api/* endpoints.

    503 if the server is configured without a token (so callers know it's a
    server-side misconfiguration, not their credential). 401 otherwise.
    """
    expected = settings.api_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API disabled: API_TOKEN is not set in server .env",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header (expected: Bearer <token>)",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = authorization.removeprefix("Bearer ")
    if not secrets.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
