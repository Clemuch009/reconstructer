# cases/store.py
#
# Case + resolution storage, and PRECEDENT.
#
# ── The safety argument lives in this file ────────────────────────────────
#
# The tempting design is: human approves a tax-id change → store it as the
# vendor's new tax id → next invoice does not get reviewed. "The engine
# immediately avoids repeating the same investigation."
#
# That is an attack surface, and it defeats the exact check it optimises.
# TAX_ID_CHANGED exists to catch impersonation: correct vendor name, altered
# identity details. If one approved change permanently disables the check for
# that vendor, an attacker needs to get ONE change accepted and then owns the
# vendor forever — the detection is dark and nothing will ever raise it again.
# Precedent matching on a similarity score is worse: an attacker who can infer
# your resolutions crafts documents that match them, and "reviewer clicks Done,
# no reinvestigation" is the goal of the attack, not a feature.
#
# So this module holds one rule:
#
#     A resolution is EVIDENCE IN the next finding. It is never a SUPPRESSOR of
#     it. The check always runs. The finding is always raised. What a precedent
#     changes is how long it takes a human to close it — thirty minutes becomes
#     five seconds — not whether they are asked.
#
# That is the same rule as the role verifier (report, never overwrite) and the
# same rule as the registry (facts, never conclusions), applied to human
# decisions. It keeps the organisational learning the reports wanted, and drops
# only the part that would get someone robbed.
#
# Precedent is matched on exact (kind, vendor_canon) — not a similarity score.
# There is no threshold to tune, nothing to overfit, and nothing to game by
# approximating. If the reviewer wants a looser judgement, they can read.

from typing import Any, Dict, List, Optional, Protocol

from cases.model import (
    Case, Resolution, OPEN, RESOLVED, SCOPE_PAIR, SCOPE_VENDOR,
)


class CaseStore(Protocol):
    def open_case(self, case: Case) -> None: ...
    def get_case(self, case_id: str) -> Optional[Case]: ...
    def list_cases(self, status: Optional[str] = None, limit: int = 100) -> List[Case]: ...
    def add_resolution(self, res: Resolution) -> None: ...
    def resolutions_for(self, case_id: str) -> List[Resolution]: ...
    def precedents(self, kind: str, vendor_canon: Optional[str]) -> List[Resolution]: ...
    def find_open(self, kind: str, subject: str, counterparts: List[str]) -> Optional[Case]: ...


class InMemoryCaseStore:
    """Same code path as the Firestore store, without a network or a bill."""

    def __init__(self):
        self._cases: Dict[str, Case] = {}
        self._resolutions: List[Resolution] = []

    def open_case(self, case: Case) -> None:
        self._cases[case.case_id] = case

    def get_case(self, case_id: str) -> Optional[Case]:
        return self._cases.get(case_id)

    def list_cases(self, status: Optional[str] = None, limit: int = 100) -> List[Case]:
        out = [c for c in self._cases.values() if status is None or c.status == status]
        return sorted(out, key=lambda c: c.created_at, reverse=True)[:limit]

    def add_resolution(self, res: Resolution) -> None:
        # Resolutions are append-only: a decision made on a date is as
        # historical as a document's contents. Superseding one means adding
        # another, never editing this.
        self._resolutions.append(res)
        c = self._cases.get(res.case_id)
        if c:
            c.status = RESOLVED          # status is workflow, not knowledge

    def resolutions_for(self, case_id: str) -> List[Resolution]:
        return [r for r in self._resolutions if r.case_id == case_id]

    def precedents(self, kind: str, vendor_canon: Optional[str]) -> List[Resolution]:
        """Prior decisions about this KIND of dispute for this vendor.

        Exact match on (kind, vendor_canon) — no similarity score, so there is
        no threshold to tune and nothing to game by approximation.

        SCOPE_PAIR resolutions are included: a reviewer's reasoning about two
        specific documents is still the most useful thing the next reviewer can
        read, even though it made no claim beyond those documents.
        """
        if not vendor_canon:
            return []
        return sorted(
            [r for r in self._resolutions
             if r.kind == kind and r.vendor_canon == vendor_canon],
            key=lambda r: r.resolved_at, reverse=True)

    def find_open(self, kind: str, subject: str,
                  counterparts: List[str]) -> Optional[Case]:
        """An already-open case for the same dispute, so re-uploading a document
        does not spawn a second identical case."""
        cs = set(counterparts or [])
        for c in self._cases.values():
            if (c.status == OPEN and c.kind == kind
                    and c.subject == subject and set(c.counterparts) == cs):
                return c
        return None


class FirestoreCaseStore:
    """Cases and resolutions per user.

    Paths:  users/{uid}/cases/{case_id}
            users/{uid}/resolutions/{resolution_id}

    Two collections, not one: cases are workflow (status mutates), resolutions
    are append-only evidence. Keeping them apart means a status write can never
    touch a recorded decision.
    """

    def __init__(self, db, uid: str):
        self._db = db
        self._uid = uid

    def _cases_col(self):
        return self._db.collection("users").document(self._uid).collection("cases")

    def _res_col(self):
        return self._db.collection("users").document(self._uid).collection("resolutions")

    def open_case(self, case: Case) -> None:
        self._cases_col().document(case.case_id).set(case.to_dict())

    def get_case(self, case_id: str) -> Optional[Case]:
        d = self._cases_col().document(case_id).get()
        return Case.from_dict(d.to_dict()) if d.exists else None

    def list_cases(self, status: Optional[str] = None, limit: int = 100) -> List[Case]:
        q = self._cases_col()
        if status:
            q = q.where("status", "==", status)
        docs = q.limit(limit).stream()
        return sorted([Case.from_dict(d.to_dict()) for d in docs],
                      key=lambda c: c.created_at, reverse=True)

    def add_resolution(self, res: Resolution) -> None:
        self._res_col().document(res.resolution_id).set(res.to_dict())
        self._cases_col().document(res.case_id).update({"status": RESOLVED})

    def resolutions_for(self, case_id: str) -> List[Resolution]:
        docs = self._res_col().where("case_id", "==", case_id).stream()
        return [Resolution.from_dict(d.to_dict()) for d in docs]

    def precedents(self, kind: str, vendor_canon: Optional[str]) -> List[Resolution]:
        if not vendor_canon:
            return []
        docs = (self._res_col()
                .where("kind", "==", kind)
                .where("vendor_canon", "==", vendor_canon)
                .stream())
        return sorted([Resolution.from_dict(d.to_dict()) for d in docs],
                      key=lambda r: r.resolved_at, reverse=True)

    def find_open(self, kind: str, subject: str,
                  counterparts: List[str]) -> Optional[Case]:
        docs = (self._cases_col()
                .where("status", "==", OPEN)
                .where("kind", "==", kind)
                .where("subject", "==", subject)
                .stream())
        cs = set(counterparts or [])
        for d in docs:
            c = Case.from_dict(d.to_dict())
            if set(c.counterparts) == cs:
                return c
        return None
