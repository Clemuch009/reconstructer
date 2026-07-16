# relationship_engine/canonicalize.py
#
# Stage 1 of the duplicate engine: CANONICALIZATION.
#
# Never compare raw invoice text. Normalize first — this is what kills most
# false negatives (the same invoice written slightly differently) BEFORE any
# matching runs. Every function here is deterministic and explainable: no fuzzy
# string distance, no ML. Same input → same canonical output, always.
#
# The canonical fields become the identity keys (Stage 2) and the comparison
# basis for the evidence engine (Stage 4). Getting this stage right is the
# single highest-leverage thing in the whole engine.
#
# Reuses existing normalization where it already exists in the codebase:
#   - amount cleaning mirrors analysis/rules_engine._to_number
#   - vendor legal-suffix handling mirrors profiles/resolver's SUFFIXES
# and adds the two pieces that did not exist yet: invoice-number and date.

import re
from datetime import datetime
from typing import Any, Dict, Optional


# ── invoice number ─────────────────────────────────────────────────────────
#
# "INV-001", "INV 001", "inv001", "INV_001" → "INV001".
# Strip separators and punctuation, uppercase. Keep alphanumerics only, so the
# same business number written any which way collapses to one canonical form.
# We do NOT strip leading zeros (INV001 and INV01 are different numbers).

_INVNUM_STRIP_RE = re.compile(r"[^A-Za-z0-9]")

