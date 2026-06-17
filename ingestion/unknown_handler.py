# ingestion/unknown_handler.py

import re
from typing import Optional
from ingestion.result import SourceFormat


# ---------------------------------
# Decoding chain
# ---------------------------------

_DECODE_CHAIN: list[tuple[str, str]] = [
    ("utf-8-sig",  "utf-8-sig"),
    ("utf-8",      "utf-8"),
    ("utf-16",     "utf-16"),
    ("utf-16-le",  "utf-16-le"),
    ("utf-16-be",  "utf-16-be"),
    ("latin-1",    "latin-1"),
]


# ---------------------------------
# Heuristic thresholds
# ---------------------------------

_HTML_TAG_DENSITY     = 0.25   # fraction of lines with paired/structural HTML tags
_CSV_LINE_THRESHOLD   = 0.7    # fraction of lines that are CSV-consistent
_CSV_CONSISTENCY      = 0.85   # fraction of CSV lines with same column count
_JSON_THRESHOLD       = 0.4    # fraction of lines with JSON structure
_MIN_LINES            = 3
_SAMPLE_LINES         = 50

# Binary detection — applied BEFORE the decode chain. latin-1 (last in the
# chain) decodes ANY byte sequence, so without this pre-check the is_binary
# path below is unreachable and true binary content is silently accepted as
# mojibake text. These thresholds gate that fallback.
_BINARY_SAMPLE_BYTES  = 4096   # inspect only the head — enough to classify
_BINARY_NONTEXT_RATIO = 0.30   # > this fraction of non-text bytes ⇒ binary


# ---------------------------------
# Binary pre-check
# ---------------------------------

# Bytes that legitimately appear in text: printable ASCII + common whitespace
# controls (tab, newline, carriage return, form feed, backspace, bell, escape).
_TEXT_BYTES = bytes(range(0x20, 0x7F)) + b"\t\n\r\f\b\x07\x1b"
_TEXT_BYTE_SET = frozenset(_TEXT_BYTES)


def _looks_binary(raw_bytes: bytes) -> bool:
    """
    Decide whether raw_bytes is binary BEFORE attempting text decoding.

    Two signals:
    1. A NUL byte (0x00) almost never occurs in real text and is the single
       strongest binary indicator.
    2. A high fraction of non-text bytes in the head of the content.

    Conservative by design: empty input is NOT binary (handled elsewhere as
    empty), and UTF-16 text — which legitimately contains NUL bytes — is
    deliberately excluded from the NUL rule so it still decodes downstream.
    """
    if not raw_bytes:
        return False

    sample = raw_bytes[:_BINARY_SAMPLE_BYTES]

    # NUL byte ⇒ binary, UNLESS it looks like UTF-16 (BOM, or regular
    # alternating-NUL pattern), which the decode chain handles as text.
    if b"\x00" in sample:
        if sample[:2] in (b"\xff\xfe", b"\xfe\xff"):
            return False  # UTF-16 BOM — let the decode chain handle it
        nul_ratio = sample.count(0) / len(sample)
        # UTF-16 ASCII text is ~50% NUL in a regular pattern; treat a moderate,
        # high NUL ratio as possible UTF-16 and defer to the decode chain.
        if 0.30 <= nul_ratio <= 0.60:
            return False
        return True

    nontext = sum(1 for b in sample if b not in _TEXT_BYTE_SET)
    return (nontext / len(sample)) > _BINARY_NONTEXT_RATIO


# ---------------------------------
# Line classifiers — corrected
# ---------------------------------

def _line_looks_html(line: str) -> bool:
    """
    Requires structural HTML density — not just any tag.
    Single <error> or <value> tags in logs do not qualify.
    Requires:
    - Closing tag pair  <tag>...</tag>
    - OR structural block tags: html, head, body, div, table, tr, td, p
    - OR DOCTYPE declaration
    """
    structural = bool(re.search(
        r"</[a-zA-Z]+>|"
        r"<(html|head|body|div|table|tr|td|th|p|ul|ol|li|form|section)"
        r"[\s>]|<!DOCTYPE",
        line, re.IGNORECASE
    ))
    return structural


def _line_looks_csv(line: str, delimiter: str) -> tuple[bool, int]:
    """
    Returns (is_csv_like, column_count).
    Requires at least 2 fields and non-trivial content.
    Does NOT trigger on lines starting with structural characters.
    """
    stripped = line.strip()
    if not stripped:
        return False, 0
    if stripped.startswith(("<", "{", "[", "#")):
        return False, 0

    parts = stripped.split(delimiter)
    if len(parts) < 2:
        return False, 0
    if not any(p.strip() for p in parts):
        return False, 0

    return True, len(parts)


