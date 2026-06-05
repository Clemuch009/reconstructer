# analysis/structure_engine/detectors/kv_detector.py

import re
from typing import List, Optional
from typing_extensions import TypedDict
from analysis.structure_engine.line_model import LineObject
from analysis.structure_engine.pattern_extractor import LinePattern


# ---------------------------------
# Contract
# ---------------------------------

class KVResult(TypedDict):
    line_index: int
    key: str
    value: str
    delimiter: str
    confidence: float
    patterns_used: List[str]


# ---------------------------------
# Allowed delimiters (locked)
# ---------------------------------

ALLOWED_DELIMITERS = [":", "="]


# ---------------------------------
# Confidence scoring (deterministic)
# ---------------------------------

def _score_confidence(key: str, value: str) -> float:
    """
    Deterministic confidence scale:
    1.0 — clean split, non-empty key and value
    0.8 — non-empty key, empty value (Name: case)
    0.0 — invalid (empty key)
    """
    if not key:
        return 0.0
    if not value:
        return 0.8
    return 1.0


# ---------------------------------
# Pattern confirmation
# ---------------------------------

def _confirm_patterns(
    line_pattern: Optional[LinePattern],
    key: str,
    value: str,
) -> List[str]:
    """
    Confirm which upstream patterns are validated by the KV result.
    patterns_used reflects confirmed structural facts, not hints.

    Rules:
    - kv_pair confirmed if KV detection succeeded (key non-empty)
    - numeric_value confirmed if value contains a numeric token
    - all other patterns excluded — not confirmed by KV detection
    """
    if line_pattern is None:
        return []

    confirmed = []
    available = set(line_pattern["patterns"])

    # kv_pair confirmed by successful detection
    if "kv_pair" in available and key:
        confirmed.append("kv_pair")

    # numeric_value confirmed if value carries numeric content
    if "numeric_value" in available and value and re.search(r"\b\d[\d,\.]*\b", value):
        confirmed.append("numeric_value")

    print(f"[DEBUG] available={available}, key={repr(key)}, value={repr(value)}")

    return confirmed


# ---------------------------------
# Core
# ---------------------------------

def detect_kv(
    line: LineObject,
    line_pattern: Optional[LinePattern] = None,
) -> Optional[KVResult]:
    """
    Detect key-value pair from a single LineObject.
    KV detector is the authority — line_pattern is advisory only.

    Rules:
    - Split on earliest occurring delimiter (: or =)
    - Both key and value stripped
    - Empty key → rejected (returns None)
    - Empty value → valid, confidence 0.8
    - patterns_used reflects confirmed facts, not hints
    """
    if line["is_empty"]:
        return None

    normalized = line["normalized"]

    # Find earliest delimiter by position
    positions = [
        (d, normalized.find(d))
        for d in ALLOWED_DELIMITERS
        if d in normalized
    ]

    if not positions:
        return None

    delimiter_found, _ = min(positions, key=lambda x: x[1])

    # Split on first occurrence only
    parts = normalized.split(delimiter_found, 1)
    if len(parts) != 2:
        return None

    key = parts[0].strip()
    value = parts[1].strip()

    # Reject empty key
    if not key:
        return None

    confidence = _score_confidence(key, value)
    confirmed_patterns = _confirm_patterns(line_pattern, key, value)

    return KVResult(
        line_index=line["line_index"],
        key=key,
        value=value,
        delimiter=delimiter_found,
        confidence=confidence,
        patterns_used=confirmed_patterns,
    )


def detect_all_kv(
    line_model: List[LineObject],
    line_patterns: Optional[List[LinePattern]] = None,
) -> List[KVResult]:
    """
    Run KV detection across all lines in a segment.
    line_patterns is optional — KV runs independently without it.
    Returns only confirmed KV results.
    """
    pattern_map = {}
    if line_patterns:
        pattern_map = {lp["line_index"]: lp for lp in line_patterns}

    results = []
    for line in line_model:
        lp = pattern_map.get(line["line_index"])
        result = detect_kv(line, lp)
        if result is not None:
            results.append(result)

    return results


# ---------------------------------
# Interactive Test
# ---------------------------------

if __name__ == "__main__":
    import sys
    from analysis.structure_engine.line_model import build_line_model
    from analysis.structure_engine.pattern_extractor import extract_all_patterns

    print("\n" + "=" * 60)
    print("KV DETECTOR INTERACTIVE TEST")
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

        model = build_line_model(raw)
        patterns = extract_all_patterns(model)
        results = detect_all_kv(model, patterns)

        print("\n--- KV RESULTS ---\n")
        if not results:
            print("No KV pairs detected.")
        else:
            for r in results:
                print(f"[{r['line_index']}]")
                print(f"  key          : {repr(r['key'])}")
                print(f"  value        : {repr(r['value'])}")
                print(f"  delimiter    : {repr(r['delimiter'])}")
                print(f"  confidence   : {r['confidence']}")
                print(f"  patterns_used: {r['patterns_used']}")
                print()

        print("-" * 60)
