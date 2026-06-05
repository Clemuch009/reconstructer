import re
import hashlib
from typing import List, Dict, Any, Optional, Tuple
from typing_extensions import TypedDict
from analysis.structure_engine.classifier import ClassificationResult
from analysis.structure_engine.builder.block_builder import StructuredBlock
from analysis.structure_engine.line_model import LineObject


# ---------------------------------
# Contracts
# ---------------------------------

class StructuredSegment(TypedDict):
    segment_id: str
    type:       str
    content:    Dict[str, Any]
    metadata:   Dict[str, Any]


class StructuredDocument(TypedDict):
    segments: List[StructuredSegment]


class PostprocessOutput(TypedDict):
    region_type:      str
    human_readable:   str
    machine_readable: StructuredDocument
    validation:       None


# ---------------------------------
# Constants
# ---------------------------------

SOURCE_TO_LABEL: Dict[str, str] = {
    "table":          "table",
    "kv":             "kv_block",
    "tree_structure": "hierarchy",
    "context":        "context",
}

# Pure separator patterns — only match lines with NO alphanumeric content
PURE_SEPARATOR_RE = re.compile(r"^[\s\-=_+|:*#~^]{3,}$")


# ---------------------------------
# Separator detection — hardened
# ---------------------------------

def _is_pure_separator(line: str) -> bool:
    """Only strips lines that contain ZERO alphanumeric characters."""
    stripped = line.strip()
    if not stripped:
        return False
    if any(c.isalnum() for c in stripped):
        return False
    return bool(PURE_SEPARATOR_RE.match(stripped))


def _strip_separators(text: str) -> str:
    lines = text.split("\n")
    kept  = [l for l in lines if not _is_pure_separator(l)]
    return "\n".join(kept).strip()


# ---------------------------------
# Deterministic segment ID
# ---------------------------------

def _segment_id(structure_type: str, text: str) -> str:
    raw = f"{structure_type}:{text}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------
# Line helpers
# ---------------------------------

def _line_text(line: LineObject) -> str:
    return line.get("text") or line.get("normalized") or ""


# ---------------------------------
# Block-based per-type packagers
# (read pre-computed detector output — NO re-parsing)
# ---------------------------------

def _blocks_of(blocks: List[StructuredBlock], source: str) -> List[StructuredBlock]:
    return [b for b in blocks if b["source"] == source]


def _pkg_kv(blocks: List[StructuredBlock]) -> Dict[str, Any]:
    """Read KVResult list verbatim, surfacing the nested decomposition."""
    pairs: List[Dict[str, Any]] = []
    for b in _blocks_of(blocks, "kv"):
        for r in b["content"].get("kv_results", []):
            pairs.append({
                "key":       r["key"],
                "value":     r["value"],
                "delimiter": r["delimiter"],
                "nested":    r.get("nested"),   # decomposed sub-pairs or None
            })
    return {"pairs": pairs}


def _pkg_table(blocks: List[StructuredBlock]) -> Dict[str, Any]:
    tb = _blocks_of(blocks, "table")
    if not tb:
        return {"rows": [], "headers": None, "col_count": 0, "row_count": 0}
    c = tb[0]["content"]
    return {
        "rows":       c.get("rows", []),
        "headers":    c.get("headers"),
        "col_count":  c.get("col_count", 0),
        "row_count":  c.get("row_count", 0),
        "table_type": c.get("table_type"),
    }


def _pkg_hierarchy(blocks: List[StructuredBlock]) -> Dict[str, Any]:
    """
    Traverse the tree block's roots into a flat depth-ordered node list.
    Reads the real node text/depth (fixes the empty-text re-parse bug).
    NOTE: node field names follow the tree_builder TreeResult contract;
    text is read defensively (text -> normalized_text -> normalized).
    """
    tb = _blocks_of(blocks, "tree_structure")
    if not tb:
        return {"nodes": [], "root_count": 0, "max_depth": 0}

    roots = tb[0]["content"].get("roots", [])
    nodes: List[Dict[str, Any]] = []

    def walk(n: Dict[str, Any]) -> None:
        text = n.get("text") or n.get("normalized_text") or n.get("normalized") or ""
        nodes.append({
            "depth":       n.get("depth", 0),
            "text":        text,
            "line_index":  n.get("line_index"),
            "child_count": len(n.get("children", [])),
        })
        for child in n.get("children", []):
            walk(child)

    for r in roots:
        walk(r)

    max_depth = max((nd["depth"] for nd in nodes), default=0)
    return {"nodes": nodes, "root_count": len(roots), "max_depth": max_depth}


