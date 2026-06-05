import hashlib
from typing import List, Dict, Set, Optional, Tuple
from typing_extensions import TypedDict
from analysis.structure_engine.builder.block_builder import StructuredBlock


# ---------------------------------
# Contract
# ---------------------------------

class ReconcilerResult(TypedDict):
    region_type:   str
    blocks:        List[StructuredBlock]
    dropped_count: int
    overlap_count: int
    source_counts: Dict[str, int]


# ---------------------------------
# Constants
# ---------------------------------

SOURCE_PRIORITY: Dict[str, int] = {
    "table":          5,
    "tree_structure": 4,
    "context":        3,
    "kv":             2,
}

MAX_PRIORITY   = max(SOURCE_PRIORITY.values())
MIN_CONFIDENCE = 0.15

# Rebalanced weights — confidence dominates, priority is tie-breaker
W_CONFIDENCE = 0.75
W_PRIORITY   = 0.15
W_SPAN       = 0.10

# Conflict pairs — only same-source or structurally incompatible sources conflict
# Everything else coexists
CONFLICT_PAIRS: Set[Tuple[str, str]] = {
    ("table",          "table"),
    ("kv",             "kv"),
    ("context",        "context"),
    ("tree_structure", "tree_structure"),
    ("table",          "kv"),
    ("kv",             "table"),
}


# ---------------------------------
# Deterministic block_id
# ---------------------------------

def _new_block_id(source: str, start_line: int, end_line: int) -> int:
    raw = f"{source}:{start_line}:{end_line}"
    return int(hashlib.sha256(raw.encode()).hexdigest(), 16) % (10 ** 12)


# ---------------------------------
# Arbitration scoring
# ---------------------------------

def _arbitration_score(
    block: StructuredBlock,
    total_lines: int,
) -> float:
    p_norm = SOURCE_PRIORITY.get(block["source"], 1) / MAX_PRIORITY
    s_coh  = min(1.0, block["line_count"] / max(1, total_lines))
    return round(
        block["confidence"] * W_CONFIDENCE +
        p_norm               * W_PRIORITY   +
        s_coh                * W_SPAN,
        6
    )


# ---------------------------------
# Tie-breaking key
# ---------------------------------

def _tie_break_key(block: StructuredBlock) -> tuple:
    return (
        -SOURCE_PRIORITY.get(block["source"], 0),
        -block["line_count"],
        block["start_line"],
        block["source"],
    )


# ---------------------------------
# Overlap and conflict checks
# ---------------------------------

def _overlaps(a: StructuredBlock, b: StructuredBlock) -> bool:
    return a["start_line"] <= b["end_line"] and b["start_line"] <= a["end_line"]


def _conflicts(a: StructuredBlock, b: StructuredBlock) -> bool:
    """
    True if blocks are structurally incompatible.
    Incompatible = same source type or explicitly conflicting pair.
    Everything else coexists.
    """
    return (a["source"], b["source"]) in CONFLICT_PAIRS


def _is_contained(
    outer: StructuredBlock,
    inner: StructuredBlock,
) -> bool:
    """Returns True if outer fully contains inner."""
    return (
        outer["start_line"] <= inner["start_line"] and
        outer["end_line"]   >= inner["end_line"]
    )


def _is_partial_edge(a: StructuredBlock, b: StructuredBlock) -> bool:
    """
    True if overlap is a partial edge — neither block fully contains the other.
    Only these cases are candidates for trailing-edge arbitration.
    """
    return (
        _overlaps(a, b) and
        not _is_contained(a, b) and
        not _is_contained(b, a)
    )


# ---------------------------------
# Active window maintenance
# ---------------------------------

def _get_active(
    accepted: List[StructuredBlock],
    block: StructuredBlock,
) -> List[Tuple[int, StructuredBlock]]:
    """
    Returns (index, block) pairs from accepted that overlap with block.
    Active window — not just tail — required for coexistence correctness.
    Pruned to overlapping range only for near-O(N) amortized performance.
    """
    return [
        (i, b) for i, b in enumerate(accepted)
        if b["end_line"] >= block["start_line"] and _overlaps(b, block)
    ]


# ---------------------------------
# Sweep-line reconciliation
# ---------------------------------

