# api/routes/knowledge.py
#
# "Tell me everything you know about X."
#
# One endpoint, subject-addressed. `subject` is a parameter rather than a path
# segment so invoice / purchase_order / payment / tax_id can be added later
# without a new route, a new client, or a new shape — the frontend keeps asking
# the same question.
#
# Only `vendor` is implemented. That is deliberate: one subject answered
# properly is worth more than six answered partially, and an unsupported subject
# says so plainly rather than returning an empty shell that looks like "we know
# nothing about this."

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.middleware.auth import require_auth, RequestContext
from registry.factory import get_registry
from registry.knowledge import build_vendor_view
from relationship_engine.canonicalize import canon_vendor
from cases.service import get_case_store

router = APIRouter()

SUPPORTED = ("vendor",)


@router.get("/knowledge")
async def knowledge(
    subject: str = Query("vendor", description="what kind of thing to describe"),
    id: str = Query(..., description="its identifier, e.g. a vendor name"),
    ctx: RequestContext = Depends(require_auth),
):
    """Everything known about one subject, assembled by the registry.

    The caller does not join anything. It asks one question and receives the
    whole picture: identity and aliases, observed tax ids over time, document
    counts, the vendor's house patterns, open and resolved cases with the
    evidence behind each, the behaviour the organisation has learned, and a
    timeline.

    What it will NOT return is a risk score, a confidence percentage, or an
    approved/rejected tally. There is no risk model, no confidence model, and no
    per-document decision store — so those numbers could only be invented, and
    an invented number is the first thing a reviewer would ask about and the
    last thing that should be trusted. `basis` says instead what the picture
    rests on: how many documents, since when.
    """
    if subject not in SUPPORTED:
        raise HTTPException(
            status_code=400,
            detail=(f"subject '{subject}' is not supported yet — "
                    f"available: {', '.join(SUPPORTED)}"))
    if not id.strip():
        raise HTTPException(status_code=400, detail="id is required")

    registry, reason = get_registry(ctx.uid)
    if registry is None:
        # No history to describe. Reported, not disguised as an empty vendor.
        return {"subject": {"type": subject, "id": id}, "known": False,
                "available": False, "reason": reason}

    # Match on the canonical form: a user typing "Horizon IT Solutions Ltd" and a
    # document saying "HORIZON IT SOLUTIONS" are the same vendor, and the
    # registry is keyed on the canonical form precisely so that holds.
    vendor_canon = canon_vendor(id)
    if not vendor_canon:
        raise HTTPException(status_code=400, detail=f"could not read a vendor from {id!r}")

    case_store, _ = get_case_store(ctx.uid)
    view = build_vendor_view(vendor_canon, registry, case_store)
    view["available"] = True
    view["query"] = {"asked": id, "resolved_to": vendor_canon}
    return view
