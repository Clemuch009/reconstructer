# analysis/rules_engine.py
#
# Rules Engine (Qrynt Core) — a generic, declarative rule executor.
#
# NOT a "validation engine": validation is only ONE application of rules. The
# same input + declarative rules → results model also serves normalization,
# derivation, classification, risk scoring, and routing. Naming it the Rules
# Engine keeps the abstraction broad at zero implementation cost.
#
# The engine has NO domain knowledge. It does not know what an invoice, a
# contract, OCR, key-value extraction, or paragraphs are. It receives a
# canonical **Document View** and a list of declarative rules, evaluates each
# rule, and returns a structured report.
#
# ── Document View (canonical input) ──────────────────────────────────────
# {
#   "fields":   { "invoice_number": "...", "subtotal": 1200, "total": 1392 },
#   "tables":   { "line_items": [ {"amount": 500}, {"amount": 700} ] },
#   "metadata": { "page_count": 3 }
# }
# The engine only ever sees this normalized model — a Profile is responsible
# for producing it (field mapping, resolving "Total Due" → total, etc.).
#
# ── Six primitives (operations, not business meanings) ───────────────────
#   exists    — a field is present (and, by default, non-empty)
#   match     — a field matches a pattern (named format or regex)
#   compare   — value OP value            (==, !=, <, <=, >, >=)
#   compute   — agg(inputs) OP target     (sum, average, count, min, max)
#   contains  — value ∈ allowed-set
#   custom    — named predicate escape hatch (profiles should avoid it)
#
# ── Result status: PASS / FAIL / SKIPPED (no severity) ───────────────────
# Severity is POLICY (one customer's "error" is another's "warning") and lives
# in the Profile, not here. The engine reports only whether a rule passed,
# failed, or could not be evaluated because a declared dependency was missing.
#
# ── Dependencies → SKIPPED ───────────────────────────────────────────────
# A rule may declare "requires": [field, ...]. If any required field is absent
# from the Document View, the rule is SKIPPED (reason: missing dependency) —
# NOT failed. A missing input means "unevaluable", not "wrong".
#
# ── Evidence ─────────────────────────────────────────────────────────────
# Every result carries an `evidence` object (expected / actual / fields / ...).
# This is the seed of provenance: later it links to page / bbox.

import re
from typing import Any, Callable, Dict, List, Optional


# ─────────────────────────────────────────────────────────────────────────
# Status constants
# ─────────────────────────────────────────────────────────────────────────

PASS    = "PASS"
FAIL    = "FAIL"
SKIPPED = "SKIPPED"


# ─────────────────────────────────────────────────────────────────────────
# Value helpers (generic — no domain assumptions)
# ─────────────────────────────────────────────────────────────────────────

_NUMBER_CLEAN_RE = re.compile(r"[,\s$£€¥]")

