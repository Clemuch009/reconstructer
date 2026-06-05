import re
from typing import Dict, Any, List


# --- sentence boundary heuristics (non-semantic) ---
SENTENCE_END_PATTERN = re.compile(r"[.!?]\s*$")

# pattern-based abbreviation guards (no hardcoded lists)
MULTI_DOT_TOKEN = re.compile(r"(?:\b[A-Za-z]\.){2,}$")   # U.S., E.U.
SHORT_ABBREV = re.compile(r"\b[A-Za-z]{1,3}\.$")      # Dr., etc.


# --- list detection (aligned with text_signals) ---
LIST_MARKER_PATTERN = re.compile(r"^\s*(?:[-*•]|\d+[\.\)])\s+")

# --- paragraph detection ---
MULTI_BLANK_PATTERN = re.compile(r"\n\s*\n+")


def _is_list_line(line: str) -> bool:
    """Check if a line matches the list marker pattern."""
    return bool(LIST_MARKER_PATTERN.match(line))


def _is_sentence_end_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False

    # must end with terminal punctuation
    if not SENTENCE_END_PATTERN.search(s):
        return False

    # exclude common non-sentence patterns
    if MULTI_DOT_TOKEN.search(s):
        return False

    if SHORT_ABBREV.search(s):
        return False

    return True


def _compute_list_transitions(lines: List[str], line_starts: List[int]) -> List[int]:
    """
    Detects the character positions where list blocks start (entry) or end (exit).
    
    TRADE-OFF & LIMITATIONS:
    This captures BOTH entry and exit positions. While splitting at list entry handles 
    prose-to-list transitions, splitting at exit (list-to-prose) can fragment 
    continuous thoughts. A short list embedded in a sentence will produce two 
    splits, potentially creating three separate paragraphs/chunks from one 
    logical unit. This structural purity currently takes precedence over 
    linguistic continuity.
    """
    transitions = []
    in_list = False

    for i, line in enumerate(lines):
        is_blank = line.strip() == ""
        is_list = _is_list_line(line)

        if is_list and not in_list:
            # entry
            transitions.append(line_starts[i])
            in_list = True
        elif not is_list and not is_blank and in_list:
            # exit
            transitions.append(line_starts[i])
            in_list = False

    return transitions


def _list_blocks(lines: List[str]) -> List[List[int]]:
    """
    Group list items into blocks.
    Blank lines between items DO NOT break the block.
    A block ends only when a non-list, non-blank line appears.
    """
    blocks = []
    current = []

    for i, line in enumerate(lines):
        if _is_list_line(line):
            current.append(i)
        elif line.strip() == "":
            continue  # allow gaps inside a list
        else:
            if current:
                blocks.append(current)
                current = []

    if current:
        blocks.append(current)

    return blocks


def extract_boundary_signals(text: str) -> Dict[str, Any]:
    """
    Structural boundary detection (no semantic interpretation).
    """

    if not isinstance(text, str):
        raise TypeError("Input must be a string")

    lines: List[str] = text.split("\n")
    line_starts = []
    current_pos = 0
    
    for i, line in enumerate(lines):
        line_starts.append(current_pos)
        # Precision increment: only add for the newline separator between lines
        current_pos += len(line) + (1 if i < len(lines) - 1 else 0)

    total_lines = len(lines)

    # --- paragraph structure ---
    paragraph_breaks = len(MULTI_BLANK_PATTERN.findall(text))

    # --- sentence boundaries (heuristic) ---
    sentence_end_lines = sum(1 for line in lines if _is_sentence_end_line(line))
    sentence_density = (
        sentence_end_lines / total_lines if total_lines > 0 else 0.0
    )

    # --- list structure & transitions ---
    blocks = _list_blocks(lines)
    list_block_count = len(blocks)
    list_block_transitions = max(0, list_block_count - 1)
    
    transition_positions = _compute_list_transitions(lines, line_starts)

    # --- structural density (layout only) ---
    structural_events = paragraph_breaks + list_block_transitions
    structural_density = (
        structural_events / total_lines if total_lines > 0 else 0.0
    )

    return {
        "paragraph_breaks": paragraph_breaks,
        "sentence_end_lines": sentence_end_lines,
        "sentence_density": sentence_density,
        "list_block_count": list_block_count,
        "list_block_transitions": list_block_transitions,
        "list_block_transition_positions": transition_positions,
        "structural_density": structural_density,
    }


# ----------------------------
# Manual test
# ----------------------------
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("BOUNDARY SIGNALS TEST (PRECISION UPDATED)")
    print("=" * 60)
    
    sample = "Intro line.\n- Item 1\n- Item 2\n\nOutro line."
    result = extract_boundary_signals(sample)
    
    for k, v in result.items():
        print(f"{k:35}: {v}")
