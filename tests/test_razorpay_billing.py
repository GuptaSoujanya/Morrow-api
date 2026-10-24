"""Synthetic provider responses only: no Razorpay account or payments used."""

import copy
import hashlib
import hmac
import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import psycopg
import pytest
from conftest import auth
from pydantic import SecretStr

from morrow_api import razorpay_billing as billing
from morrow_api.razorpay_client import RazorpayError


class Provider:
    def __init__(self):
        self.plans = {
            "plan_Pro": {
                "id": "plan_Pro",
                "period": "monthly",
                "interval": 1,
                "item": {"amount": 99900, "currency": "INR"},
            },
            "plan_Plus": {
                "id": "plan_Plus",
                "period": "monthly",
                "interval": 1,
                "item": {"amount": 49900, "currency": "INR"},
            },
        }
        self.subscriptions, self.invoices, self.payments = {}, {}, {}
        self.calls = []
        self.create_error = None
        self.fetch_error = False

    def get(self, path, *, params=None):
        self.calls.append(("GET", path, params))
        if self.fetch_error:
            raise RazorpayError()
        kind, identifier = path.split("/")
        records = {
            "plans": self.plans,
            "subscriptions": self.subscriptions,
            "payments": self.payments,
            "invoices": {item["id"]: item for group in self.invoices.values() for item in group},
        }
        return copy.deepcopy(records[kind][identifier])

    def post(self, path, data):
        self.calls.append(("POST", path, copy.deepcopy(data)))
        if path == "subscriptions":
            identifier = "sub_" + str(len(self.subscriptions) + 1)
            row = {"id": identifier, "status": "created", **copy.deepcopy(data)}
            self.subscriptions[identifier] = row
            if self.create_error:
                raise self.create_error
            return copy.deepcopy(row)
        identifier = path.split("/")[1]
        if not data["cancel_at_cycle_end"]:
            self.subscriptions[identifier]["status"] = "cancelled"
        return copy.deepcopy(self.subscriptions[identifier])

    def items(self, path, *, params=None):
        self.calls.append(("LIST", path, params))
        if self.fetch_error:
            raise RazorpayError()
        return iter(
            copy.deepcopy(
                list(self.subscriptions.values())
                if path == "subscriptions"
                else self.invoices.get(params["subscription_id"], [])
            )
        )

    def paid_cycle(self, identifier, *, amount=99900):
        stamp = int(time.time())
        self.subscriptions[identifier].update(
            status="active", current_start=stamp - 60, current_end=stamp + 86400 * 30
        )
        invoice = {
            "id": "inv_" + identifier[4:],
            "subscription_id": identifier,
            "payment_id": "pay_" + identifier[4:],
            "status": "paid",
            "currency": "INR",
            "amount": amount,
            "amount_paid": amount,
            "amount_due": 0,
            "billing_start": stamp - 60,
            "billing_end": stamp + 86400 * 30,
        }
        payment = {
            "id": invoice["payment_id"],
            "invoice_id": invoice["id"],
            "status": "captured",
            "captured": True,
            "currency": "INR",
            "amount": amount,
            "amount_refunded": 0,
        }
        self.invoices[identifier] = [invoice]
        self.payments[payment["id"]] = payment
        return invoice, payment


@pytest.fixture
def razorpay(setup, monkeypatch):
    settings = setup["settings"]
    settings.billing_provider = "razorpay"
    settings.razorpay_key_id = "rzp_test_fixture"
    settings.razorpay_key_secret = SecretStr("secret_fixture")
    settings.razorpay_webhook_secret = SecretStr("hook_fixture")
    provider = Provider()
    monkeypatch.setattr(billing, "RazorpayClient", lambda settings: provider)
    with psycopg.connect(setup["db"]["owner"]) as conn:
        conn.execute("UPDATE plans SET razorpay_plan_id='plan_Pro' WHERE slug='pro'")
        conn.execute("UPDATE plans SET razorpay_plan_id='plan_Plus' WHERE slug='plus'")
    return {**setup, "provider": provider, "headers": auth(setup)}


def checkout(ctx, plan="pro"):
    return ctx["client"].post(
        "/api/v1/billing/checkout", json={"plan": plan}, headers=ctx["headers"]
    )


def refresh(ctx):
    return ctx["client"].post("/api/v1/billing/refresh", json={}, headers=ctx["headers"])


def me(ctx):
    return ctx["client"].get("/api/v1/me", headers=ctx["headers"]).json()


def callback(ctx, subscription_id, payment_id="pay_1", *, signature=None):
    signature = (
        signature
        or hmac.new(
            b"secret_fixture", (payment_id + "|" + subscription_id).encode(), hashlib.sha256
        ).hexdigest()
    )
    return ctx["client"].post(
        "/api/v1/billing/razorpay/verify",
        headers=ctx["headers"],
        json={
            "razorpay_subscription_id": subscription_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        },
    )


def webhook(ctx, subscription_id, event="subscription.charged", **extra):
    body = json.dumps(
        {
            "event": event,
            "payload": {"subscription": {"entity": ctx["provider"].subscriptions[subscription_id]}},
            **extra,
        }
    ).encode()
    signature = hmac.new(b"hook_fixture", body, hashlib.sha256).hexdigest()
    return ctx["client"].post(
        "/api/v1/billing/razorpay/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature},
    )


def test_checkout_is_owned_durable_reusable_and_does_not_grant_access(razorpay):
    result = checkout(razorpay)
    assert result.status_code == 200, result.text
    assert result.json()["provider"] == "razorpay"
    assert result.json()["prefill"] == {"name": "Alice", "email": "alice@example.com"}
    assert result.json()["key_id"] == "rzp_test_fixture"
    assert "secret" not in result.text
    assert me(razorpay)["effective_plan"] == "free"
    assert checkout(razorpay).json()["subscription_id"] == result.json()["subscription_id"]
    assert len(razorpay["provider"].subscriptions) == 1
    assert checkout(razorpay, "plus").status_code == 409
    with psycopg.connect(razorpay["db"]["owner"]) as conn:
        identifier, owner = conn.execute("SELECT id,user_id FROM razorpay_subscriptions").fetchone()
    notes = razorpay["provider"].subscriptions[result.json()["subscription_id"]]["notes"]
    assert notes == {"morrow_attempt": str(identifier), "morrow_user": str(owner)}


def test_checkout_rejects_admin_foreign_fields_and_existing_stripe(razorpay):
    client = razorpay["client"]
    assert (
        client.post(
            "/api/v1/billing/checkout", json={"plan": "pro"}, headers=auth(razorpay, "admin")
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/v1/billing/checkout",
            json={"plan": "pro", "user_id": str(razorpay["users"]["bob"]["id"])},
            headers=razorpay["headers"],
        ).status_code
        == 422
    )
    with psycopg.connect(razorpay["db"]["owner"]) as conn:
        conn.execute(
            "UPDATE users SET stripe_subscription_id='sub_stripe',subscription_status='past_due' WHERE id=%s",
            (razorpay["users"]["alice"]["id"],),
        )
    assert checkout(razorpay).status_code == 409
    assert razorpay["provider"].subscriptions == {}


def test_checkout_does_not_charge_an_administrator_managed_account(razorpay):
    with psycopg.connect(razorpay["db"]["owner"]) as conn:
        conn.execute(
            "UPDATE users SET plan_source='admin',plan_slug='plus' WHERE id=%s",
            (razorpay["users"]["alice"]["id"],),
        )
    response = checkout(razorpay)
    assert response.status_code == 409 and "administrator" in response.text
    assert razorpay["provider"].subscriptions == {}


def test_concurrent_checkout_sees_committed_attempt_before_provider_returns(razorpay, monkeypatch):
    entered, finish = Event(), Event()
    original = razorpay["provider"].post

    def slow_post(path, data):
        entered.set()
        assert finish.wait(5)
        return original(path, data)

    monkeypatch.setattr(razorpay["provider"], "post", slow_post)
    with ThreadPoolExecutor(max_workers=1) as workers:
        first = workers.submit(checkout, razorpay)
        try:
            assert entered.wait(5)
            assert checkout(razorpay).status_code == 409
        finally:
            finish.set()
        assert first.result().status_code == 200
    assert len(razorpay["provider"].subscriptions) == 1


@pytest.mark.parametrize("field,value", [("currency", "USD"), ("amount", 100)])
def test_checkout_revalidates_remote_amount_and_currency(razorpay, field, value):
    razorpay["provider"].plans["plan_Pro"]["item"][field] = value
    assert checkout(razorpay).status_code == 422
    assert razorpay["provider"].subscriptions == {}


def test_checkout_requires_webhook_config_and_monthly_interval(razorpay):
    razorpay["settings"].razorpay_webhook_secret = SecretStr("")
    assert checkout(razorpay).status_code == 503
    plans = razorpay["client"].get("/api/v1/plans").json()
    assert not any(plan["checkout_available"] for plan in plans["items"])
    razorpay["settings"].razorpay_webhook_secret = SecretStr("hook_fixture")
    razorpay["provider"].plans["plan_Pro"]["interval"] = 12
    assert checkout(razorpay).status_code == 422


def test_unknown_creation_cannot_create_twice_and_refresh_recovers_original(razorpay):
    razorpay["provider"].create_error = RazorpayError()
    assert checkout(razorpay).status_code == 503
    assert checkout(razorpay).status_code == 409
    assert len(razorpay["provider"].subscriptions) == 1
    result = refresh(razorpay)
    assert result.status_code == 200, result.text
    assert result.json()["subscription"]["can_resume_checkout"]
    assert checkout(razorpay).json()["subscription_id"] == "sub_1"
    assert len(razorpay["provider"].subscriptions) == 1


def test_unknown_creation_without_provider_match_stays_reserved(razorpay):
    razorpay["provider"].create_error = RazorpayError()
    assert checkout(razorpay).status_code == 503
    razorpay["provider"].subscriptions.clear()
    assert refresh(razorpay).status_code == 409
    assert checkout(razorpay).status_code == 409


def test_callback_is_owned_signed_and_mandate_authorization_alone_does_not_grant(razorpay):
    sub = checkout(razorpay).json()["subscription_id"]
    assert callback(razorpay, sub, signature="0" * 64).status_code == 400
    assert callback({**razorpay, "headers": auth(razorpay, "bob")}, sub).status_code == 404
    razorpay["provider"].subscriptions[sub]["status"] = "authenticated"
    assert callback(razorpay, sub).status_code == 200
    assert me(razorpay)["effective_plan"] == "free"
    razorpay["provider"].paid_cycle(sub, amount=100)
    assert callback(razorpay, sub).status_code == 200
    assert me(razorpay)["effective_plan"] == "free"


def test_only_paid_invoice_and_captured_owned_payment_grant_finite_access(razorpay):
    sub = checkout(razorpay).json()["subscription_id"]
    invoice, payment = razorpay["provider"].paid_cycle(sub)
    assert callback(razorpay, sub).status_code == 200
    user = me(razorpay)
    assert user["effective_plan"] == "pro" and user["plan_source"] == "razorpay"
    assert user["plan_expires_at"] and user["subscription_status"] == "none"
    payment["amount_refunded"] = 99900
    assert refresh(razorpay).status_code == 200
    assert me(razorpay)["effective_plan"] == "free"


@pytest.mark.parametrize(
    "target,field,value",
    [
        ("invoice", "subscription_id", "sub_other"),
        ("invoice", "status", "issued"),
        ("invoice", "billing_end", None),
        ("invoice", "currency", "USD"),
        ("payment", "invoice_id", "inv_other"),
        ("payment", "status", "authorized"),
        ("payment", "captured", False),
        ("payment", "amount", 100),
        ("payment", "amount_refunded", 1),
        ("payment", "currency", "USD"),
    ],
)
def test_unverified_or_wrong_payment_cannot_grant(razorpay, target, field, value):
    sub = checkout(razorpay).json()["subscription_id"]
    invoice, payment = razorpay["provider"].paid_cycle(sub)
    (invoice if target == "invoice" else payment)[field] = value
    assert refresh(razorpay).status_code == 200
    assert me(razorpay)["effective_plan"] == "free"


def test_pending_lifecycle_blocks_duplicate_purchase_and_retains_paid_cycle(razorpay):
    sub = checkout(razorpay).json()["subscription_id"]
    invoice, _ = razorpay["provider"].paid_cycle(sub)
    razorpay["provider"].subscriptions[sub]["status"] = "pending"
    assert refresh(razorpay).json()["subscription"]["status"] == "pending"
    assert me(razorpay)["effective_plan"] == "pro"
    assert checkout(razorpay).status_code == 409
    invoice.update(billing_start=int(time.time()) - 86400 * 30, billing_end=int(time.time()) - 1)
    assert refresh(razorpay).json()["subscription"]["status"] == "pending"
    assert me(razorpay)["effective_plan"] == "free"
    assert checkout(razorpay).status_code == 409


