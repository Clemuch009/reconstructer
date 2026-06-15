# api/routes/ingest_file.py

import asyncio
import hashlib
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import PlainTextResponse

from core.engine import TextReconstructionEngine
from api.coc import build_coc, COCEnvelope
from api.session import build_session_trace
from api.dependencies import get_engine
from api.middleware.auth import require_auth, consume_request, RequestContext
from api.streaming import broadcast_to_sse_clients, publish_webhook
from api.routes.document import store_coc
from api.routes.ingest import _session_store, _evict_if_needed
from ingestion.router import ingest, IngestionResult


router = APIRouter()


# ---------------------------------
# Supported MIME types
# ---------------------------------

SUPPORTED_MIME_TYPES: dict[str, str] = {
    "text/plain":                                                     "txt",
    "text/csv":                                                       "csv",
    "application/csv":                                                "csv",
    "application/pdf":                                                "pdf",
    "application/vnd.openxmlformats-officedocument"
    ".wordprocessingml.document":                                     "docx",
    "application/vnd.openxmlformats-officedocument"
    ".spreadsheetml.sheet":                                           "xlsx",
    "application/vnd.ms-excel":                                      "xlsx",
    "text/html":                                                      "html",
    "application/octet-stream":                                       "unknown",
}

MAX_FILE_BYTES = 50 * 1024 * 1024


# ---------------------------------
# Shared file size guard
# ---------------------------------

def _guard_file(raw_bytes: bytes) -> None:
    if len(raw_bytes) == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Uploaded file is empty.",
        )
    if len(raw_bytes) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds {MAX_FILE_BYTES // (1024*1024)}MB limit.",
        )


# ---------------------------------
# File ingestion pipeline
# ---------------------------------

async def _process_file(
    raw_bytes: bytes,
    filename:  Optional[str],
    engine:    TextReconstructionEngine,
    ctx:       RequestContext,
) -> tuple[COCEnvelope, IngestionResult]:
    """
    Full pipeline for file ingestion.

    raw_bytes
        ↓
    ingestion router (detect → extract → normalize)
        ↓
    engine.run(normalized_text)
        ↓
    session_trace + COC envelope
        ↓
    store + broadcast + consume
    """
    ingestion_result = ingest(raw_bytes, filename=filename)

    if not ingestion_result["ingestion_success"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message":       "File ingestion failed",
                "warnings":      ingestion_result["all_warnings"],
                "source_format": ingestion_result["source_format"],
            },
        )

    normalized_text = ingestion_result["text"]

    loop   = asyncio.get_event_loop()
    output = await loop.run_in_executor(None, engine.run, normalized_text)

    raw_line_count = len(normalized_text.split("\n"))
    session_trace  = build_session_trace(output, raw_line_count)
    envelope       = build_coc(normalized_text, output, session_trace)

    envelope["ingestion_warnings"] = ingestion_result["all_warnings"]
    envelope["ingestion_metadata"] = {
        "source_format": ingestion_result["source_format"],
        "pipeline":      ingestion_result["pipeline"],
        "page_count":    ingestion_result["extraction_result"]["metadata"]["page_count"],
        "sheet_count":   ingestion_result["extraction_result"]["metadata"]["sheet_count"],
        "char_count":    ingestion_result["extraction_result"]["metadata"]["char_count"],
        "line_count":    ingestion_result["extraction_result"]["metadata"]["line_count"],
        "word_count":    ingestion_result["extraction_result"]["metadata"]["word_count"],
        "encoding_used": ingestion_result["extraction_result"]["metadata"]["encoding_used"],
        "filename":      filename,
    }

    store_coc(envelope)

    # Register in shared session store so /export and /session/{id}
    # work on file-uploaded documents the same as text-ingested ones.
    # Pipeline already ran above — mark as resolved immediately,
    # no re-processing needed when /export is called.
    source_id = envelope["source_id"]
    _evict_if_needed()
    _session_store[source_id] = {
        "raw":      normalized_text,
        "metadata": {"filename": filename, "source_format": ingestion_result["source_format"]},
        "resolved": True,
        "status":   "resolved",
        "envelope": envelope,
    }

    asyncio.create_task(broadcast_to_sse_clients(envelope))
    asyncio.create_task(publish_webhook(envelope))
    await consume_request(ctx)

    return envelope, ingestion_result


# ---------------------------------
# Endpoints
# ---------------------------------

