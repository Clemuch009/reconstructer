import asyncio
import sys
from typing import List, Optional, Dict, Any, Tuple

from core.state import EngineState

# preprocess
from preprocess.normalize import normalize_text
from preprocess.sanitize import sanitize_text

# signals
from preprocess.regex_signals import extract_signals
from hints.text_signals import extract_text_signals
from hints.token_observations import extract_token_observations
from hints.spacy_adapter import extract_spacy_hints
from hints.boundary_signals import extract_boundary_signals

# contracts
from core.signals_contract import validate_all_signals, validate_features

# analysis
from analysis.structural_metrics import compute_structural_metrics
from analysis.features import build_feature_vector

# decision
from core.decision import compute_splits

# segmentation
from core.segmentation import reconstruct_paragraphs

# pre-classifier
from pre_classifier import (
    pre_classify_document,
    process_document,
    IngestionEnvelope,
)

# structure engine
from analysis.structure_engine.orchestrator import run_structure_engine

# postprocess
from postprocess.formatter import (
    format_segments,
    PostprocessOutput,
    StructuredDocument,
)
from postprocess.cleanup import cleanup
from postprocess.validation import validate

# classifier result type
from analysis.structure_engine.classifier import ClassificationResult

# adapters
from adapters.human   import HumanAdapter
from adapters.ai      import AIAdapter
from adapters.machine import MachineAdapter

# ---------------------------------
# Prose fallback classification (used on processing failure)
# ---------------------------------

_PROSE_FALLBACK: ClassificationResult = {
    "region_type":               "classified_segment",
    "structure_type":            "prose",
    "classification_confidence": 0.0,
    "dominant_source":           "none",
    "block_count":               0,
    "dominant_coverage_lines":   0,
    "type_entropy":              0.0,
    "evidence":                  {},
    "density_profile":           {},
}


# ---------------------------------
# ENGINE
# ---------------------------------

