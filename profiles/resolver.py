# profiles/resolver.py
#
# Generic Profile Resolver.
#
# Turns raw pipeline output (key-value pairs + tables, from the filter layer)
# into a canonical **Document View** {fields, tables, metadata} by following a
# profile's declarative resolution strategies. It is GENERIC — it contains no
# invoice/contract/etc. knowledge; all domain meaning comes from the profile
# data it reads. The same resolver serves every profile.
#
# Resolution model (per canonical field):
#   The profile lists an ordered set of strategies. The resolver tries them in
#   order and stops at the FIRST successful match (order = priority):
#     {"from": "key_values", "aliases": [...]}  → match a kv key by alias
#     {"from": "tables",     "columns": [...]}  → take first cell of a column
#   New strategy kinds can be added here without changing the profile format.
#
# Output: the canonical Document View consumed by analysis/rules_engine.py.

import re as _re_mod
from typing import Any, Dict, List, Optional

from relationship_engine.canonicalize import canon_amount, canon_date, canon_currency


# ── normalization ────────────────────────────────────────────────────────

# A trailing rate qualifier: "@ 20%", "(8.25%)", " 19%", "@20.0 %".
# Structural, not a vocabulary: a number followed by a percent sign. That is why
# stripping it is canonicalisation rather than the alias treadmill — the same
# reason "£10,080.00" becomes 10080.0 without anyone adding "£10,080.00" as an
# alias. A vendor can invent a LABEL ("Total Corporate Charge"); they cannot
# invent a new way for 20% to be a percentage.
_RATE_QUALIFIER_RE = _re_mod.compile(r"\s*[@(]?\s*\d+(?:[.,]\d+)?\s*%\s*\)?\s*$")


def _norm(label: Any) -> str:
    """Normalize a label for matching: lowercase, trimmed, trailing colon and
    surrounding whitespace removed, a trailing parenthetical unit/qualifier
    stripped so "Total Due (USD)" matches "Total Due", and a trailing RATE
    qualifier stripped so "VAT @ 20%" matches "VAT". Purely syntactic (no domain
    knowledge).

    The rate strip closes a gap that was quietly expensive. "Tax (0%)" already
    worked, because the parenthetical rule caught it — but the unparenthesised
    forms real invoices actually use did not:

        VAT (20%)      ✓ matched "VAT"
        VAT @ 20%      ✗ matched nothing
        GST 5%         ✗
        MwSt. 19%      ✗

    When the tax line cannot be read, the damage is not a missing field. The role
    verifier sees line items summing to the subtotal, cannot see the tax it was
    never given, concludes "no tax or adjustment present", and reports CONFLICT
    on a perfectly clean invoice — telling a user their valid invoice is wrong,
    on the most common tax label in the UK.
    """
    s = str(label).strip()
    if s.endswith(":"):
        s = s[:-1].strip()
    # strip a single trailing parenthetical: "Total Due (USD)" -> "Total Due"
    s = _re_mod.sub(r"\s*\([^)]{1,12}\)\s*$", "", s).strip()
    # strip a trailing rate: "VAT @ 20%" -> "VAT",  "GST 5%" -> "GST"
    s = _RATE_QUALIFIER_RE.sub("", s).strip()
    return s.lower()


def _headers_of(table: Dict[str, Any]) -> List[Any]:
    """Table headers as a list. Some extractors emit `headers: None` (e.g. a
    spreadsheet block with no recognizable header row); treat that as absent
    rather than crashing."""
    h = table.get("headers")
    return h if isinstance(h, list) else []


def _rows_of(table: Dict[str, Any]) -> List[Any]:
    """Table rows as a list; tolerates a missing or null `rows` key."""
    r = table.get("rows")
    return r if isinstance(r, list) else []


# ── strategy: key_values ──────────────────────────────────────────────────

def _resolve_from_key_values(
    strategy: Dict[str, Any],
    kv_index: Dict[str, List[Any]],
) -> Optional[List[Any]]:
    """Return ALL kv values whose key matches one of the aliases (first alias
    that matches anything wins, but every value under it is returned). Never
    discards competing values — the caller applies cardinality rules."""
    for alias in strategy.get("aliases", []):
        vals = kv_index.get(_norm(alias))
        if vals:
            return list(vals)
    return None


# ── strategy: heading (value from a labeled section or prominent line) ─────

