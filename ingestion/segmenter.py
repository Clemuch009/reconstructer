# ingestion/segmenter.py

import re
import hashlib
from typing import List
from ingestion.segment import Segment, SegmentType


# ---------------------------------
# Pass 1 — Line scanner constants
# ---------------------------------

_MIN_SEGMENT_LINES  = 1     # minimum lines to emit a segment
_MERGE_GAP_LINES    = 2     # blank lines before forcing segment boundary
_SAMPLE_SIZE        = 5     # lines to sample per candidate segment


# ---------------------------------
# Pass 1 — Line-level type signals
# ---------------------------------

def _line_signal(line: str) -> SegmentType:
    """
    Assign provisional type to a single line.
    Returns dominant signal — not final classification.

    Priority order:
    html > csv > json > text
    """
    stripped = line.strip()
    if not stripped:
        return "text"

    # HTML signal — structural tags only
    if re.search(
        r"</[a-zA-Z]+>|"
        r"<(html|head|body|div|table|tr|td|th|p|ul|ol|li|form|section)"
        r"[\s>]|<!DOCTYPE",
        stripped, re.IGNORECASE
    ):
        return "html"

    # JSON signal
    if re.match(
        r'^[{\[]|^[}\]]|^"[^"]+"\s*:|^\s*"[^"]+"\s*:\s*["{{\[\d]',
        stripped
    ):
        return "json"

    # CSV signal — delimiter + multiple fields + no structural chars
    if not stripped.startswith(("<", "{", "[")):
        for delim in (",", ";", "\t", "|"):
            parts = stripped.split(delim)
            if len(parts) >= 2 and any(p.strip() for p in parts):
                return "csv"

    return "text"


# ---------------------------------
# Pass 1 — segment builder
# ---------------------------------

def _build_segments_pass1(lines: list[str]) -> list[dict]:
    """
    Scan lines, assign provisional types, emit segment boundaries
    on type transitions or blank line gaps.

    Returns list of raw segment dicts — not yet validated.
    Each dict: {lines, type, start_line, end_line}
    """
    raw_segments: list[dict] = []
    current_lines:  list[str] = []
    current_type:   SegmentType = "text"
    current_start:  int = 1
    blank_count:    int = 0

    def emit(end_line: int) -> None:
        nonlocal current_lines, current_type, current_start
        content = "\n".join(current_lines).strip()
        if content:
            raw_segments.append({
                "lines":      current_lines[:],
                "type":       current_type,
                "start_line": current_start,
                "end_line":   end_line,
            })
        current_lines = []

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()

        # Blank line handling
        if not stripped:
            blank_count += 1
            if blank_count >= _MERGE_GAP_LINES and current_lines:
                emit(idx - 1)
                current_start = idx + 1
                current_type  = "text"
            else:
                current_lines.append(line)
            continue

        blank_count = 0
        line_type   = _line_signal(line)

        # Type transition — emit current segment, start new
        if current_lines and line_type != current_type:
            emit(idx - 1)
            current_start = idx
            current_type  = line_type

        if not current_lines:
            current_type  = line_type
            current_start = idx

        current_lines.append(line)

    # Emit final segment
    if current_lines:
        emit(len(lines))

    return raw_segments


# ---------------------------------
# Pass 2 — refinement
# ---------------------------------

def _compute_confidence(
    lines: list[str],
    segment_type: SegmentType,
) -> tuple[float, dict[str, float], list[str]]:
    """
    Compute confidence score and breakdown for a segment.

    Scores each candidate type against the segment lines.
    Normalizes to sum to 1.0.
    Returns (confidence, breakdown, source_signals).
    """
    signals: list[str] = []
    sample  = [l for l in lines if l.strip()][:_SAMPLE_SIZE]
    total   = len(sample) if sample else 1

    html_hits = sum(
        1 for l in sample if re.search(
            r"</[a-zA-Z]+>|<(html|body|div|table|tr|td|p)[\s>]|<!DOCTYPE",
            l, re.IGNORECASE
        )
    )
    json_hits = sum(
        1 for l in sample
        if re.match(r'^[{\[]|^[}\]]|^"[^"]+"\s*:', l.strip())
    )
    csv_hits = 0
    for l in sample:
        s = l.strip()
        if s and not s.startswith(("<", "{", "[")):
            for delim in (",", ";", "\t", "|"):
                if len(s.split(delim)) >= 2:
                    csv_hits += 1
                    break

    scores: dict[str, float] = {
        "html": html_hits / total,
        "json": json_hits / total,
        "csv":  csv_hits  / total,
        "text": max(0.0, 1.0 - (html_hits + json_hits + csv_hits) / total),
    }

    # Normalize
    total_score = sum(scores.values())
    if total_score > 0:
        scores = {k: round(v / total_score, 3) for k, v in scores.items()}

    confidence = scores.get(segment_type, 0.0)
    signals.append(
        f"type={segment_type} confidence={confidence:.3f} "
        f"html={scores['html']:.2f} csv={scores['csv']:.2f} "
        f"json={scores['json']:.2f} text={scores['text']:.2f}"
    )

    return confidence, scores, signals


