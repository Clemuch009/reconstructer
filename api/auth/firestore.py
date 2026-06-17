# api/auth/firestore.py

import hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from typing_extensions import TypedDict

from api.auth.keys import StoredAPIKey, hash_key


# ---------------------------------
# Firestore client — lazy init
# ---------------------------------

_db = None

def _get_db():
    """
    Lazy Firestore client initialization.
    Uses Application Default Credentials on Cloud Run.
    Uses service account locally if GOOGLE_APPLICATION_CREDENTIALS set.
    """
    global _db
    if _db is None:
        from google.cloud import firestore
        _db = firestore.Client()
    return _db


# ---------------------------------
# Collection paths
# ---------------------------------

USERS_COL       = "users"
SESSIONS_COL    = "sessions"
IP_COUNTERS_COL = "ip_counters"
KEY_INDEX_COL   = "api_key_index"   # key_hash → uid, for fast key lookup


# ---------------------------------
# Tier definitions — matches locked pricing
# ---------------------------------

TIER_LIMITS = {
    "free": {
        "requests_per_day": 10,
        "max_keys":         0,
        "storage":          False,
        "max_members":      0,
    },
    "starter": {
        "requests_per_day": 50,
        "max_keys":         1,
        "storage":          False,
        "max_members":      0,
    },
    "pro": {
        "requests_per_day": 1_000,
        "max_keys":         3,
        "storage":          True,
        "max_members":      0,
    },
    "team": {
        "requests_per_day": 10_000,
        "max_keys":         10,
        "storage":          True,
        "max_members":      10,
    },
    "enterprise": {
        "requests_per_day": -1,
        "max_keys":         -1,
        "storage":          True,
        "max_members":      -1,
    },
}


# ---------------------------------
# User record contract
# ---------------------------------

class UserRecord(TypedDict):
    uid:              str
    email:            str
    tier:             str
    created_at:       str
    workspace_id:     Optional[str]
    workspace_role:   Optional[str]
    paddle:           dict
    api_keys:         List[StoredAPIKey]
    storage_settings: dict
    usage:            dict


# ---------------------------------
# User operations
# ---------------------------------

def create_user(
    uid:   str,
    email: str,
    tier:  str = "starter",
) -> UserRecord:
    """
    Create user record in Firestore on signup.
    Called after Firebase Auth creates the uid.

    Initializes workspace and stripe fields so later
    updates (workspace.py, billing.py) can merge safely.
    """
    db  = _get_db()
    now = datetime.now(timezone.utc).isoformat()

    record = UserRecord(
        uid=uid,
        email=email,
        tier=tier,
        created_at=now,
        workspace_id=None,
        workspace_role=None,
        paddle={
            "customer_id":         None,
            "subscription_id":     None,
            "subscription_status": None,
            "current_period_end":  None,
        },
        api_keys=[],
        storage_settings={
            "enabled":        False,
            "retention_days": 7,
            "auto_delete":    True,
        },
        usage={
            "requests_today": 0,
            "requests_total": 0,
            "last_request":   None,
            "reset_date":     _today(),
        },
    )

    db.collection(USERS_COL).document(uid).set(record)
    return record


def get_user(uid: str) -> Optional[UserRecord]:
    """
    Retrieve user record by Firebase UID.
    Returns None if not found.
    """
    db  = _get_db()
    doc = db.collection(USERS_COL).document(uid).get()
    if not doc.exists:
        return None
    return doc.to_dict()


def get_user_by_key_hash(key_hash: str) -> Optional[UserRecord]:
    """
    Find user by API key hash.
    Used during request authentication.

    Uses a dedicated key_hash → uid index collection rather than
    querying inside the api_keys array. Firestore array-of-maps
    queries (array_contains_any) require an exact match of the
    entire map, which breaks as soon as fields like last_used
    or active change — so a separate index is the reliable approach.
    """
    db        = _get_db()
    index_doc = db.collection(KEY_INDEX_COL).document(key_hash).get()

    if not index_doc.exists:
        return None

    uid = index_doc.to_dict().get("uid")
    if not uid:
        return None

    return get_user(uid)


