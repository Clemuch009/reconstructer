# api/routes/reconcile.py
#
# Reconciliation endpoints — the seam that connects the general core to the
# reconciliation engine over HTTP.
#
# Two endpoints, sharing internals (the "both" design):
#
#   POST /reconcile
#       Full flow for the workspace: two RAW documents in → each runs through
#       the general core (engine.run) → resolver+profile → Document Views →
#       reconcile_documents → recommend → verdict + recommendation out.
#       The client sends text; the server does core extraction AND reconciliation.
#
#   POST /reconcile/views
#       Resolver-level for API users who ALREADY have structured data: two
#       Document Views in → reconcile_documents → recommend. Skips the core.
#
# Both call the same _reconcile_views() core, so behavior is identical once the
# data is structured — the only difference is whether the server also extracts.

import asyncio
import re as _re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form

from api.middleware.auth import require_auth, RequestContext
from registry.factory import get_registry
from registry.provider import DryRunOverlay
from registry.ingest import record_from_view
from registry.record import fields_checksum
from registry.consistency import evaluate
from cases.service import (get_case_store, open_case_for, precedent_evidence,
                           NEEDS_CASE)
from pydantic import BaseModel

from core.engine import TextReconstructionEngine
from analysis.segment_filter import extract_kv_pairs, filter_segments
from profiles.resolver import resolve_document_view
from profiles import get_profile                      # profile registry
from relationship_engine.reconcile import reconcile_documents
from relationship_engine.approval import recommend
from api.report import wrap

router = APIRouter()
_engine = TextReconstructionEngine()


# ── shared internals ───────────────────────────────────────────────────────

_STRUCTURAL_MARKER_RE = _re.compile(r"\[(?:PAGE|VISUAL|TABLE|IMAGE)\s*:[^\]]*\]")


def readable_text(text: str) -> str:
    """The text minus the extractor's own structural markers.

    "[PAGE: 1]" and "[VISUAL: vis_001]" are things WE wrote, not things the
    document said. A scanned invoice extracts to exactly those and nothing else:

        [PAGE: 1]
        [VISUAL: vis_001]
        [PAGE: 2]
        [VISUAL: vis_002]

    which is 114 characters of our own annotations describing four pictures. A
    plain `if text.strip()` check calls that readable and lets it through — and
    it did, the moment the visual extractor started working again. The document
    has no text; only our commentary about it does.
    """
    return _STRUCTURAL_MARKER_RE.sub("", text or "").strip()


def _require_text(text: str, what: str = "") -> str:
    """A document with no text layer is a known condition, not a server error.

    A scanned invoice — paper through a photocopier, or a PDF that is one big
    image — extracts to "". The engine then raises TypeError("Input must be a
    non-empty string"), which nothing catches, and the caller receives a 500 and
    a stack trace. That is the single most common document in an AP department,
    and we answered it with a crash.

    We cannot read it: Qrynt reads text and does not do OCR. That is a limit, and
    the right response to a limit is to name it, not to fail like a bug. 422 says
    "I understood the request and cannot process this entity", which is exactly
    true, and the message tells the person what to do about it.
    """
    if readable_text(text):
        return text
    raise HTTPException(
        status_code=422,
        detail=(f"{what or 'The document'} contains no readable text — it is "
                "most likely a scan or an image-only PDF. Qrynt reads a "
                "document's text layer and does not perform OCR, so there is "
                "nothing here to extract. Run it through OCR and submit the "
                "result."),
    )


def _text_to_view(text: str, doc_profile: Dict[str, Any]) -> Dict[str, Any]:
    """Run one raw document through the GENERAL CORE and resolve it into a
    Document View using the given document profile. This is the exact path the
    Python tests use — the same engine, not a reimplementation."""
    output = _engine.run(text)
    segments = output["machine_readable"]["segments"]
    kvs = extract_kv_pairs(segments)
    tables = [m["content"] for m in filter_segments(segments, types=["table"])]
    lines = [l for l in text.split("\n")]
    return resolve_document_view(kvs, tables, doc_profile, lines=lines)