def _line_looks_json(line: str) -> bool:
    """
    Line has JSON structural patterns.
    Requires more than just an opening brace.
    """
    stripped = line.strip()
    return bool(re.match(
        r'^[{\[]|^[}\]]|^"[^"]+"\s*:|^\s*"[^"]+"\s*:\s*["{{\[\d]',
        stripped
    ))


# ---------------------------------
# Delimiter detection
# ---------------------------------

def _detect_delimiter(lines: list[str]) -> str:
    candidates = [",", ";", "\t", "|"]
    scores: dict[str, int] = {d: 0 for d in candidates}
    for line in lines[:_SAMPLE_LINES]:
        for delim in candidates:
            count = line.count(delim)
            if count >= 1:
                scores[delim] += count
    best = max(scores, key=lambda d: scores[d])
    return best if scores[best] > 0 else ","


# ---------------------------------
# Confidence normalization
# ---------------------------------

def _normalize_breakdown(breakdown: dict[str, float]) -> dict[str, float]:
    """
    Normalize confidence breakdown so values sum to 1.0.
    Prevents noise accumulation across signals.
    """
    total = sum(breakdown.values())
    if total <= 0:
        count = len(breakdown)
        return {k: round(1.0 / count, 3) for k in breakdown}
    return {k: round(v / total, 3) for k, v in breakdown.items()}


# ---------------------------------
# Content classifier — signal provider
# ---------------------------------

def _classify_content(
    text: str,
) -> tuple[SourceFormat, dict[str, float], list[str]]:
    """
    Classify decoded text using line-level heuristics.
    Returns (format_hint, normalized_confidence_breakdown, source_signals).

    Rules:
    - format_hint is a WEAK HINT — router makes final decision
    - "mixed" returned when no structure is dominant
    - JSON preserved as explicit hint — not collapsed to txt
    - CSV requires column consistency — not just delimiter presence
    - HTML requires structural density — not single tags
    - Confidence breakdown always sums to 1.0
    """
    lines   = [l for l in text.splitlines() if l.strip()]
    signals: list[str] = []

    if len(lines) < _MIN_LINES:
        signals.append(
            f"Too few lines ({len(lines)}) for reliable classification"
        )
        return "txt", {"txt": 1.0, "html": 0.0, "csv": 0.0, "json": 0.0}, signals

    sample = lines[:_SAMPLE_LINES]
    total  = len(sample)

    # HTML scoring
    html_hits  = sum(1 for l in sample if _line_looks_html(l))
    html_ratio = html_hits / total

    # JSON scoring
    json_hits  = sum(1 for l in sample if _line_looks_json(l))
    json_ratio = json_hits / total

    # CSV scoring — with consistency check
    delimiter   = _detect_delimiter(sample)
    csv_results = [_line_looks_csv(l, delimiter) for l in sample]
    csv_hits    = [r for r in csv_results if r[0]]
    csv_ratio   = len(csv_hits) / total

    # CSV consistency — same column count across rows
    csv_consistent = 0.0
    if csv_hits:
        col_counts    = [r[1] for r in csv_hits]
        most_common   = max(set(col_counts), key=col_counts.count)
        consistent    = sum(1 for c in col_counts if c == most_common)
        csv_consistent = consistent / len(col_counts)

    # Effective CSV score — penalized by inconsistency
    csv_effective = csv_ratio * csv_consistent

    signals.append(
        f"html={html_ratio:.2f} csv={csv_ratio:.2f} "
        f"csv_consistency={csv_consistent:.2f} "
        f"csv_effective={csv_effective:.2f} "
        f"json={json_ratio:.2f}"
    )

    # Raw breakdown — normalized below
    raw_breakdown: dict[str, float] = {
        "html": html_ratio,
        "csv":  csv_effective,
        "json": json_ratio,
        "txt":  max(0.0, 1.0 - html_ratio - csv_effective - json_ratio),
    }

    breakdown = _normalize_breakdown(raw_breakdown)

    # ---------------------------------
    # Classification — priority order
    # ---------------------------------

    # HTML — requires structural density
    if html_ratio >= _HTML_TAG_DENSITY:
        signals.append(
            f"HTML structural density: {html_hits}/{total} lines"
        )
        return "html", breakdown, signals

    # CSV — requires both ratio and consistency
    if csv_ratio >= _CSV_LINE_THRESHOLD and csv_consistent >= _CSV_CONSISTENCY:
        signals.append(
            f"CSV detected: {len(csv_hits)}/{total} lines, "
            f"consistency={csv_consistent:.2f}, delimiter='{delimiter}'"
        )
        return "csv", breakdown, signals

    # JSON — preserved as explicit hint
    if json_ratio >= _JSON_THRESHOLD:
        signals.append(
            f"JSON structure: {json_hits}/{total} lines — "
            "preserving as json hint for router"
        )
        return "json", breakdown, signals

    # Mixed — no dominant structure
    dominant = max(raw_breakdown, key=lambda k: raw_breakdown[k])
    dominant_score = raw_breakdown[dominant]

    if dominant_score < 0.5:
        signals.append(
            f"No dominant structure detected — "
            f"highest signal: {dominant}={dominant_score:.2f} — "
            "classifying as mixed"
        )
        return "txt", breakdown, signals

    signals.append(
        f"Weak dominant structure: {dominant}={dominant_score:.2f} "
        "— defaulting to txt"
    )
    return "txt", breakdown, signals


