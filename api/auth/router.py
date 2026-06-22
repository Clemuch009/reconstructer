# api/auth/router.py

from typing import Optional
import secrets
import string
from datetime import datetime, timezone, timedelta
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
    _get_db,
    USERS_COL,
    SESSIONS_COL,
    KEY_INDEX_COL,
)
from api.auth.workspace import (
    create_workspace,
    get_workspace,
    remove_member,
    update_member_role,
    delete_workspace,
)
from api.dependencies import verify_api_key


router = APIRouter(prefix="/auth", tags=["Auth"])

PENDING_INVITES_COL = "pending_invites"


# ---------------------------------
# Firebase Auth client — lazy init
# ---------------------------------

_firebase_app = None

def _get_firebase():
    global _firebase_app
    if _firebase_app is None:
        import firebase_admin
        from firebase_admin import credentials, auth
        if not firebase_admin._apps:
            firebase_admin.initialize_app()
        _firebase_app = firebase_admin.get_app()
    return _firebase_app


def _verify_firebase_token(id_token: str) -> dict:
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


def _get_user_role(user: dict) -> str:
    return user.get("workspace_role", "member")


def _require_role(user: dict, minimum_role: str) -> None:
    hierarchy = {"owner": 3, "admin": 2, "member": 1}
    role = _get_user_role(user)
    if hierarchy.get(role, 0) < hierarchy.get(minimum_role, 0):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"This action requires {minimum_role} role or higher.",
        )


def _generate_invite_token() -> str:
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(32))


# ---------------------------------
# Request models
# ---------------------------------

class SignupRequest(BaseModel):
    id_token:     str
    invite_token: Optional[str] = None   # optional — from invite URL


class GenerateKeyRequest(BaseModel):
    id_token: str
    key_type: str = "live"


class RevokeKeyRequest(BaseModel):
    id_token: str
    prefix:   str


class StorageSettingsRequest(BaseModel):
    id_token:       str
    enabled:        bool
    retention_days: int = 7
    auto_delete:    bool = True


class CreateWorkspaceRequest(BaseModel):
    id_token: str
    name:     str


class InviteMemberRequest(BaseModel):
    id_token: str
    email:    str


class RenameWorkspaceRequest(BaseModel):
    id_token: str
    name:     str


class UpdateRoleRequest(BaseModel):
    id_token:   str
    member_uid: str
    new_role:   str


class DeleteAccountRequest(BaseModel):
    id_token: str


# ---------------------------------
# Auth flow
# ---------------------------------

@router.post("/signup")
async def signup(request: SignupRequest) -> dict:
    """
    Register a new user after Firebase client-side auth.
    Accepts optional invite_token — auto-joins workspace if valid.
    """
    decoded = _verify_firebase_token(request.id_token)
    uid     = decoded["uid"]
    email   = decoded.get("email", "")

    existing = get_user(uid)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User already registered. Use /auth/login.",
        )

    user = create_user(uid=uid, email=email, tier="starter")

    api_key    = generate_key(key_type="live")
    stored_key = to_stored_key(api_key)
    add_api_key(uid, stored_key)

    # Process invite token if present
    invite_message = None
    if request.invite_token:
        invite_message = _process_invite(uid, email, request.invite_token)

    result = {
        "uid":     uid,
        "email":   email,
        "tier":    "starter",
        "limits":  TIER_LIMITS["starter"],
        "api_key": {
            "key":      api_key["key"],
            "prefix":   api_key["prefix"],
            "key_type": api_key["key_type"],
            "warning":  "Save this key — it will not be shown again.",
        },
    }

    if invite_message:
        result["invite"] = invite_message

    return result


