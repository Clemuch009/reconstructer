# profiles/reconciliation.py
#
# Example relationship (reconciliation) profile — PURE DECLARATIVE DATA.
#
# Like the invoice profile, this contains no logic. It tells the generic
# Relationship Engine HOW to match two sources and WHAT consistency means,
# for one domain: reconciling cash received (source A) against a billing /
# MRR ledger (source B).
#
# The engine knows nothing about cash or ledgers; it interprets this data.
# A different reconciliation (invoice-vs-PO, dedup, version-compare) is just
# another profile like this one.

CASH_VS_LEDGER_PROFILE = {

    "metadata": {
        "id":          "cash_vs_ledger",
        "name":        "Cash received vs. billing ledger",
        "version":     "1.0",
        "description": "Match incoming payments against expected ledger entries",
    },

    # Identity: what makes a record on each side 'the same' entity. The two
    # sources may name the key differently; each side declares its own field
    # via the adapter, but after adaptation both expose 'reference'.
    "match": {
        "field":       "reference",
        "normalizers": ["trim", "lower", "alnum"],   # INV-001 == inv001
    },

    # Consistency: once paired, what must agree. References a.<f> / b.<f>.
    "compare": [
        {"id": "amount_matches", "type": "compare", "op": "==",
         "left": "a.amount", "right": "b.expected_amount", "tolerance": 0.01,
         "requires": ["a.amount", "b.expected_amount"],
         "message": "Received amount must equal the ledger's expected amount"},

        {"id": "currency_matches", "type": "compare", "op": "==",
         "left": "a.currency", "right": "b.currency",
         "requires": ["a.currency", "b.currency"],
         "message": "Currencies must match"},
    ],

    "output": {
        "exceptions_first": True,   # surface MISMATCH / LEFT_ONLY / RIGHT_ONLY first
    },
}