class TextReconstructionEngine:

    def __init__(self):
        pass

    # ---------------------------------
    # Internal — core segmentation pipeline
    # ---------------------------------

    def _run_core_pipeline(self, text: str) -> List[str]:
        """
        Runs preprocess → signals → metrics → features → decision → segmentation.
        Returns clean List[str] segments.
        """
        state = EngineState(text)

        # Preprocess
        normalized = normalize_text(state.raw_text)
        state.set_normalized(normalized)

        cleaned = sanitize_text(state.normalized_text)
        state.set_cleaned(cleaned)

        active_text = state.get_active_text()

        # Signal extraction
        regex_signals_list = extract_signals(active_text)

        signals = {
            "regex":    {"signals": regex_signals_list},
            "text":     extract_text_signals(active_text),
            "token":    extract_token_observations(active_text),
            "spacy":    extract_spacy_hints(active_text),
            "boundary": extract_boundary_signals(active_text),
        }

        # Gate 1
        validate_all_signals(signals, len(active_text))
        state.set_signals(signals)

        # Metrics
        metrics = compute_structural_metrics(signals, len(active_text))
        state.set_metrics(metrics)

        # Features
        features = build_feature_vector(metrics)

        # Gate 2
        validate_features(features)
        state.set_features(features)

        # Decision
        splits = compute_splits(signals, features, active_text)
        state.set_splits(splits)

        # Segmentation
        segments = reconstruct_paragraphs(active_text, splits)
        state.set_segments(segments)

        return segments

    # ---------------------------------
    # Internal — structure engine pipeline
    # ---------------------------------

    def _run_structure_pipeline(
        self,
        segments: List[str],
    ) -> Tuple[List[ClassificationResult], List[List[Any]], List[List[Any]]]:
        """
        Routes segments through pre_classifier then structure_engine.
        CPU-bound parsing offloaded to thread pool — non-blocking event loop.

        Returns three parallel, document-ordered lists:
        - classifications : ClassificationResult per segment
        - block_sets      : reconciled StructuredBlock list per segment ([] for bypass)
        - line_sets       : segment LineObject list per segment ([] for bypass)
        The block/line sets feed the formatter so it packages pre-computed
        detector output instead of re-parsing raw text.
        """
        envelopes = pre_classify_document(segments)

        async def _processor(payload: str, route: str) -> Dict[str, Any]:
            """
            Route each segment to appropriate processing path.
            Returns a wrapper: {classification, blocks, lines}.
            Bypass and empty paths carry empty blocks/lines (formatter then
            uses its text fallback for those segments).
            """
            if route in ("PASS_THROUGH", "JSON_NATIVE"):
                # Fast path — skip structure engine entirely
                classification = {
                    "region_type":               "classified_segment",
                    "structure_type":            "prose" if route == "PASS_THROUGH" else "kv_block",
                    "classification_confidence": 1.0,
                    "dominant_source":           "pre_classifier_bypass",
                    "block_count":               1,
                    "dominant_coverage_lines":   len(payload.split("\n")),
                    "type_entropy":              0.0,
                    "evidence":                  {},
                    "density_profile":           {},
                }
                return {"classification": classification, "blocks": [], "lines": []}

            # Structure engine path — offload to thread pool
            # Prevents CPU-bound parsing from blocking the event loop
            loop    = asyncio.get_running_loop()
            results = await loop.run_in_executor(
                None, run_structure_engine, payload
            )

            if not results:
                return {"classification": dict(_PROSE_FALLBACK), "blocks": [], "lines": []}

            # Fan-out guard — with the current segmenter one engine-segment maps
            # to exactly one sub-segment. If that assumption is ever violated,
            # fail LOUD rather than silently dropping the extra sub-segments.
            if len(results) > 1:
                print(
                    f"[engine][WARN] segment fanned out into {len(results)} "
                    f"sub-segments; using highest-confidence sub-segment, others "
                    f"not represented. Fan-out handling not implemented.",
                    file=sys.stderr,
                )

            # Pick highest confidence sub-segment result
            best = max(
                results,
                key=lambda r: r["classification"]["classification_confidence"]
            )
            return {
                "classification": best["classification"],
                "blocks":         best["blocks"],
                "lines":          best["lines"],
            }

        # Run async — sequence_index guarantees document order on reassembly
        processed = asyncio.run(
            process_document(envelopes, _processor)
        )

        # Extract three parallel, document-ordered lists from the wrappers.
        classifications: List[ClassificationResult] = []
        block_sets:      List[List[Any]]            = []
        line_sets:       List[List[Any]]            = []

        for env in processed:
            res = env["result"]
            if isinstance(res, dict) and "classification" in res:
                classifications.append(res["classification"])
                block_sets.append(res.get("blocks", []))
                line_sets.append(res.get("lines", []))
            else:
                # Processing failure (e.g. {"error": ...}) — prose fallback,
                # no blocks. Keeps the three lists aligned with segments.
                classifications.append(dict(_PROSE_FALLBACK))
                block_sets.append([])
                line_sets.append([])

        return classifications, block_sets, line_sets

    # ---------------------------------
    # Internal — postprocess pipeline
    # ---------------------------------

    def _run_postprocess(
        self,
        segments:        List[str],
        classifications: List[ClassificationResult],
        raw_line_count:  int,
        block_sets:      Optional[List[List[Any]]] = None,
        line_sets:       Optional[List[List[Any]]] = None,
    ) -> PostprocessOutput:
        """
        Runs formatter → cleanup → validation.
        block_sets / line_sets thread the pre-computed detector output into the
        formatter so it packages real blocks (nested KV, real hierarchy nodes,
        Option C mixed) instead of re-parsing raw text.
        """
        formatted = format_segments(
            segments,
            classifications,
            block_sets=block_sets,
            line_sets=line_sets,
        )
        cleaned   = cleanup(formatted)
        validated = validate(cleaned, raw_line_count)
        return validated

    # ---------------------------------
    # Public API
    # ---------------------------------

    def run(self, text: str) -> PostprocessOutput:
        """
        Full pipeline execution.

        Flow:
        normalize → sanitize → signals → metrics → features → decision → segmentation
            ↓
        pre_classifier (L0 routing)
            ↓
        structure_engine (thread-isolated, async, ordered reassembly)
            ↓
        formatter → cleanup → validation
            ↓
        PostprocessOutput

        raw_line_count derived from cleaned segments — not raw input.
        Prevents false line conservation failures from preprocess normalization.
        """
        if not text or not isinstance(text, str):
            raise TypeError("Input must be a non-empty string")

        # Stage 1 — core segmentation
        segments = self._run_core_pipeline(text)

        if not segments:
            return PostprocessOutput(
                region_type="classified_document",
                human_readable="",
                machine_readable=StructuredDocument(segments=[]),
                validation=None,
            )

        # Fix B — raw_line_count from cleaned segments not raw input
        # Prevents false conservation failures from preprocess normalization
        cleaned_text = "\n".join(segments)
        raw_line_count = len(cleaned_text.split("\n"))

        # Stage 2 — structure engine with pre-classifier routing
        classifications, block_sets, line_sets = self._run_structure_pipeline(segments)

        # Stage 3 — postprocess
        output = self._run_postprocess(
            segments,
            classifications,
            raw_line_count,
            block_sets=block_sets,
            line_sets=line_sets,
        )

        return output

    def run_text(self, text: str) -> str:
        """
        Convenience method — returns human_readable string only.
        Backward compatible with original callers expecting plain text.
        """
        return self.run(text)["human_readable"]

    def run_human(self, text: str) -> str:
        """
        Full pipeline + human-readable terminal output.
        """
        output = self.run(text)
        return HumanAdapter().adapt(output)

    def run_ai(
        self,
        text: str,
        include_confidence: bool = False,
    ) -> dict:
        """
        Full pipeline + Logical Document Model for LLM consumption.
        """
        output = self.run(text)
        return AIAdapter(include_confidence=include_confidence).adapt(output)

    def run_machine(self, text: str) -> list:
        """
        Full pipeline + flat analytics records for DB ingestion.
        """
        output = self.run(text)
        return MachineAdapter().adapt(output)


