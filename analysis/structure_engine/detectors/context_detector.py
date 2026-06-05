import re
import hashlib
from typing import List, Optional, Dict, Any, Tuple
from typing_extensions import TypedDict
from analysis.structure_engine.line_model import LineObject
from analysis.structure_engine.detectors.kv_detector import KVResult


# ---------------------------------
# Contracts
# ---------------------------------

class AnnotatedLine(TypedDict):
    line_index: int
    line_type:  str              # "kv" | "log" | "telemetry" | "prose"
    text:       str
    kv_key:     Optional[str]
    kv_value:   Optional[str]
    log_level:  Optional[str]    # populated for log lines only


class ContextResult(TypedDict):
    region_type:      str
    annotated_lines:  List[AnnotatedLine]
    kv_results:       Optional[List[KVResult]]
    log_blocks:       List[Dict]
    telemetry_blocks: List[Dict]
    confidence:       float
    block_count:      int


# ---------------------------------
# Constants
# ---------------------------------

SEVERITY_TOKENS = {"ERROR", "WARN", "WARNING", "INFO", "DEBUG", "CRITICAL", "FATAL"}

KV_DOMINANT_THRESHOLD          = 0.35
TELEMETRY_NUMERIC_THRESHOLD    = 0.60
TELEMETRY_REPETITION_THRESHOLD = 0.70

SEPARATOR_RE = re.compile(r"^(?:[-=_]\s*){3,}$")
KV_RE        = re.compile(r"^[^:=]+[:=].*")

# Bulletproof telemetry regex to catch units and bracket framing cleanly
TELEMETRY_TOKEN_RE = re.compile(r"^\[?0x[0-9a-fA-F]+\]?|^\[?\d[\d,\.\/]*%?(?:ms|MB/s|GB|KB|s)?\]?$")


# ---------------------------------
# Line-level classification
# ---------------------------------

def _detect_log_level(tokens: List[str]) -> Optional[str]:
    for token in tokens:
        if token.upper() in SEVERITY_TOKENS:
            return token.upper()
    return None


def _is_kv_line(normalized: str) -> bool:
    return bool(KV_RE.match(normalized))


def _is_telemetry_line(line: LineObject) -> bool:
    if not line["tokens"]:
        return False
    numeric_count = sum(1 for t in line["tokens"] if TELEMETRY_TOKEN_RE.match(t))
    return numeric_count / len(line["tokens"]) >= TELEMETRY_NUMERIC_THRESHOLD


def _classify_line(
    line: LineObject,
    kv_map: Dict[int, KVResult],
) -> AnnotatedLine:
    kv_result = kv_map.get(line["line_index"])

    if kv_result is not None:
        return AnnotatedLine(
            line_index=line["line_index"],
            line_type="kv",
            text=line["text"],
            kv_key=kv_result["key"],
            kv_value=kv_result["value"],
            log_level=None,
        )

    log_level = _detect_log_level(line["tokens"])
    if log_level is not None:
        return AnnotatedLine(
            line_index=line["line_index"],
            line_type="log",
            text=line["text"],
            kv_key=None,
            kv_value=None,
            log_level=log_level,
        )

    if _is_telemetry_line(line):
        return AnnotatedLine(
            line_index=line["line_index"],
            line_type="telemetry",
            text=line["text"],
            kv_key=None,
            kv_value=None,
            log_level=None,
        )

    return AnnotatedLine(
        line_index=line["line_index"],
        line_type="prose",
        text=line["text"],
        kv_key=None,
        kv_value=None,
        log_level=None,
    )


# ---------------------------------
# Block grouping
# ---------------------------------

def _make_block(lines: List[AnnotatedLine], block_type: str) -> Dict[str, Any]:
    """
    Build a block dict with a deterministic MD5 fingerprint signature 
    to guarantee consistent cross-process layout matching.
    """
    shape_str = ",".join(al["line_type"] for al in lines)
    deterministic_sig = hashlib.md5(shape_str.encode("utf-8")).hexdigest()[:16]
    
    return {
        "block_type":  block_type,
        "start_index": lines[0]["line_index"],
        "end_index":   lines[-1]["line_index"],
        "line_count":  len(lines),
        "signature":   deterministic_sig,
    }


