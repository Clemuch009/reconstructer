"""Accuracy harness — measures extraction against ground truth read from the
documents themselves, not from the engine's opinion of them.

READ THIS BEFORE TRUSTING A NUMBER
==================================
Most of this corpus was authored alongside the engine. Measuring against
documents built to exercise what we already thought of tells you the engine does
what we designed, not what real invoices do. **Every number here is an upper
bound, not a score.** The corpus is also tiny (25 documents), so a single
document moves any percentage by 4 points.

What the harness IS good for:
  * catching regressions loudly instead of by eyeballing traces
  * separating WRONG from MISSING, which matter completely differently
  * the negative set — documents that are not invoices at all

WRONG vs MISSING is the distinction that matters
------------------------------------------------
A missing field is a loud absence: the caller sees None, the rule SKIPs, the
registry declines to record. A wrong field is a silent error that propagates
into identity keys, duplicate matching, and findings — and it is the failure
mode this engine keeps producing when it guesses. Every fix this session moved
values from WRONG to MISSING and that was always the right trade.

So the harness scores them separately and NEVER averages them into one number.

The negative set
----------------
Four documents are not invoices: a lecture on load-flow bus admittance, an
executive summary, an intelligence report, and a PDF with no text layer at all.
An extraction engine that produces an invoice_number from a lecture is worse
than one that produces nothing, because a downstream registry would record it.
These are scored on SILENCE.
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

U = "/mnt/c/Users/cleme/Downloads/qrynt_invoices_test"
D = "/mnt/c/Users/cleme/Downloads/invoices_test"

# Ground truth, transcribed from what each document SAYS.
# `None` = the document genuinely does not state this field.
# `_expect_silence` = not an invoice; any resolved identity field is a failure.
GROUND_TRUTH = {
    # ── real invoices ────────────────────────────────────────────────────
    U + "valid_invoice_vertex.pdf": dict(
        profile="invoice", invoice_number="VCA-992", total=5000.00,
        subtotal=5000.00, tax=0.00, currency="USD",
        note="clean baseline"),
    U + "invoice_inv90812.pdf": dict(
        profile="invoice", invoice_number="INV-90812", total=7952.00,
        subtotal=6900.00, tax=552.00, po_number="PO-2026-4402", currency="USD",
        note="REAL $500 over-charge: 6900+552=7452, states 7952"),
    U + "invoice_original_horizon.pdf": dict(
        profile="invoice", invoice_number="INV-2026-8819", total=4150.00,
        currency="USD"),
    U + "invoice_disguised_duplicate.pdf": dict(
        profile="invoice", invoice_number="INV-2026-8819", total=4150.00,
        currency="USD", note="same obligation relabelled BILLING STATEMENT"),
    U + "apex_invoice_5501a.pdf": dict(
        profile="invoice", invoice_number="INV-5501A", total=1250.00,
        po_number="PO-9921", currency="USD"),
    U + "apex_invoice_5501b.pdf": dict(
        profile="invoice", invoice_number="INV-5501B", total=1250.00,
        po_number="PO-9921", currency="USD"),
    U + "munich_eng_en.pdf": dict(
        profile="invoice", invoice_number="ME-2026-091", total=1850.00,
        currency="EUR"),
    U + "munich_eng_de.pdf": dict(
        profile="invoice", invoice_number="ME-2026-091", total=1850.00,
        currency="EUR", note="German: 1.850,00 — decimal comma"),
    U + "atc_invoice_standard.pdf": dict(
        profile="invoice", invoice_number="ATC-1102", total=1500.00,
        currency="USD"),
    U + "atc_invoice_missing_total.pdf": dict(
        profile="invoice", invoice_number="ATC-1102", total=1500.00,
        currency="USD", total_derived_ok=True,
        note="states NO total; Qrynt derives 1500 by summing line items"),
    U + "complex_vanguard_original.pdf": dict(
        profile="invoice", invoice_number="VCG-2026-9812", total=26476.50,
        subtotal=28310.00, currency="USD",
        note="volume discount: 28310 - 2196.50 + 363 = 26476.50"),
    U + "complex_vanguard_duplicate.pdf": dict(
        profile="invoice", invoice_number="VCG-2026-9812", total=26476.50,
        subtotal=28310.00, currency="USD"),
    U + "vertex_tax_exempt.pdf": dict(
        profile="invoice", invoice_number="VMG-4040", total=2000.00,
        subtotal=2000.00, tax=0.00, currency="USD"),
    U + "vertex_tax_inclusive.pdf": dict(
        profile="invoice", invoice_number="VMG-4040", total=2000.00,
        currency="USD", note="same number+amount, DIFFERENT recipient"),
    U + "strata_shared_services_rollup.pdf": dict(
        profile="invoice", invoice_number="SSC-2026-X89", total=40000.00,
        currency="USD", note="'Total Debit Value' label split across lines"),
    U + "strata_shared_services_split.pdf": dict(
        profile="invoice", invoice_number="SSC-2026-X89", total=40000.00,
        currency="USD", note="'Ref #' + 'Total Corporate Charge'"),
    U + "purchase_order_4402.pdf": dict(
        profile="purchase_order", po_number="PO-2026-4402", total=6250.00,
        subtotal=6250.00, currency="USD"),

    # ── demo corpus ──────────────────────────────────────────────────────
    D + "meridian_invoice_march.pdf": dict(
        profile="invoice", invoice_number="MFG-2026-0417", total=10080.00,
        subtotal=8400.00, tax=1680.00, tax_id="GB 428 119 733", currency="GBP"),
    D + "meridian_invoice_april.pdf": dict(
        profile="invoice", invoice_number="MFG-2026-0488", total=10080.00,
        subtotal=8400.00, tax=1680.00, tax_id="GB 428 119 733", currency="GBP"),
    D + "meridian_invoice_may.pdf": dict(
        profile="invoice", invoice_number="MFG-2026-0551", total=10944.00,
        subtotal=9120.00, tax=1824.00, tax_id="GB 428 119 733", currency="GBP",
        note="'Balance Payable' now recognised as total; 9120+1824=10944"),
    D + "meridian_invoice_june.pdf": dict(
        profile="invoice", invoice_number="MFG-2026-0613", total=10080.00,
        subtotal=8400.00, tax=1680.00, tax_id="GB 428 119 733", currency="GBP"),
    D + "meridian_invoice_impostor.pdf": dict(
        profile="invoice", invoice_number="2026/MFG/0771", total=27300.00,
        subtotal=22750.00, tax=4550.00, tax_id="GB 991 004 288", currency="GBP"),

    # ── the negative set: NOT invoices. Silence is the correct answer. ────
    U + "LECTURE_2_LOAD_FLOW_BUS_ADMITTANCE.pdf": dict(
        profile="invoice", _expect_silence=True,
        note="a lecture on power systems"),
    U + "Executive_Summary__1_.pdf": dict(
        profile="invoice", _expect_silence=True),
    U + "integrated_intelligence_report.pdf": dict(
        profile="invoice", _expect_silence=True),
    U + "pure_visual_sequence.pdf": dict(
        profile="invoice", _expect_silence=True,
        note="no text layer at all"),
}

# Currency is checked CANONICALLY, not as a resolved field, and the distinction
# cost a wrong measurement to notice.
#
# Almost no invoice carries a "Currency: USD" line — it prints "$5,000.00" and
# expects you to know. So `fields.currency` is legitimately None nearly
# everywhere, and the first run of this harness scored 11 documents as MISSING a
# currency the engine knows perfectly well: canonicalisation derives it from the
# symbol, and `currency_canon` is what the identity keys and the evidence engine
# actually compare. Two invoices for $1,000 and €1,000 conflict correctly.
#
# `fields` is what the DOCUMENT SAID. `canonical` is what the SYSTEM KNOWS.
# Measuring the wrong one manufactures failures that are not there — and would
# have sent me fixing an extraction gap that does not exist.
CANONICAL_FIELDS = {"currency": "currency_canon"}

MONEY = ("total", "subtotal", "tax")
IDENT = ("invoice_number", "po_number", "tax_id")
_TOL = 0.01


def _num(v):
    from relationship_engine.canonicalize import canon_amount
    return canon_amount(v)


def _same_ident(got, want):
    from relationship_engine.canonicalize import canon_invoice_number
    return canon_invoice_number(got) == canon_invoice_number(want)


def _same_tax_id(got, want):
    from relationship_engine.canonicalize import canon_tax_id
    return canon_tax_id(got) == canon_tax_id(want)


def run():
    import sitecustomize                                     # noqa: F401
    from ingestion.router import ingest
    from core.engine import TextReconstructionEngine
    from analysis.segment_filter import extract_kv_pairs, filter_segments
    from profiles.resolver import resolve_document_view
    from profiles import get_profile

    eng = TextReconstructionEngine()
    right = wrong = missing = 0
    wrongs, missings, halluc = [], [], []
    silent_ok = silent_bad = 0

    for path, truth in GROUND_TRUTH.items():
        name = os.path.basename(path)
        if not os.path.exists(path):
            print(f"  !! absent: {name}")
            continue
        prof = get_profile(truth["profile"])
        try:
            ing = ingest(open(path, "rb").read(), filename=name)
            if not (ing["text"] or "").strip():
                # No text layer — a scan. Extracting nothing is the CORRECT
                # answer, not a crash: the API returns 422 saying so. Counted as
                # silence, since that is exactly what it is.
                if truth.get("_expect_silence"):
                    silent_ok += 1
                else:
                    print(f"  !! {name}: no text layer (scan?) — nothing to extract")
                continue
            segs = eng.run(ing["text"])["machine_readable"]["segments"]
            view = resolve_document_view(
                extract_kv_pairs(segs),
                [m["content"] for m in filter_segments(segs, types=["table"])],
                prof, lines=ing["text"].split("\n"))
            f = dict(view["fields"])
            from relationship_engine.canonicalize import canonicalize_invoice
            fc = dict(f)
            fc["_line_items"] = (view.get("tables") or {}).get("line_items") or []
            canon = canonicalize_invoice(fc)
            for fld, ckey in CANONICAL_FIELDS.items():
                f[fld] = canon.get(ckey)
        except Exception as e:                               # noqa: BLE001
            print(f"  !! {name}: {type(e).__name__}: {e}")
            continue

        if truth.get("_expect_silence"):
            # Any identity field resolved from a non-invoice is a hallucination:
            # it is not merely useless, it is what a registry would record.
            found = {k: f.get(k) for k in IDENT + MONEY if f.get(k) is not None}
            if found:
                silent_bad += 1
                halluc.append((name, found))
            else:
                silent_ok += 1
            continue

        for field in IDENT + MONEY + ("currency",):
            if field not in truth:
                continue
            want, got = truth[field], f.get(field)
            if want is None:
                if got is None:
                    right += 1
                else:
                    wrong += 1
                    wrongs.append((name, field, got, "(document states none)"))
                continue
            if got is None:
                missing += 1
                missings.append((name, field, want))
                continue
            if field in MONEY:
                ok = _num(got) is not None and abs(_num(got) - want) < _TOL
            elif field == "tax_id":
                ok = _same_tax_id(got, want)
            elif field == "currency":
                ok = str(got).upper() == want
            else:
                ok = _same_ident(got, want)
            if ok:
                right += 1
            else:
                wrong += 1
                wrongs.append((name, field, got, want))

    total = right + wrong + missing
    print("=" * 68)
    print(f"  FIELD ACCURACY over {total} checked fields")
    print(f"    right   {right:3}   {right/total*100:5.1f}%")
    print(f"    WRONG   {wrong:3}   {wrong/total*100:5.1f}%   ← silent errors")
    print(f"    missing {missing:3}   {missing/total*100:5.1f}%   ← loud absences")
    print("=" * 68)
    if wrongs:
        print("\n  WRONG (each one is a bug — a wrong value propagates):")
        for n, fl, got, want in wrongs:
            print(f"    {n[:34]:34} {fl:15} got {str(got)[:18]:18} want {want}")
    if missings:
        print("\n  MISSING (honest absences — recoverable or genuinely unstated):")
        for n, fl, want in missings:
            print(f"    {n[:34]:34} {fl:15} want {want}")
    print(f"\n  NEGATIVE SET (not invoices — silence is correct)")
    print(f"    silent   {silent_ok}")
    print(f"    HALLUCINATED {silent_bad}")
    for n, found in halluc:
        print(f"      {n[:38]:38} {found}")
    return wrong, silent_bad


if __name__ == "__main__":
    run()
