# profiles/payment.py
#
# Payment profile — PURE DECLARATIVE DATA.
#
# Covers remittance advices, payment confirmations, and wire/ACH advices: the
# documents that say "we paid X against invoice Y".
#
# ── Why this exists ───────────────────────────────────────────────────────
# The PAYMENT_CONFLICT check has worked since the consistency engine was built,
# and has never once fired on a real document — because nothing could read a
# payment. The check asks "are there payments already recorded against this
# invoice?", queries the registry for doc_type="payment", and finds nothing,
# always. This file is the missing half.
#
# ── The one modelling decision worth explaining ───────────────────────────
# `invoice_number` here is the invoice being SETTLED, not the payment's own id.
#
# That reads oddly until you look at a remittance advice, which literally prints
#
#     Invoice Number: INV-7001      Amount Paid: $4,500.00
#
# The invoice number ON a payment document IS the invoice being paid. So the
# mapping is what the document says, not a convenience. It also means the
# registry's existing key builder forms `doc_ref = payment|INV-7001` with no
# changes, and `_check_payments` finds it with no changes — the cross-type
# lookup the registry was built for, working as designed.
#
# The payment's own identifier lives in `payment_reference`. It is deliberately
# NOT the lookup key: nobody asks "show me payment TXN-889", they ask "has
# invoice INV-7001 already been paid?"
#
# ── The limit, stated here because it cannot be fixed here ────────────────
# The registry only knows payments that passed through Qrynt. A payment made
# directly in the ERP and never ingested is invisible, so "has this been paid?"
# is EXTERNAL truth: this profile narrows the question, it cannot close it. Every
# PAYMENT_CONFLICT finding says so in its `unresolved` field, and an ERP would
# answer it through the same EvidenceProvider seam without engine changes.