def test_webhook_signature_dedup_and_live_state_override_stale_payload(razorpay):
    sub = checkout(razorpay).json()["subscription_id"]
    razorpay["provider"].paid_cycle(sub)
    assert (
        razorpay["client"]
        .post("/api/v1/billing/razorpay/webhook", json={"event": "subscription.charged"})
        .status_code
        == 400
    )
    assert webhook(razorpay, sub).status_code == 200
    calls = len(razorpay["provider"].calls)
    assert webhook(razorpay, sub).status_code == 200
    assert len(razorpay["provider"].calls) == calls
    assert me(razorpay)["effective_plan"] == "pro"
    razorpay["provider"].subscriptions[sub]["status"] = "cancelled"
    assert webhook(razorpay, sub, "subscription.authenticated", created_at=1).status_code == 200
    assert (
        razorpay["client"]
        .get("/api/v1/billing/status", headers=razorpay["headers"])
        .json()["subscription"]["status"]
        == "cancelled"
    )
    assert me(razorpay)["effective_plan"] == "pro"
    with psycopg.connect(razorpay["db"]["owner"]) as conn:
        assert conn.execute("SELECT count(*) FROM razorpay_events").fetchone()[0] == 2


def test_webhook_provider_failure_rolls_back_event_and_retries(razorpay):
    sub = checkout(razorpay).json()["subscription_id"]
    razorpay["provider"].paid_cycle(sub)
    razorpay["provider"].fetch_error = True
    assert webhook(razorpay, sub).status_code == 503
    with psycopg.connect(razorpay["db"]["owner"]) as conn:
        assert conn.execute("SELECT count(*) FROM razorpay_events").fetchone()[0] == 0
    razorpay["provider"].fetch_error = False
    assert webhook(razorpay, sub).status_code == 200
    assert me(razorpay)["effective_plan"] == "pro"


def test_webhook_recovers_lost_create_response_using_owned_attempt_notes(razorpay):
    razorpay["provider"].create_error = RazorpayError()
    assert checkout(razorpay).status_code == 503
    razorpay["provider"].paid_cycle("sub_1")
    assert webhook(razorpay, "sub_1").status_code == 200
    assert me(razorpay)["effective_plan"] == "pro"


def test_admin_override_and_plan_snapshot_survive_reconciliation(razorpay):
    sub = checkout(razorpay).json()["subscription_id"]
    razorpay["provider"].paid_cycle(sub)
    with psycopg.connect(razorpay["db"]["owner"]) as conn:
        conn.execute("UPDATE plans SET razorpay_plan_id=NULL WHERE slug='pro'")
        conn.execute(
            "UPDATE users SET plan_source='admin',plan_slug='plus' WHERE id=%s",
            (razorpay["users"]["alice"]["id"],),
        )
    assert refresh(razorpay).status_code == 200
    assert me(razorpay)["effective_plan"] == "plus"


def test_cancel_stops_renewal_without_removing_already_paid_access(razorpay):
    sub = checkout(razorpay).json()["subscription_id"]
    razorpay["provider"].paid_cycle(sub)
    response = razorpay["client"].post(
        "/api/v1/billing/cancel", json={}, headers=razorpay["headers"]
    )
    assert response.status_code == 200, response.text
    assert response.json()["subscription"]["cancel_at_period_end"]
    assert response.json()["subscription"]["status"] == "cancelled"
    assert not response.json()["subscription"]["cancellation_pending"]
    assert not response.json()["subscription"]["can_cancel"]
    assert me(razorpay)["effective_plan"] == "pro"
    assert (
        razorpay["client"]
        .post("/api/v1/billing/cancel", json={}, headers=razorpay["headers"])
        .status_code
        == 200
    )
    assert (
        len(
            [
                call
                for call in razorpay["provider"].calls
                if call[0] == "POST" and call[1].endswith("/cancel")
            ]
        )
        == 1
    )


def test_lost_cancel_response_stays_pending_and_refresh_proves_outcome(razorpay, monkeypatch):
    sub = checkout(razorpay).json()["subscription_id"]
    provider = razorpay["provider"]
    provider.paid_cycle(sub)
    assert refresh(razorpay).status_code == 200
    original = provider.post

    def lose_response(path, data):
        result = original(path, data)
        if path.endswith("/cancel"):
            provider.fetch_error = True
            raise RazorpayError()
        return result

    monkeypatch.setattr(provider, "post", lose_response)
    response = razorpay["client"].post(
        "/api/v1/billing/cancel", json={}, headers=razorpay["headers"]
    )
    assert response.status_code == 503
    status = (
        razorpay["client"]
        .get("/api/v1/billing/status", headers=razorpay["headers"])
        .json()["subscription"]
    )
    assert status["cancellation_pending"] and not status["cancel_at_period_end"]
    assert not status["can_resume_checkout"] and not status["can_cancel"]
    assert checkout(razorpay).status_code == 409
    assert me(razorpay)["effective_plan"] == "pro"
    provider.fetch_error = False
    status = refresh(razorpay).json()["subscription"]
    assert status["status"] == "cancelled" and status["cancel_at_period_end"]
    assert not status["cancellation_pending"]
    assert (
        len([call for call in provider.calls if call[0] == "POST" and call[1].endswith("/cancel")])
        == 1
    )


