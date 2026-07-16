# core/rules.py

import re
from typing import Dict, Any, List, Optional
from utils.context import get_local_window, compute_local_density


# ---------------------------------
# Rule priorities (higher wins)
# ---------------------------------

PRIORITY = {
    "separator": 100,
    "list_transition": 90,
    "empty_line": 50,
    "sentence_end": 20,
    "newline": 0,
}


# ---------------------------------
# Decision object
# ---------------------------------

class RuleDecision:
    def __init__(self, split: bool, reason: str, priority: int):
        self.split = split
        self.reason = reason
        self.priority = priority

    def __repr__(self):
        return (
            f"<RuleDecision split={self.split} "
            f"reason={self.reason} priority={self.priority}>"
        )


# ---------------------------------
# RULES
# ---------------------------------

def rule_separator(sig: Dict[str, Any]) -> Optional[RuleDecision]:
    if sig["type"] == "separator":
        return RuleDecision(True, "separator", PRIORITY["separator"])
    return None


def rule_list_transition(
    pos: int,
    boundary: Dict[str, Any]
) -> Optional[RuleDecision]:

    transitions = set(boundary.get("list_block_transition_positions", []))

    if pos in transitions:
        return RuleDecision(True, "list_transition", PRIORITY["list_transition"])

    return None


def _looks_like_kv_line(line: str) -> bool:
    """
    Conservative, local test for a `key: value` line.

    Deliberately mirrors (rather than imports) the kv detector's key rule: this
    module sits below the structure engine, so importing from it would invert
    the layering. Kept strict so prose that merely contains a colon
    ("Note: the results were surprising and required further study") is NOT
    treated as kv — the key must be a short identifier-like token sequence.
    """
    stripped = line.strip()
    if not stripped or ":" not in stripped:
        return False
    key, _, value = stripped.partition(":")
    key = key.strip()
    value = value.strip()
    if not key or not value:
        return False
    # Key: 1-3 word-ish tokens, no sentence punctuation.
    return bool(_KV_KEY_RE.match(key))


_KV_KEY_RE = re.compile(r"^(?=.*\w)[\w.\-#]+(?: [\w.\-#]+){0,2}$")


def _neighbours_are_kv(text: str, pos: int) -> bool:
    """
    True when the non-empty line immediately before `pos` and the non-empty line
    immediately after `pos` are both kv-shaped.

    A blank line between two `key: value` lines is FORMATTING (an HTML <p> gap,
    a DOCX paragraph gap), not a paragraph boundary. Splitting there fragments
    a single logical kv block into per-line segments, which then classify as
    prose/context and never cohere into a kv_block. Blank lines between prose
    paragraphs, or between a kv line and a prose paragraph, still split.
    """
    before = text[:pos].split("\n")
    after  = text[pos:].split("\n")

    prev_line = next((l for l in reversed(before) if l.strip()), None)
    next_line = next((l for l in after if l.strip()), None)
    if prev_line is None or next_line is None:
        return False
    return _looks_like_kv_line(prev_line) and _looks_like_kv_line(next_line)


def rule_empty_line(
    sig: Dict[str, Any],
    text: str,
) -> Optional[RuleDecision]:
    """
    A blank line is normally a paragraph boundary. It is NOT a boundary when it
    merely separates two key-value lines — a shape produced by every HTML <p>
    and DOCX paragraph. Suppressing the split there lets adjacent kv lines
    cohere into one kv_block instead of fragmenting into prose/context.
    """
    if sig["type"] != "empty_line":
        return None

    if _neighbours_are_kv(text, sig["end"]):
        return RuleDecision(False, "empty_line_within_kv", PRIORITY["empty_line"])

    return RuleDecision(True, "empty_line", PRIORITY["empty_line"])

    #window = get_local_window(text, sig["end"])
    #local_density = compute_local_density(window)
    #threshold = 0.25 - (chaos * 0.1) - (boundary_intensity * 0.05)
    #threshold = max(0.1, min(0.4, threshold))
    #print(f"[DEBUG] empty_line pos={sig['end']} density={local_density:.4f} threshold={threshold:.4f}")

    #chaos = features.get("chaos_index", 0.0)
    #boundary_intensity = features.get("boundary_intensity", 0.0)

    #threshold = (
    #    0.25
    #    - (chaos * 0.1)
    #    - (boundary_intensity * 0.05)
    #)

    #threshold = max(0.1, min(0.4, threshold))

    #print(f"[DEBUG] empty_line pos={sig['end']} density={local_density:.4f} threshold={threshold:.4f}")

    #if local_density > threshold:
    #    return RuleDecision(True, "empty_line", PRIORITY["empty_line"])



    #return RuleDecision(False, "empty_line_blocked", PRIORITY["empty_line"])


def rule_sentence_end(sig: Dict[str, Any]) -> Optional[RuleDecision]:
    # advisory only
    if sig["type"] == "sentence_end":
        return RuleDecision(False, "sentence_end_advisory", PRIORITY["sentence_end"])
    return None


def rule_newline(sig: Dict[str, Any]) -> Optional[RuleDecision]:
    # explicitly ignored
    if sig["type"] == "newline":
        return RuleDecision(False, "newline_ignored", PRIORITY["newline"])
    return None


# ---------------------------------
# RULE ENGINE (per-position)
# ---------------------------------

def evaluate_position(
    sig: Dict[str, Any],
    boundary: Dict[str, Any],
    features: Dict[str, float],
    text: str,
) -> RuleDecision:
    """
    Evaluate all rules for a single signal position.
    Returns the highest-priority decision.
    """

    pos = sig["end"]

    decisions: List[RuleDecision] = []

    # Apply all rules (no short-circuiting)
    for rule in [
        lambda: rule_separator(sig),
        lambda: rule_list_transition(pos, boundary),
        lambda: rule_empty_line(sig, text),
        lambda: rule_sentence_end(sig),
        lambda: rule_newline(sig),
    ]:
        result = rule()
        if result is not None:
            decisions.append(result)

    if not decisions:
        return RuleDecision(False, "no_rule", -1)

    # Highest priority wins
    decisions.sort(key=lambda d: d.priority, reverse=True)
    return decisions[0]


# ---------------------------------
# INTERACTIVE TEST
# ---------------------------------

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("RULE ENGINE TEST (FINAL)")
    print("=" * 60)

    sample_text = (
        "Line A\n"
        "Line B\n\n"
        "Item 1\n"
        "Item 2\n\n"
        "---\n"
        "Final paragraph.\n"
    )

    mock_signals = {
        "regex": {
            "signals": [
                {"type": "newline", "start": 5, "end": 6, "value": "\n", "strength": "weak"},
                {"type": "empty_line", "start": 12, "end": 14, "value": "\n\n", "strength": "strong"},
                {"type": "list_marker", "start": 15, "end": 20, "value": "1.", "strength": "medium"},
                {"type": "separator", "start": 30, "end": 33, "value": "---", "strength": "absolute"},
            ]
        },
        "boundary": {
            "paragraph_breaks": 0,
            "list_block_transitions": 1,
            "sentence_end_lines": 0,
            "list_block_transition_positions": [15],
        },
    }

    mock_features = {
        "structure_intensity": 0.6,
        "boundary_intensity": 0.4,
        "stability": 0.7,
        "external_agreement": 0.5,
        "chaos_index": 0.2,
    }

    for sig in mock_signals["regex"]["signals"]:
        decision = evaluate_position(sig, mock_signals["boundary"], mock_features, sample_text)
        print(f"{sig['type']:12} -> {decision}")

    print("-" * 60)
