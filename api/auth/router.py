# api/auth/router.py

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr

from api.auth.keys import generate_key, to_stored_key
from api.auth.firestore import (
    create_user,
    get_user,
    add_api_key,
    revoke_api_key,
    update_storage_settings,
    get_stored_sessions,
    delete_session,
    delete_all_sessions,
    TIER_LIMITS,
)
from api.dependencies import verify_api_key


router = APIRouter(prefix="/auth", tags=["Auth"])


# ---------------------------------
# Firebase Auth client — lazy init
# ---------------------------------

_firebase_app = None

def _get_firebase():
    """
    Lazy Firebase Admin SDK initialization.
    Uses Application Default Credentials on Cloud Run.
    """
    global _firebase_app
    if _firebase_app is None:
        import firebase_admin
        from firebase_admin import credentials, auth
        if not firebase_admin._apps:
            firebase_admin.initialize_app()
        _firebase_app = firebase_admin.get_app()
    return _firebase_app


def _verify_firebase_token(id_token: str) -> dict:
    """
    Verify Firebase ID token.
    Returns decoded token claims.
    Raises HTTPException on invalid token.
    """
    try:
        from firebase_admin import auth
        _get_firebase()
        decoded = auth.verify_id_token(id_token)
        return decoded
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {str(e)[:100]}",
        )


# ---------------------------------
# Request models
# ---------------------------------

class SignupRequest(BaseModel):
    id_token: str          # Firebase ID token from client-side auth


class GenerateKeyRequest(BaseModel):
    id_token: str
    key_type: str = "live"  # "live" | "test"


class RevokeKeyRequest(BaseModel):
    id_token: str
    prefix:   str           # key prefix to revoke


class StorageSettingsRequest(BaseModel):
    id_token:       str
    enabled:        bool
    retention_days: int = 7
    auto_delete:    bool = True


class DeleteSessionRequest(BaseModel):
    id_token:   str
    session_id: str


# ---------------------------------
# Auth flow
# ---------------------------------

@router.post("/signup")
async def signup(request: SignupRequest) -> dict:
    """
    Register a new user after Firebase client-side auth.

    Flow:
    1. Client authenticates via Firebase (email/Google)
    2. Client sends Firebase ID token to this endpoint
    3. We verify token, create Firestore user record
    4. Generate initial API key
    5. Return key — shown ONCE, never again

    Rules:
    - ID token verified server-side — never trust client claims
    - User record created in Firestore
    - One starter key generated automatically on signup
    - Full key returned once — user must save it
    """
    decoded = _verify_firebase_token(request.id_token)
    uid     = decoded["uid"]
    email   = decoded.get("email", "")

    # Check if user already exists
    existing = get_user(uid)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User already registered. Use /auth/login to get your details.",
        )

    # Create user record
    user = create_user(uid=uid, email=email, tier="starter")

    # Generate initial API key
    api_key    = generate_key(key_type="live")
    stored_key = to_stored_key(api_key)
    add_api_key(uid, stored_key)

    return {
        "uid":     uid,
        "email":   email,
        "tier":    "starter",
        "limits":  TIER_LIMITS["starter"],
        "api_key": {
            "key":      api_key["key"],     # shown ONCE
            "prefix":   api_key["prefix"],
            "key_type": api_key["key_type"],
            "warning":  "Save this key — it will not be shown again.",
        },
    }


@router.post("/login")
async def login(request: SignupRequest) -> dict:
    """
    Retrieve user profile for existing user.
    Verifies Firebase token — returns user record without keys.

    Keys are never returned after initial signup.
    If key is lost — revoke and generate a new one.
    """
    decoded = _verify_firebase_token(request.id_token)
    uid     = decoded["uid"]

    user = get_user(uid)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found. Please sign up first.",
        )

    # Return safe profile — no key hashes exposed
    safe_keys = [
        {
            "prefix":     k["prefix"],
            "key_type":   k["key_type"],
            "created_at": k["created_at"],
            "last_used":  k.get("last_used"),
            "active":     k["active"],
        }
        for k in user.get("api_keys", [])
    ]

    return {
        "uid":              uid,
        "email":            user["email"],
        "tier":             user["tier"],
        "limits":           TIER_LIMITS.get(user["tier"], {}),
        "api_keys":         safe_keys,
        "storage_settings": user.get("storage_settings", {}),
        "usage":            user.get("usage", {}),
    }


# ---------------------------------
# Key management
# ---------------------------------

