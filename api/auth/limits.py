# api/auth/limits.py

from typing import Optional, Tuple
from fastapi import Request


# ---------------------------------
# Tier limits — single source of truth
# ---------------------------------

FREE_IP_LIMIT = 10   # requests before signup required


# ---------------------------------
# IP extraction
# ---------------------------------

def _extract_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    if request.client:
        return request.client.host
    return "unknown"


# ---------------------------------
# Free tier — IP based
# ---------------------------------

def check_free_tier(request: Request) -> Tuple[bool, int, int]:
    from api.auth.firestore import is_ip_within_limit
    ip = _extract_ip(request)
    if ip == "unknown":
        return True, 0, FREE_IP_LIMIT
    within_limit, count = is_ip_within_limit(ip, FREE_IP_LIMIT)
    return within_limit, count, FREE_IP_LIMIT


def consume_free_tier(request: Request, count: int = 1) -> int:
    """
    Increment free tier counter for IP by count units.
    Returns new count after all increments.
    Call only after request is processed successfully.
    """
    from api.auth.firestore import increment_ip_count
    ip = _extract_ip(request)
    if ip == "unknown":
        return 0
    result = 0
    for _ in range(count):
        result = increment_ip_count(ip)
    return result


# ---------------------------------
# Authenticated tier
# ---------------------------------

def check_authenticated_limit(
    uid:          str,
    tier:         str,
    workspace_id: Optional[str] = None,
) -> Tuple[bool, dict]:
    """
    Check if authenticated user is within their daily limit.

    Routing:
    - team/enterprise + workspace_id → workspace shared pool
      (transactional check, no increment yet)
    - pro/starter → personal per-user counter
    """
    from api.auth.firestore import check_usage_limit

    if tier in ("team", "enterprise") and workspace_id:
        # For team: read workspace usage for the check
        # Actual atomic check+increment happens in consume_authenticated_limit
        from api.auth.firestore import get_workspace_usage, TIER_LIMITS
        usage = get_workspace_usage(workspace_id)
        limit = TIER_LIMITS.get(tier, {}).get("requests_per_day", 0)
        if limit == -1:
            return True, usage
        allowed = usage.get("requests_today", 0) < limit
        return allowed, usage

    return check_usage_limit(uid, tier)


def consume_authenticated_limit(
    uid:          str,
    workspace_id: Optional[str] = None,
    tier:         str = "starter",
    count:        int = 1,
) -> dict:
    """
    Increment usage counter after successful request.

    Routing:
    - team/enterprise + workspace_id → atomic workspace transaction
    - pro/starter/free → personal per-user counter
    """
    if tier in ("team", "enterprise") and workspace_id:
        from api.auth.firestore import check_and_increment_workspace
        _, usage = check_and_increment_workspace(
            workspace_id=workspace_id,
            uid=uid,
            tier=tier,
            count=count,
        )
        return usage

    from api.auth.firestore import increment_usage
    result = {}
    for _ in range(count):
        result = increment_usage(uid)
    return result


# ---------------------------------
# Limit response headers
# ---------------------------------

def build_limit_headers(
    tier:           str,
    requests_today: int,
    requests_limit: int,
) -> dict[str, str]:
    from datetime import datetime, timezone, timedelta

    now        = datetime.now(timezone.utc)
    midnight   = now.replace(hour=0, minute=0, second=0, microsecond=0)
    next_reset = midnight + timedelta(days=1)
    seconds_until_reset = int((next_reset - now).total_seconds())

    remaining = (
        max(0, requests_limit - requests_today)
        if requests_limit != -1
        else 999999
    )
    limit_str = str(requests_limit) if requests_limit != -1 else "unlimited"

    return {
        "X-RateLimit-Tier":      tier,
        "X-RateLimit-Limit":     limit_str,
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset":     str(seconds_until_reset),
    }


# ---------------------------------
# Unified limit check result
# ---------------------------------

class LimitCheckResult:
    __slots__ = (
        "allowed",
        "tier",
        "uid",
        "workspace_id",
        "current_count",
        "limit",
        "is_free_tier",
        "headers",
        "request",
    )

    def __init__(
        self,
        allowed:       bool,
        tier:          str,
        uid:           Optional[str],
        workspace_id:  Optional[str],
        current_count: int,
        limit:         int,
        is_free_tier:  bool,
        headers:       dict,
        request:       Optional[Request] = None,
    ):
        self.allowed       = allowed
        self.tier          = tier
        self.uid           = uid
        self.workspace_id  = workspace_id
        self.current_count = current_count
        self.limit         = limit
        self.is_free_tier  = is_free_tier
        self.headers       = headers
        self.request       = request


