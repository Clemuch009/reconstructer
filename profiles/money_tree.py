"""Money Structure Tree — recover the financial hierarchy of a document and
validate every money region locally, instead of searching for one total.

WHY THIS EXISTS
---------------
The previous verifier collected every money value on the page and asked "which
subset sums to the total?" — the subset-sum problem, which is exponential and,
worse, *ambiguous* on any real invoice with more than one subtotal. A document
with section subtotals rolling into a grand subtotal has two valid identities
(sections→grand, grand+tax→total) and the flat search cannot tell which layer
is which. It degraded to UNVERIFIABLE on exactly the multi-section invoices real
businesses send.

Humans don't solve invoices by subset search. They read the STRUCTURE first:
a subtotal closes the block above it; a section total sums its lines; the grand
total sums the section totals plus tax and adjustments. Arithmetic is local to
each region, not global over the page.

THE MODEL
---------
Every document — flat or multi-section — is ONE tree. A flat invoice is a tree
with a single region. There is no separate "flat path" to drift out of sync;
the same engine handles one section or fifty.

    Invoice
    ├── Region: line items ──────────► summarised by a Subtotal
    ├── Region: line items ──────────► summarised by a Subtotal   (repeatable)
    └── Chain:
          Subtotal  − Discount  + Shipping  = Taxable Subtotal
          Taxable Subtotal      + Tax        = Total

Each region is validated on its own and REPORTED on its own: "Consulting
section ✓, Software section ✓, tax chain ✓". The verdict is per-region, because
"do the math on every money area, not just the total" is the actual requirement.

WHAT IT SURVIVES (measured on a real invoice, not a synthetic one)
------------------------------------------------------------------
Real extraction shreds text. This parser is built against a real file whose text
came out as:

    'Subtotal — I'  /  'mplementatio'  /  'n & Support $13,200.00'   (label split 3 ways)
    'Payment is due within 30 Discoun'  /  '($2,509.50)'            (prose glued to a label)
    'Taxable Subtotal'  /  '$47,680.50'                             (label and value on separate lines)

so the parser joins split labels, rejects prose sentences as labels, and reads
parenthesised negatives as negatives.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from relationship_engine.canonicalize import canon_amount

_TOL = 0.02   # rounding tolerance for a money comparison


# ── money detection ──────────────────────────────────────────────────────────
#
# A currency-marked amount, optionally parenthesised (accounting negative) or
# sign-prefixed. Bare integers are NOT money here — a quantity ("40 hrs") or a
# percentage ("16%") is not a monetary value, and treating it as one is how the
# old code manufactured figures. Money on a real invoice carries a symbol/code.
_MONEY_RE = re.compile(
    r"""
    \(?\s*[-+]?\s*                          # optional open-paren / sign
    [£$€¥]\s*[\d][\d,]*(?:\.\d+)?\s*\)?    # LEADING symbol: $1,850.00
    |
    [-+]?[\d]{1,3}(?:[.,]\d{3})*[.,]\d{2}\s*[£$€¥]   # TRAILING symbol, EU or US: 1.850,00 €
    |
    [-+]?[\d][\d,]*(?:\.\d+)?\s*(?:USD|EUR|GBP|CHF|JPY|CAD|AUD|KES)\b  # ISO code
    """,
    re.X | re.I,
)


def _money_in(line: str) -> Optional[float]:
    """The monetary value in a line, or None. Parenthesised ⇒ negative.

    A line item row carries TWO money values — unit price and extended amount
    ("40 hrs $185.00 $7,400.00"). The value that contributes to a subtotal is
    the AMOUNT, which by invoice convention is the LAST money token on the row.
    Taking the first would sum unit prices and every section subtotal would be
    wrong. Summary lines ("Subtotal $12,650.00") have a single token, so "last"
    is correct for them too.
    """
    matches = list(_MONEY_RE.finditer(line))
    if not matches:
        return None
    tok = matches[-1].group(0)          # the amount column, not the unit price
    neg = "(" in tok
    val = canon_amount(tok.replace("(", "").replace(")", ""))
    if val is None:
        return None
    return -val if neg else val


def _strip_money(line: str) -> str:
    """The line with its money token removed — what's left is a candidate label."""
    return _MONEY_RE.sub("", line).strip()


# ── label hygiene ────────────────────────────────────────────────────────────
#
# A label names a money value ("Subtotal", "VAT (16%)", "Total Due"). A prose
# sentence ("Payment is due within 30 days...") is NOT a label, and on the real
# file a prose sentence was interleaved between the Discount and Shipping lines,
# corrupting naive label accumulation. Reject prose so it can't poison a label.
_PROSE_RE = re.compile(
    r"\b(is|are|was|were|due|please|thank|reference|subject|within|days?|"
    r"payment[s]?|late|charge|partnership|note[s]?|signature|generated)\b",
    re.I,
)


