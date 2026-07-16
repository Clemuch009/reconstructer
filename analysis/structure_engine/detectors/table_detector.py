import re
import csv
import io
from typing import List, Optional, Tuple, Dict
from typing_extensions import TypedDict
from analysis.structure_engine.line_model import LineObject

# ---------------------------------
# Contract
# ---------------------------------
import sys
class TableResult(TypedDict):
    region_type: str                  # always "table"
    table_type:  str                  # "pipe" | "aligned" | "hybrid"
    headers:     Optional[List[str]]  # None if not detected
    rows:        List[List[str]]
    col_count:   int
    row_count:   int
    confidence:  float


# ---------------------------------
# Constants
# ---------------------------------

MIN_COLUMNS          = 2
PIPE_THRESHOLD       = 0.50
HYBRID_MARGIN        = 0.15
HEADER_MIN_ROWS      = 2
MAX_ROW_LENGTH_VARIANCE = 4

# CSV detection — comma/tab/semicolon/pipe delimited text
# A line is CSV-like if it has consistent delimiter-split column counts
CSV_DELIMITERS       = [",", "\t", ";"]
CSV_MIN_ROWS         = 2
CSV_VARIANCE_MAX     = 0    # CSV must be perfectly consistent column count

# ---------------------------------
# Helpers
# ---------------------------------

def _non_empty(lines: List[LineObject]) -> List[LineObject]:
    return [l for l in lines if not l["is_empty"]]


def _pipe_density(lines: List[LineObject]) -> float:
    ne = _non_empty(lines)
    if not ne:
        return 0.0
    return sum(1 for l in ne if "|" in l["normalized"]) / len(ne)


def _is_separator_row(normalized: str) -> bool:
    return bool(re.match(r"^[\|\-\+\s=]+$", normalized))


def _is_numeric_string(s: str) -> bool:
    """
    Robust numeric check.
    Handles percentages, floats, integers.
    """
    try:
        float(s.strip().replace("%", "").replace(",", ""))
        return True
    except ValueError:
        return False


# ---------------------------------
# Header inference
# ---------------------------------

def _infer_header(rows: List[List[str]]) -> Tuple[Optional[List[str]], List[List[str]]]:
    """
    Header inference using type-based classification.
    Row 0 is a header if:
    - Its cells largely fail numeric conversion
    - Subsequent rows largely pass numeric conversion
    - OR row 0 has higher alphabetic ratio than row 1
    Never hallucinates — returns None if signal is weak.
    """
    if len(rows) < HEADER_MIN_ROWS:
        return None, rows

    def numeric_ratio(row: List[str]) -> float:
        if not row:
            return 0.0
        return sum(1 for c in row if _is_numeric_string(c)) / len(row)

    def alpha_ratio(row: List[str]) -> float:
        if not row:
            return 0.0
        return sum(1 for c in row if any(ch.isalpha() for ch in c)) / len(row)

    row0_numeric = numeric_ratio(rows[0])
    row1_numeric = numeric_ratio(rows[1])
    row0_alpha   = alpha_ratio(rows[0])
    row1_alpha   = alpha_ratio(rows[1])

    # Header signal: row 0 is less numeric and more alphabetic than row 1
    if row0_numeric < row1_numeric and row0_alpha >= row1_alpha:
        return rows[0], rows[1:]

    return None, rows


# ---------------------------------
# Row variance gate
# ---------------------------------

def _passes_variance_gate(rows: List[List[str]]) -> bool:
    """
    Reject tables where row length variance is too high.
    Prevents noisy structures from being classified as tables.
    """
    if not rows:
        return False
    lengths = [len(r) for r in rows]
    return (max(lengths) - min(lengths)) <= MAX_ROW_LENGTH_VARIANCE


# ---------------------------------
# Confidence scoring (weighted, decoupled)
# ---------------------------------

