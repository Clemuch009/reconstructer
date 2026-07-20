# profiles/invoice_po.py
#
# Invoice ↔ Purchase Order reconciliation profile — PURE DECLARATIVE DATA.
#
# Drives the generic reconcile_documents() across the five verdict dimensions.
# No engine code — this is the entire configuration for 3-way matching's
# invoice↔PO edge (step 6 of the AP workflow).
#
# What this catches, per dimension:
#   Identity   — is this invoice for this PO? (po_number links them, vendor agrees)
#   Financial  — invoice total must not EXCEED the PO total (over-billing)
#   Structural — do the invoice's line items align to the PO's line items?
#   Business   — were the ordered quantities actually billed? (partial delivery)
#   Compliance — NOT_CHECKED (needs company policy: price tolerance, approved
#                vendor, active contract) — fail-closed until that data exists

INVOICE_PO_RECONCILIATION = {

    "metadata": {
        "id":          "invoice_vs_po",
        "name":        "Invoice vs Purchase Order",
        "version":     "1.0",
        "description": "Reconcile a billed invoice against its purchase order",
    },

    # ── Identity: the fact-based weighted edge ───────────────────────────
    # po_number is the primary link; vendor and currency corroborate. A missing
    # field weakens the edge but doesn't break it (other facts carry it).
    "identity": {
        # po_number is the KEYSTONE: an invoice and its PO carry the SAME PO
        # reference, so a match on it links the two documents on its own. Vendor
        # and currency corroborate — a disagreement on them is surfaced as a
        # warning (party mismatch on linked docs), not an identity failure. When
        # no po_number is present on both sides, identity falls back to the
        # weighted vendor+currency score against the threshold.
        "keystone":  ["po_number"],
        "weights": {
            "po_number": {"b": "po_number", "w": 0.6},
            "vendor":    {"b": "vendor",    "w": 0.3},
            "currency":  {"b": "currency",  "w": 0.1},
        },
        "threshold": 0.7,
    },

    # ── Financial: invoice must not exceed the PO ────────────────────────
    # Note: NOT "invoice total == PO total" — a partial invoice legitimately
    # totals less than the PO. The financial rule is that billing must not
    # EXCEED what was ordered. (a = invoice, b = purchase order.)
    "financial": [
        {"id": "invoice_within_po", "type": "compare", "op": "<=",
         "left": "a.total", "right": "b.total", "tolerance": 0.01,
         "requires": ["a.total", "b.total"],
         "message": "Invoice total must not exceed the PO total"},
        {"id": "currency_matches", "type": "compare", "op": "==",
         "left": "a.currency", "right": "b.currency",
         "requires": ["a.currency", "b.currency"],
         "message": "Invoice and PO currencies must match"},
    ],

    # ── Structural + Business: line alignment ────────────────────────────
    # Align invoice lines (a) to PO lines (b) by description + unit_price (NOT
    # quantity — quantity is WHAT WE'RE CHECKING, not an alignment key). Then
    # the business dimension flags any quantity mismatch as partial fulfilment.
    "structural": {
        "line_table":     "line_items",
        "columns": {
            "description": ["description"],
            "quantity":    ["quantity"],
            "unit_price":  ["unit_price"],
            "amount":      ["amount"],
        },
        "identity":       ["description"],
        "weights":        {"description": 0.6, "unit_price": 0.4},
        "quantity_field": "quantity",
        "allow":          ["reorder", "split_merge"],
    },

    # compliance intentionally omitted → NOT_CHECKED (fail-closed)
}
