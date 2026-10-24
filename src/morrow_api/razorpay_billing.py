"""Owned, server-reconciled Razorpay subscriptions for monthly INR plans.

An authenticated mandate is not paid access. Only a paid subscription invoice
and its captured, unrefunded payment establish a finite paid-through date.
Official contracts: /docs/payments/subscriptions/integration-guide/ and
/docs/api/payments/subscriptions/fetch-invoices on razorpay.com.
"""

import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from .razorpay_client import RazorpayClient, RazorpayError
from .repository import audit
from .schemas import StrictModel
from .security import current_user

TERMINAL = ("creation_failed", "cancelled", "completed", "expired")
REMOTE_STATES = frozenset(
    (
        "created",
        "authenticated",
        "active",
        "pending",
        "halted",
        "paused",
        "cancelled",
        "completed",
        "expired",
    )
)
EVENTS = frozenset(
    "subscription." + state
    for state in (
        "authenticated",
        "activated",
        "charged",
        "completed",
        "updated",
        "pending",
        "halted",
        "cancelled",
        "paused",
        "resumed",
    )
) | {"invoice.paid", "payment.captured", "payment.failed", "refund.processed"}


class CheckoutVerification(StrictModel):
    razorpay_subscription_id: str = Field(pattern=r"^sub_[A-Za-z0-9]{1,80}$")
    razorpay_payment_id: str = Field(pattern=r"^pay_[A-Za-z0-9]{1,80}$")
    razorpay_signature: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


class Cancellation(StrictModel):
    at_period_end: bool = True


def ready(settings):
    return management_ready(settings) and bool(settings.razorpay_webhook_secret.get_secret_value())


def management_ready(settings):
    return bool(settings.razorpay_key_id and settings.razorpay_key_secret.get_secret_value())


def _configured(settings, *, require_webhook=True):
    if not (ready(settings) if require_webhook else management_ready(settings)):
        raise HTTPException(503, "Subscriptions are not configured yet")


def _unavailable():
    return HTTPException(
        503, "Billing could not be confirmed. Refresh billing before trying another payment."
    )


def _identifier(value, prefix):
    return isinstance(value, str) and bool(re.fullmatch(prefix + r"_[A-Za-z0-9]{1,80}", value))


def _now():
    return datetime.now(timezone.utc)


def _timestamp(value):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def validate_plan(settings, remote_id, plan):
    """Validate an administrator's mapping and revalidate before purchase."""
    _configured(settings, require_webhook=False)
    if (
        not _identifier(remote_id, "plan")
        or plan["currency"].upper() != "INR"
        or plan["monthly_amount"] <= 0
    ):
        raise HTTPException(422, "Choose a paid monthly INR plan")
    try:
        remote = RazorpayClient(settings).get("plans/" + remote_id)
    except RazorpayError:
        raise _unavailable() from None
    item = remote.get("item") or {}
    if not isinstance(item, dict):
        raise _unavailable()
    if (
        remote.get("id") != remote_id
        or remote.get("period") != "monthly"
        or remote.get("interval") != 1
        or item.get("amount") != plan["monthly_amount"]
        or item.get("currency") != "INR"
    ):
        raise HTTPException(422, "Razorpay plan must match the monthly amount and INR currency")
    return remote


def _stripe_active(account):
    return bool(
        account["stripe_subscription_id"]
        and account["subscription_status"] not in ("none", "canceled", "incomplete_expired")
    )


def _latest(conn, user_id):
    return conn.execute(
        "SELECT * FROM razorpay_subscriptions WHERE user_id=%s ORDER BY created_at DESC,id DESC LIMIT 1",
        (user_id,),
    ).fetchone()


def _checkout_options(settings, user, row):
    return {
        "provider": "razorpay",
        "key_id": settings.razorpay_key_id,
        "subscription_id": row["provider_subscription_id"],
        "plan_name": row["plan_name"],
        "name": "Morrow",
        "description": row["plan_name"] + " monthly workspace plan",
        "prefill": {"name": user.get("name", ""), "email": user["email"]},
    }