def _score_pipe(
    col_count: int,
    row_count: int,
    headers_detected: bool,
    pipe_density: float,
) -> float:
    """
    Weighted confidence for pipe tables.
    Structure: 0.4, Row consistency: 0.3, Header: 0.2, Format: 0.1
    """
    structure  = 1.0 if col_count >= MIN_COLUMNS else 0.0
    row_cons   = 1.0 if row_count >= 2 else 0.5
    header     = 1.0 if headers_detected else 0.0
    fmt_signal = min(1.0, pipe_density / PIPE_THRESHOLD)

    return round(
        structure  * 0.4 +
        row_cons   * 0.3 +
        header     * 0.2 +
        fmt_signal * 0.1,
        2
    )


def _score_aligned(
    col_count: int,
    row_count: int,
    headers_detected: bool,
    alignment_quality: float,
) -> float:
    """
    Weighted confidence for aligned tables.
    Structure: 0.4, Row consistency: 0.3, Header: 0.2, Format: 0.1
    """
    structure  = 1.0 if col_count >= MIN_COLUMNS else 0.0
    row_cons   = 1.0 if row_count >= 2 else 0.5
    header     = 1.0 if headers_detected else 0.0
    fmt_signal = min(1.0, alignment_quality)

    return round(
        structure  * 0.4 +
        row_cons   * 0.3 +
        header     * 0.2 +
        fmt_signal * 0.1,
        2
    )


# ---------------------------------
# Pipe table parser
# ---------------------------------

def _parse_pipe_row(normalized: str) -> List[str]:
    row = normalized.strip().strip("|")
    return [cell.strip() for cell in row.split("|")]


def _parse_pipe_table(
    lines: List[LineObject],
) -> Tuple[Optional[List[str]], List[List[str]], int]:
    ne = _non_empty(lines)
    raw_rows = []

    for line in ne:
        if _is_separator_row(line["normalized"]):
            continue
        cells = _parse_pipe_row(line["normalized"])
        if len(cells) >= MIN_COLUMNS:
            raw_rows.append(cells)

    if not raw_rows:
        return None, [], 0

    if not _passes_variance_gate(raw_rows):  # validate before padding
        return None, [], 0

    col_count = max(len(r) for r in raw_rows)
    rows = [r + [""] * (col_count - len(r)) for r in raw_rows]

    headers, rows = _infer_header(rows)
    return headers, rows, col_count


# ---------------------------------
# Aligned table parser
# ---------------------------------

def _infer_column_boundaries(
    lines: List[LineObject],
) -> List[int]:
    """
    Infer column anchors from token offsets.
    Dynamic support floor: max(2, int(total * 0.4))
    Prevents fragile hard threshold failures on sparse tables.
    """
    ne = _non_empty(lines)
    if not ne:
        return []

    total = len(ne)
    min_support = max(2, int(total * 0.4))

    offset_counts: Dict[int, int] = {}
    for line in ne:
        for off in line["token_offsets"]:
            offset_counts[off] = offset_counts.get(off, 0) + 1

    anchors = sorted(
        off for off, count in offset_counts.items()
        if count >= min_support
    )

    return anchors


def _assign_column(offset: int, boundaries: List[int]) -> int:
    """
    Assign a token to the nearest column boundary index.
    """
    best = 0
    for i, b in enumerate(boundaries):
        if offset >= b:
            best = i
    return best


def _parse_aligned_row(
    line: LineObject,
    boundaries: List[int],
) -> List[str]:
    """
    Map tokens to columns using offset intersection.
    Avoids destructive string slicing.
    """
    cell_buckets: List[List[str]] = [[] for _ in range(len(boundaries))]


    for token, offset in zip(line["tokens"], line["token_offsets"]):
        col = _assign_column(offset, boundaries)
        cell_buckets[col].append(token)

    return [" ".join(bucket) for bucket in cell_buckets]


def _parse_aligned_table(
    lines: List[LineObject],
) -> Tuple[Optional[List[str]], List[List[str]], int, float]:
    """
    Returns (headers, rows, col_count, alignment_quality).
    """
    ne = _non_empty(lines)
    boundaries = _infer_column_boundaries(ne)

    if len(boundaries) < MIN_COLUMNS:
        return None, [], 0, 0.0

    raw_rows = []
    for line in ne:
        cells = _parse_aligned_row(line, boundaries)
        if len(cells) >= MIN_COLUMNS:
            raw_rows.append(cells)

    if not raw_rows:
        return None, [], 0, 0.0

    if not _passes_variance_gate(raw_rows):  # validate before padding
        return None, [], 0, 0.0

    col_count = max(len(r) for r in raw_rows)
    rows = [r + [""] * (col_count - len(r)) for r in raw_rows]


    # Alignment quality: ratio of rows matching most common col count
    lengths = [len(r) for r in rows]
    most_common = max(set(lengths), key=lengths.count)
    alignment_quality = lengths.count(most_common) / len(lengths)

    headers, rows = _infer_header(rows)
    return headers, rows, col_count, alignment_quality


