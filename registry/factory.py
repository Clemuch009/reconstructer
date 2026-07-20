# registry/factory.py
#
# Resolving a registry for a request.
#
# ── Availability is a REPORTED FACT, never a silent one ───────────────────
# A registry read can fail: no credentials, Firestore down, an anonymous
# caller with no uid to scope to. When that happens dedup must NOT break — the
# batch in front of us is still a valid comparison universe and the answer over
# it is still correct.
#
# But it is a WEAKER answer, and the caller must be told. "No duplicates found
# in this upload" and "no duplicates found in your history" are different
# claims, and silently substituting the first for the second is the exact
# failure this whole engine exists to prevent: an absence of evidence
# presented as evidence of absence. So every response says whether history was
# actually consulted.

import os
from typing import Any, Optional, Tuple

from registry.provider import InMemoryRegistry, FirestoreRegistry

# Process-local registry for local runs (QRYNT_LOCAL_STORAGE=1). Module-level so
# it survives between requests within one process — enough to exercise
# across-time behaviour locally. It is NOT durable and NOT per-user; it exists
# so the app runs without GCP credentials, same rationale as the auth bypass.
_local_registry: Optional[InMemoryRegistry] = None


def _local() -> InMemoryRegistry:
    global _local_registry
    if _local_registry is None:
        _local_registry = InMemoryRegistry()
    return _local_registry


def get_registry(uid: Optional[str]) -> Tuple[Optional[Any], str]:
    """Return (registry, reason).

    registry is None when history cannot be consulted; `reason` says why, and
    that reason is surfaced to the caller rather than swallowed.
    """
    if os.environ.get("QRYNT_LOCAL_STORAGE") == "1":
        return _local(), "local in-process registry (not durable)"

    if not uid:
        # Nothing to scope history to. An anonymous caller gets batch-only, and
        # is told so — rather than being quietly given a narrower answer.
        return None, "not signed in — comparing only the documents in this request"

    try:
        from api.auth.firestore import _get_db
        return FirestoreRegistry(_get_db(), uid), "registry"
    except Exception as e:                      # noqa: BLE001 — reported, not hidden
        return None, f"registry unavailable ({type(e).__name__}) — batch only"
