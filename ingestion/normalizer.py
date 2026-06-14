# ingestion/normalizer.py

import re
from ingestion.result import ExtractionResult


# ---------------------------------
# Normalizer rules
# Applied in order — each rule is independent
# ---------------------------------

# Maximum consecutive blank lines allowed
_MAX_BLANK_LINES = 2

# Maximum line length before warning
_MAX_LINE_LENGTH = 10_000


def _strip_format_noise(text: str, source_format: str) -> tuple[str, list[str]]:
    """
     Remove format-specific byte-level noise introduced by extractors.

    Strip format-specific artifacts that extractors may leave behind.
    These are extractor implementation details — not semantic content.

    Rules per format:
    - pdf:  strip form feed characters (page breaks in some PDFs)
    - docx: strip soft hyphens, non-breaking spaces normalized
    - xlsx: no artifacts — markers are semantic, preserved
    - html: no artifacts — already cleaned in extractor
    - csv:  no artifacts
    - txt:  no artifacts
    """
    warnings: list[str] = []

    if source_format == "pdf":
        # Form feed — page break artifact from some PDF renderers
        if "\x0c" in text:
            text = text.replace("\x0c", "\n")
            warnings.append("Form feed characters normalized to newlines")

    if source_format == "docx":
        # Soft hyphen — invisible in Word, noise in plain text
        if "\xad" in text:
            text = text.replace("\xad", "")
            warnings.append("Soft hyphens removed")
        # Non-breaking space → regular space
        if "\xa0" in text:
            text = text.replace("\xa0", " ")
            warnings.append("Non-breaking spaces normalized")

    return text, warnings


def _normalize_line_endings(text: str) -> str:
    """
    Normalize all line ending variants to Unix LF.
    CRLF (Windows), CR (old Mac) → LF.
    """
    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")
    return text


def _collapse_blank_lines(text: str, max_blank: int = _MAX_BLANK_LINES) -> str:
    """
    Collapse runs of blank lines to at most max_blank consecutive blanks.
    Preserves intentional paragraph breaks.
    """
    pattern = r"\n{%d,}" % (max_blank + 2)
    replacement = "\n" * (max_blank + 1)
    return re.sub(pattern, replacement, text)


def _check_line_lengths(text: str) -> list[str]:
    """
    Warn on extremely long lines — may indicate extraction issues.
    Does not truncate — engine receives full content.
    """
    warnings: list[str] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        if len(line) > _MAX_LINE_LENGTH:
            warnings.append(
                f"Line {idx}: {len(line)} characters — "
                f"exceeds {_MAX_LINE_LENGTH} character threshold"
            )
    return warnings


def _strip_leading_trailing_whitespace(text: str) -> str:
    """
    Strip leading and trailing whitespace from the full document.
    Per-line stripping is intentionally avoided — preserves
    indentation signals that the engine uses for hierarchy detection.
    """
    return text.strip()


# ---------------------------------
# Main normalizer
# ---------------------------------

def normalize_input(result: ExtractionResult) -> tuple[str, list[str]]:
    """
    Normalize extracted text before passing to engine.

    Pipeline:
    1. Strip format-specific extraction artifacts
    2. Normalize line endings → LF
    3. Collapse excessive blank lines
    4. Strip leading/trailing whitespace
    5. Check line lengths — warn, never truncate

    Rules:
    - Never modifies semantic content
    - Never modifies structural markers ([PAGE:], [SHEET:], [WORKBOOK])
    - Never strips indentation — engine uses it for hierarchy detection
    - Never raises
    - Returns (normalized_text, additional_warnings)

    The engine receives only the normalized text string.
    It never sees ExtractionResult or source_format.
    """
    warnings: list[str] = []
    text = result["text"]
    source_format = result["metadata"]["source_format"]

    # Step 1 — strip extraction artifacts
    text, artifact_warnings = _strip_extraction_artifacts(text, source_format)
    warnings.extend(artifact_warnings)

    # Step 2 — normalize line endings
    text = _normalize_line_endings(text)

    # Step 3 — collapse blank lines
    text = _collapse_blank_lines(text)

    # Step 4 — strip leading/trailing whitespace
    text = _strip_leading_trailing_whitespace(text)

    # Step 5 — check line lengths (warn only)
    length_warnings = _check_line_lengths(text)
    warnings.extend(length_warnings)

    return text, warnings
