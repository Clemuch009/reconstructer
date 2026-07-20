# registry/consistency.py
#
# THE CONSISTENCY ENGINE — one entry point:
#
#     evaluate(new_record, provider) -> [Finding]
#
# The registry does not answer "have I seen this invoice before?" That is too
# small a question. It answers:
#
#     "What does the organisation already know about this business event,
#      and is this new evidence consistent with that knowledge?"
#
# Duplicates are just ONE way the answer can be "no". Every check below is the
# same shape — compare what this document CLAIMS against what is already known —
# and they differ only in where the prior knowledge comes from:
#
#     DUPLICATE            prior documents with the same identity
#     INTERNAL_INCONSISTENCY  the document versus its own arithmetic
#     PO_MISMATCH          the purchase order it cites
#     MISSING_TAX_ID       what this vendor has always supplied
#     TAX_ID_CHANGED       what this vendor's tax id has always been
#     TAX_RATE_DEVIATION   what this vendor's tax rate has always been
#     PAYMENT_CONFLICT     payments already recorded against this invoice
#
# ── Built for DISPUTES ────────────────────────────────────────────────────
# A finding is not a verdict; it is a case. To dispute a charge with a vendor you
# need to say precisely what you know, how you know it, and what remains open.
# So every Finding carries:
#
#   supporting / conflicts   the specific facts, with both sides' values
#   evidence_from            WHICH documents establish this, by id — you cannot
#                            argue from "the system says so"
#   risk                     what is at stake in plain language
#   unresolved               what CANNOT be established from documents at all
#
# That last field is the Strata lesson made structural. Two documents claimed
# reference SSC-2026-X89 — a debit memo and an invoice, same $40,000, same payer.
# No amount of reasoning over paper can say whether one supersedes the other, or
# whether either was already posted, because THE DOCUMENTS DO NOT CONTAIN THAT
# FACT. An engine that quietly guesses there is dishonest. So a finding states
# what it cannot know and names where the answer lives (the ledger). When an ERP
# is connected it answers those questions through the same EvidenceProvider seam
# — no engine changes.
#
# ── What this module must never do ────────────────────────────────────────
# Store anything. Findings are COMPUTED on read, every time. A stored
# `duplicate = yes` is a lie the moment a correction arrives, a policy changes,
# or the engine improves — and canonicalize.py improved five times in one
# session. Facts are stored; conclusions are derived.

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from registry.record import RegistryRecord
from registry.provider import rehydrate
from relationship_engine.candidates import InvoiceRecord
from relationship_engine.evidence import gather_evidence
from relationship_engine.classify import classify_relationship
from relationship_engine.dedup_policy import decide_action
from relationship_engine.canonicalize import canon_amount
import re

# finding kinds
DUPLICATE              = "DUPLICATE"
INTERNAL_INCONSISTENCY = "INTERNAL_INCONSISTENCY"
PO_MISMATCH            = "PO_MISMATCH"
PO_NOT_FOUND           = "PO_NOT_FOUND"
MISSING_TAX_ID         = "MISSING_TAX_ID"
TAX_ID_CHANGED         = "TAX_ID_CHANGED"
TAX_RATE_DEVIATION     = "TAX_RATE_DEVIATION"
PAYMENT_CONFLICT       = "PAYMENT_CONFLICT"
NUMBER_FORMAT_DEVIATION = "NUMBER_FORMAT_DEVIATION"

_TOL = 0.01
# A baseline needs enough observations to BE a baseline. Below this we say
# nothing rather than call a vendor's second invoice anomalous.
_MIN_BASELINE_N = 3


@dataclass
class Finding:
    kind: str
    subject: str                                  # doc_id under evaluation
    summary: str = ""
    supporting:  List[Dict[str, Any]] = field(default_factory=list)
    conflicts:   List[Dict[str, Any]] = field(default_factory=list)
    evidence_from: List[str] = field(default_factory=list)   # doc_ids
    risk: str = ""
    unresolved: str = ""                          # needs truth outside documents
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _ev(text: str, a: Any, b: Any = None) -> Dict[str, Any]:
    return {"text": text, "detail": f"{a}" if b is None or a == b else f"{a} vs {b}"}


