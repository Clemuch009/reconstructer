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
    uid:  str,
    tier: str,
) -> Tuple[bool, dict]:
    from api.auth.firestore import check_usage_limit
    return check_usage_limit(uid, tier)


def consume_authenticated_limit(uid: str, count: int = 1) -> dict:
    """
    Increment usage counter for authenticated user by count units.
    Returns updated usage dict after all increments.
    Call only after request is processed successfully.
    """
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
        "current_count",
        "limit",
        "is_free_tier",
        "headers",
        "request",       # stored for consume_free_tier call
    )

    def __init__(
        self,
        allowed:       bool,
        tier:          str,
        uid:           Optional[str],
        current_count: int,
        limit:         int,
        is_free_tier:  bool,
        headers:       dict,
        request:       Optional[Request] = None,
    ):
        self.allowed       = allowed
        self.tier          = tier
        self.uid           = uid
        self.current_count = current_count
        self.limit         = limit
        self.is_free_tier  = is_free_tier
        self.headers       = headers
        self.request       = request


# ---------------------------------
# Unified limit check
# ---------------------------------

def check_request_limit(
    request:  Request,
    api_key:  Optional[str] = None,
) -> LimitCheckResult:
    from api.auth.firestore import (
        TIER_LIMITS,
        get_user_by_key_hash,
        is_ip_within_limit,
    )
    from api.auth.keys import hash_key, verify_key_format

    # Free tier — no API key
    if not api_key:
        allowed, count, limit = check_free_tier(request)
        headers = build_limit_headers("free", count, limit)
        return LimitCheckResult(
            allowed=allowed,
            tier="free",
            uid=None,
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
            current_count=0,
            limit=0,
            is_free_tier=False,
            headers={},
            request=request,
        )

    uid   = user.get("uid", "")
    tier  = user.get("tier", "starter")

    allowed, usage_dict = check_authenticated_limit(uid, tier)
    limit       = TIER_LIMITS.get(tier, {}).get("requests_per_day", 0)
    today_count = usage_dict.get("requests_today", 0)
    headers     = build_limit_headers(tier, today_count, limit)

    return LimitCheckResult(
        allowed=allowed,
        tier=tier,
        uid=uid,
        current_count=today_count,
        limit=limit,
        is_free_tier=False,
        headers=headers,
        request=request,
    )
