import re

import psycopg
import pytest
from conftest import PASSWORD, auth

from morrow_api.db import tenant_transaction


def test_service_exposes_api_without_a_second_frontend(setup):
    client = setup["client"]
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert response.json()["service"] == "Morrow API"
    assert "location" not in response.headers
    assert client.get("/account/").status_code == 404
    assert client.get("/account/main.js").status_code == 404


def test_authentication_and_revocation(setup):
    client = setup["client"]
    assert client.get("/api/v1/me").status_code == 401
    bad = client.post(
        "/api/v1/auth/login", json={"email": "alice@example.com", "password": "incorrect"}
    )
    assert bad.status_code == 401
    headers = auth(setup)
    assert client.get("/api/v1/me", headers=headers).json()["email"] == "alice@example.com"
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/v1/me", headers=headers).status_code == 401


def test_cookie_csrf_and_origin(setup):
    client = setup["client"]
    auth(setup)
    body = {"expected_version": 0, "data": {"full_name": "Alice"}}
    assert client.put("/api/v1/profile", json=body).status_code == 403
    csrf = client.cookies["morrow_csrf"]
    assert (
        client.put(
            "/api/v1/profile",
            json=body,
            headers={"X-CSRF-Token": csrf, "Origin": "https://attacker.invalid"},
        ).status_code
        == 403
    )
    assert (
        client.put(
            "/api/v1/profile",
            json=body,
            headers={"X-CSRF-Token": csrf, "Origin": setup["settings"].public_origin},
        ).status_code
        == 200
    )
    cookie = client.post(
        "/api/v1/auth/login", json={"email": "alice@example.com", "password": PASSWORD}
    ).headers.get_list("set-cookie")
    assert any("HttpOnly" in c and "morrow_session" in c for c in cookie)


def test_profile_tenant_isolation_and_version_conflict(setup):
    client = setup["client"]
    alice, bob = auth(setup), auth(setup, "bob")
    response = client.put(
        "/api/v1/profile",
        headers=alice,
        json={"expected_version": 0, "data": {"full_name": "Alice only"}},
    )
    assert response.status_code == 200
    assert client.get("/api/v1/profile", headers=bob).json()["data"] == {}
    assert (
        client.put(
            "/api/v1/profile", headers=alice, json={"expected_version": 0, "data": {}}
        ).status_code
        == 409
    )
    assert (
        client.put(
            "/api/v1/profile",
            headers=alice,
            json={
                "expected_version": 1,
                "data": {},
                "tenant_id": str(setup["users"]["bob"]["tenant_id"]),
            },
        ).status_code
        == 422
    )


def test_rls_without_application_filter_and_pool_reset(setup):
    alice = auth(setup)
    setup["client"].put(
        "/api/v1/profile", headers=alice, json={"expected_version": 0, "data": {"private": True}}
    )
    pool = setup["app"].state.pool
    with tenant_transaction(pool, setup["users"]["bob"]["tenant_id"]) as conn:
        assert conn.execute("SELECT * FROM profiles").fetchall() == []
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO profiles(tenant_id,version,payload) VALUES (%s,1,'{}')",
                (setup["users"]["alice"]["tenant_id"],),
            )
    with pool.connection() as conn:
        assert conn.execute("SELECT * FROM profiles").fetchall() == []
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM audit_events")


def test_applications_idempotency_and_ownership(setup):
    client = setup["client"]
    alice, bob = auth(setup), auth(setup, "bob")
    body = {
        "platform": "naukri",
        "external_job_id": "job-1",
        "status": "applied",
        "title": "Engineer",
    }
    headers = {**alice, "Idempotency-Key": "same-command"}
    first = client.post("/api/v1/applications", headers=headers, json=body)
    assert first.status_code == 201, first.text
    assert (
        client.post("/api/v1/applications", headers=headers, json=body).json()["id"]
        == first.json()["id"]
    )
    assert (
        client.post(
            "/api/v1/applications", headers=headers, json={**body, "title": "Changed"}
        ).status_code
        == 409
    )
    assert client.get("/api/v1/applications/" + first.json()["id"], headers=bob).status_code == 404
    assert client.get("/api/v1/applications", headers=bob).json()["items"] == []


