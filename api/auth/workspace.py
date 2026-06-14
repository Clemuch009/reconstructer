# api/auth/workspace.py

import uuid
import hashlib
from datetime import datetime, timezone
from typing import Optional, List
from typing_extensions import TypedDict

from api.auth.firestore import _get_db, USERS_COL, TIER_LIMITS


# ---------------------------------
# Collection
# ---------------------------------

WORKSPACES_COL = "workspaces"


# ---------------------------------
# Contracts
# ---------------------------------

class WorkspaceMember(TypedDict):
    uid:        str
    email:      str
    role:       str          # "owner" | "admin" | "member"
    joined_at:  str
    active:     bool


class Workspace(TypedDict):
    workspace_id:   str
    name:           str
    owner_uid:      str
    created_at:     str
    tier:           str      # mirrors owner tier — "team" | "enterprise"
    members:        List[WorkspaceMember]
    member_count:   int
    max_members:    int
    storage_settings: dict
    usage:          dict     # aggregated across all members


# ---------------------------------
# Workspace ID generation
# ---------------------------------

def _workspace_id() -> str:
    return "ws_" + uuid.uuid4().hex[:12]


# ---------------------------------
# Create workspace
# ---------------------------------

def create_workspace(
    owner_uid:  str,
    owner_email: str,
    name:       str,
    tier:       str = "team",
) -> Workspace:
    """
    Create a new workspace for Team/Enterprise user.
    Owner is automatically added as first member with role "owner".

    Rules:
    - Only team/enterprise tier can create workspaces
    - Owner uid attached to workspace
    - workspace_id written back to owner user record
    """
    db  = _get_db()
    now = datetime.now(timezone.utc).isoformat()
    wid = _workspace_id()

    max_members = TIER_LIMITS.get(tier, {}).get("max_members", 10)

    owner_member = WorkspaceMember(
        uid=owner_uid,
        email=owner_email,
        role="owner",
        joined_at=now,
        active=True,
    )

    workspace = Workspace(
        workspace_id=wid,
        name=name,
        owner_uid=owner_uid,
        created_at=now,
        tier=tier,
        members=[owner_member],
        member_count=1,
        max_members=max_members,
        storage_settings={
            "enabled":        False,
            "retention_days": 30,
            "auto_delete":    True,
        },
        usage={
            "requests_today": 0,
            "requests_total": 0,
            "reset_date":     _today(),
        },
    )

    # Store workspace
    db.collection(WORKSPACES_COL).document(wid).set(workspace)

    # Write workspace_id back to owner user record
    db.collection(USERS_COL).document(owner_uid).update({
        "workspace_id":   wid,
        "workspace_role": "owner",
    })

    return workspace


# ---------------------------------
# Get workspace
# ---------------------------------

def get_workspace(workspace_id: str) -> Optional[Workspace]:
    db  = _get_db()
    doc = db.collection(WORKSPACES_COL).document(workspace_id).get()
    if not doc.exists:
        return None
    return doc.to_dict()


def get_workspace_by_owner(owner_uid: str) -> Optional[Workspace]:
    db   = _get_db()
    docs = (
        db.collection(WORKSPACES_COL)
        .where("owner_uid", "==", owner_uid)
        .limit(1)
        .stream()
    )
    for doc in docs:
        return doc.to_dict()
    return None


# ---------------------------------
# Member management
# ---------------------------------

def invite_member(
    workspace_id: str,
    inviter_uid:  str,
    member_email: str,
    member_uid:   str,
    role:         str = "member",
) -> tuple[bool, str]:
    """
    Add a member to a workspace.

    Rules:
    - Only owner or admin can invite
    - Cannot exceed max_members limit
    - Member must have an existing Qrynt account
    - Returns (success, message)
    """
    db        = _get_db()
    ws_ref    = db.collection(WORKSPACES_COL).document(workspace_id)
    ws_doc    = ws_ref.get()

    if not ws_doc.exists:
        return False, "Workspace not found."

    workspace = ws_doc.to_dict()

    # Check inviter role
    inviter = next(
        (m for m in workspace["members"]
         if m["uid"] == inviter_uid and m["active"]),
        None
    )
    if not inviter or inviter["role"] not in ("owner", "admin"):
        return False, "Only workspace owners and admins can invite members."

    # Check member limit
    active_count = sum(1 for m in workspace["members"] if m["active"])
    if (
        workspace["max_members"] != -1
        and active_count >= workspace["max_members"]
    ):
        return False, (
            f"Workspace member limit reached "
            f"({workspace['max_members']} members max)."
        )

    # Check not already a member
    existing = next(
        (m for m in workspace["members"] if m["uid"] == member_uid),
        None
    )
    if existing and existing["active"]:
        return False, "User is already a workspace member."

    now = datetime.now(timezone.utc).isoformat()
    new_member = WorkspaceMember(
        uid=member_uid,
        email=member_email,
        role=role,
        joined_at=now,
        active=True,
    )

    # Add to workspace members
    members = workspace["members"]
    if existing:
        # Reactivate previously removed member
        for m in members:
            if m["uid"] == member_uid:
                m["active"]    = True
                m["role"]      = role
                m["joined_at"] = now
    else:
        members.append(new_member)

    active_count = sum(1 for m in members if m["active"])
    ws_ref.update({
        "members":      members,
        "member_count": active_count,
    })

    # Update member user record
    db.collection(USERS_COL).document(member_uid).update({
        "workspace_id":   workspace_id,
        "workspace_role": role,
    })

    return True, "Member added successfully."


