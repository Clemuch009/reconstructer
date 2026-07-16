# registry/provider.py
#
# THE EVIDENCE SEAM.
#
# Everything that can answer a question about a business event implements this
# one interface: the batch of documents in front of you, Qrynt's own history, or
# an ERP. The engines never learn which. There is deliberately no
#
#     if sap_exists: ...
#
# anywhere — that would couple reasoning to a vendor. The engine asks a typed
# question; something answers with evidence; the engine does not care who.
#
# ── Three query shapes, because the test corpus needs three ────────────────
#
#   candidates_for   "another document with this identity?"
#                    → duplicates. THE one that matters most right now: every
#                      adversarial case in the corpus — Horizon/disguised,
#                      Munich EN/DE, Vanguard, Vertex, Strata — was only caught
#                      because both files were uploaded TOGETHER. Real duplicates
#                      arrive weeks apart. Batch mode keeps nothing between
#                      calls, so without this the dedup engine fires almost never
#                      in production.
#
#   by_reference     "does the PO this invoice cites exist, and what does it say?"
#                    → mismatched POs, and the other half of split-invoice
#                      detection. Crosses document types.
#
#   vendor_history   "everything this vendor ever sent"
#                    → tax-ID and bank-account baselines. The capability neither
#                      the documents nor an ERP provides: an ERP knows the
#                      REGISTERED tax ID; only observation knows what is NORMAL.
#
# ── What a provider must never do ─────────────────────────────────────────
#
# Return conclusions. A provider returns RECORDS — what documents asserted. The
# finding is computed by the engine from those assertions, every time, so it is
# always current and re-derives correctly when the engine improves.

from typing import Any, Dict, Iterable, List, Optional, Protocol

from registry.record import RegistryRecord, ENGINE_VERSION
from registry.keys import registry_keys
from relationship_engine.candidates import InvoiceRecord


class EvidenceProvider(Protocol):
    """Anything that can answer questions about business events: the current
    batch, Qrynt's registry, or an ERP."""

    def candidates_for(self, query: InvoiceRecord,
                       doc_type: Optional[str] = None) -> List[InvoiceRecord]:
        """Documents sharing an identity key with `query` — duplicate candidates.

        `doc_type` restricts the search to one kind of document, and callers
        doing duplicate detection MUST pass it. The registry is deliberately
        cross-type (an invoice must be able to find the PO it cites), which means
        an invoice and a purchase order share keys — same vendor, same PO
        reference. Without the filter the dedup engine is handed a PO as a
        candidate duplicate of an invoice and compares two different kinds of
        thing. A duplicate is the same document twice, not two documents that
        mention each other.
        """
        ...

    def by_reference(self, doc_type: str, reference: str) -> List[RegistryRecord]:
        """Documents of `doc_type` carrying `reference` (e.g. purchase_order
        PO-9921) — for cross-type checks."""
        ...

    def vendor_history(self, vendor_canon: str, limit: int = 200) -> List[RegistryRecord]:
        """Everything this vendor has sent — the basis for baselines."""
        ...


def rehydrate(rec: RegistryRecord) -> InvoiceRecord:
    """RegistryRecord → InvoiceRecord for the dedup engine.

    Re-derives canonical values from raw_fields whenever the stored beliefs came
    from an older engine. This is the entire payoff of storing raw + version:
    canonicalize.py changed five times in one session (US→European numbers,
    EN→DE/FR months, new fields), so an invoice canonicalized last week would
    NOT match the same invoice canonicalized today. Facts survive engine
    changes; beliefs get recomputed. A registry that stored only canonical
    values would silently rot with every improvement.
    """
    if rec.is_stale() or not rec.canonical:
        # InvoiceRecord canonicalises from raw on construction — current engine
        return InvoiceRecord(rec.doc_id, dict(rec.raw_fields))
    inv = InvoiceRecord(rec.doc_id, dict(rec.raw_fields))
    return inv


