"""Cases: what is disputed, and what was decided.

Deliberately separate from registry/ (what we know). See cases/model.py.
Precedent surfaces prior decisions as EVIDENCE; it never suppresses a check —
see cases/store.py for why that distinction is load-bearing.
"""
from cases.model import (
    Case, Resolution, new_case, new_resolution,
    OPEN, RESOLVED, SCOPE_PAIR, SCOPE_VENDOR,
)
from cases.store import CaseStore, InMemoryCaseStore, FirestoreCaseStore
from cases.service import (
    get_case_store, open_case_for, resolve_case, precedent_evidence, NEEDS_CASE,
)