def _validate_subscription(row, remote, *, bind=False):
    remote_id = remote.get("id")
    if (
        not _identifier(remote_id, "sub")
        or remote.get("plan_id") != row["provider_plan_id"]
        or remote.get("quantity") != 1
        or remote.get("status") not in REMOTE_STATES
        or (row["provider_subscription_id"] and row["provider_subscription_id"] != remote_id)
    ):
        raise HTTPException(409, "The subscription needs a billing review before access can change")
    if bind:
        notes = remote.get("notes") or {}
        if (
            not isinstance(notes, dict)
            or notes.get("morrow_attempt") != str(row["id"])
            or notes.get("morrow_user") != str(row["user_id"])
        ):
            raise HTTPException(409, "The subscription could not be linked to this checkout")


def checkout(settings, pool, user, plan):
    _configured(settings)
    if user["role"] == "admin":
        raise HTTPException(
            409, "Administrators already have all modules and do not need a subscription"
        )
    remote_plan = plan.get("razorpay_plan_id")
    if not remote_plan or not plan.get("active", True):
        raise HTTPException(503, "This subscription is not available yet")
    with pool.connection() as conn, conn.transaction():
        account = conn.execute(
            "SELECT * FROM users WHERE id=%s FOR UPDATE", (user["id"],)
        ).fetchone()
        if account["role"] == "admin" or account["plan_source"] == "admin":
            raise HTTPException(
                409, "Your plan is managed by an administrator. Contact them before purchasing."
            )
        if _stripe_active(account):
            raise HTTPException(
                409, "Manage or cancel your existing Stripe subscription before starting another"
            )
        existing = conn.execute(
            "SELECT * FROM razorpay_subscriptions WHERE user_id=%s AND "
            "(status <> ALL(%s) OR paid_through>now() OR cancel_requested) ORDER BY created_at DESC LIMIT 1",
            (user["id"], list(TERMINAL)),
        ).fetchone()
        if existing:
            if existing["cancel_requested"]:
                raise HTTPException(
                    409,
                    "Cancellation is being confirmed. Refresh billing before starting another payment.",
                )
            if existing["plan_slug"] != plan["slug"]:
                raise HTTPException(
                    409,
                    "Finish or cancel the existing subscription before choosing a different plan",
                )
            if existing["status"] == "created" and existing["provider_subscription_id"]:
                return _checkout_options(settings, user, existing)
            if existing["status"] in ("creating", "creation_unknown"):
                raise HTTPException(
                    409,
                    "Your previous checkout is still being confirmed. Refresh billing; do not start another payment.",
                )
            raise HTTPException(
                409,
                "This account already has a subscription. Use the billing controls to manage it.",
            )
        if account["stripe_customer_id"]:
            from .billing_migration import close_stripe_checkout

            close_stripe_checkout(settings, account)
        # The checked catalog value is copied into the durable attempt. Future
        # plan-ID changes cannot reinterpret an already purchased subscription.
        validate_plan(settings, remote_plan, plan)
        attempt = conn.execute(
            "INSERT INTO razorpay_subscriptions(id,user_id,provider_plan_id,plan_slug,plan_name,monthly_amount,currency,status) "
            "VALUES (%s,%s,%s,%s,%s,%s,'INR','creating') RETURNING *",
            (uuid4(), user["id"], remote_plan, plan["slug"], plan["name"], plan["monthly_amount"]),
        ).fetchone()
        audit(
            conn,
            user["id"],
            account["tenant_id"],
            "billing.checkout.started",
            str(attempt["id"]),
            "Reserved owned Razorpay subscription checkout",
        )
    # The first transaction is committed before this non-idempotent operation.
    # Never retry a POST with an unknown outcome or release its reservation.
    try:
        remote = RazorpayClient(settings).post(
            "subscriptions",
            {
                "plan_id": remote_plan,
                "quantity": 1,
                "total_count": settings.razorpay_total_count,
                "customer_notify": True,
                "notes": {"morrow_attempt": str(attempt["id"]), "morrow_user": str(user["id"])},
            },
        )
        _validate_subscription(attempt, remote, bind=True)
    except (RazorpayError, HTTPException) as exc:
        uncertain = not isinstance(exc, RazorpayError) or exc.uncertain
        with pool.connection() as conn:
            conn.execute(
                "UPDATE razorpay_subscriptions SET status=%s,updated_at=now() WHERE id=%s AND provider_subscription_id IS NULL AND status='creating'",
                ("creation_unknown" if uncertain else "creation_failed", attempt["id"]),
            )
        raise _unavailable() from None
    with pool.connection() as conn, conn.transaction():
        conn.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (user["id"],))
        row = conn.execute(
            "SELECT * FROM razorpay_subscriptions WHERE id=%s FOR UPDATE", (attempt["id"],)
        ).fetchone()
        _validate_subscription(row, remote, bind=True)
        # A fast signed webhook may have already reconciled this reservation.
        if not row["provider_subscription_id"]:
            row = conn.execute(
                "UPDATE razorpay_subscriptions SET provider_subscription_id=%s,status=%s,updated_at=now() WHERE id=%s RETURNING *",
                (remote["id"], remote["status"], row["id"]),
            ).fetchone()
    return _checkout_options(settings, user, row)


