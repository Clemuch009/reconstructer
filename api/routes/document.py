# api/routes/document.py

from collections import OrderedDict
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status

from api.coc import COCEnvelope
from api.dependencies import verify_api_key


router = APIRouter()


# ---------------------------------
# Ephemeral in-memory document store
# LRU — last 100 documents
# Restarts clear it
# Replace backing store when storage layer is added
# ---------------------------------

_document_cache: OrderedDict = OrderedDict()
MAX_CACHE = 100


def store_coc(envelope: COCEnvelope) -> None:
    """
    Store COC envelope by source_id.
    Evicts oldest entry when cache exceeds MAX_CACHE.
    """
    source_id = envelope["source_id"]
    if source_id in _document_cache:
        # Move to end — most recently accessed
        _document_cache.move_to_end(source_id)
    _document_cache[source_id] = envelope
    if len(_document_cache) > MAX_CACHE:
        _document_cache.popitem(last=False)


def get_coc(source_id: str) -> Optional[COCEnvelope]:
    """
    Retrieve COC envelope by source_id.
    Returns None if not found or evicted.
    """
    return _document_cache.get(source_id)


def list_coc() -> list:
    """
    List all cached document IDs with metadata.
    """
    return [
        {
            "source_id": envelope["source_id"],
            "timestamp": envelope["timestamp"],
            "segments":  len(
                envelope["payload"]["machine_readable"]["segments"]
            ),
            "is_valid":  envelope["payload"].get("validation", {}).get(
                "is_valid", True
            ),
        }
        for envelope in _document_cache.values()
    ]


# ---------------------------------
# Endpoints
# ---------------------------------

@router.get("/document/{source_id}")
async def get_document(
    source_id: str,
    _:         str = Depends(verify_api_key),
) -> dict:
    """
    Retrieve a processed COC envelope by source_id.
    source_id is returned from /process endpoints.
    Cache holds last 100 documents — older entries are evicted.
    """
    envelope = get_coc(source_id)
    if envelope is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document '{source_id}' not found. "
                   f"May have been evicted from cache or not yet processed.",
        )
    return envelope


@router.get("/document/{source_id}/human")
async def get_document_human(
    source_id: str,
    _:         str = Depends(verify_api_key),
) -> dict:
    """
    Retrieve human-readable view of a cached document.
    """
    from fastapi.responses import Response
    from adapters.human import HumanAdapter

    envelope = get_coc(source_id)
    if envelope is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document '{source_id}' not found.",
        )
    rendered = HumanAdapter().adapt(envelope["payload"])
    return Response(
        content=rendered,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="document_{source_id}.txt"'
            )
        },
    )


@router.get("/document/{source_id}/ai")
async def get_document_ai(
    source_id: str,
    _:         str = Depends(verify_api_key),
) -> dict:
    """
    Retrieve AI LDM view of a cached document.
    """
    from adapters.ai import AIAdapter

    envelope = get_coc(source_id)
    if envelope is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document '{source_id}' not found.",
        )
    ldm = AIAdapter(include_confidence=True).adapt(envelope["payload"])
    return {
        "source_id": source_id,
        "timestamp": envelope["timestamp"],
        "ldm":       ldm,
    }


@router.get("/document/{source_id}/machine")
async def get_document_machine(
    source_id: str,
    _:         str = Depends(verify_api_key),
) -> dict:
    """
    Retrieve machine analytics records of a cached document.
    """
    from adapters.machine import MachineAdapter

    envelope = get_coc(source_id)
    if envelope is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document '{source_id}' not found.",
        )
    records = MachineAdapter().adapt(envelope["payload"])
    return {
        "source_id": source_id,
        "timestamp": envelope["timestamp"],
        "records":   records,
    }


@router.get("/documents")
async def list_documents(
    _: str = Depends(verify_api_key),
) -> dict:
    """
    List all documents currently in cache.
    Returns source_id, timestamp, segment count, and validity per document.
    """
    return {
        "count":     len(_document_cache),
        "max_cache": MAX_CACHE,
        "documents": list_coc(),
    }
