from typing import Dict, Any, List, Tuple
import spacy


# strict pipeline control
try:
    nlp = spacy.load("en_core_web_sm", disable=["ner", "tagger", "lemmatizer", "parser"])
    nlp.add_pipe("sentencizer")
    nlp.max_length = 100_000_000
except OSError:
    raise RuntimeError(
            "spaCy model 'en_core_web_sm' not found. "
            "Run: python -m spacy download en_core_web_sm"
            )

if "sentencizer" not in nlp.pipe_names:
    nlp.add_pipe("sentencizer")


def extract_spacy_hints(text: str) -> Dict[str, Any]:
    """
    spaCy-based boundary suggestions (advisory only)

    Returns spans, not raw text.
    """

    if not isinstance(text, str):
        raise TypeError("Input must be a string")

    doc = nlp(text)

    sentence_spans: List[Tuple[int, int]] = []
    sentence_lengths: List[int] = []

    for sent in doc.sents:
        if sent.text.strip() == "":
            continue
        start = sent.start_char
        end = sent.end_char

        sentence_spans.append((start, end))
        sentence_lengths.append(end - start)

    sentence_count = len(sentence_spans)

    avg_sentence_length = (
        sum(sentence_lengths) / sentence_count if sentence_count > 0 else 0.0
    )

    return {
        "spacy_sentence_count": sentence_count,
        "spacy_sentence_spans": sentence_spans,
        "spacy_avg_sentence_length": avg_sentence_length,
    }


# ----------------------------
# Manual test
# ----------------------------
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("SPACY ADAPTER TEST (FIXED)")
    print("=" * 60)
    print("Enter text (type 'exit' to quit)\n")

    while True:
        user_input = input("INPUT> ")

        if user_input.lower() == "exit":
            break

        user_input = user_input.strip()

        result = extract_spacy_hints(user_input)

        print("\nSPACY HINTS:")
        for k, v in result.items():
            print(f"{k:30}: {v}")

        print("-" * 60)
