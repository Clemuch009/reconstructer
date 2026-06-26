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

    sheet_names   = wb.sheetnames
    sheet_count   = len(sheet_names)
    hidden_sheets: list[str] = []
    empty_sheets:  list[str] = []
    sheet_blocks:  list[str] = []

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
        sheet_blocks.append(f"{sheet_marker}\n{csv_text}")

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

    return ExtractionResult(
        text=text,
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
