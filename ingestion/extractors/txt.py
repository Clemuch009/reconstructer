# ingestion/extractors/txt.py

from ingestion.result import ExtractionResult, ExtractionMetadata


# ---------------------------------
# Encoding attempt chain
# Order matters — most specific first
# ---------------------------------

_ENCODING_CHAIN: list[tuple[str, str]] = [
    ("utf-8-sig",  "utf-8-sig"),        # UTF-8 with BOM — before plain utf-8
    ("utf-8",      "utf-8"),            # Standard UTF-8
    ("utf-16",     "utf-16"),           # UTF-16 with BOM (auto LE/BE)
    ("utf-16-le",  "utf-16-le"),        # UTF-16 Little Endian (Windows/SAP)
    ("utf-16-be",  "utf-16-be"),        # UTF-16 Big Endian
    ("latin-1",    "latin-1"),          # Windows cp1252 fallback
]


def extract_txt(raw_bytes: bytes) -> ExtractionResult:
    """
    Plain text extractor — passthrough with encoding detection.

    Encoding attempt order:
    1. UTF-8 with BOM
    2. UTF-8
    3. UTF-16 (auto BOM detection)
    4. UTF-16 LE (Windows/SAP/ERP exports)
    5. UTF-16 BE
    6. Latin-1 (cp1252 — most Windows text files)
    7. UTF-8 recovered (replace errors — last resort, never fails)

    Rules:
    - Never raises
    - Always returns extraction_success: True unless completely empty
    - Encoding chain recorded in extraction_warnings on fallback
    - No transformation — text passed as-is to engine
    - Null bytes removed silently with warning
    """
    warnings:      list[str] = []
    text:          str = ""
    encoding_used: str = "utf-8-recovered"

    # Attempt encoding chain — stop at first success
    for codec, label in _ENCODING_CHAIN:
        try:
            text = raw_bytes.decode(codec)
            encoding_used = label
            if label not in ("utf-8", "utf-8-sig"):
                warnings.append(
                    f"UTF-8 decode failed — decoded as {label}"
                )
            break
        except (UnicodeDecodeError, Exception):
            continue
    else:
        # All encodings failed — recover with replacement characters
        text = raw_bytes.decode("utf-8", errors="replace")
        encoding_used = "utf-8-recovered"
        warnings.append(
            "All encoding attempts failed — "
            "decoded as utf-8-recovered with replacement characters"
        )

    # Strip null bytes — common in Windows and ERP exports
    if "\x00" in text:
        text = text.replace("\x00", "")
        warnings.append("Null bytes removed from input")

    lines     = text.splitlines()
    words     = text.split()
    success   = bool(text.strip())

    if not success:
        warnings.append("Extracted text is empty")

    return ExtractionResult(
        text=text,
        metadata=ExtractionMetadata(
            source_format="txt",
            page_count=None,
            sheet_count=None,
            char_count=len(text),
            line_count=len(lines),
            word_count=len(words),
            encoding_used=encoding_used,
        ),
        extraction_warnings=warnings,
        extraction_success=success,
    )
