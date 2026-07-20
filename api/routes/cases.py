# api/routes/cases.py
#
# The review queue as an API.
#
# Two operations, and no more: see what needs a person, and record what they
# decided. Deliberately no assignment, priority, SLA, or bulk actions — that is
# ticketing software, and it is not what makes this valuable.
#
# What makes it valuable is the RATIONALE on a resolution. "Approved" teaches
# the organisation nothing. "Vertex bills each subsidiary separately and
# restarts numbering" closes the next identical case in five seconds instead of
# thirty minutes. The resolution store is the only durable output of a review.

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.middleware.auth import require_auth, RequestContext
from cases.model import OPEN, RESOLVED, SCOPE_PAIR, SCOPE_VENDOR
from cases.service import get_case_store, resolve_case, precedent_evidence

router = APIRouter()


class ResolveRequest(BaseModel):
    decision:  str                       # what was concluded
    rationale: str = ""                  # WHY — what the next reviewer reads
    scope:     str = SCOPE_PAIR          # this_pair | vendor_pattern


@router.get("/cases")
async def list_cases(status: Optional[str] = OPEN, limit: int = 100,
                     ctx: RequestContext = Depends(require_auth)):
    """The queue. Each case carries its finding snapshot (supporting, conflicts,
    risk, and what could NOT be determined from the documents) plus any
    precedent — prior decisions about this kind of dispute for this vendor.

    Precedent is shown as evidence to read, not as a reason the case was
    skipped: the case is here precisely because it was NOT skipped.
    """
    store, reason = get_case_store(ctx.uid)
    if store is None:
        return {"cases": [], "available": False, "reason": reason}
    if status not in (None, OPEN, RESOLVED):
        raise HTTPException(status_code=400,
                            detail=f"status must be '{OPEN}' or '{RESOLVED}'")
    out: List[Dict[str, Any]] = []
    for c in store.list_cases(status=status, limit=limit):
        d = c.to_dict()
        pres = precedent_evidence(store, c.kind, c.vendor_canon)
        # A case never cites itself as its own precedent.
        pres = [p for p in pres if p["case_id"] != c.case_id]
        if pres:
            d["precedent"] = pres
        if c.status == RESOLVED:
            d["resolutions"] = [r.to_dict() for r in store.resolutions_for(c.case_id)]
        out.append(d)
    return {"cases": out, "available": True, "reason": reason,
            "count": len(out), "status": status}


@router.get("/cases/{case_id}")
async def get_case(case_id: str, ctx: RequestContext = Depends(require_auth)):
    store, reason = get_case_store(ctx.uid)
    if store is None:
        raise HTTPException(status_code=503, detail=reason)
    c = store.get_case(case_id)
    if c is None:
        raise HTTPException(status_code=404, detail=f"no such case: {case_id}")
    d = c.to_dict()
    d["resolutions"] = [r.to_dict() for r in store.resolutions_for(case_id)]
    pres = [p for p in precedent_evidence(store, c.kind, c.vendor_canon)
            if p["case_id"] != case_id]
    if pres:
        d["precedent"] = pres
    return d


@router.post("/cases/{case_id}/resolve")
async def resolve(case_id: str, req: ResolveRequest,
                  ctx: RequestContext = Depends(require_auth)):
    """Record a decision and close the case.

    The resolution is append-only: a decision someone made on a date is as
    historical as a document's contents. Changing your mind means adding another
    resolution, never editing this one.

    `scope` is how far the decision reaches. `this_pair` (default) is the honest
    claim — a person looked at THESE documents and judged THEM. `vendor_pattern`
    says the pattern is normal for this vendor, and even then it only makes the
    decision visible as precedent on future cases. It does NOT stop them being
    raised: a resolution that silently disabled a check would mean one accepted
    tax-id change permanently blinds us to that vendor's impersonation.
    """
    if req.scope not in (SCOPE_PAIR, SCOPE_VENDOR):
        raise HTTPException(
            status_code=400,
            detail=f"scope must be '{SCOPE_PAIR}' or '{SCOPE_VENDOR}'")
    if not req.decision.strip():
        raise HTTPException(status_code=400, detail="a decision is required")
    store, reason = get_case_store(ctx.uid)
    if store is None:
        raise HTTPException(status_code=503, detail=reason)
    res = resolve_case(store, case_id, decision=req.decision,
                       rationale=req.rationale,
                       resolved_by=(ctx.uid or "local"), scope=req.scope)
    if res is None:
        raise HTTPException(status_code=404, detail=f"no such case: {case_id}")
    return {"resolved": True, "case_id": case_id, "resolution": res.to_dict()}
