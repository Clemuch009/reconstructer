# postprocess/validation.py

import re
from typing import List, Dict, Any, Optional, Tuple
from typing_extensions import TypedDict
from postprocess.formatter import (
    StructuredSegment,
    StructuredDocument,
    PostprocessOutput,
)


# ---------------------------------
# Contracts
# ---------------------------------

class AnomalyRecord(TypedDict):
    segment_id: str
    severity:   str
    code:       str
    message:    str


class ValidationChecks(TypedDict):
    schema_compliance: str
    matrix_uniformity: str
    line_conservation: str


class ValidationMetrics(TypedDict):
    total_segments:     int
    lines_reconciled:   int
    anomalies_detected: int


class ValidationResult(TypedDict):
    is_valid:       bool
    quality_score:  float
    metrics:        ValidationMetrics
    checks:         ValidationChecks
    document_flags: List[str]
    anomalies:      List[AnomalyRecord]


# ---------------------------------
# Constants
# ---------------------------------

MIN_CONFIDENCE_THRESHOLD     = 0.40
HIGH_FRAGMENTATION_THRESHOLD = 3.0
EXCESSIVE_MIXED_RATIO        = 0.60
HIGH_EMPTY_VALUE_RATIO       = 0.50
MIN_TEXT_DENSITY             = 0.20

# Line conservation is ADVISORY (see _line_conservation_check). A structuring
# engine legitimately reduces line count (separators collapse, headers relocate,
# overlapping mixed sub_blocks), so exact mass-balance is not achievable. This
# floor turns conservation into a gross-loss sanity signal, not a hard gate.
RECONCILIATION_FLOOR = 0.50

# Each failed document-level check (schema / matrix / conservation) applies this
# flat penalty to quality_score — UN-normalized, so a failure is always visible
# and is_valid (structural) and quality_score can never contradict.
FAILED_CHECK_PENALTY = 0.20

TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}"
)


# ---------------------------------
# Anomaly builder
# ---------------------------------

def _anomaly(
    segment_id: str,
    severity:   str,
    code:       str,
    message:    str,
) -> AnomalyRecord:
    return AnomalyRecord(
        segment_id=segment_id,
        severity=severity,
        code=code,
        message=message,
    )


# ---------------------------------
# Context-aware line counter
# ---------------------------------

def _count_mixed_lines(content: Dict[str, Any]) -> int:
    """
    Physical line count of a mixed (Option C) segment.
    Computed as the UNION of sub_block line spans plus any gap line indices —
    union, not sum, so overlapping sub_blocks (e.g. a kv block inside a context
    block) are not double-counted.
    """
    covered = set()
    for sb in content.get("sub_blocks", []):
        s = sb.get("start_line")
        e = sb.get("end_line")
        if s is not None and e is not None:
            covered.update(range(s, e + 1))
    for g in content.get("gaps", []):
        li = g.get("line_index")
        if li is not None:
            covered.add(li)
    return len(covered)


def _count_segment_lines(segments: List[StructuredSegment]) -> int:
    """
    Faithful per-type physical line count.
    - prose      : text lines
    - table      : data rows + 1 for an extracted header
    - kv_block   : pairs, or lines when packaged via the bypass text path
    - hierarchy  : nodes (1:1 with physical lines)
    - mixed      : union of sub_block spans + gaps (Option C)
    - context    : lines
    Preserved gap lines (metadata.gap_lines) are added for non-mixed types
    (mixed already folds its gaps into _count_mixed_lines).
    """
    total = 0
    for seg in segments:
        stype   = seg["type"]
        content = seg["content"]

        if stype == "mixed":
            total += _count_mixed_lines(content)
            continue

        if stype == "prose":
            text = content.get("text", "")
            total += len(text.split("\n")) if text else 0
        elif stype == "table":
            total += len(content.get("rows", []))
            if content.get("headers"):
                total += 1
        elif stype == "kv_block":
            pairs = content.get("pairs", [])
            if pairs:
                total += len(pairs)
            else:
                total += len([l for l in content.get("lines", []) if str(l).strip()])
        elif stype == "hierarchy":
            total += len(content.get("nodes", []))
        else:
            lines = content.get("lines") or content.get("raw_lines", [])
            total += len([l for l in lines if str(l).strip()])

        # Gap lines are preserved physical lines — count them.
        total += len(seg["metadata"].get("gap_lines", []))

    return total


