# registry/record.py
#
# THE REGISTRY RECORD — one append-only assertion per document seen.
#
# ── The rule this whole module exists to enforce ───────────────────────────
#
#   The registry stores FACTS and RELATIONSHIPS — never CONCLUSIONS.
#
# There is no `duplicate = yes` field here, and there must never be one. A
# duplicate is not a property of a document; it is a finding computed from
# evidence, and evidence changes: a correction arrives, a policy changes, an
# ERP gets connected, a reviewer overrides. A stored conclusion is a lie the
# moment any of those happen. Findings are computed on read, so they are always
# current — including when the engine itself improves, which it did five times
# in a single session.
#
# ── Why a record IS an assertion (and why there are no triples) ────────────
#
# The obvious design for "who asserted what" is a triple store: subject /
# predicate / object / source / confidence. That is RDF with reification, and
# on Firestore it is a bad trade — per-read billing, no joins, no traversal.
#
# But a document record ALREADY IS an assertion:
#
#     "document D, ingested from S at time T by engine version V,
#      asserts the field values F"
#
# Subject = the document. Predicates = its fields. Provenance = the record's own
# metadata. Same expressive power as triples; one collection; no traversal. Two
# records disagreeing about the same invoice number IS a conflicting assertion —
# discoverable by query, not by graph walk, because the registry is APPEND-ONLY
# and never overwrites.
#
# ── Every field below is here because a real test demanded it ──────────────
#
#   doc_type          Strata: a DEBIT MEMO and an INVOICE both claimed reference
#                     SSC-2026-X89. Different instruments, different accounting
#                     treatment. An invoice-shaped registry cannot even represent
#                     the pair — and PO matching and payment conflicts need the
#                     same type-agnostic shape. This is the single decision that
#                     would have been most expensive to reverse later.
#
#   raw_fields        canonicalize.py changed FIVE times in one session:
#     + engine_version    canon_amount  US-only → US + European ('1.850,00' was
#                                       parsed 1.85, now 1850.00)
#                         canon_date    EN-only → EN + DE + FR months
#                         canonicalize  + recipient/subtotal/tax/amount_derived
#                     An invoice canonicalized last week would NOT match the same
#                     invoice canonicalized today. So canonical values are a
#                     BELIEF about the raw text, not a fact. Store the raw text
#                     (the fact) plus the version that produced the belief, and
#                     re-derive when the engine moves. Facts survive; beliefs get
#                     recomputed.
#
#   checksum          Physical identity. The same file uploaded twice is the
#                     strongest possible duplicate — and we shipped a bug where
#                     identical filenames collapsed two uploads into ONE record
#                     and found 0 pairs. Identity must never rest on a filename.
#
#   identity_keys     Cross-type, not just invoice→invoice:
#                       po       Apex 5501a/b — same PO, different invoice
#                                numbers, same $1,250 = split-invoice fraud.
#                                Also "does the PO this invoice cites exist?"
#                       vendor   vendor history → tax-ID and bank-account
#                                baselines (the fraud vector no ERP can see,
#                                because an ERP knows the REGISTERED tax ID, not
#                                what is NORMAL for this vendor as observed).
#                       tax_id   missing / changed tax ID.
#
#   recipient         Vertex VMG-4040: same invoice number, vendor, total,
#                     currency AND date — but addressed to Acme Research
#                     Foundation vs Acme Retail Corp. Without recipient the
#                     engine auto-BLOCKS, silently electing one document as the
#                     real VMG-4040 on no evidence. Two documents to different
#                     customers are not one event.
#
#   asserted_at       Provenance. The one thing that is cheap now and
#   source            UNRECOVERABLE later. Everything else can be added against
#                     preserved facts; you cannot reconstruct who said what, when.
#
#   retention_class   Retention is a POLICY applied at the storage layer, never a
#                     property baked into the schema. Bake in "permanent" and you
#                     cannot add expiry without a migration; bake in "7 days" and
#                     vendor baselines are impossible forever. Tag instead:
#                       "content"   raw text / extracted fields — expirable
#                       "identity"  the fingerprint needed to prevent double
#                                   payment — retainable on a much more
#                                   defensible basis (fraud prevention) than
#                                   "we keep your invoices indefinitely"
#                     Currently unenforced (permanent, by decision) — but the
#                     shape keeps every future position open.
#
#   superseded_by     Append-only means a correction NEVER mutates the original.
#                     Day 1 invoice A, day 20 correction B → A stays exactly as
#                     asserted, plus an edge. History is not editable.
#
# Deliberately ABSENT: duplicate flags, review status, approval state, "posted",
# "paid". The first three are conclusions. The last two are EXTERNAL truth — the
# Strata boundary: no amount of reasoning over documents can establish whether an
# obligation was posted or reversed, because the document does not contain that
# fact. Putting them here would quietly re-import an assumption we rejected.

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import hashlib
import json
import uuid

