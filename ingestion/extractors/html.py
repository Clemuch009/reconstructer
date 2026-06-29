# ingestion/extractors/html.py

import re
import os
import base64
from typing import List, Optional, Tuple
from ingestion.result import ExtractionResult, ExtractionMetadata
from ingestion.visual import EmbeddedVisual, make_visual_id, visual_placeholder

_STRIP_TAGS = {
    "script", "style", "noscript", "meta", "link",
    "head", "iframe", "object", "embed", "canvas",
}
# Note: svg removed from strip tags — SVGs are extracted as visuals

_RESIDUAL_TAG_RE = re.compile(
    r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^>]*)?>",
    re.DOTALL,
)

# MIME types from data: URI prefix
_DATA_MIME_RE = re.compile(r"^data:(image/[a-zA-Z0-9+\-.]+);base64,(.+)$", re.DOTALL)

# UI chrome detection for <img> tags
_UI_SRC_RE = re.compile(
    r'/(profile_images|icons?|favicon|assets|static|ui|sprites?|thumbs?|'
    r'buttons?|avatars?|badges?|emoji|glyph|toolbar)/',
    re.IGNORECASE,
)
_UI_ALT_RE = re.compile(
    r'^(avatar|icon|logo|button|emoji|badge|spinner|thumbnail|play|pause|'
    r'close|menu|arrow|chevron|search|loading|verified|checkmark)$',
    re.IGNORECASE,
)
_UI_MIN_SIZE = 100  # px — images smaller than this in both dimensions are chrome


def _is_ui_chrome_img(src: str, alt: str, width, height) -> bool:
    """Return True if an <img> tag looks like UI chrome rather than content."""
    if src and not src.startswith("data:") and _UI_SRC_RE.search(src):
        return True
    if alt and _UI_ALT_RE.match(alt.strip()):
        return True
    if width and height:
        try:
            if int(width) <= _UI_MIN_SIZE and int(height) <= _UI_MIN_SIZE:
                return True
        except (ValueError, TypeError):
            pass
    return False


def _strip_residual_html(text: str) -> Tuple[str, List[str]]:
    warnings: List[str] = []
    lines = text.splitlines()
    cleaned: List[str] = []
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

    final: List[str] = []
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


def _get_label(img_tag) -> str:
    alt = (img_tag.get("alt", "") or "").strip()
    if alt and alt.lower() not in ("", "image", "img", "photo", "picture"):
        return alt[:200]
    title = (img_tag.get("title", "") or "").strip()
    if title:
        return title[:200]
    src = (img_tag.get("src", "") or "").strip()
    if src and not src.startswith("data:"):
        filename = os.path.basename(src.split("?")[0])
        name = os.path.splitext(filename)[0]
        if name and len(name) > 1:
            return name[:200]
    return ""


def _table_to_csv(tag) -> str:
    import csv as _csv
    import io as _io

    output = _io.StringIO()
    writer = _csv.writer(output, quoting=_csv.QUOTE_MINIMAL, lineterminator="\n")

    for row in tag.find_all("tr"):
        cells = []
        for cell in row.find_all(["td", "th"]):
            text = cell.get_text(separator=" ", strip=True).replace("\n", " ")
            cells.append(text)
        if any(cells):
            writer.writerow(cells)

    return output.getvalue().strip()


