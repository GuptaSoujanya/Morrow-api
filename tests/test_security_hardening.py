from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from conftest import PASSWORD, auth
from fastapi.testclient import TestClient

from morrow_api.app import create_app
from morrow_api.storage import LocalStorage, StorageError, validate_key

REASON = "Support access review"


def admin_routes(setup):
    bob = setup["users"]["bob"]["id"]
    plan = setup["db"]["plans"][0]["slug"]
    access = {"role": "user", "plan": plan, "active": True}
    edit = {"name": "Edited", "description": "d", "modules": ["profile", "applications", "files"]}
    return [
        ("GET", "/api/v1/admin/users", None),
        ("GET", f"/api/v1/admin/users/{bob}/profile", None),
        ("GET", f"/api/v1/admin/users/{bob}/applications", None),
        ("GET", f"/api/v1/admin/users/{bob}/records", None),
        ("GET", f"/api/v1/admin/users/{bob}/artifacts", None),
        ("GET", f"/api/v1/admin/users/{bob}/artifacts/{uuid4()}/content", None),
        ("GET", "/api/v1/admin/audit-events", None),
        ("PATCH", f"/api/v1/admin/users/{bob}/access", access),
        ("GET", "/api/v1/admin/plans", None),
        ("PUT", f"/api/v1/admin/plans/{plan}", edit),
    ]


def test_every_admin_route_rejects_anonymous_users_members_and_blank_reasons(setup):
    client = setup["client"]
    alice, admin = auth(setup), auth(setup, "admin")
    client.cookies.clear()
    for method, path, body in admin_routes(setup):
        anonymous = client.request(method, path, json=body, headers={"X-Admin-Reason": REASON})
        assert anonymous.status_code == 401, (method, path, anonymous.text)
        member = client.request(
            method, path, json=body, headers={**alice, "X-Admin-Reason": REASON}
        )
        assert member.status_code == 403, (method, path, member.text)
        unexplained = client.request(method, path, json=body, headers=admin)
        assert unexplained.status_code == 422, (method, path, unexplained.text)
        short = client.request(method, path, json=body, headers={**admin, "X-Admin-Reason": "why"})
        assert short.status_code == 422, (method, path, short.text)


def test_every_admin_route_rejects_reasons_with_control_characters(setup):
    client = setup["client"]
    admin = auth(setup, "admin")
    for method, path, body in admin_routes(setup):
        for reason in ("Support\x1b[31m access review", "Support\taccess review"):
            response = client.request(
                method, path, json=body, headers={**admin, "X-Admin-Reason": reason}
            )
            assert response.status_code == 422, (method, path, repr(reason), response.text)
    with psycopg.connect(setup["db"]["owner"]) as conn:
        assert conn.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 0


def test_every_admin_route_audits_a_well_formed_request(setup):
    client = setup["client"]
    admin = auth(setup, "admin")
    for method, path, body in admin_routes(setup):
        response = client.request(
            method, path, json=body, headers={**admin, "X-Admin-Reason": REASON}
        )
        assert response.status_code in (200, 404), (method, path, response.text)
    with psycopg.connect(setup["db"]["owner"]) as conn:
        reasons = {
            row[0] for row in conn.execute("SELECT DISTINCT reason FROM audit_events").fetchall()
        }
        count = conn.execute("SELECT count(*) FROM audit_events").fetchone()[0]
    assert reasons == {REASON}
    assert count >= len(admin_routes(setup)) - 1


def test_a_member_cannot_read_or_delete_another_tenants_objects(setup):
    client = setup["client"]
    alice, bob = auth(setup), auth(setup, "bob")
    artifact = client.post(
        "/api/v1/artifacts?filename=resume.pdf",
        content=b"alice only",
        headers={**alice, "Content-Type": "application/octet-stream"},
    ).json()["id"]
    application = client.post(
        "/api/v1/applications",
        headers={**alice, "Idempotency-Key": "one"},
        json={"platform": "naukri", "external_job_id": "j1", "status": "applied"},
    ).json()["id"]
    client.put("/api/v1/records/notes/shared", headers=alice, json={"data": {"owner": "alice"}})

    assert client.get(f"/api/v1/applications/{application}", headers=bob).status_code == 404
    assert client.get(f"/api/v1/artifacts/{artifact}/content", headers=bob).status_code == 404
    assert client.delete(f"/api/v1/artifacts/{artifact}", headers=bob).status_code == 404
    assert client.get("/api/v1/records?collection=notes", headers=bob).json()["items"] == []

    assert (
        client.get(f"/api/v1/artifacts/{artifact}/content", headers=alice).content == b"alice only"
    )
    assert client.get(f"/api/v1/applications/{application}", headers=alice).status_code == 200
    mine = client.get("/api/v1/records?collection=notes", headers=alice).json()["items"]
    assert [row["data"] for row in mine] == [{"owner": "alice"}]


def test_malformed_identifiers_are_rejected_before_any_query(setup):
    client = setup["client"]
    alice, admin = auth(setup), auth(setup, "admin")
    assert client.get("/api/v1/applications/not-a-uuid", headers=alice).status_code == 422
    assert client.get("/api/v1/artifacts/not-a-uuid/content", headers=alice).status_code == 422
    assert client.delete("/api/v1/artifacts/not-a-uuid", headers=alice).status_code == 422
    assert (
        client.get(
            "/api/v1/admin/users/not-a-uuid/profile",
            headers={**admin, "X-Admin-Reason": REASON},
        ).status_code
        == 422
    )
    assert client.get("/api/v1/records?collection=Notes", headers=alice).status_code == 422
    assert client.get("/api/v1/records?collection=../x", headers=alice).status_code == 422
    assert client.get("/api/v1/applications?limit=1000", headers=alice).status_code == 422
    assert client.get("/api/v1/applications?offset=-1", headers=alice).status_code == 422


