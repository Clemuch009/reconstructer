# ingestion/visual.py
#
# EmbeddedVisual — shared contract for all extracted visuals.
# Used by pdf.py, docx.py, html.py extractors.
# Stored as raw bytes in memory.
# Serialized to base64 only at API response time.

from typing import Optional, List
from typing_extensions import TypedDict


class EmbeddedVisual(TypedDict):
    id:          str            # "vis_001", "vis_002" ...
    page:        Optional[int]  # PDF page number, None for DOCX/HTML
    mime_type:   str            # "image/png" | "image/jpeg" | "image/svg+xml" etc.
    width:       Optional[int]  # pixels, None if unknown
    height:      Optional[int]  # pixels, None if unknown
    image_bytes: bytes          # raw bytes — NOT base64
    warnings:    List[str]


def make_visual_id(index: int) -> str:
    """
    Generate a zero-padded visual ID.
    index is 1-based.
    """
    return f"vis_{index:03d}"


def visual_placeholder(visual_id: str) -> str:
    """
    Inline placeholder inserted at the visual's position in text flow.
    """
    return f"[VISUAL: {visual_id}]"