# ── 1. duplicates ─────────────────────────────────────────────────────────
def _check_duplicates(rec: RegistryRecord, provider) -> List[Finding]:
    """Prior documents sharing an identity. Delegates entirely to the tested
    dedup engine (stages 1-6) — the registry only supplies the candidates that
    batch mode could never see, because real duplicates arrive weeks apart.

    Relationships the policy ALLOWs are NOT emitted. A RECURRING_INVOICE — same
    vendor and amount, different billing month — is the engine concluding that
    nothing is wrong. Reporting that as a "finding" is noise, and it does not
    scale: a monthly vendor's 100th invoice would carry 99 of them, burying the
    one finding that matters underneath. A finding is a PROBLEM; the absence of
    a problem is not news.

    The check still runs against every prior — nothing is skipped, and a
    duplicate hiding among recurring invoices is still found. Only the
    all-clear verdicts are left unsaid.
    """
    query = rehydrate(rec)
    # same-type only: the registry is cross-type by design, so an invoice and the
    # PO it cites share keys. A duplicate is the same document twice.
    try:
        priors = provider.candidates_for(query, doc_type=rec.doc_type)
    except TypeError:
        priors = provider.candidates_for(query)   # batch providers: invoices only
    out: List[Finding] = []
    for prior in priors:
        ev = gather_evidence(prior, query)
        rel = classify_relationship(ev, prior, query)
        d = decide_action(rel)
        if d["action"] == "ALLOW":
            continue                     # checked, and nothing is wrong
        out.append(Finding(
            kind=DUPLICATE,
            subject=rec.doc_id,
            summary=f"{d['finding']}: {d['reason']}",
            supporting=d.get("supporting", []),
            conflicts=d.get("conflicts", []),
            evidence_from=[prior.id],
            risk=d.get("risk", ""),
            unresolved=("Whether the earlier document was posted, paid, "
                        "cancelled or reversed cannot be established from the "
                        "documents — check the ledger."),
            detail={"relationship": d["finding"], "suggested_action": d["action"],
                    "auto_blockable": d.get("auto_blockable", False)},
        ))
    return out


# ── 2. incorrect data (the document vs its own arithmetic) ────────────────
def _check_internal(rec: RegistryRecord) -> List[Finding]:
    """subtotal + tax == total? Label-free, so renaming fields cannot hide it.

    This is the invoice_inv90812 case: stated $7,952 against a $6,900 subtotal
    and $552 tax — a real $500 over-charge. It needs no registry and no ERP; the
    contradiction is inside the document. Included here so `evaluate` is the one
    place a caller asks "is this document sound?"
    """
    c = rec.canonical

    # The money-structure tree is the authority on arithmetic: it validates the
    # WHOLE chain (subtotal − discount + shipping + tax = total). If it CONFIRMed
    # the document, there is no inconsistency — and the flat subtotal+tax check
    # below would false-positive on any invoice with a discount, shipping, or a
    # multi-section structure it cannot see. Defer to the tree when it ruled.
    if c.get("arithmetic_verdict") == "CONFIRM":
        return []

    sub, tax, tot = c.get("subtotal_canon"), c.get("tax_canon"), c.get("amount_canon")
    if sub is None or tot is None:
        return []
    # Fold in discount and shipping when present, so this check matches the chain
    # the tree uses rather than a flat sum that ignores adjustments.
    disc = c.get("discount_canon") or 0.0
    ship = c.get("shipping_canon") or 0.0
    expected = sub + disc + ship + (tax or 0.0)
    if abs(expected - tot) < _TOL:
        return []
    delta = round(tot - expected, 2)
    return [Finding(
        kind=INTERNAL_INCONSISTENCY,
        subject=rec.doc_id,
        summary=(f"the document does not add up: subtotal {sub} + tax {tax or 0} "
                 f"= {round(expected,2)}, but it states {tot}"),
        conflicts=[_ev("Stated total", tot, round(expected, 2))],
        evidence_from=[rec.doc_id],
        risk=(f"The invoice charges {abs(delta)} {'more' if delta>0 else 'less'} "
              f"than its own line detail supports. Paying as stated "
              f"{'over-pays' if delta>0 else 'under-pays'} by {abs(delta)}."),
        detail={"subtotal": sub, "tax": tax, "stated_total": tot,
                "computed_total": round(expected, 2), "difference": delta},
    )]


