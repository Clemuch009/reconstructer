# ingestion/extractors/pdf_columns.py
#
# Reading-order recovery for multi-column PDF pages.
#
# THE PROBLEM (a representation bug, not an extraction bug):
# pdfplumber's extract_text() groups words into lines by their vertical
# position. When a page has two columns, words at the same height from
# DIFFERENT columns share a line and get concatenated left-to-right:
#
#     "Vertex Creative Agency"  +  "Invoice ID: VCA-992"
#        (left column)              (right column, same y)
#   →  "Vertex Creative Agency Invoice ID: VCA-992"
#
# The key-value "Invoice ID: VCA-992" is now buried mid-line; the kv detector
# can't find it, and the page collapses to prose. The structure was thrown away
# at flattening time and can't be recovered downstream.
#
# THE FIX (structure preservation, per the redesign principle):
# Use the word bounding boxes pdfplumber already provides to recover reading
# ORDER before emitting text. Detect a vertical gap that splits words into
# columns; if found, emit the left column's lines, then the right column's —
# so each column's key-values land on their own lines. Single-column pages have
# no such gap and are emitted unchanged (the caller falls back to extract_text).
#
# This changes ONE parser. It does not touch the core, the detectors, or any
# other format. Coordinates → reading order → logical lines → text.

from typing import Any, Dict, List, Optional, Tuple


# A column gap must be at least this wide (in points) to count as a real column
# boundary rather than ordinary inter-word spacing. ~40pt is wider than any
# normal word gap but narrower than a real two-column layout's gutter.
MIN_COLUMN_GAP = 40.0

# Words within this vertical distance are considered the same visual line.
LINE_TOLERANCE = 3.0


