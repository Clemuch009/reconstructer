# relationship_engine/approval.py
#
# Approval Recommendation Engine.
#
# This engine RECOMMENDS; it never approves. Like an autopilot that reports
# "landing conditions satisfied" rather than "landing complete", it produces an
# explainable recommendation and leaves authorization to a human. A wrong
# approval is far worse than an unmatched record, so this layer is the most
# conservative in the system.
#
# It consumes a multi-dimensional Verdict (relationship_engine.verdict) plus
# optional risk signals, and emits a recommendation with per-dimension
# confidence, blocking conditions, and auditable reasons.
#
# ── Fail closed — the core philosophy ──────────────────────────────────────
# APPROVE requires POSITIVE evidence that it is safe. "We didn't detect a
# problem" is NOT sufficient. Any dimension that is NOT_CHECKED, any blocking
# condition, any unmet required check → the recommendation is MANUAL_REVIEW (or
# NEVER), never RECOMMEND_APPROVE. Absence of evidence of a problem is not
# evidence of absence of a problem.
#
# ── Recommendations (never a bare "approved") ──────────────────────────────
#   NEVER_APPROVE   — a blocking condition fired; no confidence overrides it
#   MANUAL_REVIEW   — cannot establish positive safety (NOT_CHECKED / FAIL)
#   RECOMMEND_APPROVE — every required check passed with positive evidence
#   (AUTO_APPROVE is intentionally NOT emitted here — auto-approval is a policy
#    decision for the consuming system, gated on RECOMMEND_APPROVE + its own
#    thresholds.)

from typing import Any, Dict, List

from relationship_engine.verdict import (
    IDENTITY, FINANCIAL, STRUCTURAL, BUSINESS, COMPLIANCE,
    PASS, FAIL, NOT_CHECKED,
)


NEVER_APPROVE     = "NEVER_APPROVE"
MANUAL_REVIEW     = "MANUAL_REVIEW"
RECOMMEND_APPROVE = "RECOMMEND_APPROVE"

NOT_ASSESSED = "NOT_ASSESSED"   # a risk signal that needs data we don't have


# ── Blocking conditions ────────────────────────────────────────────────────
#
# Hard stops. No confidence, however high, overrides these. Each is a function
# (verdict, context) -> reason string or None. They encode the "never approve"
# cases from the report: duplicate roots, multi-document suspicion, inconsistent
# totals, ambiguous identity.

def _block_identity_failed(verdict, ctx):
    if verdict["dimensions"][IDENTITY]["status"] == FAIL:
        return "identity dimension FAILED — documents may not be the same transaction"
    return None

def _block_financial_failed(verdict, ctx):
    if verdict["dimensions"][FINANCIAL]["status"] == FAIL:
        return "financial dimension FAILED — totals/amounts do not reconcile"
    return None

def _block_multi_document(verdict, ctx):
    # partition suspicion is passed in context from the resolver
    p = ctx.get("partition", {})
    if p.get("status") not in (None, "SINGLE_DOCUMENT"):
        return f"multiple-document suspicion ({p.get('status')}) — resolve boundaries first"
    return None

def _block_competing_identity(verdict, ctx):
    # competing root entities detected upstream (resolver boundary_candidates)
    p = ctx.get("partition", {})
    if p.get("boundary_candidates"):
        fields = [c.get("field") for c in p["boundary_candidates"]]
        return f"competing values for anchor field(s): {', '.join(fields)}"
    return None

def _block_duplicate(verdict, ctx):
    if ctx.get("duplicate_detected"):
        return "duplicate document detected — possible duplicate payment"
    return None


_BLOCKING_CONDITIONS = [
    _block_identity_failed,
    _block_financial_failed,
    _block_multi_document,
    _block_competing_identity,
    _block_duplicate,
]


# ── Gates ───────────────────────────────────────────────────────────────────
#
# Gates run in order. Each reports PASS / FAIL / NOT_CHECKED for a scope. A gate
# maps to verdict dimensions plus, for Risk, to external signals. Gates do not
# themselves block — they feed the fail-closed decision below — but a FAIL in
# any required gate prevents RECOMMEND_APPROVE.

def _gate_identity(verdict, ctx):
    return {"gate": "identity", "status": verdict["dimensions"][IDENTITY]["status"]}

