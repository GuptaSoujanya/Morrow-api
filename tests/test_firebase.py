import copy
import time
from types import SimpleNamespace

import psycopg
import pytest
from conftest import PASSWORD
from fastapi import HTTPException
from fastapi.testclient import TestClient
from firebase_admin import auth, exceptions

from morrow_api.app import create_app
from morrow_api.cli import link_firebase_account
from morrow_api.firebase_identity import FirebaseIdentity
from morrow_api.security import check_login_limit, token_hash


@pytest.fixture
def firebase(setup, monkeypatch):
    state = {
        "claims": {
            "uid": "firebase-user-1",
            "email": "firebase@example.com",
            "name": "Firebase User",
            "email_verified": True,
            "auth_time": int(time.time()),
            "firebase": {"sign_in_provider": "password"},
            "role": "admin",
            "plan": "pro",
        },
        "verify_error": None,
        "user_error": None,
        "verifications": [],
        "user": SimpleNamespace(
            uid="firebase-user-1",
            email="firebase@example.com",
            email_verified=True,
            disabled=False,
            tokens_valid_after_timestamp=0,
        ),
    }

    def verify(token, *, app, check_revoked):
        assert app.project_id == "morrow-fixture"
        state["verifications"].append((token, check_revoked))
        if state["verify_error"]:
            raise state["verify_error"]
        return copy.deepcopy(state["claims"])

    def get_user(uid, *, app):
        assert app.project_id == "morrow-fixture"
        if state["user_error"]:
            raise state["user_error"]
        assert uid == state["user"].uid
        return state["user"]

    monkeypatch.setattr(auth, "verify_id_token", verify)
    monkeypatch.setattr(auth, "get_user", get_user)
    settings = setup["settings"].model_copy(
        update={
            "auth_provider": "firebase",
            "firebase_project_id": "morrow-fixture",
            "firebase_api_key": "public-fixture-key",
            "firebase_auth_domain": "morrow-fixture.firebaseapp.com",
            "firebase_app_id": "public-fixture-app",
        }
    )
    app = create_app(settings)
    with TestClient(app) as client:
        yield {
            **setup,
            "client": client,
            "app": app,
            "settings": settings,
            "firebase": state,
            "local_client": setup["client"],
        }


def exchange(fixture):
    client = fixture["client"]
    options = client.get("/api/v1/auth/options").json()
    return client.post(
        "/api/v1/auth/firebase/session",
        json={"id_token": "synthetic-firebase-token-123456"},
        headers={
            "Origin": fixture["settings"].public_origin,
            "X-CSRF-Token": options["csrf_token"],
        },
    )


def test_firebase_options_exchange_and_uid_account_are_private_and_least_privileged(firebase):
    client = firebase["client"]
    options = client.get("/api/v1/auth/options").json()
    assert options["provider"] == "firebase"
    assert options["google"] is True
    assert set(options["firebase"]) == {"apiKey", "authDomain", "projectId", "appId"}
    response = exchange(firebase)
    assert response.status_code == 200, response.text
    assert firebase["firebase"]["verifications"][0][1] is True
    assert any(
        "HttpOnly" in cookie
        for cookie in response.headers.get_list("set-cookie")
        if "morrow_session" in cookie
    )
    user = client.get("/api/v1/me").json()
    assert user["email"] == "firebase@example.com"
    assert user["role"] == "user" and user["effective_plan"] == "free"
    assert "firebase_uid" not in user and "firebase_auth_time" not in user
    assert exchange(firebase).status_code == 200
    assert client.get("/api/v1/me").json()["tenant_id"] == user["tenant_id"]
    with psycopg.connect(firebase["db"]["owner"]) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM users WHERE firebase_uid='firebase-user-1'"
            ).fetchone()[0]
            == 1
        )
        stored = conn.execute(
            "SELECT firebase_auth_time FROM auth_sessions WHERE user_id=%s", (user["id"],)
        ).fetchall()
        assert all(row[0] == firebase["firebase"]["claims"]["auth_time"] for row in stored)


