from typing import Dict, Any, List, Tuple
import spacy

# strict pipeline control
try:
    nlp = spacy.load("en_core_web_sm", disable=["ner", "tagger", "lemmatizer", "parser"])
    nlp.add_pipe("sentencizer")
    nlp.max_length = 2_000_000  # per-chunk limit, not full doc
except OSError:
    raise RuntimeError(
        "spaCy model 'en_core_web_sm' not found. "
        "Run: python -m spacy download en_core_web_sm"
    )

if "sentencizer" not in nlp.pipe_names:
    nlp.add_pipe("sentencizer")

# Maximum chars per spaCy chunk.
# sentencizer (no parser/NER) uses ~2MB per 100k chars — 1M chars = ~20MB.
# Safe well within Cloud Run memory limits.
_CHUNK_SIZE = 1_000_000


def extract_spacy_hints(text: str) -> Dict[str, Any]:
    """
    spaCy-based boundary suggestions (advisory only).
    Returns spans, not raw text.

    For large documents, processes in 1M-char chunks and merges results.
    Sentence spans from each chunk are offset-corrected so they reflect
    character positions in the original full text — downstream consumers
    receive correct absolute offsets regardless of chunking.
    """
    if not isinstance(text, str):
        raise TypeError("Input must be a string")

    if len(text) <= _CHUNK_SIZE:
        # Fast path — single chunk, no offset adjustment needed
        return _process_chunk(text, offset=0)

    # Chunked path — process each chunk, adjust offsets, merge
    all_spans:   List[Tuple[int, int]] = []
    all_lengths: List[int]             = []

    pos = 0
    while pos < len(text):
        chunk  = text[pos:pos + _CHUNK_SIZE]
        result = _process_chunk(chunk, offset=pos)

        all_spans.extend(result["spacy_sentence_spans"])
        all_lengths.extend(
            end - start
            for start, end in result["spacy_sentence_spans"]
        )
        pos += _CHUNK_SIZE

    sentence_count = len(all_spans)
    avg_length     = sum(all_lengths) / sentence_count if sentence_count > 0 else 0.0

    return {
        "spacy_sentence_count":      sentence_count,
        "spacy_sentence_spans":      all_spans,
        "spacy_avg_sentence_length": avg_length,
    }


def _process_chunk(text: str, offset: int) -> Dict[str, Any]:
    """
    Run spaCy on a single chunk and return offset-corrected spans.
    offset — character position of this chunk's start in the full document.
    """
    doc = nlp(text)

    spans:   List[Tuple[int, int]] = []
    lengths: List[int]             = []

    for sent in doc.sents:
        if sent.text.strip() == "":
            continue
        # Adjust character offsets to full-document coordinates
        start = sent.start_char + offset
        end   = sent.end_char   + offset
        spans.append((start, end))
        lengths.append(end - start)

    sentence_count = len(spans)
    avg_length     = sum(lengths) / sentence_count if sentence_count > 0 else 0.0

    return {
        "spacy_sentence_count":      sentence_count,
        "spacy_sentence_spans":      spans,
        "spacy_avg_sentence_length": avg_length,
    }
