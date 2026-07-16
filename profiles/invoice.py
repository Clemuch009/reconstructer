# profiles/invoice.py
#
# Invoice Profile — PURE DECLARATIVE DATA.
#
# This file contains NO logic: no extraction, no key-value detection, no rule
# execution, no formatting code. It is a data description of how the generic
# machinery should interpret an invoice. A generic resolver (profiles/resolver.py)
# reads this structure; the generic Rules Engine (analysis/rules_engine.py)
# executes the rules. The Core engine stays completely unaware that invoices
# exist.
#
# Because it is data, a new vertical (contract, bank statement, bill of lading)
# is authored by writing another structure like this one — not by writing code.
#
# Structure:
#   metadata               — id / name / version / description
#   extraction             — what to REQUEST from the filter (not how to detect)
#   schema                 — the canonical field vocabulary (types, required)
#   field_resolution       — per field: ordered strategies (kv aliases, table
#                            columns) tried first-match-wins, respecting priority
#   rules                  — declarative rules for the Rules Engine
#   output                 — presentation preferences

INVOICE_PROFILE = {

    # ── 1. Metadata ──────────────────────────────────────────────────────
    "metadata": {
        "id": "invoice",
        "name": "Invoice",
        "version": "1.0",
        "description": "Generic commercial invoice profile",
    },

    # ── 2. Extraction preferences ────────────────────────────────────────
    # Requests what the filter should retrieve. Does NOT bias the Core
    # structure engine — it only says which structure types are of interest.
    "extraction": {
        "request": ["key_values", "tables", "paragraphs"],
    },

    # ── 3. Canonical schema (the profile's vocabulary) ───────────────────
    #
    # `cardinality` declares how many values may legitimately exist in ONE
    # logical document:
    #   exactly_one — a document ANCHOR. Two distinct values mean the input is
    #                 not one document. That is a PARTITION error, not a
    #                 validation error, and the resolver must never pick one.
    #   one         — at most one; a repeat is suspicious but weaker evidence.
    #   many        — repetition is normal (line items).
    #
    # `anchor_weight` is this field's contribution to boundary suspicion when
    # it repeats. Declared here as profile data — the engine invents nothing.
    "schema": {
        "tax_id":         {"type": "string",   "required": False,
                           "description": "vendor tax/VAT registration number"},
        "recipient":      {"type": "string",   "required": False,
                           "description": "the entity the invoice is addressed to (bill-to)"},
        "invoice_number": {"type": "string",   "required": True,
                           "cardinality": "exactly_one", "anchor_weight": 0.40},
        "vendor":         {"type": "string",   "required": True,
                           "cardinality": "exactly_one", "anchor_weight": 0.25},
        "invoice_date":   {"type": "date",     "required": True,
                           "cardinality": "one",         "anchor_weight": 0.15},
        "due_date":       {"type": "date",     "required": False,
                           "cardinality": "one",         "anchor_weight": 0.05},
        "po_number":      {"type": "string",   "required": False,
                           "cardinality": "one",         "anchor_weight": 0.30},
        "currency":       {"type": "currency", "required": False,
                           "cardinality": "one",         "anchor_weight": 0.0},
        "subtotal":       {"type": "money",    "required": False,
                           "cardinality": "one",         "anchor_weight": 0.10},
        "tax":            {"type": "money",    "required": False,
                           "cardinality": "one",         "anchor_weight": 0.05},
        "total":          {"type": "money",    "required": True,
                           "cardinality": "one",         "anchor_weight": 0.10},
    },

    # Boundary suspicion at or above this confidence means the input is treated
    # as multiple documents rather than one. Declared per profile.
    "partition": {
        "suspicion_threshold": 0.80,
    },

    # ── 4. Field resolution (the heart) ──────────────────────────────────
    # Per canonical field: an ordered list of strategies. The resolver tries
    # them top to bottom and stops at the first match (priority = order).
    #   {"from": "key_values", "aliases": [...]}   — match a kv key by alias
    #   {"from": "tables", "columns": [...]}       — pull from a table column
    # Aliases are matched case-insensitively, trimmed, and ignoring a trailing
    # colon (the resolver handles normalization — the profile just lists names).
    "field_resolution": {
        "tax_id": [
            {"from": "key_values", "aliases":
                ["Tax ID", "Tax ID Number", "TIN", "VAT Number", "VAT No",
                 "VAT Reg No", "VAT Registration", "GST Number", "GSTIN",
                 "EIN", "Federal Tax ID", "ABN", "Company Reg No",
                 "Tax Registration", "Tax Reg", "Tax Number",
                 # German / European
                 "USt-IdNr", "USt-IdNr.", "Umsatzsteuer-ID", "Steuernummer",
                 "Numéro de TVA", "NIF", "Partita IVA", "BTW-nummer"]},
        ],
        "recipient": [
            {"from": "key_values", "aliases":
                ["Bill To", "Billed To", "Bill-To", "Sold To", "Ship To",
                 "Customer", "Client", "Account", "Invoice To", "Buyer",
                 # German / European
                 "Empfänger", "Empfaenger", "Kunde", "Rechnungsempfänger",
                 "Destinataire", "Cliente"]},
            # the bill-to entity is normally on the line FOLLOWING the label
            {"from": "heading", "after_label":
                ["Bill To", "Billed To", "Bill-To", "Sold To", "Invoice To",
                 "Customer", "Client", "CLIENT ACCOUNT", "DEBTOR RECORD",
                 "Empfänger", "Kunde"]},
        ],
        "invoice_number": [
            {"from": "key_values", "aliases":
                ["Invoice Number", "Invoice No", "Invoice No.", "Invoice #",
                 "Inv No", "Inv #", "Document Number", "Bill No",
                 # statement / disguised-format variants
                 "REF NUMBER", "Ref Number", "Reference Number", "Ref No",
                 "Statement Number", "Statement No", "Doc Number", "Doc No",
                 # German / European
                 "Rechnungsnummer", "Rechnungs-Nr", "Rechnung Nr",
                 "Facture N", "Numéro de facture", "Factuurnummer",
                 "Numero fattura", "Número de factura",
                 # real-world variants
                 "invoice_number", "invoice_id", "inv_ref", "inv_no",
                 "invoice_ref", "invoice number", "reference", "ref",
                 "Invoice ID", "Invoice Id", "Bill Number"]},
            {"from": "tables", "columns": ["Invoice Number", "Invoice No",
                 "REF NUMBER", "Ref Number", "Reference Number"]},
        ],
        "vendor": [
            {"from": "key_values", "aliases":
                ["Vendor", "Vendor Name", "Supplier", "Billed By",
                 "Seller", "Company",
                 # real-world variants
                 "vendor_name", "vendor", "provider", "supplier_name",
                 "billed_by", "sold_by", "issued_by", "company_name"]},
            # vendor may be a bare heading (no colon). Try a labeled section
            # first, then the prominent top line — on an INVOICE the top entity
            # is the seller/vendor.
            {"from": "heading", "after_label":
                ["From", "Bill From", "Remit To", "Remit From", "Supplier", "Seller"]},
            {"from": "heading", "prominent_line": True},
        ],
        "invoice_date": [
            {"from": "key_values", "aliases":
                ["Invoice Date", "Date", "Issued On", "Bill Date", "Date Issued",
                 # real-world variants
                 "invoice_date", "issued_date", "date", "issue_date",
                 "date_issued", "created", "created_date", "billing_date",
                 "Date Created", "Date Issued", "Invoice Date",
                 # German / European
                 "Datum", "Rechnungsdatum", "Date de facture",
                 "Fecha", "Data", "Factuurdatum"]},
        ],
        "due_date": [
            {"from": "key_values", "aliases":
                ["Due Date", "Payment Due", "Pay By", "Due",
                 # real-world variants
                 "due_date", "due", "payment_due", "pay_by", "payment_date"]},
        ],
        "po_number": [
            {"from": "key_values", "aliases":
                ["PO Number", "Purchase Order", "P.O.", "PO", "Ref No",
                 "PO #", "P.O. #", "PO#", "Order #",
                 # real-world variants
                 "po_number", "purchase_order", "po_ref", "po_no",
                 "purchase_order_number", "PO Reference", "PO Ref",
                 "Purchase Order Number", "Purchase Order No", "Order Number"]},
        ],
        "currency": [
            {"from": "key_values", "aliases":
                ["Currency", "Curr",
                 # real-world variants
                 "currency", "monetary_unit", "currency_code", "ccy",
                 # German / European
                 "Währung", "Waehrung", "Devise", "Divisa", "Valuta"]},
        ],
        "subtotal": [
            {"from": "key_values", "aliases":
                ["Subtotal", "Sub Total", "Net Amount", "Total Before Tax",
                 "Subtotal amount", "Subtotal Amount", "Sub-total",
                 "Accumulated Subtotal", "Gross Subtotal", "Line Subtotal",
                 "Net",
                 # real-world variants
                 "subtotal", "sub_total", "net_amount", "net_total",
                 "amount_before_tax", "total_before_tax"]},
        ],
        "tax": [
            {"from": "key_values", "aliases":
                ["Tax", "VAT", "GST", "Sales Tax", "Tax Amount",
                 # real-world variants
                 "tax", "tax_amount", "vat", "gst", "sales_tax",
                 "tax_total", "vat_amount"]},
        ],
        "total": [
            {"from": "key_values", "aliases":
                ["Total", "Total Due", "Amount Due", "Grand Total",
                 "Balance Due", "Total Amount", "Invoice Total",
                 "Statement Total Due", "Statement Total", "Total Payable",
                 "Net Outstanding Balance", "Outstanding Balance",
                 "Net Balance", "Balance Outstanding", "Net Amount Due",
                 "Net Payable", "Final Amount Due",
                 # German / European
                 "Gesamtbetrag", "Gesamtsumme", "Rechnungsbetrag",
                 "Endbetrag", "Montant total", "Importe total",
                 "Totale", "Totaalbedrag",
                 # real-world variants
                 "total", "grand_total", "total_amount", "invoice_total",
                 "amount_due", "balance_due", "total_due", "amount_payable",
                 "final_total"]},
            # NOTE: deliberately NO {"from": "tables"} strategy here. Taking the
            # first cell of a table's "Amount"/"Total" column grabs a LINE ITEM's
            # amount and reports it as the invoice total — silently wrong (an
            # invoice whose first line is $1,000 of a $1,500 total would report
            # $1,000). If no total is stated, leave it absent: downstream can
            # derive it by SUMMING the line items, which is correct arithmetic.
        ],
    },

    # ── 4b. Table mapping ────────────────────────────────────────────────
    # Which extracted table is the line-items table, and how its columns map
    # to canonical line-item field names. The resolver builds
    # view["tables"]["line_items"] as a list of {canonical_col: value} dicts.
    "tables": {
        "line_items": {
            # Match the source table by any of these header signatures
            # (a table is the line-items table if it contains these columns).
            "identify_by": ["Description", "Amount", "Qty", "Quantity"],
            "columns": {
                "description": ["Description", "Item", "Details", "Product",
                                "description", "item", "details", "product",
                                "line_item", "service"],
                "quantity":    ["Quantity", "Qty", "Units",
                                "quantity", "qty", "units", "count"],
                "unit_price":  ["Unit Price", "Price", "Rate", "Unit Cost",
                                "unit_price", "price", "rate", "unit_cost",
                                "unit_rate"],
                "amount":      ["Amount", "Line Total", "Total", "Subtotal",
                                "Total Amount", "Line Amount", "Extended Price",
                                "Extended Amount", "Net Amount", "Line Value",
                                "amount", "line_total", "total", "line_amount",
                                "extended"],
            },
        },
    },

    # ── 5. Rule set (declarative — executed by the Rules Engine) ──────────
    # Field references match the canonical names above and the line_items
    # table columns (e.g. "line_items.amount").
    "rules": [
        {"id": "invoice_number_required", "type": "exists",
         "field": "invoice_number",
         "message": "Invoice number is required"},

        {"id": "vendor_required", "type": "exists", "field": "vendor",
         "message": "Vendor is required"},

        {"id": "total_required", "type": "exists", "field": "total",
         "message": "Total is required"},

        {"id": "invoice_date_format", "type": "match",
         "field": "invoice_date", "format": "date_iso",
         "requires": ["invoice_date"],
         "message": "Invoice date should be ISO format (YYYY-MM-DD)"},

        {"id": "currency_valid", "type": "match",
         "field": "currency", "format": "currency_code",
         "requires": ["currency"],
         "message": "Currency should be a 3-letter ISO code"},

        {"id": "totals_balance", "type": "compute", "operation": "sum",
         "inputs": ["subtotal", "tax"], "equals": "total", "tolerance": 0.01,
         "requires": ["subtotal", "tax", "total"],
         "message": "Subtotal + Tax must equal Total"},

        {"id": "line_items_sum_to_subtotal", "type": "compute",
         "operation": "sum", "inputs": ["line_items.amount"],
         "equals": "subtotal", "tolerance": 0.01,
         "requires": ["line_items.amount", "subtotal"],
         "message": "Line item amounts must sum to the subtotal"},

        {"id": "total_positive", "type": "compare", "op": ">",
         "left": "total", "right": {"const": 0},
         "requires": ["total"],
         "message": "Total must be greater than zero"},

        {"id": "due_after_invoice", "type": "compare", "op": ">=",
         "left": "due_date", "right": "invoice_date",
         "requires": ["due_date", "invoice_date"],
         "message": "Due date must be on or after the invoice date"},
    ],

    # ── 6. Output preferences ────────────────────────────────────────────
    "output": {
        "include_summary":    True,
        "include_fields":     True,
        "include_tables":     True,
        "include_validation": True,
    },
}
