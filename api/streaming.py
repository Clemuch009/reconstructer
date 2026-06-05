# api/streaming.py

import json
import asyncio
import httpx
from typing import AsyncIterator, Dict, List, Optional, Callable
from typing_extensions import TypedDict

from api.coc import COCEnvelope


# ---------------------------------
# Webhook registry
# ---------------------------------

class WebhookRecord(TypedDict):
    url:        str
    secret:     Optional[str]   # future HMAC signing support
    active:     bool


# In-memory registry — replace with DB when storage layer is added
_webhook_registry: Dict[str, WebhookRecord] = {}


def register_webhook(url: str, secret: Optional[str] = None) -> str:
    """
    Register a webhook URL.
    Returns webhook_id for management.
    """
    import hashlib
    webhook_id = hashlib.sha256(url.encode()).hexdigest()[:12]
    _webhook_registry[webhook_id] = WebhookRecord(
        url=url,
        secret=secret,
        active=True,
    )
    return webhook_id


def deregister_webhook(webhook_id: str) -> bool:
    if webhook_id in _webhook_registry:
        del _webhook_registry[webhook_id]
        return True
    return False


def list_webhooks() -> Dict[str, WebhookRecord]:
    return dict(_webhook_registry)


# ---------------------------------
# Webhook publisher
# ---------------------------------

async def publish_webhook(envelope: COCEnvelope) -> Dict[str, str]:
    """
    POST COC envelope to all registered active webhooks.
    Non-blocking — failures logged, not raised.
    Returns delivery status per webhook_id.
    """
    if not _webhook_registry:
        return {}

    payload = json.dumps(envelope, default=str)
    results: Dict[str, str] = {}

    async with httpx.AsyncClient(timeout=5.0) as client:
        for webhook_id, record in _webhook_registry.items():
            if not record["active"]:
                results[webhook_id] = "skipped"
                continue
            try:
                response = await client.post(
                    record["url"],
                    content=payload,
                    headers={"Content-Type": "application/json"},
                )
                results[webhook_id] = (
                    "delivered"
                    if response.status_code < 300
                    else f"failed:{response.status_code}"
                )
            except Exception as e:
                results[webhook_id] = f"error:{str(e)[:60]}"

    return results


# ---------------------------------
# SSE event queue
# ---------------------------------

# Global broadcast queue — all SSE clients receive all events
_sse_queue: asyncio.Queue = asyncio.Queue(maxsize=100)


async def broadcast_coc(envelope: COCEnvelope) -> None:
    """
    Broadcast COC to SSE queue.
    Drops oldest event if queue is full (non-blocking).
    """
    event = {
        "event": "document.resolved",
        "data":  json.dumps(envelope, default=str),
    }
    try:
        _sse_queue.put_nowait(event)
    except asyncio.QueueFull:
        # Drop oldest and insert newest
        try:
            _sse_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            _sse_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass


async def sse_event_generator() -> AsyncIterator[str]:
    """
    Async generator for SSE endpoint.
    Yields formatted SSE events as they arrive.
    Each client gets its own consumer — shared queue broadcasts to all.
    """
    # Each SSE client gets its own sub-queue
    client_queue: asyncio.Queue = asyncio.Queue(maxsize=50)

    # Register this client
    _sse_clients.append(client_queue)

    try:
        while True:
            try:
                event = await asyncio.wait_for(
                    client_queue.get(), timeout=30.0
                )
                yield f"event: {event['event']}\ndata: {event['data']}\n\n"
            except asyncio.TimeoutError:
                # Keepalive ping
                yield ": keepalive\n\n"
    finally:
        # Deregister on disconnect
        if client_queue in _sse_clients:
            _sse_clients.remove(client_queue)


# Per-client SSE queues
_sse_clients: List[asyncio.Queue] = []


async def broadcast_to_sse_clients(envelope: COCEnvelope) -> None:
    """
    Broadcast COC envelope to all connected SSE clients.
    """
    if not _sse_clients:
        return

    event = {
        "event": "document.resolved",
        "data":  json.dumps(envelope, default=str),
    }

    for client_queue in list(_sse_clients):
        try:
            client_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass
