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
    RegistryRecord, build_record, new_assertion_id, RETENTION_IDENTITY,
)
from registry.keys import registry_keys
from relationship_engine.canonicalize import canonicalize_invoice


def record_from_view(
    doc_id: Optional[str],
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

    # Prefer the role verifier's total over a guessed one.
    #
    # When a vendor uses a total label no alias knows — "Balance Payable",
    # "Net Outstanding" — canonicalisation cannot resolve the total and will not
    # guess it from the line items while a tax line is present, because the sum
    # of line items is the SUBTOTAL and recording it would be provably wrong.
    #
    # The verifier can do what canonicalisation cannot: it SEARCHES the
    # document's money values for a composition that balances, and reports what
    # it found and how. On the invoice above it identifies 10,944 as "the sum of
    # subtotal, vat" — the right answer, reached without reading a single label.
    #
    # This is the same ordinal precedence used in api/routes/reconcile.py: a
    # SEARCHED arithmetic identity outranks an ASSUMED formula. It is not
    # overwriting anything either — raw_fields still records that the document
    # stated no total; only the CANONICAL amount, which exists precisely to be
    # derived, is filled, and it is flagged as derived with its evidence.
    ver = ((view.get("verification") or {}).get("total") or {})
    if canonical.get("amount_canon") is None and ver.get("verdict") == "FILL":
        identified = ver.get("identified")
        if identified is not None:
            canonical["amount_canon"] = float(identified)
            canonical["amount_derived"] = True
            canonical["amount_derivation"] = ver.get("evidence") or "identified by arithmetic"

    # Correct a MIS-EXTRACTED total. When field extraction grabbed the wrong
    # value as the total — e.g. an unusual label like "PO TOTAL (USD)" is not
    # recognised, so the total falls back to the first line-item amount — the
    # canonical total is wrong even though it is not None. The money-tree reads
    # STRUCTURE not labels: when it CONFIRMs the chain (subtotal[−disc+ship]+tax
    # = total) and lands on a DIFFERENT figure, its figure is the sound one and
    # must replace the mislabelled extraction, so the registry never stores a
    # total the document's own arithmetic contradicts.
    tree_verdict = ver.get("tree_verdict")
    identified = ver.get("identified")
    if (tree_verdict == "CONFIRM" and identified is not None
            and canonical.get("amount_canon") is not None
            and abs(float(identified) - float(canonical["amount_canon"])) > 0.02):
        canonical["amount_canon"] = float(identified)
        canonical["amount_corrected"] = True
        canonical["amount_derivation"] = (
            "field extraction picked a value the document's arithmetic "
            "contradicts; corrected to the chain-validated total")

    # Record the money-structure tree's authoritative arithmetic verdict. The
    # tree validates the FULL chain (subtotal − discount + shipping + tax =
    # total), so when it CONFIRMs, a flat "subtotal + tax = total" check that
    # ignores discount/shipping is simply wrong and must not raise a case. Store
    # the verdict so the consistency evaluator can defer to it.
    canonical["arithmetic_verdict"] = ver.get("tree_verdict") or ver.get("verdict")

    keys = registry_keys(canonical, doc_type, checksum=checksum)

    # No doc_id supplied → mint one. Callers should NOT reuse a per-request id
    # (position+filename): it repeats across requests and overwrites history.
    return build_record(
        doc_id=doc_id or new_assertion_id(source),
        doc_type=doc_type,
        raw_fields=fields,
        canonical=canonical,
        identity_keys=keys,
        source=source,
        checksum=checksum,
        retention_class=RETENTION_IDENTITY,
    )
