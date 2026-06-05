# analysis/structure_engine/detectors/hierarchy_detector.py

import math
from functools import reduce
from typing import List, Optional, Dict
from typing_extensions import TypedDict
from analysis.structure_engine.line_model import LineObject


# ---------------------------------
# Contracts
# ---------------------------------

class HierarchyNode(TypedDict):
    line_index: int
    depth:      int
    text:       str
    children:   List[int]


class HierarchyResult(TypedDict):
    region_type: str
    nodes:       List[HierarchyNode]
    max_depth:   int
    root_count:  int
    confidence:  float


# ---------------------------------
# Constants
# ---------------------------------

MIN_VALID_DEPTH   = 2
DEFAULT_INDENT_UNIT = 4


# ---------------------------------
# Dynamic indent unit discovery
# ---------------------------------

def _discover_indent_unit(lines: List[LineObject]) -> int:
    """
    Dynamically calculate indent step size via GCD across all positive indents.
    Prevents single-space tautology and adapts to 2, 4, or 8-space layouts.
    Falls back to DEFAULT_INDENT_UNIT if no positive indents found.
    """
    indents = [l["indent"] for l in lines if l["indent"] > 0]
    if not indents:
        return DEFAULT_INDENT_UNIT
    gcd_val = reduce(math.gcd, indents)
    return gcd_val if gcd_val >= 2 else 2


def _indent_to_depth(indent: int, unit: int) -> int:
    return indent // unit


# ---------------------------------
# Tree construction
# ---------------------------------

def _build_tree(
    lines: List[LineObject],
    unit: int,
) -> Dict[int, HierarchyNode]:
    """
    Build parent-child relationships from indentation.

    Rules:
    - depth derived from line["indent"] only — pre-normalized by line_model
    - natural document order preserved — no sorting by key
    - children attach to nearest valid ancestor via stack
    - no cross-level downward skipping
    - upward jumps (scope closure) always valid
    """
    nodes: Dict[int, HierarchyNode] = {}

    # Build all nodes in natural document order
    for line in lines:
        depth = _indent_to_depth(line["indent"], unit)
        nodes[line["line_index"]] = HierarchyNode(
            line_index=line["line_index"],
            depth=depth,
            text=line["normalized"],
            children=[],
        )

    # Build parent-child using stack — natural order preserved
    stack: List[int] = []

    for line in lines:
        line_idx = line["line_index"]
        node = nodes[line_idx]
        current_depth = node["depth"]

        # Pop until valid parent found
        while stack and nodes[stack[-1]]["depth"] >= current_depth:
            stack.pop()

        if stack:
            nodes[stack[-1]]["children"].append(line_idx)

        stack.append(line_idx)

    return nodes


# ---------------------------------
# Confidence scoring
# ---------------------------------

def _depth_consistency(
    nodes: Dict[int, HierarchyNode],
    lines: List[LineObject],
) -> float:
    ordered = [nodes[l["line_index"]] for l in lines if l["line_index"] in nodes]
    if len(ordered) < 2:
        return 1.0

    smooth = 0
    total  = len(ordered) - 1

    for i in range(total):
        curr_depth = ordered[i]["depth"]
        next_depth = ordered[i + 1]["depth"]

        if next_depth > curr_depth:
            if (next_depth - curr_depth) <= 1:
                smooth += 1
        else:
            smooth += 1  # upward closure always smooth

    return smooth / total


def _branching_validity(
    nodes: Dict[int, HierarchyNode],
    lines: List[LineObject],
) -> float:
    """
    Directional validation:
    - Downward skips > 1 level are illegal
    - Upward jumps (scope closure) are always valid
    Uses natural document order.
    """
    ordered = [nodes[l["line_index"]] for l in lines if l["line_index"] in nodes]
    if len(ordered) < 2:
        return 1.0

    valid = 0
    total = len(ordered) - 1

    for i in range(total):
        curr_depth = ordered[i]["depth"]
        next_depth = ordered[i + 1]["depth"]

        # Illegal: downward skip of more than 1 level
        if next_depth > curr_depth and (next_depth - curr_depth) > 1:
            continue

        valid += 1

    return valid / total


def _indent_stability(
    non_empty: List[LineObject],
    unit: int,
) -> float:
    if not non_empty:
        return 0.0
    stable = sum(1 for l in non_empty if l["indent"] % unit == 0)
    return stable / len(non_empty)


def _branch_factor(nodes: Dict[int, HierarchyNode]) -> float:
    """
    Average children per non-leaf node.
    Captures structural richness — deeper branching = higher score.
    Normalized to 0-1 via min(1.0, avg / 3).
    """
    non_leaf = [n for n in nodes.values() if n["children"]]
    if not non_leaf:
        return 0.0

    avg = sum(len(n["children"]) for n in non_leaf) / len(non_leaf)
    return min(1.0, avg / 3)


def _score_confidence(
    nodes: Dict[int, HierarchyNode],
    lines: List[LineObject],
    unit: int,
) -> float:
    dc  = _depth_consistency(nodes, lines)
    bv  = _branching_validity(nodes, lines)
    is_ = _indent_stability(lines, unit)
    bf  = _branch_factor(nodes)

    return round(
        dc  * 0.35 +
        bv  * 0.25 +
        is_ * 0.25 +
        bf  * 0.15,
        2
    )


# ---------------------------------
# Core
# ---------------------------------

def detect_hierarchy(lines: List[LineObject]) -> Optional[HierarchyResult]:
    """
    Detect indentation-based hierarchy from LineObjects.

    Rules:
    - depth from line["indent"] only — pre-normalized by line_model
    - tab normalization handled upstream in line_model
    - dynamic indent unit via GCD
    - natural document order preserved throughout
    - requires max_depth >= MIN_VALID_DEPTH
    - illegal downward skips penalize confidence
    - upward jumps always valid
    - no semantic interpretation
    """
    non_empty = [l for l in lines if not l["is_empty"]]
    if not non_empty:
        return None

    unit  = _discover_indent_unit(non_empty)
    nodes = _build_tree(non_empty, unit)

    if not nodes:
        return None

    max_depth = max(n["depth"] for n in nodes.values())

    if max_depth < MIN_VALID_DEPTH:
        return None

    root_count = sum(
        1 for n in nodes.values()
        if n["depth"] == 0
    )

    confidence = _score_confidence(nodes, non_empty, unit)

    return HierarchyResult(
        region_type="hierarchy",
        nodes=list(nodes.values()),
        max_depth=max_depth,
        root_count=root_count,
        confidence=confidence,
    )


# ---------------------------------
# Interactive Test
# ---------------------------------

if __name__ == "__main__":
    import sys
    from analysis.structure_engine.line_model import build_line_model

    print("\n" + "=" * 60)
    print("HIERARCHY DETECTOR INTERACTIVE TEST")
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

        model  = build_line_model(raw)
        result = detect_hierarchy(model)

        print("\n--- HIERARCHY DETECTION ---\n")
        if result is None:
            print("No valid hierarchy detected.")
        else:
            print(f"  max_depth  : {result['max_depth']}")
            print(f"  root_count : {result['root_count']}")
            print(f"  confidence : {result['confidence']}")
            print(f"\n  nodes:")
            for node in result["nodes"]:
                indent_str = "  " * node["depth"]
                print(
                    f"    [{node['line_index']}] "
                    f"depth={node['depth']} "
                    f"{indent_str}{repr(node['text'])[:40]} "
                    f"→ {node['children']}"
                )

        print("-" * 60)
