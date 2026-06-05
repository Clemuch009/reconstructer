# analysis/structure_engine/line_model.py

from typing import List, Tuple
from typing_extensions import TypedDict
import re

# ---------------------------------
# Contract
# ---------------------------------

class LineObject(TypedDict):
    line_index: int      # 0-based position within segment
    text: str            # raw line, unchanged
    normalized: str      # text.strip() only
    indent: int          # leading whitespace count (from raw line)
    is_empty: bool       # normalized == ""
    tokens: List[str]    # normalized.split() only
    token_offsets: List[int]


# ---------------------------------
# Helpers
# ---------------------------------

INDENT_PATTERN = re.compile(r"^(\s*)")


def _count_indent(line: str) -> int:
    """
    Count leading whitespace characters on raw line.
    Measured before normalization — indent must reflect original layout.
    """
    match = INDENT_PATTERN.match(line)
    return len(match.group(1)) if match else 0


def _normalize(line: str) -> str:
    """
    Strict contract: text.strip() only.
    Forbidden: collapsing, lowercasing, punctuation changes, unicode ops.
    """
    return line.strip()


def _tokenize_with_offsets(normalized: str) -> Tuple[List[str], List[int]]:
    """
    Whitespace tokenization with start offset tracking.
    Offsets are character positions within normalized string.
    """
    tokens = []
    offsets = []
    i = 0
    n = len(normalized)
    while i < n:
        if normalized[i].isspace():
            i += 1
            continue
        start = i
        while i < n and not normalized[i].isspace():
            i += 1
        tokens.append(normalized[start:i])
        offsets.append(start)
    return tokens, offsets


# ---------------------------------
# Core
# ---------------------------------

def build_line_model(segment_text: str) -> List[LineObject]:
    if not isinstance(segment_text, str):
        raise TypeError("segment_text must be a string")

    lines = segment_text.splitlines()
    model: List[LineObject] = []

    for idx, line in enumerate(lines):
        indent = _count_indent(line)
        normalized = _normalize(line)
        is_empty = normalized == ""
        tokens, token_offsets = _tokenize_with_offsets(normalized)

        model.append(LineObject(
            line_index=idx,
            text=line,
            normalized=normalized,
            indent=indent,
            is_empty=is_empty,
            tokens=tokens,
            token_offsets=token_offsets,
        ))

    return model


# ---------------------------------
# Interactive Test
# ---------------------------------

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("LINE MODEL INTERACTIVE TEST")
    print("=" * 60)
    print("Enter multi-line text.")
    print("Type 'END' on its own line to process.")
    print("Type 'exit' to quit.\n")

    while True:
        user_input = []

        while True:
            line = input()
            if line.strip().lower() == "exit":
                print("Exiting.")
                exit(0)
            if line.strip().upper() == "END":
                break
            user_input.append(line)

        if not user_input:
            continue

        text = "\n".join(user_input)
        model = build_line_model(text)

        print("\n--- LINE MODEL ---\n")
        for obj in model:
            print(f"[{obj['line_index']}]")
            print(f"  text       : {repr(obj['text'])}")
            print(f"  normalized : {repr(obj['normalized'])}")
            print(f"  indent     : {obj['indent']}")
            print(f"  is_empty   : {obj['is_empty']}")
            print(f"  tokens     : {obj['tokens']}")
            print(f"  token_offsets: {obj['token_offsets']}")
            print()

        print("-" * 60)
