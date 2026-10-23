import hashlib
import hmac
import json
import sqlite3
import time
from uuid import uuid4

import boto3
import psycopg
import pytest
import stripe
from conftest import auth
from moto import mock_aws

from morrow_api.config import Settings
from morrow_api.importer import import_workspace
from morrow_api.storage import S3Storage, StorageError


def configure_billing(setup):
    with psycopg.connect(setup["db"]["owner"]) as conn:
        conn.execute(
            "UPDATE users SET stripe_customer_id='cus_alice' WHERE id=%s",
            (setup["users"]["alice"]["id"],),
        )
        conn.execute("UPDATE plans SET stripe_price_id='price_pro' WHERE slug='pro'")


def test_billing_portal_remains_available_when_plans_are_not_for_sale(setup, monkeypatch):
    client = setup["client"]
    plans = client.get("/api/v1/plans").json()
    assert plans["billing_available"] is True
    assert not any(plan["checkout_available"] for plan in plans["items"])
    assert all("stripe_price_id" not in plan for plan in plans["items"])
    with psycopg.connect(setup["db"]["owner"]) as conn:
        conn.execute(
            "UPDATE users SET stripe_customer_id='cus_alice' WHERE id=%s",
            (setup["users"]["alice"]["id"],),
        )
    calls = []

    def portal(**kwargs):
        calls.append(kwargs)
        return {"url": "https://billing.stripe.com/example"}

    monkeypatch.setattr(stripe.billing_portal.Session, "create", portal)
    response = client.post("/api/v1/billing/portal", headers=auth(setup))
    assert response.status_code == 200
    assert calls[0]["customer"] == "cus_alice"


def test_admin_plan_editor_preserves_price_and_requires_audited_access(setup, monkeypatch):
    configure_billing(setup)
    client = setup["client"]
    alice = auth(setup)
    reason = {"X-Admin-Reason": "Review pricing configuration for launch"}
    assert client.get("/api/v1/admin/plans", headers={**alice, **reason}).status_code == 403
    admin = auth(setup, "admin")
    assert client.get("/api/v1/admin/plans", headers=admin).status_code == 422
    rows = client.get("/api/v1/admin/plans", headers={**admin, **reason}).json()["items"]
    pro = next(p for p in rows if p["slug"] == "pro")
    assert pro["stripe_price_id"] == "price_pro"
    monkeypatch.setattr(
        stripe.Price,
        "retrieve",
        lambda *args, **kwargs: {
            "active": True,
            "unit_amount": 99900,
            "currency": "inr",
            "recurring": {"interval": "month", "interval_count": 1},
        },
    )
    body = {key: pro[key] for key in ("name", "description", "modules", "stripe_price_id")}
    body["name"] = "Pro career"
    assert (
        client.put("/api/v1/admin/plans/pro", json=body, headers={**admin, **reason}).status_code
        == 200
    )
    with psycopg.connect(setup["db"]["owner"]) as conn:
        assert (
            conn.execute("SELECT stripe_price_id FROM plans WHERE slug='pro'").fetchone()[0]
            == "price_pro"
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_events WHERE action IN ('admin.plans.read','admin.plan.update')"
            ).fetchone()[0]
            == 2
        )


