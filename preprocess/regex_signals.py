# preprocess/regex_signals.py

import re
from typing import List, Dict, Any


# --- Strength Mapping ---

SIGNAL_STRENGTH = {
    "separator": "absolute",
    "empty_line": "strong",
    "list_marker": "medium",
    "newline": "weak",
}


# --- Patterns (fixed) ---

NEWLINE_PATTERN = re.compile(r"\n")

EMPTY_LINE_PATTERN = re.compile(r"\n\s*\n")

LIST_PATTERN = re.compile(
    r"(?m)^[ \t]*([-*•]|\d+\.)\s+"
)

# ✅ FIXED: supports grouped separators like "---   ---   ---"
#SEPARATOR_PATTERN = re.compile(
#        r"(?m)^[ \t]*(?:[-=_]{3,}[ \t]*)+\n?" #THIS LINE HAS BEEN MODIFIED DURING DEBUG FROM r"(?m)^[ \t]*(?:[-=_]{3,}[ \t]*)+$"
#)

#SEPARATOR_PATTERN = re.compile(r"(?m)^[ \t]*([-=_]{3,})[ \t]*$")
SEPARATOR_PATTERN = re.compile(r"(?m)^[ \t]*([-=_]{3,})[ \t]*(?=\n|$)")

def extract_signals(text: str) -> List[Dict[str, Any]]:
    """
    Extracts structural signals and attaches their defined 'strength' metadata.
    """
    if not isinstance(text, str):
        raise TypeError("Input must be a string")

    signals = []

    # --- Newlines ---
    for m in NEWLINE_PATTERN.finditer(text):
        signals.append(_make_signal("newline", m))

    # --- Empty lines ---
    for m in EMPTY_LINE_PATTERN.finditer(text):
        signals.append(_make_signal("empty_line", m))

    # --- List markers ---
    for m in LIST_PATTERN.finditer(text):
        signals.append(_make_signal("list_marker", m))

    # --- Separators ---
    for m in SEPARATOR_PATTERN.finditer(text):
        signals.append(_make_signal("separator", m))

    # Sort signals by their position in the text
    signals.sort(key=lambda s: (s["start"], s["end"]))

    return signals


def _make_signal(signal_type: str, match: re.Match) -> Dict[str, Any]:
    """
    Internal helper to package the match with its strength from the contract.
    """
    return {
        "type": signal_type,
        "start": match.start(),
        "end": match.end(),
        "value": match.group(),
        "strength": SIGNAL_STRENGTH[signal_type],
    }

if __name__ == "__main__":
    import sys
    user_input = sys.stdin.read()
    user_input =  extract_signals(user_input)
    for i, word in enumerate(user_input):
        print(f"{i}, {word}")
