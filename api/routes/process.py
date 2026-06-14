# api/routes/process.py

import asyncio
from typing import Optional
from fastapi import APIRouter, Depends, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from fastapi import Request
from core.engine import TextReconstructionEngine
from adapters.human   import HumanAdapter
from adapters.ai      import AIAdapter
from adapters.machine import MachineAdapter
from api.coc import build_coc, COCEnvelope
from api.dependencies import get_engine, validate_input_text
from api.middleware.auth import require_auth, consume_request, RequestContext
from api.streaming import broadcast_to_sse_clients, publish_webhook
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
    ctx:    RequestContext,
) -> COCEnvelope:
    """
    Run engine, build session trace, wrap in COC envelope.
    """
    validated = validate_input_text(text)

    loop   = asyncio.get_event_loop()
    output = await loop.run_in_executor(None, engine.run, validated)

    from api.session import build_session_trace
    raw_line_count = len(validated.split("\n"))
    session_trace  = build_session_trace(output, raw_line_count)

    envelope = build_coc(validated, output, session_trace)

    store_coc(envelope)

    asyncio.create_task(broadcast_to_sse_clients(envelope))
    asyncio.create_task(publish_webhook(envelope))

    await consume_request(ctx)

    return envelope


# ---------------------------------
# Endpoints
# ---------------------------------

@router.post("/process")
async def process(
    request: ProcessRequest,
    engine:  TextReconstructionEngine = Depends(get_engine),
    ctx:     RequestContext = Depends(require_auth),
) -> dict:
    envelope = await _process(request.text, engine, ctx)
    return envelope


@router.post("/process/human")
async def process_human(
    request: ProcessRequest,
    engine:  TextReconstructionEngine = Depends(get_engine),
    ctx:     RequestContext = Depends(require_auth),
) -> Response:
    envelope = await _process(request.text, engine, ctx)
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
    ctx:     RequestContext = Depends(require_auth),
) -> dict:
    envelope = await _process(request.text, engine, ctx)
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
    ctx:     RequestContext = Depends(require_auth),
) -> dict:
    envelope = await _process(request.text, engine, ctx)
    output   = envelope["payload"]
    records  = MachineAdapter().adapt(output)
    return {
        "source_id": envelope["source_id"],
        "version":   envelope["version"],
        "timestamp": envelope["timestamp"],
        "records":   records,
    }


@router.post("/process/raw")
async def process_raw(
    request: Request,
    engine:  TextReconstructionEngine = Depends(get_engine),
    ctx:     RequestContext = Depends(require_auth),
) -> dict:
    body = await request.body()
    text = body.decode("utf-8", errors="replace")
    validate_input_text(text)
    envelope = await _process(text, engine, ctx)
    return envelope


@router.get("/stream")
async def stream(
    ctx: RequestContext = Depends(require_auth),
) -> StreamingResponse:
    from api.streaming import sse_event_generator
    return StreamingResponse(
        sse_event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/webhook/register")
async def webhook_register(
    request: WebhookRequest,
    ctx:     RequestContext = Depends(require_auth),
) -> dict:
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
    ctx:        RequestContext = Depends(require_auth),
) -> dict:
    from api.streaming import deregister_webhook
    removed = deregister_webhook(webhook_id)
    return {
        "webhook_id": webhook_id,
        "status":     "removed" if removed else "not_found",
    }


@router.get("/webhook/list")
async def webhook_list(
    ctx: RequestContext = Depends(require_auth),
) -> dict:
    from api.streaming import list_webhooks
    return {"webhooks": list_webhooks()}
