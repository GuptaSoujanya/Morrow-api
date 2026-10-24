import time

import psycopg
import stripe
from conftest import auth
from pydantic import SecretStr
from test_billing_storage_import import configure_billing, signed_event


def test_no_checkout_without_webhook_configuration(setup):
    configure_billing(setup)
    setup["settings"].stripe_webhook_secret = SecretStr("")
    client = setup["client"]
    data = client.get("/api/v1/plans").json()
    assert data["billing_available"]  # Existing customers can still manage billing.
    assert not any(plan["checkout_available"] for plan in data["items"])
    assert all("razorpay_plan_id" not in plan for plan in data["items"])
    assert (
        client.post(
            "/api/v1/billing/checkout", headers=auth(setup), json={"plan": "pro"}
        ).status_code
        == 503
    )


def test_checkout_reuses_matching_plan_and_expires_wrong_plan(setup, monkeypatch):
    configure_billing(setup)
    monkeypatch.setattr(stripe.Subscription, "list", lambda **kwargs: {"data": []})
    sessions = [{"id": "cs_other", "mode": "subscription", "metadata": {"price_id": "price_plus"}}]
    expired, created = [], []
    monkeypatch.setattr(stripe.checkout.Session, "list", lambda **kwargs: {"data": sessions})
    monkeypatch.setattr(
        stripe.checkout.Session, "expire", lambda sid, **kwargs: expired.append(sid)
    )

    def create(**kwargs):
        created.append(kwargs)
        return {"url": "https://checkout.stripe.com/new"}

    monkeypatch.setattr(stripe.checkout.Session, "create", create)
    headers = auth(setup)
    response = setup["client"].post(
        "/api/v1/billing/checkout", headers=headers, json={"plan": "pro"}
    )
    assert response.status_code == 200
    assert expired == ["cs_other"]
    assert created[0]["metadata"] == {"plan": "pro", "price_id": "price_pro"}
    sessions[:] = [
        {
            "mode": "subscription",
            "metadata": {"price_id": "price_pro"},
            "url": "https://checkout.stripe.com/same",
        }
    ]
    response = setup["client"].post(
        "/api/v1/billing/checkout", headers=headers, json={"plan": "pro"}
    )
    assert response.json()["url"] == "https://checkout.stripe.com/same"
    assert len(created) == 1


def test_completed_checkout_waiting_for_webhook_cannot_purchase_again(setup, monkeypatch):
    configure_billing(setup)
    monkeypatch.setattr(
        stripe.Subscription, "list", lambda **kwargs: {"data": [{"status": "active"}]}
    )
    response = setup["client"].post(
        "/api/v1/billing/checkout", headers=auth(setup), json={"plan": "pro"}
    )
    assert response.status_code == 409
    assert "Manage billing" in response.json()["detail"]


def test_administrator_cannot_buy_unneeded_subscription(setup):
    configure_billing(setup)
    assert (
        setup["client"]
        .post("/api/v1/billing/checkout", headers=auth(setup, "admin"), json={"plan": "pro"})
        .status_code
        == 409
    )


def test_past_due_subscription_keeps_identity_for_billing_recovery(setup, monkeypatch):
    configure_billing(setup)
    monkeypatch.setattr(
        stripe.Subscription,
        "list",
        lambda **kwargs: {"data": [{"id": "sub_recovery", "created": 1, "status": "past_due"}]},
    )
    raw, headers = signed_event(
        {
            "id": "evt_recovery",
            "type": "customer.subscription.updated",
            "data": {"object": {"customer": "cus_alice"}},
        }
    )
    assert (
        setup["client"].post("/api/v1/billing/webhook", content=raw, headers=headers).status_code
        == 200
    )
    with psycopg.connect(setup["db"]["owner"]) as conn:
        status, subscription = conn.execute(
            "SELECT subscription_status,stripe_subscription_id FROM users WHERE id=%s",
            (setup["users"]["alice"]["id"],),
        ).fetchone()
    assert (status, subscription) == ("past_due", "sub_recovery")
    assert setup["client"].get("/api/v1/me", headers=auth(setup)).json()["effective_plan"] == "free"


def test_missing_provider_period_cannot_grant_unlimited_access(setup, monkeypatch):
    configure_billing(setup)
    monkeypatch.setattr(
        stripe.Subscription,
        "list",
        lambda **kwargs: {
            "data": [
                {
                    "id": "sub_unconfirmed",
                    "created": 1,
                    "status": "active",
                    "items": {"data": [{"price": {"id": "price_pro"}}]},
                }
            ]
        },
    )
    raw, headers = signed_event(
        {
            "id": "evt_unconfirmed",
            "type": "customer.subscription.updated",
            "data": {"object": {"customer": "cus_alice"}},
        }
    )
    assert (
        setup["client"].post("/api/v1/billing/webhook", content=raw, headers=headers).status_code
        == 409
    )
    with psycopg.connect(setup["db"]["owner"]) as conn:
        assert conn.execute("SELECT count(*) FROM stripe_events").fetchone()[0] == 0
    assert setup["client"].get("/api/v1/me", headers=auth(setup)).json()["effective_plan"] == "free"


def test_late_stripe_event_preserves_razorpay_access(setup, monkeypatch):
    configure_billing(setup)
    with psycopg.connect(setup["db"]["owner"]) as conn:
        conn.execute(
            "UPDATE users SET plan_source='razorpay',plan_slug='pro',plan_expires_at=to_timestamp(%s) WHERE id=%s",
            (int(time.time()) + 86400, setup["users"]["alice"]["id"]),
        )
    monkeypatch.setattr(stripe.Subscription, "list", lambda **kwargs: {"data": []})
    raw, headers = signed_event(
        {
            "id": "evt_old_provider",
            "type": "customer.subscription.deleted",
            "data": {"object": {"customer": "cus_alice"}},
        }
    )
    assert (
        setup["client"].post("/api/v1/billing/webhook", content=raw, headers=headers).status_code
        == 200
    )
    assert setup["client"].get("/api/v1/me", headers=auth(setup)).json()["effective_plan"] == "pro"


def test_new_stripe_subscription_can_replace_expired_razorpay_access(setup, monkeypatch):
    configure_billing(setup)
    with psycopg.connect(setup["db"]["owner"]) as conn:
        conn.execute(
            "UPDATE users SET plan_source='razorpay',plan_slug='free',plan_expires_at=NULL WHERE id=%s",
            (setup["users"]["alice"]["id"],),
        )
    monkeypatch.setattr(
        stripe.Subscription,
        "list",
        lambda **kwargs: {
            "data": [
                {
                    "id": "sub_newStripe",
                    "status": "active",
                    "created": int(time.time()),
                    "items": {
                        "data": [
                            {
                                "price": {"id": "price_pro"},
                                "current_period_end": int(time.time()) + 86400,
                            }
                        ]
                    },
                }
            ]
        },
    )
    raw, headers = signed_event(
        {
            "id": "evt_new_provider",
            "type": "customer.subscription.created",
            "data": {"object": {"customer": "cus_alice"}},
        }
    )
    assert (
        setup["client"].post("/api/v1/billing/webhook", content=raw, headers=headers).status_code
        == 200
    )
    account = setup["client"].get("/api/v1/me", headers=auth(setup)).json()
    assert account["effective_plan"] == "pro"
    assert account["plan_source"] == "stripe"
