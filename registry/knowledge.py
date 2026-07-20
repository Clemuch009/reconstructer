# registry/knowledge.py
#
# KNOWLEDGE VIEW — "tell me everything you know about X."
#
# The registry assembles the picture. The frontend renders cards and joins
# nothing: it asks one question and receives a story. That matters because the
# moment a UI starts stitching together documents, cases and resolutions, the
# answer to "what do we know about this vendor?" lives in JavaScript, differs
# per screen, and cannot be tested.
#
# ── Everything here is DERIVED from stored facts. Nothing is invented. ─────
#
# This is the whole discipline of the file, and it is worth being explicit about
# what was deliberately left out. A natural knowledge view mock reaches for:
#
#     Status:      ✓ Active Vendor
#     Risk:        Medium
#     Confidence:  99%
#     Approved 180 · Review 4 · Rejected 0
#     Bank: Equity ••••4281  Verified YES
#     ERP: Already Posted
#
# Not one of those can be produced honestly today:
#
#   Risk / Confidence   There is no risk model and no confidence model. A number
#                       here would be invented to fill a card, and the first
#                       question any controller asks is "why medium?" — for which
#                       there is no answer. A fabricated score is worse than a
#                       blank: it is the part people point at.
#   Approved/Rejected   Per-document decisions are NOT stored. Cases exist only
#                       where a finding needed a person; there is no "approved"
#                       state to count.
#   Bank accounts       Not extracted. At all.
#   ERP state           External truth. The Strata boundary — no amount of
#                       reasoning over documents establishes whether something
#                       was posted.
#
# So the honest versions are facts, and they are more persuasive than scores:
#
#     Risk: Medium        →  2 open cases · 1 tax ID change
#     Confidence: 99%     →  based on 4 documents since 2026-03-03
#     Approved 180        →  184 documents · 4 findings raised · 3 resolved
#
# ── Cheap by construction ─────────────────────────────────────────────────
# One indexed lookup (vendor_history) plus the case store. No joins, no
# traversal, no graph — because a record with provenance already IS an
# assertion, and the vendor key already indexes them.

from collections import Counter
from typing import Any, Dict, List, Optional

from registry.record import RegistryRecord
from registry.consistency import _number_signature, _MIN_BASELINE_N


def _iso(s: Optional[str]) -> str:
    return (s or "")[:19]


def _identity(records: List[RegistryRecord], vendor_canon: str) -> Dict[str, Any]:
    """Who this vendor is, as observed.

    Aliases are a real, derived fact: canonicalisation groups "Horizon IT
    Solutions Ltd", "Horizon IT Ltd" and "HORIZON IT SOLUTIONS" onto one vendor,
    and the raw forms are preserved on every assertion — so we can show exactly
    which spellings this vendor has used, rather than asserting a "correct" name
    nobody chose.
    """
    aliases: List[str] = []
    for r in records:
        raw = (r.raw_fields or {}).get("vendor")
        if raw and str(raw).strip() and str(raw).strip() not in aliases:
            aliases.append(str(raw).strip())

    # tax ids in the order first observed, with when and how often
    tax: Dict[str, Dict[str, Any]] = {}
    for r in sorted(records, key=lambda x: x.asserted_at or ""):
        t = (r.canonical or {}).get("tax_id_canon")
        if not t:
            continue
        if t not in tax:
            tax[t] = {"value": t,
                      "first_processed": _iso(r.asserted_at),
                      "first_seen": _iso(r.asserted_at),
                      "last_seen": _iso(r.asserted_at), "documents": 0}
        tax[t]["last_seen"] = _iso(r.asserted_at)
        tax[t]["documents"] += 1
        d = (r.canonical or {}).get("date_canon")
        if d and (not tax[t].get("earliest_document_date")
                  or d < tax[t]["earliest_document_date"]):
            tax[t]["earliest_document_date"] = d
    tax_list = list(tax.values())
    for i, t in enumerate(tax_list):
        # "current" is the most recently observed, not a judgement about which
        # is correct — the registry does not decide that; a human does, and the
        # decision lands in resolutions.
        t["status"] = "most recent" if i == len(tax_list) - 1 else "previously used"

    return {
        "canonical": vendor_canon,
        "display": aliases[0] if aliases else vendor_canon,
        "aliases": aliases,
        "tax_ids": tax_list,
        "tax_id_changed": len(tax_list) > 1,
    }


def _documents(records: List[RegistryRecord]) -> Dict[str, Any]:
    by_type = Counter(r.doc_type for r in records)
    times = sorted(_iso(r.asserted_at) for r in records if r.asserted_at)
    recent = sorted(records, key=lambda r: r.asserted_at or "", reverse=True)[:10]
    return {
        "total": len(records),
        "by_type": dict(by_type),
        "first_seen": times[0] if times else None,
        "last_seen": times[-1] if times else None,
        "recent": [{
            "doc_id": r.doc_id, "doc_type": r.doc_type,
            "reference": (r.canonical or {}).get("invoice_number_canon")
                         or (r.canonical or {}).get("po_number_canon"),
            "amount": (r.canonical or {}).get("amount_canon"),
            "currency": (r.canonical or {}).get("currency_canon"),
            "at": _iso(r.asserted_at), "source": r.source,
        } for r in recent],
    }


