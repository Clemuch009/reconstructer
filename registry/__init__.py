"""The registry: an append-only record of what each document asserted.

Stores facts and relationships — never conclusions. See registry/record.py for
the reasoning, which traces to specific findings from the real-PDF test corpus.
"""
from registry.record import (
    RegistryRecord, build_record, content_checksum, fields_checksum,
    ENGINE_VERSION, RETENTION_CONTENT, RETENTION_IDENTITY,
)
from registry.keys import registry_keys
from registry.ingest import record_from_view
from registry.provider import (
    EvidenceProvider, InMemoryRegistry, FirestoreRegistry, rehydrate,
)
from registry.consistency import (
    evaluate, evaluate_dict, Finding,
    DUPLICATE, INTERNAL_INCONSISTENCY, PO_MISMATCH, PO_NOT_FOUND,
    MISSING_TAX_ID, TAX_ID_CHANGED, TAX_RATE_DEVIATION, PAYMENT_CONFLICT,
)