# ── 3. mismatched purchase orders ─────────────────────────────────────────
def _check_po(rec: RegistryRecord, provider) -> List[Finding]:
    """Find the PO this invoice cites and compare the claims.

    This is the cross-type lookup the registry exists for: the invoice and the PO
    are different document types, arriving at different times, and nothing in
    either one alone reveals the conflict.
    """
    c = rec.canonical
    po = c.get("po_number_canon")
    if not po or rec.doc_type != "invoice":
        return []
    pos = [p for p in provider.by_reference("purchase_order", po)
           if p.doc_type == "purchase_order"]
    if not pos:
        return [Finding(
            kind=PO_NOT_FOUND,
            subject=rec.doc_id,
            summary=f"the invoice cites PO {po}, which is not in the registry",
            conflicts=[_ev("Cited PO", po, "not found")],
            evidence_from=[rec.doc_id],
            risk=("The invoice references a purchase order the organisation has "
                  "no record of. Either the PO was never ingested, or the "
                  "reference is invalid."),
            unresolved=("Whether the PO exists in the ERP but was never sent to "
                        "Qrynt cannot be established here — check the ERP."),
            detail={"cited_po": po},
        )]

    out: List[Finding] = []
    for p in pos:
        pc = p.canonical
        sup, con = [], []
        if pc.get("vendor_canon") and c.get("vendor_canon"):
            (sup if pc["vendor_canon"] == c["vendor_canon"] else con).append(
                _ev("Vendor", c["vendor_canon"], pc["vendor_canon"]))
        if pc.get("currency_canon") and c.get("currency_canon"):
            (sup if pc["currency_canon"] == c["currency_canon"] else con).append(
                _ev("Currency", c["currency_canon"], pc["currency_canon"]))
        inv_t, po_t = c.get("amount_canon"), pc.get("amount_canon")
        over = None
        if inv_t is not None and po_t is not None:
            if inv_t - po_t > _TOL:
                over = round(inv_t - po_t, 2)
                con.append(_ev("Invoice exceeds PO value", inv_t, po_t))
            else:
                sup.append(_ev("Within PO value", inv_t, po_t))
        if not con:
            continue
        out.append(Finding(
            kind=PO_MISMATCH,
            subject=rec.doc_id,
            summary=f"the invoice disagrees with PO {po}",
            supporting=sup, conflicts=con,
            evidence_from=[p.doc_id],
            risk=(f"The invoice bills {over} above the authorised PO value."
                  if over else
                  "The invoice contradicts the purchase order it cites."),
            unresolved=("Whether the PO was amended or is closed cannot be "
                        "established from the documents — check the ERP."),
            detail={"po_doc_id": p.doc_id, "po": po,
                    "invoice_total": inv_t, "po_total": po_t, "over_by": over},
        ))
    return out


