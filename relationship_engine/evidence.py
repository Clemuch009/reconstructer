# relationship_engine/evidence.py
#
# Stage 4 of the duplicate engine: THE EVIDENCE ENGINE.
#
# For each candidate pair (from Stage 3), accumulate evidence that the two
# invoices are — or are NOT — the same economic event. Both POSITIVE evidence
# (shared invoice number, vendor, amount, PO, line items) and NEGATIVE evidence
# (different amount, different currency, different customer) contribute.
#
# ── Two rules that make this safe ──────────────────────────────────────────
#
# 1. DO NOT collapse to a single score here. The classifier (Stage 5) needs the
#    structured evidence to distinguish an EXACT_DUPLICATE from a
#    CORRECTED_INVOICE from a RECURRING_INVOICE — three situations that can have
#    a similar aggregate score but very different evidence *shapes*. We compute
#    separate positive and negative totals and keep every signal.
#
# 2. NEGATIVE evidence is not just "absence of positive". A different amount, a
#    different currency, or a different customer is active evidence AGAINST
#    same-ness. This is what stops a recurring invoice (same vendor+amount,
#    different period) or a genuine second invoice from being called a duplicate.
#
# Comparison runs on the CANONICAL fields (Stage 1), so INV-001 vs INV001 agree
# and $1,100.00 vs 1100 agree without any work here.

from typing import Any, Dict, List, Optional

from relationship_engine.candidates import InvoiceRecord
from relationship_engine.canonicalize import instruments_conflict


# ── signal weights (from the merged design) ────────────────────────────────

POSITIVE_WEIGHTS = {
    "invoice_number_exact":      0.60,   # same canonical invoice number
    "vendor_canon":              0.20,
    "amount_exact":              0.15,
    "currency_match":            0.05,
    "po_match":                  0.25,
    "date_within_3d":            0.10,
    "line_items_identical":      0.40,   # (wired when line items are passed)
}

NEGATIVE_WEIGHTS = {
    "different_amount":         -0.40,
    "different_currency":       -0.60,
    "different_recipient":      -0.30,   # addressed to a different entity
    "composition_differs":      -0.20,   # same total, built from different parts
    "different_document_type":  -0.35,   # different accounting instruments
    "different_date_month":     -0.10,   # different billing month (recurring hint)
}


def _both(a: Optional[Any], b: Optional[Any]) -> bool:
    """True only if both values are present (not None/empty)."""
    return a is not None and a != "" and b is not None and b != ""


def _amounts_equal(a: Optional[float], b: Optional[float], tol: float = 0.01) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


def _days_apart(a_iso: Optional[str], b_iso: Optional[str]) -> Optional[int]:
    if not a_iso or not b_iso:
        return None
    from datetime import date
    try:
        ay, am, ad = (int(x) for x in a_iso.split("-"))
        by, bm, bd = (int(x) for x in b_iso.split("-"))
        return abs((date(ay, am, ad) - date(by, bm, bd)).days)
    except (ValueError, TypeError):
        return None