def _clean_entity_line(line: str, stop_labels: List[str]) -> str:
    """Trim a heading line to its entity name.

    Two cleanups:
      1. Drop a document-type word glued onto the line ("Horizon IT Solutions
         INVOICE" -> "Horizon IT Solutions").
      2. If the line is a flattened two-column row ("Horizon IT Solutions Acme
         Corp - Headquarters"), cut at the entity's legal suffix so we keep only
         the first (vendor) entity. Company names typically end in a suffix
         token (Inc, Ltd, LLC, Corp, Solutions, Agency, Group, ...); the text
         after the FIRST such suffix is the flattened second column.
    """
    s = line.strip()
    for doctype in ("PURCHASE ORDER", "INVOICE", "BILL", "STATEMENT", "RECEIPT", "QUOTE"):
        idx = s.upper().find(doctype)
        if idx > 0:
            s = s[:idx].strip()
            break
    # Cut at an embedded "Label:" first. Column flattening glues the NEXT
    # column onto the entity ("Acme Research Foundation Tax Status: Tax-Exempt").
    # This is structural — it works for any label — and is deliberately tried
    # BEFORE the suffix list below, which only fires for names that happen to end
    # in a legal suffix we enumerated ("Corp" is listed, "Foundation" is not).
    # Extending that list per company name is the lookup-table trap; this is not.
    em = _EMBEDDED_LABEL_RE.match(s)
    if em:
        s = em.group(1).strip()
    # cut at the first company-suffix token (keeps the leading entity only)
    SUFFIXES = ("inc", "inc.", "llc", "ltd", "ltd.", "corp", "corp.",
                "corporation", "co", "co.", "company", "solutions", "agency",
                "group", "partners", "associates", "systems", "technologies",
                "services", "gmbh", "sa", "ag", "plc", "limited")
    tokens = s.split()
    for i, tok in enumerate(tokens):
        if tok.lower().strip(".,") in SUFFIXES:
            # A company name can carry MULTIPLE suffix-like tokens in a row
            # ("... Group Ltd.", "... Technologies Inc."). Cutting at the FIRST
            # one drops the real legal suffix ("Meridian Analytics Group Ltd."
            # became "...Group"). Only cut when what follows is a genuine SECOND
            # entity — i.e. the next token is NOT itself another suffix. Advance
            # past a run of consecutive suffix tokens and cut after the last one.
            j = i
            while (j + 1 < len(tokens)
                   and tokens[j + 1].lower().strip(".,") in SUFFIXES):
                j += 1
            return " ".join(tokens[:j + 1])
    return s


def _resolve_doc_type(strategy, lines):
    """Identify the ACCOUNTING INSTRUMENT this document is.

    Scans lines for a name from the closed instrument set. Longest match wins so
    "ALLOCATION DEBIT MEMO" is not read as "INVOICE"-adjacent noise, and the
    scan stops at the first hit — the instrument is declared at the top, and a
    later mention ("as per our invoice of 3 July") is prose, not the type.

    Deliberately searches raw lines rather than key/value pairs: a document's
    type is a heading, not a labelled field. Nobody writes "Document Type:
    Invoice".
    """
    limit = int(strategy.get("scan_lines", 12))
    for line in [l.strip() for l in (lines or [])][:limit]:
        if not line or line.startswith("["):
            continue
        up = line.upper()
        # a heading, not a sentence: short, and not a kv pair
        if len(up.split()) > 6 or _LINE_KV_RE.match(line):
            continue
        for dt in _DOC_TYPES:                 # longest-first
            if dt in up:
                return [dt]
    return None


# Document-structure words. A vendor CANDIDATE from the prominent-line fallback
# is rejected if it CONTAINS any of these — not just if it equals one. This is
# the inverse of "does it look like a company": recognising every company name
# is impossible (open, infinite set), but rejecting document headings is easy
# (small, closed, finite set). Measured across the corpus, this rejects
# "CORPORATE INVOICE", "Tax invoice", "Introduction", "Executive Summary" while
# passing every real vendor ("Paddle.com Market Ltd", "Meridian Facilities Group
# Ltd", "Munich Engineering Group").
#
# A rejected candidate yields None — a loud, honest blank the required-field
# check will catch — never a wrong vendor name that would pass validation, seed
# a false identity key, and show an AP clerk a company that isn't there. The
# known cost is a rare real company literally named with one of these words
# (e.g. "Report Solutions Inc"); that is a fair price for never inventing a
# vendor from a heading, and such a vendor is still found via a labelled field.
_DOC_STRUCTURE_WORDS = {
    "invoice", "statement", "receipt", "summary", "introduction", "report",
    "quotation", "quote", "estimate", "remittance", "advice", "memo",
    "description", "overview", "contents", "abstract", "chapter", "section",
    "terms", "conditions", "page", "total", "subtotal", "balance", "bill",
    "profile", "record", "metadata", "context", "details", "information",
}


_COMPANY_SUFFIXES = {
    "ltd", "inc", "llc", "gmbh", "corp", "co", "plc", "ag", "sa", "srl",
    "pvt", "group", "services", "solutions", "agency", "consulting",
    "partners", "associates", "holdings", "company", "limited",
}


# Known field labels that appear as SPACE-separated key-values on invoices/POs
# (no colon): "Invoice No. INV-2026-0158", "Due Date 02 Aug 2026". A line that
# begins with one of these is a labelled field, not a vendor heading — the
# prominent-line picker must skip it.
_SPACE_KV_LABELS_RESOLVER = (
    "invoice number", "invoice no", "invoice #", "invoice id", "invoice date",
    "due date", "payment terms", "terms", "currency", "po number", "po no",
    "purchase order", "order number", "order no", "reference", "ref",
    "account number", "account name", "account manager", "routing",
    "tax id", "vat no", "vat number", "gst no", "date", "phone", "email",
)


def _is_space_kv_line(line: str) -> bool:
    """True when a line is a space-separated 'Label Value' field (no colon) whose
    label is a known invoice/PO field — so the vendor picker skips it."""
    low = line.strip().lower()
    for lab in _SPACE_KV_LABELS_RESOLVER:
        if low.startswith(lab + " ") or low.startswith(lab + ". "):
            return True
    return False