# ── 4/5. tax id — missing, or changed ─────────────────────────────────────
def _check_tax_id(rec: RegistryRecord, provider) -> List[Finding]:
    """Compare this document's tax id against what this vendor has always used.

    This is the capability no ERP provides: a vendor master knows the REGISTERED
    tax id; only accumulated observation knows what is NORMAL for this vendor.
    The same shape catches the classic invoice-fraud vector — a known vendor
    suddenly presenting different details.
    """
    c = rec.canonical
    vendor = c.get("vendor_canon")
    if not vendor:
        return []
    this_tax = c.get("tax_id_canon")
    history = [h for h in provider.vendor_history(vendor) if h.doc_id != rec.doc_id]
    seen = [h.canonical.get("tax_id_canon") for h in history]
    seen = [s for s in seen if s]
    if not seen:
        return []

    # the established value: only if the vendor has been consistent, over enough
    # observations to mean anything. Anything less and we say nothing rather than
    # invent an anomaly.
    distinct = sorted(set(seen))
    established = distinct[0] if len(distinct) == 1 and len(seen) >= _MIN_BASELINE_N else None

    if this_tax is None:
        return [Finding(
            kind=MISSING_TAX_ID,
            subject=rec.doc_id,
            summary=(f"no tax id on this invoice, though this vendor has supplied "
                     f"one on {len(seen)} previous document(s)"),
            conflicts=[_ev("Tax id", "absent", distinct[0] if len(distinct) == 1 else "previously supplied")],
            evidence_from=[h.doc_id for h in history if h.canonical.get("tax_id_canon")][:5],
            risk=("Mandatory tax information is missing from an invoice whose "
                  "vendor has always provided it. This can block tax recovery "
                  "and may indicate the document did not originate with the "
                  "usual sender."),
            detail={"vendor": vendor, "previously_seen": distinct},
        )]

    if established and this_tax != established:
        return [Finding(
            kind=TAX_ID_CHANGED,
            subject=rec.doc_id,
            summary=(f"tax id differs from the {len(seen)} previous documents "
                     f"from this vendor"),
            supporting=[_ev("Vendor", vendor)],
            conflicts=[_ev("Tax id", this_tax, established)],
            evidence_from=[h.doc_id for h in history
                           if h.canonical.get("tax_id_canon") == established][:5],
            risk=("A known vendor is presenting a different tax registration. "
                  "This is a common impersonation pattern: correct vendor name, "
                  "altered identity details. Verify through a channel other than "
                  "this document before paying."),
            unresolved=("Whether the vendor legitimately re-registered cannot be "
                        "established from the documents — confirm with the "
                        "vendor master or the vendor directly."),
            detail={"vendor": vendor, "claimed": this_tax,
                    "established": established, "observations": len(seen)},
        )]
    return []


# ── 6. tax rate deviation ─────────────────────────────────────────────────
def _check_tax_rate(rec: RegistryRecord, provider) -> List[Finding]:
    """Compare the effective tax rate against this vendor's norm."""
    c = rec.canonical
    vendor = c.get("vendor_canon")
    sub, tax = c.get("subtotal_canon"), c.get("tax_canon")
    if not vendor or sub is None or tax is None or sub <= 0:
        return []
    rate = round(tax / sub, 4)

    rates = []
    hist = [h for h in provider.vendor_history(vendor) if h.doc_id != rec.doc_id]
    for h in hist:
        hs, ht = h.canonical.get("subtotal_canon"), h.canonical.get("tax_canon")
        if hs and ht is not None and hs > 0:
            rates.append(round(ht / hs, 4))
    if len(rates) < _MIN_BASELINE_N:
        return []
    distinct = sorted(set(rates))
    if len(distinct) != 1:
        return []                     # vendor is not consistent — no baseline
    norm = distinct[0]
    if abs(rate - norm) < 0.0001:
        return []
    return [Finding(
        kind=TAX_RATE_DEVIATION,
        subject=rec.doc_id,
        summary=(f"tax rate {rate:.2%} differs from this vendor's usual "
                 f"{norm:.2%} across {len(rates)} documents"),
        supporting=[_ev("Vendor", vendor)],
        conflicts=[_ev("Effective tax rate", f"{rate:.2%}", f"{norm:.2%}")],
        evidence_from=[h.doc_id for h in hist][:5],
        risk=("The tax charged departs from this vendor's established pattern. "
              "Either the tax treatment changed, or the amount is wrong."),
        unresolved=("Whether a rate change or an exemption legitimately applies "
                    "cannot be established from the documents."),
        detail={"vendor": vendor, "rate": rate, "baseline": norm,
                "observations": len(rates)},
    )]


