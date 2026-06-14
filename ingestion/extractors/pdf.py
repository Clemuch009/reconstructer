# ingestion/extractors/pdf.py

import io
from typing import Optional
from ingestion.result import ExtractionResult, ExtractionMetadata


# ---------------------------------
# PDF extractor
# Requires: pdfplumber
# pip install pdfplumber
# ---------------------------------

def extract_pdf(raw_bytes: bytes) -> ExtractionResult:
    """
    PDF extractor using pdfplumber.

    Pipeline:
    1. Open PDF from bytes
    2. Extract text page by page
    3. Extract tables per page — render as CSV text
    4. Interleave text and tables in document order
    5. Return single text block — engine sees one document

    Page format:
        [PAGE: 1]
        <extracted text>
        <extracted tables as CSV>

        [PAGE: 2]
        ...

    Rules:
    - Never raises — returns extraction_success: False on failure
    - Tables extracted as CSV text — no pipe conversion
    - Empty pages skipped with warning
    - Encrypted PDFs warned and skipped
    - pdfplumber import failure returns actionable warning
    """
    warnings: list[str] = []

    # Guard — pdfplumber optional dependency
    try:
        import pdfplumber
    except ImportError:
        return ExtractionResult(
            text="",
            metadata=ExtractionMetadata(
                source_format="pdf",
                page_count=None,
                sheet_count=None,
                char_count=0,
                line_count=0,
                word_count=0,
                encoding_used=None,
            ),
            extraction_warnings=[
                "pdfplumber not installed. "
                "Run: pip install pdfplumber"
            ],
            extraction_success=False,
        )

    page_blocks: list[str] = []
    page_count:  int = 0

    try:
        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            page_count = len(pdf.pages)

            # Encrypted PDF check
            if pdf.doc.is_encrypted:
                return ExtractionResult(
                    text="",
                    metadata=ExtractionMetadata(
                        source_format="pdf",
                        page_count=page_count,
                        sheet_count=None,
                        char_count=0,
                        line_count=0,
                        word_count=0,
                        encoding_used=None,
                    ),
                    extraction_warnings=[
                        "PDF is encrypted — cannot extract text. "
                        "Decrypt before ingestion."
                    ],
                    extraction_success=False,
                )

            for page_num, page in enumerate(pdf.pages, start=1):
                page_parts: list[str] = []

                # Extract raw text
                page_text = page.extract_text()
                if page_text and page_text.strip():
                    page_parts.append(page_text.strip())
                else:
                    warnings.append(
                        f"Page {page_num}: no text extracted — "
                        "may be image-based (OCR not supported)"
                    )

                # Extract tables — render as CSV text
                tables = page.extract_tables()
                for table_idx, table in enumerate(tables, start=1):
                    if not table:
                        continue
                    table_lines: list[str] = []
                    for row in table:
                        if row is None:
                            continue
                        cleaned = [
                            (cell or "").strip().replace("\n", " ")
                            for cell in row
                        ]
                        if any(cleaned):
                            table_lines.append(",".join(cleaned))
                    if table_lines:
                        page_parts.append("\n".join(table_lines))

                if page_parts:
                    block = f"[PAGE: {page_num}]\n" + "\n\n".join(page_parts)
                    page_blocks.append(block)

    except Exception as e:
        return ExtractionResult(
            text="",
            metadata=ExtractionMetadata(
                source_format="pdf",
                page_count=page_count,
                sheet_count=None,
                char_count=0,
                line_count=0,
                word_count=0,
                encoding_used=None,
            ),
            extraction_warnings=[f"PDF extraction failed: {str(e)[:200]}"],
            extraction_success=False,
        )

    text  = "\n\n".join(page_blocks)
    lines = text.splitlines()
    words = text.split()

    if not text.strip():
        warnings.append(
            "No text extracted from any page — "
            "PDF may be entirely image-based"
        )

    return ExtractionResult(
        text=text,
        metadata=ExtractionMetadata(
            source_format="pdf",
            page_count=page_count,
            sheet_count=None,
            char_count=len(text),
            line_count=len(lines),
            word_count=len(words),
            encoding_used=None,   # binary format — no text encoding
        ),
        extraction_warnings=warnings,
        extraction_success=bool(text.strip()),
    )
