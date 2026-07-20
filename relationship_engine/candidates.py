# relationship_engine/candidates.py
#
# Stage 3 of the duplicate engine: CANDIDATE GENERATION.
#
# Never compare every invoice to every invoice. For each invoice, look up its
# identity keys (Stage 2) and return only the small set of prior invoices that
# share at least one key — the handful worth the expensive evidence comparison
# (Stage 4). This turns an O(N^2) problem into O(N) with cheap dict lookups.
#
# ── The pluggable seam ─────────────────────────────────────────────────────
# The engine depends on a CandidateProvider, not on where invoices are stored:
#
#   BatchCandidateProvider     — the other invoices in THIS upload (in-memory).
#                                Catches same-batch double submission. No storage.
#   RegistryCandidateProvider  — (built later) queries the existing per-user
#                                session ledger by identity key. Catches
#                                double-payment across history. Same interface.
#
# The engine, classifier, and policy never change when the backing store does.
# This is the generic-core / pluggable-backend discipline the codebase follows.

from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Tuple

from relationship_engine.canonicalize import canonicalize_invoice
from relationship_engine.identity import identity_keys, present_keys


# An "invoice record" is whatever the caller holds — we only require a stable id
# and access to its resolved fields. We keep the original opaque so callers get
# their own object back in results.

class InvoiceRecord:
    """A thin wrapper pairing a caller's record id with its canonical identity.
    Precomputes canonical fields and keys once, so lookups are pure dict work."""

    __slots__ = ("id", "fields", "canon", "keys", "doc_type")

    def __init__(self, record_id: str, fields: Dict[str, Any], dayfirst: bool = False,
                 doc_type: str = "invoice"):
        self.id       = record_id
        self.fields   = fields
        # The KIND of document this is. A duplicate is the same document twice —
        # a payment and the invoice it settles share invoice number, vendor,
        # amount and currency by DESIGN, and without this they score as a
        # NORMALIZED_DUPLICATE and BLOCK a legitimate invoice because its own
        # remittance advice was uploaded alongside it.
        self.doc_type = doc_type or "invoice"
        self.canon    = canonicalize_invoice(fields, dayfirst=dayfirst)
        self.keys     = present_keys(identity_keys(self.canon))


class CandidateProvider(Protocol):
    """The seam. Given a query invoice, return prior records that share at least
    one identity key (the candidates). Implementations differ only in WHERE the
    prior records live (this batch, or stored history)."""

    def candidates_for(self, query: InvoiceRecord) -> List[InvoiceRecord]:
        ...


class BatchCandidateProvider:
    """
    In-memory candidate provider over a fixed SET of invoices (one upload batch).

    Builds an index: identity-key → the records carrying that key. A query's
    candidates are the union of records sharing any of its keys, minus itself.
    No persistence, no external calls — the batch is the whole world.
    """

    def __init__(self, records: Iterable[InvoiceRecord]):
        self._records: List[InvoiceRecord] = list(records)
        # index: key_type → key_value → [record_id, ...]
        self._index: Dict[str, Dict[str, List[str]]] = {}
        self._by_id: Dict[str, InvoiceRecord] = {}
        for rec in self._records:
            self._by_id[rec.id] = rec
            for key_type, key_val in rec.keys.items():
                self._index.setdefault(key_type, {}).setdefault(key_val, []).append(rec.id)

    def candidates_for(self, query: InvoiceRecord) -> List[InvoiceRecord]:
        candidate_ids: set = set()
        for key_type, key_val in query.keys.items():
            bucket = self._index.get(key_type, {}).get(key_val)
            if bucket:
                candidate_ids.update(bucket)
        candidate_ids.discard(query.id)  # never a self-pair
        # deterministic order: by id, so results are stable
        return [self._by_id[cid] for cid in sorted(candidate_ids)]

    def all_records(self) -> List[InvoiceRecord]:
        return list(self._records)


def generate_candidate_pairs(
    records: List[InvoiceRecord],
    provider: Optional[CandidateProvider] = None,
) -> List[Tuple[InvoiceRecord, InvoiceRecord]]:
    """
    Produce the unique candidate PAIRS to hand to the evidence engine.

    For a batch, each pair (A, B) that shares a key is emitted once (unordered).
    If no provider is given, a BatchCandidateProvider over `records` is used —
    the common "dedupe this upload" case.

    Returns pairs as (earlier_id, later_id) by id order, deduplicated, so the
    evidence engine never scores the same pair twice.
    """
    if provider is None:
        provider = BatchCandidateProvider(records)

    seen: set = set()
    pairs: List[Tuple[InvoiceRecord, InvoiceRecord]] = []
    for rec in records:
        for cand in provider.candidates_for(rec):
            # Same-type only. Cross-type documents legitimately share identity
            # keys — an invoice, the PO it cites and the payment that settles it
            # all carry the same reference, vendor and amount. That is a
            # RELATIONSHIP, not a duplication, and the consistency engine reads
            # it as such. Pairing them here would BLOCK an invoice for matching
            # its own payment.
            if getattr(cand, "doc_type", "invoice") != getattr(rec, "doc_type", "invoice"):
                continue
            key = tuple(sorted((rec.id, cand.id)))
            if key in seen:
                continue
            seen.add(key)
            a, b = (rec, cand) if rec.id <= cand.id else (cand, rec)
            pairs.append((a, b))
    return pairs