# Bump when canonicalisation semantics change, so stored beliefs can be spotted
# as stale and re-derived from raw. (This session alone would have bumped it 5x.)
ENGINE_VERSION = "canon-2026.07.15"

RETENTION_CONTENT  = "content"    # raw text / fields — expirable
RETENTION_IDENTITY = "identity"   # the fingerprint — retainable


@dataclass
class RegistryRecord:
    """One document's assertion. Append-only: never edited after write."""

    # ── identity ──────────────────────────────────────────────────────────
    doc_id:    str
    doc_type:  str                     # invoice | purchase_order | payment | ...
    checksum:  Optional[str] = None    # physical identity (file bytes)

    # ── provenance (unrecoverable if not captured now) ────────────────────
    asserted_at:    str = ""           # ISO8601 UTC
    source:         str = ""           # filename / upload / api / erp
    engine_version: str = ENGINE_VERSION

    # ── the fact ──────────────────────────────────────────────────────────
    raw_fields: Dict[str, Any] = field(default_factory=dict)

    # ── the belief (re-derivable from raw_fields at engine_version) ───────
    canonical: Dict[str, Any] = field(default_factory=dict)

    # ── lookup ────────────────────────────────────────────────────────────
    identity_keys: Dict[str, str] = field(default_factory=dict)

    # ── policy tag, not schema ────────────────────────────────────────────
    retention_class: str = RETENTION_IDENTITY

    # ── append-only lineage ───────────────────────────────────────────────
    superseded_by: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "RegistryRecord":
        known = {f for f in RegistryRecord.__dataclass_fields__}
        return RegistryRecord(**{k: v for k, v in d.items() if k in known})

    def is_stale(self) -> bool:
        """True if the stored canonical beliefs were produced by an older engine
        and should be re-derived from raw_fields before being trusted."""
        return self.engine_version != ENGINE_VERSION


def content_checksum(raw: bytes) -> str:
    """Physical identity of the file itself. Identity must never rest on a
    filename — two uploads of one file share a name and we shipped a bug where
    that collapsed them into a single record and found zero pairs."""
    return hashlib.sha256(raw).hexdigest()


def fields_checksum(raw_fields: Dict[str, Any]) -> str:
    """Fallback physical identity when the bytes are unavailable (pasted text):
    a stable hash of the asserted values."""
    blob = json.dumps(raw_fields, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def new_assertion_id(label: str = "") -> str:
    """A fresh identity for ONE assertion.

    Identity is per SUBMISSION, not per content and not per filename:

      * Per filename is broken. "1·invoice.pdf" repeats every month, and a
        Firestore write keyed on it OVERWRITES the previous assertion —
        append-only violated, history destroyed, silently. (InMemoryRegistry
        appends to a list, so local runs never show this.)

      * Per content (a checksum) is also broken, and worse. The same file
        submitted twice IS the duplicate we exist to catch. Key on the checksum
        and the second submission collides with the first, gets skipped as
        "self", and the duplicate becomes invisible.

    So: unique per assertion. The checksum still travels on the record as a
    FIELD and is indexed as a key, which is what actually matches a re-submitted
    file — two assertions, one checksum, correctly flagged.

    The label is kept as a prefix purely so ids are legible in evidence output.
    """
    slug = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (label or "doc"))[:40]
    return f"{slug}·{uuid.uuid4().hex[:10]}"


def build_record(
    doc_id: str,
    doc_type: str,
    raw_fields: Dict[str, Any],
    canonical: Dict[str, Any],
    identity_keys: Dict[str, str],
    source: str = "",
    checksum: Optional[str] = None,
    retention_class: str = RETENTION_IDENTITY,
) -> RegistryRecord:
    """Assemble an assertion. Note what is NOT a parameter: any verdict, flag,
    or status. The registry records what a document SAID — not what we concluded
    about it."""
    return RegistryRecord(
        doc_id=doc_id,
        doc_type=doc_type,
        checksum=checksum or fields_checksum(raw_fields),
        asserted_at=datetime.now(timezone.utc).isoformat(),
        source=source,
        engine_version=ENGINE_VERSION,
        raw_fields=dict(raw_fields),
        canonical=dict(canonical),
        identity_keys=dict(identity_keys),
        retention_class=retention_class,
    )
