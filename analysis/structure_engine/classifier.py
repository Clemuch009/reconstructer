# analysis/structure_engine/classifier.py

import math
from typing import Dict, List
from typing_extensions import TypedDict
from analysis.structure_engine.builder.block_builder import StructuredBlock
from analysis.structure_engine.reconciler import ReconcilerResult


# ---------------------------------
# Contract
# ---------------------------------

class ClassificationResult(TypedDict):
    region_type:               str
    structure_type:            str
    classification_confidence: float
    dominant_source:           str
    block_count:               int
    dominant_coverage_lines:   int
    type_entropy:              float
    evidence:                  Dict[str, float]
    density_profile:           Dict[str, int]


# ---------------------------------
# Constants
# ---------------------------------

SOURCE_WEIGHT: Dict[str, float] = {
    "table":          1.0,
    "tree_structure": 0.9,
    "kv":             0.8,
    "context":        0.7,
}

SOURCE_TO_LABEL: Dict[str, str] = {
    "table":          "table",
    "kv":             "kv_block",
    "tree_structure": "hierarchy",
    "context":        "context",
}

W_CONFIDENCE     = 0.60
W_SPAN           = 0.25
W_PRIORITY       = 0.15

MIXED_THRESHOLD       = 0.15
MIN_STRUCTURE_SIGNAL  = 0.10
HIGH_ENTROPY_THRESHOLD = 0.85   # entropy above this forces mixed


# ---------------------------------
# Block scoring
# ---------------------------------

def _block_score(
    block:       StructuredBlock,
    total_lines: int,
) -> float:
    span_ratio      = min(1.0, block["line_count"] / max(1, total_lines))
    source_priority = SOURCE_WEIGHT.get(block["source"], 0.5)

    return round(
        block["confidence"] * W_CONFIDENCE +
        span_ratio          * W_SPAN       +
        source_priority     * W_PRIORITY,
        6
    )


# ---------------------------------
# Evidence aggregation — bounded
# ---------------------------------

def _suppress_redundant_context(
    blocks: List[StructuredBlock],
) -> List[StructuredBlock]:
    """
    `context` is the generic fallback signal for line-structured content. The
    context detector annotates each line (kv / log / telemetry / prose), so a
    block of key-value lines is emitted BOTH as a specific `kv` block AND as a
    generic `context` block covering the SAME lines. Downstream this reads as
    two near-tied competing labels (kv_block vs context), tripping the
    high-entropy / small-gap gate and forcing an otherwise-pure kv region to
    `mixed`. Same shadowing can affect table/hierarchy regions.

    Fix: drop a `context` block when its line span is substantially covered by a
    more-specific block (kv, table, tree/hierarchy). The specific detector has
    already claimed that content; the generic context copy is redundant and
    should not compete. Genuinely-mixed regions are unaffected — a context block
    that covers DIFFERENT lines than the specific blocks (real prose/log mixed
    in) is kept, so multi-type regions still resolve to `mixed`.

    Overlap is measured as the fraction of the context block's own lines that
    fall inside any specific block's span; >= OVERLAP_SUPPRESS_RATIO means the
    context block is redundant.
    """
    SPECIFIC_SOURCES = {"kv", "table", "tree_structure"}
    OVERLAP_SUPPRESS_RATIO = 0.8

    specific_spans = [
        (b["start_line"], b["end_line"])
        for b in blocks
        if b["source"] in SPECIFIC_SOURCES
    ]
    if not specific_spans:
        return blocks

    def _covered_fraction(ctx_start: int, ctx_end: int) -> float:
        ctx_lines = set(range(ctx_start, ctx_end + 1))
        if not ctx_lines:
            return 0.0
        covered = set()
        for s, e in specific_spans:
            covered |= (ctx_lines & set(range(s, e + 1)))
        return len(covered) / len(ctx_lines)

    kept: List[StructuredBlock] = []
    for b in blocks:
        if b["source"] == "context":
            frac = _covered_fraction(b["start_line"], b["end_line"])
            if frac >= OVERLAP_SUPPRESS_RATIO:
                # Redundant with a specific block — drop it.
                continue
        kept.append(b)
    return kept