PAYMENT_PROFILE = {

    "metadata": {
        "id":          "payment",
        "name":        "Payment / Remittance Advice",
        "version":     "1.0",
        "description": "Remittance advice, payment confirmation, or wire/ACH advice",
    },

    "extraction": {
        "request": ["key_values", "tables", "paragraphs"],
    },

    "schema": {
        # the invoice this payment settles — see the note above
        "invoice_number":    {"type": "string",   "required": True,
                              "cardinality": "one", "anchor_weight": 0.40},
        "vendor":            {"type": "string",   "required": True,
                              "cardinality": "exactly_one", "anchor_weight": 0.25},
        "total":             {"type": "money",    "required": True,
                              "cardinality": "one", "anchor_weight": 0.15},
        "currency":          {"type": "currency", "required": False,
                              "cardinality": "one", "anchor_weight": 0.0},
        "invoice_date":      {"type": "date",     "required": False,
                              "cardinality": "one", "anchor_weight": 0.10},
        # the payment's OWN identifier — recorded, not used as the lookup key
        "payment_reference": {"type": "string",   "required": False,
                              "cardinality": "one", "anchor_weight": 0.10},
        "payment_method":    {"type": "string",   "required": False,
                              "cardinality": "one", "anchor_weight": 0.0},
        "document_type":     {"type": "string",   "required": False,
                              "cardinality": "one", "anchor_weight": 0.0},
        "tax_id":            {"type": "string",   "required": False,
                              "cardinality": "one", "anchor_weight": 0.0},
    },

    "partition": {
        "suspicion_threshold": 0.80,
    },

    "field_resolution": {

        # The invoice being settled. A payment document names it explicitly —
        # that is the entire point of a remittance advice.
        "invoice_number": [
            {"from": "key_values", "aliases":
                ["Invoice Number", "Invoice No", "Invoice No.", "Invoice #",
                 "Invoice Ref", "Invoice Reference", "Against Invoice",
                 "Paying Invoice", "Settles Invoice", "Applied To",
                 "Applied To Invoice", "In Payment Of", "Payment For",
                 "Document Number", "Document Ref", "Bill Number", "Bill No",
                 # generic reference labels — an identifier cannot be recovered
                 # by arithmetic, so a label is the only evidence available
                 "Ref", "Ref #", "Ref No", "Reference", "Reference #",
                 "Reference No", "Our Ref", "Your Ref",
                 # real-world variants
                 "invoice_number", "invoice_no", "invoice_ref", "inv_no"]},
            {"from": "tables", "columns":
                ["Invoice", "Invoice Number", "Invoice No", "Reference"]},
        ],

        # The payee. Same treatment as an invoice's vendor: the resolver's
        # entity cleaning and heading fallback apply unchanged.
        "vendor": [
            {"from": "key_values", "aliases":
                ["Payee", "Paid To", "Pay To", "Beneficiary",
                 "Beneficiary Name", "Supplier", "Supplier Name",
                 "Vendor", "Vendor Name", "Remit To", "Creditor",
                 "payee", "vendor", "supplier", "beneficiary"]},
            {"from": "heading", "after_label":
                ["Payee", "Paid To", "Pay To", "Beneficiary", "Remit To"]},
        ],

        # The amount actually paid — which may be LESS than the invoice total
        # (partial settlement) or more (overpayment). Both are findings the
        # consistency engine can compute; neither is assumed here.
        "total": [
            {"from": "key_values", "aliases":
                ["Amount Paid", "Payment Amount", "Amount", "Total Paid",
                 "Net Payment", "Payment Total", "Remittance Amount",
                 "Settled Amount", "Amount Remitted", "Value",
                 "Total Remittance", "Paid Amount", "Sum Paid",
                 "amount_paid", "payment_amount", "amount", "total_paid"]},
            # deliberately NO {"from": "tables"} — a tables strategy on `total`
            # once grabbed a LINE ITEM amount and reported it as the document
            # total, silently. A missing total is recovered by summing line
            # items or reported as absent; it is never guessed from a column.
        ],

        "currency": [
            {"from": "key_values", "aliases":
                ["Currency", "Payment Currency", "Remit Currency", "CCY",
                 "currency", "payment_currency"]},
        ],

        "invoice_date": [
            {"from": "key_values", "aliases":
                ["Payment Date", "Date Paid", "Value Date", "Settlement Date",
                 "Remittance Date", "Transaction Date", "Date",
                 "payment_date", "value_date", "date_paid", "date"]},
        ],

        "payment_reference": [
            {"from": "key_values", "aliases":
                ["Payment Reference", "Payment Ref", "Payment ID",
                 "Payment Number", "Transaction ID", "Transaction Reference",
                 "Remittance Number", "Remittance Ref", "Advice Number",
                 "Advice Ref", "Confirmation Number", "Trace Number",
                 "payment_reference", "payment_id", "transaction_id"]},
        ],

        "payment_method": [
            {"from": "key_values", "aliases":
                ["Payment Method", "Method", "Payment Type", "Pay Method",
                 "Transfer Type", "payment_method", "method"]},
        ],

        "document_type": [
            {"from": "doc_type", "scan_lines": 12},
        ],

        "tax_id": [
            {"from": "key_values", "aliases":
                ["Tax ID", "VAT Number", "VAT No", "GSTIN", "EIN",
                 "USt-IdNr", "Steuernummer", "Tax Registration"]},
        ],
    },

    # Rules stay minimal. A payment has no internal arithmetic to check — there
    # is no subtotal+tax identity to satisfy, and the amount paid is legitimately
    # allowed to differ from the invoice total (partial payment, credit applied,
    # early-settlement discount). Whether a payment is CORRECT is a question
    # about the invoice it settles, and that comparison belongs in the
    # consistency engine where both documents are visible — not in a rule that
    # can only see this one.
    "rules": [
        {
            "id":       "payment_amount_positive",
            "type":     "comparison",
            "left":     "total",
            "op":       ">",
            "right":    0,
            "severity": "error",
            "message":  "payment amount must be greater than zero",
        },
    ],
}
