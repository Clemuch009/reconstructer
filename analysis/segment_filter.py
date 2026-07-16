# analysis/segment_filter.py
#
# Foundational platform capability: select processed segments by structural
# type (and optionally by confidence).
#
# This is NOT specific to any vertical (invoices, contracts, etc.). It is a
# general access layer over the engine's classified output: callers ask for the
# structure types they care about ("give me the tables", "give me the key-value
# fields") and receive exactly those, in a stable shape, without having to walk
# the nested segment tree themselves.
#
# Design notes
# ------------
# The engine emits top-level segments whose `type` is one of the structural
# classes (prose, table, kv, context, hierarchy, mixed, ...). A `mixed` segment
# is a container: its content holds `sub_blocks`, each with its own type
# (e.g. kv_block, context, table). So "give me kv" must also reach kv content
# nested inside `mixed` segments — otherwise most real-world kv data (which the
# engine frequently wraps in `mixed`) would be invisible to the filter.
#
# The filter therefore flattens: it yields a match for a top-level segment whose
# type is requested, AND for any sub_block whose type maps to a requested type.
# Nothing is mutated; matches reference the original content.
#
# Type name normalization: callers use simple names (kv, table, context). The
# engine's nested blocks sometimes use suffixed names (kv_block). We normalize
# so "kv" matches both "kv" and "kv_block", "table" matches "table"/"table_block",
# etc. This keeps the public vocabulary small and stable.

from typing import List, Dict, Any, Optional, Iterable


# Canonical structural type names the platform exposes to callers.
CANONICAL_TYPES = {"prose", "table", "kv", "context", "hierarchy", "mixed"}


def _canonical(type_name: Optional[str]) -> Optional[str]:
    """
    Normalize an engine/internal type name to a canonical public type.
    e.g. 'kv_block' -> 'kv', 'table_block' -> 'table'. Unknown names pass
    through unchanged so nothing is silently lost.
    """
    if not type_name:
        return None
    name = type_name.strip().lower()
    for base in ("kv", "table", "context", "hierarchy", "prose"):
        if name == base or name.startswith(base + "_"):
            return base
    return name


def _iter_matches(
    segment: Dict[str, Any],
    wanted: Optional[set],
    min_confidence: float,
) -> Iterable[Dict[str, Any]]:
    """
    Yield filter matches from a single top-level segment.

    A match is a small, stable record:
        {
          "segment_id": <id of the top-level segment>,
          "type":       <canonical type of the matched unit>,
          "content":    <the matched content (segment or sub_block content)>,
          "confidence": <confidence if available, else None>,
          "nested":     <True if this came from a sub_block, else False>,
        }

    For a plain segment (prose/table/etc.) the match is the segment itself.
    For a `mixed` segment, each qualifying sub_block yields its own match, so
    callers asking for "kv" receive the kv sub_blocks directly.
    """
    seg_id     = segment.get("segment_id")
    seg_type   = _canonical(segment.get("type"))
    metadata   = segment.get("metadata", {}) or {}
    confidence = metadata.get("confidence")
    content    = segment.get("content", {})

    def _passes_conf(conf) -> bool:
        # If no confidence is present, do not exclude on a confidence filter
        # (absence is not low-confidence). Only exclude when a value exists and
        # is below the threshold.
        if min_confidence <= 0.0:
            return True
        if conf is None:
            return True
        return conf >= min_confidence

    # Case 1: mixed container — descend into sub_blocks.
    if seg_type == "mixed" and isinstance(content, dict) and "sub_blocks" in content:
        # If the caller explicitly wants "mixed" itself, also yield the whole.
        if (wanted is None or "mixed" in wanted) and _passes_conf(confidence):
            yield {
                "segment_id": seg_id,
                "type":       "mixed",
                "content":    content,
                "confidence": confidence,
                "nested":     False,
            }
        for sub in content.get("sub_blocks", []):
            sub_type = _canonical(sub.get("type"))
            sub_conf = sub.get("confidence", confidence)
            if (wanted is None or sub_type in wanted) and _passes_conf(sub_conf):
                yield {
                    "segment_id": seg_id,
                    "type":       sub_type,
                    "content":    sub.get("content", sub),
                    "confidence": sub_conf,
                    "nested":     True,
                }
        return

    # Case 2: plain segment.
    if (wanted is None or seg_type in wanted) and _passes_conf(confidence):
        yield {
            "segment_id": seg_id,
            "type":       seg_type,
            "content":    content,
            "confidence": confidence,
            "nested":     False,
        }


