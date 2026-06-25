# preprocess/sanitize.py

import re
import nh3
import html


# Detects genuine HTML tags: '<' followed by a letter or '/' (e.g. <p>, </div>).
# A bare '<' in data (e.g. "latency < 5ms") does NOT match, so plain text and
# CSV/TSV are never mistaken for HTML.
_HTML_TAG_RE = re.compile(r'<[a-zA-Z/][^>]*>')


def sanitize_text(text: str) -> str:
    """
    Removes HTML/markup and returns safe plain text.

    Rules:
        - Strip all tags
        - Preserve visible text
        - Do NOT restructure content

    IMPORTANT: nh3.clean is an HTML sanitizer. Running it on non-HTML input
    (CSV, TSV, logs, plain text) is a category error — it can alter quoting,
    re-encode entities, and normalize whitespace/newlines in ways that destroy
    tabular structure (e.g. a CSV's row newlines), causing the engine to see a
    single flattened line and misclassify a table as prose.

    Fix: only invoke the HTML sanitizer when the text actually contains HTML
    tags. Plain text passes through untouched, preserving newlines and quoting
    so downstream structure detection (tables, etc.) works correctly. Genuine
    HTML is still fully sanitized.
    """
    if not isinstance(text, str):
        raise TypeError("Input must be a string")

    # No HTML tags → not HTML → pass through unchanged (preserves structure).
    if not _HTML_TAG_RE.search(text):
        return text

    # Strip all tags, keep text content only
    cleaned = nh3.clean(text, tags=set(), attributes={})
    return html.unescape(cleaned)


# -------------------------------
# Interactive manual test section
# -------------------------------
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("SANITIZE INTERACTIVE TEST")
    print("=" * 60)
    print("Enter text (type 'exit' to quit)\n")

    while True:
        user_input = input("INPUT>")
        if user_input.lower() == "exit":
            print("Exiting text session.")
            break
        result = sanitize_text(user_input)
        print("\nOUTPUT:")
        print(result)
        print("-" * 60)
