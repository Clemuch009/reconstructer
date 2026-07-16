# profiles/role_verifier.py
#
# ROLE VERIFICATION — establish what a value IS from evidence that does not
# depend on what it is CALLED.
#
# ── Why this exists ────────────────────────────────────────────────────────
# The resolver assigns fields by matching a label against an alias list. That
# works for vocabularies we have already seen and fails SILENTLY on any we have
# not. The label space is open, vendor-invented, unbounded, multilingual — and
# in a fraud context, attacker-controlled. An adversary renames "Total Balance
# Due" to "Net Outstanding Balance" and a lookup table is defeated.
#
# They cannot, however, make  subtotal + tax − discount = total  stop being true
# without producing an internally inconsistent invoice. Arithmetic is the part
# of a document an adversary cannot rewrite for free. So:
#
#   arithmetic is HIGH-authority evidence; labels are LOW-authority evidence.
#
# This module applies the higher-authority evidence after resolution has run.
#
# ── Ordinal precedence, deliberately not tuned weights ─────────────────────
#   arithmetic identity  >  structural role  >  value type  >  position  >  label
#
# There are no scalar weights here to tune. Hand-tuning floats until our own
# test corpus passes would just move the overfitting from string lists into a
# weight vector — the same disease in better clothes. Precedence is ordinal: if
# arithmetic settles it, the label does not get a vote.
#
# ── Bounding the search (why kv pairs, not raw text) ───────────────────────
# Subset-sum over every money value in a document is exponential (Vanguard has
# 28 of them → 2^28). We instead search over MONEY-VALUED KEY-VALUE PAIRS. That
# is not an arbitrary window: line items live in TABLES, while summary figures
# (subtotal / tax / discount / total) are what appear as labelled kv lines. So
# the money-valued kv set IS the totals block, structurally — typically 2-6
# values. Bounded, principled, and it still works when the line-item table fails
# to parse entirely (which is exactly what happens on Vanguard).
#
# ── Verdicts, and what is deliberately absent ──────────────────────────────
#   CONFIRM       arithmetic supports the resolved value
#   FILL          resolver found nothing; arithmetic identifies one uniquely
#   CONFLICT      resolved value contradicts what arithmetic identifies → FLAG
#   INCONSISTENT  summary values present but NO identity holds → the document
#                 does not balance. A finding, not a parse failure.
#   UNVERIFIABLE  too little to check, or ambiguous → say so, never guess
#
# Absent by design: silent correction. CONFLICT and INCONSISTENT do NOT
# overwrite the stated value. A stated total that violates the document's own
# arithmetic means either (a) we extracted the wrong value, or (b) the document's
# own maths is wrong — i.e. over-billing. Case (b) is the product's whole point:
# invoice_inv90812 states $7,952 while its subtotal + tax is $7,452, a real $500
# over-charge. Silently "correcting" that would destroy the signal we exist to
# surface. So we report, and let the rules engine / a human decide.

import re
from itertools import combinations
from typing import Any, Dict, List, Optional, Tuple

from relationship_engine.canonicalize import canon_amount
from profiles.resolver import _LINE_KV_RE, _EMBEDDED_LABEL_RE, _norm

CONFIRM      = "CONFIRM"
FILL         = "FILL"
CONFLICT     = "CONFLICT"
INCONSISTENT = "INCONSISTENT"
UNVERIFIABLE = "UNVERIFIABLE"

_TOL = 0.01          # money comparison tolerance
_MAX_SUMMARY_VALUES = 12   # 2^12 subsets; beyond this, bail rather than crawl

# A value is MONEY if it carries a currency symbol, or is a bare number with
# exactly two decimal places. This keeps "Net 30" / "45 days" / "8.25%" out of
# the arithmetic — a term count is not an amount.
_CURRENCY_SYM = re.compile(r"[\$£€¥]")
_BARE_MONEY   = re.compile(r"^-?[\d][\d,. ]*[.,]\d{2}$")


