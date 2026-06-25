import re
import csv
import io
from typing import List, Tuple
from typing_extensions import TypedDict
from analysis.structure_engine.line_model import LineObject


# ---------------------------------
# Contract
# ---------------------------------<

print("latest deplyment running")
class RegionResult(TypedDict):
    region_type: str        # table_candidate | structured_block | unstructured
    start_line:  int
    end_line:    int
    confidence:  float      # passed_signals / total_relevant_signals


# ---------------------------------
# Locked thresholds
# ---------------------------------

PIPE_DENSITY_THRESHOLD        = 0.50
ALIGNED_ROW_RATIO_THRESHOLD   = 0.70
TOKEN_STABILITY_THRESHOLD     = 0.70
KV_DENSITY_THRESHOLD          = 0.35
DELIMITER_RATIO_THRESHOLD     = 0.40
SHAPE_REPETITION_THRESHOLD    = 0.50
CONFIDENCE_FALLBACK_THRESHOLD = 0.55

MIN_TABLE_LINES               = 3
MIN_STRUCTURED_LINES          = 2


# ---------------------------------
# Signal patterns
# ---------------------------------

DELIMITER_RE  = re.compile(r"[:=]")
KV_RE         = re.compile(r"^[^:=]+[:=].*")
SEPARATOR_RE  = re.compile(r"^(?:[-=_]\s*){3,}$")

# Glyph line detection
GLYPH_LINE_RE = re.compile(
    r"^\s*"           # optional leading spaces
    r"(?:[│|]\s*)*"   # zero or more pipe continuation chars
    r"[+├└]"          # branch marker
    r"\s*[-─]+"       # dash sequence
)

# Multi-space detection
MULTI_SPACE_RE = re.compile(r"\s{2,}")


def _glyph_line_density(lines: List[LineObject]) -> float:
    ne = _non_empty(lines)
    if not ne:
        return 0.0
    return sum(1 for l in ne if GLYPH_LINE_RE.match(l["normalized"])) / len(ne)


def _multi_space_ratio(lines: List[LineObject]) -> float:
    ne = _non_empty(lines)
    if not ne:
        return 0.0
    return sum(1 for l in ne if MULTI_SPACE_RE.search(l["normalized"])) / len(ne)


# ---------------------------------
# Signal extractors
# ---------------------------------

def _non_empty(lines: List[LineObject]) -> List[LineObject]:
    return [l for l in lines if not l["is_empty"]]


def _pipe_density(lines: List[LineObject]) -> float:
    ne = _non_empty(lines)
    if not ne:
        return 0.0
    return sum(1 for l in ne if "|" in l["normalized"]) / len(ne)


def _token_count_stability(lines: List[LineObject]) -> float:
    """
    Ratio of non-empty lines sharing the most common token count.
    Renamed from _aligned_row_ratio — measures token count only.
    """
    ne = _non_empty(lines)
    if not ne:
        return 0.0
    counts = [len(l["tokens"]) for l in ne]
    most_common = max(set(counts), key=counts.count)
    return counts.count(most_common) / len(counts)


def _column_alignment(lines: List[LineObject]) -> float:
    """
    Real alignment detection using token_offsets.
    Measures how consistently tokens start at the same
    character positions across lines.
    Only considers lines with identical token counts.
    """
    ne = _non_empty(lines)
    if not ne:
        return 0.0

    counts = [len(l["tokens"]) for l in ne]
    if not counts:
        return 0.0

    most_common_count = max(set(counts), key=counts.count)
    candidate_lines = [l for l in ne if len(l["tokens"]) == most_common_count]

    if len(candidate_lines) < 2:
        return 0.0

    # Compare token offsets across candidate lines
    # A column is aligned if its offset variance is low
    aligned_columns = 0
    total_columns = most_common_count

    for col in range(total_columns):
        offsets = [l["token_offsets"][col] for l in candidate_lines]
        variance = max(offsets) - min(offsets)
        if variance <= 2:  # tolerance of 2 chars
            aligned_columns += 1

    return aligned_columns / total_columns if total_columns > 0 else 0.0


def _has_structural_separator(lines: List[LineObject]) -> bool:
    """
    Returns True if any line is a pure structural separator.
    Required gate for table_candidate to prevent ghost tables.
    """
    return any(SEPARATOR_RE.match(l["normalized"]) for l in lines)