def _vendor_from_prominent_line(line: str) -> Optional[str]:
    """Strip document-structure words from a prominent line; return the real
    entity that remains, or None if nothing company-like is left.

    The discriminator is NOT "does the line contain a doc-word" — that rejects
    "Apex Logistics Services INVOICE", where the vendor and the heading are
    glued onto one physical line by layout flattening, and the vendor is real.
    The discriminator is what SURVIVES removing the doc-words:

        "Apex Logistics Services INVOICE" -> "Apex Logistics Services"  (3 words → vendor)
        "CORPORATE INVOICE"               -> "CORPORATE"                (1 generic word → None)
        "Tax invoice"                     -> ""                         (nothing → None)
        "Introduction"                    -> ""                         (nothing → None)

    A real company is either multi-word or carries a company suffix. A lone
    residual adjective ("CORPORATE") is not enough — better None, which the
    required-field check surfaces, than a heading fragment masquerading as a
    vendor. Measured: keeps all 22 working vendors, rejects the heading cases.
    """
    toks = line.split()
    kept = [t for t in toks if t.strip(".,:;#").lower() not in _DOC_STRUCTURE_WORDS]
    if not kept:
        return None
    remainder = " ".join(kept).strip()
    if len(kept) >= 2:
        return remainder
    if len(kept) == 1 and kept[0].strip(".,").lower() in _COMPANY_SUFFIXES:
        return remainder
    return None


def _resolve_from_heading(
    strategy: Dict[str, Any],
    lines: List[str],
) -> Optional[List[Any]]:
    """
    Resolve a field from document headings/labeled sections rather than kv pairs.
    Two modes, tried in the order the profile lists them:

      "after_label": [labels...]   → find a line that IS one of these labels (or
                                      starts with it), return the NEXT non-empty
                                      line's entity name. Reliable: the label
                                      names what follows ("VENDOR", "From",
                                      "Supplier", "Bill From").

      "prominent_line": true       → return the first prominent non-kv line
                                      (the top entity). ONLY safe when the
                                      profile enables it — on an invoice the top
                                      entity is the seller; on a PO it's the
                                      BUYER, so a PO profile must NOT enable this.

    A field that cannot be resolved this way returns None (fail-closed): better
    absent than the wrong entity, which would create false reconciliation
    mismatches.
    """
    if not lines:
        return None

    clean_lines = [l.strip() for l in lines if l.strip() and l.strip() != "[PAGE: 1]"]

    # mode 1: after an explicit section label
    labels = [l.lower() for l in strategy.get("after_label", [])]
    if labels:
        for i, line in enumerate(clean_lines):
            low = line.lower()
            # the label may be the whole line ("VENDOR") or lead a two-column
            # header ("VENDOR SHIP TO") — match on the leading token(s)
            if any(low == lab or low.startswith(lab + " ") or low.startswith(lab + ":")
                   or low.startswith(lab + "\t") for lab in labels):
                # Take the next ENTITY-looking line — not simply the next line.
                # Column flattening interleaves the neighbouring column, so the
                # entity may not be adjacent:
                #     'Bill To:'  /  'Terms: Net 30'  /  'Acme Retail Corp ...'
                # Skip lines that are themselves kv pairs ("Terms: Net 30") or
                # bare labels; the first line that is not one is the entity.
                j = i + 1
                val = None
                while j < len(clean_lines) and j <= i + 4:
                    cand = clean_lines[j]
                    if (_VALUE_IS_LABEL_RE.match(cand) or _LABEL_ONLY_RE.match(cand)
                            or cand.strip().upper() in _SECTION_LABELS):
                        j += 1
                        continue    # a pure kv line, or the next column's heading
                    val = _clean_entity_line(cand, labels)
                    break
                if val is not None:
                    # if the label line was two-column ("VENDOR SHIP TO"), the
                    # entity line is likely also two-column; keep only the part
                    # before a large gap is not available post-flatten, so take
                    # the whole cleaned line — downstream matching is tolerant.
                    if val:
                        return [val]

    # mode 2: prominent top line (profile-gated)
    if strategy.get("prominent_line"):
        # skip leading label/document-type lines; return the first entity-like
        # line. Labels ("SENDER PROFILE", "BILLING STATEMENT", "STATEMENT",
        # "TAX INVOICE") are headers, not vendor names — skip them and keep
        # looking so the real entity on a later line is found.
        # Skip section headers ("SENDER PROFILE", "BILLING STATEMENT") — they
        # name a block, not a company — and keep looking for the real entity.
        NON_VENDOR_LABELS = _SECTION_LABELS
        for line in clean_lines:
            if ":" in line:            # kv line, not a heading
                continue
            if _is_space_kv_line(line):  # space-separated KV field, not a vendor
                continue
            cleaned = _clean_entity_line(line, [])
            if not cleaned:
                continue
            # skip pure labels / document-type headers
            if cleaned.upper() in NON_VENDOR_LABELS:
                continue
            # skip obvious address lines (start with a number)
            if cleaned[0].isdigit():
                continue
            # Strip any document-structure words glued onto this line and keep
            # the real entity — or nothing, if the line was only a heading.
            # "Apex Logistics Services INVOICE" → "Apex Logistics Services";
            # "CORPORATE INVOICE" → None. See _vendor_from_prominent_line.
            vend = _vendor_from_prominent_line(cleaned)
            if vend is None:
                continue
            return [vend]

    return None


# ── strategy: tables (scalar from a column's first cell) ──────────────────

def _resolve_from_tables(
    strategy: Dict[str, Any],
    tables: List[Dict[str, Any]],
) -> Optional[Any]:
    """Return the first cell of the first column (by alias) found in any table.
    Used for scalar fields that live in a table (e.g. a 'Total' cell)."""
    wanted = [_norm(c) for c in strategy.get("columns", [])]
    for table in tables:
        headers = [_norm(h) for h in _headers_of(table)]
        for w in wanted:
            if w in headers:
                idx = headers.index(w)
                for row in _rows_of(table):
                    cell = _cell(row, idx)
                    if cell is not None and str(cell).strip() != "":
                        return cell
    return None


