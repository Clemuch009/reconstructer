# ingestion/detector.py

import os
import io
import zipfile
from typing import Optional
from ingestion.result import SourceFormat


# ---------------------------------
# Extension map — primary detection
# ---------------------------------

_EXT_MAP: dict[str, SourceFormat] = {
    ".txt":  "txt",
    ".log":  "txt",
    ".md":   "txt",
    ".csv":  "csv",
    ".tsv":  "csv",
    ".pdf":  "pdf",
    ".docx": "docx",
    ".doc":  "unknown",   # OLE2 — ambiguous, cannot safely classify
    ".xlsx": "xlsx",
    ".xls":  "unknown",   # OLE2 — ambiguous
    ".xlsm": "xlsx",
    ".html": "html",
    ".htm":  "html",
}


# ---------------------------------
# Magic bytes — secondary detection
# ---------------------------------

_MAGIC_BYTES: list[tuple[bytes, SourceFormat]] = [
    (b"%PDF",         "pdf"),
    (b"PK\x03\x04",  "xlsx"),    # OOXML — needs disambiguation
    # OLE2 removed — ambiguous, handled via extension only
]


# ---------------------------------
# OOXML disambiguation
# Reads full ZIP entry list — not header bytes
# ---------------------------------

def _detect_ooxml(raw_bytes: bytes) -> SourceFormat:
    """
    Distinguish .docx from .xlsx by inspecting ZIP entry names.
    Both share PK magic bytes — only internal structure reveals type.
    Reads full ZIP — not header slice — because entry names
    are not guaranteed to appear in the first N bytes.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as z:
            names = z.namelist()
            if any(name.startswith("word/") for name in names):
                return "docx"
            if any(name.startswith("xl/") for name in names):
                return "xlsx"
    except Exception:
        pass
    return "unknown"


# ---------------------------------
# HTML content sniff
# ---------------------------------

def _sniff_html(raw_bytes: bytes) -> bool:
    """
    Detect HTML content from bytes regardless of file extension.
    Checks first 200 bytes for HTML markers.
    """
    try:
        snippet = raw_bytes[:200].decode("utf-8", errors="ignore").lower().strip()
        return snippet.startswith("<html") or "<!doctype html" in snippet[:80]
    except Exception:
        return False


# ---------------------------------
# Main detector
# ---------------------------------

def detect_format(
    filename:  Optional[str] = None,
    raw_bytes: Optional[bytes] = None,
) -> SourceFormat:
    """
    Detect source format from filename extension and/or raw bytes.

    Detection order:
    1. Magic bytes (most reliable — content over name)
    2. HTML content sniff (before extension resolution)
    3. Extension map
    4. "unknown" if nothing resolves

    Rules:
    - Never raises — always returns a SourceFormat
    - Magic bytes take precedence over extension on conflict
    - HTML sniff runs before extension to catch misnamed files
    - OLE2 (.doc, .xls) returns "unknown" — too ambiguous to classify
    - "unknown" is valid — caller decides how to handle
    """
    magic_format: Optional[SourceFormat] = None
    ext_format:   Optional[SourceFormat] = None

    # Step 1 — magic bytes
    if raw_bytes and len(raw_bytes) >= 4:
        for magic, fmt in _MAGIC_BYTES:
            if raw_bytes[:len(magic)] == magic:
                if fmt == "xlsx":
                    # PK header — could be xlsx or docx. A successful probe
                    # returns "docx"/"xlsx"; a FAILED probe returns "unknown",
                    # which is NOT a positive magic result — treat it as no
                    # magic match so a valid extension hint (e.g. .xlsx/.docx)
                    # can still resolve below instead of being overridden.
                    probed = _detect_ooxml(raw_bytes)
                    magic_format = probed if probed != "unknown" else None
                else:
                    magic_format = fmt
                break

    # Step 2 — HTML content sniff (before extension)
    # Catches .txt or extensionless files containing HTML
    if raw_bytes and magic_format is None:
        if _sniff_html(raw_bytes):
            return "html"

    # Step 3 — extension map
    if filename:
        ext = os.path.splitext(filename.lower())[1]
        ext_format = _EXT_MAP.get(ext)

    # Step 4 — resolve
    if magic_format and ext_format:
        return magic_format   # magic wins on conflict
    if magic_format:
        return magic_format
    if ext_format:
        return ext_format

    return "unknown"
