# relationship_engine/planner.py
#
# The Planner decides WHICH capabilities to invoke based on the data shape,
# rather than running a fixed pipeline. This is the seam that lets higher
# capabilities (Group, Structural, Graph, Temporal) plug in later without
# rewiring callers — the strongly-recommended design from the reviews.
#
# Today it knows exactly one path (Layer 1, pair matching). But it already
# INSPECTS the data and reports what it chose and why, so when Group matching
# exists, the planner routes to it when 1:1 doesn't hold — and callers are
# unchanged.

from typing import Any, Dict, List

from relationship_engine.core import match_pairs, _index_by_identity


def plan_and_run(
    source_a: List[Dict[str, Any]],
    source_b: List[Dict[str, Any]],
    profile:  Dict[str, Any],
) -> Dict[str, Any]:
    """
    Inspect the data, choose capabilities, run them.

    profile (declarative) supplies:
      "match":   {"field"/"fields", "normalizers": [...]}   — identity key
      "compare": [ rule, ... ]                              — consistency rules

    Returns the capability output plus a `plan` describing what ran and why.
    """
    key_spec      = profile.get("match", {})
    compare_rules = profile.get("compare", [])

    # Inspect: is this plausibly a clean 1:1 problem?
    idx_a, _ = _index_by_identity(source_a, key_spec)
    idx_b, _ = _index_by_identity(source_b, key_spec)
    dup_a = any(len(v) > 1 for v in idx_a.values())
    dup_b = any(len(v) > 1 for v in idx_b.values())

    plan: List[str] = ["normalize", "identity", "candidate_generation"]
    reasons: List[str] = []

    if dup_a or dup_b:
        # A key maps to multiple records on a side. Layer 1 handles the clean
        # keys and REPORTS the rest as not_one_to_one; it does not guess.
        reasons.append(
            "duplicate identity keys detected — clean keys matched 1:1; "
            "repeated keys flagged for Group matching (not yet built)")
    else:
        reasons.append("clean 1:1 identity keys — pair matching sufficient")

    plan.append("pair_matching")
    plan.append("consistency")

    result = match_pairs(source_a, source_b, key_spec, compare_rules)
    result["plan"] = {
        "capabilities": plan,
        "reasons":      reasons,
        # Named, not-yet-built capabilities the planner will route to later.
        "deferred": ["group_matching", "structural_matching",
                     "graph_matching", "temporal", "probabilistic"],
    }
    return result