# Money-summary words that mark a real label even when prose words are also
# present. "Total Corporate Charge" contains "charge" (a prose word) but IS a
# label; the summary word "total" must win over the prose signal.
_LABEL_ANCHOR_RE = re.compile(
    r"\b(sub\s?total|total|amount|balance|vat|tax|discount|shipping|"
    r"handling|freight|net|due|gross|taxable|charge)\b", re.I)


def _looks_like_prose(text: str) -> bool:
    """True for sentence-like text that should never be treated as a label.

    A label carrying a money-summary anchor ("Total Corporate Charge") is never
    prose, even when it also contains a word from the prose list — the anchor
    wins. Without this, a legitimate three-word total label sharing a word with
    a sentence ("charge") was discarded and its value lost its role.
    """
    if not text:
        return True
    words = text.split()
    if len(words) > 6:                       # too long to be a label
        return True
    # a money anchor makes it a label regardless of prose words — but only if
    # it is still short (a full sentence mentioning "total" is still prose)
    if _LABEL_ANCHOR_RE.search(text) and len(words) <= 5:
        return False
    if _PROSE_RE.search(text) and len(words) >= 3:
        return True
    if text.endswith(".") and len(words) >= 3:
        return True
    return False


# label fragments that mark a SUMMARY value (closes/rolls up a region), vs an
# ordinary line item. Matched against the joined label, case-insensitively.
_SUMMARY_WORDS = ("subtotal", "sub total", "total", "grand total", "amount due",
                  "total due", "balance", "net due")
_TAXABLE_WORDS = ("taxable",)
_TAX_WORDS     = ("tax", "vat", "gst", "sales tax")
# short international tax abbreviations — matched as WHOLE WORDS only, since
# "iva"/"tva" otherwise match inside "activation"/"cultivation" and mis-tag a
# line item as tax.
_TAX_WORDS_WB  = ("mwst", "ust", "tva", "iva", "igv")
_DISCOUNT_WORDS = ("discount", "rebate", "less", "rabatt", "skonto", "remise")
_SHIPPING_WORDS = ("shipping", "handling", "freight", "carriage", "postage",
                   "delivery", "versand")

# Localized GRAND-TOTAL terms — a total whose label is in another language must
# still classify as the total. German "Gesamtbetrag", French "Total à payer",
# etc. Conservative list of well-known ones; extend as real invoices demand.
_TOTAL_WORDS_INTL = ("gesamtbetrag", "gesamtsumme", "rechnungsbetrag",
                     "total à payer", "montant total", "importe total",
                     "totale", "endbetrag")
_SUBTOTAL_WORDS_INTL = ("zwischensumme", "sous-total", "subtotale")


_ADJUSTMENT_WORDS = ("adjustment", "credit", "rebate", "discount", "less",
                     "deduction", "allowance", "offset", "reduction", "waiver",
                     "concession")


def _looks_like_adjustment(label: str) -> bool:
    """A label that plausibly names a deduction from the total. Requires an
    adjustment word AND excludes date/prose lines, so a stray negative parsed off
    a date or note is not mis-rolled as a discount."""
    L = (label or "").lower().strip()
    if not L:
        return False
    if not any(w in L for w in _ADJUSTMENT_WORDS):
        return False
    # reject if it looks like a date line (month name or dd/mm/yyyy present)
    import re as _re
    if _re.search(r"\b\d{1,2}(st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|"
                  r"aug|sep|oct|nov|dec)", L) or _re.search(r"\d{4}", L):
        # a year/date in the label — only accept if the adjustment word is
        # clearly the subject, not incidental
        return False
    return True


def _classify(label: str) -> str:
    """Map a label to a money ROLE. Order matters: taxable-subtotal before
    subtotal, total-due before total, so the more specific role wins."""
    L = label.lower()
    if any(w in L for w in _TAXABLE_WORDS):
        return "taxable_subtotal"
    if any(re.search(r"\b" + re.escape(w) + r"\b", L)
           for w in (_TAX_WORDS + _TAX_WORDS_WB)):
        return "tax"
    if any(re.search(r"\b" + re.escape(w) + r"\b", L) for w in _DISCOUNT_WORDS):
        return "discount"
    if any(re.search(r"\b" + re.escape(w) + r"\b", L) for w in _SHIPPING_WORDS):
        return "shipping"
    # "total due" / "grand total" / "amount due" → the final total
    if any(w in L for w in _SUBTOTAL_WORDS_INTL):
        return "subtotal"
    if any(w in L for w in _TOTAL_WORDS_INTL):
        return "total"
    if ("total due" in L or "grand total" in L or "amount due" in L
            or "net due" in L or "balance due" in L
            or "balance payable" in L or "amount payable" in L
            or "total payable" in L or "please pay" in L
            or "outstanding balance" in L or "net outstanding" in L
            or "outstanding amount" in L or "balance outstanding" in L):
        return "total"
    if "subtotal" in L or "sub total" in L:
        return "subtotal"
    if "total" in L:
        return "total"
    return "line"


