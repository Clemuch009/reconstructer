# api/routes/billing.py

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from api.middleware.auth import require_auth, require_authenticated, RequestContext
from api.auth.stripe import (
    create_checkout_session,
    create_portal_session,
    handle_webhook,
    get_subscription_status,
    PRICE_TIER_MAP,
)
from api.auth.firestore import get_user, _get_db, USERS_COL


router = APIRouter(prefix="/billing", tags=["Billing"])


# ---------------------------------
# Request models
# ---------------------------------

class CheckoutRequest(BaseModel):
    id_token: str
    tier:     str    # "pro" | "team"


class PortalRequest(BaseModel):
    id_token: str


# ---------------------------------
# Firestore stripe field updater
# ---------------------------------

def _update_stripe_fields(uid: str, fields: dict) -> None:
    """
    Update stripe sub-document on user record.
    Merges fields — does not overwrite entire stripe object.
    """
    db  = _get_db()
    ref = db.collection(USERS_COL).document(uid)
    ref.update({f"stripe.{k}": v for k, v in fields.items()})


def _update_user_tier(uid: str, tier: str) -> None:
    """
    Update user tier in Firestore.
    Called after successful Stripe webhook.
    """
    db  = _get_db()
    ref = db.collection(USERS_COL).document(uid)
    ref.update({"tier": tier})


# ---------------------------------
# POST /billing/checkout
# ---------------------------------

@router.post("/checkout")
async def create_checkout(
    request: CheckoutRequest,
) -> dict:
    """
    Create Stripe Checkout session for Pro or Team subscription.

    Flow:
    1. Verify Firebase token
    2. Validate tier
    3. Create Stripe checkout session
    4. Return checkout_url — frontend redirects user

    Payment handled entirely by Stripe.
    Tier upgrade triggered by webhook on successful payment.
    """
    from firebase_admin import auth as firebase_auth
    from api.auth.router import _get_firebase, _verify_firebase_token

    decoded = _verify_firebase_token(request.id_token)
    uid     = decoded["uid"]

    if request.tier not in ("pro", "team"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid tier '{request.tier}'. Use: pro | team",
        )

    user = get_user(uid)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found. Please sign up first.",
        )

    # Already on this tier or higher
    current_tier = user.get("tier", "starter")
    tier_rank    = {"free": 0, "starter": 1, "pro": 2, "team": 3, "enterprise": 4}
    if tier_rank.get(current_tier, 0) >= tier_rank.get(request.tier, 0):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Already on {current_tier} tier.",
        )

    customer_id = user.get("stripe", {}).get("customer_id")

    try:
        result = create_checkout_session(
            uid=uid,
            email=user["email"],
            tier=request.tier,
            customer_id=customer_id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Stripe error: {str(e)[:200]}",
        )

    return {
        "session_id":   result["session_id"],
        "checkout_url": result["checkout_url"],
    }


# ---------------------------------
# POST /billing/portal
# ---------------------------------

@router.post("/portal")
async def customer_portal(
    request: PortalRequest,
) -> dict:
    """
    Create Stripe Customer Portal session.
    Allows user to manage subscription, update payment, cancel.

    Requires active Stripe customer_id — only available after
    first successful payment.
    """
    from api.auth.router import _verify_firebase_token

    decoded = _verify_firebase_token(request.id_token)
    uid     = decoded["uid"]

    user = get_user(uid)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    customer_id = user.get("stripe", {}).get("customer_id")
    if not customer_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No billing account found. Subscribe to a plan first.",
        )

    try:
        result = create_portal_session(customer_id=customer_id)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Stripe error: {str(e)[:200]}",
        )

    return {"portal_url": result["portal_url"]}


# ---------------------------------
# POST /billing/webhook
# Stripe sends events here after payment
# ---------------------------------

@router.post("/webhook", include_in_schema=False)
async def stripe_webhook(request: Request) -> dict:
    """
    Stripe webhook endpoint.
    Verifies signature, processes subscription events.

    Events handled:
    - checkout.session.completed    → activate subscription
    - customer.subscription.updated → update tier on plan change
    - customer.subscription.deleted → downgrade to starter
    - invoice.payment_failed        → mark past_due

    Stripe requires 200 response — errors logged, not raised.
    """
    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    event = handle_webhook(payload, sig_header)

    if event is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook signature.",
        )

    event_type = event.get("type", "")
    data       = event.get("data", {}).get("object", {})

    # ─── checkout.session.completed ───
    if event_type == "checkout.session.completed":
        uid  = data.get("metadata", {}).get("uid")
        tier = data.get("metadata", {}).get("tier")
        customer_id     = data.get("customer")
        subscription_id = data.get("subscription")

        if uid and tier:
            _update_user_tier(uid, tier)
            _update_stripe_fields(uid, {
                "customer_id":          customer_id,
                "subscription_id":      subscription_id,
                "subscription_status":  "active",
            })

    # ─── customer.subscription.updated ───
    elif event_type == "customer.subscription.updated":
        subscription_id = data.get("id")
        sub_status      = data.get("status")
        uid             = data.get("metadata", {}).get("uid")
        price_id        = (
            data.get("items", {})
                .get("data", [{}])[0]
                .get("price", {})
                .get("id")
        )
        tier = PRICE_TIER_MAP.get(price_id)

        if uid:
            if tier:
                _update_user_tier(uid, tier)
            _update_stripe_fields(uid, {
                "subscription_id":     subscription_id,
                "subscription_status": sub_status,
            })

    # ─── customer.subscription.deleted ───
    elif event_type == "customer.subscription.deleted":
        uid = data.get("metadata", {}).get("uid")
        if uid:
            _update_user_tier(uid, "starter")
            _update_stripe_fields(uid, {
                "subscription_id":     None,
                "subscription_status": "canceled",
            })

    # ─── invoice.payment_failed ───
    elif event_type == "invoice.payment_failed":
        customer_id = data.get("customer")
        # Find user by customer_id and mark past_due
        db   = _get_db()
        docs = (
            db.collection(USERS_COL)
            .where("stripe.customer_id", "==", customer_id)
            .limit(1)
            .stream()
        )
        for doc in docs:
            doc.reference.update({
                "stripe.subscription_status": "past_due"
            })

    return {"status": "received"}