def _looks_money(raw: Any) -> bool:
    s = str(raw).strip()
    if not s or "%" in s:
        return False
    if _CURRENCY_SYM.search(s):
        return True
    return bool(_BARE_MONEY.match(s))


_LABEL_ONLY = re.compile(r"^\s*([A-Za-z][\w .\-#/]{0,40}?(?:\s*\([^)]{1,12}\))?)\s*[:=]\s*$")


def money_kv_values(lines: List[str]) -> Dict[str, float]:
    """The document's summary money figures: one entry per PHYSICAL line that
    carries a labelled money value.

    Indexed by LINE, not by label — and that is load-bearing. The resolver's kv
    index deliberately registers several keys for one line (the full key plus a
    trailing-label alias, so "Acme Corporation PO #: X" is findable as "PO #").
    Harmless for lookup, fatal here: the same figure would enter subset-sum
    twice under two names and could manufacture a false identity (a single
    $3,400 appearing as both "gross charges" and "charges" makes $6,800 look
    real). One physical line = one figure.

    Only money-valued lines are kept, which structurally selects the totals
    block: line items live in TABLES, summary figures appear as kv lines.
    """
    out: Dict[str, float] = {}
    clean = [l.strip() for l in lines]
    for i, line in enumerate(clean):
        if not line or line.startswith("[") or "://" in line:
            continue
        label = raw = None
        m = _LINE_KV_RE.match(line)
        if m:
            label, raw = m.group(1).strip(), m.group(2).strip()
            vm = _EMBEDDED_LABEL_RE.match(raw)      # strip a merged next column
            if vm:
                raw = vm.group(1).strip()
        else:
            lm = _LABEL_ONLY.match(line)            # "Grand Total:" / value below
            if lm and i + 1 < len(clean):
                nxt = clean[i + 1]
                if nxt and ":" not in nxt and not nxt.startswith("["):
                    label, raw = lm.group(1).strip(), nxt
        if not (label and raw and _looks_money(raw)):
            continue
        amt = canon_amount(raw)
        if amt is None:
            continue
        out[f"{i:04d}|{_norm(label)}"] = amt
    return out


def _label_of(key: str) -> str:
    """Human-readable label from a line-indexed key ('0007|grand total')."""
    return key.split("|", 1)[1] if "|" in key else key


def sum_line_items(line_items: Any) -> Optional[float]:
    """Σ of the line-item amount column. Fails CLOSED: returns None if any line
    lacks a parseable amount, so an incomplete sum can never masquerade as a
    complete one."""
    if not line_items or not isinstance(line_items, list):
        return None
    total = 0.0
    seen = False
    for li in line_items:
        if not isinstance(li, dict):
            return None
        amt = canon_amount(li.get("amount"))
        if amt is None:
            return None
        total += amt
        seen = True
    return round(total, 2) if seen else None


def _identify_by_identity(money: Dict[str, float]) -> Tuple[Optional[float], List[str], bool]:
    """Find the value that equals the sum of a subset of the OTHER summary
    values — i.e. the one satisfying  total = subtotal + tax − discount  without
    reading a single label.

    Returns (value, supporting_keys, ambiguous).
      value      the uniquely identified total, or None
      ambiguous  True if two DIFFERENT values both satisfy an identity → we
                 refuse to choose (fail closed)

    Candidates are deduplicated BY VALUE: on an invoice with no tax, subtotal
    and total are the same number and both "satisfy" — that is not ambiguity,
    it is the same answer twice.
    """
    keys = sorted(money)
    if len(keys) < 3 or len(keys) > _MAX_SUMMARY_VALUES:
        return None, [], False

    solutions: Dict[float, List[str]] = {}
    for cand_k in keys:
        cand_v = money[cand_k]
        others = [(k, money[k]) for k in keys if k != cand_k]
        found = None
        for r in range(2, len(others) + 1):
            for combo in combinations(others, r):
                if abs(sum(v for _, v in combo) - cand_v) < _TOL:
                    found = [k for k, _ in combo]
                    break
            if found:
                break
        if found and round(cand_v, 2) not in solutions:
            solutions[round(cand_v, 2)] = found

    if not solutions:
        return None, [], False
    if len(solutions) > 1:
        return None, [], True                    # two different values qualify
    value = next(iter(solutions))
    return value, solutions[value], False


