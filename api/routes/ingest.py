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
from api.middleware.auth import require_auth, consume_request, RequestContext
from api.dependencies import get_engine, validate_input_text
from api.streaming import broadcast_to_sse_clients, publish_webhook
from api.routes.document import store_coc


router = APIRouter()


# ---------------------------------
# In-memory session store — LRU, max 200
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
    async_mode:   bool = False
    callback_url: Optional[str] = None


class ExportRequest(BaseModel):
    session_id:         str
    format:             str  = "machine"
    include_confidence: bool = False


# ---------------------------------
# POST /ingest
# ---------------------------------

@router.post("/ingest")
async def ingest(
    request: IngestRequest,
    ctx:     RequestContext = Depends(require_auth),
) -> dict:
    validated = validate_input_text(request.payload)
    source_id = _source_id(validated)

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
    ctx:     RequestContext = Depends(require_auth),
) -> dict:
    session = _session_store.get(request.session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{request.session_id}' not found. "
                   f"Call /ingest first.",
        )

    if session["resolved"] and session["envelope"]:
        return {
            "session_id": request.session_id,
            "status":     "resolved",
            "cached":     True,
            "envelope":   session["envelope"],
        }

    if request.async_mode:
        _session_store[request.session_id]["status"] = "processing"

        async def _background_resolve():
            try:
                await _run_pipeline(
                    request.session_id,
                    engine,
                    ctx,                      # ← ctx passed correctly
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

    envelope = await _run_pipeline(
        request.session_id,
        engine,
        ctx,
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
    ctx:     RequestContext = Depends(require_auth),
) -> Any:
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
    ctx:        RequestContext = Depends(require_auth),   # ← fixed
) -> dict:
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
    ctx:          RequestContext,
    callback_url: Optional[str] = None,
) -> COCEnvelope:
    session = _session_store[session_id]
    raw     = session["raw"]

    _session_store[session_id]["status"] = "processing"

    loop   = asyncio.get_event_loop()
    output = await loop.run_in_executor(None, engine.run, raw)

    raw_line_count = len(raw.split("\n"))
    session_trace  = build_session_trace(output, raw_line_count)
    envelope       = build_coc(raw, output, session_trace)

    _session_store[session_id]["resolved"] = True
    _session_store[session_id]["status"]   = "resolved"
    _session_store[session_id]["envelope"] = envelope

    store_coc(envelope)

    asyncio.create_task(broadcast_to_sse_clients(envelope))
    asyncio.create_task(publish_webhook(envelope))

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
            pass

    await consume_request(ctx)
    return envelope
