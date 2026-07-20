# relationship_engine/reconcile.py
#
# Document-to-document reconciliation: takes two Document Views and produces a
# multi-dimensional Verdict. This is the single edge of the reconciliation
# graph — two documents, reconciled across independent dimensions.
#
# Dimensions computed here:
#   Identity   — fact-based weighted edge over declared identity fields
#   Financial  — declared financial rules via the Rules Engine (order-agnostic)
#   Structural — line alignment mapping (relationship_engine.structural)
#   Business   — quantity/fulfillment discrepancies read off the alignment
#   Compliance — NOT_CHECKED (needs company policy data — fail-closed)
#
# All WHAT/HOW is declarative profile data; this module is generic.

from typing import Any, Dict, List

from relationship_engine.verdict import (
    Verdict, IDENTITY, FINANCIAL, STRUCTURAL, BUSINESS, COMPLIANCE,
    PASS, FAIL, NOT_CHECKED,
)
from relationship_engine.structural import align_lines, _num


# ── Identity: the fact-based weighted edge ─────────────────────────────────
#
# The edge is NOT one key. It is a weighted bundle of facts (invoice_number
# 0.6 + amount 0.2 + currency 0.1 + vendor 0.1). A missing field weakens the
# edge; other facts still carry it. Edge holds if evidence >= threshold.

def _edge_identity(a_fields, b_fields, spec):
    weights = spec.get("weights", {})          # {a_field: {"b": b_field, "w": w}}
    threshold = spec.get("threshold", 0.7)
    # A "keystone" field is one whose EXACT match is sufficient on its own to
    # establish that two documents are the same transaction — a purchase-order
    # reference, a contract number. An invoice and its PO have DIFFERENT document
    # numbers by nature, so identity cannot require them to match; what links
    # them is the invoice quoting the PO's reference. When that reference agrees,
    # the documents are linked, and a disagreement on a corroborating field
    # (vendor spelled differently, currency absent) becomes a WARNING surfaced in
    # the evidence — not an identity failure that hides the fact they are related.
    keystones = set(spec.get("keystone", []))
    threshold = spec.get("threshold", 0.7)
    total_w = got_w = 0.0
    evidence = []
    keystone_hit = False
    disagreements = []
    for a_field, m in weights.items():
        b_field = m["b"]; w = float(m["w"])
        if a_field in a_fields and b_field in b_fields:
            total_w += w
            an, bn = _num(a_fields[a_field]), _num(b_fields[b_field])
            if an is not None and bn is not None:
                agrees = abs(an - bn) <= float(m.get("tolerance", 0.001))
            else:
                agrees = str(a_fields[a_field]).strip().lower() == str(b_fields[b_field]).strip().lower()
            if agrees:
                got_w += w
                if a_field in keystones:
                    keystone_hit = True
            else:
                disagreements.append(a_field)
            evidence.append({"a_field": a_field, "b_field": b_field, "weight": w,
                             "agrees": agrees, "a": a_fields[a_field], "b": b_fields[b_field]})
        else:
            evidence.append({"a_field": a_field, "b_field": b_field, "weight": w,
                             "agrees": None, "note": "field absent on a side"})
    score = round(got_w / total_w, 4) if total_w > 0 else 0.0

    # A matched keystone establishes identity regardless of the weighted score,
    # but any corroborating-field disagreement is recorded as a warning so a
    # vendor mismatch on linked documents is never silently dropped.
    if keystone_hit:
        status = PASS
        for f in disagreements:
            for e in evidence:
                if e["a_field"] == f:
                    e["warning"] = (f"{f} disagrees on documents linked by a "
                                    f"matching reference — review the parties")
    else:
        status = PASS if score >= threshold else FAIL
    return status, score, evidence


# ── Financial: declared rules via the Rules Engine ─────────────────────────

def _financial(a_fields, b_fields, rules):
    if not rules:
        return NOT_CHECKED, []
    from analysis.rules_engine import evaluate
    combined = {"fields": {}}
    for k, v in a_fields.items(): combined["fields"][f"a.{k}"] = v
    for k, v in b_fields.items(): combined["fields"][f"b.{k}"] = v
    report = evaluate(combined, rules)
    evidence = [{"rule": r["rule_id"], "status": r["status"], **r.get("evidence", {})}
                for r in report["results"]]
    status = FAIL if report["summary"]["failed"] > 0 else PASS
    return status, evidence


# ── Structural + Business from the line alignment ──────────────────────────