@router.post("/keys/generate")
async def generate_api_key(request: GenerateKeyRequest) -> dict:
    """
    Generate a new API key for authenticated user.
    Enforces max_keys limit per tier.

    Key returned ONCE — user must save it.
    """
    decoded = _verify_firebase_token(request.id_token)
    uid     = decoded["uid"]

    user = get_user(uid)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    tier     = user.get("tier", "starter")
    max_keys = TIER_LIMITS[tier]["max_keys"]

    if max_keys == 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Free tier does not include API keys. Please sign up for a Starter account.",
        )

    api_key    = generate_key(key_type=request.key_type)
    stored_key = to_stored_key(api_key)
    success    = add_api_key(uid, stored_key)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"API key limit reached for {tier} tier ({max_keys} keys max).",
        )

    return {
        "api_key": {
            "key":      api_key["key"],     # shown ONCE
            "prefix":   api_key["prefix"],
            "key_type": api_key["key_type"],
            "warning":  "Save this key — it will not be shown again.",
        }
    }


@router.post("/keys/revoke")
async def revoke_key(request: RevokeKeyRequest) -> dict:
    """
    Revoke an API key by prefix.
    Key is deactivated — record preserved for audit.
    """
    decoded = _verify_firebase_token(request.id_token)
    uid     = decoded["uid"]

    success = revoke_api_key(uid, request.prefix)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Key with prefix '{request.prefix}' not found.",
        )

    return {
        "status":  "revoked",
        "prefix":  request.prefix,
        "message": "Key deactivated. Generate a new key if needed.",
    }


# ---------------------------------
# Storage settings
# ---------------------------------

@router.post("/storage/settings")
async def update_storage(request: StorageSettingsRequest) -> dict:
    """
    Update storage preferences for Pro/Enterprise users.
    Returns error if tier does not support storage.
    """
    decoded = _verify_firebase_token(request.id_token)
    uid     = decoded["uid"]

    user = get_user(uid)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    tier = user.get("tier", "starter")
    if not TIER_LIMITS.get(tier, {}).get("storage", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Storage is not available on the {tier} tier. Upgrade to Pro.",
        )

    success = update_storage_settings(
        uid=uid,
        enabled=request.enabled,
        retention_days=request.retention_days,
        auto_delete=request.auto_delete,
    )

    return {
        "status":   "updated",
        "settings": {
            "enabled":        request.enabled,
            "retention_days": request.retention_days,
            "auto_delete":    request.auto_delete,
        },
    }


# ---------------------------------
# Session history (Pro/Enterprise)
# ---------------------------------

@router.get("/sessions")
async def list_sessions(
    id_token: str,
    limit:    int = 20,
) -> dict:
    """
    List stored sessions for authenticated user.
    Returns empty list if storage not enabled.
    """
    decoded = _verify_firebase_token(id_token)
    uid     = decoded["uid"]

    user = get_user(uid)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    settings = user.get("storage_settings", {})
    if not settings.get("enabled", False):
        return {
            "sessions": [],
            "message":  "Storage not enabled. Enable in /auth/storage/settings.",
        }

    sessions = get_stored_sessions(uid, limit=min(limit, 100))
    return {
        "count":    len(sessions),
        "sessions": sessions,
    }


@router.delete("/sessions/{session_id}")
async def delete_one_session(
    session_id: str,
    id_token:   str,
) -> dict:
    """
    Delete a specific stored session.
    User-controlled deletion — GDPR compliant.
    """
    decoded = _verify_firebase_token(id_token)
    uid     = decoded["uid"]

    delete_session(uid, session_id)
    return {
        "status":     "deleted",
        "session_id": session_id,
    }


@router.delete("/sessions")
async def delete_all_user_sessions(
    id_token: str,
) -> dict:
    """
    Delete ALL stored sessions for authenticated user.
    Right to erasure — GDPR Article 17.
    """
    decoded = _verify_firebase_token(id_token)
    uid     = decoded["uid"]

    count = delete_all_sessions(uid)
    return {
        "status":          "deleted",
        "sessions_deleted": count,
        "message":         "All stored sessions permanently deleted.",
    }


# ---------------------------------
# Account deletion
# ---------------------------------

@router.delete("/account")
async def delete_account(
    id_token: str,
) -> dict:
    """
    Delete user account and all associated data.
    GDPR Article 17 — right to erasure.

    Deletes:
    - All stored sessions
    - User record in Firestore
    - Firebase Auth account
    """
    decoded = _verify_firebase_token(id_token)
    uid     = decoded["uid"]

    # Delete all sessions
    delete_all_sessions(uid)

    # Delete Firestore user record
    from api.auth.firestore import _get_db, USERS_COL
    _get_db().collection(USERS_COL).document(uid).delete()

    # Delete Firebase Auth account
    try:
        from firebase_admin import auth
        auth.delete_user(uid)
    except Exception:
        pass

    return {
        "status":  "deleted",
        "message": "Account and all associated data permanently deleted.",
    }

