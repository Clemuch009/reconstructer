# ingestion/extractors/html.py

import re
import os
from ingestion.result import ExtractionResult, ExtractionMetadata

_STRIP_TAGS = {
    "script", "style", "noscript", "meta", "link",
    "head", "iframe", "object", "embed", "svg", "canvas",
}

_RESIDUAL_TAG_RE = re.compile(
    r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^>]*)?>",
    re.DOTALL,
)

_HTML_ENTITY_RE = re.compile(r"&(?:lt|gt|amp|nbsp;|quot|apos);")


def _strip_residual_html(text: str) -> tuple[str, list[str]]:
    warnings: list[str] = []
    lines = text.splitlines()
    cleaned: list[str] = []
    residual_count = 0

    for line in lines:
        stripped = line.strip()

        for prefix in ("HTMLCopy", "CSSCopy", "JSCopy", "JavaScriptCopy",
                        "PythonCopy", "BashCopy", "TextCopy"):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):].strip()
                break

        detagged = _RESIDUAL_TAG_RE.sub("", stripped).strip()

        if stripped and not detagged:
            residual_count += 1
            continue

        if re.match(r'^<(?:div|section|article|header|footer|main|nav|aside|p|h[1-6]|ul|ol|li|table|tr|td|th|form|figure|blockquote)\b', stripped, re.IGNORECASE):
            residual_count += 1
            continue

        if detagged:
            json_chars = sum(1 for c in detagged if c in '{}[]":\\,\t')
            ratio = json_chars / max(len(detagged), 1)
            if ratio > 0.40 and len(detagged) > 20:
                residual_count += 1
                continue
            if re.match(r'^["\\\s]*\w+["\\]*\s*:\s*["\[{<\\]', detagged):
                residual_count += 1
                continue

        cleaned.append(detagged if detagged != stripped else stripped)

    if residual_count > 0:
        warnings.append(
            f"{residual_count} lines of residual HTML/JS data removed "
            f"(browser-saved page JS blob leakage)"
        )

    final: list[str] = []
    prev_blank = False
    for line in cleaned:
        if not line:
            if not prev_blank:
                final.append("")
            prev_blank = True
        else:
            final.append(line)
            prev_blank = False

    return "\n".join(final), warnings


def _get_image_label(img_tag) -> str:
    """
    Extract best available label for an HTML image:
    1. alt attribute (most informative)
    2. title attribute
    3. filename from src (strip path and extension)
    4. "no description" fallback
    """
    alt = (img_tag.get("alt", "") or "").strip()
    if alt and alt.lower() not in ("", "image", "img", "photo", "picture"):
        return alt[:200]

    title = (img_tag.get("title", "") or "").strip()
    if title:
        return title[:200]

    src = (img_tag.get("src", "") or "").strip()
    if src and not src.startswith("data:"):
        # Extract filename without extension from src URL
        filename = os.path.basename(src.split("?")[0])
        name = os.path.splitext(filename)[0]
        if name and len(name) > 1:
            return name[:200]

    return "no description"


def _table_to_csv(tag) -> str:
    # Serialize via the shared helper (quotes comma-containing cells) instead of
    # replacing commas with semicolons + naive join, which corrupted values and
    # could break column counts.
    from ingestion.extractors._table_serialize import rows_to_delimited_text
    rows: list[list[str]] = []
    for row in tag.find_all("tr"):
        cells = [
            cell.get_text(separator=" ", strip=True)
            for cell in row.find_all(["td", "th"])
        ]
        rows.append(cells)
    return rows_to_delimited_text(rows)


