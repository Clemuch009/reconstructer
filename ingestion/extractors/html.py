# ingestion/extractors/html.py

import re
from ingestion.result import ExtractionResult, ExtractionMetadata

_STRIP_TAGS = {
    "script", "style", "noscript", "meta", "link",
    "head", "iframe", "object", "embed", "svg", "canvas",
}


def _table_to_csv(tag) -> str:
    lines: list[str] = []
    for row in tag.find_all("tr"):
        cells = []
        for cell in row.find_all(["td", "th"]):
            text = cell.get_text(separator=" ", strip=True)
            text = text.replace(",", ";").replace("\n", " ")
            cells.append(text)
        if any(cells):
            lines.append(",".join(cells))
    return "\n".join(lines)


def extract_html(raw_bytes: bytes) -> ExtractionResult:
    """
    HTML extractor using BeautifulSoup.

    Pipeline:
    1. Decode bytes — UTF-8, meta charset, latin-1, utf-8-recovered
    2. Strip script/style/noise tags
    3. Replace tables with deterministic placeholders
    4. Extract text with block structure preserved
    5. Substitute placeholders with CSV text
    6. Collapse whitespace — return clean text block

    Rules:
    - Never raises
    - Script/style/head stripped entirely
    - Tables extracted as CSV — consistent with other extractors
    - Table placeholders use deterministic counters — not id()
    - Dead traversal loops removed
    - BeautifulSoup import failure returns actionable warning
    """
    warnings: list[str] = []

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return ExtractionResult(
            text="",
            metadata=ExtractionMetadata(
                source_format="html",
                page_count=None,
                sheet_count=None,
                char_count=0,
                line_count=0,
                word_count=0,
                encoding_used=None,
            ),
            extraction_warnings=[
                "beautifulsoup4 not installed. "
                "Run: pip install beautifulsoup4 lxml"
            ],
            extraction_success=False,
        )

    # ---------------------------------
    # Step 1 — decode bytes
    # ---------------------------------
    encoding_used = "utf-8"
    try:
        text_raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            from bs4 import BeautifulSoup as _BS
            soup_peek = _BS(raw_bytes[:2048], "html.parser")
            meta_charset = soup_peek.find("meta", charset=True)
            if meta_charset:
                charset = meta_charset.get("charset", "latin-1")
                text_raw = raw_bytes.decode(charset, errors="replace")
                encoding_used = charset
                warnings.append(
                    f"UTF-8 decode failed — used charset from meta tag: {charset}"
                )
            else:
                text_raw = raw_bytes.decode("latin-1")
                encoding_used = "latin-1"
                warnings.append(
                    "UTF-8 decode failed — fell back to latin-1"
                )
        except Exception:
            text_raw = raw_bytes.decode("utf-8", errors="replace")
            encoding_used = "utf-8-recovered"
            warnings.append(
                "Encoding detection failed — decoded as utf-8-recovered"
            )

    # ---------------------------------
    # Step 2 — parse
    # ---------------------------------
    try:
        soup = BeautifulSoup(text_raw, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(text_raw, "html.parser")
            warnings.append(
                "lxml parser unavailable — fell back to html.parser"
            )
        except Exception as e:
            return ExtractionResult(
                text="",
                metadata=ExtractionMetadata(
                    source_format="html",
                    page_count=None,
                    sheet_count=None,
                    char_count=0,
                    line_count=0,
                    word_count=0,
                    encoding_used=encoding_used,
                ),
                extraction_warnings=[
                    f"HTML parse failed: {str(e)[:200]}"
                ],
                extraction_success=False,
            )

    # Strip noise tags
    for tag in soup.find_all(_STRIP_TAGS):
        tag.decompose()

    # ---------------------------------
    # Step 3 — replace tables with deterministic placeholders
    # ---------------------------------
    table_csv_map: dict[str, str] = {}
    table_idx = 0

    for table in soup.find_all("table"):
        csv_text = _table_to_csv(table)
        if csv_text.strip():
            placeholder = f"__TABLE_{table_idx}__"
            table_csv_map[placeholder] = csv_text
            table.replace_with(f"\n{placeholder}\n")
            table_idx += 1
        else:
            warnings.append("Empty HTML table skipped")
            table.decompose()

    # ---------------------------------
    # Step 4 — extract text with block structure
    # ---------------------------------
    body = soup.find("body") or soup
    raw_text = body.get_text(separator="\n")

    lines_raw  = raw_text.splitlines()
    cleaned:   list[str] = []
    prev_blank = False

    for line in lines_raw:
        stripped = line.strip()
        if not stripped:
            if not prev_blank:
                cleaned.append("")
            prev_blank = True
        else:
            cleaned.append(stripped)
            prev_blank = False

    # ---------------------------------
    # Step 5 — substitute placeholders with CSV text
    # ---------------------------------
    text_with_placeholders = "\n".join(cleaned)
    final_text = text_with_placeholders

    for placeholder, csv_text in table_csv_map.items():
        final_text = final_text.replace(placeholder, csv_text)

    # ---------------------------------
    # Step 6 — final whitespace collapse
    # ---------------------------------
    final_text = re.sub(r"\n{3,}", "\n\n", final_text).strip()

    lines = final_text.splitlines()
    words = final_text.split()

    if not final_text:
        warnings.append("No text extracted from HTML")

    return ExtractionResult(
        text=final_text,
        metadata=ExtractionMetadata(
            source_format="html",
            page_count=None,
            sheet_count=None,
            char_count=len(final_text),
            line_count=len(lines),
            word_count=len(words),
            encoding_used=encoding_used,
        ),
        extraction_warnings=warnings,
        extraction_success=bool(final_text.strip()),
    )