def _process_invite(uid: str, email: str, token: str) -> Optional[str]:
    """
    Process a workspace invite token on signup.
    Returns a status message or None on failure.
    """
    try:
        db  = _get_db()
        doc = db.collection(PENDING_INVITES_COL).document(token).get()
        if not doc.exists:
            return "Invite token not found or expired."

        invite = doc.to_dict()

        # Check not already used
        if invite.get("used", False):
            return "Invite token already used."

        # Check expiry
        expires_at = invite.get("expires_at", "")
        if expires_at and datetime.now(timezone.utc).isoformat() > expires_at:
            return "Invite token expired."

        # Check email matches (optional but recommended)
        if invite.get("invitee_email") and invite["invitee_email"] != email:
            return "Invite was for a different email address."

        workspace_id   = invite["workspace_id"]
        workspace_name = invite.get("workspace_name", "")
        role           = invite.get("role", "member")

        # Add member to workspace
        workspace = get_workspace(workspace_id)
        if not workspace:
            return "Workspace no longer exists."

        now = datetime.now(timezone.utc).isoformat()
        members = workspace.get("members", [])
        members.append({
            "uid":       uid,
            "email":     email,
            "role":      role,
            "joined_at": now,
            "active":    True,
        })

        db.collection("workspaces").document(workspace_id).update({
            "members":      members,
            "member_count": len([m for m in members if m.get("active", True)]),
        })

        # Update user record
        db.collection(USERS_COL).document(uid).update({
            "workspace_id":   workspace_id,
            "workspace_role": role,
            "workspace_name": workspace_name,
            "tier":           "team",  # grant team tier on joining
        })

        # Mark invite as used
        db.collection(PENDING_INVITES_COL).document(token).update({
            "used":    True,
            "used_by": uid,
            "used_at": now,
        })

        return f"Joined workspace '{workspace_name}' as {role}."

    except Exception as e:
        return f"Could not process invite: {str(e)[:100]}"


@router.post("/login")
async def login(request: SignupRequest) -> dict:
    decoded = _verify_firebase_token(request.id_token)
    uid     = decoded["uid"]

    user = get_user(uid)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found. Please sign up first.",
        )

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
        "workspace_id":     user.get("workspace_id"),
        "workspace_role":   user.get("workspace_role"),
        "workspace_name":   user.get("workspace_name"),
    }


# ---------------------------------
# Key management
# ---------------------------------

@router.post("/keys/generate")
async def generate_api_key(request: GenerateKeyRequest) -> dict:
    decoded = _verify_firebase_token(request.id_token)
    uid     = decoded["uid"]

    user = get_user(uid)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

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
            "key":      api_key["key"],
            "prefix":   api_key["prefix"],
            "key_type": api_key["key_type"],
            "warning":  "Save this key — it will not be shown again.",
        }
    }


@router.post("/keys/revoke")
async def revoke_key(request: RevokeKeyRequest) -> dict:
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
        "message": "Key deactivated.",
    }


# ---------------------------------
# Storage settings
# ---------------------------------

