# api/routes/dedupe.py
#
# Duplicate-detection endpoint — the seam connecting the general core to the
# batch duplicate engine over HTTP.
#
#   POST /dedupe
#       A SET of raw documents in → each runs through the general core
#       (engine.run) → resolver+profile → Document View fields → the batch
#       duplicate engine (canonicalize → identities → candidates → evidence →
#       classify → policy) → typed relationships with actions out.
#
# The set provided is ONE source of candidates. The registry supplies the others:
# every document this user has processed before. That distinction is the whole
# game — real duplicate submissions arrive DAYS OR WEEKS APART, not in one
# upload, so batch-only detection fires almost never in production. Every
# adversarial case in the test corpus (disguised statement, EN/DE, Vanguard,
# Vertex collision) only reproduced because both files were uploaded together.
#
# Same engine either way: stages 1-6 never learn where a candidate came from.
# The provider seam does that work, so an ERP can be added later as one more
# source without touching any reasoning.

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

from core.engine import TextReconstructionEngine
from analysis.segment_filter import extract_kv_pairs, filter_segments
from profiles.resolver import resolve_document_view
from profiles import get_profile
from relationship_engine.dedup_engine import make_records, analyze
from api.middleware.auth import require_auth, RequestContext
from registry.factory import get_registry
from registry.provider import DryRunOverlay
from api.routes.reconcile import _require_text, readable_text
from registry.ingest import record_from_view
from registry.consistency import evaluate
from cases.service import (get_case_store, open_case_for, precedent_evidence,
                           NEEDS_CASE)
from registry.record import fields_checksum
from api.report import wrap

router = APIRouter()
_engine = TextReconstructionEngine()


def _text_to_fields(text: str, doc_profile: Dict[str, Any]) -> Dict[str, Any]:
    """Raw document → resolved Document View fields, via the general core. Same
    path the reconcile endpoint uses."""
    output = _engine.run(text)
    segments = output["machine_readable"]["segments"]
    kvs = extract_kv_pairs(segments)
    tables = [m["content"] for m in filter_segments(segments, types=["table"])]
    lines = text.split("\n")
    view = resolve_document_view(kvs, tables, doc_profile, lines=lines)
    fields = dict(view["fields"])
    # carry line items alongside fields so canonicalization can recover a missing
    # total by summing them (arithmetic the other modes do; dedup should too).
    li = view.get("tables", {}).get("line_items")
    if li:
        fields["_line_items"] = li
    return fields


class DedupeDoc(BaseModel):
    id: Optional[str] = None
    text: str
    profile: str = "invoice"


class DedupeRequest(BaseModel):
    documents: List[DedupeDoc]
    dayfirst: bool = False


