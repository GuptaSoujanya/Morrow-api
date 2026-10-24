from test_razorpay_billing import checkout, me
from test_razorpay_billing import razorpay as razorpay

from morrow_api.reconciliation import reconcile_billing


def test_scheduled_reconciliation_recovers_a_missed_payment_event(razorpay):
    subscription = checkout(razorpay).json()["subscription_id"]
    razorpay["provider"].paid_cycle(subscription)
    assert me(razorpay)["effective_plan"] == "free"
    result = reconcile_billing(razorpay["settings"], razorpay["app"].state.pool)
    assert result == {"checked": 1, "synced": 1, "failed": 0}
    assert me(razorpay)["effective_plan"] == "pro"


def test_scheduled_provider_outage_reports_failure_without_granting_access(razorpay):
    checkout(razorpay)
    razorpay["provider"].fetch_error = True
    result = reconcile_billing(razorpay["settings"], razorpay["app"].state.pool)
    assert result == {"checked": 1, "synced": 0, "failed": 1}
    assert me(razorpay)["effective_plan"] == "free"
