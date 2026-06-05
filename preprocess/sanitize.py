import nh3
import html

def sanitize_text(text: str) -> str:
    """
    Removes HTML/markup and returns safe plain text.

    Rules:
        - Strip all tags
        - Preserve visible text
        - Do NOT restructure content
    """

    if not isinstance(text, str):
        raise TypeError("Input must be a string")

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

