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
    auto_error=False,    # we handle the error ourselves
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
    """
    Attached to request.state after auth middleware.
    Available in all route handlers.
    """
    __slots__ = (
        "uid",
        "tier",
        "is_free_tier",
        "is_authenticated",
        "key_hash",
        "limit_result",
    )

    def __init__(
        self,
        uid:              Optional[str],
        tier:             str,
        is_free_tier:     bool,
        is_authenticated: bool,
        key_hash:         Optional[str],
        limit_result:     LimitCheckResult,
    ):
        self.uid              = uid
        self.tier             = tier
        self.is_free_tier     = is_free_tier
        self.is_authenticated = is_authenticated
        self.key_hash         = key_hash
        self.limit_result     = limit_result


# ---------------------------------
# Auth dependency
# Used in route handlers via Depends()
# ---------------------------------

async def require_auth(
    request: Request,
    api_key: Optional[str] = None,
) -> RequestContext:
    """
    FastAPI dependency — enforces auth and rate limits.

    Flow:
    1. Extract API key from X-API-Key header
    2. Check request limit (free tier IP or authenticated)
    3. Reject if limit exceeded
    4. Attach RequestContext to request.state
    5. Return context for route handler use

    Free tier:
    - No key required
    - 10 requests per IP per day
    - Prompt signup on limit

    Authenticated:
    - Valid qrynt_live_ or qrynt_test_ key required
    - Daily limit per tier
    - Rate limit headers in response
    """
    # Extract API key from header
    api_key = request.headers.get("X-API-Key")

    # Check limit
    limit_result = check_request_limit(request, api_key)

    # Invalid key format or revoked key
    if api_key and not limit_result.allowed and limit_result.tier == "unknown":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "message": "Invalid or revoked API key.",
                "hint":    "Check your key at https://moonlit-grail-386316.web.app/dashboard",
            },
        )

    # Free tier limit exceeded
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

    # Authenticated limit exceeded
    if not limit_result.is_free_tier and not limit_result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "message": f"Daily limit reached for {limit_result.tier} tier.",
                "limit":   limit_result.limit,
                "count":   limit_result.current_count,
                "hint":    "Upgrade your plan for higher limits.",
            },
            headers=limit_result.headers,
        )

    # Build context
    key_hash = hash_key(api_key) if api_key else None

    ctx = RequestContext(
        uid=limit_result.uid,
        tier=limit_result.tier,
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
    """
    Stricter dependency — requires authenticated user.
    Used for endpoints that cannot serve anonymous users.
    Example: storage, session history, key management.
    """
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
# Called after successful processing
# ---------------------------------

async def consume_request(ctx: RequestContext) -> None:
    """
    Increment usage counter after successful request.
    Called in route handler after processing completes.

    Rules:
    - Called only on success — failed requests do not count
    - Free tier: increments IP counter
    - Authenticated: increments user daily counter
    - Updates key last_used timestamp
    - Non-blocking — failures do not affect response
    """
    try:
        if ctx.is_free_tier:
            # IP counter handled in limits.py
            pass
        else:
            if ctx.uid:
                consume_authenticated_limit(ctx.uid)
                if ctx.key_hash and ctx.uid:
                    update_key_last_used(ctx.uid, ctx.key_hash)
    except Exception:
        pass


# ---------------------------------
# Middleware — attaches limit headers to all responses
# ---------------------------------

async def rate_limit_headers_middleware(request: Request, call_next):
    """
    Starlette middleware — attaches rate limit headers to responses.
    Applied globally in main.py.

    Skips exempt paths (health, docs).
    Reads limit headers from request.state.ctx if available.
    """
    response = await call_next(request)

    # Attach rate limit headers if context available
    if hasattr(request.state, "ctx"):
        ctx = request.state.ctx
        for key, value in ctx.limit_result.headers.items():
            response.headers[key] = value

    return response
