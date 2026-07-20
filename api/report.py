"""The report layer — the application's answer, not the engine's output.

Every analysis endpoint (process, reconcile, dedupe, validate) produces rich,
correct evidence, but in four different vocabularies: validation rules,
verification regions, reconciliation dimensions, dedup relationships. A human —
an AP clerk — should not have to read four structures to learn one thing:
*what happened, do I need to act, and what do I do?*

This module answers that. It reads an engine response and produces a single
`report` with a uniform shape, regardless of which engine produced the response:

    {
      "status":         needs_review | clear | blocked | derived | error
      "confidence":     0..1
      "headline":       one plain sentence a person reads first
      "findings":       [ {severity, category, what, detail, source} ]   ranked
      "recommendation": {action, reason}
    }

The engine outputs are NOT recomputed or altered — the report only reads them.
It is deliberately the *application's* output: the report can be improved over
time without touching a single engine, and no integration breaks because the
raw `results` travel unchanged alongside it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# ── severity & status vocabulary ─────────────────────────────────────────────
# Severity orders findings for the human. Status is the document-level verdict.
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2, "info": 3}

HIGH, MEDIUM, LOW, INFO = "high", "medium", "low", "info"


def _doc_label(doc_id) -> str:
    """A readable name for a prior document id like 'orig.pdf·06936da889'."""
    if not doc_id:
        return ""
    s = str(doc_id)
    # strip the checksum suffix after the middot
    for sep in ("\u00b7", "·"):
        if sep in s:
            s = s.split(sep)[0]
            break
    return s


def _money(v) -> str:
    """Format a monetary value for a human sentence."""
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def _finding(severity: str, category: str, what: str,
             detail: str = "", source: str = "") -> Dict[str, Any]:
    return {"severity": severity, "category": category, "what": what,
            "detail": detail, "source": source}


# ── source readers: each maps ONE engine vocabulary into unified findings ─────
#
# Every reader is defensive: a missing key means that engine did not run, not an
# error. None of them recompute anything — they translate what is already there.


def _from_verification(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The money-structure verdict: over-billing, derived totals, per-region."""
    out: List[Dict[str, Any]] = []
    vt = (resp.get("verification") or {}).get("total") or {}
    verdict = vt.get("verdict")
    if verdict == "CONFLICT":
        out.append(_finding(
            HIGH, "financial",
            "The stated total does not match the document's own arithmetic",
            vt.get("evidence", ""), "arithmetic verification"))
    elif vt.get("total_is_derived"):
        out.append(_finding(
            MEDIUM, "financial",
            "No total was stated — Qrynt computed one from the line items",
            vt.get("total_note", ""), "arithmetic verification"))
    # surface any individual region that failed, even under an overall verdict
    for r in vt.get("regions", []):
        if not r.get("ok") and not r.get("derived"):
            out.append(_finding(
                HIGH, "financial",
                f"A money area does not balance: {r.get('region','')}",
                f"stated {r.get('stated')}, expected {r.get('expected')}",
                "arithmetic verification"))
    return out


