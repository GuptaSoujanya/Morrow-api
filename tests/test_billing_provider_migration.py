import psycopg
import stripe
from test_razorpay_billing import checkout
from test_razorpay_billing import razorpay as razorpay


def existing_customer(ctx):
    with psycopg.connect(ctx["db"]["owner"]) as conn:
        conn.execute(
            "UPDATE users SET stripe_customer_id='cus_previous' WHERE id=%s",
            (ctx["users"]["alice"]["id"],),
        )


def test_razorpay_cannot_duplicate_stripe_payment_awaiting_webhook(razorpay, monkeypatch):
    existing_customer(razorpay)
    monkeypatch.setattr(
        stripe.Subscription, "list", lambda **kwargs: {"data": [{"status": "active"}]}
    )
    response = checkout(razorpay)
    assert response.status_code == 409
    assert not razorpay["provider"].subscriptions


def test_provider_switch_expires_old_stripe_checkout_before_new_subscription(razorpay, monkeypatch):
    existing_customer(razorpay)
    monkeypatch.setattr(stripe.Subscription, "list", lambda **kwargs: {"data": []})
    monkeypatch.setattr(
        stripe.checkout.Session,
        "list",
        lambda **kwargs: {"data": [{"id": "cs_old", "mode": "subscription"}]},
    )
    expired = []
    monkeypatch.setattr(
        stripe.checkout.Session, "expire", lambda sid, **kwargs: expired.append(sid)
    )
    assert checkout(razorpay).status_code == 200
    assert expired == ["cs_old"]
    assert len(razorpay["provider"].subscriptions) == 1


def test_failed_old_checkout_expiry_cannot_start_razorpay(razorpay, monkeypatch):
    existing_customer(razorpay)
    monkeypatch.setattr(stripe.Subscription, "list", lambda **kwargs: {"data": []})
    monkeypatch.setattr(
        stripe.checkout.Session,
        "list",
        lambda **kwargs: {"data": [{"id": "cs_old", "mode": "subscription"}]},
    )

    def completed(*args, **kwargs):
        raise stripe.InvalidRequestError("Checkout is no longer open", "session")

    monkeypatch.setattr(stripe.checkout.Session, "expire", completed)
    assert checkout(razorpay).status_code == 503
    assert not razorpay["provider"].subscriptions
