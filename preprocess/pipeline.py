from typing import Dict, Any

from .normalize import normalize_text
from .sanitize import sanitize_text
from .regex_signals import extract_signals


def run_preprocess(text: str) -> Dict[str, Any]:
    """
    Full preprocessing pipeline.

    Steps:
    1. Normalize (encoding repair)
    2. Sanitize (remove HTML)
    3. Extract structural signals

    Returns:
    {
        "text": <cleaned_text>,
        "signals": <list of signals>
    }
    """
    if not isinstance(text, str):
        raise TypeError("Input must be a string")

    # Step 1: normalize
    normalized = normalize_text(text)

    # Step 2: sanitize
    sanitized = sanitize_text(normalized)

    # Step 3: extract signals
    signals = extract_signals(sanitized)

    return {
        "text": sanitized,
        "signals": signals,
    }


# -------------------------------
# Interactive manual test section
# -------------------------------
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("PREPROCESS PIPELINE TEST")
    print("=" * 60)
    print("Enter text (type 'exit' to quit)\n")

    import sys
    #while True:
    #    user_input = input("INPUT> ")

    #    if user_input.lower() == "exit":
    #        print("Exiting test session.")
    #        break

    while True:
        user_input = sys.stdin.read()
        result = run_preprocess(user_input)

        print("\n--- CLEAN TEXT ---")
        print(result["text"])

        print("\n--- SIGNALS ---")
        for s in result["signals"]:
            print(f"{s['type']:12} | {s['start']:4}-{s['end']:4} | {repr(s['value'])}")

        print("\n" + "-" * 60)
