import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import RedirectResponse
from joserfc.errors import JoseError
from pydantic import EmailStr, Field, TypeAdapter

from .mail import flush_mail, queue_mail
from .schemas import StrictModel
from .security import (
    attach_session,
    check_login_limit,
    create_session,
    hasher,
    token_hash,
)


class EmailInput(StrictModel):
    email: EmailStr


class Register(EmailInput):
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=12, max_length=128)


class TokenInput(StrictModel):
    token: str = Field(min_length=20, max_length=256)


class Reset(TokenInput):
    password: str = Field(min_length=12, max_length=128)


def issue_token(conn, settings, user, purpose):
    token = secrets.token_urlsafe(48)
    hours = 24 if purpose == "verify" else 1
    expires_at = datetime.now(timezone.utc) + timedelta(hours=hours)
    conn.execute("DELETE FROM auth_tokens WHERE user_id=%s AND purpose=%s", (user["id"], purpose))
    conn.execute(
        "INSERT INTO auth_tokens(token_hash,user_id,purpose,expires_at) VALUES (%s,%s,%s,%s)",
        (
            token_hash(token),
            user["id"],
            purpose,
            expires_at,
        ),
    )
    verb = "Verify your email" if purpose == "verify" else "Reset your password"
    link = f"{settings.public_origin}/#/{purpose}?token={token}"
    queue_mail(
        conn,
        user["email"],
        f"{verb} · Morrow",
        f"{verb}\n\n{link}\n\nThis link expires in {hours} hour(s) and can be used once. If you did not request this, ignore this email.",
        expires_at=expires_at,
    )