# ---------------------------------
# GET /billing/status
# ---------------------------------

@router.get("/status")
async def billing_status(
    id_token: str,
) -> dict:
    """
    Return current billing status for authenticated user.
    Includes tier, subscription status, and period end date.
    """
    from api.auth.router import _verify_firebase_token

    decoded = _verify_firebase_token(id_token)
    uid     = decoded["uid"]

    user = get_user(uid)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    stripe_data     = user.get("stripe", {})
    subscription_id = stripe_data.get("subscription_id")

    # Fetch live status from Stripe if subscription exists
    live_status = None
    if subscription_id:
        try:
            live_status = get_subscription_status(subscription_id)
        except Exception:
            pass

    from api.auth.firestore import TIER_LIMITS

    return {
        "uid":                  uid,
        "tier":                 user.get("tier", "starter"),
        "limits":               TIER_LIMITS.get(user.get("tier", "starter"), {}),
        "stripe": {
            "customer_id":          stripe_data.get("customer_id"),
            "subscription_id":      subscription_id,
            "subscription_status":  (
                live_status["subscription_status"]
                if live_status
                else stripe_data.get("subscription_status")
            ),
            "current_period_end":   (
                live_status["current_period_end"]
                if live_status
                else None
            ),
        },
        "usage": user.get("usage", {}),
    }# api/routes/billing.py

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from api.middleware.auth import require_auth, require_authenticated, RequestContext
from api.auth.stripe import (
    create_checkout_session,
    create_portal_session,
    handle_webhook,
    get_subscription_status,
    PRICE_TIER_MAP,
)
from api.auth.firestore import get_user, _get_db, USERS_COL


router = APIRouter(prefix="/billing", tags=["Billing"])


# ---------------------------------
# Request models
# ---------------------------------

class CheckoutRequest(BaseModel):
    id_token: str
    tier:     str    # "pro" | "team"


class PortalRequest(BaseModel):
    id_token: str


# ---------------------------------
# Firestore stripe field updater
# ---------------------------------

def _update_stripe_fields(uid: str, fields: dict) -> None:
    """
    Update stripe sub-document on user record.
    Merges fields — does not overwrite entire stripe object.
    """
    db  = _get_db()
    ref = db.collection(USERS_COL).document(uid)
    ref.update({f"stripe.{k}": v for k, v in fields.items()})


def _update_user_tier(uid: str, tier: str) -> None:
    """
    Update user tier in Firestore.
    Called after successful Stripe webhook.
    """
    db  = _get_db()
    ref = db.collection(USERS_COL).document(uid)
    ref.update({"tier": tier})


# ---------------------------------
# POST /billing/checkout
# ---------------------------------

@router.post("/checkout")
async def create_checkout(
    request: CheckoutRequest,
) -> dict:
    """
    Create Stripe Checkout session for Pro or Team subscription.

    Flow:
    1. Verify Firebase token
    2. Validate tier
    3. Create Stripe checkout session
    4. Return checkout_url — frontend redirects user

    Payment handled entirely by Stripe.
    Tier upgrade triggered by webhook on successful payment.
    """
    from firebase_admin import auth as firebase_auth
    from api.auth.router import _get_firebase, _verify_firebase_token

    decoded = _verify_firebase_token(request.id_token)
    uid     = decoded["uid"]

    if request.tier not in ("pro", "team"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid tier '{request.tier}'. Use: pro | team",
        )

    user = get_user(uid)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found. Please sign up first.",
        )

    # Already on this tier or higher
    current_tier = user.get("tier", "starter")
    tier_rank    = {"free": 0, "starter": 1, "pro": 2, "team": 3, "enterprise": 4}
    if tier_rank.get(current_tier, 0) >= tier_rank.get(request.tier, 0):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Already on {current_tier} tier.",
        )

    customer_id = user.get("stripe", {}).get("customer_id")

    try:
        result = create_checkout_session(
            uid=uid,
            email=user["email"],
            tier=request.tier,
            customer_id=customer_id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Stripe error: {str(e)[:200]}",
        )

    return {
        "session_id":   result["session_id"],
        "checkout_url": result["checkout_url"],
    }