# ---------------------------------
# INTERACTIVE TEST
# ---------------------------------

if __name__ == "__main__":
    import sys
    import json

    print("\n" + "=" * 60)
    print("TEXT RECONSTRUCTION ENGINE — FULL PIPELINE")
    print("=" * 60)
    print("Commands after input:")
    print("  [h] human readable (raw)")
    print("  [H] human adapter (formatted terminal)")
    print("  [m] machine readable (raw segments)")
    print("  [M] machine adapter (analytics records)")
    print("  [a] AI adapter (Logical Document Model)")
    print("  [v] validation report")
    print("  [A] all outputs\n")

    engine = TextReconstructionEngine()

    while True:
        print("INPUT> ", end="", flush=True)
        try:
            raw = sys.stdin.read()
        except EOFError:
            break

        if raw.strip().lower() == "exit":
            print("Exiting.")
            break

        raw = raw.replace("\\n", "\n")

        print("VIEW [h/H/m/M/a/v/A]> ", end="", flush=True)
        try:
            view = input().strip().lower() or "h"
        except EOFError:
            view = "h"

        try:
            result = engine.run(raw)
            with open("result.json", "w") as f:
                json.dump(result["machine_readable"], f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"ERROR: {e}")
            print("-" * 60)
            continue

        # Raw human readable
        if view in ("h", "a"):
            print("\n--- HUMAN READABLE (raw) ---")
            print(result["human_readable"])

        # Human adapter
        if view in ("H", "a"):
            print("\n--- HUMAN ADAPTER (formatted terminal) ---")
            try:
                print(engine.run_human.__func__(engine, raw) if view == "a" else HumanAdapter().adapt(result))
            except Exception as e:
                print(f"  [ERROR] {e}")

        # Raw machine readable
        if view in ("m", "a"):
            print("\n--- MACHINE READABLE (raw segments) ---")
            for seg in result["machine_readable"]["segments"]:
                print(f"  [{seg['segment_id']}] type={seg['type']}")
                print(f"    flags   : {seg['metadata'].get('flags', [])}")
                content_preview = json.dumps(
                    seg["content"], indent=6, default=str
                )[:300]
                print(f"    content : {content_preview}")

        # Machine adapter
        if view in ("M", "a"):
            print("\n--- MACHINE ADAPTER (analytics records) ---")
            try:
                records = MachineAdapter().adapt(result)
                for rec in records:
                    print(f"  [{rec['segment_id']}] type={rec['type']}"
                          f"  conf={rec['confidence']:.2f}"
                          f"  label={rec['section_label']}")
                    content_preview = json.dumps(
                        rec["content"], default=str
                    )[:200]
                    print(f"    content: {content_preview}")
            except Exception as e:
                print(f"  [ERROR] {e}")

        # AI adapter
        if view in ("a",):
            print("\n--- AI ADAPTER (Logical Document Model) ---")
            try:
                ldm = AIAdapter(include_confidence=True).adapt(result)
                print(f"  is_valid : {ldm['is_valid']}")
                print(f"  blocks   : {len(ldm['blocks'])}")
                for block in ldm["blocks"]:
                    print(f"    type={block['type']:12}"
                          f"  tags={block['tags']}"
                          f"  label={block['label']}"
                          f"  conf={block['confidence']}")
            except Exception as e:
                print(f"  [ERROR] {e}")

        # Validation
        if view in ("v", "a"):
            v = result["validation"]
            if v:
                print("\n--- VALIDATION ---")
                print(f"  is_valid      : {v['is_valid']}")
                print(f"  quality_score : {v['quality_score']}")
                print(f"  checks        : {v['checks']}")
                print(f"  doc_flags     : {v['document_flags']}")
                print(f"  anomalies     : {len(v['anomalies'])}")
                for a in v["anomalies"]:
                    print(f"    [{a['severity']}] {a['code']}: {a['message']}")

        print("-" * 60)
