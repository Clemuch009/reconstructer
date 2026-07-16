# api/routes/document.py

from collections import OrderedDict
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status

from api.coc import COCEnvelope
from api.dependencies import verify_api_key
from analysis.segment_filter import (
    filter_segments,
    extract_kv_pairs,
    available_types,
)
from analysis.rules_engine import evaluate
from profiles import get_profile, available_profiles
from profiles.resolver import resolve_document_view


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


# ---------------------------------
# Foundational segment filtering
#
# General platform capability (NOT vertical-specific): retrieve a document's
# classified segments by structural type and/or confidence. Verticals (invoice
# processing, contract analysis, ...) and downstream features (validation,
# export) build on these primitives instead of walking the segment tree.
# ---------------------------------

def _segments_for(source_id: str):
    envelope = get_coc(source_id)
    if envelope is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document '{source_id}' not found.",
        )
    return envelope, envelope["payload"]["machine_readable"]["segments"]


@router.get("/document/{source_id}/segments")
async def get_document_segments(
    source_id: str,
    type:           Optional[str] = Query(
        None,
        description="Comma-separated structural types to keep "
                    "(e.g. 'table', 'kv', 'table,kv'). Omit for all types.",
    ),
    min_confidence: float = Query(
        0.0, ge=0.0, le=1.0,
        description="Keep only matches with confidence >= this value. "
                    "Matches without a confidence value are kept.",
    ),
    _: str = Depends(verify_api_key),
) -> dict:
    """
    Filter a cached document's segments by structural type and confidence.

    Returns a flat list of matches (nested kv/table blocks are surfaced in place
    of their `mixed` parent), so callers get exactly the structure types they
    ask for without walking the tree. Foundational — used by every vertical and
    downstream feature.
    """
    envelope, segments = _segments_for(source_id)
    types = [t.strip() for t in type.split(",")] if type else None
    matches = filter_segments(segments, types=types, min_confidence=min_confidence)
    return {
        "source_id": source_id,
        "timestamp": envelope["timestamp"],
        "filter":    {"type": types, "min_confidence": min_confidence},
        "count":     len(matches),
        "segments":  matches,
    }


@router.get("/document/{source_id}/kv")
async def get_document_kv(
    source_id: str,
    _: str = Depends(verify_api_key),
) -> dict:
    """
    Return all key-value pairs the engine recognized in the document, harvested
    from wherever they were annotated (not only clean kv blocks). Each pair
    carries its source segment_id and line_index for later provenance.
    """
    envelope, segments = _segments_for(source_id)
    pairs = extract_kv_pairs(segments)
    return {
        "source_id": source_id,
        "timestamp": envelope["timestamp"],
        "count":     len(pairs),
        "pairs":     pairs,
    }


@router.get("/document/{source_id}/types")
async def get_document_types(
    source_id: str,
    _: str = Depends(verify_api_key),
) -> dict:
    """
    Report which structural types are present in the document and how many of
    each — so a caller (or UI) knows what can be filtered before asking.
    """
    envelope, segments = _segments_for(source_id)
    return {
        "source_id": source_id,
        "timestamp": envelope["timestamp"],
        "types":     available_types(segments),
    }


# ---------------------------------
# Profile application
#
# Applies a declarative Profile (invoice, contract, ...) to an already-processed
# document: the generic resolver builds a canonical Document View from the
# document's key-values and tables, then the generic Rules Engine evaluates the
# profile's rules against it. No vertical logic lives here — the profile is data
# and both the resolver and rules engine are generic.
# ---------------------------------

@router.get("/profiles")
async def list_profiles(
    _: str = Depends(verify_api_key),
) -> dict:
    """List the declarative profiles available to apply to a document."""
    return {"profiles": available_profiles()}


@router.get("/document/{source_id}/profile/{profile_id}")
async def apply_profile(
    source_id:  str,
    profile_id: str,
    _: str = Depends(verify_api_key),
) -> dict:
    """
    Apply a profile to a cached document.

    Returns the canonical Document View (resolved fields + mapped tables) and
    the rule evaluation report (PASS / FAIL / SKIPPED per rule, with evidence).

    Rule statuses carry no severity — that is profile/caller policy. A rule whose
    declared dependencies are absent is SKIPPED, not failed: a missing input
    means "unevaluable", not "wrong".
    """
    profile = get_profile(profile_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile '{profile_id}' not found.",
        )

    envelope, segments = _segments_for(source_id)

    # Generic extraction: the profile only *requests* these; it does not detect.
    kv_pairs = extract_kv_pairs(segments)
    tables   = [m["content"] for m in filter_segments(segments, types=["table"])]

    view   = resolve_document_view(kv_pairs, tables, profile, metadata={})
    report = evaluate(view, profile.get("rules", []))

    return {
        "source_id": source_id,
        "timestamp": envelope["timestamp"],
        "profile": {
            "id":      profile["metadata"]["id"],
            "name":    profile["metadata"]["name"],
            "version": profile["metadata"]["version"],
        },
        "fields":     view["fields"],
        "tables":     view["tables"],
        "resolution": view["_resolution"],
        "validation": report,
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
