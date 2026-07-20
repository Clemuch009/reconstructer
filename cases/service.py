# cases/service.py
#
# The glue: findings → cases, and precedent → evidence.
#
# Note what does NOT happen here: no finding is suppressed, downgraded, or
# auto-closed because of a precedent. See cases/store.py for why that matters.

import os
from typing import Any, Dict, List, Optional, Tuple

from cases.model import (
    Case, Resolution, new_case, new_resolution, OPEN, RESOLVED,
    SCOPE_PAIR, SCOPE_VENDOR,
)
from cases.store import InMemoryCaseStore, FirestoreCaseStore

# Which workflow actions put a document in front of a person. ALLOW does not
# need a case; BLOCK does — an auto-block is still a decision someone may need
# to overturn, and a blocked invoice that nobody can see is an invoice that
# silently never gets paid.
NEEDS_CASE = {"REVIEW", "ESCALATE", "BLOCK"}

_local_store: Optional[InMemoryCaseStore] = None


def get_case_store(uid: Optional[str]) -> Tuple[Optional[Any], str]:
    """(store, reason). None when cases cannot be persisted — reported, not hidden."""
    global _local_store
    if os.environ.get("QRYNT_LOCAL_STORAGE") == "1":
        if _local_store is None:
            _local_store = InMemoryCaseStore()
        return _local_store, "local in-process case store (not durable)"
    if not uid:
        return None, "not signed in — findings are not saved to a queue"
    try:
        from api.auth.firestore import _get_db
        return FirestoreCaseStore(_get_db(), uid), "cases"
    except Exception as e:                       # noqa: BLE001 — reported
        return None, f"case store unavailable ({type(e).__name__})"


def precedent_evidence(store, kind: str, vendor_canon: Optional[str],
                       limit: int = 3) -> List[Dict[str, Any]]:
    """Prior decisions about this kind of dispute for this vendor, as EVIDENCE.

    This is the whole value of the resolution store and the whole of its danger,
    depending on how it is used. Used as evidence, a reviewer reads "Jane decided
    this exact thing in July, because the vendor bills subsidiaries separately"
    and closes in seconds instead of re-investigating. Used as a suppressor, the
    same record silently disables the check and the next impersonation walks
    through. It is evidence.
    """
    if store is None or not vendor_canon:
        return []
    try:
        out = []
        for r in store.precedents(kind, vendor_canon)[:limit]:
            out.append({
                "case_id":   r.case_id,
                "decision":  r.decision,
                "rationale": r.rationale,
                "by":        r.resolved_by,
                "at":        r.resolved_at,
                "scope":     r.scope,
            })
        return out
    except Exception:                            # noqa: BLE001
        return []                                # precedent is a convenience;
                                                 # never fail a finding over it


def open_case_for(store, kind: str, subject: str, summary: str,
                  counterparts: List[str], vendor_canon: Optional[str],
                  finding: Dict[str, Any]) -> Optional[Case]:
    """Persist a finding as a case, unless the same dispute is already open.

    Idempotent on (kind, subject, counterparts): re-uploading a document must
    not spawn a second identical case, or the queue becomes noise and stops
    being read — which is the same as having no queue.
    """
    if store is None:
        return None
    try:
        existing = store.find_open(kind, subject, counterparts)
        if existing:
            return existing
        c = new_case(kind=kind, subject=subject, summary=summary,
                     counterparts=counterparts, vendor_canon=vendor_canon,
                     finding=finding)
        store.open_case(c)
        return c
    except Exception:                            # noqa: BLE001
        return None


def resolve_case(store, case_id: str, decision: str, rationale: str = "",
                 resolved_by: str = "", scope: str = SCOPE_PAIR) -> Optional[Resolution]:
    """Record a human decision and close the case.

    The rationale matters more than the decision: it is what the NEXT reviewer
    reads when the same pattern recurs. "Approved" teaches nothing; "vendor
    reuses invoice numbers across subsidiaries" closes the next case in seconds.
    """
    if store is None:
        return None
    c = store.get_case(case_id)
    if c is None:
        return None
    res = new_resolution(case_id=case_id, decision=decision, rationale=rationale,
                         resolved_by=resolved_by, scope=scope,
                         kind=c.kind, vendor_canon=c.vendor_canon)
    store.add_resolution(res)
    return res