# ── the money node ───────────────────────────────────────────────────────────
class MoneyNode:
    __slots__ = ("label", "value", "role")

    def __init__(self, label: str, value: float, role: str):
        self.label = label
        self.value = value
        self.role = role

    def __repr__(self):
        return f"<{self.role} {self.value:,.2f} {self.label!r}>"


_HEADER_COLS = ("description", "quantity", "qty", "unit price", "unit", "amount",
                "item", "rate", "service")

_HEADER_WORDS = {"description", "qty", "quantity", "unit", "price", "amount",
                 "total", "rate", "item", "service", "hours", "hrs", "line",
                 "campaign", "product", "model", "tax"}


_CURRENCY_QUALIFIER_RE = re.compile(
    r"^\(?\s*(?:in\s+)?(?:USD|EUR|GBP|CHF|JPY|CAD|AUD|KES|[£$€¥])\s*\)?$", re.I)


def _is_currency_qualifier(text: str) -> bool:
    """True for a bare currency marker like "(USD)" or "EUR" that sits between a
    label and its value and must not be mistaken for the label itself."""
    return bool(_CURRENCY_QUALIFIER_RE.match(text.strip()))


def _is_column_header_row(line: str) -> bool:
    """True when a line is a table COLUMN-HEADER row ("Service Description Total",
    "Description Qty Amount") rather than data. Such a row carries no money and
    is almost entirely column words. If it is mistaken for a label, the word
    "Total" or "Amount" in it poisons the NEXT line's money value, tagging a real
    line item as a total. Measured across the corpus this catches every header
    row and no real label or money line."""
    words = [w.strip(".,:#/") for w in line.split()]
    words = [w for w in words if w]
    if len(words) < 2:
        return False
    known = sum(1 for w in words if w.lower() in _HEADER_WORDS)
    return known >= 2 and known / len(words) >= 0.6


def _is_table_header(label: str) -> bool:
    """True when a label is a column-header row rather than a money label.

    Extraction sometimes attaches a stray value to a header line
    ("Description, Quantity, Unit Price, Total  5,700.00"); the word "Total"
    there is a COLUMN NAME, not a summary, and the value is spurious. A header
    is recognised by carrying two or more of the standard column words.
    """
    L = label.lower()
    # Require the COMMA-flattened header signature, not just column words. A
    # real label like "Service Amount" or "Unit Price Amount" legitimately
    # carries a column word; a genuine header row was CSV-flattened and reads
    # "Description,Quantity,Unit Price,Total". Demand 3+ column words AND commas,
    # so real two-word labels are never mistaken for headers.
    if "," not in L:
        return False
    hits = sum(1 for w in _HEADER_COLS if w in L)
    return hits >= 3


def _is_split_continuation(prev: str, cur: str) -> bool:
    """True when `cur` is the tail of a word split off `prev` by extraction,
    e.g. "Subtotal — I" + "mplementatio". Heuristic: prev ends with a single
    letter or a hyphen/dash, and cur begins lowercase — a mid-word break."""
    if not prev or not cur:
        return False
    if prev.rstrip().endswith(("-", "—", "–")):
        return True
    last = prev.rstrip()[-1:]
    return last.isalpha() and last.isupper() and cur[:1].islower()


def _role_hint_from_prose(text: str) -> Optional[str]:
    """If prose has a role word (even truncated) glued to its end, return that
    role. "Payment is due within 30 Discoun" → "discount"."""
    L = text.lower()
    for stem, role in (("discoun", "discount"), ("rebate", "discount"),
                       ("shippin", "shipping"), ("handlin", "shipping"),
                       ("freight", "shipping"), ("taxable", "taxable_subtotal"),
                       ("subtotal", "subtotal")):
        if stem in L:
            return role
    return None


# A role keyword STARTS a genuine split label ("Subtotal —" / "Grand" / "Value
# Added"). These stem-initial forms are what a broken section-summary label looks
# like. A product description that merely CONTAINS the word ("Subtotal
# Reconciliation Service"), or prose that mentions it mid-sentence, does not —
# there the keyword is embedded, not leading. This is the signal that separates a
# real split label from a false hit.
_ROLE_STEMS = ("subtotal", "sub total", "total", "grand total", "amount due",
               "total due", "balance", "tax", "vat", "gst", "discount",
               "rebate", "taxable", "shipping", "freight", "gesamt", "importe",
               "montant", "zwischensumme", "sous-total")


_QTY_UNIT_ROW = re.compile(
    r'\b\d+(?:\.\d+)?\s*(hrs?|days?|qtr|lic|seat|trip|nights?|flat|ea|month|'
    r'units?|pcs?|items?|hours?)\b', re.IGNORECASE)


