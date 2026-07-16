# relationship_engine/__init__.py
#
# Relationship Engine — a generic engine for discovering relationships between
# two sets of records.
#
# This is deliberately NOT named "reconciliation": reconciliation is one
# application. The same pair-matching machinery — match records across two
# sources and emit relationship objects with evidence — also powers
# deduplication (A against itself), version comparison (A against A'), change
# detection, and cross-document linking. One engine, many uses.
#
# ── The stack (capabilities, invoked by a planner — not a fixed pipeline) ──
#   Normalization        canonicalize values           (Acme Ltd == ACME LTD.)
#   Identity Resolution   are two canonical things one entity?
#   Candidate Generation  who could possibly match? (prunes the N² space)
#   Pair Matching (1:1)   MATCH / MISMATCH / LEFT_ONLY / RIGHT_ONLY
#   Consistency           reuse the Rules Engine — validate before comparing
#   Reporting             relationship objects + evidence
#
#   DEFERRED (named, not built): Group (1:N/N:1/N:N — evidence-based grouping,
#   NOT merely subset-sum), Structural (line-level), Graph (cross-entity),
#   Temporal, Probabilistic. Each is a capability the planner will invoke when
#   the data needs it; none is required to reconcile the first real dataset.
#   The deterministic core stops BEFORE the probabilistic layer — determinism
#   is the product's brand; probabilistic matching is a quarantined extension.
#
# ── The universal currency: the Relationship Object ────────────────────────
# Every capability emits, and higher capabilities consume, the same object:
#
#   {
#     "left":         <record or key or None>,
#     "right":        <record or key or None>,
#     "relationship": MATCH | MISMATCH | LEFT_ONLY | RIGHT_ONLY,
#     "confidence":   0..1,
#     "evidence":     [ {field, status, ...}, ... ],
#   }
#
# Building the foundation once means every advanced capability reuses it
# instead of replacing it.

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── Relationship vocabulary ───────────────────────────────────────────────

MATCH      = "MATCH"        # paired, and consistent
MISMATCH   = "MISMATCH"     # paired, but a compared value differs  (an EXCEPTION)
LEFT_ONLY  = "LEFT_ONLY"    # present in A, no counterpart in B      (an EXCEPTION)
RIGHT_ONLY = "RIGHT_ONLY"   # present in B, no counterpart in A      (an EXCEPTION)

# The three EXCEPTION relationships are the product's real value: everyone can
# report the clean matches; the differentiator is surfacing precisely what did
# NOT reconcile, and why, instead of dumping it back on a human to re-audit.
EXCEPTIONS = {MISMATCH, LEFT_ONLY, RIGHT_ONLY}


@dataclass
class Relationship:
    """A discovered relationship between a record in A and/or a record in B.

    `left` / `right` hold the records (or None for LEFT_ONLY / RIGHT_ONLY).
    `key` is the identity value that paired them (or the lone side's key).
    `evidence` is the per-field detail from the consistency check — the seed of
    an auditable explanation, never just a verdict.
    """
    relationship: str
    key:          Any
    left:         Optional[Dict[str, Any]] = None
    right:        Optional[Dict[str, Any]] = None
    confidence:   float = 1.0
    evidence:     List[Dict[str, Any]] = field(default_factory=list)

    def is_exception(self) -> bool:
        return self.relationship in EXCEPTIONS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "relationship": self.relationship,
            "key":          self.key,
            "left":         self.left,
            "right":        self.right,
            "confidence":   self.confidence,
            "evidence":     self.evidence,
        }