def test_firebase_exchange_requires_origin_and_prelogin_csrf(firebase):
    client = firebase["client"]
    options = client.get("/api/v1/auth/options").json()
    body = {"id_token": "synthetic-firebase-token-123456"}
    for headers in (
        {},
        {"Origin": firebase["settings"].public_origin},
        {"Origin": "https://other.invalid", "X-CSRF-Token": options["csrf_token"]},
    ):
        assert (
            client.post("/api/v1/auth/firebase/session", json=body, headers=headers).status_code
            == 403
        )
    headers = {"Origin": firebase["settings"].public_origin, "X-CSRF-Token": options["csrf_token"]}
    assert (
        client.post("/api/v1/auth/firebase/session", json=body, headers=headers).status_code == 200
    )
    assert (
        client.post("/api/v1/auth/firebase/session", json=body, headers=headers).status_code == 403
    )
    assert len(firebase["firebase"]["verifications"]) == 1


@pytest.mark.parametrize(
    "changed,status",
    [
        ({"email_verified": False}, 403),
        ({"auth_time": int(time.time()) - 600}, 401),
        ({"auth_time": int(time.time()) + 3600}, 401),
        ({"auth_time": "recent"}, 401),
        ({"firebase": {"sign_in_provider": "anonymous"}}, 401),
        ({"email": "invalid"}, 401),
    ],
)
def test_firebase_rejects_unverified_stale_or_unsupported_identity(firebase, changed, status):
    firebase["firebase"]["claims"].update(changed)
    assert exchange(firebase).status_code == status
    with psycopg.connect(firebase["db"]["owner"]) as conn:
        assert (
            conn.execute("SELECT count(*) FROM users WHERE firebase_uid IS NOT NULL").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "error,status",
    [
        (auth.InvalidIdTokenError("bad signature"), 401),
        (auth.RevokedIdTokenError("revoked"), 401),
        (auth.UserDisabledError("disabled"), 401),
        (auth.CertificateFetchError("provider unavailable", None), 503),
        (exceptions.UnavailableError("provider credentials must never leak"), 503),
    ],
)
def test_firebase_sdk_rejections_fail_closed_without_leaking_details(firebase, error, status):
    firebase["firebase"]["verify_error"] = error
    response = exchange(firebase)
    assert response.status_code == status
    assert str(error) not in response.text
    assert firebase["client"].get("/api/v1/me").status_code == 401


def test_firebase_never_claims_existing_email_or_accepts_local_login(firebase):
    firebase["firebase"]["claims"]["email"] = "admin@example.com"
    response = exchange(firebase)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "account_requires_link"
    client = firebase["client"]
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "admin@example.com", "password": PASSWORD}
        ).status_code
        == 409
    )
    for path in ("register", "forgot-password", "reset-password", "resend-verification", "verify"):
        assert client.post("/api/v1/auth/" + path, json={}).status_code == 404
    assert client.get("/api/v1/auth/google/start").status_code == 404
    local = (
        firebase["local_client"]
        .post("/api/v1/auth/login", json={"email": "alice@example.com", "password": PASSWORD})
        .json()
    )
    assert (
        client.get(
            "/api/v1/me", headers={"Authorization": "Bearer " + local["access_token"]}
        ).status_code
        == 401
    )
    with psycopg.connect(firebase["db"]["owner"]) as conn:
        assert (
            conn.execute(
                "SELECT firebase_uid FROM users WHERE email='admin@example.com'"
            ).fetchone()[0]
            is None
        )


@pytest.mark.parametrize(
    "change", ["revoked", "disabled", "unverified", "deleted", "email_changed"]
)
def test_firebase_revocation_password_reset_and_account_changes_end_postgres_sessions(
    firebase, change
):
    session = exchange(firebase).json()
    provider = firebase["firebase"]
    if change == "revoked":
        provider["user"].tokens_valid_after_timestamp = (provider["claims"]["auth_time"] + 1) * 1000
    elif change == "disabled":
        provider["user"].disabled = True
    elif change == "unverified":
        provider["user"].email_verified = False
    elif change == "deleted":
        provider["user_error"] = auth.UserNotFoundError("removed")
    else:
        provider["user"].email = "changed@example.com"
    assert firebase["client"].get("/api/v1/me").status_code == 401
    with psycopg.connect(firebase["db"]["owner"]) as conn:
        assert not conn.execute(
            "SELECT 1 FROM auth_sessions WHERE token_hash=%s",
            (token_hash(session["access_token"]),),
        ).fetchone()


