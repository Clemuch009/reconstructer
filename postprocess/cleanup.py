# postprocess/cleanup.py

import re
from typing import List, Dict, Any, Optional, Tuple
from postprocess.formatter import (
    StructuredSegment,
    StructuredDocument,
    PostprocessOutput,
)


# ---------------------------------
# Constants
# ---------------------------------

MAX_KEY_LENGTH = 128

NULL_REPRESENTATIONS = {"-", "n/a", "na", "nan", "none", "null", ""}

COSMETIC_ROW_RE = re.compile(r"^[\s\-=|:+*~^]{2,}$")

JUNK_KEY_RE = re.compile(r"^[\-=:+|*#~^.\s]+$")

STANDALONE_DEBRIS_RE = re.compile(
    r"^[\}\{\|\[\]]{1,3}$|^:{2,}$|^-{1,2}$|^={1,2}$"
)

CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

TREE_GLYPH_RE = re.compile(r"^[\|\+\-\\\/ \t]*(?:[└├─│+\-]+\s*)*")

# Compound bracket noise — [tag] [tag] prefixes
BRACKET_PREFIX_RE = re.compile(r"^(\[[^\]]{1,32}\]\s*)+")


# ---------------------------------
# Universal control char stripping
# ---------------------------------

def _strip_control_chars(text: str) -> str:
    """Runs universally across all segment types."""
    return CONTROL_CHAR_RE.sub("", text)


def _strip_compound_bracket_prefix(key: str) -> str:
    """
    Strip compound bracket noise from keys.
    [12:35:32] [INFO] [Thread-4] timeout_value → timeout_value
    """
    return BRACKET_PREFIX_RE.sub("", key).strip()


# ---------------------------------
# KV cleanup
# ---------------------------------

def _clean_kv_key(key: str) -> Optional[str]:
    """
    Clean and validate a KV key.
    Returns None if key should be discarded.
    No timestamp/severity stripping — those are detector failures flagged by validation.
    """
    if not key:
        return None

    # Strip compound bracket noise only
    key = _strip_compound_bracket_prefix(key)
    key = _strip_control_chars(key)

    if not key:
        return None

    # Reject pure structural junk
    if JUNK_KEY_RE.match(key):
        return None

    # Reject excessively long keys
    if len(key) > MAX_KEY_LENGTH:
        return None

    return key


def _is_cosmetic_table_row(cells: List[Optional[str]]) -> bool:
    if not cells:
        return True
    non_null = [c for c in cells if c is not None and str(c).strip()]
    if not non_null:
        return True
    return all(
        bool(COSMETIC_ROW_RE.match(str(c).strip())) if c else True
        for c in cells
    )


def _is_empty_row(cells: List[Optional[str]]) -> bool:
    return all(
        c is None or str(c).strip() == ""
        for c in cells
    )


def _normalize_null_cell(cell: Optional[str]) -> Optional[str]:
    if cell is None:
        return None
    if str(cell).strip().lower() in NULL_REPRESENTATIONS:
        return None
    stripped = str(cell).strip()
    return stripped if stripped else None


def _positional_split(line: str, col_starts: List[int]) -> List[Optional[str]]:
    """
    Positional-aware column splitting using column boundary coordinates.
    Preserves empty cells as None — no index shifting.
    """
    cells = []
    for i, start in enumerate(col_starts):
        end  = col_starts[i + 1] if i + 1 < len(col_starts) else len(line)
        cell = line[start:end].strip()
        cells.append(cell if cell else None)
    return cells


def _detect_col_starts(rows: List[List[Optional[str]]]) -> List[int]:
    """
    Derive column start positions from first valid row.
    Used as anchor for positional splitting.
    """
    if not rows:
        return []
    first = rows[0]
    # Fake positions — use uniform distribution as fallback
    return list(range(len(first)))


# ---------------------------------
# Mixed (Option C) cleanup
# ---------------------------------

