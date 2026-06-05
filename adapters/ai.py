# adapters/ai.py

from typing import List, Dict, Any, Optional
from postprocess.formatter import PostprocessOutput, StructuredSegment
from adapters.base import (
    BaseAdapter,
    LDMBlock,
    LDMDocument,
    _extract_section_label,
)


# ---------------------------------
# Tag mapping
# ---------------------------------

TYPE_TAGS: Dict[str, List[str]] = {
    "table":     ["TABLE", "STRUCTURED"],
    "kv_block":  ["KV", "STRUCTURED"],
    "hierarchy": ["HIERARCHY", "STRUCTURED"],
    "context":   ["CONTEXT", "LOG"],
    "prose":     ["PROSE", "UNSTRUCTURED"],
    "mixed":     ["MIXED", "STRUCTURED"],
}


# ---------------------------------
# Content extractors — read-only
# ---------------------------------

def _ldm_prose(content: dict) -> Dict[str, Any]:
    return {"text": content.get("text", "")}


def _ldm_context(content: dict) -> Dict[str, Any]:
    return {
        "lines":     content.get("lines", []),
        "annotated": content.get("annotated", []),
    }


def _ldm_table(content: dict) -> Dict[str, Any]:
    return {
        "headers":    content.get("headers"),
        "rows":       content.get("rows", []),
        "col_count":  content.get("col_count", 0),
        "row_count":  content.get("row_count", 0),
        "table_type": content.get("table_type", "unknown"),
    }


def _ldm_kv(content: dict) -> Dict[str, Any]:
    return {"pairs": content.get("pairs", [])}


def _ldm_hierarchy(content: dict) -> Dict[str, Any]:
    return {
        "nodes":      content.get("nodes", []),
        "root_count": content.get("root_count", 0),
        "max_depth":  content.get("max_depth", 0),
    }


def _ldm_mixed(content: dict) -> Dict[str, Any]:
    return {
        "dominant_type": content.get("dominant_type"),
        "sub_blocks":    content.get("sub_blocks", []),
        "gaps":          content.get("gaps", []),
        "confidence":    content.get("confidence"),
    }


CONTENT_EXTRACTORS = {
    "prose":     _ldm_prose,
    "context":   _ldm_context,
    "table":     _ldm_table,
    "kv_block":  _ldm_kv,
    "hierarchy": _ldm_hierarchy,
    "mixed":     _ldm_mixed,
}


# ---------------------------------
# AI adapter
# ---------------------------------

class AIAdapter(BaseAdapter):
    """
    Renders PostprocessOutput as a Logical Document Model (LDM).

    The LDM is a format-neutral, intent-preserving structured
    representation. No markdown, no XML, no display JSON.
    Rendering format decisions are left to downstream LLM consumers.

    Rules:
    - Read-only — never modifies PostprocessOutput
    - Format-neutral output
    - Type labels preserved as tags
    - Section labels preserved
    - Confidence attached as optional metadata
    - Validation summary appended only when is_valid: False
    - No decorative formatting, no visual padding
    """

    def __init__(self, include_confidence: bool = False):
        """
        include_confidence: if True, confidence scores are
        included in each block for downstream ranking.
        Default False to minimize token usage.
        """
        self.include_confidence = include_confidence

    def adapt(self, output: PostprocessOutput) -> LDMDocument:
        self._validate_input(output)

        segments = self._segments(output)
        blocks:  List[LDMBlock] = []

        for segment in segments:
            stype   = segment["type"]
            content = segment["content"]
            label   = _extract_section_label(segment)

            extractor    = CONTENT_EXTRACTORS.get(stype, _ldm_prose)
            ldm_content  = extractor(content)
            tags         = TYPE_TAGS.get(stype, ["UNKNOWN"])

            confidence = (
                segment["metadata"].get("confidence")
                if self.include_confidence
                else None
            )

            block = LDMBlock(
                type=stype,
                label=label,
                content=ldm_content,
                tags=tags,
                confidence=confidence,
            )
            blocks.append(block)

        # Validation summary — attached only when invalid
        is_valid   = self._is_valid(output)
        validation = None

        if not is_valid:
            v = self._validation(output)
            if v:
                validation = {
                    "is_valid":       v.get("is_valid"),
                    "quality_score":  v.get("quality_score"),
                    "document_flags": v.get("document_flags", []),
                    "anomalies":      v.get("anomalies", []),
                }

        return LDMDocument(
            blocks=blocks,
            is_valid=is_valid,
            validation=validation,
        )