def _gate_integrity(verdict, ctx):
    # integrity = financial + structural internal consistency
    fin = verdict["dimensions"][FINANCIAL]["status"]
    struct = verdict["dimensions"][STRUCTURAL]["status"]
    if FAIL in (fin, struct):
        status = FAIL
    elif NOT_CHECKED in (fin, struct):
        status = NOT_CHECKED
    else:
        status = PASS
    return {"gate": "integrity", "status": status,
            "detail": {"financial": fin, "structural": struct}}

def _gate_business(verdict, ctx):
    return {"gate": "business", "status": verdict["dimensions"][BUSINESS]["status"]}

def _gate_risk(verdict, ctx):
    # Risk signals often need company data (new vendor? already paid? expired
    # PO?). Those we lack are NOT_ASSESSED (fail-closed — an unassessed risk is
    # not a passed risk). Signals present in context are evaluated.
    signals = ctx.get("risk_signals", {})
    declared = ["new_vendor", "already_paid", "expired_po", "unexpected_tax",
                "future_invoice_date"]
    results = {}
    any_fail = False
    any_unassessed = False
    for name in declared:
        if name in signals:
            val = bool(signals[name])
            results[name] = "FLAGGED" if val else "CLEAR"
            if val:
                any_fail = True
        else:
            results[name] = NOT_ASSESSED
            any_unassessed = True
    if any_fail:
        status = FAIL
    elif any_unassessed:
        status = NOT_CHECKED
    else:
        status = PASS
    return {"gate": "risk", "status": status, "detail": results}


_GATES = [_gate_identity, _gate_integrity, _gate_business, _gate_risk]

# Which gates MUST positively PASS for an approval recommendation. Compliance
# is not gated here (it lives in the verdict and is NOT_CHECKED without policy
# data); it is surfaced but the fail-closed rule below already blocks on any
# NOT_CHECKED required dimension.
_REQUIRED_GATES = ["identity", "integrity", "business", "risk"]


# ── Public API ──────────────────────────────────────────────────────────────

def recommend(
    verdict: Dict[str, Any],
    context: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """
    Produce an approval recommendation from a multi-dimensional verdict.

    context (optional) supplies signals the verdict doesn't carry:
      "partition":          resolver partition status + boundary_candidates
      "duplicate_detected": bool (from duplicate detection, if run)
      "risk_signals":       {new_vendor: bool, already_paid: bool, ...}

    Returns:
      {
        "recommendation": NEVER_APPROVE | MANUAL_REVIEW | RECOMMEND_APPROVE,
        "blocking":       [reasons...],        # hard stops, if any
        "gates":          [ {gate, status, ...} ],
        "confidence":     {dimension: status}, # multi-dimensional, never one #
        "reasons":        [auditable reasons], # why this recommendation
      }
    """
    ctx = context or {}

    # 1. Blocking conditions — hard stops, no confidence overrides them.
    blocking = []
    for cond in _BLOCKING_CONDITIONS:
        reason = cond(verdict, ctx)
        if reason:
            blocking.append(reason)

    # 2. Gates.
    gates = [g(verdict, ctx) for g in _GATES]
    gate_status = {g["gate"]: g["status"] for g in gates}

    # 3. Multi-dimensional confidence = the verdict's dimension statuses.
    confidence = {name: dim["status"] for name, dim in verdict["dimensions"].items()}

    # 4. Fail-closed decision.
    reasons: List[str] = []
    if blocking:
        recommendation = NEVER_APPROVE
        reasons.append("blocking condition(s) present — approval is impossible "
                       "regardless of confidence")
        reasons.extend(f"BLOCK: {b}" for b in blocking)
    else:
        required = [gate_status[g] for g in _REQUIRED_GATES]
        if FAIL in required:
            recommendation = MANUAL_REVIEW
            failed = [g for g in _REQUIRED_GATES if gate_status[g] == FAIL]
            reasons.append(f"required gate(s) FAILED: {', '.join(failed)}")
        elif NOT_CHECKED in required:
            recommendation = MANUAL_REVIEW
            unchecked = [g for g in _REQUIRED_GATES if gate_status[g] == NOT_CHECKED]
            reasons.append("cannot establish positive safety — required gate(s) "
                           f"not verified: {', '.join(unchecked)} "
                           "(absence of detected problems is not evidence of safety)")
        else:
            recommendation = RECOMMEND_APPROVE
            reasons.append("all required gates positively PASSED with evidence")
            reasons.extend(f"✓ {g} verified" for g in _REQUIRED_GATES)

    return {
        "recommendation": recommendation,
        "blocking":       blocking,
        "gates":          gates,
        "confidence":     confidence,
        "reasons":        reasons,
    }
