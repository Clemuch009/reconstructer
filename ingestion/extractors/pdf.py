# ingestion/extractors/pdf.py

import io
from typing import Optional, List
from ingestion.result import ExtractionResult, ExtractionMetadata
from ingestion.visual import EmbeddedVisual, make_visual_id, visual_placeholder


# A 'table' this large whose cells hold this much text is a page, not a table.
# Thresholds are set from measurement (see _is_layout_container), with a wide
# margin: the real tables in the corpus sit at 12% / 54 chars.
_LAYOUT_AREA_FRACTION = 0.60
_LAYOUT_CELL_CHARS = 150

# Minimum pixel dimension for a raster to count as content. Named in the code
# that uses it ("a 32px floor removes only noise") but never defined, so every
# PDF raised NameError on the visual path — caught, turned into the warning
# string "raster extraction error: name '_MIN_VISUAL_DIM' is not defined", and
# ignored. The visual extraction has therefore never run in production.
_MIN_VISUAL_DIM = 32


def _region_text(region, page_width: float) -> str:
    """Text from a page region, with the columns put back in reading order.

    This exists because the column fix was built and then never ran.
    reconstruct_reading_order() was gated behind "only if this page has no
    tables" — and every real invoice has a table, so on every real invoice the
    fix was skipped and extract_text() flattened the header block. An invoice's
    identity lives in that header: "Invoice to | Invoice from" side by side, the
    vendor in one column and the customer in the other, the invoice number and
    tax id beneath. Flattened, they concatenate into one line and stop being
    key/values at all.

    The region above and below a table is exactly where that header sits, so the
    reconstruction has to happen HERE, not only on table-free pages.

    Falls back to extract_text() when no columns are detected — single-column
    layouts are unchanged, which is what makes this safe to apply everywhere.
    """
    try:
        from ingestion.extractors.pdf_columns import reconstruct_reading_order
        recovered = reconstruct_reading_order(region.extract_words(), page_width)
        if recovered:
            return recovered
    except Exception:
        pass
    return region.extract_text() or ""


