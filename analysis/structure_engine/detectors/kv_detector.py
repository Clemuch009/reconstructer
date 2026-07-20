import re
from typing import List, Optional, Dict, Any
from typing_extensions import TypedDict
from analysis.structure_engine.line_model import LineObject
from analysis.structure_engine.pattern_extractor import LinePattern


# ---------------------------------
# Contract
# ---------------------------------

class KVResult(TypedDict):
    line_index: int
    key: str
    value: str
    delimiter: str
    confidence: float
    patterns_used: List[str]
    nested: Optional[Dict[str, Any]]   # decomposed sub-pairs, or None


# ---------------------------------
# Allowed delimiters (locked)
# ---------------------------------

ALLOWED_DELIMITERS = [":", "="]

# Space-separated label prefixes common on invoices/POs where no colon is used
# ("Invoice No. INV-2026-0158", "Due Date 02 Aug 2026", "Payment Terms Net 14").
# Longest-first so "Invoice No." wins over "Invoice". Matched case-insensitively
# at the START of the line; the remainder is the value.
_SPACE_KV_PREFIXES = sorted([
    "Invoice Number", "Invoice No.", "Invoice No", "Invoice #", "Invoice ID",
    "Invoice Date", "Due Date", "Payment Terms", "Terms", "Currency",
    "PO Number", "PO No.", "PO No", "Purchase Order", "P.O. Number",
    "Order Number", "Order No.", "Reference", "Ref", "Ref.",
    "Account Manager", "Account Number", "Account Name", "Routing",
    "Bill To", "Ship To", "Sold To", "Vendor", "Supplier", "Customer",
    "Tax ID", "VAT No.", "VAT Number", "PIN", "GST No.",
    "Date", "Number", "No.",
], key=len, reverse=True)


# A KV VALUE looks like a value, not a sentence: it is short and typically
# carries an identifier, number, code, date, currency, or a small number of
# capitalized/technical tokens ("INV-2026-0158", "02 Aug 2026", "Net 14",
# "USD", "PO-90417"). Prose ("and conditions apply to all purchases") is none of
# these — long, lowercase, sentence-like — and must NOT be split.
_VALUE_LIKE_RE = re.compile(
    r"^("
    r"[A-Z0-9][A-Z0-9\-/#.]*"                 # code/ID: INV-2026-0158, PO-90417
    r"|\$?[\d,]+(?:\.\d+)?"                   # a number/amount
    r"|\d{1,2}[ /\-][A-Za-z0-9]+[ /\-]\d{2,4}" # a date: 02 Aug 2026, 02/08/2026
    r"|[A-Za-z]{3,}\s+\d"                      # "Net 14", "Aug 2026"
    r"|[A-Z]{2,4}"                              # currency/code: USD, GBP
    r")",
)


def _looks_like_value(value: str) -> bool:
    """True when the RHS of a space-KV split looks like a field value rather
    than prose. Guards against splitting sentences that merely begin with a
    label word ("Terms and conditions apply...")."""
    v = value.strip()
    if not v:
        return False
    # short values are almost always real; long ones must be value-shaped
    words = v.split()
    if len(words) <= 3 and _VALUE_LIKE_RE.match(v):
        return True
    if len(words) <= 5 and _VALUE_LIKE_RE.match(v) and any(ch.isdigit() for ch in v):
        return True
    # a value that is a short proper-noun phrase (all Titlecase, <=5 words) is a
    # plausible name value ("Harbor & Pine Design Studio" as Account Name)
    if len(words) <= 5 and all(w[0].isupper() or not w[0].isalpha() for w in words):
        return True
    return False


def _split_space_kv(normalized: str):
    """Split a space-separated 'Label Value' line when the label is a known
    invoice/PO field prefix AND the remainder looks like a value (not prose).
    Returns (key, value) or None. Precise by design: only recognised labels with
    value-like RHS split, so ordinary prose is never mis-split."""
    low = normalized.lower()
    for prefix in _SPACE_KV_PREFIXES:
        pl = prefix.lower()
        # must match at start, followed by a space and a non-empty value
        if low.startswith(pl + " "):
            value = normalized[len(prefix):].strip()
            if value and _looks_like_value(value):
                return prefix, value
    return None


# ---------------------------------
# Pre-validation rejection gates
# ---------------------------------

ISO_TIMESTAMP_RE  = re.compile(r"^\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}")
BRACKET_LOGGER_RE = re.compile(r"^\[?\d{4}-\d{2}-\d{2}")
STACK_TRACE_RE    = re.compile(r"^\s*(at\s+[\w\.\$]+\(|Caused\s+by:|\.{3}\s+\d+\s+more)")
METHOD_SIG_RE     = re.compile(r"\(?[\w\.\$]+\.java:\d+\)?")

