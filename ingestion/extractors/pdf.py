# ingestion/extractors/pdf.py

import io
from typing import Optional
from ingestion.result import ExtractionResult, ExtractionMetadata


def _get_image_caption(page, img_bbox: tuple, page_text: str) -> str:
    """
    Attempt to find a caption for an image by looking for text
    immediately below the image bounding box.
    Returns caption text or empty string if none found.

    Strategy: extract words within a 50pt vertical band below
    the image bottom edge, within the same horizontal span.
    """
    try:
        x0, top, x1, bottom = img_bbox
        caption_band = page.within_bbox((
            max(0, x0 - 20),
            bottom,
            min(page.width, x1 + 20),
            bottom + 60,   # 60pt band below image
        ))
        caption_text = caption_band.extract_text()
        if caption_text and caption_text.strip():
            return caption_text.strip()[:200]  # cap caption length
    except Exception:
        pass
    return ""


def extract_pdf(raw_bytes: bytes) -> ExtractionResult:
    """
    PDF extractor using pdfplumber.

    Pipeline:
    1. Open PDF from bytes
    2. Extract text page by page
    3. Extract tables per page — render as CSV text
    4. Detect images per page — insert [IMAGE_N: caption] markers
    5. Interleave text, tables, and image markers in document order
    6. Return single text block — engine sees one document

    Page format:
        [PAGE: 1]
        <extracted text>
        <extracted tables as CSV>
        [IMAGE_1: Figure 1. Revenue chart]
        [IMAGE_2: no description]

    Image markers:
    - Inserted at end of each page's content block
    - Caption extracted from text immediately below image bbox
    - If no caption found: [IMAGE_N: no description]
    - Image count tracked across all pages (globally incrementing)

    Rules:
    - Never raises — returns extraction_success: False on failure
    - Tables extracted as CSV text — no pipe conversion
    - Empty pages skipped with warning
    - Encrypted PDFs warned and skipped
    - pdfplumber import failure returns actionable warning
    """
    warnings: list[str] = []

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

    page_blocks:  list[str] = []
    page_count:   int = 0
    image_counter: int = 0  # global across all pages

    try:
        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            page_count = len(pdf.pages)

            is_encrypted = getattr(pdf.doc, "encryption", None) is not None
            if is_encrypted:
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

                # Extract tables — render as CSV text (quoted via shared helper
                # so comma-containing cells like "142,500" keep column counts
                # consistent instead of being mis-split into extra columns).
                from ingestion.extractors._table_serialize import rows_to_delimited_text
                tables = page.extract_tables()
                for table_idx, table in enumerate(tables, start=1):
                    if not table:
                        continue
                    table_rows = [row for row in table if row is not None]
                    table_text = rows_to_delimited_text(table_rows)
                    if table_text:
                        page_parts.append(table_text)

                # Detect images — insert markers with captions
                try:
                    images = page.images
                    if images:
                        image_markers: list[str] = []
                        for img in images:
                            image_counter += 1
                            bbox = (
                                img.get("x0", 0),
                                img.get("top", 0),
                                img.get("x1", 0),
                                img.get("bottom", 0),
                            )
                            caption = _get_image_caption(
                                page, bbox, page_text or ""
                            )
                            label = caption if caption else "no description"
                            image_markers.append(
                                f"[IMAGE_{image_counter}: {label}]"
                            )
                        if image_markers:
                            page_parts.append("\n".join(image_markers))
                except Exception:
                    pass  # image detection never blocks text extraction

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

    if image_counter > 0:
        warnings.append(
            f"{image_counter} image(s) detected — "
            "inserted as [IMAGE_N: caption] markers. "
            "Image content not extracted (OCR not enabled)."
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
            encoding_used=None,
        ),
        extraction_warnings=warnings,
        extraction_success=bool(text.strip()),
    )
