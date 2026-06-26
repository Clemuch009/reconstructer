import re
import json
import csv
import io
import asyncio
import hashlib
from typing import List, Optional, Any, Tuple
from typing_extensions import TypedDict

from core.signals.hierarchy import hierarchy_signal


# ---------------------------------
# Contracts
# ---------------------------------

class RoutingSignals(TypedDict):
    delimiter_density:    float
    csv_density:          float   # consistent delimiter column count signal
    glyph_density:        float
    kv_density:           float
    pipe_density:         float
    hierarchy_step_ratio: float
    multi_space_ratio:    float
    prose_score:          float
    is_hierarchy:         bool
    hierarchy_confidence: float


# ---------------------------------
# CSV density — uses csv.reader for accuracy
# ---------------------------------

def _compute_csv_density(text: str) -> float:
    """
    Returns 1.0 if text lines have consistent column count when parsed
    as CSV/TSV/semicolon-delimited — 0.0 otherwise.

    Uses csv.reader to handle quoted fields correctly, so commas inside
    quoted strings don't produce false positives from prose sentences.

    Minimum 3 non-empty lines required.
    All lines must parse to the same column count (>= 2).
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) < 3:
        return 0.0

    for delim in [",", "\t", ";"]:
        try:
            reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delim)
            parsed = [row for row in reader if row]
            if len(parsed) < 3:
                continue
            col_counts = [len(row) for row in parsed]
            if min(col_counts) >= 3 and max(col_counts) - min(col_counts) == 0:
                return 1.0
        except Exception:
            continue

    return 0.0


class RoutingDecision(TypedDict):
    route:        str
    confidence:   float
    signals:      RoutingSignals
    reason_codes: List[str]


class IngestionEnvelope(TypedDict):
    envelope_id:    str
    sequence_index: int
    raw_line_count: int
    assigned_route: str
    payload:        str
    result:         Optional[Any]
    status:         str
    routing:        RoutingDecision


# ---------------------------------
# Constants — locked thresholds
# ---------------------------------

# Layer 2 — structural noise barrier
DENSITY_BARRIER_THRESHOLD    = 0.15

# Structural confidence collapse — below this everything bypasses
CONFIDENCE_COLLAPSE_THRESHOLD = 0.10

# Layer 3 — route thresholds
TABLE_LOW_THRESHOLD  = 0.15
TABLE_HIGH_THRESHOLD = 0.60
TREE_HIGH_THRESHOLD  = 0.40
KV_LOW_THRESHOLD     = 0.20
KV_HIGH_THRESHOLD    = 0.70

# Hierarchy step detector — valid structural indent steps
VALID_INDENT_STEPS = {2, 4, 8}
HIERARCHY_STEP_THRESHOLD = 0.50

# KV line pattern — key ≤ 32 chars, colon or equals delimiter
KV_LINE_RE = re.compile(r"^[\w\.\-]{2,32}\s*[:=]\s*.+")

# Tree glyph pattern
TREE_GLYPH_RE = re.compile(r"[└├─┌┐┘┤┬┴┼│]|^\s*[\+\|]\-\-")

# Multi-space indicator
MULTI_SPACE_RE = re.compile(r"\s{2,}")


# ---------------------------------
# Structural tier classification
# ---------------------------------

# Three structural tiers — explicitly separated
# NO_STRUCTURE   → absolute prose bypass, no detector call at all
# WEAK_STRUCTURE → below confidence threshold → bypass with flag
# STRUCTURED     → proceed to Layer 3 routing

NO_STRUCTURE_THRESHOLD   = 0.05   # max signal below this → NO_STRUCTURE
WEAK_STRUCTURE_THRESHOLD = 0.15   # max signal below this → WEAK_STRUCTURE


def _classify_structural_tier(max_signal: float) -> str:
    """
    Explicit three-tier structural classification.
    Separates 'no structure' from 'weak structure' from 'structured'.
    """
    if max_signal < NO_STRUCTURE_THRESHOLD:
        return "NO_STRUCTURE"
    elif max_signal < WEAK_STRUCTURE_THRESHOLD:
        return "WEAK_STRUCTURE"
    else:
        return "STRUCTURED"


# ---------------------------------
# Deterministic envelope ID
# ---------------------------------

def _envelope_id(payload: str, sequence_index: int) -> str:
    raw = f"{sequence_index}:{payload[:64]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------
# Layer 1 — robust JSON verification
# ---------------------------------

def _is_valid_json_payload(text: str) -> bool:
    """
    Robust JSON detection via actual parse attempt.
    Prevents bracket-matching false positives from [INFO] prefixes.
    """
    stripped = text.strip()
    if not (
        (stripped.startswith("{") and stripped.endswith("}")) or
        (stripped.startswith("[") and stripped.endswith("]"))
    ):
        return False
    try:
        json.loads(stripped)
        return True
    except (ValueError, TypeError):
        return False


# ---------------------------------
# Signal extraction
# ---------------------------------

def _extract_signals(text: str, lines: List[str]) -> RoutingSignals:
    """
    Lightweight sequential pass — no deep parsing.
    Computes geometric density markers only.
    """
    total_chars     = max(len(text), 1)
    total_lines     = max(len(lines), 1)
    non_alpha_chars = max(
        sum(1 for c in text if not c.isalnum() and c != " "), 1
    )

    # Delimiter density — normalized against non-alphanumeric chars
    # Structural delimiters — pipe, tab, tilde, plus only
    # Commas excluded — too common in prose to use as raw signal
    delimiter_count = sum(1 for c in text if c in "|+~\t")
    delimiter_density = delimiter_count / non_alpha_chars

    # CSV density — uses csv.reader to check column consistency
    # This correctly handles quoted fields and distinguishes
    # CSV tables from prose containing commas/semicolons
    csv_density = _compute_csv_density(text)

    # Structural glyph density — normalized against total chars
    glyph_count   = sum(1 for c in text if c in "└├─┌┐┘┤┬┴┼│")
    glyph_density = glyph_count / total_chars

    # Pipe density — line-normalized
    pipe_lines   = sum(1 for l in lines if "|" in l)
    pipe_density = pipe_lines / total_lines

    # KV density — line-normalized
    kv_lines   = sum(1 for l in lines if KV_LINE_RE.match(l.strip()))
    kv_density = kv_lines / total_lines

    # Hierarchy step ratio — structural indent delta tracking
    # Replaces flawed MAD variance calculation
    indents = [len(l) - len(l.lstrip()) for l in lines if l.strip()]
    if len(indents) > 1:
        indent_steps = [
            abs(indents[i] - indents[i - 1])
            for i in range(1, len(indents))
        ]
        matching_steps     = sum(1 for s in indent_steps if s in VALID_INDENT_STEPS)
        hierarchy_step_ratio = matching_steps / len(indent_steps)
    else:
        hierarchy_step_ratio = 0.0

    # Multi-space ratio — columnar alignment signal
    multi_space_lines = sum(1 for l in lines if MULTI_SPACE_RE.search(l))
    multi_space_ratio = multi_space_lines / total_lines

    # Hierarchy verdict — shared, field-agnostic definition (depth-based).
    # Authoritative signal for tree-ness; replaces glyph-character density,
    # which is blind to ascii "+--" trees.
    hverdict = hierarchy_signal(lines)

    # Prose score — absence of structural signals
    # Uses only line-normalized metrics for fair comparison
    prose_score = max(
        0.0,
        1.0 - (
            pipe_density             * 0.30 +
            kv_density               * 0.30 +
            glyph_density            * 0.20 +
            hierarchy_step_ratio     * 0.20
        )
    )

    return RoutingSignals(
        delimiter_density=round(delimiter_density, 4),
        csv_density=round(csv_density, 4),
        glyph_density=round(glyph_density, 4),
        kv_density=round(kv_density, 4),
        pipe_density=round(pipe_density, 4),
        hierarchy_step_ratio=round(hierarchy_step_ratio, 4),
        multi_space_ratio=round(multi_space_ratio, 4),
        prose_score=round(prose_score, 4),
        is_hierarchy=hverdict["is_hierarchy"],
        hierarchy_confidence=round(hverdict["valid_fraction"], 4),
    )


# ---------------------------------
# Layer 3 — independent candidate evaluation
# ---------------------------------

def _evaluate_candidates(
    signals:              RoutingSignals,
    hierarchy_step_ratio: float,
) -> Tuple[List[Tuple[str, float]], List[str]]:
    """
    Independent evaluation of each structural type.
    All types evaluated simultaneously — no elif exclusions.
    Enables correct AMBIGUOUS detection on overlapping signals.
    """
    candidates:   List[Tuple[str, float]] = []
    reason_codes: List[str]               = []

    # Table score
    # Pipe tables: pipe_density dominates
    # CSV/TSV tables: csv_density dominates (column-consistent delimiter check)
    pipe_table_score = (
        signals["pipe_density"]      * 0.50 +
        signals["multi_space_ratio"] * 0.30 +
        signals["delimiter_density"] * 0.20
    )
    csv_table_score = signals["csv_density"] * 0.80
    table_score = max(pipe_table_score, csv_table_score)

    # KV score
    kv_score = (
        signals["kv_density"]        * 0.70 +
        signals["delimiter_density"] * 0.30
    )

    # Evaluate TABLE — independent block
    # Pipe tables: high pipe density or moderate pipe + multi-space
    # CSV/TSV tables: csv_density = 1.0 when columns are consistent
    if (
        signals["pipe_density"] >= TABLE_HIGH_THRESHOLD or
        (
            signals["pipe_density"] >= TABLE_LOW_THRESHOLD and
            signals["multi_space_ratio"] >= 0.40
        ) or
        signals["csv_density"] >= 1.0
    ):
        candidates.append(("TABLE", table_score))
        reason_codes.append("TABLE_SIGNATURE_MATCHED")

    # Evaluate TREE — independent block
    # Decided by the shared hierarchy definition (depth-based, field-agnostic),
    # not glyph character density. Confidence is the valid-transition fraction.
    if signals["is_hierarchy"]:
        candidates.append(("TREE", signals["hierarchy_confidence"]))
        reason_codes.append("TREE_SIGNATURE_MATCHED")

    # Evaluate KV — independent block
    if signals["kv_density"] >= KV_LOW_THRESHOLD:
        candidates.append(("KV", kv_score))
        reason_codes.append("KV_SIGNATURE_MATCHED")

    return candidates, reason_codes


# ---------------------------------
# Routing decision
# ---------------------------------

def _decide_route(
    text:    str,
    signals: RoutingSignals,
) -> RoutingDecision:
    """
    Three-layer routing with explicit structural tier separation.

    Layer 1 — JSON native bypass (robust parse check)
    Layer 2 — Structural tier gate:
               NO_STRUCTURE   → PASS_THROUGH (no detectors called)
               WEAK_STRUCTURE → PASS_THROUGH with flag (confidence collapse bypass)
               STRUCTURED     → proceed to Layer 3
               (a confirmed hierarchy always reaches Layer 3)
    Layer 3 — Independent candidate evaluation → route or AMBIGUOUS
    """
    reason_codes: List[str] = []

    # Layer 1 — JSON native
    if _is_valid_json_payload(text):
        reason_codes.append("JSON_NATIVE_DETECTED")
        return RoutingDecision(
            route="JSON_NATIVE",
            confidence=1.0,
            signals=signals,
            reason_codes=reason_codes,
        )

    # Layer 2 — Structural tier gate
    max_line_signal = max(
        signals["pipe_density"],
        signals["kv_density"],
        signals["hierarchy_step_ratio"],
        signals["csv_density"],   # CSV/TSV column-consistency signal
    )

    # A confirmed hierarchy (shared definition) always reaches Layer 3 —
    # never bypassed as NO_STRUCTURE / WEAK_STRUCTURE. This is what lets
    # ascii "+--" trees survive the gate that previously flattened them.
    if signals["is_hierarchy"]:
        structural_tier = "STRUCTURED"
    else:
        structural_tier = _classify_structural_tier(max_line_signal)

    if structural_tier == "NO_STRUCTURE":
        # Absolute prose — no detectors called at all
        reason_codes.append("NO_STRUCTURE_DETECTED")
        reason_codes.append("DETECTOR_SKIP_ABSOLUTE")
        return RoutingDecision(
            route="PASS_THROUGH",
            confidence=signals["prose_score"],
            signals=signals,
            reason_codes=reason_codes,
        )

    if structural_tier == "WEAK_STRUCTURE":
        # Structural confidence collapse — bypass with explicit flag
        reason_codes.append("WEAK_STRUCTURE_DETECTED")
        reason_codes.append("CONFIDENCE_COLLAPSE_BYPASS")
        return RoutingDecision(
            route="PASS_THROUGH",
            confidence=max_line_signal,
            signals=signals,
            reason_codes=reason_codes,
        )

    # Layer 3 — Structured path
    candidates, layer3_reasons = _evaluate_candidates(
        signals, signals["hierarchy_step_ratio"]
    )
    reason_codes.extend(layer3_reasons)

    # No candidates despite structural signal — prose dominant residual
    if not candidates:
        reason_codes.append("PROSE_DOMINANT_RESIDUAL")
        return RoutingDecision(
            route="PASS_THROUGH",
            confidence=signals["prose_score"],
            signals=signals,
            reason_codes=reason_codes,
        )

    # Single clear candidate
    if len(candidates) == 1:
        route, confidence = candidates[0]
        return RoutingDecision(
            route=route,
            confidence=round(min(1.0, confidence), 4),
            signals=signals,
            reason_codes=reason_codes,
        )

    # Multiple candidates — check dominance gap
    candidates.sort(key=lambda x: x[1], reverse=True)
    top_route,    top_score    = candidates[0]
    second_route, second_score = candidates[1]
    gap = top_score - second_score

    if gap >= 0.15:
        return RoutingDecision(
            route=top_route,
            confidence=round(min(1.0, top_score), 4),
            signals=signals,
            reason_codes=reason_codes,
        )
    else:
        # Ambiguous — full detector suite
        reason_codes.append("AMBIGUOUS_SIGNAL_OVERLAP")
        return RoutingDecision(
            route="AMBIGUOUS",
            confidence=round(top_score, 4),
            signals=signals,
            reason_codes=reason_codes,
        )


# ---------------------------------
# Pre-classify single segment
# ---------------------------------

def pre_classify(
    payload:        str,
    sequence_index: int,
) -> IngestionEnvelope:
    """
    Classify a single text segment and wrap in IngestionEnvelope.
    Lightweight — no deep parsing, no structure modification.
    """
    lines   = payload.split("\n")
    signals = _extract_signals(payload, lines)
    routing = _decide_route(payload, signals)

    return IngestionEnvelope(
        envelope_id=_envelope_id(payload, sequence_index),
        sequence_index=sequence_index,
        raw_line_count=len(lines),
        assigned_route=routing["route"],
        payload=payload,
        result=None,
        status="queued",
        routing=routing,
    )


# ---------------------------------
# Async processing
# ---------------------------------

async def _process_envelope(
    envelope:  IngestionEnvelope,
    processor: Any,
) -> IngestionEnvelope:
    """
    Async execution of a single envelope.
    Updates status on completion or failure.
    processor: callable(payload: str, route: str) -> Any
    """
    updated         = dict(envelope)
    updated["status"] = "processing"

    try:
        if asyncio.iscoroutinefunction(processor):
            result = await processor(
                envelope["payload"],
                envelope["assigned_route"],
            )
        else:
            loop   = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                processor,
                envelope["payload"],
                envelope["assigned_route"],
            )
        updated["result"] = result
        updated["status"] = "done"
    except Exception as e:
        updated["status"] = "failed"
        updated["result"] = {"error": str(e)}

    return IngestionEnvelope(**updated)


async def process_document(
    envelopes: List[IngestionEnvelope],
    processor: Any,
) -> List[IngestionEnvelope]:
    """
    Async concurrent processing of all envelopes.
    Reassembles in original document order by sequence_index.
    Out-of-order execution is safe — sort guarantees document order.
    """
    tasks   = [_process_envelope(env, processor) for env in envelopes]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    return sorted(results, key=lambda e: e["sequence_index"])


# ---------------------------------
# Batch pre-classification
# ---------------------------------

def pre_classify_document(segments: List[str]) -> List[IngestionEnvelope]:
    """
    Pre-classify all segments from a document.
    Assigns monotonic sequence_index to each.
    Returns envelopes in original document order.
    """
    return [
        pre_classify(payload, idx)
        for idx, payload in enumerate(segments)
    ]


# ---------------------------------
# Interactive Test
# ---------------------------------

if __name__ == "__main__":
    import sys

    print("\n" + "=" * 60)
    print("PRE-CLASSIFIER INTERACTIVE TEST")
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

        segments  = [s.strip() for s in raw.split("\n\n") if s.strip()]
        envelopes = pre_classify_document(segments)

        print(f"\n--- PRE-CLASSIFICATION ({len(envelopes)} segments) ---\n")
        for env in envelopes:
            r = env["routing"]
            s = r["signals"]
            print(
                f"[{env['sequence_index']}] "
                f"route={env['assigned_route']:15} "
                f"conf={r['confidence']:.3f} "
                f"lines={env['raw_line_count']}"
            )
            print(
                f"     signals: "
                f"kv={s['kv_density']:.2f} "
                f"pipe={s['pipe_density']:.2f} "
                f"glyph={s['glyph_density']:.2f} "
                f"hier={s['hierarchy_step_ratio']:.2f} "
                f"is_hier={s['is_hierarchy']} "
                f"prose={s['prose_score']:.2f}"
            )
            print(f"     reasons: {r['reason_codes']}")
            print()

        print("-" * 60)