def _is_invalid_kv_line(normalized: str) -> bool:
    if ISO_TIMESTAMP_RE.match(normalized):
        return True
    if BRACKET_LOGGER_RE.match(normalized):
        return True
    if STACK_TRACE_RE.match(normalized):
        return True
    if METHOD_SIG_RE.search(normalized):
        return True
    return False


# ---------------------------------
# Key validity gate
# ---------------------------------

# A KV key must be a plausible identifier: bounded length, identifier-like
# characters only (word chars, dots, dashes), and at least one word char.
# Allows realistic multi-word keys of up to THREE space-separated tokens
# ("First Name", "Date of Birth", "Prepared by") — these are common in forms
# and metadata and were previously misclassified as prose (and dropped from the
# human rendering). The <=3-token bound is what still rejects full sentences
# that merely precede a colon ("The results were surprising indeed:"), along
# with brackets, tree markers, and pure-symbol runs — i.e. tree lines
# ("+-- NODE [Key"), separators ("---"), section labels ("Config (nested
# madness)"), JSON fragments, and log prefixes. Genuinely ambiguous 2-word
# phrases ("For example:") are further disambiguated by the surrounding
# block-level kv-density / value context, not by this key pattern alone.
# A valid kv key: 1-3 words of identifier chars, optionally followed by a
# single trailing parenthetical unit/qualifier — "Total Due (USD)", "Tax (0%)",
# "Amount (net)" — which is a very common invoice/label pattern. The
# parenthetical is optional and must be at the end; this does not admit
# arbitrary prose (no internal punctuation runs, still <=3 words before it).
KEY_IDENTIFIER_RE = re.compile(
    r"^(?=.*\w)[\w.\-]+(?: [\w.\-]+){0,2}(?: \([^)]{1,12}\))?$"
)

def _is_valid_kv_key(key: str) -> bool:
    return bool(KEY_IDENTIFIER_RE.match(key))


# ---------------------------------
# Recursive value decomposition
# ---------------------------------

# A value may itself be a sequence of k<delim>v pairs joined by a secondary
# delimiter (e.g. "enabled;swift=v2024;fraud.ml=v3.1"). Decompose it into
# explicit sub-pairs instead of leaving one opaque value string.
SECONDARY_DELIMITERS  = [";", ","]
NESTED_PAIR_RATIO_MIN = 0.60


def _split_on_delimiter(s: str):
    """Split on the earliest-occurring allowed delimiter. Returns
    (key, delimiter, value) or None if no delimiter is present."""
    positions = [(d, s.find(d)) for d in ALLOWED_DELIMITERS if d in s]
    if not positions:
        return None
    d, i = min(positions, key=lambda x: x[1])
    return s[:i].strip(), d, s[i + 1:].strip()


def _decompose_value(value: str) -> Optional[Dict[str, Any]]:
    """
    Decompose a value that is itself a sequence of sub-pairs joined by a
    secondary delimiter. A segment becomes a sub-pair only if its sub-key
    passes the key-validity gate, so value fragments like "swift_mt202:with"
    do not manufacture garbage pairs from arbitrary colons. Returns
    {"delimiter", "pairs", "loose"} when a majority of segments are valid
    sub-pairs, else None (value stays opaque).
    """
    best: Optional[Dict[str, Any]] = None
    for sec in SECONDARY_DELIMITERS:
        if sec not in value:
            continue
        segments = [s.strip() for s in value.split(sec) if s.strip()]
        if len(segments) < 2:
            continue
        pairs: List[Dict[str, str]] = []
        loose: List[str]            = []
        for seg in segments:
            sub = _split_on_delimiter(seg)
            if sub is not None and _is_valid_kv_key(sub[0]):
                pairs.append({"key": sub[0], "value": sub[2]})
            else:
                loose.append(seg)
        if len(pairs) / len(segments) >= NESTED_PAIR_RATIO_MIN:
            if best is None or len(pairs) > len(best["pairs"]):
                best = {"delimiter": sec, "pairs": pairs, "loose": loose}
    return best


# ---------------------------------
# Confidence scoring (deterministic)
# ---------------------------------

def _score_confidence(key: str, value: str) -> float:
    """
    Deterministic confidence scale:
    1.0 — clean split, non-empty key and value
    0.8 — non-empty key, empty value (Name: case)
    0.0 — invalid (empty key)
    """
    if not key:
        return 0.0
    if not value:
        return 0.8
    return 1.0


# ---------------------------------
# Pattern confirmation
# ---------------------------------

