# api/middleware/auth.py

from typing import Optional
from fastapi import HTTPException, Request, status
from fastapi.security import APIKeyHeader

from api.auth.limits import (
    LimitCheckResult,
    check_request_limit,
    consume_free_tier,
    consume_authenticated_limit,
    build_limit_headers,
)
from api.auth.firestore import (
    update_key_last_used,
    TIER_LIMITS,
)
from api.auth.keys import hash_key


# ---------------------------------
# API key header extractor
# ---------------------------------

_api_key_scheme = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
)


# ---------------------------------
# Routes exempt from auth
# ---------------------------------

_EXEMPT_PATHS = {
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/auth/signup",
    "/auth/login",
}

_FREE_TIER_PATHS = {
    "/ingest",
    "/ingest/file",
    "/process",
    "/process/raw",
    "/resolve",
    "/export",
}


# ---------------------------------
# Request context
# ---------------------------------

class RequestContext:
    __slots__ = (
        "uid",
        "tier",
        "workspace_id",
        "is_free_tier",
        "is_authenticated",
        "key_hash",
        "limit_result",
    )

    def __init__(
        self,
        uid:              Optional[str],
        tier:             str,
        workspace_id:     Optional[str],
        is_free_tier:     bool,
        is_authenticated: bool,
        key_hash:         Optional[str],
        limit_result:     LimitCheckResult,
    ):
        self.uid              = uid
        self.tier             = tier
        self.workspace_id     = workspace_id
        self.is_free_tier     = is_free_tier
        self.is_authenticated = is_authenticated
        self.key_hash         = key_hash
        self.limit_result     = limit_result


# ---------------------------------
# Auth dependency
# ---------------------------------

async def require_auth(
    request: Request,
    api_key: Optional[str] = None,
) -> RequestContext:
    api_key = request.headers.get("X-API-Key")

    # Web UI session auth: Authorization: Bearer <firebase_id_token>
    id_token = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        id_token = auth_header[len("Bearer "):].strip() or None

    limit_result = check_request_limit(request, api_key, id_token=id_token)

    if limit_result.is_free_tier and not limit_result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "message":  "Free tier limit reached.",
                "limit":    limit_result.limit,
                "count":    limit_result.current_count,
                "hint":     "Sign up for a free Starter account to get 50 requests/day and an API key.",
                "signup":   "https://moonlit-grail-386316.web.app/signup",
            },
            headers=limit_result.headers,
        )

    key_hash = hash_key(api_key) if api_key else None

    ctx = RequestContext(
        uid=limit_result.uid,
        tier=limit_result.tier,
        workspace_id=limit_result.workspace_id,
        is_free_tier=limit_result.is_free_tier,
        is_authenticated=not limit_result.is_free_tier,
        key_hash=key_hash,
        limit_result=limit_result,
    )

    request.state.ctx = ctx
    return ctx


async def require_authenticated(
    request: Request,
    api_key: Optional[str] = None,
) -> RequestContext:
    ctx = await require_auth(request, api_key)

    if ctx.is_free_tier or not ctx.is_authenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "message": "Authentication required.",
                "hint":    "Sign up for a free Starter account.",
                "signup":  "https://moonlit-grail-386316.web.app/signup",
            },
        )

    return ctx


# ---------------------------------
# Post-request consumption
# ---------------------------------

async def consume_request(ctx: RequestContext, count: int = 1) -> None:
    """
    Increment usage counter after successful request.

    count — number of units to consume, default 1.
    Pass compute_request_units(text) for proportional cost.

    Routing:
    - Free tier:   increments IP counter by count
    - Team/Enterprise: atomic workspace transaction (correct under concurrency)
    - Pro/Starter: increments personal daily counter by count
    Non-blocking — failures never affect the response.
    """
    try:
        if ctx.is_free_tier:
            if ctx.limit_result.request is not None:
                consume_free_tier(ctx.limit_result.request, count=count)
        else:
            if ctx.uid:
                consume_authenticated_limit(
                    uid=ctx.uid,
                    workspace_id=ctx.workspace_id,
                    tier=ctx.tier,
                    count=count,
                )
                if ctx.key_hash:
                    update_key_last_used(ctx.uid, ctx.key_hash)
    except Exception:
        pass


# ---------------------------------
# Middleware — attaches limit headers to all responses
# ---------------------------------

async def rate_limit_headers_middleware(request: Request, call_next):
    response = await call_next(request)

    if hasattr(request.state, "ctx"):
        ctx = request.state.ctx
        for key, value in ctx.limit_result.headers.items():
            response.headers[key] = value

    return response