# ---------------------------------
# CSV table parser
# ---------------------------------

def _detect_csv_delimiter(lines: List[LineObject]) -> Optional[str]:
    """
    Detect consistent delimiter across all non-empty lines.
    Returns delimiter if all lines split to the same column count,
    None if no consistent delimiter found.

    Skips structural marker lines from extractors ([PAGE:], [SHEET:] etc.)
    """
    import re as _re
    _MARKER = _re.compile(
        r"^\[PAGE:\s*\d+\]$|^\[WORKBOOK\]$|^\[SHEET:|^\[VISUAL:"
        r"|^rows:\s*\d+$|^sheets:\s*\d+|^sheet_names:|^hidden_sheets:|^empty_sheets:"
    )
    ne = [
        l["normalized"] for l in lines
        if not l["is_empty"] and not _MARKER.match(l["normalized"].strip())
    ]
    if len(ne) < CSV_MIN_ROWS:
        return None

    for delim in CSV_DELIMITERS:
        try:
            reader = csv.reader(io.StringIO("\n".join(ne)), delimiter=delim)
            parsed = [row for row in reader if row]
            if len(parsed) < CSV_MIN_ROWS:
                continue
            col_counts = [len(row) for row in parsed]
            if (
                min(col_counts) >= MIN_COLUMNS
                and max(col_counts) - min(col_counts) == CSV_VARIANCE_MAX
            ):
                return delim
        except Exception:
            continue

    return None


def _parse_csv_table(
    lines: List[LineObject],
    delimiter: str,
) -> Tuple[Optional[List[str]], List[List[str]], int]:
    """
    Parse CSV lines into headers and rows using csv.reader.
    Handles quoted fields containing the delimiter correctly.
    Skips structural marker lines from extractors.
    Returns (headers, rows, col_count).
    """
    import re as _re
    _MARKER = _re.compile(
        r"^\[PAGE:\s*\d+\]$|^\[WORKBOOK\]$|^\[SHEET:|^\[VISUAL:"
        r"|^rows:\s*\d+$|^sheets:\s*\d+|^sheet_names:|^hidden_sheets:|^empty_sheets:"
    )
    ne = [
        l["normalized"] for l in lines
        if not l["is_empty"] and not _MARKER.match(l["normalized"].strip())
    ]

    try:
        reader = csv.reader(io.StringIO("\n".join(ne)), delimiter=delimiter)
        raw_rows = [[cell.strip() for cell in row] for row in reader if row]
    except Exception:
        return None, [], 0

    if not raw_rows:
        return None, [], 0

    col_count = len(raw_rows[0])
    headers, rows = _infer_header(raw_rows)
    return headers, rows, col_count


def _score_csv(
    col_count: int,
    row_count: int,
    headers_detected: bool,
) -> float:
    """
    Confidence for CSV tables.
    CSV has perfect column consistency by definition (csv.reader handles quoting).
    Structure: 0.4, Row consistency: 0.3, Header: 0.2, Format: 0.1
    """
    structure = 1.0 if col_count >= MIN_COLUMNS else 0.0
    row_cons  = 1.0 if row_count >= 2 else 0.5
    header    = 1.0 if headers_detected else 0.0
    fmt       = 1.0  # CSV is always perfectly consistent

    return round(
        structure * 0.4 +
        row_cons  * 0.3 +
        header    * 0.2 +
        fmt       * 0.1,
        2
    )


# ---------------------------------
# Core
# ---------------------------------