def _build_user_limit_result(user: dict, request: Request) -> LimitCheckResult:
    """
    Build a LimitCheckResult for an authenticated user record.
    Shared by the id_token (session) path and reused logic — applies the same
    tier/workspace limit routing as an API-key request so behaviour is identical
    regardless of how the user authenticated.
    """
    from api.auth.firestore import TIER_LIMITS

    uid          = user.get("uid", "")
    tier         = user.get("tier", "starter")
    workspace_id = user.get("workspace_id")

    if tier in ("team", "enterprise") and workspace_id:
        allowed, usage_dict = check_authenticated_limit(
            uid=uid, tier=tier, workspace_id=workspace_id
        )
    else:
        allowed, usage_dict = check_authenticated_limit(uid=uid, tier=tier)

    limit       = TIER_LIMITS.get(tier, {}).get("requests_per_day", 0)
    today_count = usage_dict.get("requests_today", 0)
    headers     = build_limit_headers(tier, today_count, limit)

    return LimitCheckResult(
        allowed=allowed,
        tier=tier,
        uid=uid,
        workspace_id=workspace_id,
        current_count=today_count,
        limit=limit,
        is_free_tier=False,
        headers=headers,
        request=request,
    )


# ---------------------------------
# Unified limit check
# ---------------------------------

def check_request_limit(
    request:  Request,
    api_key:  Optional[str] = None,
    id_token: Optional[str] = None,
) -> LimitCheckResult:
    from api.auth.firestore import (
        TIER_LIMITS,
        get_user_by_key_hash,
        get_user,
        is_ip_within_limit,
    )
    from api.auth.keys import hash_key, verify_key_format

    # ─── Session auth (web UI) — Firebase id_token takes precedence ───
    # The console/observer authenticate logged-in users with their Firebase
    # id_token (Authorization: Bearer ...). This resolves to the SAME uid/tier
    # as an API key, so storage, usage metering, and limits behave identically.
    if id_token:
        from api.auth.router import _verify_firebase_token
        try:
            decoded = _verify_firebase_token(id_token)
            uid     = decoded.get("uid")
        except Exception:
            uid = None

        user = get_user(uid) if uid else None
        if user:
            return _build_user_limit_result(user, request)
        # invalid/unknown token → fall through to API key / free tier

    # Free tier — no API key
    if not api_key:
        allowed, count, limit = check_free_tier(request)
        headers = build_limit_headers("free", count, limit)
        return LimitCheckResult(
            allowed=allowed,
            tier="free",
            uid=None,
            workspace_id=None,
            current_count=count,
            limit=limit,
            is_free_tier=True,
            headers=headers,
            request=request,
        )

    # Authenticated — API key present
    is_valid, key_type = verify_key_format(api_key)
    if not is_valid:
        return LimitCheckResult(
            allowed=False,
            tier="unknown",
            uid=None,
            workspace_id=None,
            current_count=0,
            limit=0,
            is_free_tier=False,
            headers={},
            request=request,
        )

    key_hash = hash_key(api_key)
    user     = get_user_by_key_hash(key_hash)

    if not user:
        return LimitCheckResult(
            allowed=False,
            tier="unknown",
            uid=None,
            workspace_id=None,
            current_count=0,
            limit=0,
            is_free_tier=False,
            headers={},
            request=request,
        )

    active_key = next(
        (k for k in user.get("api_keys", [])
         if k.get("key_hash") == key_hash and k.get("active", False)),
        None
    )

    if not active_key:
        return LimitCheckResult(
            allowed=False,
            tier=user.get("tier", "starter"),
            uid=user.get("uid"),
            workspace_id=user.get("workspace_id"),
            current_count=0,
            limit=0,
            is_free_tier=False,
            headers={},
            request=request,
        )

    uid          = user.get("uid", "")
    tier         = user.get("tier", "starter")
    workspace_id = user.get("workspace_id")

    # Team/Enterprise → check workspace shared pool
    if tier in ("team", "enterprise") and workspace_id:
        allowed, usage_dict = check_authenticated_limit(
            uid=uid, tier=tier, workspace_id=workspace_id
        )
        limit       = TIER_LIMITS.get(tier, {}).get("requests_per_day", 0)
        today_count = usage_dict.get("requests_today", 0)
        headers     = build_limit_headers(tier, today_count, limit)
        return LimitCheckResult(
            allowed=allowed,
            tier=tier,
            uid=uid,
            workspace_id=workspace_id,
            current_count=today_count,
            limit=limit,
            is_free_tier=False,
            headers=headers,
            request=request,
        )

    # Pro/Starter → personal counter
    allowed, usage_dict = check_authenticated_limit(uid=uid, tier=tier)
    limit       = TIER_LIMITS.get(tier, {}).get("requests_per_day", 0)
    today_count = usage_dict.get("requests_today", 0)
    headers     = build_limit_headers(tier, today_count, limit)

    return LimitCheckResult(
        allowed=allowed,
        tier=tier,
        uid=uid,
        workspace_id=workspace_id,
        current_count=today_count,
        limit=limit,
        is_free_tier=False,
        headers=headers,
        request=request,
    )