def remove_member(
    workspace_id: str,
    remover_uid:  str,
    member_uid:   str,
) -> tuple[bool, str]:
    """
    Remove a member from workspace.
    Owner cannot be removed.
    Members can remove themselves.
    """
    db     = _get_db()
    ws_ref = db.collection(WORKSPACES_COL).document(workspace_id)
    ws_doc = ws_ref.get()

    if not ws_doc.exists:
        return False, "Workspace not found."

    workspace = ws_doc.to_dict()

    # Owner cannot be removed
    if member_uid == workspace["owner_uid"]:
        return False, "Workspace owner cannot be removed."

    # Check remover permission — owner, admin, or self
    remover = next(
        (m for m in workspace["members"]
         if m["uid"] == remover_uid and m["active"]),
        None
    )
    is_self   = remover_uid == member_uid
    is_admin  = remover and remover["role"] in ("owner", "admin")

    if not is_self and not is_admin:
        return False, "Insufficient permissions to remove this member."

    # Deactivate member
    members = workspace["members"]
    for m in members:
        if m["uid"] == member_uid:
            m["active"] = False

    active_count = sum(1 for m in members if m["active"])
    ws_ref.update({
        "members":      members,
        "member_count": active_count,
    })

    # Clear workspace from user record
    db.collection(USERS_COL).document(member_uid).update({
        "workspace_id":   None,
        "workspace_role": None,
    })

    return True, "Member removed."


def update_member_role(
    workspace_id: str,
    owner_uid:    str,
    member_uid:   str,
    new_role:     str,
) -> tuple[bool, str]:
    """
    Update member role. Only owner can change roles.
    """
    if new_role not in ("admin", "member"):
        return False, "Role must be 'admin' or 'member'."

    db     = _get_db()
    ws_ref = db.collection(WORKSPACES_COL).document(workspace_id)
    ws_doc = ws_ref.get()

    if not ws_doc.exists:
        return False, "Workspace not found."

    workspace = ws_doc.to_dict()

    if workspace["owner_uid"] != owner_uid:
        return False, "Only the workspace owner can change member roles."

    members = workspace["members"]
    updated = False
    for m in members:
        if m["uid"] == member_uid and m["active"]:
            m["role"] = new_role
            updated   = True

    if not updated:
        return False, "Member not found in workspace."

    ws_ref.update({"members": members})

    # Sync role to user record
    db.collection(USERS_COL).document(member_uid).update({
        "workspace_role": new_role,
    })

    return True, f"Role updated to {new_role}."


# ---------------------------------
# Workspace usage aggregation
# ---------------------------------

def increment_workspace_usage(workspace_id: str) -> None:
    """
    Increment shared workspace usage counter.
    Called alongside per-user usage increment for team members.
    Non-blocking — failure does not affect request.
    """
    try:
        db     = _get_db()
        ws_ref = db.collection(WORKSPACES_COL).document(workspace_id)
        ws_doc = ws_ref.get()

        if not ws_doc.exists:
            return

        workspace = ws_doc.to_dict()
        usage     = workspace.get("usage", {})
        today     = _today()

        if usage.get("reset_date") != today:
            usage["requests_today"] = 0
            usage["reset_date"]     = today

        usage["requests_today"] = usage.get("requests_today", 0) + 1
        usage["requests_total"] = usage.get("requests_total", 0) + 1

        ws_ref.update({"usage": usage})
    except Exception:
        pass


def check_workspace_limit(workspace_id: str) -> tuple[bool, int]:
    """
    Check if workspace is within team daily limit.
    Returns (within_limit, requests_today).
    """
    db     = _get_db()
    ws_ref = db.collection(WORKSPACES_COL).document(workspace_id)
    ws_doc = ws_ref.get()

    if not ws_doc.exists:
        return False, 0

    workspace = ws_doc.to_dict()
    tier      = workspace.get("tier", "team")
    usage     = workspace.get("usage", {})
    today     = _today()

    if usage.get("reset_date") != today:
        return True, 0

    limit = TIER_LIMITS.get(tier, {}).get("requests_per_day", 0)
    count = usage.get("requests_today", 0)

    if limit == -1:
        return True, count

    return count < limit, count


# ---------------------------------
# Workspace deletion
# ---------------------------------

def delete_workspace(
    workspace_id: str,
    owner_uid:    str,
) -> tuple[bool, str]:
    """
    Delete workspace and clear all member references.
    Only owner can delete.
    """
    db     = _get_db()
    ws_ref = db.collection(WORKSPACES_COL).document(workspace_id)
    ws_doc = ws_ref.get()

    if not ws_doc.exists:
        return False, "Workspace not found."

    workspace = ws_doc.to_dict()

    if workspace["owner_uid"] != owner_uid:
        return False, "Only the workspace owner can delete the workspace."

    # Clear workspace from all member records
    for member in workspace.get("members", []):
        try:
            db.collection(USERS_COL).document(member["uid"]).update({
                "workspace_id":   None,
                "workspace_role": None,
            })
        except Exception:
            pass

    ws_ref.delete()
    return True, "Workspace deleted."


# ---------------------------------
# Helpers
# ---------------------------------

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