def _paid_through(client, row):
    ends = []
    for invoice in client.items(
        "invoices", params={"subscription_id": row["provider_subscription_id"]}
    ):
        start, end = (
            _timestamp(invoice.get("billing_start")),
            _timestamp(invoice.get("billing_end")),
        )
        if (
            invoice.get("subscription_id") != row["provider_subscription_id"]
            or invoice.get("status") != "paid"
            or invoice.get("currency") != row["currency"]
            or invoice.get("amount") != row["monthly_amount"]
            or invoice.get("amount_paid") != row["monthly_amount"]
            or invoice.get("amount_due") != 0
            or not _identifier(invoice.get("id"), "inv")
            or not _identifier(invoice.get("payment_id"), "pay")
            or not start
            or not end
            or not start <= _now() < end
            or not start < end <= start + timedelta(days=35)
        ):
            continue
        payment = client.get("payments/" + invoice["payment_id"])
        if (
            payment.get("id") == invoice["payment_id"]
            and payment.get("invoice_id") == invoice["id"]
            and payment.get("status") == "captured"
            and payment.get("captured") is True
            and payment.get("currency") == row["currency"]
            and payment.get("amount") == row["monthly_amount"]
            and payment.get("amount_refunded") == 0
        ):
            ends.append(end)
    return max(ends) if ends else None


def _apply_access(conn, account):
    if account["plan_source"] == "admin" or _stripe_active(account):
        return
    paid = conn.execute(
        "SELECT r.plan_slug,r.paid_through FROM razorpay_subscriptions r JOIN plans p ON p.slug=r.plan_slug "
        "WHERE r.user_id=%s AND r.paid_through>now() AND p.active ORDER BY r.created_at DESC,r.id DESC LIMIT 1",
        (account["id"],),
    ).fetchone()
    conn.execute(
        "UPDATE users SET plan_slug=%s,plan_source='razorpay',plan_expires_at=%s WHERE id=%s",
        (
            paid["plan_slug"] if paid else "free",
            paid["paid_through"] if paid else None,
            account["id"],
        ),
    )