def _cell(row: Any, idx: int) -> Optional[Any]:
    """Get cell at column index from a row that may be a list or a dict
    (col_0.. or header-keyed). Returns None if absent."""
    if isinstance(row, list):
        return row[idx] if idx < len(row) else None
    if isinstance(row, dict):
        # try positional col_N, then the idx-th value
        key = f"col_{idx}"
        if key in row:
            return row[key]
        vals = list(row.values())
        return vals[idx] if idx < len(vals) else None
    return None


_STRATEGIES = {
    "key_values": _resolve_from_key_values,
    "tables":     _resolve_from_tables,
    "heading":    _resolve_from_heading,
    "doc_type":   _resolve_doc_type,
}


# ── table mapping (line-items) ────────────────────────────────────────────

import re as _re
# Matches a value that has another column's "Label:" glued onto it. The label
# must start with a capital and be 1-2 words. NOT 3: with 3 the non-greedy
# match treats 'Foundation Tax Status:' as the label and eats the entity
# ('Acme Research Foundation' -> 'Acme Research'). Ordinary text with a colon
# (times, ratios, URLs already excluded) is not truncated.
_EMBEDDED_LABEL_RE = _re.compile(r"^(.*?\S)\s+[A-Z][A-Za-z]*(?: [A-Z][A-Za-z]*){0,1}\s*:\s")

# NO comma in the key. Tried and reverted, with the measurement:
#
#   PDF column flattening glues a neighbour onto the label —
#   'Silicon Valley, CA 94025 Due Date: August 13, 2026' — and without a comma
#   the line does not match, so that due date is never seen. Allowing the comma
#   recovers it. It also makes things WORSE, because the trailing-label recovery
#   then registers BOTH 'due date' AND its 1-word tail 'date', and 'Date' is an
#   invoice_date alias — so the DUE date silently becomes the INVOICE date.
#
#   Measured over the corpus (invoice_date vs ground truth):
#       with comma:     right=6  wrong=1  missing=2
#       without comma:  right=6  wrong=0  missing=3
#
#   It converts one MISSING into one WRONG and recovers nothing. A missing field
#   is a loud absence the caller can see; a wrong field is a silent error that
#   propagates into dedup and findings. Not a trade worth making.
#
# The real defect is upstream: the tail recovery cannot tell pollution
# ('Acme Corporation Total' → 'Total', correct) from a modifier
# ('Due Date' → 'Date', destroys the meaning). Fixing THAT is the way to
# recover these dates; widening this regex just routes around it into a worse
# failure. Left as it is, deliberately.
_LINE_KV_RE = _re.compile(
    # @ and % belong in a KEY: "VAT @ 20%", "Sales Tax 8.5%". Without them the
    # line does not parse at all, the tax is invisible, and the role verifier
    # reports CONFLICT on a clean invoice. Low risk in practice — an address or
    # a sentence does not carry a colon after an @; and where something odd does
    # slip through, the declared-type check rejects a value of the wrong kind
    # whatever label pointed at it.
    r"^\s*([A-Za-z][\w .\-#/@%]{0,40}?(?:\s*\([^)]{1,12}\))?)\s*[:=]\s+(.+?)\s*$"
)

def _merge_header_value_rows(kv_index, lines):
    """Recover a two-line mini-table: a HEADER line of column labels immediately
    followed by a VALUE line, common in 'statement' layouts where the invoice
    number sits under a 'REF NUMBER' column rather than in a 'key: value' line.

    Handles the messy PDF case where the value row collapsed into one quoted
    cell ('"INV-2026-8819 July 12, 2026 Net 30 USD",,,'): the header is split on
    commas, the value cell is unquoted and split on runs of >=1 space, and the
    two are aligned positionally. Only ADDS keys not already present.
    """
    import re as _r
    def _clean(s): return s.strip().strip('"').strip()
    ne = [l for l in lines if l.strip() and not l.strip().startswith("[")]
    for i in range(len(ne) - 1):
        header = _clean(ne[i]); value = _clean(ne[i+1])
        if "," not in header:
            continue
        cols = [c.strip() for c in header.split(",") if c.strip()]
        if len(cols) < 2:
            continue
        # value row: drop trailing empty CSV cells, unquote, split the payload
        vraw = ne[i+1].strip()
        vraw = vraw.rstrip(",").strip().strip('"').strip()
        if not vraw:
            continue
        # only trust this pairing if the header looks like labels (all short,
        # alphabetic-ish) and the value has as many space-runs as columns-1
        if not all(len(c.split()) <= 3 for c in cols):
            continue
        parts = _r.split(r"\s{1,}", vraw)
        # try to align: first column usually the ref/number (single token)
        if len(parts) < len(cols):
            continue
        # Only emit the FIRST column (the ref/number) from this fallback. On a
        # collapsed value row the remaining columns cannot be aligned reliably
        # (a multi-word date shifts every following field), and a wrong currency
        # or date is worse than none. The first token is the identity anchor we
        # need; the rest are recovered elsewhere or left absent (fail-closed).
        ref_col, ref_val = cols[0], parts[0]
        k = _norm(ref_col)
        if k and k not in kv_index and ref_val:
            kv_index[k] = [ref_val]