def _aggregate_evidence(
    blocks:      List[StructuredBlock],
    total_lines: int,
) -> Dict[str, float]:
    """
    Bounded evidence via max-signal + log-scaled frequency boost.
    Prevents fragmentation inflation from many small blocks.
    Caps at 1.0 regardless of block count.

    Formula per label:
    evidence = min(1.0, max_signal + min(0.2, (count - 1) * 0.05))
    """
    raw_accum: Dict[str, List[float]] = {}

    for block in blocks:
        label = SOURCE_TO_LABEL.get(block["source"], block["source"])
        raw_accum.setdefault(label, []).append(
            _block_score(block, total_lines)
        )

    evidence: Dict[str, float] = {}
    for label, scores in raw_accum.items():
        max_sig    = max(scores)
        freq_boost = min(0.2, (len(scores) - 1) * 0.05)
        evidence[label] = round(min(1.0, max_sig + freq_boost), 6)

    return evidence


# ---------------------------------
# Density profile
# ---------------------------------

def _build_density_profile(
    blocks:      List[StructuredBlock],
) -> Dict[str, int]:
    """
    Absolute line counts per structure label.
    """
    profile: Dict[str, int] = {}
    for block in blocks:
        label = SOURCE_TO_LABEL.get(block["source"], block["source"])
        profile[label] = profile.get(label, 0) + block["line_count"]
    return profile


# ---------------------------------
# Type entropy
# ---------------------------------

def _compute_entropy(evidence: Dict[str, float]) -> float:
    """
    Shannon entropy of evidence distribution.
    High entropy = many competing structural types.
    Low entropy = one dominant type.
    Normalized to [0.0, 1.0] via log2(n).
    """
    if not evidence:
        return 0.0

    total = sum(evidence.values())
    if total == 0.0:
        return 0.0

    n = len(evidence)
    if n == 1:
        return 0.0

    entropy = 0.0
    for score in evidence.values():
        p = score / total
        if p > 0:
            entropy -= p * math.log2(p)

    max_entropy = math.log2(n)
    return round(entropy / max_entropy if max_entropy > 0 else 0.0, 4)


# ---------------------------------
# Dominant source — deterministic
# ---------------------------------

def _find_dominant_source(
    blocks:        List[StructuredBlock],
    winning_label: str,
    total_lines:   int,
) -> str:
    """
    Highest scoring block matching winning label.
    Deterministic — no order dependency.
    """
    candidates = [
        b for b in blocks
        if SOURCE_TO_LABEL.get(b["source"], b["source"]) == winning_label
    ]
    if not candidates:
        return "unknown"
    best = max(candidates, key=lambda b: _block_score(b, total_lines))
    return best["source"]


# ---------------------------------
# Dominant coverage
# ---------------------------------

def _dominant_coverage(
    blocks:        List[StructuredBlock],
    winning_label: str,
) -> int:
    return sum(
        b["line_count"]
        for b in blocks
        if SOURCE_TO_LABEL.get(b["source"], b["source"]) == winning_label
    )


# ---------------------------------
# Classification confidence — damped
# ---------------------------------

def _classification_confidence(
    winning_score:  float,
    total_lines:    int,
    coverage_lines: int,
) -> float:
    """
    Damped scaling — decouples detection quality from segment size.
    Small high-quality structures retain clear signal.

    confidence = winning_score × (0.5 + 0.5 × coverage_ratio)
    """
    coverage_ratio   = min(1.0, coverage_lines / max(1, total_lines))
    density_dampener = 0.5 + (coverage_ratio * 0.5)
    return round(min(1.0, max(0.0, winning_score * density_dampener)), 2)


# ---------------------------------
# Prose dual-gate check
# ---------------------------------

def _is_prose(
    evidence:      Dict[str, float],
    total_lines:   int,
    blocks:        List[StructuredBlock],
) -> bool:
    """
    Dual-gate prose detection:
    1. max evidence score below MIN_STRUCTURE_SIGNAL
    AND
    2. dominant coverage ratio below MIN_STRUCTURE_SIGNAL
    Both must fail to trigger prose fallback.
    """
    if not evidence:
        return True

    max_score = max(evidence.values())
    if max_score >= MIN_STRUCTURE_SIGNAL:
        return False

    total_coverage = sum(b["line_count"] for b in blocks)
    coverage_ratio = min(1.0, total_coverage / max(1, total_lines))

    return coverage_ratio < MIN_STRUCTURE_SIGNAL


# ---------------------------------
# Core
# ---------------------------------

