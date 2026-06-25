import sys
from typing import List, Optional
from typing_extensions import TypedDict

from analysis.structure_engine.line_model import build_line_model, LineObject
from analysis.structure_engine.pattern_extractor import extract_all_patterns
from analysis.structure_engine.sub_segmenter import sub_segment, SubSegment
from analysis.structure_engine.router import route_lines, RoutedRegion
from analysis.structure_engine.detectors.kv_detector import detect_all_kv
from analysis.structure_engine.detectors.table_detector import detect_table
from analysis.structure_engine.detectors.context_detector import detect_context
from analysis.structure_engine.detectors.hierarchy_detector import detect_hierarchy
from analysis.structure_engine.builder.tree_builder import build_tree
from analysis.structure_engine.builder.block_builder import (
    build_all_blocks,
    StructuredBlock,
)
from analysis.structure_engine.reconciler import reconcile
from analysis.structure_engine.classifier import classify, ClassificationResult


# ---------------------------------
# Per-segment result contract
# ---------------------------------

class SegmentResult(TypedDict):
    sub_segment_index: int
    start_line:        int
    end_line:          int
    engine_routed:     str
    classification:    ClassificationResult
    block_count:       int
    blocks:            List[StructuredBlock]   # reconciled blocks (for formatter)
    lines:             List[LineObject]        # segment lines (for gap detection)


# ---------------------------------
# Engine dispatch
# ---------------------------------

def _run_structure_engine(
    lines:       List[LineObject],
    total_lines: int,
) -> List[StructuredBlock]:
    """
    Structure engine path — table_detector only.
    Receives high-confidence table_candidate regions.
    """
    blocks = []

    table_result = detect_table(lines)
    if table_result:
        from analysis.structure_engine.builder.block_builder import build_table_block
        block = build_table_block(table_result, lines)
        if block:
            blocks.append(block)

    return blocks


def _run_block_engine(
    lines:       List[LineObject],
    total_lines: int,
) -> List[StructuredBlock]:
    """
    Block engine path — kv, context, hierarchy, tree.
    Receives structured_block regions and low-confidence table candidates.
    """
    blocks = []

    # KV detection
    patterns   = extract_all_patterns(lines)
    kv_results = detect_all_kv(lines, patterns)

    # Context detection — consumes kv as read-only references
    context_result = detect_context(lines, kv_results if kv_results else None)

    # Hierarchy detection
    hierarchy_result = detect_hierarchy(lines)

    # Tree building from hierarchy
    tree_result = None
    if hierarchy_result:
        tree_result = build_tree(hierarchy_result)

    # Assemble blocks
    blocks = build_all_blocks(
        kv_results=kv_results     if kv_results     else None,
        context_result=context_result,
        tree_result=tree_result,
    )

    return blocks


def _run_fallback_engine(
    lines:       List[LineObject],
    total_lines: int,
) -> List[StructuredBlock]:
    """
    Fallback engine path — context only.
    Receives unstructured regions.
    """
    context_result = detect_context(lines)
    blocks = build_all_blocks(context_result=context_result)
    return blocks


# ---------------------------------
# Single sub-segment processing
# ---------------------------------

def _process_sub_segment(
    idx:         int,
    sub_seg:     SubSegment,
    total_lines: int,
) -> SegmentResult:
    """
    Route and process a single sub-segment through the structure engine.
    """
    lines  = sub_seg["lines"]
    routed = route_lines(lines)
    engine = routed["engine"]

    if engine == "structure_engine":
        blocks = _run_structure_engine(lines, total_lines)
    elif engine == "block_engine":
        blocks = _run_block_engine(lines, total_lines)
    else:
        blocks = _run_fallback_engine(lines, total_lines)

    reconciled     = reconcile(blocks, total_lines)
    classification = classify(reconciled, total_lines)

    return SegmentResult(
        sub_segment_index=idx,
        start_line=sub_seg["start_line"],
        end_line=sub_seg["end_line"],
        engine_routed=engine,
        classification=classification,
        block_count=classification["block_count"],
        blocks=reconciled["blocks"],
        lines=lines,
    )


# ---------------------------------
# Core orchestrator
# ---------------------------------

def run_structure_engine(text: str) -> List[SegmentResult]:
    """
    Full structure_engine pipeline orchestration.

    Flow:
    raw text
        ↓ build_line_model
        ↓ sub_segment
        ↓ for each sub_segment:
            route_lines
            → structure_engine  (table_detector)
            → block_engine      (kv + context + hierarchy + tree)
            → fallback_engine   (context only)
            ↓ reconcile
            ↓ classify
        ↓ List[SegmentResult]

    Returns one SegmentResult per sub-segment.
    """
    if not text or not isinstance(text, str):
        return []

    model       = build_line_model(text)
    total_lines = len(model)
    import sys

    print(f"[engine-debug] received {len(text)} chars, {len(text.splitlines())} lines; first 200: {text[:200]!r}", file=sys.stderr)

    if not model:
        return []

    sub_segments = sub_segment(model)

    if not sub_segments:
        # No structural boundaries — treat entire input as one segment
        dummy = {
            "lines":         model,
            "start_line":    model[0]["line_index"],
            "end_line":      model[-1]["line_index"],
            "boundary_type": "terminal",
            "trigger":       "eof",
            "boundary_marker": False,
        }
        return [_process_sub_segment(0, dummy, total_lines)]

    results = []
    for idx, sub_seg in enumerate(sub_segments):
        result = _process_sub_segment(idx, sub_seg, total_lines)
        results.append(result)

    return results


# ---------------------------------
# Interactive Test
# ---------------------------------

def _print_result(result: SegmentResult) -> None:
    c = result["classification"]
    print(f"\n[Sub-segment {result['sub_segment_index']}]"
          f" lines {result['start_line']}–{result['end_line']}"
          f" | engine: {result['engine_routed']}")
    print(f"  structure_type            : {c['structure_type']}")
    print(f"  classification_confidence : {c['classification_confidence']}")
    print(f"  dominant_source           : {c['dominant_source']}")
    print(f"  block_count               : {c['block_count']}")
    print(f"  type_entropy              : {c['type_entropy']}")
    print(f"  evidence                  : {c['evidence']}")
    print(f"  density_profile           : {c['density_profile']}")
