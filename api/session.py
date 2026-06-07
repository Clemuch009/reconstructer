# api/session.py

import uuid
import hashlib
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional
from typing_extensions import TypedDict

from postprocess.formatter import PostprocessOutput


# ---------------------------------
# Pipeline version — bump on release
# ---------------------------------

PIPELINE_VERSION = "1.0.0"


# ---------------------------------
# Canonical rule map
# Internal flags → stable external contract names
# ---------------------------------

CANONICAL_RULE_MAP: Dict[str, List[str]] = {
    "KV_FORCE_EXTRACT":    [
        "kv_junk_key_removed",
        "kv_spacing_normalized",
        "type_casted_primitives",
    ],
    "TABLE_REPAIR":        [
        "ragged_row_repaired",
        "ragged_row_detected",
        "line_gap_prose_fallback",
        "cosmetic_row_removed",
        "empty_row_removed",
    ],
    "HIERARCHY_REPAIR":    [
        "hierarchy_depth_jump_detected",
        "hierarchy_baseline_normalized",
        "hierarchy_step_compressed",
    ],
    "DEBRIS_REMOVAL":      [
        "debris_line_removed",
        "empty_segment_pruned",
    ],
    "CONTEXT_ABSORPTION":  [
        "isolated_context_absorbed",
    ],
}

# Inverted map: raw_flag → canonical_rule
_FLAG_TO_CANONICAL: Dict[str, str] = {
    flag: rule
    for rule, flags in CANONICAL_RULE_MAP.items()
    for flag in flags
}


# ---------------------------------
# Route → type normalization
# Maps pre_classifier routes to comparable structure types
# ---------------------------------

ROUTE_TO_TYPE: Dict[str, str] = {
    "TABLE":        "table",
    "KV":           "kv_block",
    "TREE":         "hierarchy",
    "AMBIGUOUS":    "mixed",
    "PASS_THROUGH": "prose",
    "JSON_NATIVE":  "kv_block",
}


# ---------------------------------
# Contracts
# ---------------------------------

class SessionTelemetry(TypedDict):
    conflict_rate:                float   # structural conflict — system level
    classifier_disagreement_rate: float   # model disagreement — ML level
    rule_trigger_counts:          Dict[str, int]   # canonical rule names only
    total_segments:               int
    valid_segments:               int
    conflicted_segments:          int
    disagreed_segments:           int


class SessionTrace(TypedDict):
    session_id:       str
    pipeline_version: str
    timestamp:        str
    raw_line_count:   int
    segment_count:    int
    telemetry:        SessionTelemetry


# ---------------------------------
# Canonical rule trigger computation
# ---------------------------------

def _compute_rule_triggers(
    output: PostprocessOutput,
) -> Dict[str, int]:
    """
    Aggregate raw flags from all segments into canonical rule counts.
    Raw flags are internal — only canonical names exposed.
    """
    canonical_counts: Dict[str, int] = {}

    segments = output["machine_readable"]["segments"]
    for seg in segments:
        flags = seg["metadata"].get("flags", [])
        for flag in flags:
            canonical = _FLAG_TO_CANONICAL.get(flag)
            if canonical:
                canonical_counts[canonical] = (
                    canonical_counts.get(canonical, 0) + 1
                )

    return canonical_counts


# ---------------------------------
# Conflict and disagreement computation
# ---------------------------------

def _compute_conflict_metrics(
    output:    PostprocessOutput,
    envelopes: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """
    Compute conflict_rate and classifier_disagreement_rate.

    conflict_rate:
        segments where pre_classifier.assigned_route != final.structure_type
        / total valid segments

    classifier_disagreement_rate:
        segments where pre_classifier.assigned_route != final.structure_type
        / segments with valid pre_classifier output
        (PASS_THROUGH → prose is treated as agreement — expected behavior)

    envelopes: List[IngestionEnvelope] from pre_classifier
    If not provided — both rates are 0.0 (pre_classifier data unavailable)
    """
    segments = output["machine_readable"]["segments"]
    total_valid     = 0
    conflicted      = 0
    valid_pre       = 0
    disagreed       = 0

    if not envelopes:
        return {
            "total_valid":     len(segments),
            "valid_pre":       0,
            "conflicted":      0,
            "disagreed":       0,
            "conflict_rate":   0.0,
            "disagreement_rate": 0.0,
        }

    # Build route lookup by sequence_index
    route_map: Dict[int, str] = {
        env["sequence_index"]: env["assigned_route"]
        for env in envelopes
    }

    for idx, seg in enumerate(segments):
        final_type = seg["type"]
        pre_route  = route_map.get(idx)

        # Skip dropped or failed segments
        if not final_type or not pre_route:
            continue

        total_valid += 1

        # Normalize route to comparable type
        expected_type = ROUTE_TO_TYPE.get(pre_route)

        # PASS_THROUGH → prose is expected agreement — not a conflict
        is_passthrough_prose = (
            pre_route == "PASS_THROUGH" and final_type == "prose"
        )

        if expected_type is not None and not is_passthrough_prose:
            valid_pre += 1
            if expected_type != final_type:
                conflicted += 1
                disagreed  += 1
        elif is_passthrough_prose:
            valid_pre  += 1  # count as valid pre output
            # agreement — no conflict, no disagreement

    conflict_rate = (
        round(conflicted / total_valid, 4)
        if total_valid > 0 else 0.0
    )
    disagreement_rate = (
        round(disagreed / valid_pre, 4)
        if valid_pre > 0 else 0.0
    )

    return {
        "total_valid":       total_valid,
        "valid_pre":         valid_pre,
        "conflicted":        conflicted,
        "disagreed":         disagreed,
        "conflict_rate":     conflict_rate,
        "disagreement_rate": disagreement_rate,
    }


# ---------------------------------
# Session ID
# ---------------------------------

def _session_id() -> str:
    return str(uuid.uuid4()).replace("-", "")[:16]


# ---------------------------------
# Core builders
# ---------------------------------

def build_session_telemetry(
    output:    PostprocessOutput,
    envelopes: Optional[List[Any]] = None,
) -> SessionTelemetry:
    """
    Build SessionTelemetry from PostprocessOutput.

    rule_trigger_counts — canonical names only, mapped from raw flags
    conflict_rate       — structural conflict, system level
    classifier_disagreement_rate — model disagreement, ML level

    envelopes: pass pre_classifier IngestionEnvelopes for conflict metrics
    """
    rule_triggers = _compute_rule_triggers(output)
    metrics       = _compute_conflict_metrics(output, envelopes)
    segments      = output["machine_readable"]["segments"]

    return SessionTelemetry(
        conflict_rate=metrics["conflict_rate"],
        classifier_disagreement_rate=metrics["disagreement_rate"],
        rule_trigger_counts=rule_triggers,
        total_segments=len(segments),
        valid_segments=metrics["total_valid"],
        conflicted_segments=metrics["conflicted"],
        disagreed_segments=metrics["disagreed"],
    )


def build_session_trace(
    output:        PostprocessOutput,
    raw_line_count: int,
    envelopes:     Optional[List[Any]] = None,
) -> SessionTrace:
    """
    Build full SessionTrace envelope.
    """
    telemetry = build_session_telemetry(output, envelopes)
    segments  = output["machine_readable"]["segments"]

    return SessionTrace(
        session_id=_session_id(),
        pipeline_version=PIPELINE_VERSION,
        timestamp=datetime.now(timezone.utc).isoformat(),
        raw_line_count=raw_line_count,
        segment_count=len(segments),
        telemetry=telemetry,
    )