def canon_invoice_number(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = _INVNUM_STRIP_RE.sub("", str(value)).upper()
    return s or None


# ── vendor ─────────────────────────────────────────────────────────────────
#
# "Horizon IT Solutions Corp" / "Horizon IT Solutions Ltd" → "HORIZON IT".
# Lowercase-fold, drop legal suffixes and generic descriptor tail words, collapse
# whitespace, uppercase. Mirrors the resolver's suffix list so extraction and
# dedup agree on what a vendor's canonical identity is.

_VENDOR_SUFFIXES = {
    "inc", "incorporated", "llc", "llp", "ltd", "limited", "corp",
    "corporation", "co", "company", "gmbh", "sa", "ag", "plc", "pty",
    "bv", "nv", "srl", "spa",
}
# generic descriptor words that vary between references to the same vendor and
# carry no identity ("Solutions", "Services", "Group"). Dropped from the tail
# so "Horizon IT Solutions" == "Horizon IT".
_VENDOR_DESCRIPTORS = {
    "solutions", "services", "group", "partners", "associates", "systems",
    "technologies", "technology", "holdings", "enterprises", "consulting",
    "international", "worldwide", "global",
}
_VENDOR_STRIP_RE = re.compile(r"[.,]")

def canon_vendor(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = _VENDOR_STRIP_RE.sub("", str(value)).lower().strip()
    if not s:
        return None
    tokens = s.split()
    # drop trailing legal-suffix and generic-descriptor tokens (repeatedly, so
    # "Solutions Corp" → "" both go)
    while tokens and (tokens[-1] in _VENDOR_SUFFIXES or tokens[-1] in _VENDOR_DESCRIPTORS):
        tokens.pop()
    if not tokens:
        # vendor was ONLY suffix/descriptor words — fall back to the stripped
        # original rather than returning nothing
        tokens = s.split()
    return " ".join(tokens).upper()


# ── amount + currency ──────────────────────────────────────────────────────
#
# "$1,100.00" → (1100.0, "USD"). Strip currency symbols and thousands
# separators; infer the currency from a leading/trailing symbol or an explicit
# ISO code. Amount and currency are returned together because a duplicate check
# must compare amount WITHIN a currency (USD 1100 ≠ EUR 1100).

_SYMBOL_TO_ISO = {"$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY"}
_ISO_RE = re.compile(r"\b(USD|EUR|GBP|JPY|CAD|AUD|CHF|CNY|INR)\b", re.IGNORECASE)
_AMOUNT_CLEAN_RE = re.compile(r"[,\s$£€¥]")

_CURRENCY_STRIP_RE = re.compile(r"[\s$£€¥]")

_TAXID_STRIP = __import__("re").compile(r"[^A-Za-z0-9]")

def canon_tax_id(value: Any) -> Optional[str]:
    """Canonical tax/VAT id: strip punctuation and case.
    'DE 123 456 789', 'DE-123456789' and 'de123456789' are the same
    registration — a vendor writing it differently is not a new tax id, and a
    tax id that only LOOKS different would manufacture a false conflict."""
    if value is None:
        return None
    s = _TAXID_STRIP.sub("", str(value)).upper()
    return s or None


def canon_recipient(value: Any) -> Optional[str]:
    """Canonical bill-to entity. Same normalisation as the vendor: an invoice
    addressed to "Acme Retail Corp" and one to "Acme Retail Corporation" are the
    same customer, but "Acme Retail Corp" and "Acme Research Foundation" are
    NOT — and that difference is what separates a duplicate from two documents
    wearing one invoice number."""
    return canon_vendor(value)


def canon_amount(value: Any) -> Optional[float]:
    """Numeric amount, currency stripped. Handles BOTH conventions:
      US/UK:    1,850.00   (comma = thousands, period = decimal)
      European: 1.850,00   (period = thousands, comma = decimal)
    We decide by which separator appears LAST: the last-occurring of ',' or '.'
    is the decimal separator; the other is the thousands separator and is
    removed. A lone separator with 3 trailing digits is treated as thousands.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = _CURRENCY_STRIP_RE.sub("", str(value).strip())
    s = _ISO_RE.sub("", s).strip()
    if s == "":
        return None

    has_comma = "," in s
    has_dot   = "." in s
    if has_comma and has_dot:
        # the separator that appears LAST is the decimal point
        if s.rfind(",") > s.rfind("."):
            # European: '.' thousands, ',' decimal
            s = s.replace(".", "").replace(",", ".")
        else:
            # US: ',' thousands, '.' decimal
            s = s.replace(",", "")
    elif has_comma:
        # only commas: decimal if exactly 2 digits follow the last comma AND it's
        # the only comma (e.g. '1850,00'); otherwise thousands ('1,850')
        frac = s.split(",")[-1]
        if s.count(",") == 1 and len(frac) == 2:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    # only dots or no separator: leave as-is (standard float form)
    try:
        return float(s)
    except ValueError:
        return None

def canon_currency(*values: Any) -> Optional[str]:
    """Infer ISO currency from any of the given values (an explicit currency
    field, or the amount string's symbol/code). First hit wins."""
    for value in values:
        if value is None:
            continue
        s = str(value).strip()
        m = _ISO_RE.search(s)
        if m:
            return m.group(1).upper()
        for sym, iso in _SYMBOL_TO_ISO.items():
            if sym in s:
                return iso
    return None


# ── date ───────────────────────────────────────────────────────────────────
#
# "01/02/2026", "2026-02-01", "July 10, 2026" → "2026-02-01" (ISO).
# Tries a set of explicit formats; ambiguous DD/MM vs MM/DD is resolved by a
# configurable dayfirst hint (default False = US MM/DD, the common invoice case)
# but flags ambiguity so the caller isn't misled. Returns None if unparseable —
# never guesses a date it can't parse.

_DATE_FORMATS = [
    "%Y-%m-%d", "%Y/%m/%d",
    "%d %B %Y", "%B %d, %Y", "%B %d %Y", "%d %b %Y", "%b %d, %Y",
    "%d-%b-%Y", "%d/%b/%Y",
]

_MONTH_TRANSLATIONS = {
    # German
    "januar":"January","februar":"February","märz":"March","maerz":"March",
    "april":"April","mai":"May","juni":"June","juli":"July","august":"August",
    "september":"September","oktober":"October","november":"November","dezember":"December",
    # French
    "janvier":"January","février":"February","fevrier":"February","mars":"March",
    "avril":"April","juin":"June","juillet":"July","août":"August","aout":"August",
    "septembre":"September","octobre":"October","novembre":"November","décembre":"December","decembre":"December",
}

def _translate_months(s):
    low = s.lower()
    for foreign, eng in _MONTH_TRANSLATIONS.items():
        if foreign in low:
            # replace preserving surrounding text, case-insensitively
            import re as _r
            s = _r.sub(foreign, eng, s, flags=_r.IGNORECASE)
            low = s.lower()
    return s


def canon_date(value: Any, dayfirst: bool = False) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # normalize non-English month names and German ordinal day dots ("14." -> "14")
    s = _translate_months(s)
    import re as _r2
    s = _r2.sub(r"(\d)\.(\s)", r"\1\2", s)
    # explicit unambiguous formats first
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    # numeric slash/dash separated: DD?/MM?/YYYY — order per dayfirst hint
    m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})$", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        day, month = (a, b) if dayfirst else (b, a)
        # if the "month" is impossible but the other field is a valid month,
        # swap (tolerate obvious mis-hinted inputs like 25/12/2026 under US hint)
        if month > 12 and day <= 12:
            day, month = month, day
        try:
            return datetime(y, month, day).date().isoformat()
        except ValueError:
            return None
    return None


# ── whole-record canonicalization ──────────────────────────────────────────

def canonicalize_invoice(fields: Dict[str, Any], dayfirst: bool = False) -> Dict[str, Any]:
    """
    Produce the canonical identity view of an invoice's resolved fields. Input is
    a Document View's `fields` dict; output is the canonical fields the identity
    keys and evidence engine consume. Missing inputs yield None outputs (never
    fabricated) — fail-closed, consistent with the rest of the engine.
    """
    amount = canon_amount(fields.get("total") if fields.get("total") is not None
                          else fields.get("amount"))
    amount_derived = False
    # If no stated total/amount, recover it by summing line items — the same
    # arithmetic the reconcile/process modes do. Flagged as derived so the
    # evidence trail shows the amount was computed, not stated.
    if amount is None:
        li_sum = _sum_line_items(fields.get("_line_items"))
        if li_sum is not None:
            amount = li_sum
            amount_derived = True
    return {
        "invoice_number_canon": canon_invoice_number(fields.get("invoice_number")),
        "recipient_canon":      canon_recipient(fields.get("recipient")),
        "tax_id_canon":         canon_tax_id(fields.get("tax_id")),
        "subtotal_canon":       canon_amount(fields.get("subtotal")),
        "tax_canon":            canon_amount(fields.get("tax")),
        "po_number_canon":      canon_invoice_number(fields.get("po_number")),
        "vendor_canon":         canon_vendor(fields.get("vendor")),
        "amount_canon":         amount,
        "amount_derived":       amount_derived,
        "currency_canon":       canon_currency(fields.get("currency"),
                                               fields.get("total"),
                                               fields.get("amount")),
        "date_canon":           canon_date(fields.get("invoice_date")
                                          or fields.get("date"), dayfirst=dayfirst),
    }


def _sum_line_items(line_items: Any) -> Optional[float]:
    """Sum the amount column of line items to recover a missing invoice total.
    Returns None if there are no line items or none has a parseable amount."""
    if not line_items or not isinstance(line_items, list):
        return None
    total = 0.0
    found = False
    for li in line_items:
        if not isinstance(li, dict):
            return None
        amt = canon_amount(li.get("amount"))
        if amt is None:
            # a line with no amount means we can't trust the sum — fail closed
            return None
        total += amt
        found = True
    return round(total, 2) if found else None
