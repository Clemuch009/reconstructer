# adapters/human.py

from typing import List, Optional
from postprocess.formatter import PostprocessOutput, StructuredSegment
from adapters.base import BaseAdapter, _extract_section_label


# ---------------------------------
# Constants
# ---------------------------------

SECTION_DIVIDER   = "─" * 60
MAJOR_DIVIDER     = "═" * 60
KV_INDENT         = "    "
HIERARCHY_INDENT  = "  "
TABLE_COL_PAD     = 2


# ---------------------------------
# Type renderers
# ---------------------------------

def _render_prose(content: dict) -> str:
    return content.get("text", "").strip()


def _render_context(content: dict, drop_label: Optional[str] = None) -> str:
    """
    Render context lines. If drop_label is the first line (the line a section
    label was extracted from), omit that single occurrence so the label is not
    printed twice. Robust to leading/trailing whitespace differences.
    """
    lines = [l for l in content.get("lines", []) if l.strip()]
    if drop_label and lines and lines[0].strip() == drop_label.strip():
        lines = lines[1:]
    return "\n".join(lines)


def _render_table(content: dict) -> str:
    headers = content.get("headers") or []
    rows    = content.get("rows", [])

    if not rows:
        return ""

    all_rows = ([headers] + rows) if headers else rows

    # Compute per-column widths
    col_count = max(len(r) for r in all_rows)
    widths    = [0] * col_count

    for row in all_rows:
        for i, cell in enumerate(row):
            if i < col_count:
                widths[i] = max(widths[i], len(str(cell) if cell is not None else ""))

    def render_row(row: list) -> str:
        cells = []
        for i in range(col_count):
            cell = row[i] if i < len(row) else ""
            val  = str(cell) if cell is not None else ""
            cells.append(val.ljust(widths[i] + TABLE_COL_PAD))
        return "  ".join(cells).rstrip()

    lines = []
    if headers:
        lines.append(render_row(headers))
        lines.append("  ".join("─" * (w + TABLE_COL_PAD) for w in widths).rstrip())
    for row in rows:
        lines.append(render_row(row))

    return "\n".join(lines)


def _render_kv_pairs(pairs: list, indent: str = "") -> str:
    lines = []
    for pair in pairs:
        key       = pair.get("key", "")
        value     = pair.get("value", "")
        delimiter = pair.get("delimiter", ":")
        nested    = pair.get("nested")

        if nested and nested.get("pairs"):
            lines.append(f"{indent}{key}{delimiter} {value}")
            for sub in nested["pairs"]:
                sub_key = sub.get("key", "")
                sub_val = sub.get("value", "")
                lines.append(f"{indent}{KV_INDENT}{sub_key}: {sub_val}")
            for loose in nested.get("loose", []):
                lines.append(f"{indent}{KV_INDENT}- {loose}")
        else:
            lines.append(f"{indent}{key}{delimiter} {value}")

    return "\n".join(lines)


def _render_kv_block(content: dict) -> str:
    pairs = content.get("pairs", [])
    return _render_kv_pairs(pairs)


def _render_hierarchy(content: dict) -> str:
    nodes = content.get("nodes", [])
    lines = []
    for node in nodes:
        depth = node.get("depth", 0)
        text  = node.get("text", "").strip()
        lines.append(HIERARCHY_INDENT * depth + text)
    return "\n".join(lines)


def _render_mixed(content: dict, label: Optional[str]) -> str:
    """
    Render mixed segment.
    Show label if available.
    Render kv_block sub-blocks with expansion.
    Skip context sub-blocks to avoid duplication.
    """
    parts      = []
    sub_blocks = content.get("sub_blocks", [])

    if label:
        parts.append(label)

    for sub in sub_blocks:
        stype = sub.get("type")
        if stype == "kv_block":
            pairs = sub.get("content", {}).get("pairs", [])
            parts.append(_render_kv_pairs(pairs))

    return "\n".join(parts)


# ---------------------------------
# Segment renderer
# ---------------------------------

def _render_segment(segment: StructuredSegment) -> Optional[str]:
    """
    Render a single segment to human-readable string.
    Returns None if segment has no renderable content.
    """
    stype   = segment["type"]
    content = segment["content"]
    label   = _extract_section_label(segment)

    if stype == "prose":
        text = _render_prose(content)
        return text if text else None

    elif stype == "context":
        # If the label was extracted from this context's first line, drop that
        # line from the body so it is not rendered twice.
        text = _render_context(content, drop_label=label)
        if label:
            return f"{label}\n{text}" if text else label
        return text if text else None

    elif stype == "table":
        rendered = _render_table(content)
        if label:
            return f"{label}\n{rendered}" if rendered else label
        return rendered if rendered else None

    elif stype == "kv_block":
        rendered = _render_kv_block(content)
        if label:
            return f"{label}\n{rendered}" if rendered else label
        return rendered if rendered else None

    elif stype == "hierarchy":
        rendered = _render_hierarchy(content)
        if label:
            return f"{label}\n{rendered}" if rendered else label
        return rendered if rendered else None

    elif stype == "mixed":
        return _render_mixed(content, label) or None

    return None


# ---------------------------------
# Human adapter
# ---------------------------------

class HumanAdapter(BaseAdapter):
    """
    Renders PostprocessOutput as a formatted terminal/CLI string.

    Rules:
    - Read-only — never modifies PostprocessOutput
    - Preserves section labels
    - Section dividers between major structural segments
    - Fixed column width tables
    - Hierarchy with consistent indentation
    - KV pairs one per line, nested indented
    - Prose as clean paragraphs
    - No JSON, no metadata, no confidence scores
    - No validation internals
    """

    # Segment types that warrant a divider before them
    MAJOR_TYPES = {"table", "hierarchy", "kv_block"}

    def adapt(self, output: PostprocessOutput) -> str:
        self._validate_input(output)

        segments = self._segments(output)
        parts:   List[str] = []
        prev_type: Optional[str] = None

        for segment in segments:
            rendered = _render_segment(segment)
            if not rendered:
                continue

            stype = segment["type"]

            # Add divider before major structural transitions
            if (
                prev_type is not None and
                (stype in self.MAJOR_TYPES or prev_type in self.MAJOR_TYPES)
                and stype != prev_type
            ):
                parts.append(SECTION_DIVIDER)

            parts.append(rendered)
            prev_type = stype

        return "\n\n".join(parts)
