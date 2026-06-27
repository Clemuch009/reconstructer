# ingestion/extractors/docx.py

import io
from typing import List, Optional
from ingestion.result import ExtractionResult, ExtractionMetadata
from ingestion.visual import EmbeddedVisual, make_visual_id, visual_placeholder

_HEADING_STYLES = {
    "heading 1", "heading 2", "heading 3",
    "heading 4", "heading 5", "heading 6",
    "title", "subtitle",
}

# MIME type map from OOXML relationship content types
_MIME_MAP = {
    "image/png":  "image/png",
    "image/jpeg": "image/jpeg",
    "image/jpg":  "image/jpeg",
    "image/gif":  "image/gif",
    "image/bmp":  "image/bmp",
    "image/tiff": "image/tiff",
    "image/svg+xml": "image/svg+xml",
    "image/x-emf": "image/x-emf",
    "image/x-wmf": "image/x-wmf",
}


def _table_to_csv(table) -> str:
    lines: List[str] = []
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
    2. Walk doc body in document order (paragraphs + tables)
    3. For each paragraph:
       - Detect inline drawings → extract image bytes from relationship parts
       - Insert [VISUAL: vis_N] placeholder at image position
       - Extract paragraph text
    4. Tables rendered as CSV
    5. Return text + visuals[]

    Visual extraction:
    - Finds w:drawing → wp:inline elements in paragraph runs
    - Reads r:embed from a:blip to get relationship ID
    - Loads image bytes from word/media/ via part.related_parts
    - Width/height from wp:extent (EMUs → pixels at 96dpi)
    """
    warnings:  List[str] = []
    visuals:   List[EmbeddedVisual] = []
    vis_index: int = 0

    try:
        from docx import Document
    except ImportError:
        return ExtractionResult(
            text="", visuals=[],
            metadata=ExtractionMetadata(
                source_format="docx", page_count=None, sheet_count=None,
                char_count=0, line_count=0, word_count=0, encoding_used=None,
            ),
            extraction_warnings=["python-docx not installed. Run: pip install python-docx"],
            extraction_success=False,
        )

    try:
        doc = Document(io.BytesIO(raw_bytes))
    except Exception as e:
        return ExtractionResult(
            text="", visuals=[],
            metadata=ExtractionMetadata(
                source_format="docx", page_count=None, sheet_count=None,
                char_count=0, line_count=0, word_count=0, encoding_used=None,
            ),
            extraction_warnings=[f"DOCX open failed: {str(e)[:200]}"],
            extraction_success=False,
        )

    parts: List[str] = []

    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    # EMU → pixels at 96dpi
    EMU_PER_PIXEL = 914400 / 96

    def _extract_visual_from_drawing(drawing_elem, doc_part) -> Optional[EmbeddedVisual]:
        """
        Extract image bytes from a w:drawing element.
        Returns EmbeddedVisual or None if extraction fails.
        """
        nonlocal vis_index

        try:
            # Find a:blip for the relationship ID
            blip = drawing_elem.find(".//" + qn("a:blip"))
            if blip is None:
                return None

            r_embed = blip.get(qn("r:embed"))
            if not r_embed:
                return None

            # Get image part via relationship
            img_part = doc_part.related_parts.get(r_embed)
            if img_part is None:
                return None

            img_bytes = img_part.blob
            if not img_bytes:
                return None

            # Get dimensions from wp:extent (EMUs)
            extent = drawing_elem.find(".//" + qn("wp:extent"))
            width  = None
            height = None
            if extent is not None:
                cx = extent.get("cx")
                cy = extent.get("cy")
                if cx:
                    width  = int(int(cx) / EMU_PER_PIXEL)
                if cy:
                    height = int(int(cy) / EMU_PER_PIXEL)

            # MIME from content type
            ct = getattr(img_part, "content_type", "image/png")
            mime = _MIME_MAP.get(ct, ct if ct.startswith("image/") else "image/png")

            vis_index += 1
            return EmbeddedVisual(
                id=make_visual_id(vis_index),
                page=None,
                mime_type=mime,
                width=width,
                height=height,
                image_bytes=img_bytes,
                warnings=[],
            )

        except Exception as e:
            warnings.append(f"Visual extraction failed: {str(e)[:100]}")
            return None

    def iter_block_items(document):
        parent_elm = document.element.body
        for child in parent_elm.iterchildren():
            if child.tag == qn("w:p"):
                yield Paragraph(child, document)
            elif child.tag == qn("w:tbl"):
                yield Table(child, document)

    for block in iter_block_items(doc):
        if isinstance(block, Paragraph):
            # Check for drawings in this paragraph's runs
            for run in block.runs:
                for drawing in run._element.findall(".//" + qn("w:drawing")):
                    visual = _extract_visual_from_drawing(drawing, doc.part)
                    if visual:
                        visuals.append(visual)
                        parts.append(visual_placeholder(visual["id"]))

            text = block.text.strip()
            if text:
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

    if visuals:
        warnings.append(
            f"{len(visuals)} visual(s) extracted — "
            "available in result.visuals[] as raw bytes."
        )

    return ExtractionResult(
        text=text,
        visuals=visuals,
        metadata=ExtractionMetadata(
            source_format="docx", page_count=None, sheet_count=None,
            char_count=len(text), line_count=len(lines), word_count=len(words),
            encoding_used=None,
        ),
        extraction_warnings=warnings,
        extraction_success=bool(text.strip()),
    )
