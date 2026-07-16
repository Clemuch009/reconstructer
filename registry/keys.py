# registry/keys.py
#
# IDENTITY KEYS — the indexed handles a document can be found by.
#
# The dedup engine's keys (relationship_engine/identity.py) answer exactly one
# question: "is there another INVOICE with this identity?" That is not enough for
# what the registry has to serve. The test corpus demanded more:
#
#   po        Apex 5501a vs 5501b: same PO-9921, same $1,250, DIFFERENT invoice
#             numbers → split-invoice fraud, found only by grouping on PO. The
#             same key answers the mismatched-PO case from the other direction:
#             "does the PO this invoice cites actually exist, and does it agree?"
#             That lookup crosses document types — invoice → purchase_order — so
#             the key cannot live on an invoice-shaped index.
#
#   vendor    The key that makes vendor KNOWLEDGE possible: every document this
#             vendor ever sent. From that you get baselines — the usual tax ID,
#             the usual bank account, the usual terms. This is the fraud vector
#             an ERP cannot see: a vendor master knows the REGISTERED tax ID; it
#             does not know what is NORMAL for this vendor as observed. A bank
#             account changing on a known vendor is the classic invoice-fraud
#             pattern and only accumulated observation catches it.
#
#   tax_id    Missing / changed tax ID, looked up directly.
#
#   checksum  Physical identity. The same file twice is the strongest duplicate
#             there is, and it must not depend on the filename — we shipped a bug
#             where two uploads of one file shared a name, collapsed into a single
#             record, and produced ZERO candidate pairs.
#
# A key is only formed when every part of it is present (see _join). A composite
# built from a missing field would be a false handle — it would match documents
# that share nothing. Absence is not a value.

from typing import Any, Dict, Optional

from relationship_engine.identity import identity_keys as invoice_identity_keys


def _join(*parts: Optional[str]) -> Optional[str]:
    """Compose a key only if EVERY part exists. A key with a hole in it would
    match documents that have nothing in common."""
    if any(p is None or str(p).strip() == "" for p in parts):
        return None
    return "|".join(str(p).strip() for p in parts)


def registry_keys(
    canonical: Dict[str, Any],
    doc_type: str,
    checksum: Optional[str] = None,
) -> Dict[str, str]:
    """Every handle this document can be found by.

    Invoice-identity keys (vendor_invnum, vendor_amount_curr, ...) come from the
    dedup engine unchanged — Stages 1-6 keep working exactly as tested. The
    cross-type keys below are additions the registry needs.

    Returns only the keys that actually formed; a missing field yields no key
    rather than a broken one.
    """
    keys: Dict[str, str] = {}

    # dedup keys — reuse the tested builder, do not re-implement
    for k, v in (invoice_identity_keys(canonical) or {}).items():
        if v:
            keys[k] = v

    vendor = canonical.get("vendor_canon")
    po     = canonical.get("po_number_canon")
    tax_id = canonical.get("tax_id_canon")

    # ── cross-type handles ────────────────────────────────────────────────
    # PO grouping. Deliberately NOT vendor-scoped: the split-invoice case needs
    # every document citing PO-9921 regardless of type, and the PO-mismatch case
    # needs the PO itself, which a vendor-scoped key would hide if the invoice
    # and the PO disagree about the vendor — which is exactly the conflict we
    # want to surface.
    if po:
        keys["po_ref"] = po
    if po and vendor:
        keys["po_vendor"] = _join(po, vendor)

    # vendor history → baselines
    if vendor:
        keys["vendor"] = vendor

    if tax_id:
        keys["tax_id"] = tax_id
    if vendor and tax_id:
        keys["vendor_tax_id"] = _join(vendor, tax_id)

    # a document's own reference, by type — "does PO-9921 exist?"
    ref = (canonical.get("invoice_number_canon")
           if doc_type != "purchase_order" else po)
    if ref:
        keys["doc_ref"] = _join(doc_type, ref)

    # physical identity
    if checksum:
        keys["checksum"] = checksum

    return {k: v for k, v in keys.items() if v}