def _patterns(records: List[RegistryRecord]) -> Dict[str, Any]:
    """What is NORMAL for this vendor, as observed.

    This is the capability no ERP has: a vendor master knows the REGISTERED tax
    id; only accumulated observation knows the house style. Every pattern states
    its own basis and whether it is established, because a pattern from two
    documents is not a pattern — it fails closed below _MIN_BASELINE_N, exactly
    as the consistency checks do.
    """
    out: Dict[str, Any] = {}

    sigs = [_number_signature((r.canonical or {}).get("invoice_number_canon"))
            for r in records if r.doc_type == "invoice"
            and (r.canonical or {}).get("invoice_number_canon")]
    sigs = [s for s in sigs if s]
    if sigs:
        distinct = sorted(set(sigs))
        out["invoice_number_format"] = {
            "signature": distinct[0] if len(distinct) == 1 else None,
            "variants": distinct,
            "established": len(distinct) == 1 and len(sigs) >= _MIN_BASELINE_N,
            "observations": len(sigs),
        }

    rates = []
    for r in records:
        c = r.canonical or {}
        sub, tax = c.get("subtotal_canon"), c.get("tax_canon")
        if sub and tax is not None and sub > 0:
            rates.append(round(tax / sub, 4))
    if rates:
        distinct_r = sorted(set(rates))
        out["tax_rate"] = {
            "rate": distinct_r[0] if len(distinct_r) == 1 else None,
            "variants": distinct_r,
            "established": len(distinct_r) == 1 and len(rates) >= _MIN_BASELINE_N,
            "observations": len(rates),
        }

    curr = Counter((r.canonical or {}).get("currency_canon")
                   for r in records if (r.canonical or {}).get("currency_canon"))
    if curr:
        out["currencies"] = dict(curr)
    return out


def build_vendor_view(vendor_canon: str, registry, case_store=None,
                      limit: int = 500) -> Dict[str, Any]:
    """Everything the organisation knows about one vendor.

    Returns facts and what was decided about them. No score, no status, no
    judgement the registry is not entitled to make.
    """
    records = list(registry.vendor_history(vendor_canon, limit=limit) or [])

    view: Dict[str, Any] = {
        "subject": {"type": "vendor", "id": vendor_canon},
        "known": bool(records),
        "identity": _identity(records, vendor_canon),
        "documents": _documents(records),
        "patterns": _patterns(records),
        "cases": {"open": [], "resolved": []},
        "known_behaviour": [],
        "timeline": [],
        # Instead of a fabricated confidence: what this picture actually rests on.
        # Every date here is when QRYNT SAW the document, not the date printed
        # on it. Both are facts and they are routinely months apart — an invoice
        # dated March can be processed in July. Labelling assertion time as
        # "since" would quietly answer a question nobody asked, so the field
        # names say which clock they are on, and document dates travel
        # separately in the timeline.
        "basis": {
            "documents": len(records),
            "first_processed": _documents(records).get("first_seen"),
            "last_processed": _documents(records).get("last_seen"),
            "note": "everything here is derived from documents processed through "
                    "Qrynt; activity elsewhere is not visible",
        },
    }
    if not records:
        return view

    # Ordered by when we learned it (`at`), carrying the document's own date
    # (`document_date`) alongside. The registry's timeline is a history of
    # KNOWLEDGE, not of commerce: we cannot claim to have known something before
    # the document reached us.
    timeline: List[Dict[str, Any]] = [{
        "at": _iso(r.asserted_at),
        "kind": "document",
        "what": f"{r.doc_type} {(r.canonical or {}).get('invoice_number_canon') or (r.canonical or {}).get('po_number_canon') or ''}".strip(),
        "document_date": (r.canonical or {}).get("date_canon"),
        "amount": (r.canonical or {}).get("amount_canon"),
        "ref": r.doc_id,
    } for r in records]

    if case_store is not None:
        try:
            for c in case_store.list_cases(status=None, limit=200):
                if c.vendor_canon != vendor_canon:
                    continue
                entry = {"case_id": c.case_id, "kind": c.kind, "status": c.status,
                         "summary": c.summary, "created_at": _iso(c.created_at),
                         # The "Why?" — already carried by every finding since the
                         # consistency engine: what was checked, what conflicts,
                         # what is at stake, and what CANNOT be known from paper.
                         "why": c.finding or {}}
                res = case_store.resolutions_for(c.case_id)
                if res:
                    entry["resolutions"] = [r.to_dict() for r in res]
                view["cases"]["resolved" if c.status == "resolved" else "open"].append(entry)
                timeline.append({"at": _iso(c.created_at), "kind": "case",
                                 "what": c.kind, "ref": c.case_id})
                for r in res:
                    timeline.append({"at": _iso(r.resolved_at), "kind": "resolution",
                                     "what": r.decision, "ref": r.case_id})
                    # A resolution's RATIONALE is the vendor knowledge the
                    # organisation actually accumulated — "Vertex bills each
                    # subsidiary separately and restarts numbering". It is the
                    # only durable output of a review, and the thing that closes
                    # the next identical case in seconds instead of thirty
                    # minutes. It is EVIDENCE to read, never a rule that
                    # suppresses a future check.
                    if r.rationale:
                        view["known_behaviour"].append({
                            "behaviour": r.rationale, "decision": r.decision,
                            "established_by": r.case_id, "kind": r.kind,
                            "at": _iso(r.resolved_at), "by": r.resolved_by,
                            "scope": r.scope,
                        })
        except Exception:                    # noqa: BLE001 — cases are additive
            pass                             # never fail the whole view over them

    view["timeline"] = sorted(timeline, key=lambda t: t["at"] or "")
    return view
