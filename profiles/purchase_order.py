# profiles/purchase_order.py
#
# Purchase Order profile — PURE DECLARATIVE DATA.
#
# Same pattern as the invoice profile: it tells the generic resolver how to read
# a purchase order into a canonical Document View. No logic here. Adding PO
# support to Qrynt required ZERO engine code — only this data file and a
# reconciliation profile (profiles/invoice_po.py).
#
# A PO and an invoice share most fields (vendor, currency, line items, totals)
# but the PO carries what was ORDERED, while the invoice carries what was
# BILLED. Reconciling them is how you catch partial deliveries and over-billing.

PURCHASE_ORDER_PROFILE = {

    "metadata": {
        "id":          "purchase_order",
        "name":        "Purchase Order",
        "version":     "1.0",
        "description": "Generic purchase order profile",
    },

    "extraction": {
        "request": ["key_values", "tables", "paragraphs"],
    },

    "schema": {
        "po_number":    {"type": "string",   "required": True,
                         "cardinality": "exactly_one", "anchor_weight": 0.45},
        "vendor":       {"type": "string",   "required": True,
                         "cardinality": "exactly_one", "anchor_weight": 0.25},
        "order_date":   {"type": "date",     "required": False,
                         "cardinality": "one",         "anchor_weight": 0.10},
        "currency":     {"type": "currency", "required": False,
                         "cardinality": "one",         "anchor_weight": 0.0},
        "subtotal":     {"type": "money",    "required": False,
                         "cardinality": "one",         "anchor_weight": 0.10},
        "tax":          {"type": "money",    "required": False,
                         "cardinality": "one",         "anchor_weight": 0.05},
        "total":        {"type": "money",    "required": True,
                         "cardinality": "one",         "anchor_weight": 0.10},
    },

    "partition": {
        "suspicion_threshold": 0.80,
    },

    "field_resolution": {
        "po_number": [
            {"from": "key_values", "aliases":
                ["PO Number", "Purchase Order", "Purchase Order Number",
                 "P.O.", "PO", "PO No", "PO #", "Order Number",
                 # real-world variants
                 "po_number", "purchase_order", "po_ref", "po_no",
                 "order_number", "order_id", "purchase_order_number"]},
            {"from": "tables", "columns": ["PO Number", "Purchase Order"]},
        ],
        "vendor": [
            {"from": "key_values", "aliases":
                ["Vendor", "Vendor Name", "Supplier", "Supplier Name",
                 "Sold By", "To",
                 # real-world variants
                 "vendor_name", "vendor", "supplier_name", "supplier",
                 "provider", "seller"]},
            # vendor may be a bare heading under a "VENDOR" section label. Use
            # ONLY the labeled section — NOT the prominent top line, because on
            # a PURCHASE ORDER the top entity is the BUYER, not the vendor.
            {"from": "heading", "after_label":
                ["VENDOR", "Supplier", "Sold By", "Ship From", "Vendor Details"]},
        ],
        "order_date": [
            {"from": "key_values", "aliases":
                ["Order Date", "PO Date", "Date", "Issued On", "Created",
                 # real-world variants
                 "order_date", "po_date", "date", "issued_date", "created_date"]},
        ],
        "currency": [
            {"from": "key_values", "aliases":
                ["Currency", "Curr", "currency", "monetary_unit", "currency_code"]},
        ],
        "subtotal": [
            {"from": "key_values", "aliases":
                ["Subtotal", "Sub Total", "Net Amount", "Net",
                 "subtotal", "sub_total", "net_amount", "net_total"]},
        ],
        "tax": [
            {"from": "key_values", "aliases":
                ["Tax", "VAT", "GST", "Sales Tax",
                 "tax", "tax_amount", "vat", "gst"]},
        ],
        "total": [
            {"from": "key_values", "aliases":
                ["Total", "Order Total", "PO Total", "Grand Total",
                 "Total Amount",
                 # real-world variants
                 "total", "order_total", "po_total", "grand_total",
                 "total_amount", "order_value", "Total PO Value",
                 "PO Value", "Total Order Value", "Total Value"]},
            {"from": "tables", "columns": ["Total", "Amount"]},
        ],
    },

    # Line items: what was ORDERED. Same canonical columns as invoice lines so
    # the structural matcher can align an invoice line to a PO line directly.
    "tables": {
        "line_items": {
            "identify_by": ["Description", "Amount", "Qty", "Quantity", "Item"],
            "columns": {
                "description": ["Description", "Item", "Details", "Product",
                                "description", "item", "details", "product"],
                "quantity":    ["Quantity", "Qty", "Ordered", "Units",
                                "quantity", "qty", "ordered", "units",
                                "ordered_qty"],
                "unit_price":  ["Unit Price", "Price", "Rate", "Unit Cost",
                                "unit_price", "price", "rate", "unit_cost"],
                "amount":      ["Amount", "Line Total", "Total", "Extended",
                                "Total Amount", "Line Amount", "Extended Price",
                                "Extended Amount", "Net Amount", "Line Value",
                                "amount", "line_total", "total", "extended"],
            },
        },
    },

    # A PO validates internally like an invoice: line items should sum to the
    # order subtotal, subtotal + tax = total.
    "rules": [
        {"id": "po_number_required", "type": "exists", "field": "po_number",
         "message": "PO number is required"},
        {"id": "vendor_required", "type": "exists", "field": "vendor",
         "message": "Vendor is required"},
        {"id": "total_required", "type": "exists", "field": "total",
         "message": "Total is required"},
        {"id": "totals_balance", "type": "compute", "operation": "sum",
         "inputs": ["subtotal", "tax"], "equals": "total", "tolerance": 0.01,
         "requires": ["subtotal", "tax", "total"],
         "message": "Subtotal + Tax must equal Total"},
        {"id": "line_items_sum_to_subtotal", "type": "compute",
         "operation": "sum", "inputs": ["line_items.amount"],
         "equals": "subtotal", "tolerance": 0.01,
         "requires": ["line_items.amount", "subtotal"],
         "message": "Line item amounts must sum to the subtotal"},
    ],

    "output": {
        "include_summary":    True,
        "include_fields":     True,
        "include_tables":     True,
        "include_validation": True,
    },
}