def add_api_key(uid: str, stored_key: StoredAPIKey) -> bool:
    """
    Add API key to user record.
    Enforces max_keys limit per tier.
    Also writes to the key_hash → uid index for fast lookup.
    Returns False if limit reached.
    """
    db   = _get_db()
    ref  = db.collection(USERS_COL).document(uid)
    doc  = ref.get()

    if not doc.exists:
        return False

    user     = doc.to_dict()
    tier     = user.get("tier", "starter")
    max_keys = TIER_LIMITS.get(tier, TIER_LIMITS["starter"])["max_keys"]
    active_keys = [
        k for k in user.get("api_keys", [])
        if k.get("active", False)
    ]

    if max_keys != -1 and len(active_keys) >= max_keys:
        return False

    ref.update({
        "api_keys": user.get("api_keys", []) + [dict(stored_key)]
    })

    # Write to key index for fast auth lookup
    db.collection(KEY_INDEX_COL).document(stored_key["key_hash"]).set({
        "uid": uid,
    })

    return True


def revoke_api_key(uid: str, prefix: str) -> bool:
    """
    Deactivate API key by prefix.
    Key record preserved for audit — active set to False.
    Removes the key from the lookup index so it can no longer
    authenticate requests.
    """
    db  = _get_db()
    ref = db.collection(USERS_COL).document(uid)
    doc = ref.get()

    if not doc.exists:
        return False

    user     = doc.to_dict()
    api_keys = user.get("api_keys", [])
    updated  = False

    for key in api_keys:
        if key.get("prefix") == prefix:
            key["active"] = False
            updated = True
            # Remove from key index — revoked keys can't authenticate
            try:
                db.collection(KEY_INDEX_COL).document(key["key_hash"]).delete()
            except Exception:
                pass

    if updated:
        ref.update({"api_keys": api_keys})

    return updated


def update_key_last_used(uid: str, key_hash: str) -> None:
    """
    Update last_used timestamp for an API key.
    Non-blocking — failure does not affect request.
    """
    try:
        db  = _get_db()
        ref = db.collection(USERS_COL).document(uid)
        doc = ref.get()
        if not doc.exists:
            return

        user     = doc.to_dict()
        api_keys = user.get("api_keys", [])
        now      = datetime.now(timezone.utc).isoformat()

        for key in api_keys:
            if key.get("key_hash") == key_hash:
                key["last_used"] = now

        ref.update({"api_keys": api_keys})
    except Exception:
        pass


# ---------------------------------
# Usage tracking
# ---------------------------------

def increment_usage(uid: str) -> dict:
    """
    Increment request count for authenticated user.
    Resets daily counter if reset_date is yesterday.
    Returns updated usage dict.
    """
    db  = _get_db()
    ref = db.collection(USERS_COL).document(uid)
    doc = ref.get()

    if not doc.exists:
        return {}

    user  = doc.to_dict()
    usage = user.get("usage", {})
    today = _today()

    # Reset daily counter if new day
    if usage.get("reset_date") != today:
        usage["requests_today"] = 0
        usage["reset_date"]     = today

    usage["requests_today"] = usage.get("requests_today", 0) + 1
    usage["requests_total"] = usage.get("requests_total", 0) + 1
    usage["last_request"]   = datetime.now(timezone.utc).isoformat()

    ref.update({"usage": usage})
    return usage


def check_usage_limit(uid: str, tier: str) -> tuple[bool, dict]:
    """
    Check if user is within their daily request limit.
    Returns (within_limit, usage_dict).
    For team/enterprise tiers, use check_workspace_limit instead.
    """
    db  = _get_db()
    ref = db.collection(USERS_COL).document(uid)
    doc = ref.get()

    if not doc.exists:
        return False, {}

    user  = doc.to_dict()
    usage = user.get("usage", {})
    today = _today()

    # Reset if new day
    if usage.get("reset_date") != today:
        usage["requests_today"] = 0

    limit = TIER_LIMITS.get(tier, TIER_LIMITS["starter"]).get("requests_per_day", 0)

    # Unlimited tier
    if limit == -1:
        return True, usage

    within_limit = usage.get("requests_today", 0) < limit
    return within_limit, usage


# ---------------------------------
# Workspace usage — shared pool (team tier)
# Uses Firestore transactions for atomic check + increment
# ---------------------------------

WORKSPACES_COL = "workspaces"