def filter_segments(
    segments: List[Dict[str, Any]],
    types: Optional[Iterable[str]] = None,
    min_confidence: float = 0.0,
) -> List[Dict[str, Any]]:
    """
    Select segments (and nested sub-blocks) by canonical structural type and
    optional minimum confidence.

    Parameters
    ----------
    segments : the engine's machine_readable["segments"] list.
    types    : iterable of canonical type names to keep (e.g. ["table", "kv"]).
               None means "all types" (no type filtering).
    min_confidence : keep only matches whose confidence >= this value. Matches
               without a confidence value are kept (absence != low confidence).
               Default 0.0 keeps everything.

    Returns
    -------
    A flat list of match records (see _iter_matches for the shape). Order follows
    document order; nested kv/table blocks appear in place of their `mixed`
    parent. Nothing is mutated or dropped beyond the requested filter.

    This is a pure function: deterministic, side-effect free, and safe to call
    on any engine output. It is the foundational primitive other platform
    features (vertical extractors, validation, export) build on.
    """
    wanted: Optional[set]
    if types is None:
        wanted = None
    else:
        wanted = {_canonical(t) for t in types if t}
        wanted.discard(None)
        if not wanted:
            wanted = None

    results: List[Dict[str, Any]] = []
    for segment in segments:
        results.extend(_iter_matches(segment, wanted, min_confidence))
    return results


def extract_kv_pairs(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Surface key-value pairs from the processed output as clean records,
    regardless of where the engine stored them.

    Why this exists: the engine detects kv content and annotates each line with
    `kv_key` / `kv_value` (inside `context` sub-blocks), but does not always
    populate the clean `kv_block.pairs` structure — so a naive "read kv_block"
    would miss data that is present in the annotations. This function harvests
    kv pairs from BOTH places (annotated lines and any populated `pairs`), so a
    caller reliably gets every key-value pair the engine actually recognized.

    Returns a list of:
        {"key": <str>, "value": <str>, "segment_id": <id>, "line_index": <int|None>}
    in document order, de-duplicated on (key, value, line_index).
    """
    pairs: List[Dict[str, Any]] = []
    # De-duplicate on (segment_id, key, value): the same pair can appear both as
    # an annotated line (with a line_index) and via a pairs/other path (line
    # None). Keep the first occurrence but upgrade its line_index if a later
    # occurrence supplies one, so we retain the positional breadcrumb.
    index_by_sig: Dict[tuple, int] = {}

    def _add(key, value, seg_id, line_index):
        if not key:
            return
        sig = (seg_id, key, value)
        if sig in index_by_sig:
            # Already have this pair; fill in a line_index if we didn't have one.
            existing = pairs[index_by_sig[sig]]
            if existing["line_index"] is None and line_index is not None:
                existing["line_index"] = line_index
            return
        index_by_sig[sig] = len(pairs)
        pairs.append({
            "key":        key,
            "value":      value,
            "segment_id": seg_id,
            "line_index": line_index,
        })

    def _harvest(content, seg_id):
        if not isinstance(content, dict):
            return
        # Populated clean pairs, if present.
        for p in content.get("pairs", []) or []:
            if isinstance(p, dict):
                _add(p.get("key"), p.get("value"), seg_id, p.get("line_index"))
        # Annotated lines carrying kv_key/kv_value (the common case).
        for a in content.get("annotated", []) or []:
            if a.get("kv_key"):
                _add(a.get("kv_key"), a.get("kv_value"), seg_id, a.get("line_index"))
        # Recurse into nested sub_blocks.
        for sub in content.get("sub_blocks", []) or []:
            _harvest(sub.get("content", sub), seg_id)

    for segment in segments:
        _harvest(segment.get("content", {}), segment.get("segment_id"))

    return pairs


def available_types(segments: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Report which canonical types are present and how many of each — useful for
    a caller (or UI) to know what can be filtered before asking. Counts nested
    sub_blocks under their canonical type.
    """
    counts: Dict[str, int] = {}
    for match in filter_segments(segments, types=None, min_confidence=0.0):
        t = match["type"] or "unknown"
        counts[t] = counts.get(t, 0) + 1
    return counts
