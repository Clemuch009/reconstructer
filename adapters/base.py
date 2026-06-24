# adapters/base.py

from typing import List, Dict, Any, Optional
from typing_extensions import TypedDict
from postprocess.formatter import PostprocessOutput, StructuredSegment


# ---------------------------------
# Read-only enforcement
# ---------------------------------

class AdapterViolation(Exception):
    """
    Raised when an adapter attempts to mutate extracted structure.
    Adapters may transform representation only.
    Adapters may not transform meaning.
    """
    pass


def _guard(output: PostprocessOutput) -> None:
    """
    Verify PostprocessOutput is not None and has required fields.
    Does not copy — adapters receive read-only reference.
    """
    if output is None:
        raise AdapterViolation("PostprocessOutput is None")
    if "machine_readable" not in output:
        raise AdapterViolation("PostprocessOutput missing machine_readable")
    if "segments" not in output["machine_readable"]:
        raise AdapterViolation("machine_readable missing segments")


# ---------------------------------
# Logical Document Model (LDM)
# ---------------------------------

class LDMBlock(TypedDict):
    type:       str              # kv | table | hierarchy | prose | context | mixed
    label:      Optional[str]    # section label if available
    content:    Dict[str, Any]   # type-specific, read-only reference
    tags:       List[str]        # structural type tags e.g. ["TABLE", "STRUCTURED"]
    confidence: Optional[float]  # optional, for downstream ranking


class LDMDocument(TypedDict):
    blocks:     List[LDMBlock]
    is_valid:   bool
    validation: Optional[Dict[str, Any]]  # attached only when is_valid: False


# ---------------------------------
# Shared label detection heuristic
# ---------------------------------

def _looks_like_label(text: str) -> bool:
    """
    A line is treated as a section label only when it actually looks like one:
    a short header, or a line ending in a colon. This prevents arbitrary
    content (e.g. a dropped data line surfaced as a gap) from being promoted
    to a label. Adapters surface detected labels — they never invent them.
    """
    t = text.strip()
    if not t:
        return False
    return t.endswith(":") or len(t) < 60


# ---------------------------------
# Shared label extraction
# ---------------------------------

def _extract_section_label(segment: StructuredSegment) -> Optional[str]:
    """
    Extract section label from segment content if available.

    Sources in priority order:
    1. gap_lines prose label (e.g. "BROKEN TABLE ZONE (pipes destroyed):")
    2. context annotated prose prefix lines
    3. mixed context sub-block first prose line

    Returns None if no label-like source is found.
    Adapters never generate labels — only surface what was detected, and only
    when the candidate line actually looks like a label.
    """
    stype   = segment["type"]
    content = segment["content"]

    # Gap lines may carry prose labels like section headers. Only promote a gap
    # line when it actually looks like a label (not an arbitrary dropped line),
    # and never a tree-glyph line.
    gap_lines = segment["metadata"].get("gap_lines", [])
    if gap_lines:
        first_gap = gap_lines[0].get("text", "").strip()
        if first_gap and not first_gap.startswith("+") and _looks_like_label(first_gap):
            return first_gap

    # Context annotated first line if prose-type and looks like a label
    if stype == "context":
        annotated = content.get("annotated", [])
        if annotated:
            first = annotated[0]
            text  = first.get("text", "").strip()
            ltype = first.get("line_type", "")
            if ltype == "prose" and _looks_like_label(text):
                return text

    # Mixed — check context sub-block first prose line
    if stype == "mixed":
        sub_blocks = content.get("sub_blocks", [])
        for sub in sub_blocks:
            if sub.get("type") == "context":
                annotated = sub.get("content", {}).get("annotated", [])
                if annotated:
                    first = annotated[0]
                    text  = first.get("text", "").strip()
                    ltype = first.get("line_type", "")
                    if ltype == "prose" and _looks_like_label(text):
                        return text

    return None


# ---------------------------------
# Shared classification-stats extraction
# ---------------------------------

def _extract_classification_stats(segment: StructuredSegment) -> Optional[Dict[str, Any]]:
    """
    Surface the segment's classification statistics (read-only).

    NOTE: this is NOT a line span. Top-level segments do not carry source line
    offsets; only the classifier's coverage stats are available. Consumers that
    need true line spans must have them threaded from the orchestrator upstream.
    """
    stats = segment["metadata"].get("classification_stats", {})
    if not stats:
        return None

    # Normalize internal classifier source names to user-friendly values
    raw_source = stats.get("dominant_source", "none")
    source_map = {
        "pre_classifier_bypass": "automatic",
        "structure_engine":      "table_detector",
        "block_engine":          "block_detector",
        "fallback_engine":       "fallback",
        "none":                  "none",
    }
    classified_by = source_map.get(raw_source, raw_source)

    return {
        "dominant_coverage_lines": stats.get("dominant_coverage_lines", 0),
        "block_count":             stats.get("block_count", 0),
        "classified_by":           classified_by,
    }


# ---------------------------------
# Base adapter class
# ---------------------------------

class BaseAdapter:
    """
    Base class for all output adapters.

    Enforces read-only contract:
    - Adapters may transform representation
    - Adapters may not transform meaning
    - No mutations to PostprocessOutput or its nested structures

    Subclasses implement adapt() only.
    """

    def adapt(self, output: PostprocessOutput) -> Any:
        raise NotImplementedError

    def _validate_input(self, output: PostprocessOutput) -> None:
        _guard(output)

    def _segments(self, output: PostprocessOutput) -> List[StructuredSegment]:
        return output["machine_readable"]["segments"]

    def _validation(self, output: PostprocessOutput) -> Optional[Dict[str, Any]]:
        return output.get("validation")

    def _is_valid(self, output: PostprocessOutput) -> bool:
        v = self._validation(output)
        if v is None:
            return True
        return v.get("is_valid", True)
