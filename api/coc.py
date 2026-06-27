# api/coc.py

import base64
import hashlib
from datetime import datetime, timezone
from typing import Any, List, Optional
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
    session_trace: SessionTrace
    visuals:       List[dict]   # serialized EmbeddedVisual (bytes → base64)


def _source_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _serialize_visuals(visuals: List) -> List[dict]:
    """
    Convert EmbeddedVisual list to JSON-serializable dicts.
    raw bytes → base64 string for API transport.
    """
    result = []
    for v in visuals:
        serialized = {
            "id":        v["id"],
            "page":      v.get("page"),
            "mime_type": v.get("mime_type", "image/png"),
            "width":     v.get("width"),
            "height":    v.get("height"),
            "warnings":  v.get("warnings", []),
        }
        img_bytes = v.get("image_bytes", b"")
        if img_bytes:
            serialized["data"] = base64.b64encode(img_bytes).decode("ascii")
        else:
            serialized["data"] = None
        result.append(serialized)
    return result


def build_coc(
    text:          str,
    output:        PostprocessOutput,
    session_trace: SessionTrace,
    visuals:       Optional[List] = None,
) -> COCEnvelope:
    """
    Wrap PostprocessOutput + SessionTrace + Visuals in versioned COC envelope.
    Visuals are serialized (bytes → base64) for JSON transport.
    """
    return COCEnvelope(
        version=COC_VERSION,
        event_type="document.resolved",
        source_id=_source_id(text),
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=output,
        session_trace=session_trace,
        visuals=_serialize_visuals(visuals or []),
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
