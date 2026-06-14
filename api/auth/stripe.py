# api/auth/stripe.py

import os
from typing import Optional
from typing_extensions import TypedDict


# ---------------------------------
# Stripe configuration — replace before production
# ---------------------------------

STRIPE_SECRET_KEY     = os.getenv("STRIPE_SECRET_KEY",     "sk_test_YOUR_STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "whsec_YOUR_WEBHOOK_SECRET")
STRIPE_PRO_PRICE_ID   = os.getenv("STRIPE_PRO_PRICE_ID",   "price_YOUR_PRO_PRICE_ID")
STRIPE_TEAM_PRICE_ID  = os.getenv("STRIPE_TEAM_PRICE_ID",  "price_YOUR_TEAM_PRICE_ID")

# Price ID → tier name mapping
PRICE_TIER_MAP: dict[str, str] = {
    STRIPE_PRO_PRICE_ID:  "pro",
    STRIPE_TEAM_PRICE_ID: "team",
}

# Tier → price ID mapping
TIER_PRICE_MAP: dict[str, str] = {
    "pro":  STRIPE_PRO_PRICE_ID,
    "team": STRIPE_TEAM_PRICE_ID,
}


# ---------------------------------
# Stripe client — lazy init
# ---------------------------------

_stripe = None

def _get_stripe():
    global _stripe
    if _stripe is None:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        _stripe = stripe
    return _stripe


# ---------------------------------
# Contracts
# ---------------------------------

class CheckoutSessionResult(TypedDict):
    session_id:   str
    checkout_url: str


class PortalSessionResult(TypedDict):
    portal_url: str


class SubscriptionStatus(TypedDict):
    customer_id:         Optional[str]
    subscription_id:     Optional[str]
    subscription_status: Optional[str]
    tier:                str
    current_period_end:  Optional[str]


# ---------------------------------
# Customer management
# ---------------------------------

def create_stripe_customer(
    uid:   str,
    email: str,
) -> str:
    """
    Create Stripe customer for new subscriber.
    Returns customer_id.
    """
    stripe = _get_stripe()
    customer = stripe.Customer.create(
        email=email,
        metadata={"uid": uid},
    )
    return customer.id


def get_or_create_customer(
    uid:         str,
    email:       str,
    customer_id: Optional[str] = None,
) -> str:
    """
    Return existing customer_id or create new one.
    """
    if customer_id:
        return customer_id
    return create_stripe_customer(uid, email)


# ---------------------------------
# Checkout session
# ---------------------------------

def create_checkout_session(
    uid:          str,
    email:        str,
    tier:         str,
    customer_id:  Optional[str] = None,
    success_url:  str = "https://moonlit-grail-386316.web.app/dashboard?upgraded=true",
    cancel_url:   str = "https://moonlit-grail-386316.web.app/pricing",
) -> CheckoutSessionResult:
    """
    Create Stripe Checkout session for subscription.

    Flow:
    1. Get or create Stripe customer
    2. Create checkout session with correct price
    3. Return session_id + checkout_url
    4. Frontend redirects user to checkout_url
    5. Stripe handles payment
    6. Stripe webhook fires → update user tier in Firestore
    """
    stripe = _get_stripe()

    price_id = TIER_PRICE_MAP.get(tier)
    if not price_id:
        raise ValueError(f"No price configured for tier: {tier}")

    cust_id = get_or_create_customer(uid, email, customer_id)

    session = stripe.checkout.Session.create(
        customer=cust_id,
        payment_method_types=["card"],
        line_items=[
            {
                "price":    price_id,
                "quantity": 1,
            }
        ],
        mode="subscription",
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={
            "uid":  uid,
            "tier": tier,
        },
        subscription_data={
            "metadata": {
                "uid":  uid,
                "tier": tier,
            }
        },
        allow_promotion_codes=True,
    )

    return CheckoutSessionResult(
        session_id=session.id,
        checkout_url=session.url,
    )


# ---------------------------------
# Customer portal
# ---------------------------------

def create_portal_session(
    customer_id: str,
    return_url:  str = "https://moonlit-grail-386316.web.app/dashboard",
) -> PortalSessionResult:
    """
    Create Stripe Customer Portal session.
    Allows user to manage subscription, update payment, cancel.
    """
    stripe = _get_stripe()

    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url,
    )

    return PortalSessionResult(portal_url=session.url)


# ---------------------------------
# Webhook handler
# ---------------------------------

def handle_webhook(
    payload:   bytes,
    sig_header: str,
) -> Optional[dict]:
    """
    Verify and parse Stripe webhook event.
    Returns parsed event dict or None on failure.

    Signature verification prevents spoofed webhooks.
    """
    stripe = _get_stripe()

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
        return event
    except stripe.error.SignatureVerificationError:
        return None
    except Exception:
        return None


# ---------------------------------
# Subscription status
# ---------------------------------

def get_subscription_status(
    subscription_id: str,
) -> SubscriptionStatus:
    """
    Fetch current subscription status from Stripe.
    Used to verify active subscription on demand.
    """
    stripe = _get_stripe()

    try:
        sub  = stripe.Subscription.retrieve(subscription_id)
        tier = PRICE_TIER_MAP.get(
            sub["items"]["data"][0]["price"]["id"], "starter"
        )

        from datetime import datetime, timezone
        period_end = datetime.fromtimestamp(
            sub["current_period_end"], tz=timezone.utc
        ).isoformat()

        return SubscriptionStatus(
            customer_id=sub["customer"],
            subscription_id=sub["id"],
            subscription_status=sub["status"],
            tier=tier,
            current_period_end=period_end,
        )
    except Exception:
        return SubscriptionStatus(
            customer_id=None,
            subscription_id=subscription_id,
            subscription_status="unknown",
            tier="starter",
            current_period_end=None,
        )
