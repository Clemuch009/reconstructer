# relationship_engine/dedup_policy.py
#
# Stage 6 of the duplicate engine: POLICY → ACTION.
#
# Classification (Stage 5) says WHAT the relationship is. Policy says WHAT TO DO
# about it. These are deliberately separate: the same relationship can warrant
# different actions under different customer policies, and separating them means
# policy can change without touching detection.
#
# The action vocabulary:
#   BLOCK       recommend NEVER_APPROVE; show the matching invoice. Auto-only
#               when identity is PROVABLE.
#   REVIEW      route to a human with the evidence; do not auto-anything.
#   ALLOW       not a duplicate; let it proceed (still subject to other checks).
#   ESCALATE    fraud signal; route to a heightened-review / investigation path.
#
# ── The safety invariant ───────────────────────────────────────────────────
# BLOCK is only auto-applied when the relationship is a duplicate AND identity is
# provable (same canonical invoice number + vendor + amount + currency). A
# duplicate-type relationship WITHOUT provable identity (e.g. VENDOR_VARIANT)
# downgrades to REVIEW — never auto-block on similarity alone. This is the whole
# reason the engine exists: false blocks on legitimate invoices are the toxic
# failure, so blocking requires proof, not a high score.

from typing import Any, Dict, List

from relationship_engine.classify import (
    EXACT_DUPLICATE, NORMALIZED_DUPLICATE, VENDOR_VARIANT_DUPLICATE,
    CORRECTED_INVOICE, RECURRING_INVOICE, SPLIT_INVOICE_SUSPECT, LOW_CONFIDENCE,
    IDENTITY_COLLISION,
)


BLOCK    = "BLOCK"
REVIEW   = "REVIEW"
ALLOW    = "ALLOW"
ESCALATE = "ESCALATE"


# Base policy: relationship type → default action. This is DATA — a customer
# policy could override individual rows (e.g. auto-block vendor variants) without
# any code change.
DEFAULT_POLICY: Dict[str, str] = {
    EXACT_DUPLICATE:          BLOCK,
    NORMALIZED_DUPLICATE:     BLOCK,
    VENDOR_VARIANT_DUPLICATE: REVIEW,
    CORRECTED_INVOICE:        REVIEW,
    RECURRING_INVOICE:        ALLOW,
    SPLIT_INVOICE_SUSPECT:    ESCALATE,
    # Never auto-anything. Blocking B silently elects A as the authoritative
    # VCG-4040 on no evidence; allowing B risks paying twice. The only honest
    # action is to hold both and have a human establish which is real.
    IDENTITY_COLLISION:       REVIEW,
    LOW_CONFIDENCE:           REVIEW,
}


def decide_action(
    relationship: Dict[str, Any],
    policy: Dict[str, str] = None,
) -> Dict[str, Any]:
    """
    Map one classified relationship to an action, enforcing the safety invariant.

    Returns the relationship enriched with:
      "action":        BLOCK | REVIEW | ALLOW | ESCALATE
      "auto_blockable": bool   (BLOCK *and* provable identity)
      "action_reason": str
    """
    policy = policy or DEFAULT_POLICY
    rtype = relationship["type"]
    action = policy.get(rtype, REVIEW)

    # Safety invariant: never auto-BLOCK without provable identity. A BLOCK policy
    # on a relationship whose identity isn't provable downgrades to REVIEW.
    if action == BLOCK and not relationship.get("provable_identity"):
        action = REVIEW
        action_reason = ("would block, but identity is not provable "
                         "(missing an exact invoice-number/vendor/amount/currency "
                         "match) — downgraded to review")
    else:
        action_reason = _default_action_reason(rtype, action)

    auto_blockable = (action == BLOCK and bool(relationship.get("provable_identity")))

    supporting, conflicts = structure_evidence(relationship)
    return {**relationship,
            "action": action,
            "auto_blockable": auto_blockable,
            "action_reason": action_reason,
            # the finding explains WHY this is in the queue; the action says what
            # the workflow does about it. Keeping them separate means new
            # findings never require new workflow states.
            "finding": rtype,
            "risk": RISK_STATEMENTS.get(rtype, ""),
            "supporting": supporting,
            "conflicts": conflicts}


def _default_action_reason(rtype: str, action: str) -> str:
    return {
        BLOCK:    "provable duplicate of an existing invoice — recommend blocking to prevent double payment",
        REVIEW:   "needs a human decision with the matching invoice and evidence in view",
        ALLOW:    "not a duplicate — may proceed (still subject to other approval checks)",
        ESCALATE: "possible split-invoice / fraud pattern — route to investigation",
    }[action]