def _parse_token_type_table(
    lines: List[LineObject],
) -> Tuple[Optional[List[str]], List[List[str]], int]:
    """
    Parse a whitespace-separated DATA table by TOKEN TYPE rather than column
    position. A data row is: a text description followed by a run of trailing
    numeric/currency tokens (quantity, price, amount). This handles single-space
    separated tables ("Design Sprint 40 $100.00 $4,000.00") that positional
    alignment can't split.

    Row shape: [description, num1, num2, ...] — description is everything before
    the first token of the trailing numeric run; the numeric tokens become the
    remaining columns. The header row (leading line of non-numeric labels) is
    used if its column count is compatible.
    """
    ne = _non_empty(lines)
    if len(ne) < 2:
        return None, [], 0

    # A pure-label header row ("Description Amount") tells us this really is a
    # table, which lets us accept rows with only ONE trailing numeric column
    # (a 2-column Description|Amount table). Without a header we stay
    # conservative and require >=2 numeric columns, so prose lines that happen
    # to end in a number are not mistaken for table rows.
    def _line_text(l):
        return l["normalized"] if "normalized" in l else " ".join(l.get("tokens", []))

    def _is_label_header(text: str) -> bool:
        toks = text.split()
        if not toks or len(toks) > 8:
            return False
        if _NUM_TOKEN_RE.search(text) or any(ch.isdigit() or ch in "$£€¥" for ch in text):
            return False
        hits = sum(1 for t in toks if t.strip(":,").lower() in _HEADER_LABEL_WORDS)
        return hits >= 2 and hits / len(toks) >= 0.5

    has_header = _is_label_header(_line_text(ne[0]))
    min_nums = 1 if has_header else 2

    def _split_row(text: str) -> Optional[List[str]]:
        toks = text.split()
        if len(toks) < (2 if has_header else 3):
            return None
        # find the trailing run of numeric/currency tokens
        k = 0
        for t in reversed(toks):
            if _NUM_TOKEN_RE.match(t):
                k += 1
            else:
                break
        if k < min_nums:
            return None
        desc = " ".join(toks[:len(toks) - k])
        nums = toks[len(toks) - k:]
        if not desc:
            return None
        return [desc] + nums

    rows: List[List[str]] = []
    for line in ne:
        text = line["normalized"] if "normalized" in line else " ".join(line.get("tokens", []))
        r = _split_row(text)
        if r is not None:
            rows.append(r)

    if len(rows) < 1:
        return None, [], 0

    # consistent column count across data rows (allow the description to hold
    # varying words; the numeric column count must match)
    num_counts = [len(r) - 1 for r in rows]
    if len(set(num_counts)) != 1:
        return None, [], 0
    col_count = num_counts[0] + 1
    if col_count < MIN_COLUMNS:
        return None, [], 0

    # header: first non-empty line whose tokens are mostly non-numeric labels
    headers = None
    first_text = ne[0]["normalized"] if "normalized" in ne[0] else " ".join(ne[0].get("tokens", []))
    first_toks = first_text.split()
    if first_toks and _split_row(first_text) is None:
        # looks like a label row — map it to col_count columns:
        # last (col_count-1) tokens are the numeric-column labels; the rest are
        # the description label.
        if len(first_toks) >= col_count:
            nlabels = col_count - 1
            headers = [" ".join(first_toks[:len(first_toks) - nlabels])] + first_toks[len(first_toks) - nlabels:]

    return headers, rows, col_count


_NUM_TOKEN_RE = re.compile(r"^[\$£€¥]?\-?[\d,]+(?:\.\d+)?%?$")

# Column-label words used to recognise a pure-label table header row.
_HEADER_LABEL_WORDS = {
    "description", "item", "items", "quantity", "qty", "price", "rate", "unit",
    "amount", "total", "cost", "service", "charge", "value", "net", "sum",
    "details", "line", "type", "volume", "activity", "resource",
}


