# relationship_engine/core.py
#
# The Layer-1 floor of the Relationship Engine: normalize → identity →
# candidate generation → pair matching → relationship objects.
#
# Generic. Contains no domain knowledge. WHAT to match on and HOW to compare is
# supplied by a declarative relationship profile (see profiles/reconciliation.py
# for an example), exactly as field aliases and rules are profile data
# elsewhere in Qrynt. The engine interprets that data; it hard-codes nothing.

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from relationship_engine import (
    Relationship,
    MATCH, MISMATCH, LEFT_ONLY, RIGHT_ONLY,
)


# ─────────────────────────────────────────────────────────────────────────
# Normalization — canonicalize a value so equal things compare equal
# ─────────────────────────────────────────────────────────────────────────
#
# "Acme Ltd", "ACME LTD.", "acme  ltd" → one canonical form. "INV-001",
# "INV001", "inv 001" → one canonical form. Normalization is declarative: a
# profile names which normalizers to apply per key. Nothing here knows about
# invoices or vendors.

_WS_RE      = re.compile(r"\s+")
_NONALNUM_RE = re.compile(r"[^0-9a-z]")

def _n_lower(v: str) -> str:      return v.lower()
def _n_trim(v: str) -> str:       return v.strip()
def _n_collapse_ws(v: str) -> str: return _WS_RE.sub(" ", v)
def _n_alnum(v: str) -> str:      return _NONALNUM_RE.sub("", v)   # strip - . # spaces
def _n_strip_zeros(v: str) -> str:
    # normalize numeric-ish strings: "1,200.00" -> "1200", "100.0" -> "100"
    s = v.replace(",", "").strip()
    try:
        f = float(s)
        return str(int(f)) if f == int(f) else str(f)
    except ValueError:
        return v

_NORMALIZERS: Dict[str, Callable[[str], str]] = {
    "lower":       _n_lower,
    "trim":        _n_trim,
    "collapse_ws": _n_collapse_ws,
    "alnum":       _n_alnum,          # for IDs: INV-001 == INV001
    "number":      _n_strip_zeros,    # for amounts compared as identity
}


def normalize_value(value: Any, normalizers: List[str]) -> str:
    """Apply the named normalizers in order. Non-str values are stringified
    first. An unknown normalizer name is skipped (fails safe, not loud —
    a profile typo shouldn't crash a reconciliation run)."""
    s = "" if value is None else str(value)
    for name in normalizers:
        fn = _NORMALIZERS.get(name)
        if fn is not None:
            s = fn(s)
    return s


# ─────────────────────────────────────────────────────────────────────────
# Identity — the canonical key that decides candidacy
# ─────────────────────────────────────────────────────────────────────────

def identity_key(
    record: Dict[str, Any],
    key_spec: Dict[str, Any],
) -> Optional[str]:
    """
    Compute a record's identity key from the profile's key spec:
      { "field": "invoice_number", "normalizers": ["lower","alnum"] }
    Composite keys (match on several fields) are supported:
      { "fields": ["vendor","invoice_number"], "normalizers": [...] }
    Returns None if the key field(s) are absent — such a record cannot be a
    candidate and will surface as LEFT_ONLY / RIGHT_ONLY.
    """
    fields = record.get("fields", record)  # accept a Document View or a flat dict
    norms  = key_spec.get("normalizers", ["trim", "lower"])

    if "field" in key_spec:
        raw = fields.get(key_spec["field"])
        if raw is None:
            return None
        return normalize_value(raw, norms)

    if "fields" in key_spec:
        parts = []
        for f in key_spec["fields"]:
            raw = fields.get(f)
            if raw is None:
                return None
            parts.append(normalize_value(raw, norms))
        return "|".join(parts)

    return None


# ─────────────────────────────────────────────────────────────────────────
# Candidate generation — who could possibly match?
# ─────────────────────────────────────────────────────────────────────────
#
# Index each side by identity key. Only records sharing a key are candidates,
# which prunes the N² comparison space to the keys that actually coincide.
# For Layer 1 (1:1), a key mapping to >1 record on a side is a signal that this
# is NOT a clean 1:1 problem (it may need Group matching) — the engine reports
# that honestly rather than silently picking one.

def _index_by_identity(
    records: List[Dict[str, Any]],
    key_spec: Dict[str, Any],
) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]]:
    """Return (index: key -> [records], keyless: [records with no identity])."""
    index: Dict[str, List[Dict[str, Any]]] = {}
    keyless: List[Dict[str, Any]] = []
    for rec in records:
        k = identity_key(rec, key_spec)
        if k is None:
            keyless.append(rec)
        else:
            index.setdefault(k, []).append(rec)
    return index, keyless