# ---------------------------------
# Line conservation check (ADVISORY — not a hard is_valid gate)
# ---------------------------------

def _line_conservation_check(
    output:               PostprocessOutput,
    raw_input_line_count: int,
) -> Tuple[bool, str]:
    """
    Advisory line reconciliation.

    Exact mass-balance (Raw == Processed + Pruned) is NOT achievable for a
    structuring engine: separators collapse, headers relocate out of rows, and
    overlapping mixed sub_blocks make element counts non-1:1 with physical lines.
    So this is a gross-loss sanity signal, reported in checks and reflected in
    quality_score, but it does NOT determine is_valid. The reliable
    loss-prevention is upstream (formatter gap-flagging + cleanup preserving
    every segment). This FAILS only on impossible inflation or a gross deficit.
    """
    segments = output["machine_readable"]["segments"]

    processed_lines = _count_segment_lines(segments)

    pruned_lines = sum(
        1 for seg in segments
        for f in seg["metadata"].get("flags", [])
        if f in {
            "cosmetic_row_removed",
            "empty_row_removed",
            "debris_line_removed",
            "empty_segment_pruned",
        }
    )

    if raw_input_line_count == 0:
        return True, "PASS (no input line count provided)"

    accounted = processed_lines + pruned_lines

    # Both bounds are GROSS-only. Counting is approximate (structural reduction,
    # overlapping mixed spans), so accounted may land slightly above or below raw
    # without indicating a defect. Flag only a gross over-count (e.g. a doubling
    # bug) or a gross deficit (possible data loss).
    if accounted > raw_input_line_count * (1.0 + (1.0 - RECONCILIATION_FLOOR)):
        return False, (
            f"Gross line inflation: accounted={accounted} far exceeds input="
            f"{raw_input_line_count} (over-count bug)."
        )

    if accounted < raw_input_line_count * RECONCILIATION_FLOOR:
        return False, (
            f"Gross line deficit: accounted={accounted} is below "
            f"{RECONCILIATION_FLOOR:.0%} of input={raw_input_line_count} "
            f"(possible data loss)."
        )

    return True, (
        f"PASS (accounted={accounted}, input={raw_input_line_count}; "
        f"structural reduction within advisory band)"
    )


# ---------------------------------
# Quality score — normalized, check-aware
# ---------------------------------

def _compute_quality_score(
    anomalies:          List[AnomalyRecord],
    document_flags:     List[str],
    total_segments:     int,
    failed_check_count: int,
) -> float:
    """
    Normalized quality score.

    Per-segment anomaly penalty is normalized by segment count (large documents
    not unfairly penalized). Failed document-level CHECKS apply a flat,
    UN-normalized penalty so a hard failure is always visible — this is what
    keeps is_valid and quality_score from contradicting (a failed check can no
    longer leave quality at ~0.99).
    """
    if total_segments == 0:
        return 0.0

    error_count    = sum(1 for a in anomalies if a["severity"] == "ERROR")
    warning_count  = sum(1 for a in anomalies if a["severity"] == "WARNING")
    doc_flag_count = len(document_flags)

    weighted_penalty = (
        error_count    * 0.15 +
        warning_count  * 0.05 +
        doc_flag_count * 0.10
    )
    normalized_penalty = weighted_penalty / total_segments

    check_penalty = failed_check_count * FAILED_CHECK_PENALTY

    return round(
        max(0.0, min(1.0, 1.0 - normalized_penalty - check_penalty)),
        2,
    )


# ---------------------------------
# KV validation
# ---------------------------------