# quantity followed (after some text) by TWO money values — the structural
# signature of a priced row: qty ... unit_price ... amount. Unit-word-agnostic.
_QTY_TWO_MONEY = re.compile(
    r'\b\d+(?:\.\d+)?\b.*?[\$€£]?\s?[\d,]+\.\d{2}\D+[\$€£]?\s?[\d,]+\.\d{2}')


def _is_category_header(text: str) -> bool:
    """True when a non-money line is a section/category header rather than a
    description fragment. Heuristic: predominantly upper-case section titles
    ("DATA INFRASTRUCTURE & HOSTING", "SOFTWARE LICENSING") — mixed-case wrapped
    descriptions ("Enterprise Platform License") are NOT headers and must still
    join their line item."""
    t = text.strip()
    if not t:
        return False
    letters = [c for c in t if c.isalpha()]
    if not letters:
        return False
    upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    # a header is mostly uppercase and has no lowercase run of a real word
    return upper_ratio >= 0.8


def _has_own_description(inline: str) -> bool:
    """True when a priced row's inline text (money already stripped) carries its
    own description — i.e. there are descriptive words BEFORE the quantity token.
    "Cloud Hosting Production Environment (Annual) 1 flat" has a description; a
    bare "1 flat" does not. Used to decide whether the row needs a preceding
    label fragment (a wrapped description) or is already complete (so a category
    header must not be prepended)."""
    m = _QTY_UNIT_ROW.search(inline)
    if not m:
        # two-money priced rows: check for words before the first number
        m2 = re.search(r'\d', inline)
        if not m2:
            return False
        return len(inline[:m2.start()].split()) >= 2
    # words before the quantity match
    return len(inline[:m.start()].split()) >= 2


def _is_priced_line_item(line: str) -> bool:
    """True when a line is a priced line item. Detected two ways, so it does not
    depend on a fixed unit vocabulary:
      1. a quantity+known-unit token ("1 flat", "8 hrs", "2 days"), OR
      2. a quantity followed by TWO money values on the line (unit price AND
         extended amount: "20 roll $55.00 $1,100.00", "100 ea $17.60
         $1,760.00").
    A summary row (subtotal/tax/total) has neither — it carries one value and no
    quantity — so this correctly separates line items from summaries regardless
    of any keyword in the description."""
    if _QTY_UNIT_ROW.search(line):
        return True
    if _QTY_TWO_MONEY.search(line):
        return True
    return False


def _is_split_label_buffer(pending: List[str]) -> bool:
    """True when the pending buffer looks like a section-summary label broken
    across lines, rather than a product description or prose that merely mentions
    a role word. Requirements:
      - the FIRST fragment must begin with a role stem (a real split label leads
        with "Subtotal —", "Grand", "Value Added", etc.), and
      - the fragments are short label pieces, not a full priced line or a
        sentence (no fragment is long prose).
    """
    if not pending:
        return False
    first = pending[0].strip().lower()
    # first fragment must START with a role stem (leading, not embedded)
    if not any(first.startswith(s) for s in _ROLE_STEMS):
        return False
    # reject if any fragment is long prose (a real label piece is short)
    for frag in pending:
        words = frag.split()
        if len(words) > 4:            # label fragments are terse
            return False
    return True


