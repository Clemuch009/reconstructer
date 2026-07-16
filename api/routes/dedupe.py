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
# Batch mode: the set provided IS the comparison universe. Nothing persists
# between calls; to check an invoice against history, the registry provider
# (built later) reads the stored session ledger instead. Same engine.

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

from core.engine import TextReconstructionEngine
from analysis.segment_filter import extract_kv_pairs, filter_segments
from profiles.resolver import resolve_document_view
from profiles import get_profile
from relationship_engine.dedup_engine import make_records, analyze

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


def _run_dedupe(items: List[Dict[str, Any]], dayfirst: bool) -> Dict[str, Any]:
    """Resolve each document to fields, then run the batch engine. Returns the
    engine result augmented with each document's resolved fields (so the UI can
    show both invoices side by side)."""
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
        invoices.append({"id": rid, "fields": fields, "label": label})
        resolved[rid] = fields

    records = make_records(invoices, dayfirst=dayfirst)
    result = analyze(records, policy=None)
    result["resolved_fields"] = resolved      # id → fields, for side-by-side UI
    return result


@router.post("/dedupe")
async def dedupe(req: DedupeRequest):
    """Detect duplicate relationships among a set of pasted documents."""
    if not req.documents or len(req.documents) < 2:
        raise HTTPException(status_code=400,
                            detail="provide at least 2 documents to compare")
    items = [{"id": d.id, "text": d.text, "profile": d.profile} for d in req.documents]
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run_dedupe, items, req.dayfirst)


@router.post("/dedupe/files")
async def dedupe_files(
    files: List[UploadFile] = File(...),
    profile: str = Form("invoice"),
    dayfirst: bool = Form(False),
):
    """Detect duplicates among a set of UPLOADED files (PDF/DOCX/XLSX/…). Each is
    ingested to text, resolved, then run through the batch engine."""
    if not files or len(files) < 2:
        raise HTTPException(status_code=400, detail="upload at least 2 files")
    from ingestion.router import ingest

    doc_profile = get_profile(profile)
    if doc_profile is None:
        raise HTTPException(status_code=400, detail=f"unknown profile: {profile}")

    async def _read(f: UploadFile):
        return f.filename, await f.read()
    raws = [await _read(f) for f in files]

    def _work() -> Dict[str, Any]:
        items = []
        for fname, raw in raws:
            ing = ingest(raw, filename=fname)
            items.append({"id": fname, "text": ing["text"], "profile": profile})
        return _run_dedupe(items, dayfirst)

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _work)
