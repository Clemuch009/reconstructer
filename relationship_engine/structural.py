# relationship_engine/structural.py
#
# Structural matching — aligns the LINE ITEMS of two documents.
#
# Its sole job is to answer "how do these two structures correspond?" — NOT
# "do the values match" and NOT "is this approved". It produces a mapping
# (matched pairs, unmatched-left, unmatched-right) that the financial and
# business dimensions then consume. No verdict here.
#
# Why this is fundamental, not a feature: the moment either side has line items
# (invoices and POs always do), header-only matching is incomplete by
# construction. PO says 10 laptops, invoice says 8 — the header total can look
# fine while 2 units were never delivered. Only line alignment surfaces that.
#
# ── Correctness properties ────────────────────────────────────────────────
#   • Alignment is by EVIDENCE, not position — reordered rows align correctly.
#   • 1:1 alignment is implemented and correct.
#   • Split lines (1 PO line → 2 invoice lines) and merged lines are NOT
#     silently guessed. They are reported as unmatched, so the engine says
#     "these lines didn't align, review them" rather than fabricating a match.
#     That is fail-closed: safe, not wrong. (Split/merge resolution is a later
#     capability; its absence never produces an incorrect mapping.)

import re
from typing import Any, Dict, List, Optional, Tuple


# ── line normalization ─────────────────────────────────────────────────────
#
# Two documents describe the same line with different schemas (qty vs quantity,
# unit_price vs rate). A structural profile maps each side's columns into a
# common vocabulary so lines are comparable. This mirrors the resolver's field
# mapping, at the line level.

def _norm_text(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v).strip().lower()) if v is not None else ""

_NUM_RE = re.compile(r"-?[\d,]*\.?\d+")

def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = _NUM_RE.search(str(v).replace(",", ""))
    return float(m.group()) if m else None


def _canonical_line(line: Dict[str, Any], col_map: Dict[str, List[str]]) -> Dict[str, Any]:
    """Map one raw line dict into the common vocabulary declared by col_map:
      {"description": ["description","item"], "quantity": ["qty","quantity"], ...}
    First matching source column wins. Returns {canonical_field: value}."""
    lower = { _norm_text(k): v for k, v in line.items() }
    out: Dict[str, Any] = {}
    for canon, aliases in col_map.items():
        for a in aliases:
            na = _norm_text(a)
            if na in lower and lower[na] is not None:
                out[canon] = lower[na]
                break
    return out


# ── evidence-based line similarity ─────────────────────────────────────────
#
# Each candidate pairing accrues weighted evidence across declared fields. A
# line pairs with its best-supported counterpart, not the one in the same
# position. Weights come from the structural profile (data), not hardcoded.

def _field_agrees(canon_field: str, a_val: Any, b_val: Any) -> bool:
    """Deterministic agreement test. Numeric fields compare as numbers (with a
    tiny tolerance); everything else compares as normalized text equality."""
    an, bn = _num(a_val), _num(b_val)
    if an is not None and bn is not None:
        return abs(an - bn) <= 0.001
    return _norm_text(a_val) == _norm_text(b_val)


def _pair_evidence(
    a_line: Dict[str, Any],
    b_line: Dict[str, Any],
    weights: Dict[str, float],
) -> Tuple[float, List[Dict[str, Any]]]:
    """Weighted evidence that a_line and b_line are the same line. Returns
    (score 0..1, evidence[]). Only fields present on BOTH sides contribute;
    a field absent on one side is neither agreement nor disagreement."""
    total_w = 0.0
    got_w   = 0.0
    evidence: List[Dict[str, Any]] = []
    for field, w in weights.items():
        if field in a_line and field in b_line:
            total_w += w
            agrees = _field_agrees(field, a_line[field], b_line[field])
            if agrees:
                got_w += w
            evidence.append({
                "field": field, "weight": w, "agrees": agrees,
                "a": a_line[field], "b": b_line[field],
            })
    score = round(got_w / total_w, 4) if total_w > 0 else 0.0
    return score, evidence


# ── identity-based candidate generation ────────────────────────────────────
#
# Don't compare every line to every line. A line's identity field(s) (sku,
# product_code, description) generate candidates; only lines sharing identity
# evidence are scored. Falls back to scoring all pairs when no identity field
# is available (small line counts make this acceptable).