# ---------------------------------
# Result
# ---------------------------------

class UnknownHandlerResult:
    __slots__ = (
        "format_hint",
        "decoded_text",
        "encoding_used",
        "confidence_breakdown",
        "source_signals",
        "warnings",
        "is_binary",
    )

    def __init__(
        self,
        format_hint:          SourceFormat,
        decoded_text:         str,
        encoding_used:        Optional[str],
        confidence_breakdown: dict[str, float],
        source_signals:       list[str],
        warnings:             list[str],
        is_binary:            bool,
    ):
        self.format_hint          = format_hint
        self.decoded_text         = decoded_text
        self.encoding_used        = encoding_used
        self.confidence_breakdown = confidence_breakdown
        self.source_signals       = source_signals
        self.warnings             = warnings
        self.is_binary            = is_binary


# ---------------------------------
# Main handler — signal provider
# ---------------------------------

def handle_unknown(
    raw_bytes: bytes,
    filename:  Optional[str] = None,
) -> UnknownHandlerResult:
    """
    Handle files with unrecognized format.

    Role: signal provider — not a classifier.
    Outputs decoded text + hints + signals.
    Router makes all final routing decisions.

    Pipeline:
    1. Binary pre-check — reject binary BEFORE decoding (latin-1 would
       otherwise decode anything and mask binary content)
    2. Attempt text decoding — full encoding chain
    3. Binary content → is_binary: True, router rejects gracefully
    4. Classify content → format_hint + confidence_breakdown
    5. Return UnknownHandlerResult — router consumes signals

    Rules:
    - Never raises
    - format_hint is a WEAK HINT — not a routing decision
    - "mixed" signals segmentation is needed
    - Binary returns is_binary: True with clear warning
    - Confidence breakdown always sums to 1.0
    """
    warnings:       list[str] = []
    source_signals: list[str] = []
    encoding_used:  Optional[str] = None
    decoded_text:   str = ""

    # Step 1 — binary pre-check (before decoding).
    # latin-1 at the end of the decode chain cannot fail, so without this the
    # binary branch below is unreachable and binary content is accepted as
    # mojibake. Reject obvious binary here; genuine text (incl. UTF-16) passes.
    if _looks_binary(raw_bytes):
        warnings.append(
            f"Cannot decode as text — binary content detected. "
            f"Filename: {filename or 'unknown'}"
        )
        return UnknownHandlerResult(
            format_hint="unknown",
            decoded_text="",
            encoding_used=None,
            confidence_breakdown={},
            source_signals=["Binary — failed binary pre-check"],
            warnings=warnings,
            is_binary=True,
        )

    # Step 2 — attempt decoding
    for codec, label in _DECODE_CHAIN:
        try:
            decoded_text  = raw_bytes.decode(codec)
            encoding_used = label
            if label not in ("utf-8", "utf-8-sig"):
                warnings.append(f"Unknown format decoded as {label}")
            break
        except (UnicodeDecodeError, Exception):
            continue
    else:
        warnings.append(
            f"Cannot decode as text — binary content detected. "
            f"Filename: {filename or 'unknown'}"
        )
        return UnknownHandlerResult(
            format_hint="unknown",
            decoded_text="",
            encoding_used=None,
            confidence_breakdown={},
            source_signals=["Binary — all text decoding attempts failed"],
            warnings=warnings,
            is_binary=True,
        )

    source_signals.append(f"Decoded as {encoding_used}")
    if filename:
        source_signals.append(f"Filename: {filename}")

    # Step 3 — classify content
    format_hint, breakdown, classify_signals = _classify_content(decoded_text)
    source_signals.extend(classify_signals)

    warnings.append(
        f"Unrecognized format — heuristic hint: {format_hint}. "
        f"Filename: {filename or 'unknown'}"
    )

    return UnknownHandlerResult(
        format_hint=format_hint,
        decoded_text=decoded_text,
        encoding_used=encoding_used,
        confidence_breakdown=breakdown,
        source_signals=source_signals,
        warnings=warnings,
        is_binary=False,
    )
