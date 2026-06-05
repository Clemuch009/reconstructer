# adapters/machine.py

from typing import List, Dict, Any, Optional
from postprocess.formatter import PostprocessOutput, StructuredSegment
from adapters.base import (
    BaseAdapter,
    _extract_section_label,
    _extract_classification_stats,
)


# ---------------------------------
# Type-specific content serializers
# ---------------------------------

def _machine_prose(content: dict) -> Dict[str, Any]:
    return {"text": content.get("text", "")}


def _machine_context(content: dict) -> Dict[str, Any]:
    return {
        "lines":     content.get("lines", []),
        "annotated": content.get("annotated", []),
    }


def _machine_table(content: dict) -> Dict[str, Any]:
    """
    Rows as array of dicts keyed by header when available.
    Falls back to indexed dicts when no headers.
    Row order is preserved (list), so this is a lossless representation.
    """
    headers   = content.get("headers") or []
    rows      = content.get("rows", [])
    col_count = content.get("col_count", 0)
    table_type = content.get("table_type", "unknown")

    if headers:
        keyed_rows = []
        for row in rows:
            record = {}
            for i, cell in enumerate(row):
                key = headers[i] if i < len(headers) else f"col_{i}"
                record[key] = cell
            keyed_rows.append(record)
    else:
        keyed_rows = []
        for row in rows:
            record = {f"col_{i}": cell for i, cell in enumerate(row)}
            keyed_rows.append(record)

    return {
        "headers":    headers,
        "rows":       keyed_rows,
        "col_count":  col_count,
        "row_count":  len(rows),
        "table_type": table_type,
    }


def _machine_kv(content: dict) -> Dict[str, Any]:
    """
    KV pairs as an ordered list of {key, value, delimiter, nested}.

    The ordered list is the canonical, LOSSLESS contract: it preserves order,
    duplicate keys, the delimiter, and the nested decomposition. A flattened
    key->value dict is intentionally NOT emitted, because duplicate keys (which
    the pipeline detects via DUPLICATE_KEYS_DETECTED) would silently collapse —
    a transformation of meaning, which adapters must not do.
    """
    return {"pairs": content.get("pairs", [])}


def _machine_hierarchy(content: dict) -> Dict[str, Any]:
    """
    Flat, depth-ordered node list (depth + child_count) for graph traversal.
    Not re-nested: rebuilding a child tree from depths is reconstruction of
    structure, which adapters must not do.
    """
    return {
        "nodes":      content.get("nodes", []),
        "root_count": content.get("root_count", 0),
        "max_depth":  content.get("max_depth", 0),
    }


def _machine_mixed(content: dict) -> Dict[str, Any]:
    return {
        "dominant_type": content.get("dominant_type"),
        "sub_blocks":    content.get("sub_blocks", []),
        "gaps":          content.get("gaps", []),
        "confidence":    content.get("confidence"),
    }


CONTENT_SERIALIZERS = {
    "prose":     _machine_prose,
    "context":   _machine_context,
    "table":     _machine_table,
    "kv_block":  _machine_kv,
    "hierarchy": _machine_hierarchy,
    "mixed":     _machine_mixed,
}


# ---------------------------------
# Machine adapter
# ---------------------------------

class MachineAdapter(BaseAdapter):
    """
    Renders PostprocessOutput as List[Dict] for analytics/DB ingestion.

    Rules:
    - Read-only — never modifies PostprocessOutput
    - One record per segment
    - Type-specific content schema
    - No presentation formatting
    - No CLI formatting
    - No markdown
    - Confidence always present
    - Flags always present
    - Section label where available
    - source_span: None (true line spans are not threaded from the pipeline);
      classifier coverage stats surfaced separately under classification_stats
    """

    def adapt(self, output: PostprocessOutput) -> List[Dict[str, Any]]:
        self._validate_input(output)

        segments = self._segments(output)
        records: List[Dict[str, Any]] = []

        for segment in segments:
            stype   = segment["type"]
            content = segment["content"]
            meta    = segment["metadata"]

            serializer  = CONTENT_SERIALIZERS.get(stype, _machine_prose)
            typed_content = serializer(content)

            section_label       = _extract_section_label(segment)
            classification_stats = _extract_classification_stats(segment)

            record: Dict[str, Any] = {
                "segment_id":           segment["segment_id"],
                "type":                 stype,
                "content":              typed_content,
                "confidence":           meta.get("confidence", 0.0),
                "flags":                meta.get("flags", []),
                "section_label":        section_label,
                "source_span":          None,
                "classification_stats": classification_stats,
            }

            records.append(record)

        return records
