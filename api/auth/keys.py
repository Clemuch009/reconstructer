# api/auth/keys.py

import hashlib
import secrets
import string
from typing import Optional
from typing_extensions import TypedDict


# ---------------------------------
# Key format
# qrynt_live_{32 alphanumeric}
# qrynt_test_{32 alphanumeric}
# ---------------------------------

_ALPHABET  = string.ascii_letters + string.digits
_KEY_LENGTH = 32

_PREFIXES = {
    "live": "qrynt_live_",
    "test": "qrynt_test_",
}


# ---------------------------------
# Contracts
# ---------------------------------

class APIKey(TypedDict):
    key:        str    # full key — shown to user ONCE on creation
    prefix:     str    # first 16 chars — safe to store and display
    key_hash:   str    # sha256 of full key — stored in Firestore
    key_type:   str    # "live" | "test"
    created_at: str    # ISO-8601 UTC


class StoredAPIKey(TypedDict):
    prefix:     str    # first 16 chars — shown in dashboard
    key_hash:   str    # sha256 — used for verification
    key_type:   str    # "live" | "test"
    created_at: str
    last_used:  Optional[str]
    active:     bool


# ---------------------------------
# Generation
# ---------------------------------

def generate_key(key_type: str = "live") -> APIKey:
    """
    Generate a new API key.

    Format: qrynt_live_{32 alphanumeric}
            qrynt_test_{32 alphanumeric}

    Rules:
    - Full key shown to user ONCE — never stored in plaintext
    - key_hash stored in Firestore — used for verification
    - prefix stored for display — safe, not secret
    - Uses cryptographically secure random generation
    """
    from datetime import datetime, timezone

    if key_type not in _PREFIXES:
        key_type = "live"

    prefix_str = _PREFIXES[key_type]
    random_part = "".join(
        secrets.choice(_ALPHABET) for _ in range(_KEY_LENGTH)
    )
    full_key    = prefix_str + random_part
    prefix      = full_key[:16]   # "qrynt_live_J8mK" — safe to display
    key_hash    = _hash_key(full_key)

    return APIKey(
        key=full_key,
        prefix=prefix,
        key_hash=key_hash,
        key_type=key_type,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------
# Hashing
# ---------------------------------

def _hash_key(key: str) -> str:
    """
    SHA-256 hash of full API key.
    Stored in Firestore — never the plaintext key.
    """
    return hashlib.sha256(key.encode()).hexdigest()


def hash_key(key: str) -> str:
    """Public interface for key hashing — used during verification."""
    return _hash_key(key)


# ---------------------------------
# Verification
# ---------------------------------

def verify_key_format(key: str) -> tuple[bool, str]:
    """
    Verify API key has correct format before DB lookup.
    Returns (is_valid, key_type).

    Fast check — no DB call needed.
    Rejects obviously malformed keys immediately.
    """
    for key_type, prefix in _PREFIXES.items():
        if key.startswith(prefix):
            remainder = key[len(prefix):]
            if (
                len(remainder) == _KEY_LENGTH
                and all(c in _ALPHABET for c in remainder)
            ):
                return True, key_type
    return False, ""


def to_stored_key(api_key: APIKey) -> StoredAPIKey:
    """
    Convert generated APIKey to StoredAPIKey for Firestore.
    Full key is never included — only hash and prefix.
    """
    return StoredAPIKey(
        prefix=api_key["prefix"],
        key_hash=api_key["key_hash"],
        key_type=api_key["key_type"],
        created_at=api_key["created_at"],
        last_used=None,
        active=True,
    )
