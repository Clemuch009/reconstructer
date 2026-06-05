# core/signal_contracts.py

from typing import Dict, Any, List, Optional


# ---------------------------------
# Allowed enums
# ---------------------------------

VALID_SIGNAL_TYPES = {
    "separator",
    "empty_line",
    "newline",
    "list_marker",
}

VALID_STRENGTHS = {
    "absolute",
    "strong",
    "medium",
    "weak",
}


# ---------------------------------
# Helpers
# ---------------------------------

def _require_keys(obj: Dict[str, Any], keys: List[str], context: str):
    for k in keys:
        if k not in obj:
            raise ValueError(f"{context}: missing '{k}'")


def _require_type(value, expected, context: str):
    if not isinstance(value, expected):
        raise TypeError(f"{context}: expected {expected}, got {type(value)}")


# ---------------------------------
# REGEX SIGNALS (positional)
# ---------------------------------

def validate_regex_signals(regex: Dict[str, Any], text_length: int) -> None:
    """
    Validates regex signal schema and physical bounds.
    """
    _require_keys(regex, ["signals"], "regex")

    signals = regex["signals"]
    _require_type(signals, list, "regex.signals")

    for i, sig in enumerate(signals):
        ctx = f"regex[{i}]"

        _require_keys(sig, ["type", "start", "end", "value", "strength"], ctx)

        if sig["type"] not in VALID_SIGNAL_TYPES:
            raise ValueError(f"{ctx}: invalid type '{sig['type']}'")

        if sig["strength"] not in VALID_STRENGTHS:
            raise ValueError(f"{ctx}: invalid strength '{sig['strength']}'")

        _require_type(sig["start"], int, ctx)
        _require_type(sig["end"], int, ctx)

        # Physical Truth Checks
        if sig["start"] < 0 or sig["end"] < 0:
            raise ValueError(f"{ctx}: negative index")

        if sig["start"] > sig["end"]:
            raise ValueError(f"{ctx}: start > end")

        if sig["end"] > text_length:
            raise ValueError(f"{ctx}: end {sig['end']} exceeds text length {text_length}")


# ---------------------------------
# BOUNDARY SIGNALS
# ---------------------------------

def validate_boundary_signals(boundary: Dict[str, Any], text_length: int) -> None:
    # NOTE: These keys are scalar counts (ints), not positional arrays.
    # Only list_block_transition_positions is a positional array.
    required = [
        "paragraph_breaks",
        "list_block_transitions",
        "sentence_end_lines",
        "list_block_transition_positions",
    ]

    _require_keys(boundary, required, "boundary")

    for key in ["paragraph_breaks", "list_block_transitions", "sentence_end_lines"]:
        _require_type(boundary[key], int, f"boundary.{key}")

    positions = boundary["list_block_transition_positions"]
    _require_type(positions, list, "boundary.list_block_transition_positions")

    for i, pos in enumerate(positions):
        _require_type(pos, int, f"boundary.positions[{i}]")
        
        if pos < 0:
            raise ValueError(f"boundary.positions[{i}]: negative index")
        
        # Physical Truth Check
        if pos > text_length:
            raise ValueError(
                f"boundary.positions[{i}]: index {pos} exceeds text length {text_length}"
            )


# ---------------------------------
# TEXT SIGNALS (STRUCTURAL AGGREGATES)
# ---------------------------------