def classify(
    reconciled:  ReconcilerResult,
    total_lines: int,
) -> ClassificationResult:
    """
    Final segment classification from reconciled block stream.

    Rules:
    - ReconcilerResult never mutated — fresh output only
    - Empty stream → prose
    - Dual-gate: score AND coverage both weak → prose
    - High entropy → mixed regardless of score gap
    - Gap between top two below MIXED_THRESHOLD → mixed
    - Otherwise dominant label wins
    - Confidence is damped — small structures not penalized
    - dominant_source is deterministic via max score, not position
    """
    blocks = reconciled["blocks"]

    # Prose — empty stream
    if not blocks:
        return ClassificationResult(
            region_type="classified_segment",
            structure_type="prose",
            classification_confidence=0.0,
            dominant_source="none",
            block_count=0,
            dominant_coverage_lines=0,
            type_entropy=0.0,
            evidence={},
            density_profile={},
        )

    # Suppress redundant `context` blocks that merely shadow a specific-type
    # block (kv/table/hierarchy) on the same lines, BEFORE computing evidence
    # AND entropy — otherwise the shadow context inflates entropy and forces an
    # otherwise-pure region to `mixed`. Genuinely-mixed regions keep their
    # context (it covers different lines) and still resolve to `mixed`.
    blocks = _suppress_redundant_context(blocks)

    evidence        = _aggregate_evidence(blocks, total_lines)
    density_profile = _build_density_profile(blocks)

    # Entropy from actual physical line coverage — not bounded evidence scores
    density_float   = {k: float(v) for k, v in density_profile.items()}
    entropy         = _compute_entropy(density_float)

    # Prose — dual gate
    if _is_prose(evidence, total_lines, blocks):
        return ClassificationResult(
            region_type="classified_segment",
            structure_type="prose",
            classification_confidence=0.0,
            dominant_source="none",
            block_count=len(blocks),
            dominant_coverage_lines=0,
            type_entropy=entropy,
            evidence=evidence,
            density_profile=density_profile,
        )

    sorted_evidence = sorted(
        evidence.items(),
        key=lambda x: x[1],
        reverse=True,
    )

    top_label,  top_score    = sorted_evidence[0]
    second_score             = sorted_evidence[1][1] if len(sorted_evidence) > 1 else 0.0

    # Mixed — high entropy forces it regardless of gap
    if entropy >= HIGH_ENTROPY_THRESHOLD:
        structure_type = "mixed"
        dominant_src   = "none"
        coverage_lines = sum(b["line_count"] for b in blocks)

    # Mixed — gap too small
    elif (top_score - second_score) < MIXED_THRESHOLD:
        structure_type = "mixed"
        dominant_src   = "none"
        coverage_lines = sum(b["line_count"] for b in blocks)

    else:
        structure_type = top_label
        dominant_src   = _find_dominant_source(blocks, top_label, total_lines)
        coverage_lines = _dominant_coverage(blocks, top_label)

    conf = _classification_confidence(top_score, total_lines, coverage_lines)

    return ClassificationResult(
        region_type="classified_segment",
        structure_type=structure_type,
        classification_confidence=conf,
        dominant_source=dominant_src,
        block_count=len(blocks),
        dominant_coverage_lines=coverage_lines,
        type_entropy=entropy,
        evidence=evidence,
        density_profile=density_profile,
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
    from analysis.structure_engine.reconciler import reconcile

    print("\n" + "=" * 60)
    print("CLASSIFIER INTERACTIVE TEST")
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

        blocks     = build_all_blocks(
            kv_results=kv if kv else None,
            context_result=context,
        )
        total_lines = len(model)
        reconciled  = reconcile(blocks, total_lines)
        result      = classify(reconciled, total_lines)

        print(f"\n--- CLASSIFICATION RESULT ---\n")
        print(f"  structure_type            : {result['structure_type']}")
        print(f"  classification_confidence : {result['classification_confidence']}")
        print(f"  dominant_source           : {result['dominant_source']}")
        print(f"  block_count               : {result['block_count']}")
        print(f"  dominant_coverage_lines   : {result['dominant_coverage_lines']}")
        print(f"  type_entropy              : {result['type_entropy']}")
        print(f"\n  evidence:")
        for label, score in sorted(
            result["evidence"].items(), key=lambda x: -x[1]
        ):
            print(f"    {label:20} {score:.4f}")
        print(f"\n  density_profile:")
        for label, lines in sorted(
            result["density_profile"].items(), key=lambda x: -x[1]
        ):
            print(f"    {label:20} {lines} lines")

        print("-" * 60)