def _identity_signature(line: Dict[str, Any], identity_fields: List[str]) -> Optional[str]:
    parts = [_norm_text(line[f]) for f in identity_fields if f in line and line[f] is not None]
    return "|".join(parts) if parts else None


ALIGN_THRESHOLD = 0.75   # minimum evidence score to accept a 1:1 alignment


def align_lines(
    a_lines: List[Dict[str, Any]],
    b_lines: List[Dict[str, Any]],
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Align two lists of line items by evidence.

    profile (declarative) supplies:
      "columns":   {canonical: [aliases...]}         — per side vocabulary map
      "identity":  [canonical fields for candidacy]  — e.g. ["description","sku"]
      "weights":   {canonical: weight}               — evidence weighting
      "allow":     ["reorder"] (splits/merges deferred → reported unmatched)

    Returns:
      {
        "matched":   [ {a_index, b_index, score, evidence} ],
        "unmatched_left":  [a_index, ...],
        "unmatched_right": [b_index, ...],
        "summary": {a_total, b_total, matched, unmatched_left, unmatched_right},
      }

    1:1 only. A line that has no counterpart above threshold — including split
    or merged lines — is reported unmatched, never force-fit.
    """
    col_map = profile.get("columns", {})
    identity_fields = profile.get("identity", [])
    weights = profile.get("weights", {})

    A = [_canonical_line(l, col_map) for l in a_lines]
    B = [_canonical_line(l, col_map) for l in b_lines]

    # candidate pairs via identity signature (prunes the comparison space)
    b_by_sig: Dict[str, List[int]] = {}
    for j, bl in enumerate(B):
        sig = _identity_signature(bl, identity_fields)
        if sig is not None:
            b_by_sig.setdefault(sig, []).append(j)

    # score candidate pairs
    scored: List[Tuple[float, int, int, List[Dict[str, Any]]]] = []
    for i, al in enumerate(A):
        sig = _identity_signature(al, identity_fields)
        candidates = b_by_sig.get(sig, []) if sig is not None else list(range(len(B)))
        if sig is None and not identity_fields:
            candidates = list(range(len(B)))
        for j in candidates:
            score, ev = _pair_evidence(al, B[j], weights)
            if score >= ALIGN_THRESHOLD:
                scored.append((score, i, j, ev))

    # greedy best-first 1:1 assignment (deterministic: highest score wins,
    # ties broken by index order). A line already used cannot be reused —
    # which is exactly why a split (1→2) leaves the second line unmatched.
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    used_a, used_b = set(), set()
    matched: List[Dict[str, Any]] = []
    for score, i, j, ev in scored:
        if i in used_a or j in used_b:
            continue
        used_a.add(i); used_b.add(j)
        matched.append({"a_index": i, "b_index": j, "score": score, "evidence": ev})

    unmatched_left  = [i for i in range(len(A)) if i not in used_a]
    unmatched_right = [j for j in range(len(B)) if j not in used_b]

    # ── Split / merge detection (second pass) ────────────────────────────
    # A split (1 line on one side → N lines on the other) or merge (N → 1)
    # shows up as leftover lines on ONE side that share an identity signature
    # with a line on the OTHER side. Rather than burying these as plain
    # "unmatched", group them by shared signature and report them as
    # split_merge groups WITH evidence, so the caller can reason about them
    # (e.g. compare the SUM of the group to the single counterpart). This is
    # still evidence-based — grouping requires a matching identity signature,
    # never a guess — and it does not force a 1:1 match.
    if profile.get("allow") and "split_merge" in profile.get("allow", []):
        split_merge = _detect_split_merge(
            A, B, unmatched_left, unmatched_right, matched, identity_fields, weights
        )
        # remove grouped indices from the plain unmatched lists
        grouped_a = {i for g in split_merge for i in g["a_indices"]}
        grouped_b = {j for g in split_merge for j in g["b_indices"]}
        unmatched_left  = [i for i in unmatched_left  if i not in grouped_a]
        unmatched_right = [j for j in unmatched_right if j not in grouped_b]
    else:
        split_merge = []

    return {
        "matched": matched,
        "unmatched_left":  unmatched_left,
        "unmatched_right": unmatched_right,
        "split_merge": split_merge,
        "summary": {
            "a_total": len(A), "b_total": len(B),
            "matched": len(matched),
            "unmatched_left":  len(unmatched_left),
            "unmatched_right": len(unmatched_right),
            "split_merge": len(split_merge),
        },
    }


def _sum_field(lines: List[Dict[str, Any]], indices: List[int], field: str) -> Optional[float]:
    """Sum a numeric field across the given lines. None if any is non-numeric."""
    total = 0.0
    any_num = False
    for i in indices:
        v = _num(lines[i].get(field))
        if v is None:
            return None
        total += v; any_num = True
    return total if any_num else None


def _detect_split_merge(
    A: List[Dict[str, Any]],
    B: List[Dict[str, Any]],
    unmatched_left: List[int],
    unmatched_right: List[int],
    matched: List[Dict[str, Any]],
    identity_fields: List[str],
    weights: Dict[str, float],
) -> List[Dict[str, Any]]:
    """
    Find split (1↔N) / merge (N↔1) groups.

    A split shows up as: an unmatched line sharing an identity signature with a
    line that DID match 1:1 — the matched pair plus the leftover sibling(s)
    together form the split/merge group. We fold the matched sibling back in so
    the group is complete, then attach evidence comparing the SUM of the
    many-side against the single counterpart.

    Conservative: grouping requires a shared identity signature (never a guess).
    """
    groups: List[Dict[str, Any]] = []

    def _sig(lines, i):
        return _identity_signature(lines[i], identity_fields)

    # map signature -> matched pair (a_index, b_index)
    matched_by_sig_a: Dict[str, Tuple[int, int]] = {}
    for m in matched:
        s = _sig(A, m["a_index"])
        if s is not None:
            matched_by_sig_a[s] = (m["a_index"], m["b_index"])

    # group unmatched-left siblings by signature
    left_by_sig: Dict[str, List[int]] = {}
    for i in unmatched_left:
        s = _sig(A, i)
        if s is not None:
            left_by_sig.setdefault(s, []).append(i)
    right_by_sig: Dict[str, List[int]] = {}
    for j in unmatched_right:
        s = _sig(B, j)
        if s is not None:
            right_by_sig.setdefault(s, []).append(j)

    handled_sigs = set()

    # SPLIT: matched pair exists for sig, plus extra unmatched lines on side A
    for sig, extra_a in left_by_sig.items():
        if sig in matched_by_sig_a:
            m_a, m_b = matched_by_sig_a[sig]
            a_indices = sorted(set([m_a] + extra_a))
            b_indices = [m_b]
            groups.append(_make_split_group("split", A, B, a_indices, b_indices, sig))
            handled_sigs.add(sig)

    # MERGE: matched pair exists for sig, plus extra unmatched lines on side B
    for sig, extra_b in right_by_sig.items():
        if sig in handled_sigs:
            continue
        if sig in matched_by_sig_a:
            m_a, m_b = matched_by_sig_a[sig]
            a_indices = [m_a]
            b_indices = sorted(set([m_b] + extra_b))
            groups.append(_make_split_group("merge", A, B, a_indices, b_indices, sig))
            handled_sigs.add(sig)

    # Pure split/merge with NO 1:1 anchor (e.g. 2 lines ↔ 1 line, none matched):
    for sig in set(left_by_sig) | set(right_by_sig):
        if sig in handled_sigs:
            continue
        la = left_by_sig.get(sig, [])
        rb = right_by_sig.get(sig, [])
        if len(rb) == 1 and len(la) >= 2:
            groups.append(_make_split_group("split", A, B, sorted(la), rb, sig))
        elif len(la) == 1 and len(rb) >= 2:
            groups.append(_make_split_group("merge", A, B, la, sorted(rb), sig))

    return groups


def _make_split_group(kind, A, B, a_indices, b_indices, sig):
    """Build a split/merge group with reconciliation evidence (sum of the many
    side vs the single counterpart, for amount and quantity)."""
    ev = {"kind": kind, "signature": sig}
    if kind == "split":
        many_lines, many_idx, one_lines, one_idx = A, a_indices, B, b_indices[0]
    else:
        many_lines, many_idx, one_lines, one_idx = B, b_indices, A, a_indices[0]
    for field in ("amount", "quantity"):
        many_sum = _sum_field(many_lines, many_idx, field)
        one_val  = _num(one_lines[one_idx].get(field))
        if many_sum is not None and one_val is not None:
            ev[f"{field}_sum_many"] = many_sum
            ev[f"{field}_one"] = one_val
            ev[f"{field}_reconciles"] = abs(many_sum - one_val) < 0.01
    return {"kind": kind, "a_indices": a_indices, "b_indices": b_indices, "evidence": ev}
