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


class DryRunOverlay:
    """A registry you can write to, whose writes never land.

    ── Why a dry run needs this at all ───────────────────────────────────
    "Would this invoice create a duplicate?" is a real question a finance team
    asks before they commit to anything, and it must be answerable WITHOUT the
    document entering history. But the answer has to be the one a real run would
    give, or the simulation is worthless — and a real run commits the assertion
    BEFORE evaluating it, precisely so that siblings in the same submission are
    visible to each other. Three identical invoices submitted together must see
    one another; if a dry run skipped the write, each would be evaluated against
    a world where the other two do not exist and cheerfully report "no
    duplicate" three times.

    So a dry run cannot mean "don't write". It means "write, evaluate, and then
    make it as if you never had".

    ── Why an overlay rather than a transaction ──────────────────────────
    Firestore has transactions; the in-process registry does not, and a rollback
    that only works on one backend is a rollback you cannot trust. An overlay
    needs neither: reads see the real registry PLUS whatever this run appended,
    writes accumulate only here, and when the request ends the whole thing is
    garbage. Identical answers, nothing persisted, same code path on both
    backends.

    The base registry is only ever READ through this class. That is the
    guarantee, and it is structural rather than remembered: there is no call
    that reaches base.append().
    """

    def __init__(self, base):
        self._base = base
        self._pending: List[RegistryRecord] = []

    # ── writes go nowhere ────────────────────────────────────────────────
    def append(self, rec: RegistryRecord) -> None:
        self._pending.append(rec)

    def all_records(self) -> List[RegistryRecord]:
        base = list(self._base.all_records()) if hasattr(self._base, "all_records") else []
        return base + list(self._pending)

    # ── reads see the real history AND this run's uncommitted records ────
    def candidates_for(self, query, doc_type=None):
        try:
            out = list(self._base.candidates_for(query, doc_type=doc_type))
        except TypeError:
            out = list(self._base.candidates_for(query))
        seen = {getattr(c, "id", None) for c in out}
        # Match the pending records the same way the base does — through the
        # same keys — so a dry run cannot be more or less sensitive than the
        # real thing.
        qkeys = set(getattr(query, "keys", {}).values() if isinstance(getattr(query, "keys", None), dict)
                    else getattr(query, "keys", []) or [])
        for rec in self._pending:
            if doc_type is not None and rec.doc_type != doc_type:
                continue
            cand = rehydrate(rec)
            if cand.id in seen or cand.id == getattr(query, "id", None):
                continue
            ckeys = set(cand.keys.values() if isinstance(cand.keys, dict) else cand.keys or [])
            if qkeys & ckeys:
                out.append(cand)
        return out

    def by_reference(self, doc_type: str, reference: str) -> List[RegistryRecord]:
        out = list(self._base.by_reference(doc_type, reference))
        ids = {r.doc_id for r in out}
        for rec in self._pending:
            if rec.doc_id in ids:
                continue
            c = rec.canonical or {}
            ref = (c.get("po_number_canon") if rec.doc_type == "purchase_order"
                   else c.get("invoice_number_canon"))
            if rec.doc_type == doc_type and ref == reference:
                out.append(rec)
            elif c.get("po_number_canon") == reference:
                out.append(rec)
        return out

    def vendor_history(self, vendor_canon: str, limit: int = 200) -> List[RegistryRecord]:
        out = list(self._base.vendor_history(vendor_canon, limit=limit))
        ids = {r.doc_id for r in out}
        for rec in self._pending:
            if rec.doc_id not in ids and (rec.canonical or {}).get("vendor_canon") == vendor_canon:
                out.append(rec)
        return out[:limit]


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
    # doc_type travels with the record: cross-type documents share identity keys
    # by design (an invoice, its PO, its payment), and pairing them would BLOCK
    # an invoice for matching its own payment.
    return InvoiceRecord(rec.doc_id, dict(rec.raw_fields), doc_type=rec.doc_type)


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
