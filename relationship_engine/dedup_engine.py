# relationship_engine/dedup_engine.py
#
# The duplicate engine, assembled. Runs the six stages end to end over a set of
# invoices and returns typed relationships with actions, plus the signal the
# approval recommender consumes.
#
#   1 Canonicalize   (canonicalize.py, inside InvoiceRecord)
#   2 Identity keys   (identity.py, inside InvoiceRecord)
#   3 Candidate gen   (candidates.py)
#   4 Evidence        (evidence.py)
#   5 Classify        (classify.py)
#   6 Policy → action (dedup_policy.py)
#
# The candidate SET is pluggable: pass a provider (BatchCandidateProvider now,
# RegistryCandidateProvider later) or let it default to batch mode over the given
# records. The five stages after candidate generation never change with the
# backing store.

from typing import Any, Dict, List, Optional

from relationship_engine.candidates import (
    InvoiceRecord, CandidateProvider, BatchCandidateProvider,
    generate_candidate_pairs,
)
from relationship_engine.evidence import gather_evidence
from relationship_engine.classify import classify_relationship
from relationship_engine.dedup_policy import (
    decide_action, summarize_for_approval, DEFAULT_POLICY,
)


def make_records(
    invoices: List[Dict[str, Any]],
    dayfirst: bool = False,
) -> List[InvoiceRecord]:
    """Wrap raw resolved-field dicts as InvoiceRecords. Each item is
    {"id": ..., "fields": {...}} or just {...fields...} (id auto-assigned)."""
    records: List[InvoiceRecord] = []
    for i, inv in enumerate(invoices):
        dt = inv.get("doc_type", "invoice") if isinstance(inv, dict) else "invoice"
        if "fields" in inv and "id" in inv:
            rid, fields = inv["id"], inv["fields"]
        elif "fields" in inv:
            rid, fields = f"doc_{i}", inv["fields"]
        else:
            rid, fields = inv.get("id", f"doc_{i}"), inv
        records.append(InvoiceRecord(str(rid), fields, dayfirst=dayfirst, doc_type=dt))
    return records


def analyze(
    records: List[InvoiceRecord],
    provider: Optional[CandidateProvider] = None,
    policy: Dict[str, str] = None,
) -> Dict[str, Any]:
    """
    Run the full engine over a set of records.

    provider: candidate source (defaults to batch mode over `records`).
    policy:   relationship-type → action overrides (defaults to DEFAULT_POLICY).

    Returns:
      {
        "relationships": [ <classified + action-decided pair>, ... ],
        "approval":      <summarize_for_approval output; feeds the recommender>,
        "summary":       {records, candidate_pairs, by_type, by_action},
      }
    """
    policy = policy or DEFAULT_POLICY
    if provider is None:
        provider = BatchCandidateProvider(records)

    pairs = generate_candidate_pairs(records, provider)

    decisions: List[Dict[str, Any]] = []
    by_type: Dict[str, int] = {}
    for a, b in pairs:
        ev = gather_evidence(a, b)
        rel = classify_relationship(ev, a, b)
        decision = decide_action(rel, policy)
        decisions.append(decision)
        by_type[rel["type"]] = by_type.get(rel["type"], 0) + 1

    approval = summarize_for_approval(decisions)

    return {
        "relationships": decisions,
        "approval": approval,
        "summary": {
            "records": len(records),
            "candidate_pairs": len(pairs),
            "by_type": by_type,
            "by_action": approval["counts"],
        },
    }


def analyze_invoices(
    invoices: List[Dict[str, Any]],
    dayfirst: bool = False,
    policy: Dict[str, str] = None,
) -> Dict[str, Any]:
    """Convenience: wrap raw invoice dicts and run the batch engine in one call —
    the 'dedupe this upload' entry point."""
    return analyze(make_records(invoices, dayfirst=dayfirst), policy=policy)