def signed_event(event):
    raw = json.dumps(event).encode()
    timestamp = int(time.time())
    signature = hmac.new(
        b"whsec_fixture", str(timestamp).encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    return raw, {
        "Stripe-Signature": f"t={timestamp},v1={signature}",
        "Content-Type": "application/json",
    }


def test_webhook_signature_replay_and_out_of_order(setup, monkeypatch):
    configure_billing(setup)
    alice = auth(setup)
    client = setup["client"]
    current = [
        {
            "id": "sub_latest",
            "created": 2,
            "status": "active",
            "items": {
                "data": [
                    {"price": {"id": "price_pro"}, "current_period_end": int(time.time()) + 86400}
                ]
            },
        }
    ]
    monkeypatch.setattr(stripe.Subscription, "list", lambda **kwargs: {"data": current})
    event = {
        "id": "evt_1",
        "type": "customer.subscription.updated",
        "data": {"object": {"id": "sub_latest", "customer": "cus_alice"}},
    }
    assert client.post("/api/v1/billing/webhook", json=event).status_code == 400
    raw, headers = signed_event(event)
    assert client.post("/api/v1/billing/webhook", content=raw, headers=headers).status_code == 200
    assert client.get("/api/v1/me", headers=alice).json()["effective_plan"] == "pro"
    assert client.post("/api/v1/billing/webhook", content=raw, headers=headers).status_code == 200
    with psycopg.connect(setup["db"]["owner"]) as conn:
        assert conn.execute("SELECT count(*) FROM stripe_events").fetchone()[0] == 1
    stale = {**event, "id": "evt_older", "type": "customer.subscription.deleted"}
    raw, headers = signed_event(stale)
    assert client.post("/api/v1/billing/webhook", content=raw, headers=headers).status_code == 200
    assert client.get("/api/v1/me", headers=alice).json()["effective_plan"] == "pro"
    current.clear()
    raw, headers = signed_event({**event, "id": "evt_cancelled"})
    assert client.post("/api/v1/billing/webhook", content=raw, headers=headers).status_code == 200
    assert client.get("/api/v1/me", headers=alice).json()["effective_plan"] == "free"


def test_checkout_does_not_grant_access_and_uses_owned_customer(setup, monkeypatch):
    configure_billing(setup)
    alice = auth(setup)
    calls = []
    monkeypatch.setattr(stripe.checkout.Session, "list", lambda **kwargs: {"data": []})

    def create(**kwargs):
        calls.append(kwargs)
        return {"url": "https://checkout.stripe.com/example"}

    monkeypatch.setattr(stripe.checkout.Session, "create", create)
    client = setup["client"]
    assert (
        client.post(
            "/api/v1/billing/checkout", headers=alice, json={"plan": "pro", "customer": "cus_bob"}
        ).status_code
        == 422
    )
    assert (
        client.post("/api/v1/billing/checkout", headers=alice, json={"plan": "pro"}).status_code
        == 200
    )
    assert calls[0]["customer"] == "cus_alice"
    assert calls[0]["line_items"] == [{"price": "price_pro", "quantity": 1}]
    assert calls[0]["success_url"] == setup["settings"].public_origin + "/#/plans?checkout=success"
    assert calls[0]["cancel_url"] == setup["settings"].public_origin + "/#/plans?checkout=cancelled"
    assert client.get("/api/v1/me", headers=alice).json()["effective_plan"] == "free"


def test_expired_assignment_and_regular_user_cannot_edit_plans(setup):
    alice = auth(setup)
    with psycopg.connect(setup["db"]["owner"]) as conn:
        conn.execute(
            "UPDATE users SET plan_slug='pro',plan_expires_at=now()-interval '1 day' WHERE id=%s",
            (setup["users"]["alice"]["id"],),
        )
    assert setup["client"].get("/api/v1/modules/research", headers=alice).status_code == 403
    assert (
        setup["client"]
        .put(
            "/api/v1/admin/plans/free",
            headers={**alice, "X-Admin-Reason": "Attempt privilege escalation"},
            json={
                "name": "Free",
                "description": "Free tier",
                "modules": ["profile", "applications", "files"],
            },
        )
        .status_code
        == 403
    )


@mock_aws
def test_s3_requires_private_bucket_and_encrypts_objects(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="private-morrow-test")
    settings = Settings(
        _env_file=None,
        database_url="postgresql://unused/unused",
        artifact_backend="s3",
        s3_bucket="private-morrow-test",
    )
    storage = S3Storage(settings)
    client.put_public_access_block(
        Bucket=settings.s3_bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": False,
            "IgnorePublicAcls": False,
            "BlockPublicPolicy": False,
            "RestrictPublicBuckets": False,
        },
    )
    with pytest.raises(StorageError):
        storage.check()
    client.put_public_access_block(
        Bucket=settings.s3_bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    storage.check()
    key = f"tenants/{uuid4()}/artifacts/{uuid4()}"
    storage.put(key, b"private")
    assert storage.get(key) == b"private"
    assert (
        client.head_object(Bucket=settings.s3_bucket, Key=key)["ServerSideEncryption"] == "AES256"
    )
    with pytest.raises(ValueError):
        storage.get("../../secret")


def test_import_is_repeatable_owner_scoped_and_skips_credentials(setup, tmp_path):
    source = tmp_path / "legacy"
    for relative in ("data/db", "data/inputs", "data/reports/day", "data/browser", ".memory"):
        (source / relative).mkdir(parents=True)
    (source / "data/inputs/profile.yaml").write_text("full_name: Legacy Person\nskills: [Python]\n")
    (source / ".env").write_text("PASSWORD=never-import")
    (source / "data/reports/day/secrets.env").write_text("TOKEN=never-import")
    (source / "data/browser/cookies.txt").write_text("private-cookie")
    (source / "data/reports/day/report.md").write_text("# Application report")
    (source / ".memory/learning.json").write_text('{"done": [1]}')
    with sqlite3.connect(source / "data/db/naukri_bot.db") as conn:
        conn.execute(
            "CREATE TABLE applications(id integer,job_id text,title text,company text,url text,status text,detail text,at text)"
        )
        conn.execute(
            "INSERT INTO applications VALUES (1,'job-1','Engineer','Example','https://example.com/job','applied','','2026-10-01T10:00:00+00:00')"
        )
    preview = import_workspace(
        source, "alice@example.com", False, setup["settings"], setup["db"]["owner"]
    )
    assert preview["tables"]["applications"] == 1
    for _ in range(2):
        import_workspace(source, "alice@example.com", True, setup["settings"], setup["db"]["owner"])
    alice, bob = auth(setup), auth(setup, "bob")
    client = setup["client"]
    assert len(client.get("/api/v1/applications", headers=alice).json()["items"]) == 1
    assert client.get("/api/v1/applications", headers=bob).json()["items"] == []
    files = client.get("/api/v1/artifacts", headers=alice).json()["items"]
    assert {item["filename"] for item in files} == {"profile.yaml", "report.md", "learning.json"}
    assert client.get("/api/v1/profile", headers=alice).json()["version"] == 1