# Add to api/auth/router.py

from api.auth.workspace import (
    create_workspace,
    get_workspace,
    invite_member,
    remove_member,
    update_member_role,
    delete_workspace,
)


class CreateWorkspaceRequest(BaseModel):
    id_token: str
    name:     str


class InviteMemberRequest(BaseModel):
    id_token:     str
    member_email: str
    member_uid:   str
    role:         str = "member"


class UpdateRoleRequest(BaseModel):
    id_token:   str
    member_uid: str
    new_role:   str


@router.post("/workspace/create")
async def create_workspace_endpoint(
    request: CreateWorkspaceRequest,
) -> dict:
    """
    Create a workspace for Team/Enterprise user.
    Called automatically after Team tier upgrade.
    """
    decoded = _verify_firebase_token(request.id_token)
    uid     = decoded["uid"]

    user = get_user(uid)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    tier = user.get("tier", "starter")
    if tier not in ("team", "enterprise"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Workspace requires Team or Enterprise tier.",
        )

    workspace = create_workspace(
        owner_uid=uid,
        owner_email=user["email"],
        name=request.name,
        tier=tier,
    )

    return {
        "workspace_id": workspace["workspace_id"],
        "name":         workspace["name"],
        "tier":         workspace["tier"],
        "max_members":  workspace["max_members"],
    }


@router.get("/workspace")
async def get_workspace_endpoint(id_token: str) -> dict:
    """
    Get workspace details for authenticated user.
    """
    decoded = _verify_firebase_token(id_token)
    uid     = decoded["uid"]

    user = get_user(uid)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    workspace_id = user.get("workspace_id")
    if not workspace_id:
        raise HTTPException(
            status_code=404,
            detail="No workspace found. Create one or join a team.",
        )

    workspace = get_workspace(workspace_id)
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")

    # Return safe view — no internal IDs of other users exposed beyond email
    return {
        "workspace_id": workspace["workspace_id"],
        "name":         workspace["name"],
        "tier":         workspace["tier"],
        "member_count": workspace["member_count"],
        "max_members":  workspace["max_members"],
        "your_role":    user.get("workspace_role"),
        "members": [
            {
                "email":     m["email"],
                "role":      m["role"],
                "joined_at": m["joined_at"],
                "active":    m["active"],
            }
            for m in workspace.get("members", [])
            if m["active"]
        ],
        "usage":    workspace.get("usage", {}),
    }


@router.post("/workspace/invite")
async def invite_member_endpoint(
    request: InviteMemberRequest,
) -> dict:
    decoded      = _verify_firebase_token(request.id_token)
    uid          = decoded["uid"]
    user         = get_user(uid)
    workspace_id = user.get("workspace_id") if user else None

    if not workspace_id:
        raise HTTPException(status_code=404, detail="No workspace found.")

    success, message = invite_member(
        workspace_id=workspace_id,
        inviter_uid=uid,
        member_email=request.member_email,
        member_uid=request.member_uid,
        role=request.role,
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=message,
        )

    return {"status": "invited", "message": message}


@router.delete("/workspace/member/{member_uid}")
async def remove_member_endpoint(
    member_uid: str,
    id_token:   str,
) -> dict:
    decoded      = _verify_firebase_token(id_token)
    uid          = decoded["uid"]
    user         = get_user(uid)
    workspace_id = user.get("workspace_id") if user else None

    if not workspace_id:
        raise HTTPException(status_code=404, detail="No workspace found.")

    success, message = remove_member(
        workspace_id=workspace_id,
        remover_uid=uid,
        member_uid=member_uid,
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=message,
        )

    return {"status": "removed", "message": message}


@router.patch("/workspace/member/{member_uid}/role")
async def update_role_endpoint(
    member_uid: str,
    request:    UpdateRoleRequest,
) -> dict:
    decoded      = _verify_firebase_token(request.id_token)
    uid          = decoded["uid"]
    user         = get_user(uid)
    workspace_id = user.get("workspace_id") if user else None

    if not workspace_id:
        raise HTTPException(status_code=404, detail="No workspace found.")

    success, message = update_member_role(
        workspace_id=workspace_id,
        owner_uid=uid,
        member_uid=member_uid,
        new_role=request.new_role,
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=message,
        )

    return {"status": "updated", "message": message}