def _run_dedupe(items: List[Dict[str, Any]], dayfirst: bool,
                uid: Optional[str] = None,
                dry_run: bool = False) -> Dict[str, Any]:
    """Resolve each document to fields, gather candidates from BOTH the current
    request and the user's history, run the engine, then record the new
    documents as assertions.

    Order matters: history is read BEFORE anything is written, so a document
    never matches itself.
    """
    invoices: List[Dict[str, Any]] = []
    resolved: Dict[str, Dict[str, Any]] = {}
    for i, it in enumerate(items):
        # Always make the id unique per position. Two uploads can share a
        # filename (the same file added twice — the strongest duplicate case);
        # if their ids collided, the engine would treat them as ONE record and
        # find no pair. Prefix with the position, keep the label for display.
        label = it.get("id") or f"doc_{i+1}"
        rid = f"{i+1}·{label}"
        profile = get_profile(it.get("profile", "invoice"))
        if profile is None:
            raise HTTPException(status_code=400, detail=f"unknown profile: {it.get('profile')}")
        fields = _text_to_fields(it["text"], profile)
        # The profile id IS the document type. The registry is type-agnostic on
        # purpose — an invoice must be able to find the purchase_order it cites,
        # and a payment must be findable against the invoice it settles. Writing
        # everything as "invoice" silently defeats that: a PO stored as an
        # invoice is invisible to by_reference("purchase_order", ...) and the
        # invoice citing it reports PO_NOT_FOUND.
        invoices.append({"id": rid, "fields": fields, "label": label,
                         "doc_type": it.get("profile", "invoice")})
        resolved[rid] = fields

    records = make_records(invoices, dayfirst=dayfirst)
    registry, reason = get_registry(uid)
    # "Would these create duplicates?" — same history, same engine, nothing kept.
    if dry_run and registry is not None:
        registry = DryRunOverlay(registry)

    # ── read history BEFORE writing, so a document cannot match itself ─────
    priors: Dict[str, Any] = {}
    if registry is not None:
        for inv, rec in zip(invoices, records):
            try:
                for p in registry.candidates_for(rec, doc_type=inv["doc_type"]):
                    if p.id not in {r.id for r in records}:
                        priors[p.id] = p
            except TypeError:
                for p in registry.candidates_for(rec):
                    if p.id not in {r.id for r in records}:
                        priors[p.id] = p
            except Exception as e:              # noqa: BLE001
                reason = f"registry read failed ({type(e).__name__}) — batch only"
                priors = {}
                break

    universe = records + list(priors.values())
    result = analyze(universe, policy=None)
    # Priors are matched from history, so their fields are not in `resolved`.
    # Without this the UI renders a relationship card against an id with no
    # detail — the user is told "this duplicates something" and cannot see what.
    for pid, p in priors.items():
        resolved.setdefault(pid, dict(getattr(p, "fields", {}) or {}))
    result["resolved_fields"] = resolved      # id → fields, for side-by-side UI

    # ── record the assertions, THEN evaluate ──────────────────────────────
    # Write first, evaluate second. Every consistency check already excludes the
    # subject by doc_id, so a document can never be evidence about itself — but
    # if evaluation ran before the writes, documents in the SAME request would be
    # invisible to each other, and an invoice uploaded alongside the purchase
    # order it cites would report PO_NOT_FOUND with the PO sitting right there.
    #
    # include_duplicates=False: duplicates are already reported above as pairwise
    # relationships (A vs B), which is their true shape. The findings here are
    # properties of THIS document — its own arithmetic, the PO it cites, its tax
    # id against this vendor's history, payments already recorded against it.
    findings: Dict[str, List[Dict[str, Any]]] = {}
    written = 0
    if registry is not None:
        pending = []
        for inv, rec in zip(invoices, records):
            try:
                # doc_id=None → a fresh per-submission id. Reusing rec.id
                # ("1·invoice.pdf") would repeat every month and overwrite the
                # previous assertion in Firestore, destroying history silently.
                rr = record_from_view(
                    None, inv["doc_type"], {"fields": inv["fields"]},
                    source=inv.get("label") or "",
                    checksum=fields_checksum(inv["fields"]),
                )
                registry.append(rr)
                pending.append(rr)
                written += 1
            except Exception as e:              # noqa: BLE001
                reason = f"registry write failed ({type(e).__name__})"
                break
        for rr in pending:
            try:
                fs = evaluate(rr, registry, include_duplicates=False)
                if fs:
                    findings[rr.doc_id] = [f.to_dict() for f in fs]
            except Exception as e:              # noqa: BLE001
                reason = f"consistency evaluation failed ({type(e).__name__})"
                break

    # ── findings → cases ──────────────────────────────────────────────────
    # A finding that needs a person becomes a CASE: a finding that persists and
    # can be closed. No new taxonomy — `kind` IS the finding kind, and the
    # workflow actions are unchanged. Findings are many; decisions stay few.
    #
    # Each case carries any PRECEDENT: prior decisions about this kind of
    # dispute for this vendor. Precedent is EVIDENCE the reviewer reads — it
    # never suppresses, downgrades, or auto-closes the finding. A resolution
    # that silently disabled a check would institutionalise the first fraud
    # anyone approved. See cases/store.py.
    case_store, case_reason = get_case_store(uid)
    if dry_run:
        case_store = None          # a simulation creates no state
    # Vendor per subject, for precedent lookup. Keyed by BOTH id spaces: a
    # relationship names batch record ids ("1·october_a.pdf") while a consistency
    # finding names the registry assertion id ("october_a.pdf·9f2c..."). Keying
    # only one way silently loses precedent — the case still opens, so nothing
    # looks broken; the reviewer just never learns this was decided in July.
    vendor_of: Dict[str, Optional[str]] = {}
    for _rec in records:
        vendor_of[_rec.id] = (_rec.canon or {}).get("vendor_canon")
    for _p in priors.values():
        vendor_of[_p.id] = (_p.canon or {}).get("vendor_canon")
    if registry is not None:
        for _rr in pending:
            vendor_of[_rr.doc_id] = _rr.canonical.get("vendor_canon")
    opened: List[Dict[str, Any]] = []

    def _open(kind, subject, summary, counterparts, finding_snapshot):
        vc = vendor_of.get(subject)
        c = open_case_for(case_store, kind, subject, summary,
                          counterparts, vc, finding_snapshot)
        if c is None:
            return
        entry = {"case_id": c.case_id, "kind": c.kind, "status": c.status,
                 "subject": c.subject, "counterparts": c.counterparts,
                 "summary": c.summary}
        pres = precedent_evidence(case_store, kind, vc)
        if pres:
            entry["precedent"] = pres
        opened.append(entry)

    if case_store is not None:
        for rel in result.get("relationships", []):
            if rel.get("action") in NEEDS_CASE:
                _open(rel.get("finding") or rel.get("type"), rel["a_id"],
                      rel.get("reason", ""), [rel["b_id"]], rel)
        for doc_id, fs in findings.items():
            for f in fs:
                _open(f["kind"], f["subject"], f.get("summary", ""),
                      f.get("evidence_from", []), f)

    result["cases"] = opened
    result["case_store"] = {"available": case_store is not None,
                            "reason": case_reason, "opened": len(opened)}

    # Availability is reported, never assumed. "No duplicates in this upload" and
    # "no duplicates in your history" are different claims; the caller must be
    # able to tell which one they just received.
    result["dry_run"] = bool(dry_run)
    result["registry"] = {
        "consulted":     registry is not None,
        "reason":        reason if not dry_run else
                         "dry run — evaluated against your history, nothing was added to it",
        "priors_found":  len(priors),
        # The overlay took the writes; the registry did not. Saying "recorded"
        # here would be a lie in the one place a caller is relying on us not to.
        "documents_recorded": 0 if dry_run else written,
    }
    if dry_run:
        result["registry"]["would_record"] = written
        result["case_store"] = {"available": False,
                                "reason": "dry run — no cases opened", "opened": 0}
    # id → [finding]. Non-duplicate consistency findings: incorrect data,
    # mismatched POs, missing/changed tax ids, tax deviation, payment conflicts.
    result["findings"] = findings
    result["finding_count"] = sum(len(v) for v in findings.values())
    return result