def _reconcile(conn, settings, account, row, *, remote=None):
    client = RazorpayClient(settings)
    if not row["provider_subscription_id"]:
        # Recover a lost create response by the opaque server attempt, never by
        # email, plan alone, a browser callback or the absence of a list result.
        matches = []
        for candidate in client.items(
            "subscriptions",
            params={
                "plan_id": row["provider_plan_id"],
                "from": int(row["created_at"].timestamp()) - 60,
            },
        ):
            notes = candidate.get("notes") or {}
            if (
                isinstance(notes, dict)
                and notes.get("morrow_attempt") == str(row["id"])
                and notes.get("morrow_user") == str(account["id"])
            ):
                matches.append(candidate)
        if len(matches) != 1:
            raise HTTPException(
                409,
                "The previous checkout is not confirmed yet. Please retry Refresh billing or contact support; another payment has not been started.",
            )
        remote = client.get("subscriptions/" + matches[0]["id"])
        _validate_subscription(row, remote, bind=True)
        row = conn.execute(
            "UPDATE razorpay_subscriptions SET provider_subscription_id=%s WHERE id=%s RETURNING *",
            (remote["id"], row["id"]),
        ).fetchone()
    remote = remote or client.get("subscriptions/" + row["provider_subscription_id"])
    _validate_subscription(row, remote)
    end = _paid_through(client, row)
    row = conn.execute(
        "UPDATE razorpay_subscriptions SET status=%s,paid_through=%s,cancel_at_period_end=%s,cancel_requested=%s,updated_at=now() WHERE id=%s RETURNING *",
        (
            remote["status"],
            end,
            bool(
                (row["cancel_requested"] or row["cancel_at_period_end"])
                and remote["status"] in TERMINAL
                and end
                and end > _now()
            ),
            row["cancel_requested"] and remote["status"] not in TERMINAL,
            row["id"],
        ),
    ).fetchone()
    _apply_access(conn, account)
    return row


def billing_status(settings, pool, user):
    with pool.connection() as conn:
        account = conn.execute("SELECT * FROM users WHERE id=%s", (user["id"],)).fetchone()
        row = _latest(conn, user["id"])
    if _stripe_active(account) or (not row and account["stripe_customer_id"]):
        return {
            "provider": "stripe",
            "configured": bool(settings.stripe_secret_key.get_secret_value()),
            "subscription": {
                "status": account["subscription_status"],
                "plan_slug": account["plan_slug"],
                "plan_name": user.get("plan_name", account["plan_slug"]),
                "current_period_end": account["plan_expires_at"],
                "cancel_at_period_end": False,
                "can_cancel": False,
                "can_resume_checkout": False,
                "cancellation_pending": False,
            },
        }
    result = {
        "provider": "razorpay" if row else settings.billing_provider,
        "configured": management_ready(settings)
        if row
        else ready(settings)
        if settings.billing_provider == "razorpay"
        else bool(settings.stripe_secret_key.get_secret_value()),
        "subscription": None,
    }
    if row:
        result["subscription"] = {
            "status": row["status"],
            "plan_slug": row["plan_slug"],
            "plan_name": row["plan_name"],
            "current_period_end": row["paid_through"],
            "cancel_at_period_end": bool(
                row["cancel_at_period_end"] and row["paid_through"] and row["paid_through"] > _now()
            ),
            "cancellation_pending": bool(row["cancel_requested"]),
            "can_cancel": bool(
                row["provider_subscription_id"]
                and row["status"] not in TERMINAL
                and not row["cancel_requested"]
            ),
            "can_resume_checkout": bool(
                row["provider_subscription_id"]
                and row["status"] == "created"
                and not row["cancel_requested"]
            ),
        }
    return result


def _resolve_cancellation(settings, pool, user_id):
    """Complete the durable stop-renewal request, independent of invoice reads.

    Razorpay does not document an unambiguous scheduled-cancellation readback.
    Cancel the mandate now and retain Morrow's previously verified paid access.
        A subsequent GET must confirm a terminal status. Keep the durable request
        pending until invoice reconciliation also succeeds, including when the
        first paid cycle has not yet reached Morrow through a webhook.
    """
    with pool.connection() as conn, conn.transaction():
        account = conn.execute("SELECT * FROM users WHERE id=%s FOR UPDATE", (user_id,)).fetchone()
        row = _latest(conn, user_id)
        if not row or not row["cancel_requested"] or row["status"] in TERMINAL:
            return
        client = RazorpayClient(settings)
        remote = client.get("subscriptions/" + row["provider_subscription_id"])
        _validate_subscription(row, remote)
        if remote["status"] not in TERMINAL:
            try:
                client.post(
                    "subscriptions/" + row["provider_subscription_id"] + "/cancel",
                    {"cancel_at_cycle_end": False},
                )
            except RazorpayError:
                # A timeout or an already-cancelled error is inconclusive. GET
                # can prove the actual outcome without another payment/change.
                pass
            remote = client.get("subscriptions/" + row["provider_subscription_id"])
            _validate_subscription(row, remote)
        if remote["status"] not in TERMINAL:
            raise _unavailable()
        conn.execute(
            "UPDATE razorpay_subscriptions SET status=%s,cancel_at_period_end=%s,updated_at=now() WHERE id=%s",
            (
                remote["status"],
                bool(row["paid_through"] and row["paid_through"] > _now()),
                row["id"],
            ),
        )
        audit(
            conn,
            user_id,
            account["tenant_id"],
            "billing.subscription.cancel",
            str(row["id"]),
            "Provider confirmed renewal stopped; retained verified paid access",
        )


