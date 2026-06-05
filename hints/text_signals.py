# hints/text_signals.py

import re
from typing import Dict, Any


NUMBERED_LIST_PATTERN = re.compile(r"^\s*\d+[\.\)]\s+")
BULLET_PATTERN = re.compile(r"^\s*[-*•]\s+")


def extract_text_signals(text: str) -> Dict[str, Any]:
    """
    Extract structural signals from text.

    This layer does NOT define:
    - separators (handled in regex_signals.py)
    - semantic meaning
    """

    if not isinstance(text, str):
        raise TypeError("Input must be a string")

    lines = text.split("\n")

    total_lines = len(lines)
    empty_lines = sum(1 for line in lines if line.strip() == "")

    bullet_lines = sum(1 for line in lines if BULLET_PATTERN.match(line))
    numbered_lines = sum(1 for line in lines if NUMBERED_LIST_PATTERN.match(line))

    # IMPORTANT: only meaningful content lines
    non_empty_lines = [line for line in lines if line.strip() != ""]

    line_lengths = [len(line) for line in non_empty_lines]

    avg_line_length = (
        sum(len(line) for line in non_empty_lines) / len(non_empty_lines)
        if non_empty_lines else 0.0
    )

    return {
        "total_lines": total_lines,
        "empty_lines": empty_lines,
        "bullet_lines": bullet_lines,
        "numbered_lines": numbered_lines,
        "line_lengths": line_lengths,
        "avg_line_length": avg_line_length,
    }


# -------------------------------
# Manual test
# -------------------------------
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("TEXT SIGNALS TEST")
    print("=" * 60)
    print("Enter text (type 'exit' to quit)\n")

    while True:
        user_input = input("INPUT> ")

        if user_input.lower() == "exit":
            break

        result = extract_text_signals(user_input)

        print("\nSIGNALS:")
        for k, v in result.items():
            print(f"{k:20}: {v}")

        print("-" * 60)
