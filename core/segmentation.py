# core/segmentation.py
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
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not split_positions:
        return [text] if text else []
    # Defensive normalization
    splits = sorted(set(split_positions))
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
    return clean_segments(segments)
# ---------------------------------
# INTERACTIVE TEST
# ---------------------------------
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("SEGMENTATION TEST")
    print("=" * 60)
    print("Enter text (type 'exit' to quit)\n")
    while True:
        user_input = input("TEXT> ")
        if user_input.lower() == "exit":
            print("Exiting.")
            break
        raw_splits = input("SPLITS (comma-separated indices)> ")
        try:
            splits = [int(x.strip()) for x in raw_splits.split(",") if x.strip()]
        except ValueError:
            print("Invalid split input")
            continue
        segments = reconstruct_paragraphs(user_input, splits)
        print("\nSEGMENTS:")
        for i, seg in enumerate(segments):
            print(f"[{i}] {repr(seg)}")
        print("-" * 60)