def _validate_kv(
    segment: StructuredSegment,
) -> List[AnomalyRecord]:
    anomalies = []
    seg_id    = segment["segment_id"]
    content   = segment["content"]
    pairs     = content.get("pairs", [])

    if not pairs:
        anomalies.append(_anomaly(
            seg_id, "WARNING",
            "KV_EMPTY_PAIRS",
            "kv_block contains no valid pairs after cleanup.",
        ))
        return anomalies

    # Empty key check
    for i, pair in enumerate(pairs):
        if not pair.get("key", "").strip():
            anomalies.append(_anomaly(
                seg_id, "ERROR",
                "KV_INVALID_STRUCTURE",
                f"Pair index {i} has an empty key.",
            ))

    # High empty value ratio
    empty_values = sum(
        1 for p in pairs
        if p.get("value") is None or str(p.get("value", "")).strip() == ""
    )
    if pairs and empty_values / len(pairs) >= HIGH_EMPTY_VALUE_RATIO:
        anomalies.append(_anomaly(
            seg_id, "WARNING",
            "KV_HIGH_EMPTY_VALUE_RATIO",
            f"{empty_values}/{len(pairs)} values are empty — "
            f"possible upstream split failure.",
        ))

    # Duplicate conflicting keys
    # NOTE: logged as WARNING only — does NOT affect schema_ok or is_valid
    seen_keys: Dict[str, str] = {}
    for pair in pairs:
        key   = pair.get("key", "")
        value = str(pair.get("value", ""))
        if key in seen_keys:
            if seen_keys[key] != value:
                anomalies.append(_anomaly(
                    seg_id, "WARNING",
                    "DUPLICATE_KEYS_DETECTED",
                    f"Key '{key}' appears with conflicting values: "
                    f"'{seen_keys[key]}' vs '{value}'.",
                ))
        else:
            seen_keys[key] = value

    # Bracket noise leakage
    for pair in pairs:
        key = pair.get("key", "")
        if key.startswith("[") or key.endswith("]"):
            anomalies.append(_anomaly(
                seg_id, "WARNING",
                "KV_SUSPECTED_BRACKET_LEAKAGE",
                f"Key '{key}' contains bracket artifacts — "
                f"possible timestamp/severity split.",
            ))

    # Timestamp-like key detection — compiled regex
    for pair in pairs:
        key = pair.get("key", "")
        if TIMESTAMP_PATTERN.search(key):
            anomalies.append(_anomaly(
                seg_id, "WARNING",
                "KV_TIMESTAMP_LIKE_KEY",
                f"Key '{key}' contains structural artifacts resembling "
                f"an ISO-style date token.",
            ))

    return anomalies


# ---------------------------------
# Table validation
# ---------------------------------

def _validate_table(
    segment: StructuredSegment,
) -> List[AnomalyRecord]:
    anomalies    = []
    seg_id       = segment["segment_id"]
    content      = segment["content"]
    rows         = content.get("rows", [])
    col_count    = content.get("col_count", 0)
    alignment_ok = content.get("alignment_ok", True)
    flags        = segment["metadata"].get("flags", [])

    if not rows:
        anomalies.append(_anomaly(
            seg_id, "WARNING",
            "TABLE_EMPTY",
            "Table segment contains no data rows after cleanup.",
        ))
        return anomalies

    # Matrix uniformity
    if col_count > 0:
        mismatched = [
            i for i, row in enumerate(rows)
            if len(row) != col_count
        ]
        if mismatched:
            anomalies.append(_anomaly(
                seg_id, "WARNING",
                "TABLE_COLUMN_MISMATCH",
                f"Rows {mismatched} have length != col_count ({col_count}).",
            ))

    # Alignment stability
    if not alignment_ok:
        anomalies.append(_anomaly(
            seg_id, "WARNING",
            "TABLE_COLUMN_INSTABILITY",
            "Table alignment_ok is False — column structure is unstable.",
        ))

    # Ragged repair ratio
    repair_count = flags.count("ragged_row_repaired")
    if rows and repair_count / len(rows) > 0.20:
        anomalies.append(_anomaly(
            seg_id, "WARNING",
            "TABLE_HIGH_REPAIR_RATIO",
            f"{repair_count}/{len(rows)} rows required padding.",
        ))

    # Single row — classification suspect
    if len(rows) < 2:
        anomalies.append(_anomaly(
            seg_id, "WARNING",
            "CLASSIFICATION_MISMATCH",
            "Table has fewer than 2 data rows — "
            "classifier may have over-predicted table structure.",
        ))

    return anomalies


# ---------------------------------
# Hierarchy validation
# ---------------------------------