@pytest.mark.parametrize("key", ["%00", "a%00b", "%0a", "%7f", "a%1fb"])
def test_record_keys_with_control_characters_are_rejected_not_crashed(setup, key):
    client, alice = setup["client"], auth(setup)
    response = client.put(f"/api/v1/records/notes/{key}", headers=alice, json={"data": {}})
    assert response.status_code == 422, response.text
    assert (
        client.put("/api/v1/records/no tes/key", headers=alice, json={"data": {}}).status_code
        == 422
    )
    assert (
        client.put("/api/v1/records/notes/ok-key", headers=alice, json={"data": {}}).status_code
        == 200
    )


def test_expired_sessions_and_oversized_tokens_are_refused(setup):
    client = setup["client"]
    alice = auth(setup)
    client.cookies.clear()
    assert client.get("/api/v1/me", headers=alice).status_code == 200
    with psycopg.connect(setup["db"]["owner"]) as conn:
        conn.execute("UPDATE auth_sessions SET expires_at=now()-interval '1 minute'")
    assert client.get("/api/v1/me", headers=alice).status_code == 401
    huge = {"Authorization": "Bearer " + "a" * 257}
    assert client.get("/api/v1/me", headers=huge).status_code == 401
    assert client.get("/api/v1/me", headers={"Authorization": "Basic abc"}).status_code == 401


def test_session_cookies_carry_secure_lax_and_httponly_flags(setup):
    settings = setup["settings"].model_copy(update={"cookie_secure": True})
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/auth/login", json={"email": "alice@example.com", "password": PASSWORD}
        )
        assert response.status_code == 200, response.text
        cookies = {c.split("=", 1)[0]: c for c in response.headers.get_list("set-cookie")}
        session, csrf = cookies["morrow_session"], cookies["morrow_csrf"]
        for cookie in (session, csrf):
            lowered = cookie.lower()
            assert "secure" in lowered and "samesite=lax" in lowered and "path=/" in lowered
        assert "httponly" in session.lower()
        assert "httponly" not in csrf.lower()
    plain = setup["client"].post(
        "/api/v1/auth/login", json={"email": "alice@example.com", "password": PASSWORD}
    )
    assert all("secure" not in c.lower() for c in plain.headers.get_list("set-cookie"))


def test_logout_revokes_the_session_and_clears_both_cookies(setup):
    client = setup["client"]
    alice = auth(setup)
    response = client.post("/api/v1/auth/logout", headers=alice)
    assert response.status_code == 204
    cleared = response.headers.get_list("set-cookie")
    assert any("morrow_session=" in c for c in cleared)
    assert any("morrow_csrf=" in c for c in cleared)
    assert client.get("/api/v1/me", headers=alice).status_code == 401


def test_bearer_requests_skip_csrf_but_cookie_requests_still_need_it(setup):
    # Pins the documented design: CSRF double-submit only guards cookie sessions.
    client = setup["client"]
    alice = auth(setup)
    body = {"expected_version": 0, "data": {"full_name": "Alice"}}
    client.cookies.clear()
    assert client.put("/api/v1/profile", json=body, headers=alice).status_code == 200
    auth(setup)
    assert client.put("/api/v1/profile", json={**body, "expected_version": 1}).status_code == 403
    assert (
        client.put(
            "/api/v1/profile",
            json={**body, "expected_version": 1},
            headers={"X-CSRF-Token": client.cookies["morrow_csrf"]},
        ).status_code
        == 200
    )


def test_stripe_webhook_without_a_signature_is_rejected(setup):
    client = setup["client"]
    response = client.post("/api/v1/billing/webhook", content=b'{"id":"evt_1"}')
    assert response.status_code == 400
    response = client.post(
        "/api/v1/billing/webhook",
        content=b'{"id":"evt_1"}',
        headers={"Stripe-Signature": "t=1,v1=deadbeef"},
    )
    assert response.status_code == 400
    with psycopg.connect(setup["db"]["owner"]) as conn:
        assert conn.execute("SELECT count(*) FROM stripe_events").fetchone()[0] == 0


def test_security_headers_on_every_response(setup):
    client = setup["client"]
    for response in (client.get("/health/live"), client.get("/api/v1/me")):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize(
    "key",
    [
        "",
        "../etc/passwd",
        "tenants/../../x",
        f"tenants/{uuid4()}/artifacts/{uuid4()}/../other",
        f"tenants/{uuid4()}/artifacts/{uuid4()}\n",
        f"/tenants/{uuid4()}/artifacts/{uuid4()}",
        f"tenants/{str(uuid4()).upper()}/artifacts/{uuid4()}",
        f"tenants/{uuid4()}/artifacts/{uuid4()}" + "a",
    ],
)
def test_storage_rejects_keys_that_are_not_server_generated(key):
    with pytest.raises(ValueError):
        validate_key(key)


def test_storage_refuses_symlinks_that_escape_the_root(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    outside.mkdir()
    store = LocalStorage(root, 1024)
    store.check()
    tenant = uuid4()
    (root / "tenants" / str(tenant)).mkdir(parents=True)
    (root / "tenants" / str(tenant) / "artifacts").symlink_to(outside)
    with pytest.raises(StorageError):
        store.path(f"tenants/{tenant}/artifacts/{uuid4()}")
    assert Path(store.root).is_relative_to(tmp_path)
