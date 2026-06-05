# core/decision.py

from typing import Dict, Any, List
from core.rules import evaluate_position
# from utils.context import get_local_window, compute_local_density


# ---------------------------------
# Main decision function
# This module orchestrates only. All rule logic lives in core/rules.py.
# ---------------------------------
def compute_splits(
    signals: Dict[str, Any],
    features: Dict[str, float],
    text: str
) -> List[int]:
    """
    Determine split positions by orchestrating signal evaluation.

    Delegates specific rule logic to core/rules.py while maintaining
    the authoritative collection of final split indices.

    Returns:
        Sorted list of character indices where splits occur.
    """

    regex = signals.get("regex", {})
    boundary = signals.get("boundary", {})

    splits: List[int] = []

    # ---------------------------------
    # Iterate through positional signals
    # ---------------------------------
    for sig in regex.get("signals", []):

        # Ensure signal has physical grounding
        if sig.get("end") is None:
            continue

        decision = evaluate_position(
            sig=sig,
            boundary=boundary,
            features=features,
            text=text
        )

        if decision.split:
            sig_type = sig.get("type")

            # ✅ FIX: separators should NOT become their own segment
            if sig_type == "separator":
                splits.append(sig["start"]) # split BEFORE separator only
                splits.append(sig["end"])  # THIS LINE HAS BEEN ADDED DURING DEBUG!
            else:
                # original behavior unchanged
                splits.append(sig["start"])
                splits.append(sig["end"])

    # ---------------------------------
    # Deduplicate + sort (Physical integrity)
    # ---------------------------------
    return sorted(set(splits))


# ---------------------------------
# Interactive test
# ---------------------------------
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("ORCHESTRATED DECISION ENGINE TEST")
    print("=" * 60)

    sample_text = (
        "Line A\n"
        "Line B\n\n"
        "Item 1\n"
        "Item 2\n\n"
        "---\n"
        "Final paragraph.\n"
    )

    mock_signals = {
        "regex": {
            "signals": [
                {"type": "newline", "start": 5, "end": 6, "value": "\n", "strength": "weak"},
                {"type": "empty_line", "start": 12, "end": 14, "value": "\n\n", "strength": "strong"},
                {"type": "list_marker", "start": 15, "end": 17, "value": "1.", "strength": "medium"},
                {"type": "separator", "start": 30, "end": 33, "value": "---", "strength": "absolute"},
            ]
        },
        "boundary": {
            "list_block_transition_positions": [17]
        }
    }

    mock_features = {
        "structure_intensity": 0.6,
        "boundary_intensity": 0.4,
        "stability": 0.7,
        "external_agreement": 0.5,
        "chaos_index": 0.2,
    }

    splits = compute_splits(mock_signals, mock_features, sample_text)

    print("\nFINAL SPLIT POSITIONS:")
    for s in splits:
        print(f" - Index {s}")

    print("\nTEXT SEGMENTS:")
    last = 0
    for s in splits:
        print(f"SEGMENT: {repr(sample_text[last:s])}")
        last = s
    print(f"SEGMENT: {repr(sample_text[last:])}")

    print("-" * 60)
