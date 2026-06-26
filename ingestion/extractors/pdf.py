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

                # Extract tables first — pdfplumber detects table regions
                # Filter to only valid tables (skip single-cell full-page tables
                # which are pdfplumber misdetections of the whole page as a table)
                tables = page.extract_tables()
                valid_tables = [
                    t for t in tables
                    if t and len(t) >= 2
                    and len(t[0]) >= 2
                    and not (len(t) <= 2 and len(t[0]) == 1)
                ]

                # Get bounding boxes of valid tables to exclude from text
                table_bboxes = []
                try:
                    for pt in page.find_tables():
                        bbox = pt.bbox  # (x0, top, x1, bottom)
                        # Check if this table matches a valid table
                        extracted = pt.extract()
                        if (extracted and len(extracted) >= 2
                                and len(extracted[0]) >= 2
                                and not (len(extracted) <= 2 and len(extracted[0]) == 1)):
                            table_bboxes.append(bbox)
                except Exception:
                    pass

                # Extract text excluding table regions
                if table_bboxes:
                    try:
                        # Crop page to non-table regions
                        non_table_text_parts = []
                        prev_bottom = 0
                        sorted_bboxes = sorted(table_bboxes, key=lambda b: b[1])
                        for bbox in sorted_bboxes:
                            x0, top, x1, bottom = bbox
                            if top > prev_bottom:
                                region = page.within_bbox((0, prev_bottom, page.width, top))
                                t = region.extract_text()
                                if t and t.strip():
                                    non_table_text_parts.append(t.strip())
                            prev_bottom = bottom
                        # Text after last table
                        if prev_bottom < page.height:
                            region = page.within_bbox((0, prev_bottom, page.width, page.height))
                            t = region.extract_text()
                            if t and t.strip():
                                non_table_text_parts.append(t.strip())
                        page_text = "\n".join(non_table_text_parts)
                    except Exception:
                        page_text = page.extract_text() or ""
                else:
                    page_text = page.extract_text() or ""

                if page_text and page_text.strip():
                    page_parts.append(page_text.strip())
                elif not valid_tables:
                    warnings.append(
                        f"Page {page_num}: no text extracted — "
                        "may be image-based (OCR not supported)"
                    )

                # Render valid tables as CSV text
                for table_idx, table in enumerate(valid_tables, start=1):
                    if not table:
                        continue
                    table_lines: list[str] = []
                    for row in table:
                        if row is None:
                            continue
                        cleaned = [
                            (cell or "").strip()
                                     .replace("\n", " ")
                            for cell in row
                        ]
                        if any(cleaned):
                            # Use csv.writer to re-quote fields containing commas
                            import csv as _csv, io as _io
                            buf = _io.StringIO()
                            _csv.writer(buf, quoting=_csv.QUOTE_MINIMAL).writerow(cleaned)
                            table_lines.append(buf.getvalue().strip())
                    if table_lines:
                        page_parts.append("\n".join(table_lines))

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
