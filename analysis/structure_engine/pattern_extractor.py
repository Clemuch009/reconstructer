# analysis/structure_engine/pattern_extractor.py

import re
from typing import List
from typing_extensions import TypedDict
#from line_model import LineObject
from analysis.structure_engine.line_model import LineObject


# ---------------------------------
# Contract
# ---------------------------------

class LinePattern(TypedDict):
    line_index: int
    patterns: List[str]


# ---------------------------------
# Compiled patterns
# ---------------------------------

# Matches pure separator lines including spaced variants: ---- ---- ----
SEPARATOR_RE = re.compile(r"^(?:[-=_]\s*){3,}$")

# Matches standard list markers
LIST_ITEM_RE = re.compile(r"^(?:[-*•]|\d+[\.\)])\s+")

# Matches key-value pairs with : or = delimiter
KV_RE = re.compile(r"^[^:=]+[:=].*")

# Detects any numeric token — intentionally broad
# Covers amounts, IDs, counts, measurements
# Downstream detectors filter by context
NUMERIC_RE = re.compile(r"\b\d[\d,\.]*\b")


# ---------------------------------
# Individual detectors
# ---------------------------------

def _is_empty(line: LineObject) -> bool:
    return line["is_empty"]


def _is_separator(line: LineObject) -> bool:
    return bool(SEPARATOR_RE.match(line["normalized"]))


def _is_list_item(line: LineObject) -> bool:
    return bool(LIST_ITEM_RE.match(line["normalized"]))


def _is_kv_pair(line: LineObject) -> bool:
    return bool(KV_RE.match(line["normalized"]))


def _is_indented(line: LineObject) -> bool:
    return line["indent"] > 0


def _is_numeric_value(line: LineObject) -> bool:
    return bool(NUMERIC_RE.search(line["normalized"]))


def _is_header_like(line: LineObject) -> bool:
    """
    Header heuristic (structural only):
    - non-empty
    - short line (<=60 chars)
    - few tokens (<=5)
    - no terminal punctuation
    - not a list item
    - not a kv pair
    """
    n = line["normalized"]
    if not n:
        return False
    if not any(c.isalpha() for c in line["normalized"]):
        return False
    if len(n) > 60:
        return False
    if len(line["tokens"]) > 5:
        return False
    if n[-1] in ".!?,;:":
        return False
    if _is_list_item(line):
        return False
    if _is_kv_pair(line):
        return False
    if _is_separator(line):
        return False
    return True


# ---------------------------------
# DETECTORS — sole authority for pattern order
# ---------------------------------

DETECTORS = [
    ("empty",         _is_empty),
    ("separator",     _is_separator),
    ("list_item",     _is_list_item),
    ("kv_pair",       _is_kv_pair),
    ("indented",      _is_indented),
    ("numeric_value", _is_numeric_value),
    ("header_like",   _is_header_like),
]


# ---------------------------------
# Core
# ---------------------------------

def extract_patterns(line: LineObject) -> LinePattern:
    """
    Extract all observable patterns from a single LineObject.
    Order is deterministic — defined by DETECTORS list.
    """
    patterns = [
        name for name, detect in DETECTORS if detect(line)
    ]
    return LinePattern(
        line_index=line["line_index"],
        patterns=patterns,
    )


def extract_all_patterns(line_model: List[LineObject]) -> List[LinePattern]:
    """
    Extract patterns from all lines in a segment.
    """
    return [extract_patterns(line) for line in line_model]


# ---------------------------------
# Interactive Test
# ---------------------------------

if __name__ == "__main__":
    #from line_model import build_line_model
    from analysis.structure_engine.line_model import build_line_model
    import os

    print("\n" + "=" * 60)
    print("PATTERN EXTRACTOR INTERACTIVE TEST")
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
        patterns = extract_all_patterns(model)

        print("\n--- PATTERNS ---\n")
        for lp in patterns:
            print(f"[{lp['line_index']}] {lp['patterns']}")

        print("-" * 60)