def identity_router(settings, pool, firebase=None):
    if settings.auth_provider == "firebase":
        from .firebase_identity import firebase_router

        return firebase_router(settings, pool, firebase)
    router = APIRouter(prefix="/api/v1/auth")
    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret.get_secret_value(),
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
    )

    @router.get("/options")
    def options():
        return {
            "provider": "local",
            "google": bool(
                settings.google_client_id and settings.google_client_secret.get_secret_value()
            ),
            "email": True,
        }

    @router.post("/register", status_code=202)
    def register(data: Register, request: Request, tasks: BackgroundTasks):
        email = str(data.email).lower()
        check_login_limit(
            pool, "signup:" + email, request.client.host, ip_limit=settings.auth_ip_attempt_limit
        )
        encoded = hasher.hash(data.password)
        with pool.connection() as conn, conn.transaction():
            user = conn.execute(
                "INSERT INTO users(id,tenant_id,email,name,password_hash) VALUES (%s,%s,%s,%s,%s) ON CONFLICT(email) DO NOTHING RETURNING id,email",
                (uuid4(), uuid4(), email, data.name.strip(), encoded),
            ).fetchone()
            if user:
                issue_token(conn, settings, user, "verify")
        tasks.add_task(flush_mail, pool, settings)
        return {"message": "If this email is eligible, a verification link will arrive shortly."}

    def send_link(data, request, tasks, purpose):
        email = str(data.email).lower()
        check_login_limit(
            pool,
            purpose + ":" + email,
            request.client.host,
            ip_limit=settings.auth_ip_attempt_limit,
        )
        with pool.connection() as conn, conn.transaction():
            user = conn.execute(
                "SELECT id,email,email_verified FROM users WHERE email=%s AND active", (email,)
            ).fetchone()
            if user and (purpose == "reset" or not user["email_verified"]):
                issue_token(conn, settings, user, purpose)
        tasks.add_task(flush_mail, pool, settings)
        return {"message": "If an eligible account exists, a link will arrive shortly."}

    @router.post("/forgot-password", status_code=202)
    def forgot(data: EmailInput, request: Request, tasks: BackgroundTasks):
        return send_link(data, request, tasks, "reset")

    @router.post("/resend-verification", status_code=202)
    def resend(data: EmailInput, request: Request, tasks: BackgroundTasks):
        return send_link(data, request, tasks, "verify")

    def consume(conn, token, purpose):
        row = conn.execute(
            "DELETE FROM auth_tokens WHERE token_hash=%s AND purpose=%s AND expires_at > now() RETURNING user_id",
            (token_hash(token), purpose),
        ).fetchone()
        if not row:
            raise HTTPException(400, "This link has expired or has already been used")
        return row["user_id"]

    @router.post("/verify")
    def verify(data: TokenInput):
        with pool.connection() as conn, conn.transaction():
            user_id = consume(conn, data.token, "verify")
            conn.execute("UPDATE users SET email_verified=true WHERE id=%s", (user_id,))
        return {"message": "Email verified. You can now sign in."}

    @router.post("/reset-password")
    def reset(data: Reset):
        encoded = hasher.hash(data.password)
        with pool.connection() as conn, conn.transaction():
            user_id = consume(conn, data.token, "reset")
            conn.execute(
                "UPDATE users SET password_hash=%s,email_verified=true WHERE id=%s AND active",
                (encoded, user_id),
            )
            conn.execute("DELETE FROM auth_sessions WHERE user_id=%s", (user_id,))
            conn.execute("DELETE FROM auth_tokens WHERE user_id=%s", (user_id,))
        return {"message": "Password updated. Sign in with your new password."}

    @router.get("/google/start")
    async def google_start(request: Request):
        if not settings.google_client_id or not settings.google_client_secret.get_secret_value():
            raise HTTPException(503, "Google sign-in is not available yet")
        state = secrets.token_urlsafe(32)
        with pool.connection() as conn:
            conn.execute("DELETE FROM oauth_states WHERE expires_at <= now()")
            conn.execute(
                "INSERT INTO oauth_states(state_hash,expires_at) VALUES (%s,now()+interval '10 minutes')",
                (token_hash(state),),
            )
        return await oauth.google.authorize_redirect(
            request, settings.public_origin + "/api/v1/auth/google/callback", state=state
        )

    @router.get("/google/callback")
    async def google_callback(request: Request):
        try:
            state = request.query_params.get("state", "")
            with pool.connection() as conn:
                fresh = conn.execute(
                    "DELETE FROM oauth_states WHERE state_hash=%s AND expires_at>now() RETURNING state_hash",
                    (token_hash(state),),
                ).fetchone()
            if not fresh:
                raise OAuthError("invalid_state")
            token = await oauth.google.authorize_access_token(request)
            info = token.get("userinfo", {})
            if not info.get("email_verified") or not info.get("sub"):
                raise OAuthError("unverified_email")
            email = str(TypeAdapter(EmailStr).validate_python(info["email"])).lower()
            with pool.connection() as conn, conn.transaction():
                user = conn.execute(
                    "SELECT * FROM users WHERE google_sub=%s", (info["sub"],)
                ).fetchone()
                if not user:
                    # Email ownership alone must not silently link an existing password account.
                    if conn.execute("SELECT 1 FROM users WHERE email=%s", (email,)).fetchone():
                        return RedirectResponse(settings.public_origin + "/#/login?error=use_email")
                    user = conn.execute(
                        "INSERT INTO users(id,tenant_id,email,name,email_verified,google_sub) VALUES (%s,%s,%s,%s,true,%s) RETURNING *",
                        (uuid4(), uuid4(), email, str(info.get("name", ""))[:100], info["sub"]),
                    ).fetchone()
                if not user["active"] or user["firebase_uid"]:
                    raise OAuthError("account_disabled")
                session = create_session(conn, settings, user["id"])
            response = RedirectResponse(settings.public_origin + "/#/overview")
            attach_session(response, session, settings)
            return response
        except (OAuthError, JoseError, httpx.HTTPError, ValueError, KeyError):
            return RedirectResponse(settings.public_origin + "/#/login?error=google")

    return router
