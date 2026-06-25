from typing import List

# ---------------------------------
# Core segmentation
# ---------------------------------

def segment_text(text: str, split_positions: List[int]) -> List[str]:
    """
    Splits text into segments based on character positions.

    Assumptions:
    - split_positions are validated (sorted, unique, within bounds)
    - positions represent END of segment (exclusive split point)
    - a split position of 0 is meaningless (text[0:0] is empty) and
      is discarded during normalization rather than treated as an error —
      callers may legitimately compute a "split at start of text" marker
      (e.g. a heading on the very first line), which should simply be
      absorbed into the first segment, not raise.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not split_positions:
        return [text] if text else []

    # Defensive normalization
    # Drop non-positive positions (0 or negative) — a split at the very
    # start of the text produces an empty leading segment and is not
    # a meaningful boundary. This is what allows text that begins with
    # a structural marker line (e.g. "[PAGE: 1]", "[WORKBOOK]") to be
    # segmented normally instead of raising on the first iteration.
    splits = sorted(set(p for p in split_positions if p > 0))

    if not splits:
        return [text] if text and text.strip() else []

    segments: List[str] = []
    prev = 0
    for pos in splits:
        # 1. Sequence Violation
        if pos <= prev:
            raise ValueError(
                    f"Segmentation Error: Split position {pos} is not greater than "
                    f"previous position {prev}. Indices must be strictly increasing."
                    )

        # 2. Buffer Violation
        if pos > len(text):
            raise ValueError(
                    f"Segmentation Error: Split position {pos} exceeds "
                    f"text length {len(text)}."
                    )
        segment = text[prev:pos]
        #THIS BLOCK HAS BEEN ADDED DURING DEBUG!!
        # IMPROVED: Check if there's any actual word/number content
        # This kills separators (---), dots (....), and whitE
        if any(c.isalnum() for c in segment):
            segments.append(segment.strip())
        # Avoid zero-length or whitespace-only segments
        #if segment.strip():
        #    segments.append(segment)
        prev = pos
    # Final segment
    if prev < len(text):
        tail = text[prev:]
        if tail.strip():
            segments.append(tail.strip())
    return segments

# ---------------------------------
# Optional cleanup (minimal, safe)
# ---------------------------------

def clean_segments(segments: List[str]) -> List[str]:
    """
    Minimal cleanup:
    - strip leading/trailing whitespace
    - preserve internal formatting
    """
    cleaned = []
    for seg in segments:
        s = seg.strip()
        if s:
            cleaned.append(s)
    return cleaned

# ---------------------------------
# Full pipeline helper
# ---------------------------------

def reconstruct_paragraphs(text: str, split_positions: List[int]) -> List[str]:
    """
    Full segmentation pipeline:
    - segment
    - clean
    """
    segments = segment_text(text, split_positions)
    cleaned = clean_segments(segments)
    import sys
    print(f"[SEG] input {len(text.splitlines())} lines -> {len(cleaned)} segments; "
          f"sizes={[len(s.splitlines()) for s in cleaned]}", file=sys.stderr)
    return clean_segments(segments)
# ---------------------------------