def _has_tax_like(money: Dict[str, float]) -> bool:
    """Whether a tax/adjustment figure is present. Used only to decide whether
    Σ(line items) may stand in for the TOTAL or only for the SUBTOTAL."""
    for k, v in money.items():
        k = _label_of(k)
        if any(t in k for t in ("tax", "vat", "gst", "discount", "adjustment",
                                "shipping", "freight", "levy", "surcharge")):
            if abs(v) > _TOL:
                return True
    return False


def verify_total(
    fields: Dict[str, Any],
    lines: List[str],
    line_items: Any = None,
) -> Dict[str, Any]:
    """
    Establish the invoice total from arithmetic, independent of its label, and
    compare that with whatever the resolver claimed.

    Returns {verdict, claimed, identified, evidence, support}.
    Never mutates `fields`.
    """
    claimed = canon_amount(fields.get("total"))
    money   = money_kv_values(lines)
    li_sum  = sum_line_items(line_items)

    # ── Highest authority: the full arithmetic identity over the totals block.
    identified, support, ambiguous = _identify_by_identity(money)
    evidence = None
    if identified is not None:
        evidence = ("equals the sum of " + ", ".join(_label_of(s) for s in support) +
                    " — identified by arithmetic, not by its label")

    # ── Next: Σ(line items). This equals the SUBTOTAL in general, and the total
    #    only when no tax/adjustment applies. Used only if the identity above
    #    could not settle it.
    if identified is None and not ambiguous and li_sum is not None and not _has_tax_like(money):
        matches = sorted({round(v, 2) for v in money.values()
                          if abs(v - li_sum) < _TOL})
        if len(matches) == 1:
            identified = matches[0]
            evidence = ("equals the sum of the line items (no tax or adjustment "
                        "present) — identified by arithmetic, not by its label")
        elif not money and claimed is None:
            # nothing labelled at all, but the line items are complete
            identified = li_sum
            evidence = "recovered by summing the line items; no total is stated"

    # ── Verdict ───────────────────────────────────────────────────────────
    if ambiguous:
        return _res(UNVERIFIABLE, claimed, None,
                    "more than one value satisfies an arithmetic identity — "
                    "refusing to choose", [])

    if identified is None:
        # Could we have checked? If there are >=3 summary figures and none
        # balances, the document does not add up — that is a finding.
        if len(money) >= 3:
            return _res(INCONSISTENT, claimed, None,
                        "no value equals the sum of the others — the document's "
                        "own arithmetic does not balance",
                        [_label_of(k) for k in sorted(money)])
        return _res(UNVERIFIABLE, claimed, None,
                    "too few summary figures to check arithmetically", [])

    if claimed is None:
        return _res(FILL, None, identified, evidence, support)

    if abs(claimed - identified) < _TOL:
        return _res(CONFIRM, claimed, identified, evidence, support)

    return _res(CONFLICT, claimed, identified,
                f"the stated total ({claimed}) is not the value the arithmetic "
                f"identifies ({identified}) — {evidence}", support)


def _res(verdict, claimed, identified, evidence, support):
    return {"verdict": verdict, "claimed": claimed, "identified": identified,
            "evidence": evidence, "support": support}


def verify_roles(
    fields: Dict[str, Any],
    lines: List[str],
    line_items: Any = None,
) -> Dict[str, Any]:
    """
    Run role verification over a resolved Document View's fields.

    Returns {"fields": <fields, with misses FILLed>, "verification": {...}}.

    FILL is applied (a field the resolver missed is supplied, marked derived).
    CONFLICT / INCONSISTENT are reported and NOT applied — see module header.
    """
    out = dict(fields)
    report: Dict[str, Any] = {}

    tot = verify_total(fields, lines, line_items)
    report["total"] = tot
    if tot["verdict"] == FILL:
        out["total"] = tot["identified"]
        out["_total_derived"] = True

    return {"fields": out, "verification": report}