# A "value" that is ITSELF a label:value pair means the real value was empty and
# the neighbouring column was flattened onto the line:
#     'Bill To: Terms: Net 30'   ->  "Bill To" is a section header with no inline
#                                    value; "Terms: Net 30" is the next column.
# Registering that would make recipient = "Terms: Net 30". Skip instead, and let
# a heading strategy find the entity on a following line.
_VALUE_IS_LABEL_RE = _re.compile(
    r"^[A-Z][A-Za-z]*(?: [A-Z][A-Za-z]*){0,2}\s*:\s")

# Section headers that name a BLOCK rather than an entity. An entity strategy
# that lands on one of these has found the next column's heading, not a company.
# This is a CLOSED set of document-structure words (not vendor-invented labels),
# so enumerating it is legitimate — cf. ISO currency codes.
_SECTION_LABELS = {
    "INVOICE", "PURCHASE ORDER", "BILL", "SENDER PROFILE",
    "BILLING STATEMENT", "STATEMENT", "TAX INVOICE", "RECEIPT",
    "QUOTE", "QUOTATION", "CREDIT NOTE", "REMITTANCE", "SENDER",
    "FROM", "BILL FROM", "VENDOR", "SUPPLIER", "PROFILE",
    "BILLING PROVIDER", "PROVIDER", "ACCOUNT TRANSACTION RECORD",
    "TRANSACTION RECORD", "METADATA PROFILE", "DEBTOR RECORD",
    "CLIENT ACCOUNT", "INVOICE INFORMATION", "PROJECT CONTEXT",
    "SERVICE PROVIDER", "ISSUER", "BILLED BY", "REMIT TO",
    "PAYMENT METHOD", "PAYMENT DETAILS", "BILLED TO", "BILL TO",
    "SHIP TO", "SOLD TO", "PAYMENT TERMS", "BANK DETAILS",
    "NOTES", "TERMS", "TERMS AND CONDITIONS",
}

# INSTRUMENT TYPES — a CLOSED, standardised set, so enumerating it is
# legitimate (cf. ISO currency codes) and is NOT the alias treadmill: these are
# accounting instruments, not vendor-invented labels. A vendor may call the
# total "Net Outstanding Balance", but they cannot invent a new KIND of
# accounting document.
#
# This matters because instrument type is a DISCRIMINATOR. A debit memo and an
# invoice bearing the same reference, vendor, date and amount are not the same
# obligation twice — they are different instruments with different accounting
# treatment. Without this field they satisfy provable identity and get
# auto-BLOCKED, silently electing one as the real document.
#
# Ordered longest-first: "ALLOCATION DEBIT MEMO" must win over "INVOICE" on a
# line reading "ALLOCATION DEBIT MEMO", and "CREDIT NOTE" over "NOTE".
_DOC_TYPES = (
    "ALLOCATION DEBIT MEMO", "ALLOCATION INVOICE", "ACCOUNT TRANSACTION RECORD",
    "REMITTANCE ADVICE", "SELF-BILLING INVOICE", "COMMERCIAL INVOICE",
    "PROFORMA INVOICE", "PRO FORMA INVOICE", "BILLING STATEMENT",
    "PURCHASE ORDER", "CREDIT NOTE", "CREDIT MEMO", "DEBIT NOTE", "DEBIT MEMO",
    "TAX INVOICE", "GOODS RECEIPT", "DELIVERY NOTE", "QUOTATION",
    "STATEMENT", "INVOICE", "RECEIPT", "QUOTE", "ESTIMATE",
)

# Same charset as _LINE_KV_RE — a label alone on a line ("VAT @ 20%:" with the
# amount on the next line) is exactly how PDF column layouts flatten, so the two
# must agree or the fix only works for one layout.
# A label alone on a line with NO colon, and a value that is unambiguously
# money. Both halves are deliberately strict — see "shape 3" in _merge_line_kv
# for the measurement that set them.
#   label: letters and spacing only, no trailing '.' (that is prose)
#   value: must carry a currency symbol or ISO code — a bare number is not money
_BARE_LABEL_RE = _re.compile(r"^\s*([A-Za-z][A-Za-z .\-/&]{0,38}[A-Za-z])\s*$")
_BARE_MONEY_RE = _re.compile(
    r"^\s*[-+]?(?:[£$€¥]\s*\d[\d.,]*"
    r"|\d[\d.,]*\s*(?:[£$€¥]|USD|EUR|GBP|CHF|JPY|CAD|AUD))\s*$", _re.I)

_LABEL_ONLY_RE = _re.compile(
    r"^\s*([A-Za-z][\w .\-#/@%]{0,40}?(?:\s*\([^)]{1,12}\))?)\s*[:=]\s*$")


def _register_kv(kv_index, key, value, tails: bool = True):
    """Register one label/value into the index, under the full key AND (unless
    `tails=False`) its trailing 1-2 word label.

    The trailing-label part matters for two independent reasons:
      * two-column flattening prepends the neighbouring column onto the key
        ("Acme Corporation PO #: PO-9921" — the real label is "PO #");
      * a compound label may only partly match the profile's aliases
        ("Grand Total Due" is not an alias, but its tail "Total Due" is).

    Only ADDS; an existing key keeps its engine-detected values first.

    This is shared by BOTH line shapes ("Label: value" on one line, and "Label:"
    with the value on the next). Having it in only one of them made extraction
    depend on how the PDF happened to flatten — the same "Grand Total Due:"
    resolved on one layout and vanished on the other.
    """
    if not value or len(key.split()) > 6:
        return
    if _VALUE_IS_LABEL_RE.match(value):
        return          # the "value" is the next column's kv pair, not ours
    keys_to_add = []
    if len(key.split()) <= 4:
        keys_to_add.append(key)
    toks = key.split()
    if tails and len(toks) >= 2:
        # Callers pass tails=False when the label CANNOT have been polluted —
        # see _merge_line_kv shape 3. Recovering a tail from an intact label
        # does not rescue it, it corrupts a different field: "Tax total" yields
        # "total", which then collides with the document's real Total. The
        # engine refuses the ambiguity and reports no total at all, on an
        # invoice that plainly states one.
        for tail in (" ".join(toks[-2:]), toks[-1]):
            if tail and tail not in keys_to_add:
                keys_to_add.append(tail)
    for kk in keys_to_add:
        k = _norm(kk)
        if not k:
            continue
        existing = kv_index.get(k)
        if existing:
            if value not in [str(v).strip() for v in existing]:
                existing.append(value)
        else:
            kv_index[k] = [value]


