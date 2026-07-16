# relationship_engine/classify.py
#
# Stage 5 of the duplicate engine: RELATIONSHIP CLASSIFICATION.
#
# This is where the engine stops asking "is this a duplicate?" and answers "what
# RELATIONSHIP exists between these two invoices?" — the central principle of the
# whole design. It reads the evidence SHAPE from Stage 4 (not just an aggregate
# score) and assigns exactly one typed relationship.
#
# Why shape, not score: a CORRECTED_INVOICE can out-score a RECURRING_INVOICE on
# raw positives (both share vendor; the correction also shares the invoice
# number). A single number would rank the correction as "more duplicate" — which
# is backwards. The evidence shape — which signals fired, whether identity is
# provable, which negatives are present — is what separates them.
#
# The taxonomy (action is assigned separately in Stage 6 — policy):
#
#   EXACT_DUPLICATE         same invoice, identical surface form   → block
#   NORMALIZED_DUPLICATE    same invoice, differ only by formatting → block
#   VENDOR_VARIANT_DUPLICATE same invoice#/amount, vendor spelling differs → review
#   CORRECTED_INVOICE       same invoice#, changed amount           → review (NOT dup)
#   RECURRING_INVOICE       same vendor+amount, different period     → allow (NOT dup)
#   SPLIT_INVOICE_SUSPECT   different invoice#s, same PO/vendor       → investigate (fraud)
#   LOW_CONFIDENCE          shares a key but no clear pattern         → review
#
# The one invariant: NEVER auto-block without provable identity. Only the two
# DUPLICATE-and-provable types carry that; everything else routes to a human.

import re
from typing import Any, Dict

from relationship_engine.candidates import InvoiceRecord


EXACT_DUPLICATE          = "EXACT_DUPLICATE"
NORMALIZED_DUPLICATE     = "NORMALIZED_DUPLICATE"
VENDOR_VARIANT_DUPLICATE = "VENDOR_VARIANT_DUPLICATE"
CORRECTED_INVOICE        = "CORRECTED_INVOICE"
RECURRING_INVOICE        = "RECURRING_INVOICE"
SPLIT_INVOICE_SUSPECT    = "SPLIT_INVOICE_SUSPECT"
LOW_CONFIDENCE           = "LOW_CONFIDENCE"

# which relationships actually mean "this is the same invoice already seen"
DUPLICATE_TYPES = {EXACT_DUPLICATE, NORMALIZED_DUPLICATE, VENDOR_VARIANT_DUPLICATE}


def _raw(rec: InvoiceRecord, field: str) -> str:
    v = rec.fields.get(field)
    return "" if v is None else str(v).strip()


def _surface_identical(a: InvoiceRecord, b: InvoiceRecord) -> bool:
    """True if the RAW invoice number and vendor strings are byte-identical (not
    merely canonically equal). Distinguishes EXACT (same surface form) from
    NORMALIZED (equal only after canonicalization)."""
    return (_raw(a, "invoice_number") == _raw(b, "invoice_number")
            and _raw(a, "vendor") == _raw(b, "vendor")
            and _raw(a, "invoice_number") != "")