def extract_html(raw_bytes: bytes) -> ExtractionResult:
    """
    HTML extractor using BeautifulSoup.

    Pipeline:
    1. Decode bytes
    2. Pre-parse: remove JS hydration blobs
    3. Strip noise tags (nav, aside, header, footer, script, style...)
    4. Extract visuals from <img> tags:
       - UI chrome filtered out (src path, alt text, small dimensions, chrome parents)
       - data: URI → decode base64 → EmbeddedVisual with raw bytes
       - URL src  → EmbeddedVisual with empty bytes + src as warning
       - Insert [VISUAL: vis_N] placeholder at img position in flow
    5. Replace tables with placeholders
    6. Extract text
    7. Substitute placeholders
    8. Strip residual HTML/JS
    9. Collapse whitespace

    Visuals:
    - data: URIs fully decoded to raw bytes
    - URL references: placeholder inserted, bytes=b'' (not fetched)
    - Width/height from width/height attributes if present
    - UI chrome images (icons, avatars, buttons, tiny images) are discarded
    """
    warnings:  List[str] = []
    visuals:   List[EmbeddedVisual] = []
    vis_index: int = 0
    chrome_discarded: int = 0

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return ExtractionResult(
            text="", visuals=[],
            metadata=ExtractionMetadata(
                source_format="html", page_count=None, sheet_count=None,
                char_count=0, line_count=0, word_count=0, encoding_used=None,
            ),
            extraction_warnings=["beautifulsoup4 not installed. Run: pip install beautifulsoup4 lxml"],
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
            else:
                text_raw = raw_bytes.decode("latin-1")
                encoding_used = "latin-1"
        except Exception:
            text_raw = raw_bytes.decode("utf-8", errors="replace")
            encoding_used = "utf-8-recovered"

    # Step 2 — remove JS hydration blobs
    text_raw = re.sub(
        r'<script[^>]*(?:id="__NEXT_DATA__"|type="application/json")[^>]*>.*?</script>',
        "", text_raw, flags=re.DOTALL | re.IGNORECASE,
    )
    text_raw = re.sub(
        r'<script[^>]*>\s*window\.__[A-Z_]+\s*=.*?</script>',
        "", text_raw, flags=re.DOTALL | re.IGNORECASE,
    )

    # Step 3 — parse
    try:
        soup = BeautifulSoup(text_raw, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(text_raw, "html.parser")
            warnings.append("lxml parser unavailable — fell back to html.parser")
        except Exception as e:
            return ExtractionResult(
                text="", visuals=[],
                metadata=ExtractionMetadata(
                    source_format="html", page_count=None, sheet_count=None,
                    char_count=0, line_count=0, word_count=0, encoding_used=encoding_used,
                ),
                extraction_warnings=[f"HTML parse failed: {str(e)[:200]}"],
                extraction_success=False,
            )

    _STRIP_STRUCTURAL = _STRIP_TAGS | {"nav", "aside", "header", "footer"}
    for tag in soup.find_all(_STRIP_STRUCTURAL):
        tag.decompose()

    # Step 4 — extract visuals from <img> tags
    # UI chrome images are discarded before creating EmbeddedVisuals.
    # Chrome signals (any one is sufficient to discard):
    #   - src path matches known UI/asset patterns (_UI_SRC_RE)
    #   - alt text matches known chrome labels (_UI_ALT_RE)
    #   - both width and height are <= _UI_MIN_SIZE px
    #   - parent is a chrome structural element (nav/header/footer/button/aside)
    #     Note: nav/header/footer/aside are already stripped in Step 3, so this
    #     catches <button> and any that survive structural stripping.
    vis_placeholder_map: dict = {}

    for img in soup.find_all("img"):
        src = (img.get("src", "") or "").strip()
        alt = (img.get("alt", "") or "").strip()

        try:
            width  = int(img.get("width",  0)) or None
            height = int(img.get("height", 0)) or None
        except (ValueError, TypeError):
            width = height = None

        # Chrome filter: structural parent
        if img.find_parent(["nav", "header", "footer", "button", "aside"]):
            img.decompose()
            chrome_discarded += 1
            continue

        # Chrome filter: src path, alt text, small dimensions
        if _is_ui_chrome_img(src, alt, width, height):
            img.decompose()
            chrome_discarded += 1
            continue

        vis_index += 1
        vid = make_visual_id(vis_index)

        img_bytes = b""
        mime_type = "image/png"
        img_warnings: List[str] = []

        if src.startswith("data:"):
            # Inline base64 image — decode to raw bytes
            m = _DATA_MIME_RE.match(src)
            if m:
                mime_type = m.group(1)
                try:
                    img_bytes = base64.b64decode(m.group(2))
                except Exception:
                    img_warnings.append(f"{vid}: base64 decode failed")
            else:
                img_warnings.append(f"{vid}: malformed data URI")

        elif src:
            # External URL — can't fetch, emit as inline text reference
            # Format: [vis_001: https://example.com/image.png]
            placeholder = f"__VIS_{vid}__"
            vis_placeholder_map[placeholder] = f"[{vid}: {src}]"
            img.replace_with(f" {placeholder} ")
            continue  # skip creating an EmbeddedVisual for unfetchable URLs

        else:
            img_warnings.append(f"{vid}: no src attribute")

        visual = EmbeddedVisual(
            id=vid,
            page=None,
            mime_type=mime_type,
            width=width,
            height=height,
            image_bytes=img_bytes,
            warnings=img_warnings,
        )
        visuals.append(visual)
        warnings.extend(img_warnings)

        placeholder = f"__VIS_{vid}__"
        vis_placeholder_map[placeholder] = visual_placeholder(vid)
        img.replace_with(f" {placeholder} ")

    if chrome_discarded:
        warnings.append(f"{chrome_discarded} UI chrome image(s) discarded (icon/avatar/small)")

    # Step 4b — extract SVG elements as visuals
    # General chrome filters — no site-specific class patterns.
    # An SVG is discarded if it looks like an icon/decoration by universal signals:
    #   F1: role="presentation" or aria-hidden="true" — explicitly decorative
    #   F2: hidden via inline style
    #   F3: small viewBox (both w,h <= 32 units) AND no <title>/<desc> — icon heuristic
    #       viewBox is checked because icons sized via CSS carry no width/height attrs.
    #       Content SVGs (charts, diagrams) use large coordinate spaces (e.g. 400x300).
    #   F4: fixed pixel width/height attributes both <= 32px (fallback for no viewBox)
    _SVG_ICON_VIEWBOX = 32   # viewBox units threshold
    _SVG_ICON_PX      = 32   # px attribute threshold

    for svg in soup.find_all("svg"):
        # F1: explicitly decorative
        if svg.get("role") == "presentation" or svg.get("aria-hidden") == "true":
            svg.replace_with("")
            continue

        # F2: hidden SVG sprite containers
        style = svg.get("style", "").replace(" ", "")
        if "width:0" in style or "height:0" in style or "display:none" in style:
            svg.replace_with("")
            continue

        # F3: small viewBox + no semantic label
        viewbox = (svg.get("viewBox") or svg.get("viewbox") or "").strip()
        if viewbox:
            parts = viewbox.split()
            if len(parts) == 4:
                try:
                    vb_w, vb_h = float(parts[2]), float(parts[3])
                    has_label  = bool(svg.find(["title", "desc"]))
                    if vb_w <= _SVG_ICON_VIEWBOX and vb_h <= _SVG_ICON_VIEWBOX and not has_label:
                        svg.replace_with("")
                        continue
                except ValueError:
                    pass

        # F4: explicit small px dimensions (fallback when no viewBox)
        if not viewbox:
            try:
                w_raw = str(svg.get("width",  "") or "").replace("px", "").strip()
                h_raw = str(svg.get("height", "") or "").replace("px", "").strip()
                if w_raw.replace(".", "").isdigit() and h_raw.replace(".", "").isdigit():
                    if float(w_raw) <= _SVG_ICON_PX and float(h_raw) <= _SVG_ICON_PX:
                        svg.replace_with("")
                        continue
            except Exception:
                pass

        vis_index += 1
        vid       = make_visual_id(vis_index)
        svg_str   = str(svg)
        svg_bytes = svg_str.encode("utf-8")

        svg_width = svg_height = None
        try:
            w = svg.get("width", "")
            h = svg.get("height", "")
            svg_width  = int(float(str(w).replace("px","").replace("%",""))) if w and str(w).replace("px","").replace("%","").replace(".","").isdigit() else None
            svg_height = int(float(str(h).replace("px","").replace("%",""))) if h and str(h).replace("px","").replace("%","").replace(".","").isdigit() else None
        except Exception:
            pass

        visual = EmbeddedVisual(
            id=vid,
            page=None,
            mime_type="image/svg+xml",
            width=svg_width,
            height=svg_height,
            image_bytes=svg_bytes,
            warnings=[],
        )
        visuals.append(visual)

        placeholder = f"__VIS_{vid}__"
        vis_placeholder_map[placeholder] = visual_placeholder(vid)
        svg.replace_with(f" {placeholder} ")

    table_csv_map: dict = {}
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

    # Step 6 — extract text
    body     = soup.find("body") or soup
    raw_text = body.get_text(separator="\n")
    lines_raw = raw_text.splitlines()
    cleaned: List[str] = []
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

    # Step 7 — substitute placeholders
    final_text = "\n".join(cleaned)
    for placeholder, marker in vis_placeholder_map.items():
        final_text = final_text.replace(placeholder, marker)
    for placeholder, csv_text in table_csv_map.items():
        final_text = final_text.replace(placeholder, csv_text)

    # Step 8 — strip residual HTML
    final_text, residual_warnings = _strip_residual_html(final_text)
    warnings.extend(residual_warnings)

    # Step 9 — collapse whitespace
    final_text = re.sub(r"\n{3,}", "\n\n", final_text).strip()
    lines = final_text.splitlines()
    words = final_text.split()

    if not final_text:
        warnings.append("No text extracted from HTML")

    if visuals:
        embedded  = sum(1 for v in visuals if v["image_bytes"])
        url_refs  = sum(1 for v in visuals if not v["image_bytes"])
        msg = f"{len(visuals)} visual(s) detected"
        if embedded:
            msg += f" — {embedded} embedded (bytes available)"
        if url_refs:
            msg += f", {url_refs} URL reference(s) (not fetched)"
        warnings.append(msg)

    return ExtractionResult(
        text=final_text,
        visuals=visuals,
        metadata=ExtractionMetadata(
            source_format="html", page_count=None, sheet_count=None,
            char_count=len(final_text), line_count=len(lines), word_count=len(words),
            encoding_used=encoding_used,
        ),
        extraction_warnings=warnings,
        extraction_success=bool(final_text.strip()),
    )
