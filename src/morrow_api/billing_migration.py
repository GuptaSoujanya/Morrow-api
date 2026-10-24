"""Retire outstanding checkout links before moving a customer between providers."""

import stripe
from fastapi import HTTPException


def close_stripe_checkout(settings, account):
    customer = account["stripe_customer_id"]
    if not customer:
        return
    key = settings.stripe_secret_key.get_secret_value()
    if not key:
        raise HTTPException(
            409,
            "Existing Stripe billing must be checked before switching payment providers. Contact support.",
        )
    try:
        subscriptions = stripe.Subscription.list(
            customer=customer, status="all", limit=100, api_key=key
        )
        if subscriptions.get("has_more") or any(
            sub["status"] not in ("canceled", "incomplete_expired") for sub in subscriptions["data"]
        ):
            raise HTTPException(
                409, "Use Manage billing to close your existing Stripe subscription first"
            )
        sessions = stripe.checkout.Session.list(
            customer=customer, status="open", limit=100, api_key=key
        )
        if sessions.get("has_more"):
            raise HTTPException(
                409, "Existing checkout links need a billing review before switching providers"
            )
        for session in sessions["data"]:
            if session.get("mode") == "subscription":
                # If a concurrent payment completed this session, expiry fails;
                # do not create a second subscription at the new provider.
                stripe.checkout.Session.expire(session["id"], api_key=key)
    except stripe.StripeError:
        raise HTTPException(
            503,
            "Existing billing could not be confirmed. Refresh billing before trying another payment.",
        ) from None
