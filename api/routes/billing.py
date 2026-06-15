# api/routes/billing.py

from typing import Optional
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from api.auth.paddle import (
    get_checkout_config,
    create_portal_session,
    get_subscription_status,
    verify_webhook,
    parse_webhook,
    PRICE_TIER_MAP,
)
from api.auth.firestore import get_user, _get_db, USERS_COL, TIER_LIMITS


router = APIRouter(prefix="/billing", tags=["Billing"])


# ---------------------------------
# Request models
# ---------------------------------

class CheckoutConfigRequest(BaseModel):
    id_token: str
    tier:     str    # "pro" | "team"


class PortalRequest(BaseModel):
    id_token: str


# ---------------------------------
# Firestore paddle field updater
# ---------------------------------

def _update_paddle_fields(uid: str, fields: dict) -> None:
    """
    Update paddle sub-document on user record.
    Merges fields — does not overwrite entire paddle object.
    """
    db  = _get_db()
    ref = db.collection(USERS_COL).document(uid)
    ref.update({f"paddle.{k}": v for k, v in fields.items()})


def _update_user_tier(uid: str, tier: str) -> None:
    """
    Update user tier in Firestore.
    Called after successful Paddle webhook.
    """
    db  = _get_db()
    ref = db.collection(USERS_COL).document(uid)
    ref.update({"tier": tier})


def _ensure_paddle_field(uid: str) -> None:
    """
    Ensure the user document has a 'paddle' map before merging
    dot-notation fields into it. Older records created before
    Paddle integration may not have this key.
    """
    db   = _get_db()
    ref  = db.collection(USERS_COL).document(uid)
    doc  = ref.get()
    if not doc.exists:
        return
    user = doc.to_dict()
    if "paddle" not in user:
        ref.set({"paddle": {
            "customer_id":         None,
            "subscription_id":     None,
            "subscription_status": None,
            "current_period_end":  None,
        }}, merge=True)


# ---------------------------------
# GET /billing/checkout-config
# Frontend calls this, then opens Paddle.js overlay directly
# ---------------------------------

@router.get("/checkout-config")
async def checkout_config(
    id_token: str,
    tier:     str,
) -> dict:
    """
    Return configuration needed for the Paddle.js overlay checkout.

    Unlike Stripe, Paddle overlay checkout opens client-side —
    the backend does not create a session. This endpoint validates
    the request and returns the client token + price ID for the
    requested tier.

    Frontend then calls:
        Paddle.Checkout.open({
          items: [{ priceId: <price_id>, quantity: 1 }],
          customer: { email: <user_email> },
          customData: { uid: <uid>, tier: <tier> }
        })

    customData is echoed back in the webhook payload — this is how
    we map the completed purchase back to the Qrynt user.
    """
    from api.auth.router import _verify_firebase_token

    decoded = _verify_firebase_token(id_token)
    uid     = decoded["uid"]

    if tier not in ("pro", "team"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid tier '{tier}'. Use: pro | team",
        )

    user = get_user(uid)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found. Please sign up first.",
        )

    current_tier = user.get("tier", "starter")
    tier_rank    = {"free": 0, "starter": 1, "pro": 2, "team": 3, "enterprise": 4}
    if tier_rank.get(current_tier, 0) >= tier_rank.get(tier, 0):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Already on {current_tier} tier.",
        )

    try:
        config = get_checkout_config(tier)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )

    return {
        "client_token": config["client_token"],
        "price_id":     config["price_id"],
        "tier":         config["tier"],
        "environment":  config["environment"],
        "uid":          uid,
        "email":        user["email"],
    }


# ---------------------------------
# POST /billing/portal
# ---------------------------------

