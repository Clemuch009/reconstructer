# ingestion/extractors/docx.py

import io
from ingestion.result import ExtractionResult, ExtractionMetadata

_HEADING_STYLES = {
    "heading 1", "heading 2", "heading 3",
    "heading 4", "heading 5", "heading 6",
    "title", "subtitle",
}


def _table_to_csv(table) -> str:
    # Serialize via the shared helper, which quotes cells containing commas
    # (e.g. "142,500") so column counts stay consistent and values are not
    # corrupted. Previously this did .replace(",", ";") + ",".join(), which
    # both mangled the data (142,500 -> 142;500) and could still break columns.
    from ingestion.extractors._table_serialize import rows_to_delimited_text
    rows = [[cell.text for cell in row.cells] for row in table.rows]
    return rows_to_delimited_text(rows)


def _get_image_alt(shape) -> str:
    """
    Extract alt-text description from an inline image shape.
    python-docx exposes this via the docPr XML element's 'descr' attribute.
    Falls back to 'name' attribute (usually "Picture 1" etc.) if no description.
    Returns empty string if neither is available.
    """
    try:
        from docx.oxml.ns import qn
        # Inline images: shape._inline.docPr
        doc_pr = shape._inline.find(qn("wp:docPr"))
        if doc_pr is not None:
            descr = doc_pr.get("descr", "").strip()
            if descr:
                return descr[:200]
            name = doc_pr.get("name", "").strip()
            if name:
                return name
    except Exception:
        pass
    return ""


def extract_docx(raw_bytes: bytes) -> ExtractionResult:
    """
    DOCX extractor using python-docx.

    Pipeline:
    1. Open DOCX from bytes
    2. Walk doc.paragraphs and doc.tables in document order
       using iter_block_items — stable public API
    3. Headings preserved as text — engine detects hierarchy
    4. Tables rendered as CSV text
    5. Inline images detected — [IMAGE_N: alt-text] markers inserted
       at the position where the image appears in the document flow
    6. Return single text block

    Image markers:
    - Inserted inline at image position in document flow
    - Alt-text extracted from wp:docPr descr attribute
    - Falls back to shape name (e.g. "Picture 1") if no alt-text
    - [IMAGE_N: no description] if neither available

    Rules:
    - Never raises
    - Uses stable python-docx API only — no lxml traversal beyond docPr
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
    image_counter: int = 0

    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    def iter_block_items(document):
        parent_elm = document.element.body
        for child in parent_elm.iterchildren():
            if child.tag == qn("w:p"):
                yield Paragraph(child, document)
            elif child.tag == qn("w:tbl"):
                yield Table(child, document)

    for block in iter_block_items(doc):
        if isinstance(block, Paragraph):
            # Check for inline images in this paragraph
            # Inline images are in runs as drawing elements
            try:
                from docx.oxml.ns import qn as _qn
                for run in block.runs:
                    drawings = run._element.findall(
                        f".//{_qn('w:drawing')}"
                    )
                    for drawing in drawings:
                        # Find inline shape
                        inline = drawing.find(_qn("wp:inline"))
                        if inline is not None:
                            image_counter += 1
                            # Extract alt text from docPr
                            doc_pr = inline.find(_qn("wp:docPr"))
                            alt = ""
                            if doc_pr is not None:
                                alt = doc_pr.get("descr", "").strip()
                                if not alt:
                                    alt = doc_pr.get("name", "").strip()
                            label = alt[:200] if alt else "no description"
                            parts.append(
                                f"[IMAGE_{image_counter}: {label}]"
                            )
            except Exception:
                pass  # image detection never blocks text extraction

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

    if image_counter > 0:
        warnings.append(
            f"{image_counter} image(s) detected — "
            "inserted as [IMAGE_N: alt-text] markers. "
            "Image content not extracted (OCR not enabled)."
        )

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
