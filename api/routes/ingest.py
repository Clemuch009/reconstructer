# api/routes/ingest.py

import asyncio
import hashlib
from collections import OrderedDict
from typing import Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from core.engine import TextReconstructionEngine
from api.coc import build_coc, COCEnvelope
from api.session import build_session_trace
from api.dependencies import get_engine, verify_api_key, validate_input_text
from api.streaming import broadcast_to_sse_clients, publish_webhook
from api.routes.document import store_coc


router = APIRouter()


# ---------------------------------
# In-memory session store — LRU, max 200
# Replace with DB when storage layer added
# ---------------------------------

_session_store: OrderedDict = OrderedDict()
MAX_SESSIONS = 200


def _evict_if_needed() -> None:
    while len(_session_store) >= MAX_SESSIONS:
        _session_store.popitem(last=False)


def _source_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ---------------------------------
# Request models
# ---------------------------------

class IngestRequest(BaseModel):
    payload:      str
    metadata:     Optional[Dict[str, Any]] = None


class ResolveRequest(BaseModel):
    session_id:   str
    async_mode:   bool = False              # True = return immediately, poll /session/{id}
    callback_url: Optional[str] = None     # optional webhook on completion


class ExportRequest(BaseModel):
    session_id:         str
    format:             str  = "machine"   # "human" | "machine" | "ai"
    include_confidence: bool = False


# ---------------------------------
# POST /ingest
# ---------------------------------

@router.post("/ingest")
async def ingest(
    request: IngestRequest,
    _:       str = Depends(verify_api_key),
) -> dict:
    """
    Stage 1 — Ingest raw input.
    Idempotent: same payload returns existing session_id.
    source_id defines identity. session_id defines execution instance.
    They map 1:1.

    Request:
      payload:  str               required
      metadata: Dict | null       optional

    Response 200:
      session_id: str             use in /resolve and /export
      source_id:  str             deterministic hash of payload
      status:     "created" | "existing"
      line_count: int
      char_count: int
    """
    validated = validate_input_text(request.payload)
    source_id = _source_id(validated)

    # Idempotency — return existing session if source_id already known
    if source_id in _session_store:
        existing = _session_store[source_id]
        _session_store.move_to_end(source_id)
        return {
            "session_id": source_id,
            "source_id":  source_id,
            "status":     "existing",
            "resolved":   existing["resolved"],
            "line_count": len(validated.split("\n")),
            "char_count": len(validated),
        }

    # New session
    _evict_if_needed()
    _session_store[source_id] = {
        "raw":      validated,
        "metadata": request.metadata or {},
        "resolved": False,
        "status":   "ingested",
        "envelope": None,
    }

    return {
        "session_id": source_id,
        "source_id":  source_id,
        "status":     "created",
        "resolved":   False,
        "line_count": len(validated.split("\n")),
        "char_count": len(validated),
    }


# ---------------------------------
# POST /resolve
# ---------------------------------

@router.post("/resolve")
async def resolve(
    request: ResolveRequest,
    engine:  TextReconstructionEngine = Depends(get_engine),
    _:       str = Depends(verify_api_key),
) -> dict:
    """
    Stage 2 — Run classifier + resolver pipeline.
    Idempotent — safe to re-run, returns cached result if already resolved.

    Sync mode (default):
      Blocks until complete. Returns full envelope.

    Async mode (async_mode=true):
      Returns immediately with status=processing.
      Client polls GET /session/{session_id} for completion.
      Optional callback_url receives POST with envelope on completion.

    Request:
      session_id:   str           required
      async_mode:   bool          default false
      callback_url: str | null    optional webhook on completion

    Response 200 sync:
      session_id: str
      status:     "resolved"
      cached:     bool
      envelope:   COCEnvelope

    Response 200 async:
      session_id: str
      status:     "processing"
      poll_url:   str
    """
    session = _session_store.get(request.session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{request.session_id}' not found. "
                   f"Call /ingest first.",
        )

    # Idempotent — return cached result if already resolved
    if session["resolved"] and session["envelope"]:
        return {
            "session_id": request.session_id,
            "status":     "resolved",
            "cached":     True,
            "envelope":   session["envelope"],
        }

    # Async mode — return immediately, process in background
    if request.async_mode:
        _session_store[request.session_id]["status"] = "processing"

        async def _background_resolve():
            try:
                await _run_pipeline(
                    request.session_id,
                    engine,
                    request.callback_url,
                )
            except Exception as e:
                _session_store[request.session_id]["status"] = "failed"
                _session_store[request.session_id]["error"]  = str(e)

        asyncio.create_task(_background_resolve())

        return {
            "session_id": request.session_id,
            "status":     "processing",
            "poll_url":   f"/session/{request.session_id}",
        }

    # Sync mode — block until complete
    envelope = await _run_pipeline(
        request.session_id,
        engine,
        request.callback_url,
    )

    return {
        "session_id": request.session_id,
        "status":     "resolved",
        "cached":     False,
        "envelope":   envelope,
    }