# ── invoice-number format against the vendor's own history ────────────────
#
# This is the honest answer to "can we verify the invoice number?"
#
# We CANNOT identify an invoice number label-free the way we identify the total.
# The total is verifiable because it satisfies an EQUATION — total = subtotal +
# tax − discount — a constraint the document enforces on itself, which is why no
# amount of relabelling defeats it. An invoice number satisfies no equation. It
# is an arbitrary string; nothing in the document constrains it, so there is
# nothing to check it against. Measured on the corpus, a real invoice contains
# several identifier-shaped tokens (invoice number, PO number, cost centre, bank
# account fragments) and NOTHING intrinsic tells them apart. Picking by
# type+position+cardinality would be a heuristic wearing verification's clothes,
# and it would be confidently wrong.
#
# What CAN be checked is the number's SHAPE against what this vendor has always
# used. That is not identification — it is verification against accumulated
# observation, the same class of evidence as TAX_ID_CHANGED, and the same fraud
# vector: an impersonator gets the vendor name right and the house style wrong.

_SIG_ALPHA = re.compile(r"A{2,}")
_SIG_DIGIT = re.compile(r"9{2,}")


def _number_signature(value: str) -> Optional[str]:
    """Shape of an identifier: letters→A+, digits→9+, runs collapsed.

    Computed on the CANONICAL number, so punctuation is already gone. That is
    deliberate: INV-2026-8819 and INV/2026/8819 are the SAME number to every
    other part of the engine (canonicalisation exists precisely so INV-001 and
    INV001 compare equal), so treating a separator change as a format deviation
    would contradict that and manufacture false positives. What survives is the
    STRUCTURE — the letter/digit arrangement — which is what actually
    distinguishes a vendor's house style from an impostor's.
    """
    if not value:
        return None
    out = []
    for ch in str(value).strip().upper():
        out.append("A" if ch.isalpha() else "9" if ch.isdigit() else ch)
    s = _SIG_DIGIT.sub("9+", "".join(out))
    return _SIG_ALPHA.sub("A+", s) or None


def _check_number_format(rec: RegistryRecord, provider) -> List[Finding]:
    c = rec.canonical
    vendor = c.get("vendor_canon")
    num = c.get("invoice_number_canon")
    if not vendor or not num or rec.doc_type != "invoice":
        return []

    this_sig = _number_signature(num)
    if not this_sig:
        return []

    sigs = []
    hist = [h for h in provider.vendor_history(vendor)
            if h.doc_id != rec.doc_id and h.doc_type == "invoice"]
    for h in hist:
        hn = h.canonical.get("invoice_number_canon")
        s = _number_signature(hn) if hn else None
        if s:
            sigs.append(s)
    if len(sigs) < _MIN_BASELINE_N:
        return []
    distinct = sorted(set(sigs))
    if len(distinct) != 1:
        return []            # this vendor is not consistent — no baseline exists
    norm = distinct[0]
    if this_sig == norm:
        return []
    return [Finding(
        kind=NUMBER_FORMAT_DEVIATION,
        subject=rec.doc_id,
        summary=(f"invoice number {rec.raw_fields.get('invoice_number') or num} "
                 f"does not follow the structure this vendor has used on all "
                 f"{len(sigs)} previous invoices"),
        supporting=[_ev("Vendor", vendor)],
        conflicts=[_ev("Invoice number format", this_sig, norm)],
        evidence_from=[h.doc_id for h in hist][:5],
        risk=("A known vendor's invoice number departs from its established "
              "house format. This is weaker evidence than a changed tax id, but "
              "it is the same pattern: correct vendor name, wrong details. Worth "
              "confirming the invoice originated with the usual sender."),
        unresolved=("Whether the vendor changed its numbering scheme cannot be "
                    "established from the documents — confirm with the vendor."),
        detail={"vendor": vendor, "number": num, "signature": this_sig,
                "baseline": norm, "observations": len(sigs)},
    )]


