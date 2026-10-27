from datetime import datetime, timezone
from uuid import UUID

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg.types.json import Jsonb
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from . import razorpay_billing
from .entitlements import MODULES
from .repository import audit
from .schemas import StrictModel
from .security import Reason, administrator, current_user


class Checkout(StrictModel):
    plan: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")


class AccountSettings(StrictModel):
    ai_provider: str = Field(pattern=r"^(claude|kiro)$")


class UserAccess(StrictModel):
    role: str = Field(pattern=r"^(admin|user)$")
    plan: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    active: bool
    expires_at: datetime | None = None


class PlanUpdate(StrictModel):
    name: str = Field(min_length=1, max_length=50)
    description: str = Field(min_length=1, max_length=200)
    modules: list[str] = Field(max_length=20)
    stripe_price_id: str | None = Field(default=None, max_length=100)
    razorpay_plan_id: str | None = Field(
        default=None, max_length=100, pattern=r"^plan_[a-zA-Z0-9]+$"
    )


def billing_router(settings, pool):
    router = APIRouter(prefix="/api/v1")
    stripe_key = settings.stripe_secret_key.get_secret_value()

    def configured():
        if not stripe_key:
            raise HTTPException(503, "Subscriptions are temporarily unavailable")

    def checkout_ready():
        return bool(stripe_key and settings.stripe_webhook_secret.get_secret_value())

    @router.get("/plans")
    def plans():
        with pool.connection() as conn:
            rows = conn.execute(
                "SELECT slug,name,description,monthly_amount,currency,modules,stripe_price_id,razorpay_plan_id FROM plans WHERE active ORDER BY monthly_amount"
            ).fetchall()
        for row in rows:
            price_id = row.pop("stripe_price_id")
            razorpay_plan = row.pop("razorpay_plan_id")
            row["checkout_available"] = bool(
                row["monthly_amount"] > 0
                and (
                    razorpay_billing.ready(settings)
                    and razorpay_plan
                    and row["currency"].lower() == "inr"
                    if settings.billing_provider == "razorpay"
                    else checkout_ready() and price_id
                )
            )
        return {
            "items": rows,
            "modules": MODULES,
            "ai_subscription_included": False,
            "billing_available": razorpay_billing.ready(settings)
            if settings.billing_provider == "razorpay"
            else bool(stripe_key),
            "billing_provider": settings.billing_provider,
            "payment_methods": ["UPI AutoPay", "Cards"]
            if settings.billing_provider == "razorpay"
            else ["Cards"],
        }

    @router.put("/account/settings")
    def account_settings(data: AccountSettings, user=Depends(current_user)):
        with pool.connection() as conn:
            conn.execute(
                "UPDATE users SET ai_provider=%s WHERE id=%s", (data.ai_provider, user["id"])
            )
        return {
            "ai_provider": data.ai_provider,
            "connected": False,
            "message": "Preference saved. Your Claude or Kiro subscription is separate.",
        }

    @router.get("/modules/{module}")
    def module_access(module: str, user=Depends(current_user)):
        from .entitlements import require_module

        if module not in MODULES:
            raise HTTPException(404, "Module not found")
        require_module(user, module)
        return {
            "module": module,
            "allowed": True,
            "execution_available": module in ("profile", "applications", "files"),
        }

    @router.post("/billing/checkout")
    def checkout(data: Checkout, user=Depends(current_user)):
        if user["role"] == "admin":
            raise HTTPException(409, "Your administrator role already includes every module")
        if user["plan_source"] == "admin":
            raise HTTPException(
                409, "Your plan is managed by an administrator. Contact them before purchasing."
            )
        if settings.billing_provider == "razorpay":
            with pool.connection() as conn:
                plan = conn.execute(
                    "SELECT * FROM plans WHERE slug=%s AND active AND monthly_amount>0",
                    (data.plan,),
                ).fetchone()
            if not plan or not plan["razorpay_plan_id"]:
                raise HTTPException(503, "This subscription is not available yet")
            return razorpay_billing.checkout(settings, pool, user, plan)
        configured()
        if not checkout_ready():
            raise HTTPException(503, "Subscriptions are temporarily unavailable")
        with pool.connection() as conn, conn.transaction():
            account = conn.execute(
                "SELECT * FROM users WHERE id=%s FOR UPDATE", (user["id"],)
            ).fetchone()
            plan = conn.execute(
                "SELECT * FROM plans WHERE slug=%s AND active AND monthly_amount>0", (data.plan,)
            ).fetchone()
            if not plan or not plan["stripe_price_id"]:
                raise HTTPException(503, "This subscription is not available yet")
            if conn.execute(
                "SELECT 1 FROM razorpay_subscriptions WHERE user_id=%s AND "
                "(status NOT IN ('creation_failed','cancelled','completed','expired') OR paid_through>now() OR cancel_requested) LIMIT 1",
                (user["id"],),
            ).fetchone():
                raise HTTPException(
                    409, "Manage your existing subscription before starting another"
                )
            if account["stripe_subscription_id"] and account["subscription_status"] not in (
                "canceled",
                "none",
                "incomplete_expired",
            ):
                raise HTTPException(409, "Use Manage billing to change an existing subscription")
            customer = account["stripe_customer_id"]
            if not customer:
                customer = stripe.Customer.create(
                    email=user["email"],
                    metadata={"user_id": str(user["id"])},
                    api_key=stripe_key,
                    idempotency_key=f"morrow-customer-{user['id']}",
                )["id"]
                conn.execute(
                    "UPDATE users SET stripe_customer_id=%s WHERE id=%s", (customer, user["id"])
                )
            # A completed checkout can precede its webhook. Check the provider as
            # well as local state before allowing another subscription purchase.
            subscriptions = stripe.Subscription.list(
                customer=customer, status="all", limit=100, api_key=stripe_key
            )
            if any(
                sub["status"] not in ("canceled", "incomplete_expired")
                for sub in subscriptions["data"]
            ):
                raise HTTPException(409, "Use Manage billing to change an existing subscription")
            # Only reuse the requested price. Expire older links so a plan switch
            # cannot charge the previously selected amount.
            existing = stripe.checkout.Session.list(
                customer=customer, status="open", limit=10, api_key=stripe_key
            )
            for entry in existing["data"]:
                if entry.get("mode") == "subscription":
                    if entry.get("metadata", {}).get("price_id") == plan["stripe_price_id"]:
                        return {"url": entry["url"], "provider": "stripe"}
                    stripe.checkout.Session.expire(entry["id"], api_key=stripe_key)
            session = stripe.checkout.Session.create(
                mode="subscription",
                customer=customer,
                client_reference_id=str(user["id"]),
                line_items=[{"price": plan["stripe_price_id"], "quantity": 1}],
                subscription_data={"metadata": {"user_id": str(user["id"])}},
                metadata={"plan": plan["slug"], "price_id": plan["stripe_price_id"]},
                success_url=settings.public_origin + "/#/plans?checkout=success",
                cancel_url=settings.public_origin + "/#/plans?checkout=cancelled",
                api_key=stripe_key,
            )
        return {"url": session["url"], "provider": "stripe"}

    @router.post("/billing/portal")
    def portal(user=Depends(current_user)):
        configured()
        with pool.connection() as conn:
            customer = conn.execute(
                "SELECT stripe_customer_id FROM users WHERE id=%s", (user["id"],)
            ).fetchone()["stripe_customer_id"]
        if not customer:
            raise HTTPException(409, "No billing account exists yet")
        session = stripe.billing_portal.Session.create(
            customer=customer,
            return_url=settings.public_origin + "/#/plans",
            api_key=stripe_key,
        )
        return {"url": session["url"]}

    @router.post("/billing/webhook")
    async def webhook(request: Request):
        secret = settings.stripe_webhook_secret.get_secret_value()
        if not secret or not stripe_key:
            raise HTTPException(503, "Billing is not configured")
        try:
            event = stripe.Webhook.construct_event(
                await request.body(), request.headers.get("stripe-signature", ""), secret
            )
        except (ValueError, stripe.SignatureVerificationError):
            raise HTTPException(400, "Invalid webhook signature") from None
        return await run_in_threadpool(process_stripe_event, event)

    def process_stripe_event(event):
        if event["type"] not in (
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
            "checkout.session.completed",
        ):
            return {"received": True}
        obj = event["data"]["object"]
        customer = obj.get("customer")
        with pool.connection() as conn, conn.transaction():
            account = conn.execute(
                "SELECT * FROM users WHERE stripe_customer_id=%s FOR UPDATE", (customer,)
            ).fetchone()
            if not account:
                return {"received": True}
            if not conn.execute(
                "INSERT INTO stripe_events(id,event_type) VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING id",
                (event["id"], event["type"]),
            ).fetchone():
                return {"received": True}
            # Fetch current provider state under the user lock, so delayed events cannot
            # restore an expired plan or revoke a newer subscription.
            subscriptions = stripe.Subscription.list(
                customer=customer, status="all", limit=100, api_key=stripe_key
            )["data"]
            active = sorted(
                (sub for sub in subscriptions if sub["status"] in ("active", "trialing")),
                key=lambda sub: sub["created"],
                reverse=True,
            )
            current = sorted(
                (
                    sub
                    for sub in subscriptions
                    if sub["status"] not in ("canceled", "incomplete_expired")
                ),
                key=lambda sub: sub["created"],
                reverse=True,
            )
            selected = active[0] if active else (current[0] if current else None)
            plan_slug, expires, status, subscription_id = "free", None, "canceled", None
            if selected:
                status, subscription_id = selected["status"], selected["id"]
                if status in ("active", "trialing"):
                    items = selected["items"]["data"]
                    price = items[0]["price"]["id"] if len(items) == 1 else None
                    plan = conn.execute(
                        "SELECT slug FROM plans WHERE stripe_price_id=%s AND active", (price,)
                    ).fetchone()
                    if not plan:
                        raise HTTPException(409, "Subscription price is not mapped to a plan")
                    end = selected.get("current_period_end") or items[0].get("current_period_end")
                    if not end:
                        raise HTTPException(409, "Subscription billing period is not confirmed yet")
                    plan_slug = plan["slug"]
                    expires = datetime.fromtimestamp(end, timezone.utc)
            # Explicit administrator assignments take priority until changed by an admin.
            razorpay_in_use = (
                account["plan_source"] == "razorpay"
                and conn.execute(
                    "SELECT 1 FROM razorpay_subscriptions WHERE user_id=%s AND "
                    "(status NOT IN ('creation_failed','cancelled','completed','expired') OR paid_through>now() OR cancel_requested) LIMIT 1",
                    (account["id"],),
                ).fetchone()
            )
            stripe_takes_over = bool(active and not razorpay_in_use)
            if account["plan_source"] != "admin" and (
                account["plan_source"] != "razorpay" or stripe_takes_over
            ):
                conn.execute(
                    "UPDATE users SET plan_slug=%s,plan_source='stripe',plan_expires_at=%s,stripe_subscription_id=%s,subscription_status=%s WHERE id=%s",
                    (plan_slug, expires, subscription_id, status, account["id"]),
                )
            else:
                conn.execute(
                    "UPDATE users SET stripe_subscription_id=%s,subscription_status=%s WHERE id=%s",
                    (subscription_id, status, account["id"]),
                )
            audit(
                conn,
                None,
                account["tenant_id"],
                "billing.subscription.sync",
                event["id"],
                "Verified Stripe event; retrieved current subscription state",
            )
        return {"received": True}

    @router.patch("/admin/users/{user_id}/access")
    def change_access(
        user_id: UUID,
        data: UserAccess,
        reason: Reason,
        user=Depends(administrator),
    ):
        if data.expires_at and data.expires_at.tzinfo is None:
            raise HTTPException(422, "Expiry must include a timezone")
        with pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(775012)")
            target = conn.execute(
                "SELECT * FROM users WHERE id=%s FOR UPDATE", (user_id,)
            ).fetchone()
            if not target:
                raise HTTPException(404, "User not found")
            if (
                target["role"] == "admin"
                and target["active"]
                and (data.role != "admin" or not data.active)
            ):
                if (
                    conn.execute(
                        "SELECT count(*) AS n FROM users WHERE role='admin' AND active AND email_verified"
                    ).fetchone()["n"]
                    <= 1
                ):
                    raise HTTPException(409, "Keep at least one verified active administrator")
            if not conn.execute(
                "SELECT 1 FROM plans WHERE slug=%s AND active", (data.plan,)
            ).fetchone():
                raise HTTPException(422, "Unknown plan")
            conn.execute(
                "UPDATE users SET role=%s,plan_slug=%s,plan_source='admin',plan_expires_at=%s,active=%s WHERE id=%s",
                (data.role, data.plan, data.expires_at, data.active, user_id),
            )
            if not data.active:
                conn.execute("DELETE FROM auth_sessions WHERE user_id=%s", (user_id,))
            audit(
                conn, user["id"], target["tenant_id"], "admin.access.update", str(user_id), reason
            )
        return {"ok": True}

    @router.get("/admin/plans")
    def admin_plans(
        reason: Reason,
        user=Depends(administrator),
    ):
        with pool.connection() as conn, conn.transaction():
            rows = conn.execute("SELECT * FROM plans ORDER BY monthly_amount").fetchall()
            audit(conn, user["id"], None, "admin.plans.read", "plans", reason)
        return {"items": rows, "billing_provider": settings.billing_provider}

    @router.put("/admin/plans/{slug}")
    def edit_plan(
        slug: str,
        data: PlanUpdate,
        reason: Reason,
        user=Depends(administrator),
    ):
        if not set(data.modules) <= MODULES.keys() or not {
            "profile",
            "applications",
            "files",
        } <= set(data.modules):
            raise HTTPException(
                422, "Keep the three core modules and choose only supported modules"
            )
        with pool.connection() as conn, conn.transaction():
            plan = conn.execute("SELECT * FROM plans WHERE slug=%s FOR UPDATE", (slug,)).fetchone()
            if not plan:
                raise HTTPException(404, "Plan not found")
            price_id = (
                data.stripe_price_id
                if "stripe_price_id" in data.model_fields_set
                else plan["stripe_price_id"]
            )
            if price_id and price_id != plan["stripe_price_id"]:
                configured()
                price = stripe.Price.retrieve(price_id, api_key=stripe_key)
                if (
                    not price["active"]
                    or price["unit_amount"] != plan["monthly_amount"]
                    or price["currency"] != plan["currency"]
                    or price.get("recurring", {}).get("interval") != "month"
                    or price["recurring"].get("interval_count", 1) != 1
                ):
                    raise HTTPException(
                        422, "Stripe price must match this plan's monthly amount and currency"
                    )
            razorpay_plan_id = (
                data.razorpay_plan_id
                if "razorpay_plan_id" in data.model_fields_set
                else plan["razorpay_plan_id"]
            )
            if razorpay_plan_id and razorpay_plan_id != plan["razorpay_plan_id"]:
                razorpay_billing.validate_plan(settings, razorpay_plan_id, plan)
            conn.execute(
                "UPDATE plans SET name=%s,description=%s,modules=%s,stripe_price_id=%s,razorpay_plan_id=%s WHERE slug=%s",
                (
                    data.name,
                    data.description,
                    Jsonb(sorted(set(data.modules))),
                    price_id,
                    razorpay_plan_id,
                    slug,
                ),
            )
            audit(conn, user["id"], None, "admin.plan.update", slug, reason)
        return {"ok": True}

    return router
