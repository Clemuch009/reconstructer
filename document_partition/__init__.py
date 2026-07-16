# document_partition/__init__.py
#
# Document Partition — answers exactly one question:
#
#     "How many logical documents are present, and where are their boundaries?"
#
# This is a document problem, not a layout or extraction problem. It exists in
# every format: a PDF holding 500 invoices, a CSV of 20 purchase orders, a JSON
# array of invoices, an XML file of <Invoice> elements, an mbox archive. Rather
# than teach every parser and every detector about batching, one deterministic
# stage resolves boundaries up front.
#
# The invariant this preserves is the important part:
#
#     Every downstream component (segmentation, kv detection, table detection,
#     profiles) always receives ONE logical document. It never needs to know
#     whether the upload contained one invoice or one hundred.
#
# ── Format independence ──────────────────────────────────────────────────
# Parsers NEVER create regions. They emit standardized `PartitionSignal`s and
# decide nothing. This module consumes only signals, so boundary logic is
# written once and reused for every format Qrynt supports; format-specific
# heuristics stay isolated in their respective parsers.
#
# ── Scope (V1) ───────────────────────────────────────────────────────────
# Implemented deterministically:
#   ROOT_OBJECT      — a self-contained top-level object (JSON array member)
#   OBJECT_COMPLETE  — that object's end
# A source with no boundary signals yields exactly ONE region: a single
# document is simply a batch of one. No special-casing anywhere.
#
# Explicitly NOT implemented (named gaps, not silently mishandled):
#   • PDF boundary detection (page breaks, repeated headers/footers, entity
#     resets). Genuinely hard; a PDF currently yields one region.
#   • Region typing — `region_type` stays "unknown". Classifying a region as
#     an invoice is a different problem from partitioning.
#   • Region validation (has beginning / body / ending; continuation pages).

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ─────────────────────────────────────────────────────────────────────────
# Signal vocabulary
# ─────────────────────────────────────────────────────────────────────────
#
# Parsers emit these. They are evidence, not decisions. Strength is 0..1 and
# lets weak/ambiguous evidence (a blank separator) coexist with strong evidence
# (a JSON root object) without either parser needing to know the outcome.

ROOT_OBJECT       = "ROOT_OBJECT"        # self-contained top-level object begins
OBJECT_COMPLETE   = "OBJECT_COMPLETE"    # that object ends
PAGE_BREAK        = "PAGE_BREAK"         # (pdf) reserved — not yet emitted
HEADER_REPEAT     = "HEADER_REPEAT"      # (pdf/csv) reserved
FOOTER_REPEAT     = "FOOTER_REPEAT"      # (pdf) reserved
HEADER_ROW_REPEAT = "HEADER_ROW_REPEAT"  # (csv) reserved
ENTITY_RESET      = "ENTITY_RESET"       # (any) reserved
BLANK_SEPARATOR   = "BLANK_SEPARATOR"    # (text/csv) reserved
METADATA_RESET    = "METADATA_RESET"     # (any) reserved
SCHEMA_CHANGE     = "SCHEMA_CHANGE"      # (json/csv) reserved

# Signals that, on their own, are strong enough to open a new logical document.
_BOUNDARY_OPENING = {ROOT_OBJECT}

# Evidence at or above this strength is treated as a boundary.
BOUNDARY_THRESHOLD = 0.8


@dataclass
class PartitionSignal:
    """
    Standardized evidence emitted by a parser. Parsers decide nothing; they
    only report what they observed and how strongly.

    type     — one of the vocabulary constants above
    strength — 0..1 confidence that this observation indicates a boundary
    position — location in the source; interpretation is parser-defined
               (character offset for text, page index for PDF, item index for
               arrays). document_partition only uses it for ordering.
    source   — which parser produced it ("json_parser", "pdf_parser", ...)
    payload  — optional parser-specific detail, carried through untouched
    """
    type:     str
    strength: float
    position: int
    source:   str
    payload:  Dict[str, Any] = field(default_factory=dict)


@dataclass
class DocumentRegion:
    """
    One logical document. `content` is the extracted text/structure for this
    region — exactly what the normal pipeline expects as a whole document.

    region_type stays "unknown": partitioning answers *where*, not *what*.
    """
    id:          int
    content:     Any
    start:       int
    end:         Optional[int]
    confidence:  float
    reason:      List[str]
    source:      str
    region_type: str = "unknown"
    payload:     Dict[str, Any] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────
# Boundary resolution
# ─────────────────────────────────────────────────────────────────────────

def partition(
    signals: List[PartitionSignal],
    fallback_content: Any,
    source: str = "unknown",
) -> List[DocumentRegion]:
    """
    Resolve partition signals into logical document regions.

    Parameters
    ----------
    signals          : evidence from a parser, in any order.
    fallback_content : the whole source's content, used when no boundary
                       evidence exists (→ a single region).
    source           : which parser produced the signals.

    Returns
    -------
    A list of DocumentRegion. ALWAYS at least one: a source with no boundary
    evidence is a batch of one. Callers therefore never branch on
    "single vs multi" — they always iterate.

    Determinism: regions follow source order (by `position`); no scoring
    randomness, no heuristics beyond the declared threshold.
    """
    opening = [
        s for s in signals
        if s.type in _BOUNDARY_OPENING and s.strength >= BOUNDARY_THRESHOLD
    ]

    if not opening:
        # No boundary evidence → exactly one logical document.
        return [DocumentRegion(
            id=0,
            content=fallback_content,
            start=0,
            end=None,
            confidence=1.0,
            reason=["NO_BOUNDARY_EVIDENCE"],
            source=source,
        )]

    opening.sort(key=lambda s: s.position)

    regions: List[DocumentRegion] = []
    for i, sig in enumerate(opening):
        # A ROOT_OBJECT signal carries its own content in the payload: the
        # parser already isolated the object. Boundary logic stays format-free.
        content = sig.payload.get("content", fallback_content)
        regions.append(DocumentRegion(
            id=i,
            content=content,
            start=sig.position,
            end=(opening[i + 1].position if i + 1 < len(opening) else None),
            confidence=sig.strength,
            reason=[sig.type],
            source=sig.source,
            payload={k: v for k, v in sig.payload.items() if k != "content"},
        ))
    return regions