def test_cancel_confirmation_survives_invoice_refresh_failure(razorpay, monkeypatch):
    sub = checkout(razorpay).json()["subscription_id"]
    provider = razorpay["provider"]
    provider.paid_cycle(sub)
    assert refresh(razorpay).status_code == 200
    original = provider.items

    def invoice_outage(path, *, params=None):
        if path == "invoices":
            raise RazorpayError()
        return original(path, params=params)

    monkeypatch.setattr(provider, "items", invoice_outage)
    response = razorpay["client"].post(
        "/api/v1/billing/cancel", json={}, headers=razorpay["headers"]
    )
    assert response.status_code == 503
    status = (
        razorpay["client"]
        .get("/api/v1/billing/status", headers=razorpay["headers"])
        .json()["subscription"]
    )
    assert status["status"] == "cancelled" and status["cancel_at_period_end"]
    assert status["cancellation_pending"]
    assert checkout(razorpay).status_code == 409
    assert me(razorpay)["effective_plan"] == "pro"
    monkeypatch.setattr(provider, "items", original)
    refreshed = refresh(razorpay)
    assert refreshed.status_code == 200
    assert not refreshed.json()["subscription"]["cancellation_pending"]
    assert (
        len([call for call in provider.calls if call[0] == "POST" and call[1].endswith("/cancel")])
        == 1
    )


def test_cancel_with_unconfirmed_paid_cycle_blocks_new_checkout_until_invoice_recovery(
    razorpay, monkeypatch
):
    sub = checkout(razorpay).json()["subscription_id"]
    provider = razorpay["provider"]
    provider.paid_cycle(sub)
    original = provider.items

    def invoice_outage(path, *, params=None):
        if path == "invoices":
            raise RazorpayError()
        return original(path, params=params)

    monkeypatch.setattr(provider, "items", invoice_outage)
    result = razorpay["client"].post("/api/v1/billing/cancel", json={}, headers=razorpay["headers"])
    assert result.status_code == 503
    status = (
        razorpay["client"]
        .get("/api/v1/billing/status", headers=razorpay["headers"])
        .json()["subscription"]
    )
    assert status["status"] == "cancelled" and status["current_period_end"] is None
    assert status["cancellation_pending"] and not status["can_cancel"]
    assert checkout(razorpay).status_code == 409
    assert checkout(razorpay, "plus").status_code == 409
    assert len(provider.subscriptions) == 1
    monkeypatch.setattr(provider, "items", original)
    recovered = refresh(razorpay)
    assert recovered.status_code == 200
    status = recovered.json()["subscription"]
    assert not status["cancellation_pending"] and status["cancel_at_period_end"]
    assert me(razorpay)["effective_plan"] == "pro"


def test_existing_subscription_can_be_cancelled_when_webhook_configuration_is_missing(razorpay):
    checkout(razorpay)
    razorpay["settings"].razorpay_webhook_secret = SecretStr("")
    result = razorpay["client"].post("/api/v1/billing/cancel", json={}, headers=razorpay["headers"])
    assert result.status_code == 200 and result.json()["subscription"]["status"] == "cancelled"


def test_created_checkout_cancelled_immediately_and_replacement_is_owned(razorpay):
    sub = checkout(razorpay).json()["subscription_id"]
    response = razorpay["client"].post(
        "/api/v1/billing/cancel", json={}, headers=razorpay["headers"]
    )
    assert response.status_code == 200
    assert response.json()["subscription"]["status"] == "cancelled"
    cancel_call = next(
        call
        for call in razorpay["provider"].calls
        if call[0] == "POST" and call[1].endswith("/cancel")
    )
    assert cancel_call[2] == {"cancel_at_cycle_end": False}
    next_checkout = checkout(razorpay, "plus")
    assert next_checkout.status_code == 200 and next_checkout.json()["subscription_id"] != sub


def test_expired_invoices_do_not_fetch_historical_payments(razorpay):
    sub = checkout(razorpay).json()["subscription_id"]
    invoice, _ = razorpay["provider"].paid_cycle(sub)
    invoice.update(billing_start=int(time.time()) - 86400 * 31, billing_end=int(time.time()) - 1)
    assert refresh(razorpay).status_code == 200
    assert not any(
        call[0] == "GET" and call[1].startswith("payments/") for call in razorpay["provider"].calls
    )