def _group_all_blocks(annotated: List[AnnotatedLine]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Extracts all block configurations in a single linear O(N) pass.
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {
        "kv": [], "log": [], "telemetry": [], "prose": []
    }
    if not annotated:
        return grouped

    buffer: List[AnnotatedLine] = [annotated[0]]
    current_type = annotated[0]["line_type"]

    for al in annotated[1:]:
        if al["line_type"] == current_type:
            buffer.append(al)
        else:
            grouped[current_type].append(_make_block(buffer, current_type))
            buffer = [al]
            current_type = al["line_type"]

    if buffer:
        grouped[current_type].append(_make_block(buffer, current_type))

    return grouped


# ---------------------------------
# Dominant region classification
# ---------------------------------

def _dominant_type(annotated: List[AnnotatedLine]) -> str:
    if not annotated:
        return "prose"

    counts: Dict[str, int] = {"kv": 0, "log": 0, "telemetry": 0, "prose": 0}
    for al in annotated:
        counts[al["line_type"]] += 1

    total = len(annotated)
    dominant = max(counts, key=counts.__getitem__)
    ratio = counts[dominant] / total

    if ratio >= 0.50:
        type_map = {
            "kv":        "kv_block",
            "log":       "log_block",
            "telemetry": "telemetry_block",
            "prose":     "prose",
        }
        return type_map[dominant]

    return "mixed"


# ---------------------------------
# Confidence scoring
# ---------------------------------

def _score_confidence(
    region_type: str,
    annotated: List[AnnotatedLine],
    all_blocks: Dict[str, List[Dict]],
) -> float:
    if not annotated:
        return 0.0

    total = len(annotated)

    dominant_count = sum(
        1 for al in annotated
        if (
            (region_type == "kv_block"        and al["line_type"] == "kv") or
            (region_type == "log_block"       and al["line_type"] == "log") or
            (region_type == "telemetry_block" and al["line_type"] == "telemetry") or
            (region_type == "prose"           and al["line_type"] == "prose")
        )
    )
    type_purity = dominant_count / total if region_type != "mixed" else 0.3

    # High consolidation (low relative block count) = high coherence
    block_count = sum(len(v) for v in all_blocks.values())
    raw_coherence = 1.0 - (block_count / max(1, total))
    block_coherence = max(0.0, min(1.0, raw_coherence))

    return round(type_purity * 0.7 + block_coherence * 0.3, 2)


# ---------------------------------
# Core
# ---------------------------------

def detect_context(
    lines: List[LineObject],
    kv_results: Optional[List[KVResult]] = None,
) -> ContextResult:
    kv_map: Dict[int, KVResult] = {}
    if kv_results:
        kv_map = {r["line_index"]: r for r in kv_results}

    annotated = [_classify_line(line, kv_map) for line in lines]

    # Single-pass grouping execution loop
    all_blocks = _group_all_blocks(annotated)

    log_blocks       = all_blocks["log"]
    telemetry_blocks = all_blocks["telemetry"]

    region_type = _dominant_type(annotated)
    block_count = sum(len(v) for v in all_blocks.values())
    confidence = _score_confidence(region_type, annotated, all_blocks)

    return ContextResult(
        region_type=region_type,
        annotated_lines=annotated,
        kv_results=kv_results,
        log_blocks=log_blocks,
        telemetry_blocks=telemetry_blocks,
        confidence=confidence,
        block_count=block_count,
    )


if __name__ == "__main__":
    import sys
    from analysis.structure_engine.line_model import build_line_model
    from analysis.structure_engine.pattern_extractor import extract_all_patterns
    from analysis.structure_engine.detectors.kv_detector import detect_all_kv

    print("\n" + "=" * 60)
    print("CONTEXT DETECTOR INTERACTIVE TEST")
    print("=" * 60)
    print("Paste text then Ctrl+D to process. 'exit' to quit.\n")

    while True:
        print("INPUT> ", end="", flush=True)
        try:
            raw = sys.stdin.read()
        except EOFError:
            break

        if raw.strip().lower() == "exit":
            print("Exiting.")
            break

        model   = build_line_model(raw)
        patterns = extract_all_patterns(model)
        kv      = detect_all_kv(model, patterns)
        result  = detect_context(model, kv)

        print("\n--- CONTEXT DETECTION ---\n")
        print(f"  region_type  : {result['region_type']}")
        print(f"  confidence   : {result['confidence']}")
        print(f"  block_count  : {result['block_count']}")
        print(f"  log_blocks   : {len(result['log_blocks'])}")
        print(f"  telemetry    : {len(result['telemetry_blocks'])}")
        print(f"\n  annotated lines:")
        for al in result["annotated_lines"]:
            extras = ""
            if al["kv_key"]:
                extras = f" → {al['kv_key']}: {al['kv_value']}"
            if al["log_level"]:
                extras = f" [{al['log_level']}]"
            print(f"    [{al['line_index']}] {al['line_type']:10} {repr(al['text'])[:50]}{extras}")

        print("-" * 60)