# Plain-language risk statements, keyed by FINDING. The action tells the workflow
# what to do; the finding and this statement tell the reviewer WHY it landed in
# their queue. Without this, "Review" is just a bucket for "not sure" — which is
# what makes review queues untrusted and ignored.
RISK_STATEMENTS = {
    IDENTITY_COLLISION:
        "Two documents claim to represent the same invoice but disagree on "
        "critical business facts. One of them is not what it says it is — "
        "paying either without resolving which is authoritative risks paying "
        "the wrong party, or paying twice.",
    EXACT_DUPLICATE:
        "The same invoice appears twice. Paying both would double-pay this "
        "vendor for one obligation.",
    NORMALIZED_DUPLICATE:
        "The same invoice appears twice in different formats. Paying both would "
        "double-pay this vendor for one obligation.",
    VENDOR_VARIANT_DUPLICATE:
        "Same invoice number and amount under vendor names that differ. Either "
        "one vendor is spelt two ways, or two entities are claiming one charge.",
    CORRECTED_INVOICE:
        "Same invoice number, different amount — likely a re-issue. Paying both "
        "would over-pay; paying neither would leave the obligation unmet.",
    SPLIT_INVOICE_SUSPECT:
        "Charges against one PO have been billed under separate invoice "
        "numbers. This is how approval thresholds are evaded.",
    RECURRING_INVOICE:
        "A recurring charge for a new period — not a duplicate.",
    LOW_CONFIDENCE:
        "These invoices share an identifying detail but the evidence does not "
        "form a recognised pattern.",
}


def structure_evidence(relationship):
    """Split a relationship's raw signals into what SUPPORTS and what CONFLICTS.

    The signals already carry polarity; this presents them as a reviewer reads
    them — "these agree, these do not" — rather than as a scored blob. The point
    is that the queue item is actionable on sight.
    """
    sigs = (relationship.get("evidence") or {}).get("signals") or []
    label = {
        "invoice_number_exact": "Same invoice number",
        "vendor_canon":         "Same vendor",
        "amount_exact":         "Same total",
        "currency_match":       "Same currency",
        "po_match":             "Same PO",
        "date_within_3d":       "Same invoice date",
        "different_amount":     "Different total",
        "different_currency":   "Different currency",
        "different_recipient":  "Different bill-to entity",
        "composition_differs":  "Different subtotal/tax composition",
        "different_document_type": "Different accounting instrument",
        "different_date_month": "Different billing month",
    }
    supporting, conflicts = [], []
    for s in sigs:
        text = label.get(s["name"], s["name"].replace("_", " "))
        detail = (f"{s['a']}" if s["a"] == s["b"] else f"{s['a']} vs {s['b']}")
        entry = {"signal": s["name"], "text": text, "detail": detail}
        if s.get("derived"):
            entry["note"] = "total computed from line items, not stated"
        (supporting if s["polarity"] == "+" else conflicts).append(entry)
    return supporting, conflicts


def summarize_for_approval(decisions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Fold a set of per-pair action decisions (for one invoice, or one batch) into
    the signal the approval recommender consumes.

    The approval engine already has a `_block_duplicate` condition gated on
    `duplicate_detected`. This produces that flag — set ONLY when at least one
    relationship is auto-blockable (provable duplicate) — plus richer detail for
    the UI and the audit trail.

    Returns:
      {
        "duplicate_detected": bool,      # feeds approval's blocking condition
        "highest_action":     str,       # BLOCK > ESCALATE > REVIEW > ALLOW
        "relationships":      [ {type, a_id, b_id, action, reason} ],
        "counts":             {action: n},
      }
    """
    order = {BLOCK: 3, ESCALATE: 2, REVIEW: 1, ALLOW: 0}
    counts: Dict[str, int] = {}
    highest = ALLOW
    duplicate_detected = False
    rels: List[Dict[str, Any]] = []

    for d in decisions:
        action = d["action"]
        counts[action] = counts.get(action, 0) + 1
        if order[action] > order[highest]:
            highest = action
        if d.get("auto_blockable"):
            duplicate_detected = True
        rels.append({
            "type": d["type"], "a_id": d["a_id"], "b_id": d["b_id"],
            "action": action, "reason": d.get("action_reason", ""),
            "is_duplicate": d.get("is_duplicate", False),
            "provable_identity": d.get("provable_identity", False),
        })

    return {
        "duplicate_detected": duplicate_detected,
        "highest_action": highest,
        "relationships": rels,
        "counts": counts,
    }