def _validate_hierarchy(
    segment: StructuredSegment,
) -> List[AnomalyRecord]:
    anomalies = []
    seg_id    = segment["segment_id"]
    content   = segment["content"]
    nodes     = content.get("nodes", [])

    if not nodes:
        anomalies.append(_anomaly(
            seg_id, "WARNING",
            "HIERARCHY_EMPTY",
            "Hierarchy segment contains no nodes after cleanup.",
        ))
        return anomalies

    # Abstract depth progression — no glyph dependency
    for i in range(1, len(nodes)):
        prev_depth = nodes[i - 1]["depth"]
        curr_depth = nodes[i]["depth"]

        if curr_depth < 0:
            anomalies.append(_anomaly(
                seg_id, "ERROR",
                "HIERARCHY_NEGATIVE_DEPTH",
                f"Node {i} has negative depth {curr_depth}.",
            ))

        if curr_depth > prev_depth + 1:
            anomalies.append(_anomaly(
                seg_id, "WARNING",
                "HIERARCHY_DEPTH_INSTABILITY",
                f"Node {i} jumps from depth {prev_depth} to depth {curr_depth} "
                f"— impossible generational step.",
            ))

    # Empty node check
    for i, node in enumerate(nodes):
        text = node.get("normalized_text") or node.get("text", "")
        if not text.strip():
            anomalies.append(_anomaly(
                seg_id, "WARNING",
                "HIERARCHY_EMPTY_NODE",
                f"Node {i} at depth {node.get('depth', '?')} has empty text.",
            ))

    return anomalies


# ---------------------------------
# Mixed validation (Option C)
# ---------------------------------

def _validate_mixed(
    segment: StructuredSegment,
) -> List[AnomalyRecord]:
    """
    Validate an Option C mixed segment by its sub_blocks/gaps — NOT as prose.
    Avoids the spurious LOW_TEXT_DENSITY warning the prose path produced when
    mixed content carries no 'lines'/'raw_lines'.
    """
    anomalies  = []
    seg_id     = segment["segment_id"]
    content    = segment["content"]
    sub_blocks = content.get("sub_blocks", [])
    gaps       = content.get("gaps", [])

    if not sub_blocks and not gaps:
        anomalies.append(_anomaly(
            seg_id, "WARNING",
            "MIXED_EMPTY",
            "mixed segment has neither sub_blocks nor gap lines after cleanup.",
        ))

    return anomalies


# ---------------------------------
# Prose and context validation
# ---------------------------------

def _validate_prose(
    segment: StructuredSegment,
) -> List[AnomalyRecord]:
    anomalies = []
    seg_id    = segment["segment_id"]
    content   = segment["content"]
    stype     = segment["type"]

    if stype == "prose":
        text  = content.get("text", "")
        lines = text.split("\n") if text else []
    else:
        lines = content.get("lines") or content.get("raw_lines", [])

    if not lines:
        anomalies.append(_anomaly(
            seg_id, "WARNING",
            "LOW_TEXT_DENSITY",
            "Segment contains no lines after cleanup.",
        ))
        return anomalies

    meaningful = [l for l in lines if l.strip() and len(l.strip()) > 2]
    density    = len(meaningful) / len(lines) if lines else 0.0

    if density < MIN_TEXT_DENSITY:
        anomalies.append(_anomaly(
            seg_id, "WARNING",
            "LOW_TEXT_DENSITY",
            f"Only {len(meaningful)}/{len(lines)} lines have meaningful content "
            f"(density={density:.2f}).",
        ))

    return anomalies


# ---------------------------------
# Confidence check
# ---------------------------------

def _validate_confidence(
    segment: StructuredSegment,
) -> List[AnomalyRecord]:
    anomalies  = []
    seg_id     = segment["segment_id"]
    confidence = segment["metadata"].get("confidence", 1.0)

    if isinstance(confidence, (int, float)) and confidence < MIN_CONFIDENCE_THRESHOLD:
        anomalies.append(_anomaly(
            seg_id, "WARNING",
            "LOW_CONFIDENCE_STRUCTURE",
            f"Segment classification confidence {confidence:.2f} is below "
            f"threshold {MIN_CONFIDENCE_THRESHOLD}.",
        ))

    return anomalies


# ---------------------------------
# Per-segment router
# ---------------------------------

def validate_segment(
    segment: StructuredSegment,
) -> List[AnomalyRecord]:
    stype     = segment["type"]
    anomalies = []

    if stype == "kv_block":
        anomalies.extend(_validate_kv(segment))
    elif stype == "table":
        anomalies.extend(_validate_table(segment))
    elif stype == "hierarchy":
        anomalies.extend(_validate_hierarchy(segment))
    elif stype == "mixed":
        anomalies.extend(_validate_mixed(segment))
    else:
        anomalies.extend(_validate_prose(segment))

    anomalies.extend(_validate_confidence(segment))
    return anomalies


# ---------------------------------
# Document-level checks
# ---------------------------------