def check_and_increment_workspace(
    workspace_id: str,
    uid:          str,
    tier:         str,
    count:        int = 1,
) -> tuple[bool, dict]:
    """
    Atomically check and increment the workspace shared request pool.

    Uses a Firestore transaction to guarantee correctness under
    concurrent requests from multiple team members. The transaction:
    1. Reads current workspace usage
    2. Checks against tier limit
    3. Increments atomically if within limit
    4. Resets daily counter if new day

    Returns (allowed, usage_dict).

    Member-level breakdown (non-transactional, approximate):
    After a successful transaction, increments the member's own
    contribution counter on the workspace document using
    firestore.Increment() — atomic but outside the transaction,
    so counts may be off by 1 under extreme concurrency.
    This is acceptable for "who used the most today" display.
    """
    from google.cloud import firestore as fs

    db    = _get_db()
    ref   = db.collection(WORKSPACES_COL).document(workspace_id)
    today = _today()

    limit = TIER_LIMITS.get(tier, TIER_LIMITS["team"]).get("requests_per_day", 0)

    # Enterprise — unlimited, skip counter entirely
    if limit == -1:
        # Still log member usage non-transactionally for visibility
        _log_member_usage(workspace_id, uid, count, today)
        return True, {"requests_today": 0, "limit": -1}

    usage_result: dict = {}
    allowed      = False

    @fs.transactional
    def _txn(transaction):
        nonlocal allowed, usage_result

        doc = ref.get(transaction=transaction)
        if not doc.exists:
            allowed = False
            return

        data  = doc.to_dict()
        usage = data.get("usage", {})

        # Reset daily counter if new day
        if usage.get("reset_date") != today:
            usage = {
                "requests_today": 0,
                "requests_total": usage.get("requests_total", 0),
                "reset_date":     today,
                "last_request":   None,
                "member_usage":   {},  # reset per-member breakdown too
            }

        current = usage.get("requests_today", 0)

        if current + count > limit:
            allowed      = False
            usage_result = {
                "requests_today": current,
                "limit":          limit,
                "remaining":      max(0, limit - current),
            }
            return

        # Within limit — increment
        usage["requests_today"] = current + count
        usage["requests_total"] = usage.get("requests_total", 0) + count
        usage["last_request"]   = datetime.now(timezone.utc).isoformat()
        usage["reset_date"]     = today

        transaction.update(ref, {"usage": usage})

        allowed      = True
        usage_result = {
            "requests_today": usage["requests_today"],
            "limit":          limit,
            "remaining":      max(0, limit - usage["requests_today"]),
        }

    transaction = db.transaction()
    _txn(transaction)

    # Non-transactional member breakdown — approximate, best-effort
    if allowed:
        _log_member_usage(workspace_id, uid, count, today)

    return allowed, usage_result


def _log_member_usage(
    workspace_id: str,
    uid:          str,
    count:        int,
    today:        str,
) -> None:
    """
    Log per-member usage on the workspace document.
    Non-transactional — uses atomic Increment so concurrent
    writes don't lose counts, but not tied to the limit check.
    Best-effort: used for dashboard breakdown display only.
    """
    from google.cloud import firestore as fs

    try:
        db  = _get_db()
        ref = db.collection(WORKSPACES_COL).document(workspace_id)
        ref.update({
            f"member_usage.{uid}.{today}": fs.Increment(count),
        })
    except Exception:
        pass  # never block a request on analytics logging


def get_workspace_usage(workspace_id: str) -> dict:
    """
    Get current workspace usage stats.
    Returns usage dict with today's count, total, member breakdown.
    """
    db  = _get_db()
    doc = db.collection(WORKSPACES_COL).document(workspace_id).get()

    if not doc.exists:
        return {}

    data  = doc.to_dict()
    usage = data.get("usage", {})
    today = _today()

    # Reset if stale
    if usage.get("reset_date") != today:
        usage["requests_today"] = 0

    # Per-member breakdown for today
    member_usage = data.get("member_usage", {})
    today_breakdown = {
        uid: counts.get(today, 0)
        for uid, counts in member_usage.items()
        if counts.get(today, 0) > 0
    }

    return {
        "requests_today":   usage.get("requests_today", 0),
        "requests_total":   usage.get("requests_total", 0),
        "reset_date":       usage.get("reset_date", today),
        "last_request":     usage.get("last_request"),
        "member_breakdown": today_breakdown,
    }





# ---------------------------------
# IP counter operations (free tier)
# ---------------------------------

def _hash_ip(ip: str) -> str:
    """Hash IP address — never store raw IPs."""
    return hashlib.sha256(ip.encode()).hexdigest()[:32]


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_ip_count(ip: str) -> int:
    """
    Get request count for IP address.
    Returns 0 if IP not found.
    """
    db       = _get_db()
    ip_hash  = _hash_ip(ip)
    doc      = db.collection(IP_COUNTERS_COL).document(ip_hash).get()

    if not doc.exists:
        return 0

    data  = doc.to_dict()
    today = _today()

    # Reset if new day
    if data.get("date") != today:
        return 0

    return data.get("count", 0)