def _reconcile_views(
    view_a: Dict[str, Any],
    view_b: Dict[str, Any],
    recon_profile: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The shared reconciliation core: verdict + recommendation. Both endpoints
    end here, so structured-data API users and raw-document workspace users get
    identical reconciliation behavior."""
    verdict = reconcile_documents(view_a, view_b, recon_profile)
    recommendation = recommend(verdict, context or {})
    return {"verdict": verdict, "recommendation": recommendation}


# ── request models ──────────────────────────────────────────────────────────

class ReconcileRawRequest(BaseModel):
    source_a:        str            # raw text of document A
    source_b:        str            # raw text of document B
    profile_a:       str = "invoice"          # document profile for A
    profile_b:       str = "purchase_order"   # document profile for B
    reconciliation:  str = "invoice_vs_po"    # reconciliation profile id

class ReconcileViewsRequest(BaseModel):
    view_a:          Dict[str, Any]           # already-structured Document View
    view_b:          Dict[str, Any]
    reconciliation:  str = "invoice_vs_po"


# ── reconciliation profile registry ────────────────────────────────────────
# Reconciliation profiles live alongside document profiles. Kept explicit here
# so an unknown id fails loudly rather than silently mismatching.

def _get_reconciliation_profile(recon_id: str) -> Dict[str, Any]:
    if recon_id == "invoice_vs_po":
        from profiles.invoice_po import INVOICE_PO_RECONCILIATION
        return INVOICE_PO_RECONCILIATION
    if recon_id == "cash_vs_ledger":
        from profiles.reconciliation import CASH_VS_LEDGER_PROFILE
        return CASH_VS_LEDGER_PROFILE
    raise HTTPException(status_code=400,
                        detail=f"unknown reconciliation profile: {recon_id}")


# ── endpoints ───────────────────────────────────────────────────────────────

@router.post("/reconcile")
async def reconcile_raw(req: ReconcileRawRequest,
                        dry_run: bool = False,
                        ctx: RequestContext = Depends(require_auth)):
    """Full flow: two raw documents → core → resolve → reconcile → recommend."""
    try:
        doc_profile_a = get_profile(req.profile_a)
        doc_profile_b = get_profile(req.profile_b)
    except Exception:
        raise HTTPException(status_code=400,
                            detail=f"unknown document profile: {req.profile_a} / {req.profile_b}")
    recon_profile = _get_reconciliation_profile(req.reconciliation)

    loop = asyncio.get_event_loop()
    # run the (blocking) core extraction off the event loop
    view_a = await loop.run_in_executor(None, _text_to_view, req.source_a, doc_profile_a)
    view_b = await loop.run_in_executor(None, _text_to_view, req.source_b, doc_profile_b)

    result = _reconcile_views(view_a, view_b, recon_profile)
    # include the resolved views so the workspace can show extracted fields/lines
    result["view_a"] = view_a
    result["view_b"] = view_b
    # Reconcile is an ingestion event too. Both documents are real evidence, and
    # the purchase order entering history is the point: every FUTURE invoice
    # citing it is checked against it, without anyone reconciling again.
    result.update(await loop.run_in_executor(
        None, _ingest_and_evaluate, [view_a, view_b], doc_profile_a, ctx.uid,
        "reconcile", dry_run,
        [(doc_profile_a.get("metadata") or {}).get("id"),
         (doc_profile_b.get("metadata") or {}).get("id")]))
    return wrap(result)


@router.post("/reconcile/views")
async def reconcile_views(req: ReconcileViewsRequest):
    """Resolver-level: two Document Views (already structured) → reconcile."""
    recon_profile = _get_reconciliation_profile(req.reconciliation)
    return _reconcile_views(req.view_a, req.view_b, recon_profile)


@router.post("/reconcile/files")
async def reconcile_files(
    file_a: UploadFile = File(...),
    file_b: UploadFile = File(...),
    profile_a: str = Form("invoice"),
    profile_b: str = Form("purchase_order"),
    reconciliation: str = Form("invoice_vs_po"),
    dry_run: bool = False,
    ctx: RequestContext = Depends(require_auth),
):
    """Reconcile two UPLOADED files (PDF/DOCX/XLSX/CSV/HTML/TXT/JSON): ingest
    each to text, resolve to Document Views, then reconcile. Same core as
    /reconcile — this is the binary-upload bridge for the workspace's reconcile
    mode so a PDF invoice can be reconciled against a PDF purchase order."""
    doc_profile_a = get_profile(profile_a)
    doc_profile_b = get_profile(profile_b)
    if doc_profile_a is None or doc_profile_b is None:
        raise HTTPException(status_code=400,
                            detail=f"unknown document profile: {profile_a} / {profile_b}")
    recon_profile = _get_reconciliation_profile(reconciliation)

    raw_a = await file_a.read()
    raw_b = await file_b.read()
    loop = asyncio.get_event_loop()

    def _ingest_to_view(raw, filename, doc_profile):
        from ingestion.router import ingest
        ing = ingest(raw, filename=filename)
        _require_text(ing["text"], filename or "This file")
        return _text_to_view(ing["text"], doc_profile), ing.get("source_format")

    view_a, fmt_a = await loop.run_in_executor(None, _ingest_to_view, raw_a, file_a.filename, doc_profile_a)
    view_b, fmt_b = await loop.run_in_executor(None, _ingest_to_view, raw_b, file_b.filename, doc_profile_b)

    result = _reconcile_views(view_a, view_b, recon_profile)
    result["view_a"] = view_a
    result["view_b"] = view_b
    result["file_a"] = {"filename": file_a.filename, "source_format": fmt_a}
    result["file_b"] = {"filename": file_b.filename, "source_format": fmt_b}
    result.update(await loop.run_in_executor(
        None, _ingest_and_evaluate, [view_a, view_b], doc_profile_a, ctx.uid,
        f"reconcile:{file_a.filename or ''}+{file_b.filename or ''}", dry_run,
        [profile_a, profile_b]))
    return wrap(result)


# ── single-document processing (Process mode) ──────────────────────────────
#
# Extract + validate ONE document through the general core. This is what the
# workspace's Process mode calls, so pasted text and uploaded files run the
# real engine (not a client-side parser).

# Rules whose question the role verifier answers better. Keyed by rule id, so a
# customer's own rules are never touched — only ones we shipped.
_VERIFIER_SUPERSEDES = {"totals_balance"}


def _apply_verifier_precedence(report: dict, view: dict) -> dict:
    """When arithmetic has CONFIRMED the total, a rule that assumed a different
    composition is not evidence against it.

    `totals_balance` asserts  subtotal + tax == total.  That is not a check, it
    is an ASSUMPTION about how an invoice is built — and ordinary commerce
    breaks it: discounts, shipping, credits, retention, withholding. Vanguard is
    a real example:

        28,310 − 2,196.50 (volume discount) + 363 = 26,476.50   ← correct invoice
        28,310 + 363 = 28,673 ≠ 26,476.50                        ← rule says FAIL

    The invoice is right and the rule is wrong. It cannot be fixed either:
    `operation: sum` cannot subtract, `discount` is not an extracted field, and
    adding one invites shipping, then credits, then retention — the alias
    treadmill in arithmetic form.

    The role verifier does not assume a composition, it SEARCHES for one, and it
    found this invoice's: "gross subtotal, volume discount, calculated vat/tax".
    Same document, same numbers, correct answer — and it still reports
    invoice_inv90812's real $500 over-charge as INCONSISTENT. Its coverage is a
    superset of the rule's with better precision.

    So this is ORDINAL PRECEDENCE, the same principle the verifier already uses
    internally: a SEARCHED arithmetic identity outranks an ASSUMED formula. Both
    analyses read the SAME document; the verifier's is strictly better informed
    because it established the actual composition.

    Note what this is NOT. It is not the precedent-style suppression rejected in
    cases/store.py, where a HUMAN decision would disable a FUTURE check on
    DIFFERENT documents and institutionalise the first approved fraud. Here,
    stronger evidence about THIS document outranks weaker evidence about the
    same document. That is just reasoning correctly.

    And nothing is deleted. The rule's result stays in the report, restated as
    SUPERSEDED with the reason — report, never overwrite. A caller that wants
    the raw rule outcome still has it; it simply stops being counted as an
    exception a human must clear.
    """
    ver = (view.get("verification") or {}).get("total") or {}
    if ver.get("verdict") != "CONFIRM":
        return report                     # nothing established — rule stands

    changed = 0
    for r in report.get("results", []):
        if r.get("rule_id") in _VERIFIER_SUPERSEDES and r.get("status") == "FAIL":
            r["status"] = "SUPERSEDED"
            r["superseded_by"] = "role_verifier"
            r["reason"] = (
                "the total was confirmed by the document's own arithmetic "
                + (f"({ver.get('evidence')})" if ver.get("evidence") else "")
                + " — this rule assumes subtotal + tax = total, which does not "
                  "hold for invoices carrying a discount, shipping or credit"
            )
            changed += 1

    if changed:
        s = report.get("summary") or {}
        s["failed"] = max(0, int(s.get("failed", 0)) - changed)
        s["superseded"] = int(s.get("superseded", 0)) + changed
        report["summary"] = s
    return report


def _process_view(view: Dict[str, Any], doc_profile: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a resolved Document View against its profile's rules and shape
    the response the workspace expects."""
    from analysis.rules_engine import evaluate
    rules = doc_profile.get("rules", [])
    report = evaluate(view, rules) if rules else {"results": [], "summary": {}}
    report = _apply_verifier_precedence(report, view)
    return {
        "fields":     view.get("fields", {}),
        "tables":     view.get("tables", {}),
        "validation": report,
        # Role verification: the resolver checks its LABEL-derived answers
        # against the document's own arithmetic, which no relabelling can alter.
        # It was already being computed and then dropped here — so a document
        # whose stated total contradicts its own subtotal+tax was reported clean
        # unless a label-based rule happened to catch it. Passing it through
        # costs nothing and is the only check an adversary cannot rename away.
        "verification": view.get("verification", {}),
    }


class ProcessTextRequest(BaseModel):
    source:  str                    # raw document text
    profile: str = "invoice"        # document profile id


def _has_usable_identity(canonical: Dict[str, Any]) -> bool:
    """Could this assertion ever match anything?

    Making Process a WRITE means extraction failures become permanent knowledge.
    The registry is append-only and, by decision, kept indefinitely — so a
    document that did not extract must never enter it. A real example: a pasted
    JSON array of three invoices renders as one CSV-shaped region, resolves to

        {'document_type': 'INVOICE', 'vendor': 'invoice_number,vendor,total,currency'}

    — the CSV HEADER stored as the vendor, no number, no amount. As a screenful
    of nonsense that was merely unhelpful. As an assertion it is corrosive: it is
    permanent, it can never match a real document, and it pollutes the vendor
    baselines that TAX_ID_CHANGED and NUMBER_FORMAT_DEVIATION depend on.

    A usable identity is the minimum that makes a record capable of being
    evidence later:
      * an invoice number, or
      * a PO reference, or
      * a vendor AND an amount (the vendor_amount_currency key)

    Anything less cannot be matched by any query the registry supports, so
    storing it adds noise and no recall. This is a guard on WRITING, not on
    reporting: the document is still extracted, validated, verified and shown.
    We simply decline to call a failed extraction organisational knowledge — and
    we say so, rather than dropping it silently.
    """
    if canonical.get("invoice_number_canon"):
        return True
    if canonical.get("po_number_canon"):
        return True
    if canonical.get("vendor_canon") and canonical.get("amount_canon") is not None:
        return True
    return False


def _ingest_and_evaluate(docs: List[Dict[str, Any]], doc_profile: Dict[str, Any],
                         uid: Optional[str], source: str = "",
                         dry_run: bool = False,
                         doc_types: Optional[List[str]] = None) -> Dict[str, Any]:
    """Make these documents part of what the organisation knows, then say what
    that knowledge implies.

    ── Why Process writes ────────────────────────────────────────────────
    Processing a document IS the moment new evidence enters the system. For a
    long time only /dedupe recorded anything, which inverted the product: a
    person's normal workflow is to process invoices, while deduplication is a
    specific check they might never run. The consequence was quiet and total —
    vendor baselines (tax id, tax rate, number format) need N>=3 observations of
    one vendor, and nobody dedupes the same vendor three times. The strongest
    finding we have, TAX_ID_CHANGED, could not fire from ordinary use.

    ── Order: commit, THEN evaluate ──────────────────────────────────────
    Extraction never writes to the registry directly. A document becomes an
    ASSERTION first — raw fields, provenance, engine version, no conclusions —
    and only then is anything reasoned from it.

    But the assertion is committed BEFORE evaluation, not after. Evaluating
    first and committing after reads a registry the document is not in yet:
    submit three identical invoices and each one is evaluated against a world
    where the other two do not exist, producing three "no duplicate" answers and
    three duplicate payments. We hit exactly this at a smaller scale — an
    invoice reported PO_NOT_FOUND with its purchase order in the same request.
    Every check already excludes its subject by doc_id, so a document is never
    evidence about itself; committing first is what makes its SIBLINGS visible.

    ── include_duplicates=True — the opposite of /dedupe ─────────────────
    /dedupe reports duplicates as pairwise relationships (A vs B), which is
    their true shape there, so it suppresses them here to avoid saying the same
    thing twice under two vocabularies. Process has no relationship list. If
    duplicates were suppressed here, "you already processed this invoice in
    June" would simply vanish — which is the single most valuable thing
    processing can tell you.

    ── Findings are computed, never stored ───────────────────────────────
    Nothing about a conclusion is written back. The registry holds what
    documents asserted; findings are re-derived on every read, so they stay
    correct when a correction arrives or the engine improves — and
    canonicalisation improved five times in one session.
    """
    # One profile for a batch of like documents, or one type PER document when
    # the caller has a mixed pair — reconcile hands us an invoice and a purchase
    # order together, and recording the PO as an invoice would make the registry
    # assert something the document never said.
    default_type = (doc_profile.get("metadata") or {}).get("id") or "invoice"
    if doc_types is not None and len(doc_types) != len(docs):
        raise HTTPException(
            status_code=500,
            detail="doc_types must line up with docs — refusing to guess which "
                   "document is which")
    registry, reason = get_registry(uid)

    # A dry run answers "what WOULD happen?" — the same question, against the
    # same history, by the same code, with nothing left behind. The overlay
    # still takes the writes (so siblings see each other, exactly as in a real
    # run) but the base registry is only ever read through it.
    if dry_run and registry is not None:
        registry = DryRunOverlay(registry)

    findings: Dict[str, List[Dict[str, Any]]] = {}
    out: Dict[str, Any] = {
        "dry_run": bool(dry_run),
        "registry": {"consulted": registry is not None, "reason": reason,
                     "documents_recorded": 0},
        "findings": {}, "finding_count": 0,
        "cases": [], "case_store": {"available": False, "reason": "", "opened": 0},
    }
    if registry is None or not docs:
        return out

    # ── commit the assertions ────────────────────────────────────────────
    pending = []
    skipped = 0
    for idx, d in enumerate(docs):
        dt = doc_types[idx] if doc_types is not None else default_type
        try:
            rr = record_from_view(
                None,                      # mint a fresh per-submission id; a
                dt,                        # reused id would overwrite history
                {"fields": d.get("fields") or {},
                 "tables": d.get("tables") or {},
                 # The verifier's findings travel with the view: where a vendor
                 # used a total label no alias knows, the arithmetic identified
                 # the total anyway, and the registry should record THAT rather
                 # than nothing (see registry/ingest.py).
                 "verification": d.get("verification") or {}},
                source=source,
                checksum=fields_checksum(d.get("fields") or {}),
            )
            if not _has_usable_identity(rr.canonical):
                skipped += 1
                continue
            registry.append(rr)
            pending.append(rr)
            out["registry"]["documents_recorded"] += 1
        except Exception as e:                      # noqa: BLE001 — reported
            out["registry"]["reason"] = f"registry write failed ({type(e).__name__})"
            break

    # ── then evaluate against everything known, including these ──────────
    for rr in pending:
        try:
            fs = evaluate(rr, registry, include_duplicates=True)
            if fs:
                findings[rr.doc_id] = [f.to_dict() for f in fs]
        except Exception as e:                      # noqa: BLE001
            out["registry"]["reason"] = f"evaluation failed ({type(e).__name__})"
            break

    if dry_run:
        # documents_recorded counted the overlay's writes; nothing reached the
        # registry, and the wording must not imply otherwise.
        out["registry"]["would_record"] = out["registry"]["documents_recorded"]
        out["registry"]["documents_recorded"] = 0
        out["registry"]["reason"] = (
            "dry run — evaluated against your history, nothing was added to it")

    if skipped:
        # Reported, never silent: the caller must know this document did not
        # enter their history, and why.
        out["registry"]["not_recorded"] = skipped
        out["registry"]["reason"] = (
            "not added to your history — no invoice number, PO reference, or "
            "vendor+amount could be extracted, so it could never be matched")

    out["findings"] = findings
    out["finding_count"] = sum(len(v) for v in findings.values())

    # ── findings that need a person become cases ─────────────────────────
    # The queue receives FINDINGS, not documents. Each case carries any
    # precedent — prior decisions about this kind of dispute for this vendor —
    # as EVIDENCE to read. Precedent never suppresses the finding: a resolution
    # that silently disabled a check would institutionalise the first approved
    # fraud (see cases/store.py).
    case_store, case_reason = get_case_store(uid)
    out["case_store"] = {"available": case_store is not None,
                         "reason": case_reason, "opened": 0}
    if case_store is None:
        return out
    if dry_run:
        # Opening a case is state, and a simulation creates none. Report what
        # WOULD be raised instead — that is the answer the question wanted.
        out["case_store"]["would_open"] = sum(
            1 for fs in findings.values() for f in fs
            if f["kind"] != "DUPLICATE"
            or (f.get("detail") or {}).get("suggested_action") in NEEDS_CASE)
        out["case_store"]["reason"] = "dry run — no cases opened"
        return out

    vendor_of = {rr.doc_id: rr.canonical.get("vendor_canon") for rr in pending}
    opened: List[Dict[str, Any]] = []
    # Informational findings are surfaced to the reader but do NOT open a case.
    # PO_NOT_FOUND is the normal state when an invoice is processed before (or
    # without) its purchase order — the PO simply is not in Qrynt yet. Opening a
    # case for it fills the queue with the expected, not the actionable, which is
    # the contamination we must avoid: a reviewer's queue should hold problems,
    # not the routine absence of a document that may live only in the ERP.
    INFORMATIONAL = {"PO_NOT_FOUND"}
    for doc_id, fs in findings.items():
        for f in fs:
            action = (f.get("detail") or {}).get("suggested_action")
            if f["kind"] in INFORMATIONAL:
                continue                       # information, not a case
            # A duplicate finding carries the policy action from the dedup
            # engine; the other kinds are conflicts a person should see.
            if f["kind"] == "DUPLICATE" and action not in NEEDS_CASE:
                continue
            vc = vendor_of.get(f.get("subject") or doc_id)
            c = open_case_for(case_store, f["kind"], f.get("subject") or doc_id,
                              f.get("summary", ""), f.get("evidence_from", []),
                              vc, f)
            if c is None:
                continue
            entry = {"case_id": c.case_id, "kind": c.kind, "status": c.status,
                     "subject": c.subject, "counterparts": c.counterparts,
                     "summary": c.summary}
            pres = precedent_evidence(case_store, f["kind"], vc)
            if pres:
                entry["precedent"] = pres
            opened.append(entry)

    out["cases"] = opened
    out["case_store"]["opened"] = len(opened)
    return out


def _process_source(text: str, doc_profile: Dict[str, Any],
                    uid: Optional[str] = None,
                    source: str = "",
                    dry_run: bool = False) -> Dict[str, Any]:
    """Process one raw source through partition → core → resolve → validate.

    If the source contains multiple logical documents (a JSON array of invoices,
    a batched file), partition splits them and each is processed independently.
    Returns the first document as the primary result, plus a `documents` list
    when there's more than one — so a pasted JSON array of 3 invoices returns 3.
    """
    from document_partition import partition
    from document_partition.json_signals import emit_signals, render_single

    regions = partition(
        emit_signals(text),
        fallback_content=render_single(text),
        source="text",
    )

    docs = []
    for reg in regions:
        view = _text_to_view(reg.content if reg.content else text, doc_profile)
        docs.append(_process_view(view, doc_profile))

    if not docs:
        docs = [_process_view(_text_to_view(text, doc_profile), doc_profile)]

    # Ingestion. Partition-aware: a source holding three invoices records THREE
    # assertions, not one — each is a separate claim and each is evaluated on
    # its own terms.
    ingest_result = _ingest_and_evaluate(docs, doc_profile, uid, source, dry_run)

    primary = dict(docs[0])
    if len(docs) > 1:
        primary["documents"] = docs
        primary["document_count"] = len(docs)
    primary.update(ingest_result)
    return primary


@router.post("/process-text")
async def process_text(req: ProcessTextRequest,
                       dry_run: bool = False,
                       ctx: RequestContext = Depends(require_auth)):
    """Full single-document flow: raw text → partition → core → resolve →
    validate. A JSON array or batched input yields multiple documents."""
    try:
        doc_profile = get_profile(req.profile)
        if doc_profile is None:
            raise ValueError(req.profile)
    except Exception:
        raise HTTPException(status_code=400,
                            detail=f"unknown document profile: {req.profile}")
    loop = asyncio.get_event_loop()
    _require_text(req.source, "The pasted text")
    result = await loop.run_in_executor(
        None, _process_source, req.source, doc_profile, ctx.uid, "pasted", dry_run)
    return wrap(result)


@router.post("/process-file")
async def process_file(file: UploadFile = File(...), profile: str = Form("invoice"),
                       dry_run: bool = False,
                       ctx: RequestContext = Depends(require_auth)):
    """Upload a file (PDF/DOCX/XLSX/CSV/HTML/TXT/JSON) → ingest → core →
    resolve → validate. No auth (unlike /ingest/file) so the workspace can use
    it directly for the demo. Returns the same shape as /process-text."""
    doc_profile = get_profile(profile)
    if doc_profile is None:
        raise HTTPException(status_code=400, detail=f"unknown document profile: {profile}")

    raw = await file.read()
    loop = asyncio.get_event_loop()

    def _ingest_and_process():
        from ingestion.router import ingest
        ing = ingest(raw, filename=file.filename)
        _require_text(ing["text"], file.filename or "This file")
        result = _process_source(ing["text"], doc_profile, ctx.uid, file.filename or "", dry_run)
        result["source_format"] = ing.get("source_format")
        result["filename"] = file.filename
        return result

    result = await loop.run_in_executor(None, _ingest_and_process)
    return wrap(result)
