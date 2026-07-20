# cases/model.py
#
# CASES — what is disputed. Deliberately NOT part of the registry.
#
#   Knowledge  answers "what do we know?"        → registry/  (append-only facts)
#   Cases      answer  "what is disputed?"       → here       (workflow state)
#   Resolutions answer "what did we decide?"     → here       (append-only evidence)
#
# Mixing them is the mistake. A duplicate flag stored beside an invoice's fields
# is a conclusion pretending to be a fact; it rots the moment a correction
# arrives or the engine improves. So the registry stores only what documents
# asserted, findings are recomputed on read, and a CASE exists only when a
# finding needed a human.
#
# ── A case is a finding that got persisted because it needed a person ──────
# There is no new taxonomy here. `kind` IS the finding kind — DUPLICATE,
# IDENTITY_COLLISION, PO_MISMATCH, TAX_ID_CHANGED, PAYMENT_CONFLICT,
# INTERNAL_INCONSISTENCY... The findings already carry supporting/conflicts/risk
# and name what they cannot know. A case adds exactly two things a finding
# lacks: it persists, and it can be closed.
#
# ── Status is mutable, and that is not a contradiction ────────────────────
# The append-only rule governs ASSERTIONS — what a document said, which is
# historical fact and can never change. A case's status is workflow, not
# knowledge: it is genuinely open, then genuinely resolved. Resolutions,
# however, ARE append-only: a decision someone made on a date is as historical
# as a document's contents, and superseding one means adding another.
#
# ── What is NOT here, on purpose ──────────────────────────────────────────
# No SLA, owner, priority, assignment, or linked-case graph. That is ticketing
# software; Jira and Zendesk exist and are better at it. This is the smallest
# thing that lets a human close a loop and lets the next document benefit.

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import uuid

OPEN     = "open"
RESOLVED = "resolved"

# Resolution scope — how far a human's decision reaches.
#
# SCOPE_PAIR is the default and the honest one: a reviewer looked at THESE
# documents and judged THEM. That is all they actually did.
#
# SCOPE_VENDOR says "this pattern is normal for this vendor". It is opt-in, and
# critically it still does NOT suppress anything — see cases/precedent.py. A
# resolution that silently disabled a future check would institutionalise the
# first fraud that got approved: an attacker needs one accepted tax-id change
# and then owns the vendor forever, because the detection goes dark.
SCOPE_PAIR   = "this_pair"
SCOPE_VENDOR = "vendor_pattern"


@dataclass
class Case:
    case_id: str
    kind: str                                   # = the finding kind
    status: str = OPEN
    subject: str = ""                           # doc_id under evaluation
    counterparts: List[str] = field(default_factory=list)   # evidence_from
    vendor_canon: Optional[str] = None          # for precedent lookup
    summary: str = ""
    finding: Dict[str, Any] = field(default_factory=dict)   # the snapshot
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Case":
        known = set(Case.__dataclass_fields__)
        return Case(**{k: v for k, v in d.items() if k in known})


@dataclass
class Resolution:
    """A human decision. Durable EVIDENCE, not an inference.

    This is the one thing that legitimately persists beside the assertions: it
    does not go stale when the engine improves, because a person genuinely
    decided it on a date. It is the only durable output of a review — and the
    reason the same collision should not cost thirty minutes twice.
    """
    resolution_id: str
    case_id: str
    decision: str                               # free text: what was concluded
    rationale: str = ""                         # why — this is what a future
                                                # reviewer actually reads
    resolved_by: str = ""
    resolved_at: str = ""
    scope: str = SCOPE_PAIR
    kind: str = ""                              # denormalised for precedent query
    vendor_canon: Optional[str] = None           # denormalised for precedent query

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Resolution":
        known = set(Resolution.__dataclass_fields__)
        return Resolution(**{k: v for k, v in d.items() if k in known})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_case(kind: str, subject: str, summary: str = "",
             counterparts: Optional[List[str]] = None,
             vendor_canon: Optional[str] = None,
             finding: Optional[Dict[str, Any]] = None) -> Case:
    return Case(
        case_id=f"case_{uuid.uuid4().hex[:12]}",
        kind=kind,
        status=OPEN,
        subject=subject,
        counterparts=list(counterparts or []),
        vendor_canon=vendor_canon,
        summary=summary,
        finding=dict(finding or {}),
        created_at=_now(),
    )


def new_resolution(case_id: str, decision: str, rationale: str = "",
                   resolved_by: str = "", scope: str = SCOPE_PAIR,
                   kind: str = "", vendor_canon: Optional[str] = None) -> Resolution:
    if scope not in (SCOPE_PAIR, SCOPE_VENDOR):
        raise ValueError(f"unknown scope: {scope}")
    return Resolution(
        resolution_id=f"res_{uuid.uuid4().hex[:12]}",
        case_id=case_id,
        decision=decision,
        rationale=rationale,
        resolved_by=resolved_by,
        resolved_at=_now(),
        scope=scope,
        kind=kind,
        vendor_canon=vendor_canon,
    )
