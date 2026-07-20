"""Coverage Validation — the stage that asks "what did we miss?" before any
answer is presented.

Today the pipeline says "here is what I found" and can silently drop 21 of 25
line items while still producing a polished total. This stage inverts that: it
independently counts the countable material actually present in the raw
document — money values, line-item rows, labelled reference fields — and checks
how much of it the pipeline assigned to a structure (a field, a line item, a
recognised region). The gap is the coverage deficit.

It is deliberately DOMAIN-FREE. It knows nothing about invoices, vendors, or
totals. It counts atoms that any financial document is made of and reports the
fraction explained. A low score means the later stages (money-tree,
reconciliation, report) are reasoning over a document only partially understood,
and the pipeline should refuse to present a confident answer rather than build
one on incomplete evidence.

The output is a scorecard, not a fix. Its value is that it makes silent
under-reading loud.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple


# A monetary amount: optional currency mark, optional accounting parentheses,
# thousands separators, exactly two decimals. This is the universal "money atom"
# — it does not depend on any label.
_MONEY_RE = re.compile(r'[\$€£]?\s?\(?-?[\d,]+\.\d{2}\)?')

# A row that looks like a line item: has a quantity/unit pattern OR ends in a
# money amount preceded by descriptive text. Again: no invoice vocabulary.
_QTY_UNIT_RE = re.compile(
    r'\b\d+(?:\.\d+)?\s*(hrs?|days?|qtr|lic|seat|trip|nights?|flat|ea|month|'
    r'units?|pcs?|items?)\b', re.IGNORECASE)


def _money_values(text: str) -> List[float]:
    """Every monetary value in the raw text, as floats."""
    out: List[float] = []
    for m in _MONEY_RE.findall(text):
        s = (m.replace('$', '').replace('€', '').replace('£', '')
              .replace(',', '').replace('(', '-').replace(')', '').strip())
        try:
            out.append(round(float(s), 2))
        except ValueError:
            continue
    return out


def _looks_like_line_item(line: str) -> bool:
    """A raw line that carries a priced quantity — a line item, in any layout."""
    if not _MONEY_RE.search(line):
        return False
    return bool(_QTY_UNIT_RE.search(line))


# A percentage like "8.25%" or "(16%)" is NOT a money atom — it is a rate.
_PERCENT_RE = re.compile(r'\(?\d+(?:\.\d+)?\s*%\)?')
# European money: 1.850,00 (dot thousands, comma decimal), optional trailing €.
_EU_MONEY_RE = re.compile(r'(?<![\d.])-?\d{1,3}(?:\.\d{3})*,\d{2}(?![\d.])')


def _parse_amount(token: str) -> Optional[float]:
    """Parse one money token to a float, or None. Handles US (1,850.00) and
    European (1.850,00) formats and accounting parentheses."""
    t = (token.replace('$', '').replace('€', '').replace('£', '')
          .replace(' ', '').strip())
    neg = t.startswith('(') and t.endswith(')')
    t = t.strip('()')
    # European: dot-thousands + comma-decimal
    if re.fullmatch(r'-?\d{1,3}(?:\.\d{3})*,\d{2}', t):
        t = t.replace('.', '').replace(',', '.')
    else:
        t = t.replace(',', '')
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _amounts_only(lines: List[str]) -> List[float]:
    """The meaningful money atoms: the AMOUNT on each line (its rightmost money
    value), not every number. Skips percentages (rates, not money) and zero
    values (no money moved). Handles US and European number formats. On a priced
    row "24 hr $185.00 $4,440.00" the amount is $4,440 — the unit price is a
    component, not a separate atom."""
    out: List[float] = []
    for l in lines:
        # strip percentages first so "Sales Tax (8.25%):" doesn't yield 8.25
        cleaned = _PERCENT_RE.sub(' ', l)
        # Choose the number format by structure, not by priority. A European
        # token (period-thousands + comma-decimal, e.g. "1.850,00") and a US
        # token ("$6,250.00") are mutually exclusive on a well-formed line, but
        # each regex can match a FRAGMENT of the other. So: if the line clearly
        # contains a European amount (comma-decimal with two trailing digits and
        # no US period-decimal), parse it European; otherwise US.
        has_eu = bool(_EU_MONEY_RE.search(cleaned))
        has_us_dec = bool(re.search(r'\d\.\d{2}(?!\d)', cleaned))
        token = None
        if has_eu and not has_us_dec:
            m = _EU_MONEY_RE.findall(cleaned)
            token = m[-1] if m else None
        else:
            m = _MONEY_RE.findall(cleaned)
            token = m[-1] if m else None
        if token is None:
            continue
        v = _parse_amount(token)
        if v is None:
            continue
        # a zero amount is not money that moved — nothing to "cover"
        if abs(v) < 0.005:
            continue
        out.append(round(v, 2))
    return out


def _raw_inventory(lines: List[str]) -> Dict[str, Any]:
    """Count the countable atoms actually present in the raw document."""
    # amount atoms — one per money-bearing line (the amount column)
    money = _amounts_only(lines)
    line_item_rows = [l for l in lines if _looks_like_line_item(l)]
    return {
        "money_values": money,
        "money_count": len(money),
        "line_item_rows": len(line_item_rows),
        "line_item_row_text": line_item_rows,
    }


def _extracted_money(view: Dict[str, Any]) -> List[float]:
    """Every money value the pipeline actually placed into a structure —
    fields, line items, and the money regions the verifier reasoned over."""
    out: List[float] = []

    def _num(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            n = round(float(v), 2)
            return None if abs(n) < 0.005 else n
        n = _parse_amount(str(v))
        if n is None:
            return None
        return None if abs(n) < 0.005 else round(n, 2)

    # money that landed in fields
    for k in ("subtotal", "tax", "total", "discount", "shipping", "taxable_subtotal"):
        n = _num((view.get("fields") or {}).get(k))
        if n is not None:
            out.append(n)

    # money inside every extracted line item (any numeric-looking cell)
    for item in ((view.get("tables") or {}).get("line_items") or []):
        for v in (item.values() if isinstance(item, dict) else []):
            n = _num(v)
            if n is not None:
                out.append(n)

    # money the verifier accounted for via its regions
    vt = ((view.get("verification") or {}).get("total") or {})
    for reg in vt.get("regions", []):
        for key in ("stated", "expected"):
            n = _num(reg.get(key))
            if n is not None:
                out.append(n)

    # the PUBLISHED money nodes — the tree's full recovered structure (every
    # line item, section subtotal, tax, discount, total). This is what
    # "publish what's understood" makes visible; coverage must read it, or it
    # under-counts the very structure the tree recovered (dropping tax/discount).
    for src_key in ("money_nodes",):
        for node in ((view.get("tables") or {}).get(src_key) or []):
            n = _num(node.get("value") if isinstance(node, dict) else None)
            if n is not None:
                out.append(n)

    return out


def compute_coverage(view: Dict[str, Any], lines: List[str]) -> Dict[str, Any]:
    """Compare what the raw document contains against what the pipeline
    assigned to a structure. Returns a scorecard with per-atom coverage and an
    overall verdict. Purely a measurement — it changes nothing upstream."""
    raw = _raw_inventory(lines)

    # ── money coverage ───────────────────────────────────────────────────────
    # Coverage is by MULTISET: a value present three times in the document must
    # be accounted for three times, so a single extracted subtotal cannot "cover"
    # every occurrence of that number.
    raw_money = list(raw["money_values"])
    got_money = _extracted_money(view)

    from collections import Counter
    # match on absolute value — sign is a role concern (discount vs charge), not
    # a coverage concern; the atom is "was this amount accounted for at all".
    raw_c = Counter(abs(v) for v in raw_money)
    got_c = Counter(abs(v) for v in got_money)
    accounted = 0
    for val, need in raw_c.items():
        accounted += min(need, got_c.get(val, 0))
    money_total = len(raw_money)
    has_money = money_total > 0
    money_cov = (accounted / money_total) if money_total else None

    # unexplained money values (what fell through) — the actionable list
    unexplained: List[float] = []
    for val, need in raw_c.items():
        missed = need - min(need, got_c.get(val, 0))
        unexplained += [val] * missed  # abs values

    # ── line-item coverage ───────────────────────────────────────────────────
    raw_rows = raw["line_item_rows"]
    got_rows = len((view.get("tables") or {}).get("line_items") or [])
    row_cov = (got_rows / raw_rows) if raw_rows else 1.0

    # ── verdict ──────────────────────────────────────────────────────────────
    # The pipeline should refuse to present a confident answer when it has
    # demonstrably not read the whole document. Thresholds are deliberate: a
    # money or row coverage below 0.9 means real content was dropped.
    # a document with no money and no priced rows is not an invoice-like doc;
    # coverage is Not Applicable rather than "perfectly complete".
    if not has_money and raw_rows == 0:
        return {
            "verdict": "NO_FINANCIAL_CONTENT",
            "overall_coverage": None,
            "money": {"found": 0, "accounted": 0, "coverage": None,
                      "unexplained_values": [], "unexplained_count": 0},
            "line_items": {"rows_in_document": 0, "rows_extracted": got_rows,
                           "coverage": None},
            "safe_to_present": True,
        }
    parts = [c for c in (money_cov, row_cov) if c is not None]
    overall = min(parts) if parts else 1.0
    if overall >= 0.95:
        verdict = "COMPLETE"
    elif overall >= 0.85:
        verdict = "MOSTLY_COMPLETE"
    else:
        verdict = "INCOMPLETE"

    return {
        "verdict": verdict,
        "overall_coverage": round(overall, 3),
        "money": {
            "found": money_total,
            "accounted": accounted,
            "coverage": round(money_cov, 3) if money_cov is not None else None,
            "unexplained_values": sorted(set(unexplained), reverse=True),
            "unexplained_count": len(unexplained),
        },
        "line_items": {
            "rows_in_document": raw_rows,
            "rows_extracted": got_rows,
            "coverage": round(row_cov, 3),
        },
        "safe_to_present": verdict != "INCOMPLETE",
    }