@router.post("/dedupe")
async def dedupe(req: DedupeRequest,
                 dry_run: bool = False,
                 ctx: RequestContext = Depends(require_auth)):
    """Detect duplicate relationships among a set of pasted documents, and
    against everything this user has processed before."""
    # ONE document is a valid request once history exists: "is this invoice a
    # duplicate of anything I have seen before?" is the real-world question —
    # duplicates arrive weeks apart, not in pairs. The <2 floor was a batch-mode
    # constraint. If no history is available the response says so
    # (registry.consulted=false) rather than the caller getting a narrow answer
    # dressed as a complete one.
    if not req.documents:
        raise HTTPException(status_code=400, detail="provide at least 1 document")
    items = [{"id": d.id, "text": d.text, "profile": d.profile} for d in req.documents]
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _run_dedupe, items, req.dayfirst, ctx.uid, dry_run)


@router.post("/dedupe/files")
async def dedupe_files(
    request: Request,
    files: List[UploadFile] = File(...),
    profile: str = Form("invoice"),
    dayfirst: bool = Form(False),
    dry_run: bool = False,
    ctx: RequestContext = Depends(require_auth),
):
    """Detect duplicates among a set of UPLOADED files (PDF/DOCX/XLSX/…). Each is
    ingested to text, resolved, then run through the batch engine."""
    # `profiles` is read off the raw form, NOT declared as List[str] = Form(None).
    #
    # Whether FastAPI turns a single repeated form field into a one-element list
    # or leaves it a bare string depends on the FastAPI/Pydantic version. On the
    # deployed version it does not coerce: one file means one `profiles` field,
    # which arrives as "invoice" and fails validation with
    #     {"loc":["body","profiles"],"msg":"Input should be a valid list"}
    # so deduplicating a SINGLE document — the case that matters most, since real
    # duplicates arrive weeks apart rather than in one upload — returned 422.
    #
    # This sandbox's FastAPI (0.139) DOES coerce, so every test here passed and
    # the bug was invisible. Depending on that behaviour is the bug; getlist()
    # returns a list for one value or ten, on every version.
    form = await request.form()
    profiles: Optional[List[str]] = form.getlist("profiles") or None
    # See /dedupe: one file is valid — it is checked against history.
    if not files:
        raise HTTPException(status_code=400, detail="upload at least 1 file")
    from ingestion.router import ingest

    # Per-file profiles. A single `profile` applied to every upload cannot
    # express the case the registry exists for — an invoice checked against the
    # purchase_order it cites. `profiles` is a parallel list; `profile` remains
    # the fallback for callers sending one kind.
    per_file = list(profiles) if profiles else []
    if per_file and len(per_file) != len(files):
        raise HTTPException(
            status_code=400,
            detail=f"profiles has {len(per_file)} entries but {len(files)} files were sent")
    resolved_profiles = per_file or [profile] * len(files)
    for p in set(resolved_profiles):
        if get_profile(p) is None:
            raise HTTPException(status_code=400, detail=f"unknown profile: {p}")

    async def _read(f: UploadFile):
        return f.filename, await f.read()
    raws = [await _read(f) for f in files]

    def _work() -> Dict[str, Any]:
        items = []
        skipped_unreadable = []
        for (fname, raw), prof in zip(raws, resolved_profiles):
            ing = ingest(raw, filename=fname)
            # A document with no text layer cannot be compared to anything. But
            # this is a BATCH: refusing the whole request because one member is
            # unreadable throws away the work on every other file, and that is
            # what happened — the workspace posts every slot including empty
            # ones, so a single blank textarea returned 422 for the entire
            # comparison, with a message about scans that made no sense for it.
            #
            # One document alone is different: /process-file has exactly one
            # thing to do and 422 is the whole truth. Here the honest answer is
            # to set the unreadable ones aside and SAY SO, then compare the rest.
            if not readable_text(ing["text"]):
                skipped_unreadable.append({
                    "filename": fname,
                    "reason": ("no readable text — an empty slot, or a scan / "
                               "image-only PDF (Qrynt reads a text layer and "
                               "does not perform OCR)"),
                })
                continue
            items.append({"id": fname, "text": ing["text"], "profile": prof})
        # ...unless there is nothing left to compare, which IS the whole truth.
        if not items:
            raise HTTPException(
                status_code=422,
                detail=("none of the submitted documents contain readable text — "
                        "they are most likely scans or image-only PDFs. Qrynt "
                        "reads a document's text layer and does not perform OCR."),
            )
        out = _run_dedupe(items, dayfirst, ctx.uid, dry_run)
        if skipped_unreadable:
            # Loud, not fatal: the caller must never think these were compared.
            out["skipped_unreadable"] = skipped_unreadable
        return out

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _work)
    return wrap(result)