def _should_reclassify(
    segment_type: SegmentType,
    breakdown: dict[str, float],
    confidence: float,
) -> tuple[bool, SegmentType]:
    """
    Decide if segment should be reclassified.

    Rules:
    - Low confidence (< 0.4) → reclassify to dominant type
    - If dominant type matches current → keep
    - Otherwise → return new type
    """
    if confidence >= 0.4:
        return False, segment_type

    dominant = max(breakdown, key=lambda k: breakdown[k])
    if dominant == segment_type:
        return False, segment_type

    return True, dominant  # type: ignore


def _check_embedded_structures(
    content: str,
    segment_type: SegmentType,
) -> tuple[bool, list[str]]:
    """
    Detect embedded structures inside a segment.

    Examples:
    - CSV inside HTML block → flag for further splitting
    - JSON inside text block → flag for reclassification

    Returns (has_embedded, warnings).
    Does NOT split — flags only. Splitting is router responsibility.
    """
    warnings: list[str] = []

    if segment_type == "html":
        # Check for CSV inside HTML
        lines = content.splitlines()
        csv_lines = sum(
            1 for l in lines
            if not l.strip().startswith("<")
            and len(l.split(",")) >= 3
        )
        if csv_lines / max(len(lines), 1) > 0.3:
            warnings.append(
                "Embedded CSV-like content detected inside HTML segment — "
                "consider further segmentation"
            )
            return True, warnings

    if segment_type == "text":
        # Check for JSON inside text
        lines = content.splitlines()
        json_lines = sum(
            1 for l in lines
            if re.match(r'^[{\[]|^"[^"]+"\s*:', l.strip())
        )
        if json_lines / max(len(lines), 1) > 0.3:
            warnings.append(
                "Embedded JSON content detected inside text segment — "
                "consider reclassification to json"
            )
            return True, warnings

    return False, warnings


def _merge_adjacent(raw_segments: list[dict]) -> list[dict]:
    """
    Merge adjacent segments of the same type.
    Prevents over-segmentation from single-line type transitions.

    Rule: merge only if both segments have same type
    and gap between them is 0 lines (adjacent).
    """
    if not raw_segments:
        return []

    merged: list[dict] = [raw_segments[0]]

    for seg in raw_segments[1:]:
        prev = merged[-1]
        if (
            seg["type"] == prev["type"]
            and seg["start_line"] <= prev["end_line"] + 2
        ):
            # Merge — extend previous
            prev["lines"].extend(seg["lines"])
            prev["end_line"] = seg["end_line"]
        else:
            merged.append(seg)

    return merged


# ---------------------------------
# Hash computation
# ---------------------------------

def _compute_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


# ---------------------------------
# Main segmenter
# ---------------------------------

def segment(text: str) -> List[Segment]:
    """
    Segment text into typed blocks using two-pass approach.

    Pass 1 — Line scanner:
        Scan line-by-line, assign provisional type per line.
        Emit segment boundary on type transition or blank gap.

    Pass 2 — Refinement:
        Compute confidence per segment.
        Reclassify low-confidence segments.
        Detect embedded structures.
        Merge adjacent same-type segments.

    Rules:
    - Never raises — empty input returns empty list
    - pass_assigned reflects which pass made the final type decision
    - "mixed" emitted when confidence is low and no dominant type
    - Embedded structures flagged in warnings — not split here
    - Splitting responsibility belongs to segment_router
    """
    if not text.strip():
        return []

    lines = text.splitlines()

    # ---------------------------------
    # Pass 1 — line scanner
    # ---------------------------------
    raw_segments = _build_segments_pass1(lines)

    if not raw_segments:
        return []

    # Merge adjacent same-type segments before refinement
    raw_segments = _merge_adjacent(raw_segments)

    # ---------------------------------
    # Pass 2 — refinement
    # ---------------------------------
    segments: List[Segment] = []

    for raw in raw_segments:
        seg_lines    = raw["lines"]
        seg_type     = raw["type"]
        start_line   = raw["start_line"]
        end_line     = raw["end_line"]
        content      = "\n".join(seg_lines).strip()
        seg_warnings: list[str] = []

        if not content:
            continue

        # Compute confidence
        confidence, breakdown, conf_signals = _compute_confidence(
            seg_lines, seg_type
        )

        # Reclassify if confidence is low
        reclassified, new_type = _should_reclassify(
            seg_type, breakdown, confidence
        )

        pass_assigned: str = "pass1"

        if reclassified:
            if new_type == seg_type:
                pass_assigned = "pass1"
            else:
                seg_warnings.append(
                    f"Reclassified from {seg_type} → {new_type} "
                    f"(confidence was {confidence:.3f})"
                )
                seg_type      = new_type  # type: ignore
                confidence    = breakdown.get(new_type, 0.0)
                pass_assigned = "pass2"

        # Handle low confidence after reclassification
        if confidence < 0.4:
            seg_warnings.append(
                f"Low confidence ({confidence:.3f}) — "
                "segment type uncertain, marked as mixed"
            )
            seg_type      = "mixed"
            pass_assigned = "pass2"

        # Check for embedded structures
        has_embedded, embed_warnings = _check_embedded_structures(
            content, seg_type  # type: ignore
        )
        seg_warnings.extend(embed_warnings)

        segments.append(Segment(
            content=content,
            segment_type=seg_type,  # type: ignore
            start_line=start_line,
            end_line=end_line,
            confidence=confidence,
            confidence_breakdown=breakdown,
            source_signals=conf_signals,
            warnings=seg_warnings,
            hash=_compute_hash(content),
            pass_assigned=pass_assigned,  # type: ignore
        ))

    return segments