def parse_money_nodes(lines: List[str]) -> List[MoneyNode]:
    """Walk the document in reading order and emit (label, value, role) money
    nodes, surviving split labels and interleaved prose.

    Accumulate non-money text as a pending label; on a money line, join the
    pending fragments with any inline label and emit. Prose fragments are
    dropped from the pending buffer so they can't corrupt the next label.
    """
    nodes: List[MoneyNode] = []
    pending: List[str] = []
    role_hint: Optional[str] = None       # salvaged from prose glued to a label
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("["):        # skip structural markers
            continue
        val = _money_in(line)
        inline = _strip_money(line)
        if val is not None:
            # BOUNDED label: this line's own leftover text, plus at most the
            # ONE immediately-preceding non-money fragment. Greedily joining
            # everything since the last value swallowed real line items — a
            # header ("...Total"), a bill-to block and item #1's amount all
            # fused into one mislabelled node, and the item vanished from its
            # section sum. A money value's label lives next to it, not paragraphs
            # away. Split labels ("Subtotal — I / mplementatio / n & Support")
            # still join because each fragment is the immediate predecessor of
            # the next, chained up to the value.
            parts: List[str] = []
            _priced = _is_priced_line_item(line)
            # A standalone CATEGORY HEADER (all-caps section title like "DATA
            # INFRASTRUCTURE & HOSTING") sitting in `pending` must NOT be glued
            # onto the first item's description. But a mixed-case wrapped
            # description fragment ("Enterprise Platform License") SHOULD still
            # join. Distinguish by header shape, and only when the current row
            # already carries its own description (so we never strip a needed
            # continuation from a bare "1 lic" row).
            skip_pending = (pending
                            and _priced and _has_own_description(inline)
                            and _is_category_header(pending[-1]))
            if pending and not skip_pending:
                parts.append(pending[-1])           # nearest preceding fragment
            # a priced line item's inline text is its description — always a
            # valid label, regardless of length or em-dash punctuation.
            if inline and (_priced or not _looks_like_prose(inline)):
                parts.append(inline)
            label = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
            label = label.replace(" — ", " ").replace("  ", " ")
            role = _classify(label)
            # SPLIT-LABEL ACROSS MANY LINES: a section subtotal whose label is
            # broken over more lines than the one-fragment window (common right
            # at a page break: "Subtotal —" / "Data" / "Infrastructure" /
            # "& Hosting $20,100") would classify as a line item, because the
            # "Subtotal" keyword sits earlier in `pending` than pending[-1]. If
            # the joined fragments of the CURRENT pending buffer name a role,
            # recover it — this only fires when the immediate label did not
            # already carry the role, so it cannot mis-tag a real line item.
            if role == "line" and pending and _is_split_label_buffer(pending):
                joined_pending = " ".join(pending)
                # the fragments must form ONE contiguous split phrase — no money
                # value has intervened (pending is cleared on every emit), so a
                # role keyword anywhere in the live buffer belongs to THIS value.
                # Guarded by _is_split_label_buffer so a product NAME containing
                # "Subtotal"/"Total", or prose mentioning it, does not hijack a
                # real line item's role.
                recovered = _classify(joined_pending)
                if recovered != "line":
                    role = recovered
                    label = re.sub(r"\s+", " ",
                                   (joined_pending + " " + (inline or "")).strip())
                    label = label.replace(" — ", " ").replace("  ", " ")
            if role == "line" and role_hint:
                role = role_hint
                if not label:
                    label = role_hint
            # A NEGATIVE value under an ADJUSTMENT-like label is a deduction from
            # the total (adjustment, credit, rebate) — re-role as discount so the
            # chain folds it in rather than dropping a real money area. Guarded
            # tightly: only when the label actually reads like an adjustment, so
            # a stray negative on a date or prose line ("Qrynt 17th July 2026")
            # is NOT mistaken for a discount. A negative under an explicit
            # discount/shipping label is already handled by _classify above.
            if role == "line" and val < 0 and _looks_like_adjustment(label):
                role = "discount"
            # A priced row (quantity + unit present) is a line item regardless
            # of any summary keyword in its description — a subtotal has no qty.
            if role != "line" and _is_priced_line_item(line):
                role = "line"
            if not _is_table_header(label):
                nodes.append(MoneyNode(label, val, role))
            pending = []
            role_hint = None
        else:
            if _is_currency_qualifier(inline):
                # a bare "(USD)" between a label and its value — skip it, keep
                # the real label already in `pending`
                continue
            if _is_column_header_row(inline):
                # a column-header row is not a label — drop it so it can't
                # become the next money value's label and mis-tag it as a total
                pending = []
            elif not _looks_like_prose(inline):
                # keep only the most recent fragment as the live label candidate;
                # chained split-labels still work because each line appends and
                # the value reads pending[-1]. But we must preserve a growing
                # split label: if the previous pending item looks like a label
                # STEM (ends mid-word or with a connector), concatenate.
                if pending and _is_split_continuation(pending[-1], inline):
                    pending[-1] = (pending[-1] + inline).strip()
                else:
                    pending.append(inline)
            else:
                role_hint = _role_hint_from_prose(inline)
                pending = []
    return nodes


# ── region & chain validation ────────────────────────────────────────────────
#
# Two kinds of money area get validated, each locally and each reported on its
# own — never a single global verdict:
#
#   1. SECTION regions: the line items between one subtotal and the previous
#      boundary must sum to that subtotal.
#   2. The CHAIN: the invoice-level roll-up
#         subtotal (− discount + shipping) = taxable_subtotal
#         taxable_subtotal (+ tax)          = total
#      with each step checked only if its inputs are present, so a flat invoice
#      (subtotal + tax = total, no sections) is just the degenerate one-region
#      case of the same walk.


def _positive_lines(nodes) -> List[float]:
    """Positive line-item values that feed a sum. Excludes negatives (payments/
    credits) and empty-label lines appearing after a summary (trailing total
    echoes whose label was lost in extraction)."""
    out = []
    seen_chain = False
    for n in nodes:
        if n.role in ("tax", "taxable_subtotal", "total", "discount", "shipping"):
            seen_chain = True
            continue
        if n.role == "subtotal":
            continue
        if n.role == "line" and n.value > 0 and not (seen_chain and not n.label.strip()):
            out.append(n.value)
    return out


