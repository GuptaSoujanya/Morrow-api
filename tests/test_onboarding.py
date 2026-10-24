from uuid import uuid4

import psycopg
import pytest
from conftest import auth

from morrow_api.db import tenant_transaction
from morrow_api.onboarding import missing_profile_fields

PROFILE = {
    "full_name": "Alice Example",
    "email": "alice@example.com",
    "phone": "+91 99999 00000",
    "current_location": "Bengaluru",
    "titles": ["Software engineer"],
}


def draft(**changes):
    return {"expected_version": 0, "platforms": ["naukri", "google"], **changes}


def upload_resume(setup, headers, *, body=b"%PDF-1.7\nsynthetic resume", **changes):
    params = {"filename": "resume.pdf", "category": "resumes", **changes}
    response = setup["client"].post(
        "/api/v1/artifacts",
        params=params,
        content=body,
        headers={**headers, "Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def save_profile(setup, headers, data=None, version=0):
    response = setup["client"].put(
        "/api/v1/profile",
        headers=headers,
        json={"expected_version": version, "data": PROFILE if data is None else data},
    )
    assert response.status_code == 200, response.text


def test_defaults_are_private_authenticated_and_not_browser_verification(setup):
    client = setup["client"]
    assert client.get("/api/v1/onboarding").status_code == 401
    headers = auth(setup)
    response = client.get("/api/v1/onboarding", headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    state = response.json()
    assert state == {
        "version": 0,
        "platforms": [],
        "resume_artifact_id": None,
        "resume": None,
        "profile": {"version": 0, "data": {}, "updated_at": None},
        "account_hints": {},
        "completed_at": None,
        "readiness": {
            "profile_complete": False,
            "resume_ready": False,
            "missing_profile_fields": list(PROFILE),
        },
    }


def test_tenant_isolation_and_optimistic_updates_preserve_saved_data(setup):
    client = setup["client"]
    alice, bob = auth(setup), auth(setup, "bob")
    response = client.put(
        "/api/v1/onboarding",
        headers=alice,
        json=draft(account_hints={"naukri": "  alice@example.com  "}),
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 1
    assert response.json()["account_hints"] == {"naukri": "alice@example.com"}
    assert client.get("/api/v1/onboarding", headers=bob).json()["version"] == 0
    assert client.put("/api/v1/onboarding", headers=bob, json=draft()).status_code == 200
    update = draft(expected_version=1, platforms=["linkedin"])
    assert client.put("/api/v1/onboarding", headers=alice, json=update).status_code == 200
    assert client.put("/api/v1/onboarding", headers=alice, json=update).status_code == 409
    assert client.put("/api/v1/onboarding", headers=alice, json=draft()).status_code == 409
    saved = client.get("/api/v1/onboarding", headers=alice).json()
    assert saved["platforms"] == ["linkedin"] and saved["version"] == 2
    assert client.get("/api/v1/onboarding", headers=bob).json()["platforms"] == ["naukri", "google"]


@pytest.mark.parametrize(
    "fields",
    [
        {"password": "must-not-be-saved"},
        {"cookies": [{"name": "session", "value": "must-not-be-saved"}]},
        {"storage_state": {}},
        {"verified": True},
        {"status": "connected"},
        {"tenant_id": str(uuid4())},
        {"platforms": ["untrusted-platform"]},
        {"platforms": ["google", "google"]},
        {"account_hints": {"password": "must-not-be-saved"}},
        {"account_hints": {"google": {"email": "alice@example.com", "verified": True}}},
        {"account_hints": {"google": "x" * 255}},
        {"account_hints": {"google": "alice\nexample"}},
        {"completed": "true"},
    ],
)
def test_unknown_providers_secrets_and_client_verification_are_rejected(setup, fields):
    headers = auth(setup)
    response = setup["client"].put("/api/v1/onboarding", headers=headers, json=draft(**fields))
    assert response.status_code == 422
    assert "must-not-be-saved" not in response.text
    assert setup["client"].get("/api/v1/onboarding", headers=headers).json()["version"] == 0


def test_private_resume_selection_completion_and_missing_resume(setup):
    client, alice = setup["client"], auth(setup)
    save_profile(setup, alice)
    artifact = upload_resume(setup, alice)
    payload = draft(resume_artifact_id=artifact["id"], completed=True)
    first = client.put("/api/v1/onboarding", headers=alice, json=payload)
    assert first.status_code == 200, first.text
    state = first.json()
    assert state["resume"] == artifact
    assert state["completed_at"]
    assert state["readiness"] == {
        "profile_complete": True,
        "resume_ready": True,
        "missing_profile_fields": [],
    }
    assert "object_key" not in first.text
    next_save = client.put(
        "/api/v1/onboarding", headers=alice, json={**payload, "expected_version": 1}
    ).json()
    assert next_save["completed_at"] == state["completed_at"]
    assert client.delete("/api/v1/artifacts/" + artifact["id"], headers=alice).status_code == 204
    stale = client.get("/api/v1/onboarding", headers=alice).json()
    assert stale["resume"] is None
    assert stale["resume_artifact_id"] == artifact["id"]
    assert stale["readiness"]["resume_ready"] is False
    assert stale["completed_at"] == state["completed_at"]
    clear = client.put("/api/v1/onboarding", headers=alice, json=draft(expected_version=2))
    assert clear.status_code == 200
    assert clear.json()["resume_artifact_id"] is None
    assert clear.json()["completed_at"] is None


def test_selected_resume_must_belong_to_requesting_tenant(setup):
    client = setup["client"]
    alice, bob, admin = auth(setup), auth(setup, "bob"), auth(setup, "admin")
    artifact = upload_resume(setup, alice)
    for stranger in (bob, admin):
        response = client.put(
            "/api/v1/onboarding",
            headers=stranger,
            json=draft(resume_artifact_id=artifact["id"]),
        )
        assert response.status_code == 404
        assert client.get("/api/v1/onboarding", headers=stranger).json()["version"] == 0


@pytest.mark.parametrize(
    "upload_args", [{"category": "document"}, {"filename": "resume.txt"}, {"body": b"not a PDF"}]
)
def test_invalid_resume_types_cannot_be_selected(setup, upload_args):
    headers = auth(setup)
    artifact = upload_resume(setup, headers, **upload_args)
    response = setup["client"].put(
        "/api/v1/onboarding", headers=headers, json=draft(resume_artifact_id=artifact["id"])
    )
    assert response.status_code == 422


def test_completion_requires_profile_resume_and_preferences(setup):
    client, headers = setup["client"], auth(setup)
    assert (
        client.put("/api/v1/onboarding", headers=headers, json=draft(completed=True)).status_code
        == 422
    )
    save_profile(setup, headers)
    assert (
        client.put("/api/v1/onboarding", headers=headers, json=draft(completed=True)).status_code
        == 422
    )
    artifact = upload_resume(setup, headers)
    assert (
        client.put(
            "/api/v1/onboarding",
            headers=headers,
            json=draft(completed=True, platforms=[], resume_artifact_id=artifact["id"]),
        ).status_code
        == 422
    )
    # An incomplete draft is still saveable, so users can return to setup later.
    assert (
        client.put("/api/v1/onboarding", headers=headers, json=draft(platforms=[])).status_code
        == 200
    )


def test_readiness_rechecks_current_profile_without_rewriting_saved_setup(setup):
    client, headers = setup["client"], auth(setup)
    save_profile(setup, headers)
    assert client.put("/api/v1/onboarding", headers=headers, json=draft()).status_code == 200
    changed = {**PROFILE, "phone": "", "titles": []}
    save_profile(setup, headers, changed, version=1)
    state = client.get("/api/v1/onboarding", headers=headers).json()
    assert state["profile"]["version"] == 2
    assert state["readiness"]["missing_profile_fields"] == ["phone", "titles"]
    assert state["version"] == 1


def test_cookie_csrf_and_origin_apply_to_onboarding(setup):
    client = setup["client"]
    auth(setup)
    assert client.put("/api/v1/onboarding", json=draft()).status_code == 403
    csrf = client.cookies["morrow_csrf"]
    assert (
        client.put(
            "/api/v1/onboarding",
            json=draft(),
            headers={"X-CSRF-Token": csrf, "Origin": "https://attacker.invalid"},
        ).status_code
        == 403
    )
    assert (
        client.put("/api/v1/onboarding", json=draft(), headers={"X-CSRF-Token": csrf}).status_code
        == 200
    )


def test_onboarding_rls_and_generic_record_routes_cannot_mutate_state(setup):
    headers = auth(setup)
    client = setup["client"]
    assert client.put("/api/v1/onboarding", headers=headers, json=draft()).status_code == 200
    assert (
        client.put(
            "/api/v1/records/onboarding/state",
            headers=headers,
            json={"data": {"completed_at": "fake", "verified": True}},
        ).status_code
        == 200
    )
    assert client.get("/api/v1/onboarding", headers=headers).json()["completed_at"] is None
    pool = setup["app"].state.pool
    with tenant_transaction(pool, setup["users"]["bob"]["tenant_id"]) as conn:
        assert conn.execute("SELECT * FROM onboarding_state").fetchall() == []
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO onboarding_state(tenant_id,version,payload) VALUES (%s,1,'{}')",
                (setup["users"]["alice"]["tenant_id"],),
            )
    with pool.connection() as conn:
        assert conn.execute("SELECT * FROM onboarding_state").fetchall() == []


@pytest.mark.parametrize("value", [None, "", [], [""], [1], "Engineer"])
def test_target_titles_must_be_saved_as_nonempty_list(value):
    assert missing_profile_fields({**PROFILE, "titles": value}) == ["titles"]


def test_email_and_required_profile_values_are_validated():
    assert missing_profile_fields({**PROFILE, "email": "not an email", "phone": 123}) == [
        "email",
        "phone",
    ]