def gather_evidence(a: InvoiceRecord, b: InvoiceRecord) -> Dict[str, Any]:
    """
    Accumulate positive and negative evidence for one candidate pair, using the
    canonical fields. Returns the UNCOLLAPSED evidence bundle for the classifier:

      {
        "a_id", "b_id",
        "signals":       [ {name, polarity, weight, a, b} ],   # every signal fired
        "positive_score": float,   # sum of positive weights that fired
        "negative_score": float,   # sum of negative weights that fired (<= 0)
        "provable_identity": bool, # gate for auto-block (Stage 6)
        "facts":         { ...raw canonical comparisons... },
      }
    """
    ca, cb = a.canon, b.canon
    signals: List[Dict[str, Any]] = []

    def add(name: str, polarity: str, weight: float, av: Any, bv: Any):
        signals.append({"name": name, "polarity": polarity,
                        "weight": weight, "a": av, "b": bv})

    # ── positive signals ──────────────────────────────────────────────────
    invnum_a = ca.get("invoice_number_canon")
    invnum_b = cb.get("invoice_number_canon")
    invnum_match = _both(invnum_a, invnum_b) and invnum_a == invnum_b
    if invnum_match:
        add("invoice_number_exact", "+", POSITIVE_WEIGHTS["invoice_number_exact"], invnum_a, invnum_b)

    vendor_a = ca.get("vendor_canon")
    vendor_b = cb.get("vendor_canon")
    vendor_match = _both(vendor_a, vendor_b) and vendor_a == vendor_b
    if vendor_match:
        add("vendor_canon", "+", POSITIVE_WEIGHTS["vendor_canon"], vendor_a, vendor_b)

    amount_a = ca.get("amount_canon")
    amount_b = cb.get("amount_canon")
    amount_match = _amounts_equal(amount_a, amount_b)
    if amount_match:
        add("amount_exact", "+", POSITIVE_WEIGHTS["amount_exact"], amount_a, amount_b)

    curr_a = ca.get("currency_canon")
    curr_b = cb.get("currency_canon")
    currency_match = _both(curr_a, curr_b) and curr_a == curr_b
    if currency_match:
        add("currency_match", "+", POSITIVE_WEIGHTS["currency_match"], curr_a, curr_b)

    po_a = ca.get("po_number_canon")
    po_b = cb.get("po_number_canon")
    po_match = _both(po_a, po_b) and po_a == po_b
    if po_match:
        add("po_match", "+", POSITIVE_WEIGHTS["po_match"], po_a, po_b)

    date_a = ca.get("date_canon")
    date_b = cb.get("date_canon")
    days = _days_apart(date_a, date_b)
    if days is not None and days <= 3:
        add("date_within_3d", "+", POSITIVE_WEIGHTS["date_within_3d"], date_a, date_b)

    # ── negative signals ──────────────────────────────────────────────────
    # These fire only when BOTH values are present and they DISAGREE — a missing
    # field is not evidence against, it is just absence.
    if _both(amount_a, amount_b) and not amount_match:
        add("different_amount", "-", NEGATIVE_WEIGHTS["different_amount"], amount_a, amount_b)

    if _both(curr_a, curr_b) and not currency_match:
        add("different_currency", "-", NEGATIVE_WEIGHTS["different_currency"], curr_a, curr_b)

    # Different accounting INSTRUMENT. A debit memo and an invoice bearing the
    # same reference, vendor, date and amount are not one obligation stated
    # twice — they are different instruments with different accounting
    # treatment, and only one of them (or both, or neither) may be payable.
    # Without this they satisfy provable identity and auto-BLOCK.
    dt_a, dt_b = ca.get("doc_type_canon"), cb.get("doc_type_canon")
    doc_type_differs = instruments_conflict(dt_a, dt_b)
    if doc_type_differs:
        add("different_document_type", "-",
            NEGATIVE_WEIGHTS["different_document_type"], dt_a, dt_b)

    # Different bill-to entity. Two invoices addressed to DIFFERENT customers
    # are not repetitions of one event, however identical the other fields — so
    # this is the signal that separates a duplicate from two documents wearing
    # the same invoice number. Fires only when both are present and disagree.
    recip_a = ca.get("recipient_canon")
    recip_b = cb.get("recipient_canon")
    recipient_differs = _both(recip_a, recip_b) and recip_a != recip_b
    if recipient_differs:
        add("different_recipient", "-", NEGATIVE_WEIGHTS["different_recipient"], recip_a, recip_b)

    # Same total, DIFFERENT composition. If two invoices agree on the total but
    # disagree on how it is built (subtotal / tax), they are not the same
    # document restated — they assert different underlying facts.
    sub_a, sub_b = ca.get("subtotal_canon"), cb.get("subtotal_canon")
    tax_a, tax_b = ca.get("tax_canon"), cb.get("tax_canon")
    composition_differs = bool(
        amount_match and (
            (_both(sub_a, sub_b) and not _amounts_equal(sub_a, sub_b)) or
            (_both(tax_a, tax_b) and not _amounts_equal(tax_a, tax_b))))
    if composition_differs:
        add("composition_differs", "-", NEGATIVE_WEIGHTS["composition_differs"],
            f"subtotal {sub_a} / tax {tax_a}", f"subtotal {sub_b} / tax {tax_b}")

    if _both(date_a, date_b) and date_a[:7] != date_b[:7]:
        add("different_date_month", "-", NEGATIVE_WEIGHTS["different_date_month"], date_a[:7], date_b[:7])

    positive_score = round(sum(s["weight"] for s in signals if s["polarity"] == "+"), 4)
    negative_score = round(sum(s["weight"] for s in signals if s["polarity"] == "-"), 4)

    # ── provable identity gate (Stage 6 auto-block precondition) ──────────
    # Auto-block requires PROVABLE identity: same canonical invoice number AND
    # same vendor AND same amount AND same currency (see below — a derived
    # amount does not count).

    # A derived amount (recovered by summing line items) counts toward PROVABLE
    # identity just like a stated one. _sum_line_items returns a value ONLY when
    # every line had a parseable amount — it fails closed to None otherwise — so
    # a derived total that exists is the sum of a fully-extracted line-item
    # table, no less trustworthy than a printed total. The `amount_derived` flag
    # is kept for the audit trail (the reason notes the total was computed) but
    # does NOT weaken identity or force review: making a missing "Grand Total"
    # label trigger manual review would be an operational gap in an automated
    # pipeline, not a safety gain.
    amount_derived = bool(ca.get("amount_derived") or cb.get("amount_derived"))
    provable_identity = bool(invnum_match and vendor_match and amount_match
                             and currency_match)
    if amount_match and amount_derived:
        for s in signals:
            if s["name"] == "amount_exact":
                s["derived"] = True

    return {
        "a_id": a.id,
        "b_id": b.id,
        "signals": signals,
        "positive_score": positive_score,
        "negative_score": negative_score,
        "provable_identity": provable_identity,
        "amount_derived": amount_derived and amount_match,
        "facts": {
            "invnum_match":    invnum_match,
            "vendor_match":    vendor_match,
            "amount_match":    amount_match,
            "currency_match":  currency_match,
            "po_match":            po_match,
            "recipient_differs":   recipient_differs,
            "doc_type_differs":    doc_type_differs,
            "composition_differs": composition_differs,
            "days_apart":      days,
            "same_month":      (date_a[:7] == date_b[:7]) if _both(date_a, date_b) else None,
        },
    }
