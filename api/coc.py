# api/coc.py

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from typing_extensions import TypedDict

from postprocess.formatter import PostprocessOutput


# ---------------------------------
# COC version
# ---------------------------------

COC_VERSION = "1.0"


# ---------------------------------
# COC envelope contract
# ---------------------------------

class COCEnvelope(TypedDict):
    version:    str           # schema version — consumers check this
    event_type: str           # always "document.resolved"
    source_id:  str           # deterministic hash of input text
    timestamp:  str           # ISO-8601 UTC
    payload:    PostprocessOutput


# ---------------------------------
# Source ID — deterministic
# ---------------------------------

def _source_id(text: str) -> str:
    """
    Deterministic source ID from input text.
    Same input always produces same ID — enables deduplication.
    """
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ---------------------------------
# COC builder
# ---------------------------------

def build_coc(
    text:    str,
    output:  PostprocessOutput,
) -> COCEnvelope:
    """
    Wrap PostprocessOutput in a versioned COC envelope.

    Rules:
    - Core Engine output is never modified
    - source_id is deterministic from input text
    - timestamp is UTC at envelope creation time
    - version allows downstream consumers to detect schema changes
    """
    return COCEnvelope(
        version=COC_VERSION,
        event_type="document.resolved",
        source_id=_source_id(text),
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=output,
    )


# ---------------------------------
# COC validation
# ---------------------------------

def validate_coc(envelope: COCEnvelope) -> bool:
    """
    Verify COC envelope has all required fields and known version.
    Returns False if envelope is malformed or version is unsupported.
    """
    if not isinstance(envelope, dict):
        return False
    required = {"version", "event_type", "source_id", "timestamp", "payload"}
    if not required.issubset(envelope.keys()):
        return False
    if envelope["version"] != COC_VERSION:
        return False
    if envelope["event_type"] != "document.resolved":
        return False
    if "machine_readable" not in envelope["payload"]:
        return False
    return True