def refresh_billing(settings, pool, user):
    if billing_status(settings, pool, user)["provider"] == "stripe":
        return billing_status(settings, pool, user)
    _configured(settings, require_webhook=False)
    try:
        _resolve_cancellation(settings, pool, user["id"])
        with pool.connection() as conn, conn.transaction():
            account = conn.execute(
                "SELECT * FROM users WHERE id=%s FOR UPDATE", (user["id"],)
            ).fetchone()
            row = _latest(conn, user["id"])
            if row and row["status"] != "creation_failed":
                _reconcile(conn, settings, account, row)
                audit(
                    conn,
                    user["id"],
                    account["tenant_id"],
                    "billing.subscription.refresh",
                    str(row["id"]),
                    "Retrieved current owned Razorpay subscription and paid invoices",
                )
    except RazorpayError:
        raise _unavailable() from None
    return billing_status(settings, pool, user)


def _event_subscription(client, event):
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return None, None

    def entity(name):
        wrapper = payload.get(name) or {}
        result = wrapper.get("entity") if isinstance(wrapper, dict) else None
        return result if isinstance(result, dict) else {}

    subscription = entity("subscription")
    if _identifier(subscription.get("id"), "sub"):
        return subscription["id"], subscription.get("notes")
    invoice = entity("invoice")
    if _identifier(invoice.get("subscription_id"), "sub"):
        return invoice["subscription_id"], None
    payment_id = entity("payment").get("id") or entity("refund").get("payment_id")
    if _identifier(payment_id, "pay"):
        payment = client.get("payments/" + payment_id)
        if _identifier(payment.get("invoice_id"), "inv"):
            invoice = client.get("invoices/" + payment["invoice_id"])
            if _identifier(invoice.get("subscription_id"), "sub"):
                return invoice["subscription_id"], None
    return None, None


def _process_event(settings, pool, raw):
    try:
        event = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid webhook body") from None
    if (
        not isinstance(event, dict)
        or not isinstance(event.get("event"), str)
        or event["event"] not in EVENTS
    ):
        return {"received": True}
    # The event header is not covered by Razorpay's body HMAC. Deduplicate the
    # signed bytes so changing that header cannot bypass replay protection.
    event_id = hashlib.sha256(raw).hexdigest()
    try:
        with pool.connection() as conn:
            if conn.execute("SELECT 1 FROM razorpay_events WHERE id=%s", (event_id,)).fetchone():
                return {"received": True}
            remote_id, notes = _event_subscription(RazorpayClient(settings), event)
            if not remote_id:
                return {"received": True}
            row = conn.execute(
                "SELECT * FROM razorpay_subscriptions WHERE provider_subscription_id=%s",
                (remote_id,),
            ).fetchone()
            if not row and isinstance(notes, dict):
                try:
                    attempt = UUID(notes.get("morrow_attempt", ""))
                    owner = UUID(notes.get("morrow_user", ""))
                except (ValueError, TypeError, AttributeError):
                    return {"received": True}
                row = conn.execute(
                    "SELECT * FROM razorpay_subscriptions WHERE id=%s AND user_id=%s AND provider_subscription_id IS NULL",
                    (attempt, owner),
                ).fetchone()
            if not row:
                return {"received": True}
        with pool.connection() as conn, conn.transaction():
            account = conn.execute(
                "SELECT * FROM users WHERE id=%s FOR UPDATE", (row["user_id"],)
            ).fetchone()
            row = conn.execute(
                "SELECT * FROM razorpay_subscriptions WHERE id=%s FOR UPDATE", (row["id"],)
            ).fetchone()
            if not conn.execute(
                "INSERT INTO razorpay_events(id,event_type) VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING id",
                (event_id, event["event"]),
            ).fetchone():
                return {"received": True}
            if not row["provider_subscription_id"]:
                remote = RazorpayClient(settings).get("subscriptions/" + remote_id)
                _validate_subscription(row, remote, bind=True)
                row = conn.execute(
                    "UPDATE razorpay_subscriptions SET provider_subscription_id=%s WHERE id=%s RETURNING *",
                    (remote_id, row["id"]),
                ).fetchone()
            _reconcile(conn, settings, account, row)
            audit(
                conn,
                None,
                account["tenant_id"],
                "billing.subscription.sync",
                event_id,
                "Verified Razorpay event; retrieved current subscription and paid invoices",
            )
    except RazorpayError:
        raise _unavailable() from None
    return {"received": True}