@router.post("/storage/settings")
async def update_storage(request: StorageSettingsRequest) -> dict:
    decoded = _verify_firebase_token(request.id_token)
    uid     = decoded["uid"]

    user = get_user(uid)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    tier = user.get("tier", "starter")
    if not TIER_LIMITS.get(tier, {}).get("storage", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Storage is not available on the {tier} tier. Upgrade to Pro.",
        )

    update_storage_settings(
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
# Session history
# ---------------------------------

@router.get("/sessions")
async def list_sessions(id_token: str, limit: int = 20) -> dict:
    decoded = _verify_firebase_token(id_token)
    uid     = decoded["uid"]

    user = get_user(uid)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    settings = user.get("storage_settings", {})
    if not settings.get("enabled", False):
        return {
            "sessions": [],
            "message":  "Storage not enabled. Enable in Settings → Document storage.",
        }

    sessions = get_stored_sessions(uid, limit=min(limit, 100))
    return {"count": len(sessions), "sessions": sessions}


@router.delete("/sessions/{session_id}")
async def delete_one_session(session_id: str, id_token: str) -> dict:
    decoded = _verify_firebase_token(id_token)
    uid     = decoded["uid"]
    delete_session(uid, session_id)
    return {"status": "deleted", "session_id": session_id}


@router.delete("/sessions")
async def delete_all_user_sessions(id_token: str) -> dict:
    decoded = _verify_firebase_token(id_token)
    uid     = decoded["uid"]
    count   = delete_all_sessions(uid)
    return {
        "status":           "deleted",
        "sessions_deleted": count,
        "message":          "All stored sessions permanently deleted.",
    }


# ---------------------------------
# Account deletion
# ---------------------------------

@router.delete("/account")
async def delete_account(request: DeleteAccountRequest) -> dict:
    decoded = _verify_firebase_token(request.id_token)
    uid     = decoded["uid"]

    db = _get_db()

    # ── Step 1: delete the Firebase Auth identity FIRST ──
    # The Auth user is what "resurrects" an account: if it survives, signing
    # up again with the same email returns the SAME uid and (if any Firestore
    # data also survived) restores the old account. So we must remove the Auth
    # identity before touching Firestore data — and if we CANNOT, we abort the
    # whole deletion loudly rather than half-deleting. A swallowed failure here
    # was the original bug (deleted account "came back" on re-signup).
    try:
        from firebase_admin import auth
        auth.revoke_refresh_tokens(uid)
        auth.delete_user(uid)
    except Exception as e:
        # Most common cause: the Cloud Run service account lacks the
        # 'firebaseauth.users.delete' permission (grant the Firebase
        # Authentication Admin role). Surface it instead of silently
        # leaving the account intact.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Could not delete the authentication account, so no data was "
                "removed. Please retry; if this persists, contact support. "
                f"({type(e).__name__})"
            ),
        )

    # ── Step 2: Auth identity is gone — now remove Firestore data ──
    # From here the account can no longer be signed into, so partial failures
    # cannot resurrect it. We still attempt every piece and report if cleanup
    # was incomplete (orphaned data is harmless but worth surfacing).
    cleanup_errors: list[str] = []

    # Delete sessions subcollection (Firestore doesn't cascade)
    try:
        sessions_ref = (
            db.collection(USERS_COL).document(uid).collection(SESSIONS_COL)
        )
        for doc in sessions_ref.stream():
            doc.reference.delete()
    except Exception as e:
        cleanup_errors.append(f"sessions:{type(e).__name__}")

    # Delete api_key_index entries for this user's keys
    try:
        user_doc = db.collection(USERS_COL).document(uid).get()
        if user_doc.exists:
            user = user_doc.to_dict()
            for key in user.get("api_keys", []):
                key_hash = key.get("key_hash")
                if key_hash:
                    try:
                        db.collection(KEY_INDEX_COL).document(key_hash).delete()
                    except Exception:
                        cleanup_errors.append("key_index_entry")
    except Exception as e:
        cleanup_errors.append(f"key_index:{type(e).__name__}")

    # Delete user document
    try:
        db.collection(USERS_COL).document(uid).delete()
    except Exception as e:
        cleanup_errors.append(f"user_doc:{type(e).__name__}")

    result = {"status": "deleted", "message": "Account permanently deleted."}
    if cleanup_errors:
        # Auth is gone (account is unusable), but some data cleanup failed.
        result["warning"] = (
            "Account access removed, but some data cleanup was incomplete: "
            + ", ".join(cleanup_errors)
        )
    return result


# ---------------------------------
# Workspace
# ---------------------------------

@router.post("/workspace/create")
async def create_workspace_endpoint(request: CreateWorkspaceRequest) -> dict:
    decoded = _verify_firebase_token(request.id_token)
    uid     = decoded["uid"]

    user = get_user(uid)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    name = request.name.strip() or user["email"].split("@")[0]

    workspace = create_workspace(
        owner_uid=uid,
        owner_email=user["email"],
        name=name,
        tier=user.get("tier", "starter"),
    )

    _get_db().collection(USERS_COL).document(uid).update({
        "workspace_id":   workspace["workspace_id"],
        "workspace_role": "owner",
        "workspace_name": name,
    })

    return {
        "workspace_id": workspace["workspace_id"],
        "name":         workspace["name"],
        "tier":         workspace["tier"],
    }


@router.get("/workspace")
async def get_workspace_endpoint(id_token: str) -> dict:
    decoded = _verify_firebase_token(id_token)
    uid     = decoded["uid"]

    user = get_user(uid)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    workspace_id = user.get("workspace_id")
    if not workspace_id:
        raise HTTPException(status_code=404, detail="No workspace found.")

    workspace = get_workspace(workspace_id)
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")

    return {
        "workspace_id": workspace["workspace_id"],
        "name":         workspace["name"],
        "tier":         workspace["tier"],
        "member_count": workspace.get("member_count", 1),
        "max_members":  workspace.get("max_members", 10),
        "your_role":    user.get("workspace_role", "owner"),
        "members": [
            {
                "uid":       m.get("uid", ""),
                "email":     m["email"],
                "role":      m["role"],
                "joined_at": m.get("joined_at"),
                "active":    m.get("active", True),
            }
            for m in workspace.get("members", [])
            if m.get("active", True)
        ],
    }