def test_firebase_outage_denies_data_but_allows_csrf_protected_logout(firebase):
    session = exchange(firebase).json()
    firebase["firebase"]["user_error"] = exceptions.UnavailableError("private provider details")
    client = firebase["client"]
    response = client.get("/api/v1/profile")
    assert response.status_code == 503 and "private provider details" not in response.text
    assert client.post("/api/v1/auth/logout").status_code == 403
    assert (
        client.post(
            "/api/v1/auth/logout", headers={"X-CSRF-Token": session["csrf_token"]}
        ).status_code
        == 204
    )
    assert client.get("/api/v1/me").status_code == 401


def test_firebase_role_and_subscription_remain_database_authoritative(firebase):
    assert exchange(firebase).status_code == 200
    client = firebase["client"]
    user_id = client.get("/api/v1/me").json()["id"]
    assert client.get("/api/v1/modules/automations").status_code == 403
    with psycopg.connect(firebase["db"]["owner"]) as conn:
        conn.execute("UPDATE users SET plan_slug='pro',plan_source='admin' WHERE id=%s", (user_id,))
    assert client.get("/api/v1/modules/automations").status_code == 200
    assert (
        client.get(
            "/api/v1/admin/users",
            headers={"X-Admin-Reason": "Check unauthorized administrator access"},
        ).status_code
        == 403
    )
    with psycopg.connect(firebase["db"]["owner"]) as conn:
        conn.execute("UPDATE users SET active=false WHERE id=%s", (user_id,))
    assert client.get("/api/v1/me").status_code == 401
    assert exchange(firebase).status_code == 403


def test_explicit_firebase_link_preserves_existing_owner_tenant_and_requires_email_change_flag(
    firebase, monkeypatch
):
    monkeypatch.setenv("DATABASE_OWNER_URL", firebase["db"]["owner"])
    old = firebase["users"]["admin"]
    with pytest.raises(ValueError, match="--new-email"):
        link_firebase_account(
            "admin@example.com",
            "firebase-user-1",
            "Migrate the local owner identity",
            firebase["settings"],
        )
    linked = link_firebase_account(
        "admin@example.com",
        "firebase-user-1",
        "Migrate the local owner identity",
        firebase["settings"],
        "firebase@example.com",
    )
    assert linked["tenant_id"] == old["tenant_id"] and linked["id"] == old["id"]
    assert linked["role"] == "admin"
    with psycopg.connect(firebase["db"]["owner"]) as conn:
        row = conn.execute(
            "SELECT password_hash,google_sub,firebase_uid FROM users WHERE id=%s", (old["id"],)
        ).fetchone()
        assert row == (None, None, "firebase-user-1")
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_events WHERE action='admin.firebase.link'"
            ).fetchone()[0]
            == 1
        )
    assert exchange(firebase).status_code == 200
    assert firebase["client"].get("/api/v1/me").json()["tenant_id"] == str(old["tenant_id"])


def test_firebase_configuration_never_accepts_emulator_tokens(firebase, monkeypatch):
    monkeypatch.setenv("FIREBASE_AUTH_EMULATOR_HOST", "127.0.0.1:9099")
    with pytest.raises(ValueError, match="emulator"):
        FirebaseIdentity(firebase["settings"])


def test_firebase_project_changes_cannot_reuse_an_existing_identity(firebase):
    assert exchange(firebase).status_code == 200
    firebase["app"].state.firebase.project_id = "another-project"
    assert firebase["client"].get("/api/v1/me").status_code == 401
    assert exchange(firebase).status_code == 409


def test_gateway_limit_allows_a_hundred_users_but_preserves_identifier_throttling(setup):
    pool = setup["app"].state.pool
    limit = setup["settings"].auth_ip_attempt_limit
    assert limit == 1000
    for i in range(100):
        check_login_limit(pool, f"user-{i}@example.com", "shared-gateway", ip_limit=limit)
    for _ in range(10):
        check_login_limit(pool, "repeated@example.com", "shared-gateway", ip_limit=limit)
    with pytest.raises(HTTPException) as error:
        check_login_limit(pool, "repeated@example.com", "shared-gateway", ip_limit=limit)
    assert error.value.status_code == 429