def _check(name: str, got: float, want: float) -> Dict[str, Any]:
    ok = abs(got - want) < _TOL
    return {"region": name, "ok": ok, "expected": round(want, 2),
            "stated": round(got, 2),
            "delta": round(got - want, 2)}


def validate_money_tree(lines: List[str]) -> Dict[str, Any]:
    """Parse the document into money regions and validate each one.

    Returns {regions: [...], total, subtotal, tax, verdict, summary} where
    `regions` is one entry PER money area with its own pass/fail, and `verdict`
    is the roll-up: CONFIRM if every region balances, CONFLICT if any region's
    arithmetic is wrong, UNVERIFIABLE if the structure could not be recovered.
    """
    nodes = parse_money_nodes(lines)
    regions: List[Dict[str, Any]] = []

    # A section-sum check only makes sense on a genuine MULTI-section document
    # (two or more subtotals). On a flat invoice a lone subtotal's "section" is
    # polluted by stray values — a payment line, a credit, a repeated figure —
    # and the chain check (subtotal + tax = total) is the reliable authority
    # there. So: validate section sums only when multi-section; always validate
    # the chain.
    n_subtotals = sum(1 for n in nodes if n.role == "subtotal")
    multi_section = n_subtotals >= 2

    # 1) SECTION regions — sum of line items up to each subtotal boundary.
    run: List[float] = []
    section_subtotals: List[float] = []
    seen_chain = False
    for n in nodes:
        if n.role == "line":
            # Only POSITIVE line items feed a section subtotal. A negative value
            # tagged "line" is a payment/credit entry, which belongs to the
            # payment chain, not the section sum.
            # And an EMPTY-label line appearing AFTER a subtotal/tax is a
            # trailing total echo whose label was lost in extraction — a real
            # line item sits before the summary and carries a description.
            if n.value > 0 and not (seen_chain and not n.label.strip()):
                run.append(n.value)
        elif n.role == "subtotal":
            # Only validate a SECTION sum when there are at least two line items
            # feeding it. A real section has multiple lines; a single stray
            # value sitting before a subtotal is usually a misclassified summary
            # figure (a tax or a header amount), and checking it manufactures a
            # false CONFLICT. When we don't validate, the subtotal still counts
            # toward the grand-subtotal and chain checks below — we simply don't
            # assert a section identity we can't trust.
            if len(run) >= 2:
                regions.append(_check(f"section: {n.label[:32] or 'items'}",
                                      n.value, sum(run)))
            section_subtotals.append(n.value)
            run = []
        else:
            # tax/total/taxable are the CHAIN — an empty-label line after these
            # is a trailing echo. A subtotal is only a section boundary, so it
            # does NOT set this (later sections still have real line items).
            seen_chain = True

    # 2) CHAIN — build a dict of the last value seen per chain role.
    by_role: Dict[str, float] = {}
    for n in nodes:
        if n.role in ("subtotal", "discount", "shipping", "taxable_subtotal",
                      "tax", "total"):
            by_role[n.role] = n.value        # last wins (grand subtotal, final total)

    # grand subtotal = Σ section subtotals (when there is more than one section
    # AND we actually saw them all — otherwise skip rather than assert a partial)
    if len(section_subtotals) >= 2 and "subtotal" in by_role:
        summed = sum(section_subtotals[:-1]) if section_subtotals[-1] == by_role["subtotal"] \
            else sum(section_subtotals)
        # only assert if the sum is plausibly the grand subtotal
        if abs(summed - by_role["subtotal"]) < _TOL:
            regions.append(_check("grand subtotal = Σ sections",
                                  by_role["subtotal"], summed))

    # subtotal − discount + shipping = taxable_subtotal
    # A discount REDUCES the base and shipping INCREASES it, by role intent —
    # regardless of whether the document wrote the discount as "-$333" or "$333"
    # (minus implied by the label). Use magnitudes so the stored sign cannot
    # invert the formula (the bug: a positive-written discount was ADDED,
    # expecting 6,993 instead of 6,327).
    if "taxable_subtotal" in by_role and "subtotal" in by_role:
        base = (by_role["subtotal"]
                - abs(by_role.get("discount", 0.0))
                + abs(by_role.get("shipping", 0.0)))
        regions.append(_check("subtotal − discount + shipping = taxable",
                              by_role["taxable_subtotal"], base))

    # base + tax = total, where base folds in discount/shipping if there is no
    # explicit taxable-subtotal line. When a taxable_subtotal IS present the
    # discount/shipping were already applied to reach it (checked above), so we
    # start from it directly; otherwise we apply them here so the discount is
    # never silently dropped from the roll-up to total.
    if "total" in by_role:
        if "taxable_subtotal" in by_role:
            base_for_tax = by_role["taxable_subtotal"]
        elif "subtotal" in by_role:
            base_for_tax = (by_role["subtotal"]
                            - abs(by_role.get("discount", 0.0))
                            + abs(by_role.get("shipping", 0.0)))
        else:
            base_for_tax = None
        if base_for_tax is not None and "tax" in by_role:
            # Tax is normally ADDED to the base to reach the total. But some
            # invoices state tax INCLUSIVELY — the total already contains it, and
            # the tax line is a breakdown ("Total $1,000, incl. VAT $137.93").
            # There the total equals the base, not base+tax. Both are valid, and
            # the arithmetic itself tells them apart: accept whichever the
            # document's own numbers support, so an inclusive-tax invoice is not
            # falsely flagged. A genuine error (total matching NEITHER) still
            # fails.
            additive = base_for_tax + by_role["tax"]
            inclusive = base_for_tax
            stated = by_role["total"]
            if abs(stated - inclusive) < _TOL and abs(stated - additive) >= _TOL:
                regions.append(_check("base = total (tax shown inclusive)",
                                      stated, inclusive))
            else:
                regions.append(_check("base − discount + shipping + tax = total",
                                      stated, additive))
        elif base_for_tax is not None and "tax" not in by_role:
            regions.append(_check("base − discount + shipping = total",
                                  by_role["total"], base_for_tax))
        elif base_for_tax is None:
            # No subtotal at all — a flat invoice of line items and a total.
            #   Σ line items (+ tax if present) = total
            line_sum = sum(v for v in _positive_lines(nodes))
            if line_sum > 0:
                target = line_sum + by_role.get("tax", 0.0)
                regions.append(_check("Σ line items (+ tax) = total",
                                      by_role["total"], target))

    # subtotal present but NO total line, AND a single section — validate
    # Σ lines = subtotal so the document still gets an arithmetic check instead
    # of falling to UNVERIFIABLE. Skipped when multi-section (each section sum is
    # already checked, and summing all lines against the grand subtotal would
    # double-count) and when a total exists (the chain above covers it).
    if ("total" not in by_role and "subtotal" in by_role
            and n_subtotals == 1 and not any(r["region"].startswith("section")
                                             for r in regions)):
        line_sum = sum(v for v in _positive_lines(nodes))
        if line_sum > 0:
            regions.append(_check("Σ line items = subtotal",
                                  by_role["subtotal"], line_sum))

    # ── derive a total when the document states none ─────────────────────
    # The math is present even when the label is not. If line items exist but no
    # total was stated, Qrynt computes it rather than reporting "no total" — the
    # engine's job is to do the arithmetic, not only to check someone else's.
    # The derived figure is flagged so it is never confused with a stated one.
    derived_total = None
    if "total" not in by_role:
        line_sum = sum(_positive_lines(nodes))
        if "subtotal" in by_role:
            # subtotal stated, total not: total = subtotal (+tax −disc +ship)
            base = (by_role["subtotal"]
                    - abs(by_role.get("discount", 0.0))
                    + abs(by_role.get("shipping", 0.0)))
            derived_total = round(base + by_role.get("tax", 0.0), 2)
        elif line_sum > 0:
            # only line items: total = Σ lines (+ tax if a tax line is present)
            derived_total = round(line_sum + by_role.get("tax", 0.0), 2)
        if derived_total is not None:
            regions.append({"region": "total computed by Qrynt (none stated)",
                            "ok": True, "expected": derived_total,
                            "stated": derived_total, "delta": 0.0,
                            "derived": True})

    # ── roll-up verdict ──────────────────────────────────────────────────
    checked = [r for r in regions]
    non_derived = [r for r in checked if not r.get("derived")]
    if not checked:
        verdict = "UNVERIFIABLE"
        summary = "no money regions could be recovered from the document"
    elif derived_total is not None:
        # Is the derived figure backed by a VALIDATED chain to the total, or only
        # by bare summation? A chain region whose name ends in "= total" that
        # balanced means every step (subtotal, discount, shipping, tax) was
        # present and checked — the chain is the cross-check, so this is
        # CONFIRM-grade. Bare summation with nothing confirming the final total
        # (a discount could be missing, as in vanguard's duplicate) is review.
        chain_to_total = [r for r in non_derived
                          if r["ok"] and r["region"].endswith("= total")]
        if chain_to_total:
            verdict = "CONFIRM"
            summary = f"all {len(non_derived)} money regions balance"
        else:
            verdict = "DERIVED"
            n_ok = sum(1 for r in non_derived if r["ok"])
            summary = (f"no total was stated; Qrynt computed "
                       f"{derived_total:,.2f} from the line items — review, as "
                       f"nothing in the document confirms it"
                       + (f" ({n_ok}/{len(non_derived)} other regions balance)"
                          if non_derived else ""))
    elif all(r["ok"] for r in checked):
        verdict = "CONFIRM"
        summary = f"all {len(checked)} money regions balance"
    else:
        verdict = "CONFLICT"
        bad = [r for r in checked if not r["ok"]]
        summary = (f"{len(bad)} of {len(checked)} money regions do not balance: "
                   + "; ".join(f"{r['region']} (stated {r['stated']:,.2f}, "
                               f"expected {r['expected']:,.2f})" for r in bad))

    stated_total = by_role.get("total")
    # PUBLISH the recovered structure. The tree parsed every money node — line
    # items, section subtotals, tax, total — and until now discarded all but the
    # roll-up. Expose them so downstream stages (coverage, reconciliation, the
    # report) read what was understood instead of re-deriving it from scratch.
    published_nodes = [
        {"label": n.label, "value": n.value, "role": n.role}
        for n in nodes
    ]
    published_line_items = [
        {"description": n.label, "amount": n.value}
        for n in nodes if n.role == "line" and n.value > 0
    ]
    return {
        "verdict": verdict,
        "summary": summary,
        "regions": regions,
        "nodes": published_nodes,
        "line_items": published_line_items,
        "total": stated_total if stated_total is not None else derived_total,
        "total_is_derived": stated_total is None and derived_total is not None,
        "total_note": ("This total was calculated by Qrynt; the document did not "
                       "state one." if stated_total is None and derived_total is not None
                       else None),
        "subtotal": by_role.get("subtotal"),
        "taxable_subtotal": by_role.get("taxable_subtotal"),
        "tax": by_role.get("tax"),
        "discount": by_role.get("discount"),
        "shipping": by_role.get("shipping"),
    }


