from typing import List, Dict, Set, Tuple, Optional
from typing_extensions import TypedDict
from analysis.structure_engine.detectors.hierarchy_detector import (
    HierarchyResult,
    HierarchyNode,
)


# ---------------------------------
# Contracts
# ---------------------------------

class TreeNode(TypedDict):
    line_index: int
    depth:      int
    text:       str
    children:   List["TreeNode"]


class TreeResult(TypedDict):
    region_type:  str
    roots:        List[TreeNode]
    max_depth:    int
    node_count:   int
    orphan_count: int


# ---------------------------------
# Iterative cycle detection (heap-based)
# ---------------------------------

def _iterative_has_cycle(
    start: int,
    children_map: Dict[int, List[int]],
    visited: Set[int],
    cyclic: Set[int],
) -> bool:
    """
    Iterative DFS cycle detection using explicit stack on heap.
    Prevents RecursionError on deep trees.
    Stack frame: (current_node, child_iterator_index)
    """
    if start in visited:
        return False

    stack: List[Tuple[int, int]] = [(start, 0)]
    path_set: Set[int] = {start}

    while stack:
        curr, child_idx = stack[-1]
        children = children_map.get(curr, [])

        if child_idx >= len(children):
            # All children processed — backtrack
            stack.pop()
            path_set.discard(curr)
            visited.add(curr)
            continue

        # Advance child index on current frame
        stack[-1] = (curr, child_idx + 1)
        child = children[child_idx]

        if child in path_set:
            # Cycle detected — mark every node on the back-edge slice
            path_nodes = [node for node, _ in stack]
            cut        = path_nodes.index(child)
            cyclic.update(path_nodes[cut:])
            return True  # Cycle detected

        if child not in visited:
            stack.append((child, 0))
            path_set.add(child)

    return False


def _detect_cyclic_nodes(
    children_map: Dict[int, List[int]],
) -> Set[int]:
    """
    Returns set of node indices involved in cycles.
    Uses iterative DFS — safe for large inputs.
    """
    visited: Set[int] = set()
    cyclic:  Set[int] = set()

    for node_idx in children_map:
        _iterative_has_cycle(node_idx, children_map, visited, cyclic)

    return cyclic


# ---------------------------------
# Three-step iterative tree assembly
# ---------------------------------

def _assemble_tree(
    node_map: Dict[int, HierarchyNode],
    children_map: Dict[int, List[int]],
    valid_indices: Set[int],
    root_indices: List[int],
) -> Tuple[List[TreeNode], int, int]:
    """
    Three-step iterative assembly pipeline.

    Step 1 — Instantiate all blank TreeNode shells in one linear pass.
    Step 2 — Map child object references iteratively via flat lookup.
    Step 3 — Resolve and return roots list.

    Returns (roots, node_count, orphan_count).
    node_count incremented on each physical instantiation.
    orphan_count tracks dropped references explicitly.
    """
    # Step 1 — Instantiate blank shells
    shells: Dict[int, TreeNode] = {}
    for idx in valid_indices:
        source = node_map[idx]
        shells[idx] = TreeNode(
            line_index=source["line_index"],
            depth=source["depth"],
            text=source["text"],
            children=[],
        )

    node_count   = len(shells)
    orphan_count = 0

    # Step 2 — Map child references iteratively
    for idx in valid_indices:
        for child_idx in children_map.get(idx, []):
            if child_idx in shells:
                shells[idx]["children"].append(shells[child_idx])
            else:
                # Orphan — child index missing from valid set
                orphan_count += 1

    # Step 3 — Resolve roots
    roots = [shells[idx] for idx in root_indices if idx in shells]

    return roots, node_count, orphan_count


# ---------------------------------
# Root resolution
# ---------------------------------

def _resolve_roots(
    valid_indices: Set[int],
    children_map: Dict[int, List[int]],
) -> List[int]:
    """
    True root detection across arbitrary indentation blocks.
    Any unparented valid node constitutes a valid tree root anchor.
    Removes min_depth restriction — does not assume uniform tree origins.
    """
    active_children: Set[int] = set()
    for idx in valid_indices:
        for child_idx in children_map.get(idx, []):
            if child_idx in valid_indices:
                active_children.add(child_idx)

    return sorted(
        idx for idx in valid_indices
        if idx not in active_children
    )


# ---------------------------------
# Max depth from assembled tree
# ---------------------------------

def _compute_max_depth(roots: List[TreeNode]) -> int:
    """
    Iterative max depth computation — avoids recursion on deep trees.
    Uses explicit stack on heap.
    """
    if not roots:
        return 0

    max_d = 0
    stack: List[TreeNode] = list(roots)

    while stack:
        node = stack.pop()
        if node["depth"] > max_d:
            max_d = node["depth"]
        stack.extend(node["children"])

    return max_d


# ---------------------------------
# Core
# ---------------------------------

def build_tree(hierarchy: HierarchyResult) -> Optional[TreeResult]:
    """
    Compile HierarchyResult into a recursive TreeResult.

    Rules:
    - Input is read-only — HierarchyResult never mutated
    - O(N) index cache and three-step iterative assembly
    - Cycle detection is iterative — no RecursionError risk
    - Orphan references tracked explicitly in orphan_count
    - Root = not an active child AND at minimum depth in valid set
    - node_count reflects physical instantiations only
    - Logical depth preserved from HierarchyDetector unchanged
    """
    if not hierarchy or not hierarchy["nodes"]:
        return None

    nodes = hierarchy["nodes"]

    # O(N) index cache — immutable input, fresh structures
    node_map: Dict[int, HierarchyNode] = {
        n["line_index"]: n for n in nodes
    }

    children_map: Dict[int, List[int]] = {
        n["line_index"]: list(n["children"]) for n in nodes
    }

    # Detect and exclude cyclic nodes
    cyclic = _detect_cyclic_nodes(children_map)
    valid_indices: Set[int] = set(node_map.keys()) - cyclic

    if not valid_indices:
        return None

    # Resolve roots explicitly
    root_indices = _resolve_roots(valid_indices, children_map)

    if not root_indices:
        return None

    # Assemble tree iteratively
    roots, node_count, orphan_count = _assemble_tree(
        node_map, children_map, valid_indices, root_indices
    )

    if not roots:
        return None

    max_depth = _compute_max_depth(roots)

    return TreeResult(
        region_type="tree_structure",
        roots=roots,
        max_depth=max_depth,
        node_count=node_count,
        orphan_count=orphan_count,
    )
