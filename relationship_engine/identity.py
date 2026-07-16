# relationship_engine/identity.py
#
# Stage 2 of the duplicate engine: IDENTITY KEY GENERATION.
#
# From one canonicalized invoice, produce SEVERAL deterministic keys — not one.
# These are lookup accelerators, NOT verdicts. Their only job is candidate
# generation (Stage 3): given a new invoice, find the handful of prior invoices
# worth the expensive evidence comparison, out of potentially millions.
#
# Why multiple keys? Different duplicate types start from different anchors:
#   - exact / reformatted resubmission → (vendor, invoice_number)
#   - recurring vs. correction         → (vendor, amount, currency)
#   - split-invoice suspicion          → (po_number)
#   - number-missing / mistyped        → (vendor, amount, date_bucket)
# No single key catches every case, so we index by all of them and take the
# union of candidates.
#
# A key is None (absent) when its required canonical fields are missing — a
# partial invoice simply produces fewer keys, never a fabricated one. This is
# fail-closed: a missing field can't manufacture a false match.

from typing import Any, Dict, List, Optional


# A key is a stable string; None means "cannot form this key for this invoice".
# The prefix names the key TYPE so different key types never collide in a shared
# index (e.g. a vendor+invnum key can't accidentally equal a po key).

def _join(*parts: Optional[str]) -> Optional[str]:
    """Join key parts with a delimiter; return None if ANY part is missing, so a
    composite key is only formed when all its components exist."""
    if any(p is None or p == "" for p in parts):
        return None
    return "|".join(str(p) for p in parts)


def _date_bucket(date_canon: Optional[str]) -> Optional[str]:
    """Coarsen an ISO date to its year-month bucket (2026-07-10 → 2026-07). Used
    for the temporal key so invoices in the same billing month are candidates
    even if the exact day differs (re-keyed dates, off-by-a-day)."""
    if not date_canon or len(date_canon) < 7:
        return None
    return date_canon[:7]


def identity_keys(canon: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """
    Produce the set of identity keys for a canonicalized invoice.

    Input: the dict from canonicalize_invoice (invoice_number_canon,
    vendor_canon, amount_canon, currency_canon, date_canon, po_number_canon).

    Output: named keys, each a string or None. Named (not a bare list) so the
    candidate-provider can index each key type in its own bucket and so the
    classifier can later reason about WHICH key matched.
    """
    invnum   = canon.get("invoice_number_canon")
    vendor   = canon.get("vendor_canon")
    amount   = canon.get("amount_canon")
    currency = canon.get("currency_canon")
    date     = canon.get("date_canon")
    po       = canon.get("po_number_canon")

    amount_str = f"{amount:.2f}" if isinstance(amount, (int, float)) else None

    return {
        # exact / reformatted resubmission anchor
        "vendor_invnum":       _join(vendor, invnum),
        # recurring vs. correction disambiguation anchor
        "vendor_amount_curr":  _join(vendor, amount_str, currency),
        # split-invoice / cross-doc anchor
        "po":                  po,
        # number-missing / mistyped anchor (coarse temporal)
        "vendor_amount_month": _join(vendor, amount_str, _date_bucket(date)),
        # invoice-number-only (weak alone; useful when vendor didn't resolve)
        "invnum":              invnum,
    }


def present_keys(keys: Dict[str, Optional[str]]) -> Dict[str, str]:
    """The subset of keys that actually formed (drop the None ones). This is what
    gets written to the index / used for lookups."""
    return {name: val for name, val in keys.items() if val}
