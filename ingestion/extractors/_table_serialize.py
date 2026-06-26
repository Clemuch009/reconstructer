# ingestion/extractors/_table_serialize.py
"""
Single source of truth for serializing extracted table rows to delimited text.

Every extractor that turns a structured table (docx tables, pdf tables, xlsx
sheets, html <table>) into the plain text the engine consumes MUST route through
rows_to_delimited_text() instead of doing an ad-hoc ",".join(cells).

Why this exists
---------------
A naive ",".join(cells) breaks whenever a cell legitimately contains the
delimiter — e.g. a number like "142,500" or a phrase like "market analysis,
risk". Two failure modes were observed in the wild:

  1. Column-count corruption: "North America,142,500,94%" parses as 4 fields,
     not 3, so the row's column count no longer matches the header. The engine's
     table detector requires consistent column counts, so the block is rejected
     and rendered as prose.

  2. Data corruption via comma->semicolon substitution: some extractors tried to
     dodge (1) by replacing "," with ";" inside cells, turning "142,500" into
     "142;500" — silently changing the data.

csv.writer with QUOTE_MINIMAL solves both correctly: a cell containing the
delimiter (or a quote, or a newline) is wrapped in quotes, e.g.
  142,500  ->  "142,500"
so the column count stays correct AND the original value is preserved. The
engine's CSV detector uses csv.reader, which round-trips the quoting perfectly.
"""

import csv
import io
from typing import Optional, Sequence


def rows_to_delimited_text(
    rows: Sequence[Sequence[object]],
    delimiter: str = ",",
) -> str:
    """
    Serialize table rows to delimited text with correct quoting.

    - Cells containing the delimiter / quotes / newlines are quoted (QUOTE_MINIMAL)
      so column counts stay consistent and no data is mangled.
    - None cells become empty strings; all cells coerced to str.
    - Empty rows (all cells blank) are skipped.
    - Newlines inside a cell are flattened to a space (a hard newline inside a
      cell would otherwise be read as a row break by the line-based engine).
    - Returns "" for empty input.
    """
    if not rows:
        return ""

    out = io.StringIO()
    writer = csv.writer(
        out,
        delimiter=delimiter,
        quoting=csv.QUOTE_MINIMAL,
        lineterminator="\n",
    )

    wrote_any = False
    for row in rows:
        cells = [
            ("" if c is None else str(c)).replace("\n", " ").replace("\r", " ").strip()
            for c in row
        ]
        if not any(cells):
            continue
        writer.writerow(cells)
        wrote_any = True

    if not wrote_any:
        return ""

    return out.getvalue().strip()
