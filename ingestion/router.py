# ingestion/router.py

from typing import Optional
from typing_extensions import TypedDict

from ingestion.result import ExtractionResult, ExtractionMetadata, SourceFormat
from ingestion.detector import detect_format
from ingestion.normalizer import normalize_input


# ---------------------------------
# Ingestion result — final output
# ---------------------------------

class IngestionResult(TypedDict):
    text:                   str              # normalized text — engine input only
    extraction_result:      ExtractionResult # full extraction metadata
    normalization_warnings: list[str]        # warnings from normalizer
    all_warnings:           list[str]        # all warnings combined
    source_format:          SourceFormat     # detected or forced format
    ingestion_success:      bool
    pipeline:               str              # "single" | "segmented"


# ---------------------------------
# Pipeline A — single extractor
# Known format: txt, csv, pdf, docx, xlsx, html
# ---------------------------------

def _get_extractor(source_format: SourceFormat):
    """
    Return extractor function for known format.
    Deferred imports — optional dependencies loaded only when needed.
    """
    if source_format == "txt":
        from ingestion.extractors.txt import extract_txt
        return extract_txt
    elif source_format == "csv":
        from ingestion.extractors.csv import extract_csv
        return extract_csv
    elif source_format == "pdf":
        from ingestion.extractors.pdf import extract_pdf
        return extract_pdf
    elif source_format == "docx":
        from ingestion.extractors.docx import extract_docx
        return extract_docx
    elif source_format == "xlsx":
        from ingestion.extractors.xlsx import extract_xlsx
        return extract_xlsx
    elif source_format == "html":
        from ingestion.extractors.html import extract_html
        return extract_html
    return None


def _run_single_pipeline(
    raw_bytes:     bytes,
    source_format: SourceFormat,
    all_warnings:  list[str],
) -> tuple[ExtractionResult, str, list[str]]:
    """
    Pipeline A — single extractor path.

    raw_bytes → extractor → ExtractionResult → normalizer → text

    Returns (extraction_result, normalized_text, normalization_warnings).
    """
    extractor = _get_extractor(source_format)

    if extractor is None:
        empty_result = ExtractionResult(
            text="",
            metadata=ExtractionMetadata(
                source_format=source_format,
                page_count=None,
                sheet_count=None,
                char_count=0,
                line_count=0,
                word_count=0,
                encoding_used=None,
            ),
            extraction_warnings=[
                f"No extractor registered for format: {source_format}"
            ],
            extraction_success=False,
        )
        return empty_result, "", []

    extraction_result = extractor(raw_bytes)
    all_warnings.extend(extraction_result["extraction_warnings"])

    if not extraction_result["extraction_success"]:
        return extraction_result, "", []

    normalized_text, norm_warnings = normalize_input(extraction_result)
    return extraction_result, normalized_text, norm_warnings


# ---------------------------------
# Pipeline B — segmented path
# Unknown/mixed format:
# decode only → segmenter → segment_router → normalizer
# ---------------------------------