def _has_pipe(lines: List[LineObject]) -> bool:
    return any("|" in l["normalized"] for l in lines)


def _kv_density(lines: List[LineObject]) -> float:
    ne = _non_empty(lines)
    if not ne:
        return 0.0
    return sum(1 for l in ne if KV_RE.match(l["normalized"])) / len(ne)


def _delimiter_ratio(lines: List[LineObject]) -> float:
    ne = _non_empty(lines)
    if not ne:
        return 0.0
    return sum(
        1 for l in ne if DELIMITER_RE.search(l["normalized"])
    ) / len(ne)


def _shape_repetition(lines: List[LineObject]) -> float:
    """
    Shape proxy: (token_count, has_delimiter, is_indented)
    """
    ne = _non_empty(lines)
    if not ne:
        return 0.0

    def shape(l: LineObject) -> Tuple:
        return (
            len(l["tokens"]),
            bool(DELIMITER_RE.search(l["normalized"])),
            l["indent"] > 0,
        )

    shapes = [shape(l) for l in ne]
    most_common = max(set(shapes), key=shapes.count)
    return shapes.count(most_common) / len(shapes)


def _csv_column_consistency(lines: List[LineObject]) -> Tuple[bool, float]:
    """
    Detect CSV structure: consistent column count when split by comma/tab/semicolon.
    Uses csv.reader to handle quoted fields correctly.

    Returns (is_csv, confidence):
    - is_csv: True if all non-empty lines have same column count >= 2
    - confidence: 1.0 if perfect consistency, 0.0 otherwise

    This is the missing signal for CSV files which have no pipes,
    no space alignment, and no structural separators.
    """
    ne = [l["normalized"] for l in lines if not l["is_empty"]]
    if len(ne) < MIN_TABLE_LINES:
        return False, 0.0

    for delim in [",", "\t", ";"]:
        try:
            reader = csv.reader(io.StringIO("\n".join(ne)), delimiter=delim)
            parsed = [row for row in reader if row]
            if len(parsed) < MIN_TABLE_LINES:
                continue
            col_counts = [len(row) for row in parsed]
            if (min(col_counts) >= 2 and
                    max(col_counts) - min(col_counts) == 0):
                return True, 1.0
        except Exception:
            continue

    return False, 0.0


# ---------------------------------
# Confidence — normalized signal agreement
# ---------------------------------

def _table_confidence(
    pipe: float,
    aligned: float,
    stable: float,
    has_separator_or_pipe: bool,
) -> float:
    """
    passed_signals / total_relevant_signals
    """
    signals = [
        pipe >= PIPE_DENSITY_THRESHOLD,
        aligned >= ALIGNED_ROW_RATIO_THRESHOLD,
        stable >= TOKEN_STABILITY_THRESHOLD,
        has_separator_or_pipe,
    ]
    return round(sum(signals) / len(signals), 2)


def _structured_confidence(
    kv: float,
    delim: float,
    shape: float,
) -> float:
    signals = [
        kv >= KV_DENSITY_THRESHOLD,
        delim >= DELIMITER_RATIO_THRESHOLD,
        shape >= SHAPE_REPETITION_THRESHOLD,
    ]
    return round(sum(signals) / len(signals), 2)


# ---------------------------------
# Classification logic
# ---------------------------------