# ─────────────────────────────────────────────────────────────────────────
# Consistency — reuse the Rules Engine to compare a matched pair
# ─────────────────────────────────────────────────────────────────────────
#
# Once two records are paired by identity, deciding whether they AGREE is just
# rules over their combined fields (a.total == b.amount within tolerance). We
# reuse the Rules Engine rather than reinventing comparison, and inherit its
# evidence and PASS/FAIL/SKIPPED semantics.

def _consistency_evidence(
    left: Dict[str, Any],
    right: Dict[str, Any],
    compare_rules: List[Dict[str, Any]],
) -> Tuple[bool, List[Dict[str, Any]], float]:
    """
    Evaluate compare_rules against a combined view whose fields are the left
    and right records namespaced as a.* and b.*. Returns
    (consistent, evidence, confidence).

    A rule looks like a normal Rules Engine rule but references a.<field> /
    b.<field>:
      {"id":"amount_matches","type":"compare","op":"==",
       "left":"a.total","right":"b.amount","tolerance":0.01}
    """
    from analysis.rules_engine import evaluate, PASS, FAIL, SKIPPED

    combined = {"fields": {}}
    for k, v in (left.get("fields", left) or {}).items():
        combined["fields"][f"a.{k}"] = v
    for k, v in (right.get("fields", right) or {}).items():
        combined["fields"][f"b.{k}"] = v

    report = evaluate(combined, compare_rules)
    evidence = [
        {"rule": r["rule_id"], "status": r["status"], **r.get("evidence", {})}
        for r in report["results"]
    ]
    total    = report["summary"]["total"] or 1
    passed   = report["summary"]["passed"]
    consistent = report["summary"]["failed"] == 0
    confidence = round(passed / total, 4)
    return consistent, evidence, confidence


# ─────────────────────────────────────────────────────────────────────────
# Pair matching (Layer 1) — the public capability
# ─────────────────────────────────────────────────────────────────────────

def match_pairs(
    source_a: List[Dict[str, Any]],
    source_b: List[Dict[str, Any]],
    key_spec: Dict[str, Any],
    compare_rules: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Layer-1 1:1 matching. Returns relationship objects plus a note of any keys
    that were not 1:1 (a signal the data may need Group matching — deferred).

    Output:
      {
        "relationships": [ Relationship.to_dict(), ... ],
        "summary": {matched, mismatched, left_only, right_only, exceptions},
        "not_one_to_one": [ {key, a_count, b_count}, ... ],
      }
    """
    idx_a, keyless_a = _index_by_identity(source_a, key_spec)
    idx_b, keyless_b = _index_by_identity(source_b, key_spec)

    relationships: List[Relationship] = []
    not_1to1: List[Dict[str, Any]] = []

    all_keys = list(dict.fromkeys(list(idx_a.keys()) + list(idx_b.keys())))
    for key in all_keys:
        a_recs = idx_a.get(key, [])
        b_recs = idx_b.get(key, [])

        # Not a clean 1:1 for this key → flag for Group matching, do not guess.
        if len(a_recs) > 1 or len(b_recs) > 1:
            not_1to1.append({"key": key, "a_count": len(a_recs), "b_count": len(b_recs)})
            continue

        if a_recs and b_recs:
            consistent, evidence, conf = _consistency_evidence(
                a_recs[0], b_recs[0], compare_rules)
            relationships.append(Relationship(
                relationship=(MATCH if consistent else MISMATCH),
                key=key, left=a_recs[0], right=b_recs[0],
                confidence=conf, evidence=evidence,
            ))
        elif a_recs:
            relationships.append(Relationship(
                relationship=LEFT_ONLY, key=key, left=a_recs[0], confidence=1.0))
        else:
            relationships.append(Relationship(
                relationship=RIGHT_ONLY, key=key, right=b_recs[0], confidence=1.0))

    # keyless records can't be matched — surface them as one-sided exceptions
    for rec in keyless_a:
        relationships.append(Relationship(
            relationship=LEFT_ONLY, key=None, left=rec, confidence=1.0,
            evidence=[{"note": "no identity key"}]))
    for rec in keyless_b:
        relationships.append(Relationship(
            relationship=RIGHT_ONLY, key=None, right=rec, confidence=1.0,
            evidence=[{"note": "no identity key"}]))

    matched    = sum(1 for r in relationships if r.relationship == MATCH)
    mismatched = sum(1 for r in relationships if r.relationship == MISMATCH)
    left_only  = sum(1 for r in relationships if r.relationship == LEFT_ONLY)
    right_only = sum(1 for r in relationships if r.relationship == RIGHT_ONLY)

    return {
        "relationships": [r.to_dict() for r in relationships],
        "summary": {
            "matched":    matched,
            "mismatched": mismatched,
            "left_only":  left_only,
            "right_only": right_only,
            "exceptions": mismatched + left_only + right_only,
            "total_a":    len(source_a),
            "total_b":    len(source_b),
        },
        "not_one_to_one": not_1to1,
    }
