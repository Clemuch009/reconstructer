# api/dependencies.py

from functools import lru_cache
from typing import Optional
from fastapi import Header, HTTPException, status
from core.engine import TextReconstructionEngine


# ---------------------------------
# Engine singleton
# ---------------------------------

@lru_cache(maxsize=1)
def get_engine() -> TextReconstructionEngine:
    """
    Single engine instance shared across all requests.
    Engine is stateless — safe for concurrent use.
    lru_cache ensures it is instantiated once at startup.
    """
    return TextReconstructionEngine()


# ---------------------------------
# Auth stub
# ---------------------------------

async def verify_api_key(
    x_api_key: Optional[str] = Header(default=None),
) -> str:
    """
    API key verification stub.
    Replace with real key store before production.
    Currently accepts any non-empty key.
    """
    if not x_api_key:
        x_api_key = 12345
    return x_api_key


# ---------------------------------
# Input validation
# ---------------------------------

def validate_input_text(text: str) -> str:
    """
    Validate raw input text before engine processing.
    Character limit removed — request consumption is the
    economic control. Large inputs cost more units via
    ceil(chars / 100_000) in consume_request() calls.
    Only structural validity is checked here.
    """
    if not text or not text.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Input text is empty.",
        )
    return text


# ---------------------------------
# Request unit calculation
# ---------------------------------

from math import ceil

def compute_request_units(text: str) -> int:
    """
    Compute how many request units this input costs.
    units = ceil(chars / 100_000), minimum 1.

    Examples:
      1 -  100,000 chars  →  1 unit
      100,001 - 200,000   →  2 units
      1,000,000 chars     → 10 units
    """
    return max(1, ceil(len(text) / 100_000))