def _structural_and_business(a_tables, b_tables, struct_profile):
    line_key = struct_profile.get("line_table", "line_items")
    a_lines = a_tables.get(line_key, [])
    b_lines = b_tables.get(line_key, [])
    if not a_lines and not b_lines:
        return (NOT_CHECKED, [], {}), (NOT_CHECKED, [], {})

    alignment = align_lines(a_lines, b_lines, struct_profile)
    s = alignment["summary"]

    # Structural: did the lines correspond? Unmatched lines on either side fail
    # the structural dimension (something didn't align — review it).
    struct_status = PASS if (s["unmatched_left"] == 0 and s["unmatched_right"] == 0) else FAIL
    struct_detail = dict(alignment)

    # Enrich unmatched lines with their actual content, and detect NEAR-MISSES:
    # an unmatched line on A and one on B that describe the SAME item (matching
    # description) but failed to align — almost always a value conflict (e.g.
    # unit price differs). Reporting "the Dell Monitor's price differs, $380 vs
    # $350" is actionable; "1 unmatched on each side" is not.
    id_fields = struct_profile.get("identity", ["description"])
    def _sig(line):
        from relationship_engine.structural import _canonical_line, _identity_signature
        return _identity_signature(_canonical_line(line, struct_profile.get("columns", {})), id_fields)

    near_misses = []
    used_l, used_r = set(), set()
    for li in alignment["unmatched_left"]:
        for rj in alignment["unmatched_right"]:
            if rj in used_r:
                continue
            if _sig(a_lines[li]) is not None and _sig(a_lines[li]) == _sig(b_lines[rj]):
                # same item — find which field(s) differ
                diffs = []
                for col in struct_profile.get("columns", {}):
                    av = _get_ci(a_lines[li], col); bv = _get_ci(b_lines[rj], col)
                    if av is not None and bv is not None and str(av).strip() != str(bv).strip():
                        diffs.append({"field": col, "a": av, "b": bv})
                near_misses.append({
                    "a_index": li, "b_index": rj,
                    "description": _get_ci(a_lines[li], "description"),
                    "conflicts": diffs,
                })
                used_l.add(li); used_r.add(rj)
                break

    struct_detail["near_misses"] = near_misses
    struct_detail["unmatched_left_lines"]  = [
        {"index": i, "line": a_lines[i]} for i in alignment["unmatched_left"] if i not in used_l
    ]
    struct_detail["unmatched_right_lines"] = [
        {"index": j, "line": b_lines[j]} for j in alignment["unmatched_right"] if j not in used_r
    ]

    # Business: for aligned pairs, did quantities/fulfillment agree? A matched
    # pair whose quantity differs is a fulfillment discrepancy (partial
    # delivery), even if identity/financial pass.
    qty_field = struct_profile.get("quantity_field", "quantity")
    biz_evidence = []
    biz_fail = False
    for m in alignment["matched"]:
        a_line = a_lines[m["a_index"]]
        b_line = b_lines[m["b_index"]]
        qa = _num(_get_ci(a_line, qty_field))
        qb = _num(_get_ci(b_line, qty_field))
        if qa is not None and qb is not None and abs(qa - qb) > 0.001:
            biz_fail = True
            biz_evidence.append({"a_index": m["a_index"], "b_index": m["b_index"],
                                 "quantity_a": qa, "quantity_b": qb,
                                 "difference": round(qb - qa, 4),
                                 "note": "quantity discrepancy (possible partial fulfillment)"})
    # unmatched lines are also a business concern (something ordered/billed absent)
    if s["unmatched_left"] or s["unmatched_right"]:
        biz_evidence.append({
            "unmatched_left":  alignment["unmatched_left"],
            "unmatched_right": alignment["unmatched_right"],
            "unmatched_left_desc":  [_get_ci(a_lines[i], "description") for i in alignment["unmatched_left"]],
            "unmatched_right_desc": [_get_ci(b_lines[j], "description") for j in alignment["unmatched_right"]],
            "note": "lines with no counterpart",
        })
        biz_fail = True
    biz_status = FAIL if biz_fail else PASS

    return (struct_status, [], struct_detail), (biz_status, biz_evidence, {})


def _get_ci(d, field):
    """Case-insensitive dict get."""
    if field in d: return d[field]
    lf = field.lower()
    for k, v in d.items():
        if str(k).lower() == lf:
            return v
    return None


# ── public API ─────────────────────────────────────────────────────────────

def reconcile_documents(
    view_a: Dict[str, Any],
    view_b: Dict[str, Any],
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Reconcile two Document Views into a multi-dimensional Verdict.

    profile (declarative):
      "identity":   {"weights": {a_field: {"b": b_field, "w": .., "tolerance": ..}},
                     "threshold": ..}
      "financial":  [ rules referencing a.<f> / b.<f> ]
      "structural": {"line_table","columns","identity","weights",
                     "quantity_field"}
      (compliance intentionally omitted → NOT_CHECKED)
    """
    a_fields = view_a.get("fields", {})
    b_fields = view_b.get("fields", {})
    a_tables = view_a.get("tables", {})
    b_tables = view_b.get("tables", {})

    v = Verdict()

    # Identity
    id_spec = profile.get("identity")
    if id_spec:
        status, score, ev = _edge_identity(a_fields, b_fields, id_spec)
        v.set(IDENTITY, status, evidence=ev, detail={"edge_score": score})

    # Financial
    fin_status, fin_ev = _financial(a_fields, b_fields, profile.get("financial", []))
    if fin_status != NOT_CHECKED:
        v.set(FINANCIAL, fin_status, evidence=fin_ev)

    # Structural + Business
    struct_profile = profile.get("structural")
    if struct_profile:
        (s_status, s_ev, s_detail), (b_status, b_ev, b_detail) = \
            _structural_and_business(a_tables, b_tables, struct_profile)
        if s_status != NOT_CHECKED:
            v.set(STRUCTURAL, s_status, evidence=s_ev, detail=s_detail)
        if b_status != NOT_CHECKED:
            v.set(BUSINESS, b_status, evidence=b_ev, detail=b_detail)

    # Compliance stays NOT_CHECKED (needs company policy data) — fail-closed.
    return v.to_dict()