def _classify(lines: List[LineObject]) -> Tuple[str, float]:
    """
    Returns (region_type, confidence).
    All decisions based on locked thresholds and size gates.
    """
    total = len(lines)
    ne    = _non_empty(lines)

    pipe    = _pipe_density(lines)
    stable  = _token_count_stability(lines)
    aligned = _column_alignment(lines)
    kv      = _kv_density(lines)
    delim   = _delimiter_ratio(lines)
    shape   = _shape_repetition(lines)

    has_sep_or_pipe = _has_structural_separator(lines) or _has_pipe(lines)

    # --- CSV table candidate ---
    # CSV has no pipes, no space alignment, no separators.
    # Detected purely by consistent column count via csv.reader
    # (which handles quoted fields containing the delimiter).
    # Checked first — bypasses the has_sep_or_pipe structural gate.
    is_csv, csv_conf = _csv_column_consistency(lines)
    if is_csv and len(ne) >= MIN_TABLE_LINES:
        return "table_candidate", csv_conf

    # --- table_candidate ---
    # Size gate: minimum 3 non-empty lines
    # Structural gate: must have pipe or separator — prevents ghost tables
    if len(ne) >= MIN_TABLE_LINES and has_sep_or_pipe:
        if pipe >= PIPE_DENSITY_THRESHOLD:
            conf = _table_confidence(pipe, aligned, stable, has_sep_or_pipe)
            return "table_candidate", conf

        if (aligned >= ALIGNED_ROW_RATIO_THRESHOLD
                and stable >= TOKEN_STABILITY_THRESHOLD):
            conf = _table_confidence(pipe, aligned, stable, has_sep_or_pipe)
            return "table_candidate", conf

    # --- glyph-based tree candidate ---
    # Detects +-- ├── └── style trees
    # Independent of pipe_density and space alignment
    glyph = _glyph_line_density(lines)
    if glyph >= 0.30 and len(ne) >= MIN_TABLE_LINES:
        conf = round(glyph, 2)
        return "structured_block", conf

    # --- space-aligned table candidate ---
    # Detects thread dumps, metrics tables without pipe borders
    multi_space = _multi_space_ratio(lines)
    if (stable >= 0.60 and
            multi_space >= 0.50 and
            len(ne) >= MIN_TABLE_LINES):
        conf = _table_confidence(0.0, stable, stable, False)
        return "table_candidate", conf

    # --- structured_block ---
    # Size gate: minimum 2 non-empty lines
    if len(ne) >= MIN_STRUCTURED_LINES:
        if kv >= KV_DENSITY_THRESHOLD:
            conf = _structured_confidence(kv, delim, shape)
            return "structured_block", conf

        if (delim >= DELIMITER_RATIO_THRESHOLD
                and shape >= SHAPE_REPETITION_THRESHOLD):
            conf = _structured_confidence(kv, delim, shape)
            return "structured_block", conf

    # --- unstructured fallback ---
    strongest = max(pipe, aligned, kv, delim)
    confidence = round(1.0 - strongest, 2)
    return "unstructured", confidence


# ---------------------------------
# Core
# ---------------------------------

def classify_region(lines: List[LineObject]) -> RegionResult:
    """
    Classify a list of LineObjects as a single structural region.
    Input is a pre-segmented block of lines from one segment.
    """
    if not lines:
        return RegionResult(
            region_type="unstructured",
            start_line=0,
            end_line=0,
            confidence=0.0,
        )

    region_type, confidence = _classify(lines)

    return RegionResult(
        region_type=region_type,
        start_line=lines[0]["line_index"],
        end_line=lines[-1]["line_index"],
        confidence=confidence,
    )


# ---------------------------------
# Interactive Test
# ---------------------------------

if __name__ == "__main__":
    import sys
    from analysis.structure_engine.line_model import build_line_model

    print("\n" + "=" * 60)
    print("REGION CLASSIFIER INTERACTIVE TEST")
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
        result = classify_region(model)

        print("\n--- REGION CLASSIFICATION ---\n")
        print(f"  region_type : {result['region_type']}")
        print(f"  start_line  : {result['start_line']}")
        print(f"  end_line    : {result['end_line']}")
        print(f"  confidence  : {result['confidence']}")
        print(f"\n  --- Signal Debug ---")

        ne = [l for l in model if not l["is_empty"]]
        print(f"  non_empty_lines      : {len(ne)}")
        print(f"  pipe_density         : {_pipe_density(model):.2f}")
        print(f"  token_stability      : {_token_count_stability(model):.2f}")
        print(f"  column_alignment     : {_column_alignment(model):.2f}")
        print(f"  kv_density           : {_kv_density(model):.2f}")
        print(f"  delimiter_ratio      : {_delimiter_ratio(model):.2f}")
        print(f"  shape_repetition     : {_shape_repetition(model):.2f}")
        print(f"  has_sep_or_pipe      : {_has_structural_separator(model) or _has_pipe(model)}")
        print("-" * 60)

# TEMP DEBUG — remove after diagnosis
_orig_csv_check = _csv_column_consistency
def _csv_column_consistency_debug(lines):
    import sys
    ne = [l["normalized"] for l in lines if not l["is_empty"]]
    result, conf = _orig_csv_check(lines)
    if ne:
        print(f"[CSV_DEBUG] ne={len(ne)} first={repr(ne[0][:80])} is_csv={result}", file=sys.stderr)
    return result, conf
_csv_column_consistency = _csv_column_consistency_debug