def _merge_line_kv(kv_index, lines):
    """Add 'key: value' pairs found directly in raw lines, for kv lines the
    engine did not annotate (they sat in a context/prose block). Only ADDS;
    engine-detected pairs keep precedence. Conservative: the key must look like
    a label and the value must be non-empty.

    Two line shapes, deliberately routed through the SAME registration:
      "Grand Total Due: $2,000.00"      value on the same line
      "Grand Total Due:" / "$2,000.00"  value on the next line (PDF footers)
    """
    clean = [l.strip() for l in lines]
    for idx, line in enumerate(clean):
        if not line or line.startswith("[") or "://" in line:
            continue
        # shape 2: bare "Label:" with the value on the following line
        lm = _LABEL_ONLY_RE.match(line)
        if lm and idx + 1 < len(clean):
            nxt = clean[idx + 1]
            if nxt and ":" not in nxt and not nxt.startswith("["):
                _register_kv(kv_index, lm.group(1).strip(), nxt)
            continue
        # shape 3: bare "Label" — NO COLON — with a money value on the next line
        #
        # The colon is a convention of typed documents. Invoices generated from
        # HTML — Paddle, Stripe, Xero, and most modern billing systems — put the
        # label in one table cell and the amount in the next, and a colon never
        # appears anywhere on the page:
        #
        #     Subtotal        Total
        #     $25.00          $29.00
        #
        # Our whole test corpus was authored with colons, so this was invisible
        # until a real invoice arrived: every total, subtotal and tax on it was
        # unreadable, and the document reported "no total" while stating one.
        #
        # This is the riskiest kind of rule — pairing two adjacent lines on the
        # strength of their SHAPE — so it is constrained by what the value is,
        # not by what the label looks like. Measured across the corpus, a rule
        # matching any bare label above any bare number produced 39 pairs, of
        # which 34 were garbage: 'Bus Admittance Matrix' → '8' from the lecture
        # in the NEGATIVE set, 'regulatory interest.' → '3' from prose and page
        # numbers, 'Lead DevOps Engineering Support' → '40' from a line item and
        # its quantity. Requiring the value to be CURRENCY-MARKED left exactly 5
        # pairs, all on the real invoice, all correct.
        #
        # A bare integer is a page number, a quantity, an equation number. A
        # currency-marked amount is money. That distinction is structural, and it
        # is the only reason this rule is safe enough to exist.
        if idx + 1 < len(clean):
            bl = _BARE_LABEL_RE.match(line)
            if bl and _BARE_MONEY_RE.match(clean[idx + 1]):
                # tails=False: this label sits ALONE on its line, so no
                # neighbouring column can have been glued onto it — there is
                # nothing to recover, and recovering anyway turns "Tax total"
                # into "total" and destroys the real total.
                _register_kv(kv_index, bl.group(1).strip(), clean[idx + 1],
                             tails=False)
                continue
        # shape 1: "Label: value" on one line
        m = _LINE_KV_RE.match(line)
        if not m:
            continue
        key, value = m.group(1).strip(), m.group(2).strip()
        # column flattening can glue the NEXT column's "Label: ..." onto the
        # value; truncate so the value is just this field's.
        vm = _EMBEDDED_LABEL_RE.match(value)
        if vm:
            value = vm.group(1).strip()
        _register_kv(kv_index, key, value)


def _build_kv_index(kv_pairs: List[Dict[str, Any]]) -> Dict[str, List[Any]]:
    """
    Index kv pairs by normalized key, collecting ALL values in document order.

    Engine-detected pairs are filtered the same way _register_kv filters
    line-scanned ones: a value that is ITSELF a label:value pair came from a
    flattened neighbouring column ('Bill To: Terms: Net 30' -> "Bill To" has no
    inline value at all), and registering it would resolve recipient to
    "Terms: Net 30".

    Deliberately NOT "first occurrence wins". A resolver is never allowed to
    discard competing values: if a document anchor (invoice_number, vendor)
    appears twice, that is evidence the input is not one logical document. The
    caller decides what that means; the index must not destroy the evidence.
    """
    index: Dict[str, List[Any]] = {}
    for p in kv_pairs:
        k = _norm(p.get("key"))
        if not k:
            continue
        v = p.get("value")
        if v is None or not str(v).strip():
            continue        # an empty value is not an answer ("Billed To: " )
        if _VALUE_IS_LABEL_RE.match(str(v).strip()):
            continue        # merged next column, not this key's value
        index.setdefault(k, []).append(v)
    return index