def _is_layout_container(rows, bbox, page_w: float, page_h: float) -> bool:
    """True when a 'table' pdfplumber found is really the page's layout.

    Invoices generated from HTML — Paddle, Stripe, most modern billing systems —
    are drawn with background shading and horizontal rules. pdfplumber's line
    detection reads those as table borders and reports the whole page as one
    table. The Paddle invoice comes back as 2 columns x 12 rows covering 84% of
    the page, with the entire "Invoice to / Invoice from" block sitting in a
    single 363-character cell.

    Everything downstream then breaks, invisibly: the page is emitted as
    comma-joined cells, "Subtotal $25.00 VAT $4.00 Total $29.00" lands mid-line
    with no separators to parse, and the resolver — which is working perfectly —
    is handed a document with no readable key/values. The invoice number, total
    and tax id are all present on the paper and none of them survive.

    The discriminator is what a cell CONTAINS. A data table's cell holds a value:
    "$25.00", "Individual Plan", "16%". A layout container's cell holds a block
    of the document. Measured across the corpus, the separation is not close:

        real tables      <=12% of page area, longest cell  54 chars
        the Paddle page   84% of page area, longest cell 363 chars

    So: big AND blocky. Both are required, because either alone is legitimate —
    a wide table of short values is a real table, and a small table with one
    chatty description cell is a real table. Only their combination says "this is
    not a grid of values, it is a page someone drew lines on".

    Rejecting it costs nothing: the caller falls back to reading order
    reconstruction, which recovers the columns properly (proven on this exact
    document) and lets the borderless-table detection find the REAL line-item
    table nested inside.
    """
    if not rows or not rows[0]:
        return False
    x0, top, x1, bottom = bbox
    if not (page_w and page_h):
        return False
    area = ((x1 - x0) * (bottom - top)) / (page_w * page_h)
    longest = max((len(c or "") for r in rows for c in r), default=0)
    return area >= _LAYOUT_AREA_FRACTION and longest >= _LAYOUT_CELL_CHARS


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
                #
                # Both come from find_tables(), deliberately. extract_tables()
                # returns rows with no bbox, so a table found that way cannot be
                # checked for being the page's layout — and the two lists would
                # then disagree about what a valid table is. They did: the fake
                # full-page table was excluded from the bboxes and still rendered
                # as CSV, so the page came out BOTH ways at once.
                #
                # One definition, used for both. Two will always drift.
                valid_tables = []
                table_bboxes = []
                try:
                    for pt in page.find_tables():
                        extracted = pt.extract()
                        if not (extracted and len(extracted) >= 2
                                and len(extracted[0]) >= 2
                                and not (len(extracted) <= 2 and len(extracted[0]) == 1)):
                            continue
                        # An HTML-rendered invoice's shading reads as borders and
                        # the whole page comes back as one "table". Treating it as
                        # one silently destroys every key/value on the document.
                        if _is_layout_container(extracted, pt.bbox,
                                                page.width, page.height):
                            continue
                        valid_tables.append(extracted)
                        table_bboxes.append(pt.bbox)
                except Exception:
                    # find_tables() failed; fall back to the row-only view. No
                    # bbox means no layout check — accept, as before.
                    valid_tables = [
                        t for t in page.extract_tables()
                        if t and len(t) >= 2 and len(t[0]) >= 2
                        and not (len(t) <= 2 and len(t[0]) == 1)
                    ]
                    table_bboxes = []

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
                                t = _region_text(region, page.width)
                                if t and t.strip():
                                    non_table_parts.append(t.strip())
                            prev_bottom = bottom
                        if prev_bottom < page.height:
                            region = page.within_bbox((0, prev_bottom, page.width, page.height))
                            t = _region_text(region, page.width)
                            if t and t.strip():
                                non_table_parts.append(t.strip())
                        page_text = "\n".join(non_table_parts)
                    except Exception:
                        page_text = page.extract_text() or ""
                else:
                    # Structure-preserving: recover reading order from word
                    # coordinates BEFORE flattening. On a two-column layout,
                    # extract_text() concatenates side-by-side columns onto one
                    # line, burying key-values mid-line. reconstruct_reading_order
                    # returns column-corrected text when it detects columns, else
                    # None → fall back to normal extraction (single-column
                    # unchanged, no regression).
                    page_text = None
                    try:
                        from ingestion.extractors.pdf_columns import reconstruct_reading_order
                        words = page.extract_words()
                        recovered = reconstruct_reading_order(words, page.width)
                        if recovered:
                            page_text = recovered
                    except Exception:
                        page_text = None
                    if page_text is None:
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

                # ── Steps 4+5: unified visual extraction ──
                #
                # A) Raster images → extract bytes from stream
                #    Track their bboxes to avoid re-extracting as vectors
                # B) Vector clusters → cluster objects by proximity,
                #    skip if overlaps a raster bbox (dedup)
                # C) No-text, no-raster pages → rasterize full page

                extracted_bboxes = []  # (top, bot) of already extracted regions

                def bbox_overlaps(top, bot, threshold=0.5):
                    for et, eb in extracted_bboxes:
                        region  = bot - top
                        overlap = min(bot, eb) - max(top, et)
                        if region > 0 and overlap / region > threshold:
                            return True
                    return False

                # A) Raster images
                try:
                    for img in page.images:
                        # Size gate: skip sub-32px fragments. PowerPoint/vector
                        # PDFs emit many 6x6-style specks that are not real
                        # content; extracting them inflates the visual count and
                        # payload. Real figures/diagrams/matrices are far larger
                        # (hundreds of px), so a 32px floor removes only noise.
                        # Applied before any extraction work so skipped fragments
                        # cost nothing and never consume a visual id.
                        _w = int(img.get("width",  0) or 0)
                        _h = int(img.get("height", 0) or 0)
                        if _w < _MIN_VISUAL_DIM or _h < _MIN_VISUAL_DIM:
                            continue

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
                                    crop    = page.within_bbox((x0, top, x1, bot))
                                    pil_img = crop.to_image(resolution=150).original
                                    buf     = _io.BytesIO()
                                    pil_img.save(buf, format="PNG")
                                    img_bytes = buf.getvalue()
                            except Exception:
                                pass

                        if not img_bytes:
                            vis_index -= 1
                            warnings.append(f"Page {page_num}: raster image not extractable — skipped")
                            continue

                        img_top = float(img.get("top",    0))
                        img_bot = float(img.get("bottom", page.height))
                        extracted_bboxes.append((img_top, img_bot))

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
                    warnings.append(f"Page {page_num}: raster extraction error: {str(e)[:100]}")

                # B) Vector clusters
                try:
                    max_span   = page.height * 0.80
                    min_height = 80

                    all_vec = []
                    for obj in list(page.rects) + list(page.lines) + list(page.curves):
                        ot = float(obj.get('top',    obj.get('y0', 0)))
                        ob = float(obj.get('bottom', obj.get('y1', page.height)))
                        if (ob - ot) > max_span:
                            continue
                        if ob > ot:
                            all_vec.append((ot, ob))

                    if all_vec:
                        all_vec.sort()
                        clusters = []
                        ct, cb = all_vec[0]
                        for ot, ob in all_vec[1:]:
                            if ot - cb <= 30:
                                cb = max(cb, ob)
                            else:
                                clusters.append((ct, cb))
                                ct, cb = ot, ob
                        clusters.append((ct, cb))

                        # Precompute which objects have meaningful content
                        # (lines or curves) — pure rect clusters are UI boxes
                        lines_curves = []
                        for obj in list(page.lines) + list(page.curves):
                            ot = float(obj.get('top',    obj.get('y0', 0)))
                            ob = float(obj.get('bottom', obj.get('y1', page.height)))
                            if (ob - ot) <= max_span:
                                lines_curves.append((ot, ob))

                        for ct, cb in clusters:
                            if (cb - ct) < min_height:
                                continue
                            if bbox_overlaps(ct, cb):
                                continue
                            # Must contain at least one line or curve
                            # (pure rect clusters are decorative boxes/borders)
                            has_content = any(
                                (min(ob, cb) - max(ot, ct)) > 10
                                for ot, ob in lines_curves
                            )
                            if not has_content:
                                continue

                            vis_index += 1
                            vid = make_visual_id(vis_index)
                            try:
                                pad      = 10
                                crop_top = max(0, ct - pad)
                                crop_bot = min(page.height, cb + pad)
                                crop     = page.within_bbox((0, crop_top, page.width, crop_bot))
                                pil_img  = crop.to_image(resolution=150).original
                                buf      = _io.BytesIO()
                                pil_img.save(buf, format="PNG")
                                img_bytes = buf.getvalue()

                                extracted_bboxes.append((ct, cb))
                                visuals.append(EmbeddedVisual(
                                    id=vid, page=page_num,
                                    mime_type="image/png",
                                    width=pil_img.width, height=pil_img.height,
                                    image_bytes=img_bytes,
                                    warnings=["rasterized from vector drawing"],
                                ))
                                page_parts.append(visual_placeholder(vid))
                            except Exception as e:
                                warnings.append(f"Page {page_num}: vector rasterize failed: {str(e)[:80]}")
                                vis_index -= 1

                except Exception as e:
                    warnings.append(f"Page {page_num}: vector detection error: {str(e)[:100]}")

                # C) No-text, no-raster page → rasterize full page
                if not page.chars and not page.images and not extracted_bboxes:
                    try:
                        vis_index += 1
                        vid     = make_visual_id(vis_index)
                        pil_img = page.to_image(resolution=150).original
                        buf     = _io.BytesIO()
                        pil_img.save(buf, format="PNG")
                        visuals.append(EmbeddedVisual(
                            id=vid, page=page_num,
                            mime_type="image/png",
                            width=pil_img.width, height=pil_img.height,
                            image_bytes=buf.getvalue(),
                            warnings=["full page rasterized — no text found"],
                        ))
                        page_parts.append(visual_placeholder(vid))
                    except Exception as e:
                        warnings.append(f"Page {page_num}: full page rasterize failed: {str(e)[:80]}")
                        vis_index -= 1

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
