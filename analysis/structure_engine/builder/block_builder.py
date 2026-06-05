# analysis/structure_engine/builder/block_builder.py

import hashlib
from typing import List, Dict, Any, Optional
from typing_extensions import TypedDict

from analysis.structure_engine.line_model import LineObject
from analysis.structure_engine.detectors.kv_detector import KVResult
from analysis.structure_engine.detectors.table_detector import TableResult
from analysis.structure_engine.detectors.context_detector import ContextResult
from analysis.structure_engine.builder.tree_builder import TreeResult


# ---------------------------------
# Contract
# ---------------------------------

class StructuredBlock(TypedDict):
    block_id:    int
    source:      str
    region_type: str
    content:     Dict[str, Any]
    start_line:  int
    end_line:    int
    line_count:  int
    confidence:  float


# ---------------------------------
# Deterministic block_id
# ---------------------------------

def _make_block_id(source: str, start_line: int, end_line: int) -> int:
    """
    Deterministic composite hash.
    Prevents block_id collisions across concurrent detector runs.
    """
    raw = f"{source}:{start_line}:{end_line}"
    return int(hashlib.sha256(raw.encode()).hexdigest(), 16) % (10 ** 12)


# ---------------------------------
# Boundary inference
# ---------------------------------

def _bounds_from_kv(results: List[KVResult]) -> Optional[tuple]:
    indices = [r["line_index"] for r in results]
    if not indices:
        return None
    return min(indices), max(indices)


def _bounds_from_lines(lines: List[LineObject]) -> Optional[tuple]:
    """
    Compute boundary from LineObject collection directly.
    Used by table_block — eliminates external caller context dependency.
    """
    indices = [l["line_index"] for l in lines]
    if not indices:
        return None
    return min(indices), max(indices)


def _bounds_from_context(result: ContextResult) -> Optional[tuple]:
    indices = [al["line_index"] for al in result["annotated_lines"]]
    if not indices:
        return None
    return min(indices), max(indices)


def _bounds_from_tree(result: TreeResult) -> Optional[tuple]:
    """
    Iterative traversal with visited guard.
    Prevents duplicate indices from shared child references
    corrupting boundary calculations.
    """
    indices: List[int] = []
    visited: set       = set()
    stack              = list(result["roots"])

    while stack:
        node = stack.pop()
        idx  = node["line_index"]
        if idx in visited:
            continue
        visited.add(idx)
        indices.append(idx)
        stack.extend(node["children"])

    return (min(indices), max(indices)) if indices else None


# ---------------------------------
# Structural validation
# ---------------------------------

def _validate_block(block: StructuredBlock) -> None:
    """
    Fail-fast structural gate.
    line_count enforced as physical coordinate footprint span.
    No semantic correction — fail loud on integrity failures.

    Confidence semantics by source:
    kv             → detection reliability of key/value extraction
    table          → structural certainty of cell intersections
    context        → classification purity of block label
    tree_structure → graph integrity and nesting completeness
    """
    if block["start_line"] > block["end_line"]:
        raise ValueError(
            f"block_id={block['block_id']}: "
            f"start_line {block['start_line']} > end_line {block['end_line']}"
        )

    if not (0.0 <= block["confidence"] <= 1.0):
        raise ValueError(
            f"block_id={block['block_id']}: "
            f"confidence {block['confidence']} out of [0.0, 1.0]"
        )

    expected_span = block["end_line"] - block["start_line"] + 1
    if block["line_count"] != expected_span:
        raise ValueError(
            f"block_id={block['block_id']}: "
            f"Expected footprint span of {expected_span}, "
            f"got {block['line_count']}"
        )


# ---------------------------------
# Block assembly
# ---------------------------------

def _make_block(
    source:      str,
    region_type: str,
    content:     Dict[str, Any],
    start_line:  int,
    end_line:    int,
    confidence:  float,
) -> StructuredBlock:
    block_id   = _make_block_id(source, start_line, end_line)
    line_count = end_line - start_line + 1

    block = StructuredBlock(
        block_id=block_id,
        source=source,
        region_type=region_type,
        content=dict(content),
        start_line=start_line,
        end_line=end_line,
        line_count=line_count,
        confidence=confidence,
    )

    _validate_block(block)
    return block


# ---------------------------------
# Source-specific builders
# ---------------------------------

def build_kv_block(results: List[KVResult]) -> Optional[StructuredBlock]:
    """
    Build block from KV detector results.
    Boundary inferred from line_index fields.

    UPSTREAM RESPONSIBILITY: Caller must split disconnected KV groups
    into contiguous runs before invoking this function.
    Disjoint groups passed as one list will produce an inaccurate
    oversized block absorbing unrelated lines between them.
    """
    if not results:
        return None

    bounds = _bounds_from_kv(results)
    if bounds is None:
        return None

    start_line, end_line   = bounds
    avg_confidence         = sum(r["confidence"] for r in results) / len(results)

    return _make_block(
        source="kv",
        region_type="kv_block",
        content={"kv_results": results},
        start_line=start_line,
        end_line=end_line,
        confidence=round(min(1.0, max(0.0, avg_confidence)), 2),
    )