def _distinct(values: List[Any]) -> List[Any]:
    """Distinct values, order-preserving. Values equal after trimming are the
    same value (a repeated header restating the same invoice number is not a
    competing root)."""
    seen, out = set(), []
    for v in values:
        key = str(v).strip()
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


def _map_line_items(
    spec: Dict[str, Any],
    tables: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Find the source table matching `identify_by` and remap its columns to
    canonical line-item field names. Returns a list of {canonical: value}."""
    identify = [_norm(c) for c in spec.get("identify_by", [])]
    col_map  = spec.get("columns", {})

    # pick the first table whose headers include any identify_by signature
    chosen = None
    for table in tables:
        headers = [_norm(h) for h in _headers_of(table)]
        if any(sig in headers for sig in identify):
            chosen = table
            break
    if chosen is None:
        return []

    headers = [_norm(h) for h in _headers_of(chosen)]
    # resolve each canonical column to a source index
    canon_to_idx: Dict[str, int] = {}
    for canon, aliases in col_map.items():
        for a in aliases:
            na = _norm(a)
            if na in headers:
                canon_to_idx[canon] = headers.index(na)
                break

    items: List[Dict[str, Any]] = []
    for row in _rows_of(chosen):
        item: Dict[str, Any] = {}
        for canon, idx in canon_to_idx.items():
            cell = _cell(row, idx)
            if cell is not None:
                item[canon] = cell
        if item:
            items.append(item)
    return items


# ── public API ────────────────────────────────────────────────────────────

PARTITION_SINGLE    = "SINGLE_DOCUMENT"
PARTITION_MULTI     = "MULTI_DOCUMENT_DETECTED"
PARTITION_AMBIGUOUS = "AMBIGUOUS"


def _type_ok(value: Any, declared: Optional[str]) -> bool:
    """Does this value satisfy the type its profile DECLARES for the field?

    The profiles have always declared types — `due_date` is `{"type": "date"}`,
    `total` is `{"type": "money"}` — and the resolver never checked them. That
    let a money amount land in a date field and be reported as fact:

        'Total Balance Due: $26,476.50'  → trailing-label recovery registers
                                           "Due"  →  "Due" is a due_date alias
                                           →  due_date = "$26,476.50"

    That is not an alias bug to be fixed by deleting "Due" — the same collision
    will recur for the next generic label, and deleting aliases loses real
    matches. It is a missing CONSTRAINT. A declared type is exactly the kind of
    invariant this engine trusts elsewhere: arithmetic for the total, instrument
    sets for document type. A date field must hold something that parses as a
    date, whatever label pointed at it.

    Failing the check means the strategy did not resolve, so the resolver moves
    on to the next alias or strategy — a wrong value is never preferred to
    continuing the search, and an unresolved field is reported as absent rather
    than filled with nonsense.
    """
    if value is None or str(value).strip() == "":
        return False
    if declared == "date":
        return canon_date(value) is not None
    if declared == "money":
        return canon_amount(value) is not None
    if declared == "currency":
        return canon_currency(value) is not None
    return True          # string / untyped: no constraint to check


def resolve_document_view(
    kv_pairs: List[Dict[str, Any]],
    tables:   List[Dict[str, Any]],
    profile:  Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    lines:    Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Build a canonical Document View from raw pipeline output using a profile's
    declarative resolution strategies.

    ── Hard architectural invariant ─────────────────────────────────────────
    A resolver is NEVER allowed to discard competing root entities. If a field
    the profile declares `cardinality: exactly_one` resolves to two distinct
    values, the input is not one logical document. That is a PARTITION error,
    not a validation error — and the resolver refuses to pick a winner.

    Rather than confidently returning a plausible but blended document, the
    view carries `partition` status:

        SINGLE_DOCUMENT         — one document root; fields resolved normally
        MULTI_DOCUMENT_DETECTED — competing anchors; suspicion >= threshold
        AMBIGUOUS               — some repetition, below threshold

    When status is not SINGLE_DOCUMENT, anchor fields with competing values are
    left UNRESOLVED (absent from `fields`) and reported in
    `partition.boundary_candidates`. Refusing to produce a believable lie is
    more valuable than producing a complete-looking answer.

    Returns
    -------
    {
      "fields":   {canonical_field: value},
      "tables":   {name: [ {canonical_col: value} ]},
      "metadata": {...},
      "_resolution": {field: strategy_used | None},
      "partition": {
          "status": ..., "confidence": float,
          "boundary_candidates": [ {field, values, anchor_weight} ],
      },
    }
    """
    kv_index = _build_kv_index(kv_pairs)
    # Fallback: harvest 'key: value' pairs directly from the raw lines. The
    # engine only kv-annotates lines inside blocks it classified as kv; a clean
    # 'Invoice Number: INV-001' line sitting in a two-column header block gets
    # classified as context/prose and is never annotated, so it never reaches
    # kv_pairs. Scanning the lines recovers these without disturbing the pairs
    # the engine did find (engine pairs take precedence; line pairs only ADD).
    if lines:
        _merge_line_kv(kv_index, lines)
        _merge_header_value_rows(kv_index, lines)
    schema   = profile.get("schema", {})
    threshold = profile.get("partition", {}).get("suspicion_threshold", 0.8)

    fields: Dict[str, Any] = {}
    trace:  Dict[str, Any] = {}
    candidates: List[Dict[str, Any]] = []
    suspicion = 0.0

    for field, strategies in profile.get("field_resolution", {}).items():
        values = None
        used   = None
        declared = (schema.get(field) or {}).get("type")
        for strategy in strategies:
            kind = strategy.get("from")
            fn = _STRATEGIES.get(kind)
            if fn is None:
                continue
            if kind == "key_values":
                values = fn(strategy, kv_index)
            elif kind == "tables":
                single = fn(strategy, tables)
                values = [single] if single is not None else None
            elif kind == "heading":
                values = fn(strategy, lines or [])
            elif kind == "doc_type":
                values = fn(strategy, lines or [])
            # Honour the declared type. A strategy that produced a value of the
            # wrong kind has not resolved the field, so keep looking.
            values = [v for v in (values or []) if _type_ok(v, declared)]
            if values:
                used = kind
                break

        trace[field] = used
        if not values:
            continue

        spec        = schema.get(field, {})
        cardinality = spec.get("cardinality", "one")
        weight      = float(spec.get("anchor_weight", 0.0))
        distinct    = _distinct(values)

        if cardinality == "many":
            fields[field] = distinct
            continue

        if len(distinct) > 1:
            # Competing root entities. Do NOT choose. Record evidence.
            candidates.append({
                "field":         field,
                "values":        distinct,
                "cardinality":   cardinality,
                "anchor_weight": weight,
            })
            suspicion += weight
            if cardinality == "exactly_one":
                # An anchor conflict: leave the field unresolved entirely.
                continue
            # Weaker cardinality: still refuse to blend; leave unresolved.
            continue

        fields[field] = distinct[0]

    suspicion = min(1.0, round(suspicion, 4))
    if not candidates:
        status = PARTITION_SINGLE
        confidence = 1.0
    elif suspicion >= threshold:
        status = PARTITION_MULTI
        confidence = suspicion
    else:
        status = PARTITION_AMBIGUOUS
        confidence = suspicion

    # line-item and other declared tables
    out_tables: Dict[str, Any] = {}
    for name, spec in profile.get("tables", {}).items():
        mapped = _map_line_items(spec, tables)
        if mapped:
            out_tables[name] = mapped

    # ── Role verification ─────────────────────────────────────────────────
    # Everything above assigns fields by matching LABELS against alias lists —
    # low-authority evidence that an unknown vocabulary defeats silently and an
    # adversary defeats deliberately. This checks those assignments against
    # evidence that does not depend on labels (arithmetic identity).
    #
    # Reports ONLY: it deliberately does not mutate `fields`. That keeps this
    # purely additive (no existing consumer can regress) and, more importantly,
    # never silently rewrites a stated figure — a stated total that violates the
    # document's own arithmetic may be an extraction error OR real over-billing,
    # and only the caller can decide. See profiles/role_verifier.py.
    verification: Dict[str, Any] = {}
    if lines:
        from profiles.role_verifier import verify_total   # local: breaks import cycle
        from profiles.money_tree import verify_total_tree

        # The money-structure tree does math on EVERY money region (each section
        # subtotal, the discount/tax chain, the grand total) rather than
        # searching for one total, and it computes a total when the document
        # states none. It is the primary verifier.
        tree = verify_total_tree(fields, lines, out_tables.get("line_items"))

        # Fall back to the label-based verifier ONLY when the tree could not
        # recover any money structure (tree_verdict UNVERIFIABLE) AND the old
        # verifier actually concluded something — so wiring the tree in can
        # never regress a file the old path could verify.
        if tree.get("tree_verdict") == "UNVERIFIABLE":
            legacy = verify_total(fields, lines, out_tables.get("line_items"))
            if legacy.get("verdict") not in (None, "UNVERIFIABLE"):
                verification["total"] = legacy
            else:
                verification["total"] = tree
        else:
            verification["total"] = tree

        # FILL: if the resolver's label matching did not find a total but the
        # tree recovered one from the document's arithmetic, populate it — and
        # mark HOW it was derived so a computed total is never shown as if the
        # document stated it. The tree total is what verification already checked,
        # so fields and verification stay in sync (previously fields.total stayed
        # None and total_required failed even though verification found 55,309).
        vt = verification.get("total", {})
        if not fields.get("total") and vt.get("identified") is not None:
            fields["total"] = vt["identified"]
            fields["total_derived"] = bool(vt.get("total_is_derived"))
            if vt.get("total_note"):
                fields["total_note"] = vt["total_note"]

        # PUBLISH the tree's recovered line items. The label/table extractor
        # (_map_line_items) reads only the FIRST matching table, so on a
        # multi-section invoice it captures one category and drops the rest —
        # Meridian's 25 rows collapse to 4. The money-tree, reading the whole
        # document in order, already recovered them all. When the tree found
        # MORE priced rows than the table path, its set is the fuller truth, so
        # publish it as the line items the rest of the system sees. This is the
        # core structure-first move: one recovered structure, published once,
        # not re-derived and discarded per stage.
        tree_items = vt.get("line_items") or []
        table_items = out_tables.get("line_items") or []
        if len(tree_items) > len(table_items):
            out_tables["line_items"] = tree_items
            out_tables["line_items_source"] = "money_tree"
        # always publish the full node structure alongside, so coverage and
        # reconciliation can see every money atom the tree accounted for.
        if vt.get("nodes"):
            out_tables["money_nodes"] = vt["nodes"]

    return {
        "fields":       fields,
        "tables":       out_tables,
        "metadata":     metadata or {},
        "verification": verification,
        "_resolution":  trace,
        "partition": {
            "status":              status,
            "confidence":          confidence,
            "boundary_candidates": candidates,
            "action": ("requires_partition"
                       if status != PARTITION_SINGLE else None),
        },
    }