@router.post("/ingest/file")
async def ingest_file(
    file:   UploadFile = File(...),
    engine: TextReconstructionEngine = Depends(get_engine),
    ctx:    RequestContext = Depends(require_auth),
) -> dict:
    raw_bytes = await file.read()
    _guard_file(raw_bytes)

    envelope, _ = await _process_file(
        raw_bytes=raw_bytes,
        filename=file.filename,
        engine=engine,
        ctx=ctx,
    )
    return envelope


@router.post("/ingest/file/human")
async def ingest_file_human(
    file:   UploadFile = File(...),
    engine: TextReconstructionEngine = Depends(get_engine),
    ctx:    RequestContext = Depends(require_auth),
) -> PlainTextResponse:
    raw_bytes = await file.read()
    _guard_file(raw_bytes)

    envelope, _ = await _process_file(
        raw_bytes=raw_bytes,
        filename=file.filename,
        engine=engine,
        ctx=ctx,
    )

    from adapters.human import HumanAdapter
    rendered = HumanAdapter().adapt(envelope["payload"])
    stem     = (file.filename or "output").rsplit(".", 1)[0]

    return PlainTextResponse(
        content=rendered,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{stem}_structured.txt"'
        },
    )


@router.post("/ingest/file/machine")
async def ingest_file_machine(
    file:   UploadFile = File(...),
    engine: TextReconstructionEngine = Depends(get_engine),
    ctx:    RequestContext = Depends(require_auth),
) -> dict:
    raw_bytes = await file.read()
    _guard_file(raw_bytes)

    envelope, ingestion_result = await _process_file(
        raw_bytes=raw_bytes,
        filename=file.filename,
        engine=engine,
        ctx=ctx,
    )

    from adapters.machine import MachineAdapter
    records = MachineAdapter().adapt(envelope["payload"])

    return {
        "source_id":          envelope["source_id"],
        "filename":           file.filename,
        "source_format":      ingestion_result["source_format"],
        "pipeline":           ingestion_result["pipeline"],
        "ingestion_warnings": ingestion_result["all_warnings"],
        "records":            records,
    }


@router.post("/ingest/file/ai")
async def ingest_file_ai(
    file:               UploadFile = File(...),
    include_confidence: bool = False,
    engine:             TextReconstructionEngine = Depends(get_engine),
    ctx:                RequestContext = Depends(require_auth),
) -> dict:
    raw_bytes = await file.read()
    _guard_file(raw_bytes)

    envelope, ingestion_result = await _process_file(
        raw_bytes=raw_bytes,
        filename=file.filename,
        engine=engine,
        ctx=ctx,
    )

    from adapters.ai import AIAdapter
    ldm = AIAdapter(include_confidence=include_confidence).adapt(envelope["payload"])

    return {
        "source_id":          envelope["source_id"],
        "filename":           file.filename,
        "source_format":      ingestion_result["source_format"],
        "pipeline":           ingestion_result["pipeline"],
        "ingestion_warnings": ingestion_result["all_warnings"],
        "ldm":                ldm,
    }


@router.get("/ingest/file/formats")
async def supported_formats(
    ctx: RequestContext = Depends(require_auth),
) -> dict:
    return {
        "supported_formats": [
            {
                "format":     "txt",
                "extensions": [".txt", ".log", ".md"],
                "mime_types": ["text/plain"],
                "notes":      "Plain text, any encoding",
            },
            {
                "format":     "csv",
                "extensions": [".csv", ".tsv"],
                "mime_types": ["text/csv", "application/csv"],
                "notes":      "Comma, semicolon, tab, pipe delimited",
            },
            {
                "format":     "pdf",
                "extensions": [".pdf"],
                "mime_types": ["application/pdf"],
                "notes":      "Text-based PDFs only — image PDFs not supported",
            },
            {
                "format":     "docx",
                "extensions": [".docx"],
                "mime_types": [
                    "application/vnd.openxmlformats-officedocument"
                    ".wordprocessingml.document"
                ],
                "notes":      "Word 2007+ only — .doc not supported",
            },
            {
                "format":     "xlsx",
                "extensions": [".xlsx", ".xlsm"],
                "mime_types": [
                    "application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"
                ],
                "notes":      "Excel 2007+, multiple sheets supported",
            },
            {
                "format":     "html",
                "extensions": [".html", ".htm"],
                "mime_types": ["text/html"],
                "notes":      "Tables extracted as CSV, scripts stripped",
            },
            {
                "format":     "unknown",
                "extensions": ["any"],
                "mime_types": ["application/octet-stream"],
                "notes":      "Heuristic segmentation pipeline applied",
            },
        ],
        "max_file_size_mb": MAX_FILE_BYTES // (1024 * 1024),
    }