def _pkg_context(blocks: List[StructuredBlock]) -> Dict[str, Any]:
    cb = _blocks_of(blocks, "context")
    if not cb:
        return {"lines": [], "annotated": []}
    c = cb[0]["content"]
    annotated = c.get("annotated_lines", [])
    return {
        "lines":     [a.get("text", "") for a in annotated],
        "annotated": annotated,
    }


# ---------------------------------
# Gap detection (line conservation — decision 3)
# ---------------------------------

def _detect_gaps(
    line_set: List[LineObject],
    blocks:   List[StructuredBlock],
) -> List[Dict[str, Any]]:
    """
    Find non-empty lines in the segment that no block claims.
    Coverage = union of [start_line, end_line] over all blocks.
    Uncovered lines are returned for explicit prose fallback + flagging,
    so no physical line silently vanishes from the output.
    """
    covered = set()
    for b in blocks:
        for idx in range(b["start_line"], b["end_line"] + 1):
            covered.add(idx)

    gaps = []
    for l in line_set:
        if l["is_empty"]:
            continue
        if l["line_index"] not in covered:
            gaps.append({"line_index": l["line_index"], "text": _line_text(l)})
    return gaps


# ---------------------------------
# Mixed packaging — Option C
# (dominant_type + explicit self-describing sub_blocks)
# ---------------------------------

def _pkg_one_block(b: StructuredBlock) -> Dict[str, Any]:
    src = b["source"]
    if src == "kv":
        content = _pkg_kv([b])
    elif src == "table":
        content = _pkg_table([b])
    elif src == "tree_structure":
        content = _pkg_hierarchy([b])
    elif src == "context":
        content = _pkg_context([b])
    else:
        content = {}
    return {
        "type":       SOURCE_TO_LABEL.get(src, src),
        "source":     src,
        "start_line": b["start_line"],
        "end_line":   b["end_line"],
        "confidence": b["confidence"],
        "content":    content,
    }


def _package_mixed(
    blocks:         List[StructuredBlock],
    gaps:           List[Dict[str, Any]],
    classification: ClassificationResult,
) -> Dict[str, Any]:
    evidence = classification.get("evidence", {})
    dominant_type = max(evidence, key=evidence.get) if evidence else "none"

    return {
        "dominant_type": dominant_type,
        "sub_blocks":    [_pkg_one_block(b) for b in blocks],
        "gaps":          gaps,
        "confidence":    classification["classification_confidence"],
    }


# ---------------------------------
# Block-based content router
# ---------------------------------