@router.post("/workspace/invite")
async def invite_member_endpoint(request: InviteMemberRequest) -> dict:
    """
    Create an invite link for a new member.
    Returns a URL the owner shares manually — no email sending needed.
    Invitee clicks the link → signs up → auto-joins workspace.
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
            detail="Team workspace requires Team or Enterprise plan.",
        )

    _require_role(user, "admin")

    workspace_id = user.get("workspace_id")
    if not workspace_id:
        raise HTTPException(status_code=404, detail="No workspace found. Create one first.")

    workspace = get_workspace(workspace_id)
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")

    # Check member limit
    active  = [m for m in workspace.get("members", []) if m.get("active", True)]
    max_m   = workspace.get("max_members", 10)
    if max_m != -1 and len(active) >= max_m:
        raise HTTPException(
            status_code=403,
            detail=f"Workspace is at member limit ({max_m}).",
        )

    # Check not already a member
    if request.email in [m["email"] for m in workspace.get("members", [])]:
        raise HTTPException(
            status_code=409,
            detail=f"{request.email} is already a member.",
        )

    # Create pending invite record
    token      = _generate_invite_token()
    db         = _get_db()
    now        = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=7)).isoformat()

    db.collection(PENDING_INVITES_COL).document(token).set({
        "token":          token,
        "workspace_id":   workspace_id,
        "workspace_name": workspace.get("name", ""),
        "inviter_uid":    uid,
        "inviter_email":  user["email"],
        "invitee_email":  request.email,
        "role":           "member",
        "created_at":     now.isoformat(),
        "expires_at":     expires_at,
        "used":           False,
    })

    invite_url = f"https://moonlit-grail-386316.web.app/signup?invite={token}"

    return {
        "status":     "created",
        "invite_url": invite_url,
        "email":      request.email,
        "expires_at": expires_at,
        "message":    f"Share this link with {request.email}. Expires in 7 days.",
    }


@router.post("/workspace/rename")
async def rename_workspace(request: RenameWorkspaceRequest) -> dict:
    """Rename workspace. Owner only."""
    decoded = _verify_firebase_token(request.id_token)
    uid     = decoded["uid"]

    user = get_user(uid)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    _require_role(user, "owner")

    workspace_id = user.get("workspace_id")
    if not workspace_id:
        raise HTTPException(status_code=404, detail="No workspace found.")

    name = request.name.strip()
    if not name or len(name) > 50:
        raise HTTPException(status_code=422, detail="Name must be 1–50 characters.")

    db = _get_db()
    db.collection("workspaces").document(workspace_id).update({"name": name})
    db.collection(USERS_COL).document(uid).update({"workspace_name": name})

    return {"status": "renamed", "name": name}


@router.delete("/workspace/member/{member_uid}")
async def remove_member_endpoint(member_uid: str, id_token: str) -> dict:
    decoded = _verify_firebase_token(id_token)
    uid     = decoded["uid"]

    user = get_user(uid)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    _require_role(user, "admin")

    workspace_id = user.get("workspace_id")
    if not workspace_id:
        raise HTTPException(status_code=404, detail="No workspace found.")

    success, message = remove_member(
        workspace_id=workspace_id,
        remover_uid=uid,
        member_uid=member_uid,
    )

    if not success:
        raise HTTPException(status_code=403, detail=message)

    return {"status": "removed", "message": message}


@router.patch("/workspace/member/{member_uid}/role")
async def update_role_endpoint(member_uid: str, request: UpdateRoleRequest) -> dict:
    decoded = _verify_firebase_token(request.id_token)
    uid     = decoded["uid"]

    user = get_user(uid)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    _require_role(user, "owner")

    workspace_id = user.get("workspace_id")
    if not workspace_id:
        raise HTTPException(status_code=404, detail="No workspace found.")

    success, message = update_member_role(
        workspace_id=workspace_id,
        owner_uid=uid,
        member_uid=member_uid,
        new_role=request.new_role,
    )

    if not success:
        raise HTTPException(status_code=403, detail=message)

    return {"status": "updated", "message": message}
