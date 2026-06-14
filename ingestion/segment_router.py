# ingestion/segment_router.py

import io
from typing import List, Optional
from ingestion.segment import Segment, SegmentType
from ingestion.result import ExtractionResult, ExtractionMetadata


# ---------------------------------
# Segment extraction result
# ---------------------------------

from typing_extensions import TypedDict

class SegmentExtractionResult(TypedDict):
    segment:     Segment
    text:        str           # extracted text for this segment
    warnings:    List[str]


class SegmentedExtractionResult(TypedDict):
    text:                str          # joined text — engine input
    segments:            List[SegmentExtractionResult]
    all_warnings:        List[str]
    extraction_success:  bool
    segment_count:       int
    char_count:          int
    line_count:          int
    word_count:          int


# ---------------------------------
# Per-type extractors
# ---------------------------------

def _extract_html_segment(content: str) -> tuple[str, list[str]]:
    """
    Extract text from HTML segment using html extractor.
    Passes content as bytes — reuses full extractor pipeline.
    """
    warnings: list[str] = []
    try:
        from ingestion.extractors.html import extract_html
        result = extract_html(content.encode("utf-8"))
        warnings.extend(result["extraction_warnings"])
        return result["text"], warnings
    except Exception as e:
        warnings.append(f"HTML segment extraction failed: {str(e)[:200]}")
        return content, warnings


def _extract_csv_segment(content: str) -> tuple[str, list[str]]:
    """
    Extract text from CSV segment using csv extractor.
    """
    warnings: list[str] = []
    try:
        from ingestion.extractors.csv import extract_csv
        result = extract_csv(content.encode("utf-8"))
        warnings.extend(result["extraction_warnings"])
        return result["text"], warnings
    except Exception as e:
        warnings.append(f"CSV segment extraction failed: {str(e)[:200]}")
        return content, warnings


def _extract_json_segment(content: str) -> tuple[str, list[str]]:
    """
    Extract text from JSON segment.
    Passes as-is — engine handles KV extraction.
    Pretty-prints valid JSON for better engine readability.
    """
    warnings: list[str] = []
    try:
        import json
        parsed = json.loads(content)
        return json.dumps(parsed, indent=2, ensure_ascii=False), warnings
    except json.JSONDecodeError:
        warnings.append(
            "JSON segment is malformed — passing as raw text"
        )
        return content, warnings
    except Exception as e:
        warnings.append(f"JSON segment processing failed: {str(e)[:200]}")
        return content, warnings


def _extract_text_segment(content: str) -> tuple[str, list[str]]:
    """
    Text segment — passthrough.
    No transformation — engine receives as-is.
    """
    return content, []


def _extract_mixed_segment(content: str) -> tuple[str, list[str]]:
    """
    Mixed segment — low confidence, no dominant type.
    Pass as-is with warning.
    Engine handles whatever structure exists.
    """
    warnings = [
        "Mixed segment passed as-is — "
        "low classification confidence, engine will attempt structure detection"
    ]
    return content, warnings


# ---------------------------------
# Extractor dispatch
# ---------------------------------

_SEGMENT_EXTRACTORS = {
    "html":  _extract_html_segment,
    "csv":   _extract_csv_segment,
    "json":  _extract_json_segment,
    "text":  _extract_text_segment,
    "mixed": _extract_mixed_segment,
}


def _route_segment(segment: Segment) -> SegmentExtractionResult:
    """
    Route a single segment to its extractor.
    Returns SegmentExtractionResult with extracted text.

    Rules:
    - Never raises — failures captured in warnings
    - Unknown segment types fall through to text passthrough
    - Embedded structure warnings propagated from segment
    """
    seg_type  = segment["segment_type"]
    content   = segment["content"]
    warnings  = list(segment["warnings"])  # carry forward segment warnings

    extractor = _SEGMENT_EXTRACTORS.get(seg_type, _extract_text_segment)

    try:
        extracted_text, extract_warnings = extractor(content)
        warnings.extend(extract_warnings)
    except Exception as e:
        extracted_text = content
        warnings.append(
            f"Segment extraction failed for type={seg_type}: "
            f"{str(e)[:200]} — using raw content"
        )

    return SegmentExtractionResult(
        segment=segment,
        text=extracted_text,
        warnings=warnings,
    )


# ---------------------------------
# Section label injection
# ---------------------------------

def _inject_section_label(
    segment:        Segment,
    extracted_text: str,
    seg_index:      int,
) -> str:
    """
    Inject section label before segment text when confidence is low
    or segment is mixed — helps engine detect boundaries.

    Label format: [SEGMENT: {type}] — consistent with global marker format.
    Only injected for mixed or low-confidence segments.
    """
    if (
        segment["segment_type"] == "mixed"
        or segment["confidence"] < 0.5
    ):
        label = f"[SEGMENT: {segment['segment_type'].upper()}]"
        return f"{label}\n{extracted_text}"
    return extracted_text


# ---------------------------------
# Main segment router
# ---------------------------------

def route_segments(segments: List[Segment]) -> SegmentedExtractionResult:
    """
    Route all segments through their respective extractors.
    Join extracted text in document order for engine input.

    Pipeline:
    1. Route each segment to correct extractor
    2. Inject section labels for mixed/low-confidence segments
    3. Join all extracted texts in document order
    4. Return SegmentedExtractionResult

    Rules:
    - Document order preserved — segments processed in input order
    - Engine receives single joined text string
    - All warnings collected and surfaced
    - Never raises — segment failures captured in warnings
    """
    all_warnings:        list[str] = []
    segment_results:     list[SegmentExtractionResult] = []
    text_parts:          list[str] = []

    for idx, segment in enumerate(segments):
        result = _route_segment(segment)
        all_warnings.extend(result["warnings"])

        # Inject section label if needed
        final_text = _inject_section_label(
            segment, result["text"], idx
        )

        if final_text.strip():
            text_parts.append(final_text)

        segment_results.append(SegmentExtractionResult(
            segment=segment,
            text=final_text,
            warnings=result["warnings"],
        ))

    joined_text = "\n\n".join(text_parts)
    lines       = joined_text.splitlines()
    words       = joined_text.split()

    return SegmentedExtractionResult(
        text=joined_text,
        segments=segment_results,
        all_warnings=all_warnings,
        extraction_success=bool(joined_text.strip()),
        segment_count=len(segments),
        char_count=len(joined_text),
        line_count=len(lines),
        word_count=len(words),
    )
