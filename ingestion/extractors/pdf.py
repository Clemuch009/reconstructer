# ingestion/extractors/pdf.py

import io
from typing import Optional, List
from ingestion.result import ExtractionResult, ExtractionMetadata
from ingestion.visual import EmbeddedVisual, make_visual_id, visual_placeholder


def _mime_from_filter(filters) -> str:
    """
    Infer MIME type from pdfplumber image filter names.
    Falls back to image/png.
    """
    if not filters:
        return "image/png"
    f = str(filters).lower()
    if "jpeg" in f or "dct" in f:
        return "image/jpeg"
    if "jp2" in f:
        return "image/jp2"
    return "image/png"


def extract_pdf(raw_bytes: bytes) -> ExtractionResult:
    """
    PDF extractor using pdfplumber.

    Pipeline:
    1. Open PDF from bytes
    2. Per page:
       a. Detect tables → extract as CSV, get bounding boxes
       b. Extract text from non-table regions only
       c. Detect embedded visuals → extract bytes, insert [VISUAL: vis_N] placeholder
    3. Return text + visuals list

    Visual extraction:
    - Uses page.images (pdfplumber) to locate image objects
    - Extracts raw bytes via page.to_image().original or image stream
    - Width/height from image dict
    - Placeholder [VISUAL: vis_001] inserted at end of page block
    - Full EmbeddedVisual in result.visuals[]
    """
    warnings:  List[str] = []
    visuals:   List[EmbeddedVisual] = []
    vis_index: int = 0

    try:
        import pdfplumber
    except ImportError:
        return ExtractionResult(
            text="", visuals=[],
            metadata=ExtractionMetadata(
                source_format="pdf", page_count=None, sheet_count=None,
                char_count=0, line_count=0, word_count=0, encoding_used=None,
            ),
            extraction_warnings=["pdfplumber not installed. Run: pip install pdfplumber"],
            extraction_success=False,
        )

    page_blocks: List[str] = []
    page_count:  int = 0

    try:
        import csv as _csv, io as _io

        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            page_count = len(pdf.pages)

            is_encrypted = getattr(pdf.doc, "encryption", None) is not None
            if is_encrypted:
                return ExtractionResult(
                    text="", visuals=[],
                    metadata=ExtractionMetadata(
                        source_format="pdf", page_count=page_count, sheet_count=None,
                        char_count=0, line_count=0, word_count=0, encoding_used=None,
                    ),
                    extraction_warnings=["PDF is encrypted — cannot extract text. Decrypt before ingestion."],
                    extraction_success=False,
                )

            for page_num, page in enumerate(pdf.pages, start=1):
                page_parts: List[str] = []

                # ── Step 1: detect valid tables and get bboxes ──
                tables = page.extract_tables()
                valid_tables = [
                    t for t in tables
                    if t and len(t) >= 2 and len(t[0]) >= 2
                    and not (len(t) <= 2 and len(t[0]) == 1)
                ]

                table_bboxes = []
                try:
                    for pt in page.find_tables():
                        extracted = pt.extract()
                        if (extracted and len(extracted) >= 2
                                and len(extracted[0]) >= 2
                                and not (len(extracted) <= 2 and len(extracted[0]) == 1)):
                            table_bboxes.append(pt.bbox)
                except Exception:
                    pass

                # ── Step 2: extract text from non-table regions ──
                if table_bboxes:
                    try:
                        non_table_parts = []
                        prev_bottom = 0
                        sorted_bboxes = sorted(table_bboxes, key=lambda b: b[1])
                        for bbox in sorted_bboxes:
                            x0, top, x1, bottom = bbox
                            if top > prev_bottom:
                                region = page.within_bbox((0, prev_bottom, page.width, top))
                                t = region.extract_text()
                                if t and t.strip():
                                    non_table_parts.append(t.strip())
                            prev_bottom = bottom
                        if prev_bottom < page.height:
                            region = page.within_bbox((0, prev_bottom, page.width, page.height))
                            t = region.extract_text()
                            if t and t.strip():
                                non_table_parts.append(t.strip())
                        page_text = "\n".join(non_table_parts)
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

                # ── Step 3: render valid tables as CSV ──
                for table in valid_tables:
                    table_lines: List[str] = []
                    for row in table:
                        if row is None:
                            continue
                        cleaned = [
                            (cell or "").strip().replace("\n", " ")
                            for cell in row
                        ]
                        if any(cleaned):
                            buf = _io.StringIO()
                            _csv.writer(buf, quoting=_csv.QUOTE_MINIMAL).writerow(cleaned)
                            table_lines.append(buf.getvalue().strip())
                    if table_lines:
                        page_parts.append("\n".join(table_lines))

                # ── Step 4: extract embedded raster visuals ──
                try:
                    page_images = page.images
                    for img in page_images:
                        vis_index += 1
                        vid = make_visual_id(vis_index)

                        img_bytes = None
                        try:
                            stream = img.get("stream")
                            if stream is not None:
                                img_bytes = (
                                    stream.get_data()
                                    if hasattr(stream, "get_data")
                                    else bytes(stream)
                                )
                        except Exception:
                            pass

                        if not img_bytes:
                            try:
                                x0  = float(img.get("x0",  0))
                                top = float(img.get("top", 0))
                                x1  = float(img.get("x1",  page.width))
                                bot = float(img.get("bottom", page.height))
                                if x1 > x0 and bot > top:
                                    crop = page.within_bbox((x0, top, x1, bot))
                                    pil_img = crop.to_image(resolution=150).original
                                    buf = _io.BytesIO()
                                    pil_img.save(buf, format="PNG")
                                    img_bytes = buf.getvalue()
                            except Exception:
                                pass

                        if not img_bytes:
                            warnings.append(
                                f"Page {page_num} visual {vid}: "
                                "could not extract image bytes — skipped"
                            )
                            vis_index -= 1
                            continue

                        mime   = _mime_from_filter(img.get("filters"))
                        width  = int(img.get("width",  0)) or None
                        height = int(img.get("height", 0)) or None

                        visuals.append(EmbeddedVisual(
                            id=vid, page=page_num, mime_type=mime,
                            width=width, height=height,
                            image_bytes=img_bytes, warnings=[],
                        ))
                        page_parts.append(visual_placeholder(vid))

                except Exception as e:
                    warnings.append(f"Page {page_num}: raster visual extraction error: {str(e)[:100]}")

                # ── Step 5: detect and rasterize vector drawing regions ──
                # PDF vector graphics (rects/lines/curves) have no stream bytes.
                # Detect them by finding large vertical gaps in char layout
                # that coincide with vector drawing objects on the page.
                try:
                    has_vectors = (
                        len(page.rects) > 0 or
                        len(page.lines) > 0 or
                        len(page.curves) > 0
                    )

                    if has_vectors and page.chars:
                        char_tops = sorted(set(round(c['top']) for c in page.chars))
                        prev_top  = char_tops[0]

                        # Collect raw gaps > 40pt that contain vector objects
                        raw_gaps = []
                        for top in char_tops[1:]:
                            gap_size = top - prev_top
                            if gap_size > 40:
                                gap_top = prev_top
                                gap_bot = top

                                def overlaps(obj, g_top=gap_top, g_bot=gap_bot):
                                    ot = obj.get('top', obj.get('y0', 0))
                                    ob = obj.get('bottom', obj.get('y1', page.height))
                                    return (min(ob, g_bot) - max(ot, g_top)) > 20

                                if (
                                    any(overlaps(r) for r in page.rects) or
                                    any(overlaps(l) for l in page.lines) or
                                    any(overlaps(c) for c in page.curves)
                                ):
                                    raw_gaps.append((gap_top, gap_bot))

                            prev_top = top

                        # Merge adjacent gaps within 50pt of each other
                        # Charts with axis labels create text interruptions
                        # inside a single chart region — merge into one visual
                        # Only merge if the connector region also has vectors
                        merged_gaps = []
                        for gap_top, gap_bot in raw_gaps:
                            if merged_gaps:
                                prev_end   = merged_gaps[-1][1]
                                connector  = gap_top - prev_end  # gap between gaps
                                # Merge if close AND total merged height < 500pt
                                # (prevents merging entire page into one visual)
                                total_height = gap_bot - merged_gaps[-1][0]
                                if connector <= 50 and total_height < 500:
                                    merged_gaps[-1] = [merged_gaps[-1][0], gap_bot]
                                    continue
                            merged_gaps.append([gap_top, gap_bot])

                        for gap_top, gap_bot in merged_gaps:
                            if (gap_bot - gap_top) < 120:  # skip small decorative elements
                                continue
                            vis_index += 1
                            vid = make_visual_id(vis_index)
                            try:
                                pad  = 8
                                bbox = (
                                    0,
                                    max(0, gap_top - pad),
                                    page.width,
                                    min(page.height, gap_bot + pad),
                                )
                                crop    = page.within_bbox(bbox)
                                pil_img = crop.to_image(resolution=150).original
                                buf     = _io.BytesIO()
                                pil_img.save(buf, format="PNG")
                                img_bytes = buf.getvalue()

                                visuals.append(EmbeddedVisual(
                                    id=vid, page=page_num,
                                    mime_type="image/png",
                                    width=pil_img.width,
                                    height=pil_img.height,
                                    image_bytes=img_bytes,
                                    warnings=["rasterized from vector drawing"],
                                ))
                                page_parts.append(visual_placeholder(vid))
                            except Exception as e:
                                warnings.append(
                                    f"Page {page_num}: vector rasterize failed: {str(e)[:80]}"
                                )
                                vis_index -= 1

                            prev_top = top

                    elif has_vectors and not page.chars:
                        # Entire page is vector — rasterize whole page
                        vis_index += 1
                        vid = make_visual_id(vis_index)
                        try:
                            pil_img   = page.to_image(resolution=150).original
                            buf       = _io.BytesIO()
                            pil_img.save(buf, format="PNG")
                            img_bytes = buf.getvalue()
                            visuals.append(EmbeddedVisual(
                                id=vid, page=page_num,
                                mime_type="image/png",
                                width=pil_img.width, height=pil_img.height,
                                image_bytes=img_bytes,
                                warnings=["full page rasterized — no text found"],
                            ))
                            page_parts.append(visual_placeholder(vid))
                        except Exception as e:
                            warnings.append(
                                f"Page {page_num}: full page rasterize failed: {str(e)[:80]}"
                            )
                            vis_index -= 1

                except Exception as e:
                    warnings.append(f"Page {page_num}: vector detection error: {str(e)[:100]}")

                if page_parts:
                    block = f"[PAGE: {page_num}]\n" + "\n\n".join(page_parts)
                    page_blocks.append(block)

    except Exception as e:
        return ExtractionResult(
            text="", visuals=[],
            metadata=ExtractionMetadata(
                source_format="pdf", page_count=page_count, sheet_count=None,
                char_count=0, line_count=0, word_count=0, encoding_used=None,
            ),
            extraction_warnings=[f"PDF extraction failed: {str(e)[:200]}"],
            extraction_success=False,
        )

    text  = "\n\n".join(page_blocks)
    lines = text.splitlines()
    words = text.split()

    if not text.strip():
        warnings.append("No text extracted from any page — PDF may be entirely image-based")

    if visuals:
        warnings.append(
            f"{len(visuals)} visual(s) extracted — "
            "available in result.visuals[] as raw bytes."
        )

    return ExtractionResult(
        text=text,
        visuals=visuals,
        metadata=ExtractionMetadata(
            source_format="pdf", page_count=page_count, sheet_count=None,
            char_count=len(text), line_count=len(lines), word_count=len(words),
            encoding_used=None,
        ),
        extraction_warnings=warnings,
        extraction_success=bool(text.strip()),
    )
