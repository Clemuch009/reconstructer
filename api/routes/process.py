# api/routes/process.py

import asyncio
from typing import Optional
from fastapi import APIRouter, Depends, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.engine import TextReconstructionEngine
from adapters.human   import HumanAdapter
from adapters.ai      import AIAdapter
from adapters.machine import MachineAdapter
from api.coc import build_coc, COCEnvelope
from api.dependencies import get_engine, verify_api_key, validate_input_text
from api.streaming import broadcast_to_sse_clients, publish_webhook
# Store in document cache
from api.routes.document import store_coc

router = APIRouter()


# ---------------------------------
# Request / Response models
# ---------------------------------

class ProcessRequest(BaseModel):
    text:               str
    include_confidence: bool = False


class WebhookRequest(BaseModel):
    url:    str
    secret: Optional[str] = None


# ---------------------------------
# Shared processing
# ---------------------------------

async def _process(
    text:   str,
    engine: TextReconstructionEngine,
) -> COCEnvelope:
    validated = validate_input_text(text)

    loop   = asyncio.get_event_loop()
    output = await loop.run_in_executor(None, engine.run, validated)

    envelope = build_coc(validated, output)

    # Store in document cache
    from api.routes.document import store_coc
    store_coc(envelope)

    # Broadcast non-blocking
    asyncio.create_task(broadcast_to_sse_clients(envelope))
    asyncio.create_task(publish_webhook(envelope))

    return envelope


# ---------------------------------
# Endpoints
# ---------------------------------

@router.post("/process")
async def process(
    request: ProcessRequest,
    engine:  TextReconstructionEngine = Depends(get_engine),
    _:       str = Depends(verify_api_key),
) -> dict:
    """
    Process text and return full COC envelope.
    """
    envelope = await _process(request.text, engine)
    return envelope


@router.post("/process/human")
async def process_human(
    request: ProcessRequest,
    engine:  TextReconstructionEngine = Depends(get_engine),
    _:       str = Depends(verify_api_key),
) -> Response:
    """
    Process text and return human-readable formatted string.
    Downloadable as plain text file.
    """
    envelope = await _process(request.text, engine)
    output   = envelope["payload"]
    rendered = HumanAdapter().adapt(output)

    return Response(
        content=rendered,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="document_{envelope["source_id"]}.txt"'
            )
        },
    )


@router.post("/process/ai")
async def process_ai(
    request: ProcessRequest,
    engine:  TextReconstructionEngine = Depends(get_engine),
    _:       str = Depends(verify_api_key),
) -> dict:
    """
    Process text and return Logical Document Model for LLM consumption.
    """
    envelope = await _process(request.text, engine)
    output   = envelope["payload"]
    ldm      = AIAdapter(
        include_confidence=request.include_confidence
    ).adapt(output)
    return {
        "source_id":  envelope["source_id"],
        "version":    envelope["version"],
        "timestamp":  envelope["timestamp"],
        "ldm":        ldm,
    }


@router.post("/process/machine")
async def process_machine(
    request: ProcessRequest,
    engine:  TextReconstructionEngine = Depends(get_engine),
    _:       str = Depends(verify_api_key),
) -> dict:
    """
    Process text and return flat analytics records for DB ingestion.
    """
    envelope = await _process(request.text, engine)
    output   = envelope["payload"]
    records  = MachineAdapter().adapt(output)
    return {
        "source_id": envelope["source_id"],
        "version":   envelope["version"],
        "timestamp": envelope["timestamp"],
        "records":   records,
    }


@router.get("/stream")
async def stream(
    _: str = Depends(verify_api_key),
) -> StreamingResponse:
    """
    SSE endpoint — push COC events to connected clients.
    Connect and receive document.resolved events in real time.
    """
    from api.streaming import sse_event_generator
    return StreamingResponse(
        sse_event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/webhook/register")
async def webhook_register(
    request: WebhookRequest,
    _:       str = Depends(verify_api_key),
) -> dict:
    """
    Register a webhook URL to receive COC push events.
    """
    from api.streaming import register_webhook
    webhook_id = register_webhook(request.url, request.secret)
    return {
        "webhook_id": webhook_id,
        "url":        request.url,
        "status":     "registered",
    }


@router.delete("/webhook/{webhook_id}")
async def webhook_deregister(
    webhook_id: str,
    _:          str = Depends(verify_api_key),
) -> dict:
    """
    Deregister a webhook by ID.
    """
    from api.streaming import deregister_webhook
    removed = deregister_webhook(webhook_id)
    return {
        "webhook_id": webhook_id,
        "status":     "removed" if removed else "not_found",
    }


@router.get("/webhook/list")
async def webhook_list(
    _: str = Depends(verify_api_key),
) -> dict:
    """
    List all registered webhooks.
    """
    from api.streaming import list_webhooks
    return {"webhooks": list_webhooks()}
