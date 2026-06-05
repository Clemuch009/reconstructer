import re
import unicodedata
from typing import Dict, Any


WORD_PATTERN = re.compile(r"\b\w+\b")


def is_punctuation(c: str) -> bool:
    return unicodedata.category(c).startswith("P")

def is_symbol(c: str) -> bool:
    return unicodedata.category(c).startswith("S")

def extract_token_observations(text: str) -> Dict[str, Any]:
    if not isinstance(text, str):
        raise TypeError("Input must be a string")

    words = WORD_PATTERN.findall(text)

    char_count = len(text)
    word_count = len(words)

    avg_word_length = (
        sum(len(w) for w in words) / word_count if word_count > 0 else 0.0
    )

    uppercase_count = sum(1 for c in text if c.isupper())
    lowercase_count = sum(1 for c in text if c.islower())
    digit_count = sum(1 for c in text if c.isdigit())
    punctuation_count = sum(1 for c in text if is_punctuation(c))
    symbol_count = sum(1 for c in text if is_symbol(c))

    # meaningful line count (ignore whitespace-only lines)
    lines = text.split("\n")
    non_empty_lines = [ln for ln in lines if ln.strip() != ""]
    content_line_count = len(non_empty_lines)

    return {
        "char_count": char_count,
        "word_count": word_count,
        "symbol_count": symbol_count,
        "avg_word_length": avg_word_length,
        "uppercase_count": uppercase_count,
        "lowercase_count": lowercase_count,
        "digit_count": digit_count,
        "punctuation_count": punctuation_count,
        "content_line_count": content_line_count,
    }


# -------------------------------
# Interactive manual test section
# -------------------------------
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("TOKEN OBSERVATIONS TEST (FIXED)")
    print("=" * 60)
    print("Enter text (type 'exit' to quit)\n")

    while True:
        user_input = input("INPUT> ")

        if user_input.lower() == "exit":
            print("Exiting test session.")
            break

        obs = extract_token_observations(user_input)

        print("\nOBSERVATIONS:")
        for k, v in obs.items():
            print(f"{k:20}: {v}")

        print("-" * 60)