def _reconcile_stream(
    blocks:      List[StructuredBlock],
    total_lines: int,
) -> Tuple[List[StructuredBlock], int, int]:
    """
    Active-window sweep reconciliation.

    Complexity:
    - O(N log N) sort
    - O(N * W) sweep where W = active window size
    - W stays small in well-segmented input
    - Degrades to O(N²) only in pathological fully-overlapping input

    Rules:
    - Coexisting blocks admitted without arbitration
    - Conflicting overlapping blocks arbitrated by score
    - Containment → arbitration only, no clipping
    - Partial edge → arbitration only, no clipping
    - Losers always dropped whole — detector payloads never modified
    - No in-place mutation of accepted list during iteration
    """
    if not blocks:
        return [], 0, 0

    sorted_blocks = sorted(
    blocks,
    key=lambda b: (
        b["start_line"],
        -SOURCE_PRIORITY.get(b["source"], 0),  # tables/trees before context
        -b["end_line"],
        -_arbitration_score(b, total_lines),
        _tie_break_key(b),
    )
)

    accepted:      List[StructuredBlock] = []
    dropped_ids:   Set[int]              = set()
    dropped_count: int                   = 0
    overlap_count: int                   = 0

    for block in sorted_blocks:

        if block["block_id"] in dropped_ids:
            dropped_count += 1
            continue

        # Get all active overlapping blocks
        active = _get_active(accepted, block)

        if not active:
            # No overlap — insert directly
            accepted.append(block)
            continue

        block_score    = _arbitration_score(block, total_lines)
        drop_incoming  = False


        blocks_to_drop: Set[int] = set()

        for idx, existing in active:
            if not _conflicts(block, existing):
                continue

            overlap_count += 1
            existing_score = _arbitration_score(existing, total_lines)

            if block_score >= existing_score:
                blocks_to_drop.add(existing["block_id"])
                dropped_ids.add(existing["block_id"])
                dropped_count += 1
            else:
                drop_incoming = True
                dropped_ids.add(block["block_id"])
                dropped_count += 1
                break

        if drop_incoming:
            continue

        # Rebuild accepted using object references — no index shifting
        accepted = [b for b in accepted if b["block_id"] not in blocks_to_drop]
        accepted.append(block)

    # Final sort by document flow
    accepted.sort(key=lambda b: (b["start_line"], -b["end_line"]))

    return accepted, dropped_count, overlap_count


# ---------------------------------
# Core
# ---------------------------------

def reconcile(
    blocks:      List[StructuredBlock],
    total_lines: int,
) -> ReconcilerResult:
    """
    Arbitrate and normalize StructuredBlocks into a clean stream.

    Execution sequence:
    1. Deduplicate — identical block_ids silently dropped
    2. Confidence gate — blocks below MIN_CONFIDENCE dropped
    3. Active-window sweep reconciliation
    4. Source counts from final accepted blocks

    Rules:
    - Heterogeneous non-conflicting blocks coexist
    - Conflicting blocks arbitrated by score
    - No clipping — losers dropped whole
    - Detector payloads never mutated
    - Output is chronologically sorted

    Known limitation:
    - Priority weights may need revisiting as real data accumulates
    - High-confidence kv may deserve to outrank weak table in future
    """
    if not blocks:
        return ReconcilerResult(
            region_type="reconciled_stream",
            blocks=[],
            dropped_count=0,
            overlap_count=0,
            source_counts={},
        )

    dropped_count = 0

    # Step 1 — Deduplication
    seen_ids: Set[int]              = set()
    deduped:  List[StructuredBlock] = []

    for block in blocks:
        if block["block_id"] in seen_ids:
            dropped_count += 1
            continue
        seen_ids.add(block["block_id"])
        deduped.append(block)

    # Step 2 — Confidence gate
    filtered: List[StructuredBlock] = []
    for block in deduped:
        if block["confidence"] < MIN_CONFIDENCE:
            dropped_count += 1
        else:
            filtered.append(block)

    # Step 3 — Active-window sweep
    accepted, reconcile_dropped, overlap_count = _reconcile_stream(
        filtered, total_lines
    )
    dropped_count += reconcile_dropped

    # Step 4 — Source counts
    source_counts: Dict[str, int] = {}
    for block in accepted:
        source_counts[block["source"]] = (
            source_counts.get(block["source"], 0) + 1
        )

    return ReconcilerResult(
        region_type="reconciled_stream",
        blocks=accepted,
        dropped_count=dropped_count,
        overlap_count=overlap_count,
        source_counts=source_counts,
    )


# ---------------------------------
# Interactive Test
# ---------------------------------

if __name__ == "__main__":
    import sys
    from analysis.structure_engine.line_model import build_line_model
    from analysis.structure_engine.pattern_extractor import extract_all_patterns
    from analysis.structure_engine.detectors.kv_detector import detect_all_kv
    from analysis.structure_engine.detectors.context_detector import detect_context
    from analysis.structure_engine.builder.block_builder import build_all_blocks

    print("\n" + "=" * 60)
    print("RECONCILER INTERACTIVE TEST")
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

        blocks      = build_all_blocks(
            kv_results=kv if kv else None,
            context_result=context,
        )
        total_lines = len(model)
        result      = reconcile(blocks, total_lines)

        print(f"\n--- RECONCILER RESULT ---\n")
        print(f"  region_type  : {result['region_type']}")
        print(f"  block_count  : {len(result['blocks'])}")
        print(f"  dropped      : {result['dropped_count']}")
        print(f"  overlaps     : {result['overlap_count']}")
        print(f"  source_counts: {result['source_counts']}")
        print(f"\n  blocks:")
        for b in result["blocks"]:
            print(
                f"    [{b['start_line']}–{b['end_line']}] "
                f"{b['source']:15} "
                f"conf={b['confidence']:.2f} "
                f"lines={b['line_count']}"
            )

        print("-" * 60)
