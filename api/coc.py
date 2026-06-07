# api/coc.py  — updated

import hashlib
from datetime import datetime, timezone
from typing import Any, Optional
from typing_extensions import TypedDict

from postprocess.formatter import PostprocessOutput
from api.session import SessionTrace


COC_VERSION = "1.0"


class COCEnvelope(TypedDict):
    version:       str
    event_type:    str
    source_id:     str
    timestamp:     str
    payload:       PostprocessOutput
    session_trace: SessionTrace        # ← new


def _source_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def build_coc(
    text:          str,
    output:        PostprocessOutput,
    session_trace: SessionTrace,
) -> COCEnvelope:
    """
    Wrap PostprocessOutput + SessionTrace in versioned COC envelope.
    """
    return COCEnvelope(
        version=COC_VERSION,
        event_type="document.resolved",
        source_id=_source_id(text),
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=output,
        session_trace=session_trace,
    )


def validate_coc(envelope: COCEnvelope) -> bool:
    if not isinstance(envelope, dict):
        return False
    required = {
        "version", "event_type", "source_id",
        "timestamp", "payload", "session_trace"
    }
    if not required.issubset(envelope.keys()):
        return False
    if envelope["version"] != COC_VERSION:
        return False
    if envelope["event_type"] != "document.resolved":
        return False
    if "machine_readable" not in envelope["payload"]:
        return False
    return True