def _run_segmented_pipeline(
    raw_bytes:    bytes,
    filename:     Optional[str],
    all_warnings: list[str],
) -> tuple[ExtractionResult, str, list[str]]:
    """
    Pipeline B — segmented path for unknown/mixed format files.

    raw_bytes
        ↓
    handle_unknown()     — decode only, produce signals + format_hint
        ↓
    segment()            — segment raw decoded text, preserve original structure
        ↓
    route_segments()     — each segment extracted from original raw content
        ↓
    normalize_input()    — final normalization before engine

    Rules:
    - Segmenter receives raw decoded text — not extracted/normalized text
    - Each segment carries original raw content
    - segment_router extracts from original segment content
    - Engine receives single joined normalized text
    """
    from ingestion.unknown_handler import handle_unknown
    from ingestion.segmenter import segment
    from ingestion.segment_router import route_segments

    # Step 1 — decode only, generate signals
    handler_result = handle_unknown(raw_bytes, filename)
    all_warnings.extend(handler_result.warnings)

    # Binary — cannot process
    if handler_result.is_binary:
        empty_result = ExtractionResult(
            text="",
            metadata=ExtractionMetadata(
                source_format="unknown",
                page_count=None,
                sheet_count=None,
                char_count=0,
                line_count=0,
                word_count=0,
                encoding_used=None,
            ),
            extraction_warnings=handler_result.warnings,
            extraction_success=False,
        )
        return empty_result, "", []

    # Step 2 — segment raw decoded text
    # Segmenter sees original structure — not extracted output
    decoded_text = handler_result.decoded_text
    segments     = segment(decoded_text)

    if not segments:
        all_warnings.append(
            "Segmentation produced no segments — "
            "falling back to raw decoded text"
        )
        segments = []

    # Step 3 — route segments through extractors
    if segments:
        seg_result = route_segments(segments)
        all_warnings.extend(seg_result["all_warnings"])
        extracted_text = seg_result["text"]
        char_count     = seg_result["char_count"]
        line_count     = seg_result["line_count"]
        word_count     = seg_result["word_count"]
    else:
        # Fallback — use raw decoded text directly
        extracted_text = decoded_text
        char_count     = len(decoded_text)
        line_count     = len(decoded_text.splitlines())
        word_count     = len(decoded_text.split())

    extraction_result = ExtractionResult(
        text=extracted_text,
        metadata=ExtractionMetadata(
            source_format="unknown",
            page_count=None,
            sheet_count=None,
            char_count=char_count,
            line_count=line_count,
            word_count=word_count,
            encoding_used=handler_result.encoding_used,
        ),
        extraction_warnings=all_warnings[:],
        extraction_success=bool(extracted_text.strip()),
    )

    if not extraction_result["extraction_success"]:
        return extraction_result, "", []

    # Step 4 — normalize
    normalized_text, norm_warnings = normalize_input(extraction_result)
    return extraction_result, normalized_text, norm_warnings


# ---------------------------------
# Main ingestion entry point
# ---------------------------------

def ingest(
    raw_bytes:    bytes,
    filename:     Optional[str] = None,
    force_format: Optional[SourceFormat] = None,
) -> IngestionResult:
    """
    Route raw bytes through the correct ingestion pipeline.

    Pipeline A — single extractor (known formats):
        raw_bytes → extractor → normalizer → engine

    Pipeline B — segmented (unknown/mixed formats):
        raw_bytes → decode → segmenter → segment_router → normalizer → engine

    Rules:
    - Engine never sees raw_bytes, filename, or source_format
    - Engine only receives normalized text string
    - force_format bypasses detection — for testing and explicit overrides
    - unknown format → Pipeline B (segmented), never a hard failure
    - Never raises — all failures captured in warnings + ingestion_success
    """
    all_warnings:  list[str] = []
    pipeline_used: str = "single"

    # Step 1 — detect format
    source_format: SourceFormat = (
        force_format
        if force_format
        else detect_format(filename=filename, raw_bytes=raw_bytes)
    )

    # Step 2 — select pipeline
    if source_format == "unknown":
        # Pipeline B — segmented
        pipeline_used = "segmented"
        extraction_result, normalized_text, norm_warnings = _run_segmented_pipeline(
            raw_bytes, filename, all_warnings
        )
    else:
        # Pipeline A — single extractor
        pipeline_used = "single"
        extraction_result, normalized_text, norm_warnings = _run_single_pipeline(
            raw_bytes, source_format, all_warnings
        )

    all_warnings.extend(norm_warnings)

    ingestion_success = (
        extraction_result["extraction_success"]
        and bool(normalized_text.strip())
    )

    if not ingestion_success and normalized_text.strip():
        all_warnings.append(
            "Extraction reported failure but normalized text is non-empty — "
            "proceeding with available content"
        )
        ingestion_success = True

    return IngestionResult(
        text=normalized_text,
        extraction_result=extraction_result,
        normalization_warnings=norm_warnings,
        all_warnings=all_warnings,
        source_format=source_format,
        ingestion_success=ingestion_success,
        pipeline=pipeline_used,
    )