def increment_ip_count(ip: str) -> int:
    """
    Increment request count for IP address.
    Resets daily.
    Returns new count.
    """
    db       = _get_db()
    ip_hash  = _hash_ip(ip)
    ref      = db.collection(IP_COUNTERS_COL).document(ip_hash)
    doc      = ref.get()
    today    = _today()
    now      = datetime.now(timezone.utc).isoformat()

    if not doc.exists or doc.to_dict().get("date") != today:
        # New record or new day — reset
        ref.set({
            "count":      1,
            "date":       today,
            "first_seen": now,
            "last_seen":  now,
        })
        return 1

    data      = doc.to_dict()
    new_count = data.get("count", 0) + 1
    ref.update({
        "count":     new_count,
        "last_seen": now,
    })
    return new_count


def is_ip_within_limit(ip: str, limit: int = 10) -> tuple[bool, int]:
    """
    Check if IP is within free tier limit.
    Returns (within_limit, current_count).
    """
    count = get_ip_count(ip)
    return count < limit, count


# ---------------------------------
# Storage settings
# ---------------------------------

def update_storage_settings(
    uid:            str,
    enabled:        bool,
    retention_days: int,
    auto_delete:    bool,
) -> bool:
    """
    Update user storage preferences.
    Only tiers with storage=True can enable storage.
    Returns False if tier does not support storage.
    """
    db  = _get_db()
    ref = db.collection(USERS_COL).document(uid)
    doc = ref.get()

    if not doc.exists:
        return False

    user = doc.to_dict()
    tier = user.get("tier", "starter")

    if not TIER_LIMITS.get(tier, {}).get("storage", False):
        return False

    if retention_days not in (7, 30, 90):
        retention_days = 7

    ref.update({
        "storage_settings": {
            "enabled":        enabled,
            "retention_days": retention_days,
            "auto_delete":    auto_delete,
        }
    })
    return True


# ---------------------------------
# Session storage (opt-in)
# ---------------------------------

def store_session(
    uid:      str,
    session:  dict,
) -> bool:
    """
    Store session COC envelope for users who have opted in
    to storage (Pro, Team, Enterprise).

    Attaches expiry based on retention_days setting.
    Returns False if storage not enabled for user.
    """
    db  = _get_db()
    ref = db.collection(USERS_COL).document(uid)
    doc = ref.get()

    if not doc.exists:
        return False

    user     = doc.to_dict()
    settings = user.get("storage_settings", {})

    if not settings.get("enabled", False):
        return False

    retention_days = settings.get("retention_days", 7)
    now            = datetime.now(timezone.utc)
    expires_at     = (now + timedelta(days=retention_days)).isoformat()

    session_doc = {
        **session,
        "uid":        uid,
        "stored_at":  now.isoformat(),
        "expires_at": expires_at,
    }

    # COC envelope uses source_id as the document identifier
    doc_id = (
        session.get("session_id") or
        session.get("source_id") or
        "unknown"
    )

    db.collection(USERS_COL)\
      .document(uid)\
      .collection(SESSIONS_COL)\
      .document(doc_id)\
      .set(session_doc)

    return True


def get_stored_sessions(
    uid:   str,
    limit: int = 20,
) -> List[dict]:
    """
    Retrieve stored sessions for a user.
    Filters out expired sessions.
    Single order_by to avoid composite index requirement.
    """
    db  = _get_db()
    now = datetime.now(timezone.utc).isoformat()

    docs = (
        db.collection(USERS_COL)
        .document(uid)
        .collection(SESSIONS_COL)
        .where("expires_at", ">", now)
        .order_by("expires_at", direction="DESCENDING")
        .limit(limit)
        .stream()
    )

    return [doc.to_dict() for doc in docs]


def delete_session(uid: str, session_id: str) -> bool:
    """
    Delete a specific stored session.
    User-controlled deletion — GDPR compliant.
    """
    db = _get_db()
    db.collection(USERS_COL)\
      .document(uid)\
      .collection(SESSIONS_COL)\
      .document(session_id)\
      .delete()
    return True


def delete_all_sessions(uid: str) -> int:
    """
    Delete all stored sessions for a user.
    Returns count of deleted sessions.
    """
    db   = _get_db()
    docs = (
        db.collection(USERS_COL)
        .document(uid)
        .collection(SESSIONS_COL)
        .stream()
    )

    count = 0
    for doc in docs:
        doc.reference.delete()
        count += 1

    return count
