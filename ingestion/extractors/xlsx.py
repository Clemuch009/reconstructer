# ingestion/extractors/xlsx.py

import io
from ingestion.result import ExtractionResult, ExtractionMetadata


def _cell_value(cell) -> str:
    """
    Normalize cell value to string.
    Handles None, datetime, float, int, string.
    """
    if cell.value is None:
        return ""
    try:
        from datetime import datetime, date
        if isinstance(cell.value, (datetime, date)):
            return cell.value.isoformat()
    except ImportError:
        pass
    if isinstance(cell.value, float):
        if cell.value == int(cell.value):
            return str(int(cell.value))
        return str(round(cell.value, 10))
    return str(cell.value).strip()


def _sheet_to_csv(sheet) -> tuple[str, int, list[str]]:
    """
    Convert openpyxl worksheet to CSV text.
    Returns (csv_text, row_count, warnings).

    Serialization goes through the shared rows_to_delimited_text helper so cells
    containing commas (e.g. "142,500") are quoted and column counts stay
    consistent — a naive ",".join would split such cells into extra columns and
    cause the engine to misclassify the sheet as prose.
    """
    from ingestion.extractors._table_serialize import rows_to_delimited_text

    warnings:      list[str] = []
    data_rows:     list[list[str]] = []
    expected_cols: int | None = None
    row_count:     int = 0

    for row_num, row in enumerate(sheet.iter_rows(), start=1):
        cells = [_cell_value(cell) for cell in row]

        if not any(cells):
            continue

        row_count += 1

        if expected_cols is None:
            expected_cols = len(cells)
        elif len(cells) != expected_cols:
            warnings.append(
                f"Row {row_num}: expected {expected_cols} columns "
                f"got {len(cells)} — ragged row detected"
            )

        data_rows.append(cells)

    return rows_to_delimited_text(data_rows), row_count, warnings