def _mixed_cleanup(
    segment: StructuredSegment,
) -> Tuple[StructuredSegment, List[str]]:
    """
    Option C mixed cleanup.
    Mixed content is {dominant_type, sub_blocks:[{type,content}], gaps, confidence}.
    Strip control chars from gap text only; sub_blocks carry already-packaged
    detector output and are left structurally intact (their own detectors own
    their shape). NEVER flatten to raw_lines — that is what previously erased
    the entire segment.
    """
    flags   = []
    content = segment["content"]

    new_content = dict(content)

    gaps = content.get("gaps", [])
    if gaps:
        cleaned_gaps = []
        for g in gaps:
            txt = _strip_control_chars(g.get("text", "")).rstrip()
            cleaned_gaps.append(dict(g, text=txt))
        new_content["gaps"] = cleaned_gaps

    new_segment = StructuredSegment(
        segment_id=segment["segment_id"],
        type=segment["type"],
        content=new_content,
        metadata=segment["metadata"],
    )
    return new_segment, flags


# ---------------------------------
# Pass 1 — Type-aware local sanitization
# ---------------------------------

def _kv_cleanup(
    segment: StructuredSegment,
) -> Tuple[StructuredSegment, List[str]]:
    flags   = []
    content = segment["content"]
    pairs   = content.get("pairs", [])

    cleaned_pairs = []
    for pair in pairs:
        raw_key   = _strip_control_chars(pair.get("key", ""))
        raw_value = _strip_control_chars(pair.get("value", ""))
        delimiter = pair.get("delimiter", ":")

        clean_key = _clean_kv_key(raw_key)
        if clean_key is None:
            flags.append("kv_junk_key_removed")
            continue

        # Normalize delimiter spacing only
        clean_key   = clean_key.strip()
        clean_value = raw_value.strip()

        if clean_key != raw_key or clean_value != str(pair.get("value", "")):
            flags.append("kv_spacing_normalized")

        # Preserve any nested decomposition verbatim — do not drop it.
        cleaned_pair = {
            "key":       clean_key,
            "value":     clean_value,
            "delimiter": delimiter,
        }
        if "nested" in pair:
            cleaned_pair["nested"] = pair["nested"]
        cleaned_pairs.append(cleaned_pair)

    new_content          = dict(content)
    new_content["pairs"] = cleaned_pairs

    new_segment = StructuredSegment(
        segment_id=segment["segment_id"],
        type=segment["type"],
        content=new_content,
        metadata=segment["metadata"],
    )
    return new_segment, flags


def _table_cleanup(
    segment: StructuredSegment,
) -> Tuple[StructuredSegment, List[str]]:
    flags     = []
    content   = segment["content"]
    rows      = content.get("rows", [])
    alignment_ok = content.get("alignment_ok", True)

    # Derive col_count from the MODAL row length — robust to a leading artifact
    # row (e.g. a 1-cell label) that would otherwise force col_count=1 and make
    # every real row spuriously "mismatch".
    _lengths  = [len(r) for r in rows if r]
    col_count = max(set(_lengths), key=_lengths.count) if _lengths else 0

    cleaned_rows = []
    for row in rows:
        # Strip control chars from all cells universally
        row = [
            _strip_control_chars(str(c)) if c is not None else None
            for c in row
        ]

        # Drop cosmetic grid rows
        if _is_cosmetic_table_row(row):
            flags.append("cosmetic_row_removed")
            continue

        # Drop completely empty rows
        if _is_empty_row(row):
            flags.append("empty_row_removed")
            continue

        # Normalize null cells
        normalized = [_normalize_null_cell(c) for c in row]

        # Ragged row handling — only repair if alignment_ok
        if col_count > 0 and len(normalized) != col_count:
            if alignment_ok:
                # Pad short rows with None
                if len(normalized) < col_count:
                    normalized = normalized + [None] * (col_count - len(normalized))
                    flags.append("ragged_row_repaired")
                # Do NOT coalesce — leave overlong rows raw
            else:
                flags.append("ragged_row_detected")

        # Trim cell whitespace
        cleaned = [
            c.strip() if isinstance(c, str) else c
            for c in normalized
        ]
        cleaned_rows.append(cleaned)

    new_content              = dict(content)
    new_content["rows"]      = cleaned_rows
    new_content["row_count"] = len(cleaned_rows)
    new_content["col_count"] = col_count

    new_segment = StructuredSegment(
        segment_id=segment["segment_id"],
        type=segment["type"],
        content=new_content,
        metadata=segment["metadata"],
    )
    return new_segment, flags