def _document_checks(
    segments: List[StructuredSegment],
) -> List[str]:
    flags = []

    if not segments:
        flags.append("EMPTY_DOCUMENT")
        return flags

    # Fragmentation check
    total_lines = _count_segment_lines(segments)
    avg_lines   = total_lines / len(segments)
    if avg_lines < HIGH_FRAGMENTATION_THRESHOLD:
        flags.append("HIGH_FRAGMENTATION")

    # Excessive mixed
    mixed_count = sum(1 for s in segments if s["type"] == "mixed")
    if mixed_count / len(segments) >= EXCESSIVE_MIXED_RATIO:
        flags.append("EXCESSIVE_MIXED_CLASSIFICATION")

    return flags


# ---------------------------------
# Build validation report
# ---------------------------------

def _build_validation_report(
    segments:         List[StructuredSegment],
    all_anomalies:    List[AnomalyRecord],
    document_flags:   List[str],
    conservation_ok:  bool,
    lines_reconciled: int,
) -> ValidationResult:

    schema_ok = not any(
        a["severity"] == "ERROR" and
        a["code"] not in {"DUPLICATE_KEYS_DETECTED"}
        for a in all_anomalies
    )

    matrix_ok = not any(
        a["code"] in {"TABLE_COLUMN_MISMATCH", "TABLE_COLUMN_INSTABILITY"}
        for a in all_anomalies
    )

    checks = ValidationChecks(
        schema_compliance="PASS" if schema_ok else "FAIL",
        matrix_uniformity="PASS" if matrix_ok else "FAIL",
        line_conservation="PASS" if conservation_ok else "FAIL",
    )

    failed_check_count = sum(
        1 for ok in (schema_ok, matrix_ok, conservation_ok) if not ok
    )

    quality_score = _compute_quality_score(
        all_anomalies, document_flags, len(segments), failed_check_count
    )

    # is_valid reflects STRUCTURAL validity only. line_conservation is advisory
    # (it cannot be exact for a structuring engine) and is intentionally NOT a
    # hard gate here — it surfaces in checks and lowers quality_score instead.
    is_valid = (
        schema_ok and
        "EMPTY_DOCUMENT" not in document_flags
    )

    return ValidationResult(
        is_valid=is_valid,
        quality_score=quality_score,
        metrics=ValidationMetrics(
            total_segments=len(segments),
            lines_reconciled=lines_reconciled,
            anomalies_detected=len(all_anomalies),
        ),
        checks=checks,
        document_flags=document_flags,
        anomalies=all_anomalies,
    )


# ---------------------------------
# Core
# ---------------------------------

def validate(
    output:               PostprocessOutput,
    raw_input_line_count: int = 0,
) -> PostprocessOutput:
    """
    Immutable validation pass on PostprocessOutput.

    Layers:
    1. Per-segment type-aware structural checks (incl. dedicated mixed validator)
    2. Document-level global checks
    3. Advisory line reconciliation (reported + quality signal, not a hard gate)
    4. Quality score computation — normalized, check-aware

    Rules:
    - Read-only — never mutates content
    - Never reclassifies segments
    - DUPLICATE_KEYS_DETECTED is WARNING only — does not affect is_valid
    - Hard failures (is_valid): KV_INVALID_STRUCTURE (empty key),
      HIERARCHY_NEGATIVE_DEPTH, EMPTY_DOCUMENT
    - line_conservation is ADVISORY: it informs checks + quality_score but does
      NOT gate is_valid (exact balance is unachievable for a structuring engine;
      loss-prevention lives in formatter gap-flagging + cleanup segment retention)
    - quality_score and is_valid are reconciled: a failed hard check always
      lowers quality, so the prior is_valid=False / quality=0.99 contradiction
      cannot occur
    - validation slot populated and returned
    """
    segments = output["machine_readable"]["segments"]

    # Per-segment validation
    all_anomalies: List[AnomalyRecord] = []
    for seg in segments:
        all_anomalies.extend(validate_segment(seg))

    # Document checks
    document_flags = _document_checks(segments)

    # Line conservation (advisory)
    conservation_ok, _ = _line_conservation_check(output, raw_input_line_count)

    # Line count for metrics
    lines_reconciled = _count_segment_lines(segments)

    # Build report
    validation_result = _build_validation_report(
        segments=segments,
        all_anomalies=all_anomalies,
        document_flags=document_flags,
        conservation_ok=conservation_ok,
        lines_reconciled=lines_reconciled,
    )

    return PostprocessOutput(
        region_type=output["region_type"],
        human_readable=output["human_readable"],
        machine_readable=output["machine_readable"],
        validation=validation_result,
    )