# ── bridge to the resolver's verification slot ───────────────────────────────
#
# The resolver builds verification["total"] from verify_total(), whose shape is
# {verdict, claimed, identified, evidence, support}. This adapter runs the tree
# engine and returns THAT shape (so every existing consumer keeps working) with
# the per-region detail added under extra keys. The tree's answer takes
# precedence when it recovered a total; otherwise we fall back to whatever the
# old verifier concluded, so nothing a caller relied on regresses.

# map tree verdicts onto the verifier's vocabulary the resolver/UI already know
_VERDICT_MAP = {
    "CONFIRM":      "CONFIRM",
    "CONFLICT":     "CONFLICT",
    "DERIVED":      "FILL",          # a computed total the doc didn't state
    "UNVERIFIABLE": "UNVERIFIABLE",
}


def verify_total_tree(fields, lines, line_items=None):
    """Drop-in replacement for verify_total that uses the money-structure tree.

    Returns the verify_total shape plus:
      regions          — per-money-area pass/fail (the "math on every area")
      total_is_derived — True when Qrynt computed the total (none was stated)
      total_note       — the human note for a derived total
      tree_verdict     — the raw tree verdict (CONFIRM/CONFLICT/DERIVED/UNVERIFIABLE)
    """
    from relationship_engine.canonicalize import canon_amount

    tree = validate_money_tree(lines)
    claimed = canon_amount(fields.get("total")) if fields else None
    identified = tree.get("total")

    verdict = _VERDICT_MAP.get(tree["verdict"], "UNVERIFIABLE")

    # The tree's verdict is AUTHORITATIVE. It already validated every region,
    # including whether the stated total matches what the structure produces.
    # A CONFLICT means the document's own arithmetic does not balance (e.g.
    # inv90812 states 7,952 but subtotal+tax = 7,452 — a $500 over-billing), and
    # the bridge must NOT override that. An earlier version compared `claimed`
    # against the tree's REPORTED total — but the tree reports the STATED figure
    # as its total, so that comparison always agreed and silently reconfirmed the
    # very number that failed the arithmetic, losing the over-billing catch.
    #
    # We only add a comparison the tree could not make: when the tree confirmed
    # its own structure but a DIFFERENT stated total exists in fields that the
    # tree did not see, flag the disagreement.
    if (verdict == "CONFIRM" and claimed is not None and identified is not None
            and abs(claimed - identified) >= _TOL):
        verdict = "CONFLICT"

    support = [r["region"] for r in tree["regions"] if r["ok"]]

    return {
        "verdict":          verdict,
        "claimed":          claimed,
        "identified":       identified,
        "evidence":         tree["summary"],
        "support":          support,
        # richer, additive fields the UI can show but old consumers can ignore:
        "regions":          tree["regions"],
        "total_is_derived": tree.get("total_is_derived", False),
        "total_note":       tree.get("total_note"),
        "tree_verdict":     tree["verdict"],
        "subtotal":         tree.get("subtotal"),
        "tax":              tree.get("tax"),
        "discount":         tree.get("discount"),
        "shipping":         tree.get("shipping"),
        # the recovered structure, published for downstream stages
        "nodes":            tree.get("nodes", []),
        "line_items":       tree.get("line_items", []),
    }