def razorpay_router(settings, pool):
    router = APIRouter(prefix="/api/v1")

    @router.get("/billing/status")
    def status(user=Depends(current_user)):
        return billing_status(settings, pool, user)

    @router.post("/billing/refresh")
    def refresh(user=Depends(current_user)):
        return refresh_billing(settings, pool, user)

    @router.post("/billing/razorpay/verify")
    def verify(data: CheckoutVerification, user=Depends(current_user)):
        _configured(settings, require_webhook=False)
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM razorpay_subscriptions WHERE user_id=%s AND provider_subscription_id=%s",
                (user["id"], data.razorpay_subscription_id),
            ).fetchone()
        if not row:
            raise HTTPException(404, "Subscription not found")
        signature = hmac.new(
            settings.razorpay_key_secret.get_secret_value().encode(),
            (data.razorpay_payment_id + "|" + row["provider_subscription_id"]).encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, data.razorpay_signature.lower()):
            raise HTTPException(400, "Invalid checkout signature")
        # A valid authentication callback alone never grants a paid plan.
        return refresh_billing(settings, pool, user)

    @router.post("/billing/cancel")
    def cancel(data: Cancellation, user=Depends(current_user)):
        _configured(settings, require_webhook=False)
        with pool.connection() as conn, conn.transaction():
            account = conn.execute(
                "SELECT * FROM users WHERE id=%s FOR UPDATE", (user["id"],)
            ).fetchone()
            if _stripe_active(account):
                raise HTTPException(
                    409, "Use Manage billing to cancel the existing Stripe subscription"
                )
            row = _latest(conn, user["id"])
            if not row or not row["provider_subscription_id"]:
                raise HTTPException(
                    409, "Refresh billing to confirm the existing checkout before cancelling"
                )
            if not row["cancel_requested"] and row["status"] not in TERMINAL:
                conn.execute(
                    "UPDATE razorpay_subscriptions SET cancel_requested=true,updated_at=now() WHERE id=%s",
                    (row["id"],),
                )
                audit(
                    conn,
                    user["id"],
                    account["tenant_id"],
                    "billing.subscription.cancel_requested",
                    str(row["id"]),
                    "Requested stop to renewal; paid access remains until its verified expiry",
                )
        # Intent is durable before the provider call. Both API choices preserve
        # already paid access; they never promise a refund or remove paid time.
        return refresh_billing(settings, pool, user)

    @router.post("/billing/razorpay/webhook")
    async def webhook(request: Request):
        _configured(settings)
        raw = await request.body()
        signature = request.headers.get("x-razorpay-signature", "")
        expected = hmac.new(
            settings.razorpay_webhook_secret.get_secret_value().encode(), raw, hashlib.sha256
        ).hexdigest()
        if not re.fullmatch(r"[a-fA-F0-9]{64}", signature) or not hmac.compare_digest(
            expected, signature.lower()
        ):
            raise HTTPException(400, "Invalid webhook signature")
        return await run_in_threadpool(_process_event, settings, pool, raw)

    return router