def _hierarchy_cleanup(
    segment: StructuredSegment,
) -> Tuple[StructuredSegment, List[str]]:
    """
    Hierarchy cleanup.
    - Root-relative baseline alignment
    - NO step compression — flag depth jumps instead
    - Glyph decoupling for machine output
    - Universal control char stripping
    """
    flags   = []
    content = segment["content"]
    nodes   = content.get("nodes", [])

    if not nodes:
        return segment, flags

    # Root-relative baseline
    min_depth = min(n["depth"] for n in nodes)
    if min_depth > 0:
        nodes = [
            dict(n, depth=n["depth"] - min_depth)
            for n in nodes
        ]
        flags.append("hierarchy_baseline_normalized")

    # Flag depth jumps — do NOT compress
    final_nodes = []
    for i, node in enumerate(nodes):
        # Strip control chars from text universally
        raw_text = _strip_control_chars(node.get("text", ""))

        # Glyph decoupling — strip glyphs for normalized_text
        normalized_text = TREE_GLYPH_RE.sub("", raw_text).strip()

        # Detect depth jump
        if i > 0:
            prev_depth = final_nodes[-1]["depth"]
            curr_depth = node["depth"]
            if curr_depth > prev_depth + 1:
                flags.append("hierarchy_depth_jump_detected")

        final_nodes.append(dict(
            node,
            text=raw_text,
            normalized_text=normalized_text,
        ))

    new_content          = dict(content)
    new_content["nodes"] = final_nodes

    new_segment = StructuredSegment(
        segment_id=segment["segment_id"],
        type=segment["type"],
        content=new_content,
        metadata=segment["metadata"],
    )
    return new_segment, flags


def _prose_context_cleanup(
    segment: StructuredSegment,
) -> Tuple[StructuredSegment, List[str]]:
    flags   = []
    content = segment["content"]

    if segment["type"] == "prose":
        text  = content.get("text", "")
        lines = text.split("\n")
    else:
        lines = content.get("lines") or content.get("raw_lines", [])

    cleaned = []
    for line in lines:
        line = _strip_control_chars(line)
        line = line.rstrip()

        if STANDALONE_DEBRIS_RE.match(line.strip()):
            flags.append("debris_line_removed")
            continue

        cleaned.append(line)

    # Collapse excessive blank lines
    collapsed   = []
    blank_count = 0
    for line in cleaned:
        if line.strip() == "":
            blank_count += 1
            if blank_count <= 2:
                collapsed.append(line)
        else:
            blank_count = 0
            collapsed.append(line)

    new_content = dict(content)
    if segment["type"] == "prose":
        new_content["text"] = "\n".join(collapsed)
    else:
        new_content["lines"] = [l for l in collapsed if l.strip()]

    new_segment = StructuredSegment(
        segment_id=segment["segment_id"],
        type=segment["type"],
        content=new_content,
        metadata=segment["metadata"],
    )
    return new_segment, flags


# ---------------------------------
# Pass 1 dispatcher
# ---------------------------------

def _pass1_sanitize(
    segment: StructuredSegment,
) -> Tuple[StructuredSegment, List[str]]:
    stype = segment["type"]
    if stype == "kv_block":
        return _kv_cleanup(segment)
    elif stype == "table":
        return _table_cleanup(segment)
    elif stype == "hierarchy":
        return _hierarchy_cleanup(segment)
    elif stype == "mixed":
        return _mixed_cleanup(segment)
    else:
        return _prose_context_cleanup(segment)


