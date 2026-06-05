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
        #raise HTTPException(
        #    status_code=status.HTTP_401_UNAUTHORIZED,
        #    detail="Missing API key. Pass X-API-Key header.",
        #)
    return x_api_key


# ---------------------------------
# Input validation
# ---------------------------------

MAX_INPUT_CHARS = 500_000   # 500k characters hard limit

def validate_input_text(text: str) -> str:
    """
    Validate raw input text before engine processing.
    Raises HTTPException on violation.
    """
    if not text or not text.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Input text is empty.",
        )
    if len(text) > MAX_INPUT_CHARS:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Input exceeds {MAX_INPUT_CHARS:,} character limit.",
        )
    return text
