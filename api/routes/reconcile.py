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
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

from core.engine import TextReconstructionEngine
from analysis.segment_filter import extract_kv_pairs, filter_segments
from profiles.resolver import resolve_document_view
from profiles import get_profile                      # profile registry
from relationship_engine.reconcile import reconcile_documents
from relationship_engine.approval import recommend

router = APIRouter()
_engine = TextReconstructionEngine()


# ── shared internals ───────────────────────────────────────────────────────

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
async def reconcile_raw(req: ReconcileRawRequest):
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
    return result


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
        return _text_to_view(ing["text"], doc_profile), ing.get("source_format")

    view_a, fmt_a = await loop.run_in_executor(None, _ingest_to_view, raw_a, file_a.filename, doc_profile_a)
    view_b, fmt_b = await loop.run_in_executor(None, _ingest_to_view, raw_b, file_b.filename, doc_profile_b)

    result = _reconcile_views(view_a, view_b, recon_profile)
    result["view_a"] = view_a
    result["view_b"] = view_b
    result["file_a"] = {"filename": file_a.filename, "source_format": fmt_a}
    result["file_b"] = {"filename": file_b.filename, "source_format": fmt_b}
    return result


# ── single-document processing (Process mode) ──────────────────────────────
#
# Extract + validate ONE document through the general core. This is what the
# workspace's Process mode calls, so pasted text and uploaded files run the
# real engine (not a client-side parser).

def _process_view(view: Dict[str, Any], doc_profile: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a resolved Document View against its profile's rules and shape
    the response the workspace expects."""
    from analysis.rules_engine import evaluate
    rules = doc_profile.get("rules", [])
    report = evaluate(view, rules) if rules else {"results": [], "summary": {}}
    return {
        "fields":     view.get("fields", {}),
        "tables":     view.get("tables", {}),
        "validation": report,
    }


class ProcessTextRequest(BaseModel):
    source:  str                    # raw document text
    profile: str = "invoice"        # document profile id


def _process_source(text: str, doc_profile: Dict[str, Any]) -> Dict[str, Any]:
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

    primary = docs[0] if docs else _process_view(_text_to_view(text, doc_profile), doc_profile)
    if len(docs) > 1:
        primary = dict(primary)
        primary["documents"] = docs
        primary["document_count"] = len(docs)
    return primary


@router.post("/process-text")
async def process_text(req: ProcessTextRequest):
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
    return await loop.run_in_executor(None, _process_source, req.source, doc_profile)


@router.post("/process-file")
async def process_file(file: UploadFile = File(...), profile: str = Form("invoice")):
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
        result = _process_source(ing["text"], doc_profile)
        result["source_format"] = ing.get("source_format")
        result["filename"] = file.filename
        return result

    return await loop.run_in_executor(None, _ingest_and_process)
