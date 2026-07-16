# document_partition/json_signals.py
#
# JSON partition-signal emitter.
#
# Per the partition architecture, a parser NEVER creates document regions. It
# observes the source and emits standardized `PartitionSignal`s. All boundary
# logic lives in document_partition, which consumes signals from any format.
#
# ── The discriminator: object independence, not JSON syntax ──────────────
# A JSON array does NOT imply a table, and a JSON object does NOT imply a kv
# block. Representation follows the *relationship between objects*, not the
# serialization format:
#
#   [{"name":"Alice","age":21}, {"name":"Bob","age":18}]
#       → same schema, flat, meaningless alone → ONE dataset (one document)
#
#   [{"invoice_number":..., "line_items":[...]}, {...}]
#       → each object is a complete standalone entity → SEPARATE documents
#
# The strongest deterministic signal for independence is NESTED STRUCTURE: an
# object holding its own sub-array/sub-object (e.g. `line_items`) cannot be a
# table row, because a table cell cannot contain a table. Heterogeneous key
# sets reinforce this.
#
# We deliberately do NOT attempt the semantic dimensions ("shared purpose",
# "document completeness") that would be needed to separate, say, a list of
# server configs from a list of invoices when both are flat and homogeneous.
# Those are not computable from structure, and guessing them would be
# dishonest. A flat homogeneous array is treated as ONE document containing a
# dataset — which the downstream table detector then handles.

import json
from typing import Any, Dict, List, Tuple

from document_partition import (
    PartitionSignal,
    ROOT_OBJECT,
    OBJECT_COMPLETE,
)


SOURCE = "json_parser"


def _has_nested_structure(obj: Dict[str, Any]) -> bool:
    """True if any value is itself a list or dict — the object carries its own
    sub-structure and therefore cannot be a single table row."""
    return any(isinstance(v, (list, dict)) for v in obj.values())


def _schemas_identical(objs: List[Dict[str, Any]]) -> bool:
    keysets = {frozenset(o.keys()) for o in objs if isinstance(o, dict)}
    return len(keysets) == 1


def _independence_evidence(objs: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    """
    Decide whether array members are independent documents. Returns
    (independent, reasons). Purely structural — no semantics.
    """
    reasons: List[str] = []
    dict_objs = [o for o in objs if isinstance(o, dict)]
    if not dict_objs:
        return False, ["NOT_OBJECT_ARRAY"]

    nested = [o for o in dict_objs if _has_nested_structure(o)]
    if len(nested) == len(dict_objs):
        reasons.append("ALL_OBJECTS_HAVE_NESTED_STRUCTURE")
    elif nested:
        reasons.append("SOME_OBJECTS_HAVE_NESTED_STRUCTURE")

    if not _schemas_identical(dict_objs):
        reasons.append("HETEROGENEOUS_SCHEMAS")

    # An object with its own sub-structure cannot be a table row.
    independent = bool(nested)
    if not independent:
        reasons.append("FLAT_HOMOGENEOUS_RECORDS")
    return independent, reasons


def _render(obj: Any) -> str:
    """Render one logical document back to text for the existing pipeline.

    Flattens a JSON object into key: value lines, which the structure engine
    already understands (kv lines, and nested arrays of objects as tables).
    This keeps the downstream pipeline completely unchanged.
    """
    lines: List[str] = []

    def emit_scalar(prefix: str, key: str, value: Any):
        label = f"{prefix}{key}"
        lines.append(f"{label}: {value}")

    def walk(o: Any, prefix: str = ""):
        if isinstance(o, dict):
            # scalars first — they read as the document's kv header
            for k, v in o.items():
                if not isinstance(v, (list, dict)):
                    emit_scalar(prefix, k, v)
            # then nested structures
            for k, v in o.items():
                if isinstance(v, dict):
                    lines.append("")
                    walk(v, prefix=f"{k}.")
                elif isinstance(v, list):
                    lines.append("")
                    _emit_list(k, v)
        else:
            lines.append(str(o))

    def _emit_list(name: str, items: List[Any]):
        dicts = [i for i in items if isinstance(i, dict)]
        if dicts and len(dicts) == len(items):
            # homogeneous-ish list of objects → render as a CSV-style table,
            # which the table detector recognizes.
            headers: List[str] = []
            for d in dicts:
                for k in d.keys():
                    if k not in headers:
                        headers.append(k)
            lines.append(",".join(headers))
            for d in dicts:
                lines.append(",".join(str(d.get(h, "")) for h in headers))
        else:
            for i in items:
                lines.append(str(i))

    walk(obj)
    return "\n".join(lines).strip()


def emit_signals(text: str) -> List[PartitionSignal]:
    """
    Observe JSON source text and emit partition signals. Emits nothing (an
    empty list) when the text is not JSON or contains no boundary evidence —
    document_partition then yields a single region.

    Each independent document produces a ROOT_OBJECT signal carrying that
    document's rendered content, plus an OBJECT_COMPLETE marker.
    """
    stripped = text.strip()
    if not stripped:
        return []
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError):
        return []          # not JSON — no signals, single region

    signals: List[PartitionSignal] = []

    # A top-level array MAY hold independent documents.
    if isinstance(data, list):
        independent, reasons = _independence_evidence(data)
        if not independent:
            # Flat homogeneous records → one document containing a dataset.
            return []
        for i, obj in enumerate(data):
            signals.append(PartitionSignal(
                type=ROOT_OBJECT,
                strength=1.0,
                position=i,
                source=SOURCE,
                payload={"content": _render(obj), "reasons": reasons},
            ))
            signals.append(PartitionSignal(
                type=OBJECT_COMPLETE,
                strength=1.0,
                position=i,
                source=SOURCE,
                payload={},
            ))
        return signals

    # A single top-level object is one document — no boundary evidence needed.
    return []


def render_single(text: str) -> str:
    """Render a non-partitioned JSON payload (single object, or a flat record
    array) into pipeline-friendly text. Returns the original text unchanged if
    it is not JSON."""
    stripped = text.strip()
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError):
        return text
    if isinstance(data, dict):
        return _render(data)
    if isinstance(data, list):
        dicts = [i for i in data if isinstance(i, dict)]
        if dicts and len(dicts) == len(data):
            headers: List[str] = []
            for d in dicts:
                for k in d.keys():
                    if k not in headers:
                        headers.append(k)
            rows = [",".join(headers)]
            for d in dicts:
                rows.append(",".join(str(d.get(h, "")) for h in headers))
            return "\n".join(rows)
    return text