# ---------------------------------
# POST /billing/portal
# ---------------------------------

@router.post("/portal")
async def customer_portal(
    request: PortalRequest,
) -> dict:
    """
    Create Stripe Customer Portal session.
    Allows user to manage subscription, update payment, cancel.

    Requires active Stripe customer_id — only available after
    first successful payment.
    """
    from api.auth.router import _verify_firebase_token

    decoded = _verify_firebase_token(request.id_token)
    uid     = decoded["uid"]

    user = get_user(uid)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    customer_id = user.get("stripe", {}).get("customer_id")
    if not customer_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No billing account found. Subscribe to a plan first.",
        )

    try:
        result = create_portal_session(customer_id=customer_id)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Stripe error: {str(e)[:200]}",
        )

    return {"portal_url": result["portal_url"]}


# ---------------------------------
# POST /billing/webhook
# Stripe sends events here after payment
# ---------------------------------

@router.post("/webhook", include_in_schema=False)
async def stripe_webhook(request: Request) -> dict:
    """
    Stripe webhook endpoint.
    Verifies signature, processes subscription events.

    Events handled:
    - checkout.session.completed    → activate subscription
    - customer.subscription.updated → update tier on plan change
    - customer.subscription.deleted → downgrade to starter
    - invoice.payment_failed        → mark past_due

    Stripe requires 200 response — errors logged, not raised.
    """
    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    event = handle_webhook(payload, sig_header)

    if event is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook signature.",
        )

    event_type = event.get("type", "")
    data       = event.get("data", {}).get("object", {})

    # ─── checkout.session.completed ───
    if event_type == "checkout.session.completed":
        uid  = data.get("metadata", {}).get("uid")
        tier = data.get("metadata", {}).get("tier")
        customer_id     = data.get("customer")
        subscription_id = data.get("subscription")

        if uid and tier:
            _update_user_tier(uid, tier)
            _update_stripe_fields(uid, {
                "customer_id":          customer_id,
                "subscription_id":      subscription_id,
                "subscription_status":  "active",
            })

    # ─── customer.subscription.updated ───
    elif event_type == "customer.subscription.updated":
        subscription_id = data.get("id")
        sub_status      = data.get("status")
        uid             = data.get("metadata", {}).get("uid")
        price_id        = (
            data.get("items", {})
                .get("data", [{}])[0]
                .get("price", {})
                .get("id")
        )
        tier = PRICE_TIER_MAP.get(price_id)

        if uid:
            if tier:
                _update_user_tier(uid, tier)
            _update_stripe_fields(uid, {
                "subscription_id":     subscription_id,
                "subscription_status": sub_status,
            })

    # ─── customer.subscription.deleted ───
    elif event_type == "customer.subscription.deleted":
        uid = data.get("metadata", {}).get("uid")
        if uid:
            _update_user_tier(uid, "starter")
            _update_stripe_fields(uid, {
                "subscription_id":     None,
                "subscription_status": "canceled",
            })

    # ─── invoice.payment_failed ───
    elif event_type == "invoice.payment_failed":
        customer_id = data.get("customer")
        # Find user by customer_id and mark past_due
        db   = _get_db()
        docs = (
            db.collection(USERS_COL)
            .where("stripe.customer_id", "==", customer_id)
            .limit(1)
            .stream()
        )
        for doc in docs:
            doc.reference.update({
                "stripe.subscription_status": "past_due"
            })

    return {"status": "received"}


# ---------------------------------
# GET /billing/status
# ---------------------------------

@router.get("/status")
async def billing_status(
    id_token: str,
) -> dict:
    """
    Return current billing status for authenticated user.
    Includes tier, subscription status, and period end date.
    """
    from api.auth.router import _verify_firebase_token

    decoded = _verify_firebase_token(id_token)
    uid     = decoded["uid"]

    user = get_user(uid)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    stripe_data     = user.get("stripe", {})
    subscription_id = stripe_data.get("subscription_id")

    # Fetch live status from Stripe if subscription exists
    live_status = None
    if subscription_id:
        try:
            live_status = get_subscription_status(subscription_id)
        except Exception:
            pass

    from api.auth.firestore import TIER_LIMITS

    return {
        "uid":                  uid,
        "tier":                 user.get("tier", "starter"),
        "limits":               TIER_LIMITS.get(user.get("tier", "starter"), {}),
        "stripe": {
            "customer_id":          stripe_data.get("customer_id"),
            "subscription_id":      subscription_id,
            "subscription_status":  (
                live_status["subscription_status"]
                if live_status
                else stripe_data.get("subscription_status")
            ),
            "current_period_end":   (
                live_status["current_period_end"]
                if live_status
                else None
            ),
        },
        "usage": user.get("usage", {}),
    }
