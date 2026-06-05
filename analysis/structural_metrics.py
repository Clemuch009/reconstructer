# analysis/structural_metrics.py

from typing import Dict, Any
import statistics
from core.signals_contract import validate_all_signals

STRENGTH_WEIGHTS = {
    "absolute": 1.0,
    "strong": 0.75,
    "medium": 0.5,
    "weak": 0.25,
}


def compute_spacy_alignment(regex_signals, spacy_spans, tolerance=5):
    if not spacy_spans or not regex_signals:
        return 0.0

    internal_positions = [
        s["end"]
        for s in regex_signals
        if s["type"] in {"separator", "empty_line"}
    ]

    if not internal_positions:
        return 0.0

    matches = 0

    for (_, sp_end) in spacy_spans:
        for ip in internal_positions:
            if abs(sp_end - ip) <= tolerance:
                matches += 1
                break

    return matches / len(spacy_spans)


def compute_structural_metrics(signals: Dict[str, Any], text_length: int) -> Dict[str, Any]:
    """
    Computes structural metrics. 
    Applies signal contract validation before processing.
    """
    # ---------------------------------
    # GATE: Contract Validation
    # ---------------------------------
    validate_all_signals(signals, text_length)

    regex = signals.get("regex", {})
    boundary = signals.get("boundary", {})
    text = signals.get("text", {})
    spacy = signals.get("spacy", {})

    total_lines = max(text.get("total_lines", 1), 1)

    # ---------------------------------
    # 1. Structural Strength
    # ---------------------------------
    strength_score = 0.0

    for sig in regex.get("signals", []):
        strength = sig.get("strength")
        if strength in STRENGTH_WEIGHTS:
            strength_score += STRENGTH_WEIGHTS[strength]

    strength_score += (
        boundary.get("paragraph_breaks", 0) * STRENGTH_WEIGHTS["strong"]
    )
    strength_score += (
        boundary.get("list_block_transitions", 0) * STRENGTH_WEIGHTS["medium"]
    )
    strength_score += (
        boundary.get("sentence_end_lines", 0) * STRENGTH_WEIGHTS["weak"]
    )

    normalized_strength = strength_score / total_lines

    # ---------------------------------
    # 2. Boundary Pressure
    # ---------------------------------
    boundary_pressure = (
        boundary.get("paragraph_breaks", 0)
        + boundary.get("list_block_transitions", 0)
    ) / total_lines

    # ---------------------------------
    # 3. Consistency
    # ---------------------------------
    line_lengths = text.get("line_lengths")

    if not line_lengths:
        raise ValueError("line_lengths missing from text_signals")

    variance = statistics.pvariance(line_lengths) if len(line_lengths) > 1 else 0.0
    consistency_score = 1 / (1 + variance)

    # ---------------------------------
    # 4. spaCy Alignment
    # ---------------------------------
    alignment = compute_spacy_alignment(
        regex.get("signals", []),
        spacy.get("spacy_sentence_spans", [])
    )

    return {
        "normalized_strength": normalized_strength,
        "boundary_pressure": boundary_pressure,
        "consistency_score": consistency_score,
        "spacy_alignment": alignment
    }