def validate_text_signals(text_data: Dict[str, Any]) -> None:
    """
    NOTE: text_signals are structural aggregates/counts and are NOT 
    validated against text_length directly. Only positional systems 
    (regex/boundary/spacy) enforce physical buffer constraints.
    """
    required = [
        "total_lines",
        "empty_lines",
        "bullet_lines",
        "numbered_lines",
        "line_lengths",
        "avg_line_length",
    ]

    _require_keys(text_data, required, "text")

    _require_type(text_data["total_lines"], int, "text.total_lines")
    _require_type(text_data["empty_lines"], int, "text.empty_lines")
    _require_type(text_data["bullet_lines"], int, "text.bullet_lines")
    _require_type(text_data["numbered_lines"], int, "text.numbered_lines")
    _require_type(text_data["avg_line_length"], (int, float), "text.avg_line_length")

    line_lengths = text_data["line_lengths"]
    _require_type(line_lengths, list, "text.line_lengths")

    for i, l in enumerate(line_lengths):
        _require_type(l, int, f"text.line_lengths[{i}]")
        if l < 0:
            raise ValueError(f"text.line_lengths[{i}]: negative length")


# ---------------------------------
# TOKEN SIGNALS (CHARACTER LEVEL)
# ---------------------------------

def validate_token_signals(token: Dict[str, Any]) -> None:
    required = [
        "char_count",
        "word_count",
        "symbol_count",
        "avg_word_length",
        "uppercase_count",
        "lowercase_count",
        "digit_count",
        "punctuation_count",
        "content_line_count",
    ]

    _require_keys(token, required, "token")

    for key in required:
        value = token[key]

        if key == "avg_word_length":
            _require_type(value, (int, float), f"token.{key}")
        else:
            _require_type(value, int, f"token.{key}")

        if value < 0:
            raise ValueError(f"token.{key} must be >= 0")


# ---------------------------------
# SPACY SIGNALS (ADVISORY)
# ---------------------------------

def validate_spacy_signals(spacy: Dict[str, Any], text_length: int) -> None:
    required = [
        "spacy_sentence_count",
        "spacy_sentence_spans",
        "spacy_avg_sentence_length",
    ]

    _require_keys(spacy, required, "spacy")

    spans = spacy["spacy_sentence_spans"]
    _require_type(spans, list, "spacy.spans")

    for i, span in enumerate(spans):
        if not isinstance(span, tuple) or len(span) != 2:
            raise ValueError(f"spacy.span[{i}] must be (start, end)")

        start, end = span
        _require_type(start, int, f"spacy.span[{i}].start")
        _require_type(end, int, f"spacy.span[{i}].end")

        if start > end:
            raise ValueError(f"spacy.span[{i}]: start {start} > end {end}")
            
        # Comprehensive Physical Truth Check
        if start > text_length:
            raise ValueError(f"spacy.span[{i}]: start {start} exceeds text length {text_length}")
        if end > text_length:
            raise ValueError(f"spacy.span[{i}]: end {end} exceeds text length {text_length}")


# ---------------------------------
# FEATURE VECTOR (POST-NORMALIZATION)
# ---------------------------------

def validate_features(features: Dict[str, Any]) -> None:
    """
    Validate FINAL normalized feature vector.
    LIFECYCLE: Called AFTER build_feature_vector(), BEFORE decision.py.
    """
    required = [
        "structure_intensity",
        "boundary_intensity",
        "stability",
        "external_agreement",
        "chaos_index",
    ]

    _require_keys(features, required, "features")

    for key in required:
        value = features[key]
        _require_type(value, (int, float), f"features.{key}")

        if value < 0.0 or value > 1.0:
            raise ValueError(
                f"features.{key} out of bounds: {value} "
                "(expected post-normalization feature)"
            )


# ---------------------------------
# FULL ENTRY POINT (PRE-ANALYSIS)
# ---------------------------------

def validate_all_signals(signals: Dict[str, Any], text_length: int) -> None:
    """
    Validates raw signal dictionary BEFORE analysis.
    Enforces 'Physical Truth' by checking all positional systems against text_length.
    """
    _require_keys(signals, ["regex", "boundary", "text", "token", "spacy"], "signals")

    validate_regex_signals(signals["regex"], text_length)
    validate_boundary_signals(signals["boundary"], text_length)
    validate_text_signals(signals["text"])
    validate_token_signals(signals["token"])
    validate_spacy_signals(signals["spacy"], text_length)