def test_private_artifacts_and_download_headers(setup):
    client = setup["client"]
    alice, bob = auth(setup), auth(setup, "bob")
    created = client.post(
        "/api/v1/artifacts?filename=resume.pdf",
        content=b"private pdf",
        headers={**alice, "Content-Type": "application/octet-stream"},
    )
    assert created.status_code == 201, created.text
    assert "object_key" not in created.json()
    path = "/api/v1/artifacts/" + created.json()["id"]
    assert client.get(path + "/content", headers=bob).status_code == 404
    response = client.get(path + "/content", headers=alice)
    assert response.content == b"private pdf"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert client.delete(path, headers=bob).status_code == 404
    assert client.delete(path, headers=alice).status_code == 204
    assert client.get(path + "/content", headers=alice).status_code == 404


def test_upload_limits_and_filename(setup):
    client, headers = setup["client"], auth(setup)
    headers["Content-Type"] = "application/octet-stream"
    assert (
        client.post(
            "/api/v1/artifacts?filename=a", content=b"x" * 1025, headers=headers
        ).status_code
        == 413
    )
    assert (
        client.post(
            "/api/v1/artifacts?filename=../secret", content=b"x", headers=headers
        ).status_code
        == 422
    )

    def chunks():
        yield b"x" * 600
        yield b"x" * 600

    assert (
        client.post("/api/v1/artifacts?filename=a", content=chunks(), headers=headers).status_code
        == 413
    )


def test_admin_access_requires_role_reason_and_audits(setup):
    client = setup["client"]
    alice, admin = auth(setup), auth(setup, "admin")
    path = f"/api/v1/admin/users/{setup['users']['alice']['id']}/profile"
    assert (
        client.get(path, headers={**alice, "X-Admin-Reason": "Support access review"}).status_code
        == 403
    )
    assert client.get(path, headers=admin).status_code == 422
    assert (
        client.get(path, headers={**admin, "X-Admin-Reason": "Support access review"}).status_code
        == 200
    )
    with psycopg.connect(setup["db"]["owner"]) as conn:
        row = conn.execute(
            "SELECT actor_id,target_tenant_id,reason FROM audit_events WHERE action='admin.profile.read'"
        ).fetchone()
    assert row == (
        setup["users"]["admin"]["id"],
        setup["users"]["alice"]["tenant_id"],
        "Support access review",
    )


def mail_token(setup, purpose):
    from email import policy
    from email.parser import BytesParser

    bodies = [
        BytesParser(policy=policy.default)
        .parsebytes(p.read_bytes())
        .get_body(preferencelist=("plain",))
        .get_content()
        for p in setup["settings"].mail_root.glob("*.eml")
    ]
    match = re.findall(
        re.escape(setup["settings"].public_origin + "/#/" + purpose) + r"\?token=([A-Za-z0-9_-]+)",
        "\n".join(bodies),
    )
    assert match
    return match[-1]


def test_signup_verification_and_one_time_reset(setup):
    client = setup["client"]
    signup = {"email": "new@example.com", "name": "New User", "password": PASSWORD}
    assert client.post("/api/v1/auth/register", json={**signup, "role": "admin"}).status_code == 422
    assert client.post("/api/v1/auth/register", json=signup).status_code == 202
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": signup["email"], "password": PASSWORD}
        ).status_code
        == 403
    )
    token = mail_token(setup, "verify")
    assert client.post("/api/v1/auth/verify", json={"token": token}).status_code == 200
    assert client.post("/api/v1/auth/verify", json={"token": token}).status_code == 400
    login = client.post(
        "/api/v1/auth/login", json={"email": signup["email"], "password": PASSWORD}
    ).json()
    assert (
        client.post("/api/v1/auth/forgot-password", json={"email": signup["email"]}).status_code
        == 202
    )
    token = mail_token(setup, "reset")
    assert (
        client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "password": "New-test-password-123!"},
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/v1/me", headers={"Authorization": "Bearer " + login["access_token"]}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/auth/reset-password", json={"token": token, "password": PASSWORD}
        ).status_code
        == 400
    )


def test_expired_tokens_and_generic_recovery_response(setup):
    client = setup["client"]
    a = client.post("/api/v1/auth/forgot-password", json={"email": "alice@example.com"})
    b = client.post("/api/v1/auth/forgot-password", json={"email": "absent@example.com"})
    assert a.json() == b.json()
    token = mail_token(setup, "reset")
    with psycopg.connect(setup["db"]["owner"]) as conn:
        conn.execute("UPDATE auth_tokens SET expires_at=now()-interval '1 minute'")
    assert (
        client.post(
            "/api/v1/auth/reset-password", json={"token": token, "password": PASSWORD}
        ).status_code
        == 400
    )