# ---------------------------------
# POST /export
# ---------------------------------

@router.post("/export")
async def export(
    request: ExportRequest,
    _:       str = Depends(verify_api_key),
) -> Any:
    """
    Stage 3 — Export resolved output in specified format.

    Format response types:
      human   → text/plain, downloadable .txt  (NO JSON wrapper)
      machine → application/json, List[Dict]
      ai      → application/json, LDMDocument

    Request:
      session_id:         str     required
      format:             str     "human" | "machine" | "ai"
      include_confidence: bool    default false, AI format only

    Response human:
      Content-Type: text/plain
      Content-Disposition: attachment; filename="session_{id}.txt"
      Body: formatted human-readable string

    Response machine:
      { "session_id": str, "format": "machine", "content": List[Dict] }

    Response ai:
      { "session_id": str, "format": "ai", "content": LDMDocument }
    """
    session = _session_store.get(request.session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{request.session_id}' not found.",
        )

    if not session["resolved"] or not session["envelope"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Session '{request.session_id}' not yet resolved. "
                   f"Call /resolve first.",
        )

    envelope = session["envelope"]
    output   = envelope["payload"]
    fmt      = request.format.lower()

    if fmt == "human":
        from adapters.human import HumanAdapter
        content = HumanAdapter().adapt(output)
        return PlainTextResponse(
            content=content,
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="session_{request.session_id}.txt"'
                )
            },
        )

    elif fmt == "machine":
        from adapters.machine import MachineAdapter
        content = MachineAdapter().adapt(output)
        return {
            "session_id": request.session_id,
            "format":     "machine",
            "content":    content,
        }

    elif fmt == "ai":
        from adapters.ai import AIAdapter
        content = AIAdapter(
            include_confidence=request.include_confidence
        ).adapt(output)
        return {
            "session_id": request.session_id,
            "format":     "ai",
            "content":    content,
        }

    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown format '{fmt}'. Use: human | machine | ai",
        )


# ---------------------------------
# GET /session/{session_id}
# ---------------------------------

@router.get("/session/{session_id}")
async def get_session(
    session_id: str,
    _:          str = Depends(verify_api_key),
) -> dict:
    """
    Inspect session state and telemetry.
    Used for polling in async resolve mode.

    Response — not resolved:
      { "session_id": str, "status": "ingested"|"processing", "resolved": false }

    Response — resolved:
      { "session_id": str, "status": "resolved", "resolved": true,
        "session_trace": SessionTrace }

    Response — failed:
      { "session_id": str, "status": "failed", "error": str }
    """
    session = _session_store.get(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{session_id}' not found.",
        )

    result = {
        "session_id": session_id,
        "status":     session.get("status", "ingested"),
        "resolved":   session["resolved"],
        "metadata":   session.get("metadata", {}),
    }

    if session["resolved"] and session["envelope"]:
        result["session_trace"] = session["envelope"]["session_trace"]

    if session.get("status") == "failed":
        result["error"] = session.get("error", "unknown error")

    return result


# ---------------------------------
# Shared pipeline runner
# ---------------------------------

async def _run_pipeline(
    session_id:   str,
    engine:       TextReconstructionEngine,
    callback_url: Optional[str] = None,
) -> COCEnvelope:
    """
    Run core engine on session payload.
    Updates session store on completion.
    Fires callback_url webhook if provided.
    """
    session = _session_store[session_id]
    raw     = session["raw"]

    _session_store[session_id]["status"] = "processing"

    loop   = asyncio.get_event_loop()
    output = await loop.run_in_executor(None, engine.run, raw)

    raw_line_count = len(raw.split("\n"))
    session_trace  = build_session_trace(output, raw_line_count)
    envelope       = build_coc(raw, output, session_trace)

    # Update session store
    _session_store[session_id]["resolved"] = True
    _session_store[session_id]["status"]   = "resolved"
    _session_store[session_id]["envelope"] = envelope

    # Store in document cache
    store_coc(envelope)

    # Broadcast to SSE clients
    asyncio.create_task(broadcast_to_sse_clients(envelope))
    asyncio.create_task(publish_webhook(envelope))

    # Fire callback webhook if provided
    if callback_url:
        import httpx
        import json
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    callback_url,
                    content=json.dumps(envelope, default=str),
                    headers={"Content-Type": "application/json"},
                )
        except Exception:
            pass  # callback failure does not break pipeline

    return envelope
