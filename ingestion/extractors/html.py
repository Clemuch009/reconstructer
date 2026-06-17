# ingestion/extractors/html.py

import re
from ingestion.result import ExtractionResult, ExtractionMetadata

_STRIP_TAGS = {
    "script", "style", "noscript", "meta", "link",
    "head", "iframe", "object", "embed", "svg", "canvas",
}

# Matches residual HTML tags that survive get_text() via JS string data.
# Covers: <tag>, </tag>, <tag attr="...">, <tag attr='...'>, self-closing.
# Compiled once — applied after extraction on the plain text output.
_RESIDUAL_TAG_RE = re.compile(
    r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^>]*)?>",
    re.DOTALL,
)

# Matches JSON-escaped HTML sequences left behind after JS blob extraction:
# &lt;div&gt; &amp;nbsp; etc. — only strip when dense (>3 per line).
_HTML_ENTITY_RE = re.compile(r"&(?:lt|gt|amp|nbsp|quot|apos);")


def _strip_residual_html(text: str) -> tuple[str, list[str]]:
    """
    Post-extraction cleanup for HTML tags and entities that survive
    BeautifulSoup's get_text() via JS string data / JSON blobs embedded
    in browser-saved pages (__NEXT_DATA__, __INITIAL_STATE__, etc.).

    Strategy:
    1. Remove residual <tag> / </tag> patterns from plain text lines
    2. Remove lines that are clearly JSON/JS data remnants:
       - lines where >40% of characters are JSON structural chars
         ({ } [ ] " : , \\ \n \t)
       - lines starting with JSON keys ("key": or \"key\":)
    3. Collapse newly-empty runs of blank lines

    Never removes lines that have real sentence content even if they
    contain some special characters — threshold-based, not binary.
    """
    warnings: list[str] = []
    lines = text.splitlines()
    cleaned: list[str] = []
    residual_count = 0

    for line in lines:
        stripped = line.strip()

        # Remove residual HTML tags from the line
        detagged = _RESIDUAL_TAG_RE.sub("", stripped).strip()

        # If the line WAS a tag and nothing remains — skip it
        if stripped and not detagged:
            residual_count += 1
            continue

        # Detect JSON data lines — JS blob remnants
        # Heuristic: >40% JSON structural characters
        if detagged:
            json_chars = sum(
                1 for c in detagged
                if c in '{}[]":\\,\t'
            )
            ratio = json_chars / max(len(detagged), 1)
            if ratio > 0.40 and len(detagged) > 20:
                residual_count += 1
                continue

            # Lines that are pure JSON key-value remnants
            # e.g.  "socialStorm": "<div class=..." or \":\"<div
            if re.match(r'^["\\\s]*\w+["\\]*\s*:\s*["\[{<\\]', detagged):
                residual_count += 1
                continue

        # Line survived — use detagged version
        cleaned.append(detagged if detagged != stripped else stripped)

    if residual_count > 0:
        warnings.append(
            f"{residual_count} lines of residual HTML/JS data removed "
            f"(browser-saved page JS blob leakage)"
        )

    # Re-collapse blank lines that opened up after removals
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
    6. Strip residual HTML/JS data from browser-saved pages
    7. Collapse whitespace — return clean text block

    Rules:
    - Never raises
    - Script/style/head stripped entirely
    - Tables extracted as CSV — consistent with other extractors
    - Table placeholders use deterministic counters — not id()
    - Residual <tag> patterns from JS blobs removed in post-pass
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
    # Step 1b — pre-parse: remove __NEXT_DATA__ / __INITIAL_STATE__ blobs
    # These are large <script> tags with type="application/json" or
    # id="__NEXT_DATA__" that contain the full React/Redux state as JSON.
    # BeautifulSoup strips the <script> tag but the JSON string content
    # (which itself contains raw HTML) leaks through get_text().
    # Removing these before parsing eliminates the source of leakage.
    # ---------------------------------
    text_raw = re.sub(
        r'<script[^>]*(?:id="__NEXT_DATA__"|type="application/json")[^>]*>.*?</script>',
        "",
        text_raw,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Also strip any <script> tag whose content starts with window.__
    # (common pattern for hydration state blobs)
    text_raw = re.sub(
        r'<script[^>]*>\s*window\.__[A-Z_]+\s*=.*?</script>',
        "",
        text_raw,
        flags=re.DOTALL | re.IGNORECASE,
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
    # Step 6 — strip residual HTML/JS data from browser-saved pages
    # Catches anything the pre-parse regex didn't eliminate
    # ---------------------------------
    final_text, residual_warnings = _strip_residual_html(final_text)
    warnings.extend(residual_warnings)

    # ---------------------------------
    # Step 7 — final whitespace collapse
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
