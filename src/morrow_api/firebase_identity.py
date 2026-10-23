"""Firebase verifies identity; PostgreSQL remains authoritative for access and data."""

import os
import secrets
import threading
import time
from uuid import uuid4

import firebase_admin
import psycopg
from fastapi import APIRouter, HTTPException, Request, Response
from firebase_admin import auth, exceptions
from google.auth.exceptions import GoogleAuthError
from pydantic import EmailStr, Field, TypeAdapter
from requests import RequestException

from .schemas import StrictModel
from .security import attach_session, check_login_limit, create_session, token_hash


class FirebaseToken(StrictModel):
    id_token: str = Field(min_length=20, max_length=16384)


class FirebaseIdentity:
    def __init__(self, settings):
        if os.getenv("FIREBASE_AUTH_EMULATOR_HOST"):
            raise ValueError("Firebase emulator tokens are not accepted by this API")
        self.project_id = settings.firebase_project_id
        self.public_config = {
            "apiKey": settings.firebase_api_key,
            "authDomain": settings.firebase_auth_domain,
            "projectId": self.project_id,
            "appId": settings.firebase_app_id,
        }
        if not all(self.public_config.values()):
            raise ValueError(
                "Firebase mode requires FIREBASE_PROJECT_ID, API_KEY, AUTH_DOMAIN, and APP_ID"
            )
        self._app = None
        self._lock = threading.Lock()

    def app(self):
        try:
            with self._lock:
                if self._app is None:
                    self._app = firebase_admin.initialize_app(
                        options={"projectId": self.project_id, "httpTimeout": 5},
                        name="morrow-" + uuid4().hex,
                    )
                return self._app
        except (ValueError, GoogleAuthError, OSError):
            raise HTTPException(503, "Identity service is unavailable. Please try again.") from None

    def close(self):
        if self._app is not None:
            firebase_admin.delete_app(self._app)
            self._app = None

    def verify_token(self, token):
        try:
            claims = auth.verify_id_token(token, app=self.app(), check_revoked=True)
        except (
            auth.InvalidIdTokenError,
            auth.RevokedIdTokenError,
            auth.UserDisabledError,
            auth.UserNotFoundError,
            ValueError,
        ):
            raise HTTPException(401, "Sign in again to continue.") from None
        except (exceptions.FirebaseError, GoogleAuthError, RequestException):
            raise HTTPException(503, "Identity service is unavailable. Please try again.") from None
        try:
            uid = claims["uid"]
            email = str(TypeAdapter(EmailStr).validate_python(claims["email"])).lower()
            auth_time = claims["auth_time"]
            if not isinstance(uid, str) or not 1 <= len(uid) <= 128:
                raise ValueError()
            if not isinstance(auth_time, int) or isinstance(auth_time, bool):
                raise ValueError()
            if not 0 <= time.time() - auth_time <= 300:
                raise ValueError()
            if claims.get("firebase", {}).get("sign_in_provider") not in ("password", "google.com"):
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise HTTPException(401, "Sign in again to continue.") from None
        if claims.get("email_verified") is not True:
            raise HTTPException(403, "Verify your email before signing in.")
        return {
            "uid": uid,
            "email": email,
            "auth_time": auth_time,
            "name": str(claims.get("name") or "")[:100],
        }

    def get_user(self, uid):
        try:
            return auth.get_user(uid, app=self.app())
        except (auth.UserNotFoundError, auth.UserDisabledError, ValueError):
            raise HTTPException(401, "Sign in again to continue.") from None
        except (exceptions.FirebaseError, GoogleAuthError, RequestException):
            raise HTTPException(503, "Identity service is unavailable. Please try again.") from None

    def check_session(self, uid, auth_time, email, project_id):
        if not uid or not auth_time or project_id != self.project_id:
            raise HTTPException(401, "Sign in again to continue.")
        record = self.get_user(uid)
        if (
            record.disabled
            or not record.email_verified
            or (record.email or "").lower() != email
            or record.tokens_valid_after_timestamp > auth_time * 1000
        ):
            raise HTTPException(401, "Your sign-in has changed. Sign in again to continue.")


def firebase_router(settings, pool, provider):
    router = APIRouter(prefix="/api/v1/auth")

    @router.get("/options")
    def options(request: Request):
        csrf = secrets.token_urlsafe(32)
        request.session["firebase_csrf"] = csrf
        return {
            "provider": "firebase",
            "email": True,
            "google": True,
            "firebase": provider.public_config,
            "csrf_token": csrf,
        }

    @router.post("/firebase/session")
    def exchange(data: FirebaseToken, request: Request, response: Response):
        # This is a browser session exchange, not a general bearer-token endpoint.
        csrf = request.session.get("firebase_csrf", "")
        if (
            request.headers.get("origin") != settings.public_origin
            or not csrf
            or not secrets.compare_digest(csrf, request.headers.get("x-csrf-token", ""))
        ):
            raise HTTPException(403, "Refresh the sign-in page and try again.")
        check_login_limit(
            pool,
            "firebase:" + token_hash(data.id_token),
            request.client.host if request.client else "unknown",
            ip_limit=settings.auth_ip_attempt_limit,
        )
        identity = provider.verify_token(data.id_token)
        try:
            with pool.connection() as conn, conn.transaction():
                # Serialize each identity so parallel exchanges cannot create two tenants.
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("firebase:" + identity["uid"],),
                )
                user = conn.execute(
                    "SELECT * FROM users WHERE firebase_uid=%s FOR UPDATE", (identity["uid"],)
                ).fetchone()
                if not user:
                    if conn.execute(
                        "SELECT 1 FROM users WHERE email=%s", (identity["email"],)
                    ).fetchone():
                        raise HTTPException(
                            409,
                            {
                                "code": "account_requires_link",
                                "message": "This email already has a workspace. An administrator must link it before Firebase sign-in.",
                            },
                        )
                    user = conn.execute(
                        "INSERT INTO users(id,tenant_id,email,name,email_verified,firebase_uid,firebase_project_id) VALUES (%s,%s,%s,%s,true,%s,%s) RETURNING *",
                        (
                            uuid4(),
                            uuid4(),
                            identity["email"],
                            identity["name"],
                            identity["uid"],
                            provider.project_id,
                        ),
                    ).fetchone()
                if user["firebase_project_id"] != provider.project_id:
                    raise HTTPException(
                        409,
                        "This workspace belongs to another Firebase project. Contact your administrator.",
                    )
                if not user["active"]:
                    raise HTTPException(403, "This account has been disabled.")
                # A verified email change keeps the same tenant; it never claims another one.
                if user["email"] != identity["email"]:
                    conn.execute(
                        "UPDATE users SET email=%s WHERE id=%s", (identity["email"], user["id"])
                    )
                    conn.execute("DELETE FROM auth_sessions WHERE user_id=%s", (user["id"],))
                session = create_session(
                    conn, settings, user["id"], firebase_auth_time=identity["auth_time"]
                )
        except psycopg.errors.UniqueViolation:
            raise HTTPException(
                409,
                "This identity conflicts with an existing workspace. Contact your administrator.",
            ) from None
        request.session.pop("firebase_csrf", None)
        attach_session(response, session, settings)
        return session

    return router