def _group_into_lines(words: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Group words into visual lines by their `top` coordinate."""
    if not words:
        return []
    ordered = sorted(words, key=lambda w: (round(w["top"], 1), w["x0"]))
    lines: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = [ordered[0]]
    current_top = ordered[0]["top"]
    for w in ordered[1:]:
        if abs(w["top"] - current_top) <= LINE_TOLERANCE:
            current.append(w)
        else:
            lines.append(current)
            current = [w]
            current_top = w["top"]
    lines.append(current)
    return lines


def detect_column_split(
    words: List[Dict[str, Any]],
    page_width: float,
    exclude_bands: Optional[List[Tuple[float, float]]] = None,
) -> Optional[float]:
    """
    Decide whether the page has a two-column layout, and if so return the
    x-coordinate of the column boundary. Returns None for single-column pages.

    Method (per-line, not page-global): a two-column layout shows up as the
    SAME horizontal gap appearing inside MANY separate visual lines. We find,
    per line, any wide internal gap in the central region, then take the
    boundary that recurs across the most lines. This is robust to full-width
    lines (titles, totals) that don't have the gap — they simply don't vote.

    `exclude_bands` are (top, bottom) y-ranges to ignore (e.g. detected table
    regions), so a table's inter-column gaps don't masquerade as the page's
    column boundary.
    """
    if len(words) < 6:
        return None

    lines = _group_into_lines(words)
    left_bound  = page_width * 0.20
    right_bound = page_width * 0.80

    # Count, per candidate boundary (bucketed to 10pt), how many DISTINCT lines
    # contain a wide central gap centered there.
    from collections import Counter
    votes: Counter = Counter()
    voting_lines: Dict[int, int] = {}

    for ln in lines:
        # skip lines inside excluded (table) bands
        if exclude_bands:
            ltop = min(w["top"] for w in ln)
            if any(tb <= ltop <= bb for tb, bb in exclude_bands):
                continue
        ws = sorted(ln, key=lambda w: w["x0"])
        # only the FIRST wide central gap in a line counts as its column split
        for i in range(len(ws) - 1):
            gap = ws[i + 1]["x0"] - ws[i]["x1"]
            center = (ws[i + 1]["x0"] + ws[i]["x1"]) / 2
            if gap >= MIN_COLUMN_GAP and left_bound <= center <= right_bound:
                bucket = round(center / 10) * 10
                votes[bucket] += 1
                break

    if not votes:
        return None

    boundary, count = votes.most_common(1)[0]
    # Require the boundary to appear in at least 2 distinct lines — a single
    # gap is just spacing, not a column layout.
    if count < 2:
        return None

    # Require substantial words on both sides across the whole page.
    left_words  = sum(1 for w in words if w["x1"] <= boundary)
    right_words = sum(1 for w in words if w["x0"] >= boundary)
    if left_words < 3 or right_words < 3:
        return None

    return float(boundary)


def _line_text(line: List[Dict[str, Any]]) -> str:
    """Join a visual line's words left-to-right into text."""
    return " ".join(w["text"] for w in sorted(line, key=lambda w: w["x0"])).strip()


def _detect_table_band(lines: List[List[Dict[str, Any]]]) -> Optional[Tuple[float, float]]:
    """Detect a line-item table region by its header row and return (top, bottom)
    of the band to protect from column splitting.

    Header detection: a line containing several column-header keywords
    (Description/Item, Quantity/Qty, Price/Rate/Unit, Amount/Total). The band
    runs from that header's top down to the first totals line (Subtotal / Tax /
    Total) or the end of the page. Conservative: only fires on a clear header.
    """
    HEADER_WORDS = {"description", "item", "items", "quantity", "qty", "price",
                    "rate", "unit", "amount", "total", "cost", "service",
                    "charge", "value", "net", "sum"}
    TOTALS_WORDS = ("subtotal", "sub-total", "tax", "total", "grand total",
                    "balance", "amount due")
    _NUMERIC = __import__("re").compile(r"[\$£€¥]|\d")

    def _is_header_row(text: str) -> bool:
        """A table header row is PURE LABELS: it contains >=2 column keywords,
        those keywords make up at least half its tokens, and — the key
        discriminator — it carries NO numeric or currency value. That last rule
        is what keeps a totals line ('Total Amount Due: $500', which also has 2
        keywords) from being mistaken for a header."""
        toks = text.split()
        if not toks or len(toks) > 8:
            return False
        if _NUMERIC.search(text):
            return False
        hits = sum(1 for t in toks if t.strip(":,") in HEADER_WORDS)
        return hits >= 2 and hits / len(toks) >= 0.5

    header_top = None
    for ln in lines:
        text = " ".join(w["text"] for w in ln).lower()
        if header_top is None and _is_header_row(text):
            header_top = min(w["top"] for w in ln)
            continue
        if header_top is not None:
            # end the band at the first totals line
            if any(tw in text for tw in TOTALS_WORDS):
                bottom = min(w["top"] for w in ln) - 1
                return (header_top, bottom)

    if header_top is not None:
        # no totals line found → band runs to the end of the page
        last_bottom = max(max(w["bottom"] for w in ln) for ln in lines)
        return (header_top, last_bottom)
    return None


def reconstruct_reading_order(
    words: List[Dict[str, Any]],
    page_width: float,
    exclude_bands: Optional[List[Tuple[float, float]]] = None,
) -> Optional[str]:
    """
    Recover reading-order text for a page from its word boxes.

    Returns column-corrected text if a two-column layout is detected, else None
    (signalling the caller to use pdfplumber's normal extract_text()).

    Reconstruction is done PER VISUAL LINE, top to bottom, so the vertical flow
    of the document is preserved:
      • a line that straddles the column boundary (words on both sides) is a
        two-column line → emit its left part, then its right part, as two lines.
      • a line wholly on one side, or spanning the boundary as one unit (a
        full-width title, or a table row inside an excluded band) → emit as-is.
    This keeps key-values in each column on their own lines WITHOUT scrambling
    full-width rows or table content.
    """
    boundary = detect_column_split(words, page_width, exclude_bands=exclude_bands)
    if boundary is None:
        return None  # single column — caller uses default extraction

    lines = _group_into_lines(words)

    # Auto-detect a borderless line-item TABLE region and protect it from column
    # splitting. A table below the two-column header (e.g. "Description Quantity
    # Rate Amount ... Subtotal") would otherwise be split at the same boundary,
    # scrambling its rows. find_tables() misses borderless tables, so we detect
    # the region here: from a header row of column keywords down to a totals
    # line. Rows in that band are emitted whole so the table extractor sees them.
    auto_bands = list(exclude_bands or [])
    table_band = _detect_table_band(lines)
    if table_band:
        auto_bands.append(table_band)

    out: List[str] = []
    prev_in_band = False

    for ln in lines:
        ltop = min(w["top"] for w in ln)
        in_excluded = bool(auto_bands) and any(tb <= ltop <= bb for tb, bb in auto_bands)

        # Insert a blank line at the boundary of the table band so downstream
        # segmentation splits the table into its own segment instead of merging
        # it into the surrounding kv block (which would hide it from the table
        # detector). Blank on entering AND leaving the band.
        if in_excluded != prev_in_band:
            out.append("")
        prev_in_band = in_excluded

        ws = sorted(ln, key=lambda w: w["x0"])
        left  = [w for w in ws if w["x1"] <= boundary]
        right = [w for w in ws if w["x0"] >= boundary]
        straddle = [w for w in ws if w["x0"] < boundary < w["x1"]]

        # A line is "two-column" only if it has words clearly on BOTH sides and
        # is not in an excluded (table) band and has no boundary-straddling word
        # (a straddling word means it's really one full-width unit).
        is_two_col = (not in_excluded and left and right and not straddle)

        if is_two_col:
            out.append(_line_text(left))
            out.append(_line_text(right))
        else:
            out.append(_line_text(ws))

    # collapse multiple blank lines but PRESERVE single blanks (segment breaks)
    cleaned: List[str] = []
    for t in out:
        if t == "" and (not cleaned or cleaned[-1] == ""):
            continue
        cleaned.append(t)
    return "\n".join(cleaned).strip() or None