def extract_html(raw_bytes: bytes) -> ExtractionResult:
    """
    HTML extractor using BeautifulSoup.

    Pipeline:
    1. Decode bytes — UTF-8, meta charset, latin-1, utf-8-recovered
    2. Pre-parse: remove __NEXT_DATA__ / JS hydration blobs
    3. Strip script/style/noise tags
    4. Replace images with [IMAGE_N: label] placeholders
       — label from alt, title, or src filename
    5. Replace tables with deterministic placeholders
    6. Extract text with block structure preserved
    7. Substitute placeholders with CSV / image markers
    8. Strip residual HTML/JS data from browser-saved pages
    9. Collapse whitespace — return clean text block

    Image markers:
    - data: URIs skipped (base64 inline images, no useful label)
    - alt text used when meaningful (not generic "image"/"img")
    - title attribute as fallback
    - src filename (without extension) as last resort
    - [IMAGE_N: no description] if nothing available
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

    # Step 1 — decode
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
                warnings.append("UTF-8 decode failed — fell back to latin-1")
        except Exception:
            text_raw = raw_bytes.decode("utf-8", errors="replace")
            encoding_used = "utf-8-recovered"
            warnings.append("Encoding detection failed — decoded as utf-8-recovered")

    # Step 1b — remove JS hydration blobs
    text_raw = re.sub(
        r'<script[^>]*(?:id="__NEXT_DATA__"|type="application/json")[^>]*>.*?</script>',
        "", text_raw, flags=re.DOTALL | re.IGNORECASE,
    )
    text_raw = re.sub(
        r'<script[^>]*>\s*window\.__[A-Z_]+\s*=.*?</script>',
        "", text_raw, flags=re.DOTALL | re.IGNORECASE,
    )

    # Step 2 — parse
    try:
        soup = BeautifulSoup(text_raw, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(text_raw, "html.parser")
            warnings.append("lxml parser unavailable — fell back to html.parser")
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
                extraction_warnings=[f"HTML parse failed: {str(e)[:200]}"],
                extraction_success=False,
            )

    # Strip noise tags + nav/aside/header/footer
    _STRIP_STRUCTURAL = _STRIP_TAGS | {"nav", "aside", "header", "footer"}
    for tag in soup.find_all(_STRIP_STRUCTURAL):
        tag.decompose()

    # Step 3 — replace images with placeholders BEFORE get_text()
    # so markers appear at the correct position in document flow
    image_placeholder_map: dict[str, str] = {}
    image_counter = 0
    image_count_skipped = 0

    for img in soup.find_all("img"):
        src = (img.get("src", "") or "").strip()

        # Skip data: URIs — base64 inline images have no useful label
        # and their src would be thousands of chars
        if src.startswith("data:"):
            image_count_skipped += 1
            img.decompose()
            continue

        image_counter += 1
        label       = _get_image_label(img)
        placeholder = f"__IMAGE_{image_counter}__"
        image_placeholder_map[placeholder] = (
            f"[IMAGE_{image_counter}: {label}]"
        )
        img.replace_with(f" {placeholder} ")

    # Step 4 — replace tables with placeholders
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

    # Step 5 — extract text
    body     = soup.find("body") or soup
    raw_text = body.get_text(separator="\n")

    lines_raw = raw_text.splitlines()
    cleaned:  list[str] = []
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

    # Step 6 — substitute placeholders
    final_text = "\n".join(cleaned)

    for placeholder, marker in image_placeholder_map.items():
        final_text = final_text.replace(placeholder, marker)

    for placeholder, csv_text in table_csv_map.items():
        final_text = final_text.replace(placeholder, csv_text)

    # Step 7 — strip residual HTML/JS
    final_text, residual_warnings = _strip_residual_html(final_text)
    warnings.extend(residual_warnings)

    # Step 8 — collapse whitespace
    final_text = re.sub(r"\n{3,}", "\n\n", final_text).strip()

    lines = final_text.splitlines()
    words = final_text.split()

    if not final_text:
        warnings.append("No text extracted from HTML")

    if image_counter > 0:
        warnings.append(
            f"{image_counter} image(s) detected — "
            "inserted as [IMAGE_N: label] markers. "
            "Image content not extracted (OCR not enabled)."
        )
    if image_count_skipped > 0:
        warnings.append(
            f"{image_count_skipped} inline data: image(s) skipped."
        )

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