def _package_from_blocks(
    structure_type: str,
    blocks:         List[StructuredBlock],
    line_set:       List[LineObject],
    classification: ClassificationResult,
    text:           str,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    gaps = _detect_gaps(line_set, blocks)

    if structure_type == "prose":
        content = {"text": text}
    elif structure_type == "kv_block":
        content = _pkg_kv(blocks)
    elif structure_type == "table":
        content = _pkg_table(blocks)
    elif structure_type == "hierarchy":
        content = _pkg_hierarchy(blocks)
    elif structure_type == "context":
        content = _pkg_context(blocks)
    elif structure_type == "mixed":
        content = _package_mixed(blocks, gaps, classification)
    else:
        content = {"text": text}

    return content, gaps


# ---------------------------------
# Text-based fallback packagers (used when no blocks are threaded in)
# ---------------------------------

def _package_prose_text(text: str) -> Dict[str, Any]:
    return {"text": text}


def _package_content_text(
    text:           str,
    structure_type: str,
    classification: ClassificationResult,
) -> Dict[str, Any]:
    # Minimal fallback: preserve raw text content by type so nothing breaks
    # when the caller has not yet been wired to pass blocks.
    lines = [l for l in text.split("\n") if l.strip()]
    if structure_type == "prose":
        return {"text": text}
    if structure_type == "mixed":
        return {
            "dominant_type": "none",
            "sub_blocks":    [],
            "gaps":          [{"line_index": None, "text": l} for l in lines],
            "confidence":    classification["classification_confidence"],
        }
    return {"lines": lines}


# ---------------------------------
# Human-readable derivation (from machine_readable only)
# ---------------------------------

def _hr_kv(content: Dict[str, Any]) -> str:
    out = []
    for p in content.get("pairs", []):
        out.append(f"{p['key']}{p['delimiter']} {p['value']}")
        nested = p.get("nested")
        if nested:
            for sp in nested.get("pairs", []):
                out.append(f"    {sp['key']}: {sp['value']}")
            for loose in nested.get("loose", []):
                out.append(f"    - {loose}")
    return "\n".join(out)


def _hr_table(content: Dict[str, Any]) -> str:
    rows = content.get("rows", [])
    out = []
    for row in rows:
        cells = [str(c) if c is not None else "null" for c in row]
        out.append("  ".join(cells))
    return "\n".join(out)


def _hr_hierarchy(content: Dict[str, Any]) -> str:
    return "\n".join(
        "  " * n.get("depth", 0) + n.get("text", "")
        for n in content.get("nodes", [])
    )


def _hr_context(content: Dict[str, Any]) -> str:
    return "\n".join(content.get("lines", []))


def _hr_mixed(content: Dict[str, Any]) -> str:
    parts = []
    for sb in content.get("sub_blocks", []):
        t = sb["type"]
        c = sb["content"]
        if t == "kv_block":
            parts.append(_hr_kv(c))
        elif t == "table":
            parts.append(_hr_table(c))
        elif t == "hierarchy":
            parts.append(_hr_hierarchy(c))
        elif t == "context":
            parts.append(_hr_context(c))
    for g in content.get("gaps", []):
        parts.append(g["text"])
    return "\n".join(p for p in parts if p)


def _derive_human_readable(segments: List[StructuredSegment]) -> str:
    parts = []
    for seg in segments:
        stype   = seg["type"]
        content = seg["content"]

        if stype == "prose":
            parts.append(content.get("text", ""))
        elif stype == "kv_block":
            parts.append(_hr_kv(content))
        elif stype == "table":
            parts.append(_hr_table(content))
        elif stype == "hierarchy":
            parts.append(_hr_hierarchy(content))
        elif stype == "context":
            parts.append(_hr_context(content))
        elif stype == "mixed":
            parts.append(_hr_mixed(content))
        else:
            parts.append(content.get("text", ""))

    return "\n\n".join(p for p in parts if p.strip())


# ---------------------------------
# Core
# ---------------------------------

def format_segments(
    segments:        List[str],
    classifications: List[ClassificationResult],
    block_sets:      Optional[List[List[StructuredBlock]]] = None,
    line_sets:       Optional[List[List[LineObject]]]      = None,
) -> PostprocessOutput:
    """
    Package classified segments into PostprocessOutput.

    Block-based path (block_sets and line_sets supplied):
    - Packages PRE-COMPUTED detector output; no re-parsing of raw text
    - mixed → Option C (dominant_type + sub_blocks + gaps)
    - Gap detection → uncovered lines flagged + preserved (line conservation)
    - NO merge step (1a-strict): one sub-segment → one output segment

    Text fallback path (block_sets omitted):
    - Preserves prior behavior so callers not yet wired do not break
    """
    if len(segments) != len(classifications):
        raise ValueError(
            f"Segment/classification count mismatch: "
            f"{len(segments)} vs {len(classifications)}"
        )

    use_blocks = block_sets is not None and line_sets is not None
    if use_blocks and (len(block_sets) != len(segments) or len(line_sets) != len(segments)):
        raise ValueError("block_sets / line_sets length must match segments")

    structured_segments: List[StructuredSegment] = []

    for i in range(len(segments)):
        raw_text       = segments[i]
        classification = classifications[i]
        structure_type = classification["structure_type"]

        text  = _strip_separators(raw_text)
        flags: List[str] = []

        # Per-segment decision: use the block path only when THIS segment has
        # blocks + lines. Bypass segments (PASS_THROUGH / JSON_NATIVE) arrive
        # with empty block/line sets and correctly fall back to the text path,
        # even within a document where other segments use the block path.
        seg_blocks = block_sets[i] if block_sets is not None else None
        seg_lines  = line_sets[i]  if line_sets  is not None else None

        if seg_blocks and seg_lines:
            content, gaps = _package_from_blocks(
                structure_type, seg_blocks, seg_lines, classification, text
            )
            if gaps:
                flags.append("line_gap_prose_fallback")
        else:
            content = _package_content_text(text, structure_type, classification)
            gaps    = []

        seg_id = _segment_id(structure_type, text)

        structured_segments.append(StructuredSegment(
            segment_id=seg_id,
            type=structure_type,
            content=content,
            metadata={
                "confidence":           classification["classification_confidence"],
                "classification_stats": {
                    "block_count":             classification.get("block_count", 0),
                    "dominant_coverage_lines": classification.get("dominant_coverage_lines", 0),
                    "dominant_source":         classification.get("dominant_source", "none"),
                    "type_entropy":            classification.get("type_entropy", 0.0),
                },
                "gap_lines": gaps,
                "flags":     flags,
            },
        ))

    machine_readable = StructuredDocument(segments=structured_segments)
    human_readable   = _derive_human_readable(structured_segments)

    return PostprocessOutput(
        region_type="classified_document",
        human_readable=human_readable,
        machine_readable=machine_readable,
        validation=None,
    )
