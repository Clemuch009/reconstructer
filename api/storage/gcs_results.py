# api/storage/gcs_results.py
#
# Result storage for asynchronous file processing.
#
# Large processed envelopes (image-heavy PDFs ~120 MB, large HTML ~36 MB) cannot
# be returned in one synchronous HTTP response — the request exceeds memory /
# duration limits and the container is killed mid-response. Async processing
# solves the timeout, but the COMPLETED result still has to live somewhere the
# polling request (which may land on a DIFFERENT autoscaled instance) can read.
# Firestore cannot hold it (1 MiB document limit). GCS can — no size limit, and
# it is reachable from every instance.
#
# This module stores each completed envelope as a single GCS object and streams
# it back on retrieval. Retention honors the user's existing storage_settings:
#   - storage enabled  → kept for retention_days (auto_delete per their setting)
#   - storage disabled → kept only a short TRANSIENT window so the user can poll
#                        and download their just-processed result, then expires.

import json
from datetime import datetime, timezone, timedelta
from typing import Iterator, Optional, Tuple

# Result bucket. The Cloud Run service account must have
# roles/storage.objectAdmin on this bucket.
BUCKET = "moonlit-grail-386316-results"

# Transient retention for users WITHOUT persistent storage (free / starter).
# Long enough to reliably poll + download the result; short enough to be clearly
# non-persistent. This is the only retention value not already in the user's
# storage_settings.
TRANSIENT_RETENTION_HOURS = 6

# Object key prefix.
_RESULTS_PREFIX = "results"


# ── Local-disk fallback ────────────────────────────────────────────────────
#
# Option B: when GCS is unavailable (google-cloud-storage not installed, no
# credentials, or QRYNT_LOCAL_STORAGE set) results are stored on local disk
# instead, so the server runs and invoices can be tested without any GCS setup.
# The GCS path below is untouched and remains the production backend.

import os

def _use_local() -> bool:
    """True when we should use local disk instead of GCS."""
    if os.environ.get("QRYNT_LOCAL_STORAGE") == "1":
        return True
    # If GCS client can't be constructed (no package / no creds), fall back.
    try:
        _get_client()
        return False
    except Exception:
        return True

def _local_dir() -> str:
    d = os.environ.get("QRYNT_LOCAL_STORAGE_DIR", "/tmp/qrynt_results")
    os.makedirs(d, exist_ok=True)
    return d

def _local_path(job_id: str) -> str:
    return os.path.join(_local_dir(), f"{job_id}.json")


_client = None


def _get_client():
    """
    Lazy GCS client initialization. Mirrors firestore._get_db.
    Uses Application Default Credentials on Cloud Run; a service-account key
    locally if GOOGLE_APPLICATION_CREDENTIALS is set.
    """
    global _client
    if _client is None:
        from google.cloud import storage
        _client = storage.Client()
    return _client


def _object_path(job_id: str) -> str:
    return f"{_RESULTS_PREFIX}/{job_id}.json"


def compute_expiry(storage_settings: Optional[dict]) -> datetime:
    """
    Decide when a result should expire, from the user's storage_settings.

    storage_settings (existing shape):
        {"enabled": bool, "retention_days": int, "auto_delete": bool}

    Rules:
    - enabled is True  → expire at now + retention_days. (auto_delete governs
      whether it is actually purged; when auto_delete is False the expiry is
      effectively "keep" — represented here as a far-future timestamp.)
    - enabled is False → expire at now + TRANSIENT_RETENTION_HOURS (transient
      delivery window only).
    """
    now = datetime.now(timezone.utc)
    settings = storage_settings or {}

    if settings.get("enabled", False):
        if not settings.get("auto_delete", True):
            # Persistent (kept) — represent as far future so it never expires
            # at read time; bucket lifecycle is the only backstop.
            return now + timedelta(days=3650)
        retention_days = int(settings.get("retention_days", 7))
        return now + timedelta(days=retention_days)

    # Storage disabled — transient delivery window only.
    return now + timedelta(hours=TRANSIENT_RETENTION_HOURS)


def store_result(
    job_id:           str,
    envelope:         dict,
    storage_settings: Optional[dict] = None,
) -> Tuple[str, datetime]:
    """
    Stream the envelope JSON to GCS and return (gcs_path, expires_at).

    Serialization is incremental (iterencode) to avoid building the entire
    JSON string in memory at once — the same spike that crashes large
    synchronous responses. The chunks are joined into the upload payload;
    GCS upload itself streams to the network.

    expires_at is stored as object metadata AND returned so the caller can
    record it in the job's Firestore status doc (so the poll can check expiry
    without fetching the object).
    """
    expires_at = compute_expiry(storage_settings)

    if _use_local():
        # Local-disk fallback: write the envelope JSON to a file.
        import io
        path = _local_path(job_id)
        with open(path, "w", encoding="utf-8") as fh:
            for chunk in json.JSONEncoder().iterencode(envelope):
                fh.write(chunk)
        # Sidecar for expiry metadata (parity with GCS object metadata).
        with open(path + ".meta", "w", encoding="utf-8") as fh:
            fh.write(expires_at.isoformat())
        return _local_path(job_id), expires_at

    client = _get_client()
    bucket = client.bucket(BUCKET)
    blob   = bucket.blob(_object_path(job_id))

    blob.metadata = {"expires_at": expires_at.isoformat()}

    # Incremental serialization → bytes, streamed to GCS.
    # upload_from_file streams the file-like object without holding a second
    # full copy the way upload_from_string(json.dumps(...)) would.
    import io
    buffer = io.BytesIO()
    for chunk in json.JSONEncoder().iterencode(envelope):
        buffer.write(chunk.encode("utf-8"))
    buffer.seek(0)

    blob.upload_from_file(buffer, content_type="application/json")

    return _object_path(job_id), expires_at


def get_result_stream(job_id: str) -> Optional[Iterator[bytes]]:
    """
    Return an iterator of bytes for the stored result, or None if the object
    does not exist. Streams from GCS in chunks — never loads the whole result
    into memory — so the polling response stays flat in memory regardless of
    result size.

    Expiry is checked by the caller using the job's recorded expires_at; this
    function only handles "exists / does not exist".
    """
    if _use_local():
        path = _local_path(job_id)
        if not os.path.exists(path):
            return None
        def _iter_local() -> Iterator[bytes]:
            with open(path, "rb") as fh:
                while True:
                    data = fh.read(1024 * 256)
                    if not data:
                        break
                    yield data
        return _iter_local()

    client = _get_client()
    bucket = client.bucket(BUCKET)
    blob   = bucket.blob(_object_path(job_id))

    if not blob.exists():
        return None

    def _iter() -> Iterator[bytes]:
        # Stream the blob in chunks via a file-like reader.
        with blob.open("rb") as fh:
            while True:
                data = fh.read(1024 * 256)  # 256 KB chunks
                if not data:
                    break
                yield data

    return _iter()


def delete_result(job_id: str) -> None:
    """Best-effort delete of a stored result object."""
    try:
        if _use_local():
            path = _local_path(job_id)
            for p in (path, path + ".meta"):
                if os.path.exists(p):
                    os.remove(p)
            return
        client = _get_client()
        bucket = client.bucket(BUCKET)
        bucket.blob(_object_path(job_id)).delete()
    except Exception:
        pass