def _to_number(value: Any) -> Optional[float]:
    """Best-effort numeric read. Strips thousands separators/spaces. None if
    not a number (currency symbols/units are a Profile concern, not here)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = _NUMBER_CLEAN_RE.sub("", value.strip())
        if s == "":
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


_NAMED_FORMATS: Dict[str, re.Pattern] = {
    "number":        re.compile(r"^-?[\d,\s]*\.?\d+$"),
    "integer":       re.compile(r"^-?[\d,\s]+$"),
    "date_iso":      re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "email":         re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    "currency_code": re.compile(r"^[A-Z]{3}$"),
    "non_empty":     re.compile(r"\S"),
}

_COMPARATORS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<":  lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">":  lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


# ─────────────────────────────────────────────────────────────────────────
# Document View access
# ─────────────────────────────────────────────────────────────────────────
#
# References into the Document View:
#   "subtotal"            → fields["subtotal"]
#   "fields.subtotal"     → fields["subtotal"]  (explicit)
#   "line_items.amount"   → column "amount" across tables["line_items"] rows
#   "tables.line_items.amount" → same, explicit
#   "metadata.page_count" → metadata["page_count"]
#
# A field reference resolves to a scalar; a table column reference resolves to
# a list of the column's values (used by compute aggregations).

_MISSING = object()


def _get_field(view: Dict[str, Any], ref: str) -> Any:
    """Resolve a scalar field reference. Returns _MISSING if absent."""
    fields = view.get("fields", {})
    meta   = view.get("metadata", {})
    if ref.startswith("fields."):
        ref = ref[len("fields."):]
        return fields.get(ref, _MISSING)
    if ref.startswith("metadata."):
        return meta.get(ref[len("metadata."):], _MISSING)
    if ref in fields:
        return fields[ref]
    if ref in meta:
        return meta[ref]
    return _MISSING


def _get_column(view: Dict[str, Any], ref: str) -> Any:
    """Resolve a table-column reference to a list of values, or _MISSING.
    Accepts 'table.column' or 'tables.table.column'."""
    tables = view.get("tables", {})
    parts = ref.split(".")
    if parts and parts[0] == "tables":
        parts = parts[1:]
    if len(parts) != 2:
        return _MISSING
    table_name, col = parts
    rows = tables.get(table_name)
    if rows is None:
        return _MISSING
    column = []
    for row in rows:
        if isinstance(row, dict) and col in row:
            column.append(row[col])
    return column


def _resolve_value(view: Dict[str, Any], ref: Any) -> Any:
    """Resolve an operand that may be a scalar field ref or a literal.
    Literals are given as {"const": X}; anything else is treated as a field
    reference string (scalar). Returns _MISSING if a field ref is absent."""
    if isinstance(ref, dict) and "const" in ref:
        return ref["const"]
    if isinstance(ref, str):
        return _get_field(view, ref)
    return ref  # already a literal (number/bool)


# ─────────────────────────────────────────────────────────────────────────
# Custom predicate registry (escape hatch)
# ─────────────────────────────────────────────────────────────────────────
#
# Profiles should avoid custom predicates, but one escape hatch is healthy for
# things impossible to express declaratively. A predicate is a function
# (view, rule) -> bool. It is referenced by name from a rule:
#   {"type": "custom", "predicate": "my_check", "requires": [...]}
# Nothing is registered by default; the interface exists so a Profile CAN plug
# one in without touching the engine.

_CUSTOM_PREDICATES: Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], bool]] = {}


def register_predicate(name: str, fn: Callable[[Dict[str, Any], Dict[str, Any]], bool]) -> None:
    """Register a named custom predicate. Overwrites any existing name."""
    _CUSTOM_PREDICATES[name] = fn


# ─────────────────────────────────────────────────────────────────────────
# Primitive executors — each returns (status, evidence)
# status is PASS/FAIL (SKIPPED is decided earlier by dependency check)
# ─────────────────────────────────────────────────────────────────────────

def _p_exists(rule, view):
    field = rule.get("field")
    val = _get_field(view, field)
    present = val is not _MISSING
    if present and rule.get("non_empty", True):
        present = val is not None and str(val).strip() != ""
    return (PASS if present else FAIL), {
        "field": field,
        "actual": None if val is _MISSING else val,
        "fields": [field],
    }


def _p_match(rule, view):
    field = rule.get("field")
    val = _get_field(view, field)
    if val is _MISSING:
        return FAIL, {"field": field, "error": "field not found", "fields": [field]}
    fmt = rule.get("format")
    if fmt == "regex":
        pattern = re.compile(rule.get("pattern", ""))
    else:
        pattern = _NAMED_FORMATS.get(fmt)
        if pattern is None:
            return FAIL, {"field": field, "error": f"unknown format '{fmt}'", "fields": [field]}
    ok = bool(pattern.search(str(val)))
    return (PASS if ok else FAIL), {"field": field, "actual": val, "format": fmt, "fields": [field]}


def _p_compare(rule, view):
    op = rule.get("op")
    if op not in _COMPARATORS:
        return FAIL, {"error": f"unknown compare op '{op}'"}
    left  = _resolve_value(view, rule.get("left"))
    right = _resolve_value(view, rule.get("right"))
    if left is _MISSING or right is _MISSING:
        return FAIL, {"error": "field not found",
                      "left": None if left is _MISSING else left,
                      "right": None if right is _MISSING else right}
    ln, rn = _to_number(left), _to_number(right)
    if ln is not None and rn is not None:
        ok = _COMPARATORS[op](ln, rn)
        return (PASS if ok else FAIL), {"expected": f"{ln} {op} {rn}", "actual": ok,
                                        "left": ln, "right": rn, "op": op}
    if op in ("==", "!="):
        ok = _COMPARATORS[op](left, right)
        return (PASS if ok else FAIL), {"left": left, "right": right, "op": op}
    # Ordering on non-numeric values: allow when both are strings. This is
    # correct for ISO dates (YYYY-MM-DD sorts lexicographically) and other
    # sortable strings. Mismatched types (e.g. str vs None) still fail clearly.
    if isinstance(left, str) and isinstance(right, str):
        ok = _COMPARATORS[op](left, right)
        return (PASS if ok else FAIL), {"left": left, "right": right, "op": op,
                                        "compared_as": "string"}
    return FAIL, {"error": "cannot order non-numeric/mismatched values",
                  "left": left, "right": right, "op": op}


_AGGREGATIONS = {
    "sum":     lambda ns: sum(ns),
    "average": lambda ns: (sum(ns) / len(ns)) if ns else None,
    "count":   lambda ns: float(len(ns)),
    "min":     lambda ns: min(ns) if ns else None,
    "max":     lambda ns: max(ns) if ns else None,
}


def _collect_numbers(view, inputs):
    """Resolve `inputs` (scalar field refs and/or table-column refs) into a
    flat list of numbers. Returns (numbers, missing_refs)."""
    numbers: List[float] = []
    missing: List[str] = []
    for ref in inputs:
        if isinstance(ref, dict) and "const" in ref:
            n = _to_number(ref["const"])
            if n is None:
                missing.append(str(ref))
            else:
                numbers.append(n)
            continue
        # try scalar field, then table column
        scalar = _get_field(view, ref) if isinstance(ref, str) else _MISSING
        if scalar is not _MISSING:
            n = _to_number(scalar)
            if n is None:
                missing.append(ref)
            else:
                numbers.append(n)
            continue
        column = _get_column(view, ref) if isinstance(ref, str) else _MISSING
        if column is not _MISSING:
            for cell in column:
                cn = _to_number(cell)
                if cn is not None:
                    numbers.append(cn)
            continue
        missing.append(ref)
    return numbers, missing


def _p_compute(rule, view):
    operation = rule.get("operation", "sum")
    agg = _AGGREGATIONS.get(operation)
    if agg is None:
        return FAIL, {"error": f"unknown compute operation '{operation}'"}
    inputs = rule.get("inputs", [])
    numbers, missing = _collect_numbers(view, inputs)
    if missing:
        return FAIL, {"error": "input not found or non-numeric", "missing": missing, "fields": inputs}

    computed = agg(numbers)
    if computed is None:
        return FAIL, {"error": "no values to aggregate", "operation": operation}

    # 'equals' target (with tolerance) is the common case; also allow op/target.
    tol = float(rule.get("tolerance", 0.0))
    if "equals" in rule:
        target_val = _resolve_value(view, rule["equals"])
        target = _to_number(target_val)
        if target is None:
            return FAIL, {"error": "target not found or non-numeric", "target": rule["equals"]}
        ok = abs(computed - target) <= tol
        return (PASS if ok else FAIL), {
            "operation": operation,
            "expected": target,
            "actual": round(computed, 6),
            "difference": round(computed - target, 6),
            "tolerance": tol,
            "fields": inputs + ([rule["equals"]] if isinstance(rule["equals"], str) else []),
        }
    # comparison form: computed OP target
    op = rule.get("op")
    if op in _COMPARATORS and "target" in rule:
        target = _to_number(_resolve_value(view, rule["target"]))
        if target is None:
            return FAIL, {"error": "target not found or non-numeric", "target": rule.get("target")}
        ok = _COMPARATORS[op](computed, target)
        return (PASS if ok else FAIL), {
            "operation": operation, "actual": round(computed, 6),
            "op": op, "target": target, "fields": inputs,
        }
    # bare compute (no assertion) — just report the value as PASS
    return PASS, {"operation": operation, "actual": round(computed, 6), "fields": inputs}


def _p_contains(rule, view):
    field = rule.get("field")
    val = _get_field(view, field)
    if val is _MISSING:
        return FAIL, {"field": field, "error": "field not found", "fields": [field]}
    allowed = rule.get("allowed", [])
    ok = val in allowed
    return (PASS if ok else FAIL), {"field": field, "actual": val,
                                    "allowed": allowed, "fields": [field]}


def _p_custom(rule, view):
    name = rule.get("predicate")
    fn = _CUSTOM_PREDICATES.get(name)
    if fn is None:
        return FAIL, {"error": f"custom predicate '{name}' not registered"}
    try:
        ok = bool(fn(view, rule))
        return (PASS if ok else FAIL), {"predicate": name}
    except Exception as exc:
        return FAIL, {"error": f"custom predicate error: {exc}", "predicate": name}


_PRIMITIVES = {
    "exists":   _p_exists,
    "match":    _p_match,
    "compare":  _p_compare,
    "compute":  _p_compute,
    "contains": _p_contains,
    "custom":   _p_custom,
}


# ─────────────────────────────────────────────────────────────────────────
# Dependency check → SKIPPED
# ─────────────────────────────────────────────────────────────────────────

def _missing_dependencies(rule: Dict[str, Any], view: Dict[str, Any]) -> List[str]:
    """Return declared 'requires' fields that are absent from the view. A field
    ref is satisfied by either a scalar field or a table column of that name."""
    missing = []
    for ref in rule.get("requires", []):
        if _get_field(view, ref) is not _MISSING:
            continue
        if _get_column(view, ref) is not _MISSING:
            continue
        missing.append(ref)
    return missing


# ─────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────

def evaluate(
    view:  Dict[str, Any],
    rules: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Evaluate `rules` against a canonical Document View.

    Each rule:
      "id"       : stable identifier
      "type"     : exists | match | compare | compute | contains | custom
      "requires" : optional list of field refs that must exist, else SKIPPED
      ...type-specific fields...

    Returns:
      {
        "results": [
          {"rule_id", "type", "status": PASS|FAIL|SKIPPED,
           "reason": <str|None>, "evidence": {...}}, ...
        ],
        "summary": {"total", "passed", "failed", "skipped"},
      }

    No `severity` — that is Profile policy. No `passed` boolean — a caller
    decides pass/fail policy from the counts (e.g. a Profile may treat some
    FAILs as warnings). A malformed/unknown rule fails closed with an error in
    `evidence` rather than raising.
    """
    results: List[Dict[str, Any]] = []
    passed = failed = skipped = 0

    for rule in rules:
        rid   = rule.get("id", "<unnamed>")
        rtype = rule.get("type")

        # Dependency gate first — missing input means unevaluable, not wrong.
        miss = _missing_dependencies(rule, view)
        if miss:
            results.append({
                "rule_id": rid, "type": rtype, "status": SKIPPED,
                "reason": "missing dependency",
                "evidence": {"missing": miss},
            })
            skipped += 1
            continue

        executor = _PRIMITIVES.get(rtype)
        if executor is None:
            results.append({
                "rule_id": rid, "type": rtype, "status": FAIL,
                "reason": f"unknown rule type '{rtype}'", "evidence": {},
            })
            failed += 1
            continue

        try:
            status, evidence = executor(rule, view)
        except Exception as exc:  # a bad rule must not crash the batch
            status, evidence = FAIL, {"error": f"rule execution error: {exc}"}

        results.append({
            "rule_id": rid, "type": rtype, "status": status,
            "reason": evidence.get("error"), "evidence": evidence,
        })
        if status == PASS:
            passed += 1
        else:
            failed += 1

    return {
        "results": results,
        "summary": {
            "total":   len(rules),
            "passed":  passed,
            "failed":  failed,
            "skipped": skipped,
        },
    }