def classify_relationship(
    evidence: Dict[str, Any],
    a: InvoiceRecord,
    b: InvoiceRecord,
) -> Dict[str, Any]:
    """
    Assign one typed relationship to a candidate pair from its evidence bundle.

    Returns:
      {
        "type": <one of the taxonomy constants>,
        "a_id", "b_id",
        "is_duplicate": bool,          # true only for the DUPLICATE_TYPES
        "provable_identity": bool,     # gates auto-block (carried from evidence)
        "net_same_score": float,       # positive+negative; evidence OF sameness
        "reason": str,                 # human-readable justification
        "evidence": <the Stage-4 bundle>,
      }
    """
    f = evidence["facts"]
    invnum   = f["invnum_match"]
    vendor   = f["vendor_match"]
    amount   = f["amount_match"]
    currency = f["currency_match"]
    po       = f["po_match"]
    same_month = f["same_month"]
    provable = evidence["provable_identity"]
    net = round(evidence["positive_score"] + evidence["negative_score"], 4)

    # both invoice numbers present and DIFFERENT (a real disagreement, not just
    # one side missing)
    invnum_a = a.canon.get("invoice_number_canon")
    invnum_b = b.canon.get("invoice_number_canon")
    invnum_present_both = bool(invnum_a and invnum_b)
    invnum_differ = invnum_present_both and invnum_a != invnum_b

    def result(rtype: str, reason: str) -> Dict[str, Any]:
        return {
            "type": rtype,
            "a_id": a.id, "b_id": b.id,
            "is_duplicate": rtype in DUPLICATE_TYPES,
            "provable_identity": provable,
            "net_same_score": net,
            "reason": reason,
            "evidence": evidence,
        }

    # ── 1. Provable identity → a genuine duplicate (auto-block candidates) ──
    # invoice number + vendor + amount + currency all match.
    if provable:
        derived_note = ("" if not evidence.get("amount_derived") else
                        " (one total was recovered by summing line items, not "
                        "stated on the invoice)")
        if _surface_identical(a, b):
            return result(EXACT_DUPLICATE,
                          "identical invoice number, vendor, amount and currency; "
                          "surface forms match — same invoice submitted twice" + derived_note)
        return result(NORMALIZED_DUPLICATE,
                      "same invoice number, vendor, amount and currency after "
                      "normalization — same invoice in a different format" + derived_note)

    # ── 2. Corrected invoice: same number + vendor, amount CHANGED ─────────
    # This must be checked before anything amount-based: it has a matching
    # invoice number but a different amount, which is a re-bill, NOT a duplicate.
    if invnum and vendor and not amount and _both_amounts_present(a, b):
        return result(CORRECTED_INVOICE,
                      "same invoice number and vendor but a different amount — "
                      "a corrected re-bill, not a duplicate; needs review")

    # ── 3. Vendor-variant duplicate: same number + amount, vendor differs ──
    # Everything that anchors identity matches EXCEPT the vendor canonical form.
    # Could be the same vendor spelled differently (normalization gap) — cannot
    # prove it, so review, never auto-block.
    if invnum and amount and currency and not vendor:
        return result(VENDOR_VARIANT_DUPLICATE,
                      "same invoice number and amount but vendor names differ — "
                      "possibly the same vendor with a spelling variation; review")

    # ── 4. Recurring invoice: same vendor+amount+currency, DIFFERENT period ─
    # The classic false-positive trap. Same vendor and amount, but a different
    # billing month and NOT the same invoice number → legitimate recurring bill.
    if vendor and amount and currency and same_month is False and not invnum:
        return result(RECURRING_INVOICE,
                      "same vendor and amount in different billing months with "
                      "different invoice numbers — legitimate recurring invoice, "
                      "not a duplicate")

    # ── 5. Split-invoice suspect: same PO + vendor, DIFFERENT invoice #s ────
    # Separate fraud signal, not duplicate detection. Two different invoices
    # against the same PO/vendor. Two shapes, both suspicious, both → investigate
    # (never auto-anything — same PO can be legitimate partial billing):
    #   a) different amounts  → classic "split one job under a threshold"
    #   b) SAME amount        → same work billed twice under different numbers
    #      (arguably more suspicious — a near-clone with only the number changed)
    if po and vendor and invnum_differ:
        if amount:
            return result(SPLIT_INVOICE_SUSPECT,
                          "same PO and vendor and identical amount but different "
                          "invoice numbers — the same charge billed under two "
                          "numbers; investigate")
        elif _both_amounts_present(a, b):
            return result(SPLIT_INVOICE_SUSPECT,
                          "different invoice numbers against the same PO and vendor "
                          "with differing amounts — possible split billing; "
                          "investigate")

    # ── 6. Fallback: shares a candidate key but fits no clean pattern ──────
    return result(LOW_CONFIDENCE,
                  "shares an identity key but the evidence does not match a clear "
                  "relationship pattern — review")


def _both_amounts_present(a: InvoiceRecord, b: InvoiceRecord) -> bool:
    return a.canon.get("amount_canon") is not None and b.canon.get("amount_canon") is not None