@router.post("/portal")
async def customer_portal(
    request: PortalRequest,
) -> dict:
    """
    Create Paddle Customer Portal session.
    Allows user to manage subscription, update payment, view invoices.

    Requires active Paddle customer_id — only available after
    first successful payment (set via webhook).
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

    customer_id = user.get("paddle", {}).get("customer_id")
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
            detail=f"Paddle error: {str(e)[:200]}",
        )

    return {"portal_url": result["portal_url"]}


# ---------------------------------
# POST /billing/webhook
# Paddle sends events here after checkout / subscription changes
# ---------------------------------

@router.post("/webhook", include_in_schema=False)
async def paddle_webhook(request: Request) -> dict:
    """
    Paddle webhook endpoint.
    Verifies signature, processes subscription events.

    Events handled:
    - transaction.completed          → activate subscription, set customer_id
    - subscription.created           → set subscription_id, tier
    - subscription.updated           → update tier on plan change, status
    - subscription.canceled          → downgrade to starter
    - subscription.past_due          → mark past_due

    Paddle requires 200 response — errors logged, not raised.

    uid is recovered from custom_data, which was set by the frontend
    when opening the overlay checkout (Paddle.Checkout.open customData).
    """
    payload    = await request.body()
    sig_header = request.headers.get("paddle-signature", "")

    if not verify_webhook(payload, sig_header):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook signature.",
        )

    event = parse_webhook(payload)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook payload.",
        )

    event_type = event.get("event_type", "")
    data       = event.get("data", {})

    # ─── transaction.completed ───
    if event_type == "transaction.completed":
        custom_data = data.get("custom_data") or {}
        uid  = custom_data.get("uid")
        tier = custom_data.get("tier")
        customer_id = data.get("customer_id")
        subscription_id = data.get("subscription_id")

        if uid and tier:
            _ensure_paddle_field(uid)
            _update_user_tier(uid, tier)
            _update_paddle_fields(uid, {
                "customer_id":         customer_id,
                "subscription_id":     subscription_id,
                "subscription_status": "active",
            })

    # ─── subscription.created ───
    elif event_type == "subscription.created":
        custom_data = data.get("custom_data") or {}
        uid  = custom_data.get("uid")
        tier = custom_data.get("tier")
        customer_id      = data.get("customer_id")
        subscription_id  = data.get("id")
        sub_status       = data.get("status")

        if not tier:
            items    = data.get("items", [])
            price_id = items[0]["price"]["id"] if items else None
            tier     = PRICE_TIER_MAP.get(price_id)

        if uid:
            _ensure_paddle_field(uid)
            if tier:
                _update_user_tier(uid, tier)
            _update_paddle_fields(uid, {
                "customer_id":         customer_id,
                "subscription_id":     subscription_id,
                "subscription_status": sub_status,
            })

    # ─── subscription.updated ───
    elif event_type == "subscription.updated":
        subscription_id = data.get("id")
        sub_status      = data.get("status")
        custom_data     = data.get("custom_data") or {}
        uid             = custom_data.get("uid")

        items    = data.get("items", [])
        price_id = items[0]["price"]["id"] if items else None
        tier     = PRICE_TIER_MAP.get(price_id)

        period_end = data.get("current_billing_period", {}).get("ends_at")

        if uid:
            _ensure_paddle_field(uid)
            if tier and sub_status == "active":
                _update_user_tier(uid, tier)
            _update_paddle_fields(uid, {
                "subscription_id":     subscription_id,
                "subscription_status": sub_status,
                "current_period_end":  period_end,
            })

    # ─── subscription.canceled ───
    elif event_type == "subscription.canceled":
        custom_data = data.get("custom_data") or {}
        uid = custom_data.get("uid")

        if not uid:
            # Fallback — find by subscription_id
            subscription_id = data.get("id")
            db   = _get_db()
            docs = (
                db.collection(USERS_COL)
                .where("paddle.subscription_id", "==", subscription_id)
                .limit(1)
                .stream()
            )
            for doc in docs:
                uid = doc.id

        if uid:
            _ensure_paddle_field(uid)
            _update_user_tier(uid, "starter")
            _update_paddle_fields(uid, {
                "subscription_status": "canceled",
            })

    # ─── subscription.past_due ───
    elif event_type == "subscription.past_due":
        subscription_id = data.get("id")
        db   = _get_db()
        docs = (
            db.collection(USERS_COL)
            .where("paddle.subscription_id", "==", subscription_id)
            .limit(1)
            .stream()
        )
        for doc in docs:
            doc.reference.update({"paddle.subscription_status": "past_due"})

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

    paddle_data     = user.get("paddle", {})
    subscription_id = paddle_data.get("subscription_id")

    # Fetch live status from Paddle if subscription exists
    live_status = None
    if subscription_id:
        try:
            live_status = get_subscription_status(subscription_id)
        except Exception:
            pass

    return {
        "uid":    uid,
        "tier":   user.get("tier", "starter"),
        "limits": TIER_LIMITS.get(user.get("tier", "starter"), {}),
        "paddle": {
            "customer_id":         paddle_data.get("customer_id"),
            "subscription_id":     subscription_id,
            "subscription_status": (
                live_status["subscription_status"]
                if live_status
                else paddle_data.get("subscription_status")
            ),
            "current_period_end": (
                live_status["current_period_end"]
                if live_status
                else paddle_data.get("current_period_end")
            ),
        },
        "usage": user.get("usage", {}),
    }