class InMemoryRegistry:
    """A full registry with a dict behind it.

    Not a toy: it is the same code path the Firestore registry uses, so registry
    semantics are testable without a network or a bill. The Firestore class below
    differs only in where records come from.
    """

    def __init__(self, records: Optional[Iterable[RegistryRecord]] = None):
        self._records: List[RegistryRecord] = []
        self._by_key: Dict[str, List[int]] = {}
        for r in (records or []):
            self.append(r)

    # ── append-only ───────────────────────────────────────────────────────
    def append(self, rec: RegistryRecord) -> None:
        """Add an assertion. There is no update() and no delete() by design —
        a correction is a NEW record plus a supersedes edge, never a mutation of
        what was originally asserted."""
        idx = len(self._records)
        self._records.append(rec)
        for k, v in (rec.identity_keys or {}).items():
            self._by_key.setdefault(f"{k}={v}", []).append(idx)

    def _lookup(self, key: str, val: str) -> List[RegistryRecord]:
        return [self._records[i] for i in self._by_key.get(f"{key}={val}", [])]

    # ── the three questions ───────────────────────────────────────────────
    def candidates_for(self, query: InvoiceRecord,
                       doc_type: Optional[str] = None) -> List[InvoiceRecord]:
        hits: Dict[str, RegistryRecord] = {}
        for k, v in (query.keys or {}).items():
            if not v:
                continue
            for rec in self._lookup(k, v):
                if rec.doc_id == query.id:
                    continue
                if doc_type and rec.doc_type != doc_type:
                    continue        # a PO is not a duplicate of an invoice
                hits[rec.doc_id] = rec
        return [rehydrate(r) for r in
                sorted(hits.values(), key=lambda r: (r.asserted_at, r.doc_id))]

    def by_reference(self, doc_type: str, reference: str) -> List[RegistryRecord]:
        out = self._lookup("doc_ref", f"{doc_type}|{reference}")
        if not out:
            out = self._lookup("po_ref", reference)
        return sorted(out, key=lambda r: (r.asserted_at, r.doc_id))

    def vendor_history(self, vendor_canon: str, limit: int = 200) -> List[RegistryRecord]:
        out = self._lookup("vendor", vendor_canon)
        return sorted(out, key=lambda r: (r.asserted_at, r.doc_id))[:limit]

    def all_records(self) -> List[RegistryRecord]:
        return list(self._records)


class FirestoreRegistry:
    """The registry over Firestore, per user.

    Deliberately ONE collection, not the six-store graph the design discussions
    reached for. A document record with provenance already IS an assertion
    ("document D from source S at time T asserts fields F"), so the claims model
    is fully expressed without triples — and without traversals, reification, or
    per-read graph billing on a store that has no joins.

    Path: users/{uid}/registry/{doc_id}
    Lookup is by indexed identity keys, so a query is O(1) in the number of keys
    rather than a scan or a graph walk.
    """

    COLLECTION = "registry"

    def __init__(self, db, uid: str):
        self._db = db
        self._uid = uid

    def _col(self):
        return (self._db.collection("users").document(self._uid)
                .collection(self.COLLECTION))

    def append(self, rec: RegistryRecord) -> None:
        """Append-only write. Uses doc_id as the key so re-ingesting the same
        document is idempotent rather than duplicating the assertion."""
        payload = rec.to_dict()
        # store keys flattened for indexed equality queries
        payload["_keys"] = [f"{k}={v}" for k, v in (rec.identity_keys or {}).items()]
        self._col().document(rec.doc_id).set(payload)

    def _query_key(self, key: str, val: str) -> List[RegistryRecord]:
        docs = self._col().where("_keys", "array_contains", f"{key}={val}").stream()
        return [RegistryRecord.from_dict(d.to_dict()) for d in docs]

    def candidates_for(self, query: InvoiceRecord,
                       doc_type: Optional[str] = None) -> List[InvoiceRecord]:
        hits: Dict[str, RegistryRecord] = {}
        for k, v in (query.keys or {}).items():
            if not v:
                continue
            for rec in self._query_key(k, v):
                if rec.doc_id == query.id:
                    continue
                if doc_type and rec.doc_type != doc_type:
                    continue        # a PO is not a duplicate of an invoice
                hits[rec.doc_id] = rec
        return [rehydrate(r) for r in
                sorted(hits.values(), key=lambda r: (r.asserted_at, r.doc_id))]

    def by_reference(self, doc_type: str, reference: str) -> List[RegistryRecord]:
        out = self._query_key("doc_ref", f"{doc_type}|{reference}")
        if not out:
            out = self._query_key("po_ref", reference)
        return sorted(out, key=lambda r: (r.asserted_at, r.doc_id))

    def vendor_history(self, vendor_canon: str, limit: int = 200) -> List[RegistryRecord]:
        out = self._query_key("vendor", vendor_canon)
        return sorted(out, key=lambda r: (r.asserted_at, r.doc_id))[:limit]
