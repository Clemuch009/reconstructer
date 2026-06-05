# analysis/structure_engine/router.py

from typing import List, Dict
from typing_extensions import TypedDict
from analysis.structure_engine.line_model import LineObject
from analysis.structure_engine.region_classifier import (
    RegionResult,
    classify_region,
    CONFIDENCE_FALLBACK_THRESHOLD,
)


# ---------------------------------
# Contract
# ---------------------------------

class RoutedRegion(TypedDict):
    region: RegionResult
    engine: str              # "structure_engine" | "block_engine" | "fallback_engine"
    lines:  List[LineObject]


# ---------------------------------
# Engine labels (locked)
# ---------------------------------

STRUCTURE_ENGINE = "structure_engine"
BLOCK_ENGINE     = "block_engine"
FALLBACK_ENGINE  = "fallback_engine"


# ---------------------------------
# Routing rules (locked)
# ---------------------------------

def _route(region: RegionResult) -> str:
    """
    Deterministic routing based on region type and confidence.

    Rules:
    1. table_candidate with low confidence → block_engine (ghost table prevention)
    2. table_candidate with sufficient confidence → structure_engine
    3. structured_block → block_engine
    4. unstructured → fallback_engine

    Safety rule applies to table_candidate ONLY.
    structured_block and unstructured route normally regardless of confidence.
    """
    if region["region_type"] == "table_candidate":
        if region["confidence"] < CONFIDENCE_FALLBACK_THRESHOLD:
            return BLOCK_ENGINE
        return STRUCTURE_ENGINE

    if region["region_type"] == "structured_block":
        return BLOCK_ENGINE

    return FALLBACK_ENGINE


# ---------------------------------
# Core
# ---------------------------------

def route_lines(lines: List[LineObject]) -> RoutedRegion:
    """
    Classify and route a list of LineObjects.
    Returns region classification + assigned engine + original lines.
    """
    region = classify_region(lines)
    engine = _route(region)

    return RoutedRegion(
        region=region,
        engine=engine,
        lines=lines,
    )


def route_all(
    segments: List[List[LineObject]],
) -> Dict[str, List[RoutedRegion]]:
    """
    Route all segments and group by engine.

    Returns:
    {
        "structure_engine": [...],
        "block_engine":     [...],
        "fallback_engine":  [...],
    }
    """
    result: Dict[str, List[RoutedRegion]] = {
        STRUCTURE_ENGINE: [],
        BLOCK_ENGINE:     [],
        FALLBACK_ENGINE:  [],
    }

    for lines in segments:
        routed = route_lines(lines)
        result[routed["engine"]].append(routed)

    return result


# ---------------------------------
# Interactive Test
# ---------------------------------

if __name__ == "__main__":
    import sys
    from analysis.structure_engine.line_model import build_line_model

    print("\n" + "=" * 60)
    print("ROUTER INTERACTIVE TEST")
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
        routed = route_lines(model)

        print("\n--- ROUTING RESULT ---\n")
        print(f"  region_type : {routed['region']['region_type']}")
        print(f"  confidence  : {routed['region']['confidence']}")
        print(f"  engine      : {routed['engine']}")
        print(f"  start_line  : {routed['region']['start_line']}")
        print(f"  end_line    : {routed['region']['end_line']}")
        print("-" * 60)
