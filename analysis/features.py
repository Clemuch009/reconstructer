# features.py

from typing import Dict, Any
#import math
from core.signals_contract import validate_features


def clamp(x: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, x))


def soft_normalize(x: float, scale: float = 2.0) -> float:
    """
    Compress unbounded values into (0,1)-like range without hard cutoff.
    """
    return x / (x + scale) if x > 0 else 0.0


def build_feature_vector(metrics: Dict[str, Any]) -> Dict[str, float]:
    """
    Convert structural metrics into bounded decision-space features.
    LIFECYCLE: Called AFTER structural_metrics.py, BEFORE decision.py.
    """

    raw_strength = metrics.get("normalized_strength", 0.0)
    boundary_pressure = metrics.get("boundary_pressure", 0.0)
    consistency_score = metrics.get("consistency_score", 0.0)
    spacy_alignment = metrics.get("spacy_alignment", 0.0)

    # ---------------------------------
    # 1. Structure Intensity (bounded)
    # ---------------------------------
    structure_intensity = soft_normalize(raw_strength, scale=1.0)

    # ---------------------------------
    # 2. Boundary Intensity (bounded)
    # ---------------------------------
    boundary_intensity = soft_normalize(boundary_pressure, scale=1.0)

    # ---------------------------------
    # 3. Stability (already bounded but enforced)
    # ---------------------------------
    stability = clamp(consistency_score)

    # ---------------------------------
    # 4. External Agreement (bounded)
    # ---------------------------------
    external_agreement = clamp(spacy_alignment)

    # ---------------------------------
    # 5. Chaos Index (DEFINED ROLE PROPERLY)
    # ---------------------------------
    # Represents structural noise: high boundary pressure + low stability
    chaos_index = boundary_intensity * (1.0 - stability)

    # second-order compression
    chaos_index = soft_normalize(chaos_index, scale=0.5)

    feature_vector = {
        "structure_intensity": float(structure_intensity),
        "boundary_intensity": float(boundary_intensity),
        "stability": float(stability),
        "external_agreement": float(external_agreement),
        "chaos_index": float(chaos_index),
    }

    # ---------------------------------
    # GATE: Final Contract Validation
    # ---------------------------------
    # Ensures all features are in [0.0, 1.0] and keys match contract
    validate_features(feature_vector)

    return feature_vector
