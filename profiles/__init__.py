# profiles/__init__.py
#
# Profile registry.
#
# Profiles are declarative DATA (see profiles/invoice.py). This registry simply
# makes them discoverable by name so the API and console can offer them without
# hard-coding any vertical. Adding a new vertical (contract, bank statement,
# bill of lading) means writing another data structure and registering it here
# — no engine code changes.

from typing import Any, Dict, List, Optional

from profiles.invoice import INVOICE_PROFILE
from profiles.purchase_order import PURCHASE_ORDER_PROFILE
from profiles.payment import PAYMENT_PROFILE


# name → declarative profile data
_REGISTRY: Dict[str, Dict[str, Any]] = {
    INVOICE_PROFILE["metadata"]["id"]:        INVOICE_PROFILE,
    PURCHASE_ORDER_PROFILE["metadata"]["id"]: PURCHASE_ORDER_PROFILE,
    PAYMENT_PROFILE["metadata"]["id"]:        PAYMENT_PROFILE,
}


def get_profile(name: str) -> Optional[Dict[str, Any]]:
    """Return the declarative profile registered under `name`, or None."""
    return _REGISTRY.get(name)


def available_profiles() -> List[Dict[str, str]]:
    """List registered profiles for UI/API discovery (id, name, version)."""
    return [
        {
            "id":          p["metadata"]["id"],
            "name":        p["metadata"]["name"],
            "version":     p["metadata"]["version"],
            "description": p["metadata"].get("description", ""),
        }
        for p in _REGISTRY.values()
    ]


def register_profile(profile: Dict[str, Any]) -> None:
    """Register an additional declarative profile."""
    _REGISTRY[profile["metadata"]["id"]] = profile
