from ftfy import fix_text
import html
import re

# NO 'r' prefix here! This lets Python process the Unicode escapes.
ZERO_WIDTH_PATTERN = re.compile('[\u200b\u200c\u200d\ufeff]')

def normalize_text(text: str) -> str:
    """
    repairs broken Unicode / encoding issues.

    Rules:
        -No sematic changes allowed
        -No trimming  or reconstructing
        -Only encoding fixes
        -zer-width character removal

    """

    if not isinstance(text, str):
        raise TypeError("input must be a string")

    text = fix_text(text)

    #Decode HTML entities(&nbsp;, &lt;, etc.)
    text = html.unescape(text)

    # Remove zero-width characters
    text = ZERO_WIDTH_PATTERN.sub("", text)

    # Normalize non-breaking space
    text = text.replace('\xa0', ' ')

    # Normalize excessive vertical whitespace
    text =  re.sub(r'\n{4,}', '\n\n', text) #added during debug
    
    # This looks for newlines that have any amount of whitespace (spaces/tabs) between them
    text = re.sub(r'(\n\s*){3,}', '\n\n', text)
    return text


# -------------------------------
# Interactive manual test section
# -------------------------------

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("NORMALIZATION INTERACTIVE TEST")
    print("=" * 60)
    print("Enter text (type 'exit' to quit)\n")

    while True:
        user_input = input("INPUT> ")

        if user_input.lower() == "exit":
            print("Exiting test session.")
            break

        result = normalize_text(user_input)

        print("\nOUTPUT:")
        print(result)
        print("-" * 60)