def _confirm_patterns(
    line_pattern: Optional[LinePattern],
    key: str,
    value: str,
) -> List[str]:
    """
    Confirm which upstream patterns are validated by the KV result.
    patterns_used reflects confirmed structural facts, not hints.

    Rules:
    - kv_pair confirmed if KV detection succeeded (key non-empty)
    - numeric_value confirmed if value contains a numeric token
    - all other patterns excluded — not confirmed by KV detection
    """
    if line_pattern is None:
        return []

    confirmed = []
    available = set(line_pattern["patterns"])

    # kv_pair confirmed by successful detection
    if "kv_pair" in available and key:
        confirmed.append("kv_pair")

    # numeric_value confirmed if value carries numeric content
    if "numeric_value" in available and value and re.search(r"\b\d[\d,\.]*\b", value):
        confirmed.append("numeric_value")

    #print(f"[DEBUG] available={available}, key={repr(key)}, value={repr(value)}")

    return confirmed


# ---------------------------------
# Core
# ---------------------------------

def detect_kv(
    line: LineObject,
    line_pattern: Optional[LinePattern] = None,
) -> Optional[KVResult]:
    """
    Detect key-value pair from a single LineObject.
    KV detector is the authority — line_pattern is advisory only.

    Rules:
    - Split on earliest occurring delimiter (: or =)
    - Both key and value stripped
    - Empty key → rejected (returns None)
    - Key must be a plausible identifier → rejected otherwise (returns None)
    - Empty value → valid, confidence 0.8
    - patterns_used reflects confirmed facts, not hints
    """
    if line["is_empty"]:
        return None

    normalized = line["normalized"]

    # Rejection gate — before any splitting
    if _is_invalid_kv_line(normalized):
        return None

    # Find earliest delimiter by position
    positions = [
        (d, normalized.find(d))
        for d in ALLOWED_DELIMITERS
        if d in normalized
    ]

    if not positions:
        # No colon/equals — try a space-separated known-label split before giving
        # up ("Invoice No. INV-2026-0158").
        sk = _split_space_kv(normalized)
        if sk is None:
            return None
        key, value = sk
        if not _is_valid_kv_key(key):
            return None
        return {
            "line_index": line.get("line_index", -1),
            "key": key, "value": value,
            "delimiter": " ",
            "confidence": 0.75,
            "patterns_used": ["space_kv"],
            "nested": None,
        }

    delimiter_found, _ = min(positions, key=lambda x: x[1])

    # Split on first occurrence only
    parts = normalized.split(delimiter_found, 1)
    if len(parts) != 2:
        return None

    key = parts[0].strip()
    value = parts[1].strip()

    # Reject empty key
    if not key:
        return None

    # Reject keys that aren't plausible identifiers — prevents tree lines,
    # separators, labels, and log prefixes from being captured as KV.
    if not _is_valid_kv_key(key):
        return None

    confidence = _score_confidence(key, value)
    confirmed_patterns = _confirm_patterns(line_pattern, key, value)

    # Recursively decompose the value if it is itself a sequence of sub-pairs.
    nested = _decompose_value(value)

    return KVResult(
        line_index=line["line_index"],
        key=key,
        value=value,
        delimiter=delimiter_found,
        confidence=confidence,
        patterns_used=confirmed_patterns,
        nested=nested,
    )


def detect_all_kv(
    line_model: List[LineObject],
    line_patterns: Optional[List[LinePattern]] = None,
) -> List[KVResult]:
    """
    Run KV detection across all lines in a segment.
    line_patterns is optional — KV runs independently without it.
    Returns only confirmed KV results.
    """
    pattern_map = {}
    if line_patterns:
        pattern_map = {lp["line_index"]: lp for lp in line_patterns}

    results = []
    for line in line_model:
        lp = pattern_map.get(line["line_index"])
        result = detect_kv(line, lp)
        if result is not None:
            results.append(result)

    return results


# ---------------------------------
# Interactive Test
# ---------------------------------

if __name__ == "__main__":
    import sys
    from analysis.structure_engine.line_model import build_line_model
    from analysis.structure_engine.pattern_extractor import extract_all_patterns

    print("\n" + "=" * 60)
    print("KV DETECTOR INTERACTIVE TEST")
    print("=" * 60)
    print("Paste text then Ctrl+D to process. 'exit' to quit.\n")

    while True:
        print("INPUT> ", end="", flush=True)
        try:
            raw = sys.stdin.read()
        except EOFError:
            break

        if raw.strip().lower() == "exit":
            print("Exiting.")
            break

        model = build_line_model(raw)
        patterns = extract_all_patterns(model)
        results = detect_all_kv(model, patterns)

        print("\n--- KV RESULTS ---\n")
        if not results:
            print("No KV pairs detected.")
        else:
            for r in results:
                print(f"[{r['line_index']}]")
                print(f"  key          : {repr(r['key'])}")
                print(f"  value        : {repr(r['value'])}")
                print(f"  delimiter    : {repr(r['delimiter'])}")
                print(f"  confidence   : {r['confidence']}")
                print(f"  patterns_used: {r['patterns_used']}")
                print()

        print("-" * 60)
