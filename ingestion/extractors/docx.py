# ingestion/extractors/docx.py

import io
from ingestion.result import ExtractionResult, ExtractionMetadata

_HEADING_STYLES = {
    "heading 1", "heading 2", "heading 3",
    "heading 4", "heading 5", "heading 6",
    "title", "subtitle",
}


def _table_to_csv(table) -> str:
    lines: list[str] = []
    for row in table.rows:
        cells = [
            cell.text.strip().replace("\n", " ").replace(",", ";")
            for cell in row.cells
        ]
        if any(cells):
            lines.append(",".join(cells))
    return "\n".join(lines)


def extract_docx(raw_bytes: bytes) -> ExtractionResult:
    """
    DOCX extractor using python-docx.

    Pipeline:
    1. Open DOCX from bytes
    2. Walk doc.paragraphs and doc.tables in document order
       using iter_block_items — stable public API
    3. Headings preserved as text — engine detects hierarchy
    4. Tables rendered as CSV text
    5. Return single text block

    Rules:
    - Never raises
    - Uses stable python-docx API only — no lxml traversal
    - Tables rendered as CSV — no pipe conversion
    - Page count not available in DOCX — None
    """
    warnings: list[str] = []

    try:
        from docx import Document
    except ImportError:
        return ExtractionResult(
            text="",
            metadata=ExtractionMetadata(
                source_format="docx",
                page_count=None,
                sheet_count=None,
                char_count=0,
                line_count=0,
                word_count=0,
                encoding_used=None,
            ),
            extraction_warnings=[
                "python-docx not installed. "
                "Run: pip install python-docx"
            ],
            extraction_success=False,
        )

    try:
        doc = Document(io.BytesIO(raw_bytes))
    except Exception as e:
        return ExtractionResult(
            text="",
            metadata=ExtractionMetadata(
                source_format="docx",
                page_count=None,
                sheet_count=None,
                char_count=0,
                line_count=0,
                word_count=0,
                encoding_used=None,
            ),
            extraction_warnings=[f"DOCX open failed: {str(e)[:200]}"],
            extraction_success=False,
        )

    parts: list[str] = []

    # ---------------------------------
    # iter_block_items — stable API
    # Yields paragraphs and tables in document order
    # Source: python-docx official recipe
    # ---------------------------------
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    def iter_block_items(document):
        """
        Yield Paragraph and Table objects in document order.
        Uses parent.iterchildren() on document body — stable,
        version-safe approach from python-docx documentation.
        """
        parent_elm = document.element.body
        for child in parent_elm.iterchildren():
            if child.tag == qn("w:p"):
                yield Paragraph(child, document)
            elif child.tag == qn("w:tbl"):
                yield Table(child, document)

    for block in iter_block_items(doc):
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if not text:
                continue
            parts.append(text)

        elif isinstance(block, Table):
            csv_text = _table_to_csv(block)
            if csv_text.strip():
                parts.append(csv_text)
            else:
                warnings.append("Empty table skipped")

    text  = "\n\n".join(parts)
    lines = text.splitlines()
    words = text.split()

    if not text.strip():
        warnings.append("No text extracted from DOCX")

    return ExtractionResult(
        text=text,
        metadata=ExtractionMetadata(
            source_format="docx",
            page_count=None,
            sheet_count=None,
            char_count=len(text),
            line_count=len(lines),
            word_count=len(words),
            encoding_used=None,
        ),
        extraction_warnings=warnings,
        extraction_success=bool(text.strip()),
    )
