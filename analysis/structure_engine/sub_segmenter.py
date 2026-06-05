# analysis/structure_engine/sub_segmenter.py

import re
from typing import List, Optional
from typing_extensions import TypedDict
from analysis.structure_engine.line_model import LineObject


# ---------------------------------
# Contract
# ---------------------------------

class SubSegment(TypedDict):
    lines:          List[LineObject]
    start_line:     int
    end_line:       int
    boundary_type:  str   # "hard" | "soft" | "terminal"
    trigger:        str   # "separator" | "empty" | "eof"
    boundary_marker: bool  # True if preceded by a hard separator


# ---------------------------------
# Constants
# ---------------------------------

MIN_SEGMENT_LINES = 2

SEPARATOR_RE = re.compile(r"^(?:[-=_]\s*){3,}$")


# ---------------------------------
# Boundary detection
# ---------------------------------

def _is_separator(line: LineObject) -> bool:
    return bool(SEPARATOR_RE.match(line["normalized"]))


def _is_structural_empty(line: LineObject) -> bool:
    """
    Structural emptiness only — not raw emptiness.
    Prevents over-segmentation on OCR noise and log spacing.
    Condition: normalized is empty AND no indentation.
    """
    return line["is_empty"] and line["indent"] == 0


# ---------------------------------
# Buffer flush helper
# ---------------------------------

def _flush(
    buffer: List[LineObject],
    boundary_type: str,
    trigger: str,
    boundary_marker: bool,
    pending: Optional[List[LineObject]],
) -> tuple:
    if not buffer:
        return None, pending

    # Force emit at EOF — no forward merge when nothing follows
    if len(buffer) < MIN_SEGMENT_LINES and trigger != "eof":
        merged = (pending or []) + buffer
        return None, merged

    full_buffer = (pending or []) + buffer

    seg = SubSegment(
        lines=full_buffer,
        start_line=full_buffer[0]["line_index"],
        end_line=full_buffer[-1]["line_index"],
        boundary_type=boundary_type,
        trigger=trigger,
        boundary_marker=boundary_marker,
    )
    return seg, None


# ---------------------------------
# Core
# ---------------------------------

def sub_segment(lines: List[LineObject]) -> List[SubSegment]:
    """
    Split a LineObject array into coherent sub-segments
    on structural boundaries.

    Split triggers:
    - Separator lines  → hard boundary
    - Structural empty → soft boundary

    Rules:
    - Separator and structural empty lines excluded from sub-segments
    - Segments below MIN_SEGMENT_LINES merged forward
    - boundary_marker flags segments preceded by hard separator
    - boundary_type and trigger are separate concepts
    """
    if not lines:
        return []

    sub_segments: List[SubSegment] = []
    buffer: List[LineObject] = []
    pending: Optional[List[LineObject]] = None

    # Track boundary state explicitly — never rely on loop variable
    last_boundary_type = "terminal"
    last_trigger        = "eof"
    last_was_hard       = False

    for line in lines:

        if _is_separator(line):
            seg, pending = _flush(
                buffer,
                boundary_type="hard",
                trigger="separator",
                boundary_marker=last_was_hard,
                pending=pending,
            )
            if seg:
                sub_segments.append(seg)
            buffer = []
            last_boundary_type = "hard"
            last_trigger        = "separator"
            last_was_hard       = True
            continue

        if _is_structural_empty(line):
            seg, pending = _flush(
                buffer,
                boundary_type="soft",
                trigger="empty",
                boundary_marker=last_was_hard,
                pending=pending,
            )
            if seg:
                sub_segments.append(seg)
            buffer = []
            last_boundary_type = "soft"
            last_trigger        = "empty"
            last_was_hard       = False
            continue

        buffer.append(line)

    # Final flush — use tracked boundary state, not loop variable
    seg, pending = _flush(
        buffer,
        boundary_type="terminal",
        trigger="eof",
        boundary_marker=last_was_hard,
        pending=pending,
    )
    if seg:
        sub_segments.append(seg)

    return sub_segments


# ---------------------------------
# Interactive Test
# ---------------------------------

if __name__ == "__main__":
    import sys
    from analysis.structure_engine.line_model import build_line_model

    print("\n" + "=" * 60)
    print("SUB SEGMENTER INTERACTIVE TEST")
    print("=" * 60)
    print("Paste text then Ctrl+D to process. 'exit' to quit.\n")

    while True:
        print("INPUT> ", end="", flush=True)
        try:
            raw = sys.stdin.read()
        except EOFError:
            break

        if raw.strip().lower() == "exit":
            print("Exiting.")
            break

        model = build_line_model(raw)
        results = sub_segment(model)

        print(f"\n--- SUB SEGMENTS ({len(results)}) ---\n")
        for i, seg in enumerate(results):
            print(f"[{i}] lines {seg['start_line']}–{seg['end_line']}")
            print(f"     boundary_type  : {seg['boundary_type']}")
            print(f"     trigger        : {seg['trigger']}")
            print(f"     boundary_marker: {seg['boundary_marker']}")
            print(f"     line_count     : {len(seg['lines'])}")
            for l in seg["lines"]:
                print(f"       [{l['line_index']}] {repr(l['normalized'])}")
            print()
        print("-" * 60)
