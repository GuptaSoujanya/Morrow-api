"""Scheduled recovery for missed Razorpay renewal/refund notifications."""

from fastapi import HTTPException

from .razorpay_billing import management_ready, refresh_billing


def reconcile_billing(settings, pool):
    if not management_ready(settings):
        raise HTTPException(503, "Configure Razorpay before running billing reconciliation")
    with pool.connection() as conn:
        accounts = conn.execute(
            "SELECT DISTINCT u.id,u.plan_slug,u.name,u.email FROM users u "
            "JOIN razorpay_subscriptions r ON r.user_id=u.id WHERE "
            "r.status NOT IN ('creation_failed','cancelled','completed','expired') "
            "OR r.paid_through>now() OR r.cancel_requested"
        ).fetchall()
    result = {"checked": len(accounts), "synced": 0, "failed": 0}
    for account in accounts:
        try:
            refresh_billing(settings, pool, account)
        except HTTPException:
            # Continue other accounts; the nonzero count makes the timer/job
            # fail visibly without printing customer records or provider data.
            result["failed"] += 1
        else:
            result["synced"] += 1
    return result