def test_plan_enforcement_changes_without_relogin(setup):
    client = setup["client"]
    alice, admin = auth(setup), auth(setup, "admin")
    assert client.get("/api/v1/modules/research", headers=alice).status_code == 403
    assert (
        client.put("/api/v1/records/research/one", headers=alice, json={"data": {}}).status_code
        == 403
    )
    response = client.patch(
        f"/api/v1/admin/users/{setup['users']['alice']['id']}/access",
        headers={**admin, "X-Admin-Reason": "Assign paid access for support"},
        json={"role": "user", "plan": "plus", "active": True},
    )
    assert response.status_code == 200, response.text
    assert client.get("/api/v1/modules/research", headers=alice).status_code == 200
    assert client.get("/api/v1/modules/automations", headers=alice).status_code == 403
    assert client.get("/api/v1/modules/automations", headers=admin).status_code == 200


def test_last_admin_and_disabled_user(setup):
    client = setup["client"]
    alice, admin = auth(setup), auth(setup, "admin")
    headers = {**admin, "X-Admin-Reason": "Account access maintenance"}
    data = {"role": "user", "plan": "free", "active": False}
    assert (
        client.patch(
            f"/api/v1/admin/users/{setup['users']['admin']['id']}/access",
            headers=headers,
            json=data,
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"/api/v1/admin/users/{setup['users']['alice']['id']}/access",
            headers=headers,
            json=data,
        ).status_code
        == 200
    )
    assert client.get("/api/v1/me", headers=alice).status_code == 401


def test_login_throttling_and_no_password_in_validation(setup):
    client = setup["client"]
    for _ in range(10):
        assert (
            client.post(
                "/api/v1/auth/login", json={"email": "no@example.com", "password": "bad"}
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "no@example.com", "password": "bad"}
        ).status_code
        == 429
    )
    secret = "x" * 1025
    assert (
        secret
        not in client.post("/api/v1/auth/login", json={"email": "bad", "password": secret}).text
    )


def test_google_missing_config_and_forged_callback(setup):
    client = setup["client"]
    assert client.get("/api/v1/auth/options").json()["google"] is False
    assert client.get("/api/v1/auth/google/start").status_code == 503
    reply = client.get(
        "/api/v1/auth/google/callback?state=forged&code=forged", follow_redirects=False
    )
    assert reply.status_code == 307
    assert "error=google" in reply.headers["location"]


def test_google_verified_identity_is_single_use_and_does_not_link_by_email(setup, monkeypatch):
    from uuid import uuid4

    from authlib.integrations.starlette_client import StarletteOAuth2App

    from morrow_api.security import token_hash

    info = {"sub": "google-subject", "email": "google@example.com", "email_verified": True}

    async def authorized_identity(self, request):
        # The provider adapter has already checked Google's signed ID token and nonce.
        return {"userinfo": info}

    monkeypatch.setattr(StarletteOAuth2App, "authorize_access_token", authorized_identity)
    client = setup["client"]

    def callback():
        state = uuid4().hex
        with psycopg.connect(setup["db"]["owner"]) as conn:
            conn.execute(
                "INSERT INTO oauth_states(state_hash,expires_at) VALUES (%s,now()+interval '10 minutes')",
                (token_hash(state),),
            )
        path = "/api/v1/auth/google/callback?state=" + state + "&code=adapter-fixture"
        return client.get(path, follow_redirects=False), path

    reply, path = callback()
    assert reply.headers["location"] == setup["settings"].public_origin + "/#/overview"
    assert client.get("/api/v1/me").json()["email"] == "google@example.com"
    client.cookies.clear()
    assert "error=google" in client.get(path, follow_redirects=False).headers["location"]
    assert client.get("/api/v1/me").status_code == 401
    info.update(sub="another-subject", email="alice@example.com")
    reply, _ = callback()
    assert "error=use_email" in reply.headers["location"]
    assert client.get("/api/v1/me").status_code == 401
    info.update(sub="unverified-subject", email="unverified@example.com", email_verified=False)
    reply, _ = callback()
    assert "error=google" in reply.headers["location"]