def build_table_block(
    result: TableResult,
    lines:  List[LineObject],
) -> Optional[StructuredBlock]:
    """
    Build block from table detector result.
    Boundary computed from LineObject collection directly.
    Eliminates external caller context dependency.
    """
    if not result or not result["rows"]:
        return None
    if not lines:
        return None

    bounds = _bounds_from_lines(lines)
    if bounds is None:
        return None

    start_line, end_line = bounds

    return _make_block(
        source="table",
        region_type="table",
        content=dict(result),
        start_line=start_line,
        end_line=end_line,
        confidence=result["confidence"],
    )


def build_context_block(result: ContextResult) -> Optional[StructuredBlock]:
    """
    Build block from context detector result.
    Boundary inferred from annotated_lines line indices.
    """
    if not result or not result["annotated_lines"]:
        return None

    bounds = _bounds_from_context(result)
    if bounds is None:
        return None

    start_line, end_line = bounds

    return _make_block(
        source="context",
        region_type=result["region_type"],
        content=dict(result),
        start_line=start_line,
        end_line=end_line,
        confidence=result["confidence"],
    )


def build_tree_block(result: TreeResult) -> Optional[StructuredBlock]:
    """
    Build block from tree builder result.
    Boundary inferred via iterative traversal with visited guard.
    Confidence fixed at 1.0 — tree is assembled structure,
    not a detection result with probabilistic uncertainty.
    """
    if not result or not result["roots"]:
        return None

    bounds = _bounds_from_tree(result)
    if bounds is None:
        return None

    start_line, end_line = bounds

    return _make_block(
        source="tree_structure",
        region_type="tree_structure",
        content=dict(result),
        start_line=start_line,
        end_line=end_line,
        confidence=1.0,
    )


# ---------------------------------
# Sort blocks
# ---------------------------------

def sort_blocks(blocks: List[StructuredBlock]) -> List[StructuredBlock]:
    """
    Sort by document flow.
    Primary:   start_line ascending
    Secondary: end_line descending (widest block first on tie)
    """
    return sorted(
        blocks,
        key=lambda b: (b["start_line"], -b["end_line"])
    )


# ---------------------------------
# Core
# ---------------------------------

def build_all_blocks(
    kv_results:     Optional[List[KVResult]]   = None,
    table_result:   Optional[TableResult]      = None,
    table_lines:    Optional[List[LineObject]] = None,
    context_result: Optional[ContextResult]   = None,
    tree_result:    Optional[TreeResult]       = None,
) -> List[StructuredBlock]:
    """
    Assemble all available detector outputs into sorted StructuredBlocks.

    Rules:
    - Each source packaged independently — no merging
    - Overlaps preserved — reconciler owns arbitration
    - Blocks sorted by document flow
    - Failed or empty detectors produce no block — no crash
    - content is immutable copy of raw detector payload
    - table_lines required for table boundary computation
    """
    blocks: List[StructuredBlock] = []

    if kv_results:
        block = build_kv_block(kv_results)
        if block:
            blocks.append(block)

    if table_result and table_lines:
        block = build_table_block(table_result, table_lines)
        if block:
            blocks.append(block)

    if context_result:
        block = build_context_block(context_result)
        if block:
            blocks.append(block)

    if tree_result:
        block = build_tree_block(tree_result)
        if block:
            blocks.append(block)

    return sort_blocks(blocks)


# ---------------------------------
# Interactive Test
# ---------------------------------

if __name__ == "__main__":
    import sys
    from analysis.structure_engine.line_model import build_line_model
    from analysis.structure_engine.pattern_extractor import extract_all_patterns
    from analysis.structure_engine.detectors.kv_detector import detect_all_kv
    from analysis.structure_engine.detectors.context_detector import detect_context

    print("\n" + "=" * 60)
    print("BLOCK BUILDER INTERACTIVE TEST")
    print("=" * 60)
    print("Paste text then Ctrl+D to process. 'exit' to quit.\n")

    while True:
        print("INPUT> ", end="", flush=True)
        try:
            raw = sys.stdin.read()
        except EOFError:
            break

        if raw.strip().lower() == "exit":
            print("Exiting.")
            break

        model    = build_line_model(raw)
        patterns = extract_all_patterns(model)
        kv       = detect_all_kv(model, patterns)
        context  = detect_context(model, kv)

        blocks = build_all_blocks(
            kv_results=kv     if kv      else None,
            context_result=context,
        )

        print(f"\n--- BLOCKS ({len(blocks)}) ---\n")
        for b in blocks:
            print(f"  block_id   : {b['block_id']}")
            print(f"  source     : {b['source']}")
            print(f"  region_type: {b['region_type']}")
            print(f"  lines      : {b['start_line']}–{b['end_line']} "
                  f"({b['line_count']} lines)")
            print(f"  confidence : {b['confidence']}")
            print()

        print("-" * 60)
