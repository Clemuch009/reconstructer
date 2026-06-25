# ingestion/extractors/csv.py

import csv
import io
from typing import Optional
from ingestion.result import ExtractionResult, ExtractionMetadata
from ingestion.extractors.txt import extract_txt

import sys
print("Cvs working")
# ---------------------------------
# Dialect detection
# ---------------------------------
print("new version")
def _detect_dialect(
    text: str,
) -> tuple[csv.Dialect, list[str]]:
    """
    Detect CSV dialect (delimiter, quotechar, line terminator).
    Falls back to excel dialect on detection failure.

    Returns (dialect, warnings).
    """
    warnings: list[str] = []
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        return dialect, warnings
    except csv.Error:
        warnings.append(
            "Dialect detection failed — falling back to comma delimiter"
        )
        return csv.excel, warnings


# ---------------------------------
# Row parser
# ---------------------------------

def _parse_rows(
    text:    str,
    dialect: csv.Dialect,
) -> tuple[list[list[str]], list[str]]:
    """
    Parse CSV text into rows.
    Strips whitespace from each cell.
    Records warnings on malformed rows.

    Returns (rows, warnings).
    """
    warnings: list[str] = []
    rows:     list[list[str]] = []

    reader = csv.reader(io.StringIO(text), dialect=dialect)

    expected_cols: Optional[int] = None

    for line_num, row in enumerate(reader, start=1):
        # Skip completely empty rows
        if not any(cell.strip() for cell in row):
            continue

        cleaned = [cell.strip() for cell in row]

        # Track column consistency
        if expected_cols is None:
            expected_cols = len(cleaned)
        elif len(cleaned) != expected_cols:
            warnings.append(
                f"Row {line_num}: expected {expected_cols} columns, "
                f"got {len(cleaned)} — ragged row detected"
            )

        rows.append(cleaned)

    return rows, warnings


# ---------------------------------
# Rows → plain text reconstruction
# ---------------------------------

def _rows_to_text(rows: list[list[str]], delimiter: str = ",") -> str:
    """
    Reconstruct CSV rows as plain text using csv.writer.

    Rules:
    - Fields containing the delimiter are re-quoted automatically
      by csv.writer — this preserves column count consistency so
      the engine correctly classifies the output as a table
    - Header row preserved as first line
    - Each row on its own line
    - Empty result returns empty string

    Without re-quoting, a field like:
      "Data-driven diagnostics, market analysis, risk..."
    becomes:
      Data-driven diagnostics, market analysis, risk...
    which adds spurious commas → ragged rows → engine misclassifies
    as prose instead of table.
    """
    if not rows:
        return ""

    output = io.StringIO()
    writer = csv.writer(
        output,
        delimiter=delimiter,
        quoting=csv.QUOTE_MINIMAL,
        lineterminator="\n",
    )
    writer.writerows(rows)
    return output.getvalue().strip()


# ---------------------------------
# CSV extractor
# ---------------------------------

def extract_csv(raw_bytes: bytes) -> ExtractionResult:
    """
    CSV extractor.

    Pipeline:
    1. Decode bytes via txt extractor (full encoding chain)
    2. Detect dialect (delimiter, quotechar)
    3. Parse rows — track ragged rows as warnings
    4. Reconstruct as plain CSV text — no format conversion
    5. Return ExtractionResult — engine sees clean CSV text

    Rules:
    - Never raises
    - No pipe conversion — engine receives raw CSV text
    - Ragged rows warned, not dropped
    - Encoding handled entirely by txt extractor
    - Empty file sets extraction_success: False
    """
    # Step 1 — decode via txt extractor
    txt_result = extract_txt(raw_bytes)
    warnings   = list(txt_result["extraction_warnings"])
    text       = txt_result["text"]

    if not txt_result["extraction_success"] or not text.strip():
        return ExtractionResult(
            text="",
            metadata=ExtractionMetadata(
                source_format="csv",
                page_count=None,
                sheet_count=None,
                char_count=0,
                line_count=0,
                word_count=0,
                encoding_used=txt_result["metadata"]["encoding_used"],
            ),
            extraction_warnings=warnings + ["CSV file is empty"],
            extraction_success=False,
        )

    # Step 2 — detect dialect
    dialect, dialect_warnings = _detect_dialect(text)
    warnings.extend(dialect_warnings)

    # Step 3 — parse rows
    rows, row_warnings = _parse_rows(text, dialect)
    warnings.extend(row_warnings)

    # Step 4 — reconstruct as plain CSV text (re-quoting fields with delimiters)
    delimiter  = getattr(dialect, "delimiter", ",")
    clean_text = _rows_to_text(rows, delimiter=delimiter)

    lines  = clean_text.splitlines()
    words  = clean_text.split()

    return ExtractionResult(
        text=clean_text,
        metadata=ExtractionMetadata(
            source_format="csv",
            page_count=None,
            sheet_count=None,
            char_count=len(clean_text),
            line_count=len(lines),
            word_count=len(words),
            encoding_used=txt_result["metadata"]["encoding_used"],
        ),
        extraction_warnings=warnings,
        extraction_success=bool(clean_text.strip()),
    )