# ── 7. payment conflicts ──────────────────────────────────────────────────
def _check_payments(rec: RegistryRecord, provider) -> List[Finding]:
    """Payments already recorded against this invoice.

    HONEST LIMIT, stated in every finding this produces: the registry only knows
    payments that passed through Qrynt. A payment made in the ERP and never
    ingested is invisible here. "Has this been paid?" is EXTERNAL truth — this
    check narrows the question; it cannot close it.
    """
    c = rec.canonical
    inv = c.get("invoice_number_canon")
    if not inv or rec.doc_type != "invoice":
        return []
    payments = [p for p in provider.by_reference("payment", inv)
                if p.doc_type == "payment"]
    if not payments:
        return []
    total_paid = 0.0
    for p in payments:
        amt = canon_amount(p.raw_fields.get("amount") or p.canonical.get("amount_canon"))
        if amt:
            total_paid += amt
    inv_total = c.get("amount_canon")
    con = [_ev("Payments already recorded", f"{len(payments)} document(s)")]
    if inv_total is not None:
        con.append(_ev("Amount", f"invoice {inv_total}", f"already paid {round(total_paid,2)}"))
    return [Finding(
        kind=PAYMENT_CONFLICT,
        subject=rec.doc_id,
        summary=(f"{len(payments)} payment(s) totalling {round(total_paid,2)} are "
                 f"already recorded against invoice {inv}"),
        conflicts=con,
        evidence_from=[p.doc_id for p in payments],
        risk=("This invoice appears to have been settled already. Paying again "
              "would duplicate the payment."),
        unresolved=("The registry only knows payments processed through Qrynt. "
                    "Payments made directly in the ERP are not visible here — "
                    "confirm settlement status in the ledger."),
        detail={"invoice": inv, "payments": [p.doc_id for p in payments],
                "total_paid": round(total_paid, 2), "invoice_total": inv_total},
    )]


# ── the one entry point ───────────────────────────────────────────────────
def evaluate(rec: RegistryRecord, provider,
             include_duplicates: bool = True) -> List[Finding]:
    """Evaluate one new document against everything the organisation knows.

    `include_duplicates=False` omits the DUPLICATE findings. That is not a
    weakening: callers that already run the dedup engine over a batch report
    duplicates as PAIRWISE relationships (A vs B), which is the right shape for
    them — a duplicate is a claim about two documents, not a property of one.
    Emitting both would show the same fact twice under two vocabularies. The
    other findings ARE properties of the single document under evaluation, so
    they belong here.

    Returns findings — never a decision. The policy engine turns findings into
    Approve / Review / Reject, so a customer can change policy without touching
    reasoning, and new findings never require new workflow states.

    Nothing is stored. Findings are recomputed from facts on every call, so they
    are always current: when the engine improves, yesterday's documents get
    today's reasoning for free.
    """
    findings: List[Finding] = []
    findings += _check_internal(rec)
    if include_duplicates:
        findings += _check_duplicates(rec, provider)
    findings += _check_po(rec, provider)
    findings += _check_tax_id(rec, provider)
    findings += _check_tax_rate(rec, provider)
    findings += _check_number_format(rec, provider)
    findings += _check_payments(rec, provider)
    return findings


def evaluate_dict(rec: RegistryRecord, provider,
                  include_duplicates: bool = True) -> Dict[str, Any]:
    """`evaluate` as plain data, grouped by kind — the API/UI shape."""
    fs = evaluate(rec, provider, include_duplicates=include_duplicates)
    by_kind: Dict[str, List[Dict[str, Any]]] = {}
    for f in fs:
        by_kind.setdefault(f.kind, []).append(f.to_dict())
    return {
        "subject": rec.doc_id,
        "doc_type": rec.doc_type,
        "finding_count": len(fs),
        "kinds": sorted(by_kind),
        "findings": [f.to_dict() for f in fs],
        "by_kind": by_kind,
    }
