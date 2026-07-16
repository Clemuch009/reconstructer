# relationship_engine/verdict.py
#
# The multi-dimensional reconciliation verdict.
#
# A reconciliation is NEVER a single MATCH / NO_MATCH. It is several
# INDEPENDENT dimensions, each with its own status and evidence:
#
#   Identity     — do these refer to the same transaction? (keys, vendor, ccy)
#   Financial    — do the numbers reconcile? (totals, tax) — ignores line order
#   Structural   — can the line items be aligned? (the alignment mapping)
#   Business     — did the business intent happen? (quantities, fulfillment)
#   Compliance   — policy: tolerances, approved vendor, active contract
#
# Collapsing these into one verdict is the dangerous move: a PO for 10 laptops
# vs an invoice for 8 can pass Identity and Financial (invoice total is
# internally fine) while FAILING Business (2 never delivered). One number hides
# that. Five dimensions surface it.
#
# ── Fail closed ────────────────────────────────────────────────────────────
# A dimension that was not evaluated is NOT_CHECKED — never silently PASS.
# "Absence of a detected problem" is not evidence of correctness. A verdict is
# only as strong as its weakest CHECKED dimension, and NOT_CHECKED dimensions
# prevent a clean overall result on their own. This is the reconciliation form
# of "loud failure over silent errors".

from dataclasses import dataclass, field
from typing import Any, Dict, List


# dimension identifiers
IDENTITY   = "identity"
FINANCIAL  = "financial"
STRUCTURAL = "structural"
BUSINESS   = "business"
COMPLIANCE = "compliance"

ALL_DIMENSIONS = [IDENTITY, FINANCIAL, STRUCTURAL, BUSINESS, COMPLIANCE]

# per-dimension status
PASS        = "PASS"
FAIL        = "FAIL"
NOT_CHECKED = "NOT_CHECKED"   # fail-closed default — never assume PASS

# overall outcomes (derived, never a bare "MATCH")
RECONCILED         = "RECONCILED"          # every dimension CHECKED and PASS
EXCEPTION          = "EXCEPTION"           # at least one CHECKED dimension FAIL
INCOMPLETE         = "INCOMPLETE"          # no FAILs, but some NOT_CHECKED


@dataclass
class Dimension:
    name:     str
    status:   str = NOT_CHECKED
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    detail:   Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "evidence": self.evidence, "detail": self.detail}


class Verdict:
    """Accumulates dimension results and derives an overall outcome.

    Overall logic (fail-closed):
      • any CHECKED dimension FAIL  → EXCEPTION
      • else any NOT_CHECKED        → INCOMPLETE  (cannot claim reconciled)
      • else                        → RECONCILED
    """

    def __init__(self):
        self.dimensions: Dict[str, Dimension] = {
            d: Dimension(d) for d in ALL_DIMENSIONS
        }

    def set(self, name: str, status: str,
            evidence: List[Dict[str, Any]] = None,
            detail: Dict[str, Any] = None) -> "Verdict":
        dim = self.dimensions[name]
        dim.status = status
        if evidence is not None:
            dim.evidence = evidence
        if detail is not None:
            dim.detail = detail
        return self

    def overall(self) -> str:
        statuses = [d.status for d in self.dimensions.values()]
        if FAIL in statuses:
            return EXCEPTION
        if NOT_CHECKED in statuses:
            return INCOMPLETE
        return RECONCILED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overall":    self.overall(),
            "dimensions": {n: d.to_dict() for n, d in self.dimensions.items()},
            # convenience: which dimensions blocked a clean result
            "failed":      [n for n, d in self.dimensions.items() if d.status == FAIL],
            "not_checked": [n for n, d in self.dimensions.items() if d.status == NOT_CHECKED],
        }
