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

from typing import Any, Dict, List, Optional


# ── normalization ────────────────────────────────────────────────────────

def _norm(label: Any) -> str:
    """Normalize a label for matching: lowercase, trimmed, trailing colon and
    surrounding whitespace removed, and a trailing parenthetical unit/qualifier
    stripped so "Total Due (USD)" matches "Total Due" and "Tax (0%)" matches
    "Tax". Purely syntactic (no domain knowledge)."""
    import re as _re
    s = str(label).strip()
    if s.endswith(":"):
        s = s[:-1].strip()
    # strip a single trailing parenthetical: "Total Due (USD)" -> "Total Due"
    s = _re.sub(r"\s*\([^)]{1,12}\)\s*$", "", s).strip()
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
    # cut at the first company-suffix token (keeps the leading entity only)
    SUFFIXES = ("inc", "inc.", "llc", "ltd", "ltd.", "corp", "corp.",
                "corporation", "co", "co.", "company", "solutions", "agency",
                "group", "partners", "associates", "systems", "technologies",
                "services", "gmbh", "sa", "ag", "plc", "limited")
    tokens = s.split()
    for i, tok in enumerate(tokens):
        if tok.lower().strip(".,") in SUFFIXES:
            return " ".join(tokens[:i + 1])
    return s


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
            if any(low == lab or low.startswith(lab + " ") or low.startswith(lab + "\t")
                   for lab in labels):
                # take the next non-empty line as the entity
                if i + 1 < len(clean_lines):
                    val = _clean_entity_line(clean_lines[i + 1], labels)
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
        NON_VENDOR_LABELS = {
            "INVOICE", "PURCHASE ORDER", "BILL", "SENDER PROFILE",
            "BILLING STATEMENT", "STATEMENT", "TAX INVOICE", "RECEIPT",
            "QUOTE", "QUOTATION", "CREDIT NOTE", "REMITTANCE", "SENDER",
            "FROM", "BILL FROM", "VENDOR", "SUPPLIER", "PROFILE",
            # section headers seen on "statement"/"record" style layouts
            "BILLING PROVIDER", "PROVIDER", "ACCOUNT TRANSACTION RECORD",
            "TRANSACTION RECORD", "METADATA PROFILE", "DEBTOR RECORD",
            "CLIENT ACCOUNT", "INVOICE INFORMATION", "PROJECT CONTEXT",
            "SERVICE PROVIDER", "ISSUER", "BILLED BY", "REMIT TO",
        }
        for line in clean_lines:
            if ":" in line:            # kv line, not a heading
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
            return [cleaned]

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
}


# ── table mapping (line-items) ────────────────────────────────────────────

import re as _re
# Matches a value that has another column's "Label:" glued onto it. The label
# must start with a capital and be 1-3 words, so ordinary text with a colon
# (times, ratios, URLs already excluded) is not truncated.
_EMBEDDED_LABEL_RE = _re.compile(r"^(.*?\S)\s+[A-Z][A-Za-z]*(?: [A-Z][A-Za-z]*){0,2}\s*:\s")

_LINE_KV_RE = _re.compile(
    r"^\s*([A-Za-z][\w .\-#/]{0,40}?(?:\s*\([^)]{1,12}\))?)\s*[:=]\s+(.+?)\s*$"
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


def _merge_line_kv(kv_index, lines):
    """Add 'key: value' pairs found directly in raw lines for kv lines the engine
    did not annotate (they sat in a context/prose block). Only ADDS values under
    keys, never removes; skips a key already present so engine pairs keep
    precedence. Conservative: key must look like a label (starts with a letter,
    <=4 words) and the value must be non-empty."""
    _LABEL_ONLY_RE = _re.compile(r"^\s*([A-Za-z][\w .\-#/]{0,40}?(?:\s*\([^)]{1,12}\))?)\s*[:=]\s*$")
    clean = [l.strip() for l in lines]
    for idx, raw in enumerate(clean):
        line = raw
        if not line or line.startswith("[") or "://" in line:
            continue
        # "Label:" with the value on the FOLLOWING line (e.g. "Grand Total:" then
        # "$1,250.00" on the next line — common in single-column PDF footers).
        lm = _LABEL_ONLY_RE.match(line)
        if lm and idx + 1 < len(clean):
            nxt = clean[idx + 1]
            if nxt and ":" not in nxt and not nxt.startswith("["):
                k = _norm(lm.group(1))
                if k and k not in kv_index:
                    kv_index[k] = [nxt]
            continue
        m = _LINE_KV_RE.match(line)
        if not m:
            continue
        key, value = m.group(1).strip(), m.group(2).strip()
        # Two/three-column flattening merges neighbouring columns onto one line:
        #   "Acme Corporation Invoice Number: VCG-2026-9812 Project: Odyssey"
        # The captured VALUE then carries the next column's "Label: ..." text.
        # Truncate at the first embedded label so the value is just this field's
        # ("VCG-2026-9812", not "VCG-2026-9812 Project: Odyssey").
        vm = _EMBEDDED_LABEL_RE.match(value)
        if vm:
            value = vm.group(1).strip()
        if not value or len(key.split()) > 6:
            continue
        keys_to_add = []
        if len(key.split()) <= 4:
            keys_to_add.append(key)
        # Two-column flattening can prepend the left column onto the key, e.g.
        # "Acme Corporation PO #: PO-9921" — the real label is the TRAILING
        # portion ("PO #"). Also register the last 1-2 word label ending in a
        # symbol (#) or a known short token, so the value is still findable.
        toks = key.split()
        if len(toks) >= 2:
            tail2 = " ".join(toks[-2:])
            tail1 = toks[-1]
            for tail in (tail2, tail1):
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


def _build_kv_index(kv_pairs: List[Dict[str, Any]]) -> Dict[str, List[Any]]:
    """
    Index kv pairs by normalized key, collecting ALL values in document order.

    Deliberately NOT "first occurrence wins". A resolver is never allowed to
    discard competing values: if a document anchor (invoice_number, vendor)
    appears twice, that is evidence the input is not one logical document. The
    caller decides what that means; the index must not destroy the evidence.
    """
    index: Dict[str, List[Any]] = {}
    for p in kv_pairs:
        k = _norm(p.get("key"))
        if k:
            index.setdefault(k, []).append(p.get("value"))
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
        verification["total"] = verify_total(
            fields, lines, out_tables.get("line_items"))

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
