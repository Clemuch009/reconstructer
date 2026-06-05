"""
Shared structural definition: HIERARCHY.

Single canonical definition of "is this block a hierarchy, and at what depths,"
imported by every layer that reasons about hierarchy so all layers agree on ONE
field-agnostic definition instead of each re-deriving its own private one.

A block is a hierarchy when each line carries a DEPTH signaled by leading
whitespace indentation, depths nest consistently (descend one level at a time;
pop up any number of levels; never jump down more than one level at once), and
real depth variation exists. No surface marker (+--, def, tabs) appears in the
definition: the marker is how a field DRAWS depth; this module measures the
depth itself.

SCOPE (this version):
- Covers whitespace/tab-indented hierarchies, including ascii "+--" trees whose
  depth is carried by leading spaces (the live banking-tree case), Python
  indentation, and tab-indented outlines/org-charts.
- Does NOT yet cover trees whose depth is drawn purely with leading vertical box
  glyphs ("|   ", "│   ├──") and no whitespace indent. Those collide with
  pipe-table borders ("| a |") over what a leading vertical bar means, and are
  deferred until the TABLE invariant defines the pipe-vs-tree boundary. Such
  input falls through (state != "hierarchy") rather than being misclassified.
"""

import re
from math import gcd
from functools import reduce
from typing import List
from typing_extensions import TypedDict


# ---------------------------------
# Contract
# ---------------------------------

class HierarchyVerdict(TypedDict):
    is_hierarchy:   bool
    depths:         List[int]
    max_depth:      int
    valid_fraction: float
    state:          str          # "hierarchy" | "degraded" | "flat" | "insufficient"


# ---------------------------------
# Thresholds (locked — single source of truth)
# ---------------------------------

VALID_FRACTION_MIN = 0.90
MIN_NONEMPTY_LINES = 2


# ---------------------------------
# Depth measurement
# ---------------------------------

# Depth prefix = leading whitespace only. Vertical glyphs are intentionally
# excluded here (see SCOPE) to avoid colliding with pipe-table borders.
_PREFIX_RE = re.compile(r"^([ \t]*)")


def structural_prefix_width(line: str) -> int:
    """Width of the leading whitespace prefix (spaces, tabs)."""
    return len(_PREFIX_RE.match(line).group(1))


def discover_unit(prefix_widths: List[int]) -> int:
    """
    Indent unit = GCD of positive prefix widths, floored at 1. Adapts to
    2/4/8-space layouts and single-tab layouts without assuming a fixed size.
    """
    positive = [w for w in prefix_widths if w > 0]
    if not positive:
        return 1
    return max(reduce(gcd, positive), 1)


def line_depths(lines: List[str]) -> List[int]:
    """
    Per non-empty line: prefix_width // unit, normalized so the shallowest
    line is depth 0. Empty lines are ignored.
    """
    non_empty = [l for l in lines if l.strip()]
    if not non_empty:
        return []
    widths = [structural_prefix_width(l) for l in non_empty]
    unit = discover_unit(widths)
    raw = [w // unit for w in widths]
    base = min(raw)
    return [d - base for d in raw]


def _valid_fraction(depths: List[int]) -> float:
    """
    Fraction of consecutive depth transitions that are structurally valid:
    sibling (==), descend one (+1), or pop up (any amount <). A downward jump
    of more than one level is invalid (a node deeper than its parent allows).
    """
    if len(depths) < 2:
        return 0.0
    ok = sum(
        1 for i in range(len(depths) - 1)
        if depths[i + 1] == depths[i]
        or depths[i + 1] == depths[i] + 1
        or depths[i + 1] < depths[i]
    )
    return ok / (len(depths) - 1)


# ---------------------------------
# Canonical entry point
# ---------------------------------

def hierarchy_signal(lines: List[str]) -> HierarchyVerdict:
    """Block-level hierarchy verdict. The single canonical entry point."""
    depths = line_depths(lines)

    if len(depths) < MIN_NONEMPTY_LINES:
        return HierarchyVerdict(
            is_hierarchy=False, depths=depths,
            max_depth=(max(depths) if depths else 0),
            valid_fraction=0.0, state="insufficient",
        )

    if not (max(depths) > min(depths)):
        return HierarchyVerdict(
            is_hierarchy=False, depths=depths,
            max_depth=max(depths), valid_fraction=1.0, state="flat",
        )

    frac = _valid_fraction(depths)
    if frac < VALID_FRACTION_MIN:
        return HierarchyVerdict(
            is_hierarchy=False, depths=depths,
            max_depth=max(depths), valid_fraction=round(frac, 4), state="degraded",
        )

    return HierarchyVerdict(
        is_hierarchy=True, depths=depths,
        max_depth=max(depths), valid_fraction=round(frac, 4), state="hierarchy",
    )
