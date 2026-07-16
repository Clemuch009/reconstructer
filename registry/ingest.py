# registry/ingest.py
#
# Document View → registry assertion.
#
# The bridge is deliberately thin, and deliberately one-directional: it records
# what a document ASSERTED. It does not record what we concluded about it. No
# verdict, no duplicate flag, no review status crosses this boundary — those are
# findings, computed from assertions on read, so they stay correct when evidence
# arrives or the engine improves.

from typing import Any, Dict, Optional

from registry.record import (
    RegistryRecord, build_record, RETENTION_IDENTITY,
)
from registry.keys import registry_keys
from relationship_engine.canonicalize import canonicalize_invoice


def record_from_view(
    doc_id: str,
    doc_type: str,
    view: Dict[str, Any],
    source: str = "",
    checksum: Optional[str] = None,
    dayfirst: bool = False,
) -> RegistryRecord:
    """Turn a resolved Document View into one append-only assertion.

    Carries `_line_items` into raw_fields so a missing total stays recoverable by
    arithmetic later (the ATC case: no stated Grand Total, line items summing to
    $1,500 — a complete derived sum is deterministic and trustworthy, and must
    not become a manual-review trigger just because a vendor omitted a label).

    Stores BOTH raw fields and canonical values, plus the engine version that
    produced the canonical ones — so beliefs can be spotted as stale and
    re-derived from the facts. See registry/record.py.
    """
    fields = dict(view.get("fields") or {})
    li = (view.get("tables") or {}).get("line_items")
    if li:
        fields["_line_items"] = li

    canonical = canonicalize_invoice(fields, dayfirst=dayfirst)
    keys = registry_keys(canonical, doc_type, checksum=checksum)

    return build_record(
        doc_id=doc_id,
        doc_type=doc_type,
        raw_fields=fields,
        canonical=canonical,
        identity_keys=keys,
        source=source,
        checksum=checksum,
        retention_class=RETENTION_IDENTITY,
    )