def _from_validation(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Required-field and rule checks: what the document is missing."""
    out: List[Dict[str, Any]] = []
    results = (resp.get("validation") or {}).get("results") or []
    for r in results:
        if r.get("status") != "FAIL":
            continue
        rid = r.get("rule_id", "")
        # required-field misses are the common, plain case
        if rid.endswith("_required"):
            field = rid[:-9].replace("_", " ")
            out.append(_finding(
                MEDIUM, "completeness",
                f"Missing {field}",
                "a required field could not be extracted from the document",
                "field validation"))
        elif rid == "totals_balance":
            # already covered by verification's CONFLICT; keep as low to avoid
            # double-shouting the same fact
            continue
        else:
            out.append(_finding(
                LOW, "rule", f"Check failed: {rid.replace('_',' ')}",
                r.get("reason") or "", "field validation"))
    return out


def _from_reconcile(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The 5-dimension reconciliation verdict and any warnings within it."""
    out: List[Dict[str, Any]] = []
    dims = (resp.get("verdict") or {}).get("dimensions") or {}

    # Each dimension becomes a SPECIFIC, plain-language finding that says WHAT and
    # WHY with the actual numbers — not a generic "does not agree financially"
    # restatement of the dimension name. A finding a person cannot act on is
    # noise; the whole point of the report is to explain, not to label.

    # FINANCIAL — the invoice exceeds the authorised PO amount, with the figures.
    fin = dims.get("financial") or {}
    if fin.get("status") == "FAIL":
        inv_amt = po_amt = None
        for e in fin.get("evidence", []):
            if isinstance(e, dict) and e.get("rule") == "invoice_within_po" and e.get("status") == "FAIL":
                inv_amt, po_amt = e.get("left"), e.get("right")
        if inv_amt is not None and po_amt is not None:
            over = round(inv_amt - po_amt, 2)
            out.append(_finding(
                HIGH, "financial",
                f"The invoice bills more than the purchase order authorises",
                f"Invoice total {_money(inv_amt)}, but the PO authorises only "
                f"{_money(po_amt)} — {_money(over)} over.",
                "invoice vs purchase order"))

    # STRUCTURAL — describe the line differences concretely.
    st = dims.get("structural") or {}
    if st.get("status") == "FAIL":
        det = st.get("detail") or {}
        # a matched pair that disagrees on price is the sharpest signal
        priced = None
        for m in det.get("matched", []):
            bad = [e for e in m.get("evidence", []) if not e.get("agrees")]
            if bad:
                priced = bad[0]; break
        if priced:
            out.append(_finding(
                HIGH, "billing",
                "You are being charged a different price than you agreed",
                f"An item's {priced.get('field','price')} on the invoice "
                f"({priced.get('a')}) does not match the price on the purchase "
                f"order ({priced.get('b')}).",
                "invoice vs purchase order"))
        um_l = det.get("unmatched_left") or []
        um_r = det.get("unmatched_right") or []
        if um_l:
            out.append(_finding(
                HIGH, "billing",
                "You are being billed for items that were not on the order",
                f"{len(um_l)} item(s) appear on the invoice but were not on the "
                f"purchase order — confirm they were actually ordered before paying.",
                "invoice vs purchase order"))
        if um_r:
            out.append(_finding(
                MEDIUM, "delivery",
                "Some ordered items are not on this invoice",
                f"{len(um_r)} item(s) on the purchase order were not billed here "
                f"— this may be a partial delivery, or an invoice still to come.",
                "invoice vs purchase order"))

    # BUSINESS — surface the actual note (quantity discrepancy, etc.), not a
    # generic "a business rule flagged a discrepancy".
    bus = dims.get("business") or {}
    if bus.get("status") == "FAIL":
        notes = []
        for e in bus.get("evidence", []):
            if isinstance(e, dict) and e.get("note"):
                notes.append(e["note"])
        # skip pure "lines with no counterpart" — already covered by structural
        notes = [n for n in notes if "no counterpart" not in n.lower()]
        if notes:
            out.append(_finding(
                MEDIUM, "quantities",
                "A quantity or delivery detail differs between the documents",
                "; ".join(dict.fromkeys(notes)) + ".", "business rules"))

    # IDENTITY — a genuine party/transaction mismatch, phrased as why.
    idn = dims.get("identity") or {}
    if idn.get("status") == "FAIL":
        vmis = None
        for e in idn.get("evidence", []):
            if isinstance(e, dict) and e.get("a_field") == "vendor" and e.get("agrees") is False:
                vmis = (e.get("a"), e.get("b"))
        if vmis:
            out.append(_finding(
                MEDIUM, "parties",
                "The invoice and PO name different suppliers",
                f"Invoice: {vmis[0]} · PO: {vmis[1]} — confirm they are the "
                f"same transaction.", "party comparison"))
        else:
            out.append(_finding(
                MEDIUM, "parties",
                "The documents could not be confirmed as the same transaction",
                "No shared reference linked them.", "party comparison"))

    # a vendor WARNING on otherwise-linked docs (keystone matched) — low, phrased plainly
    for dim, v in dims.items():
        for e in (v.get("evidence") or []):
            if isinstance(e, dict) and e.get("warning"):
                out.append(_finding(
                    LOW, "parties",
                    "Supplier names differ on documents linked by a matching PO",
                    f"{e.get('a')} vs {e.get('b')} — worth confirming the parties.",
                    "party comparison"))
    return out


def _reconcile_detail(dim: str, v: Dict[str, Any]) -> str:
    """Best available human detail for a failed reconciliation dimension."""
    det = v.get("detail") or {}
    if dim == "structural":
        um_l = det.get("unmatched_left") or []
        um_r = det.get("unmatched_right") or []
        matched = det.get("matched") or []
        # report matched-pair disagreements (e.g. price changed) first
        for m in matched:
            bad = [e for e in m.get("evidence", []) if not e.get("agrees")]
            if bad:
                e = bad[0]
                return (f"{e.get('field','a field')} differs on a matched line: "
                        f"{e.get('a')} vs {e.get('b')}")
        if um_l or um_r:
            return f"{len(um_l)} line(s) only on the invoice, {len(um_r)} only on the PO"
    if dim == "financial":
        for e in v.get("evidence", []):
            if isinstance(e, dict) and e.get("status") == "FAIL":
                return e.get("message") or e.get("rule") or ""
    if dim == "business":
        for e in v.get("evidence", []):
            if isinstance(e, dict) and e.get("note"):
                return e["note"]
    return ""


def _from_dedupe(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Duplicate / relationship findings between documents. Surface the WHY —
    which fields matched — and the risk, not just "duplicate: BLOCK"."""
    out: List[Dict[str, Any]] = []
    for rel in resp.get("relationships", []):
        finding = rel.get("finding", "")
        action = rel.get("action", "")
        sev = HIGH if action in ("BLOCK", "ESCALATE") else MEDIUM
        # Build the "why" from the matching reason and the risk of acting on it.
        reason = rel.get("reason", "")
        risk = rel.get("risk", "")
        detail = ""
        if reason and risk:
            detail = f"{_sentence(reason)} {_sentence(risk)}"
        elif reason:
            detail = _sentence(reason)
        elif risk:
            detail = _sentence(risk)
        # name the two documents so "a duplicate" is specific
        a_name = _doc_label(rel.get("a_id"))
        b_name = _doc_label(rel.get("b_id"))
        if a_name and b_name:
            prefix = f"{a_name} and {b_name} look like the same invoice. "
            detail = (prefix + detail).strip()
        out.append(_finding(
            sev, "duplicate",
            _humanize_dedup(finding),
            detail, "duplicate detection"))
    return out


def _sentence(s: str) -> str:
    """Capitalise and end a fragment so concatenated reasons read as prose."""
    s = (s or "").strip()
    if not s:
        return ""
    s = s[0].upper() + s[1:]
    if s[-1] not in ".!?":
        s += "."
    return s


def _humanize_dedup(finding: str) -> str:
    return {
        "EXACT_DUPLICATE":        "This appears to be an exact duplicate of a document already seen",
        "IDENTITY_COLLISION":     "Two documents share an identity but differ — possible tampering",
        "SPLIT_INVOICE_SUSPECT":  "This may be one invoice split across several documents",
        "VENDOR_VARIANT_DUPLICATE": "A likely duplicate under a slightly different vendor name",
        "NUMBER_FORMAT_DEVIATION": "A near-duplicate with a differently formatted number",
        "TAX_ID_CHANGED":         "A near-duplicate whose tax ID changed — review carefully",
        "LOW_CONFIDENCE":         "A possible relationship to another document, low confidence",
    }.get(finding, finding.replace("_", " ").title())


# ── the surviving-findings channel (the engine's own findings dict) ──────────
def _from_findings(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The engine's first-class findings (fraud/consistency signals), which
    already carry human text. Highest trust — surfaced as high severity."""
    out: List[Dict[str, Any]] = []
    findings = resp.get("findings") or {}
    # The money-tree verification is authoritative for arithmetic. If it CONFIRMED
    # the totals, an older flat checker's INTERNAL_INCONSISTENCY (which ignores
    # the discount/VAT chain and computes subtotal+tax only) is stale and wrong —
    # suppress it so the report never contradicts the verified arithmetic.
    vt = (resp.get("verification") or {}).get("total") or {}
    arithmetic_confirmed = vt.get("verdict") == "CONFIRM"
    # findings is {subject_id: [ {kind, summary, risk, ...} ]}
    for subject, items in findings.items():
        for f in items or []:
            kind = f.get("kind", "")
            if arithmetic_confirmed and kind == "INTERNAL_INCONSISTENCY":
                continue
            # PO_NOT_FOUND is informational: an invoice processed before or
            # without its purchase order cites a PO that simply is not in Qrynt
            # yet. It is context a reviewer may want, not a problem — so it is
            # INFO, and does not by itself push the document to needs_review.
            if kind == "PO_NOT_FOUND":
                # Normal and expected: an invoice often arrives before (or
                # without) its PO. Phrase it as routine context, not a problem,
                # and do not repeat the raw "not in the registry" engine text.
                po = (f.get("detail") or {}).get("cited_po", "")
                out.append(_finding(
                    INFO, "reference",
                    "Matching purchase order not on file yet",
                    (f"This invoice references PO {po}, which hasn't been "
                     f"processed by Qrynt. This is normal if the PO hasn't been "
                     f"uploaded — no action needed unless you expected it here."),
                    "document context"))
                continue
            if kind == "PO_MISMATCH":
                det = f.get("detail") or {}
                inv_t, po_t = det.get("invoice_total"), det.get("po_total")
                po = det.get("po", "")
                if inv_t is not None and po_t is not None:
                    over = round(inv_t - po_t, 2)
                    detail = (f"The invoice bills {_money(inv_t)}, but PO {po} "
                              f"authorised {_money(po_t)} — {_money(over)} over "
                              f"the approved amount.")
                else:
                    detail = f.get("risk", "")
                out.append(_finding(
                    HIGH, "purchase order",
                    "The invoice bills more than the purchase order approved",
                    detail, "invoice vs purchase order"))
                continue
            if kind == "DUPLICATE":
                # Name the specific earlier document this duplicates, so the
                # reader knows WHAT it was seen as, not just "seen before".
                prior = (f.get("evidence_from") or [None])[0]
                prior_name = _doc_label(prior)
                det = f.get("detail") or {}
                rel = det.get("relationship", "")
                what = _humanize_dedup(rel) if rel else "This looks like a duplicate of an earlier document"
                risk = f.get("risk", "")
                if prior_name:
                    detail = (f"It matches {prior_name}, already processed by "
                              f"Qrynt. " + _sentence(risk)).strip()
                else:
                    detail = _sentence(risk)
                out.append(_finding(
                    HIGH, "duplicate", what, detail, "duplicate detection"))
                continue
            if kind == "INTERNAL_INCONSISTENCY":
                det = f.get("detail") or {}
                sub, tax = det.get("subtotal"), det.get("tax")
                stated, computed = det.get("stated_total"), det.get("computed_total")
                diff = det.get("difference")
                if stated is not None and computed is not None:
                    direction = "more than" if (diff or 0) > 0 else "less than"
                    detail = (f"The line items and tax add up to {_money(computed)}, "
                              f"but the invoice states {_money(stated)} — "
                              f"{_money(abs(diff or 0))} {direction} expected.")
                else:
                    detail = f.get("risk", "")
                out.append(_finding(
                    HIGH, "totals",
                    "The invoice's total doesn't match its line items",
                    detail, "arithmetic check"))
                continue
            out.append(_finding(
                HIGH, "integrity",
                f.get("summary") or kind.replace("_", " ").title(),
                f.get("risk", ""), "document analysis"))
    return out


# ── assembly ─────────────────────────────────────────────────────────────────
# semantic topics — findings sharing a topic within a category are the same
# real-world problem stated by different engines, and must appear only once.
def _topic(f: Dict[str, Any]) -> str:
    w = (f["what"] + " " + f["detail"]).lower()
    if any(t in w for t in ("bills more than", "above the authorised",
                            "exceeds the", "disagrees with po", "over the po",
                            "authorises only")):
        return "exceeds_po"
    if any(t in w for t in ("does not add up", "does not match the document",
                            "not balance", "over-pay", "over-bill", "add up")):
        return "arithmetic_mismatch"
    if "computed one" in w or "computed a total" in w or "derived" in w:
        return "derived_total"
    if "duplicate" in w:
        return "duplicate"
    return f["what"][:60].lower()


def _dedupe_findings(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse findings that describe the SAME real-world problem, even when
    different engines phrase it differently. One problem, one line. Within a
    (category, topic) the most human-worded finding wins — the engine's own
    findings channel is richest, so earlier entries (added first) are kept and
    later mechanical restatements are dropped."""
    # Strong cross-engine topics identify the SAME problem regardless of which
    # category each engine filed it under (the arithmetic mismatch is reported
    # by the integrity channel AND the financial verifier). Dedup those on topic
    # alone; everything else on (category, topic).
    _GLOBAL_TOPICS = {"arithmetic_mismatch", "derived_total", "duplicate", "exceeds_po"}
    seen = set()
    out = []
    for f in findings:
        t = _topic(f)
        key = t if t in _GLOBAL_TOPICS else (f["category"], t)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def _status_and_recommendation(resp: Dict[str, Any],
                               findings: List[Dict[str, Any]]) -> tuple:
    """Derive the document-level status and the recommended action."""
    highs = [f for f in findings if f["severity"] == HIGH]
    meds  = [f for f in findings if f["severity"] == MEDIUM]

    # dedup BLOCK/ESCALATE is the strongest signal
    for rel in resp.get("relationships", []):
        if rel.get("action") == "BLOCK":
            return "blocked", {"action": "block",
                               "reason": "a blocking duplicate was detected"}

    # a derived-total with no other high findings is its own status
    vt = (resp.get("verification") or {}).get("total") or {}
    derived_only = (vt.get("total_is_derived") and not highs)

    if highs:
        return "needs_review", {
            "action": "review",
            "reason": f"{len(highs)} issue(s) require attention before proceeding"}
    if derived_only:
        return "derived", {
            "action": "review",
            "reason": "a total was computed by Qrynt because the document stated none"}
    if meds:
        return "needs_review", {
            "action": "review",
            "reason": f"{len(meds)} item(s) to check"}
    return "clear", {"action": "accept",
                     "reason": "no issues were found"}


def _headline(status: str, findings: List[Dict[str, Any]],
              resp: Dict[str, Any]) -> str:
    n_high = sum(1 for f in findings if f["severity"] == HIGH)
    n_total = len(findings)
    if status == "blocked":
        return "This document is blocked — a duplicate was detected."
    if status == "clear":
        return "No issues found. The document is clear."
    if status == "derived":
        return "The document did not state a total; Qrynt computed one — please review."
    if n_high and n_total > n_high:
        return (f"{n_high} issue(s) require attention and "
                f"{n_total - n_high} more to review.")
    if n_high:
        return f"{n_high} issue(s) require attention."
    return f"{n_total} item(s) to review."


def _confidence(resp: Dict[str, Any], findings: List[Dict[str, Any]]) -> float:
    """A rough confidence that the report is complete/reliable. Lower it when a
    derived total (unverified) or an extraction gap is present."""
    conf = 0.98
    vt = (resp.get("verification") or {}).get("total") or {}
    if vt.get("total_is_derived"):
        conf -= 0.15
    # missing required fields reduce confidence in the picture
    conf -= 0.03 * sum(1 for f in findings if f["category"] == "completeness")
    return round(max(0.5, conf), 2)


def build_report(resp: Dict[str, Any]) -> Dict[str, Any]:
    """Build the human-facing report from any engine response. Reads only; never
    recomputes. Safe on any response shape — unknown/absent sources contribute
    nothing rather than erroring."""
    if not isinstance(resp, dict):
        return {"status": "error", "confidence": 0.0,
                "headline": "The analysis did not return a usable result.",
                "findings": [], "recommendation": {"action": "retry", "reason": ""}}

    findings: List[Dict[str, Any]] = []
    findings += _from_findings(resp)        # engine's own (highest trust)
    findings += _from_verification(resp)
    findings += _from_validation(resp)
    findings += _from_reconcile(resp)
    findings += _from_dedupe(resp)
    findings = _dedupe_findings(findings)
    findings.sort(key=lambda f: _SEVERITY_RANK.get(f["severity"], 9))

    status, recommendation = _status_and_recommendation(resp, findings)
    return {
        "status":         status,
        "confidence":     _confidence(resp, findings),
        "headline":       _headline(status, findings, resp),
        "findings":       findings,
        "recommendation": recommendation,
    }


def wrap(resp: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap an engine response in the platform contract: report over results.

    Idempotent — a response already wrapped is returned unchanged. Preserves the
    original response untouched under `results` so no integration reading the raw
    output breaks.
    """
    if not isinstance(resp, dict) or ("report" in resp and "results" in resp):
        return resp
    report = build_report(resp)
    return {"report": report, "results": resp}