def extract_xlsx(raw_bytes: bytes) -> ExtractionResult:
    """
    XLSX extractor using openpyxl.

    Output format:
        [WORKBOOK]
        sheets: 3
        sheet_names: Sales,Inventory,Finance
        hidden_sheets: 0
        empty_sheets: 0

        [SHEET: Sales]
        rows: 42
        Sales,Q1,Q2
        Widget A,500,620
        ...

        [SHEET: Inventory]
        rows: 18
        SKU,Qty,Location
        ...

    Rules:
    - Never raises
    - Workbook marker carries summary metadata inline
    - Sheet marker carries row count inline
    - Empty sheets warned and skipped
    - Hidden sheets included with warning, counted in hidden_sheets
    - Formulas deferred to detectors/ — passed as resolved values only
    - openpyxl import failure returns actionable warning
    """
    warnings: list[str] = []

    try:
        import openpyxl
    except ImportError:
        return ExtractionResult(
            text="",
            metadata=ExtractionMetadata(
                source_format="xlsx",
                page_count=None,
                sheet_count=None,
                char_count=0,
                line_count=0,
                word_count=0,
                encoding_used=None,
            ),
            extraction_warnings=[
                "openpyxl not installed. "
                "Run: pip install openpyxl"
            ],
            extraction_success=False,
        )

    try:
        wb = openpyxl.load_workbook(
            io.BytesIO(raw_bytes),
            read_only=True,
            data_only=True,
        )
    except Exception as e:
        return ExtractionResult(
            text="",
            metadata=ExtractionMetadata(
                source_format="xlsx",
                page_count=None,
                sheet_count=None,
                char_count=0,
                line_count=0,
                word_count=0,
                encoding_used=None,
            ),
            extraction_warnings=[
                f"XLSX open failed: {str(e)[:200]}"
            ],
            extraction_success=False,
        )

    from ingestion.visual import EmbeddedVisual, make_visual_id, visual_placeholder

    sheet_names   = wb.sheetnames
    sheet_count   = len(sheet_names)
    hidden_sheets: list[str] = []
    empty_sheets:  list[str] = []
    sheet_blocks:  list[str] = []
    visuals:       list      = []
    vis_index:     int       = 0

    # MIME map from openpyxl image format strings
    _MIME = {
        "png":  "image/png",
        "jpeg": "image/jpeg",
        "jpg":  "image/jpeg",
        "gif":  "image/gif",
        "bmp":  "image/bmp",
        "tiff": "image/tiff",
        "emf":  "image/x-emf",
        "wmf":  "image/x-wmf",
    }

    for sheet_name in sheet_names:
        sheet = wb[sheet_name]

        # Track hidden sheets — include anyway
        if hasattr(sheet, "sheet_state") and sheet.sheet_state == "hidden":
            hidden_sheets.append(sheet_name)
            warnings.append(
                f"Sheet '{sheet_name}' is hidden — included in extraction"
            )

        csv_text, row_count, sheet_warnings = _sheet_to_csv(sheet)
        warnings.extend(sheet_warnings)

        if not csv_text.strip():
            empty_sheets.append(sheet_name)
            warnings.append(f"Sheet '{sheet_name}' is empty — skipped")
            continue

        sheet_marker = (
            f"[SHEET: {sheet_name}]\n"
            f"rows: {row_count}"
        )

        # Extract images embedded in this sheet
        sheet_visual_placeholders: list[str] = []
        try:
            sheet_images = getattr(sheet, "_images", [])
            for img_obj in sheet_images:
                try:
                    # openpyxl Image object has .ref (path in zip) and ._data() or .path
                    img_bytes = None
                    fmt = "png"

                    # Try _data() method first
                    if hasattr(img_obj, "_data"):
                        try:
                            img_bytes = img_obj._data()
                        except Exception:
                            pass

                    # Fallback: read from the workbook zip directly
                    if not img_bytes and hasattr(img_obj, "path"):
                        try:
                            import zipfile
                            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                                img_path = img_obj.path.lstrip("/")
                                img_bytes = zf.read(img_path)
                            fmt = img_path.rsplit(".", 1)[-1].lower()
                        except Exception:
                            pass

                    if not img_bytes:
                        warnings.append(
                            f"Sheet '{sheet_name}': image bytes not extractable — skipped"
                        )
                        continue

                    vis_index += 1
                    vid  = make_visual_id(vis_index)
                    mime = _MIME.get(fmt, f"image/{fmt}")

                    # Dimensions from anchor if available
                    width = height = None
                    try:
                        anchor = img_obj.anchor
                        if hasattr(anchor, "ext"):
                            # EMU → pixels at 96dpi
                            EMU = 914400 / 96
                            width  = int(anchor.ext.cx / EMU)
                            height = int(anchor.ext.cy / EMU)
                    except Exception:
                        pass

                    visuals.append(EmbeddedVisual(
                        id=vid,
                        page=None,
                        mime_type=mime,
                        width=width,
                        height=height,
                        image_bytes=img_bytes,
                        warnings=[f"from sheet: {sheet_name}"],
                    ))
                    sheet_visual_placeholders.append(visual_placeholder(vid))

                except Exception as e:
                    warnings.append(
                        f"Sheet '{sheet_name}': image extraction error: {str(e)[:80]}"
                    )
        except Exception:
            pass

        # Append sheet block with visuals at end
        sheet_content = f"{sheet_marker}\n{csv_text}"
        if sheet_visual_placeholders:
            sheet_content += "\n" + "\n".join(sheet_visual_placeholders)
        sheet_blocks.append(sheet_content)

    try:
        wb.close()
    except Exception:
        pass

    # Build workbook marker with inline summary
    workbook_marker = (
        f"[WORKBOOK]\n"
        f"sheets: {sheet_count}\n"
        f"sheet_names: {','.join(sheet_names)}\n"
        f"hidden_sheets: {len(hidden_sheets)}\n"
        f"empty_sheets: {len(empty_sheets)}"
    )

    blocks = [workbook_marker] + sheet_blocks
    text   = "\n\n".join(blocks)
    lines  = text.splitlines()
    words  = text.split()

    has_data = len(sheet_blocks) > 0

    if not has_data:
        warnings.append("No sheet data extracted from workbook")

    if visuals:
        warnings.append(
            f"{len(visuals)} visual(s) extracted from workbook — "
            "available in result.visuals[] as raw bytes."
        )

    return ExtractionResult(
        text=text,
        visuals=visuals,
        metadata=ExtractionMetadata(
            source_format="xlsx",
            page_count=None,
            sheet_count=sheet_count,
            char_count=len(text),
            line_count=len(lines),
            word_count=len(words),
            encoding_used=None,
        ),
        extraction_warnings=warnings,
        extraction_success=has_data,
    )