# ---------------------------------
# Pass 2 — Global smoothing
# ---------------------------------

def _is_empty_segment(segment: StructuredSegment) -> bool:
    stype   = segment["type"]
    content = segment["content"]

    if stype == "kv_block":
        return len(content.get("pairs", [])) == 0
    elif stype == "table":
        return len(content.get("rows", [])) == 0
    elif stype == "hierarchy":
        return len(content.get("nodes", [])) == 0
    elif stype == "prose":
        return not content.get("text", "").strip()
    elif stype == "mixed":
        # Option C: mixed carries data in sub_blocks and gaps, not lines.
        # Empty only when it has neither sub_blocks nor gap lines.
        return (
            len(content.get("sub_blocks", [])) == 0 and
            len(content.get("gaps", [])) == 0
        )
    else:
        lines = content.get("lines") or content.get("raw_lines", [])
        return len([l for l in lines if l.strip()]) == 0


def _pass2_global_smooth(
    segments: List[StructuredSegment],
) -> Tuple[List[StructuredSegment], List[str]]:
    """
    Pass 2 — Drop empty segments only.
    Context absorption removed — masks vital information.
    """
    flags:     List[str]              = []
    non_empty: List[StructuredSegment] = []

    for seg in segments:
        if _is_empty_segment(seg):
            flags.append("empty_segment_pruned")
        else:
            non_empty.append(seg)

    return non_empty, flags


# ---------------------------------
# Core
# ---------------------------------

def cleanup(output: PostprocessOutput) -> PostprocessOutput:
    """
    Two-pass cleanup of PostprocessOutput.

    Pass 1 — Local type-aware sanitization per segment
    Pass 2 — Global smoothing (empty segment pruning only)

    Rules:
    - Never reclassify segments
    - Never infer missing data
    - Never mask upstream detector failures
    - Every mutation appended to segment flags
    - No timestamp/severity stripping (detector failures — flagged by validation)
    - No type casting (consumer adapter responsibility)
    - No step compression (flag depth jumps only)
    - No context absorption (masks vital information)
    - Control char stripping runs universally across all types
    - global_flags persisted into postprocess_metadata
    - validation slot remains None
    """
    doc      = output["machine_readable"]
    segments = doc["segments"]

    total_mutations = 0
    total_warnings  = 0

    # Pass 1
    pass1_results: List[StructuredSegment] = []
    for seg in segments:
        cleaned_seg, flags = _pass1_sanitize(seg)

        total_mutations += len(flags)

        new_meta             = dict(cleaned_seg["metadata"])
        existing_flags       = list(new_meta.get("flags", []))
        new_meta["flags"]    = existing_flags + flags

        pass1_results.append(StructuredSegment(
            segment_id=cleaned_seg["segment_id"],
            type=cleaned_seg["type"],
            content=cleaned_seg["content"],
            metadata=new_meta,
        ))

    # Pass 2
    pass2_results, global_flags = _pass2_global_smooth(pass1_results)
    total_warnings += len(global_flags)

    # Persist postprocess_metadata into each segment
    final_segments: List[StructuredSegment] = []
    for seg in pass2_results:
        new_meta = dict(seg["metadata"])
        new_meta["postprocess_metadata"] = {
            "global_flags":    global_flags,
            "cleanup_summary": {
                "mutations": total_mutations,
                "warnings":  total_warnings,
            },
        }
        final_segments.append(StructuredSegment(
            segment_id=seg["segment_id"],
            type=seg["type"],
            content=seg["content"],
            metadata=new_meta,
        ))

    # Rebuild document
    new_doc = StructuredDocument(segments=final_segments)

    # Re-derive human_readable from cleaned machine_readable
    from postprocess.formatter import _derive_human_readable
    new_human = _derive_human_readable(final_segments)

    return PostprocessOutput(
        region_type=output["region_type"],
        human_readable=new_human,
        machine_readable=new_doc,
        validation=None,
    )