def detect_table(lines: List[LineObject]) -> Optional[TableResult]:
    """
    Detect and parse table from LineObjects.
    Routes to CSV, pipe, or aligned parser.
    CSV is tried first — it is the most unambiguous format.
    Detects hybrid when pipe/aligned scores are within HYBRID_MARGIN.
    Returns None if no valid table detected.
    """
    ne = _non_empty(lines)
    if not ne:
        return None


    # CSV detection — try before pipe/aligned
    # CSV is unambiguous: consistent delimiter, quoted fields, exact columns
    csv_delimiter = _detect_csv_delimiter(lines)
    if csv_delimiter is not None:
        c_headers, c_rows, c_cols = _parse_csv_table(lines, csv_delimiter)
        if c_rows and c_cols >= MIN_COLUMNS:
            csv_score = _score_csv(c_cols, len(c_rows), c_headers is not None)
            return TableResult(
                region_type="table",
                table_type="csv",
                headers=c_headers,
                rows=c_rows,
                col_count=c_cols,
                row_count=len(c_rows),
                confidence=csv_score,
            )

    pipe = _pipe_density(lines)

    # Parse candidates
    p_headers, p_rows, p_cols = _parse_pipe_table(lines)
    a_headers, a_rows, a_cols, align_q = _parse_aligned_table(lines)
    # Token-type parse: splits single-space data rows by token type. Preferred
    # when positional alignment produces a degenerate result (it often merges a
    # whole single-space row into one cell, yielding too few columns) — OR when
    # a pure-label header row tells us the TRUE column count and the token-type
    # parse matches it. The latter matters because positional alignment can
    # report MORE columns than really exist by splitting a phrase mid-way
    # ("Cloud Architecture | None | Consulting (Phase | 1) $1,000.00"), which
    # would otherwise beat a correct 2-column parse on column count alone.
    t_headers, t_rows, t_cols = _parse_token_type_table(lines)
    header_authoritative = bool(t_headers) and len(t_headers) == t_cols
    if t_rows and (t_cols > max(a_cols, p_cols) or header_authoritative):
        return TableResult(
            region_type="table",
            table_type="aligned",
            headers=t_headers,
            rows=t_rows,
            col_count=t_cols,
            row_count=len(t_rows),
            confidence=0.72,
        )

    pipe_score    = _score_pipe(p_cols, len(p_rows), p_headers is not None, pipe)
    aligned_score = _score_aligned(a_cols, len(a_rows), a_headers is not None, align_q)

    # Determine table type
    if abs(pipe_score - aligned_score) < HYBRID_MARGIN:
        table_type = "hybrid"
        # Use higher scoring parser for data
        if pipe_score >= aligned_score:
            headers, rows, col_count = p_headers, p_rows, p_cols
            confidence = pipe_score
        else:
            headers, rows, col_count = a_headers, a_rows, a_cols
            confidence = aligned_score
    elif pipe_score > aligned_score:
        table_type = "pipe"
        headers, rows, col_count = p_headers, p_rows, p_cols
        confidence = pipe_score
    else:
        table_type = "aligned"
        headers, rows, col_count = a_headers, a_rows, a_cols
        confidence = aligned_score

    if not rows or col_count < MIN_COLUMNS:
        # Fallback: token-type parser for single-space data tables that
        # positional alignment can't split ("Design Sprint 40 $100 $4,000").
        t_headers, t_rows, t_cols = _parse_token_type_table(lines)
        if t_rows and t_cols >= MIN_COLUMNS:
            return TableResult(
                region_type="table",
                table_type="aligned",
                headers=t_headers,
                rows=t_rows,
                col_count=t_cols,
                row_count=len(t_rows),
                confidence=0.7,
            )
        return None

    return TableResult(
        region_type="table",
        table_type=table_type,
        headers=headers,
        rows=rows,
        col_count=col_count,
        row_count=len(rows),
        confidence=confidence,
    )


# ---------------------------------
# Interactive Test
# ---------------------------------

if __name__ == "__main__":
    import sys
    from analysis.structure_engine.line_model import build_line_model

    print("\n" + "=" * 60)
    print("TABLE DETECTOR INTERACTIVE TEST")
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
        result = detect_table(model)

        print("\n--- TABLE DETECTION ---\n")
        if result is None:
            print("No table detected.")
        else:
            print(f"  table_type : {result['table_type']}")
            print(f"  col_count  : {result['col_count']}")
            print(f"  row_count  : {result['row_count']}")
            print(f"  confidence : {result['confidence']}")
            print(f"  headers    : {result['headers']}")
            print(f"\n  rows:")
            for i, row in enumerate(result["rows"]):
                print(f"    [{i}] {row}")

        print("-" * 60)
