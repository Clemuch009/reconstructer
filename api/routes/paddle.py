# api/auth/paddle.py

import os
import hashlib
import hmac
from typing import Optional
from typing_extensions import TypedDict


# ---------------------------------
# Paddle configuration — replace before production
# ---------------------------------

# Client-side token — used by Paddle.js overlay in the browser.
# Safe to expose to frontend. Sandbox token provided.
PADDLE_CLIENT_TOKEN = os.getenv("PADDLE_CLIENT_TOKEN", "test_75b8f4cd8740520d9986fd7818a")

# Server-side API key — from Paddle Dashboard → Developer Tools → Authentication.
# NEVER expose this to the frontend.
PADDLE_API_KEY = os.getenv("PADDLE_API_KEY", "YOUR_PADDLE_API_KEY")

# Webhook signing secret — from Paddle Dashboard → Developer Tools → Notifications.
PADDLE_WEBHOOK_SECRET = os.getenv("PADDLE_WEBHOOK_SECRET", "YOUR_PADDLE_WEBHOOK_SECRET")

# Price IDs — from Paddle Dashboard → Catalog → Products → Prices.
PADDLE_PRO_PRICE_ID  = os.getenv("PADDLE_PRO_PRICE_ID",  "pri_YOUR_PRO_PRICE_ID")
PADDLE_TEAM_PRICE_ID = os.getenv("PADDLE_TEAM_PRICE_ID", "pri_YOUR_TEAM_PRICE_ID")

# API base — sandbox by default. Switch to "https://api.paddle.com" for production.
PADDLE_API_BASE = os.getenv("PADDLE_API_BASE", "https://sandbox-api.paddle.com")


# Price ID → tier name mapping
PRICE_TIER_MAP: dict[str, str] = {
    PADDLE_PRO_PRICE_ID:  "pro",
    PADDLE_TEAM_PRICE_ID: "team",
}

# Tier → price ID mapping
TIER_PRICE_MAP: dict[str, str] = {
    "pro":  PADDLE_PRO_PRICE_ID,
    "team": PADDLE_TEAM_PRICE_ID,
}


# ---------------------------------
# Contracts
# ---------------------------------

class CheckoutConfig(TypedDict):
    client_token: str
    price_id:     str
    tier:         str


class PortalSessionResult(TypedDict):
    portal_url: str


class SubscriptionStatus(TypedDict):
    customer_id:         Optional[str]
    subscription_id:     Optional[str]
    subscription_status: Optional[str]
    tier:                str
    current_period_end:  Optional[str]


# ---------------------------------
# HTTP client — lazy import
# ---------------------------------

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {PADDLE_API_KEY}",
        "Content-Type":  "application/json",
    }


# ---------------------------------
# Checkout configuration
# Overlay checkout — frontend opens directly, no session creation needed
# ---------------------------------

def get_checkout_config(tier: str) -> CheckoutConfig:
    """
    Return configuration the frontend needs to open the Paddle.js overlay.

    Unlike Stripe Checkout, Paddle overlay does not require the backend
    to create a session — the frontend calls Paddle.Checkout.open()
    directly with the client token and price ID.

    The backend's role is limited to:
    1. Providing the client token + price ID (this function)
    2. Handling the resulting webhook after payment
    """
    price_id = TIER_PRICE_MAP.get(tier)
    if not price_id:
        raise ValueError(f"No price configured for tier: {tier}")

    return CheckoutConfig(
        client_token=PADDLE_CLIENT_TOKEN,
        price_id=price_id,
        tier=tier,
    )


# ---------------------------------
# Customer portal
# ---------------------------------

def create_portal_session(customer_id: str) -> PortalSessionResult:
    """
    Create a Paddle customer portal session.
    Allows user to manage subscription, update payment, view invoices.

    Paddle API: POST /customers/{customer_id}/portal-sessions
    """
    import httpx

    url = f"{PADDLE_API_BASE}/customers/{customer_id}/portal-sessions"

    with httpx.Client(timeout=10.0) as client:
        resp = client.post(url, headers=_headers(), json={})
        resp.raise_for_status()
        data = resp.json()

    portal_url = data.get("data", {}).get("urls", {}).get("general", {}).get("overview", "")

    return PortalSessionResult(portal_url=portal_url)


# ---------------------------------
# Subscription status
# ---------------------------------

def get_subscription_status(subscription_id: str) -> SubscriptionStatus:
    """
    Fetch current subscription status from Paddle.

    Paddle API: GET /subscriptions/{subscription_id}
    """
    import httpx

    url = f"{PADDLE_API_BASE}/subscriptions/{subscription_id}"

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers=_headers())
            resp.raise_for_status()
            data = resp.json().get("data", {})

        items    = data.get("items", [])
        price_id = items[0]["price"]["id"] if items else None
        tier     = PRICE_TIER_MAP.get(price_id, "starter")

        return SubscriptionStatus(
            customer_id=data.get("customer_id"),
            subscription_id=data.get("id"),
            subscription_status=data.get("status"),
            tier=tier,
            current_period_end=data.get("current_billing_period", {}).get("ends_at"),
        )
    except Exception:
        return SubscriptionStatus(
            customer_id=None,
            subscription_id=subscription_id,
            subscription_status="unknown",
            tier="starter",
            current_period_end=None,
        )


# ---------------------------------
# Webhook verification
# ---------------------------------

def verify_webhook(payload: bytes, signature_header: str) -> bool:
    """
    Verify Paddle webhook signature.

    Paddle signature header format:
        ts=<timestamp>;h1=<hmac_hex>

    Verification:
        HMAC-SHA256(secret, f"{ts}:{raw_body}") == h1
    """
    if not signature_header:
        return False

    try:
        parts = dict(
            p.split("=", 1) for p in signature_header.split(";") if "=" in p
        )
        ts = parts.get("ts", "")
        h1 = parts.get("h1", "")

        if not ts or not h1:
            return False

        signed_payload = f"{ts}:{payload.decode('utf-8')}"
        expected = hmac.new(
            PADDLE_WEBHOOK_SECRET.encode(),
            signed_payload.encode(),
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(expected, h1)
    except Exception:
        return False


def parse_webhook(payload: bytes) -> Optional[dict]:
    """
    Parse Paddle webhook JSON payload.
    Call only after verify_webhook() returns True.
    """
    import json
    try:
        return json.loads(payload)
    except Exception:
        return None
